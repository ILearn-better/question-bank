# -*- coding: utf-8 -*-
"""卷面文字里的 LaTeX 公式 → Word / PDF 里真能看的东西。

## 为什么需要这个文件（动之前先读）

HTML 导出把 `$…$` 交给浏览器里的 KaTeX，天然是渲染好的。
但 **DOCX 和 PDF 没有 LaTeX 引擎** —— 以前的做法是照原文印出去，卷面上就出现
「已知函数 $f(x)=x^2$ 在 $x=1$ 处…」这种一堆美元符号的东西。用户 2026-11-12 报的就是它。

现在两条路分开走：
  · DOCX —— **原生 Word 公式（OMML）**：`latex2mathml` → `mathml2omml` → 塞进段落的
           `m:oMath`。Word/WPS 里双击能改、字号跟着段落、打印是 Cambria Math 的排版，
           不再是「一张小图片贴在那儿」。解析不了才退回图片，再不行退回可读文本。
           （`SHIKE_DOCX_MATH=image` 可整卷退回图片，给老版 WPS 留的开关。）
  · PDF  —— 走**真 LaTeX**（`paper_latex.py`，tectonic/XeLaTeX），公式是 LaTeX 自己排的。
           这里只为「引擎没装 / 编译失败」的兜底路径准备 `pdf_inline()`（贴图 + htmlbox）。

两条路共用的只有**分段规则**（`latex_img.split_math`）与**退路**（`plain_text`），
各写一份的下场已经吃过一次（见 `paper_export._tag_line` 的注释）。

底层的公式渲染用的是 `latex_img.py`（matplotlib mathtext），和批改导出**同一套** ——
那边 2026-11-02 就修好了，这里不再造第二套引擎。

## 三条口径

① **渲不出来就退回可读文本**（`latex_img.plain_text`：`\\times` → `×`），
   绝不把 `$` 和反斜杠印到卷面上。老师拿这份卷子是去印给学生做的。
   失败在 `latex_img._math_png` 里已经写了 log，不是静默。

② **公式图的高度按 px = pt 换算**（matplotlib 是 dpi=72 渲的，1px 就是 1pt），
   所以 `img.height` 可以直接当 pt 用。`FORMULA_SCALE` 让公式比同字号汉字略大一点 ——
   数学符号本身比汉字矮，等字号看着偏小。

③ **`$$…$$`（display）独占一行居中**，与屏幕上 KaTeX 的 displayMode 同口径。
   否则多行推导会被挤在句子中间、左右各跟着半句话。
"""
from __future__ import annotations

import base64
import html as html_mod
import io
import logging
import os
import re
from functools import lru_cache

from . import latex_img

_log = logging.getLogger("shike")

# 公式图相对正文字号的倍数。1.0 = 与正文等大；略放大一点更接近 KaTeX 的观感。
FORMULA_SCALE = 1.10

# 行距（相对字号）。与 paper_export 里 HTML 那版的 line-height 保持一致。
LINE_HEIGHT = 1.65


# ---------------------------------------------------------------- 中间表示
def pieces(text, font_pt: float, color: tuple[int, int, int] = (0, 0, 0)) -> list[dict]:
    """把一段卷面文字切成「文字 / 公式」交替的片段，公式已经渲染好。

    返回的每一项：
      · `{'t': 'text', 'v': str}`
      · `{'t': 'math', 'img': PIL.Image, 'tex': str, 'display': bool}`
      · `{'t': 'math', 'v': str, 'display': bool}`   ← 渲不出来的退路（v 是可读文本）

    调用方判断有没有 `img` 就知道该插图还是该印字，不用再关心失败。
    """
    out: list[dict] = []
    px = max(6, int(round(font_pt * FORMULA_SCALE)))
    for part in latex_img.split_math(text):
        if part["t"] != "math":
            out.append({"t": "text", "v": part["v"]})
            continue
        tex = part["tex"]
        display = bool(part.get("display"))
        img = latex_img.math_image(tex, px, color)
        if img is None:
            # 退路：可读文本。**不是**把反斜杠原文印出去。
            out.append({"t": "math", "v": latex_img.plain_text(tex), "display": display})
        else:
            out.append({"t": "math", "img": img, "tex": tex, "display": display})
    return out


def has_math(text) -> bool:
    """这段文字里有没有公式？调用方据此决定走不走新路径（没有就走原来的纯文本快路）。"""
    return any(p["t"] == "math" for p in latex_img.split_math(text))


# ---------------------------------------------------------------- PDF（HTML 片段）
def _png_uri(img) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _esc(s) -> str:
    return html_mod.escape("" if s is None else str(s))


def pdf_inline(text, font_pt: float, color: tuple[int, int, int] = (0, 0, 0)) -> str:
    """一段文字 → HTML 片段（公式已是 data-uri 的 <img>）。**会转义**，可直接拼进 HTML。

    公式图的高度写成 `pt` 而不是 `em`：em 会被外层的 font-size 再乘一次，
    遇到「答案区字号比正文小一号」的地方公式就会跟着缩，和正文对不上。
    """
    out: list[str] = []
    for p in pieces(text, font_pt, color):
        if p["t"] == "text":
            # ⚠️ HTML 里 `\n` 只是空白，**不会换行** —— 题干的多段会被压成一行。
            #    必须显式转 <br>（以前走 insert_textbox 时 `\n` 是天然换行的）。
            out.append(_esc(p["v"]).replace("\n", "<br>"))
            continue
        if not p.get("img"):
            out.append(_esc(p["v"]).replace("\n", "<br>"))   # 退路的可读文本
            continue
        img = p["img"]
        tag = ('<img src="%s" style="height:%dpt;vertical-align:middle">'
               % (_png_uri(img), img.height))
        if p.get("display"):
            out.append("</div><div style='text-align:center;margin:.2em 0'>%s</div><div>" % tag)
        else:
            out.append(tag)
    return "".join(out)


def pdf_block(head: str, text, tail: str = "", font_pt: float = 11.0,
              bold: bool = False, center: bool = False,
              color: tuple[int, int, int] = (0, 0, 0)) -> str:
    """一整个段落：题号(head) + 正文(text) + 尾巴(分值/标签)。

    分三段传是因为它们的样式不同（题号加粗、标签小字灰），
    但**换行要一起算** —— 分成三个 HTML 块排会在题号后面强行断行。
    """
    style = "font-size:%.2fpt;line-height:%.2f" % (font_pt, LINE_HEIGHT)
    if center:
        style += ";text-align:center"
    inner = ""
    if head:
        inner += "<b>" + _esc(head) + "</b>" if bold else _esc(head)
    inner += pdf_inline(text, font_pt, color)
    if tail:
        inner += _esc(tail)
    if bold and not head:
        # 没有 head 时 bold 指的是**整段**（标题、分区标题都是这样用的）
        inner = "<b>" + inner + "</b>"
    return "<div style='%s'>%s</div>" % (style, inner)


def measure_width(fragment: str, width_pt: float) -> float:
    """用 Story 量一段 HTML 在给定宽度下要占多高（pt）。**不画东西**。

    PDF 排版要按块精确分页，先量后排是唯一稳的办法：
    `insert_htmlbox` 放不下时不会告诉你「差多少」，只会缩字或者截断。
    """
    import pymupdf
    try:
        story = pymupdf.Story(html=fragment)
        _more, filled = story.place(pymupdf.Rect(0, 0, width_pt, 100000))
        return float(filled[3] - filled[1])
    except Exception:                       # noqa: BLE001 —— 量不出来就当一行高
        return 20.0


# ---------------------------------------------------------------- DOCX
# Word 原生公式（OMML）的命名空间。python-docx 的默认模板里已经声明过，
# 但 `mathml2omml` 输出的片段自带 `<m:oMath>` 而**不带** xmlns，
# lxml 会以「Namespace prefix m on oMath is not defined」直接拒绝解析。
_M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def native_math_enabled() -> bool:
    """DOCX 里用原生 Word 公式（默认）还是退回图片。

    `SHIKE_DOCX_MATH=image` 可切回图片 —— 给**老版 WPS** 留的退路：
    新版 WPS 和 Word 都认 OMML，但较老的 WPS 打开可能出现空白公式框。
    出问题时不至于没法交卷子，改个环境变量重启即可。
    """
    return (os.environ.get("SHIKE_DOCX_MATH") or "omml").strip().lower() != "image"


@lru_cache(maxsize=1024)
def omml(tex: str, display: bool = False) -> str | None:
    """LaTeX → MathML（latex2mathml）→ OMML（mathml2omml）。失败返回 None。

    纯 Python，不需要 pandoc，也不需要本机 Office 的 MML2OMML.XSL。
    覆盖到的写法见 test_paper_omml.py 的矩阵（分数、根号、积分、求和、分段、矩阵、
    向量箭头、集合、角度、因为/所以、绝对值、对数、组合数、带中文的 \\text{}）。
    ⚠️ 已知不支持：`\\begin{aligned}`（latex2mathml 那边会产出非法 XML）。
       遇到就自动落到图片退路，不会崩。
    加 lru_cache 是因为答案区的解析里同一个公式常常出现两次。
    """
    if not (tex or "").strip():
        return None
    try:
        from latex2mathml.converter import convert as tex_to_mathml
    except ImportError:                        # 没装依赖 → 交给图片退路
        return None
    try:
        ml = tex_to_mathml(tex, display="block" if display else "inline")
        import mathml2omml
        xml = mathml2omml.convert(ml)
    except Exception:                          # noqa: BLE001 —— 解析不了就退图片
        return None
    if not xml or "<m:oMath" not in xml:
        return None
    xml = _repair_omml(xml)
    if not _omml_is_valid(xml):
        _log.warning("OMML 结构不合法，这道公式退回图片：%s", tex[:60])
        return None
    return xml.replace("<m:oMath>", '<m:oMath xmlns:m="%s">' % _M_NS, 1)


# `mathml2omml` 0.0.2 有个笔误：`<m:groupChrPr>` 被它用 `</m:groupChr>` 关掉
# （见 https://pypi.org/project/mathml2omml/ 的源码映射表）。后果是**凡是带 `\overrightarrow`
# 的公式**（向量，中学卷子里到处都是）生成的 XML 都不合法 —— lxml 直接拒绝解析，
# 表现是「悄悄退回图片」。这里补一刀；改坏了后面还有校验兜着。
_GROUPCHR_BUG = re.compile(r"(<m:groupChrPr>.*?)</m:groupChr>", re.S)


def _repair_omml(xml: str) -> str:
    return _GROUPCHR_BUG.sub(r"\1</m:groupChrPr>", xml)


def _omml_is_valid(xml: str) -> bool:
    """能不能被 lxml 解析（不能就别塞进 docx，塞了 Word 会报文件损坏）。"""
    try:
        from lxml import etree
        etree.fromstring(xml.replace(
            "<m:oMath>", '<m:oMath xmlns:m="%s">' % _M_NS, 1).encode("utf-8"))
        return True
    except Exception:                          # noqa: BLE001
        return False


def docx_runs(par, text, font_pt: float, bold: bool = False,
              color: tuple[int, int, int] = (0, 0, 0), native: bool | None = None):
    """往一个 python-docx 段落里追加「文字 + 公式」的 run 序列。

    公式优先做成**原生 Word 公式（OMML）**：Word 里双击能改、字号跟着段落走、
    打印出来是 Cambria Math 的排版效果，不是一张小图片贴在那儿。
    解析不了才退回图片（matplotlib 渲的 PNG），再不行退回可读文本。

    ⚠️ 公式和图**必须在同一段落**里：分成两个 paragraph 就成了「公式另起一行」，
        而卷面上的公式大多是嵌在句子中间（「求 $a$ 的取值范围」）。
    ⚠️ 文字里的 `\\n` 要显式转 `add_break()` —— run 里那个字符不会变成换行，
        只会被 Word 吞掉，题干的多段就全挤成一行。
    ⚠️ 图片高度用 `Pt(img.height)`：公式图是 dpi=72 渲的，1px = 1pt。
    """
    from docx.oxml import parse_xml
    from docx.shared import Pt

    use_native = native_math_enabled() if native is None else native
    px = max(6, int(round(font_pt * FORMULA_SCALE)))

    def _run():
        r = par.add_run()
        r.bold = bold
        r.font.size = Pt(font_pt)
        return r

    def _text(v: str) -> None:
        lines = str(v).split("\n")
        for i, seg in enumerate(lines):
            if i:
                _run().add_break()          # 换行也要带字号，否则 Word 会回退到默认
            if seg:
                _run().text = seg

    def _picture(img) -> None:
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        _run().add_picture(buf, height=Pt(img.height))

    for part in latex_img.split_math(text):
        if part["t"] != "math":
            _text(part.get("v"))
            continue
        tex = part.get("tex") or ""
        display = bool(part.get("display"))
        if use_native:
            xml = omml(tex, display)
            if xml:
                try:
                    par._p.append(parse_xml(xml))
                    continue
                except Exception:          # noqa: BLE001 —— OMML 拼不进去就退图片
                    pass
        img = latex_img.math_image(tex, px, color)
        if img is not None:
            _picture(img)
        else:
            _text(latex_img.plain_text(tex))   # 最后的退路：可读文本，不是反斜杠原文
