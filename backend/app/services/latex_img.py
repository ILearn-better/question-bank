# -*- coding: utf-8 -*-
"""批注里的 LaTeX 公式 → 一张可以直接贴进导出图的小图。

## 为什么单独一个文件（动之前先读）

① **屏幕上是 KaTeX，导出是 matplotlib mathtext —— 两套引擎，一个口径**。
   前端 `src/gradeLabelMath.js` 负责「屏幕上怎么显示」，这里负责「导出 PDF 里怎么显示」。
   **定界符规则必须和它逐字一致**（`$$` → `\\[` → `$` → `\\(`，且 `$$` 先于 `$`），
   否则就会出现最难解释的那种差异：屏幕上明明是公式，导出的 PDF 里却是一串美元符号。

② **Pillow 不会排版公式**。导出走的是「在页图 PNG 上画」（不碰 PyMuPDF，AGPL 边界），
   而 Pillow 只会画字，不认 `\\frac`。所以公式先用 matplotlib 的 mathtext 渲成透明 PNG，
   再由 `grade_export._draw_text` 贴到行里。matplotlib 早就在 requirements 里
   （当初就是为这件事装的），不是新依赖、也没把 AGPL 带进来。

③ **mathtext 不是 KaTeX，符号集小一圈**。最坑的一个：`\\ge` 在 mathtext 里叫 `\\geq`，
   直接喂进去抛 `ParseFatalException: Unknown symbol: \\ge`。所以下面有一张别名表，
   把常用的 KaTeX 写法映射过去。映射不到 → **退回纯文本** 并在 shike.log 留一行：
   导出不该因为一个符号整页失败，但也不能悄悄吞掉、让人以为渲染成功了。

④ **环境（`\\begin{aligned}` 一类）mathtext 一个都不认**（2026-11-03 实测：抛
   `Unknown symbol: \\begin`）。而老师在批注里写多行推导，出来的恰好全是
   `$$ \\begin{aligned} … \\end{aligned} $$` —— 于是导出把整段反斜杠原文印在卷子上
   （用户 2026-11-03 报的「文本框中的 latex 导出的不是渲染好的」就是这个）。
   处理办法**不是**放弃，而是自己当排版引擎：把环境体按行拆开、按 `&` 分列，
   逐格渲成小图再拼成一张（`_env_image`）。环境名本身丢掉即可，mathtext 根本不需要它。

## 判据
`split_math` 的行为与前端 `splitMath` 逐条对齐（单测 `test_grade_export_math.py` 守住）；
`math_image` 失败返回 `None`，**永不抛异常** —— 让排版层决定退路。
退路是 `plain_text()`（把 `\\times` 之类翻成 `×`），**不是**把反斜杠原文印出去。
"""
from __future__ import annotations

import io
import logging
import re
from functools import lru_cache

from PIL import Image, ImageOps

_log = logging.getLogger("shike")

# 与前端 `src/gradeLabelMath.js` 的 MATH_RE **逐字相同**（含 `$$` 先于 `$` 的次序）。
# ⚠️ 改这里必须同步改那边，否则屏幕与导出会分叉。
MATH_RE = re.compile(
    r"\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\$([^$\n]+?)\$|\\\(([\s\S]+?)\\\)"
)

# KaTeX → mathtext 的常用别名。只收「同一个符号、两个叫法」这一类，
# 不做语法翻译 —— 真需要复杂排版的地方，退回纯文本比画错强。
_ALIASES = (
    (r"\ge", r"\geq"),
    (r"\le", r"\leq"),
    (r"\ne", r"\neq"),
    (r"\to", r"\rightarrow"),
    (r"\gets", r"\leftarrow"),
    (r"\implies", r"\Rightarrow"),
    (r"\iff", r"\Leftrightarrow"),
    (r"\dfrac", r"\frac"),
    (r"\tfrac", r"\frac"),
    (r"\degree", r"^{\circ}"),
    (r"\text", r"\mathrm"),
    (r"\lvert", r"|"),
    (r"\rvert", r"|"),
    (r"\mid", r"|"),
    (r"\parallel", r"\Vert"),
    (r"\because", r"\because"),
    (r"\therefore", r"\therefore"),
)


def split_math(text) -> list[dict]:
    """把一段批注切成「文字 / 公式」交替的片段。

    没配对的定界符（比如老师写「花了 $5」）当普通文字 —— 与前端同一口径：
    不该因为一个孤零零的 `$` 让整段话消失。
    """
    s = "" if text is None else str(text)
    out: list[dict] = []
    last = 0
    for m in MATH_RE.finditer(s):
        if m.group(0) == "":
            continue
        if m.start() > last:
            out.append({"t": "text", "v": s[last:m.start()]})
        display = m.group(1) is not None or m.group(2) is not None
        tex = next(g for g in m.groups() if g is not None)
        out.append({"t": "math", "tex": tex, "display": display})
        last = m.end()
    if last < len(s):
        out.append({"t": "text", "v": s[last:]})
    return out


def _normalize(tex: str) -> str:
    """别名替换。按「命令后面不能紧跟字母」来切，避免 `\\geq` 被 `\\ge` 命中后变成 `\\geqq`。"""
    out = str(tex or "")
    for src, dst in _ALIASES:
        if src == dst:
            continue
        out = re.sub(re.escape(src) + r"(?![A-Za-z])", dst.replace("\\", "\\\\"), out)
    return out


# ---------------------------------------------------------------------------
# 环境：mathtext 一个都不认（`\begin` / `\end` 直接抛），所以下面自己拆行拼版
# ---------------------------------------------------------------------------

# 认识的列式环境。不收 `equation` 这类「天然只有一列」的 —— 拆成行就对了，
# 但 `figure` / `tabular` 那种带非数学内容的**不做**（宁可退回纯文本）。
_ENV_NAMES = (
    "aligned", "alignedat", "align", "align*", "split", "gathered", "gather", "gather*",
    "array", "eqnarray", "eqnarray*",
    "cases", "dcases", "rcases",
    "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix",
)

# 整个公式就是「一个环境」的形态。
_ENV_ONE_RE = re.compile(
    r"^\s*\\begin\{(" + "|".join(re.escape(n) for n in _ENV_NAMES) + r")\}"
    r"([\s\S]*)\\end\{\1\}\s*$"
)
# 公式里**夹着**一个环境（`f(x)=\begin{cases}…` 这种最常见的写法）
_ENV_ANY_RE = re.compile(r"\\begin\{(" + "|".join(re.escape(n) for n in _ENV_NAMES) + r")\}")

# 行分隔：真正的 `\\`；以及**被上游吃成单个反斜杠**的 `\ `（AI → Markdown 那一趟会把
# `\\` 折成 `\ `，库里的批注就是这样存的）。`\times` 之类「反斜杠+字母」不会被误伤，
# 因为这里要求反斜杠后面紧跟空白。
_ROW_SEP_RE = re.compile(r"\\\\|\\(?=\s)")

# 按 `&` 分列时，交错对齐（aligned 的传统口径：右、左、右、左…）
_ALIGN_RL = ("aligned", "alignedat", "align", "align*", "split", "gathered", "gather", "gather*")
_ALIGN_L = ("cases", "dcases", "rcases")


@lru_cache(maxsize=1)
def _mathtext():
    """matplotlib 的 (math_to_image, FontProperties)。**延迟导入**：import 要几百毫秒，
    而导出是低频操作，不该让服务启动 / 每个请求都付这个钱。"""
    from matplotlib.font_manager import FontProperties
    from matplotlib.mathtext import math_to_image
    return math_to_image, FontProperties


def _cell_gray(tex: str, px: int) -> Image.Image | None:
    """一格公式 → 灰度小图（空串返回 None）。**失败要抛**：调用方决定整块退回纯文本。"""
    s = str(tex or "").strip()
    if not s:
        return None
    math_to_image, FP = _mathtext()
    buf = io.BytesIO()
    math_to_image("$" + s + "$", buf, prop=FP(size=int(px)), dpi=72, format="png")
    buf.seek(0)
    # mathtext 出的是白底黑字的 RGB，转 L 正好可以后面拿亮度当 alpha（见 math_image）
    return Image.open(buf).convert("L")


def _env_image(tex: str, px: int) -> Image.Image | None:
    """带 `\\begin{aligned}` 一类环境的公式 → 一张（可能多行）灰度图。拆不开返回 None。

    先按「整条就是一个环境」试；不是的话，把环境**夹在中间**的写法也接住
    （`f(x)=\\begin{cases}…\\end{cases}`）：环境前面那段（`f(x)=`）和后面那段各自
    渲成小图，跟列式块**左右并排、竖直居中** —— 少了这一步，最常见的那种分段函数
    会整条掉进纯文本退路。
    """
    one = _ENV_ONE_RE.match(tex)
    if one:
        return _rows_image(one.group(1), one.group(2), px)

    m = _ENV_ANY_RE.search(tex)
    if not m:
        return None
    name = m.group(1)
    tail = "\\end{" + name + "}"
    end = tex.find(tail, m.end())
    if end < 0:
        return None
    rows = _rows_image(name, tex[m.end():end], px)
    if rows is None:
        return None
    pre_s, post_s = tex[:m.start()], tex[end + len(tail):]
    try:
        pre = _cell_gray(pre_s, px) if pre_s.strip() else None
        post = _cell_gray(post_s, px) if post_s.strip() else None
    except Exception:                      # noqa: BLE001 —— 前后那截认不出 → 交回上层退纯文本
        return None
    parts = [im for im in (pre, rows, post) if im is not None]
    if len(parts) == 1:
        return rows
    gap = max(2, int(round(px * 0.16)))
    canvas = Image.new("L", (sum(im.width for im in parts) + gap * (len(parts) - 1),
                             max(im.height for im in parts)), 255)
    x = 0
    for im in parts:
        canvas.paste(im, (int(x), int((canvas.height - im.height) / 2)))
        x += im.width + gap
    return canvas


def _rows_image(name: str, body: str, px: int) -> Image.Image | None:
    """环境体 → 多行图。按 `\\\\` 拆行、按 `&` 分列，列宽取该列最大。

    列内对齐按环境习惯：aligned 右/左交错、cases 全左、matrix 居中。
    行高取该行最高的一格，格子在行内竖直居中。`cases` 额外画左手大括号 ——
    少了它，分段函数看着就是两行散式子。
    """
    if name in ("array", "alignedat") and body.lstrip().startswith("{"):
        # `\begin{array}{|c|c|}` / `\begin{alignedat}{2}` 的那段**列格式**要丢掉，
        # 否则它会被当成第一行的一格交给 mathtext（`{|c|c|} a` 必然解析失败）
        body = re.sub(r"^\s*\{[^{}]*\}", "", body, count=1)
    rows = [r for r in (c.strip() for c in _ROW_SEP_RE.split(body)) if r]
    if not rows:
        return None

    cells: list[list[Image.Image | None]] = []
    try:
        for row in rows:
            cells.append([_cell_gray(c, px) for c in row.split("&")])
    except Exception:                      # noqa: BLE001 —— 有一格认不出就让整块退纯文本
        return None

    gap_x = max(3, int(round(px * 0.30)))
    gap_y = max(2, int(round(px * 0.22)))
    ncol = max((len(c) for c in cells), default=0)
    colw = [0] * ncol
    for imgs in cells:
        for i, im in enumerate(imgs):
            if im is not None:
                colw[i] = max(colw[i], im.width)
    rowh = [max([im.height for im in imgs if im is not None] or [0]) for imgs in cells]
    if sum(colw) <= 0 or sum(rowh) <= 0:
        return None

    canvas = Image.new("L", (sum(colw) + gap_x * max(0, ncol - 1),
                             sum(rowh) + gap_y * max(0, len(rowh) - 1)), 255)

    y = 0
    for imgs, rh in zip(cells, rowh):
        x = 0
        for i, im in enumerate(imgs):
            if im is not None:
                if name in _ALIGN_L:
                    dx = x
                elif name in _ALIGN_RL:
                    dx = x if i % 2 else x + (colw[i] - im.width)   # 偶数列靠右
                else:
                    dx = x + (colw[i] - im.width) // 2
                canvas.paste(im, (int(dx), int(y + (rh - im.height) / 2)))
            x += colw[i] + gap_x
        y += rh + gap_y

    if name in _ALIGN_L:
        canvas = _with_brace(canvas, px)
    return canvas


def _with_brace(canvas: Image.Image, px: int) -> Image.Image:
    """给 cases 左边补一个贯穿整块的大括号。画不出来就原样返回（**不因为装饰失败丢内容**）。"""
    try:
        brace = _cell_gray(r"\{", max(px * 2, int(canvas.height * 0.92)))
        if brace is None:
            return canvas
        h = max(1, canvas.height)
        w = max(2, int(round(brace.width * h / brace.height)))
        brace = brace.resize((w, h), Image.LANCZOS)
        out = Image.new("L", (canvas.width + w + max(2, px // 8), canvas.height), 255)
        out.paste(brace, (0, 0))
        out.paste(canvas, (w + max(2, px // 8), 0))
        return out
    except Exception:                      # noqa: BLE001
        return canvas


# ---------------------------------------------------------------------------
# 公式里的中文：mathtext 没有 CJK 字形（\text{面积} 会渲成豆腐块）
# ---------------------------------------------------------------------------

_CJK = r"\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
_CJK_RE = re.compile("[" + _CJK + "]+")
# 包着中文的排版壳子（\text{面积} / \mathrm{面积}），剥掉壳子留内容
_TEXT_WRAP_RE = re.compile(
    r"\\(?:text|textrm|textbf|textit|mbox|mathrm|mathbf|mathit)\s*\{\s*([^{}]*)\s*\}"
)


def split_cjk(tex: str) -> list[dict]:
    """把一条公式按**中文**切成「文字 / 公式」交替的片段。

    为什么要在排版层切：mathtext 的字体里一个汉字都没有，`\\text{面积}=12` 会渲成
    「□=12」（matplotlib 还会在 stderr 抱怨 "does not have a glyph"）。这里把中文挑出来
    交给批注字体当普通文字排，剩下的才是真公式 —— 两边都不丢。

    没有中文就原样一条返回，调用方不必分支。
    """
    s = str(tex or "")
    if not _CJK_RE.search(s):
        return [{"t": "math", "tex": s}] if s.strip() else []
    # 只剥「内容里有中文」的壳子；`\text{if }` 这种英文壳子照旧留给 mathtext
    s = _TEXT_WRAP_RE.sub(lambda m: m.group(1) if _CJK_RE.search(m.group(1)) else m.group(0), s)
    out: list[dict] = []
    for piece in re.split("(" + _CJK_RE.pattern + ")", s):
        if not piece:
            continue
        if _CJK_RE.fullmatch(piece):
            out.append({"t": "text", "v": piece})
        elif piece.strip():
            out.append({"t": "math", "tex": piece})
    return out


# ---------------------------------------------------------------------------
# 退路：真的渲不出来时，翻成**人能读的纯文本**（不是把反斜杠原文印给家长）
# ---------------------------------------------------------------------------

_SYMBOL = {
    r"\times": "×", r"\div": "÷", r"\cdot": "·", r"\pm": "±", r"\mp": "∓",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠",
    r"\approx": "≈", r"\equiv": "≡", r"\propto": "∝",
    r"\infty": "∞", r"\angle": "∠", r"\triangle": "△", r"\perp": "⊥", r"\parallel": "∥",
    r"\because": "∵", r"\therefore": "∴", r"\degree": "°", r"\circ": "°",
    r"\pi": "π", r"\theta": "θ", r"\alpha": "α", r"\beta": "β", r"\gamma": "γ",
    r"\delta": "δ", r"\Delta": "Δ", r"\lambda": "λ", r"\mu": "μ", r"\sigma": "σ",
    r"\omega": "ω", r"\varphi": "φ", r"\Sigma": "Σ",
    r"\rightarrow": "→", r"\to": "→", r"\leftarrow": "←",
    r"\Rightarrow": "⇒", r"\implies": "⇒", r"\Leftarrow": "⇐",
    r"\Leftrightarrow": "⇔", r"\iff": "⇔",
    r"\sum": "Σ", r"\prod": "Π", r"\int": "∫",
    r"\in": "∈", r"\notin": "∉", r"\subset": "⊂", r"\supset": "⊃",
    r"\cup": "∪", r"\cap": "∩", r"\forall": "∀", r"\exists": "∃",
    r"\%": "%", r"\&": "&", r"\#": "#",
    r"\quad": "  ", r"\qquad": "    ",
}
# 长命令先匹配，否则 `\rightarrow` 会被 `\to` 之类截胡（这里是子串风险，不是正则交替）
_SYMBOL_KEYS = sorted(_SYMBOL, key=len, reverse=True)

_FRAC_RE = re.compile(r"\\(?:frac|dfrac|tfrac)\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_SQRT_RE = re.compile(r"\\sqrt\s*\{([^{}]*)\}")
_SQRT_N_RE = re.compile(r"\\sqrt\s*\[([^\]]*)\]\s*\{([^{}]*)\}")
_CMDNAME_RE = re.compile(r"\\([A-Za-z]+)")
_LONGBAR_RE = re.compile(
    r"\\(?:overline|underline|bar|vec|hat|widehat|dot|ddot|tilde|boxed)\s*\{([^{}]*)\}"
)


def plain_text(tex: str) -> str:
    """LaTeX 片段 → 可读文本。**任何输入都不抛异常**，最差是原样返回。

    守的底线：宁可印 `≤`、印 `(a)/(b)`，也不要把 `\\leq`、`\\frac{a}{b}` 印在
    发给家长的卷子上 —— 后者看着像程序坏了。**未知命令保留名字**（`\\foo` → `foo`），
    老师能看出「这里有个东西没渲出来」，但至少不是一串反斜杠。
    """
    s = str(tex or "")
    if not s:
        return ""
    try:
        s = re.sub(r"\\(?:begin|end)\s*\{[^{}]*\}", "", s)      # 环境名本身没有信息量
        s = _ROW_SEP_RE.sub("\n", s)                            # 行分隔 → 真换行
        s = s.replace("&", "  ")
        for _ in range(6):                                      # 嵌套的壳子要剥几轮
            before = s
            s = _SQRT_N_RE.sub(r"\1√(\2)", s)
            s = _SQRT_RE.sub(r"√(\1)", s)
            s = _FRAC_RE.sub(r"(\1)/(\2)", s)
            s = _LONGBAR_RE.sub(r"\1", s)
            s = _TEXT_WRAP_RE.sub(r"\1", s)
            s = re.sub(r"\\left|\\right", "", s)
            if s == before:
                break
        for k in _SYMBOL_KEYS:
            s = re.sub(re.escape(k) + r"(?![A-Za-z])", _SYMBOL[k], s)
        s = _CMDNAME_RE.sub(lambda m: m.group(1), s)            # 认不出的：留名字、去反斜杠
        s = s.replace("\\", "").replace("{", "").replace("}", "")
        return re.sub(r"[ \t]{3,}", "  ", s).strip()
    except Exception:                                           # noqa: BLE001 —— 退路自己不许炸
        return str(tex or "")


@lru_cache(maxsize=128)
def _math_png(tex: str, px: int) -> bytes | None:
    """渲成 PNG 字节（失败返回 None）。函数级缓存：同一句批注在一份卷子里可能出现很多次。

    matplotlib 是**延迟导入**的（见 `_mathtext`）：它 import 要几百毫秒，而导出是
    低频操作，不该让服务启动 / 每个请求都付这个钱。
    """
    try:
        math_to_image, FP = _mathtext()
    except Exception as e:                      # noqa: BLE001 —— 没装也不该炸掉导出
        _log.warning("公式渲染不可用（matplotlib 没装上）：%s", e)
        return None
    tex = _normalize(tex)
    if not tex.strip():
        return None

    # ① 环境：mathtext 不认 `\begin`，自己拆行拼（见 `_env_image`）
    if _ENV_ANY_RE.search(tex):
        img = _env_image(tex, px)
        if img is not None:
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        _log.warning("多行环境拆不开，导出退回纯文本：%r", tex[:80])

    # ② 普通公式。mathtext **必须**有 $ 包裹；前端传进来的 tex 是去掉定界符的。
    buf = io.BytesIO()
    try:
        math_to_image("$" + tex + "$", buf, prop=FP(size=int(px)), dpi=72, format="png")
    except Exception as e:                      # noqa: BLE001 —— 认不出的命令/字符都在这里
        _log.warning("公式渲染失败，导出退回纯文本：%r —— %s", tex, str(e).splitlines()[-1][:160])
        return None
    return buf.getvalue()



@lru_cache(maxsize=128)
def math_image(tex: str, px: int, color: tuple[int, int, int]) -> Image.Image | None:
    """公式 → 着色后的 RGBA 小图。渲不出来返回 `None`（调用方退回纯文本）。

    ⚠️ **mathtext 出的是「白底黑字、alpha 全 255」的 RGB，不是透明底 PNG**（实测
    34×28 那张的 alpha 只有 255 一个值）。所以不能拿 alpha 当镂空 —— 那样贴上去
    就是一个**实心色块**（2026-11-02 第一版就是这么画的，导出图里公式变成一条蓝条）。
    正确做法：用**亮度取反**当 alpha —— 白底→0（透明）、黑字→255（不透明），
    再把颜色铺上去。
    """
    raw = _math_png(tex, px)
    if not raw:
        return None
    try:
        gray = Image.open(io.BytesIO(raw)).convert("L")
    except Exception:                           # noqa: BLE001
        return None
    solid = Image.new("RGBA", gray.size, (color[0], color[1], color[2], 255))
    solid.putalpha(ImageOps.invert(gray))
    solid.load()
    return solid
