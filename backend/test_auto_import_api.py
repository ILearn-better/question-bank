# -*- coding: utf-8 -*-
"""自动导入的**接口契约**测试：只验「调用方能不能正确地把活派下去」。

⚠️ 刻意**不跑真实识别**（一次 30 页要十几分钟、几十次 AI 调用）。
   真链路验在人工跑批时做，记录在 `.workbuddy/memory/2026-10-23.md`。
   这里守的是另一件事：agent 拿到的每个错误都得是**可纠正的**——
   体系认不出要列出可选项、页号越界要说共几页、任务不存在要 404 而不是 200 空壳。

需要服务在跑（用户双击「启动拾课.bat」）。连不上就 SKIP 退出 0 ——
不该因为「这会儿没开服务」把回归弄红。

跑法：cd backend && .venv/Scripts/python.exe test_auto_import_api.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from pathlib import Path

# 免代理 opener（契约第六节第 2 条）：本机 http_proxy 会把 127.0.0.1 的请求也送去代理，
# 回来是 502 Bad Gateway —— 于是「服务没起」被误读成「服务坏了」，
# 更坏的是 502 的响应体被当成数据往下跑（报 byte indices 之类，完全误导）。
# 本机 HTTP 一律走这个 opener；连不上就该是 ConnectionRefused，红得明白。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 与 auto_import_cli 同一套端口判定（写死任一个会在另一份检出上假通过）
BASE = os.getenv("SHIKE_BASE") or (
    "http://127.0.0.1:8010" if "question-bank-ai" in Path(__file__).resolve().parent.parent.name
    else "http://127.0.0.1:8000")

fails: list[str] = []

def check(name: str, ok: bool, detail="") -> None:
    if ok:
        print("  [OK ] %s" % name)
    else:
        print("  [FAIL] %s   %s" % (name, detail))
        fails.append(name)

def call(path: str, body=None, method: str | None = None) -> tuple[int, dict]:
    """返回 (状态码, 响应体)。**4xx 不当异常抛** —— 那些正是要验的东西。"""
    data = None
    hdrs = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=hdrs,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with _OPENER.open(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8")
        except Exception:                            # noqa: BLE001
            pass
        try:
            return e.code, json.loads(raw) if raw.strip() else {"detail": e.reason}
        except ValueError:
            return e.code, {"detail": raw[:200]}

def main() -> int:
    print("自动导入接口契约测试（不跑识别、不烧 AI）  目标 %s" % BASE)
    try:
        code, d = call("/api/auto/curricula")
    except Exception as e:                           # noqa: BLE001
        print("SKIP: 服务没在跑（%s）—— 先双击「启动拾课.bat」" % e)
        return 0

    print("\n==== ① 路由与体系 ====")
    check("curricula 可用", code == 200 and "curricula" in d, code)
    opts = d.get("curricula") or []
    check("返回了体系列表（agent 第一步靠它）", len(opts) > 0, opts)
    first = opts[0] if opts else {}

    print("\n==== ② 体系认三种写法（不猜、认不出要列可选项） ====")
    # 用「文件不存在」来探测体系校验是否通过：体系过了才会报文件错。
    # 这样既能验体系解析，又不会真的建任务、不会烧 AI。
    for label, spec in (("id", str(first.get("id"))), ("code", first.get("code")),
                        ("name", first.get("name"))):
        c, r = call("/api/auto/import-path",
                    {"path": "_no_such_file_.pdf", "curriculum": spec})
        check("体系按 %s 传能通过校验（应止步于『找不到文件』）" % label,
              c == 422 and "找不到文件" in str(r.get("detail")), (c, r.get("detail")))

    c, r = call("/api/auto/import-path", {"path": "x.pdf"})
    check("不给体系 → 422",
          c == 422 and "体系" in str(r.get("detail")), (c, r.get("detail")))
    check("不给体系的错误里**列出可选项**（agent 能自己纠正或问用户）",
          all(("名称" in str(r.get("detail")) or o.get("name") in str(r.get("detail")))
              for o in opts if o.get("name")),
          r.get("detail"))
    c, r = call("/api/auto/import-path", {"path": "x.pdf", "curriculum": "不存在的体系"})
    check("体系认不出 → 422 且带可选清单",
          c == 422 and "可选" in str(r.get("detail")), (c, r.get("detail")))

    print("\n==== ③ 文件与页号 ====")
    c, r = call("/api/auto/import-path", {"path": "x.pdf", "curriculum": first.get("id")})
    check("文件不存在 → 422（不是 500）", c == 422, c)
    # 白名单：拿一个真存在但扩展名不认的文件
    bad = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_import_cli.py"))
    c, r = call("/api/auto/import-path", {"path": str(bad), "curriculum": first.get("id")})
    check("扩展名不认 → 422（.py 不能被当讲义）", c == 422, (c, r.get("detail")))

    print("\n==== ④ 不存在的任务 ====")
    c, r = call("/api/auto/imports/__nope__")
    check("查不存在的任务 → 404", c == 404, c)
    c, r = call("/api/auto/imports/__nope__/resume", {})
    check("收尾不存在的任务 → 404", c == 404, c)

    print("\n==== ⑤ 列表形状（agent 靠这些字段判断要不要再等） ====")
    c, r = call("/api/auto/imports?limit=5")
    check("imports 返回数组", c == 200 and isinstance(r, list), type(r).__name__)
    if isinstance(r, list) and r:
        one = r[0]
        need = ("id", "status", "done", "ok", "counts", "progress")
        check("每条都带 id/status/done/ok/counts/progress",
              all(k in one for k in need), [k for k in need if k not in one])

    print()
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0

if __name__ == "__main__":
    sys.exit(main())
