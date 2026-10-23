# -*- coding: utf-8 -*-
"""自动导入接口：**给 agent / 脚本用的**一条命令入口。

    讲义 → [POST /api/auto/import] → task_id
         → [GET /api/auto/imports/{id}] 轮询（跑到 done）
         → 报告里 needs_human 为空 = 全自动入库完毕

设计上刻意的三个取舍：

① **一个入口而不是让调用方串七个接口**
   现有 batches 那条链要上传 → 看页数 → 建 job → 轮询 → 读条目 → 改图 → 通过，
   任何一步顺序错或漏掉，结果是「跑完了但没入库」。编排属于服务端。

② **同步只做校验，重活全在后台**
   一份 44 页讲义要跑十几到几十分钟。接口必须立刻返回 task_id，
   否则 agent 那边必然超时。所以这里的每个 POST 都只做「能不能开工」的预检。

③ **错误信息里带可选项**
   比如没传体系时不是「缺少参数」，而是把库里四个体系列出来。
   agent 拿到就能自己纠正，或者直接问用户 —— 猜体系是绝对不行的
   （一份卷子一套体系，判错的代价是整批知识点挂错树）。
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..adapters import pdf as pdf_adapter
from ..db import get_db
from ..models import AutoTask, Curriculum, Document
from ..schemas import AutoImportIn
from ..services import auto_import
from .documents import _page_source, check_ext, register_document

router = APIRouter(prefix="/api", tags=["auto"])


# ---------------------------------------------------------------- 工具
def _curricula(db: Session) -> list[dict]:
    rows = db.scalars(select(Curriculum).order_by(Curriculum.sort_order)).all()
    return [{"id": c.id, "code": c.code, "name": c.name} for c in rows]


def _resolve_curriculum(db: Session, spec) -> Curriculum:
    """把 `id` / `code` / `name` 任意一种认成体系。

    ⚠️ 认不出就报错并**列出可选项**，绝不猜：
       一份卷子一套体系，猜错的代价是整批知识点挂到错误的树上 ——
       而那种错不会报错，只会在出卷时发现「知识点全对不上」。
    """
    opts = _curricula(db)
    hint = "、".join("%s(%s)" % (o["name"], o["code"]) for o in opts) or "（库里的体系列表是空的）"

    if spec is None or spec == "":
        raise HTTPException(422, "请指定体系（知识点要挂到对应体系的知识树上）。可选：" + hint)

    text = str(spec).strip()
    # ① 数字 → 按 id
    if text.isdigit():
        c = db.get(Curriculum, int(text))
        if c is not None:
            return c
    # ② 按 code / name 精确匹配（code 不分大小写，name 必须完全一致 —— 名字太像）
    low = text.lower()
    for c in db.scalars(select(Curriculum)).all():
        if (c.code or "").lower() == low or (c.name or "") == text:
            return c
    raise HTTPException(422, "认不出体系 %r。可选：%s" % (text, hint))


def _precheck_pages(db: Session, doc: Document, spec) -> list[int]:
    """确定要处理哪些页，并**先确认页图拿得到**。

    预检放在这里（同步）而不是等后台跑，是因为页图是整页识别的前提：
    `.docx` 要先启动 Word 导出 PDF，慢但一定会发生。等后台跑到第 1 页才报错，
    agent 已经等了半天，报告里只有一句「读不到文档页面」。
    """
    try:
        src = _page_source(db, doc.id)          # .docx 会在这里自动转 PDF（有缓存）
        total = pdf_adapter.page_count(str(src))
    except HTTPException as e:
        raise HTTPException(422, "这份文档生成不出页面图：%s" % e.detail) from e
    except Exception as e:                       # noqa: BLE001
        raise HTTPException(422, "这份文档生成不出页面图：%s" % e) from e
    if not total:
        raise HTTPException(422, "这份文档一页都没有")

    pages = auto_import.parse_pages(spec, total)
    if not pages:
        raise HTTPException(422, "没有要处理的页面（共 %d 页）" % total)
    bad = [p for p in pages if p > total]
    if bad:
        raise HTTPException(422, "页号超出范围：%s（共 %d 页）" % (bad[:5], total))
    return pages


def _task_dict(db: Session, t: AutoTask, with_report: bool = True) -> dict:
    jobs = auto_import._as_list(t.job_ids)
    done_pages = 0
    job_rows = []
    from ..models import BatchJob
    for jid in jobs:
        j = db.get(BatchJob, jid)
        if j is None:
            continue
        done_pages += (j.done or 0)
        job_rows.append({"id": j.id, "status": j.status,
                         "pages": _len(j.pages), "done": j.done or 0,
                         "failed": j.failed or 0, "error": j.error})

    cur = db.get(Curriculum, t.curriculum_id) if t.curriculum_id else None
    report = None
    if with_report and t.report:
        try:
            report = json.loads(t.report)
        except ValueError:
            report = None

    return {
        "id": t.id,
        "status": t.status,
        # 结束态：三个都算「不用再等了」。agent 轮询就看这个字段。
        "done": t.status in ("done", "partial", "failed"),
        "ok": t.status == "done",
        "stage": t.stage,
        "src_filename": t.src_filename,
        "document_id": t.document_id,
        "curriculum": ({"id": cur.id, "code": cur.code, "name": cur.name} if cur else None),
        "total_pages": t.total_pages,
        "progress": {"pages_done": done_pages, "pages_total": t.total_pages},
        "jobs": job_rows,
        "counts": {"total": t.total_items, "approved": t.approved,
                   "needs_human": t.needs_human, "skipped": t.skipped},
        "error": t.error,
        "created_at": t.created_at,
        "finished_at": t.finished_at,
        "report": report,
    }


def _len(raw) -> int:
    try:
        v = json.loads(raw or "[]")
        return len(v) if isinstance(v, list) else 0
    except ValueError:
        return 0


# ---------------------------------------------------------------- 体系
@router.get("/auto/curricula")
def auto_curricula(db: Session = Depends(get_db)):
    """可选体系。**agent 第一步调它**：把讲义交给它之前得先知道挂哪个体系。

    这里刻意不给「自动判断体系」：一份卷子一套体系，
    模型judge错的代价是整批知识点挂错树，而问用户一次的成本是零。
    """
    return {"curricula": _curricula(db)}


# ---------------------------------------------------------------- 开始
def _kickoff(db: Session, doc: Document, pages: list[int], curriculum: Curriculum) -> dict:
    task = auto_import.start(db, document_id=doc.id, pages=pages,
                             curriculum_id=curriculum.id, src_filename=doc.filename)
    return _task_dict(db, task, with_report=False)


@router.post("/auto/import")
async def auto_import_upload(
    file: UploadFile = File(...),
    curriculum: str | None = Form(None, description="体系：id / code / name 任一"),
    pages: str | None = Form(None, description="页范围，如 '1-30,35'；不传 = 全部"),
    db: Session = Depends(get_db),
):
    """上传一份讲义并**立刻开始**全自动导入。返回任务（此时还没跑完）。

    `curriculum` 认 id / code / name 三种写法，认不出会列出可选项。
    `pages` 不传 = 全部页；支持 `1-30,35` 这种区间写法。
    """
    spec = _resolve_curriculum(db, curriculum)          # 先校验体系：别为一次无效调用存文件
    filename = file.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    check_ext(ext)

    save_path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    with open(save_path, "wb") as f:
        f.write(await file.read())

    doc, _blocks = register_document(db, filename, ext, save_path)
    try:
        ps = _precheck_pages(db, doc, pages)
    except HTTPException:
        raise
    return _kickoff(db, doc, ps, spec)


@router.post("/auto/import-path")
def auto_import_path(payload: AutoImportIn, db: Session = Depends(get_db)):
    """同上，但传**本机文件路径**而不是上传 —— agent 手上通常就是路径。

    文件会被复制进 `data/uploads/`（原文件不动）：题库的资产归自己管，
    否则老师哪天移动了桌面上的讲义，库里的题就全变成「读不到原卷」。
    """
    spec = _resolve_curriculum(db, payload.curriculum)
    src = Path(payload.path or "").expanduser()
    if not src.is_file():
        raise HTTPException(422, "找不到文件：%s" % src)
    ext = src.suffix.lower()
    check_ext(ext)

    save_path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    shutil.copy2(src, save_path)

    doc, _blocks = register_document(db, src.name, ext, save_path)
    ps = _precheck_pages(db, doc, payload.pages)
    return _kickoff(db, doc, ps, spec)


# ---------------------------------------------------------------- 查 / 收尾
@router.get("/auto/imports")
def list_auto_imports(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(AutoTask).order_by(AutoTask.created_at.desc()).limit(limit)
    ).all()
    # 列表不带 report（可能几百 KB），要详情请调下面那个
    return [_task_dict(db, t, with_report=False) for t in rows]


@router.get("/auto/imports/{task_id}")
def get_auto_import(task_id: str, db: Session = Depends(get_db)):
    """**agent 轮询这个**。`done=true` 就别再等了，看 `report.needs_human`。"""
    t = db.get(AutoTask, task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    return _task_dict(db, t)


@router.post("/auto/imports/{task_id}/resume")
def resume_auto_import(task_id: str, db: Session = Depends(get_db)):
    """老师把 `needs_human` 处理完了 → 调这个**只补入库**，不重新识别。

    为什么要单独一个动作：重新识别要重花 AI 调用（几十页十几分钟），
    而人工框完图之后只需要「再判一次、能过的入库」。
    """
    t = db.get(AutoTask, task_id)
    if t is None:
        raise HTTPException(404, "任务不存在")
    if t.status in ("queued", "recognizing", "fixing", "approving"):
        raise HTTPException(409, "任务还在跑，等它结束再收尾（当前：%s）" % t.stage)
    if not auto_import._as_list(t.job_ids):
        raise HTTPException(422, "这个任务没有识别结果，没什么可收的")
    try:
        auto_import.resume(task_id)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    db.expire_all()
    return _task_dict(db, db.get(AutoTask, task_id))
