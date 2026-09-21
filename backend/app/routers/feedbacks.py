# -*- coding: utf-8 -*-
"""课后反馈与能力评分。

设计约束（开发文档 §6.5）：**所有字段可选，允许只写一句话就存。**
绝不能让"字段没填完"卡住记录 —— 一旦卡住，这个系统就会像所有失败的工具一样被弃用。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import AbilityDim, AbilityScore, Feedback, Lesson
from ..schemas import DimIn, FeedbackIn

router = APIRouter(prefix="/api", tags=["feedbacks"])


def _lesson_or_404(db: Session, lid: int) -> Lesson:
    ls = db.get(Lesson, lid)
    if ls is None:
        raise HTTPException(404, "课时记录不存在")
    return ls


def _serialize(fb: Feedback, scores: list[AbilityScore], dims: dict[int, str]) -> dict:
    return {
        "id": fb.id,
        "lesson_id": fb.lesson_id,
        "student_id": fb.student_id,
        "performance": fb.performance,
        "problems": fb.problems,
        "homework": fb.homework,
        "next_plan": fb.next_plan,
        "rating": fb.rating,
        "share_to_parent": fb.share_to_parent,
        "created_at": fb.created_at,
        "ability_scores": [
            {"dim_id": s.dim_id, "name": dims.get(s.dim_id, f"#{s.dim_id}"), "score": s.score}
            for s in scores
        ],
    }


def _dim_map(db: Session) -> dict[int, str]:
    return {d.id: d.name for d in db.scalars(select(AbilityDim)).all()}


# ---------------------------------------------------------------- 能力维度
@router.get("/ability-dims")
def list_dims(include_inactive: bool = False, db: Session = Depends(get_db)):
    stmt = select(AbilityDim).where(AbilityDim.owner_id == config.OWNER_ID)
    if not include_inactive:
        stmt = stmt.where(AbilityDim.active == 1)
    rows = db.scalars(stmt.order_by(AbilityDim.sort_order, AbilityDim.id)).all()
    return [
        {"id": d.id, "name": d.name, "curriculum_id": d.curriculum_id,
         "sort_order": d.sort_order, "active": d.active}
        for d in rows
    ]


@router.post("/ability-dims")
def create_dim(payload: DimIn, db: Session = Depends(get_db)):
    d = AbilityDim(owner_id=config.OWNER_ID, **payload.model_dump())
    db.add(d)
    db.commit()
    return {"id": d.id}


@router.patch("/ability-dims/{dim_id}")
def patch_dim(dim_id: int, payload: DimIn, db: Session = Depends(get_db)):
    d = db.get(AbilityDim, dim_id)
    if d is None:
        raise HTTPException(404, "维度不存在")
    d.name = payload.name
    d.curriculum_id = payload.curriculum_id
    d.sort_order = payload.sort_order
    db.commit()
    return {"ok": True}


@router.delete("/ability-dims/{dim_id}")
def delete_dim(dim_id: int, db: Session = Depends(get_db)):
    """停用而不是删除 —— 历史评分必须保留，否则雷达图的「变化」就断了。"""
    d = db.get(AbilityDim, dim_id)
    if d is None:
        raise HTTPException(404, "维度不存在")
    d.active = 0
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 反馈
@router.get("/lessons/{lid}/feedback")
def get_feedback(lid: int, db: Session = Depends(get_db)):
    _lesson_or_404(db, lid)
    fb = db.scalar(select(Feedback).where(Feedback.lesson_id == lid))
    if fb is None:
        return None
    scores = db.scalars(select(AbilityScore).where(AbilityScore.lesson_id == lid)).all()
    return _serialize(fb, list(scores), _dim_map(db))


@router.put("/lessons/{lid}/feedback")
def upsert_feedback(lid: int, payload: FeedbackIn, db: Session = Depends(get_db)):
    """一节课一条反馈：不存在就建，存在就覆盖。前端不必关心是新增还是修改。"""
    ls = _lesson_or_404(db, lid)
    fb = db.scalar(select(Feedback).where(Feedback.lesson_id == lid))
    if fb is None:
        fb = Feedback(owner_id=config.OWNER_ID, lesson_id=lid, student_id=ls.student_id)
        db.add(fb)
    fb.performance = payload.performance
    fb.problems = payload.problems
    fb.homework = payload.homework
    fb.next_plan = payload.next_plan
    fb.rating = payload.rating
    fb.share_to_parent = payload.share_to_parent
    db.flush()

    # 能力评分整组替换：不用逐条 diff，语义更清楚，也不会留下上次多打的分数
    if payload.ability_scores:
        db.execute(delete(AbilityScore).where(AbilityScore.lesson_id == lid))
        for item in payload.ability_scores:
            db.add(
                AbilityScore(
                    owner_id=config.OWNER_ID,
                    student_id=ls.student_id,
                    dim_id=item.dim_id,
                    lesson_id=lid,
                    score=item.score,
                    period=ls.start_at[:7],
                )
            )
    db.commit()
    scores = db.scalars(select(AbilityScore).where(AbilityScore.lesson_id == lid)).all()
    return _serialize(fb, list(scores), _dim_map(db))


@router.delete("/lessons/{lid}/feedback")
def delete_feedback(lid: int, db: Session = Depends(get_db)):
    db.execute(delete(AbilityScore).where(AbilityScore.lesson_id == lid))
    db.execute(delete(Feedback).where(Feedback.lesson_id == lid))
    db.commit()
    return {"ok": True}


@router.get("/students/{sid}/feedbacks")
def student_feedbacks(sid: int, limit: int = 30, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Feedback)
        .where(Feedback.student_id == sid)
        .order_by(Feedback.created_at.desc())
        .limit(limit)
    ).all()
    dims = _dim_map(db)
    out = []
    for fb in rows:
        scores = db.scalars(select(AbilityScore).where(AbilityScore.lesson_id == fb.lesson_id)).all()
        out.append(_serialize(fb, list(scores), dims))
    return out
