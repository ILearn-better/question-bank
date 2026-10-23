# -*- coding: utf-8 -*-
"""离线重放「整题原貌图兜底」—— 用真 PDF + 库里真条目，不调 AI。

目的：确认新的门槛（_range_has_figure）不再产「假兜底」，
      并亲眼看看真兜底裁出来的图对不对。
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services import batch_page                     # noqa: E402
from app.adapters import pdf as pdf_adapter             # noqa: E402

JOB = "2fcc24d666c7"
DOC = "a5fa3c1a50fb"
PDF = os.path.join("data", "uploads", "converted", DOC + ".pdf")
OUT = os.path.join(tempfile.gettempdir(), "shike_whole_replay")
os.makedirs(OUT, exist_ok=True)

db = sqlite3.connect("shike.db")
db.row_factory = sqlite3.Row

rows = db.execute(
    "select id, seq, page_no, content, needs_figure, figure_box, flags "
    "from batch_items where job_id=? order by seq", (JOB,)).fetchall()

# 按页分组（跟 _write_page_items 里「一页一批条目」的形态一致）
by_page = {}
for r in rows:
    by_page.setdefault(r["page_no"], []).append(r)

print("PDF:", PDF, "存在:", os.path.exists(PDF))
print("共 %d 页有条目\n" % len(by_page))

import pymupdf                                          # noqa: E402

results = []
for pno_db, items in sorted(by_page.items()):
    # 先定页码基准：page_no 是 1 基还是 0 基
    pno0 = pno_db          # ⚠️ _page 是 1-based，直接传库里的 page_no
    W, H = pdf_adapter.page_size(PDF, pno0)
    cands = pdf_adapter.figure_candidates(PDF, pno0)
    heads = [it["content"] or "" for it in items]
    starts = pdf_adapter.question_starts(PDF, pno0, heads)
    stops, bottom = pdf_adapter.page_marks(PDF, pno0)
    whole = batch_page.plan_whole_regions(starts, W, H, stops=stops, bottom=bottom)

    # ⚠️ 必须跟生产一致：走 plan_page_figures，只有「配图没定位到」的题才兜底。
    #    不模拟的话会把已经有 snapped 配图的题也裁一遍（重放就失真了）。
    def _box(v):
        if not v:
            return None
        try:
            return json.loads(v) if isinstance(v, str) else v
        except ValueError:
            return None
    page_items = [{"content": it["content"], "needs_figure": bool(it["needs_figure"]),
                   "figure_box": _box(it["figure_box"])} for it in items]
    decisions = (batch_page.plan_page_figures(page_items, cands, W, H) if W
                 else [{"rect": None, "box": None, "mode": "", "dup": False,
                        "shared": False} for _ in items])

    hit = sum(1 for s in starts if s is not None)
    print("=== p%d  W=%.0f H=%.0f  候选=%d  定位 %d/%d ===" %
          (pno_db, W, H, len(cands), hit, len(starts)))
    for i, it in enumerate(items):
        need = bool(it["needs_figure"])
        w = whole[i]
        has_fig = bool(w) and batch_page._range_has_figure(w, cands)
        verdict = ""
        d = decisions[i]
        if d["rect"] is not None:
            verdict = "→ 配图已自动定位（mode=%s），不兜底" % d["mode"]
            results.append((pno_db, it["id"], need, w, False, verdict))
            print("   %-12s need=%d %s" % (it["id"], need, verdict))
            continue
        if need and not has_fig:
            if w and w[3] >= H * 0.9:
                verdict = "→ figure_cross_page（图在下一页，不兜底）"
            elif w is None:
                verdict = "→ 定位失败，等老师框"
            else:
                verdict = "→ 范围里没图，不兜底"
        elif need and has_fig:
            verdict = "→ 裁整题原貌图"
        results.append((pno_db, it["id"], need, w, has_fig, verdict))
        print("   %-12s need=%d 首行y=%s 范围=%s %s" % (
            it["id"], need,
            ("%.0f" % starts[i]) if starts[i] is not None else "None",
            ("(%.0f,%.0f,%.0f,%.0f)" % w) if w else "None",
            verdict))
    print()

# ---- 把「该兜底」的裁出来，亲眼看 ----
print("\n==== 裁剪（只裁真会兜底的） ====")
made = []
with pymupdf.open(PDF) as doc:
    for pno_db, iid, need, w, has_fig, verdict in results:
        if not (need and has_fig):
            continue
        page = doc[pno_db - 1]
        clip = pymupdf.Rect(w[0], w[1], w[2], w[3])
        pix = page.get_pixmap(clip=clip, dpi=150)
        fn = os.path.join(OUT, "whole_%s_p%d.png" % (iid, pno_db))
        pix.save(fn)
        made.append(fn)
        print("  ", fn, pix.width, "x", pix.height)

# ---- 对照：原来会产假图的 5 条，现在怎么说 ----
print("\n==== 以前产「假兜底」的 5 条 ====")
FAKE5 = {"d104f3839dce": 10, "a8ea4d7b9432": 13,
         "f7f591e9fbf7": 22, "a4dd7f03a799": 23, "6731ab6ee7a5": 32}
for iid, pg in FAKE5.items():
    for pno_db, i2, need, w, has_fig, verdict in results:
        if i2 == iid:
            print("  %-12s p%-3d %s" % (iid, pg, verdict))

print("\n共裁出 %d 张" % len(made))
