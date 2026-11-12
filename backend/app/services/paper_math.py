# -*- coding: utf-8 -*-
"""卷面文字里的 LaTeX 公式 → Word / PDF 里真能看的东西。

## 为什么需要这个文件（动之前先读）

HTML 导出把 `$…$` 交给浏览器里的 KaTeX，天然是渲染好的。
但 **DOCX 和 PDF 没有 LaTeX 引擎** —— 以前的做法是照原文印出去，卷面上就出现
「已知函数 $f(x)=x^2$ 在 $x=1$ 处…」这种一堆美元符号的东西。用户 2026-11-12 报的就是它。

现在统一走「公式先渲成小图，再嵌进文字流」：
  · DOCX —— python-docx 的 `run.add_picture()`，Word 原生行内图；
  · PDF  —— PyMuPDF 的 `insert_htmlbox()`，HTML 里的 `<img src="data:…">`。
两边的**分段规则、字号换算、退路**全部出自这里，格式层只负责「把 pieces 画出来」。
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

from . import latex_img

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
def docx_runs(par, text, font_pt: float, bold: bool = False,
              color: tuple[int, int, int] = (0, 0, 0)):
    """往一个 python-docx 段落里追加「文字 + 公式图」的 run 序列。

    ⚠️ 公式图和文字**必须在同一段落**里：分成两个 paragraph 就成了「公式另起一行」，
        而卷面上的公式大多是嵌在句子中间的（「求 $a$ 的取值范围」）。
    ⚠️ 高度用 `Pt(img.height)`：公式图是 dpi=72 渲的，1px = 1pt，直接对应。
        python-docx 会按这个高度等比缩放，所以宽度不用管。
    ⚠️ 文字里的 `\\n` 要显式转 `add_break()` —— python-docx 的 run 里那个字符
        不会变成换行，只会被 Word 吞掉，题干的多段就全挤成一行了。
    """
    from docx.shared import Pt

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

    for p in pieces(text, font_pt, color):
        if p["t"] == "text" or not p.get("img"):
            _text(p["v"])                   # 退路的可读文本也走这里
            continue
        buf = io.BytesIO()
        p["img"].save(buf, "PNG")
        buf.seek(0)
        _run().add_picture(buf, height=Pt(p["img"].height))
