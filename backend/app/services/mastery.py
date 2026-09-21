# -*- coding: utf-8 -*-
"""学情聚合：把散落的记录变成家长看得懂的东西。

第一阶段只需要能力维度雷达图 —— 数据源是老师每节课的主观评分，
**完全不依赖题库判分闭环**。这是家长报告能从「最后一步」提到 Phase 3 的原因。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AbilityDim, AbilityScore, Feedback, Lesson


def ability_radar(db: Session, student_id: int, owner_id: int = 1) -> dict:
    """最新一次 vs 上一次的能力雷达数据（**按维度各自回溯**）。

    设计要点一：家长报告要能看出「变化」，所以每个维度都给两个值，
    而不是一张孤立的图 —— 孤立的雷达图家长其实看不懂好还是不好。

    设计要点二：**不要求每节课把 5 个维度都打完**。
    记录成本是生命线，允许某节课只点 3 下。所以这里按「维度」而不是按「课时」
    回溯：某个维度没打分就往前找上一次的分数，而不是整组丢掉。
    """
    dims = list(
        db.scalars(
            select(AbilityDim)
            .where(AbilityDim.owner_id == owner_id, AbilityDim.active == 1)
            .order_by(AbilityDim.sort_order, AbilityDim.id)
        ).all()
    )
    scores = list(
        db.scalars(
            select(AbilityScore)
            .where(AbilityScore.student_id == student_id)
            .order_by(AbilityScore.created_at, AbilityScore.id)
        ).all()
    )

    history: dict[int, list[int]] = {}
    for s in scores:
        history.setdefault(s.dim_id, []).append(s.score)

    rows = []
    for d in dims:
        seq = history.get(d.id, [])
        rows.append(
            {
                "dim_id": d.id,
                "name": d.name,
                "latest": seq[-1] if seq else None,
                "previous": seq[-2] if len(seq) >= 2 else None,
                "count": len(seq),
            }
        )
    return {"dims": rows, "has_data": any(r["latest"] is not None for r in rows)}


def student_stats(db: Session, student_id: int) -> dict:
    """学生概览数字：上了多少节课、累计多久、最近一次上课时间、待写反馈数。"""
    lessons = list(db.scalars(select(Lesson).where(Lesson.student_id == student_id)).all())
    feedback_lesson_ids = {
        f.lesson_id for f in db.scalars(select(Feedback).where(Feedback.student_id == student_id)).all()
    }
    done = [ls for ls in lessons if ls.status in ("done", "makeup")]
    done.sort(key=lambda ls: ls.start_at)
    return {
        "lesson_total": len(lessons),
        "lesson_done": len(done),
        "minutes_total": sum(ls.duration_min or 0 for ls in done),
        "last_lesson_at": done[-1].start_at if done else None,
        "feedback_pending": sum(1 for ls in done if ls.id not in feedback_lesson_ids),
        "amount_total": round(sum(ls.amount or 0 for ls in done), 2),
    }
