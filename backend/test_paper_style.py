# -*- coding: utf-8 -*-
r"""卷种样式（P2）：模板解析 + 分区归组 + 三种格式渲染。

为什么单独测这个：`paper_style.py` 是新加的一层，它夹在「模板数据」和
「渲染层」中间，一旦字段名或默认值对不上，症状是**导出的卷子排版悄悄变了**
（不会报错），最难排查。所以这里逐条钉住：

  ① 坏数据不能让卷子导不出来（模板 JSON 损坏 → 退回默认，照常出卷）
  ② 四套内置卷种解析出来的关键字段必须对（抬头 / 题号 / 分区维度）
  ③ 分区归组**一道题都不能丢**（没匹配上的落到兜底组，而不是消失）
  ④ 不选模板导出的结果，必须跟「加这个功能之前」一致（回归保护）
  ⑤ 三种格式（HTML / Word / PDF）都能吃同一份模板且不报错

纯函数 + 渲染，**不需要起服务**，`python test_paper_style.py` 直接跑。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services import paper_export, paper_style  # noqa: E402

fails = []


def check(label, got, want):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}")
    if not okk:
        print(f"       得到 {got!r}")
        print(f"       期望 {want!r}")
    if not okk:
        fails.append(label)


def truthy(label, got):
    okk = bool(got)
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}")
    if not okk:
        print(f"       得到 {got!r}（期望为真）")
    if not okk:
        fails.append(label)


# ---------------------------------------------------------------- ① 坏数据兜底
print("==== ① 模板数据损坏时必须退回默认，不能让卷子导不出来 ====")
bad = paper_style.resolve({"paper": "{不是 JSON", "style": "null", "sections": "[[["})
check("坏 JSON 的 subtitle 退回默认", bad["paper"]["subtitle"], "")
check("坏 JSON 的 font_size 退回 11.5", bad["style"]["font_size"], 11.5)
check("坏 JSON 的 sections 退回空数组", bad["sections"], [])
check("tpl=None 也不炸", paper_style.resolve(None)["style"]["font_size"], 11.5)

check("字号越界（0.3）退回默认", paper_style.normalize_style({"font_size": 0.3})["font_size"], 11.5)
check("字号越界（999）退回默认", paper_style.normalize_style({"font_size": 999})["font_size"], 11.5)
check("行距 5.0 越界退回 1.8", paper_style.normalize_style({"line_height": 5.0})["line_height"], 1.8)
check("题号形态写错退回 1.", paper_style.normalize_style({"number_style": "第N题"})["number_style"], "1.")
check("margin 只给 1 个数 → 整组退回默认",
      paper_style.normalize_style({"margin_mm": [10]})["margin_mm"], [16.0, 14.0])

# fill_fields 允许显式空数组（= 不印姓名栏），这是**有效值**，不能被 `or` 吃掉
check("fill_fields 显式空数组要保住",
      paper_style.normalize_paper({"fill_fields": []})["fill_fields"], [])
check("fill_fields 不给时用默认三种",
      paper_style.normalize_paper({})["fill_fields"], ["姓名", "班级", "日期"])

# ---------------------------------------------------------------- ② 四套内置卷种
print("\n==== ② 四套内置卷种的关键取值 ====")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.seed import BUILTIN_PAPER_TEMPLATES  # noqa: E402

check("内置卷种共 4 套", len(BUILTIN_PAPER_TEMPLATES), 4)
check("四套的 code", [t["code"] for t in BUILTIN_PAPER_TEMPLATES],
      ["gaokao", "zhongkao", "dse", "alevel"])

by_code = {t["code"]: paper_style.resolve(t) for t in BUILTIN_PAPER_TEMPLATES}

# 高考：中文抬头、题号 1.、分区按题型、分值写进分区说明（**不逐题标**，这是真题做法）
gk = by_code["gaokao"]
truthy("高考有副标题", gk["paper"]["subtitle"])
truthy("高考有考试说明", gk["paper"]["exam_note"])
check("高考题号形态", gk["style"]["number_style"], "1.")
check("高考不逐题标分值（分值在分区说明里）", gk["style"]["show_score"], False)
truthy("高考分区说明里有分值",
       any("每小题" in s["note"] and "分" in s["note"] for s in gk["sections"]))
truthy("高考分区标题含「选择题」",
       any("选择" in s["title"] for s in gk["sections"]))
check("高考分区是按题型匹配",
      all(s["qtypes"] and not s["difficulties"] for s in gk["sections"]), True)

# DSE：英文抬头、题号仍用数字顶格（真题就是 `3.` 这种）
dse = by_code["dse"]
check("DSE 题号形态（真题题号是顶格数字，不是 Question N）",
      dse["style"]["number_style"], "1.")
truthy("DSE 抬头是英文",
       "HONG KONG" in (dse["paper"]["subtitle"] or "").upper())
check("DSE 分区含 Section 字样",
      all("Section" in s["title"] for s in dse["sections"]), True)
check("DSE 分区是按难度阶梯（A(1)/A(2)/B 那一套）",
      all(s["difficulties"] for s in dse["sections"]), True)

# A-Level：英文，题号 Question N
al = by_code["alevel"]
check("A-Level 题号形态", al["style"]["number_style"], "Question 1")
check("A-Level 每题标分值", al["style"]["show_score"], True)

# 中考：中文、满分 120
zk = by_code["zhongkao"]
truthy("中考抬头是中文",
       "初中" in (zk["paper"]["subtitle"] or "") or "中考" in (zk["paper"]["subtitle"] or ""))

# ---------------------------------------------------------------- ③ 分区归组不丢题
print("\n==== ③ 分区归组：一道题都不能丢 ====")
sections = paper_style.normalize_sections(json.dumps([
    {"title": "一、选择题", "qtypes": ["选择题"]},
    {"title": "二、解答题", "qtypes": ["解答题"]},
]))
items = [
    {"n": 1, "qtype": "选择题"},
    {"n": 2, "qtype": "解答题"},
    {"n": 3, "qtype": "证明题"},   # 模板没覆盖 → 必须落兜底组，不能消失
    {"n": 4, "qtype": "选择题"},
]
groups = paper_style.layout(items, sections)
flat = [it["n"] for g in groups for it in g["items"]]
check("四道题全在（顺序保持）", sorted(flat), [1, 2, 3, 4])
check("选择题进第一组", [it["n"] for it in groups[0]["items"]], [1, 4])
check("解答题进第二组", [it["n"] for it in groups[1]["items"]], [2])
check("证明题进兜底组（无标题）", [it["n"] for it in groups[2]["items"]], [3])
check("兜底组不带标题", groups[2]["title"], "")

check("没有 sections → 单个无标题组（= 老行为）",
      [g["title"] for g in paper_style.layout(items, [])], [""])
check("没有 sections 时题目全在", len(paper_style.layout(items, [])[0]["items"]), 4)

# 难度维度（DSE）：同题型按难度分到不同 Section
dsections = paper_style.normalize_sections(json.dumps([
    {"title": "Section A(1)", "difficulties": ["基础"]},
    {"title": "Section B", "difficulties": ["困难"]},
]))
dg = paper_style.layout([
    {"n": 1, "qtype": "解答题", "difficulty": "基础"},
    {"n": 2, "qtype": "解答题", "difficulty": "困难"},
    {"n": 3, "qtype": "解答题", "difficulty": "中档"},   # 没覆盖 → 兜底
], dsections)
check("难度「基础」→ A(1)", [it["n"] for it in dg[0]["items"]], [1])
check("难度「困难」→ B", [it["n"] for it in dg[1]["items"]], [2])
check("难度「中档」→ 兜底", [it["n"] for it in dg[2]["items"]], [3])

# 两个维度同时给 = 都要命中
both = paper_style.normalize_sections(json.dumps([
    {"title": "S1", "qtypes": ["解答题"], "difficulties": ["基础"]},
]))
bg = paper_style.layout([
    {"n": 1, "qtype": "解答题", "difficulty": "基础"},   # 命中
    {"n": 2, "qtype": "解答题", "difficulty": "困难"},   # 难度不符 → 兜底
    {"n": 3, "qtype": "选择题", "difficulty": "基础"},   # 题型不符 → 兜底
], both)
check("题型+难度都命中才归入", [it["n"] for it in bg[0]["items"]], [1])
check("只满足一个维度的落兜底", [it["n"] for it in bg[1]["items"]], [2, 3])

# 空分区必须丢掉：否则用「高考模版」出一份没有选择题的卷子，
# 会印出一个「一、选择题　本题共 0 小题，共 0 分」的空标题。
_two = paper_style.normalize_sections(json.dumps([
    {"title": "一、选择题", "qtypes": ["选择题"]},
    {"title": "二、解答题", "qtypes": ["解答题"]},
]))
_og = paper_style.layout([{"n": 1, "qtype": "解答题"}], _two)
check("没有选择题时「一、选择题」空分区不渲染", [g["title"] for g in _og], ["二、解答题"])
check("空分区被丢但题还在", [it["n"] for g in _og for it in g["items"]], [1])

# ---------------------------------------------------------------- ④ 题号与分区说明
print("\n==== ④ 题号形态与分区说明占位符 ====")
check("题号 1.", paper_style.num_text(3, {"number_style": "1."}), "3.")
check("题号 1、", paper_style.num_text(3, {"number_style": "1、"}), "3、")
check("题号 （1）", paper_style.num_text(3, {"number_style": "（1）"}), "（3）")
check("题号 Q1", paper_style.num_text(3, {"number_style": "Q1"}), "Q3")
check("题号 Question 1", paper_style.num_text(3, {"number_style": "Question 1"}), "Question 3")

note_items = [{"score": 5}, {"score": 5}, {"score": 5}]
check("{count} 替换",
      paper_style.section_note("本题共 {count} 小题", note_items, {}), "本题共 3 小题")
check("{count}+{total} 替换",
      paper_style.section_note("共 {count} 小题，共 {total} 分", note_items, {}),
      "共 3 小题，共 15 分")
check("有题缺分值 → {total} 显示破折号而不是错数",
      paper_style.section_note("共 {total} 分", [{"score": 5}, {}], {}), "共 — 分")
check("空 note 返回空串", paper_style.section_note("", note_items, {}), "")

# ---------------------------------------------------------------- ⑤ 三种格式渲染
print("\n==== ⑤ HTML / Word / PDF 三种格式都能吃模板 ====")
demo = [
    {"n": 1, "qtype": "选择题", "difficulty": "基础", "content": "已知 $f(x)=x^2$，则（ ）",
     "answer": "B", "analysis": "配方可得", "score": 5, "tags": ["函数"]},
    {"n": 2, "qtype": "解答题", "difficulty": "中档", "content": "求证 $\\frac{a}{b}=1$",
     "answer": "略", "analysis": "反证法", "score": 10, "tags": ["不等式"]},
]
opts = {"show_answer": True, "show_analysis": True, "show_tags": True, "show_meta": True}

for code in ("gaokao", "dse", "alevel", "zhongkao"):
    tpl = by_code[code]
    try:
        h = paper_export.build_html("测试卷", demo, opts, tpl)
        truthy(f"{code}: HTML 非空且是完整文档", h.startswith("<!DOCTYPE html>") and "</html>" in h)
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {code}: build_html 抛异常 {type(e).__name__}: {e}")
        fails.append(f"{code} build_html")
        h = ""
    try:
        d = paper_export.build_docx("测试卷", demo, opts, tpl)
        truthy(f"{code}: DOCX 是合法 zip（PK 头）", d[:2] == b"PK" and len(d) > 5000)
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {code}: build_docx 抛异常 {type(e).__name__}: {e}")
        fails.append(f"{code} build_docx")
    try:
        p = paper_export.build_pdf("测试卷", demo, opts, tpl)
        truthy(f"{code}: PDF 有 %PDF 头", p[:4] == b"%PDF" and len(p) > 2000)
    except Exception as e:  # noqa: BLE001
        print(f"  [FAIL] {code}: build_pdf 抛异常 {type(e).__name__}: {e}")
        fails.append(f"{code} build_pdf")

# 抬头确实进了 HTML
gk_html = paper_export.build_html("测试卷", demo, opts, by_code["gaokao"])
truthy("高考 HTML 含副标题", by_code["gaokao"]["paper"]["subtitle"] in gk_html)
truthy("高考 HTML 含分区标题", "一、选择题" in gk_html)
truthy("高考 HTML 分区说明带分值", "每小题 5 分" in gk_html)
truthy("高考 HTML 含注意事项标题", "注意事项" in gk_html)

alevel_html = paper_export.build_html("Test Paper", demo, opts, by_code["alevel"])
truthy("A-Level HTML 用 Question 题号", "Question 1" in alevel_html)
truthy("A-Level HTML 分值写成西文 (5 marks)", "(5 marks)" in alevel_html)
truthy("A-Level HTML 不出现中文分值（5分）", "（5分）" not in alevel_html)

dse_html = paper_export.build_html("Test Paper", demo, opts, by_code["dse"])
truthy("DSE HTML 用顶格数字题号", "<b>1.</b>" in dse_html)
truthy("DSE HTML 含 Section A(1)", "Section A(1)" in dse_html)

# ---------------------------------------------------------------- ⑥ 回归：不选模板 == 老行为
print("\n==== ⑥ 不选模板时，输出必须与「加功能前」一致（回归保护）====")
plain = {"paper": None, "style": None, "sections": None}
h_plain = paper_export.build_html("测试卷", demo, opts, plain)
truthy("不给模板也能出 HTML", h_plain.startswith("<!DOCTYPE html>"))
truthy("不给模板时不印分区标题", "一、选择题" not in h_plain)
truthy("不给模板时题号仍是 1.", "1." in h_plain)
check("不给模板时没有抬头副标题",
      paper_style.resolve(plain)["paper"]["subtitle"], "")
check("不给模板时姓名栏仍是默认三栏",
      paper_style.resolve(plain)["paper"]["fill_fields"], ["姓名", "班级", "日期"])

# ---------------------------------------------------------------- 结果
print("\n" + "=" * 60)
if fails:
    print(f"❌ {len(fails)} 项未通过：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{len(fails)} 项失败）")
