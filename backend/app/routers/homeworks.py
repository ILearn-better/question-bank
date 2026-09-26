# -*- coding: utf-8 -*-
"""学生作业：交作业的记录、作业维度、老师打分。

设计要点（细节见 docs/学生作业上传与评分方案.md，那几件事是用户定过的）：

  · **独立于反馈**：作业能单独存在。老师可能只记「没交」，也可能先收了作业照片、
    隔天再写反馈、再补打分 —— 挂在 feedbacks 上会把两件事绑死。
  · **粒度一节一条**（lesson_id 唯一）：跟反馈一致，老师的心智模型是「这次课的作业」。
  · **作业维度与课堂维度分开**（用户明确的「分开」）：这套维度独立于 ability_dims。
  · **未交不打分**：missing 只记状态。用 0 分记会把平均值与雷达图一起带偏，
    看起来像退步，而事实可能只是那天没写。
  · **评价不以文件为前提**（用户原话「有文件也可上传，无则不传」）：
    上传是可选的，界面上不许把它做成必填或强提示。
  · 作业原件复用 `lesson_files`（role='homework'）那套上传/抽文字/归档/清理。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import (
    Homework,
    HomeworkDim,
    HomeworkScore,
    Lesson,
    LessonFile,
    Student,
)
from ..schemas import DimIn, HomeworkIn
from ..services import storage
from . import lesson_files

router = APIRouter(prefix="/api", tags=["homework"])

STATUS_LABEL = {"submitted": "已交", "late": "迟交", "missing": "未交"}
STATUS_OK = set(STATUS_LABEL)


def _lesson_or_404(db: Session, lid: int) -> Lesson:
    ls = db.get(Lesson, lid)
    if ls is None:
        raise HTTPException(404, "课时记录不存在")
    return ls


def _homework_or_none(db: Session, lid: int) -> Homework | None:
    return db.scalar(select(Homework).where(Homework.lesson_id == lid))


def _dim_map(db: Session) -> dict[int, str]:
    rows = db.scalars(
        select(HomeworkDim).order_by(HomeworkDim.sort_order, HomeworkDim.id)
    ).all()
    return {d.id: d.name for d in rows}


def _score_rows(db: Session, homework_id: int) -> list[HomeworkScore]:
    return list(
        db.scalars(
            select(HomeworkScore)
            .where(HomeworkScore.homework_id == homework_id)
            .order_by(HomeworkScore.id)
        ).all()
    )


def _prev_scores(db: Session, ls: Lesson) -> dict[int, int]:
    """各维度在**这节课之前**最近一次的作业分数 —— 和课堂雷达同一套用意：
    没有「上次」这条线，一张孤立的雷达图家长看不出好还是不好。"""
    rows = db.execute(
        select(HomeworkScore.dim_id, HomeworkScore.score)
        .join(Homework, Homework.id == HomeworkScore.homework_id)
        .join(Lesson, Lesson.id == Homework.lesson_id)
        .where(
            HomeworkScore.student_id == ls.student_id,
            Homework.lesson_id != ls.id,
            Lesson.start_at < ls.start_at,
        )
        .order_by(Lesson.start_at.desc(), HomeworkScore.id.desc())
    ).all()
    out: dict[int, int] = {}
    for dim_id, score in rows:      # 已按时间倒序：某维度第一次出现就是它最近的一次
        out.setdefault(dim_id, score)
    return out


def _serialize(db: Session, hw: Homework) -> dict:
    dims = _dim_map(db)
    return {
        "id": hw.id,
        "lesson_id": hw.lesson_id,
        "student_id": hw.student_id,
        "status": hw.status,
        "status_label": STATUS_LABEL.get(hw.status, hw.status),
        "note": hw.note or "",
        "scores": [
            {"dim_id": s.dim_id, "name": dims.get(s.dim_id, f"#{s.dim_id}"), "score": s.score}
            for s in _score_rows(db, hw.id)
        ],
        "created_at": hw.created_at,
        "updated_at": hw.updated_at,
    }


# ---------------------------------------------------------------- 作业维度
@router.get("/homework-dims")
def list_dims(include_inactive: bool = False, db: Session = Depends(get_db)):
    stmt = select(HomeworkDim).where(HomeworkDim.owner_id == config.OWNER_ID)
    if not include_inactive:
        stmt = stmt.where(HomeworkDim.active == 1)
    rows = db.scalars(stmt.order_by(HomeworkDim.sort_order, HomeworkDim.id)).all()
    return [
        {"id": d.id, "name": d.name, "curriculum_id": d.curriculum_id,
         "sort_order": d.sort_order, "active": d.active}
        for d in rows
    ]


@router.post("/homework-dims")
def create_dim(payload: DimIn, db: Session = Depends(get_db)):
    d = HomeworkDim(owner_id=config.OWNER_ID, **payload.model_dump())
    db.add(d)
    db.commit()
    return {"id": d.id}


@router.patch("/homework-dims/{dim_id}")
def patch_dim(dim_id: int, payload: DimIn, db: Session = Depends(get_db)):
    d = db.get(HomeworkDim, dim_id)
    if d is None:
        raise HTTPException(404, "维度不存在")
    d.name = payload.name
    d.curriculum_id = payload.curriculum_id
    d.sort_order = payload.sort_order
    db.commit()
    return {"ok": True}


@router.delete("/homework-dims/{dim_id}")
def delete_dim(dim_id: int, db: Session = Depends(get_db)):
    """停用而不是删除 —— 历史作业评分必须保留，否则趋势就断了（与能力维度同一道理）。"""
    d = db.get(HomeworkDim, dim_id)
    if d is None:
        raise HTTPException(404, "维度不存在")
    d.active = 0
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 作业记录
@router.get("/lessons/{lid}/homework")
def get_homework(lid: int, db: Session = Depends(get_db)):
    """这节课的作业记录 + 这次作业的原件清单。

    没有记录时返回 None（界面据此显示「还没记」，而不是「未交」——
    这两件事完全不同：没记 = 不知道，未交 = 明确没交）。
    """
    ls = _lesson_or_404(db, lid)
    stu = db.get(Student, ls.student_id)
    hw = _homework_or_none(db, lid)
    files = list(
        db.scalars(
            select(LessonFile)
            .where(LessonFile.lesson_id == lid, LessonFile.role == "homework")
            .order_by(LessonFile.id)
        ).all()
    )
    return {
        "homework": _serialize(db, hw) if hw else None,
        "files": [lesson_files._out(f) for f in files],
        "accept": lesson_files.file_text.ACCEPT,
        "supported": lesson_files.file_text.SUPPORTED_NOTE,
        "rel_dir": storage.dated_rel(stu, ls) if stu else "",
        "status_options": [{"value": k, "label": v} for k, v in STATUS_LABEL.items()],
        # 打分参照：各维度上次的作业分（界面显示「上次 3」，和课堂评分同一用意）
        "previous": _prev_scores(db, ls) if hw else {},
    }


@router.put("/lessons/{lid}/homework")
def upsert_homework(lid: int, payload: HomeworkIn, db: Session = Depends(get_db)):
    """记/改这次的作业。一节课一条，不存在就建。

    关于 status：只接受 submitted / late / missing。
    「没布置 / 不用记」的语义是**删掉这条记录**（DELETE），不是存一个空状态 ——
    否则「空状态」要不要算进平均、要不要显示，会变成永远吵不清的问题。
    """
    ls = _lesson_or_404(db, lid)
    status = (payload.status or "").strip()
    if status not in STATUS_OK:
        raise HTTPException(422, f"不认识的状态 {payload.status!r}"
                                 f"（可选：{'、'.join(STATUS_OK)}；不用记就删掉这条作业）")

    hw = _homework_or_none(db, lid)
    if hw is None:
        hw = Homework(owner_id=config.OWNER_ID, student_id=ls.student_id, lesson_id=lid)
        db.add(hw)
    hw.status = status
    hw.note = payload.note or ""
    db.flush()

    # 与 feedbacks 的 ability_scores 同一路数：**带了字段就整组替换**（空列表 = 清除），
    # 没带才保留。用 `if payload.scores:` 会让「把分全部取消」变成「不动」，旧分留在库里，
    # 然后静默出现在雷达图与文件夹里的 txt 快照里。
    #
    # 未交（missing）单独一条路：**不写分、并把旧分清掉**。
    # ⚠️ 这里不能写成「先按上面那条 add、再 delete」—— 试过，结果是「未交 + 4 分」并存：
    #    批量 delete 用的是 SQL、而刚 add 的对象还在 session 里，两者的先后不如直觉可靠。
    #    干脆在这一支里根本不 add，就没有顺序问题。
    if status == "missing":
        db.execute(delete(HomeworkScore).where(HomeworkScore.homework_id == hw.id))
    elif "scores" in payload.model_dump(exclude_unset=True):
        db.execute(delete(HomeworkScore).where(HomeworkScore.homework_id == hw.id))
        for item in payload.scores:
            db.add(
                HomeworkScore(
                    owner_id=config.OWNER_ID,
                    homework_id=hw.id,
                    student_id=ls.student_id,
                    dim_id=item.dim_id,
                    score=item.score,
                )
            )

    db.commit()
    out = _serialize(db, hw)
    # 归档目录里同步一份 txt（与反馈同一用意：翻文件夹时看得见文字，不只是照片）
    out["archive_txt"] = _write_homework_txt(db, ls, hw)
    return out


@router.delete("/lessons/{lid}/homework")
def delete_homework(lid: int, db: Session = Depends(get_db)):
    """整块撤掉（「这节没布置作业 / 不用记」）。

    顺手做的事：删掉评分行、删掉归档里那份 txt 快照。
    **作业原件不删** —— 那是老师上传的实物，撤掉一条评价不该连带把学生的作业删了。
    要删原件请用 lesson-files 那个接口（界面上每条文件也有 × ）。
    """
    ls = db.get(Lesson, lid)
    hw = _homework_or_none(db, lid) if ls is not None else None
    if hw is not None:
        db.execute(delete(HomeworkScore).where(HomeworkScore.homework_id == hw.id))
        db.execute(delete(Homework).where(Homework.id == hw.id))
        db.commit()
    stu = db.get(Student, ls.student_id) if ls is not None else None
    if ls is not None and stu is not None:
        storage.unlink_rels([storage.homework_txt_rel(stu, ls)])
    return {"ok": True}


# ---------------------------------------------------------------- 学生作业雷达
@router.get("/students/{sid}/homework-radar")
def homework_radar(sid: int, db: Session = Depends(get_db)):
    """最新一次 vs 上一次的**作业**雷达（按维度各自回溯，与课堂雷达同一算法）。

    只看打过分的记录：missing（未交）没有分数，自然不参与 ——
    这正是「未交不打分」这条设计在数据上的体现。
    """
    stu = db.get(Student, sid)
    if stu is None:
        raise HTTPException(404, "学生不存在")
    dims = list(
        db.scalars(
            select(HomeworkDim)
            .where(HomeworkDim.owner_id == config.OWNER_ID, HomeworkDim.active == 1)
            .order_by(HomeworkDim.sort_order, HomeworkDim.id)
        ).all()
    )
    scores = list(
        db.scalars(
            select(HomeworkScore)
            .where(HomeworkScore.student_id == sid)
            .order_by(HomeworkScore.created_at, HomeworkScore.id)
        ).all()
    )
    history: dict[int, list[int]] = {}
    for s in scores:
        history.setdefault(s.dim_id, []).append(s.score)
    rows = []
    for d in dims:
        seq = history.get(d.id, [])
        rows.append({
            "dim_id": d.id,
            "name": d.name,
            "latest": seq[-1] if seq else None,
            "previous": seq[-2] if len(seq) >= 2 else None,
            "count": len(seq),
        })
    # 顺手给「交了没交」的概览：家长沟通里最先要说的就是这个
    hws = list(db.scalars(select(Homework).where(Homework.student_id == sid)).all())
    counts = {k: 0 for k in STATUS_LABEL}
    for h in hws:
        counts[h.status] = counts.get(h.status, 0) + 1
    return {
        "dims": rows,
        "has_data": any(r["latest"] is not None for r in rows),
        "status_counts": counts,
        "total": len(hws),
    }


# ---------------------------------------------------------------- txt 快照
def _build_homework_txt(hw: Homework, student_name: str, ls: Lesson,
                        dims: dict[int, str], scores: list[HomeworkScore],
                        prev: dict[int, int]) -> str:
    """作业情况的一份纯文本。**不复制导出模块那套排版** —— 作业没有家长稿那种格式，
    这里只要「翻文件夹时看得懂」：谁、哪天、交没交、打了什么分、老师写了什么。"""
    lines = ["作业情况", "=" * 24]
    meta = f"学生：{student_name}　　时间：{(ls.start_at or '').replace('T', ' ')}"
    if ls.topic:
        meta += f"　　内容：{ls.topic}"
    lines += [meta, "", f"【状态】{STATUS_LABEL.get(hw.status, hw.status)}"]
    if scores:
        lines += ["", "【作业评分】"]
        for s in scores:
            name = dims.get(s.dim_id, f"#{s.dim_id}")
            back = prev.get(s.dim_id)
            lines.append(f"· {name}：{s.score} / 5" + (f"（上次 {back}）" if back else ""))
    if (hw.note or "").strip():
        lines += ["", "【批改备注】", hw.note.strip()]
    return "\n".join(lines).rstrip() + "\n"


def _write_homework_txt(db: Session, ls: Lesson, hw: Homework) -> dict:
    """把作业情况同步成归档目录里的一份 txt（学生 / 上课日期 那一格）。

    与反馈 txt 快照同一套路数：**覆盖**同一份（不是 _2、_3 越存越多）、
    写失败如实回报但不影响保存、`utf-8-sig` 让记事本/微信不乱码。
    """
    stu = db.get(Student, ls.student_id)
    if stu is None:
        return {"ok": False, "reason": "学生不存在"}
    rel = storage.homework_txt_rel(stu, ls)
    p = storage.safe_join(rel)
    if p is None:
        return {"ok": False, "reason": "归档路径不合法"}
    try:
        txt = _build_homework_txt(
            hw, stu.name or "", ls, _dim_map(db), _score_rows(db, hw.id), _prev_scores(db, ls)
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig：带 BOM，Windows 记事本/Excel 打开中文才不乱码（与导出的 txt 一致）
        p.write_bytes(txt.encode("utf-8-sig"))
    except OSError as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}", "path": rel}
    return {"ok": True, "path": rel, "bytes": len(txt.encode("utf-8-sig"))}
