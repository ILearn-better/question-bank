# -*- coding: utf-8 -*-
r"""R5：分区重排之后，**题号必须从 1 起连续，且卷面和答案册对得上**。

为什么单独一片：`paper_style.layout()` 按模板把题**重新分组**（选择题聚到第一区、
填空题第二区……），分组后卷面顺序和用户选题的顺序天然不同。如果题号还是入参那个
`n`，老师手里就是 `2. 3. 1.` 这种乱号；而这不会报错、不会崩，只是**印出来难看**。

更坏的一种改法（fixer 在规格里发现的缝）：只把 `layout()` 的 n 重编，
而答案区仍按原顺序印原号 —— 于是卷面 `1.` 是选择题、答案册 `1.` 是解答题，
**老师拿着卷子对答案，题号在答案册里查不到**。这比乱号更糟：乱号只是难找，
错配是**找不到 / 找到错的**。所以本文件最重的一条是 E（卷面号 == 答案号）。

判据（A~D 是分组逻辑，E/F 是「老师真正看到的东西」）：
  A 乱序纠正：印出来的 n 从 1 起连续，且**按内容对得上人**
  B 兜底组接在最后（模板没覆盖的题型不能消失、也不能插在中间）
  C **不污染入参**：调完之后调用方的 items 里 n 一个字没变
  D 无 sections 时不动（echo 路径不许被顺手重编）
  E **卷面号 == 答案号**，且配对正确（`1.` 后面的题与 `1.` 后面的答案要同一个人）
  F 无 sections 时两边也一致，且都是入参原号

⚠️ 这里**故意只验 HTML 输出里看得见的东西**（老师看到的就是这个），
   不假设实现用 `flat()`、映射表还是别的办法 —— 那是实现自由。
"""
import json
import re
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
        fails.append(label)


def truthy(label, got):
    okk = bool(got)
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}")
    if not okk:
        print(f"       得到 {got!r}（期望为真）")
        fails.append(label)


# ---------------------------------------------------------------- 工具
SECTIONS = [
    {"title": "一、选择题", "qtypes": ["选择题"]},
    {"title": "二、填空题", "qtypes": ["填空题"]},
    {"title": "三、解答题", "qtypes": ["解答题"]},
]


def template(sections):
    """模板三件套用 JSON 字符串给（与库里存的一样；`resolve()` 就是吃这个形态）。"""
    return {"paper": json.dumps({"title": "布局测试卷"}),
            "style": json.dumps({"number_style": "1."}),
            "sections": json.dumps(sections)}


def q(n, qtype, tag):
    """tag 是身份标记：**分组之后 n 不再能当身份**，只能靠内容认人。"""
    return {"n": n, "qtype": qtype, "content": "题目" + tag, "answer": "答案" + tag, "score": 5}


OPTS = {"show_answer": True, "show_analysis": False, "show_tags": False, "show_meta": False}
ANS_SPLIT = '<div class="ans">'
BODY_RE = re.compile(r'class="stem"><b>(\d+)\.</b>\s*([^<]*)')
ANS_RE = re.compile(r'class="item"><b>(\d+)\.</b>\s*([^<]*)')


def split_body_answer(html):
    i = html.find(ANS_SPLIT)
    assert i > 0, "HTML 里找不到答案区（<div class=\"ans\">）——版面变了，先看输出再改测试"
    return html[:i], html[i:]


def nums(pairs):
    return [int(n) for n, _ in pairs]


# ---------------------------------------------------------------- E 卷面号 == 答案号
print("==== E 卷面号 == 答案号（本轮 R5 的核心验收）====")
items = [q(1, "解答题", "甲"), q(2, "选择题", "乙"), q(3, "填空题", "丙")]
html = paper_export.build_html("布局测试卷", items, OPTS, template(SECTIONS))
body, ans = split_body_answer(html)
bp, ap = BODY_RE.findall(body), ANS_RE.findall(ans)

check("正文编号从 1 起连续（跨分区、不重排成 2.3.1.）", nums(bp), [1, 2, 3])
check("答案区编号与正文**逐位相等**", nums(ap), nums(bp))
# 光「编号序列相同」不够：两边都错成同一个顺序也能混过去。真正要守的是**配对**。
check("正文按分区的顺序排（选择乙 → 填空丙 → 解答甲）",
      [t.strip() for _, t in bp], ["题目乙", "题目丙", "题目甲"])
check("答案区与正文同一个人（1. 后面是乙的题，就得是乙的答案）",
      [(t.strip().replace("答案", "题目")) for _, t in ap], [t.strip() for _, t in bp])

# E2：入参 n 不是 1 起（真实场景：老师挑的第 6、7、8 题）。这一组专抓
# 「卷面印新号 1,2,3 而答案册印旧号 6,7,8」—— fixer 在规格里发现的正是这个形态：
# 只改 layout() 而答案区还按原顺序走，两个可能都「没报错」，但老师查不到题号。
print("\n---- E2 入参 n=6,7,8：答案册不许印旧号 ----")
old = [q(6, "解答题", "甲"), q(7, "选择题", "乙"), q(8, "填空题", "丙")]
html2 = paper_export.build_html("布局测试卷", old, OPTS, template(SECTIONS))
body2, ans2 = split_body_answer(html2)
bp2, ap2 = BODY_RE.findall(body2), ANS_RE.findall(ans2)
check("E2 正文重编成 1,2,3", nums(bp2), [1, 2, 3])
check("E2 答案区也是 1,2,3（不是入参的 6,7,8）", nums(ap2), [1, 2, 3])
check("E2 配对也对（1 号题的答案配 1 号答案）",
      [(t.strip().replace("答案", "题目")) for _, t in ap2], [t.strip() for _, t in bp2])
check("E2 调完之后入参旧号还在（不污染）", [it["n"] for it in old], [6, 7, 8])

# ---------------------------------------------------------------- F 无分区时两边一致
print("\n==== F 无 sections 时两边也一致，且都是入参原号 ====")
odd = [q(5, "解答题", "甲"), q(6, "选择题", "乙"), q(7, "填空题", "丙")]
html_ns = paper_export.build_html("布局测试卷", odd, OPTS, template([]))
body_ns, ans_ns = split_body_answer(html_ns)
bp_ns, ap_ns = BODY_RE.findall(body_ns), ANS_RE.findall(ans_ns)
check("无分区：正文用入参原号（5,6,7 —— 不许被顺手重编成 1,2,3）", nums(bp_ns), [5, 6, 7])
check("无分区：答案区与正文逐位相等", nums(ap_ns), nums(bp_ns))
check("无分区：顺序就是入参顺序（解答甲 → 选择乙 → 填空丙）",
      [t.strip() for _, t in bp_ns], ["题目甲", "题目乙", "题目丙"])

# ---------------------------------------------------------------- A 乱序纠正
print("\n==== A 分组后题号必须从 1 起连续 ====")
groups = paper_style.layout([q(1, "解答题", "甲"), q(2, "选择题", "乙"), q(3, "填空题", "丙")],
                            paper_style.normalize_sections(json.dumps(SECTIONS)))
flat_n = [it["n"] for g in groups for it in g["items"]]
flat_c = [it["content"] for g in groups for it in g["items"]]
check("展开后 n == [1,2,3]", flat_n, [1, 2, 3])
check("顺序按分区（乙 → 丙 → 甲），不是入参顺序（甲 → 乙 → 丙）",
      flat_c, ["题目乙", "题目丙", "题目甲"])
check("各组的 n 也连续（第一组 1、第二组 2、第三组 3）",
      [[it["n"] for it in g["items"]] for g in groups], [[1], [2], [3]])

# ---------------------------------------------------------------- B 兜底组接在最后
print("\n==== B 模板没覆盖的题型 → 接在最后的无标题组 ====")
four = [q(1, "解答题", "甲"), q(2, "选择题", "乙"), q(3, "填空题", "丙"), q(4, "其他", "丁")]
g4 = paper_style.layout(four, paper_style.normalize_sections(json.dumps(SECTIONS)))
check("四道题印出来的 n == [1,2,3,4]", [it["n"] for g in g4 for it in g["items"]], [1, 2, 3, 4])
check("兜底组在最后一个", g4[-1]["title"], "")
check("兜底组里就是那道「其他」", [it["content"] for it in g4[-1]["items"]], ["题目丁"])
check("兜底题拿到的是 4 号（不是入参的 4 也没关系，但必须接在末尾）",
      [it["n"] for it in g4[-1]["items"]], [4])

# ---------------------------------------------------------------- C 不污染入参
print("\n==== C 调完之后调用方的 items 一个字没变 ====")
# 正面断言「还是原值」，不写 assertNotEqual —— 只验「坏事没发生」会放过「什么都没做」。
check("入参的 n 仍是 [1,2,3,4]", [it["n"] for it in four], [1, 2, 3, 4])
check("入参的 content 顺序没被重排", [it["content"] for it in four],
      ["题目甲", "题目乙", "题目丙", "题目丁"])
# 另起一组「n 明显不是 1..N」的，防止实现靠「入参本来就是 1..4」蒙混
dirty = [q(9, "解答题", "甲"), q(7, "选择题", "乙")]
paper_style.layout(dirty, paper_style.normalize_sections(json.dumps(SECTIONS)))
check("入参 n=[9,7] 重排后仍是 [9,7]", [it["n"] for it in dirty], [9, 7])

# ---------------------------------------------------------------- D 无 sections 不动
print("\n==== D 没有 sections 时不许重编 ====")
ns = paper_style.layout([q(5, "解答题", "甲"), q(6, "选择题", "乙")], [])
check("单组、无标题", [g["title"] for g in ns], [""])
check("n 就是入参原值 [5,6]", [it["n"] for it in ns[0]["items"]], [5, 6])
check("顺序也没动", [it["content"] for it in ns[0]["items"]], ["题目甲", "题目乙"])

# ---------------------------------------------------------------- 结果
print("\n" + "=" * 60)
if fails:
    print(f"❌ {len(fails)} 项未通过：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
sys.exit(0)
