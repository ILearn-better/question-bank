# -*- coding: utf-8 -*-
"""真机验证：补答案的多张截图（Ctrl+V 粘贴 / 一次选多张 / 删掉贴错的那张）。

⚠️ 为什么必须真浏览器：这几条全是「浏览器给不给这个事件、给的是什么形态」的问题，
   jsdom 里粘贴只能自己造 DataTransfer，测的是我自己写的桩，不是浏览器。
   （前车之鉴：in-DOM 模板里 `/>` 不自闭合 → 整页白屏，静态断言全绿，只有真挂载才炸。）

⚠️ 规矩（记忆里的教训）：
   · 不读 `app._instance`（生产构建里是 null）→ 只走 DOM + HTTP 两条真通道；
   · **同一件事里必须再查一次服务端接口**：只看 DOM 会把「界面变了、服务端没变」
     当成通过。这里的判据是 `GET /api/crops/<name>` 回来那张图**真实像素尺寸**；
   · `/api/crops/{name}` 的 HEAD 返 404，探测必须用 GET；
   · 会往 data/uploads/crops 里写文件 → 结束后**按本次拿到的文件名逐个删**，
     绝不清目录（目录里是老师真实录题留下的截图）。

⚠️ 需要服务已经在跑（用户双击「启动拾课.bat」）；连不上就 SKIP 退出 0。

跑法：backend/.venv/Scripts/python.exe test_answer_shots_browser.py
"""
import asyncio
import io
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets
from PIL import Image

BASE = "http://127.0.0.1:8000"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9334
CROPS = Path(__file__).resolve().parent / "data" / "uploads" / "crops"

# 两张尺寸不同的图，拼起来才验得出「谁在上面、宽度取谁」
A = (300, 400)
B = (200, 300)
WANT_W = max(A[0], B[0])          # 300
WANT_H = A[1] + B[1]              # 700

fails = []
made_files = []                   # 本次在 crops 里新增的文件名（只删这些）

# ⚠️ 本机（含 agent 的 shell）常年挂着 http_proxy/https_proxy。urllib 会照着走代理，
#    于是「连 127.0.0.1:8000」被送到代理 → 502 Bad Gateway，看起来像服务坏了。
#    测试必须自己绕开代理，不能依赖跑它的人有没有 export。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("OK " if ok else "FAIL", name, ("  " + str(detail)) if detail else ""))
    if not ok:
        fails.append(name)


def http_get(path):
    """GET 原文（不是 HEAD —— /api/crops 的 HEAD 返 404）。"""
    with _OPENER.open(BASE + path, timeout=20) as r:
        return r.read()


def img_size(url):
    """服务端那张图真实多少像素。任一环节失败就抛，让断言看见。"""
    raw = http_get(url)
    with Image.open(io.BytesIO(raw)) as im:
        return im.size


def probe():
    try:
        http_get("/api/questions/search?limit=1")
    except Exception as e:                                  # noqa: BLE001
        print("SKIP: 服务没在跑（%s）—— 先双击「启动拾课.bat」" % e)
        return False
    return True


async def send(ws, mid, method, **params):
    await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError("%s: %s" % (method, msg["error"]))
            return msg.get("result", {})


async def ev(ws, expr, mid):
    r = await send(ws, mid, "Runtime.evaluate", expression=expr,
                   returnByValue=True, awaitPromise=True)
    res = r.get("result", {})
    if res.get("subtype") == "error":
        raise RuntimeError("页面里报错: %s" % res.get("description"))
    return res.get("value")


# 页面里那几处 DOM 查询的唯一出处（改选择器只改这里）
JS_SHOTS = "document.querySelectorAll('.shot-strip .shot').length"
JS_ANSWER_IMG = """(() => {
  const h = [...document.querySelectorAll('.muted-hint')]
    .find(e => e.textContent.trim().indexOf('答案图') === 0);
  if (!h) return '';
  const box = h.parentElement && h.parentElement.parentElement;
  const im = box ? box.querySelector('img.prev-img') : null;
  return im ? (im.getAttribute('src') || '') : '';
})()"""


# 在页面里造两张真图，再按「Windows 截图工具粘图」的形态发 paste 事件。
# 用 DataTransfer 而不是硬塞 clipboardData 对象：只有真 DataTransfer 的 items
# 才带 kind/type/getAsFile，桩出来的对象测不出「真浏览器给的是什么形态」。
JS_PASTE = """(async () => {
  const mk = (w, h, color) => new Promise(res => {
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    const g = c.getContext('2d');
    g.fillStyle = color; g.fillRect(0, 0, w, h);
    g.fillStyle = '#111'; g.font = 'bold 32px sans-serif';
    g.fillText(w + 'x' + h, 12, 48);
    c.toBlob(b => res(new File([b], 'shot_' + w + 'x' + h + '.png', {type: 'image/png'})), 'image/png');
  });
  const files = [await mk(300, 400, '#cfe8ff'), await mk(200, 300, '#ffe0cf')];
  const dt = new DataTransfer();
  files.forEach(f => dt.items.add(f));
  const ok = dt.items.length === 2;
  document.dispatchEvent(new ClipboardEvent('paste', {
    clipboardData: dt, bubbles: true, cancelable: true
  }));
  return ok;
})()"""

# 只贴一张。单张走的是另一条分支（直传 /api/uploads/image，不拼接）——
# 这条路径坏了老师感觉不到「跨页」，但每题都会用，所以也要验。
JS_PASTE_SINGLE = """(async () => {
  const c = document.createElement('canvas');
  c.width = __W__; c.height = __H__;
  const g = c.getContext('2d');
  g.fillStyle = '#d8f5d8'; g.fillRect(0, 0, __W__, __H__);
  const blob = await new Promise(r => c.toBlob(r, 'image/png'));
  const dt = new DataTransfer();
  dt.items.add(new File([blob], 'single.png', {type: 'image/png'}));
  document.dispatchEvent(new ClipboardEvent('paste', {
    clipboardData: dt, bubbles: true, cancelable: true
  }));
  return true;
})()"""

# 「带着文字一起粘」：一个文字 + 一张图。老师往正文里粘参考链接就是这种形态。
JS_PASTE_TEXT = """(async () => {
  const c = document.createElement('canvas');
  c.width = 120; c.height = 120;
  c.getContext('2d').fillRect(0, 0, 120, 120);
  const blob = await new Promise(r => c.toBlob(r, 'image/png'));
  const dt = new DataTransfer();
  dt.setData('text/plain', '【答案】见解析');
  dt.items.add(new File([blob], 'with_text.png', {type: 'image/png'}));
  document.dispatchEvent(new ClipboardEvent('paste', {
    clipboardData: dt, bubbles: true, cancelable: true
  }));
  return true;
})()"""


async def wait_for(ws, nid, expr, want, timeout=25.0):
    """等页面自己把异步活干完（上传/拼接都是 fetch，不会同步完成）。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = await ev(ws, expr, nid())
        if last == want:
            return True, last
        await asyncio.sleep(0.4)
    return False, last


def note_new_crops():
    """本次新增的文件名 = 现在目录里的 − 开始前目录里的。"""
    try:
        return {p.name for p in CROPS.iterdir() if p.is_file()}
    except Exception:                                      # noqa: BLE001
        return set()


def cleanup(before):
    """只删「本次新增」的那些文件。**绝不清目录** —— 里面有老师真实录题的截图。"""
    after = note_new_crops()
    extra = sorted(after - before)
    if not extra:
        print("（本次没有需要清理的文件）")
        return
    for name in extra:
        if name in made_files or name.startswith(("stitch", "img_", "shot_")):
            try:
                (CROPS / name).unlink()
            except Exception as e:                         # noqa: BLE001
                print("  ⚠️ 删不掉 %s: %s" % (name, e))
    print("（已删除本次生成的 %d 个文件）" % len(extra))


async def main():
    print("==== 0 准备 ====")
    if not probe():
        return 0
    if not CROPS.is_dir():
        print("SKIP: 找不到 crops 目录 %s" % CROPS)
        return 0
    before = note_new_crops()
    prof = tempfile.mkdtemp(prefix="edge_a_")
    proc = subprocess.Popen([EDGE, "--headless=new",
                             "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + prof, "--no-first-run",
                             "--disable-gpu", "--window-size=1500,1400",
                             "--no-proxy-server",
                             BASE + "/entry.html"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mid = [0]

    def nid():
        mid[0] += 1
        return mid[0]

    try:
        ws_url = None
        for _ in range(60):
            try:
                with _OPENER.open("http://127.0.0.1:%d/json/list" % PORT, timeout=1) as r:
                    tabs = json.load(r)
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:                              # noqa: BLE001
                pass
            time.sleep(0.5)
        if not ws_url:
            print("  [FAIL] 连不上 Edge 的调试端口")
            return 1

        async with websockets.connect(ws_url, max_size=64 << 20) as ws:
            await send(ws, nid(), "Runtime.enable")

            # ---- 1 页面真的挂上了 ----
            print("\n==== 1 页面挂载（白屏守卫）====")
            ok_mount, _ = await wait_for(
                ws, nid,
                "!!(document.querySelector('#app') && "
                "document.querySelector('#app').innerHTML.length > 2000 && "
                "document.querySelector('.el-radio-button__inner'))",
                True, 40)
            check("entry.html 挂载出了真界面（不是白屏）", ok_mount)
            n_radio = await ev(ws, """[...document.querySelectorAll('.el-radio-button__inner')]
              .filter(e => ['录新题', '补答案'].indexOf(e.textContent.trim()) >= 0).length""", nid())
            check("顶部「录新题 / 补答案」两个切换都在", n_radio == 2, "实际 %s" % n_radio)

            # ---- 2 切到补答案模式 ----
            print("\n==== 2 切到补答案模式 ====")
            clicked = await ev(ws, """(() => {
              const b = [...document.querySelectorAll('.el-radio-button__inner')]
                .find(e => e.textContent.trim() === '补答案');
              if (!b) return false;
              b.click();
              return true;
            })()""", nid())
            check("点得到「补答案」", clicked)
            ok_ans, _ = await wait_for(
                ws, nid,
                "(() => {const i = [...document.querySelectorAll('input[type=file]')]"
                ".find(x => x.getAttribute('accept') === 'image/*');"
                "return !!(i && i.multiple);})()", True, 15)
            check("答案阶段文件框允许多选（真 DOM 属性）", ok_ans)
            hint = await ev(ws, """(() => {
              const h = [...document.querySelectorAll('.muted-hint')]
                .find(e => e.textContent.indexOf('Ctrl+V') >= 0);
              return h ? h.textContent.trim() : '';
            })()""", nid())
            check("界面上写明了可以 Ctrl+V 粘贴", 'Ctrl+V' in (hint or ''), hint or '(没找到)')

            # ---- 3 带着文字粘贴 → 什么都不做 ----
            print("\n==== 3 剪贴板里带着文字时，不插手 ====")
            await ev(ws, JS_PASTE_TEXT, nid())
            await asyncio.sleep(3.0)
            n3 = await ev(ws, JS_SHOTS, nid())
            u3 = await ev(ws, JS_ANSWER_IMG, nid())
            check("没冒出缩略图条", n3 == 0, "实际 %s" % n3)
            check("没偷偷生成答案图", not u3, u3 or '(空)')

            # ---- 4 Ctrl+V 贴两张图（跨页答案）----
            print("\n==== 4 Ctrl+V 贴两张图 → 竖拼 ====")
            built = await ev(ws, JS_PASTE, nid())
            check("页面里造出了 2 张真图并发出 paste 事件", built is True)
            ok4, n4 = await wait_for(ws, nid, JS_SHOTS, 2, 30)
            check("缩略图条里出现 2 张", ok4, "实际 %s" % n4)
            ok_u4, _ = await wait_for(
                ws, nid, "(%s) ? true : false" % JS_ANSWER_IMG, True, 30)
            check("表单上出现了答案图", ok_u4)
            u4 = await ev(ws, JS_ANSWER_IMG, nid())
            tag = await ev(ws, """(() => {
              const h = [...document.querySelectorAll('.muted-hint')]
                .find(e => e.textContent.trim().indexOf('答案图') === 0);
              return h ? h.textContent.trim() : '';
            })()""", nid())
            check("标签上写明是几张竖拼的", '2 张截图竖拼' in (tag or ''), tag or '(空)')

            # ⚠️ 关键：服务端真的有这张图，且**尺寸对得上**（DOM 说拼了就拼了不算）
            if u4:
                made_files.append(u4.rsplit('/', 1)[-1])
                try:
                    w, h = img_size(u4)
                    check("服务端那张拼图宽度取最宽的一张", w == WANT_W, "实际 %s 期望 %s" % (w, WANT_W))
                    check("服务端那张拼图高度是两张之和", h == WANT_H, "实际 %s 期望 %s" % (h, WANT_H))
                except Exception as e:                     # noqa: BLE001
                    check("服务端那张拼图取得回来", False, e)
            else:
                check("服务端那张拼图取得回来", False, "页面没给 url")

            # ---- 5 删掉贴错的那一张 ----
            print("\n==== 5 删掉第 1 张，剩下那张重新落成答案图 ====")
            await ev(ws, """(() => {
              const b = document.querySelector('.shot-strip .shot .shot-del');
              if (!b) return false;
              b.click();
              return true;
            })()""", nid())
            ok5, n5 = await wait_for(ws, nid, JS_SHOTS, 0, 30)
            check("缩略图条收起来了（只剩一张）", ok5, "实际 %s" % n5)
            ok_u5, _ = await wait_for(
                ws, nid,
                "(%s && %s !== %s) ? true : false" % (JS_ANSWER_IMG, JS_ANSWER_IMG, json.dumps(u4 or "")),
                True, 30)
            check("答案图换成了另一张", ok_u5, "还是 %s" % (u4 or ''))
            u5 = await ev(ws, JS_ANSWER_IMG, nid())
            if u5:
                made_files.append(u5.rsplit('/', 1)[-1])
                try:
                    w5, h5 = img_size(u5)
                    check("换成的是剩下那张（尺寸 200x300）", (w5, h5) == B, "实际 %sx%s" % (w5, h5))
                except Exception as e:                     # noqa: BLE001
                    check("换成的是剩下那张（尺寸 200x300）", False, e)
            else:
                check("换成的是剩下那张（尺寸 200x300）", False, "页面没给 url")

            # ---- 6 清空后单张贴图也走通（单张走的是另一条分支）----
            print("\n==== 6 清空后只贴一张：不出缩略图条，但要真出答案图 ====")
            cleared = await ev(ws, """(() => {
              const b = [...document.querySelectorAll('.el-button')]
                .find(e => e.textContent.trim() === '清除');
              if (!b) return false;
              b.click();
              return true;
            })()""", nid())
            check("点得到答案图的「清除」", cleared)
            ok_c, _ = await wait_for(
                ws, nid, "(%s) ? true : false" % JS_ANSWER_IMG, False, 15)
            check("清空后答案图没了", ok_c)
            await ev(ws, JS_PASTE_SINGLE.replace("__W__", str(B[0])).replace("__H__", str(B[1])), nid())
            ok6, n6 = await wait_for(ws, nid, JS_SHOTS, 0, 30)
            check("单张时不出缩略图条", ok6, "实际 %s" % n6)
            ok_u6, _ = await wait_for(
                ws, nid, "(%s) ? true : false" % JS_ANSWER_IMG, True, 30)
            check("单张贴图也落成了答案图", ok_u6)
            u6 = await ev(ws, JS_ANSWER_IMG, nid())
            if u6:
                made_files.append(u6.rsplit('/', 1)[-1])
                try:
                    w6, h6 = img_size(u6)
                    check("单张那张尺寸没被改动（200x300）", (w6, h6) == B, "实际 %sx%s" % (w6, h6))
                except Exception as e:                     # noqa: BLE001
                    check("单张那张尺寸没被改动（200x300）", False, e)
            else:
                check("单张那张尺寸没被改动（200x300）", False, "页面没给 url")

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:                                  # noqa: BLE001
            pass
        cleanup(before)

    print("\n" + "=" * 50)
    if fails:
        print("失败 %d 项：%s" % (len(fails), "、".join(fails)))
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
