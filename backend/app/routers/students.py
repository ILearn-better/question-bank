# -*- coding: utf-8 -*-
"""学生管理：档案、体系归属、时间轴、能力雷达。"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import (
    AbilityDim,
    AbilityScore,
    Curriculum,
    Feedback,
    Lesson,
    Student,
    StudentCurriculum,
)
from ..schemas import StudentIn, StudentPatch
from ..services import mastery, storage
from . import lesson_files

router = APIRouter(prefix="/api", tags=["students"])


def _student_or_404(db: Session, sid: int) -> Student:
    s = db.get(Student, sid)
    if s is None:
        raise HTTPException(404, "学生不存在")
    return s


def _serialize(db: Session, s: Student, with_stats: bool = False) -> dict:
    curr_rows = db.scalars(
        select(StudentCurriculum).where(StudentCurriculum.student_id == s.id)
    ).all()
    curr_ids = [c.curriculum_id for c in curr_rows]
    names = {
        c.id: c.name
        for c in db.scalars(select(Curriculum).where(Curriculum.id.in_(curr_ids or [0]))).all()
    }
    data = {
        "id": s.id,
        "name": s.name,
        "nickname": s.nickname,
        "grade": s.grade,
        "school": s.school,
        "contact": s.contact,
        "parent_contact": s.parent_contact,
        "hourly_rate": s.hourly_rate,
        "rate_unit": s.rate_unit,
        "status": s.status,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "remark": s.remark,
        "created_at": s.created_at,
        "curriculum_ids": curr_ids,
        "curriculum_names": [names.get(i, f"#{i}") for i in curr_ids],
    }
    if with_stats:
        data["stats"] = mastery.student_stats(db, s.id)
    return data


def _sync_curricula(db: Session, s: Student, ids: list[int], primary: int | None) -> None:
    """重建学生的体系归属。去重并保证「主体系」有且仅有一个。"""
    db.execute(delete(StudentCurriculum).where(StudentCurriculum.student_id == s.id))
    unique_ids = list(dict.fromkeys(ids))
    if not unique_ids:
        return
    if primary is None or primary not in unique_ids:
        primary = unique_ids[0]
    for cid in unique_ids:
        db.add(
            StudentCurriculum(
                student_id=s.id,
                curriculum_id=cid,
                is_primary=1 if cid == primary else 0,
            )
        )


@router.get("/students")
def list_students(status: str | None = None, q: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Student).where(Student.owner_id == config.OWNER_ID)
    if status:
        stmt = stmt.where(Student.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Student.name.like(like) | Student.nickname.like(like))
    rows = db.scalars(stmt.order_by(Student.status, Student.name)).all()
    return [_serialize(db, s, with_stats=True) for s in rows]


@router.post("/students")
def create_student(payload: StudentIn, db: Session = Depends(get_db)):
    data = payload.model_dump(exclude={"curriculum_ids", "primary_curriculum_id"})
    student = Student(owner_id=config.OWNER_ID, **data)
    db.add(student)
    db.flush()
    _sync_curricula(db, student, payload.curriculum_ids, payload.primary_curriculum_id)
    db.commit()
    return _serialize(db, student)


@router.get("/students/{sid}")
def get_student(sid: int, db: Session = Depends(get_db)):
    s = _student_or_404(db, sid)
    data = _serialize(db, s, with_stats=True)
    data["ability"] = mastery.ability_radar(db, sid)
    return data


@router.patch("/students/{sid}")
def patch_student(sid: int, payload: StudentPatch, db: Session = Depends(get_db)):
    s = _student_or_404(db, sid)
    fields = payload.model_dump(exclude_unset=True, exclude={"curriculum_ids", "primary_curriculum_id"})
    for k, v in fields.items():
        setattr(s, k, v)
    if payload.curriculum_ids is not None:
        _sync_curricula(db, s, payload.curriculum_ids, payload.primary_curriculum_id)
    db.commit()
    return _serialize(db, s, with_stats=True)


@router.delete("/students/{sid}")
def delete_student(sid: int, db: Session = Depends(get_db)):
    s = _student_or_404(db, sid)
    # 删学生会级联删掉他的所有课时 —— 磁盘上的资料得先自己收（
    # 数据库管不了文件系统，CASCADE 删完就再也查不到该删哪几个文件）
    lesson_files.cleanup_for_lessons(
        db, [r[0] for r in db.execute(select(Lesson.id).where(Lesson.student_id == sid)).all()]
    )
    # 再把这个学生的归档目录整个收掉。上一步只删得掉「数据库里有登记的」文件，
    # 上传了却没保存进反馈的配图查不到，会一直躺在硬盘上 —— 整目录删才收得干净。
    storage.remove_student_dir(s)
    db.delete(s)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 学生文件夹
# 归档目录是「学生 / 上课日期」那套布局（services/storage.py）。老师常要直接翻文件夹
# （找讲义、看看孩子这段时间留下了什么），所以给他一个能看见、能打开的入口。
def _folder_usage(d) -> tuple[int, int]:
    """数磁盘上实际有多少文件、共多大。

    为什么不复用 /students/{sid}/files 的统计：那个清单读的是**数据库登记**，
    上传了没保存的配图（见 feedbacks 的 image/remove）不在里面。
    「这个文件夹里有什么」就得看文件夹本身，否则会告诉老师「0 个文件」而其实有东西。
    """
    n = total = 0
    if d.is_dir():
        for p in d.rglob("*"):
            if p.is_file():
                n += 1
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return n, total


@router.get("/students/{sid}/folder")
def student_folder_info(sid: int, db: Session = Depends(get_db)):
    """这个学生的归档文件夹在哪、里面有多少东西。路径一律服务端算，不接受外部传入。"""
    stu = _student_or_404(db, sid)
    folder = storage.student_folder(stu)
    d = config.STUDENTS_DIR / folder
    n, total = _folder_usage(d)
    return {
        "student_id": sid,
        "folder": folder,                        # u1_李芹旭
        "rel_dir": f"students/{folder}",          # 相对 data/uploads，给人看的写法
        "abs_dir": str(d),
        "exists": d.is_dir(),
        "file_count": n,
        "total_bytes": total,
    }


@router.post("/students/{sid}/folder/open")
def open_student_folder(sid: int, db: Session = Depends(get_db)):
    """在资源管理器里打开这个学生的文件夹。

    这是本机自用工具，服务端就跑在老师自己的电脑上，所以直接 os.startfile 就行；
    路径由 student id 推出（过 safe_token），不接任何外部传入的路径。
    还没传过资料的学生就先建出空文件夹 —— 打开一个空文件夹也是合理的。
    """
    stu = _student_or_404(db, sid)
    d = config.STUDENTS_DIR / storage.student_folder(stu)
    d.mkdir(parents=True, exist_ok=True)
    if not hasattr(os, "startfile"):        # 非 Windows
        raise HTTPException(400, "这个系统上不能自动打开文件夹，照上面的路径手动找一下吧")
    try:
        os.startfile(str(d))                 # noqa: S606 —— 本机自用工具，路径是我们自己拼的
    except OSError as e:
        raise HTTPException(500, f"打开文件夹失败：{e}") from e
    return {"ok": True, "path": str(d)}


# ---------------------------------------------------------------- 时间轴
@router.get("/students/{sid}/timeline")
def student_timeline(sid: int, limit: int = 100, db: Session = Depends(get_db)):
    """一节课一条记录，反馈与能力评分挂在同一条上。

    这是「学生情况记录」的核心视图：不用在多个页面之间跳，
    打开就是这个人从头到尾发生了什么。
    """
    _student_or_404(db, sid)
    lessons = list(
        db.scalars(
            select(Lesson).where(Lesson.student_id == sid).order_by(Lesson.start_at.desc()).limit(limit)
        ).all()
    )
    if not lessons:
        return []

    lesson_ids = [ls.id for ls in lessons]
    feedbacks = {
        f.lesson_id: f
        for f in db.scalars(select(Feedback).where(Feedback.lesson_id.in_(lesson_ids))).all()
    }
    dim_names = {
        d.id: d.name
        for d in db.scalars(select(AbilityDim).where(AbilityDim.owner_id == config.OWNER_ID)).all()
    }
    scores_by_lesson: dict[int, list[dict]] = {}
    for sc in db.scalars(
        select(AbilityScore).where(AbilityScore.lesson_id.in_(lesson_ids))
    ).all():
        scores_by_lesson.setdefault(sc.lesson_id, []).append(
            {"dim_id": sc.dim_id, "name": dim_names.get(sc.dim_id, f"#{sc.dim_id}"), "score": sc.score}
        )

    out = []
    for ls in lessons:
        fb = feedbacks.get(ls.id)
        try:
            node_ids = json.loads(ls.node_ids or "[]")
        except json.JSONDecodeError:
            node_ids = []
        out.append(
            {
                "lesson": {
                    "id": ls.id,
                    "start_at": ls.start_at,
                    "duration_min": ls.duration_min,
                    "status": ls.status,
                    "mode": ls.mode,
                    "location": ls.location,
                    "rate": ls.rate,
                    "amount": ls.amount,
                    "billable": ls.billable,
                    "topic": ls.topic,
                    "node_ids": node_ids,
                    "curriculum_id": ls.curriculum_id,
                },
                "feedback": None
                if fb is None
                else {
                    "id": fb.id,
                    "performance": fb.performance,
                    "problems": fb.problems,
                    "homework": fb.homework,
                    "next_plan": fb.next_plan,
                    "rating": fb.rating,
                    "share_to_parent": fb.share_to_parent,
                    "created_at": fb.created_at,
                },
                "ability": scores_by_lesson.get(ls.id, []),
            }
        )
    return out
