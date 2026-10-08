# -*- coding: utf-8 -*-
"""试卷原件与页面视图服务。

页面视图的数据源（`_page_source`）分两种情况：
  · .pdf  → 原件本身就是 PDF，直接用
  · .docx → 用本机 Word 导出一份 PDF 作为页面视图的数据源（见 adapters/office.py）

这样「在页面上画框选区」这套交互对 PDF 和 Word 是同一套代码，
前端只需要知道「这个文档能不能进页面视图」，不需要知道背后是哪种原件。

⚠️ Word 的转换结果入库（documents.preview_pdf / preview_error），不用猜文件在不在：
   转换要启动 Word，代价是秒级；失败原因必须记住，否则每次打开都要白等一次超时。
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..adapters import office as office_adapter
from ..adapters import pdf as pdf_adapter
from ..db import get_db
from ..migrate import backup_db
from ..models import Document, Question, QuestionNode
from ..schemas import BatchCropIn, CropIn, CropStripIn
from ..services import crops

router = APIRouter(prefix="/api", tags=["documents"])

CROPS_DIR = str(config.CROPS_DIR)
PREVIEW_ERROR_MAX = 600

# 可以**直接当一页**用的图片格式。
#
# 为什么允许：用户 2026-10-08 的要求是「给一张图片，直接把图片和文本分割成可入库形式」——
# 手上只有手机拍的/截屏的一道题时，不该先逼他去找工具转成 PDF。
#
# 为什么不用单独一条处理链：pymupdf 打开图片就是**单页文档**（做过验证：
# 400x300 的 PNG 打开后 page_count=1、页面矩形按 96dpi 折算成 300x225 点）。
# 于是页面视图、渲染、框选裁剪、图形候选探测这一整套**一行都不用改** ——
# 只要让上面那几个「这是不是 PDF」的分支把图片也算进去。
#
# ⚠️ 页面坐标因此是「按 96dpi 折算的点」，不是 A4 的 595x842。
#    凡是拿归一化比例换算的地方都必须用 page_size() 取真实尺寸，
#    绝不能假定 A4（见 adapters/pdf.py 的 page_size 注释）。
PAGE_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _is_paged(filetype: str | None) -> bool:
    """这份文档是否**本身就带页面视图**（PDF / 图片，无需 Word 转换）。"""
    return (filetype or "").lower() in (".pdf", *PAGE_IMAGE_EXTS)



def _doc_or_404(db: Session, doc_id: str) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "文档不存在")
    return doc


# ---------------------------------------------------------------- 路径解析
def _source_file(db: Session, doc_id: str) -> Path:
    """原始上传文件（.pdf / .docx / 图片），必须真实存在。"""
    doc = _doc_or_404(db, doc_id)
    path = config.abs_from_data(doc.file_path)
    if path is None or not Path(path).exists():
        raise HTTPException(422, "该文档缺少原始文件（早期上传的记录），请重新上传")
    return Path(path)


def _preview_pdf_path(doc: Document) -> Path:
    """Word 转换件的存放路径。用 doc_id 命名，重建时直接覆盖，不会攒垃圾。"""
    return config.CONVERTED_DIR / f"{doc.id}.pdf"


def _clear_render_cache(doc_id: str) -> None:
    """重建转换件时必须清掉页面渲染图，否则前端会一直看到旧的页面。

    这是最容易被漏掉的一步：缓存 key 只按 doc_id 分目录，
    换了数据源但不清缓存 = 改了 Word 却还看到上一版的内容。
    """
    shutil.rmtree(config.PAGES_CACHE / doc_id, ignore_errors=True)


def ensure_preview(db: Session, doc: Document, force: bool = False) -> Path:
    """确保 Word 文档有可用的页面视图数据源，返回那份 PDF 的路径。

    .pdf 文档与**图片**直接返回原件（本身就是页面视图，无需转换）。
    .docx 文档在 preview_pdf 缺失或 force 时调用 Word 转换，并把结果入库。
    """
    if _is_paged(doc.filetype):
        return _source_file(db, doc.id)

    out = _preview_pdf_path(doc)
    if not force and doc.preview_pdf and out.exists():
        return out

    src = _source_file(db, doc.id)
    try:
        pages = office_adapter.word_to_pdf(src, out)
    except office_adapter.OfficeUnavailable as e:
        doc.preview_pdf = None
        doc.preview_pages = None
        doc.preview_error = str(e)[:PREVIEW_ERROR_MAX]
        db.commit()
        raise HTTPException(422, str(e)) from e
    except office_adapter.WordConvertError as e:
        doc.preview_pdf = None
        doc.preview_pages = None
        doc.preview_error = str(e)[:PREVIEW_ERROR_MAX]
        db.commit()
        raise HTTPException(422, str(e)) from e

    _clear_render_cache(doc.id)          # 数据源换了，旧的页面图必须作废
    doc.preview_pdf = config.rel_to_data(out)
    doc.preview_pages = pages
    doc.preview_error = None
    db.commit()
    return out


def _page_source(db: Session, doc_id: str, build: bool = True) -> Path:
    """页面视图（pages / image / lines / crop）统一走这里取数据源。"""
    doc = _doc_or_404(db, doc_id)
    if _is_paged(doc.filetype):
        return _source_file(db, doc_id)
    if not build:
        out = _preview_pdf_path(doc)
        if doc.preview_pdf and out.exists():
            return out
        raise HTTPException(422, doc.preview_error or "Word 页面图尚未生成，请先生成")
    return ensure_preview(db, doc)


def _preview_payload(db: Session, doc: Document) -> dict:
    """页面视图状态 —— 前端据此决定进页面视图还是回退内容块模式。"""
    is_pdf = _is_paged(doc.filetype)
    if is_pdf:
        return {"engine": "pdf", "ready": True, "pages": None, "error": None, "can_build": False}

    out = _preview_pdf_path(doc)
    ready = bool(doc.preview_pdf) and out.exists()
    return {
        "engine": "word",
        "ready": ready,
        "pages": doc.preview_pages if ready else None,
        "error": None if ready else (doc.preview_error or None),
        "can_build": not ready,
        "word_available": office_adapter.is_available(),
        "word_note": office_adapter.availability_note(),
    }


# ---------------------------------------------------------------- 上传 / 列表
@router.post("/documents")
async def upload_document(file: UploadFile = File(...), db: Session = Depends(get_db)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext == ".doc":
        raise HTTPException(422, "暂不支持旧版 .doc，请在 Word 中另存为 .docx 后重新上传")
    if ext not in (".pdf", ".docx", *PAGE_IMAGE_EXTS):
        raise HTTPException(
            422, "仅支持 PDF / Word(.docx) / 图片（png、jpg、jpeg、webp、bmp）文件"
        )

    save_path = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    with open(save_path, "wb") as f:
        f.write(await file.read())

    try:
        if ext == ".pdf":
            blocks = pdf_adapter.parse_blocks(str(save_path))
            # 无文字层但有页面 → 扫描版 PDF：允许上传，走纯截图模式
            scanned = 0 if blocks else 1
            if scanned and pdf_adapter.page_count(str(save_path)) == 0:
                raise HTTPException(422, "PDF 无有效页面，文件可能已损坏")
        elif ext in PAGE_IMAGE_EXTS:
            # 一张图片 = 一页。没有文字层，所以一定是「截图模式」，
            # 这也正是图片的用法：整页喂给 AI 整页识别（见 services/batch_page.py）。
            blocks = []
            scanned = 1
            if pdf_adapter.page_count(str(save_path)) == 0:
                raise HTTPException(422, "图片读不出页面，文件可能已损坏或格式不支持")
        else:
            from doc_parser import parse_docx  # 延迟导入，避免包外依赖影响导入期

            # Word 不再强求有文字：整卷是图片的 Word 也能用（在页面视图里框选即可）
            blocks = parse_docx(str(save_path))
            scanned = 0
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"解析失败: {e}") from e

    doc_id = uuid.uuid4().hex[:12]
    db.add(
        Document(
            id=doc_id,
            filename=file.filename,
            filetype=ext,
            block_count=len(blocks),
            blocks=json.dumps(blocks, ensure_ascii=False),
            created_at=datetime.now().isoformat(timespec="seconds"),
            file_path=config.rel_to_data(save_path),   # 库里只存相对路径
            scanned=scanned,
        )
    )
    db.commit()
    # 这里刻意不转 Word：上传要快。页面视图由前端在进入时显式触发生成，
    # 转换耗时（秒级）落在用户看得见的地方，而不是卡在「上传中」。
    return {
        "id": doc_id, "filename": file.filename, "filetype": ext,
        "block_count": len(blocks), "blocks": blocks, "scanned": scanned,
        "preview": _preview_payload(db, _doc_or_404(db, doc_id)),
    }


@router.get("/documents")
def list_documents(db: Session = Depends(get_db)):
    rows = db.scalars(select(Document).order_by(Document.created_at.desc())).all()
    return [
        {
            "id": d.id, "filename": d.filename, "filetype": d.filetype,
            "block_count": d.block_count, "scanned": d.scanned, "created_at": d.created_at,
            "preview_ready": bool(d.preview_pdf), "preview_pages": d.preview_pages,
        }
        for d in rows
    ]


@router.get("/documents/{doc_id}")
def get_document(doc_id: str, db: Session = Depends(get_db)):
    doc = _doc_or_404(db, doc_id)
    try:
        blocks = json.loads(doc.blocks or "[]")
    except json.JSONDecodeError:
        blocks = []
    return {
        "id": doc.id,
        "filename": doc.filename,
        "filetype": doc.filetype,
        "block_count": doc.block_count,
        "blocks": blocks,
        "scanned": doc.scanned,
        "created_at": doc.created_at,
        "preview": _preview_payload(db, doc),
    }


# ---------------------------------------------------------------- 页面视图状态 / 生成
@router.get("/documents/{doc_id}/preview-status")
def preview_status(doc_id: str, db: Session = Depends(get_db)):
    """纯读取，不触发转换 —— 前端打开文档时先问这个，再决定要不要显示「生成中」。"""
    return _preview_payload(db, _doc_or_404(db, doc_id))


@router.post("/documents/{doc_id}/build-preview")
def build_preview(
    doc_id: str,
    force: bool = Query(default=False, description="true = 丢弃旧结果重新生成"),
    db: Session = Depends(get_db),
):
    """生成（或重建）Word 的页面视图数据源。这是唯一会启动 Word 的接口。"""
    doc = _doc_or_404(db, doc_id)
    if _is_paged(doc.filetype):
        return {"ok": True, "skipped": "PDF / 图片无需转换", "preview": _preview_payload(db, doc)}
    try:
        path = ensure_preview(db, doc, force=force)
    except HTTPException as e:
        raise e
    db.refresh(doc)
    return {
        "ok": True,
        "page_count": pdf_adapter.page_count(str(path)),
        "preview": _preview_payload(db, doc),
    }


# ---------------------------------------------------------------- 页面视图
@router.get("/documents/{doc_id}/pages")
def doc_page_count(doc_id: str, db: Session = Depends(get_db)):
    try:
        return {"page_count": pdf_adapter.page_count(str(_page_source(db, doc_id)))}
    except HTTPException:
        raise
    except Exception as e:
        # 损坏的 PDF / 渲染库报错 —— 转成 4xx 并把原因说出来。
        # 直接抛出去会变成 500「Internal Server Error」，前端只能显示一行没有信息量的字。
        raise HTTPException(422, f"读不出这份文档的页面：{e}") from e


@router.get("/documents/{doc_id}/pages/{pno}/image")
def doc_page_image(doc_id: str, pno: int, db: Session = Depends(get_db)):
    path = _page_source(db, doc_id)
    cache = config.PAGES_CACHE / doc_id
    try:
        png = pdf_adapter.render_page(str(path), pno, str(cache))
    except ValueError as e:            # 页码越界，见 adapters/pdf.py 的 _page()
        raise HTTPException(422, str(e)) from e
    return FileResponse(png, media_type="image/png")


@router.get("/documents/{doc_id}/pages/{pno}/lines")
def doc_page_lines(doc_id: str, pno: int, db: Session = Depends(get_db)):
    """返回该页所有文字行及 bbox —— 前端「文本选择」靠它在框选区域里取文字。"""
    try:
        return pdf_adapter.page_lines(str(_page_source(db, doc_id)), pno)
    except ValueError as e:            # 同上：页码越界
        raise HTTPException(422, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        # 单页取不出来**不该拖垮整份文档** —— 前端逐页容错，会把它当「这页没有文字层」。
        # 这里给 4xx + 原因，方便在控制台看出是哪一页、为什么。
        raise HTTPException(422, f"第 {pno} 页读不出文字层：{e}") from e


@router.post("/documents/{doc_id}/crop")
def doc_crop(doc_id: str, c: CropIn, db: Session = Depends(get_db)):
    path = _page_source(db, doc_id)
    try:
        name = pdf_adapter.crop_region(str(path), c.page, [c.x0, c.y0, c.x1, c.y1], CROPS_DIR)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"url": f"/api/crops/{name}"}


@router.post("/documents/{doc_id}/crop-strip")
def doc_crop_strip(doc_id: str, payload: CropStripIn, db: Session = Depends(get_db)):
    """多区域（可跨页）竖拼成一张原貌图 —— 一道题跨页时用。"""
    path = _page_source(db, doc_id)
    regions = [(r.page, r.x0, r.y0, r.x1, r.y1) for r in payload.regions]
    try:
        name = pdf_adapter.crop_regions(str(path), regions, CROPS_DIR, gap=payload.gap)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"url": f"/api/crops/{name}"}


@router.get("/documents/{doc_id}/region-image")
def doc_region_image(
    doc_id: str,
    spec: str,
    zoom: float = Query(1.5, ge=0.5, le=3.0),
    db: Session = Depends(get_db),
):
    """把一个（或几个，跨页竖拼）区域渲染成 PNG **直接返回、不写盘**。

    给「分割线之间到底切成了什么」做即时预览：老师画完线，右栏每一块后面
    立刻能看到那块的样子，不用等提交。

    为什么必须是不落盘的：
        `data/uploads/crops/` 是**资产**目录 —— 里面每张图都有数据库行引用它，
        删的时候靠引用计数回收。预览图没有任何行引用，攒多了就是纯垃圾
        （实测踩过：题库 0 行、盘上 92 张图）。所以预览走内存直出。

    为什么用 GET + 查询串而不是 POST body：
        它要能直接当 `<img src="...">` 用 —— 浏览器才会替我们做缓存、
        懒加载与并发控制。POST 做不到这些。

    spec 形如 `1:40,90,550,300`（页: x0,y0,x1,y1），跨页用 `;` 连接：
        `1:40,540,550,800;2:40,90,550,320`
    """
    path = _page_source(db, doc_id)
    regions = _parse_region_spec(spec)
    try:
        png = pdf_adapter.render_region_png(str(path), regions, zoom=zoom)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return Response(
        content=png,
        media_type="image/png",
        # 同一份原件的同一块切出来是确定的 —— 让浏览器别重复问。
        # private：这是学生卷子，不进中间缓存。
        headers={"Cache-Control": "private, max-age=600"},
    )


def _parse_region_spec(spec: str) -> list[tuple[int, float, float, float, float]]:
    """把 `页:x0,y0,x1,y1` 用 `;` 连起来的串解析成区域列表。

    写错就 422 说清楚哪个片段不对 —— 前端拼 URL 的地方不止一处
    （块缩略图、配图框选），静默当成「空区域」会让图整片不显示却查不出原因。
    """
    out: list[tuple[int, float, float, float, float]] = []
    for part in (spec or "").split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            pno_s, coords = part.split(":", 1)
            nums = [float(x) for x in coords.split(",")]
            if len(nums) != 4:
                raise ValueError
            out.append((int(pno_s), nums[0], nums[1], nums[2], nums[3]))
        except ValueError as e:
            raise HTTPException(
                422, f"区域写法不对：{part}（应为 页:x0,y0,x1,y1）"
            ) from e
    if not out:
        raise HTTPException(422, "没有给出要预览的区域")
    return out


@router.post("/documents/{doc_id}/batch-crop")
def doc_batch_crop(doc_id: str, payload: BatchCropIn, db: Session = Depends(get_db)):
    """一次裁出**很多**题块的原貌图（批量入库用）。

    为什么不复用 /crop 逐个调：一次提交 30 个块，逐个走 HTTP 就是 30 次往返 +
    30 次「打开 PDF 文档」的开销（pymupdf 每次都要重新 open 同一个文件）。
    这里在同一个 _page_source 上循环裁，只有一轮往返。

    逐块容错：某一块裁不出来（越界、空区域）不该让整次提交失败 ——
    返回里那个位置的 error 非空，前端据此把它标红让老师重新画，其余照常入库。
    """
    path = _page_source(db, doc_id)
    out = []
    for i, block in enumerate(payload.blocks):
        regions = [(r.page, r.x0, r.y0, r.x1, r.y1) for r in block.regions]
        if not regions:
            out.append({"index": i, "url": None, "error": "空区域"})
            continue
        try:
            pno, x0, y0, x1, y1 = regions[0]
            if len(regions) == 1:
                name = pdf_adapter.crop_region(str(path), pno, [x0, y0, x1, y1], CROPS_DIR)
            else:
                name = pdf_adapter.crop_regions(str(path), regions, CROPS_DIR, gap=payload.gap)
            out.append({"index": i, "url": f"/api/crops/{name}", "error": None})
        except ValueError as e:
            out.append({"index": i, "url": None, "error": str(e)})
    ok = sum(1 for x in out if x["url"])
    return {"total": len(out), "ok": ok, "items": out}


@router.get("/crops/{name}")
def get_crop(name: str):
    p = config.CROPS_DIR / os.path.basename(name)
    if not p.exists():
        raise HTTPException(404, "截图不存在")
    # 按扩展名给 MIME，不写死 png —— 目录里将来混进别的格式也不用改这里
    media = mimetypes.guess_type(p.name)[0] or "image/png"
    return FileResponse(str(p), media_type=media)


# ---------------------------------------------------------------- 删除文档
def _unlink(path: Path) -> list[str]:
    """删一个文件，返回「真删掉了」的文件名。不存在或删不动都当没事发生。"""
    try:
        if path.is_file():
            path.unlink()
            return [path.name]
    except OSError:
        pass
    return []


def _delete_impact(db: Session, doc: Document) -> dict:
    """删这份文档会动到什么 —— 前端在确认框里先把账摊开给人看。

    两个数字都算的是「**真的**会少掉多少」：
      · 题目数 —— 挂在它下面的题（按 document_id 认，就是它）
      · 截图数 —— 这些题引用的截图，再扣掉别处也在用的（引用计数）
    宁可这里多查一次，也好过用户点完「删除」才发现带走了不该带的东西。
    """
    qs = db.scalars(select(Question).where(Question.document_id == doc.id)).all()
    mine: set[str] = set()
    for q in qs:
        mine |= crops.crop_names(q.image, q.answer_image)
    # 预演：把这几道题当作已经删掉，看还剩谁在引用这些图
    freed = mine - crops.referenced_crops(db, exclude_qids=[q.id for q in qs])

    files: list[dict] = []
    src = config.abs_from_data(doc.file_path)
    if src and Path(src).is_file():
        files.append({"kind": "source", "name": Path(src).name,
                      "bytes": Path(src).stat().st_size})
    conv = _preview_pdf_path(doc)
    if conv.is_file():
        files.append({"kind": "converted", "name": conv.name, "bytes": conv.stat().st_size})
    cache = config.PAGES_CACHE / doc.id
    if cache.is_dir():
        files.append({"kind": "pages", "name": f"{doc.id}/",
                      "bytes": sum(p.stat().st_size for p in cache.rglob("*") if p.is_file())})

    return {
        "id": doc.id,
        "filename": doc.filename,
        "filetype": doc.filetype,
        "created_at": doc.created_at,
        "questions": len(qs),
        "crops": len(freed),
        "files": files,
        "bytes": sum(f["bytes"] for f in files),
    }


@router.get("/documents/{doc_id}/delete-impact")
def delete_impact(doc_id: str, db: Session = Depends(get_db)):
    """纯读取：删之前先看会删掉什么。不写任何东西，可以随便点。"""
    return _delete_impact(db, _doc_or_404(db, doc_id))


@router.delete("/documents/{doc_id}")
def delete_document(
    doc_id: str,
    with_questions: bool = Query(False, description="true = 连同该文档下已录入的题目一起删"),
    db: Session = Depends(get_db),
):
    """删除一份卷子**及它落盘的每一处**。

    一份文档在磁盘上不止一个文件：原件、Word 转出的 PDF（页面视图的数据源）、
    页面渲染图缓存。以前只能去 data/ 目录里自己翻着删，漏掉哪一处都不会报错 ——
    表现是「下次打开还看到旧页面图」，或者几十 MB 悄悄占着。这里一次清干净。

    ⚠️ with_questions 默认 false：**删文档 ≠ 删题**。题目是独立资产 ——
    有自己的文本、截图，可能已经出过卷、有学生做过。把它们跟着文档一起带走，
    是用户没说过的事。真要一起删，得调用方明确表态（前端默认不勾）。
    不删题时会把题的 document_id 清空（来源名 doc_filename 保留），
    免得题库里留一堆指向已删文档的 id。

    删行之前先备份一次数据库：文件删了回不来，至少让「当时哪几道题挂在它下面」
    有据可查（备份在 设置 → 数据备份，超出保留份数自动清最旧的）。
    """
    doc = _doc_or_404(db, doc_id)
    filename = doc.filename
    src_path = config.abs_from_data(doc.file_path)
    conv_path = _preview_pdf_path(doc)

    questions = db.scalars(select(Question).where(Question.document_id == doc_id)).all()
    q_ids = [q.id for q in questions]

    # 会释放哪些截图必须在删行**之前**算 —— 删完就问不出来了
    freed: set[str] = set()
    if questions:
        mine: set[str] = set()
        for q in questions:
            mine |= crops.crop_names(q.image, q.answer_image)
        freed = mine - crops.referenced_crops(db, exclude_qids=q_ids)

    backup = ""
    try:
        made = backup_db("del-doc")
        backup = made.name if made else ""
    except Exception:            # noqa: BLE001
        pass                     # 备份失败不该挡住「用户想删东西」这件事

    if with_questions and q_ids:
        # 顺序不能反：question_nodes 有指向 questions 的外键，且是真生效的
        db.execute(delete(QuestionNode).where(QuestionNode.question_id.in_(q_ids)))
        db.execute(delete(Question).where(Question.id.in_(q_ids)))
    else:
        for q in questions:
            q.document_id = None
    db.execute(delete(Document).where(Document.id == doc_id))
    db.commit()

    # 库改完才动文件：中途失败也不会留下「文件没了、记录还在」的空壳
    files_removed: list[str] = []
    if src_path:
        files_removed += _unlink(Path(src_path))
    files_removed += _unlink(conv_path)
    # 页面缓存是一整个目录，且里面可能有 rmtree 删不掉的句柄 —— 忽略失败
    shutil.rmtree(config.PAGES_CACHE / doc_id, ignore_errors=True)

    return {
        "ok": True,
        "filename": filename,
        "questions_removed": len(q_ids) if with_questions else 0,
        "questions_kept": 0 if with_questions else len(q_ids),
        "crops_removed": crops.purge(freed),
        "files_removed": files_removed,
        "backup": backup,
    }
