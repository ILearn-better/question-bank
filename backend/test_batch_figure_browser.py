# -*- coding: utf-8 -*-
"""真机验证：批量审核页的配图四件事（分档 / 筛选 / 批量确认 / 候选点选）。

⚠️ **这个测试会短暂改真库**（点候选会落库、批量确认会摘 flag），跑完全部还原。
   所以它必须是「可反复跑」的：快照 → 动作 → finally 恢复。
⚠️ 需要服务已经在跑（用户双击「启动拾课.bat」）；连不上就 SKIP 退出 0，
   不该因为「这会儿没开服务」把回归弄红。
⚠️ 依赖库里的具体 job（2fcc24d666c7 / 5b8e066d4ee7）—— 那是老师真实的待审数据，
   被清理掉后这里要跟着调，所以开头会校验，对不上就 SKIP。

⚠️ 规矩（记忆里的教训）：
  · 不读 `app._instance`（生产构建里是 null）→ 只走 DOM + HTTP 两条真通道；
  · **同一件事里必须再查一次服务端接口**：只看 DOM 会把「界面变了、库里没变、
    几秒后又跳回去」当成通过。
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets
from pathlib import Path

# 免代理 opener（契约第六节第 2 条）：本机 http_proxy 会把 127.0.0.1 的请求也送去代理，
# 回来是 502 Bad Gateway —— 于是「服务没起」被误读成「服务坏了」，
# 更坏的是 502 的响应体被当成数据往下跑（报 byte indices 之类，完全误导）。
# 本机 HTTP 一律走这个 opener；连不上就该是 ConnectionRefused，红得明白。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BASE = os.environ.get("SHIKE_BASE") or (
    "http://127.0.0.1:8010" if Path(__file__).resolve().parents[1].name == "question-bank-ai"
    else "http://127.0.0.1:8000")   # 主仓 8000 / -ai 检出 8010，SHIKE_BASE 可覆盖
JOB = "2fcc24d666c7"
DOC = "a5fa3c1a50fb"
JOB_CREATED = "2026-10-22T23:01:51"
DOC_EXT = ".docx"
TARGET_ID = "5b8e066d4ee7"
HEAD = "2026·陕西咸阳·三模"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333

# 「这题的配图需要老师核一眼」的标记 —— 后端 _FIGURE_DOUBT_FLAGS 与前端 figTierOf
# 都是这一份，测试里再抄一遍是为了**独立对账**（不是引用它们的实现）。
DOUBT = ("figure_rough", "figure_shared_candidate", "figure_dup_candidate",
         "figure_box_missing", "figure_cross_page", "figure_crop_failed")

fails = []
# SKIP 不是通过：结束时必须印出来，绝不能混进「全部通过」。
skipped: list[str] = []

def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("OK " if ok else "FAIL", name, ("  " + str(detail)) if detail else ""))
    if not ok:
        fails.append(name)

def api(path, method="GET"):
    req = urllib.request.Request(BASE + path, method=method)
    with _OPENER.open(req, timeout=20) as r:
        body = r.read().decode("utf-8")
    return json.loads(body) if body else None

def item_flags(item_id):
    """服务端真实状态（不信界面）。"""
    items = api("/api/batches/%s/items" % JOB)
    if isinstance(items, dict):
        items = items.get("items") or items.get("list") or []
    for it in items:
        if it.get("id") == item_id:
            return it
    return None

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
    return r.get("result", {}).get("value")

def probe():
    """服务在不在、目标数据还在不在。不在就 SKIP。"""
    try:
        jobs = api("/api/batches")
    except Exception as e:                                  # noqa: BLE001
        skipped.append("服务没在跑（%s）" % e)
        print("SKIP: 服务没在跑（%s）—— 先双击「启动拾课.bat」" % e)
        return None
    if not any(j.get("id") == JOB for j in jobs or []):
        skipped.append("库里已经没有 job %s" % JOB)
        print("SKIP: 库里已经没有 job %s（测试数据被清理了）" % JOB)
        return None
    items = api("/api/batches/%s/items" % JOB)
    if isinstance(items, dict):
        items = items.get("items") or items.get("list") or []
    todo = [it for it in items if it.get("figure_image")
            and any(x in (it.get("flags") or []) for x in DOUBT)]
    if not todo:
        skipped.append("job 里已经没有待核条目")
        print("SKIP: 这个 job 已经没有「待核」条目了（老师自己处理完了）")
        return None
    print("服务在跑；待核条目 %d 条，可验" % len(todo))
    return {"items": items, "todo": todo}

def snapshot(todo):
    return [{"id": it["id"], "flags": it.get("flags") or [],
             "image": it.get("figure_image") or "",
             "box": it.get("figure_box") or [0, 0, 1000, 1000],
             "page": it.get("page_no")} for it in todo]

def restore(snap):
    """把验证碰过的条目改回去。**必须无条件跑**，异常路径也要跑。"""
    if not snap:
        return
    import sqlite3
    for s0 in snap:
        cur = item_flags(s0["id"])
        if cur is None:
            continue
        if (cur.get("flags") or []) != s0["flags"]:
            db = sqlite3.connect("shike.db")
            db.execute("update batch_items set flags=? where id=?",
                       (json.dumps(s0["flags"]), s0["id"]))
            db.commit()
            db.close()
        if (cur.get("figure_image") or "") != s0["image"]:
            # 图换成候选那张了 → 用原框重裁回去（同一框裁出来内容一致）
            try:
                meta = api("/api/batches/items/%s/figure-candidates" % s0["id"])
                W, H = meta["width"], meta["height"]
                b = s0["box"]
                reg = {"page": s0["page"], "x0": b[0] / 1000.0 * W,
                       "y0": b[1] / 1000.0 * H, "x1": b[2] / 1000.0 * W,
                       "y1": b[3] / 1000.0 * H}
                body = json.dumps({"regions": [reg], "gap": 0}).encode()
                req = urllib.request.Request(
                    BASE + "/api/batches/items/%s/figure-crop" % s0["id"],
                    data=body, method="POST",
                    headers={"Content-Type": "application/json"})
                with _OPENER.open(req, timeout=30) as r:
                    assert r.status == 200
            except Exception as e:                          # noqa: BLE001
                print("  ⚠️ 还原图失败（%s）：%s" % (s0["id"], e))
            # figure-crop 会按设计摘掉可疑标记 → 再写一次
            db = sqlite3.connect("shike.db")
            db.execute("update batch_items set flags=? where id=?",
                       (json.dumps(s0["flags"]), s0["id"]))
            db.commit()
            db.close()
    print("已还原 %d 条待核条目" % len(snap))

async def main():
    print("==== 0 打开页面 ====")
    data = probe()
    if data is None:
        return 0
    snap = snapshot(data["todo"])
    prof = tempfile.mkdtemp(prefix="edge_v_")
    proc = subprocess.Popen([EDGE, "--headless=new",
                             "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + prof, "--no-first-run",
                             "--disable-gpu", "--window-size=1500,1400",
                             BASE + "/batch.html?doc=" + DOC],
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
            await asyncio.sleep(6)                          # 等 vendor + 首屏

            # ---- 1 页面挂载（白屏守卫）----
            print("\n==== 1 页面真的挂上了 ====")
            info = await ev(ws, """(() => {
              const app = document.querySelector('#app');
              const raw = app ? app.innerHTML : '';
              return {
                appLen: raw.length,
                hasMustache: /\\{\\{/.test(raw),
                vFor: /v-for=/.test(raw),
                bodyText: (document.body.innerText || '').slice(0, 200),
                jobItems: document.querySelectorAll('[data-job], .job-item, .job-row').length,
                buttons: [...document.querySelectorAll('button')].map(b => (b.innerText||'').trim()).filter(Boolean).slice(0, 25),
              };
            })()""", nid())
            check("根节点有挂载痕迹（不是白屏）", (info or {}).get("appLen", 0) > 200, info)
            check("页面里不再残留原始模板字面量",
                  not (info or {}).get("hasMustache") and not (info or {}).get("vFor"))
            print("     按 钮:", (info or {}).get("buttons"))

            # ---- 2 进入这个 job 的审核界面 ----
            print("\n==== 2 打开 job %s ====" % JOB)
            # job 卡片：`.job-card`（`@click="openJob(j.id)"`，点开后 tab 自动切 review）。
            # 卡片上没有 id，只能按 created_at 认 —— 同一文档有好几个 job。
            clicked = await ev(ws, """(() => {
              const cards = [...document.querySelectorAll('.job-card')];
              const hit = cards.find(c => (c.innerText||'').indexOf(%s) >= 0
                                          && (c.innerText||'').indexOf('%s') >= 0
                                          && (c.innerText||'').indexOf('30 块') >= 0);
              if (!hit) return {ok: false, n: cards.length,
                                texts: cards.map(c => (c.innerText||'').replace(/\\n/g,' | ').slice(0,90))};
              hit.click();
              return {ok: true};
            })()""" % (json.dumps(JOB_CREATED), '待审 75'), nid())
            print("     点击结果:", clicked)
            await asyncio.sleep(3)

            dom = await ev(ws, """(() => {
              const txt = document.body.innerText || '';
              const tiers = [...document.querySelectorAll('.el-tag')]
                 .map(t => (t.innerText||'').trim()).filter(t => t);
              return {
                tagTexts: tiers.slice(0, 40),
                figCands: document.querySelectorAll('.fig-cand').length,
                figAck: document.querySelectorAll('.fig-ack').length,
                figBox: document.querySelectorAll('.fig-box').length,
                itemCard: document.querySelectorAll('.item-card').length,
                rowItem: document.querySelectorAll('.item').length,
                hasFilter: txt.indexOf('只看要处理的') >= 0,
                hasAckAll: txt.indexOf('待核的都确认') >= 0,
                hasPick: txt.indexOf('选现成的图') >= 0,
                cards: document.querySelectorAll('.item-card, .card').length,
                text: txt.slice(0, 300),
              };
            })()""", nid())
            print("     DOM:", {k: v for k, v in (dom or {}).items() if k != "text"})
            check("配图卡片渲染出来了", (dom or {}).get("figBox", 0) > 0, (dom or {}).get("figBox"))
            check("工具栏有「只看要处理的」筛选", (dom or {}).get("hasFilter") is True)
            check("工具栏有「待核的都确认」", (dom or {}).get("hasAckAll") is True)
            check("卡片上有「选现成的图」入口", (dom or {}).get("hasPick") is True)

            # ---- 3 分档计数 vs 服务端真实状态（两条通道对账）----
            print("\n==== 3 分档计数 ↔ 服务端对账 ====")
            items = api("/api/batches/%s/items" % JOB)
            if isinstance(items, dict):
                items = items.get("items") or items.get("list") or []
            want = {"ok": 0, "check": 0, "missing": 0, "covered": 0}
            for it in items:
                f = it.get("flags") or []
                bad = any(x in DOUBT for x in f)
                if it.get("figure_image"):
                    want["check" if bad else "ok"] += 1
                elif "figure_whole_question" in f:
                    want["covered"] += 1
                elif it.get("needs_figure") or bad:
                    want["missing"] += 1
            print("     服务端算出来:", want)
            print("     -- 明细（只列非常规的）--")
            for it in items:
                f = it.get("flags") or []
                bad = any(x in f for x in DOUBT)
                t = ("check" if bad else "ok") if it.get("figure_image") else (
                    "covered" if "figure_whole_question" in f else
                    ("missing" if (it.get("needs_figure") or bad) else ""))
                if t in ("missing", "covered"):
                    print("       %s tier=%-8s needs=%-5s fig=%-5s flags=%s" % (
                        it["id"], t, it.get("needs_figure"),
                        "Y" if it.get("figure_image") else "-", f))
            print("     服务端 items 条数:", len(items))
            shown = await ev(ws, """(() => {
              const t = document.body.innerText || '';
              return {
                tierTags: [...document.querySelectorAll('.el-tag')]
                   .map(e => (e.innerText||'').trim())
                   .filter(x => /配图|整题图|要处理/.test(x)),
                toolbar: (document.querySelector('.toolbar, .fig-tiers, .bar') || {}).innerText || '',
                body: t.slice(0, 500),
              };
            })()""", nid())
            print("     分层 tag:", (shown or {}).get("tierTags"))
            tags = (shown or {}).get("tierTags") or []
            check("界面上三档 tag 都在（兜底 0 个时那一档按设计不显示）",
                  all(any(k in x for x in tags) for k in ("配图可放心", "配图待核", "配图缺")),
                  tags)
            check("界面「待核」显示的数字 = 服务端算出来的 %d" % want["check"],
                  any(("配图待核" in x) and (str(want["check"]) in x) for x in tags), tags)
            check("界面「缺图」显示的数字 = 服务端算出来的 %d" % want["missing"],
                  any(("配图缺" in x) and (str(want["missing"]) in x) for x in tags), tags)

            # ---- 4 一个待核条目的候选缩略图 ----
            print("\n==== 4 候选缩略图（取景） ====")
            before = None
            target = None
            for it in items:
                f = it.get("flags") or []
                if it.get("figure_image") and any(x in f for x in DOUBT):
                    target = it
                    break
            check("服务端确实有待核条目可验", target is not None,
                  target and target.get("id"))
            if target:
                before = dict(target)
                opened = await ev(ws, """(() => {
                  const cards = [...document.querySelectorAll('.item-card, .card')];
                  const card = cards.find(c => (c.innerText||'').indexOf(%s) >= 0);
                  if (!card) return {ok:false, why:'没找到这张卡',
                                     head: cards.slice(0,3).map(c => (c.innerText||'').slice(0,40)),
                                     n: cards.length};
                  const btn = [...card.querySelectorAll('button')]
                     .find(b => (b.innerText||'').indexOf('选现成的图') >= 0);
                  if (!btn) return {ok:false, why:'没找到按钮'};
                  btn.click();
                  return {ok:true};
                })()""" % json.dumps(HEAD), nid())
                print("     打开候选:", opened)
                await asyncio.sleep(2)
                cands = await ev(ws, """(() => {
                  const els = [...document.querySelectorAll('.fig-cand')];
                  return els.map(e => {
                    const cs = getComputedStyle(e), r = e.getBoundingClientRect();
                    return {w: Math.round(r.width), h: Math.round(r.height),
                            bg: cs.backgroundImage.slice(0, 40),
                            size: cs.backgroundSize, pos: cs.backgroundPosition,
                            tag: (e.innerText||'').trim().slice(0, 10)};
                  });
                })()""", nid())
                print("     候选:", (cands or [])[:5])
                check("候选缩略图真的画出来了（有宽高、有底图）",
                      bool(cands) and all(c["w"] > 10 and c["h"] > 10 and "url(" in c["bg"]
                                          for c in cands[:3]), (cands or [])[:2])
                check("缩略图按框大小放大取景（background-size 不是 100%）",
                      bool(cands) and cands[0]["size"] not in ("100% 100%", "auto"),
                      cands[0]["size"] if cands else None)

            # ---- 5 点一个候选 → 服务端真的要变 ----
            if target:
                print("\n==== 5 点候选 → 落库 ====")
                picked = await ev(ws, """(() => {
                  const els = [...document.querySelectorAll('.fig-cand')];
                  if (!els.length) return {ok:false};
                  els[0].click();
                  return {ok:true, n: els.length};
                })()""", nid())
                print("     点击:", picked)
                await asyncio.sleep(4)
                after = item_flags(target["id"])
                print("     服务端 before: flags=%s figure=%s" %
                      (before.get("flags"), (before.get("figure_image") or "")[-28:]))
                print("     服务端 after : flags=%s figure=%s" %
                      (after.get("flags"), (after.get("figure_image") or "")[-28:]))
                check("服务端点选后 figure_image 真的变了（不是只改了界面）",
                      (after.get("figure_image") or "") != (before.get("figure_image") or ""),
                      after.get("figure_image"))
                check("点完后可疑标记被摘掉（figure_rough 等不再挂着）",
                      not any(x in (after.get("flags") or []) for x in DOUBT),
                      after.get("flags"))

                # 立刻还原（不等 finally）：不然后面的「分档对账」会少一条，
                # 数字虽然还自洽，但对不上老师真实看到的那个数。
                restore(snap)

            # ---- 5.5 重新点一次 job 刷新界面 ----
            # ⚠️ 任务已 finished → 页面**不轮询**，第 4/5 步改的服务端状态进不了 DOM。
            #    不刷新就验筛选，会把「界面停在旧状态」误判成功能坏了（本节就踩过）。
            print("\n==== 5.5 刷新界面（finished 的 job 不轮询） ====")
            await ev(ws, """(() => {
              const cards = [...document.querySelectorAll('.job-card')];
              const hit = cards.find(c => (c.innerText||'').indexOf(%s) >= 0
                                          && (c.innerText||'').indexOf('待审 75') >= 0);
              if (hit) hit.click();
            })()""" % json.dumps(JOB_CREATED), nid())
            await asyncio.sleep(3.5)
            items = api("/api/batches/%s/items" % JOB)
            if isinstance(items, dict):
                items = items.get("items") or items.get("list") or []
            want = {"ok": 0, "check": 0, "missing": 0, "covered": 0}
            for it in items:
                f = it.get("flags") or []
                bad = any(x in f for x in DOUBT)
                if it.get("figure_image"):
                    want["check" if bad else "ok"] += 1
                elif "figure_whole_question" in f:
                    want["covered"] += 1
                elif it.get("needs_figure") or bad:
                    want["missing"] += 1
            print("     刷新后服务端:", want)
            dom2 = await ev(ws, """(() => ({
              itemCard: document.querySelectorAll('.item-card').length,
              ack: document.querySelectorAll('.fig-ack').length,
            }))()""", nid())
            print("     刷新后卡片:", dom2)
            check("刷新后卡片数回到 75", (dom2 or {}).get("itemCard") == 75, dom2)

            # ---- 6 「只看要处理的」筛选真的收窄了列表 ----
            print("\n==== 6 筛选：只看要处理的 ====")
            before_n = (dom2 or {}).get("itemCard")
            flip = await ev(ws, """(() => {
              const b = [...document.querySelectorAll('.el-checkbox')]
                 .find(x => (x.innerText||'').indexOf('只看要处理的') >= 0);
              if (!b) return {ok: false, why: '没有这个勾选框'};
              b.click();
              return {ok: true, cls: b.className};
            })()""", nid())
            await asyncio.sleep(1.5)
            after_n = await ev(ws, """(() => {
              const n = document.querySelectorAll('.item-card').length;
              const all = [...document.querySelectorAll('.item-card')];
              // 列表里是不是只剩「待核 / 缺图」两类
              const anyOk = all.some(c => (c.innerText||'').indexOf('AI 自动框的图（核对一眼即可）') >= 0);
              return {n: n, anyOkTag: anyOk, txt: (document.body.innerText||'').slice(0, 60)};
            })()""", nid())
            print("     点勾选:", flip)
            print("     切换前卡片 %s → 切换后 %s" % (before_n, (after_n or {}).get("n")))
            check("勾上「只看要处理的」后卡片数变少",
                  (after_n or {}).get("n") is not None
                  and (after_n or {}).get("n") < (before_n or 0),
                  {"前": before_n, "后": (after_n or {}).get("n")})
            check("收窄后的条数 = 待核+缺图（%d）" % (want["check"] + want["missing"]),
                  (after_n or {}).get("n") == want["check"] + want["missing"],
                  (after_n or {}).get("n"))
            # 切回来
            await ev(ws, """(() => {
              const b = [...document.querySelectorAll('.el-checkbox')]
                 .find(x => (x.innerText||'').indexOf('只看要处理的') >= 0);
              if (b) b.click();
            })()""", nid())
            await asyncio.sleep(1.2)

            # ---- 7 「待核的都确认」→ 服务端点真的要变 ----
            print("\n==== 7 批量确认待核 ====")
            rough_ids = [it["id"] for it in items
                         if it.get("figure_image")
                         and any(x in (it.get("flags") or []) for x in DOUBT)]
            print("     待核条目:", rough_ids)
            acked = await ev(ws, """(() => {
              const b = [...document.querySelectorAll('button')]
                 .find(x => (x.innerText||'').indexOf('待核的都确认') >= 0);
              if (!b) return {ok: false};
              b.click();
              return {ok: true};
            })()""", nid())
            await asyncio.sleep(1.5)
            # 可能弹确认框
            confirmed = await ev(ws, """(() => {
              const ok = [...document.querySelectorAll('.el-message-box button, .el-dialog button')]
                 .find(x => /确\\s*定|是|确认/.test(x.innerText || ''));
              if (ok) { ok.click(); return true; }
              return false;
            })()""", nid())
            print("     点按钮:", acked, " 弹框确认:", confirmed)
            await asyncio.sleep(10)
            after_ack = {i: item_flags(i) for i in rough_ids}
            still = [i for i, v in after_ack.items()
                     if v and any(x in (v.get("flags") or []) for x in DOUBT)]
            print("     批量确认后仍挂可疑标记的:", still)
            check("批量确认后服务端不再挂可疑标记（不是只改了按钮）",
                  not still and bool(rough_ids), still)
            # flags 的写回同样统一交给 finally 的 restore()

            # ---- 8 跨页「下一页」提示 ----
            print("\n==== 8 缺图条目给出跨页线索 ====")
            cross = await ev(ws, """(() => {
              const t = document.body.innerText || '';
              return {hasCross: t.indexOf('下一页') >= 0,
                      hasPickHint: t.indexOf('选现成的图') >= 0};
            })()""", nid())
            print("     ", cross)
            check("缺图的题提示了「下一页」或「选现成的图」（不留死胡同）",
                  bool(cross) and ((cross.get("hasCross") or cross.get("hasPickHint")) is True), cross)

            # ---- 9 控制台有没有报错 ----
            errs = await ev(ws, """(() => (window.__errs || []).slice(0, 10))()""", nid())
            print("\n     控制台错误:", errs)
            return 0 if not fails else 1
    finally:
        proc.kill()
        print("\n（Edge 已关闭）")
        restore(snap)

if __name__ == "__main__":
    code = asyncio.run(main())
    if skipped:
        # 这里原来是**无条件**印「全部通过」—— 于是根本没跑的断言在报告里看起来是绿的。
        # SKIP 必须印成 SKIP，且要说清「一条断言都没跑」。
        print("\n[SKIP] 跳过 %d 项（%s）—— 不等于通过：本脚本的断言一条都没跑"
              % (len(skipped), skipped[0]))
    elif not fails:
        print("\n全部通过")
    else:
        print("\n失败 %d 项：%s" % (len(fails), fails))
    sys.exit(code)
