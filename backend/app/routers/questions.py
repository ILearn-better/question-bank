# -*- coding: utf-8 -*-
"""题库接口（原 main.py 的题目接口，响应字段保持向后兼容）。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Question, QuestionNode
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


@router.delete("/questions/{qid}")
def delete_question(qid: str, db: Session = Depends(get_db)):
    # 先删关系表（外键现在是真生效的，顺序错了会撞约束）
    db.execute(delete(QuestionNode).where(QuestionNode.question_id == qid))
    db.execute(delete(Question).where(Question.id == qid))
    db.commit()
    return {"ok": True}
