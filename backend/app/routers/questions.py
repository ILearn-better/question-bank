# -*- coding: utf-8 -*-
"""题库接口（原 main.py 的题目接口，响应字段保持向后兼容）。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Node, Question, QuestionNode
from ..schemas import QuestionIn

router = APIRouter(prefix="/api", tags=["questions"])


def _serialize(q: Question) -> dict:
    try:
        kps = json.loads(q.knowledge_points or "[]")
    except json.JSONDecodeError:
        kps = []
    return {
        "id": q.id,
        "document_id": q.document_id,
        "doc_filename": q.doc_filename,
        "start_block": q.start_block,
        "end_block": q.end_block,
        "content": q.content,
        "qtype": q.qtype,
        "difficulty": q.difficulty,
        "knowledge_points": kps,
        "answer": q.answer,
        "analysis": q.analysis,
        "image": q.image,
        "answer_image": q.answer_image or "",
        "created_at": q.created_at,
        # 新增字段（纯增量，不影响旧前端）
        "curriculum_id": q.curriculum_id,
        "node_id": q.node_id,
        "source": q.source,
        "year": q.year,
        "usage_count": q.usage_count,
        "stem_format": q.stem_format,
    }


@router.post("/questions")
def create_question(payload: QuestionIn, db: Session = Depends(get_db)):
    if not payload.content.strip() and not payload.image:
        raise HTTPException(422, "题目内容不能为空（文本或截图至少一项）")

    qid = uuid.uuid4().hex[:12]
    db.add(
        Question(
            id=qid,
            owner_id=config.OWNER_ID,
            document_id=payload.document_id,
            doc_filename=payload.doc_filename,
            start_block=payload.start_block,
            end_block=payload.end_block,
            content=payload.content,
            qtype=payload.qtype,
            difficulty=payload.difficulty,
            knowledge_points=json.dumps(payload.knowledge_points, ensure_ascii=False),
            answer=payload.answer,
            analysis=payload.analysis,
            image=payload.image,
            answer_image=payload.answer_image,
            created_at=datetime.now().isoformat(timespec="seconds"),
            curriculum_id=payload.curriculum_id,
            node_id=payload.node_id,
            source=payload.source,
            year=payload.year,
            stem_format=payload.stem_format,
        )
    )
    # 主知识点同时写入关系表 —— 将来算掌握度靠它，靠 JSON 数组是算不动的
    if payload.node_id:
        db.add(QuestionNode(question_id=qid, node_id=payload.node_id, weight=1.0))
    db.commit()
    return {"id": qid}


@router.get("/questions")
def list_questions(
    document_id: str | None = None,
    curriculum_id: int | None = None,
    node_id: int | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Question)
    if document_id:
        stmt = stmt.where(Question.document_id == document_id).order_by(Question.created_at)
    elif node_id:
        stmt = (
            select(Question)
            .join(QuestionNode, QuestionNode.question_id == Question.id)
            .where(QuestionNode.node_id == node_id)
            .order_by(Question.created_at.desc())
        )
    else:
        stmt = stmt.order_by(Question.created_at.desc())
    if curriculum_id:
        stmt = stmt.where(Question.curriculum_id == curriculum_id)
    return [_serialize(q) for q in db.scalars(stmt).all()]


def _subtree_ids(db: Session, root_id: int) -> list[int]:
    """一棵知识点的子树 id（含自己）。按「章节」筛也能筛出它下面小节的题，
    否则老师得把整棵子树逐个勾一遍。"""
    rows = db.execute(select(Node.id, Node.parent_id)).all()
    kids: dict[int | None, list[int]] = {}
    for nid, pid in rows:
        kids.setdefault(pid, []).append(nid)
    out: list[int] = []
    stack = [root_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.append(cur)
        stack.extend(kids.get(cur, []))
    return out


@router.get("/questions/search")
def search_questions(
    keyword: str | None = None,
    curriculum_id: int | None = None,
    node_id: int | None = None,
    qtype: str | None = None,
    difficulty: str | None = None,
    has_image: bool | None = None,
    with_answer: bool | None = None,
    sort: str = Query("created", description="created / oldest / usage"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """按条件筛题库（出卷用），响应 {total, items}。

    与 GET /questions 的区别：多了题型/难度/知识点/关键词等条件、分页、以及排序，
    因为出卷页要显示「共 N 题」并翻页。**旧接口一行没动**，录题页不受影响。
    """
    conds = []
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        conds.append(or_(
            Question.content.like(like),
            Question.answer.like(like),
            Question.source.like(like),
            Question.doc_filename.like(like),
        ))
    if curriculum_id:
        conds.append(Question.curriculum_id == curriculum_id)
    if node_id:
        ids = _subtree_ids(db, node_id)
        linked = select(QuestionNode.question_id).where(QuestionNode.node_id.in_(ids))
        conds.append(or_(Question.node_id.in_(ids), Question.id.in_(linked)))
    if qtype:
        conds.append(Question.qtype == qtype)
    if difficulty:
        conds.append(Question.difficulty == difficulty)
    if has_image is not None:
        # coalesce 是为了 NULL 安全：老数据里 image 可能是 NULL
        cond = func.coalesce(Question.image, "") != ""
        conds.append(cond if has_image else ~cond)
    if with_answer is not None:
        has_ans = or_(
            func.coalesce(Question.answer, "") != "",
            func.coalesce(Question.answer_image, "") != "",
        )
        conds.append(has_ans if with_answer else ~has_ans)

    total = db.scalar(select(func.count()).select_from(Question).where(*conds)) or 0

    order = {
        "created": Question.created_at.desc(),
        "oldest": Question.created_at.asc(),
        # 少用的排前面：出过卷的题尽量别再出，usage_count 就是为这个留的
        "usage": func.coalesce(Question.usage_count, 0).asc(),
    }.get(sort, Question.created_at.desc())

    rows = db.scalars(
        select(Question).where(*conds)
        .order_by(order, Question.created_at.desc())
        .limit(limit).offset(offset)
    ).all()
    return {"total": total, "items": [_serialize(q) for q in rows]}


@router.delete("/questions/{qid}")
def delete_question(qid: str, db: Session = Depends(get_db)):
    # 先删关系表（外键现在是真生效的，顺序错了会撞约束）
    db.execute(delete(QuestionNode).where(QuestionNode.question_id == qid))
    db.execute(delete(Question).where(Question.id == qid))
    db.commit()
    return {"ok": True}
