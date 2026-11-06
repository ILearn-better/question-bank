# -*- coding: utf-8 -*-
"""真机验证：批改台「往卷面上插图片」（0039）。

用户 2026-11-06 的原话：
  「在作业批改处加入文件插图图片功能，（笔画、文本框、图片谁先出现就谁放下面），
    并且要求能够删除图片、撤销插入（快捷键 ctrl z）、鼠标拖拽调整图片大小」

⚠️ 为什么必须真浏览器：
   · 拖 / 缩走的是 pointerdown → pointermove → pointerup 这条**浏览器手势**通道，
     jsdom 里根本发不出来（所以 grade_mount_check 只能验「元素在、类名对」）；
   · 层序是**绘制顺序**，静态断言只能证明 `zOf` 这个函数写对了，证明不了
     「老师眼睛看到的是谁盖谁」—— 所以这里截屏取像素做终审；
   · 上一轮已经吃过亏：in-DOM 模板自闭合 → 整页白屏，静态断言全绿、只有真挂载才炸。

⚠️ 规矩（记忆里的教训）：
   · 不读 `app._instance`（生产构建里是 null）→ 只走 DOM + HTTP 两条真通道；
   · **同一条里必须再查一次服务端接口**：只看 DOM 会把「界面变了、服务端没变」当成通过。
     这里的判据是 `GET /api/homework-grades/<gid>` 回来的 marks 里那条真的在、
     geo / image URL 都对得上，并且图能取回来、像素尺寸对；
   · `/api/crops/{name}` 的 HEAD 返 404，探测一律用 GET；
   · 会往 data/uploads/grade_marks 里写文件 → 结束时**只删本次拿到的那几个文件名**，
     绝不清目录；批注本身也只删**本次新建的 id**（开始前快照 original 的那批不动）；
   · 本机常挂代理 → urllib 探 127.0.0.1 会被送进代理返 502，必须自带 ProxyHandler({})。

跑法：
  SHIKE_BASE=http://127.0.0.1:8001 SHIKE_GID=30 \
     backend/.venv/Scripts/python.exe test_grade_mark_image_browser.py
（探针服务的搭法见 _probe_setup.py 的注释；直连 8000 也可以，但会往真库里写标注，
  所以本脚本自带的清理必须留着。）
"""
import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets
from PIL import Image

BASE = os.environ.get("SHIKE_BASE") or (
    "http://127.0.0.1:8010" if Path(__file__).resolve().parents[1].name == "question-bank-ai"
    else "http://127.0.0.1:8000")
GID = int(os.environ.get("SHIKE_GID") or 30)
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9336                 # 与另两个浏览器探针（9334 / 9335）错开，能并行跑
# 批注图目录：**按服务端自己报的 data 目录算**，不能写死仓库里那份。
# 探针服务跑在 `SHIKE_DATA_DIR` 指的临时目录上，写死的话清理会去删一个空目录、
# 而真正新落的图留在探针那边越积越多（第一次跑就踩到了）。
MARKS = None

# 插进去的那张图：**纯色 + 左上角一个白方块**。
# 纯色是为了后面截屏取像素 —— 取中心点，中心必须是这张图的蓝，不能是笔迹的红。
IMG_RGB = (37, 99, 235)     # #2563eb
CANVAS = (300, 200)
WANT_BOX_W = 360            # HomeworkGrade.IMG_BOX_W
WANT_BOX_H = round(WANT_BOX_W * CANVAS[1] / CANVAS[0])      # 240
# 居中落点：x = (1000-360)/2, y = (1000-240)/2
WANT_GEO = [320, 380, 320 + WANT_BOX_W, 380 + WANT_BOX_H]

fails = []
made_files = []             # 本次在 grade_marks 里新增的文件名
_before_ids = set()         # 开始前库里已有的标注 id（清理时绝不动它们）

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("OK " if ok else "FAIL", name, ("  " + str(detail)) if detail else ""))
    if not ok:
        fails.append(name)


def http_get(path):
    with _OPENER.open(BASE + path, timeout=30) as r:
        return r.read()


def http_json(path):
    return json.loads(http_get(path).decode("utf-8"))


def http_delete(path):
    req = urllib.request.Request(BASE + path, method="DELETE")
    with _OPENER.open(req, timeout=30) as r:
        return r.read()


def server_marks():
    """服务端真有的标注（老师的和 AI 的都算）。"""
    return http_json("/api/homework-grades/%d" % GID)["marks"]


def img_size(url):
    raw = http_get(url)
    with Image.open(io.BytesIO(raw)) as im:
        return im.size, im.convert("RGB").getpixel((10, 10))


def probe():
    global MARKS
    try:
        info = http_json("/api/system/info")
    except Exception as e:                                  # noqa: BLE001
        print("SKIP: 服务没在跑（%s）—— 先双击「启动拾课.bat」" % e)
        return False
    rev = str(info.get("revision") or "")
    if rev < "0039":
        print("SKIP: 服务还停在 revision %s —— 0039（image 列）没生效。"
              "重启一次服务让它自己迁移，再跑这个脚本。" % (rev or "?"))
        return False
    up = (info.get("dirs") or {}).get("uploads")
    MARKS = (Path(up) if up
             else Path(__file__).resolve().parent / "data" / "uploads") / "grade_marks"
    print("  批注图目录：%s" % MARKS)
    try:
        g = http_json("/api/homework-grades/%d" % GID)
    except Exception as e:                                  # noqa: BLE001
        print("SKIP: 拿不到 gid=%d 的批改（%s）" % (GID, e))
        return False
    if not g.get("pages"):
        print("SKIP: gid=%d 还没有页图 —— 先在界面上点一次「开始批改」" % GID)
        return False
    return True


def note_grade_marks():
    try:
        return {p.name for p in MARKS.iterdir() if p.is_file()}
    except Exception:                                       # noqa: BLE001
        return set()


def cleanup_marks():
    """删本次建的标注 + 本次落的图。**只按 id / 文件名点名删**，不清目录、不扫全库。"""
    try:
        now = server_marks()
    except Exception as e:                                  # noqa: BLE001
        print("  ⚠️ 清理时拿不到标注列表：%s" % e)
        now = []
    mine = [m for m in now if m.get("id") not in _before_ids]
    for m in mine:
        try:
            http_delete("/api/grade-marks/%d" % int(m["id"]))
        except Exception as e:                              # noqa: BLE001
            print("  ⚠️ 删不掉标注 %s: %s" % (m.get("id"), e))
    for name in made_files:
        try:
            (MARKS / name).unlink()
        except Exception as e:                              # noqa: BLE001
            print("  ⚠️ 删不掉 %s: %s" % (name, e))
    print("（已清掉本次建的 %d 条标注、%d 个图片文件）" % (len(mine), len(made_files)))


# ------------------------------------------------------------------ CDP 小工具
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


async def wait_for(ws, nid, expr, want, timeout=25.0):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = await ev(ws, expr, nid())
        if last == want:
            return True, last
        await asyncio.sleep(0.35)
    return False, last


async def mouse(ws, nid, kind, x, y, buttons=0):
    await send(ws, nid(), "Input.dispatchMouseEvent", type=kind, x=x, y=y,
               button="left" if kind != "mouseMoved" or buttons else "none",
               buttons=buttons, clickCount=1, pointerType="mouse")


async def click_at(ws, nid, x, y):
    await mouse(ws, nid, "mousePressed", x, y, buttons=1)
    await asyncio.sleep(0.05)
    await mouse(ws, nid, "mouseReleased", x, y, buttons=0)
    await asyncio.sleep(0.15)


async def drag(ws, nid, x1, y1, x2, y2, steps=12):
    """真鼠标拖：按下 → 一串 move（**buttons=1**，否则 Chrome 认为键已松）→ 松手。"""
    await mouse(ws, nid, "mousePressed", x1, y1, buttons=1)
    await asyncio.sleep(0.08)
    for i in range(1, steps + 1):
        await mouse(ws, nid, "mouseMoved", x1 + (x2 - x1) * i / steps,
                    y1 + (y2 - y1) * i / steps, buttons=1)
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    await mouse(ws, nid, "mouseReleased", x2, y2, buttons=0)
    await asyncio.sleep(0.3)


async def key(ws, nid, name, code, vk, modifiers=0):
    for t in ("rawKeyDown", "keyUp"):
        await send(ws, nid(), "Input.dispatchKeyEvent", type=t, key=name, code=code,
                   windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=modifiers)
    await asyncio.sleep(0.3)


async def rect_of(ws, nid, sel):
    return await ev(ws, """(() => {
      const e = document.querySelector(%s);
      if (!e) return null;
      e.scrollIntoView({block: 'center', inline: 'center'});
      const r = e.getBoundingClientRect();
      return {left: r.left, top: r.top, w: r.width, h: r.height,
              cx: r.left + r.width / 2, cy: r.top + r.height / 2,
              right: r.right, bottom: r.bottom};
    })()""" % json.dumps(sel), nid())


# 一次取多个元素的矩形。
# ⚠️ 必须**在一次 evaluate 里拿全**：rect_of 会 scrollIntoView，逐个取的话
#    第一个元素把页面滚了一次、第二个又滚一次 —— 两次拿到的坐标不在同一帧里，
#    「A 和 B 交叠了吗」这类比较会得出完全错误的结论（本轮踩过，假红）。
JS_RECTS = """(sels => sels.map(s => {
  const e = document.querySelector(s);
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return {left: r.left, top: r.top, w: r.width, h: r.height,
          cx: r.left + r.width / 2, cy: r.top + r.height / 2,
          right: r.right, bottom: r.bottom};
}))(%s)"""


async def rects_of(ws, nid, sels):
    return await ev(ws, JS_RECTS % json.dumps(list(sels)), nid())


async def scroll_to(ws, nid, sel):
    await ev(ws, """(() => {
      const e = document.querySelector(%s);
      if (e) e.scrollIntoView({block: 'center', inline: 'center'});
      return !!e;
    })()""" % json.dumps(sel), nid())
    await asyncio.sleep(0.3)


def U2PX(r, ux, uy):
    """标注坐标（0~1000）→ 视口像素（与组件里的 toPt 同一套换算）。"""
    return r["left"] + ux / 1000.0 * r["w"], r["top"] + uy / 1000.0 * r["h"]


async def shot_pixel(ws, nid, x, y, box=3):
    """截全视口 + 按 getBoundingClientRect 坐标取一小块平均色。

    **这是「老师肉眼看到的是谁盖谁」的终审。**
    ⚠️ 不用 captureScreenshot 的 clip 参数：那个 clip 原点是「页面」还是「视口」在各版本
       上说法不一，滚过页之后会对不上，而错位取到的像素看起来仍然是个颜色 —— 假绿。
       整幅截图后自己索引，坐标口径就和 getBoundingClientRect 一致了。
    """
    r = await send(ws, nid(), "Page.captureScreenshot", format="png")
    raw = base64.b64decode(r["data"])
    iw = await ev(ws, "window.innerWidth", nid())
    with Image.open(io.BytesIO(raw)) as im:
        rgb = im.convert("RGB")
        k = rgb.size[0] / float(iw or rgb.size[0])       # devicePixelRatio
        cx, cy = int(round(x * k)), int(round(y * k))
        r0 = max(1, int(round(box * k)))
        px = [rgb.getpixel((i, j))
              for i in range(max(0, cx - r0), min(rgb.size[0], cx + r0 + 1))
              for j in range(max(0, cy - r0), min(rgb.size[1], cy + r0 + 1))]
    n = len(px) or 1
    return tuple(round(sum(c[i] for c in px) / n) for i in range(3))


def near(c1, c2, tol=26):
    return all(abs(a - b) <= tol for a, b in zip(c1, c2))


# ------------------------------------------------------------------ 页面里的小动作
JS_CLICK_TOOL = """(() => {
  const b = [...document.querySelectorAll('.grade-tools .g-tool')]
    .find(e => e.textContent.trim() === %s);
  if (!b) return 'no-button';
  b.click();
  return 'ok';
})()"""

# 往隐藏的 file 框里塞一张真图，再发 change（唯一能走通的路：input.click() 起不了
# 系统选文件框，而 el.files 在 Chrome 里是可写的）。
JS_SET_FILE = """(async () => {
  const el = document.querySelector('input.g-file-hidden[type=file]');
  if (!el) return 'no-input';
  const c = document.createElement('canvas');
  c.width = %d; c.height = %d;
  const g = c.getContext('2d');
  g.fillStyle = 'rgb(%d,%d,%d)'; g.fillRect(0, 0, c.width, c.height);
  g.fillStyle = '#ffffff'; g.fillRect(8, 8, 26, 26);      // 角上做个记号，中心仍是纯色
  const blob = await new Promise(r => c.toBlob(r, 'image/png'));
  const dt = new DataTransfer();
  dt.items.add(new File([blob], 'mark_probe.png', {type: 'image/png'}));
  el.files = dt.files;
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return 'ok';
})()""" % (CANVAS[0], CANVAS[1], IMG_RGB[0], IMG_RGB[1], IMG_RGB[2])

JS_FILL_TEXT = """(() => {
  const ta = document.querySelector('.g-textbox textarea');
  if (!ta) return 'no-textarea';
  ta.value = 'PROBE 批注';
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  const btn = [...document.querySelectorAll('.g-textbox-foot button')]
    .find(b => b.textContent.trim() === '放上去');
  if (!btn) return 'no-button';
  btn.click();
  return 'ok';
})()"""

# 层序快照：每条标注节点的 z-index + 它是哪一类。DOM 顺序**不可靠**（v-for 顺序
# 恰好等于创建顺序，那是巧合不是机制），所以老老实实读计算样式。
JS_ZS = """(() => [...document.querySelectorAll('.gmark-node')].map(n => ({
  z: parseInt(getComputedStyle(n).zIndex, 10),
  kind: n.querySelector('.gmark-imgbox') ? 'image'
      : n.querySelector('.gmark-label') ? 'text'
      : (n.querySelector('svg.gmark') ? 'graph' : '?')
})))()"""

# 命中测试：交集那一点**最上层**的是什么（分层如果真的生效，图片在笔迹上面，
# 文本框又在图片上面）。
JS_TOP_AT = """(() => {
  const els = document.elementsFromPoint(%f, %f);
  for (const e of els) {
    if (e.closest && e.closest('.gmark-label')) return 'label';
    if (e.closest && e.closest('.gmark-imgbox')) return 'image';
  }
  return 'none';
})()"""


async def main():
    print("==== 0 准备 ====")
    if not probe():
        return 0
    global _before_ids
    _before_ids = {m.get("id") for m in server_marks()}
    before_files = note_grade_marks()
    print("  gid=%d / 开始前已有 %d 条标注 / grade_marks 里已有 %d 个文件"
          % (GID, len(_before_ids), len(before_files)))

    prof = tempfile.mkdtemp(prefix="edge_gm_")
    proc = subprocess.Popen([EDGE, "--headless=new",
                             "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + prof, "--no-first-run",
                             "--disable-gpu", "--window-size=1500,1500",
                             "--no-proxy-server",
                             BASE + "/index.html#/grade/%d" % GID],
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
            except Exception:                               # noqa: BLE001
                pass
            time.sleep(0.5)
        if not ws_url:
            print("  [FAIL] 连不上 Edge 的调试端口")
            return 1

        async with websockets.connect(ws_url, max_size=96 << 20) as ws:
            await send(ws, nid(), "Runtime.enable")
            await send(ws, nid(), "Page.enable")
            # 缓存关掉：改完 js 立刻看到（否则 Edge 会给旧文件）
            await send(ws, nid(), "Network.enable")
            await send(ws, nid(), "Network.setCacheDisabled", cacheDisabled=True)

            # ---- 1 批改台真的挂上了 ----
            print("\n==== 1 批改台挂载（白屏守卫 + 图片按钮在）====")
            ok, _ = await wait_for(
                ws, nid,
                """(() => {
                  const img = document.querySelector('.grade-page-img');
                  return !!(img && img.complete && img.naturalWidth > 200 &&
                            document.querySelector('.gmark-layer') &&
                            document.querySelector('.grade-tools'));
                })()""", True, 60)
            check("批改台挂载完成、卷面图有真实像素", ok)
            n_tool = await ev(ws, """[...document.querySelectorAll('.grade-tools .g-tool')]
              .filter(e => e.textContent.trim() === '图片').length""", nid())
            check("工具栏里有「图片」按钮", n_tool == 1, "实际 %s" % n_tool)
            n_file = await ev(ws, """document.querySelectorAll(
              'input.g-file-hidden[type=file][accept="image/*"]').length""", nid())
            check("配了一个 image/* 的文件框", n_file == 1, "实际 %s" % n_file)

            layer = await rect_of(ws, nid, ".gmark-svg")
            check("标注层有真实尺寸（能换算坐标）", bool(layer) and layer["w"] > 200
                  and layer["h"] > 200, layer and "%.0fx%.0f" % (layer["w"], layer["h"]))

            # ---- 2 插一张图：界面 + 服务端 + 图本身 ----
            print("\n==== 2 插图片 ====")
            r2 = await ev(ws, JS_SET_FILE, nid())
            check("文件框收到了这张图", r2 == "ok", r2)
            ok, n = await wait_for(ws, nid,
                                   "document.querySelectorAll('.gmark-imgbox').length", 1, 40)
            check("卷面上出现了图片框", ok, "实际 %s" % n)

            await asyncio.sleep(1.2)          # 等 POST + settle
            sm = [m for m in server_marks() if m.get("kind") == "image"]
            check("服务端真有一条 kind=image 的标注", len(sm) == 1, "实际 %d 条" % len(sm))
            m2 = sm[0] if sm else {}
            url = m2.get("image") or ""
            check("image 字段是自家 /api/mark-images/ 的 URL",
                  url.startswith("/api/mark-images/"), url or "(空)")
            check("geo 是等比居中后的那个框", m2.get("geo") == WANT_GEO,
                  "%s 期望 %s" % (m2.get("geo"), WANT_GEO))
            if url:
                made_files.append(url.rsplit("/", 1)[-1])
                try:
                    size, corner = img_size(url)
                    check("取回来的图就是插进去那张（像素尺寸）", size == CANVAS,
                          "%s 期望 %s" % (size, CANVAS))
                    check("取回来的图内容没被转码坏（左上角落了白方块）",
                          corner[0] > 220 and corner[1] > 220 and corner[2] > 220, corner)
                except Exception as e:                      # noqa: BLE001
                    check("取回来的图就是插进去那张", False, e)
            img_id = m2.get("id")
            check("插完就选中了（底部操作条出现，能点删除）",
                  await ev(ws, """!!document.querySelector('.g-actbar')""", nid()) is True)

            # ---- 3 拖动改位置 ----
            print("\n==== 3 鼠标拖拽移动图片 ====")
            box = await rect_of(ws, nid, ".gmark-imgbox")
            try:
                await drag(ws, nid, box["cx"], box["cy"], box["cx"] + 90, box["cy"] + 60)
                await asyncio.sleep(1.2)
                m3 = [m for m in server_marks() if m.get("id") == img_id]
                g3 = (m3[0].get("geo") if m3 else None) or []
                dx_ok = g3 and g3[0] > WANT_GEO[0] and g3[1] > WANT_GEO[1]
                size_ok = g3 and (g3[3] - g3[1]) == (WANT_GEO[3] - WANT_GEO[1])
                check("拖完之后服务端 geo 真的挪了", bool(dx_ok),
                      "%s（拖之前 %s）" % (g3, WANT_GEO))
                check("拖动只挪位置、不改大小", bool(size_ok),
                      g3 and "高 %s 期望 %s" % (g3[3] - g3[1], WANT_GEO[3] - WANT_GEO[1]))
            except Exception as e:                          # noqa: BLE001
                check("拖完之后服务端 geo 真的挪了", False, e)
                g3 = []

            # ---- 4 拖右下角改大小 ----
            print("\n==== 4 拖右下角小方块改大小 ====")
            h = await rect_of(ws, nid, ".gmark-imgbox .gmark-resize")
            if not h:
                check("图片框右下角有缩放把手", False, "没找到 .gmark-resize")
            else:
                check("图片框右下角有缩放把手", h["w"] >= 8 and h["h"] >= 8,
                      "%.0fx%.0f" % (h["w"], h["h"]))
                try:
                    await drag(ws, nid, h["cx"], h["cy"], h["cx"] + 120, h["cy"] + 80)
                    await asyncio.sleep(1.2)
                    m4 = [m for m in server_marks() if m.get("id") == img_id]
                    g4 = (m4[0].get("geo") if m4 else None) or []
                    w0 = (g3[2] - g3[0]) if len(g3) >= 4 else WANT_GEO[2] - WANT_GEO[0]
                    w1 = (g4[2] - g4[0]) if len(g4) >= 4 else 0
                    check("拖完之后服务端那张图真的变宽了", w1 > w0 + 20,
                          "宽 %s → %s" % (w0, w1))
                    check("缩放时左上角不动", len(g4) >= 4 and g4[0] == g3[0]
                          and g4[1] == g3[1], "%s vs %s" % (g4[:2], g3[:2]))
                except Exception as e:                      # noqa: BLE001
                    check("拖完之后服务端那张图真的变宽了", False, e)

            # 缩放完还能再拖（记忆里 11-03 的老坑：临时尺寸不收 → 之后拖不动）
            print("\n---- 4b 缩放之后再拖一次（临时尺寸必须自己撤掉）----")
            b2 = await rect_of(ws, nid, ".gmark-imgbox")
            g_before = [m for m in server_marks() if m.get("id") == img_id]
            g_before = (g_before[0].get("geo") if g_before else None) or []
            try:
                await drag(ws, nid, b2["cx"], b2["cy"], b2["cx"] - 50, b2["cy"] - 40)
                await asyncio.sleep(1.2)
                m5 = [m for m in server_marks() if m.get("id") == img_id]
                g5 = (m5[0].get("geo") if m5 else None) or []
                check("缩放之后再拖，服务端还能跟着变", bool(g5) and g5[:2] != g_before[:2],
                      "%s → %s" % (g_before[:2], g5[:2]))
            except Exception as e:                          # noqa: BLE001
                check("缩放之后再拖，服务端还能跟着变", False, e)

            # ---- 5 Delete 删掉图片 ----
            print("\n==== 5 选中图片 → Delete 删掉 ====")
            b3 = await rect_of(ws, nid, ".gmark-imgbox")
            if b3:
                await click_at(ws, nid, b3["cx"], b3["cy"])   # 先选中
                await asyncio.sleep(0.4)
            sel = await ev(ws, """!!document.querySelector('.gmark-imgbox.act')""", nid())
            check("点一下图片就被选中了（act 高亮）", sel is True)
            await key(ws, nid, "Delete", "Delete", 46)
            await asyncio.sleep(1.2)
            n_dom = await ev(ws, "document.querySelectorAll('.gmark-imgbox').length", nid())
            check("界面上那张图没了", n_dom == 0, "实际 %s" % n_dom)
            check("服务端那条标注也删了（不是只藏了界面）",
                  not [m for m in server_marks() if m.get("id") == img_id])
            # 删标注**不删文件**：Ctrl+Z 要能把同一条重新指回来，文件没了就永远回不去
            if url:
                try:
                    http_get(url)
                    check("删标注没连图片文件一起删（撤销还要用它）", True)
                except Exception as e:                      # noqa: BLE001
                    check("删标注没连图片文件一起删（撤销还要用它）", False, e)

            # ---- 6 Ctrl+Z 撤销插入 ----
            print("\n==== 6 再插一张 → Ctrl+Z 撤销 ====")
            r6 = await ev(ws, JS_SET_FILE, nid())
            check("又插了一张", r6 == "ok", r6)
            ok, _ = await wait_for(ws, nid,
                                   "document.querySelectorAll('.gmark-imgbox').length", 1, 40)
            await asyncio.sleep(1.2)
            sm6 = [m for m in server_marks() if m.get("kind") == "image"]
            if sm6:
                made_files.append((sm6[0].get("image") or "").rsplit("/", 1)[-1])
            check("这一张也进了服务端", len(sm6) == 1, "实际 %d 条" % len(sm6))
            await key(ws, nid, "z", "KeyZ", 90, modifiers=2)   # 2 = Ctrl
            await asyncio.sleep(1.5)
            n6 = await ev(ws, "document.querySelectorAll('.gmark-imgbox').length", nid())
            check("Ctrl+Z 之后界面上没图了", n6 == 0, "实际 %s" % n6)
            left = [m for m in server_marks() if m.get("kind") == "image"]
            check("Ctrl+Z 之后服务端也没了（真撤销，不是只藏界面）",
                  not left, "还剩 %d 条" % len(left))

            # ---- 7 层序：笔画 → 图片 → 文本框，谁先出现谁在下面 ----
            print("\n==== 7 层序：先画的在下、后放的在上 ====")
            # 7a 先画一笔（横穿页面中线，正好被随后插的图压住）
            await ev(ws, JS_CLICK_TOOL % json.dumps("画笔"), nid())
            await asyncio.sleep(0.3)
            # ⚠️ 图层矩形要**重新取**：前面几次 scrollIntoView 已经滚过页，
            #    用第 1 步那份会让拖拽落点凭空偏几十像素（拖出来的笔迹不在中线上，
            #    后面「交叠处取像素」就取不到交叠，白测）。
            layer = await rect_of(ws, nid, ".gmark-svg")
            x1, y1 = U2PX(layer, 80, 500)
            x2, y2 = U2PX(layer, 920, 500)
            await drag(ws, nid, x1, y1, x2, y2, steps=18)
            await asyncio.sleep(1.5)
            ink = [m for m in server_marks() if m.get("kind") == "ink"]
            check("笔迹落进库了", len(ink) == 1, "实际 %d 条" % len(ink))
            ink_id = ink[0].get("id") if ink else None

            # 7b 再插一张图（应该盖住那笔）
            await ev(ws, JS_CLICK_TOOL % json.dumps("选择"), nid())   # 回选择，避免图纸挡住起手
            await asyncio.sleep(0.3)
            r7 = await ev(ws, JS_SET_FILE, nid())
            check("第二张图插上去了", r7 == "ok", r7)
            ok, _ = await wait_for(ws, nid,
                                   "document.querySelectorAll('.gmark-imgbox').length", 1, 40)
            await asyncio.sleep(1.4)
            imgs7 = [m for m in server_marks() if m.get("kind") == "image"]
            if imgs7:
                made_files.append((imgs7[0].get("image") or "").rsplit("/", 1)[-1])
            img7 = imgs7[0].get("id") if imgs7 else None

            z_after_img = await ev(ws, JS_ZS, nid())
            check("此时只有「笔迹 + 图片」两个节点", len(z_after_img) == 2,
                  z_after_img)
            zs = {}
            for it in z_after_img or []:
                zs.setdefault(it["kind"], []).append(it["z"])
            check("图片的 z 高于笔迹的 z（后放的在上）",
                  bool(zs.get("image") and zs.get("graph")
                       and min(zs["image"]) > max(zs["graph"])), zs)

            # 7c 终审：截屏取「两者交叠那一点」的像素 —— 应该是图的蓝，不是笔迹的红
            ov = await rect_of(ws, nid, ".gmark-imgbox")
            if ov and layer:
                try:
                    c = await shot_pixel(ws, nid, ov["cx"], ov["cy"])
                    check("肉眼看的也是图盖住笔迹（交叠处是图的颜色）",
                          near(c, IMG_RGB, 30), "取到 %s，图是 %s、笔是红 #dc2626" % (c, IMG_RGB))
                except Exception as e:                      # noqa: BLE001
                    check("肉眼看的也是图盖住笔迹", False, e)

            # 7d 再放一个文本框（应该盖住图片）
            # ⚠️ 落点**不能落在图片上**：文字模式下 .gmark-imgbox 仍收指针（那是「按住图能拖」
            #    的入口），点在图上只会拖图、不会弹输入框。先在空白处写，再把它拖到图上 ——
            #    这样顺带也验了文本框的拖动，而且「文本框压住图片」才是真的交叠。
            await ev(ws, JS_CLICK_TOOL % json.dumps("文字框"), nid())
            await asyncio.sleep(0.3)
            layer = await rect_of(ws, nid, ".gmark-svg")     # 同上，重新取
            tx, ty = U2PX(layer, 150, 260)
            await click_at(ws, nid, tx, ty)
            ok, _ = await wait_for(ws, nid,
                                   "!!document.querySelector('.g-textbox textarea')", True, 10)
            check("点了卷面就弹出了批注输入框", ok)
            r7d = await ev(ws, JS_FILL_TEXT, nid())
            check("批注写上去并点了「放上去」", r7d == "ok", r7d)
            await asyncio.sleep(1.5)
            txt = [m for m in server_marks() if m.get("kind") == "text"]
            check("文本框落进库了", len(txt) == 1, "实际 %d 条" % len(txt))

            zs3 = await ev(ws, JS_ZS, nid())
            by_kind = {}
            for it in zs3 or []:
                by_kind.setdefault(it["kind"], []).append(it["z"])
            check("三个节点都在（笔迹 / 图片 / 文本框）",
                  all(by_kind.get(k) for k in ("graph", "image", "text")), by_kind)
            ok_order = (by_kind.get("graph") and by_kind.get("image") and by_kind.get("text")
                        and max(by_kind["graph"]) < min(by_kind["image"])
                        < min(by_kind["text"]))
            check("层序 = 创建先后：笔迹 < 图片 < 文本框", bool(ok_order), by_kind)

            # 7e 把文本框拖到图片上，验「交叠处点下去命中的是上层那个」
            await ev(ws, JS_CLICK_TOOL % json.dumps("选择"), nid())
            await asyncio.sleep(0.3)
            await scroll_to(ws, nid, ".gmark-imgbox")      # 两个都在视口里再量
            lr, ov2 = await rects_of(ws, nid, [".gmark-label", ".gmark-imgbox"])
            if lr and ov2:
                await drag(ws, nid, lr["cx"], lr["cy"], ov2["cx"], ov2["cy"], steps=10)
                await asyncio.sleep(1.2)
            lr2, ov3 = await rects_of(ws, nid, [".gmark-label", ".gmark-imgbox"])
            within = (lr2 and ov3
                      and ov3["left"] <= lr2["cx"] <= ov3["right"]
                      and ov3["top"] <= lr2["cy"] <= ov3["bottom"])
            check("文本框被拖到了图片上方（两者真的交叠）", bool(within),
                  lr2 and ov3 and "标签中心 (%.0f,%.0f) / 图 left %.0f right %.0f top %.0f bottom %.0f"
                  % (lr2["cx"], lr2["cy"], ov3["left"], ov3["right"],
                     ov3["top"], ov3["bottom"]))
            if lr2:
                top = await ev(ws, JS_TOP_AT % (lr2["cx"], lr2["cy"]), nid())
            else:
                top = "(没找到 .gmark-label)"
            check("交叠处点下去命中的是文本框（不是下面的图）", top == "label", top)

            # ---- 8 刷新后还在、位置大小不变 ----
            print("\n==== 8 刷新后位置 / 大小 / 图都还在 ====")
            before_reload = {m.get("id"): m.get("geo") for m in server_marks()
                             if m.get("kind") in ("image", "text", "ink")}
            await send(ws, nid(), "Page.reload", ignoreCache=True)
            await asyncio.sleep(1.0)
            ok, _ = await wait_for(
                ws, nid,
                """(() => {
                  const img = document.querySelector('.grade-page-img');
                  return !!(img && img.complete && img.naturalWidth > 200 &&
                            document.querySelectorAll('.gmark-imgbox').length === 1);
                })()""", True, 60)
            check("刷新后图还在（一张图片框）", ok)
            zi = await rect_of(ws, nid, ".gmark-imgbox")
            check("刷新后图片框有真实尺寸（不是塌成一条线）",
                  bool(zi) and zi["w"] > 60 and zi["h"] > 40,
                  zi and "%.0fx%.0f" % (zi["w"], zi["h"]))
            src = await ev(ws, """(() => {
              const im = document.querySelector('.gmark-imgbox img.gmark-img');
              return im ? (im.getAttribute('src') || '') : '';
            })()""", nid())
            check("刷新后 img.src 仍指着自家那张图", src.startswith("/api/mark-images/"), src)
            # 服务端一条没少
            after_reload = {m.get("id"): m.get("geo") for m in server_marks()
                            if m.get("kind") in ("image", "text", "ink")}
            check("刷新前后服务端标注完全一致（位置/大小都没被冲掉）",
                  before_reload == after_reload,
                  "前 %s / 后 %s" % (before_reload, after_reload))
            # 几何真的还原了（按 0~1000 反算回像素，允许 2px 舍入）
            if zi:
                g7 = [m for m in server_marks() if m.get("id") == img7]
                g7 = (g7[0].get("geo") if g7 else None) or []
                if len(g7) >= 4:
                    exp_w = (g7[2] - g7[0]) / 1000.0 * layer["w"]
                    exp_h = (g7[3] - g7[1]) / 1000.0 * layer["h"]
                    check("刷新后屏幕上的宽高 = 库里 geo 反算出来的宽高",
                          abs(zi["w"] - exp_w) <= 2 and abs(zi["h"] - exp_h) <= 2,
                          "屏幕 %.1fx%.1f / 反算 %.1fx%.1f" % (zi["w"], zi["h"], exp_w, exp_h))

            # ---- 9 三页都得能插（workbench 那边另跑）----
            print("\n==== 9 小结 ====")
            print("  本轮建过 %d 条标注、%d 个图片文件（结束会清掉）"
                  % (len(server_marks()) - len(_before_ids), len(made_files)))
            _ = ink_id

    finally:
        try:
            proc.terminate()
        except Exception:                                   # noqa: BLE001
            pass
        cleanup_marks()

    print("\n==== 结果 ====")
    if fails:
        print("FAIL %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
