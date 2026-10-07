# -*- coding: utf-8 -*-
"""出卷导出：把选中的题目排成 A4 试卷，输出 HTML / Word / PDF。

三种格式对应三种用途，不是同一份内容导三遍：
  · HTML —— 保真度最高。图片以 base64 内嵌，导出的单个文件发给谁都还能看；
            $…$ 交给浏览器里的 KaTeX 渲染，Ctrl+P 直接就能存成 PDF。
  · DOCX —— 可编辑。老师要改题干、加批注、套学校抬头时用。
  · PDF  —— 直接打印。服务端用 PyMuPDF 排 A4，中文用内置 china-s 字体。

⚠️ 关于公式的实话：服务端没有 LaTeX 排版引擎，**DOCX / PDF 里的 $…$ 会原样输出**。
   想要公式排版就出 HTML。这点不藏着 —— 假装支持比不支持更糟。

三处差异（卷面样式）由 `paper_style.py` 提供的模板参数控制：抬头区、分区标题、
字号行距页边距、题号形态、每题分值、解答题留白。**不传模板 = 与加样式功能之前完全一致**，
这条是刻意的：新功能不该让老习惯突然变样。

本模块只吃「已经整理好的题目 dict」，不碰 ORM / Session，
因此不依赖数据库，可以直接单独调用来验证排版。
"""
from __future__ import annotations

import base64
import html as html_mod
import mimetypes
import re
import string
from pathlib import Path

import pymupdf

from .. import config
from . import paper_style

# A4（pt）
A4_W, A4_H = 595.28, 841.89
# 页边距不再写死在这里：改由模板的 style.margin_mm 给（paper_style.DEFAULT_STYLE）。
# 原来三个格式各写各的（HTML 打印 16/14mm、DOCX 18/16mm、PDF 50pt≈17.6mm），
# 同一份卷子预览和下载的留白对不上 —— 顺手统一成一份数据。

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


def figure_path(it: dict) -> Path | None:
    """题干里那幅「如图」的图（老师审核时从原卷上框出来的）。没有就 None。

    ⚠️ **只有文本形态才该用它。** 图片形态印的是整块原貌图，
        那幅图本来就在里面 —— 再印一次，同一道题会出现两遍同样的图。
        调用点都按这个前提写了分支，改动渲染逻辑时别把这条丢了。
    """
    return image_path(it.get("figure_image"))


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


# ================================================================ 形态：印文本还是印图
# 录题时同一道题常常**同时**存着两样东西：识别出来的文本（Markdown + $LaTeX$）
# 和原貌截图。以前出卷把两者一起印（题干文字下面再压一整张原貌图），既占版面
# 又是重复的。现在二选一，谁来选见 _choose_form 的注释。
def _choose_form(text: str | None, image: str | None, pref: str | None,
                 override: str | None) -> str:
    """给「文本」和「图」两样东西，决定这一题印哪个，返回 text / image / none。

    两条规则，缺一不可：

      1. override（出卷时的整卷选择）压过单题偏好 —— 老师临时想把整卷换成图片，
         不该被每题各自的默认挡住。

      2. **缺的那一种自动退回另一种**。强制用图但没图、强制用文本但没文本，
         都不去印一道空白题，而是落到还有的那一个。反过来才可怕：
         「我明明选了图片，这一题怎么是空的」——卷子印出来才发现就晚了。
    """
    has_text = bool((text or "").strip())
    has_img = bool(image)
    p = override if override in ("text", "image") else (pref or "auto")
    if p == "text":
        return "text" if has_text else ("image" if has_img else "none")
    if p == "image":
        return "image" if has_img else ("text" if has_text else "none")
    # auto：文本优先。识别过的题文本比截图清楚得多（还能被搜到、被复用）
    return "text" if has_text else ("image" if has_img else "none")


def body_form(it: dict, override: str | None = None) -> str:
    """题干的形态。"""
    return _choose_form(_stem_text(it), it.get("image"), it.get("render_prefer"), override)


def answer_form(it: dict, override: str | None = None) -> str:
    """答案的形态。跟题干共用**同一条偏好** —— 一道题要么整体用文本、要么整体用图，
    符合直觉，也少一个要维护的字段。（真要拆开，加字段的代价很小。）"""
    return _choose_form(it.get("answer"), it.get("answer_image"), it.get("render_prefer"), override)


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


# ================================================================ 抬头区（三种格式共用）
# 抬头区抽成「一串有类型的块」，让 HTML / DOCX / PDF 三个渲染分支走同一份数据。
# 各写一遍的后果已经吃过一次了 —— `_tag_line` 的注释里记着：出卷选项说标三样，
# 三处输出必须一模一样，否则用户看到的就是「预览有、下载没有」。
def head_blocks(paper: dict, opts: dict) -> list[dict]:
    blocks: list[dict] = []
    if paper.get("subtitle"):
        blocks.append({"kind": "subtitle", "text": paper["subtitle"]})
    if paper.get("exam_note"):
        blocks.append({"kind": "note", "text": paper["exam_note"]})
    if paper.get("fill_fields") and opts.get("show_meta", True):
        blocks.append({
            "kind": "fill",
            "fields": list(paper["fill_fields"]),
            "text": "　　".join(f"{f}：____________" for f in paper["fill_fields"]),
        })
    if paper.get("instructions"):
        blocks.append({
            "kind": "instructions",
            "title": paper.get("instructions_title") or "注意事项",
            "items": list(paper["instructions"]),
        })
    if paper.get("score_table"):
        # 高考卷开头的「题号 / 得分」登分表。列数固定 12 —— 再多一页也放不下，
        # 而且真卷的登分表就是固定格数，不随题目数变。
        blocks.append({"kind": "score_table", "cols": 12})
    return blocks


def score_text(it: dict, style: dict) -> str:
    """每题的分值后缀。没填分值就返回空串 —— 不猜。

    单位是英文（marks / points）时连**括号**一起换成西文、中间加空格：
    A-Level / DSE 卷面写的是「(4 marks)」，印成「（4marks）」就不像真卷了。
    中文卷（分）保持全角括号、不加空格 —— 和「每小题 5 分」的写法一致。
    """
    if not style.get("show_score"):
        return ""
    s = it.get("score")
    if not isinstance(s, (int, float)):
        return ""
    shown = int(s) if float(s).is_integer() else round(float(s), 2)
    unit = style.get("score_unit") or "分"
    if unit.isascii():
        return f"({shown} {unit})"
    return f"（{shown}{unit}）"


def blank_lines(it: dict, style: dict) -> int:
    """这道题下面该留几行作答空白。"""
    n = int(style.get("answer_space") or 0)
    if n <= 0:
        return 0
    if (it.get("qtype") or "").strip() not in (style.get("answer_space_types") or []):
        return 0
    return n


# ================================================================ HTML
# ⚠️ 这里用 string.Template（`$name`）而不是 str.format()：
#    旧写法要求把 CSS / JS 里的每个花括号都写成双份，漏一个就抛
#    "unexpected '{' in field name"，每次改样式都得盯着这件事。
#    Template 只认 `$`，CSS 里的 `{}` 原样保留，省掉一整类低级错误。
#    代价：CSS 里若出现 `$`（不会有）才需要转义。
_HTML_HEAD = string.Template("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>$title</title>
<link rel="stylesheet" href="$katex_css">
<style>
$css
</style>
</head>
<body>
<div class="no-print"><button onclick="window.print()">打印 / 存为 PDF</button></div>
""")

_HTML_CSS = string.Template("""  /* 屏幕上是普通网页，打印时按 A4 排版 —— 一个文件两种用法 */
  @page { size: A4; margin: ${top_mm}mm ${side_mm}mm; }
  body { font-family: "Songti SC", "SimSun", "Microsoft YaHei", serif;
         font-size: ${font_size}pt; line-height: ${line_height}; color: #000; margin: 0; }
  h1.title { font-size: ${title_size}pt; text-align: center; margin: 0 0 6pt; }
  .subtitle { text-align: center; font-size: ${hint_size}pt; margin: 0 0 4pt; }
  .exam-note { text-align: center; font-size: ${hint_size}pt; margin: 0 0 10pt; }
  .fill { display: flex; justify-content: space-between; gap: 8pt; font-size: ${hint_size}pt;
          border-bottom: 1px solid #000; padding-bottom: 4pt; margin-bottom: 12pt; }
  .instr { border: 1px solid #999; padding: 6pt 8pt; margin: 0 0 12pt; font-size: ${hint_size}pt; }
  .instr b { display: block; margin-bottom: 2pt; }
  .instr ol { margin: 0; padding-left: 18pt; }
  .instr li { margin-bottom: 2pt; }
  .score-table { width: 100%; border-collapse: collapse; font-size: ${hint_size}pt; margin: 0 0 12pt; }
  .score-table td { border: 1px solid #000; height: 20pt; text-align: center; }
  h2.sec { font-size: ${section_size}pt; margin: 14pt 0 3pt; }
  .sec-note { font-size: ${hint_size}pt; margin: 0 0 8pt; }
  .pb { break-before: page; page-break-before: always; }
  .q { margin-bottom: ${question_gap}pt; break-inside: avoid; page-break-inside: avoid; }
  .q .stem { white-space: pre-wrap; }
  .q img { max-width: 100%; display: block; margin: 6pt 0; }
  .tag { font-size: ${hint_size}pt; color: #666; }
  .rule { border-bottom: 1px solid #000; height: ${rule_h}pt; }
  .ans { break-before: page; page-break-before: always; }
  .ans .item { margin-bottom: 10pt; white-space: pre-wrap; }
  .ans img { max-width: 55%; display: block; margin: 4pt 0; }
  @media screen {
    body { max-width: 820px; margin: 24px auto 60px; padding: 0 20px; }
    .ans { border-top: 1px dashed #bbb; padding-top: 16px; }
    .no-print { text-align: right; margin-bottom: 10px; }
  }
  @media print { .no-print { display: none; } }""")

def _html_tail() -> str:
    """HTML 收尾。

    ⚠️ 这块**不能用 string.Template**（`_HTML_HEAD` / `_HTML_CSS` 用的是）：
    下面的 JS 里到处是 `$` —— KaTeX 的行间/行内分隔符 `$$` 和 `$`、注释里的
    `$…$` —— 全会被 Template 当成占位符，轻则 JS 被改写、重则直接抛
    "Invalid placeholder in string"。所以这里用普通拼接，`$` 原样保留。
    """
    return (
        f'<script src="{KATEX_JS}"></script>\n'
        f'<script src="{KATEX_AUTO}"></script>\n'
        '<script>\n'
        "// 公式渲染。没网时 KaTeX 加载不出来，$…$ 会以原文显示 —— 题目本身不受影响。\n"
        "if (window.renderMathInElement) {\n"
        "  renderMathInElement(document.body, {\n"
        "    delimiters: [{ left: '$$', right: '$$', display: true },\n"
        "                 { left: '$', right: '$', display: false }],\n"
        "    throwOnError: false\n"
        "  });\n"
        "}\n"
        '</script>\n</body>\n</html>\n'
    )


def _html_css(style: dict) -> str:
    top_mm, side_mm = style["margin_mm"]
    return _HTML_CSS.substitute(
        top_mm=top_mm,
        side_mm=side_mm,
        font_size=style["font_size"],
        title_size=style["title_size"],
        section_size=style["section_size"],
        line_height=style["line_height"],
        question_gap=style["question_gap"],
        hint_size=round(style["font_size"] - 1.5, 1),
        rule_h=round(style["font_size"] * 2.2, 1),   # 作答横线间距按字号走
    )


def build_html(title: str, items: list[dict], opts: dict, tpl=None) -> str:
    t = paper_style.resolve(tpl)
    paper, style = t["paper"], t["style"]

    out = [_HTML_HEAD.substitute(title=_esc(title), katex_css=KATEX_CSS, css=_html_css(style))]
    out.append(f'<h1 class="title">{_esc(title)}</h1>')

    for b in head_blocks(paper, opts):
        if b["kind"] in ("subtitle", "note"):
            cls = "subtitle" if b["kind"] == "subtitle" else "exam-note"
            out.append(f'<div class="{cls}">{_esc(b["text"])}</div>')
        elif b["kind"] == "fill":
            out.append('<div class="fill">'
                       + "".join(f"<span>{_esc(f)}：____________</span>" for f in b["fields"])
                       + "</div>")
        elif b["kind"] == "instructions":
            lis = "".join(f"<li>{_esc(x)}</li>" for x in b["items"])
            out.append(f'<div class="instr"><b>{_esc(b["title"])}</b><ol>{lis}</ol></div>')
        elif b["kind"] == "score_table":
            cells = "".join("<td></td>" for _ in range(b["cols"]))
            out.append(f'<table class="score-table"><tr>{cells}</tr><tr>{cells}</tr></table>')

    _html_body(out, items, t, style, opts)

    if opts.get("show_answer"):
        out.append('<div class="ans">')
        out.append('<h1 class="title" style="font-size:14pt">参考答案与解析</h1>')
        for it in items:
            out.append(f'<div class="item"><b>{it["n"]}.</b>')
            if answer_form(it, opts.get("render_mode")) == "image":
                ap = image_path(it.get("answer_image"))
                if ap:
                    out.append(f'<img src="{_data_uri(ap)}" alt="">')
            else:
                out.append(' ' + (_esc(it.get("answer")) or "（未填）"))
            if opts.get("show_analysis") and it.get("analysis"):
                out.append(f'<div>解析：{_esc(it["analysis"])}</div>')
            out.append('</div>')
        out.append('</div>')

    out.append(_html_tail())
    return "".join(out)


def _html_body(out: list[str], items: list[dict], t: dict, style: dict, opts: dict) -> None:
    """题目按分区渲染。抽出来只是为了让 build_html 少一层缩进，逻辑全在这儿。"""
    for g in paper_style.layout(items, t["sections"]):
        if g["title"]:
            brk = " pb" if g["page_break"] else ""
            out.append(f'<h2 class="sec{brk}">{_esc(g["title"])}</h2>')
        if g["title"] or g["note"]:
            note = paper_style.section_note(g["note"], g["items"], style)
            if note:
                out.append(f'<div class="sec-note">{_esc(note)}</div>')
        for it in g["items"]:
            out.append('<div class="q">')
            tag = ""
            if opts.get("show_tags", True):
                line = _tag_line(it)
                if line:
                    tag = f' <span class="tag">（{_esc(line)}）</span>'
            stem = _esc(_stem_text(it))
            no = f'<b>{_esc(paper_style.num_text(it["n"], style))}</b>'
            score = f'<span class="tag">{_esc(score_text(it, style))}</span>'
            form = body_form(it, opts.get("render_mode"))
            if form == "image":
                # 用图片形态：题号照印（不然学生不知道这是第几题），题干文字不印
                out.append(f'<div class="stem">{no}{score}{tag}</div>')
                p = image_path(it.get("image"))
                if p:
                    out.append(f'<img src="{_data_uri(p)}" alt="">')
            else:
                out.append(f'<div class="stem">{no} {stem}{score}{tag}</div>')
                # 题干配图。**只在文本形态补**：图片形态的原貌图里已经有它了。
                fp = figure_path(it)
                if fp:
                    out.append(f'<img class="fig" src="{_data_uri(fp)}" alt="" '
                               'style="max-width:64%;display:block;margin:8px auto">')
            for _ in range(blank_lines(it, style)):
                out.append('<div class="rule"></div>')
            out.append('</div>')


# ================================================================ DOCX
def build_docx(title: str, items: list[dict], opts: dict, tpl=None) -> bytes:
    import io

    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt

    t = paper_style.resolve(tpl)
    paper, style = t["paper"], t["style"]
    top_mm, side_mm = style["margin_mm"]

    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(side_mm / 10.0)
    sec.top_margin = sec.bottom_margin = Cm(top_mm / 10.0)
    content_cm = 21.0 - (side_mm / 10.0) * 2

    # python-docx 默认是西文字体，中文会走回退字体（宋体/雅黑都不一定），
    # 必须显式写 w:eastAsia，否则 Word 里排版和预期不一样。
    normal = doc.styles["Normal"]
    normal.font.size = Pt(style["font_size"])
    rfonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), "宋体")
    rfonts.set(qn("w:ascii"), "Times New Roman")
    rfonts.set(qn("w:hAnsi"), "Times New Roman")
    # 行距也按模板走。不设的话 Word 用默认单倍行距，和 HTML 预览对不上。
    normal.paragraph_format.line_spacing = style["line_height"]

    def para(text: str = "", size: float | None = None, center: bool = False, bold: bool = False):
        p = doc.add_paragraph()
        if center:
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if text:
            r = p.add_run(text)
            r.bold = bold
            if size:
                r.font.size = Pt(size)
        return p

    tp = para(title, size=style["title_size"], center=True, bold=True)

    for b in head_blocks(paper, opts):
        if b["kind"] in ("subtitle", "note"):
            para(b["text"], size=style["font_size"] - 1.5, center=True)
        elif b["kind"] == "fill":
            para(b["text"], size=style["font_size"] - 1.5, center=True)
        elif b["kind"] == "instructions":
            para(b["title"], size=style["font_size"] - 0.5, bold=True)
            for i, x in enumerate(b["items"], start=1):
                para(f"{i}. {x}", size=style["font_size"] - 1.5)
        elif b["kind"] == "score_table":
            tb = doc.add_table(rows=2, cols=b["cols"])
            tb.alignment = WD_TABLE_ALIGNMENT.CENTER
            tb.style = "Table Grid"
            para("")

    for g in paper_style.layout(items, t["sections"]):
        if g["title"]:
            if g["page_break"]:
                doc.add_page_break()
            para(g["title"], size=style["section_size"], bold=True)
        if g["title"] or g["note"]:
            note = paper_style.section_note(g["note"], g["items"], style)
            if note:
                para(note, size=style["font_size"] - 1.5)
        for it in g["items"]:
            tag = ""
            if opts.get("show_tags", True):
                line = _tag_line(it)
                if line:
                    tag = f'（{line}）'
            no = paper_style.num_text(it["n"], style)
            form = body_form(it, opts.get("render_mode"))
            if form == "image":
                para(f'{no}{score_text(it, style)}{tag}')
                p = image_path(it.get("image"))
                if p:
                    doc.add_picture(str(p), width=Cm(content_cm))
            else:
                para(f'{no} {_stem_text(it)}{score_text(it, style)}{tag}')
                # 题干配图只在文本形态补（图片形态的原貌图里已经有它）
                fp = figure_path(it)
                if fp:
                    doc.add_picture(str(fp), width=Cm(content_cm * 0.64))
            for _ in range(blank_lines(it, style)):
                para("")

    if opts.get("show_answer"):
        doc.add_page_break()
        para("参考答案与解析", size=14, center=True, bold=True)
        for it in items:
            if answer_form(it, opts.get("render_mode")) == "image":
                doc.add_paragraph(f'{it["n"]}.')
                p = image_path(it.get("answer_image"))
                if p:
                    doc.add_picture(str(p), width=Cm(content_cm * 0.6))
            else:
                doc.add_paragraph(f'{it["n"]}. {it.get("answer") or "（未填）"}')
            if opts.get("show_analysis") and it.get("analysis"):
                doc.add_paragraph(f'解析：{it["analysis"]}')

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


def build_pdf(title: str, items: list[dict], opts: dict, tpl=None) -> bytes:
    t = paper_style.resolve(tpl)
    paper, style = t["paper"], t["style"]
    base = style["font_size"]

    # 页边距：模板给的是 mm，PyMuPDF 用 pt。不传模板时正好回到 50pt（≈17.6mm），
    # 与改动前的输出一致。
    top_mm, side_mm = style["margin_mm"]
    margin = side_mm * 72.0 / 25.4
    margin_top = top_mm * 72.0 / 25.4
    content_w = A4_W - margin * 2

    doc = pymupdf.open()
    state = {"page": doc.new_page(width=A4_W, height=A4_H), "y": margin_top}

    def new_page() -> None:
        state["page"] = doc.new_page(width=A4_W, height=A4_H)
        state["y"] = margin_top

    def write(text: str, size: float | None = None, gap: float | None = None,
              center: bool = False) -> None:
        size = base if size is None else size
        gap = 5.0 if gap is None else gap
        for chunk in _chunks(text):
            rect = pymupdf.Rect(margin, state["y"], A4_W - margin, A4_H - margin_top)
            if rect.height < size * 1.8:            # 只剩不到两行 → 换页
                new_page()
                rect = pymupdf.Rect(margin, state["y"], A4_W - margin, A4_H - margin_top)
            left = state["page"].insert_textbox(
                rect, chunk, fontsize=size, fontname="china-s",
                align=1 if center else 0,
            )
            if left < 0:                            # 还是放不下 → 换页重排一次
                new_page()
                rect = pymupdf.Rect(margin, state["y"], A4_W - margin, A4_H - margin_top)
                left = state["page"].insert_textbox(
                    rect, chunk, fontsize=size, fontname="china-s",
                    align=1 if center else 0,
                )
                if left < 0:
                    continue
            state["y"] += (rect.height - left) + gap

    def draw_image(path: Path | None, max_ratio: float = 1.0) -> None:
        # path 为 None = 这题没有那幅图（配图未框、或文件已被删）。
        # 在这里挡掉而不是让每个调用点各判一次 —— 漏判一处就是 AttributeError。
        if not path:
            return
        try:
            pm = pymupdf.Pixmap(str(path))
        except Exception:  # noqa: BLE001  图片坏了不该让整份卷子导不出来
            return
        ratio = pm.height / pm.width
        # 裁剪图是 3 倍分辨率渲染的，按 px/2 换成 pt 大致就是原始大小；
        # 但太小的图（小公式）缩到 35% 内容宽以下就看不清了。
        w = min(content_w * max_ratio, max(pm.width / 2, content_w * 0.35 * max_ratio))
        h = w * ratio
        if h > A4_H - margin_top * 2:               # 高图按高度回缩
            h = A4_H - margin_top * 2
            w = h / ratio
        if state["y"] + h > A4_H - margin_top:      # 本页放不下 → 换页
            new_page()
        # 必须按显示尺寸降采样再转 JPEG：裁剪图是 3 倍分辨率渲染的，
        # 整张塞进去会把一份卷子撑到几十 MB（实测 3 道题就有 4.7MB）。
        factor = max(1, round(pm.width / max(64, int(w * 2))))   # 约等于 150dpi
        if factor > 1:
            pm.shrink(factor)        # 注意：shrink() 原地改，返回 None，别写成赋值
        if pm.alpha or pm.colorspace not in (pymupdf.csRGB, pymupdf.csGRAY):
            pm = pymupdf.Pixmap(pymupdf.csRGB, pm)               # JPEG 不接受 alpha
        state["page"].insert_image(
            pymupdf.Rect(margin, state["y"], margin + w, state["y"] + h),
            stream=pm.tobytes("jpeg", jpg_quality=82),
        )
        state["y"] += h + 8

    def rule(count: int) -> None:
        """作答横线。PyMuPDF 没有样式化的表格，直接画线最省事也最像真卷。"""
        step = base * 2.2
        for _ in range(count):
            if state["y"] + step > A4_H - margin_top:
                new_page()
            y = state["y"] + step * 0.7
            state["page"].draw_line(pymupdf.Point(margin, y),
                                    pymupdf.Point(A4_W - margin, y),
                                    color=(0, 0, 0), width=0.5)
            state["y"] += step
        state["y"] += 4

    write(title, size=style["title_size"], gap=10, center=True)

    for b in head_blocks(paper, opts):
        if b["kind"] in ("subtitle", "note", "fill"):
            write(b["text"], size=base - 1.5, gap=8, center=True)
        elif b["kind"] == "instructions":
            write(b["title"], size=base - 0.5, gap=4)
            for i, x in enumerate(b["items"], start=1):
                write(f"{i}. {x}", size=base - 1.5, gap=4)
            state["y"] += 6
        elif b["kind"] == "score_table":
            # 登分表：画两行格子。列宽按内容宽均分，和 HTML 那版视觉一致。
            cols = b["cols"]
            cell_w = content_w / cols
            row_h = 20.0
            if state["y"] + row_h * 2 > A4_H - margin_top:
                new_page()
            for r in range(2):
                for c in range(cols + 1):
                    x = margin + c * cell_w
                    state["page"].draw_line(pymupdf.Point(x, state["y"] + r * row_h),
                                            pymupdf.Point(x, state["y"] + (r + 1) * row_h),
                                            color=(0, 0, 0), width=0.5)
                for c in range(cols):
                    state["page"].draw_line(
                        pymupdf.Point(margin + c * cell_w, state["y"] + (r + 1) * row_h),
                        pymupdf.Point(margin + (c + 1) * cell_w, state["y"] + (r + 1) * row_h),
                        color=(0, 0, 0), width=0.5)
            state["y"] += row_h * 2 + 10

    for g in paper_style.layout(items, t["sections"]):
        if g["title"]:
            if g["page_break"]:
                new_page()
            write(g["title"], size=style["section_size"], gap=4)
        if g["title"] or g["note"]:
            note = paper_style.section_note(g["note"], g["items"], style)
            if note:
                write(note, size=base - 1.5, gap=6)
        for it in g["items"]:
            tag = ""
            if opts.get("show_tags", True):
                line = _tag_line(it)
                if line:
                    tag = f'（{line}）'
            no = paper_style.num_text(it["n"], style)
            form = body_form(it, opts.get("render_mode"))
            if form == "image":
                write(f'{no}{score_text(it, style)}{tag}')
                p = image_path(it.get("image"))
                if p:
                    draw_image(p)
            else:
                write(f'{no} {_stem_text(it)}{score_text(it, style)}{tag}')
                # 题干配图只在文本形态补（图片形态的原貌图里已经有它）
                draw_image(figure_path(it), max_ratio=0.7)
            rule(blank_lines(it, style))

    if opts.get("show_answer"):
        new_page()
        write("参考答案与解析", size=14, gap=12, center=True)
        for it in items:
            if answer_form(it, opts.get("render_mode")) == "image":
                write(f'{it["n"]}.')
                p = image_path(it.get("answer_image"))
                if p:
                    draw_image(p, max_ratio=0.6)
            else:
                write(f'{it["n"]}. {it.get("answer") or "（未填）"}')
            if opts.get("show_analysis") and it.get("analysis"):
                write(f'解析：{it["analysis"]}', size=10.5)

    data = doc.tobytes()
    doc.close()
    return data
