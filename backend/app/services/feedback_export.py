# -*- coding: utf-8 -*-
"""课后反馈导出：txt / Word(.docx) / PDF。

三种格式的分工（以及为什么这么分）：
  · **txt** —— 最通用、任何设备都能看（家长微信里发一段文字最省事）。
    按需求，**txt 不带图片**：纯文本里塞图片路径只会变成一堆看不懂的字符。
  · **docx** —— 带配图，家长可打印、老师可再改。
  · **pdf** —— 先出 docx，再交给 adapters/office.py 用本机 Word 导出（版式最准）。
    没有 Word 时抛 PdfUnavailable，由路由翻成 503 并给可照做的提示。

内容组织：四段式（课堂表现 / 存在问题 / 作业布置 / 下次安排）+ 综合状态 + 能力评分 + 配图。
空字段**不输出标题**——留一堆「（空）」比不写更难看，这是给家长看的正式文本。
"""
from __future__ import annotations

import io
import re

from .. import config
from ..adapters import office

# 反馈导出的四个字段：(键, 中文标题)
SECTIONS = [
    ("performance", "课堂表现"),
    ("problems", "存在问题"),
    ("homework", "作业布置"),
    ("next_plan", "下次安排"),
]

# 图片 URL 形如 /api/feedbacks/files/fb_ab12.png
_IMG_URL_RE = re.compile(r"^/api/feedbacks/files/([A-Za-z0-9_.\-]+)$")

STAR_FULL, STAR_EMPTY = "★", "☆"


class PdfUnavailable(RuntimeError):
    """本机没有 Word，无法生成高保真 PDF。"""


def _stars(rating: int | None) -> str:
    if not rating:
        return ""
    n = max(0, min(5, int(rating)))
    return STAR_FULL * n + STAR_EMPTY * (5 - n)


def _meta_line(ctx: dict) -> str:
    """标题下面那行：学生 / 时间 / 课程 / 状态。缺失的项直接不出现。"""
    parts = []
    if ctx.get("student_name"):
        parts.append(f"学生：{ctx['student_name']}")
    if ctx.get("when"):
        parts.append(f"时间：{ctx['when']}")
    if ctx.get("topic"):
        parts.append(f"内容：{ctx['topic']}")
    st = _stars(ctx.get("rating"))
    if st:
        parts.append(f"综合状态：{st}")
    return "　　".join(parts)


def _score_lines(scores: list[dict]) -> list[str]:
    return [f"{s.get('name') or ('#' + str(s.get('dim_id')))}：{s.get('score')} / 5"
            for s in (scores or []) if s.get("score")]


def _image_paths(images: list[str]) -> list:
    """把 URL 换成磁盘文件。只接受本项目自己的 /api/feedbacks/files/ 形式，防路径穿越。"""
    out = []
    for url in images or []:
        m = _IMG_URL_RE.match(url or "")
        if not m:
            continue
        p = config.FEEDBACK_DIR / m.group(1)
        if p.exists() and p.is_file():
            out.append(p)
    return out


# ---------------------------------------------------------------- TXT
def build_txt(fb: dict, ctx: dict) -> bytes:
    """纯文本。刻意不带图片（需求明确：txt 不用图片）。"""
    lines: list[str] = ["课后反馈", "=" * 24]
    meta = _meta_line(ctx)
    if meta:
        lines.append(meta)
    lines.append("")

    for key, title in SECTIONS:
        text = (fb.get(key) or "").strip()
        if not text:
            continue
        lines.append(f"【{title}】")
        lines.extend(text.splitlines())
        lines.append("")

    scores = _score_lines(fb.get("ability_scores") or [])
    if scores:
        lines.append("【能力评分】")
        lines.extend("· " + s for s in scores)
        lines.append("")

    images = fb.get("images") or []
    if images:
        # 说清楚「有几张图、要去 Word/PDF 里看」，而不是默默丢掉
        lines.append(f"（本篇附有 {len(images)} 张图片，请查看 Word 或 PDF 版本）")
        lines.append("")

    text = "\n".join(lines).rstrip() + "\n"
    # utf-8-sig：带 BOM。不加的话 Windows 记事本、Excel 打开中文可能乱码 ——
    # 这个文件是要发给家长/贴在报告里的，不能出现「打开是乱码」这种事。
    return text.encode("utf-8-sig")


# ---------------------------------------------------------------- DOCX / PDF
def build_docx(fb: dict, ctx: dict) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.0)
    sec.top_margin = sec.bottom_margin = Cm(2.0)
    content_cm = 21.0 - 2.0 * 2

    # python-docx 默认西文字体，中文会走回退。必须显式写 w:eastAsia。
    normal = doc.styles["Normal"]
    normal.font.size = Pt(11.5)
    rf = normal.element.get_or_add_rPr().get_or_add_rFonts()
    rf.set(qn("w:eastAsia"), "宋体")
    rf.set(qn("w:ascii"), "Times New Roman")
    rf.set(qn("w:hAnsi"), "Times New Roman")

    tp = doc.add_paragraph()
    tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = tp.add_run("课后反馈")
    tr.bold = True
    tr.font.size = Pt(18)

    meta = _meta_line(ctx)
    if meta:
        mp = doc.add_paragraph()
        mp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        mr = mp.add_run(meta)
        mr.font.size = Pt(10)
        mr.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    for key, title in SECTIONS:
        text = (fb.get(key) or "").strip()
        if not text:
            continue
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(10)
        h.paragraph_format.space_after = Pt(2)
        hr = h.add_run(f"【{title}】")
        hr.bold = True
        hr.font.size = Pt(12)
        for line in text.splitlines():
            p = doc.add_paragraph(line)
            p.paragraph_format.space_after = Pt(2)

    scores = _score_lines(fb.get("ability_scores") or [])
    if scores:
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(10)
        h.paragraph_format.space_after = Pt(2)
        hr = h.add_run("【能力评分】")
        hr.bold = True
        hr.font.size = Pt(12)
        for s in scores:
            p = doc.add_paragraph("· " + s)
            p.paragraph_format.space_after = Pt(0)

    pics = _image_paths(fb.get("images") or [])
    if pics:
        h = doc.add_paragraph()
        h.paragraph_format.space_before = Pt(10)
        h.paragraph_format.space_after = Pt(2)
        hr = h.add_run("【附图】")
        hr.bold = True
        hr.font.size = Pt(12)
        for path in pics:
            # 按可用宽度放，同时避免竖长图撑破一页
            try:
                import pymupdf
                with pymupdf.open(str(path)) as im:
                    pw, ph = im[0].rect.width, im[0].rect.height
                ratio = (ph / pw) if pw else 1.0
            except Exception:  # noqa: BLE001  图片坏了不该让整篇导不出来
                ratio = 1.0
            width_cm = content_cm * 0.9
            max_h = 29.7 - 2.0 * 2 - 3.0
            if width_cm * ratio > max_h:
                width_cm = max_h / max(ratio, 0.01)
            doc.add_picture(str(path), width=Cm(width_cm))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_pdf(fb: dict, ctx: dict) -> bytes:
    """docx -> Word -> PDF。没有 Word 时抛 PdfUnavailable。"""
    docx_bytes = build_docx(fb, ctx)
    try:
        return office.docx_bytes_to_pdf(docx_bytes)
    except office.OfficeUnavailable as e:
        raise PdfUnavailable(
            "导出 PDF 需要本机装有 Microsoft Word（服务端用 Word 排版）。"
            f"当前不可用：{e}。可以先「导出 Word」，再用 Word 另存为 PDF。"
        ) from e
    except office.WordConvertError as e:
        raise PdfUnavailable(str(e)) from e


def safe_filename(ctx: dict, ext: str) -> str:
    """文件名带上学生和日期，家长收到一眼能认出来。"""
    name = ctx.get("student_name") or "学生"
    when = (ctx.get("when") or "")[:10]
    base = f"课后反馈-{name}-{when}" if when else f"课后反馈-{name}"
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", base).strip(" .") or "课后反馈"
    return f"{base[:60]}.{ext}"
