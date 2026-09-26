# -*- coding: utf-8 -*-
"""课表与课时记录（枢纽实体）+ 月度课时费汇总。"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Curriculum, Feedback, Lesson, Student
from ..schemas import LessonIn, LessonPatch
from ..services import billing
from . import lesson_files

router = APIRouter(prefix="/api", tags=["lessons"])


def _lesson_or_404(db: Session, lid: int) -> Lesson:
    ls = db.get(Lesson, lid)
    if ls is None:
        raise HTTPException(404, "课时记录不存在")
    return ls


def _serialize(db: Session, ls: Lesson) -> dict:
    student = db.get(Student, ls.student_id)
    curr = db.get(Curriculum, ls.curriculum_id) if ls.curriculum_id else None
    has_feedback = db.scalar(select(Feedback.id).where(Feedback.lesson_id == ls.id)) is not None
    try:
        node_ids = json.loads(ls.node_ids or "[]")
    except json.JSONDecodeError:
        node_ids = []
    return {
        "id": ls.id,
        "student_id": ls.student_id,
        "student_name": student.name if student else f"#{ls.student_id}",
        "curriculum_id": ls.curriculum_id,
        "curriculum_name": curr.name if curr else None,
        "start_at": ls.start_at,
        "duration_min": ls.duration_min,
        "status": ls.status,
        "mode": ls.mode,
        "location": ls.location,
        "rate": ls.rate,
        "billable": ls.billable,
        "amount": ls.amount,
        "topic": ls.topic,
        "node_ids": node_ids,
        "has_feedback": has_feedback,
        "created_at": ls.created_at,
    }


def _apply_billing(db: Session, ls: Lesson) -> None:
    """重算金额快照。单价在创建时定死，改学生单价不影响这里。"""
    student = db.get(Student, ls.student_id)
    rate_unit = student.rate_unit if student else "hour"
    effective_billable = 0 if ls.status in billing.NON_BILLABLE_STATUS else (ls.billable or 0)
    ls.billable = effective_billable
    ls.amount = billing.compute_amount(ls.rate, ls.duration_min or 0, rate_unit, effective_billable)


@router.get("/lessons")
def list_lessons(
    start: str | None = Query(default=None, description="起始日期 2026-09-01"),
    end: str | None = Query(default=None, description="结束日期 2026-09-30"),
    student_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Lesson).where(Lesson.owner_id == config.OWNER_ID)
    if start:
        stmt = stmt.where(Lesson.start_at >= start)
    if end:
        # 传入的是日期，补上时间上界，避免 '2026-09-30' < '2026-09-30T10:00'
        stmt = stmt.where(Lesson.start_at <= (end if "T" in end else f"{end}T23:59"))
    if student_id:
        stmt = stmt.where(Lesson.student_id == student_id)
    if status:
        stmt = stmt.where(Lesson.status == status)
    rows = db.scalars(stmt.order_by(Lesson.start_at)).all()
    return [_serialize(db, ls) for ls in rows]


@router.post("/lessons")
def create_lesson(payload: LessonIn, db: Session = Depends(get_db)):
    student = db.get(Student, payload.student_id)
    if student is None:
        raise HTTPException(422, "学生不存在")
    data = payload.model_dump(exclude={"node_ids", "rate"})
    ls = Lesson(
        owner_id=config.OWNER_ID,
        node_ids=json.dumps(payload.node_ids or []),
        rate=billing.resolve_rate(student, payload.rate),   # ← 单价快照
        **data,
    )
    db.add(ls)
    db.flush()
    _apply_billing(db, ls)
    db.commit()
    return _serialize(db, ls)


@router.patch("/lessons/{lid}")
def patch_lesson(lid: int, payload: LessonPatch, db: Session = Depends(get_db)):
    ls = _lesson_or_404(db, lid)
    fields = payload.model_dump(exclude_unset=True, exclude={"node_ids"})
    for k, v in fields.items():
        setattr(ls, k, v)
    if payload.node_ids is not None:
        ls.node_ids = json.dumps(payload.node_ids)
    _apply_billing(db, ls)
    db.commit()
    return _serialize(db, ls)


@router.post("/lessons/{lid}/complete")
def complete_lesson(lid: int, db: Session = Depends(get_db)):
    """标记这节课已上完 —— 课表上最常用的动作，单独给一个接口。"""
    ls = _lesson_or_404(db, lid)
    ls.status = "done"
    _apply_billing(db, ls)
    db.commit()
    return _serialize(db, ls)


@router.delete("/lessons/{lid}")
def delete_lesson(lid: int, db: Session = Depends(get_db)):
    ls = _lesson_or_404(db, lid)
    # ⚠️ 必须在删行**之前**收材质：反馈配图与上课文件的行都会随外键 CASCADE 消失，
    # 删完就查不到磁盘上该删哪几个文件了（数据库管不了文件系统）。
    lesson_files.cleanup_for_lessons(db, [lid])
    db.delete(ls)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 课时费
@router.get("/billing/monthly")
def billing_monthly(period: str | None = None, db: Session = Depends(get_db)):
    """某月的课时费汇总。period 形如 '2026-09'，默认当月。"""
    period = period or datetime.now().strftime("%Y-%m")
    return billing.monthly_summary(db, period, config.OWNER_ID)
