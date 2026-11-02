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
   把常用的 KaTeX 写法映射过去。映射不到 → **退回纯文本（去掉定界符）** 并在
   shike.log 留一行：导出不该因为一个符号整页失败，但也不能悄悄吞掉、让人以为渲染成功了。

## 判据
`split_math` 的行为与前端 `splitMath` 逐条对齐（单测 `test_grade_export_math.py` 守住）；
`math_image` 失败返回 `None`，**永不抛异常** —— 让排版层决定退路。
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


@lru_cache(maxsize=128)
def _math_png(tex: str, px: int) -> bytes | None:
    """渲成 PNG 字节（失败返回 None）。函数级缓存：同一句批注在一份卷子里可能出现很多次。

    matplotlib 是**延迟导入**的：它 import 要几百毫秒，而导出是低频操作，
    不该让服务启动 / 每个请求都付这个钱。
    """
    try:
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import math_to_image
    except Exception as e:                      # noqa: BLE001 —— 没装也不该炸掉导出
        _log.warning("公式渲染不可用（matplotlib 没装上）：%s", e)
        return None
    tex = _normalize(tex)
    # mathtext **必须**有 $ 包裹；前端传进来的 tex 是去掉定界符的。
    if not tex.strip():
        return None
    wrapped = "$" + tex + "$"
    buf = io.BytesIO()
    try:
        math_to_image(wrapped, buf, prop=FontProperties(size=int(px)), dpi=72, format="png")
    except Exception as e:                      # noqa: BLE001 —— 认不出的命令/字符都在这里
        _log.warning("公式渲染失败，导出退回原文（去掉定界符）：%r —— %s", tex, str(e).splitlines()[-1][:160])
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
