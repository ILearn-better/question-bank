# -*- coding: utf-8 -*-
"""配图审核提速（分档确认 + 候选点选）的后端测试。**不联网、不起服务、不花一次调用。**

    cd backend && ./.venv/Scripts/python.exe test_batch_figure_review.py

为什么要单独一套：这两个动作（`figure-ack` / `figure-candidates`）看着只是加个接口，
但它们的**语义**很容易写歪，而写歪了不会报错、只会在几天后暴露：

  · `figure-ack` 若把 flags 整体替换成空数组，`empty_stem`、`bad_json` 这些
    **与配图无关**的告警会一起消失 —— 老师会以为「这题没问题」，实际题干是空的。
  · `figure-ack` 若允许在**没有图**的条目上确认，等价于「这题不用配图」这种完全不同的
    表态被悄悄记成「图已配好」，缺图从此不再提示。
  · `figure-crop` 若不清掉 `figure_rough`，老师亲手框完之后卡片还在喊
    「AI 估的图框，容易歪」—— 提示变成了噪音，下次就没人看提示了。
  · `figure-candidates` 若框没归一化 / 没排序，前端缩略图会整片错位
    （老师凭位置认图，「第三行那个圆柱」认不出来就等于没给）。

⚠️ 这里**只验接口自己的行为**，不验「裁出来的图对不对」（那要靠真 PDF，
   见 test_batch_page.py 的几何断言）。所以页面几何全部用桩喂进去。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_figure_review_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.db 之前设 —— engine 在 import 时就绑定了 DB_PATH
os.environ["SHIKE_DB_PATH"] = str(TMP / "fig.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import HTTPException                       # noqa: E402

from app import config                                  # noqa: E402
from app.db import SessionLocal, engine                  # noqa: E402
from app.models import Base, BatchItem, BatchJob         # noqa: E402
from app.routers import batches                          # noqa: E402
from app.services import batch_page                      # noqa: E402
from app.schemas import BatchRegionIn                    # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def expect_err(fn, want: int) -> tuple[bool, str]:
    """调一个应该抛 HTTPException 的动作，拿状态码比对。

    ⚠️ 不能用「直接调 + 不抛错就算过」：真出问题时抛的是别的异常（traceback），
       输出里没有 [FAIL] 行，反向验证会被误判成「守住了」。
    """
    try:
        fn()
    except HTTPException as e:
        return (e.status_code == want), f"实际 {e.status_code}，期望 {want}"
    except Exception as e:                              # noqa: BLE001
        return False, f"抛了非 HTTPException：{type(e).__name__}: {e}"
    return False, f"没有抛错（期望 {want}）"


def mk_item(db, item_id: str, **kw) -> BatchItem:
    it = BatchItem(
        id=item_id,
        job_id="job1",
        seq=kw.pop("seq", 1),
        document_id="doc1",
        page_no=kw.pop("page_no", 3),
        content="如图，求体积。",
        qtype="解答题",
        difficulty="基础",
        tags="[]",
        confidence="high",
        flags=json.dumps(kw.pop("flags", [])),
        status="pending",
        created_at="2026-10-23T00:00:00",
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def main() -> int:
    Base.metadata.create_all(engine)
    db = SessionLocal()
    db.add(BatchJob(id="job1", status="done", total=1, done=1, failed=0))
    db.commit()

    # ---------------------------------------------------------------- ① ack
    print("\n==== ① figure-ack：摘可疑标记，但别的告警一个不少 ====")
    it = mk_item(db, "i1", needs_figure=1, figure_image="/api/crops/a.png",
                 figure_box="[100, 200, 300, 400]",
                 flags=["figure_rough", "figure_shared_candidate", "empty_stem"])
    r = batches.ack_item_figure("i1", db=db)
    flags = r["item"]["flags"]
    check("rough / shared 被摘掉", not ({"figure_rough", "figure_shared_candidate"} & set(flags)),
          str(flags))
    check("与配图无关的 empty_stem **保留**", "empty_stem" in flags, str(flags))
    check("配图本身没被动", r["item"]["figure_image"] == "/api/crops/a.png")
    check("框没被动", r["item"]["figure_box"] == [100, 200, 300, 400])

    print("\n==== ② figure-ack：没有图的条目不许确认 ====")
    mk_item(db, "i2", needs_figure=1, figure_image="", flags=["figure_box_missing"])
    ok, why = expect_err(lambda: batches.ack_item_figure("i2", db=db), 422)
    check("没图 → 422（不能把「缺图」记成「已确认」）", ok, why)
    left = json.loads(db.get(BatchItem, "i2").flags or "[]")
    check("被拒之后 flags 原样不动", left == ["figure_box_missing"], str(left))

    # ---------------------------------------------------------------- ③ crop
    print("\n==== ③ 亲手框一次 → 可疑标记自动失效 ====")
    mk_item(db, "i3", needs_figure=0, figure_image="",
            flags=["figure_rough", "figure_dup_candidate", "bad_json"])
    # 页面几何与裁图都换成桩：这条测试只关心 flags 与落库字段
    orig_size, orig_crop = batches.pdf_adapter.page_size, batches.pdf_adapter.crop_region
    batches.pdf_adapter.page_size = lambda src, pno: (1000.0, 2000.0)
    batches.pdf_adapter.crop_region = (
        lambda src, pno, rect, outdir: "deadbeef.png")
    orig_src = batches._page_source
    batches._page_source = lambda db_, doc_id: "fake.pdf"
    try:
        payload = batches.FigureCropIn(regions=[
            BatchRegionIn(page=3, x0=100, y0=200, x1=300, y1=500)])
        r = batches.crop_item_figure("i3", payload, db=db)
    finally:
        batches.pdf_adapter.page_size, batches.pdf_adapter.crop_region = orig_size, orig_crop
        batches._page_source = orig_src
    f3 = r["item"]["flags"]
    check("rough / dup 都被摘掉", not ({"figure_rough", "figure_dup_candidate"} & set(f3)), str(f3))
    check("bad_json 保留（与配图无关）", "bad_json" in f3, str(f3))
    check("框图像就等于确认有图 needs_figure=1", r["item"]["needs_figure"] is True)
    check("归一化框按 1000 制落库",
          r["item"]["figure_box"] == [100, 100, 300, 250], str(r["item"]["figure_box"]))

    # ---------------------------------------------------------------- ④ 候选
    print("\n==== ④ figure-candidates：归一化 + 排序 + 跨页候候选 ====")
    # 故意乱序给三处：下面那个、上面右边、上面左边
    FAKE = {
        7: [
            {"kind": "img", "rect": (100.0, 900.0, 500.0, 1400.0)},   # 下
            {"kind": "vec", "rect": (500.0, 100.0, 900.0, 400.0)},    # 上右
            {"kind": "img", "rect": (100.0, 100.0, 400.0, 400.0)},    # 上左
        ],
        # 下一页：一处在上半页（跨页题真正要的那张）、一处在下半页（下一页自己的题）
        8: [
            {"kind": "img", "rect": (100.0, 1500.0, 500.0, 1800.0)},  # 下半页 → 不该收
            {"kind": "img", "rect": (100.0, 200.0, 450.0, 600.0)},    # 上半页 → 收
        ],
    }
    orig = (batches.pdf_adapter.page_size, batches.pdf_adapter.figure_candidates,
            batches.pdf_adapter.page_count, batches._page_source)
    batches.pdf_adapter.page_size = lambda src, pno: (1000.0, 2000.0)
    batches.pdf_adapter.figure_candidates = lambda src, pno: FAKE.get(pno, [])
    batches.pdf_adapter.page_count = lambda src: 8
    batches._page_source = lambda db_, doc_id: "fake.pdf"
    c = c2 = c3 = None
    try:
        # A：第 7 页上还有别的题（A 不是页内最后一题）→ 不该去看下一页
        mk_item(db, "i4a", needs_figure=1, figure_image="", page_no=7,
                seq=10, figure_box="[10, 20, 30, 40]")
        mk_item(db, "i4b", needs_figure=0, figure_image="", page_no=7, seq=20)
        c = batches.item_figure_candidates("i4a", db=db)

        # C：第 7 页的**最后一题**，全局还有第 8 页 → 下一页上半页的候选要一起列出来
        mk_item(db, "i4c", needs_figure=1, figure_image="", page_no=7, seq=30)
        c2 = batches.item_figure_candidates("i4c", db=db)

        # E：全局最后一页的最后一题 → 没有下一页可看
        mk_item(db, "i4e", needs_figure=1, figure_image="", page_no=8, seq=40)
        c3 = batches.item_figure_candidates("i4e", db=db)
    finally:
        (batches.pdf_adapter.page_size, batches.pdf_adapter.figure_candidates,
         batches.pdf_adapter.page_count, batches._page_source) = orig

    check("页号 / 宽高带回来了", c["page"] == 7 and c["width"] == 1000 and c["height"] == 2000)
    check("底图地址是页面图（前端直接拿它取景）",
          c["page_image"] == "/api/documents/doc1/pages/7/image", c["page_image"])
    check("当前框回传（前端能标出「已经是它了」）", c["current"] == [10, 20, 30, 40])
    check("不是页内最后一题 → 不去凑下一页候选",
          c["cross_page"] is False and len(c["candidates"]) == 4,
          f'{len(c["candidates"])} 处, cross={c["cross_page"]}')
    rowc = [x for x in c["candidates"] if x["kind"] == "row"]
    check("同一横排的两幅另给一条「整排」（选择题的四幅选项）",
          len(rowc) == 1 and rowc[0]["box"] == [100, 50, 900, 200],
          str([x["box"] for x in rowc]))
    check("框全部归一化到 0~1000 的整数",
          all(all(isinstance(v, int) and 0 <= v <= 1000 for v in x["box"])
              for x in c["candidates"]), str([x["box"] for x in c["candidates"]]))
    check("排序 = 先上后下、同一行先左后右",
          [x["box"] for x in c["candidates"] if x["kind"] != "row"]
          == [[100, 50, 400, 200], [500, 50, 900, 200], [100, 450, 500, 700]],
          str([x["box"] for x in c["candidates"]]))
    check("kind 带回来了（位图/矢量/整排）",
          sorted(x["kind"] for x in c["candidates"]) == ["img", "img", "row", "vec"],
          str([x["kind"] for x in c["candidates"]]))

    pages = [x["page"] for x in c2["candidates"]]
    check("页内最后一题 + 还有下一页 → 打跨页标记", c2["cross_page"] is True, str(c2["cross_page"]))
    check("下一页只收**上半页**的候选（下半页是下一页自己的题）",
          pages == [7, 7, 7, 7, 8], str(pages))
    check("每个候选自带页号 / 页宽高 / 底图（跨页共用一套会全错位）",
          all(x["width"] == 1000 and x["height"] == 2000
              and x["page_image"] == f"/api/documents/doc1/pages/{x['page']}/image"
              for x in c2["candidates"]),
          str([x["page_image"] for x in c2["candidates"]]))
    check("排序：先本页、再下一页（老师是凭位置认图的）", pages == sorted(pages), str(pages))
    check("全局最后一页 → 不硬凑下一页候选", c3["cross_page"] is False, str(c3["cross_page"]))

    print("\n==== ⑤ 没有来源文档时不假装有候选 ====")
    mk_item(db, "i5", needs_figure=1, figure_image="", page_no=1)
    db.get(BatchItem, "i5").document_id = None
    db.commit()
    ok, why = expect_err(lambda: batches.item_figure_candidates("i5", db=db), 422)
    check("document_id 为空 → 422（前端据此退回「只能手工框」）", ok, why)

    # ---------------------------------------------------------------- ⑥ 兜底范围
    print("\n==== ⑥ 整题原貌图的兜底范围（纯函数，不裁图） ====")
    # 三道认得出的题 + 一道认不出的：看边界会不会算穿
    regions = batch_page.plan_whole_regions([100.0, 500.0, None, 900.0], 1000.0, 2000.0)
    near = lambda a, b: a is not None and abs(a - b) < 0.01
    check("第 1 题下边界 = 第 2 题首行", regions[0] is not None and near(regions[0][3], 499.0),
          str(regions[0]))
    check("第 2 题跳过认不出的第 3 题、下边界落在第 4 题",
          regions[1] is not None and near(regions[1][3], 899.0), str(regions[1]))
    check("认不出首行的题**不猜**（给 None，宁可等老师框）", regions[2] is None)
    check("最后一题到页底，且留了页脚余量",
          regions[3] is not None and near(regions[3][3], 2000.0 * 0.96 - 1.0),
          str(regions[3]))
    check("上边界取本题首行往上一点（不把上一题的图裁进来）",
          regions[0] is not None and near(regions[0][1], 98.0), str(regions[0]))
    check("范围是整页宽（图可能在右半边）",
          regions[0] is not None and regions[0][0] == 0.0 and regions[0][2] == 1000.0)
    tight = batch_page.plan_whole_regions([100.0, 105.0], 1000.0, 1000.0)
    check("两道题首行几乎重合 → 不兜底（那是定位串了）", tight[0] is None, str(tight[0]))

    # ---- 答案 / 详解 / 页脚必须挡在下边界外面 ----
    # 真实数据实测：页内最后一题后面紧跟【答案】【详解】，只按「下一题首行」
    # 会一路裁到页底，答案就印进卷子了（p6/p14/p29 三张全中）。
    with_answer = batch_page.plan_whole_regions(
        [400.0], 1000.0, 2000.0, stops=[592.0, 638.0], bottom=1800.0)
    check("页内最后一题 → 下边界停在【答案】之前（否则答案印进卷子）",
          with_answer[0] is not None and near(with_answer[0][3], 591.0),
          str(with_answer[0]))
    mid = batch_page.plan_whole_regions(
        [100.0, 900.0], 1000.0, 2000.0, stops=[520.0], bottom=1800.0)
    check("下一题首行比【答案】更靠上时，取更靠上的那个",
          mid[0] is not None and near(mid[0][3], 519.0), str(mid[0]))
    no_mark = batch_page.plan_whole_regions([1480.0], 1000.0, 2000.0, bottom=1800.0)
    check("没有结束标记 → 收在正文下界（不含页脚页码）",
          no_mark[0] is not None and near(no_mark[0][3], 1799.0), str(no_mark[0]))
    check("下边界不会超出页高",
          all((r is None or r[3] <= 2000.0) for r in no_mark))

    # ---- page_marks 的判据（纯函数那层，不用真 PDF） ----
    from app.adapters.pdf import _marks_from_blocks                # noqa: E402
    blk = [(100.0, 114.0, 60.0, "1．【答案】写在横线上\n"),      # 题干里含标记 → 不算
           (400.0, 414.0, 60.0, "【答案】D\n"),
           (520.0, 534.0, 60.0, "【详解】解：……\n"),
           (568.0, 579.0, 60.0, "必记结论\n"),
           (778.0, 791.0, 300.0, "4 / 44\n"),                    # 页脚页码
           (860.0, 880.0, 60.0, "正文里的 4 / 44 算式\n")]       # 超出页高，不该被当页脚
    stops, bottom = _marks_from_blocks(blk, 842.0)
    check("结束标记按块**开头**认（题干里提到【答案】不算）",
          stops == [400.0, 520.0, 568.0], str(stops))
    check("正文下界 = 页脚页码的 y（842*0.93 之外更靠上的那个）",
          near(bottom, 778.0), str(bottom))
    stops2, bottom2 = _marks_from_blocks(
        [(100.0, 114.0, 60.0, "1．下列图形中…\n")], 842.0)
    check("认不出页码 → 退回页高 93%（96% 会切不干净页码）",
          near(bottom2, 842.0 * 0.93) and stops2 == [], str(bottom2))

    print("\n==== ⑥b 同一横排并成「整排」（选择题的四幅选项） ====")
    # 三幅同一行的选项图，间隙都不大（真实卷面上 A/B/C/D 就是这个距离）
    same_row = [{"rect": (100.0, 100.0, 300.0, 400.0)},
                {"rect": (620.0, 110.0, 820.0, 390.0)},
                {"rect": (360.0, 120.0, 560.0, 380.0)}]
    bands = batch_page.merge_row_candidates(same_row, 1000.0)
    check("同一行的三幅 → 并成一条整排", len(bands) == 1, str(bands))
    check("整排范围 = 三幅的外接矩形",
          bands and bands[0]["rect"] == (100.0, 100.0, 820.0, 400.0), str(bands))
    check("整排的 kind 标成 row（界面据此标记「整排」）",
          bands and bands[0]["kind"] == "row")

    two_rows = [{"rect": (100.0, 100.0, 300.0, 400.0)},
                {"rect": (100.0, 500.0, 300.0, 800.0)}]
    check("上下两行不并（那是两道题的图）",
          batch_page.merge_row_candidates(two_rows, 1000.0) == [], "不该产出整排")
    far = [{"rect": (100.0, 100.0, 200.0, 400.0)},
           {"rect": (900.0, 100.0, 1000.0, 400.0)}]
    check("横向隔太远不并（页宽 25% 以上是两件事）",
          batch_page.merge_row_candidates(far, 1000.0) == [])
    check("只有一幅时不产出整排（并了也还是它自己）",
          batch_page.merge_row_candidates([{"rect": (100.0, 100.0, 300.0, 400.0)}], 1000.0) == [])

    # ---------------------------------------------------------------- ⑦ 入库形态
    print("\n==== ⑦ 兜底的题入库必须走图片形态 ====")
    from app.models import Question                          # noqa: E402
    from app.services import batch_import as bi              # noqa: E402

    mk_item(db, "i6", needs_figure=1, image="/api/crops/whole1.png",
            flags=["figure_whole_question", "figure_box_missing"])
    qid6 = bi.approve_item(db, db.get(BatchItem, "i6"))
    q6 = db.get(Question, qid6)
    check("带兜底标记 → render_prefer=image（否则出卷印文本、图就丢了）",
          q6.render_prefer == "image", str(q6.render_prefer))
    check("原貌图跟着进了题库", q6.image == "/api/crops/whole1.png")

    mk_item(db, "i7", needs_figure=1, figure_image="/api/crops/f7.png")
    qid7 = bi.approve_item(db, db.get(BatchItem, "i7"))
    check("不带兜底标记 → 仍是 auto（有文本用文本）",
          db.get(Question, qid7).render_prefer == "auto")

    # ---------------------------------------------------------------- ⑧ 兜底可撤
    print("\n==== ⑧ 老师自己框对图 → 兜底自动撤掉 ====")
    mk_item(db, "i8", needs_figure=1, figure_image="", image="/api/crops/whole8.png",
            flags=["figure_whole_question", "figure_box_missing", "empty_stem"])
    orig_size, orig_crop = batches.pdf_adapter.page_size, batches.pdf_adapter.crop_region
    batches.pdf_adapter.page_size = lambda src, pno: (1000.0, 2000.0)
    batches.pdf_adapter.crop_region = lambda src, pno, rect, outdir: "real8.png"
    orig_src = batches._page_source
    batches._page_source = lambda db_, doc_id: "fake.pdf"
    try:
        payload = batches.FigureCropIn(regions=[
            BatchRegionIn(page=3, x0=10, y0=20, x1=300, y1=500)])
        r8 = batches.crop_item_figure("i8", payload, db=db)
    finally:
        batches.pdf_adapter.page_size, batches.pdf_adapter.crop_region = orig_size, orig_crop
        batches._page_source = orig_src
    it8 = db.get(BatchItem, "i8")
    f8 = r8["item"]["flags"]
    check("整题图兜底被撤掉（否则出卷会印两遍）",
          "figure_whole_question" not in f8, str(f8))
    check("那张整题图的引用也清了", (it8.image or "") == "", f"image={it8.image!r}")
    check("empty_stem 这类无关告警照旧保留", "empty_stem" in f8, str(f8))
    check("新配图已挂上", r8["item"]["figure_image"] == "/api/crops/real8.png")

    db.close()
    print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项：" + "、".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
