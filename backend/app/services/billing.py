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

# 已排定、但还没上 —— 钱还没到手，算「待上」而不是「应收」。
# ⚠️ 口径只认 scheduled。moved（已调课）在本项目里**没有写入路径**（只有标签），
#    真出现时得先想清楚它算哪边，别默认并进来。
PENDING_STATUS = {"scheduled"}


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
    """某月（'2026-09'）的课时费汇总：每个学生应收 + 待上 + 总计。

    · **应收（amount）**：只算 done 与 makeup（已上/补课）—— 已经到手的钱。
    · **待上（pending_*）**：scheduled（已排好、还没上）—— 这个月**还要收**的钱，
      按排课时定下的单价快照预估。上完课它会自动从「待上」转到「应收」。
    · 取消 / 请假两边都不算，只留痕。

    ⚠️ 待上的课时如果没填单价，amount 是 None，钱**算不出来**。这种情况记进
       `pending_no_rate` 让界面能提醒老师，**绝不 `or 0.0` 当成 0 混过去** ——
       那等于告诉她「这节课不收钱」，而真相只是「你没填单价」。
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
                # 已排课、还没上（下面按需累加）
                "pending_lessons": 0,
                "pending_minutes": 0,
                "pending_amount": 0.0,
                "pending_no_rate": 0,
            },
        )
        if ls.status in ("done", "makeup"):
            bucket["lesson_count"] += 1
            bucket["billable_minutes"] += ls.duration_min or 0
            bucket["amount"] += ls.amount or 0.0
        elif ls.status in PENDING_STATUS:
            bucket["pending_lessons"] += 1
            bucket["pending_minutes"] += ls.duration_min or 0
            if ls.amount is None:
                bucket["pending_no_rate"] += 1      # 没单价：不计金额，但要报出来
            else:
                bucket["pending_amount"] += ls.amount
        elif ls.status in NON_BILLABLE_STATUS:
            pass          # 留痕不计数

    rows = sorted(per_student.values(), key=lambda r: -r["amount"])
    for r in rows:
        r["amount"] = round(r["amount"], 2)
        r["pending_amount"] = round(r["pending_amount"], 2)
    return {
        "period": period,
        "students": rows,
        "total_amount": round(sum(r["amount"] for r in rows), 2),
        "total_lessons": sum(r["lesson_count"] for r in rows),
        "total_minutes": sum(r["billable_minutes"] for r in rows),
        # ---- 待上（已排课、还没上）----
        "pending_total_amount": round(sum(r["pending_amount"] for r in rows), 2),
        "pending_total_lessons": sum(r["pending_lessons"] for r in rows),
        "pending_total_minutes": sum(r["pending_minutes"] for r in rows),
        "pending_no_rate": sum(r["pending_no_rate"] for r in rows),
    }
