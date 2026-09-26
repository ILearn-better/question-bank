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
from ..models import (
    AbilityDim,
    AbilityScore,
    AiSetting,
    Feedback,
    FeedbackDocTemplate,
    FeedbackTemplate,
    Lesson,
    LessonFile,
    Student,
)
from ..schemas import (
    AiSettingIn,
    AiTestIn,
    DimIn,
    DocTemplateIn,
    FeedbackIn,
    ImageRemoveIn,
    PolishIn,
    TemplateIn,
)
from ..services import ai_polish, feedback_export, images, storage
from . import lesson_files

router = APIRouter(prefix="/api", tags=["feedbacks"])

# 反馈配图的 URL 形如
# /api/feedbacks/files/students/u1_李芹旭/2026-09-26/fb_ab12cd34ef56.png
# 路径里带子目录，所以这里要允许斜杠与中文 —— **安全性由 storage.safe_join 兜**
# （拒绝 ..、解析后必须在 UPLOAD_DIR 之内），不靠正则把字符堵完。
_IMG_RE = re.compile(r"/api/feedbacks/files/(.+)")


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
    """所有反馈引用到的配图**相对路径**（引用计数的依据）。"""
    used: set[str] = set()
    for (raw,) in db.execute(select(Feedback.images)).all():
        for u in _parse_images(raw):
            rel = storage.url_to_rel(u)
            if rel:
                used.add(rel)
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
        # 整篇正文（AI 按模板整理的成品）。四段是原料，这个是可直接发家长的成品。
        "doc": fb.doc or "",
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
    """维度 id -> 名称。按 sort_order 取，导出里雷达图的维度顺序才能和界面一致。"""
    rows = db.scalars(select(AbilityDim).order_by(AbilityDim.sort_order, AbilityDim.id)).all()
    return {d.id: d.name for d in rows}


def _prev_scores(db: Session, ls: Lesson) -> dict[int, int]:
    """每个能力维度在**本节之前**最近一次的分数 —— 雷达图画「变化」要靠它。

    没有「上次」那条线，家长看到的是一张孤立的雷达图：看不出好坏，也看不出进步。
    """
    rows = db.execute(
        select(AbilityScore.dim_id, AbilityScore.score)
        .join(Lesson, Lesson.id == AbilityScore.lesson_id)
        .where(
            AbilityScore.student_id == ls.student_id,
            AbilityScore.lesson_id != ls.id,
            Lesson.start_at < ls.start_at,
            AbilityScore.score.isnot(None),
        )
        .order_by(Lesson.start_at.desc(), AbilityScore.id.desc())
    ).all()
    out: dict[int, int] = {}
    for dim_id, score in rows:      # 已按时间倒序：某维度第一次出现就是它最近的一次
        out.setdefault(dim_id, score)
    return out


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


def _write_feedback_txt(db: Session, ls: Lesson, fb: Feedback) -> dict:
    """把这条反馈同步成归档目录里的一份 txt（**对应学生的「上课日期」那个文件夹**）。

    为什么值得写：反馈正文只活在数据库里，而归档目录是「这个孩子这段时间留下了什么」的
    实物 —— 翻文件夹时就该看得到文字，不该只有讲义和照片。

    几个刻意的决定：
      · **复用导出那份 build_txt**（含能力评分、图片张数说明），不另写一套格式：
        同一个内容在「导出的 txt」和「文件夹里的 txt」长得不一样，只会让人怀疑哪个是准的
      · 每次保存**覆盖同一份**，不学配图那套 _2、_3：它是这条反馈的镜像，不是历史版本
      · 内容被清空了就把旧文件删掉，不留一个「说这节有反馈」的空壳
      · 写不进去也不让保存失败（正文已经进库了），但**如实回报**，前端会提醒老师

    返回 {ok, path, bytes} 或 {ok: False, reason}。
    """
    stu = db.get(Student, ls.student_id)
    if stu is None:
        return {"ok": False, "reason": "学生不存在"}
    rel = storage.feedback_txt_rel(stu, ls)
    p = storage.safe_join(rel)
    if p is None:
        return {"ok": False, "reason": "归档路径不合法"}

    scores = list(db.scalars(select(AbilityScore).where(AbilityScore.lesson_id == ls.id)).all())
    has_text = bool((fb.doc or "").strip()) or any(
        (getattr(fb, k) or "").strip()
        for k in ("performance", "problems", "homework", "next_plan")
    )
    if not has_text and not scores:
        # 内容被清空了：旧的快照要收掉，不能留个空壳让人以为这节写过反馈
        existed = p.is_file()
        storage.unlink_rels([rel])
        return {"ok": True, "path": rel, "removed": existed}

    data = _serialize(fb, scores, _dim_map(db))
    ctx = _export_context(db, ls, fb)
    ctx["ability_scores"] = data["ability_scores"]
    # 与导出一致：txt 里带「（上次 3）」这种参照，翻文件夹时也看得出变化
    ctx["ability_prev"] = _prev_scores(db, ls)
    try:
        body = feedback_export.build_txt(data, ctx, (fb.doc or "").strip() or None)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    except OSError as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}", "path": rel}
    return {"ok": True, "path": rel, "bytes": len(body)}


@router.put("/lessons/{lid}/feedback")
def upsert_feedback(lid: int, payload: FeedbackIn, db: Session = Depends(get_db)):
    """一节课一条反馈：不存在就建，存在就覆盖。前端不必关心是新增还是修改。"""
    ls = _lesson_or_404(db, lid)
    fb = db.scalar(select(Feedback).where(Feedback.lesson_id == lid))
    if fb is None:
        fb = Feedback(owner_id=config.OWNER_ID, lesson_id=lid, student_id=ls.student_id)
        db.add(fb)
    old_images = {rel for rel in (storage.url_to_rel(u) for u in _parse_images(fb.images)) if rel}
    fb.performance = payload.performance
    fb.problems = payload.problems
    fb.homework = payload.homework
    fb.next_plan = payload.next_plan
    fb.rating = payload.rating
    fb.share_to_parent = payload.share_to_parent
    # doc（整篇正文）只在**本次请求真的带了它**时才动：
    # 传空串 = 清空，不传 = 原样保留。少了这个区分，任何没带 doc 的调用
    # （旧前端、脚本）都会把用户辛苦整理出的整篇正文抹掉。
    if "doc" in payload.model_dump(exclude_unset=True):
        fb.doc = payload.doc or ""
    # 只留本项目目录内、且路径合法的图，**顺手把 URL 归一化**成服务端自己的写法；
    # 其余一律丢弃（防把外部地址或 `../` 写进来）。
    keep = []
    for u in payload.images:
        rel = storage.url_to_rel(u)
        if rel and storage.safe_join(rel) is not None:
            keep.append(f"/api/feedbacks/files/{rel}")
    fb.images = json.dumps(keep, ensure_ascii=False)
    db.flush()

    # 本次被移除的图片，如果已经没有其它反馈在引用，就从磁盘删掉（引用计数）。
    # 与「删题目清截图」「删笔记清配图」同一套路数：图不能被无限堆积。
    for rel in set(old_images) - _used_feedback_images(db):
        storage.unlink_rels([rel])

    # 能力评分整组替换：不用逐条 diff，语义更清楚，也不会留下上次多打的分数
    #
    # ⚠️ 这里必须看「有没有带这个字段」而不是「它是不是空的」——空列表是 falsy，
    #    老师把分数全部取消（界面传的就是 []）时会被当成「不动」而留着旧分，
    #    然后旧分会出现在导出的 txt / 雷达图 / 归档快照里，谁都看不出来。
    #    与上面 doc 的处理保持一致：带了字段就按它替换，没带（旧前端、脚本）才原样保留。
    if "ability_scores" in payload.model_dump(exclude_unset=True):
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
    # 归档目录里同步一份 txt 快照（学生 / 上课日期 那一格），让人翻文件夹时就看得见文字。
    # 放在 commit 之后：DB 是正文，txt 是副本 —— 副本写不进去不能让保存失败。
    txt = _write_feedback_txt(db, ls, fb)
    scores = db.scalars(select(AbilityScore).where(AbilityScore.lesson_id == lid)).all()
    out = _serialize(fb, list(scores), _dim_map(db))
    out["archive_txt"] = txt
    return out


@router.delete("/lessons/{lid}/feedback")
def delete_feedback(lid: int, db: Session = Depends(get_db)):
    # 用 db.get 而不是 or_404：删反馈保持幂等（课不存在也返回 ok），不改变原有语义
    ls = db.get(Lesson, lid)
    stu = db.get(Student, ls.student_id) if ls is not None else None
    db.execute(delete(AbilityScore).where(AbilityScore.lesson_id == lid))
    db.execute(delete(Feedback).where(Feedback.lesson_id == lid))
    db.commit()
    # 归档里那份 txt 是这条反馈的**镜像**，反馈没了就不该留着 ——
    # 否则文件在说「这节有反馈」，而系统里查不到，反而误导
    if ls is not None and stu is not None:
        storage.unlink_rels([storage.feedback_txt_rel(stu, ls)])
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


# ---------------------------------------------------------------- 润色模板（整篇）
def _doc_template_out(t: FeedbackDocTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "content": t.content or "",
        "is_builtin": bool(t.is_builtin),
        "sort_order": t.sort_order,
    }


@router.get("/feedback-doc-templates")
def list_doc_templates(db: Session = Depends(get_db)):
    """润色时「仿照的模板」列表。和按字段分的 feedback-templates 是两回事。"""
    rows = db.scalars(
        select(FeedbackDocTemplate)
        .where(FeedbackDocTemplate.owner_id == config.OWNER_ID)
        .order_by(FeedbackDocTemplate.sort_order, FeedbackDocTemplate.id)
    ).all()
    return {"items": [_doc_template_out(t) for t in rows]}


@router.post("/feedback-doc-templates")
def create_doc_template(payload: DocTemplateIn, db: Session = Depends(get_db)):
    """把当前调好的模板内容另存为一条新模板。

    「拿一篇满意的成品当模板」是最自然的生产方式，所以允许从弹窗里直接存。
    """
    t = FeedbackDocTemplate(
        owner_id=config.OWNER_ID,
        name=(payload.name or "").strip() or "未命名模板",
        content=payload.content or "",
        is_builtin=0,
        sort_order=payload.sort_order or 100 + len(db.scalars(select(FeedbackDocTemplate)).all()),
    )
    db.add(t)
    db.commit()
    return _doc_template_out(t)


@router.patch("/feedback-doc-templates/{tid}")
def patch_doc_template(tid: int, payload: DocTemplateIn, db: Session = Depends(get_db)):
    t = db.get(FeedbackDocTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    t.name = (payload.name or "").strip() or t.name
    if payload.content is not None:
        t.content = payload.content
    if payload.sort_order:
        t.sort_order = payload.sort_order
    db.commit()
    return _doc_template_out(t)


@router.delete("/feedback-doc-templates/{tid}")
def delete_doc_template(tid: int, db: Session = Depends(get_db)):
    t = db.get(FeedbackDocTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    if t.is_builtin:
        raise HTTPException(400, "内置模板不能删除；可以改它的内容，或另存一个新模板")
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 反馈配图
@router.post("/feedbacks/image")
async def upload_feedback_image(file: UploadFile = File(...), lesson_id: int = Query(...),
                               db: Session = Depends(get_db)):
    """反馈配图上传。返回可直接写进 images 数组的 URL。

    **必须带 lesson_id**：图片要归到「学生 / 上课日期」目录下（storage.py 那套布局），
    没有课时就不知道往哪个学生的哪一天放。
    存的地方不复用 CROPS_DIR —— 那边删题目时的「孤儿截图清理」会把反馈的图当成无主文件删掉。
    """
    ls = _lesson_or_404(db, lesson_id)
    stu = db.get(Student, ls.student_id)
    if stu is None:
        raise HTTPException(404, "这节课的学生不存在")
    data = await file.read(images.MAX_IMAGE_BYTES + 1)
    # 图片名用「学生_日期」（同一天多张自动 _2、_3）。
    # 图片是要被转发出去的东西：粘在微信里、存到相册里，脱离了这个目录之后
    # 还得能自证是谁的、哪天的 —— 叫 fb_ab12cd34.png 就完全认不出来了。
    stem = storage.feedback_stem(stu, ls)
    try:
        saved = images.save_image(data, storage.dir_for(stu, ls), prefix="fb", stem=stem)
    except images.ImageRejected as e:
        raise HTTPException(422, str(e)) from e
    rel = storage.rel(storage.dir_for(stu, ls) / saved["name"])
    return {"url": f"/api/feedbacks/files/{rel}", "path": rel, **saved}


@router.post("/feedbacks/image/remove")
def remove_feedback_image(payload: ImageRemoveIn, db: Session = Depends(get_db)):
    """真删掉一张配图。界面上的「×」和「贴完又取消弹窗」都会调它。

    为什么必须有这个接口：配图是「上传即落盘、保存反馈才登记」的 —— 老师贴完图又不想用了，
    只在界面上把它从列表里拿掉是**没用的**，文件会一直躺在硬盘上：既不在资料清单里
    （清单读的是数据库），也不会被任何清理碰到（清理只删数据库引用得到的文件）。
    「能贴就得能删」，所以这里真的去删文件。

    删之前的三道闸：
      · 只认本项目自己的图片 URL（storage.url_to_rel），且要过 safe_join（拒 .. 与越界）
      · 路径必须落在**这节课的学生**目录里 —— 不能拿别人的 URL 把别人的文件删了
      · **还有别的已保存反馈在引用就绝不删**：界面上点「×」只是「这次不想带它」，
        而已保存的反馈是数据，数据优先（那种情况留到保存时按引用计数清，见 upsert_feedback）
    """
    ls = _lesson_or_404(db, payload.lesson_id)
    stu = db.get(Student, ls.student_id)
    if stu is None:
        raise HTTPException(404, "这节课的学生不存在")

    rel = storage.url_to_rel(payload.url or "")
    if not rel or storage.safe_join(rel) is None:
        raise HTTPException(422, "这不是本项目的图片地址")
    # 用「u<id>_」前缀而不是完整目录名：学生改名后旧目录名对不上，但 id 永远对得上
    if not rel.startswith(f"students/u{stu.id}_"):
        raise HTTPException(422, "这张图不属于这节课的学生")
    if rel in _used_feedback_images(db):
        return {"deleted": False, "reason": "referenced"}
    return {"deleted": bool(storage.unlink_rels([rel]))}


# 这里曾经有一个 _fetch_image_bytes() + POST /feedbacks/image-from-url：让服务端顺着
# 剪贴板里的图片链接去把图下载回来（从网页复制图片时，剪贴板里往往只有一个 <img src="https://…">）。
# **已删除**，理由：那是本服务唯一会主动访问外网的地方，而本项目的承诺是「数据存在本机、
# 页面零外网请求」。为一种小概率用法去撬动整体定位不划算 —— 何况「贴缓存图」真正要的
# （截图、从微信复制的图）本来就不需要联网。要存网页上的图，右键另存到本地再 Ctrl+V 即可，
# 行为一样而且看得见。


@router.get("/feedbacks/files/{rel_path:path}")
def get_feedback_image(rel_path: str):
    """配图读取。路径由 storage.safe_join 校验（拒 .. / 越界），防路径穿越。"""
    p = storage.safe_join(rel_path)
    if p is None or not p.exists() or not p.is_file():
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
    source: str = Query("auto", description="auto / doc / fields"),
    db: Session = Depends(get_db),
):
    """导出。source 决定导哪一份内容：

      · auto（默认）—— 有整篇正文就导整篇（老师确认过的成品），否则退回四段。
      · doc         —— 强制整篇；没有就报错，而不是默默导成四段（那会让人以为导错了）。
      · fields      —— 强制四段。
    """
    ls = _lesson_or_404(db, lid)
    fb = db.scalar(select(Feedback).where(Feedback.lesson_id == lid))
    if fb is None:
        raise HTTPException(404, "这节课还没有反馈，先写点内容再导出")

    fmt = (format or "docx").lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(422, f"不支持的格式 {format}（可选 txt / docx / pdf）")

    src = (source or "auto").lower()
    if src not in ("auto", "doc", "fields"):
        raise HTTPException(422, f"不支持的 source {source}（可选 auto / doc / fields）")
    whole = (fb.doc or "").strip()
    if src == "doc" and not whole:
        raise HTTPException(422, "这条反馈还没有整篇正文，先点「AI 润色」生成一篇")
    use_doc = bool(whole) and src != "fields"

    # ⚠️ 这里曾经传的是空列表（_serialize(fb, [], ...)），接着又把 ctx 里算好的分数
    #    覆盖成空 —— 结果「能力评分」在**所有**导出里静默消失，界面上却看不出来。
    #    能力分必须走真实查出来的这一份。
    dims_map = _dim_map(db)
    scores = list(db.scalars(select(AbilityScore).where(AbilityScore.lesson_id == lid)).all())
    data = _serialize(fb, scores, dims_map)
    ctx = _export_context(db, ls, fb)
    ctx["ability_scores"] = data["ability_scores"]
    # 雷达图要「本次 vs 上次」才看得懂，上次的分数存在别的课时里
    ctx["ability_prev"] = _prev_scores(db, ls)
    ctx["ability_radar"] = [
        {"name": s["name"], "latest": s["score"],
         "previous": ctx["ability_prev"].get(s["dim_id"])}
        for s in data["ability_scores"]
    ]
    doc_arg = data["doc"] if use_doc else None

    try:
        if fmt == "txt":
            body = feedback_export.build_txt(data, ctx, doc_arg)
        elif fmt == "docx":
            body = feedback_export.build_docx(data, ctx, doc_arg)
        else:
            body = feedback_export.build_pdf(data, ctx, doc_arg)
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
    """AI 整篇润色。

    逻辑：把四段记录 + 课程信息拼成一篇「原始记录」，再让 AI 照着老师选的模板
    整理成一篇正式的反馈文档，**整篇**返回。

    ⚠️ 这里会把反馈正文发到第三方 AI 服务 —— 界面必须事先说清楚，且只能由老师主动触发。
    返回的是**建议稿**，前端必须让老师过目、可编辑、确认后才采用，绝不直接覆盖他写的内容。
    """
    ls = _lesson_or_404(db, lid)
    stu = db.get(Student, ls.student_id)
    ctx = {
        "学生": (stu.name if stu else "") or "",
        "上课时间": (ls.start_at or "").replace("T", " "),
        "本次内容": ls.topic or "",
    }
    # 模板内容：弹窗里改过的优先；否则按 id 取库里的
    template_content = payload.template_content
    template_name = ""
    if not (template_content or "").strip() and payload.template_id:
        t = db.get(FeedbackDocTemplate, payload.template_id)
        if t is None:
            raise HTTPException(404, "模板不存在")
        template_content = t.content or ""
        template_name = t.name

    draft = ai_polish.assemble_draft(payload.fields, ctx, payload.draft)

    # 上课文件当参考资料。**只取抽出了文字的那些**，并在返回值里如实回报名单 ——
    # 老师必须知道 AI 到底看到了哪些材料（没抽出来的那些更是要显眼地说）。
    materials, mat_meta = "", {"files": [], "chars": 0, "dropped": []}
    if payload.use_files:
        stmt = select(LessonFile).where(LessonFile.lesson_id == lid)
        if payload.file_ids:
            stmt = stmt.where(LessonFile.id.in_(payload.file_ids))
        rows = list(db.scalars(stmt.order_by(LessonFile.id)).all())
        materials, mat_meta = lesson_files.collect_text(rows)
        mat_meta["files"] = [
            {"id": f.id, "name": f.name, "status": f.status, "chars": f.chars,
             "reason": f.reason or "", "truncated": bool(f.truncated)}
            for f in rows
        ]

    try:
        result = ai_polish.polish_document(db, draft, template_content, payload.style, materials)
    except ai_polish.AiError as e:
        raise HTTPException(502, str(e)) from e
    result["draft"] = draft          # 回传一份「到底发了什么」，方便界面如实展示
    result["template_name"] = template_name
    result["materials"] = mat_meta
    return result