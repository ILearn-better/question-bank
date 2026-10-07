# -*- coding: utf-8 -*-
"""课前备课：老师上课前做的准备（一节课一条，与反馈/作业/课后补充并列）。

与课后补充（supplements.py）的区别：
  · 那边是「课后挑好、下次**发给学生**」，一节可以好几批；
  · 这边是「课前老师**自己准备这节课怎么上**」，一节一条，改了就是改这一份。

与笔记库的**双向**联动（note_id 是这个枢纽）：
  · 从笔记库选了一篇 → note_id 指向它（备课引用/基于这篇笔记）。
  · 写完后点「存成笔记」→ `save_as_note` 把结构化四段 + 挑的材料拼成 Markdown
    写进笔记库（已有 note_id 且笔记还在就更新那篇，否则新建落「未归档」），
    再把 id 回写到 note_id。

挑的材料（items）复用课后补充的 shape（kind / ref_id / 标题快照），
但存独立一张表 —— 语义不同，不塞进 supplement_items。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import (
    Lesson,
    LessonPrep,
    LessonPrepItem,
    Note,
    Question,
    Student,
)
from ..schemas import PrepIn
from ..services import storage
from . import note_folders

router = APIRouter(prefix="/api", tags=["prep"])

KINDS = {"question", "note"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _question_title(q: Question) -> str:
    """题目的「标题」= 题干开头一截（题库里没有标题这个字段）。"""
    text = (q.content or "").replace("\n", " ").strip()
    if not text and q.image:
        return "（图片题）"
    return (text[:40] + "…") if len(text) > 40 else (text or "（空题）")


def _note_title(n: Note) -> str:
    return n.title or "未命名笔记"


def _out(p: LessonPrep, db: Session) -> dict:
    """一条备课的完整形状。items 带 `exists` 告诉界面「引用的东西还在不在」。"""
    items = []
    for it in sorted(p.items, key=lambda x: (x.sort_order, x.id)):
        if it.kind == "question":
            alive = db.get(Question, it.ref_id) is not None
        else:
            alive = db.get(Note, it.ref_id) is not None
        items.append({"id": it.id, "kind": it.kind, "ref_id": it.ref_id,
                      "title": it.title, "exists": alive})
    note = db.get(Note, p.note_id) if p.note_id else None
    return {
        "id": p.id, "lesson_id": p.lesson_id, "student_id": p.student_id,
        "goal": p.goal or "", "key_points": p.key_points or "",
        "flow": p.flow or "", "materials": p.materials or "",
        "note_id": p.note_id or "",
        "note_title": note.title if note else "",
        "items": items,
        "questions": [i["ref_id"] for i in items if i["kind"] == "question"],
        "notes": [i["ref_id"] for i in items if i["kind"] == "note"],
        "created_at": p.created_at, "updated_at": p.updated_at,
    }


def _prep_or_none(db: Session, lid: int) -> LessonPrep | None:
    return db.scalar(select(LessonPrep).where(LessonPrep.lesson_id == lid))


def _lesson_or_404(db: Session, lid: int) -> Lesson:
    ls = db.get(Lesson, lid)
    if ls is None:
        raise HTTPException(404, "课时记录不存在")
    return ls


@router.get("/lessons/{lid}/prep")
def get_prep(lid: int, db: Session = Depends(get_db)):
    """这节课的备课。没有时返回 None（界面据此显示「还没备课」）。"""
    _lesson_or_404(db, lid)
    p = _prep_or_none(db, lid)
    return {"prep": _out(p, db) if p else None}


@router.put("/lessons/{lid}/prep")
def upsert_prep(lid: int, payload: PrepIn, db: Session = Depends(get_db)):
    """存/改备课（一节一条）。四个字段 + note_id 都是「没传 = 保持」；
    items 传了（含空数组）= 整组替换，None = 不动。"""
    ls = _lesson_or_404(db, lid)
    p = _prep_or_none(db, lid)
    if p is None:
        p = LessonPrep(owner_id=config.OWNER_ID, lesson_id=lid, student_id=ls.student_id)
        db.add(p)
        db.flush()
    data = payload.model_dump(exclude_unset=True)

    for f in ("goal", "key_points", "flow", "materials"):
        if f in data:
            setattr(p, f, (data[f] or "").strip())

    if "note_id" in data:
        nid = (data["note_id"] or "").strip()
        if nid:
            if db.get(Note, nid) is None:
                raise HTTPException(404, "关联的笔记不存在")
            p.note_id = nid
        else:
            p.note_id = None            # 传空串 = 解除关联

    if "items" in data:
        for old in list(p.items):       # 整组替换：先清旧，再按新顺序重建
            db.delete(old)
        db.flush()
        seen: set[tuple[str, str]] = set()
        # ⚠️ 这里必须用 payload.items（Pydantic 对象，有 .kind/.ref_id），
        # 不能用 data["items"] —— model_dump 把它转成了 dict，属性访问会 500。
        for i, it in enumerate(payload.items or []):
            kind = (it.kind or "").strip()
            if kind not in KINDS:
                raise HTTPException(422, f"不认识的内容类型 {kind}（只能 question / note）")
            ref = (it.ref_id or "").strip()
            if not ref or (kind, ref) in seen:
                continue                 # 重复选的同一项只算一次
            seen.add((kind, ref))
            if kind == "question":
                q = db.get(Question, ref)
                if q is None:
                    raise HTTPException(404, f"题目 {ref} 不存在")
                title = _question_title(q)
            else:
                n = db.get(Note, ref)
                if n is None:
                    raise HTTPException(404, f"笔记 {ref} 不存在")
                title = _note_title(n)
            db.add(LessonPrepItem(prep_id=p.id, kind=kind, ref_id=ref,
                                  title=title, sort_order=i, created_at=_now()))

    p.updated_at = _now()
    db.commit()
    db.refresh(p)
    return {"prep": _out(p, db)}


@router.delete("/lessons/{lid}/prep")
def delete_prep(lid: int, db: Session = Depends(get_db)):
    """整份撤掉。**不删任何题目/笔记** —— 那两样是题库与笔记那边的资产。"""
    p = _prep_or_none(db, lid)
    if p is not None:
        db.delete(p)
        db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 存成笔记
def _build_prep_markdown(p: LessonPrep, db: Session) -> str:
    """把备课的结构化四段 + 挑的材料拼成一篇 Markdown。空字段不输出标题。"""
    lines: list[str] = []
    sections = [
        ("教学目标", p.goal),
        ("重点难点", p.key_points),
        ("教学流程", p.flow),
        ("准备材料", p.materials),
    ]
    for title, body in sections:
        if (body or "").strip():
            lines += [f"## {title}", "", body.strip(), ""]
    items = sorted(p.items, key=lambda x: (x.sort_order, x.id))
    if items:
        lines += ["## 这节课要用的材料", ""]
        for it in items:
            kind = "题" if it.kind == "question" else "笔记"
            alive = (db.get(Question, it.ref_id) is not None
                     if it.kind == "question" else db.get(Note, it.ref_id) is not None)
            suffix = "" if alive else "（内容已删）"
            lines.append(f"- {kind}·{it.title}{suffix}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@router.post("/lessons/{lid}/prep/save-as-note")
def save_as_note(lid: int, db: Session = Depends(get_db)):
    """把这份备课「写回」笔记库（双向联动的另一半）。

    已有 note_id 且笔记还在 → 更新那篇的正文（标题保留用户自己起的）；
    否则 → 新建一篇，落「未归档」，标题 = `学生_日期_备课`。
    最后把笔记 id 回写到 lesson_preps.note_id。
    """
    ls = _lesson_or_404(db, lid)
    p = _prep_or_none(db, lid)
    if p is None:
        raise HTTPException(404, "这节课还没备课，先写点内容再存成笔记")

    md = _build_prep_markdown(p, db)
    note = db.get(Note, p.note_id) if p.note_id else None
    now = _now()
    created = note is None
    if note is None:
        student = db.get(Student, ls.student_id)
        stem = storage.feedback_stem(student, ls) if student else f"学生{ls.student_id}_{storage.lesson_date(ls)}"
        note = Note(
            id=uuid.uuid4().hex[:12],
            owner_id=config.OWNER_ID,
            folder_id=note_folders.unfiled_root(db).id,
            title=f"{stem}_备课",
            content=md,
            ink="[]",
            pinned=0,
            created_at=now,
            updated_at=now,
        )
        db.add(note)
        db.flush()
    else:
        note.content = md            # 只覆盖正文，标题保留用户自己起的
        note.updated_at = now
    p.note_id = note.id
    p.updated_at = now
    db.commit()
    return {"ok": True, "note_id": note.id, "note_title": note.title,
            "created": created}
