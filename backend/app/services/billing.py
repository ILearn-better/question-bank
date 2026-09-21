# -*- coding: utf-8 -*-
"""课时费结算规则。

两条财务正确性铁律（开发文档 §6.3）：
  1. **单价快照**：金额在课时记录创建时就定死。学生后来涨价，不改历史课时。
     否则你三个月后回头看流水，每一条金额都会变，等于没有账。
  2. **结算单生成即冻结**：不在本期实现，但数据结构已为它留好位置（invoice_id 字段）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Lesson, Student

# 不计费的课时状态（请假留痕但不产生金额）
NON_BILLABLE_STATUS = {"cancelled", "leave"}


def compute_amount(
    rate: float | None, duration_min: int, rate_unit: str = "hour", billable: int = 1
) -> float | None:
    """按「单价 + 时长 + 计价单位」算应计金额。"""
    if rate is None:
        return None
    if not billable:
        return 0.0
    if rate_unit == "session":
        return round(float(rate), 2)          # 按次：与时长无关
    return round(float(rate) * (duration_min or 0) / 60.0, 2)


def resolve_rate(student: Student | None, override: float | None) -> float | None:
    """确定这一节课的单价：先用调用方传的，否则取学生当前默认单价。"""
    if override is not None:
        return override
    return student.hourly_rate if student else None


def monthly_summary(db: Session, period: str, owner_id: int = 1) -> dict:
    """某月（'2026-09'）的课时费汇总：每个学生应收 + 总计。

    只统计 done 与 makeup（已上课/补课）；scheduled 是还没上的，不算进收入。
    """
    lessons = list(
        db.scalars(
            select(Lesson).where(
                Lesson.owner_id == owner_id,
                Lesson.start_at.like(f"{period}%"),
            )
        ).all()
    )
    students = {s.id: s for s in db.scalars(select(Student)).all()}

    per_student: dict[int, dict] = {}
    for ls in lessons:
        bucket = per_student.setdefault(
            ls.student_id,
            {
                "student_id": ls.student_id,
                "student_name": students.get(ls.student_id).name if students.get(ls.student_id) else f"#{ls.student_id}",
                "billable_minutes": 0,
                "lesson_count": 0,
                "amount": 0.0,
            },
        )
        if ls.status in ("done", "makeup"):
            bucket["lesson_count"] += 1
            bucket["billable_minutes"] += ls.duration_min or 0
            bucket["amount"] += ls.amount or 0.0
        elif ls.status in NON_BILLABLE_STATUS:
            pass          # 留痕不计数

    rows = sorted(per_student.values(), key=lambda r: -r["amount"])
    for r in rows:
        r["amount"] = round(r["amount"], 2)
    return {
        "period": period,
        "students": rows,
        "total_amount": round(sum(r["amount"] for r in rows), 2),
        "total_lessons": sum(r["lesson_count"] for r in rows),
        "total_minutes": sum(r["billable_minutes"] for r in rows),
    }
