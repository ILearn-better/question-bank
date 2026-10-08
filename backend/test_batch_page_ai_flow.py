# -*- coding: utf-8 -*-
"""AI 整页识别的**任务链路**测试：建任务 → 多题落库 → 排序 → 失败页留痕 → 配图自动裁。

与 test_batch_page_ai.py 的分工：
    那个测**解析**（模型文本 → 多道题，纯函数、零副作用）；
    这个测**落库**（多道题 → 待审条目，真实走 services/batch_page.py 的写入路径），
    外加**配图自动裁切**的端到端：自己造一页带图的 PDF，
    看模型给的粗框能不能吸附到几何位置、裁出来的图是不是那一块、会不会被当孤儿删。

**一次 AI 都不调** —— 直接调 _write_page_items 把「识别结果」喂进去，
所以不花钱、不依赖网络，可以随时跑。

⚠️ 用一个**临时库**（$TEMP/shike_ai_page_scratch），不碰项目库：
   库是 import app.db 之前就指好的（engine 绑定在 import 时定死），
   所以环境变量必须在最前面设。跑完只留一个临时目录，
   下次跑先删掉，天然幂等。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_ai_page_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.db 之前设 —— engine 在 import 时就绑定了 DB_PATH
os.environ["SHIKE_DB_PATH"] = str(TMP / "ai.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")

from sqlalchemy import delete, select          # noqa: E402

from app import config                          # noqa: E402
from app.db import SessionLocal, engine         # noqa: E402
from app.models import Base, BatchItem, BatchJob, Curriculum   # noqa: E402
from app.services import batch_page             # noqa: E402

assert str(config.DB_PATH) == str(TMP / "ai.db"), f"临时库没生效：{config.DB_PATH}"

fails: list[str] = []
total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global total
    total += 1
    line = f"  [{'OK ' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"   <- {detail}"
    print(line)
    if not ok:
        fails.append(name)


def mk(*contents: str) -> list[dict]:
    """造一份 parse_page_items 形状的「识别结果」（只填测试用得到的字段）。"""
    from app.services.batch_import import fields_from_obj
    out = []
    for c in contents:
        r = fields_from_obj({"content": c}, [])
        r["raw"] = None
        out.append(r)
    return out


def main() -> int:
    from app import config as cfg
    # 临时库从零建表。不用 alembic —— 这里只需要表存在，
    # 迁移链本身另有 test_migration_0014 守着（那个测的是「能不能升上来」）。
    Base.metadata.create_all(engine)

    s = SessionLocal()
    try:
        # code 是 NOT NULL（体系要有稳定的英文代号），别漏
        cur = Curriculum(code="test_ai_page", name="测试体系-ai-page", owner_id=cfg.OWNER_ID)
        s.add(cur)
        s.commit()
        cid = cur.id
    finally:
        s.close()

    job_id = None
    fig_job_id = None
    try:
        # ---------------------------------------------------- ① 建任务
        print("\n==== ① 建整页任务（不预建条目） ====")
        db = SessionLocal()
        try:
            job = batch_page.create_page_job(
                db, [1, 2], curriculum_id=cid,
                document_id="DOC_TEST", doc_filename="卷子.pdf",
            )
            job_id = job.id
            check("任务标记为整页模式", job.ai_mode == 1, f"ai_mode={job.ai_mode}")
            check("total 记的是**页数**（不是条目数）", job.total == 2, f"total={job.total}")
            check("页号按序存进 pages", job.pages == "[1, 2]", f"pages={job.pages}")
            check("建任务时一条条目都没有（条目是识别出来的）",
                  db.scalar(select(BatchItem).where(BatchItem.job_id == job.id)) is None)
        finally:
            db.close()

        # ---------------------------------------------------- ② 参数校验
        print("\n==== ② 建任务的前置校验 ====")
        db = SessionLocal()
        try:
            for args, label in (
                (dict(pages=[1], curriculum_id=None, document_id="D"), "缺体系"),
                (dict(pages=[1], curriculum_id=cid, document_id=None), "缺文档"),
                (dict(pages=[], curriculum_id=cid, document_id="D"), "没选页"),
            ):
                try:
                    batch_page.create_page_job(db, **args)
                    check(f"{label} → 报错", False, "居然没报错")
                except batch_page.BatchError:
                    check(f"{label} → 明确报错（BatchError）", True)
        finally:
            db.close()

        # ---------------------------------------------------- ③ 乱序写库
        print("\n==== ③ 两页的识别结果**乱序**写库（模拟并行完成） ====")
        # 页 2 先完成，页 1 后完成 —— 真跑时页的完成顺序是随机的
        db = SessionLocal()
        try:
            n2 = batch_page._write_page_items(job_id, "DOC_TEST", 2, mk("第二页甲", "第二页乙", "第二页丙"))
            n1 = batch_page._write_page_items(job_id, "DOC_TEST", 1, mk("第一页甲"))
            check("页 2 落 3 条", n2 == 3, f"got {n2}")
            check("页 1 落 1 条", n1 == 1, f"got {n1}")
        finally:
            db.close()

        # ---------------------------------------------------- ④ 排序收口
        print("\n==== ④ seq 收口成 1..N 且按页序（不按完成顺序） ====")
        db = SessionLocal()
        try:
            batch_page._renumber(job_id)
            rows = db.scalars(
                select(BatchItem).where(BatchItem.job_id == job_id).order_by(BatchItem.seq)
            ).all()
            check("共 4 条", len(rows) == 4, f"got {len(rows)}")
            check("seq 连续 1..4", [r.seq for r in rows] == [1, 2, 3, 4], f"{[r.seq for r in rows]}")
            check("**页 1 的题排在最前**（尽管它是后完成的）",
                  rows[0].content == "第一页甲", f"{[r.content for r in rows]}")
            check("同页内保持模型给的顺序",
                  [r.content for r in rows[1:]] == ["第二页甲", "第二页乙", "第二页丙"],
                  f"{[r.content for r in rows[1:]]}")
            check("page_no 记对来源页", [r.page_no for r in rows] == [1, 2, 2, 2],
                  f"{[r.page_no for r in rows]}")
            check("整页模式**不裁原貌图**（image 为空）",
                  all(not r.image for r in rows), f"{[r.image for r in rows]}")
            check("状态是待审", all(r.status == "pending" for r in rows))
        finally:
            db.close()

        # ---------------------------------------------------- ⑤ 失败页留痕
        print("\n==== ⑤ 失败页留痕（不许静默消失） ====")
        db = SessionLocal()
        try:
            batch_page._write_page_error(job_id, "DOC_TEST", 3, "模型返回空内容")
            batch_page._renumber(job_id)
            rows = db.scalars(
                select(BatchItem).where(BatchItem.job_id == job_id).order_by(BatchItem.seq)
            ).all()
            check("失败页也产出一条（老师看得见）", len(rows) == 5, f"got {len(rows)}")
            bad = rows[-1]
            check("失败条目带 error 文本", bool(bad.error), f"error={bad.error}")
            check("失败条目标了 ai_failed", "ai_failed" in (bad.flags or ""), f"flags={bad.flags}")
            check("失败条目的页号正确（重跑要靠它定位）", bad.page_no == 3)
            check("失败条目的 note 说明是哪一页", "第 3 页" in (bad.note or ""), f"note={bad.note}")
        finally:
            db.close()

        # ---------------------------------------------------- ⑥ 任务详情
        print("\n==== ⑥ 任务详情反映整页模式 ====")
        from app.routers.batches import _job_dict
        db = SessionLocal()
        try:
            j = db.get(BatchJob, job_id)
            d = _job_dict(db, j)
            check("_job_dict 带 ai_mode（前端据此切文案）", d.get("ai_mode") == 1, f"{d.get('ai_mode')}")
            check("pending 计数 = 识别出的条目数", d.get("pending") == 5, f"{d.get('pending')}")
            check("total 仍是页数（与条目数是两个概念）", d.get("total") == 2, f"{d.get('total')}")
        finally:
            db.close()

        # ---------------------------------------------------- ⑦ 配图自动裁切（端到端）
        print("\n==== ⑦ 配图自动裁切：真裁、真落盘、真被引用计数认 ====")
        import pymupdf

        # 造一页「卷子」：A4 白底 + 在 (80,300)-(200,440) 贴一张 120x140 的红块当图形。
        # 用真 PDF 而不是 mock —— 「模型给的框能不能落到真实的页面坐标上」
        # 正是这一段要验的东西，mock 掉就没有意义了。
        src = TMP / "fake_page.pdf"
        FIG = (80.0, 300.0, 200.0, 440.0)
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 140))
        pix.set_rect(pix.irect, (220, 40, 40))
        page.insert_image(pymupdf.Rect(*FIG), pixmap=pix)
        doc.save(str(src))
        doc.close()

        # 页面几何能不能认出来（认不出就谈不上吸附）
        cands = None
        W = H = 0.0
        try:
            from app.adapters import pdf as pdf_adapter
            W, H = pdf_adapter.page_size(str(src), 1)
            cands = pdf_adapter.figure_candidates(str(src), 1)
        except Exception as e:                           # noqa: BLE001
            check("能读出页面几何", False, str(e))
        check("页面尺寸读得到（A4）", abs(W - 595) < 1 and abs(H - 842) < 1, f"{W}x{H}")
        check("认出了那一处图形（位图候选）", len(cands) == 1, f"{cands}")
        if cands:
            r = cands[0]["rect"]
            check("候选区坐标就是图形本身的位置",
                  abs(r[0] - FIG[0]) < 2 and abs(r[1] - FIG[1]) < 2
                  and abs(r[2] - FIG[2]) < 2 and abs(r[3] - FIG[3]) < 2, f"{r}")

        db = SessionLocal()
        try:
            fjob = batch_page.create_page_job(
                db, [1], curriculum_id=cid, document_id="DOC_FIG", doc_filename="带图卷.pdf")
            fig_job_id = fjob.id
        finally:
            db.close()

        def mkfig(content, box, needs=True):
            """造一条带配图框的识别结果（走真实解析路径，不手搓 dict）。"""
            from app.services.batch_import import fields_from_obj
            r = fields_from_obj({"content": content, "needs_figure": needs}, [])
            r["figure_box"] = batch_page.norm_figure_box(box, r["flags"])
            r["raw"] = None
            return r

        # 模型给的框故意**偏离**图形（真实情况就是这样：偏高、偏大），
        # 靠中心点距离吸附到几何候选上。
        batch_page._write_page_items(
            fig_job_id, "DOC_FIG", 1, [mkfig("带图题", [100, 300, 400, 480])], src=str(src))
        batch_page._renumber(fig_job_id)

        db = SessionLocal()
        try:
            rows = db.scalars(
                select(BatchItem).where(BatchItem.job_id == fig_job_id)
            ).all()
            check("落 1 条", len(rows) == 1, f"got {len(rows)}")
            it = rows[0]
            check("自动裁出了配图（figure_image 有 URL）",
                  (it.figure_image or "").startswith("/api/crops/"), f"{it.figure_image}")
            check("配图文件**真的在盘上**",
                  (config.CROPS_DIR / os.path.basename(it.figure_image)).exists(),
                  f"{config.CROPS_DIR / os.path.basename(it.figure_image)}")
            check("标了 figure_snapped（说明是几何定框，不是模型估的）",
                  "figure_snapped" in (it.flags or ""), f"{it.flags}")
            check("needs_figure 被置 1（裁出图＝确认有图）", it.needs_figure == 1)
            check("figure_box 记下来了（前端预填框要用）",
                  bool(it.figure_box) and json.loads(it.figure_box)[0] < 200,
                  f"{it.figure_box}")
            # 裁出来的图必须**是那块图形**，不是整页 —— 这一条才真正证明坐标用对了
            png = config.CROPS_DIR / os.path.basename(it.figure_image)
            with pymupdf.open(str(png)) as im:
                pw, ph = im[0].rect.width, im[0].rect.height
            check("裁出来的图尺寸≈图形本身（zoom 3 → 360x420 点 @96dpi→270x315）",
                  250 < pw < 290 and 295 < ph < 340, f"{pw}x{ph}")
            check("远小于整页（不是把整页裁下来了）", pw < W * 0.6 and ph < H * 0.6, f"{pw}x{ph}")

            # ⚠️ 这一条是防「静默删图」那个老坑：自动裁的图必须被引用计数算进去，
            #    否则任何一次删题 / 删文档都会把老师正在用的配图当孤儿删掉。
            from app.services import crops as crops_svc
            refs = crops_svc.referenced_crops(db)
            check("引用计数认这张图（不会被当孤儿删）",
                  os.path.basename(it.figure_image) in refs, f"refs={len(refs)}")
        finally:
            db.close()

        # 扫描版：页面查不到几何候选 → 只能用模型估的框，且必须标出来
        db = SessionLocal()
        try:
            scan = TMP / "scan_like.pdf"
            d2 = pymupdf.open()
            p2 = d2.new_page(width=595, height=842)
            big = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 595, 842))
            big.set_rect(big.irect, (250, 250, 250))
            p2.insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=big)   # 整页一张位图
            d2.save(str(scan))
            d2.close()
            from app.adapters import pdf as pdf_adapter
            check("整页扫描底图**不算**图形候选",
                  pdf_adapter.figure_candidates(str(scan), 1) == [],
                  f"{pdf_adapter.figure_candidates(str(scan), 1)}")

            sjob = batch_page.create_page_job(
                db, [1], curriculum_id=cid, document_id="DOC_SCAN", doc_filename="扫描.pdf")
            batch_page._write_page_items(
                sjob.id, "DOC_SCAN", 1, [mkfig("扫描版里的带图题", [200, 200, 500, 500])],
                src=str(scan))
            row = db.scalars(select(BatchItem).where(BatchItem.job_id == sjob.id)).all()[0]
            check("扫描版仍能裁出图（用模型估的框）",
                  (row.figure_image or "").startswith("/api/crops/"), f"{row.figure_image}")
            check("标了 figure_rough（提示老师这个框是估的）",
                  "figure_rough" in (row.flags or ""), f"{row.flags}")
            db.execute(delete(BatchItem).where(BatchItem.job_id == sjob.id))
            db.execute(delete(BatchJob).where(BatchJob.id == sjob.id))
            db.commit()
        finally:
            db.close()

        # 模型说「有图」但没给框，而整页**只有一处**图形 → 用它（有 flag 明示）
        db = SessionLocal()
        try:
            ojob = batch_page.create_page_job(
                db, [1], curriculum_id=cid, document_id="DOC_ONLY", doc_filename="唯一图.pdf")
            batch_page._write_page_items(
                ojob.id, "DOC_ONLY", 1, [mkfig("说有图但没给框", None, needs=True)], src=str(src))
            row = db.scalars(select(BatchItem).where(BatchItem.job_id == ojob.id)).all()[0]
            check("整页仅一处图形 + **只有这一道题**要图 → 用它并标明来路",
                  "figure_only_candidate" in (row.flags or ""), f"{row.flags}")
            check("这种「替你挑的」也能裁出图",
                  (row.figure_image or "").startswith("/api/crops/"), f"{row.figure_image}")
            db.execute(delete(BatchItem).where(BatchItem.job_id == ojob.id))
            db.execute(delete(BatchJob).where(BatchJob.id == ojob.id))
            db.commit()
        finally:
            db.close()

        # ⚠️ 实测踩过的坑：**多道题**都说有图却没给框时，兜底不许把同一幅图发给每一道题
        db = SessionLocal()
        try:
            mjob = batch_page.create_page_job(
                db, [1], curriculum_id=cid, document_id="DOC_MANY", doc_filename="多题要图.pdf")
            batch_page._write_page_items(
                mjob.id, "DOC_MANY", 1,
                [mkfig("要图甲", None, needs=True), mkfig("要图乙", None, needs=True),
                 mkfig("要图丙", None, needs=True)], src=str(src))
            rows = db.scalars(select(BatchItem).where(BatchItem.job_id == mjob.id)).all()
            check("三道题都**没有**被塞上同一张图（一图多贴比不配图还糟）",
                  all(not r.figure_image for r in rows), f"{[r.figure_image for r in rows]}")
            check("三道题都标了 figure_box_missing（等人工框）",
                  all("figure_box_missing" in (r.flags or "") for r in rows),
                  f"{[r.flags for r in rows]}")
            db.execute(delete(BatchItem).where(BatchItem.job_id == mjob.id))
            db.execute(delete(BatchJob).where(BatchJob.id == mjob.id))
            db.commit()
        finally:
            db.close()

        # 模型说有图、没给框，页面又**查不到**图形（扫描版）→ 不猜，留标记让老师自己框
        db = SessionLocal()
        try:
            njob = batch_page.create_page_job(
                db, [1], curriculum_id=cid, document_id="DOC_NOBOX", doc_filename="无框.pdf")
            batch_page._write_page_items(
                njob.id, "DOC_NOBOX", 1, [mkfig("说有图没给框", None, needs=True)], src=str(scan))
            row = db.scalars(select(BatchItem).where(BatchItem.job_id == njob.id)).all()[0]
            check("没有任何依据时**不裁**（figure_image 为空）",
                  row.figure_image == "", f"{row.figure_image}")
            check("仍保留 needs_figure=1（有图的判断不能丢）", row.needs_figure == 1)
            check("标了 figure_box_missing（老师据此知道要自己框）",
                  "figure_box_missing" in (row.flags or ""), f"{row.flags}")
            db.execute(delete(BatchItem).where(BatchItem.job_id == njob.id))
            db.execute(delete(BatchJob).where(BatchJob.id == njob.id))
            db.commit()
        finally:
            db.close()

    finally:
        # 清理：只按本次的 job_id 删（绝不写「删掉库里所有 XX」那种）
        for jid in (job_id, fig_job_id):
            if not jid:
                continue
            db = SessionLocal()
            try:
                db.execute(delete(BatchItem).where(BatchItem.job_id == jid))
                db.execute(delete(BatchJob).where(BatchJob.id == jid))
                db.commit()
            finally:
                db.close()
        db = SessionLocal()
        try:
            db.execute(delete(Curriculum).where(Curriculum.name == "测试体系-ai-page"))
            db.commit()
        finally:
            db.close()

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)}/{total} 项未通过：")
        for f in fails:
            print("   -", f)
        return 1
    print(f"✅ 全部通过（{total} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
