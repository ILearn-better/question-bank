# -*- coding: utf-8 -*-
r"""出卷导出：Word / PDF 里的公式必须是**排好版的数学**，不是 `$…$` 原文。

用户 2026-11-12 报的第一条：「导出的 word 和 pdf 中的公式没有渲染好，还是公式符的形式」。

这条以前是**写在注释里的已知不支持**（`paper_export` 开头就写着「服务端没有 LaTeX
排版引擎，$…$ 会原样输出，想要公式就出 HTML」）。但老师拿 Word/PDF 是去印给学生做的，
印出一堆美元符号等于这份卷子不能用 —— 所以现在改成「公式渲成图片、行内嵌入」：
  · DOCX —— python-docx 的 `run.add_picture()`；
  · PDF  —— PyMuPDF `insert_htmlbox()` + data-uri 的 <img>。
底层是 `latex_img.py`（matplotlib mathtext），与批改导出同一套。

本文件的判据都是**老师看得见的东西**：
  A `paper_math` 分段与退路（公式渲不出来时不许印反斜杠原文）
  B DOCX：没有 `$`、图片真的进文档、中文还在、答案区也算
  C PDF ：没有 `$`、公式像素真的画上去了、中文还在
  D 回归：没公式的段落行为不变；HTML 版仍把 `$…$` 留给 KaTeX

⚠️ 判据不假设实现用 Story、insert_htmlbox 还是别的 —— 那是实现自由。
   只认「文档里有没有美元符号」「页面上那个位置有没有墨」。
"""
import io
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf                                          # noqa: E402

from app.services import latex_img                      # noqa: E402
from app.services import paper_export, paper_math       # noqa: E402

fails = []


def check(label, got, want):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}")
    if not okk:
        print(f"       得到 {got!r}")
        print(f"       期望 {want!r}")
        fails.append(label)


def truthy(label, got, detail=""):
    okk = bool(got)
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}")
    if not okk:
        print(f"       得到 {got!r}（期望为真）{detail}")
        fails.append(label)


def falsy(label, got, detail=""):
    okk = not got
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}")
    if not okk:
        print(f"       得到 {got!r}（期望为假）{detail}")
        fails.append(label)


# ---------------------------------------------------------------- 造题
def q(n, content, **kw):
    """一道卷面题。字段与 `routers/papers.py` 组出来的 item 同形。"""
    d = {
        "id": n, "n": n, "content": content, "image": "", "figure_image": "",
        "answer": "", "answer_image": "", "analysis": "",
        "qtype": "解答题", "difficulty": "中档", "tags": [], "render_prefer": "auto",
        "score": None,
    }
    d.update(kw)
    return d


OPTS = {"show_answer": True, "show_analysis": True, "show_tags": True, "show_meta": True}


# ================================================================ A. paper_math
def part_a():
    print("\n==== A paper_math：分段与退路 ====")

    check("有公式时 has_math 为真", paper_math.has_math("求 $x^2$ 的值"), True)
    check("没公式时 has_math 为假", paper_math.has_math("求 x 的值"), False)

    ps = paper_math.pieces("求 $x$ 的值", 11)
    kinds = [p["t"] for p in ps]
    check("切成「文字 / 公式 / 文字」三段", kinds, ["text", "math", "text"])
    check("公式段真的渲出了图", "img" in ps[1] and ps[1]["img"] is not None, True)
    check("公式段记着原始 tex", ps[1].get("tex"), "x")

    # 退路：mathtext 不认的命令 → 必须变成可读文本，绝不能留着反斜杠
    bad = r"$\nosuchcommand{a} + 1$"
    ps2 = paper_math.pieces(bad, 11)
    m = [p for p in ps2 if p["t"] == "math"]
    check("坏公式仍然产出 math 段（不是被丢掉）", len(m), 1)
    falsy("坏公式没有渲出图片", m[0].get("img"), "（渲出来了说明 mathtext 认得这个命令，换一个再试）")
    truthy("坏公式退回了可读文本", (m[0].get("v") or "").strip())
    falsy("退路文本里没有反斜杠原文", "\\" in (m[0].get("v") or ""),
          f"得到 {m[0].get('v')!r}")

    # display 公式
    ps3 = paper_math.pieces("$$x^2$$", 11)
    truthy("$$…$$ 被标成 display", any(p.get("display") for p in ps3 if p["t"] == "math"))

    # \times 这类应该翻成 ×（plain_text 的活）
    ps4 = paper_math.pieces(r"$a \unknowncmd b$", 11)
    v = "".join(p.get("v") or "" for p in ps4 if p["t"] == "math")
    truthy("退路把公式变成人能读的字", v.strip(), f"得到 {v!r}")


# ================================================================ B. DOCX
def docx_xml(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read("word/document.xml").decode("utf-8")


def docx_text(data: bytes) -> str:
    """所有 <w:t> 里的字，连起来。"""
    return "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", docx_xml(data), re.S))


def part_b():
    print("\n==== B DOCX：公式进 Word 是图片，不是美元符号 ====")

    items = [
        q(1, r"已知函数 $f(x)=x^2+2x$ 在 $x=1$ 处的值为多少？",
          answer="$f(1)=1+2=3$", analysis="代入 $x=1$ 即可。"),
        q(2, "这道题没有任何公式，纯文字。", answer="42"),
    ]
    data = paper_export.build_docx("公式测试卷", items, OPTS)
    truthy("导出的 docx 非空", data)
    truthy("是合法的 docx（zip）", zipfile.is_zipfile(io.BytesIO(data)))

    xml = docx_xml(data)
    txt = docx_text(data)
    falsy("文档里没有美元符号（题目 1 的四个公式都渲掉了）", "$" in txt, f"文字：{txt[:120]!r}")
    truthy("中文题干还在", "已知函数" in txt, txt[:120])
    truthy("答案区的中文还在", "代入" in txt or "3" in txt)

    n_draw = xml.count("<w:drawing>")
    # 题 1 题干 2 个 + 答案 1 个 + 解析 1 个 = 4 张；题 2 一个都没有
    check("公式图的数量 = 题干 2 + 答案 1 + 解析 1", n_draw, 4)

    # 纯文本题不该凭空多出图
    items2 = [q(1, "纯文字题目，没有公式。", answer="11")]
    d2 = paper_export.build_docx("纯文本卷", items2, OPTS)
    check("没有公式的卷子一张公式图都不加", docx_xml(d2).count("<w:drawing>"), 0)

    # 多段题干：\n 必须变成 Word 的换行，不能被吞成一行
    items3 = [q(1, "第一段话\n第二段话\n第三段话")]
    d3 = paper_export.build_docx("多段", items3, OPTS)
    x3 = docx_xml(d3)
    truthy("多段题干产生了 <w:br/> 换行", "<w:br/>" in x3, f"br 数 {x3.count('<w:br')}")
    t3 = docx_text(d3)
    truthy("三段文字都在", "第一段话" in t3 and "第三段话" in t3, t3[:80])

    # 坏公式：不许印反斜杠原文
    items4 = [q(1, r"坏公式 $\nosuchcommand{a}$ 在这里")]
    d4 = paper_export.build_docx("坏公式", items4, OPTS)
    t4 = docx_text(d4)
    falsy("坏公式不出美元符号", "$" in t4, t4[:100])
    falsy("坏公式不出反斜杠原文", "\\" in t4, t4[:100])
    truthy("坏公式退成可读文本（还有字）", len(t4.strip()) > 4, t4[:100])


# ================================================================ C. PDF
def pdf_open(data: bytes):
    return pymupdf.open("pdf", data)


def part_c():
    print("\n==== C PDF：公式画上去了，文字层里没有 $ ====")

    items = [
        q(1, r"已知函数 $f(x)=x^2+2x$，求 $f(1)$ 的值。", answer="$f(1)=3$"),
        q(2, "纯文字题，没有公式。"),
    ]
    data = paper_export.build_pdf("公式测试卷", items, OPTS)
    truthy("导出的 pdf 非空", data)
    doc = pdf_open(data)
    txt = "\n".join(p.get_text() for p in doc)
    falsy("PDF 文字层里没有美元符号", "$" in txt, f"文字：{txt[:150]!r}")
    truthy("中文题干还在（没变成豆腐块或消失）", "已知函数" in txt, txt[:150])
    truthy("第二道纯文字题也在", "纯文字题" in txt, txt[:150])
    doc.close()

    # 公式图真的画上去了吗：造一道**只有公式、没有别的字**的题，
    # 于是「这一页除了页眉什么都没有」——页面上有墨就只能是公式图。
    only_math = [q(1, "$x^2+y^2=1$")]
    d2 = paper_export.build_pdf("只有公式", only_math, {"show_answer": False})
    doc2 = pdf_open(d2)
    page = doc2[0]
    pix = page.get_pixmap(dpi=100)
    dark = 0
    # 只看正文区（避开标题那一行）：标题也有字，会干扰判断
    clip = pymupdf.Rect(0, 120, page.rect.width, page.rect.height - 100)
    pix2 = page.get_pixmap(dpi=100, clip=clip)
    for y in range(0, pix2.height, 2):
        for x in range(0, pix2.width, 2):
            if pix2.pixel(x, y)[0] < 120:
                dark += 1
    truthy("只有公式的那道题，正文区真的有墨点（= 公式画出来了）", dark > 20,
           f"暗像素 {dark} 个")
    t2 = page.get_text()
    falsy("只有公式的题，文字层里也没有 $", "$" in t2, f"{t2!r}")
    doc2.close()

    # 多段 \n 必须换行：同样的字，分三段应该比挤成一段占更多垂直空间
    one = paper_export.build_pdf("一段", [q(1, "甲" * 40)], {"show_answer": False})
    three = paper_export.build_pdf("三段", [q(1, "甲" * 12 + "\n" + "甲" * 12 + "\n" + "甲" * 12)],
                                   {"show_answer": False})
    b1 = pdf_text_height(one)
    b3 = pdf_text_height(three)
    truthy("多段题干占的高度更大（\\n 真的换行了）", b3 > b1,
           f"一段 {b1:.1f}pt / 三段 {b3:.1f}pt")

    # 答案区
    da = paper_export.build_pdf("答案", [q(1, "题目", answer="$\\unknowncmd{x}$")], OPTS)
    dca = pdf_open(da)
    ta = "\n".join(p.get_text() for p in dca)
    falsy("答案区里的坏公式也没印出 $", "$" in ta, ta[:150])
    truthy("答案区确实印出来了（有「参考」或数字）", "参考" in ta or "1" in ta, ta[:150])
    dca.close()


def pdf_text_height(data: bytes) -> float:
    """页面上所有文字块的纵向总高度 —— 用来判断「有没有真的换行」。"""
    d = pymupdf.open("pdf", data)
    p = d[0]
    # get_text("blocks") 每项是 (x0, y0, x1, y1, text, block_no, block_type)
    rects = p.get_text("blocks")
    h = (max(r[3] for r in rects) - min(r[1] for r in rects)) if rects else 0.0
    d.close()
    return h


# ============================================================ E. 体积
def part_e():
    print("\n==== E 体积：一份卷子不许几十 MB ====")
    # insert_htmlbox 每调一次就往文件里嵌一份**完整中文字体**（Droid Sans Fallback，
    # 单份 3.4MB）。忘了收尾的话 10 题 = 80MB、20 题 = 153MB、40 题 = 296MB，
    # 老师根本下不动。收尾做了 subset_fonts() + deflate + garbage=4，实测 40 题 248KB。
    # 阈值放宽到 1.5MB / 3MB：只要「每页嵌一份字体」这个病还在，随便就爆。
    for cnt, limit, label in ((20, 1_500_000, "20 题"), (40, 3_000_000, "40 题")):
        items = [q(i + 1, r"已知函数 $f(x)=x^2+%d x$，求 $f(%d)$ 的值。" % (i, i),
                   answer=r"$f(%d)=%d$" % (i, i * i),
                   analysis=r"代入 $x=%d$ 即可。" % i)
                 for i in range(cnt)]
        n = len(paper_export.build_pdf("体积测试", items, OPTS))
        truthy(f"{label}带公式的 PDF 体积合理（< {limit // 1024} KB）", n < limit,
               f"实际 {n / 1024:.0f} KB（{n} 字节）")


# ================================================================ D. 回归
def part_d():
    print("\n==== D 回归：HTML 那条路不许被顺手改坏 ====")
    items = [q(1, r"已知函数 $f(x)=x^2$，求导。", answer="$2x$")]
    html = paper_export.build_html("回归", items, OPTS)
    truthy("HTML 里仍保留 $…$（留给浏览器 KaTeX 渲染）", "$f(x)=x^2$" in html,
           "HTML 版靠 KaTeX，服务端不该提前把它换成图片")
    truthy("HTML 里有 KaTeX 的 auto-render", "renderMathInElement" in html)
    falsy("HTML 里没有 data:image 的公式图（那是 Word/PDF 的路子）",
          "data:image/png;base64" in html and "$f(x)" not in html)


def main():
    part_a()
    part_b()
    part_c()
    part_e()
    part_d()
    print("\n" + "=" * 60)
    if fails:
        print(f"FAIL {len(fails)} 项：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
