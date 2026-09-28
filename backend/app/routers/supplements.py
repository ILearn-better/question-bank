# -*- coding: utf-8 -*-
"""课后补充：把「下次上课要给他的材料」记在今天那节课里。

一条贯穿这个文件的取向：**老师在上完课、改完作业的那一刻顺手记一笔，之后不用再操心。**

  · 与作业方向相反，所以两块并列、互不依赖：作业是收（他交回来的），
    这里是发（我准备下次给他的）。一节课可以有好几批（今天 3 题、晚上再加 2 题）。
  · 状态只有三个：还没打印 / 已给 / 已交回。**打印时自动置「已给」** ——
    一个动作能推出来的状态就不要让人再点一次（但可以手动改回来）。
  · 「还没给」要能被看到：学生详情页会问一句「这个学生有几批还没给」。
    不然记了也没人回头看，这条链路就白做了（打印发生在下一次课，中间隔几天）。

关于「内容被删了」：条目里存**标题快照**，删了就显示「（内容已删）」，
不留一片空白让人猜。这里存引用就够 —— 它是日志，内容变了不影响日志的意义。
"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Lesson, LessonSupplement, LessonSupplementItem, Note, Question, Student
from ..schemas import SupplementIn, SupplementPatch
from ..services import paper_export, storage

router = APIRouter(prefix="/api", tags=["supplements"])

# 状态：字面就是老师要说的话（界面上直接用）
STATUS_CN = {"todo": "还没打印", "given": "已给", "returned": "已交回"}
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


def _out(s: LessonSupplement, db: Session) -> dict:
    """一批的完整形状。`exists` 告诉界面「引用的东西还在不在」。"""
    items = []
    for it in sorted(s.items, key=lambda x: (x.sort_order, x.id)):
        if it.kind == "question":
            alive = db.get(Question, it.ref_id) is not None
        else:
            alive = db.get(Note, it.ref_id) is not None
        items.append({"id": it.id, "kind": it.kind, "ref_id": it.ref_id,
                      "title": it.title, "exists": alive})
    return {
        "id": s.id, "lesson_id": s.lesson_id, "focus": s.focus, "note": s.note,
        "status": s.status, "status_label": STATUS_CN.get(s.status, s.status),
        "created_at": s.created_at, "items": items,
        "questions": [i["ref_id"] for i in items if i["kind"] == "question"],
        "notes": [i["ref_id"] for i in items if i["kind"] == "note"],
    }


def _supplement_or_404(db: Session, sid: int) -> LessonSupplement:
    s = db.get(LessonSupplement, sid)
    if s is None:
        raise HTTPException(404, "这条课后补充不存在")
    return s


@router.get("/lessons/{lid}/supplements")
def list_supplements(lid: int, db: Session = Depends(get_db)):
    """这节课的课后补充（从新到旧）。"""
    if db.get(Lesson, lid) is None:
        raise HTTPException(404, "课时不存在")
    rows = db.scalars(
        select(LessonSupplement).where(LessonSupplement.lesson_id == lid)
        .order_by(LessonSupplement.id.desc())
    ).all()
    out = [_out(s, db) for s in rows]
    return {"items": out, "pending": sum(1 for x in out if x["status"] == "todo")}


@router.post("/lessons/{lid}/supplements")
def create_supplement(lid: int, payload: SupplementIn, db: Session = Depends(get_db)):
    """记一批。**挑完就走**：items 之外什么都不填也能存。"""
    if db.get(Lesson, lid) is None:
        raise HTTPException(404, "课时不存在")
    seen: set[tuple[str, str]] = set()
    picked = []
    for it in payload.items:
        kind = (it.kind or "").strip()
        if kind not in KINDS:
            raise HTTPException(422, f"不认识的内容类型 {kind}（只能 question / note）")
        ref = (it.ref_id or "").strip()
        if not ref or (kind, ref) in seen:
            continue                     # 重复选的同一项只算一次
        seen.add((kind, ref))
        picked.append((kind, ref))
    if not picked:
        raise HTTPException(422, "还没挑任何东西")

    s = LessonSupplement(lesson_id=lid, focus=(payload.focus or "").strip()[:200],
                         note=(payload.note or "").strip(), status="todo", created_at=_now())
    db.add(s)
    db.flush()                           # 要它自己的 id 才能挂条目
    for i, (kind, ref) in enumerate(picked):
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
        db.add(LessonSupplementItem(supplement_id=s.id, kind=kind, ref_id=ref,
                                    title=title, sort_order=i, created_at=_now()))
    db.commit()
    db.refresh(s)
    return _out(s, db)


@router.patch("/supplements/{sid}")
def update_supplement(sid: int, payload: SupplementPatch, db: Session = Depends(get_db)):
    """改状态 / 知识点 / 备注。状态只由老师点，系统不推断。"""
    s = _supplement_or_404(db, sid)
    data = payload.model_dump(exclude_unset=True)
    if "status" in data:
        st = (data["status"] or "").strip()
        if st not in STATUS_CN:
            raise HTTPException(422, f"不认识的状态 {st}（只能 {' / '.join(STATUS_CN)}）")
        s.status = st
    if "focus" in data:
        s.focus = (data["focus"] or "").strip()[:200]
    if "note" in data:
        s.note = (data["note"] or "").strip()
    db.commit()
    return _out(s, db)


@router.delete("/supplements/{sid}")
def delete_supplement(sid: int, db: Session = Depends(get_db)):
    """删掉一批。**不删任何题目/笔记** —— 那两样是题库与笔记那边的资产。"""
    s = _supplement_or_404(db, sid)
    db.delete(s)
    db.commit()
    return {"ok": True}


@router.get("/students/{sid}/supplements/pending")
def pending_for_student(sid: int, db: Session = Depends(get_db)):
    """这个学生「还没给」的课后补充 —— 学生详情页那句提醒就靠它。

    打印发生在下一次课，中间隔几天：不给提醒就一定忘。
    """
    if db.get(Student, sid) is None:
        raise HTTPException(404, "学生不存在")
    rows = db.scalars(
        select(LessonSupplement).join(Lesson, Lesson.id == LessonSupplement.lesson_id)
        .where(Lesson.student_id == sid, LessonSupplement.status == "todo")
        .order_by(LessonSupplement.id.desc())
    ).all()
    items = [_out(s, db) for s in rows]
    lessons = {s.lesson_id: db.get(Lesson, s.lesson_id) for s in rows}
    for it in items:
        les = lessons.get(it["lesson_id"])
        it["lesson_date"] = storage.lesson_date(les) if les else ""
    return {"count": len(items), "items": items}


@router.post("/supplements/{sid}/print")
def print_supplement(sid: int, db: Session = Depends(get_db)):
    """出**学生版**（不含答案）：题目排版成 PDF，落进学生的归档目录，并把状态置成「已给」。

    为什么顺手写文件到归档目录：三个月后要能回答「我当时到底给了他哪份」。
    归档布局早就在了（写反馈时就在往里存），不需要新机制。

    笔记不在这一份里：笔记走它自己的导出（用户定的「两份纸」），
    所以按钮只出现在「含题目」的批次上（混着笔记也能用，只印题目那部分）。
    """
    s = _supplement_or_404(db, sid)
    lesson = db.get(Lesson, s.lesson_id)
    if lesson is None:
        raise HTTPException(404, "这节课已经不在了")

    qids = [it.ref_id for it in sorted(s.items, key=lambda x: (x.sort_order, x.id))
            if it.kind == "question" and db.get(Question, it.ref_id) is not None]
    if not qids:
        raise HTTPException(422, "这批里没有题目（只有笔记的话，去笔记页导出）")

    rows = db.scalars(select(Question).where(Question.id.in_(qids))).all()
    by_id = {q.id: q for q in rows}
    # 顺序按批次里挑的顺序，不按题库的 id 顺序
    items = [{"id": qid, "n": i + 1,
              "content": by_id[qid].content or "", "image": by_id[qid].image,
              "answer": by_id[qid].answer, "answer_image": by_id[qid].answer_image,
              "qtype": by_id[qid].qtype, "difficulty": by_id[qid].difficulty,
              "tags": [], "knowledge_points": []}
             for i, qid in enumerate(qids)]

    student = db.get(Student, lesson.student_id)
    title = f"{storage.lesson_date(lesson)} 课后补充"
    # ⚠️ show_answer=False：给学生的东西，答案**根本不写进文件**（不是隐藏）
    body = paper_export.build_pdf(title, items, {
        "show_answer": False, "show_analysis": False, "show_tags": False, "show_meta": True,
    })

    # 顺手记一次使用：题被发出去过，跟组卷同一个口径
    db.execute(update(Question).where(Question.id.in_(qids))
               .values(usage_count=func.coalesce(Question.usage_count, 0) + 1,
                       last_used_at=_now()))

    saved_rel = ""
    if student is not None:
        directory = storage.dir_for(student, lesson)
        fname = f"{storage.feedback_stem(student, lesson)}_课后补充_学生版.pdf"
        try:
            (directory / fname).write_bytes(body)
            saved_rel = f"{storage.dated_rel(student, lesson)}/{fname}"
        except OSError:
            saved_rel = ""            # 归档写不进去不能让打印失败（DB 才是正文，文件是副本）

    s.status = "given"
    db.commit()
    return Response(
        content=body, media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="supplement.pdf"; '
                f"filename*=UTF-8''{'%E8%AF%BE%E5%90%8E%E8%A1%A5%E5%85%85_%E5%AD%A6%E7%94%9F%E7%89%88.pdf'}"
            ),
            # 前端要能顺手读到这几个（不必再查一次）。
            # ⚠️ HTTP 头只能是 latin-1：归档路径里有中文（学生姓名），必须百分号编码，
            # 否则会在这里抛 UnicodeEncodeError —— 整个接口 500（踩过）。
            "X-Supplement-Status": s.status,
            "X-Supplement-Archived": quote(saved_rel, safe="/"),
        },
    )
