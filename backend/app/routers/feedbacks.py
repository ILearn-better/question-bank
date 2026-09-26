# -*- coding: utf-8 -*-
"""课后反馈与能力评分。

设计约束（开发文档 §6.5）：**所有字段可选，允许只写一句话就存。**
绝不能让"字段没填完"卡住记录 —— 一旦卡住，这个系统就会像所有失败的工具一样被弃用。
"""
from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..adapters import office
from ..db import get_db
from ..models import AbilityDim, AbilityScore, AiSetting, Feedback, FeedbackTemplate, Lesson, Student
from ..schemas import AiSettingIn, AiTestIn, DimIn, FeedbackIn, PolishIn, TemplateIn
from ..services import ai_polish, feedback_export, images

router = APIRouter(prefix="/api", tags=["feedbacks"])

# 反馈配图的 URL 形如 /api/feedbacks/files/fb_ab12cd34ef56.png
_IMG_RE = re.compile(r"/api/feedbacks/files/([A-Za-z0-9_.\-]+)")


def _lesson_or_404(db: Session, lid: int) -> Lesson:
    ls = db.get(Lesson, lid)
    if ls is None:
        raise HTTPException(404, "课时记录不存在")
    return ls


def _parse_images(raw: str | None) -> list[str]:
    try:
        v = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def _used_feedback_images(db: Session) -> set[str]:
    """所有反馈引用到的配图文件名（引用计数的依据）。"""
    used: set[str] = set()
    for (raw,) in db.execute(select(Feedback.images)).all():
        used |= set(_IMG_RE.findall(raw or ""))
    return used


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
        "images": _parse_images(fb.images),
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
    old_images = _IMG_RE.findall(fb.images or "")
    fb.performance = payload.performance
    fb.problems = payload.problems
    fb.homework = payload.homework
    fb.next_plan = payload.next_plan
    fb.rating = payload.rating
    fb.share_to_parent = payload.share_to_parent
    # 只留本项目图片目录里的名字，其余 URL 一律丢弃（防把外部地址/路径写进去）
    keep = [m.group(0) for m in (_IMG_RE.search(u) for u in payload.images) if m]
    fb.images = json.dumps(keep, ensure_ascii=False)
    db.flush()

    # 本次被移除的图片，如果已经没有其它反馈在引用，就从磁盘删掉（引用计数）。
    # 与「删题目清截图」「删笔记清配图」同一套路数：图不能被无限堆积。
    for name in set(old_images) - _used_feedback_images(db):
        try:
            (config.FEEDBACK_DIR / name).unlink()
        except OSError:
            pass

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

# ---------------------------------------------------------------- 反馈模板
def _template_out(t: FeedbackTemplate) -> dict:
    def _load(raw: str | None, fallback):
        try:
            v = json.loads(raw or "")
        except json.JSONDecodeError:
            return fallback
        return v if isinstance(v, type(fallback)) else fallback

    return {
        "id": t.id,
        "name": t.name,
        "phrases": _load(t.phrases, {}),
        "seeds": _load(t.seeds, {}),
        "is_builtin": bool(t.is_builtin),
        "sort_order": t.sort_order,
    }


@router.get("/feedback-templates")
def list_templates(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(FeedbackTemplate)
        .where(FeedbackTemplate.owner_id == config.OWNER_ID)
        .order_by(FeedbackTemplate.sort_order, FeedbackTemplate.id)
    ).all()
    return {"items": [_template_out(t) for t in rows]}


@router.post("/feedback-templates")
def create_template(payload: TemplateIn, db: Session = Depends(get_db)):
    """从当前反馈「另存为模板」也走这里 —— 写好一篇之后一键存成模板，是最自然的生产方式。"""
    t = FeedbackTemplate(
        owner_id=config.OWNER_ID,
        name=(payload.name or "").strip() or "未命名模板",
        phrases=json.dumps(payload.phrases or {}, ensure_ascii=False),
        seeds=json.dumps(payload.seeds or {}, ensure_ascii=False),
        is_builtin=0,
        sort_order=payload.sort_order or 100 + len(db.scalars(select(FeedbackTemplate)).all()),
    )
    db.add(t)
    db.commit()
    return _template_out(t)


@router.patch("/feedback-templates/{tid}")
def patch_template(tid: int, payload: TemplateIn, db: Session = Depends(get_db)):
    t = db.get(FeedbackTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    t.name = (payload.name or "").strip() or t.name
    t.phrases = json.dumps(payload.phrases or {}, ensure_ascii=False)
    t.seeds = json.dumps(payload.seeds or {}, ensure_ascii=False)
    if payload.sort_order:
        t.sort_order = payload.sort_order
    db.commit()
    return _template_out(t)


@router.delete("/feedback-templates/{tid}")
def delete_template(tid: int, db: Session = Depends(get_db)):
    t = db.get(FeedbackTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    if t.is_builtin:
        # 内置模板允许改文案，但删掉下次启动种子又会建回来，反而让人迷惑
        raise HTTPException(400, "内置模板不能删除；可以改它的文案，或另存一个新模板")
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 反馈配图
@router.post("/feedbacks/image")
async def upload_feedback_image(file: UploadFile = File(...)):
    """反馈配图上传。返回可直接写进 images 数组的 URL。

    存单独的 FEEDBACK_DIR，不复用 CROPS_DIR —— 那边删题目时的「孤儿截图清理」
    会把反馈的图当成无主文件删掉。
    """
    data = await file.read(images.MAX_IMAGE_BYTES + 1)
    try:
        saved = images.save_image(data, config.FEEDBACK_DIR, prefix="fb")
    except images.ImageRejected as e:
        raise HTTPException(422, str(e)) from e
    return {"url": f"/api/feedbacks/files/{saved['name']}", **saved}


@router.get("/feedbacks/files/{name}")
def get_feedback_image(name: str):
    """配图读取。只按文件名取（Path.name），防路径穿越。"""
    p = config.FEEDBACK_DIR / Path(name).name
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "图片不存在")
    media = mimetypes.guess_type(p.name)[0] or "image/png"
    return FileResponse(str(p), media_type=media)


# ---------------------------------------------------------------- 导出 txt / Word / PDF
EXPORT_FORMATS = {
    "txt": ("text/plain; charset=utf-8", "txt"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "pdf": ("application/pdf", "pdf"),
}


def _export_context(db: Session, ls: Lesson, fb: Feedback) -> dict:
    """导出用的上下文：学生名 / 上课时间 / 课程主题 / 评分。缺的项自动不出现。"""
    stu = db.get(Student, ls.student_id)
    dims = _dim_map(db)
    scores = db.scalars(select(AbilityScore).where(AbilityScore.lesson_id == ls.id)).all()
    when = (ls.start_at or "").replace("T", " ")
    return {
        "student_name": (stu.name if stu else "") or "",
        "when": when,
        "topic": ls.topic or "",
        "rating": fb.rating,
        "ability_scores": [
            {"dim_id": s.dim_id, "name": dims.get(s.dim_id, f"#{s.dim_id}"), "score": s.score}
            for s in scores
        ],
    }


@router.get("/lessons/{lid}/feedback/export")
def export_feedback(
    lid: int,
    format: str = Query("docx", description="txt / docx / pdf"),
    db: Session = Depends(get_db),
):
    ls = _lesson_or_404(db, lid)
    fb = db.scalar(select(Feedback).where(Feedback.lesson_id == lid))
    if fb is None:
        raise HTTPException(404, "这节课还没有反馈，先写点内容再导出")

    fmt = (format or "docx").lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(422, f"不支持的格式 {format}（可选 txt / docx / pdf）")

    data = _serialize(fb, [], _dim_map(db))
    ctx = _export_context(db, ls, fb)
    ctx["ability_scores"] = data["ability_scores"]

    try:
        if fmt == "txt":
            body = feedback_export.build_txt(data, ctx)
        elif fmt == "docx":
            body = feedback_export.build_docx(data, ctx)
        else:
            body = feedback_export.build_pdf(data, ctx)
    except feedback_export.PdfUnavailable as e:
        raise HTTPException(503, str(e)) from e

    media, ext = EXPORT_FORMATS[fmt]
    fname = feedback_export.safe_filename(ctx, ext)
    return Response(
        content=body,
        media_type=media,
        headers={
            # 中文文件名给 filename*（RFC 5987），同时留 ASCII 兜底
            "Content-Disposition": (
                f'attachment; filename="feedback.{ext}"; '
                f"filename*=UTF-8''{quote(fname)}"
            )
        },
    )


# ---------------------------------------------------------------- AI 润色
@router.get("/ai/providers")
def get_ai_providers():
    """服务商预设（地址 / 模型名 / 申请密钥的入口）。纯静态、无密钥。"""
    return ai_polish.providers()


@router.get("/ai/settings")
def get_ai_settings(db: Session = Depends(get_db)):
    return ai_polish.masked(ai_polish.get_config(db))


@router.put("/ai/settings")
def put_ai_settings(payload: AiSettingIn, db: Session = Depends(get_db)):
    """保存配置。api_key 传空串表示「不改」—— 前端拿到的是脱敏值，回填会把真 key 冲掉。"""
    return ai_polish.save_config(db, payload)


@router.post("/ai/test")
def test_ai(payload: Optional[AiTestIn] = None, db: Session = Depends(get_db)):
    """测试连接。可以带上还没保存的表单值 —— 填完就能试，不必先保存。"""
    try:
        return ai_polish.test_connection(db, payload.model_dump() if payload else None)
    except ai_polish.AiError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/lessons/{lid}/feedback/polish")
def polish_feedback(lid: int, payload: PolishIn, db: Session = Depends(get_db)):
    """AI 润色。

    ⚠️ 这里会把反馈正文发到第三方 AI 服务 —— 界面必须事先说清楚，且只能由老师主动触发。
    返回的是**建议稿**，前端应该让老师确认后再采用，绝不直接覆盖他写的内容。
    """
    ls = _lesson_or_404(db, lid)
    stu = db.get(Student, ls.student_id)
    ctx = {
        "学生": (stu.name if stu else "") or "",
        "上课时间": (ls.start_at or "").replace("T", " "),
        "本次内容": ls.topic or "",
    }
    try:
        return ai_polish.polish(db, payload.fields, ctx, payload.style)
    except ai_polish.AiError as e:
        raise HTTPException(502, str(e)) from e