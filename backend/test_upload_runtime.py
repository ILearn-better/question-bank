# -*- coding: utf-8 -*-
"""R3 的**运行时**验证：同步上传端点真的读到了「正确的那些字节」。

为什么静态检查不够（`test_upload_async.py` 只管形状）：
    那条测试能证明「是同步 def / 没 await / 没残留 `file.read(`」，但它**证明不了**
    `file.file.read(n)` 读的是不是真的那个文件对象。而这正是 R3 最实际的风险 ——
    把 `file.file.read(n)` 写成 `file.read(n)`（FastAPI 的 `read` 是 **async**，同步 def 里
    只会拿到一个**协程对象**），或者拿到的是另一个句柄：请求照样可能 200，
    落盘的文件却是空的 / 坏的 / 是上一份的。老师看到的是「上传成功」。
    这种错**只有发一次真请求、把落盘字节和上传字节逐字节比一次**才看得出来。

所以这个测试自带服务、自带数据目录，跑完自己关：
    cd backend
    ./.venv/Scripts/python.exe test_upload_runtime.py

⚠️ 8899 是**故意**跟真服务（8000 / 8010）和 scratch 服务（8001）都岔开的端口，不是打错：
   这个测试要写库、要往 uploads / crops 落文件，必须待在自己那套临时 DB + 临时 data 上。
   `SHIKE_DB_PATH` / `SHIKE_DATA_DIR` 一旦忘了设，它会往老师的真库真目录里写东西 ——
   所以那两个环境变量在**任何 app 模块被 import 之前**就设好（见下面 ⓪）。

判据：
  ① 图片上传 200
  ② 落盘字节 == 上传字节（核心：抓 file.file / file 搞混）
  ③ 返回的 url 真能取回同一份字节（证明给出的地址不是死链）
  ④ 非图片字节 → 422，且**一个文件都不落**（校验没被改坏）
  ⑤ 文档上传 200，落盘字节 == 上传字节（第二个入口，走的是另一条 read/落盘路径）
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ---------------------------------------------------------------- ⓪ 先隔离，再 import
SCRATCH = Path(tempfile.mkdtemp(prefix="shike_upload_runtime_"))
os.environ["SHIKE_DB_PATH"] = str(SCRATCH / "runtime.db")
os.environ["SHIKE_DATA_DIR"] = str(SCRATCH / "data")
# 这两个必须在 import app.config 之前设 —— config 在 import 期就把路径算成常量了，
# 晚一步设等于没设（而且不会有任何报错，只会往真目录写）。
BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import config  # noqa: E402  只为拿 UPLOAD_DIR / CROPS_DIR（不连库）

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"
SERVER_LOG = SCRATCH / "uvicorn.log"

# 本机 shell 带 http_proxy：裸 urlopen 连 127.0.0.1 会被送进代理回 502（契约第六节第 2 条）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

fails: list[str] = []


def check(label: str, got, want=True) -> None:
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


# ---------------------------------------------------------------- 小工具
def multipart(field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    head = (f"--{b}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + data + f"\r\n--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def post(path: str, body: bytes, ctype: str) -> tuple[int, object]:
    r = urllib.request.Request(BASE + path, data=body, method="POST",
                               headers={"Content-Type": ctype})
    try:
        with _OPENER.open(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:                              # noqa: BLE001  422 的 body 可能不是 JSON
            return e.code, raw.decode("utf-8", "replace")


def get_raw(path: str) -> tuple[int, bytes]:
    try:
        with _OPENER.open(BASE + path, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def new_files(d: Path, before: set[str]) -> list[Path]:
    return sorted(p for p in d.glob("*") if p.is_file() and p.name not in before)


def png_bytes() -> bytes:
    """真 PNG（8x6 纯色）——魔数校验与 PyMuPDF 都得认它。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


def start_server() -> subprocess.Popen:
    """起一个临时服务，等它真的能应答。"""
    log = open(SERVER_LOG, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BACKEND), stdout=log, stderr=subprocess.STDOUT, env=os.environ.copy(),
    )
    deadline = time.time() + 120          # 空库首次启动要跑 alembic 迁移，慢一点不能算挂
    while time.time() < deadline:
        if proc.poll() is not None:
            log.close()
            tail = SERVER_LOG.read_text(encoding="utf-8", errors="replace")[-2000:]
            print(f"  [FAIL] 临时服务起不来（exit={proc.returncode}）：\n{tail}")
            raise SystemExit(1)
        try:
            with _OPENER.open(BASE + "/api/documents", timeout=2):
                log.close()
                return proc
        except urllib.error.HTTPError:
            log.close()                   # 能回 4xx/5xx 就说明已经在应答了
            return proc
        except Exception:                 # noqa: BLE001  还没起来
            time.sleep(0.5)
    proc.terminate()
    log.close()
    print("  [FAIL] 等了 120 秒临时服务还没应答，放弃。")
    raise SystemExit(1)


# ---------------------------------------------------------------- 跑
print(f"临时库：{SCRATCH}")
print(f"临时 data：{config.DATA_DIR}")
assert str(config.DATA_DIR).startswith(str(SCRATCH)), "数据目录没被隔离，拒绝继续"

proc = start_server()
png = png_bytes()
try:
    print("\n① 图片上传：/api/uploads/image")
    crops_before = {p.name for p in config.CROPS_DIR.glob("*")} if config.CROPS_DIR.exists() else set()
    body, ctype = multipart("file", "shot.png", png)
    code, d = post("/api/uploads/image", body, ctype)
    check("/api/uploads/image 返回 200", code, 200)
    name = d.get("name") if isinstance(d, dict) else None
    url = d.get("url") if isinstance(d, dict) else None

    print("\n② 落盘字节 == 上传字节")
    saved = config.CROPS_DIR / name if name else None
    check("crops 目录里真有这个文件", bool(saved and saved.is_file()))
    check("字节完全一致（读到了正确的文件对象）",
          saved.read_bytes() if saved and saved.is_file() else None, png)
    check("返回的 size_bytes 与上传长度一致",
          d.get("size_bytes") if isinstance(d, dict) else None, len(png))

    print("\n③ 返回的 url 能取回同一份字节（不是死链）")
    check("url 形如 /api/crops/xxx", bool(url and url.startswith("/api/crops/")), True)
    if url:
        ucode, ubody = get_raw(url)
        check("GET 这个 url 返回 200", ucode, 200)
        check("取回的字节也完全一致", ubody, png)

    print("\n④ 反向对照：非图片字节 → 422，且不落盘")
    # 快照必须在 ① 之后重取一次：① 自己落了一个文件，拿 ① 之前的快照来比会把
    # 「刚上传成功的那张」算成「被拒时多出来的」—— 这是测试自己的错，不是被测代码的。
    crops_now = {p.name for p in config.CROPS_DIR.glob("*")}
    notimg = b"this is definitely not an image, just plain text" * 8
    body2, ctype2 = multipart("file", "fake.png", notimg)   # 扩展名骗人是没用的（只认魔数）
    code2, d2 = post("/api/uploads/image", body2, ctype2)
    check("被拒（422）", code2, 422)
    check("拒的时候没有多出一个文件", new_files(config.CROPS_DIR, crops_now), [])

    print("\n⑤ 文档上传：/api/documents（第二个入口，另一条 read/落盘路径）")
    up_before = {p.name for p in config.UPLOAD_DIR.glob("*")} if config.UPLOAD_DIR.exists() else set()
    body3, ctype3 = multipart("file", "page.png", png)
    code3, d3 = post("/api/documents", body3, ctype3)
    check("/api/documents 返回 200", code3, 200)
    check("返回带文档 id", bool(isinstance(d3, dict) and d3.get("id")))
    added = new_files(config.UPLOAD_DIR, up_before)
    check("uploads 目录里多出恰好一个文件", len(added), 1)
    check("字节完全一致（这一条最能抓 file.file / file 搞混）",
          added[0].read_bytes() if added else None, png)
    check("filetype 按扩展名归一成 .png",
          d3.get("filetype") if isinstance(d3, dict) else None, ".png")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项："))
for f in fails:
    print("  - " + f)
print(f"（临时目录留着方便排查：{SCRATCH}）")
sys.exit(0 if not fails else 1)
