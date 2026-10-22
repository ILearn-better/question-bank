# -*- coding: utf-8 -*-
"""答案与解析补录：给**已经在题库里**的题，重新从原页读出答案与解析。

## 为什么需要它

整页识别从 0035 起才开始收 `answer` / `analysis`。在那之前录进来的题，
`questions.answer` 是空串 —— 题在库里、能出卷，但答案永远缺一块。
而**入库之后再补**原本没有任何入口：待审页只管「还没通过的条目」，
补答案模式只能手工圈图 / 粘贴文本。

本模块给的就是这条入口：**操作对象是 questions 里的行，不是待审条目**。

## 适用范围 / 操作对象 / 预期效果（前端文案必须与这里说的一致）

    适用范围：题库里已有的题，且能定位到**原文档的某一页**
             （整页识别进来的靠 batch_items.question_id，
               按内容块录的靠 start_block，其余按题干与该页文字的相似度定页）
    操作对象：默认只碰「answer 与 analysis 都为空」的题；`overwrite=True`
             才允许覆盖已有内容（老师手写过的答案默认一个字都不动）
    预期效果：只写 questions.answer / questions.analysis。
             题干、配图（figure_image）、知识点、标签、出卷结果**一律不动**。

## 两段式：先出建议，再应用

识别一页要几十秒，一页还可能对应好几道题。所以：

    ① `create_job` / `run_job` → 逐页识别，把**建议**存进 `answer_backfills.proposals`
    ② `apply` → 老师看过、勾过之后才写进 questions

不做「识别完直接写库」的理由：模型偶尔会把相邻题的答案挂错（实测确有），
而答案写错比答案缺失更难被发现 —— 出卷印出去就直接讲错了。
所以写库这一步必须是**人点过**的。

## 与 batch 那套的关系

不新开一条识别链路：页图渲染、提示词、解析、字段整理全部复用
`batch_page.recognize_page`（同一份 PAGE_PROMPT，含 answer / analysis 两段）。
差别只在**落点**：那边写 `batch_items`（待审），这边写 `questions`（已有题）。
两条链路共用一份提示词，意味着「补出来的答案」与「新录进来的答案」口径一致。
"""
from __future__ import annotations

import difflib
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import SessionLocal
from ..models import AnswerBackfill, BatchItem, Document, Question
from . import batch_page


class BackfillError(Exception):
    """给路由层转成 4xx 的业务错误。"""


# 匹配阈值。模型对同一张页图跑两次的题干差异通常是标点/序号级别的，
# 归一化之后相似度在 0.85 以上；0.55 是「能看出是同一道题」的下限。
# 低于它宁可报「对不上」，也不要写一条可能是隔壁题的答案。
MATCH_MIN = 0.55
# 参与比对的最大字数。解答题题干几百字很常见，整段比会掩盖真正的差异点。
_CMP_CHARS = 200
# 一页最多能容纳的待补题数（防御：定位逻辑出错时不至于把一整页题都塞进来）
_PAGE_CAP = 30

_PUNCT_RE = re.compile(
    r"[\s\u3000，。、；：？！“”‘’（）【】《》〈〉…—－\-·,.;:?'\"()\[\]{}<>/\\|_=+*~`^%$#@&]+"
)
_MARK_RE = re.compile(r"【(答案|解析|详解|分析|点睛|小问).*?】")


def norm_text(s: str | None) -> str:
    """归一化：去空白、去标点、去【答案】这类小标题。

    LaTeX 的 `$` 与 `{}` 也去掉 —— 同一条公式模型两次可能写成
    `\\frac{1}{2}` 与 `1/2`，保留符号只会让相似度无端变低。
    """
    t = (s or "").strip()
    if not t:
        return ""
    t = _MARK_RE.sub("", t)
    t = t.replace("$", "").replace("\\", "")
    return _PUNCT_RE.sub("", t).lower()


def similarity(a: str | None, b: str | None) -> float:
    """题干相似度 0~1。空串对空串记 0（「都没题干」不能算匹配上）。"""
    na, nb = norm_text(a)[:_CMP_CHARS], norm_text(b)[:_CMP_CHARS]
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()


# ---------------------------------------------------------------- 定位原页
def _blocks(db: Session, doc_id: str) -> list[dict]:
    doc = db.get(Document, doc_id)
    if doc is None or not doc.blocks:
        return []
    try:
        raw = json.loads(doc.blocks)
    except (TypeError, ValueError):
        return []
    return [b for b in raw if isinstance(b, dict)]


def _page_texts(db: Session, doc_id: str) -> dict[int, str]:
    """按页拼文字层，供「题干 → 页码」兜底用。"""
    out: dict[int, list[str]] = {}
    for b in _blocks(db, doc_id):
        try:
            pno = int(b.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if pno <= 0:
            continue
        txt = b.get("text") or ""
        if txt:
            out.setdefault(pno, []).append(txt)
    return {p: "\n".join(v) for p, v in out.items()}


def locate_page(db: Session, q: Question) -> tuple[str | None, int | None, str]:
    """`(document_id, page_no, 依据)`。定位不到时页码为 None。

    三级，从可靠到兜底：
      ① `batch_items.question_id` —— 整页识别进来的题，页码是识别时记下的，最准
      ② `start_block` —— 按内容块录的题，块本身就带页号
      ③ 题干 ↔ 每页文字层的相似度 —— 页面选区录的题（start_block=-1）只剩这条路
    """
    it = db.execute(
        select(BatchItem).where(BatchItem.question_id == q.id).limit(1)
    ).scalar_one_or_none()
    if it is not None and it.page_no and it.document_id:
        return it.document_id, int(it.page_no), "待审条目"

    doc_id = (q.document_id or "").strip()
    if not doc_id:
        return None, None, "这题没有关联文档"

    blocks = _blocks(db, doc_id)
    sb = q.start_block if q.start_block is not None else -1
    if blocks and 0 <= sb < len(blocks):
        try:
            pno = int(blocks[sb].get("page") or 0)
        except (TypeError, ValueError):
            pno = 0
        if pno > 0:
            return doc_id, pno, "内容块"

    texts = _page_texts(db, doc_id)
    best, best_p = 0.0, None
    for pno, txt in texts.items():
        s = similarity(q.content, txt)
        if s > best:
            best, best_p = s, pno
    if best_p and best >= 0.35:
        return doc_id, best_p, f"题干比对（{best:.0%}）"
    return doc_id, None, "定位不到原页（这题可能只有截图、没有文字层）"


# ---------------------------------------------------------------- 选目标
def pick_targets(
    db: Session,
    question_ids: list[str] | None = None,
    document_id: str | None = None,
    only_missing: bool = True,
    limit: int = 300,
) -> list[Question]:
    """挑出这次要补的题。

    `only_missing=True`（缺省）只要「答案和解析都空」的 —— 这正是补录最常做的事；
    要重跑已有答案的题，必须**显式传 question_ids**，免得批量重读把老师手写的
    答案覆盖掉。
    """
    stmt = select(Question)
    if question_ids:
        stmt = stmt.where(Question.id.in_(question_ids))
    elif document_id:
        stmt = stmt.where(Question.document_id == document_id)
    elif not only_missing:
        # 「全库 + 覆盖」等于把老师手写过的答案全交出去，不给这条路
        raise BackfillError("要覆盖已有答案时必须指定文档或点名题目，不能选全库")
    # 三个都没给 = 全库；此时 only_missing 必然为真（见上一条）
    if only_missing:
        stmt = stmt.where(
            (Question.answer.is_(None)) | (Question.answer == ""),
        ).where(
            (Question.analysis.is_(None)) | (Question.analysis == ""),
        )
    stmt = stmt.order_by(Question.document_id, Question.created_at, Question.id).limit(limit)
    return list(db.execute(stmt).scalars())


# ---------------------------------------------------------------- 识别一页并配对
def _page_image(db: Session, doc_id: str, pno: int) -> str:
    """渲染某一页成 PNG（有磁盘缓存，第二次进来直接命中）。

    与 batch_page 里那套完全一样：页面来源统一问 `documents._page_source`
    （.pdf 用原件、.docx 先导出 PDF），延迟 import 避免 services ↔ routers 环。
    """
    from ..adapters import pdf as pdf_adapter
    from ..routers.documents import _page_source

    src = str(_page_source(db, doc_id))
    return pdf_adapter.render_page(src, int(pno), str(config.PAGES_CACHE / doc_id))


def match_items(questions: list[Question], items: list[dict]) -> list[tuple[Question, dict | None, float]]:
    """页内 1-1 配对：按题干相似度全局贪心。

    为什么不做「按顺序一一对应」：模型对一页的切分**偶发**会多一道/少一道
    （实测 p37），顺序一旦错位，后面的答案会整体串到上一题。
    按文本配对时错位只影响对不上的那一两道题。
    """
    pairs: list[tuple[float, int, int]] = []
    for qi, q in enumerate(questions):
        for ii, it in enumerate(items):
            pairs.append((similarity(q.content, it.get("content")), qi, ii))
    pairs.sort(key=lambda x: -x[0])

    used_q: set[int] = set()
    used_i: set[int] = set()
    out: list[tuple[Question, dict | None, float]] = []
    chosen: dict[int, tuple[int, float]] = {}
    for score, qi, ii in pairs:
        if qi in used_q or ii in used_i:
            continue
        used_q.add(qi)
        used_i.add(ii)
        chosen[qi] = (ii, score)

    for qi, q in enumerate(questions):
        if qi in chosen:
            ii, score = chosen[qi]
            out.append((q, items[ii], score))
        else:
            out.append((q, None, 0.0))
    return out


def build_proposal(q: Question, item: dict | None, score: float, doc_id: str, pno: int,
                   basis: str = "") -> dict:
    """一条建议。**只读**：这里绝不写库。"""
    base = {
        "qid": q.id,
        "document_id": doc_id,
        "page": pno,
        "basis": basis,
        "score": round(float(score), 3),
        "stem": (q.content or "")[:160],
        "matched_stem": ((item or {}).get("content") or "")[:160],
        "old_answer": q.answer or "",
        "old_analysis": q.analysis or "",
        "answer": "",
        "analysis": "",
        "state": "nomatch",
        "msg": "",
    }
    if item is None:
        base["msg"] = "这一页没找到与题干对得上的题，建议手动补"
        return base
    if score < MATCH_MIN:
        base["msg"] = f"页内最像的一条只有 {score:.0%}，不敢写（可能是相邻题的答案）"
        return base
    ans = (item.get("answer") or "").strip()
    ana = (item.get("analysis") or "").strip()
    base["answer"], base["analysis"] = ans, ana
    if not ans and not ana:
        base["state"] = "empty"
        base["msg"] = "这一页确实没有这道题的答案 / 解析（解析可能印在下一页）"
        return base
    base["state"] = "ready"
    if item.get("needs_figure") and not (q.figure_image or ""):
        base["msg"] = "注意：这题在原页上有图，但题库里没有配图"
    return base


def recheck_pages(db: Session, q: Question, pages: list[int] | None = None,
                  hint: str = "") -> list[dict]:
    """同步重读（单题用）。返回最多 len(pages) 条建议，**不写库**。"""
    doc_id, pno, basis = locate_page(db, q)
    if not doc_id:
        raise BackfillError(basis)
    plan = [int(p) for p in (pages or [pno]) if p and int(p) > 0]
    if not plan:
        raise BackfillError(f"定位不到原页：{basis}")
    out: list[dict] = []
    for p in plan:
        img = _page_image(db, doc_id, p)
        items = batch_page.recognize_page(db, img, hint)
        matched = match_items([q], items)[0]
        out.append(build_proposal(q, matched[1], matched[2], doc_id, p,
                                  basis=basis if p == pno else "老师指定的页"))
    return out


# ---------------------------------------------------------------- 批量任务
def create_job(
    db: Session,
    question_ids: list[str] | None = None,
    document_id: str | None = None,
    only_missing: bool = True,
    overwrite: bool = False,
    limit: int = 300,
) -> AnswerBackfill:
    qs = pick_targets(db, question_ids, document_id, only_missing, limit)
    if not qs:
        raise BackfillError(
            "没有需要补的题" if only_missing else "这些题都已有答案，若要重读请勾选「覆盖已有答案」"
        )
    job = AnswerBackfill(
        id=uuid.uuid4().hex[:12],
        owner_id=config.OWNER_ID,
        status="queued",
        scope=json.dumps({
            "document_id": document_id,
            "question_ids": [q.id for q in qs],
            "only_missing": bool(only_missing),
            "overwrite": bool(overwrite),
        }, ensure_ascii=False),
        total=len(qs),
        done=0,
        failed=0,
        proposals="[]",
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    db.add(job)
    db.commit()
    return job


def _save(db: Session, job_id: str, proposals: list[dict], done: int, failed: int,
          status: str | None = None, error: str | None = None) -> None:
    job = db.get(AnswerBackfill, job_id)
    if job is None:
        return
    job.proposals = json.dumps(proposals, ensure_ascii=False)
    job.done, job.failed = done, failed
    if status:
        job.status = status
    if error is not None:
        job.error = error
    if status in ("done", "partial", "failed"):
        job.finished_at = datetime.now().isoformat(timespec="seconds")
    db.commit()


def run_job(job_id: str) -> None:
    """后台线程入口。按**页**并行识别，主线程串行写建议。"""
    db = SessionLocal()
    try:
        job = db.get(AnswerBackfill, job_id)
        if job is None:
            return
        scope = json.loads(job.scope or "{}")
        qids = scope.get("question_ids") or []
        job.status = "running"
        db.commit()
        qs = [q for q in db.execute(select(Question).where(Question.id.in_(qids))).scalars()]
        groups: dict[tuple[str, int], list[tuple[Question, str]]] = {}
        unresolved: list[dict] = []
        for q in qs:
            doc_id, pno, basis = locate_page(db, q)
            if not doc_id or not pno:
                p = build_proposal(q, None, 0.0, doc_id or "", 0, basis=basis)
                p["msg"] = basis
                unresolved.append(p)
                continue
            groups.setdefault((doc_id, int(pno)), []).append((q, basis))
        total_pages = len(groups)
    finally:
        db.close()

    proposals: list[dict] = list(unresolved)
    done_pages = failed_pages = 0
    if groups:
        def _work(doc_id: str, pno: int, qs_in: list[tuple[Question, str]]):
            d = SessionLocal()
            try:
                img = _page_image(d, doc_id, pno)
                return batch_page.recognize_page(d, img), None
            except Exception as e:                       # noqa: BLE001 一页失败不能拖垮整批
                return None, str(e)[:300]
            finally:
                d.close()

        with ThreadPoolExecutor(max_workers=max(1, min(3, total_pages))) as pool:
            futs = {
                pool.submit(_work, doc_id, pno, qs_in): (doc_id, pno, qs_in)
                for (doc_id, pno), qs_in in groups.items()
            }
            for fut in as_completed(futs):
                doc_id, pno, qs_in = futs[fut]
                items, err = fut.result()
                if items is None:
                    failed_pages += 1
                    for q, basis in qs_in:
                        p = build_proposal(q, None, 0.0, doc_id, pno, basis=basis)
                        p["state"] = "error"
                        p["msg"] = f"这一页识别失败：{err}"
                        proposals.append(p)
                else:
                    done_pages += 1
                    for q, item, score in match_items([x[0] for x in qs_in], items):
                        basis = next(b for qq, b in qs_in if qq.id == q.id)
                        proposals.append(build_proposal(q, item, score, doc_id, pno, basis=basis))
                d = SessionLocal()
                try:
                    _save(d, job_id, proposals, done_pages + failed_pages, failed_pages)
                finally:
                    d.close()

    d = SessionLocal()
    try:
        bad = sum(1 for p in proposals if p["state"] in ("error", "nomatch"))
        status = "done" if not bad else ("failed" if bad == len(proposals) else "partial")
        _save(d, job_id, proposals, done_pages + failed_pages, failed_pages, status=status)
    finally:
        d.close()


def apply_proposals(db: Session, job: AnswerBackfill, qids: list[str] | None = None,
                    overwrite: bool = False) -> dict:
    """把建议写进 questions。**只写 answer / analysis 两列。**

    `overwrite=False` 时，题库里已有内容的字段**跳过** —— 老师手写过的答案
    不能被一次「补录」冲掉。这是本功能最重要的安全阀。
    """
    scope = json.loads(job.scope or "{}")
    props = json.loads(job.proposals or "[]")
    ready = [p.get("qid") for p in props if p.get("state") in ("ready", "empty")]
    allowed = set(ready)
    # qids 为空 = 「全部已匹配的都写」（前端「应用」按钮不传就这个语义）；
    # 显式给了 qids 才逐个校验 —— 传了不许写的要**报错**，不是静默丢掉。
    picked = list(ready) if not qids else [q for q in qids if q in allowed]
    if not picked:
        if qids:
            raise BackfillError("没有可应用的条目：只有「已匹配」的建议才能写入")
        raise BackfillError("这次补录没有任何「已匹配」的建议，先用「读这一页」补一次")

    by_qid = {p["qid"]: p for p in props}
    written = skipped = 0
    now = datetime.now().isoformat(timespec="seconds")
    for qid in picked:
        q = db.get(Question, qid)
        p = by_qid.get(qid)
        if q is None or not p:
            continue
        changed = False
        for field in ("answer", "analysis"):
            new = (p.get(field) or "").strip()
            if not new:
                continue
            old = (getattr(q, field) or "").strip()
            if old and not (overwrite or scope.get("overwrite")):
                skipped += 1
                continue
            if old == new:
                skipped += 1
                continue
            setattr(q, field, new)
            changed = True
        if changed:
            written += 1
    job.applied_at = now
    db.commit()
    return {"questions": written, "fields_skipped": skipped, "picked": len(picked)}


def job_dict(db: Session, job: AnswerBackfill, with_proposals: bool = True) -> dict:
    """给前端的形状。`state` 计数一并给出，老师先看「有多少条真的能补」。"""
    props = json.loads(job.proposals or "[]")
    counts: dict[str, int] = {}
    for p in props:
        counts[p.get("state", "?")] = counts.get(p.get("state", "?"), 0) + 1
    out = {
        "id": job.id,
        "status": job.status,
        "total": job.total,
        "done": job.done,
        "failed": job.failed,
        "error": job.error,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "applied_at": job.applied_at,
        "counts": counts,
        "scope": json.loads(job.scope or "{}"),
    }
    if with_proposals:
        out["proposals"] = props
    return out
