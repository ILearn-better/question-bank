# -*- coding: utf-8 -*-
"""今日工作台：打开系统第一眼要看到的东西。

首页只回答四个问题：
  今天有哪几节课？哪些课上完了还没写反馈？本月收了多少钱？有多少在读学生？
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Feedback, Lesson, Student
from ..services import billing

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/today")
def today(db: Session = Depends(get_db)):
    today_str = date.today().isoformat()

    lessons_today = list(
        db.scalars(
            select(Lesson)
            .where(Lesson.owner_id == config.OWNER_ID, Lesson.start_at.like(f"{today_str}%"))
            .order_by(Lesson.start_at)
        ).all()
    )

    # 已经上完、但还没写反馈的课（按时间倒序，最该先写的排最前）
    done_lessons = list(
        db.scalars(
            select(Lesson)
            .where(Lesson.owner_id == config.OWNER_ID, Lesson.status.in_(("done", "makeup")))
            .order_by(Lesson.start_at.desc())
            .limit(50)
        ).all()
    )
    feedback_ids = {f.lesson_id for f in db.scalars(select(Feedback)).all()}
    pending = [ls for ls in done_lessons if ls.id not in feedback_ids][:10]

    # 未来 7 天的安排
    horizon = (date.today() + timedelta(days=7)).isoformat()
    upcoming = list(
        db.scalars(
            select(Lesson)
            .where(
                Lesson.owner_id == config.OWNER_ID,
                Lesson.start_at > f"{today_str}T23:59",
                Lesson.start_at <= f"{horizon}T23:59",
                Lesson.status == "scheduled",
            )
            .order_by(Lesson.start_at)
        ).all()
    )

    students = {
        s.id: s
        for s in db.scalars(select(Student).where(Student.owner_id == config.OWNER_ID)).all()
    }

    def brief(ls: Lesson) -> dict:
        st = students.get(ls.student_id)
        return {
            "id": ls.id,
            "student_id": ls.student_id,
            "student_name": st.name if st else f"#{ls.student_id}",
            "start_at": ls.start_at,
            "duration_min": ls.duration_min,
            "status": ls.status,
            "mode": ls.mode,
            "topic": ls.topic,
            "amount": ls.amount,
            "has_feedback": ls.id in feedback_ids,
        }

    month = billing.monthly_summary(db, datetime.now().strftime("%Y-%m"), config.OWNER_ID)

    return {
        "date": today_str,
        "lessons_today": [brief(ls) for ls in lessons_today],
        "pending_feedback": [brief(ls) for ls in pending],
        "upcoming": [brief(ls) for ls in upcoming],
        "month": month,
        "counts": {
            "students_total": len(students),
            "students_active": sum(1 for s in students.values() if s.status == "active"),
            "lessons_total": db.scalar(select(func.count()).select_from(Lesson)) or 0,
            "feedbacks_total": len(feedback_ids),
        },
    }
