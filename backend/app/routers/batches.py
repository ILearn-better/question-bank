# -*- coding: utf-8 -*-
"""批量入库接口：建任务 / 看进度 / 审条目 / 通过或驳回。

流程（前端 batch.html 走的就是这条）：
    ① POST /documents/{id}/batch-crop   一次裁出所有题块的原貌图（不动模型、不花钱）
    ② POST /batches                     带上这些图的 URL 建任务并开始并行识别
    ③ GET  /batches/{job}               轮询进度（done / total）
    ④ GET  /batches/{job}/items         拿待审列表
    ⑤ PATCH /batches/items/{id}         改字段
       POST  /batches/items/{id}/approve  写进正式题库
       POST  /batches/items/{id}/reject   驳回（留在表里，不进题库）
       POST  /batches/items/{id}/move     在待审列表里上移 / 下移
       POST  /batches/items/{id}/merge-up 题干并入上一条（跨页题的两半在这里接上）

**识别结果一律是草稿。** 不点「通过」就不会写进 questions ——
理由与录题页的单条识别一致：模型偶尔看错一个手写字母或正负号，
悄悄替老师决定了，错的符号会一路印到发给学生的卷子上。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import config, taxonomy
from ..adapters import pdf as pdf_adapter
from ..db import get_db
from ..models import BatchItem, BatchJob, Curriculum, Question
from ..schemas import AiPageJobIn, BatchItemPatch, BatchJobIn, FigureCropIn
from ..services import batch_import as batch
# AI 整页识别（实验流程）：整页图 → 模型直接输出多道题 → 人工只补配图。
# 与块模式共用任务/条目/审核这一整套下游，只有「条目怎么来」不同。
from ..services import batch_page
from ..services import crops
# 页面视图的数据源判断（.pdf 直接用原件 / .docx 先导出 PDF）只在 documents.py 里写过一份。
# 配图要框的也是**原卷的页面**，必须和那边的 pages / image / lines 用同一个数据源 ——
# 在这里再判一次「是不是 pdf」，迟早出现「页面视图能看、框配图却读不到文件」。
from .documents import _page_source

router = APIRouter(prefix="/api", tags=["batch"])


# ---------------------------------------------------------------- 工具
def _json_list(raw: str | None) -> list:
    try:
        v = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return v if isinstance(v, list) else []


def _job_dict(db: Session, job: BatchJob) -> dict:
    cur = db.get(Curriculum, job.curriculum_id) if job.curriculum_id else None
    pending = db.scalar(
        select(func.count()).select_from(BatchItem).where(
            BatchItem.job_id == job.id, BatchItem.status == "pending"
        )
    ) or 0
    approved = db.scalar(
        select(func.count()).select_from(BatchItem).where(
            BatchItem.job_id == job.id, BatchItem.status == "approved"
        )
    ) or 0
    rejected = db.scalar(
        select(func.count()).select_from(BatchItem).where(
            BatchItem.job_id == job.id, BatchItem.status == "rejected"
        )
    ) or 0
    return {
        "id": job.id,
        "status": job.status,
        "total": job.total,
        "done": job.done,
        "failed": job.failed,
        "curriculum_id": job.curriculum_id,
        "curriculum_name": cur.name if cur else None,
        "document_id": job.document_id,
        "doc_filename": job.doc_filename,
        "error": job.error,
        # 0 = 块模式（人工画分界线 → 裁块），1 = AI 整页模式（整页图直接喂模型）。
        # 前端据此切换提示文案与按钮（比如整页模式没有「题块」这个概念）。
        "ai_mode": job.ai_mode or 0,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        # 识别跑完了没：前端据此决定是「继续轮询」还是「切到待审列表」
        "finished": job.status in ("done", "partial", "failed"),
    }


def _item_dict(db: Session, it: BatchItem) -> dict:
    node_path = None
    if it.node_id:
        from ..services.knowledge import node_path as _np
        node_path = _np(db, it.node_id)
    return {
        "id": it.id,
        "seq": it.seq,
        "job_id": it.job_id,
        "document_id": it.document_id,
        "doc_filename": it.doc_filename,
        "page_no": it.page_no,
        "region": _json_list(it.region) if it.region else None,
        "image": it.image,
        "content": it.content or "",
        "qtype": it.qtype or taxonomy.DEFAULT_QTYPE,
        "difficulty": it.difficulty or taxonomy.DEFAULT_DIFFICULTY,
        "knowledge_point": it.knowledge_point or "",
        "node_id": it.node_id,
        # 挂到知识树上的全路径。为空表示「体系还没有知识树」或「没匹配上」——
        # 前端据此显示不同措辞（前者不该报警，后者才提示人工确认）
        "node_path": node_path,
        "tags": _json_list(it.tags),
        "confidence": it.confidence or taxonomy.DEFAULT_CONFIDENCE,
        "note": it.note or "",
        # 配图三件套：前两个是「要不要图」的当前结论与模型原话，最后一个是框出来的图。
        # needs_figure 用 bool 出给前端（库里存 0/1，SQLite 没有布尔列）。
        "needs_figure": bool(it.needs_figure),
        "figure_note": it.figure_note or "",
        "figure_image": it.figure_image or "",
        # 自动裁图用的框（归一化 0~1000）。前端**预填**框选弹窗用 ——
        # 有它老师才是「拖一下边」，没有就是「重新框一遍」。
        "figure_box": _json_list(it.figure_box) if it.figure_box else None,
        "flags": _json_list(it.flags),
        "status": it.status,
        "question_id": it.question_id,
        "error": it.error,
        "created_at": it.created_at,
        "reviewed_at": it.reviewed_at,
    }


def _item_or_404(db: Session, item_id: str) -> BatchItem:
    it = db.get(BatchItem, item_id)
    if it is None:
        raise HTTPException(404, "条目不存在")
    return it


# ---------------------------------------------------------------- 选项
@router.get("/batch/options")
def batch_options():
    """批量页要用到的固定取值。

    ⚠️ 刻意由服务端给而不是前端写死：题型与难度同时写在**提示词**里
       （让模型只能从里面选）和校验回落里，如果前端再写一份，
       改题型时三处会不同步，而这种不同步不报错，只会让下拉里出现没有的值。
       唯一出处见 app/taxonomy.py。
    """
    return {
        "qtypes": list(taxonomy.QTYPES),
        "difficulties": list(taxonomy.DIFFICULTIES),
        "confidences": list(taxonomy.CONFIDENCES),
        "max_tags": taxonomy.MAX_TAGS,
        "max_tag_chars": taxonomy.MAX_TAG_CHARS,
        "workers": batch.MAX_WORKERS,
        # AI 整页模式一次最多多少页（= 多少次请求）。前端据它做前置拦截，
        # 避免老师选了 50 页、提交后才被服务端 422 顶回来。
        "max_pages": batch_page.MAX_PAGES,
        # 「可能指向图形」的措辞。前端的用法是**提醒**而不是判定：
        # 题干命中这些词、模型却说不要图 → 标黄请老师自己确认（模型判图并不总准）。
        "figure_keywords": list(taxonomy.FIGURE_KEYWORDS),
    }


# ---------------------------------------------------------------- 任务
@router.post("/batches")
def create_batch(payload: BatchJobIn, db: Session = Depends(get_db)):
    """建任务。（可选）立刻开始并行识别。

    start=False 只建不跑 —— 测试用它绕开真实模型调用，
    也留给「先把这批切好、回头再识别」的用法。
    """
    items = []
    for i, it in enumerate(payload.items):
        name = os.path.basename((it.image or "").split("?")[0])
        if not name:
            raise HTTPException(422, f"第 {i + 1} 个题块没有原貌图")
        if not (config.CROPS_DIR / name).exists():
            raise HTTPException(422, f"第 {i + 1} 个题块的原貌图不存在，请重新分割")
        items.append({
            "image": it.image,
            "page_no": it.page_no,
            "region": it.region,
            "document_id": it.document_id,
            "doc_filename": it.doc_filename,
        })

    try:
        job = batch.create_job(
            db, items,
            curriculum_id=payload.curriculum_id,
            document_id=payload.document_id,
            doc_filename=payload.doc_filename,
        )
    except batch.BatchError as e:
        raise HTTPException(422, str(e)) from e

    if payload.start:
        _launch(job.id)
    return _job_dict(db, job)


def _launch(job_id: str, item_ids: list[str] | None = None) -> None:
    """起一个后台线程跑识别。

    为什么不用 FastAPI 的 BackgroundTasks：它挂在**那一次请求**的生命周期上，
    请求一返回就可能跟着结束（取决于实现），而这一批要跑一分多钟。
    独立线程更直白：任务状态在库里，进程重启也不会把库写坏（只是这一批停在 running）。
    daemon=True：进程退出时不要因为它卡住不退。
    """
    threading.Thread(target=batch.run_job, args=(job_id, item_ids), daemon=True).start()


@router.post("/batches/ai-pages")
def create_ai_page_batch(payload: AiPageJobIn, db: Session = Depends(get_db)):
    """建一个 **AI 整页识别**任务（实验流程，2026-10-08）。

    与 POST /batches（块模式）的差别只有「条目怎么来」：
      · 不给题块，给**页号** —— 页图由服务端从文档渲染（命中页面视图的磁盘缓存）
      · 条目是**识别时生成**的：一页可能出 0 到 N 道题，页数≠条目数
      · 不裁原貌图（题干文本模型直接给，出卷走文本形态，裁了只是攒垃圾图）

    进度语义要留意：job.total / done 是**页数**，
    而待审列表的条数（pending / approved / rejected）要识别完才有。
    """
    try:
        job = batch_page.create_page_job(
            db,
            payload.pages,
            curriculum_id=payload.curriculum_id,
            document_id=payload.document_id,
            doc_filename=payload.doc_filename,
        )
    except batch.BatchError as e:
        raise HTTPException(422, str(e)) from e

    if payload.start:
        _launch_page(job.id)
    return _job_dict(db, job)


def _launch_page(job_id: str) -> None:
    """起后台线程跑整页识别。理由与 _launch 完全相同（不用 BackgroundTasks）。"""
    threading.Thread(target=batch_page.run_page_job, args=(job_id,), daemon=True).start()


@router.get("/batches")
def list_batches(limit: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(BatchJob).order_by(BatchJob.created_at.desc()).limit(limit)
    ).all()
    return [_job_dict(db, j) for j in rows]


@router.get("/batches/{job_id}")
def get_batch(job_id: str, db: Session = Depends(get_db)):
    job = db.get(BatchJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return _job_dict(db, job)


@router.delete("/batches/{job_id}")
def delete_batch(job_id: str, db: Session = Depends(get_db)):
    """删任务。条目连带删除，**已通过的题目不动**（那已经是题库资产了）。

    截图按引用计数回收：删掉条目之后再算，只有确实没人再用的原貌图才会被抹掉
    （同一张图可能已被通过的题目引用，那种要留着）。
    """
    job = db.get(BatchJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    # 候选要包含原貌图**和**老师框的配图 —— 少算哪一类，那一类就会永远留在盘上
    mine = crops.crop_names(*[it.image for it in job.items],
                            *[it.figure_image for it in job.items])
    db.execute(delete(BatchItem).where(BatchItem.job_id == job_id))
    db.execute(delete(BatchJob).where(BatchJob.id == job_id))
    db.commit()
    removed = crops.purge(mine - crops.referenced_crops(db))
    return {"ok": True, "crops_removed": removed}


# ---------------------------------------------------------------- 条目
@router.get("/batches/{job_id}/items")
def list_items(
    job_id: str,
    status: str | None = Query(None, description="pending / approved / rejected，不传=全部"),
    db: Session = Depends(get_db),
):
    if db.get(BatchJob, job_id) is None:
        raise HTTPException(404, "任务不存在")
    stmt = select(BatchItem).where(BatchItem.job_id == job_id)
    if status:
        stmt = stmt.where(BatchItem.status == status)
    rows = db.scalars(stmt.order_by(BatchItem.seq)).all()
    return [_item_dict(db, it) for it in rows]


@router.patch("/batches/items/{item_id}")
def patch_item(item_id: str, payload: BatchItemPatch, db: Session = Depends(get_db)):
    """审核时改字段。**只改传上来的那些** —— 没传的保持原样。

    枚举照旧校验：手改也不能把题型写成下拉里没有的值，
    否则出卷按题型分区时这道题会掉进兜底组（不报错，只是静默错位）。
    """
    it = _item_or_404(db, item_id)
    data = payload.model_dump(exclude_unset=True)

    if "qtype" in data and data["qtype"] not in taxonomy.QTYPES:
        raise HTTPException(422, f"题型只能是 {' / '.join(taxonomy.QTYPES)}")
    if "difficulty" in data and data["difficulty"] not in taxonomy.DIFFICULTIES:
        raise HTTPException(422, f"难度只能是 {' / '.join(taxonomy.DIFFICULTIES)}")

    for k in ("content", "qtype", "difficulty", "knowledge_point"):
        if k in data:
            setattr(it, k, data[k])
    if "node_id" in data:
        it.node_id = data["node_id"]
    if "tags" in data:
        it.tags = json.dumps(
            [str(t).strip() for t in (data["tags"] or []) if str(t).strip()],
            ensure_ascii=False,
        )
    # 「这题要不要配图」——**人勾的为准**。模型给的只是初值，这里改了就是改了，
    # 后续重跑识别也不会再覆盖（run_job 只写识别结果，不碰 figure_image）。
    if "needs_figure" in data:
        it.needs_figure = 1 if data["needs_figure"] else 0
    if "figure_note" in data:
        it.figure_note = data["figure_note"] or None
    if "figure_image" in data:
        new_fig = (data["figure_image"] or "").strip()
        old_fig = it.figure_image or ""
        it.figure_image = new_fig
        if not new_fig and old_fig:
            # 撤掉配图 → 那张图如果别处也没引用，就该回收（否则永远占着盘）
            db.flush()
            crops.purge(crops.crop_names(old_fig) - crops.referenced_crops(db))
    # 老师一旦动手改过知识点，就把「模型给的」与「匹配结果」重新对一次 ——
    # 不改的话会出现「名字是新的、node_id 还是旧节点」的错挂
    if "knowledge_point" in data or "node_id" in data:
        job = db.get(BatchJob, it.job_id)
        if it.node_id in (None, 0) and it.knowledge_point:
            from ..services.knowledge import resolve_kp
            _kps, nid, _c = resolve_kp(
                db, [it.knowledge_point], job.curriculum_id if job else None, fuzzy=True
            )
            it.node_id = nid
    db.commit()
    return _item_dict(db, it)


@router.post("/batches/items/{item_id}/figure-crop")
def crop_item_figure(item_id: str, payload: FigureCropIn, db: Session = Depends(get_db)):
    """从原卷页面上框出一幅配图，挂到这条待审条目上。

    为什么要单独框一次：题块的原貌图是**整道题**（含全部文字），出卷走文本形态时
    整张都不印 —— 题干里那句「如图」的图就丢了。所以把那幅图单独框出来存一份，
    文本形态下也能补印（见 services/paper_export.py）。

    重新框会**替换**旧图，旧图立刻按引用计数回收 —— 反复调不会攒垃圾。

    框图这个动作本身就是「确认这题有图」，所以顺手把 needs_figure 置 1；
    反过来取消勾选（PATCH needs_figure=false）**不会**删掉已框的图 ——
    老师可能只是先取消一下，下次还想用。
    """
    it = _item_or_404(db, item_id)
    if it.status == "approved":
        raise HTTPException(409, "这条已经入库了，配图请到题库里改")
    if not it.document_id:
        raise HTTPException(422, "这条没有来源文档，无法框选配图（请重新上传原卷后再提交）")

    regions = [(r.page, r.x0, r.y0, r.x1, r.y1) for r in payload.regions]
    if not regions:
        raise HTTPException(422, "没有给出要框的区域")
    try:
        path = _page_source(db, it.document_id)
    except HTTPException as e:
        # 「Word 页面图还没生成」这类要原样透出，别吞成 500
        raise HTTPException(422, f"读不到原卷页面：{e.detail}") from e

    try:
        pno, x0, y0, x1, y1 = regions[0]
        if len(regions) == 1:
            name = pdf_adapter.crop_region(str(path), pno, [x0, y0, x1, y1], str(config.CROPS_DIR))
        else:
            name = pdf_adapter.crop_regions(str(path), regions, str(config.CROPS_DIR), gap=payload.gap)
    except ValueError as e:
        raise HTTPException(422, f"框选区域裁不出图：{e}") from e

    url = f"/api/crops/{name}"
    old = it.figure_image or ""
    it.figure_image = url
    it.needs_figure = 1
    # 记住老师这次框的框 —— 下次再进来（或换台机器、或过了几天）弹窗直接预填它，
    # 而不是从零开始画。跨页竖拼的图没法用一个矩形表示，那就清掉。
    if len(regions) == 1:
        try:
            W, H = pdf_adapter.page_size(str(path), pno)
            it.figure_box = json.dumps([
                int(round(x0 / W * 1000)), int(round(y0 / H * 1000)),
                int(round(x1 / W * 1000)), int(round(y1 / H * 1000)),
            ])
        except Exception:                                # noqa: BLE001
            it.figure_box = None
    else:
        it.figure_box = None
    db.flush()                       # 新值先落进事务，引用计数才数得对
    replaced = 0
    if old and old != url:
        replaced = crops.purge(crops.crop_names(old) - crops.referenced_crops(db))
    db.commit()
    return {"ok": True, "url": url, "replaced": replaced, "item": _item_dict(db, it)}


@router.post("/batches/items/{item_id}/approve")
def approve_item(item_id: str, db: Session = Depends(get_db)):
    """通过 → 写进正式题库。返回新题目 id。"""
    it = _item_or_404(db, item_id)
    if it.status == "approved":
        raise HTTPException(409, "这条已经通过过了")
    if not (it.content or "").strip() and not it.image:
        raise HTTPException(422, "题干和原貌图都没有，无法入库")
    try:
        qid = batch.approve_item(db, it)
    except Exception as e:                                   # noqa: BLE001
        db.rollback()
        raise HTTPException(500, f"入库失败：{e}") from e
    return {"ok": True, "question_id": qid, "item": _item_dict(db, it)}


@router.post("/batches/items/{item_id}/reject")
def reject_item(item_id: str, db: Session = Depends(get_db)):
    """驳回。条目留着（留档），只是不进题库。

    为什么不直接删：老师驳回的原因常常是「这段切歪了，回头重切」，
    留着一张图能让他判断问题出在哪；也避免误点驳回后再也找不回原来的识别结果。
    真要清干净就删整个任务（DELETE /batches/{id}）。
    """
    it = _item_or_404(db, item_id)
    if it.status == "approved":
        raise HTTPException(409, "这条已经入库了，驳回前请先删掉那道题")
    it.status = "rejected"
    it.reviewed_at = datetime.now().isoformat(timespec="seconds")
    db.commit()
    return {"ok": True, "item": _item_dict(db, it)}


@router.post("/batches/items/{item_id}/reset")
def reset_item(item_id: str, db: Session = Depends(get_db)):
    """把驳回的条目退回待审。"""
    it = _item_or_404(db, item_id)
    it.status = "pending"
    it.reviewed_at = None
    db.commit()
    return {"ok": True, "item": _item_dict(db, it)}


# ------------------------------------------------- 待审列表里调整顺序 / 合并条目
# 为什么需要这两个动作（2026-10-08 新增）：
#   AI 整页识别里**没有「人工分割」这一步**，一道**跨页的题**会被页界切成上下两半 ——
#   上半是第 N 页的最后几行、下半是第 N+1 页的开头。模型只能把它当成两道题分别输出。
#   块模式里可以在分割处把两段拖到一起再提交；整页模式没有那个环节，
#   所以合并只能放到**待审列表**里做。
#
# ⚠️ 合并只动**题干文本**：题型 / 难度 / 知识点 / 标签一律以上一条为准。
#    上下两半各自带的那几项往往不一样（下半句里才知道是解答题），
#    自动挑一个等于替老师做决定 —— 而老师就在这一页上，让他自己看一眼更靠谱。

def _items_in_order(db: Session, job_id: str) -> list[BatchItem]:
    """同一任务的全部条目，按界面显示顺序。"""
    return list(
        db.scalars(
            select(BatchItem)
            .where(BatchItem.job_id == job_id)
            .order_by(BatchItem.seq, BatchItem.created_at, BatchItem.id)
        ).all()
    )


def _reorder(rows: list[BatchItem]) -> None:
    """把 seq 重排成 1..N。

    移动会打乱编号的先后，合并会留下空洞（删掉第 5 条后是 1,2,3,4,6…），
    两种情况都让「#5」这种编号跟界面上的位置对不上。重排之后才对得上。
    seq 只是**显示顺序**，改它不影响任何已入库的题。
    """
    for i, it in enumerate(rows, start=1):
        it.seq = i


def _join_content(prev: str | None, cur: str | None) -> str:
    """把两半题干接起来。

    用空行（`\\n\\n`）而不是单换行：前端是 Markdown 渲染，单换行在同一段里
    会被折成空格，两半看着像连成一句话。
    """
    a, b = (prev or "").strip(), (cur or "").strip()
    if not a:
        return b
    if not b:
        return a
    return f"{a}\n\n{b}"


def _neighbor(rows: list[BatchItem], it: BatchItem, step: int) -> BatchItem | None:
    """紧邻的那条。<0 是上一条，>0 是下一条。"""
    idx = next((i for i, r in enumerate(rows) if r.id == it.id), None)
    if idx is None:
        return None
    j = idx + step
    if j < 0 or j >= len(rows):
        return None
    return rows[j]


@router.post("/batches/items/{item_id}/move")
def move_item(
    item_id: str,
    direction: str = Query("up", description="up / down"),
    db: Session = Depends(get_db),
):
    """在待审列表里上移 / 下移一条。

    只允许**待审**条目动，而且不允许跨过已入库 / 已驳回的条目：
    跨过去会让「还没审的那几条」在列表里被劈成好几段，看着像丢了一条。
    """
    if direction not in ("up", "down"):
        raise HTTPException(422, "direction 只能是 up 或 down")
    it = _item_or_404(db, item_id)
    if it.status != "pending":
        raise HTTPException(409, "只有待审的条目能调整顺序")

    rows = _items_in_order(db, it.job_id)
    idx = next((i for i, r in enumerate(rows) if r.id == it.id), None)
    if idx is None:
        raise HTTPException(404, "条目不存在")
    other = _neighbor(rows, it, -1 if direction == "up" else 1)
    if other is None:
        raise HTTPException(422, "已经在最前面了" if direction == "up" else "已经在最后面了")
    if other.status != "pending":
        where = "上面" if direction == "up" else "下面"
        what = "入库" if other.status == "approved" else "驳回"
        raise HTTPException(422, f"{where}那条已经{what}了，不能跨过去")

    i, j = rows.index(it), rows.index(other)
    rows[i], rows[j] = rows[j], rows[i]
    _reorder(rows)
    db.commit()
    return {"ok": True, "items": [_item_dict(db, r) for r in rows]}


@router.post("/batches/items/{item_id}/merge-up")
def merge_item_up(item_id: str, db: Session = Depends(get_db)):
    """把这条的**题干文本**并入紧邻的上一条，然后删掉这条。

    典型用法：跨页题的两半在列表里相邻 → 点「并入上一题」→ 接成一道题再入库。
    两半不相邻就先上移 / 下移。
    """
    it = _item_or_404(db, item_id)
    if it.status != "pending":
        raise HTTPException(409, "只有待审的条目能合并")

    rows = _items_in_order(db, it.job_id)
    prev = _neighbor(rows, it, -1)
    if prev is None:
        raise HTTPException(422, "已经是第一条了，没有可并入的上一题")
    if prev.status != "pending":
        what = "入库" if prev.status == "approved" else "驳回"
        raise HTTPException(422, f"上一条已经{what}了，不能并入")

    # ---- 配图：只合并文本 ≠ 可以把老师框好的图丢掉 ----
    # 本条有、上一条没有 → 迁移过去（跨页题常常只有一半带图）；
    # 两条都有 → 拒绝。figure_image 只有一列，默默丢掉一张是不可接受的。
    cur_fig, prev_fig = (it.figure_image or ""), (prev.figure_image or "")
    moved_fig = False
    if cur_fig and not prev_fig:
        prev.figure_image = cur_fig
        prev.needs_figure = 1
        if not (prev.figure_note or "").strip():
            prev.figure_note = it.figure_note
        it.figure_image = ""
        moved_fig = True
    elif cur_fig and prev_fig and cur_fig != prev_fig:
        raise HTTPException(
            422, "上下两条都框了配图，一道题只能带一张 —— 先把其中一条的配图撤掉再合并"
        )

    prev.content = _join_content(prev.content, it.content)

    # 本条独有的图（块模式的原貌图 / 没迁移走的配图）在删除后按引用计数回收。
    # ⚠️ 顺序：先 flush 让「迁移后的引用」进事务，再删、再 flush，
    #    最后才数引用 —— 否则刚迁移过去的那张会被当成无人引用删掉。
    orphan = crops.crop_names(it.image, it.figure_image)
    db.flush()
    db.delete(it)
    rows.remove(it)
    _reorder(rows)
    db.flush()
    removed = crops.purge(orphan - crops.referenced_crops(db))
    db.commit()
    return {
        "ok": True,
        "merged_into": prev.id,
        "figure_moved": moved_fig,
        "crops_removed": removed,
        "items": [_item_dict(db, r) for r in rows],
    }


@router.post("/batches/{job_id}/rerun")
def rerun_job(job_id: str, only_failed: bool = Query(True), db: Session = Depends(get_db)):
    """重跑识别。默认只重跑**失败的那些**（成功的别浪费钱再识别一遍）。"""
    job = db.get(BatchJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    if job.status == "running":
        raise HTTPException(409, "这个任务正在识别中，等它跑完再重试")

    if job.ai_mode == 1:
        # 整页模式的失败是**页级**的：失败的那页留了一条标了 ai_failed 的占位条目
        # （见 services/batch_page.py 的 _write_page_error），页号记在 page_no 上。
        # 重跑 = 删掉这些占位条目 + 重新识别那几页。
        # ⚠️ 只删**带 error 的**那些 —— 已经识别成功、老师可能已经审核过的题一条都不能碰。
        rows = db.scalars(
            select(BatchItem).where(
                BatchItem.job_id == job_id, BatchItem.error.isnot(None)
            )
        ).all()
        pages = sorted({it.page_no for it in rows if it.page_no})
        if not pages:
            raise HTTPException(422, "没有识别失败的页面需要重跑")
        for it in rows:
            db.delete(it)
        job.status, job.done, job.failed = "queued", 0, 0
        job.error, job.finished_at = None, None
        job.pages = json.dumps(pages)
        job.total = len(pages)      # 这一轮的 total 指**本次要跑的页数**
        db.commit()
        _launch_page(job_id)
        return _job_dict(db, job)

    stmt = select(BatchItem).where(BatchItem.job_id == job_id)
    if only_failed:
        stmt = stmt.where(BatchItem.error.isnot(None))
    pend = db.scalars(stmt).all()
    if not pend:
        raise HTTPException(422, "没有需要重跑的条目")
    ids = [it.id for it in pend]
    for it in pend:
        it.error = None
        it.flags = "[]"
    job.status, job.done, job.failed, job.error, job.finished_at = "queued", 0, 0, None, None
    # total 在这一轮指的是**本次要跑的条数**，不是整批 —— 进度条读的就是它。
    # 全部条目数另有 pending/approved/rejected 三个计数表达。
    job.total = len(pend)
    db.commit()
    _launch(job_id, ids)
    return _job_dict(db, job)
