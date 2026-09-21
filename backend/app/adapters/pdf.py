# -*- coding: utf-8 -*-
"""PDF 能力适配层 —— **全项目唯一 import pymupdf 的地方**。

⚠️ 许可约束（开发文档 §4.1 / §12.3）：
    PyMuPDF 是 **AGPL-3.0**，商业使用需向 Artifex 购买授权。
    自用无碍；一旦要交付给别人使用，必须二选一：
      a) 购买商业授权，或
      b) 换成宽松许可的实现（pypdf / pypdfium2 等）
    把调用点收敛在这个文件里，将来 (b) 方案付出的代价是「改一个文件」，
    而不是「在全项目里找 30 处调用」。

坐标约定：一律使用 PDF 点坐标（左上角为原点，y 向下），与 PyMuPDF 一致。
前端按百分比换算显示，裁剪时把像素坐标换算回 PDF 坐标再提交。
"""
from __future__ import annotations

import os
import uuid

import pymupdf


# ---------------------------------------------------------------- 页面信息
def page_count(path: str | os.PathLike) -> int:
    with pymupdf.open(path) as doc:
        return doc.page_count


def render_page(path: str | os.PathLike, pno: int, cache_dir: str | os.PathLike, zoom: float = 2.0) -> str:
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


def page_lines(path: str | os.PathLike, pno: int) -> dict:
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


# ---------------------------------------------------------------- 裁剪
def crop_region(
    path: str | os.PathLike, pno: int, rect, out_dir: str | os.PathLike, zoom: float = 3.0
) -> str:
    """按 PDF 坐标矩形裁剪高清区域图（默认 3 倍分辨率，打印不糊）。返回文件名。"""
    os.makedirs(out_dir, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}.png"
    out = os.path.join(out_dir, name)
    x0, y0, x1, y1 = rect
    clip = pymupdf.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    with pymupdf.open(path) as doc:
        page = doc[pno - 1]
        clip = clip & page.rect          # 限制在页面范围内
        if clip.is_empty:
            raise ValueError("裁剪区域为空")
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
        pix.save(out)
    return name


def crop_regions(
    path: str | os.PathLike, regions, out_dir: str | os.PathLike, zoom: float = 3.0, gap: int = 14
) -> str:
    """把多个区域（可跨页）竖着拼成一张图。返回文件名。

    用途：一道题跨了页（Word/PDF 里很常见，尤其解答题）。
    如果按「一题一张原貌图」来做，跨页题就必须存两张 —— 但题目只有一个 image 字段。
    这里直接拼成一张，跨页题的原貌图也就是一张，数据模型不用动。

    regions 形如 [(page, x0, y0, x1, y1), ...]。
    """
    os.makedirs(out_dir, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}.png"
    out = os.path.join(out_dir, name)

    pixmaps = []
    with pymupdf.open(path) as doc:
        for pno, x0, y0, x1, y1 in regions:
            if not (1 <= pno <= doc.page_count):
                continue
            page = doc[pno - 1]
            clip = pymupdf.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)) & page.rect
            if clip.is_empty:
                continue
            pixmaps.append(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip))

    if not pixmaps:
        raise ValueError("裁剪区域为空")
    if len(pixmaps) == 1:
        pixmaps[0].save(out)
        return name

    # 拼接做法：新建一个 PDF 页，把各区域图按顺序 insert_image 进去再整体渲染。
    # 不用 Pixmap.copy/set_origin —— 那套 API 的定位语义各家版本行为不一，
    # 实测会贴出一张全白的图（真实踩过的坑），而 insert_image 的行为是文档化的。
    width = max(p.width for p in pixmaps)
    height = sum(p.height for p in pixmaps) + gap * (len(pixmaps) - 1)

    out_doc = pymupdf.open()
    page = out_doc.new_page(width=width, height=height)
    y = 0
    for p in pixmaps:
        # 统一成无 alpha 的 RGB，否则 insert_image 会因色彩空间不一致而报错
        rgb = p if (p.alpha == 0 and p.colorspace == pymupdf.csRGB) else pymupdf.Pixmap(pymupdf.csRGB, p)
        page.insert_image(pymupdf.Rect(0, y, rgb.width, y + rgb.height), pixmap=rgb)
        y += rgb.height + gap
    result = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False)
    result.save(out)
    out_doc.close()
    return name


# ---------------------------------------------------------------- 内容块解析
def parse_blocks(path: str | os.PathLike) -> list[dict]:
    """按文本块切分 PDF，保留页码；图片块标记占位。

    无文字层（扫描版）时返回空列表 —— 调用方据此进入截图模式。
    """
    blocks: list[dict] = []
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
