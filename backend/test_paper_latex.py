# -*- coding: utf-8 -*-
"""LaTeX 排版导出（paper_latex.py）的自包含测试。

只验「老师能看见的结果」：
  · 特殊字符（& % $ # _ { } ~ ^）不会把编译搞炸，也不会印出反斜杠；
  · 公式是真的在（文字层没有 `$`、正文块里有墨）；
  · 题干里的 `\\n` 真的换行；
  · 抬头区的副标题 / 须知 / 姓名栏都在；
  · 答案区、体积合理；
  · **引擎缺失或公式写坏时，导出不许崩** —— 要回退到旧路径并把原因写进日志。

跑法（backend 目录下）：
    .venv/Scripts/python.exe test_paper_latex.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf  # noqa: E402

from app.services import paper_export, paper_latex  # noqa: E402

fails: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(("  [OK ] " if ok else "  [FAIL] ") + label + ("" if ok else f"  ← 实际 {got!r}，期望 {want!r}"))
    if not ok:
        fails.append(label)


def truthy(label: str, got, detail: str = "") -> None:
    ok = bool(got)
    print(("  [OK ] " if ok else "  [FAIL] ") + label + ("" if ok else f"  ← {detail or got!r}"))
    if not ok:
        fails.append(label)


def falsy(label: str, got, detail: str = "") -> None:
    ok = not got
    print(("  [OK ] " if ok else "  [FAIL] ") + label + ("" if ok else f"  ← {detail or got!r}"))
    if not ok:
        fails.append(label)


def q(n: int, content: str, **kw) -> dict:
    d = {"n": n, "id": f"id{n:02d}", "content": content, "answer": "", "analysis": "",
         "qtype": "解答题", "difficulty": "中档", "knowledge_points": [], "tags": []}
    d.update(kw)
    return d


OPTS = {"show_answer": False, "show_tags": False, "show_meta": True}

TPL = {
    "paper": {
        "subtitle": "2026 年秋季学期期中考试",
        "exam_note": "满分 150 分，考试用时 120 分钟",
        "fill_fields": ["姓名", "班级"],
        "instructions": ["答题前请填写姓名班级。", "考试结束后交回试卷。"],
        "instructions_title": "注意事项",
    },
    "style": {"font_size": 11.5, "title_size": 15.0, "section_size": 13.0,
              "line_height": 1.8, "margin_mm": [16.0, 14.0], "question_gap": 14.0,
              "number_style": "1.", "show_score": True, "score_unit": "分",
              "answer_space": 2, "answer_space_types": ["解答题"]},
    "sections": [{"title": "一、解答题", "qtypes": ["解答题"], "note": "本题共 {count} 小题。"}],
}


def flat(s: str) -> str:
    """去掉所有空白再比较。

    ⚠️ 别拿带空格的字符串去断言 PDF 文字层：PyMuPDF 是从**几何距离**猜要不要插空格的，
       「满分 150 分」和「满分150 分」都可能提取出来 —— 排版一微调断言就假红。
       要验的是「这些字有没有」，不是「空格在不在」。
    """
    return "".join((s or "").split())


def text_of(pdf: bytes) -> str:
    d = pymupdf.open("pdf", pdf)
    t = "\n".join(p.get_text() for p in d)
    d.close()
    return t


def block_rect(page, prefix: str):
    for b in page.get_text("blocks"):
        if (b[4] or "").strip().startswith(prefix):
            return pymupdf.Rect(b[0] - 4, b[1] - 4, b[2] + 4, b[3] + 4)
    return None


def dark_in(page, rect, dpi: int = 200) -> int:
    if rect is None:
        return 0
    pix = page.get_pixmap(dpi=dpi, clip=rect)
    n = 0
    for y in range(0, pix.height, 2):
        for x in range(0, pix.width, 2):
            if pix.pixel(x, y)[0] < 120:
                n += 1
    return n


# ================================================================ A. 引擎
def part_a() -> bool:
    print("\n==== A 引擎与回退 ====")
    exe = paper_latex.tectonic_bin()
    print(f"  引擎路径：{exe}")
    truthy("找得到 tectonic（找不到也不算失败，下面的用例会自动跳过真排版）", True)
    return exe is not None


# ================================================================ B. 转义与结构
def part_b() -> None:
    print("\n==== B 特殊字符 / 抬头 / 公式 / 换行 ====")

    nasty = "打 5% 折后 $x_{1}$ 满足 a & b，价格 #3 元（含 $100），范围 [1, 2]，~ 表示等价，^ 表示幂。"
    pdf = paper_export.build_pdf("转义测试", [q(1, nasty, score=5)], OPTS, TPL)
    truthy("带特殊字符的卷子出得来（没被 TeX 转义搞炸）", pdf and len(pdf) > 1000,
           paper_latex.last_error())
    txt = text_of(pdf)
    for ch in ("%", "&", "#"):
        truthy(f"文字层能看到「{ch}」（没被吃掉/没印成命令）", ch in txt, f"文字层：{txt[:160]!r}")
    truthy("没有印出反斜杠转义原文", "\\$" not in txt and "\\&" not in txt, txt[:160])
    # 题库里 `$100` 这种「明文的美元符号」会被转义成可见字符，是对的；
    # 要防的是**公式源码漏出来**（`x_{1}` 原样印出去那种）。
    truthy("明文的美元符号当普通字符印出来了", "$100" in txt, txt[:200])
    falsy("公式源码没漏出来（不该看到 x_{1} 这种原文）", "x_{1}" in txt, txt[:200])

    # 抬头区
    truthy("副标题在", "2026 年秋季学期期中考试" in txt, txt[:200])
    truthy("考试说明在", "满分150分，考试用时120分钟" in flat(txt), txt[:200])
    truthy("注意事项标题在", "注意事项" in txt, txt[:200])
    truthy("注意事项条目在", "答题前请填写姓名班级" in txt, txt[:200])
    truthy("姓名栏在", "姓名" in txt, txt[:200])
    truthy("分区标题与说明在（{count} 已替换成真实题数）",
           "一、解答题" in txt and "本题共1小题" in flat(txt), txt[:240])
    truthy("分值标注在", "5分" in flat(txt), txt[:200])

    # 公式真的画出来了：单独一道只有公式的题，量它那个块的墨
    d2 = paper_export.build_pdf("只有公式", [q(1, "$x^{2}+y^{2}=1$", score=3)], OPTS, TPL)
    doc = pymupdf.open("pdf", d2)
    page = doc[0]
    rect = block_rect(page, "1.")
    truthy("能找到这道题的文字块", rect is not None)
    dk = dark_in(page, rect)
    truthy("公式真的有墨（= 引擎真画了，不是空白）", dk > 20, f"暗像素 {dk}")
    doc.close()

    # \n 换行
    one = paper_export.build_pdf("一段", [q(1, "甲" * 40)], OPTS, TPL)
    three = paper_export.build_pdf("三段", [q(1, "甲" * 10 + "\n" + "甲" * 10 + "\n" + "甲" * 10)], OPTS, TPL)

    def h(pdf_bytes: bytes) -> float:
        d = pymupdf.open("pdf", pdf_bytes)
        blocks = [b for b in d[0].get_text("blocks") if "甲" in (b[4] or "")]
        v = (max(b[3] for b in blocks) - min(b[1] for b in blocks)) if blocks else 0.0
        d.close()
        return v

    h1, h3 = h(one), h(three)
    truthy("一段 / 三段都量到了正文块", h1 > 0 and h3 > 0, f"{h1:.1f} / {h3:.1f}")
    truthy("题干里的 \\n 真的换行（三段明显更高）", h3 > h1 * 1.3, f"一段 {h1:.1f}pt / 三段 {h3:.1f}pt")


# ================================================================ C. 答案与体积
def part_c() -> None:
    print("\n==== C 答案区 / 体积 ====")
    items = [q(i + 1, f"第 {i + 1} 题：求 $f(x)=x^{{{i}}}$ 的导数。",
               answer=f"$f'(x)={i}x^{{{i - 1}}}$", analysis="用幂函数求导公式。", score=5)
             for i in range(8)]
    pdf = paper_export.build_pdf("答案测试", items, {"show_answer": True, "show_analysis": True}, TPL)
    txt = text_of(pdf)
    truthy("答案区标题在", "参考答案与解析" in txt, txt[-300:])
    truthy("答案里有公式（不是「未填」）", "未填" not in txt, txt[-300:])
    truthy("解析也印出来了", "幂函数求导" in txt, txt[-300:])
    truthy("8 题的卷子体积正常（< 1.5MB，不嵌整份字体）", len(pdf) < 1_500_000,
           f"{len(pdf) / 1024:.0f} KB")


# ================================================================ D. 兜底
def part_d() -> None:
    print("\n==== D 兜底：引擎缺失 / 公式写坏，导出都不许崩 ====")

    items = [q(1, "普通题 $x+1$", score=5)]

    # ① 引擎缺失：假装没装
    real_avail = paper_latex.available
    paper_latex.available = lambda: False          # type: ignore[assignment]
    try:
        pdf = paper_export.build_pdf("没引擎", items, OPTS, TPL)
    finally:
        paper_latex.available = real_avail         # type: ignore[assignment]
    truthy("没装引擎时照样出 PDF（回退旧路径）", pdf and pdf[:4] == b"%PDF", repr(pdf[:20]))
    truthy("回退后中文还在", "普通题" in text_of(pdf), text_of(pdf)[:120])

    # ② 引擎在但编译失败：用一条 TeX 不认识的命令
    if real_avail():
        bad = paper_latex.build("坏公式", [q(1, "$\\thisIsNotACommand{x}$", score=5)], OPTS, TPL)
        truthy("编译失败时 build 返回 None（不是抛异常）", bad is None, repr(bad)[:40])
        truthy("失败原因写下来了，不是静默", bool(paper_latex.last_error()), paper_latex.last_error())

        # ③ 走完整导出：坏公式整卷回退，卷子仍出得来
        pdf2 = paper_export.build_pdf("坏公式", [q(1, "$\\thisIsNotACommand{x}$", score=5)], OPTS, TPL)
        truthy("坏公式的卷子仍能导出（回退旧路径）", pdf2 and pdf2[:4] == b"%PDF", repr(pdf2[:20]))
    else:
        print("  [SKIP] 引擎没装，跳过「编译失败回退」两条")


# ================================================================ E. 表格
# 题库里「填表题」存的就是 Markdown 表格。不转的话 `| 棱柱 | 面的个数 |` 和
# `|---|---|` 会**原样印在卷子上** —— 老师是要拿去印给学生的，这等于卷子废了。
TABLE_Q = (
    "【思考 2】请同学们完成下列表格：\n\n"
    "| 棱柱 | 面的个数 | 顶点个数 |\n"
    "|---|:---|---:|\n"
    "| 三棱柱 |  |  |\n"
    "| 四棱柱 | $4$ | $8$ |\n"
    "\n然后回答：$n$ 棱柱有多少条棱？"
)


def part_e(has_engine: bool) -> None:
    print("\n==== E Markdown 表格 → 真表格 ====")

    src = paper_latex.tex_body(TABLE_Q)
    truthy("表格被转成 tabular", "\\begin{tabular}" in src, src[:200])
    falsy("没留下 Markdown 的分隔行", "|---" in src, src[:200])
    truthy("表头进了表格", "棱柱 & 面的个数 & 顶点个数" in src, src[:300])
    truthy("单元格里的公式还是数学（没被当文字转义）", "$4$" in src and "$8$" in src, src[:300])
    truthy("对齐按 Markdown 分隔行来（左/右）", "|c|l|r|" in src, src[:300])
    truthy("表格前后的文字都还在", "请同学们完成下列表格" in src and "然后回答" in src, src[:200])
    falsy("只生成一张表格（没按行重复生成）", src.count("\\end{tabular}") > 1, src[:200])

    # 不该误伤：普通文字里的竖线、以及没有分隔行的「假表格」
    plain = paper_latex.tex_body("条件 a | b 成立，求 $x_1$ 的值。")
    truthy("普通文字里的竖线原样保留", "a | b" in plain, plain)
    noisy = paper_latex.tex_body("| a | b |")
    falsy("没有分隔行就不当表格（原样输出）", "tabular" in noisy, noisy)

    if not has_engine:
        print("  [SKIP] 引擎没装，跳过下面的真排版断言")
        return

    pdf = paper_export.build_pdf("表格卷", [q(1, TABLE_Q, score=5)], OPTS, TPL)
    truthy("带表格的卷子出得来", pdf and pdf[:4] == b"%PDF", paper_latex.last_error())
    txt = flat(text_of(pdf))
    # ⚠️ 不能找 "|---"：TeX 会把 `---` 排成破折号「—」，改坏之后也照样「找不到」→ 假绿。
    #    真正的直接后果是**竖线本身印在卷面上**（表格的框线是矢量线，不进文字层）。
    falsy("卷面上没有印出 Markdown 竖线", "|" in txt, txt[:200])
    falsy("卷面上没有印出 LaTeX 源码", "tabular" in txt, txt[:200])
    truthy("表格里的字真的在卷面上", "三棱柱" in txt and "四棱柱" in txt, txt[:300])
    truthy("表格外的题干也在", "然后回答" in txt or "n棱柱" in txt, txt[:300])

    # 正面证据：表格是**真画出来的框线**（矢量线），不是凑巧没印出竖线。
    def rules(data: bytes) -> int:
        d = pymupdf.open("pdf", data)
        n = len(d[0].get_drawings())
        d.close()
        return n

    plain = paper_export.build_pdf(
        "无表格卷", [q(1, "请同学们完成下列任务，并回答：$n$ 棱柱有多少条棱？", score=5)], OPTS, TPL)
    n_tbl, n_plain = rules(pdf), rules(plain)
    truthy("表格框线真的画出来了（比同样排版的纯文字卷多出横竖线）",
           n_tbl >= n_plain + 3, f"表格卷 {n_tbl} 条 / 纯文字卷 {n_plain} 条")


def main() -> int:
    has_engine = part_a()
    if has_engine:
        part_b()
        part_c()
    else:
        print("\n==== B/C 跳过：没装 tectonic ====")
    part_e(has_engine)
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
