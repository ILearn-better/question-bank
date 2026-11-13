# -*- coding: utf-8 -*-
"""DOCX 公式：LaTeX → 原生 Word 公式（OMML）的自包含测试。

验的是「老师打开 Word 看到的是不是真公式」：
  · 常用的中学写法都能转成 OMML，且 XML 合法（不合法 Word 会报文件损坏）；
  · 结构对得上（分数/根号/积分/分段/矩阵/向量箭头）；
  · 端到端导出的 docx 里没有公式图片（w:drawing = 0）；
  · 转不了的公式退回图片而不是印出反斜杠；
  · `SHIKE_DOCX_MATH=image` 能把整卷退回图片（给老版 WPS 的开关）。

跑法（backend 目录下）：.venv/Scripts/python.exe test_paper_omml.py
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lxml import etree  # noqa: E402

from app.services import paper_export, paper_math  # noqa: E402

fails: list[str] = []


def check(label, got, want):
    ok = got == want
    print(("  [OK ] " if ok else "  [FAIL] ") + label + ("" if ok else f"  ← 实际 {got!r}，期望 {want!r}"))
    if not ok:
        fails.append(label)


def truthy(label, got, detail=""):
    ok = bool(got)
    print(("  [OK ] " if ok else "  [FAIL] ") + label + ("" if ok else f"  ← {detail or got!r}"))
    if not ok:
        fails.append(label)


def falsy(label, got, detail=""):
    ok = not got
    print(("  [OK ] " if ok else "  [FAIL] ") + label + ("" if ok else f"  ← {detail or got!r}"))
    if not ok:
        fails.append(label)


def q(n, content, **kw):
    d = {"n": n, "id": "id%02d" % n, "content": content, "answer": "", "analysis": "",
         "qtype": "解答题", "difficulty": "中档", "knowledge_points": [], "tags": []}
    d.update(kw)
    return d


# (名字, LaTeX, 该出现的 OMML 标签)
CASES = [
    ("分数", r"\frac{x^{2}+1}{2}", "m:f"),
    ("根号", r"\sqrt{x^{2}+y^{2}}", "m:rad"),
    ("积分", r"\int_{0}^{1}x\,dx", "m:nary"),
    ("求和", r"\sum_{i=1}^{n} i", "m:nary"),
    ("分段", r"\begin{cases}1 & x>0\\ 0 & x\leq 0\end{cases}", "m:m"),
    ("矩阵", r"\begin{pmatrix}1 & 2\\ 3 & 4\end{pmatrix}", "m:m"),
    ("向量箭头", r"\overrightarrow{AB}", "m:groupChr"),
    ("绝对值", r"\left|x-1\right|", "m:d"),
    ("集合", r"A \subseteq B,\ x \in \mathbb{R}", "m:r"),
    ("角度", r"\angle A=60^{\circ}", "m:sSup"),
    ("因为所以", r"\because a>0 \therefore a^{2}>0", "m:r"),
    ("三角", r"\sin^{2}\theta+\cos^{2}\theta=1", "m:sSup"),
    ("对数", r"\log_{2}8=3", "m:sSub"),
    ("组合数", r"C_{n}^{m}=\frac{n!}{m!(n-m)!}", "m:f"),
    ("带中文", r"S=\pi r^{2}\text{（面积）}", "m:r"),
]


def part_a() -> None:
    print("\n==== A 逐个写法：转得出、XML 合法、结构对得上 ====")
    for name, tex, expect_tag in CASES:
        xml = paper_math.omml(tex)
        if not xml:
            truthy(f"{name}：能转成 OMML", False, "返回了 None")
            continue
        ok_parse = True
        try:
            etree.fromstring(xml.encode("utf-8"))
        except Exception as exc:                      # noqa: BLE001
            ok_parse = False
            detail = f"{type(exc).__name__}: {exc}"
        truthy(f"{name}：OMML 是合法 XML（不合法 Word 会说文件损坏）", ok_parse,
               detail if not ok_parse else "")
        truthy(f"{name}：有 {expect_tag} 结构", ("<" + expect_tag) in xml,
               xml[:80])

    # 已知不支持：aligned。必须明确返回 None（走图片退路），而不是塞一段坏 XML
    falsy("aligned 明确不支持（返回 None，走图片退路）",
          paper_math.omml(r"\begin{aligned}a&=b\\ c&=d\end{aligned}"))


def part_b() -> None:
    print("\n==== B 端到端：导出的 docx 里公式是原生对象，不是图片 ====")
    items = [q(1, r"已知 $f(x)=\frac{x^{2}+1}{2}$，求 $\int_{0}^{1}x\,dx$。", score=5, answer=r"$\frac{1}{2}$"),
             q(2, r"设 $A=\begin{pmatrix}1 & 2\\ 3 & 4\end{pmatrix}$，$\overrightarrow{AB}\cdot\overrightarrow{AC}=0$。", score=5)]
    data = paper_export.build_docx("OMML 导出", items, {"show_answer": True})
    xml = zipfile.ZipFile(__import__("io").BytesIO(data)).read("word/document.xml").decode("utf-8")

    truthy("docx 里出现了原生公式（m:oMath）", xml.count("<m:oMath") >= 4, f"实测 {xml.count('<m:oMath')} 个")
    check("没有一张公式图片（w:drawing）", xml.count("<w:drawing>"), 0)
    for tag, lbl in (("m:f", "分数"), ("m:nary", "积分"), ("m:m", "矩阵"), ("m:groupChr", "向量箭头")):
        truthy(f"结构里能找到{lbl}", ("<" + tag) in xml)
    falsy("没有把反斜杠原文印进 Word", "\\frac" in xml, xml[:120])

    # 老版 WPS 的退路开关
    os.environ["SHIKE_DOCX_MATH"] = "image"
    try:
        data2 = paper_export.build_docx("图片模式", items, {"show_answer": True})
    finally:
        os.environ.pop("SHIKE_DOCX_MATH", None)
    xml2 = zipfile.ZipFile(__import__("io").BytesIO(data2)).read("word/document.xml").decode("utf-8")
    check("开关一拨：公式退回图片（w:drawing > 0）", xml2.count("<w:drawing>") > 0, True)
    check("开关一拨：没有 OMML 了", xml2.count("<m:oMath"), 0)


def part_c() -> None:
    print("\n==== C 兜底：转不了的公式不许印反斜杠、不许崩 ====")
    items = [q(1, r"多行对齐 $\begin{aligned}a&=b\\ c&=d\end{aligned}$ 收尾。", score=5)]
    data = paper_export.build_docx("退化", items, {"show_answer": False})
    xml = zipfile.ZipFile(__import__("io").BytesIO(data)).read("word/document.xml").decode("utf-8")
    truthy("docx 出得来", len(data) > 2000, f"{len(data)} 字节")
    falsy("没有印出反斜杠命令", "\\begin{aligned}" in xml, "整段 LaTeX 原文被印出去了")
    truthy("退路内容还在（没变成空白题）", "多行对齐" in xml and "收尾" in xml, xml[:120])
    # 这条原来写的是 truthy(..., True) —— 恒真，等于没验。改成数实际产物：
    # OMML 转不了之后应当落到图片上（latex_img 用 mathtext 还能渲个近似），
    # 绝不能是「这块什么都没印」。
    check("退路落到图片上（不留空白）", xml.count("<w:drawing>") >= 1, True)


def main() -> int:
    part_a()
    part_b()
    part_c()
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
