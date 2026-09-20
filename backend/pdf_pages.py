# -*- coding: utf-8 -*-
"""PDF 页面服务：渲染页面为图片、提取带坐标的文字行、按坐标高清裁剪。

坐标约定：全部使用 PDF 点坐标（左上角为原点，y 向下），与 PyMuPDF 一致。
前端按百分比换算显示，裁剪时前端把像素坐标换算回 PDF 坐标再提交。
"""
import os
import uuid

import pymupdf


def page_count(path: str) -> int:
    with pymupdf.open(path) as doc:
        return doc.page_count


def render_page(path: str, pno: int, cache_dir: str, zoom: float = 2.0) -> str:
    """渲染第 pno 页（1-based）为 PNG，带磁盘缓存。返回 PNG 文件路径。"""
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, f"p{pno}.png")
    if os.path.exists(out):
        return out
    with pymupdf.open(path) as doc:
        page = doc[pno - 1]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        pix.save(out)
    return out


def page_lines(path: str, pno: int) -> dict:
    """提取第 pno 页所有文字行及 bbox，供前端吸附分界线与拼接题目文本。"""
    with pymupdf.open(path) as doc:
        page = doc[pno - 1]
        d = page.get_text("dict")
        lines = []
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            for ln in block.get("lines", []):
                text = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if text:
                    x0, y0, x1, y1 = ln["bbox"]
                    lines.append({
                        "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                        "text": text,
                    })
        lines.sort(key=lambda l: l["bbox"][1])
        return {
            "page": pno,
            "width": round(page.rect.width, 1),
            "height": round(page.rect.height, 1),
            "lines": lines,
        }


def crop_region(path: str, pno: int, rect, out_dir: str, zoom: float = 3.0) -> str:
    """按 PDF 坐标矩形裁剪高清区域图（默认 3 倍分辨率，打印不糊）。返回文件名。"""
    os.makedirs(out_dir, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}.png"
    out = os.path.join(out_dir, name)
    x0, y0, x1, y1 = rect
    clip = pymupdf.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    with pymupdf.open(path) as doc:
        page = doc[pno - 1]
        clip = clip & page.rect  # 限制在页面范围内
        if clip.is_empty:
            raise ValueError("裁剪区域为空")
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
        pix.save(out)
    return name
