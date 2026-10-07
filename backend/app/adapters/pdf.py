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


def _page(doc, pno: int):
    """取第 pno 页（1-based），**显式校验范围**，越界统一抛 ValueError。

    为什么必须自己校验（踩过）：
      · pymupdf 的 `doc[i]` 对负索引走 Python 语义 —— `doc[-1]` 是最后一页。
        所以 pno=0 / 负数**不报错**，而是悄悄返回另一页；更糟的是渲染结果照样
        按 `p{pno}.png` 落进页面缓存，污染磁盘。
      · 超出上界则抛 IndexError，没人接就是 HTTP 500 + 一屏 traceback。
    两种结果都不对：越界就该明确报错，由路由层翻译成 422。
    """
    if not (1 <= pno <= doc.page_count):
        raise ValueError(f"页码 {pno} 超出范围（本文档共 {doc.page_count} 页）")
    return doc[pno - 1]


def render_page(path: str | os.PathLike, pno: int, cache_dir: str | os.PathLike, zoom: float = 2.0) -> str:
    """渲染第 pno 页（1-based）为 PNG，带磁盘缓存。返回 PNG 文件路径。"""
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, f"p{pno}.png")
    if os.path.exists(out):
        return out
    with pymupdf.open(path) as doc:
        page = _page(doc, pno)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        pix.save(out)
    return out


def page_lines(path: str | os.PathLike, pno: int) -> dict:
    """提取第 pno 页所有文字行及 bbox，供前端吸附分界线与拼接题目文本。"""
    with pymupdf.open(path) as doc:
        page = _page(doc, pno)
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
def _clip_pixmaps(path: str | os.PathLike, regions, zoom: float) -> list:
    """把若干区域渲染成 pixmap 列表。越界的页、空区域**跳过**（不抛错）。

    单段裁剪与跨页拼接共用这一份取图逻辑 —— 两处各写一遍的话，
    「区域要 & page.rect 收进页内」这种细节迟早只在一处生效。
    """
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
    return pixmaps


def _stack_png(pixmaps, gap: int) -> bytes:
    """把若干 pixmap 竖着拼成一张 PNG 的**字节**。只有一张时直接返回它。

    拼接做法：新建一个 PDF 页，把各区域图按顺序 insert_image 进去再整体渲染。
    不用 Pixmap.copy/set_origin —— 那套 API 的定位语义各家版本行为不一，
    实测会贴出一张全白的图（真实踩过的坑），而 insert_image 的行为是文档化的。
    """
    if len(pixmaps) == 1:
        return pixmaps[0].tobytes("png")

    width = max(p.width for p in pixmaps)
    height = sum(p.height for p in pixmaps) + gap * (len(pixmaps) - 1)

    out_doc = pymupdf.open()
    try:
        page = out_doc.new_page(width=width, height=height)
        y = 0
        for p in pixmaps:
            # 统一成无 alpha 的 RGB，否则 insert_image 会因色彩空间不一致而报错
            rgb = p if (p.alpha == 0 and p.colorspace == pymupdf.csRGB) else pymupdf.Pixmap(pymupdf.csRGB, p)
            page.insert_image(pymupdf.Rect(0, y, rgb.width, y + rgb.height), pixmap=rgb)
            y += rgb.height + gap
        return page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).tobytes("png")
    finally:
        out_doc.close()


def crop_region(
    path: str | os.PathLike, pno: int, rect, out_dir: str | os.PathLike, zoom: float = 3.0
) -> str:
    """按 PDF 坐标矩形裁剪高清区域图（默认 3 倍分辨率，打印不糊）。返回文件名。"""
    return crop_regions(path, [(pno, rect[0], rect[1], rect[2], rect[3])], out_dir, zoom=zoom)


def crop_regions(
    path: str | os.PathLike, regions, out_dir: str | os.PathLike, zoom: float = 3.0, gap: int = 14
) -> str:
    """把多个区域（可跨页）竖着拼成一张图，**落盘**并返回文件名。

    用途：一道题跨了页（Word/PDF 里很常见，尤其解答题）。
    如果按「一题一张原貌图」来做，跨页题就必须存两张 —— 但题目只有一个 image 字段。
    这里直接拼成一张，跨页题的原貌图也就是一张，数据模型不用动。

    regions 形如 [(page, x0, y0, x1, y1), ...]。
    """
    os.makedirs(out_dir, exist_ok=True)
    pixmaps = _clip_pixmaps(path, regions, zoom)
    if not pixmaps:
        raise ValueError("裁剪区域为空")
    name = f"{uuid.uuid4().hex[:12]}.png"
    with open(os.path.join(out_dir, name), "wb") as fh:
        fh.write(_stack_png(pixmaps, gap))
    return name


def render_region_png(
    path: str | os.PathLike, regions, zoom: float = 1.5, gap: int = 14
) -> bytes:
    """渲染若干区域并竖拼成 PNG，**不落盘**，直接返回字节。

    这是给「预览」用的，与 crop_regions 的关键差别就是**不写文件**：
    老师画完分界线要马上看到这一块切出来什么样，但这时还没提交 ——
    `data/uploads/crops/` 是提交后的资产目录，每张图都要靠引用计数才有
    清理依据。往里塞一堆没人引用的预览图，等于给未来的自己挖坑
    （实测过一次：题库 0 行、盘上 92 张图）。

    分辨率也低一档（默认 1.5 倍）：缩略图看的是「切得对不对」，不是印刷质量。
    """
    pixmaps = _clip_pixmaps(path, regions, zoom)
    if not pixmaps:
        raise ValueError("区域为空，或超出了页面范围")
    return _stack_png(pixmaps, gap)


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
