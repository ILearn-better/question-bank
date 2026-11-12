# -*- coding: utf-8 -*-
r"""出卷页「修」→ 录题页补答案：URL 直达那一条链是不是真的通。

出卷时发现某道题没答案、或者录入有问题（用户 2026-11-12 提的第 5 条），
卷面上点「修」会新开一个标签页：

    /entry.html?answer=1&qid=<题目id>

这一跳有三处能静默出错，而且**都不会报错**：
  ① 参数名/拼写对不上（`answer=1` 漏了 → 页面正常打开，只是还停在录题模式）；
  ② 进了补答案模式但没选中那道题（老师以为「已经定位到了」，结果一保存写到了
     上一次选中的题上）；
  ③ 那道题不在「最近 50 条」里时要单取一条 —— 单取接口挂了同样静默。

所以只能**真浏览器**验：`entry.html` 是 2346 行的零构建单文件页（还带 Element Plus），
jsdom 挂不起来，静态断言也证明不了「打开之后真的选中了」。

判据都是老师看得见的东西：
  A 面板标题切到「补答案」
  B 顶部模式按钮里「补答案」是选中的那个（不是只有内部状态变了、按钮还停在录题）
  C 列表里那道题被选中（`.pick-item.on` 恰好 1 个），且题干对得上
  D 按钮变成「保存答案」
  E 表单里读进了**这道题原来的答案**（说明 pickQuestion 真的执行了）

服务没跑 / 库里没题 → SKIP 并退出 0（探针不该把回归弄红）。
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

BASE = os.environ.get("SHIKE_BASE") or (
    "http://127.0.0.1:8010" if Path(__file__).resolve().parents[1].name == "question-bank-ai"
    else "http://127.0.0.1:8000")
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9339
OUTDIR = Path(tempfile.gettempdir())

# 本机常年挂着 http_proxy：urllib 探 127.0.0.1 会被送进代理 → 502，看着像服务坏了
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

fails = []


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("OK " if ok else "FAIL", name, ("  " + str(detail)) if detail else ""))
    if not ok:
        fails.append(name)


def http_json(path):
    with OPENER.open(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def pick_question():
    """从库里挑一道**有答案**的题 —— 第 E 条要看「表单里读进了原答案」。"""
    d = http_json("/api/questions/search?limit=20&with_answer=true&sort=created")
    items = d.get("items") or []
    for q in items:
        if (q.get("answer") or "").strip():
            return q
    return items[0] if items else None


async def send(ws, mid, method, **params):
    await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mid:
            if "error" in msg:
                raise RuntimeError("%s: %s" % (method, msg["error"]))
            return msg.get("result", {})


async def ev(ws, mid, expr):
    r = await send(ws, mid, "Runtime.evaluate", expression=expr,
                   returnByValue=True, awaitPromise=True)
    res = r.get("result", {})
    if res.get("subtype") == "error":
        raise RuntimeError("页面里报错: %s" % res.get("description"))
    return res.get("value")


JS_STATE = """(() => {
  const txt = (e) => (e ? (e.textContent || '').replace(/\\s+/g, ' ').trim() : '');
  const picked = document.querySelectorAll('.pick-item.on');
  return {
    titles: [...document.querySelectorAll('.panel-title')].map(txt),
    // Element Plus 的 el-radio-group + el-radio-button：选中项带 is-active
    modes: [...document.querySelectorAll('.el-radio-button')].map((e) => ({
      t: txt(e), on: e.classList.contains('is-active'),
    })),
    pickedCount: picked.length,
    pickedText: picked.length ? txt(picked[0]).slice(0, 60) : '',
    saveBtn: [...document.querySelectorAll('button')].some((b) => /保存答案/.test(txt(b))),
    firstArea: (document.querySelector('textarea') || {}).value || '',
  };
})()"""


async def main():
    try:
        q = pick_question()
    except Exception as e:                                  # noqa: BLE001
        print("SKIP: 服务没在跑（%s）—— 先双击「启动拾课.bat」" % e)
        return 0
    if not q:
        print("SKIP: 题库里一道题都没有，没法验")
        return 0

    qid = str(q["id"])
    stem = (q.get("content") or "").strip().replace("\n", " ")
    url = BASE + "/entry.html?answer=1&qid=" + qid
    print("目标题 %s：%s…" % (qid, stem[:36]))
    print("打开", url)

    prof = tempfile.mkdtemp(prefix="edge_entryurl_")
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--remote-debugging-port=%d" % PORT,
         "--user-data-dir=" + prof, "--no-first-run", "--disable-gpu",
         "--window-size=1680,1400", "--no-proxy-server", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mid = [0]

    def nid():
        mid[0] += 1
        return mid[0]

    try:
        ws_url = None
        for _ in range(60):
            try:
                with OPENER.open("http://127.0.0.1:%d/json/list" % PORT, timeout=1) as r:
                    tabs = json.load(r)
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:                               # noqa: BLE001
                pass
            time.sleep(0.5)
        if not ws_url:
            check("连得上 Edge 的调试端口", False)
            return 1

        async with websockets.connect(ws_url, max_size=64 << 20) as ws:
            await send(ws, nid(), "Runtime.enable")
            await send(ws, nid(), "Page.enable")
            await send(ws, nid(), "Network.enable")
            # 缓存不清会拿到上一版 JS —— 改完 entry.html 重跑还是旧行为
            await send(ws, nid(), "Network.setCacheDisabled", cacheDisabled=True)
            await send(ws, nid(), "Page.navigate", url=url)
            await asyncio.sleep(8)          # 等 loadCurricula / loadTags / searchQuestions 回来

            st = await ev(ws, nid(), JS_STATE)
            print("\n==== 页面状态 ====")
            print(json.dumps(st, ensure_ascii=False, indent=1))

            check("A 面板标题切到「补答案」",
                  any("补答案" in t for t in st["titles"]), st["titles"])
            check("B 顶部模式按钮里「补答案」是选中的那个",
                  [m["on"] for m in st["modes"] if "补答案" in m["t"]] == [True],
                  st["modes"])
            check("C ★ 列表里那道题被选中（.pick-item.on 恰好 1 个）", st["pickedCount"] == 1,
                  st["pickedCount"])
            check("C2 ★ 选中的是我们跳过来时指定的那道题",
                  bool(st["pickedText"]) and st["pickedText"][:12] in stem,
                  "页面：%r / 目标：%r" % (st["pickedText"][:20], stem[:20]))
            check("D 按钮变成「保存答案」", st["saveBtn"])
            check("E ★ 表单里读进了这道题原来的答案（pickQuestion 真执行了）",
                  bool(st["firstArea"].strip()) and st["firstArea"].strip()[:6] in (q.get("answer") or ""),
                  st["firstArea"][:40])

            p = OUTDIR / "_entry_answer_url.png"
            r = await send(ws, nid(), "Page.captureScreenshot", format="png")
            p.write_bytes(base64.b64decode(r["data"]))
            print("  截图 ->", p)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            pass

    print("\n" + "=" * 60)
    if fails:
        print("FAIL %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
