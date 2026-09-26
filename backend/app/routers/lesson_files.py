# -*- coding: utf-8 -*-
"""上课文件：上传 → 在本机抽文字 → 供 AI 润色当参考资料。

一条贯穿这个文件的纪律：**抽不出文字必须说清楚为什么**。
接的是纯文本接口，扫描版 PDF / 纯图片课件注定读不了；
这种情况静默跳过最糟 —— 老师会以为 AI 已经看过那份材料了。
所以每个文件都留 status + reason，失败原因原样交给界面显示。
"""
from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Lesson, LessonFile, Student
from ..services import file_text, storage

router = APIRouter(prefix="/api", tags=["lesson-files"])

MAX_BYTES = 30 * 1024 * 1024      # 30MB。课件/讲义足够，也避免把库撑爆


def _lesson_or_404(db: Session, lid: int) -> Lesson:
    ls = db.get(Lesson, lid)
    if ls is None:
        raise HTTPException(404, "课时记录不存在")
    return ls


def _out(f: LessonFile) -> dict:
    """给前端的形态。**不回传正文**（可能几万字），只给字数与开头预览。"""
    return {
        "id": f.id,
        "lesson_id": f.lesson_id,
        "name": f.name,
        "stored": f.stored,
        "kind": f.kind,
        "kind_cn": file_text.KIND_CN.get(f.kind, f.kind),
        "size_bytes": f.size_bytes,
        "pages": f.pages,
        "chars": f.chars,
        "truncated": bool(f.truncated),
        "status": f.status,
        "reason": f.reason or "",
        "preview": (f.text or "")[:160],
        "created_at": f.created_at,
    }


def collect_text(files: list[LessonFile], total_cap: int = 40000) -> tuple[str, dict]:
    """把若干文件的可读文字拼成给 AI 的一段，并回一份用于「会发出去什么」的清单。

    total_cap：总量上限。超了就按顺序收，后面的丢掉 —— 但**必须在清单里体现**，
    不能让用户以为全都发出去了。
    """
    parts: list[str] = []
    used = 0
    dropped: list[str] = []
    for f in files:
        if f.status != "ok" or not (f.text or "").strip():
            continue
        body = f.text.strip()
        head = f"### {f.name}"
        if f.pages:
            head += f"（{f.pages} 页）"
        block = head + "\n" + body
        if used + len(block) > total_cap:
            # 尽量收一部分，实在收不下就记下来
            room = total_cap - used - len(head) - 40
            if room < 200:
                dropped.append(f.name)
                continue
            block = head + "\n" + body[:room] + "\n（此文件因长度被截断）"
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts), {"chars": used, "dropped": dropped}


@router.get("/lessons/{lid}/files")
def list_files(lid: int, role: str = Query("material", description="material / homework"),
               db: Session = Depends(get_db)):
    """这节课的文件。**按 role 分开列**：写反馈时看的是「上课文件」（讲义/课件），
    作业那条要看的是学生交上来的作业原件 —— 两份清单互相看不见，
    否则老师会在反馈的文件列表里看到学生的作业照片。
    """
    ls = _lesson_or_404(db, lid)
    rows = db.scalars(
        select(LessonFile)
        .where(LessonFile.lesson_id == lid, LessonFile.role == role)
        .order_by(LessonFile.id)
    ).all()
    stu = db.get(Student, ls.student_id)
    return {
        "items": [_out(f) for f in rows],
        "accept": file_text.ACCEPT,
        "supported": file_text.SUPPORTED_NOTE,
        # 归档目录（相对 UPLOAD_DIR），界面上显示出来，老师才知道文件去哪儿找
        "rel_dir": storage.dated_rel(stu, ls) if stu else "",
    }


@router.post("/lessons/{lid}/files")
async def upload_file(lid: int, file: UploadFile = File(...),
                      role: str = Query("material", description="material / homework"),
                      db: Session = Depends(get_db)):
    """上传并**当场抽文字**。

    抽不出来也照样把行存下来（status=failed + reason）：这样界面能一直显示
    「这个文件没读进来、为什么」，而不是上传完就消失得无影无踪。

    role 决定它属于哪一份清单：上课材料（讲义/课件）还是学生作业原件。
    作业复用同一条管线（归档、抽文字、失败原因、删除清理全一样），只是标签不同。
    """
    if role not in ("material", "homework"):
        raise HTTPException(422, f"不认识的 role {role!r}（可选 material / homework）")
    _lesson_or_404(db, lid)
    name = Path(file.filename or "未命名").name        # 只取文件名，防路径穿越
    if file_text.kind_of(name) is None:
        raise HTTPException(422, f"暂不支持这种格式（{Path(name).suffix or '无扩展名'}）。"
                                 f"{file_text.SUPPORTED_NOTE}")

    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(422, f"文件太大了（超过 {MAX_BYTES // 1024 // 1024}MB）。"
                                 "课件一般不会这么大，确认一下选错文件没有？")

    ls = db.get(Lesson, lid)
    stu = db.get(Student, ls.student_id) if ls else None
    if stu is None:
        raise HTTPException(404, "这节课的学生不存在")
    # 归到「学生 / 上课日期」目录下（services/storage.py），文件名**保留原名**——
    # 这个目录是给人看的，叫「讲义.pdf」比叫 lf_9f3a…pdf 有用得多。重名自动加 _2。
    folder = storage.dir_for(stu, ls)
    stored_name = storage.unique_name(folder, storage.safe_token(Path(name).stem, "file") + Path(name).suffix.lower())
    dest = folder / stored_name
    dest.write_bytes(data)
    stored_rel = storage.rel(dest)

    f = LessonFile(
        owner_id=config.OWNER_ID, lesson_id=lid, name=name, stored=stored_rel,
        kind=file_text.kind_of(name) or "", size_bytes=len(data), role=role,
    )
    try:
        r = file_text.extract(dest, name)
        f.status, f.text, f.chars = "ok", r["text"], r["chars"]
        f.truncated = 1 if r["truncated"] else 0
        f.pages = r.get("pages")
    except file_text.ExtractError as e:
        # 文件留着（换解析器还能重试），但状态如实记为失败
        f.status, f.reason = "failed", str(e)
    db.add(f)
    db.commit()
    return _out(f)


@router.delete("/lesson-files/{fid}")
def delete_file(fid: int, db: Session = Depends(get_db)):
    f = db.get(LessonFile, fid)
    if f is None:
        raise HTTPException(404, "文件不存在")
    stored = f.stored
    db.delete(f)
    db.commit()
    # 只删这一行自己指向的那份原件 —— 不扫目录、不碰别人的文件
    storage.unlink_rels([stored])
    return {"ok": True}


@router.get("/lesson-files/{fid}/raw")
def download_file(fid: int, db: Session = Depends(get_db)):
    """取回原件。留一份原件本来就是为了「万一还想自己看看」。"""
    f = db.get(LessonFile, fid)
    if f is None or not f.stored:
        raise HTTPException(404, "文件不存在")
    p = storage.safe_join(f.stored)
    if p is None or not p.exists() or not p.is_file():
        raise HTTPException(404, "原件已不在磁盘上")
    media = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    from urllib.parse import quote
    return FileResponse(str(p), media_type=media, filename=f.name,
                        headers={"Content-Disposition":
                                 f"attachment; filename=file; filename*=UTF-8''{quote(f.name)}"})


@router.get("/students/{sid}/files")
def student_files(sid: int, db: Session = Depends(get_db)):
    """这个学生的资料清单 —— 按**上课日期**分组，含磁盘归档路径。

    存档目录本身是给人看的（services/storage.py 那套布局），这个接口是它的索引：
    做「学生情况分析」时要能一眼看到「这个孩子从 9 月到现在，每次课留下了什么材料」。
    """
    stu = db.get(Student, sid)
    if stu is None:
        raise HTTPException(404, "学生不存在")

    folder = storage.student_folder(stu)
    lessons = db.scalars(
        select(Lesson).where(Lesson.student_id == sid).order_by(Lesson.start_at.desc())
    ).all()
    ids = [ls.id for ls in lessons]
    files: dict[int, list] = {}
    pics: dict[int, list] = {}
    if ids:
        for f in db.scalars(select(LessonFile).where(LessonFile.lesson_id.in_(ids)).order_by(LessonFile.id)).all():
            files.setdefault(f.lesson_id, []).append({
                "type": "file", "id": f.id, "name": f.name,
                "kind_cn": file_text.KIND_CN.get(f.kind, f.kind),
                "size_bytes": f.size_bytes, "chars": f.chars,
                "status": f.status, "reason": f.reason or "",
                "url": f"/api/lesson-files/{f.id}/raw",
            })
        from ..models import Feedback
        for fb in db.scalars(select(Feedback).where(Feedback.lesson_id.in_(ids))).all():
            try:
                import json as _json
                urls = _json.loads(fb.images or "[]")
            except Exception:  # noqa: BLE001
                urls = []
            rels = [storage.url_to_rel(u) for u in urls]
            rels = [r for r in rels if r]
            if rels:
                pics[fb.lesson_id] = rels

    dates = []
    for ls in lessons:
        items = files.get(ls.id, [])
        for rel in pics.get(ls.id, []):
            p = storage.safe_join(rel)
            items.append({
                "type": "image", "name": Path(rel).name,
                "kind_cn": "反馈配图",
                "size_bytes": p.stat().st_size if p and p.exists() else 0,
                "url": f"/api/feedbacks/files/{rel}",
            })
        dates.append({
            "date": storage.lesson_date(ls),
            "lesson_id": ls.id,
            "start_at": ls.start_at,
            "topic": ls.topic or "",
            "rel_dir": storage.dated_rel(stu, ls),
            "items": items,
        })
    total = sum(i.get("size_bytes") or 0 for d in dates for i in d["items"])
    return {
        "student_id": sid,
        "student_name": stu.name,
        "folder": folder,
        "rel_dir": f"students/{folder}",
        # 绝对路径只是显示用（让老师知道去哪儿看）；接口不接受外部传入的路径
        "abs_dir": str(config.UPLOAD_DIR / "students" / folder),
        "total_bytes": total,
        "dates": dates,
    }


def cleanup_for_lessons(db: Session, lesson_ids: list[int]) -> int:
    """删课时/删学生**之前**把名下素材（上课文件 + 反馈配图）从磁盘收掉。

    为什么必须在删行之前：lesson_files 与 feedbacks 都会随外键 CASCADE 一起消失，
    删完就再也查不到磁盘上该删哪几个文件了。数据库管不了文件系统，这一步只能自己做。
    """
    return storage.unlink_rels(storage.collect_lesson_assets(db, lesson_ids))
