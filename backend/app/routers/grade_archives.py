# -*- coding: utf-8 -*-
"""批改文件留存 —— 把一次批改的结果存进「学生资料」，以后能翻、能看、能下载。

目标流程（用户 2026-10-20）：

    上交作业 → 批改作业 → 记录归档到学生资料的「改作业」→ 点开看批改情况

## 两条入口，一个出口

    · `POST /grade-archives/generate` —— 批改页上点「导出到作业存档」。
      **后端自己出 PDF**（复用 `grade_export`），前端只发一个 `{grade_id}`。
      ⚠️ 刻意不做「前端先下载再上传」：一份 14 页的卷子 PDF 有 20MB 上下，
         在浏览器里过一遍要几秒、还会多占一份临时空间，而服务端本来就有渲染能力。
    · `POST /grade-archives/upload` —— 老师手传（线下批好的卷子拍照、别处拿到的答案）。
      走同一条落盘路径与同一套校验，所以两种来源在列表里长得一样。

两条都落到 `services/archive_store`，**不各写一套存文件逻辑** ——
两套的差别迟早会出现在「同名避重只在其中一条生效」这种地方。

## 权限

自用单机形态没有登录体系（`config.OWNER_ID` 固定 1），但仍做两道显式校验：
  · 每条记录带 `owner_id`，读删都按它过滤（将来接登录只改这一处）；
  · 上传/生成时校验**学生存在**，`grade_id` 若给了还必须属于同一个学生 ——
    否则一条 `{grade_id: 别人的}` 就能把 A 的批改挂到 B 的资料里。
"""
from __future__ import annotations

import mimetypes
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import HomeworkGrade
from ..schemas import ArchiveGenerateIn
from ..services import archive_store as store

router = APIRouter(prefix="/api", tags=["grade-archives"])


def _fail(e: store.ArchiveError) -> HTTPException:
    return HTTPException(e.status, e.message)


def _grade_of_student(db: Session, gid: int | None, sid: int) -> HomeworkGrade | None:
    """取出批改并确认它属于这个学生。

    ⚠️ 不在这里静默忽略不匹配的 grade_id：那会让「关联错了作业」变成一个
       看不出来的数据错误（存档挂着一份别人的卷子）。
    """
    if not gid:
        return None
    g = db.get(HomeworkGrade, int(gid))
    if g is None:
        raise HTTPException(404, "这条批改记录不在了")
    if g.student_id != sid:
        raise HTTPException(422, "这份批改不是这个学生的，不能归档到他的资料里")
    return g


# ---------------------------------------------------------------- 写
@router.post("/grade-archives/generate")
def generate_archive(payload: ArchiveGenerateIn, db: Session = Depends(get_db)):
    """把这份批改导出成带笔迹的 PDF，存进 `students/<学生>/<日期>/批改/`。

    返回的就是新建的记录（含 `rel_path`、`url`、`can_preview`）—— 前端不用再查一次。
    """
    try:
        rec = store.generate_from_grade(
            db, payload.grade_id,
            on=payload.archived_on, grader=payload.grader, note=payload.note,
        )
        return store.detail(db, rec.id)
    except store.ArchiveError as e:
        raise _fail(e) from e


@router.post("/grade-archives/upload")
async def upload_archive(
    file: UploadFile = File(...),
    student_id: int = Form(...),
    archived_on: str | None = Form(None),
    grade_id: int | None = Form(None),
    grader: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """老师手传一份文件进某个学生的批改存档。

    校验（类型白名单 / 大小 / 空文件）全在 `archive_store.validate` 里，
    这里只负责把「学生」和「批改」两个关联查对。
    """
    try:
        stu = store.student_or_404(db, student_id)
        grade = _grade_of_student(db, grade_id, student_id)
        # 多读 1 字节就能判出「超了」而不必把整个大文件收进内存
        data = await file.read(store.MAX_BYTES + 1)
        rec = store.create(
            db, student=stu, filename=file.filename or "作业存档",
            data=data, on=archived_on, grade=grade,
            source="upload", grader=grader, note=note,
        )
        return store.detail(db, rec.id)
    except store.ArchiveError as e:
        raise _fail(e) from e


# ---------------------------------------------------------------- 读
@router.get("/students/{sid}/grade-archives")
def list_archives(sid: int, limit: int = Query(200, ge=1, le=1000),
                  db: Session = Depends(get_db)):
    """这个学生的批改存档（新的在前）。学生资料页的「改作业」就吃这一份。"""
    try:
        return store.list_for_student(db, sid, limit)
    except store.ArchiveError as e:
        raise _fail(e) from e


@router.get("/grade-archives/{aid}")
def get_archive(aid: int, db: Session = Depends(get_db)):
    try:
        return store.detail(db, aid)
    except store.ArchiveError as e:
        raise _fail(e) from e


@router.get("/grade-archives/{aid}/raw")
def download_archive(
    aid: int,
    inline: bool = Query(False, description="图片/PDF 可以就地显示（预览用）；其它类型一律下载"),
    db: Session = Depends(get_db),
):
    """取回存档文件。

    `inline=1` 对**图片与 PDF** 生效 —— 本模块的核心诉求就是「点开记录能看批改情况」。
    其它类型（Word / Excel / 压缩包）一律 attachment：拖进 iframe 只会是白屏或乱码。

    ⚠️ 路径只认库里的 `rel_path`（经 `safe_join`），客户端传不了路径。
    ⚠️ 带 `X-Content-Type-Options: nosniff`：不靠浏览器猜类型。
    """
    try:
        rec = store.get_or_404(db, aid)
    except store.ArchiveError as e:
        raise _fail(e) from e
    p = store.abs_path(rec)
    if p is None or not p.exists() or not p.is_file():
        raise HTTPException(404, "文件已不在磁盘上（可能被手动删掉了）")

    name = rec.name or p.name
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    disp = "inline" if (inline and rec.kind in store.PREVIEW_KINDS) else "attachment"
    return FileResponse(
        str(p), media_type=media, filename=name,
        headers={"Content-Disposition":
                 f"{disp}; filename=file; filename*=UTF-8''{quote(name)}",
                 "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- 删
@router.delete("/grade-archives/{aid}")
def remove_archive(aid: int, db: Session = Depends(get_db)):
    """删这条存档 —— 连磁盘上的文件一起删。"""
    try:
        return store.remove(db, aid)
    except store.ArchiveError as e:
        raise _fail(e) from e
