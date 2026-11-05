# -*- coding: utf-8 -*-
"""真机验证：**新版工作台**（workbench.html）的插图功能（0039）。

⚠️ 为什么 SPA 那份跑绿了还要单独跑这一遍：
   · `workbench.html` 是用户在用的那一版，SPA 只负责跳过来；
   · 它**不引 app.css**，`.gmark-*` 的样式是逐字拷贝进去的 ——
     改样式漏了这份，DOM 检查全绿而这页功能是死的（记忆里已有的坑）；
   · 它是**多页竖排**、没有「当前页」，插图落点靠「老师最后碰过的那一页」
     （`imgPage`）—— 这条路径 SPA 那边根本没有；
   · 工具条是 Element Plus 的 radio-button / button，选择器和 SPA 完全不同。

⚠️ 规矩同 SPA 那份：只走 DOM + HTTP 两条真通道；每条断言都要再查一次服务端；
   只删「本次新建的 id / 文件」，开始前快照的那批一律不动。

跑法：
  SHIKE_BASE=http://127.0.0.1:8001 SHIKE_GID=30 \
     backend/.venv/Scripts/python.exe test_grade_mark_image_workbench.py
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
PORT = 9337

IMG_RGB = (37, 99, 235)
CANVAS = (300, 200)
WANT_BOX_W = 360
WANT_BOX_H = round(WANT_BOX_W * CANVAS[1] / CANVAS[0])
WANT_GEO = [320, 380, 320 + WANT_BOX_W, 380 + WANT_BOX_H]

fails = []
made_files = []
_before_ids = set()
MARKS = None
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
    return http_json("/api/homework-grades/%d" % GID)["marks"]


def img_size(url):
    with Image.open(io.BytesIO(http_get(url))) as im:
        return im.size


def probe():
    global MARKS
    try:
        info = http_json("/api/system/info")
    except Exception as e:                                  # noqa: BLE001
        print("SKIP: 服务没在跑（%s）" % e)
        return False
    rev = str(info.get("revision") or "")
    if rev < "0039":
        print("SKIP: 服务还停在 revision %s —— 重启一次让它迁到 0039 再跑" % (rev or "?"))
        return False
    up = (info.get("dirs") or {}).get("uploads")
    MARKS = (Path(up) if up
             else Path(__file__).resolve().parent / "data" / "uploads") / "grade_marks"
    print("  批注图目录：%s" % MARKS)
    try:
        g = http_json("/api/homework-grades/%d" % GID)
    except Exception as e:                                  # noqa: BLE001
        print("SKIP: 拿不到 gid=%d（%s）" % (GID, e))
        return False
    if not g.get("pages"):
        print("SKIP: gid=%d 还没有页图" % GID)
        return False
    return True


def note_grade_marks():
    try:
        return {p.name for p in MARKS.iterdir() if p.is_file()}
    except Exception:                                       # noqa: BLE001
        return set()


def cleanup_marks():
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


# ------------------------------------------------------------------ CDP
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
    await asyncio.sleep(0.2)


async def drag(ws, nid, x1, y1, x2, y2, steps=12):
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


JS_RECTS = """(sels => sels.map(s => {
  const e = document.querySelector(s);
  if (!e) return null;
  const r = e.getBoundingClientRect();
  return {left: r.left, top: r.top, w: r.width, h: r.height,
          cx: r.left + r.width / 2, cy: r.top + r.height / 2,
          right: r.right, bottom: r.bottom};
}))(%s)"""


async def rects_of(ws, nid, sels):
    """一次拿全，保证几个矩形在同一帧（逐个 scrollIntoView 会得到不同坐标系）。"""
    return await ev(ws, JS_RECTS % json.dumps(list(sels)), nid())


async def scroll_to(ws, nid, sel):
    await ev(ws, """(() => {
      const e = document.querySelector(%s);
      if (e) e.scrollIntoView({block: 'center', inline: 'center'});
      return !!e;
    })()""" % json.dumps(sel), nid())
    await asyncio.sleep(0.4)


def U2PX(r, ux, uy):
    return r["left"] + ux / 1000.0 * r["w"], r["top"] + uy / 1000.0 * r["h"]


async def shot_pixel(ws, nid, x, y, box=3):
    r = await send(ws, nid(), "Page.captureScreenshot", format="png")
    raw = base64.b64decode(r["data"])
    iw = await ev(ws, "window.innerWidth", nid())
    with Image.open(io.BytesIO(raw)) as im:
        rgb = im.convert("RGB")
        k = rgb.size[0] / float(iw or rgb.size[0])
        cx, cy = int(round(x * k)), int(round(y * k))
        r0 = max(1, int(round(box * k)))
        px = [rgb.getpixel((i, j))
              for i in range(max(0, cx - r0), min(rgb.size[0], cx + r0 + 1))
              for j in range(max(0, cy - r0), min(rgb.size[1], cy + r0 + 1))]
    n = len(px) or 1
    return tuple(round(sum(c[i] for c in px) / n) for i in range(3))


def near(c1, c2, tol=30):
    return all(abs(a - b) <= tol for a, b in zip(c1, c2))


# ------------------------------------------------------------------ 页面动作
# 插图按钮用 title 认（这页按钮多，按文字认会撞上「判定依据」那边的字）
JS_SET_FILE = """(async () => {
  /* ⚠️ 这页不止一个 image/* 文件框（判定依据也能传图）。
     认「紧挨着插图按钮的那个」—— 它们在同一格工具条里，是兄弟节点。 */
  const btn = [...document.querySelectorAll('button')]
    .find(b => String(b.getAttribute('title') || '').indexOf('贴到卷面上') >= 0);
  if (!btn) return 'no-button';
  let el = null, n = btn.nextElementSibling;
  while (n && !el) {
    if (n.tagName === 'INPUT' && n.type === 'file') el = n;
    else el = n.querySelector && n.querySelector('input[type=file]');
    n = n.nextElementSibling;
  }
  if (!el) el = document.querySelector('input[type=file][accept="image/*"]');
  if (!el) return 'no-input';
  const c = document.createElement('canvas');
  c.width = %d; c.height = %d;
  const g = c.getContext('2d');
  g.fillStyle = 'rgb(%d,%d,%d)'; g.fillRect(0, 0, c.width, c.height);
  g.fillStyle = '#ffffff'; g.fillRect(8, 8, 26, 26);
  const blob = await new Promise(r => c.toBlob(r, 'image/png'));
  const dt = new DataTransfer();
  dt.items.add(new File([blob], 'mark_probe.png', {type: 'image/png'}));
  el.files = dt.files;
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return 'ok';
})()""" % (CANVAS[0], CANVAS[1], IMG_RGB[0], IMG_RGB[1], IMG_RGB[2])

# 工具是 el-radio-button，文字里带 emoji
JS_PICK_TOOL = """(() => {
  const want = %s;
  const b = [...document.querySelectorAll('.el-radio-button__inner')]
    .find(e => e.textContent.trim() === want);
  if (!b) return 'no-button';
  b.click();
  return 'ok';
})()"""

JS_FILL_TEXT = """(() => {
  const ta = document.querySelector('.el-dialog textarea');
  if (!ta) return 'no-textarea';
  ta.value = 'PROBE 批注';
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  const btn = [...document.querySelectorAll('.el-dialog button')]
    .find(b => b.textContent.trim() === '放上去');
  if (!btn) return 'no-button';
  btn.click();
  return 'ok';
})()"""

JS_ZS = """(() => [...document.querySelectorAll('#pg%d .gmark-node')].map(n => ({
  z: parseInt(getComputedStyle(n).zIndex, 10),
  kind: n.querySelector('.gmark-imgbox') ? 'image'
      : n.querySelector('.gmark-label') ? 'text'
      : (n.querySelector('svg.gmark') ? 'graph' : '?')
})))()"""

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

    url = BASE + "/workbench.html?gid=%d&from=workbench&sid=1" % GID
    prof = tempfile.mkdtemp(prefix="edge_gw_")
    proc = subprocess.Popen([EDGE, "--headless=new",
                             "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + prof, "--no-first-run",
                             "--disable-gpu", "--window-size=1500,1500",
                             "--no-proxy-server", url],
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
            await send(ws, nid(), "Network.enable")
            await send(ws, nid(), "Network.setCacheDisabled", cacheDisabled=True)

            # ---- 1 挂载 ----
            print("\n==== 1 工作台挂载 ====")
            ok, _ = await wait_for(
                ws, nid,
                """(() => {
                  const im = document.querySelector('#pg1 img');
                  return !!(im && im.complete && im.naturalWidth > 200 &&
                            document.querySelector('#pg1 .gmark-svg') &&
                            [...document.querySelectorAll('button')]
                              .some(b => String(b.getAttribute('title') || '')
                                        .indexOf('贴到卷面上') >= 0));
                })()""", True, 90)
            check("工作台挂载、第 1 页有真实像素、插图按钮在", ok)
            n_layers = await ev(ws, "document.querySelectorAll('.gmark-layer').length", nid())
            check("每一页各挂了一份标注层（多页竖排）", n_layers >= 2, "实际 %s 份" % n_layers)
            n_img = await ev(ws, """[...document.querySelectorAll('button')]
              .filter(b => String(b.getAttribute('title') || '').indexOf('贴到卷面上') >= 0).length""",
                             nid())
            check("插图按钮只有一个（不会认错）", n_img == 1, "实际 %s" % n_img)

            layer = (await rects_of(ws, nid, ["#pg1 .gmark-svg"]))[0]
            check("第 1 页标注层有真实尺寸", bool(layer) and layer["w"] > 200,
                  layer and "%.0fx%.0f" % (layer["w"], layer["h"]))

            # ---- 2 插一张图 ----
            print("\n==== 2 插图片 ====")
            r2 = await ev(ws, JS_SET_FILE, nid())
            check("文件框收到了这张图", r2 == "ok", r2)
            ok, n = await wait_for(ws, nid,
                                   "document.querySelectorAll('#pg1 .gmark-imgbox').length", 1, 40)
            check("第 1 页卷面上出现了图片框", ok, "实际 %s" % n)
            await asyncio.sleep(1.2)
            sm = [m for m in server_marks() if m.get("kind") == "image"]
            check("服务端有一条 kind=image 的标注", len(sm) == 1, "实际 %d 条" % len(sm))
            m2 = sm[0] if sm else {}
            url2 = m2.get("image") or ""
            check("落在第 1 页", m2.get("page_no") == 1, m2.get("page_no"))
            check("image 是自家 /api/mark-images/ 的 URL", url2.startswith("/api/mark-images/"),
                  url2 or "(空)")
            check("geo 是等比居中后的那个框", m2.get("geo") == WANT_GEO,
                  "%s 期望 %s" % (m2.get("geo"), WANT_GEO))
            if url2:
                made_files.append(url2.rsplit("/", 1)[-1])
                try:
                    check("取回来的图与插进去的是同一张（像素尺寸）",
                          img_size(url2) == CANVAS, img_size(url2))
                except Exception as e:                      # noqa: BLE001
                    check("取回来的图与插进去的是同一张", False, e)
            img_id = m2.get("id")

            # ---- 3 拖动 ----
            print("\n==== 3 鼠标拖拽移动 ====")
            await scroll_to(ws, nid, "#pg1 .gmark-imgbox")
            b3 = (await rects_of(ws, nid, ["#pg1 .gmark-imgbox"]))[0]
            try:
                await drag(ws, nid, b3["cx"], b3["cy"], b3["cx"] + 90, b3["cy"] + 60)
                await asyncio.sleep(1.2)
                mm = [m for m in server_marks() if m.get("id") == img_id]
                g3 = (mm[0].get("geo") if mm else None) or []
                check("拖完之后服务端 geo 真的挪了", bool(g3) and g3[0] > WANT_GEO[0]
                      and g3[1] > WANT_GEO[1], "%s（拖之前 %s）" % (g3, WANT_GEO))
                check("拖动只挪位置、不改大小",
                      len(g3) >= 4 and (g3[3] - g3[1]) == (WANT_GEO[3] - WANT_GEO[1]),
                      g3 and "高 %s 期望 %s" % (g3[3] - g3[1], WANT_GEO[3] - WANT_GEO[1]))
            except Exception as e:                          # noqa: BLE001
                check("拖完之后服务端 geo 真的挪了", False, e)
                g3 = []

            # ---- 4 缩放 ----
            print("\n==== 4 拖右下角小方块改大小 ====")
            h = (await rects_of(ws, nid, ["#pg1 .gmark-imgbox .gmark-resize"]))[0]
            if not h:
                check("图片框右下角有缩放把手", False, "没找到 .gmark-resize")
            else:
                check("图片框右下角有缩放把手", h["w"] >= 8 and h["h"] >= 8,
                      "%.0fx%.0f" % (h["w"], h["h"]))
                try:
                    await drag(ws, nid, h["cx"], h["cy"], h["cx"] + 120, h["cy"] + 80)
                    await asyncio.sleep(1.2)
                    mm = [m for m in server_marks() if m.get("id") == img_id]
                    g4 = (mm[0].get("geo") if mm else None) or []
                    w0 = (g3[2] - g3[0]) if len(g3) >= 4 else WANT_GEO[2] - WANT_GEO[0]
                    w1 = (g4[2] - g4[0]) if len(g4) >= 4 else 0
                    check("拖完之后服务端那张图真的变宽了", w1 > w0 + 20,
                          "宽 %s → %s" % (w0, w1))
                    check("缩放时左上角不动", len(g4) >= 4 and g4[:2] == g3[:2],
                          "%s vs %s" % (g4[:2], g3[:2]))
                except Exception as e:                      # noqa: BLE001
                    check("拖完之后服务端那张图真的变宽了", False, e)

            # ---- 5 Delete 删掉 ----
            print("\n==== 5 选中图片 → Delete 删掉 ====")
            b5 = (await rects_of(ws, nid, ["#pg1 .gmark-imgbox"]))[0]
            if b5:
                await click_at(ws, nid, b5["cx"], b5["cy"])
                await asyncio.sleep(0.5)
            sel = await ev(ws, "!!document.querySelector('#pg1 .gmark-imgbox.act')", nid())
            check("点一下图片就被选中了（act 高亮）", sel is True)
            await key(ws, nid, "Delete", "Delete", 46)
            await asyncio.sleep(1.3)
            n_dom = await ev(ws, "document.querySelectorAll('#pg1 .gmark-imgbox').length", nid())
            check("界面上那张图没了", n_dom == 0, "实际 %s" % n_dom)
            check("服务端那条标注也删了（不是只藏了界面）",
                  not [m for m in server_marks() if m.get("id") == img_id])
            if url2:
                try:
                    http_get(url2)
                    check("删标注没连图片文件一起删（撤销还要用它）", True)
                except Exception as e:                      # noqa: BLE001
                    check("删标注没连图片文件一起删（撤销还要用它）", False, e)

            # ---- 6 Ctrl+Z 撤销插入 ----
            print("\n==== 6 再插一张 → Ctrl+Z 撤销 ====")
            r6 = await ev(ws, JS_SET_FILE, nid())
            check("又插了一张", r6 == "ok", r6)
            await wait_for(ws, nid,
                           "document.querySelectorAll('#pg1 .gmark-imgbox').length", 1, 40)
            await asyncio.sleep(1.2)
            sm6 = [m for m in server_marks() if m.get("kind") == "image"]
            if sm6:
                made_files.append((sm6[0].get("image") or "").rsplit("/", 1)[-1])
            check("这一张也进了服务端", len(sm6) == 1, "实际 %d 条" % len(sm6))
            n_undo = await ev(ws, """(() => {
              const b = [...document.querySelectorAll('button')]
                .find(x => x.textContent.indexOf('撤销') >= 0);
              return b ? b.textContent.replace(/\\s+/g, '') : '';
            })()""", nid())
            check("撤销按钮上记着有一步可撤（undoStack 非空）", '（' in (n_undo or ''), n_undo)
            await key(ws, nid, "z", "KeyZ", 90, modifiers=2)
            await asyncio.sleep(1.6)
            n6 = await ev(ws, "document.querySelectorAll('#pg1 .gmark-imgbox').length", nid())
            check("Ctrl+Z 之后界面上没图了", n6 == 0, "实际 %s" % n6)
            left = [m for m in server_marks() if m.get("kind") == "image"]
            check("Ctrl+Z 之后服务端也没了（真撤销）", not left, "还剩 %d 条" % len(left))

            # ---- 7 落点页 = 最后碰过的那一页 ----
            print("\n==== 7 插图落在「最后碰过的那一页」（工作台特有）====")
            await scroll_to(ws, nid, "#pg2 .gmark-svg")
            p2 = (await rects_of(ws, nid, ["#pg2 .gmark-svg"]))[0]
            if not p2:
                check("第 2 页的标注层在", False, "没找到 #pg2 .gmark-svg")
            else:
                # 点一下第 2 页（触发 onPageDown → imgPage = 2）
                await click_at(ws, nid, p2["cx"], p2["cy"])
                await asyncio.sleep(0.4)
                r7 = await ev(ws, JS_SET_FILE, nid())
                check("在第 2 页插了一张", r7 == "ok", r7)
                ok, _ = await wait_for(
                    ws, nid,
                    "document.querySelectorAll('#pg2 .gmark-imgbox').length", 1, 40)
                check("图片框出现在第 2 页（不是第 1 页）", ok)
                await asyncio.sleep(1.2)
                s7 = [m for m in server_marks() if m.get("kind") == "image"]
                if s7:
                    made_files.append((s7[0].get("image") or "").rsplit("/", 1)[-1])
                check("服务端记的是第 2 页", s7 and s7[0].get("page_no") == 2,
                      s7[0].get("page_no") if s7 else "(没有)")
                check("第 1 页没有被误插", await ev(
                    ws, "document.querySelectorAll('#pg1 .gmark-imgbox').length", nid()) == 0)
                # 收拾掉（后面层序要在第 1 页重来）
                if s7:
                    try:
                        http_delete("/api/grade-marks/%d" % int(s7[0]["id"]))
                    except Exception as e:                  # noqa: BLE001
                        print("  ⚠️ 删不掉第 2 页那条：%s" % e)
                    await asyncio.sleep(0.8)
                    await send(ws, nid(), "Page.reload", ignoreCache=False)
                    await asyncio.sleep(1.0)
                    await wait_for(
                        ws, nid,
                        """(() => {
                          const im = document.querySelector('#pg1 img');
                          return !!(im && im.complete && im.naturalWidth > 200
                                    && document.querySelector('#pg1 .gmark-svg'));
                        })()""", True, 90)

            # ---- 8 层序：笔迹 → 图片 → 文本框 ----
            print("\n==== 8 层序：先画的在下、后放的在上 ====")
            await scroll_to(ws, nid, "#pg1 .gmark-svg")
            rt = await ev(ws, JS_PICK_TOOL % json.dumps("✏️ 笔"), nid())
            check("切到「笔」", rt == "ok", rt)
            await asyncio.sleep(0.3)
            layer = (await rects_of(ws, nid, ["#pg1 .gmark-svg"]))[0]
            x1, y1 = U2PX(layer, 80, 500)
            x2, y2 = U2PX(layer, 920, 500)
            await drag(ws, nid, x1, y1, x2, y2, steps=18)
            await asyncio.sleep(1.5)
            ink = [m for m in server_marks() if m.get("kind") == "ink"]
            check("笔迹落进库了", len(ink) == 1, "实际 %d 条" % len(ink))

            rv = await ev(ws, JS_PICK_TOOL % json.dumps("👁 只看"), nid())
            check("切回「只看」", rv == "ok", rv)
            await asyncio.sleep(0.3)
            r8 = await ev(ws, JS_SET_FILE, nid())
            check("再插一张图（应盖住笔迹）", r8 == "ok", r8)
            await wait_for(ws, nid,
                           "document.querySelectorAll('#pg1 .gmark-imgbox').length", 1, 40)
            await asyncio.sleep(1.4)
            i8 = [m for m in server_marks() if m.get("kind") == "image"]
            if i8:
                made_files.append((i8[0].get("image") or "").rsplit("/", 1)[-1])
            img8 = i8[0].get("id") if i8 else None

            z2 = await ev(ws, JS_ZS % 1, nid())
            check("第 1 页此时只有「笔迹 + 图片」两个节点", len(z2) == 2, z2)
            zs = {}
            for it in z2 or []:
                zs.setdefault(it["kind"], []).append(it["z"])
            check("图片的 z 高于笔迹的 z", bool(zs.get("image") and zs.get("graph")
                  and min(zs["image"]) > max(zs["graph"])), zs)

            await scroll_to(ws, nid, "#pg1 .gmark-imgbox")
            ov = (await rects_of(ws, nid, ["#pg1 .gmark-imgbox"]))[0]
            if ov:
                try:
                    c = await shot_pixel(ws, nid, ov["cx"], ov["cy"])
                    check("肉眼看的也是图盖住笔迹（交叠处是图的颜色）",
                          near(c, IMG_RGB), "取到 %s，图是 %s" % (c, IMG_RGB))
                except Exception as e:                      # noqa: BLE001
                    check("肉眼看的也是图盖住笔迹", False, e)

            # 文本框：先在空白处写，再拖到图上
            rt2 = await ev(ws, JS_PICK_TOOL % json.dumps("🔤 文字"), nid())
            check("切到「文字」", rt2 == "ok", rt2)
            await asyncio.sleep(0.3)
            layer = (await rects_of(ws, nid, ["#pg1 .gmark-svg"]))[0]
            tx, ty = U2PX(layer, 150, 260)
            await click_at(ws, nid, tx, ty)
            ok, _ = await wait_for(ws, nid,
                                   "!!document.querySelector('.el-dialog textarea')", True, 10)
            check("点了卷面就弹出了写评语的弹框", ok)
            r8d = await ev(ws, JS_FILL_TEXT, nid())
            check("评语写上去并点了「放上去」", r8d == "ok", r8d)
            await asyncio.sleep(1.6)
            txt = [m for m in server_marks() if m.get("kind") == "text"]
            check("文本框落进库了", len(txt) == 1, "实际 %d 条" % len(txt))

            z3 = await ev(ws, JS_ZS % 1, nid())
            bk = {}
            for it in z3 or []:
                bk.setdefault(it["kind"], []).append(it["z"])
            check("三个节点都在（笔迹 / 图片 / 文本框）",
                  all(bk.get(k) for k in ("graph", "image", "text")), bk)
            ok_order = (bk.get("graph") and bk.get("image") and bk.get("text")
                        and max(bk["graph"]) < min(bk["image"]) < min(bk["text"]))
            check("层序 = 创建先后：笔迹 < 图片 < 文本框", bool(ok_order), bk)

            await ev(ws, JS_PICK_TOOL % json.dumps("👁 只看"), nid())
            await asyncio.sleep(0.3)
            await scroll_to(ws, nid, "#pg1 .gmark-imgbox")
            lr, ov2 = await rects_of(ws, nid, ["#pg1 .gmark-label", "#pg1 .gmark-imgbox"])
            if lr and ov2:
                await drag(ws, nid, lr["cx"], lr["cy"], ov2["cx"], ov2["cy"], steps=10)
                await asyncio.sleep(1.2)
            lr2, ov3 = await rects_of(ws, nid, ["#pg1 .gmark-label", "#pg1 .gmark-imgbox"])
            within = (lr2 and ov3 and ov3["left"] <= lr2["cx"] <= ov3["right"]
                      and ov3["top"] <= lr2["cy"] <= ov3["bottom"])
            check("文本框被拖到了图片上方（两者真的交叠）", bool(within),
                  lr2 and ov2 and "标签中心 (%.0f,%.0f) / 图 left %.0f top %.0f"
                  % (lr2["cx"], lr2["cy"], ov3["left"], ov3["top"]))
            top = await ev(ws, JS_TOP_AT % (lr2["cx"], lr2["cy"]), nid()) if lr2 else "?"
            check("交叠处点下去命中的是文本框（不是下面的图）", top == "label", top)

            # ---- 9 刷新后还在 ----
            print("\n==== 9 刷新后位置 / 大小 / 图都还在 ====")
            before_reload = {m.get("id"): m.get("geo") for m in server_marks()
                             if m.get("kind") in ("image", "text", "ink")}
            await send(ws, nid(), "Page.reload", ignoreCache=True)
            await asyncio.sleep(1.0)
            ok, _ = await wait_for(
                ws, nid,
                """(() => {
                  const im = document.querySelector('#pg1 img');
                  return !!(im && im.complete && im.naturalWidth > 200 &&
                            document.querySelectorAll('#pg1 .gmark-imgbox').length === 1);
                })()""", True, 90)
            check("刷新后图还在（第 1 页一张图片框）", ok)
            await scroll_to(ws, nid, "#pg1 .gmark-imgbox")
            zi = (await rects_of(ws, nid, ["#pg1 .gmark-imgbox", "#pg1 .gmark-svg"]))
            zr, lz = zi[0], zi[1]
            check("刷新后图片框有真实尺寸（不是塌成一条线）",
                  bool(zr) and zr["w"] > 60 and zr["h"] > 40,
                  zr and "%.0fx%.0f" % (zr["w"], zr["h"]))
            src = await ev(ws, """(() => {
              const im = document.querySelector('#pg1 .gmark-imgbox img.gmark-img');
              return im ? (im.getAttribute('src') || '') : '';
            })()""", nid())
            check("刷新后 img.src 仍指着自家那张图", src.startswith("/api/mark-images/"), src)
            after_reload = {m.get("id"): m.get("geo") for m in server_marks()
                            if m.get("kind") in ("image", "text", "ink")}
            check("刷新前后服务端标注完全一致", before_reload == after_reload,
                  "前 %s / 后 %s" % (before_reload, after_reload))
            g8 = [m for m in server_marks() if m.get("id") == img8]
            g8 = (g8[0].get("geo") if g8 else None) or []
            if zr and lz and len(g8) >= 4:
                exp_w = (g8[2] - g8[0]) / 1000.0 * lz["w"]
                exp_h = (g8[3] - g8[1]) / 1000.0 * lz["h"]
                check("刷新后屏幕宽高 = 库里 geo 反算的宽高",
                      abs(zr["w"] - exp_w) <= 2 and abs(zr["h"] - exp_h) <= 2,
                      "屏幕 %.1fx%.1f / 反算 %.1fx%.1f" % (zr["w"], zr["h"], exp_w, exp_h))

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
