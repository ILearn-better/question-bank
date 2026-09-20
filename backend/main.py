# -*- coding: utf-8 -*-
"""题库工具 - 文档手动分割服务
功能：
  1. 上传 PDF/Word -> 解析成内容块 -> 前端选范围切分成题目 -> 保存题目
  2. PDF 专属：页面渲染成图 + 带坐标文字行 -> 前端点击分界线分割 / 框选截图 -> 图片题
"""
import json
import os
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import doc_parser
import pdf_pages

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
DB_PATH = os.path.join(BASE_DIR, "app.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
PAGES_CACHE = os.path.join(UPLOAD_DIR, "pages")   # 页面渲染图缓存
CROPS_DIR = os.path.join(UPLOAD_DIR, "crops")     # 框选截图/区域存档
FRONTEND_DIR = os.path.join(ROOT, "frontend")
TREE_PATH = os.path.join(ROOT, "data", "math-knowledge-tree.json")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="专属题库工具 - PDF/Word 手动分割")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------- 数据库 ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents(
            id TEXT PRIMARY KEY,
            filename TEXT,
            filetype TEXT,
            block_count INTEGER,
            blocks TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS questions(
            id TEXT PRIMARY KEY,
            document_id TEXT,
            doc_filename TEXT,
            start_block INTEGER,
            end_block INTEGER,
            content TEXT,
            qtype TEXT,
            difficulty TEXT,
            knowledge_points TEXT,
            answer TEXT,
            analysis TEXT,
            created_at TEXT
        );
        """
    )
    # 平滑加列：旧库升级（列已存在时报错直接忽略）
    for ddl in (
        "ALTER TABLE documents ADD COLUMN file_path TEXT",
        "ALTER TABLE documents ADD COLUMN scanned INTEGER DEFAULT 0",
        "ALTER TABLE questions ADD COLUMN image TEXT DEFAULT ''",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


init_db()


# ---------- 模型 ----------
class QuestionIn(BaseModel):
    document_id: str
    doc_filename: str = ""
    start_block: int = -1   # 页面分割模式为 -1
    end_block: int = -1
    content: str = ""
    qtype: str = "解答题"
    difficulty: str = "中档"
    knowledge_points: List[str] = []
    answer: str = ""
    analysis: str = ""
    image: str = ""         # 图片题 / 原貌存档的 URL


class CropIn(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


def _doc_file(doc_id: str, need_pdf: bool = True) -> str:
    """查文档原始文件路径；PDF 页面类接口只对 .pdf 且文件仍在的文档开放。"""
    conn = get_db()
    row = conn.execute(
        "SELECT file_path, filetype FROM documents WHERE id=?", (doc_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "文档不存在")
    if need_pdf and row["filetype"] != ".pdf":
        raise HTTPException(422, "仅 PDF 支持页面分割，Word 请使用内容块模式")
    if not row["file_path"] or not os.path.exists(row["file_path"]):
        raise HTTPException(422, "该文档缺少原始文件（早期上传的记录），请重新上传后再用页面分割")
    return row["file_path"]


# ---------- 文档接口 ----------
@app.post("/api/documents")
async def upload_document(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext == ".doc":
        raise HTTPException(422, "暂不支持旧版 .doc，请在 Word 中另存为 .docx 后重新上传")
    if ext not in (".pdf", ".docx"):
        raise HTTPException(422, "仅支持 PDF 或 Word(.docx) 文件")

    save_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}{ext}")
    with open(save_path, "wb") as f:
        f.write(await file.read())

    try:
        if ext == ".pdf":
            blocks = doc_parser.parse_pdf(save_path)
            # 无文字层但有页面 → 扫描版 PDF：允许上传，不做文字解析，前端走纯截图模式
            scanned = 0 if blocks else 1
            if scanned and pdf_pages.page_count(save_path) == 0:
                raise HTTPException(422, "PDF 无有效页面，文件可能已损坏")
        else:
            blocks = doc_parser.parse_docx(save_path)
            scanned = 0
            if not blocks:
                raise HTTPException(422, "未能从 Word 文档中解析出文本内容（空文档？）")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"解析失败: {e}")

    doc_id = uuid.uuid4().hex[:12]
    conn = get_db()
    conn.execute(
        "INSERT INTO documents(id, filename, filetype, block_count, blocks, created_at, file_path, scanned)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (doc_id, file.filename, ext, len(blocks),
         json.dumps(blocks, ensure_ascii=False),
         datetime.now().isoformat(timespec="seconds"),
         save_path, scanned),
    )
    conn.commit()
    conn.close()
    return {"id": doc_id, "filename": file.filename, "filetype": ext,
            "block_count": len(blocks), "blocks": blocks, "scanned": scanned}


@app.get("/api/documents")
def list_documents():
    conn = get_db()
    rows = conn.execute(
        "SELECT id, filename, filetype, block_count, scanned, created_at FROM documents ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "文档不存在")
    d = dict(row)
    d["blocks"] = json.loads(d["blocks"])
    d.pop("file_path", None)
    return d


# ---------- PDF 页面接口（视觉分割） ----------
@app.get("/api/documents/{doc_id}/pages")
def pdf_page_count(doc_id: str):
    return {"page_count": pdf_pages.page_count(_doc_file(doc_id))}


@app.get("/api/documents/{doc_id}/pages/{pno}/image")
def pdf_page_image(doc_id: str, pno: int):
    path = _doc_file(doc_id)
    cache = os.path.join(PAGES_CACHE, doc_id)
    return FileResponse(pdf_pages.render_page(path, pno, cache), media_type="image/png")


@app.get("/api/documents/{doc_id}/pages/{pno}/lines")
def pdf_page_lines(doc_id: str, pno: int):
    return pdf_pages.page_lines(_doc_file(doc_id), pno)


@app.post("/api/documents/{doc_id}/crop")
def pdf_crop(doc_id: str, c: CropIn):
    path = _doc_file(doc_id)
    try:
        name = pdf_pages.crop_region(path, c.page, [c.x0, c.y0, c.x1, c.y1], CROPS_DIR)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"url": f"/api/crops/{name}"}


@app.get("/api/crops/{name}")
def get_crop(name: str):
    p = os.path.join(CROPS_DIR, os.path.basename(name))
    if not os.path.exists(p):
        raise HTTPException(404, "截图不存在")
    return FileResponse(p, media_type="image/png")


# ---------- 题目接口 ----------
@app.post("/api/questions")
def create_question(q: QuestionIn):
    if not q.content.strip() and not q.image:
        raise HTTPException(422, "题目内容不能为空（文本或截图至少一项）")
    qid = uuid.uuid4().hex[:12]
    conn = get_db()
    conn.execute(
        "INSERT INTO questions(id, document_id, doc_filename, start_block, end_block,"
        " content, qtype, difficulty, knowledge_points, answer, analysis, image, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (qid, q.document_id, q.doc_filename, q.start_block, q.end_block,
         q.content, q.qtype, q.difficulty,
         json.dumps(q.knowledge_points, ensure_ascii=False),
         q.answer, q.analysis, q.image,
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()
    return {"id": qid}


@app.get("/api/questions")
def list_questions(document_id: Optional[str] = None):
    conn = get_db()
    if document_id:
        rows = conn.execute(
            "SELECT * FROM questions WHERE document_id=? ORDER BY created_at", (document_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM questions ORDER BY created_at DESC").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["knowledge_points"] = json.loads(d["knowledge_points"])
        result.append(d)
    return result


@app.delete("/api/questions/{qid}")
def delete_question(qid: str):
    conn = get_db()
    conn.execute("DELETE FROM questions WHERE id=?", (qid,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ---------- 知识点树 ----------
def _to_cascader(node: dict, path: str = "") -> dict:
    label = node.get("name", "")
    value = f"{path}/{label}" if path else label
    item = {"value": value, "label": label}
    children = node.get("children") or []
    if children:
        item["children"] = [_to_cascader(c, value) for c in children]
    return item


@app.get("/api/tree")
def knowledge_tree():
    with open(TREE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return [_to_cascader(c) for c in data.get("children", [])]


# ---------- 前端 ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
