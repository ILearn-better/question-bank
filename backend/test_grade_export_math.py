# -*- coding: utf-8 -*-
"""导出 PDF 里的 **LaTeX 公式**：屏幕上是 KaTeX，导出也必须像公式，不能印出 `$x^2$`。

用户 2026-11-01 原话：「文本框最好能够渲染 latex，这样我可以批注数学公式」。
屏幕上（`src/gradeLabelMath.js` + KaTeX）当时已经修好了；**导出这条路还留着旧账** ——
`grade_export._draw_text` 用 Pillow 把 `m.text` 原样画进 PDF，于是老师发出的副本上
写着 `这一步漏了讨论 $x^2-3x+2=0$ 的情况`。发给家长的东西，这条不能忍。

这个脚本守四件事（都是真跑、不 mock 掉核心逻辑）：

  ① **定界符规则和前端逐字一致**：`$$ … $$` / `\\[ … \\]` / `$ … $` / `\\( … \\)`，
     且 `$$` 必须先于 `$` 匹配；**孤零零一个 `$`**（老师写「花了 $5」）要当普通文字，
     不许把后半段整段吞成公式。
  ② **公式真的被渲成了图，并且贴进了页面**（不是只算出行宽就算了）。
     这条用「把 math_image 换成一块纯绿色小图，再去数页面上的绿像素」来验 ——
     只看布局结构的话，「算了但没画」和「画了」一模一样。
  ③ **渲不出来的公式退回纯文本、且去掉定界符**：PDF 上出现 `\\unknowncmd{x}` 可以接受
     （log 里有警告），出现 `$\\unknowncmd{x}$` 不行 —— 那看着像程序坏了。
  ④ **有宽高的文本框按新口径落位**（左上角在锚点）；老数据（零面积点）仍按旧口径
     （底边在锚点）。这是同一件事的两个分支，画错了框会整体上浮一个框高 ——
     屏幕上 `4e8a096` 已经把 `.sized` 的口径改过来了，导出不能还留在旧的。
  ⑤ **多行环境（aligned / cases / matrix / array）自己拼版**（2026-11-03 补）。
     matplotlib mathtext 一个环境都不认（`Unknown symbol: \\begin`），改之前多行推导
     整段退成反斜杠原文印在卷子上 —— 用户原话「文本框中的 latex 导出的不是渲染好的」。
     这里守：三行真是三行、cases 有左大括号、`f(x)=…` 夹在中间也认、
     上游吃成 `\\ ` 的行分隔也认、排完版文字里一个 `begin`/反斜杠都没有。
  ⑥ **display 公式独占一行**（与屏幕 KaTeX 的 displayMode 同口径），且紧跟的那个 `\\n`
     不再空出一整行 —— 但批注里**真**的空行要留着。
  ⑦ **公式里的中文走文字层**：mathtext 没有汉字字形，硬渲是豆腐块；劈开后某段渲不出时
     **整条**退回可读文本，不许印出 `frac(` 这种断口。
  ⑧ **退路是「人能读的文本」**：`\\leq` → `≤`、`\\frac{a}{b}` → `(a)/(b)`，
     环境名 / `&` / 反斜杠一个都不许印出来；未知命令保留名字（诚实，但不像程序坏了）。

⚠️ 不起服务、不碰真库：纯图像计算 + 几张假的 mark 对象。
⚠️ 临时目录在 import app 之前设好（engine 在 import 时绑定路径）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_export_math_scratch"
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.* 之前设：engine 的路径在 import 时就定死了
os.environ.setdefault("SHIKE_DB_PATH", str(TMP / "export_math.db"))
os.environ.setdefault("SHIKE_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("SHIKE_AUTO_BACKUP", "0")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PIL import Image                                    # noqa: E402

from app.services import grade_export as E               # noqa: E402
from app.services import latex_img as L                  # noqa: E402

total = 0
fails: list[str] = []


def check(name: str, ok, detail=None) -> None:
    global total
    total += 1
    mark = "[OK ]" if ok else "[FAIL]"
    print(f"  {mark} {name}" + ("" if detail is None else f": {detail}"))
    if not ok:
        fails.append(name)


class FakeMark:
    """只要有一个 mark 形状（kind / text / geo / color / points）就够，不用进库。"""

    def __init__(self, text: str, geo, color: str = "#2563eb", kind: str = "text"):
        self.kind = kind
        self.text = text
        self.geo = list(geo)
        self.color = color
        self.points = []


def page(w: int = 800, h: int = 1100) -> Path:
    p = TMP / "page.png"
    Image.new("RGB", (w, h), (255, 255, 255)).save(p)
    return p


def ink_bbox(img: Image.Image):
    """所有「不是白底」的像素的包围盒 —— 用来量框画到哪儿了。"""
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if px[x, y] < 240:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------- ① 定界符口径
def t_split():
    print("\n① 定界符：与前端 splitMath 同一口径")
    parts = L.split_math("$x^2$")
    check("`$x^2$` → 一条公式", [p["t"] for p in parts] == ["math"], parts)
    check("公式正文不带定界符", bool(parts) and parts[0].get("tex") == "x^2", parts)

    parts = L.split_math("$$a+b$$")
    check("`$$a+b$$` 是 display 公式", bool(parts) and parts[0].get("display") is True, parts)

    parts = L.split_math("$$x$$")
    check("★★ `$$x$$` 不被拆成两个空公式（$$ 必须先于 $ 匹配）",
          [p["t"] for p in parts] == ["math"] and parts[0].get("tex") == "x", parts)

    parts = L.split_math("花了 $5")
    check("★★ 孤零零的 `$` 当普通文字（不许把后半句吞成公式）",
          all(p["t"] == "text" for p in parts), parts)
    check("孤零零的 `$` 连字符都还在",
          "".join(p["v"] for p in parts if p["t"] == "text") == "花了 $5", parts)

    parts = L.split_math("这一步漏了讨论 $x^2$ 的情况")
    check("文字 / 公式 / 文字 三段",
          [p["t"] for p in parts] == ["text", "math", "text"], parts)

    parts = L.split_math("\\[a\\] 与 \\(b\\)")
    check("`\\[..\\]` 与 `\\(..\\)` 也认", [p["t"] for p in parts] == ["math", "text", "math"], parts)

    check("空串不炸", L.split_math("") == [], L.split_math(""))
    check("None 不炸", L.split_math(None) == [], L.split_math(None))

    # ★★★ 屏幕（KaTeX，前端）与导出（mathtext，后端）**必须用同一个定界符正则**。
    #     两边各写一份正则、哪天有人只改一边 → 「屏幕上好好的公式，导出的 PDF 里是 $」，
    #     这类差异最难解释。所以直接把两份正则的字面量拿来逐字比对。
    import re as _re
    js = (HERE.parent / "frontend" / "src" / "gradeLabelMath.js").read_text(encoding="utf-8")
    m = _re.search(r"const MATH_RE = /(.*?)/g;", js, _re.S)
    check("★★★ 前端 gradeLabelMath.js 与后端 latex_img.py 的定界符正则逐字相同",
          bool(m) and m.group(1) == L.MATH_RE.pattern,
          None if not m else ("前端 " + m.group(1) + " ｜ 后端 " + L.MATH_RE.pattern)
          if m.group(1) != L.MATH_RE.pattern else "一致")


# ---------------------------------------------------------------- ② 真的渲成图并贴进页面
def t_paste():
    print("\n② 公式被渲成图、并真的贴进了页面")
    img = L.math_image("x^2", 26, (220, 38, 38))
    check("`x^2` 能渲出图", img is not None and img.size[0] > 0, None if img is None else img.size)

    # ⚠️ 这条是 2026-11-02 第一版翻车的地方：mathtext 出的是「白底黑字、alpha 全 255」，
    #    拿 alpha 当镂空 → 贴上去是一块**实心色块**。判据是有没有透明像素。
    if img is not None:
        alphas = set()
        for y in range(img.size[1]):
            for x in range(img.size[0]):
                alphas.add(img.getpixel((x, y))[3])
        check("★★★ 渲出来的图是镂空的（有透明像素）—— 否则贴上去是实心色块",
              len(alphas) > 2 and min(alphas) == 0, f"alpha 取值 {len(alphas)} 种，最小 {min(alphas)}")
        # 四角是白的 → 透明；中间笔画 → 不透明
        check("★★ 左上角透明（白底没被当成笔画）", img.getpixel((0, 0))[3] == 0,
              img.getpixel((0, 0)))
        check("★★ 颜色被染成了批注色（红笔公式就是红的）",
              any(img.getpixel((x, y))[:3] == (220, 38, 38)
                  for y in range(img.size[1]) for x in range(img.size[0])
                  if img.getpixel((x, y))[3] > 200),
              "笔画处应有纯红像素")

    # ★★ 「算了但没画」和「画了」外观上一样 → 换一块**纯绿色**小图，去页面上数绿像素。
    orig = L.math_image
    green = Image.new("RGBA", (12, 12), (0, 255, 0, 255))
    L.math_image = lambda tex, px, color: green
    try:
        out = E._render_page(page(), [FakeMark("看这个 $x^2$ 公式", [100, 300, 100, 300])])
    finally:
        L.math_image = orig
    greens = 0
    for y in range(0, out.size[1]):
        for x in range(0, out.size[0]):
            r, g, b = out.getpixel((x, y))
            if g > 200 and r < 100 and b < 100:
                greens += 1
    check("★★★ 公式图真的被贴到了页面上（数到绿色像素）", greens >= 100, f"{greens} 个绿像素")

    # 布局层：切出来的东西里不该再有 `$`
    lines, bw, bh, base_lh = E._rich_lines(
        __import__("PIL.ImageDraw", fromlist=["ImageDraw"]).Draw(Image.new("RGB", (10, 10))),
        "这一步漏了讨论 $x^2$ 的情况", E._font(24), 24, (37, 99, 235), 400)
    chars = "".join(it["v"] for ln in lines for it in ln["items"] if it["k"] == "chars")
    maths = [it for ln in lines for it in ln["items"] if it["k"] == "math"]
    check("★★ 排好版之后文字里**一个 `$` 都没有**", "$" not in chars, repr(chars))
    check("公式在排版里是独立的一块（不是被当文字拼进去）", len(maths) == 1, len(maths))


# ---------------------------------------------------------------- ③ 渲不出来时的退路
def t_fallback():
    print("\n③ 认不出的公式：退回纯文本、去掉定界符")
    bad = L.math_image("\\unknowncmd{甲}", 26, (0, 0, 0))
    check("★ 认不出的命令返回 None、不抛异常", bad is None, bad)

    orig = L._math_png
    L._math_png.cache_clear()

    def boom(tex: str, px: int):
        return None

    L._math_png = boom
    L.math_image.cache_clear()
    try:
        out = E._render_page(page(), [FakeMark("$\\unknowncmd{甲}$ 这句渲不出来", [100, 300, 100, 300])])
        bb = ink_bbox(out)
        check("★ 渲不出来也要把这段文字画出来（不能整条消失）", bb is not None, bb)
        import PIL.ImageDraw as _ID
        lines, _, _, _ = E._rich_lines(
            _ID.Draw(Image.new("RGB", (10, 10))), "$\\unknowncmd{甲}$ 这句渲不出来",
            E._font(24), 24, (37, 99, 235), 600)
        chars = "".join(it["v"] for ln in lines for it in ln["items"] if it["k"] == "chars")
        check("★★ 退回纯文本时**去掉了定界符**（PDF 上不该出现 $）", "$" not in chars, repr(chars))
        check("★ 命令原文还在（没有悄悄丢内容）", "unknowncmd" in chars, repr(chars))
    finally:
        L._math_png = orig
        L._math_png.cache_clear()
        L.math_image.cache_clear()


# ---------------------------------------------------------------- ⑤ 多行环境
def t_env():
    print("\n⑤ 多行环境（aligned / cases）：mathtext 不认 \\begin，导出自己拼版")
    single = L.math_image("3x+2 = 10", 26, (0, 0, 0))
    three = L.math_image(
        "\\begin{aligned} 3x+2 &= 10 \\\\ 3x &= 8 \\\\ x &= 4 \\end{aligned}", 26, (0, 0, 0))
    check("★ aligned 能渲出图（改之前抛 `Unknown symbol: \\begin`、整段退成反斜杠原文）",
          three is not None, three)
    check("★★★ 三行真的是三行，不是被挤成一行",
          three is not None and single is not None and three.height >= single.height * 2.4,
          None if three is None else (three.size, single.size if single else None))

    # 库里真实存的那条：上游（AI → Markdown）把行分隔 `\\` 吃成了单个 `\ `
    mangled = L.math_image(
        "\\begin{aligned} 120 \\times 5+50x \\leq 800 \\ 600+50x \\leq 800 \\end{aligned}",
        26, (0, 0, 0))
    check("★★ 被吃成 `\\ ` 的行分隔也认（这是库里批注的真实形态）",
          mangled is not None and single is not None and mangled.height >= single.height * 1.7,
          None if mangled is None else mangled.size)

    cases = L.math_image("\\begin{cases} x & x>0 \\\\ -x & x<0 \\end{cases}", 26, (0, 0, 0))
    matrix = L.math_image("\\begin{matrix} x & x>0 \\\\ -x & x<0 \\end{matrix}", 26, (0, 0, 0))
    check("★ cases 也要渲得出（分段函数）", cases is not None, cases)
    check("★★ cases 比 matrix 宽出一个左手大括号（少了它看着就是两行散式子）",
          cases is not None and matrix is not None and cases.width > matrix.width + 4,
          None if (cases is None or matrix is None) else (cases.width, matrix.width))

    mid = L.math_image(
        "f(x)=\\begin{cases} x^2 & x \\ge 0 \\\\ -x & x<0 \\end{cases}", 26, (0, 0, 0))
    check("★★★ 环境夹在表达式中间（`f(x)=…` 这种最常见的写法）也要渲，不许整条退纯文本",
          mid is not None, mid)
    check("★ 带前缀的图比裸环境宽（前缀真的画进去了，不是被吞了）",
          mid is not None and cases is not None and mid.width > cases.width,
          None if (mid is None or cases is None) else (mid.width, cases.width))

    arr = L.math_image("\\begin{array}{|c|c|} a & b \\\\ c & d \\end{array}", 26, (0, 0, 0))
    check("★★ array 的列格式（`{|c|c|}`）被丢掉，不会当成第一行的一格去解析",
          arr is not None, arr)

    # 版面上：不能印出 `begin` / 反斜杠
    import PIL.ImageDraw as _ID
    lines, _, _, _ = E._rich_lines(
        _ID.Draw(Image.new("RGB", (10, 10))),
        "$$\\begin{aligned} a &= 1 \\\\ b &= 2 \\end{aligned}$$", E._font(24), 24,
        (37, 99, 235), 400)
    chars = "".join(it["v"] for ln in lines for it in ln["items"] if it["k"] == "chars")
    maths = [it for ln in lines for it in ln["items"] if it["k"] == "math"]
    check("★★★ 排完版之后文字里没有 `begin`、也没有反斜杠",
          "begin" not in chars and "\\" not in chars, repr(chars))
    check("★ 整块环境是**一块**公式，不是被拆成好几块", len(maths) == 1, len(maths))


# ---------------------------------------------------------------- ⑥ display 公式的版面
def t_display():
    print("\n⑥ display 公式（`$$…$$`）：独占一行，且不空一整行")
    import PIL.ImageDraw as _ID
    d = _ID.Draw(Image.new("RGB", (10, 10)))
    lines, _, _, _ = E._rich_lines(d, "结论：$$x=4$$\n所以最多买 4 个", E._font(24), 24,
                                  (37, 99, 235), 400)
    kinds = [[it["k"] for it in ln["items"]] for ln in lines]
    check("★★ display 公式独占一行（与屏幕 KaTeX 的 displayMode 同口径）",
          len(lines) == 3 and kinds[1] == ["math"], kinds)
    check("★★ display 之后紧跟的换行不再空出一整行（源文就是 `$$…$$\\n文字`）",
          all(ln["items"] for ln in lines), kinds)

    # 单个换行（批注中间真有空行）还得照旧空一行 —— 别把「压掉紧跟的那个 \n」做成「压掉所有空行」
    lines2, _, _, _ = E._rich_lines(d, "第一行\n\n第二行", E._font(24), 24, (37, 99, 235), 400)
    check("★★ 批注里真正的空行要留着（压 `\\n` 只压 display 后面那一个）",
          len(lines2) == 3, [[it["v"] for it in ln["items"]] for ln in lines2])


# ---------------------------------------------------------------- ⑦ 公式里的中文
def t_cjk():
    print("\n⑦ 公式里的中文：mathtext 没有汉字字形，硬渲是豆腐块")
    segs = L.split_cjk("\\text{面积}=12")
    check("★★ 中文被挑出来交给批注字体，剩下的才是公式",
          [s["t"] for s in segs] == ["text", "math"], segs)
    check("★ 中文一个字没丢", bool(segs) and segs[0].get("v") == "面积", segs)
    check("★ 没有中文时原样一条返回（调用方不必分支）",
          L.split_cjk("x^2") == [{"t": "math", "tex": "x^2"}], L.split_cjk("x^2"))

    import PIL.ImageDraw as _ID
    d = _ID.Draw(Image.new("RGB", (10, 10)))
    lines, _, _, _ = E._rich_lines(d, "$\\text{面积}=12$", E._font(24), 24, (37, 99, 235), 400)
    chars = "".join(it["v"] for ln in lines for it in ln["items"] if it["k"] == "chars")
    check("★★ 中文进的是文字层（不是被 mathtext 渲成方块）", "面积" in chars, repr(chars))

    # 中文把公式劈开后，数学段是半个括号（`\frac{`）—— 单独退回会印出 `frac(` 这种断口
    lines2, _, _, _ = E._rich_lines(d, "$\\frac{面积}{2}$", E._font(24), 24, (37, 99, 235), 400)
    chars2 = "".join(it["v"] for ln in lines2 for it in ln["items"] if it["k"] == "chars")
    check("★★★ 劈开后某段渲不出时**整条**退回可读文本，不许出现 `frac(` 这种断口",
          "frac" not in chars2 and "(面积)/(2)" in chars2, repr(chars2))


# ---------------------------------------------------------------- ⑧ 退路是「人能读的文本」
def t_plain():
    print("\n⑧ 退路：翻成可读文本，不是把反斜杠原文印给家长")
    check("★ `\\leq` → ≤", L.plain_text("x \\leq 4").replace(" ", "") == "x≤4",
          L.plain_text("x \\leq 4"))
    check("★ `\\times` → ×", "×" in L.plain_text("2 \\times 3"), L.plain_text("2 \\times 3"))
    check("★ 分数 → `(a)/(b)`", L.plain_text("\\frac{1}{2}") == "(1)/(2)", L.plain_text("\\frac{1}{2}"))
    check("★ 根号 → `√(16)`", L.plain_text("\\sqrt{16}") == "√(16)", L.plain_text("\\sqrt{16}"))
    env_txt = L.plain_text("\\begin{aligned} a &= 1 \\\\ b &= 2 \\end{aligned}")
    check("★★ 环境名 / `&` / 反斜杠都不许印出来",
          "begin" not in env_txt and "&" not in env_txt and "\\" not in env_txt, repr(env_txt))
    check("★ 行还是分行（`\\\\` → 换行）", "\n" in env_txt, repr(env_txt))
    check("★ 未知命令保留名字（老师能看出这里没渲出来，但不像程序坏了）",
          "unknowncmd" in L.plain_text("\\unknowncmd{x}"), L.plain_text("\\unknowncmd{x}"))
    check("★★ 退路自己不许炸", L.plain_text(None) == "" and isinstance(L.plain_text("{" * 40), str),
          repr(L.plain_text("{" * 40)))


# ---------------------------------------------------------------- ④ 有宽高的框怎么落位
def t_sized_anchor():
    print("\n④ 有宽高的文本框：左上角落在锚点；老数据仍是底边落在锚点")
    kx = 800 / 1000.0
    ky = 1100 / 1000.0

    # 新口径：geo 有面积 → 框的上边 ≈ 锚点 y
    m = FakeMark("新口径 $x^2$", [100, 300, 700, 400])
    out = E._render_page(page(), [m])
    bb = ink_bbox(out)
    check("★★ 有宽高的框：上边落在锚点 y（改动前会整体上浮一个框高）",
          bb is not None and abs(bb[1] - 300 * ky) <= 4,
          None if bb is None else f"上边 {bb[1]} vs 锚点 {300 * ky:.0f}")
    check("★★ 有宽高的框：宽度铺到 geo 给的宽度（不是贴着文字收窄）",
          bb is not None and bb[2] >= 700 * kx - 4,
          None if bb is None else f"右边 {bb[2]} vs 框右边 {700 * kx:.0f}")

    # 老口径：零面积点 → 框的**下边** ≈ 锚点 y
    m2 = FakeMark("老数据 $x^2$", [100, 300, 100, 300])
    out2 = E._render_page(page(), [m2])
    bb2 = ink_bbox(out2)
    check("★★ 老数据（零面积点）：下边落在锚点 y",
          bb2 is not None and abs(bb2[3] - 300 * ky) <= 6,
          None if bb2 is None else f"下边 {bb2[3]} vs 锚点 {300 * ky:.0f}")


# ---------------------------------------------------------------- 兜底：不碰 AGPL
def t_guards():
    print("\n兜底：AGPL 边界与「不自作主张」")
    src = (HERE / "app" / "services" / "latex_img.py").read_text(encoding="utf-8")
    check("★ 新模块不碰 pymupdf（AGPL 只准待在 adapters/pdf.py）",
          "pymupdf" not in src and "fitz" not in src)
    exp = (HERE / "app" / "services" / "grade_export.py").read_text(encoding="utf-8")
    check("★ 导出仍按 x/y 分别缩放（改公式不该动这条）",
          "kx, ky = w / 1000.0, h / 1000.0" in exp)
    # 延迟导入的判据是「**模块顶层**没有 import matplotlib」—— 函数体内的（缩进的）不算。
    # ⚠️ 别拿 `"matplotlib" not in src` 当判据：注释里写一句「matplotlib mathtext」就红了，
    #    而那跟导入时机毫无关系（第一版就是这么写错的）。
    top_imports = [ln for ln in src.splitlines()
                   if ln.startswith(("import matplotlib", "from matplotlib"))]
    check("★★ mathtext 是延迟导入的（顶层没有 import matplotlib）",
          not top_imports, top_imports)
    check("★ 但它确实在函数里导了（否则公式永远渲不出来）",
          "matplotlib.mathtext" in src and "from matplotlib.mathtext import" in src)


def main() -> int:
    print("=" * 72)
    print("导出 PDF 里的公式：屏幕 KaTeX / 导出 mathtext，口径要一致")
    print("=" * 72)
    t_split()
    t_paste()
    t_fallback()
    t_env()
    t_display()
    t_cjk()
    t_plain()
    t_sized_anchor()
    t_guards()

    print("\n" + "=" * 72)
    print(f"共 {total} 项，失败 {len(fails)} 项")
    if fails:
        for f in fails:
            print(f"  x {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
