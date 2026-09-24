# -*- coding: utf-8 -*-
"""出卷导出：把选中的题目排成 A4 试卷，输出 HTML / Word / PDF。

三种格式对应三种用途，不是同一份内容导三遍：
  · HTML —— 保真度最高。图片以 base64 内嵌，导出的单个文件发给谁都还能看；
            $…$ 交给浏览器里的 KaTeX 渲染，Ctrl+P 直接就能存成 PDF。
  · DOCX —— 可编辑。老师要改题干、加批注、套学校抬头时用。
  · PDF  —— 直接打印。服务端用 PyMuPDF 排 A4，中文用内置 china-s 字体。

⚠️ 关于公式的实话：服务端没有 LaTeX 排版引擎，**DOCX / PDF 里的 $…$ 会原样输出**。
   想要公式排版就出 HTML。这点不藏着 —— 假装支持比不支持更糟。

本模块只吃「已经整理好的题目 dict」，不碰 ORM / Session，
因此不依赖数据库，可以直接单独调用来验证排版。
"""
from __future__ import annotations

import base64
import html as html_mod
import mimetypes
import re
from pathlib import Path

import pymupdf

from .. import config

# A4（pt）
A4_W, A4_H = 595.28, 841.89
MARGIN = 50.0
CONTENT_W = A4_W - MARGIN * 2

# 服务端没有公式排版引擎，HTML 里靠它把 $…$ 渲染出来（与录题页用的是同一套）
KATEX_CSS = "https://unpkg.com/katex@0.16.9/dist/katex.min.css"
KATEX_JS = "https://unpkg.com/katex@0.16.9/dist/katex.min.js"
KATEX_AUTO = "https://unpkg.com/katex@0.16.9/dist/contrib/auto-render.min.js"


# ---------------------------------------------------------------- 公共小工具
def image_path(url: str | None) -> Path | None:
    """把库里存的 URL（形如 /api/crops/xxx.png）还原成本机文件路径。"""
    if not url:
        return None
    p = config.CROPS_DIR / url.rsplit("/", 1)[-1]
    return p if p.exists() else None


def _data_uri(path: Path) -> str:
    """图片转 base64：导出的 HTML 必须是自包含的单个文件，否则发给别人图片全丢。"""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _esc(s: str | None) -> str:
    return html_mod.escape(s or "")


# 题库里的题干是照原文整行存的，通常自带原卷题号（"1. xxx" / "2、xxx" / "（3）xxx"）。
# 出卷时我们要按卷面顺序重新编号（而且用户还能上下移动题目），所以得先摘掉原文题号，
# 否则印出来就是「1. 1. xxx」；移动过题目后原文题号更是错的。
# 只摘开头那一个，而且**必须带分隔符**：光看数字会把 "2026 年…" 这种正文误伤。
# "." / "．" 还要加 (?!\d)，不然 "1.5 千米…" 会被啃成 "5 千米…"。
_LEADING_NO = re.compile(
    r"^\s*(?:"
    r"[（(]\s*\d{1,3}\s*[)）]"      # （1）/ (1)
    r"|\d{1,3}\s*[、)）]"           # 1、/ 1）/ 1)
    r"|\d{1,3}\s*[.．](?!\d)"       # 1. / 1．，但 1.5 不算
    r")\s*"
)


def _stem_text(it: dict) -> str:
    """题干文字。没有文字（纯截图题）时返回空串，调用方自行跳过。"""
    s = (it.get("content") or "").strip()
    return _LEADING_NO.sub("", s, count=1)


def _tag_line(it: dict) -> str:
    """题号后面那行小字：题型·难度，有标签再挂上去。

    抽成函数是因为 HTML / DOCX / PDF 三处都要用 —— 各写一遍迟早不一致
    （出卷选项写的是「标注题型·难度·标签」，三处输出必须一样）。
    """
    line = "·".join(b for b in (it.get("qtype"), it.get("difficulty")) if b)
    tags = [t for t in (it.get("tags") or []) if t]
    if tags:
        line = f"{line}｜{'/'.join(tags)}" if line else "/".join(tags)
    return line


# ================================================================ HTML
# ⚠️ 下面两个模板是给 str.format() 用的：CSS / JS 里的花括号**必须写成双份**，
#    漏一个就会抛 "unexpected '{' in field name"。改样式时别忘了这条。
_HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="{katex_css}">
<style>
  /* 屏幕上是普通网页，打印时按 A4 排版 —— 一个文件两种用法 */
  @page {{ size: A4; margin: 16mm 14mm; }}
  body {{ font-family: "Songti SC", "SimSun", "Microsoft YaHei", serif;
         font-size: 11.5pt; line-height: 1.8; color: #000; margin: 0; }}
  h1.title {{ font-size: 16pt; text-align: center; margin: 0 0 6pt; }}
  .fill {{ display: flex; justify-content: space-between; font-size: 10pt;
          border-bottom: 1px solid #000; padding-bottom: 4pt; margin-bottom: 14pt; }}
  .q {{ margin-bottom: 14pt; break-inside: avoid; page-break-inside: avoid; }}
  .q .stem {{ white-space: pre-wrap; }}
  .q img {{ max-width: 100%; display: block; margin: 6pt 0; }}
  .tag {{ font-size: 9pt; color: #666; }}
  .ans {{ break-before: page; page-break-before: always; }}
  .ans .item {{ margin-bottom: 10pt; white-space: pre-wrap; }}
  .ans img {{ max-width: 55%; display: block; margin: 4pt 0; }}
  @media screen {{
    body {{ max-width: 820px; margin: 24px auto 60px; padding: 0 20px; }}
    .ans {{ border-top: 1px dashed #bbb; padding-top: 16px; }}
    .no-print {{ text-align: right; margin-bottom: 10px; }}
  }}
  @media print {{ .no-print {{ display: none; }} }}
</style>
</head>
<body>
<div class="no-print"><button onclick="window.print()">打印 / 存为 PDF</button></div>
"""

_HTML_TAIL = """<script src="{katex_js}"></script>
<script src="{katex_auto}"></script>
<script>
// 公式渲染。没网时 KaTeX 加载不出来，$…$ 会以原文显示 —— 题目本身不受影响。
if (window.renderMathInElement) {{
  renderMathInElement(document.body, {{
    delimiters: [{{ left: '$$', right: '$$', display: true }},
                 {{ left: '$', right: '$', display: false }}],
    throwOnError: false
  }});
}}
</script>
</body>
</html>
"""


def build_html(title: str, items: list[dict], opts: dict) -> str:
    out = [_HTML_HEAD.format(title=_esc(title), katex_css=KATEX_CSS)]
    out.append(f'<h1 class="title">{_esc(title)}</h1>')
    if opts.get("show_meta", True):
        out.append('<div class="fill"><span>姓名：____________</span>'
                   '<span>班级：____________</span><span>日期：____________</span></div>')

    for it in items:
        out.append('<div class="q">')
        tag = ""
        if opts.get("show_tags", True):
            line = _tag_line(it)
            if line:
                tag = f' <span class="tag">（{_esc(line)}）</span>'
        stem = _esc(_stem_text(it))
        out.append(f'<div class="stem"><b>{it["n"]}.</b> {stem}{tag}</div>')
        p = image_path(it.get("image"))
        if p:
            out.append(f'<img src="{_data_uri(p)}" alt="">')
        out.append('</div>')

    if opts.get("show_answer"):
        out.append('<div class="ans">')
        out.append('<h1 class="title" style="font-size:14pt">参考答案与解析</h1>')
        for it in items:
            out.append(f'<div class="item"><b>{it["n"]}.</b> {_esc(it.get("answer")) or "（未填）"}')
            if opts.get("show_analysis") and it.get("analysis"):
                out.append(f'<div>解析：{_esc(it["analysis"])}</div>')
            ap = image_path(it.get("answer_image"))
            if ap:
                out.append(f'<img src="{_data_uri(ap)}" alt="">')
            out.append('</div>')
        out.append('</div>')

    out.append(_HTML_TAIL.format(katex_js=KATEX_JS, katex_auto=KATEX_AUTO))
    return "".join(out)


# ================================================================ DOCX
def build_docx(title: str, items: list[dict], opts: dict) -> bytes:
    import io

    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(1.6)
    sec.top_margin = sec.bottom_margin = Cm(1.8)
    content_cm = 21.0 - 1.6 * 2

    # python-docx 默认是西文字体，中文会走回退字体（宋体/雅黑都不一定），
    # 必须显式写 w:eastAsia，否则 Word 里排版和预期不一样。
    normal = doc.styles["Normal"]
    normal.font.size = Pt(11.5)
    rfonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), "宋体")
    rfonts.set(qn("w:ascii"), "Times New Roman")
    rfonts.set(qn("w:hAnsi"), "Times New Roman")

    tp = doc.add_paragraph()
    tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = tp.add_run(title)
    tr.bold = True
    tr.font.size = Pt(16)

    if opts.get("show_meta", True):
        mp = doc.add_paragraph("姓名：__________　　班级：__________　　日期：__________")
        mp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        mp.runs[0].font.size = Pt(10)

    for it in items:
        tag = ""
        if opts.get("show_tags", True):
            line = _tag_line(it)
            if line:
                tag = f'（{line}）'
        doc.add_paragraph(f'{it["n"]}. {_stem_text(it)}{tag}')
        p = image_path(it.get("image"))
        if p:
            doc.add_picture(str(p), width=Cm(content_cm))

    if opts.get("show_answer"):
        doc.add_page_break()
        ap = doc.add_paragraph()
        ap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        ar = ap.add_run("参考答案与解析")
        ar.bold = True
        ar.font.size = Pt(14)
        for it in items:
            doc.add_paragraph(f'{it["n"]}. {it.get("answer") or "（未填）"}')
            if opts.get("show_analysis") and it.get("analysis"):
                doc.add_paragraph(f'解析：{it["analysis"]}')
            p = image_path(it.get("answer_image"))
            if p:
                doc.add_picture(str(p), width=Cm(content_cm * 0.6))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ================================================================ PDF
def _chunks(text: str, size: int = 400) -> list[str]:
    """把超长段落切块。PyMuPDF 的 insert_textbox 放不下就整段不写，
    先切小可以避免「整段消失」这种最难查的问题。"""
    text = text or ""
    if len(text) <= size:
        return [text] if text.strip() else []
    return [text[i:i + size] for i in range(0, len(text), size)]


def build_pdf(title: str, items: list[dict], opts: dict) -> bytes:
    doc = pymupdf.open()
    state = {"page": doc.new_page(width=A4_W, height=A4_H), "y": MARGIN}

    def new_page() -> None:
        state["page"] = doc.new_page(width=A4_W, height=A4_H)
        state["y"] = MARGIN

    def write(text: str, size: float = 11.5, gap: float = 5.0, center: bool = False) -> None:
        for chunk in _chunks(text):
            rect = pymupdf.Rect(MARGIN, state["y"], A4_W - MARGIN, A4_H - MARGIN)
            if rect.height < size * 1.8:            # 只剩不到两行 → 换页
                new_page()
                rect = pymupdf.Rect(MARGIN, state["y"], A4_W - MARGIN, A4_H - MARGIN)
            left = state["page"].insert_textbox(
                rect, chunk, fontsize=size, fontname="china-s",
                align=1 if center else 0,
            )
            if left < 0:                            # 还是放不下 → 换页重排一次
                new_page()
                rect = pymupdf.Rect(MARGIN, state["y"], A4_W - MARGIN, A4_H - MARGIN)
                left = state["page"].insert_textbox(
                    rect, chunk, fontsize=size, fontname="china-s",
                    align=1 if center else 0,
                )
                if left < 0:
                    continue
            state["y"] += (rect.height - left) + gap

    def draw_image(path: Path, max_ratio: float = 1.0) -> None:
        try:
            pm = pymupdf.Pixmap(str(path))
        except Exception:  # noqa: BLE001  图片坏了不该让整份卷子导不出来
            return
        ratio = pm.height / pm.width
        # 裁剪图是 3 倍分辨率渲染的，按 px/2 换成 pt 大致就是原始大小；
        # 但太小的图（小公式）缩到 35% 内容宽以下就看不清了。
        w = min(CONTENT_W * max_ratio, max(pm.width / 2, CONTENT_W * 0.35 * max_ratio))
        h = w * ratio
        if h > A4_H - MARGIN * 2:                   # 高图按高度回缩
            h = A4_H - MARGIN * 2
            w = h / ratio
        if state["y"] + h > A4_H - MARGIN:          # 本页放不下 → 换页
            new_page()
        # 必须按显示尺寸降采样再转 JPEG：裁剪图是 3 倍分辨率渲染的，
        # 整张塞进去会把一份卷子撑到几十 MB（实测 3 道题就有 4.7MB）。
        factor = max(1, round(pm.width / max(64, int(w * 2))))   # 约等于 150dpi
        if factor > 1:
            pm.shrink(factor)        # 注意：shrink() 原地改，返回 None，别写成赋值
        if pm.alpha or pm.colorspace not in (pymupdf.csRGB, pymupdf.csGRAY):
            pm = pymupdf.Pixmap(pymupdf.csRGB, pm)               # JPEG 不接受 alpha
        state["page"].insert_image(
            pymupdf.Rect(MARGIN, state["y"], MARGIN + w, state["y"] + h),
            stream=pm.tobytes("jpeg", jpg_quality=82),
        )
        state["y"] += h + 8

    write(title, size=16, gap=10, center=True)
    if opts.get("show_meta", True):
        write("姓名：____________　班级：____________　日期：____________",
              size=10, gap=12, center=True)

    for it in items:
        tag = ""
        if opts.get("show_tags", True):
            line = _tag_line(it)
            if line:
                tag = f'（{line}）'
        write(f'{it["n"]}. {_stem_text(it)}{tag}')
        p = image_path(it.get("image"))
        if p:
            draw_image(p)

    if opts.get("show_answer"):
        new_page()
        write("参考答案与解析", size=14, gap=12, center=True)
        for it in items:
            write(f'{it["n"]}. {it.get("answer") or "（未填）"}')
            if opts.get("show_analysis") and it.get("analysis"):
                write(f'解析：{it["analysis"]}', size=10.5)
            p = image_path(it.get("answer_image"))
            if p:
                draw_image(p, max_ratio=0.6)

    data = doc.tobytes()
    doc.close()
    return data
