# -*- coding: utf-8 -*-
"""生成样例数学试卷（docx + pdf），用于测试文档分割功能。"""
import os

from docx import Document
from docx.shared import Pt
import pymupdf

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "samples")
os.makedirs(SAMPLES, exist_ok=True)

TITLE = "高二数学专项测试卷（导数与数列）"

QUESTIONS = [
    ("一、选择题（每题 5 分，共 20 分）", None),
    ("1. 函数 $f(x)=x^3-3x$ 的单调递增区间是（　　）", "选择题"),
    ("A. $(-\\infty,-1)$ 和 $(1,+\\infty)$　　B. $(-1,1)$　　C. $(-\\infty,-1)$　　D. $(1,+\\infty)$", None),
    ("2. 等比数列 $\\{a_n\\}$ 中，$a_1=2$，公比 $q=3$，则 $a_4=$（　　）", "选择题"),
    ("A. 18　　B. 54　　C. 162　　D. 486", None),
    ("3. 曲线 $y=\\sin x$ 在点 $(0,0)$ 处的切线方程是（　　）", "选择题"),
    ("A. $y=x$　　B. $y=-x$　　C. $y=2x$　　D. $y=0$", None),
    ("4. 已知数列 $\\{a_n\\}$ 的前 $n$ 项和 $S_n=n^2$，则 $a_5=$（　　）", "选择题"),
    ("A. 9　　B. 16　　C. 25　　D. 5", None),
    ("二、填空题（每题 5 分，共 10 分）", None),
    ("5. 函数 $f(x)=x^2-2x$ 在 $[0,3]$ 上的最小值为 ______。", "填空题"),
    ("6. 数列 $1, 1, 2, 3, 5, 8, \\cdots$（斐波那契数列）的第 10 项是 ______。", "填空题"),
    ("三、解答题（共 70 分）", None),
    ("7.（12 分）已知函数 $f(x)=x^3-3x^2+2$，求：（1）$f(x)$ 的单调区间；（2）$f(x)$ 在 $[-1, 3]$ 上的最大值和最小值。", "解答题"),
    ("8.（12 分）已知等差数列 $\\{a_n\\}$ 中，$a_2=3$，$a_5=9$。（1）求 $\\{a_n\\}$ 的通项公式；（2）求前 $n$ 项和 $S_n$ 的最大值。", "解答题"),
    ("9.（14 分）设函数 $f(x)=e^x-ax-1$。（1）当 $a=1$ 时，求 $f(x)$ 的极值；（2）讨论 $f(x)$ 的单调性。", "解答题"),
    ("10.（16 分）已知数列 $\\{a_n\\}$ 满足 $a_1=1$，$a_{n+1}=2a_n+1$。（1）证明数列 $\\{a_n+1\\}$ 是等比数列；（2）求数列 $\\{a_n\\}$ 的前 $n$ 项和 $S_n$。", "证明题"),
]


def make_docx(path):
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(11)
    doc.add_heading(TITLE, level=1)
    for text, _ in QUESTIONS:
        doc.add_paragraph(text)
    doc.save(path)


def make_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()  # A4 595x842
    y = 72.0
    def draw(text, size=11, bold=False):
        nonlocal page, y, doc
        if y > 780:
            page = doc.new_page()
            y = 72.0
        page.insert_text((60, y), text, fontsize=size,
                         fontname="china-s" if any("\u4e00" <= c <= "\u9fff" for c in text) else "helv",
                         fontfile=None)
        y += size + 10
    draw(TITLE, 15, True)
    y += 6
    for text, _ in QUESTIONS:
        draw(text)
    doc.save(path)
    doc.close()


if __name__ == "__main__":
    dx = os.path.join(SAMPLES, "高二数学专项测试卷.docx")
    pf = os.path.join(SAMPLES, "高二数学专项测试卷.pdf")
    make_docx(dx)
    make_pdf(pf)
    print("生成完成:")
    print(" ", dx)
    print(" ", pf)
