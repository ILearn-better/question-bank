# -*- coding: utf-8 -*-
"""文档解析器：把 PDF / Word 试卷解析成内容块序列，供前端手动分割成题目。"""
import pymupdf  # PyMuPDF
from docx import Document as DocxDocument


def parse_pdf(path: str) -> list:
    """解析 PDF：按 PyMuPDF 的文本块切分，保留页码，图片块标记占位。"""
    blocks = []
    with pymupdf.open(path) as doc:
        for pno, page in enumerate(doc, start=1):
            for b in page.get_text("blocks"):
                # b = (x0, y0, x1, y1, text, block_no, block_type)  block_type: 0=文本 1=图片
                text = (b[4] or "").strip()
                if b[6] == 1:
                    blocks.append({"page": pno, "type": "image", "text": "[图片]"})
                elif text:
                    blocks.append({"page": pno, "type": "text", "text": text})
    return blocks


def parse_docx(path: str) -> list:
    """解析 docx：按段落切分，检测段内图片，表格追加在末尾（暂不保持与正文的相对顺序）。"""
    doc = DocxDocument(path)
    blocks = []
    for p in doc.paragraphs:
        xml = p._p.xml
        has_image = ("graphicData" in xml) or ("<pic:pic" in xml)
        if has_image:
            blocks.append({"page": None, "type": "image", "text": "[图片]"})
        text = p.text.strip()
        if text:
            blocks.append({"page": None, "type": "text", "text": text})
    for table in doc.tables:
        rows = [" | ".join(c.text.strip() for c in row.cells) for row in table.rows]
        blocks.append({"page": None, "type": "table", "text": "[表格]\n" + "\n".join(rows)})
    return blocks
