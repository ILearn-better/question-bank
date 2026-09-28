# -*- coding: utf-8 -*-
"""笔记：Markdown 正文 + 板书笔画 + 配图。

一条笔记就是一行：正文是 Markdown 文本，笔画是 JSON 数组，配图落在 data/uploads/notes/。

为什么笔画存 JSON 而不是渲染成 PNG：
  橡皮、换色、调粗细、撤销，本质都是「按新状态把线重画一遍」。存位图就做不到，
  而且位图占空间、缩放就糊。详见 models.Note 的注释。

自动保存：前端改了内容就 PATCH 一次（带防抖），updated_at 由服务端写 ——
时间戳统一由服务端给，免得依赖客户端时钟。
"""
from __future__ import annotations

import json
import mimetypes
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .. import config
from ..adapters import notes_math, office
from ..db import get_db
from ..models import Note, NoteFolder
from ..schemas import NoteIn, NotePatch
from ..services import images, notes_export
from . import note_folders, notes_transfer

router = APIRouter(prefix="/api/notes", tags=["notes"])

# 正文里引用到的配图：定义在 notes_transfer 里（导出/导入也要用同一份），
# 这里只是取个短名字。两处各写一份的话，改了一处另一处会静默漏掉配图，
# 删笔记时就留下垃圾文件。
_IMG_RE = notes_transfer.IMAGE_RE


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_ink(raw: str | None) -> list:
    try:
        v = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return v if isinstance(v, list) else []


def _excerpt(content: str, n: int = 90) -> str:
    """列表里的一段摘要：去掉 Markdown 标记与公式，只留认得出的字。"""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[图]", content or "")
    s = re.sub(r"\$\$?[^$]*\$?\$?", "", s)
    s = re.sub(r"[#>*`_~]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def _brief(n: Note) -> dict:
    """列表用的轻量字段 —— 故意不带 content/ink，列表不该把整篇正文拖下来。"""
    return {
        "id": n.id,
        "title": n.title,
        "pinned": bool(n.pinned),
        # 目录树靠它挂节点；排序靠它跟 sort_order（拖动出来的手排顺序）
        "folder_id": n.folder_id,
        "sort_order": n.sort_order,
        "excerpt": _excerpt(n.content or ""),
        "updated_at": n.updated_at,
        "created_at": n.created_at,
    }


def _full(n: Note) -> dict:
    return {**_brief(n), "content": n.content or "", "ink": _parse_ink(n.ink)}


def _note_or_404(db: Session, nid: str) -> Note:
    n = db.get(Note, nid)
    if n is None:
        raise HTTPException(404, "笔记不存在")
    return n


def _used_images(db: Session) -> set[str]:
    """所有笔记引用到的配图文件名（引用计数的依据）。"""
    used: set[str] = set()
    for (content,) in db.execute(select(Note.content)).all():
        used |= set(_IMG_RE.findall(content or ""))
    return used


# ---------------------------------------------------------------- CRUD
@router.get("")
def list_notes(
    keyword: str | None = None,
    folder_id: int | None = Query(None, description="只看某个目录"),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """平铺列表（不带 keyword 时就是「全部笔记」）。

    界面上的主视图已经改成目录树（见 note_folders.note_tree），这个接口留着的两件事：
    ① 搜索 —— 命中就平铺出来，每条带 path 说明它在哪；② 不带条件的全量导出/统计。
    """
    conds = []
    if folder_id is not None:
        conds.append(Note.folder_id == folder_id)
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        conds.append(or_(Note.title.like(like), Note.content.like(like)))
    rows = db.scalars(
        select(Note).where(*conds)
        .order_by(Note.pinned.desc(), Note.sort_order, Note.updated_at.desc())
        .limit(limit)
    ).all()
    paths = note_folders.folder_paths(db)
    items = []
    for n in rows:
        items.append({**_brief(n), "path": paths.get(n.folder_id, "")})
    return {"items": items}


@router.post("")
def create_note(payload: NoteIn, db: Session = Depends(get_db)):
    nid = uuid.uuid4().hex[:12]
    now = _now()
    # 没指定目录就落「未归档」：新建永远不会因为没选目录而没归属
    folder = (db.get(NoteFolder, payload.folder_id) if payload.folder_id
              else note_folders.unfiled_root(db))
    if folder is None:
        raise HTTPException(404, "目录不存在")
    db.add(Note(
        id=nid,
        owner_id=config.OWNER_ID,
        folder_id=folder.id,
        title=(payload.title or "").strip() or "未命名笔记",
        content=payload.content or "",
        ink=json.dumps(payload.ink or [], ensure_ascii=False),
        pinned=1 if payload.pinned else 0,
        created_at=now,
        updated_at=now,
    ))
    db.commit()
    return {"id": nid}


@router.get("/{nid}")
def get_note(nid: str, db: Session = Depends(get_db)):
    return _full(_note_or_404(db, nid))


@router.patch("/{nid}")
def update_note(nid: str, payload: NotePatch, db: Session = Depends(get_db)):
    """部分更新。只改显式传进来的字段 —— 自动保存时不会把另一头的内容覆盖掉。

    拖动（folder_id / position）走的也是这里，但**不动 updated_at**：
    那是「最后编辑」，把笔记拖个位置不该算改动，否则「最近改过的」这列会全被拖动刷乱。
    """
    n = _note_or_404(db, nid)
    data = payload.model_dump(exclude_unset=True)
    edited = False
    if "title" in data:
        n.title = (data["title"] or "").strip() or "未命名笔记"
        edited = True
    if "content" in data:
        n.content = data["content"] or ""
        edited = True
    if "ink" in data:
        n.ink = json.dumps(data["ink"] or [], ensure_ascii=False)
        edited = True
    if "pinned" in data:
        n.pinned = 1 if data["pinned"] else 0
        edited = True
    if "folder_id" in data:
        if data["folder_id"] is None:
            # 不做「传 null 就回未归档」：那会让一次写错的请求静默搬走笔记
            raise HTTPException(422, "笔记必须属于一个目录（要搬走就传目标目录的 id）")
        if db.get(NoteFolder, data["folder_id"]) is None:
            raise HTTPException(404, "目标目录不存在")
        n.folder_id = data["folder_id"]
    if data.get("position") is not None:
        note_folders.place_note(db, n, int(data["position"]))
    if edited:
        n.updated_at = _now()
    db.commit()
    return _brief(n)


@router.delete("/{nid}")
def delete_note(nid: str, db: Session = Depends(get_db)):
    """删笔记，并清掉只有它引用的配图（引用计数，和删题目清截图同一套思路）。"""
    n = _note_or_404(db, nid)
    mine = set(_IMG_RE.findall(n.content or ""))

    db.execute(delete(Note).where(Note.id == nid))
    removed = 0
    for name in (mine - _used_images(db)) if mine else ():
        try:
            (config.NOTES_DIR / name).unlink()
            removed += 1
        except OSError:
            pass

    db.commit()
    return {"ok": True, "images_removed": removed}


# ---------------------------------------------------------------- 配图
@router.post("/image")
async def upload_image(file: UploadFile = File(...)):
    """笔记配图上传。返回可直接写进 Markdown 的 URL。"""
    data = await file.read(images.MAX_IMAGE_BYTES + 1)
    try:
        saved = images.save_image(data, config.NOTES_DIR, prefix="nb")
    except images.ImageRejected as e:
        raise HTTPException(422, str(e)) from e
    return {"url": f"/api/notes/files/{saved['name']}", **saved}


@router.get("/files/{name}")
def get_file(name: str):
    """配图读取。只按文件名取（Path.name），防路径穿越。"""
    p = config.NOTES_DIR / Path(name).name
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "图片不存在")
    # 按扩展名给 MIME，不写死 png —— 目录里混进别的格式也不用改这里
    media = mimetypes.guess_type(p.name)[0] or "image/png"
    return FileResponse(str(p), media_type=media)


# ---------------------------------------------------------------- 导出 Word / PDF
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# 支持导出的格式 -> MIME
EXPORT_FORMATS = {"docx": DOCX_MIME, "pdf": "application/pdf"}


@router.get("/export/caps")
def export_caps():
    """导出能力探测。

    为什么要单独问一次：PDF 依赖本机 Word、公式渲染依赖 node + Office 的 XSLT，
    这些条件不满足时**导出会失败或降级**。事先问一下，前端就能把按钮的状态和
    原因直接写在界面上，而不是让用户点了之后看到一段报错。
    """
    formula_ok, formula_reason = notes_math.availability()
    pdf_ok = office.is_available()
    return {
        "formats": sorted(EXPORT_FORMATS),
        "pdf": pdf_ok,
        "pdf_reason": "" if pdf_ok else office.availability_note(),
        "formula": formula_ok,
        "formula_reason": formula_reason,
    }


@router.get("/{nid}/export")
def export_note(
    nid: str,
    format: str = Query("docx", description="docx / pdf"),
    ink: bool = Query(True, description="是否附上板书（手写标注）"),
    db: Session = Depends(get_db),
):
    fmt = (format or "docx").lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(422, f"不支持的格式 {format}（可选 docx / pdf）")

    n = _note_or_404(db, nid)
    note = {
        "title": n.title,
        "content": n.content or "",
        "ink": _parse_ink(n.ink),
        "updated_at": n.updated_at,
    }

    try:
        if fmt == "docx":
            body, _info = notes_export.build_docx(note, include_ink=ink)
        else:
            body, _info = notes_export.build_pdf(note, include_ink=ink)
    except notes_export.PdfUnavailable as e:
        # 503：本机能力不足（不是请求错），前端据此提示"先导 Word 再另存为 PDF"
        raise HTTPException(503, str(e)) from e

    fname = notes_export.safe_filename(n.title, fmt)
    return Response(
        content=body,
        media_type=EXPORT_FORMATS[fmt],
        headers={
            # 中文文件名给 filename*（RFC 5987），同时留 ASCII 兜底给老客户端
            "Content-Disposition": (
                f'attachment; filename="note.{fmt}"; '
                f"filename*=UTF-8''{quote(fname)}"
            )
        },
    )
