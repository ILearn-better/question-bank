# -*- coding: utf-8 -*-
"""答案与解析补录（services/answer_backfill.py）的测试。

    cd backend && ./.venv/Scripts/python.exe test_answer_backfill.py

**一次 AI 都不调**：跑任务链路时把「页图渲染」和「整页识别」两个出口换成桩，
所以不花钱、不联网、随时可跑。

为什么这个功能值得单独一套测试：它是**唯一会改「已经在题库里的题」**的入口。
写错的代价不是「多一条垃圾」，而是**把老师手写对的答案覆盖成模型给错的**，
而且出卷印出去才被发现。所以这里盯的不是「能不能补上」，而是：

  · 默认只碰「答案与解析都空」的题（`only_missing`）
  · 应用时**已有内容的字段一律跳过**（`overwrite=False`）
  · 只写 answer / analysis 两列，题干 / 配图 / 知识点一个字都不许动
  · 对不上（nomatch）、识别失败（error）的建议**进不了库**

其余几段测的是「配得上」这件事本身：题干相似度、页内 1-1 配对、页码定位三条路。
配错比配不上严重得多 —— 配错会静默写进隔壁题的答案。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_backfill_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.db 之前设 —— engine 在 import 时就绑定了 DB_PATH
os.environ["SHIKE_DB_PATH"] = str(TMP / "ab.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")

from sqlalchemy import select              # noqa: E402

from app import config                      # noqa: E402
from app.db import SessionLocal, engine     # noqa: E402
from app.models import (                    # noqa: E402
    AnswerBackfill,
    Base,
    BatchItem,
    BatchJob,
    Curriculum,
    Document,
    Question,
)
from app.services import answer_backfill as ab   # noqa: E402

assert str(config.DB_PATH) == str(TMP / "ab.db"), f"临时库没生效：{config.DB_PATH}"

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "frontend" / "entry.html"

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


def doc_blocks(pages_texts: list[list[str]]) -> str:
    """pages_texts[i] = 第 i+1 页的若干段文字 → documents.blocks 的 JSON。"""
    blocks = []
    for pi, texts in enumerate(pages_texts, start=1):
        for t in texts:
            blocks.append({"page": pi, "type": "text", "text": t})
    return json.dumps(blocks, ensure_ascii=False)


def main() -> int:
    from app import config as cfg

    Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        cur = Curriculum(code="test_ab", name="测试体系-补录", owner_id=cfg.OWNER_ID)
        db.add(cur)
        db.commit()
        cid = cur.id

        # 一份 3 页的假文档（文字层够定位用）
        db.add(Document(
            id="DOC_AB", filename="讲义.pdf", filetype=".pdf", block_count=4,
            blocks=doc_blocks([
                ["第01讲 生活中的立体图形", "1. 下列物体中，形状是圆柱的是（  ）"],
                ["2. 如图，这个几何体的名称是棱柱，它的顶点数是 12",
                 "【答案】C", "【详解】圆柱的上下底面是圆，侧面是曲面"],
                ["3. 判断：所有的棱柱都有 6 个面"],
            ]),
            created_at="2026-10-23T00:00:00",
        ))
        db.commit()
    finally:
        db.close()

    # ============================================================ ① 相似度
    print("\n==== ① 题干相似度（配错比配不上严重） ====")
    check("同一段文字归一化后相似度 = 1",
          ab.similarity("如图，这个几何体是（  ）", "如图,这个几何体是()") == 1.0,
          f"{ab.similarity('如图，这个几何体是（  ）', '如图,这个几何体是()')}")
    check("空白与标点差异不影响（归一化后再比）",
          ab.similarity("【思考1】如图 1-1，在明明的书房中", "思考1 如图1-1 在明明的书房中") > 0.95)
    check("【答案】【详解】这类小标题被剥掉（不然答案段和题干段会互相很像）",
          ab.norm_text("【详解】圆柱的上下底面是圆") == ab.norm_text("圆柱的上下底面是圆"))
    check("两道不相干的题相似度低",
          ab.similarity("下面物体中是球体的是", "判断：所有的棱柱都有 6 个面") < 0.5,
          f"{ab.similarity('下面物体中是球体的是', '判断：所有的棱柱都有 6 个面'):.2f}")
    check("空串对空串记 0 —— 「都没题干」不能算配上",
          ab.similarity("", "") == 0.0 and ab.similarity(None, "  ") == 0.0)
    check("LaTeX 的 $ 与反斜杠被剥掉（两次识别的写法常有出入）",
          ab.similarity(r"体积是 $\frac{1}{3}Sh$", "体积是 1 3 S h") > 0.5,
          f"{ab.similarity(r'体积是 $\\frac{1}{3}Sh$', '体积是 1 3 S h'):.2f}")

    # ============================================================ ② 页内配对
    print("\n==== ② 页内 1-1 配对 ====")
    qa = Question(id="Q1", content="2. 如图，这个几何体的名称是棱柱，它的顶点数是 12", answer="", analysis="")
    qb = Question(id="Q2", content="3. 判断：所有的棱柱都有 6 个面", answer="", analysis="")
    items = [
        {"content": "3. 判断：所有的棱柱都有 6 个面", "answer": "×", "analysis": "五棱柱有 10 个顶点"},
        {"content": "2. 如图，这个几何体的名称是棱柱，它的顶点数是 12", "answer": "14", "analysis": "n 棱柱有 2n 个顶点"},
    ]
    pairs = ab.match_items([qa, qb], items)
    check("配对后顺序跟着题目走（不是跟着识别结果走）",
          [p[0].id for p in pairs] == ["Q1", "Q2"], str([p[0].id for p in pairs]))
    check("Q1 配上的是「顶点数是 12」那一条",
          pairs[0][1] is not None and pairs[0][1]["answer"] == "14", str(pairs[0][1]))
    check("Q2 配上的是「判断」那一条",
          pairs[1][1] is not None and pairs[1][1]["answer"] == "×", str(pairs[1][1]))
    check("一条识别结果不会被两道题同时用掉",
          len({id(p[1]) for p in pairs if p[1]}) == 2)

    pairs2 = ab.match_items([qa], items)
    check("页里多出一条不相干的识别结果，仍然配给最像的那条",
          pairs2[0][1] is not None and pairs2[0][1]["answer"] == "14", str(pairs2[0][1]))
    pairs3 = ab.match_items([qa, qb], items[:1])
    check("页里少一条时，配不上的那道题给 None（不许把别人的答案硬塞）",
          pairs3[0][1] is None and pairs3[1][1] is not None, str([(x[0].id, x[1]) for x in pairs3]))

    # ============================================================ ③ 建议状态
    print("\n==== ③ 建议的四种状态 ====")
    p = ab.build_proposal(qa, {"content": qa.content, "answer": "14", "analysis": "n 棱柱有 2n 个顶点"},
                          0.95, "DOC_AB", 2)
    check("配得上且有内容 → ready", p["state"] == "ready", p["state"])
    check("建议里带上了原题内容（应用时要拿它拦覆盖）",
          p["old_answer"] == "" and p["qid"] == "Q1")
    p = ab.build_proposal(qa, {"content": qa.content, "answer": "", "analysis": ""}, 0.95, "DOC_AB", 2)
    check("配得上但本页没答案 → empty（不是失败，也不许当成可写）",
          p["state"] == "empty" and "下一页" in p["msg"], p["msg"])
    p = ab.build_proposal(qa, {"content": "完全不相干的一道题", "answer": "A"}, 0.2, "DOC_AB", 2)
    check("分数低于阈值 → nomatch（宁可空着，也不写隔壁题的答案）",
          p["state"] == "nomatch" and not p["answer"], p["state"])
    p = ab.build_proposal(qa, None, 0.0, "DOC_AB", 2)
    check("页里压根没配上 → nomatch 且给出原因", p["state"] == "nomatch" and p["msg"])

    q_fig = Question(id="Q3", content="如图，求体积", answer="", analysis="", figure_image="")
    p = ab.build_proposal(q_fig, {"content": "如图，求体积", "answer": "12", "needs_figure": True},
                          0.9, "DOC_AB", 2)
    check("原页上有图而题库里没配图 → 在建议里提醒（补录不改配图，只能提醒）",
          "配图" in (p.get("msg") or ""), p.get("msg"))

    # ============================================================ ④ 定位原页
    print("\n==== ④ 定位原页的三条路 ====")
    db = SessionLocal()
    try:
        # ① 待审条目的 question_id：整页识别进来的题
        q_it = Question(id="QA_ITEM", document_id="DOC_AB", start_block=-1, content="随便什么",
                        answer="已答")
        db.add(q_it)
        # batch_items.job_id 是外键，得先有任务（这里只借它挂条目）
        db.add(BatchJob(id="J1", owner_id=cfg.OWNER_ID, status="done", total=1, done=1,
                        ai_mode=1, pages="[3]", document_id="DOC_AB"))
        db.add(BatchItem(id="BI1", job_id="J1", seq=1, document_id="DOC_AB", page_no=3,
                         content="随便什么", question_id="QA_ITEM", status="approved"))
        # ② 内容块：按块录的题
        q_blk = Question(id="QA_BLK", document_id="DOC_AB", start_block=2, content="块录的题",
                         answer="已答")
        # ③ 只剩题干：页面选区录的题（start_block=-1）
        q_txt = Question(id="QA_TXT", document_id="DOC_AB", start_block=-1,
                         content="如图，这个几何体的名称是棱柱，它的顶点数是 12",
                         answer="已答")
        # ④ 连文档都没有
        q_nod = Question(id="QA_NOD", document_id=None, content="孤立题")
        for q in (q_it, q_blk, q_txt, q_nod):
            db.add(q)
        db.commit()

        got = ab.locate_page(db, db.get(Question, "QA_ITEM"))
        check("① 待审条目的 page_no 最准", got[1] == 3 and got[2] == "待审条目", str(got))
        got = ab.locate_page(db, db.get(Question, "QA_BLK"))
        check("② 内容块的页号（start_block=2 落在第 2 页）",
              got[1] == 2 and got[2] == "内容块", str(got))
        got = ab.locate_page(db, db.get(Question, "QA_TXT"))
        check("③ 题干只在第 2 页出现 → 落到第 2 页", got[1] == 2, str(got))
        check("③ 的定位依据写明了相似度（老师要能判断可信度）", "题干比对" in got[2], got[2])
        got = ab.locate_page(db, db.get(Question, "QA_NOD"))
        check("没有文档 → 页码 None 且给出原因", got[1] is None and "文档" in got[2], str(got))

        q_img = Question(id="QA_IMG", document_id="DOC_AB", start_block=-1, content="",
                         answer="已答")
        db.add(q_img)
        db.commit()
        got = ab.locate_page(db, db.get(Question, "QA_IMG"))
        check("没有文字层的图片题 → 定位不到（明确说清，不瞎猜页码）",
              got[1] is None and "定位不到" in got[2], str(got))
    finally:
        db.close()

    # ============================================================ ⑤ 选目标
    print("\n==== ⑤ 选目标：默认只碰「答案与解析都空」的题 ====")
    db = SessionLocal()
    try:
        db.add_all([
            Question(id="T_EMPTY", document_id="DOC_AB", content="空答案题", answer="", analysis=""),
            Question(id="T_HAS_ANS", document_id="DOC_AB", content="有答案题", answer="A", analysis=""),
            Question(id="T_HAS_ANA", document_id="DOC_AB", content="有解析题", answer="", analysis="老师写的解析"),
            Question(id="T_OTHER", document_id="DOC_X", content="别的文档", answer="", analysis=""),
        ])
        db.commit()
        ids = {q.id for q in ab.pick_targets(db, document_id="DOC_AB")}
        check("只挑「答案与解析都空」的", ids == {"T_EMPTY"}, str(ids))
        ids = {q.id for q in ab.pick_targets(db, document_id="DOC_AB", only_missing=False)}
        check("only_missing=False 时文档里的题全挑上（重读覆盖要走这条路）",
              {"T_EMPTY", "T_HAS_ANS", "T_HAS_ANA"} <= ids, str(sorted(ids)))
        ids = {q.id for q in ab.pick_targets(db, question_ids=["T_HAS_ANS"])}
        check("点名某题 + only_missing=True：它有答案 → 挑不到（默认不覆盖）",
              ids == set(), str(ids))
        ids = {q.id for q in ab.pick_targets(db, question_ids=["T_HAS_ANS"], only_missing=False)}
        check("点名 + only_missing=False：能挑上", ids == {"T_HAS_ANS"}, str(ids))
        try:
            ab.pick_targets(db, only_missing=False)
            check("全库 + 覆盖会被挡（等于把手写答案全交出去）", False, "没抛错")
        except ab.BackfillError as e:
            check("全库 + 覆盖会被挡（等于把手写答案全交出去）", "覆盖" in str(e), str(e))
        ids = {q.id for q in ab.pick_targets(db, only_missing=True, limit=500)}
        check("全库 + 只补缺失：跨文档都要（DOC_X 那道也算）",
              {"T_EMPTY", "T_OTHER"} <= ids, str(sorted(ids)))
    finally:
        db.close()

    # ============================================================ ⑥ 应用
    print("\n==== ⑥ 应用：只写两列，已有内容不覆盖 ====")
    db = SessionLocal()
    try:
        db.add_all([
            Question(id="A_EMPTY", document_id="DOC_AB", content="题干甲", answer="", analysis="",
                     figure_image="/api/crops/x.png", knowledge_points='["棱柱"]'),
            Question(id="A_HAS", document_id="DOC_AB", content="题干乙", answer="老师写的", analysis=""),
        ])
        db.commit()
        job = AnswerBackfill(
            id="JOB1", owner_id=cfg.OWNER_ID, status="done", total=2, done=2, failed=0,
            scope=json.dumps({"question_ids": ["A_EMPTY", "A_HAS"], "overwrite": False}),
            proposals=json.dumps([
                {"qid": "A_EMPTY", "state": "ready", "answer": "14", "analysis": "n 棱柱 2n 顶点"},
                {"qid": "A_HAS", "state": "ready", "answer": "模型给的", "analysis": "模型解析"},
                {"qid": "A_NOMATCH", "state": "nomatch", "answer": "不许写", "analysis": ""},
            ], ensure_ascii=False),
            created_at="2026-10-23T00:00:00",
        )
        db.add(job)
        db.commit()

        r = ab.apply_proposals(db, job, None, None)
        q1 = db.get(Question, "A_EMPTY")
        check("空题库的题被补上答案", q1.answer == "14" and q1.analysis == "n 棱柱 2n 顶点", str(r))
        check("题干 / 配图 / 知识点一个字没动",
              q1.content == "题干甲" and q1.figure_image == "/api/crops/x.png"
              and q1.knowledge_points == '["棱柱"]')
        q2 = db.get(Question, "A_HAS")
        check("★ 老师手写的答案不被覆盖（overwrite=False）", q2.answer == "老师写的", q2.answer)
        check("但空的解析照样补上（逐字段判断，不是整条跳过）",
              q2.analysis == "模型解析", q2.analysis)
        check("nomatch 的建议进不了库（哪怕它在 proposals 里）",
              db.get(Question, "A_NOMATCH") is None and r["questions"] == 2, str(r))
        check("跳过的字段数有回报", r["fields_skipped"] >= 1, str(r))

        # 显式 overwrite 才覆盖
        db.add(Question(id="A_OVR", document_id="DOC_AB", content="题干丙", answer="旧的", analysis=""))
        db.commit()
        job2 = AnswerBackfill(
            id="JOB2", owner_id=cfg.OWNER_ID, status="done", total=1, done=1, failed=0,
            scope=json.dumps({"question_ids": ["A_OVR"], "overwrite": True}),
            proposals=json.dumps([{"qid": "A_OVR", "state": "ready", "answer": "新的",
                                   "analysis": "新解析"}], ensure_ascii=False),
            created_at="2026-10-23T00:00:00",
        )
        db.add(job2)
        db.commit()
        ab.apply_proposals(db, job2, ["A_OVR"], True)
        check("显式 overwrite=True 才覆盖已有答案",
              db.get(Question, "A_OVR").answer == "新的", db.get(Question, "A_OVR").answer)

        try:
            ab.apply_proposals(db, job, ["A_NOMATCH"], None)
            check("点名应用一条 nomatch 会被拒（不是静默跳过）", False, "没抛错")
        except ab.BackfillError as e:
            check("点名应用一条 nomatch 会被拒（不是静默跳过）", "可应用" in str(e), str(e))
        check("应用后记了时间（下次能看出这批补过）", bool(job.applied_at), str(job.applied_at))
    finally:
        db.close()

    # ============================================================ ⑦ 任务链路
    print("\n==== ⑦ 任务链路（页图与识别都换成桩，不调 AI） ====")
    calls: list[tuple[str, int]] = []

    def fake_image(db, doc_id, pno):
        calls.append((doc_id, pno))
        return str(TMP / f"p{pno}.png")

    def fake_recognize(db, img, hint=""):
        # 文件名里取页号。⚠️ 不能用 rsplit("p") —— ".png" 里也有个 p（踩过）
        pno = int(re.search(r"p(\d+)\.png", str(img)).group(1))
        if pno == 2:
            return [
                {"content": "另一道同页的题", "answer": "A", "analysis": "第 2 页的解析乙"},
                {"content": "空答案题", "answer": "B", "analysis": "第 2 页的解析甲"},
            ]
        return [{"content": "第 3 页那道判断题", "answer": "×", "analysis": "五棱柱有 10 个顶点"}]

    real_image, real_rec = ab._page_image, ab.batch_page.recognize_page
    ab._page_image, ab.batch_page.recognize_page = fake_image, fake_recognize
    try:
        db = SessionLocal()
        try:
            db.add_all([
                # blocks 索引：第 1 页 0~1，第 2 页 2~4，第 3 页 5
                Question(id="R1", document_id="DOC_AB", start_block=2, content="空答案题"),
                # 两道题落在同一页 → 只该识别一次
                Question(id="R2", document_id="DOC_AB", start_block=3, content="另一道同页的题"),
                Question(id="R3", document_id="DOC_AB", start_block=5, content="第 3 页那道判断题"),
            ])
            db.commit()
            # 点名这三道（DOC_AB 里还有别处留下的空答案题，不该被算进来）
            job = ab.create_job(db, question_ids=["R1", "R2", "R3"])
            check("任务把待补题数记成 total", job.total == 3, str(job.total))
            check("建任务时一条建议都没有（识别前不知道能补几条）",
                  json.loads(job.proposals) == [])
        finally:
            db.close()

        ab.run_job(job.id)

        db = SessionLocal()
        try:
            job = db.get(AnswerBackfill, job.id)
            props = json.loads(job.proposals)
            check("任务跑完状态是 done（没有识别失败页）", job.status == "done", job.status)
            check("同一页的两道题只识别一次（省 AI 调用）",
                  len([c for c in calls if c[1] == 2]) == 1, str(calls))
            check("每道题都有一条建议", len(props) == 3, str(len(props)))
            by_q = {p["qid"]: p for p in props}
            check("R1 匹配上并拿到答案", by_q["R1"]["state"] == "ready" and by_q["R1"]["answer"] == "B",
                  str(by_q["R1"]))
            check("R3 在第 3 页配上了自己的答案（第 3 页的桩里第一条就是它）",
                  by_q["R3"]["answer"] == "×", str(by_q["R3"]))
            check("建议里 page 记的是真页码", by_q["R1"]["page"] == 2 and by_q["R3"]["page"] == 3,
                  f"R1={by_q['R1']['page']} R3={by_q['R3']['page']}")
            check("counts 有汇总（前端靠它显示「可补 N 条」）",
                  job.status == "done" and ab.job_dict(db, job)["counts"].get("ready") == 3,
                  str(ab.job_dict(db, job)["counts"]))

            # 应用：默认只写空的那两列
            r = ab.apply_proposals(db, job, ["R1", "R3"], None)
            check("应用后题库里真有答案",
                  db.get(Question, "R1").answer == "B" and db.get(Question, "R3").answer == "×",
                  str(r))
        finally:
            db.close()
    finally:
        ab._page_image, ab.batch_page.recognize_page = real_image, real_rec

    # ============================================================ ⑧ 前端契约
    print("\n==== ⑧ 前端：入口在「补答案」模式里，写库要人点 ====")
    src = ENTRY.read_text(encoding="utf-8")
    i = src.find("批量补录答案与解析")
    check("补答案模式里有批量补录面板", i > 0)
    for word in ("适用范围", "操作对象", "预期效果"):
        check(f"面板说清了「{word}」（用户点名要这三项）", word in src)
    check("面板声明「只写答案/解析两列，题干配图不动」", "一律不动" in src)
    j = src.find("ab-row")
    body = src[i:i + 6000] if i > 0 else ""
    check("只有「可补」的建议能勾（对不上/失败不许写入）",
          ':disabled="p.state !== \'ready\'"' in body, body[:200])
    check("应用时把勾中的 question_ids 传回去", "question_ids: this.abPicked" in src)
    check("单题也有「AI 从原页重读」，且只填表单不写库",
          "AI 从原页重读" in src and "answer-recheck" in src)
    check("填完表单后要老师点「保存答案」才写库（沿用既有 PATCH，不开新写入口）",
          "核对无误后点「保存答案」" in src)
    check("切回录新题时把补录任务状态清掉（不再轮询）",
          "clearTimeout(this.abTimer)" in src)
    check("「解析可能在下一页」有出口（本页空时给 page+1 的按钮）",
          "解析可能在下一页" in src)

    # ============================================================ ⑨ 迁移链
    print("\n==== ⑨ 迁移：0036 在祖先链上 ====")
    # ⚠️ 两种写法都有：早期是 `revision: str = "0035"`，后来是 `revision = "0036"`。
    # 只认带冒号那种的话，一半的迁移会被当成「没被引用」→ 假的「分叉」。
    vers: dict[str, str | None] = {}
    for f in (ROOT / "backend" / "alembic" / "versions").glob("0*.py"):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"^revision\s*(?::[^=]*)?=\s*[\"'](\w+)[\"']", text, re.M)
        d = re.search(r"^down_revision\s*(?::[^=]*)?=\s*(?:[\"'](\w+)[\"']|None)", text, re.M)
        if not m:
            continue
        vers[m.group(1)] = d.group(1) if (d and d.group(1)) else None
    # 从没有后继的那一个开始往下走（不写死 head 版本号：加新迁移时这里不该红）
    heads = [r for r in vers if r not in vers.values()]
    chain: list[str] = []
    cur = heads[0] if len(heads) == 1 else None
    while cur:
        chain.append(cur)
        cur = vers.get(cur)
    check("迁移链是单条（没有分叉）", len(heads) == 1, f"heads={heads}")
    check("0036 在祖先链上", "0036" in chain, str(chain[:6]))
    check("answer_backfills 表由 0036 建（模型与迁移没脱节）",
          "answer_backfills" in (ROOT / "backend" / "alembic" / "versions"
                                 / "0036_answer_backfill.py").read_text(encoding="utf-8"))

    print(f"\n共 {total} 项，失败 {len(fails)} 项")
    if fails:
        for f in fails:
            print("  FAIL:", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
