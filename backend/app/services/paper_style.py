# -*- coding: utf-8 -*-
"""卷种样式：把模板数据解析成渲染层能直接用的参数。

为什么单独抽一个模块：同一份模板要经过三个地方 ——
  · `papers.py` 接口 —— 校验、归一、把「这套长什么样」发给前端
  · `paper_export.py` 渲染 —— HTML / Word / PDF 三个分支都要吃同样的字段
  · 种子数据 `seed.py` —— 四套内置卷种的取值
三处各写一份「字段叫什么、默认值是多少」迟早对不上，所以统一在这里定义。

**只做解析与分组，不碰数据库、不碰渲染** —— 这样它可以被单独调用验证
（`test_paper_style.py` 就是这么跑四个卷种的）。
"""
from __future__ import annotations

import json
from typing import Any

# ---------------------------------------------------------------- 默认值
# 默认取「改动前那组」值：不选模板导出，出来的卷子跟以前一样
#（字号 11.5pt、行距 1.8、题间距 14pt，都是原 paper_export 里的硬编码值）。
#
# 唯一的例外是页边距。原来三个格式各写各的 —— HTML 打印 16/14mm、
# DOCX 18/16mm、PDF 50pt（≈17.6mm）—— 同一份卷子「预览」和「下载」的留白本来就对不上。
# 这里统一由模板给，默认取 HTML（也就是日常用的「预览 / 打印」）那一组。
# 这点变化是有意的：不是回归，是修掉一个一直存在的不一致。
DEFAULT_PAPER: dict[str, Any] = {
    "subtitle": "",              # 副标题，如「2025 年普通高等学校招生全国统一考试」
    "exam_note": "",             # 考试说明，如「满分 150 分，考试用时 120 分钟」
    "instructions": [],          # 注意事项，逐条排在最前面
    "instructions_title": "注意事项",   # 上面那一块的小标题（英文卷写 INSTRUCTIONS）
    "fill_fields": ["姓名", "班级", "日期"],   # 姓名栏字段；空数组 = 不印姓名栏
    "score_table": False,        # 是否印「题号 / 得分」登分表（高考卷开头有）
}

DEFAULT_STYLE: dict[str, Any] = {
    "font_size": 11.5,           # 正文字号 pt
    "title_size": 16.0,          # 卷名字号
    "section_size": 13.0,        # 分区标题字号
    "line_height": 1.8,          # 行距（HTML / DOCX）
    "margin_mm": [16.0, 14.0],   # 页边距 [上下, 左右] mm
    "question_gap": 14.0,        # 题间距 pt
    "number_style": "1.",        # 题号形态：1. / 1、/ （1） / Q1 / Question 1
    "show_score": False,         # 每题后是否标「（5 分）」
    "score_unit": "分",
    "answer_space": 0,           # 解答题下方留白行数；0 = 不留
    "answer_space_types": ["解答题", "证明题", "应用题"],
}

# 支持的题号形态。写在 normalize_style 之前 —— 定义在被引用之前更不容易看漏。
NUMBER_STYLES = ("1.", "1、", "（1）", "Q1", "Question 1")


def _loads(raw: Any, fallback: Any) -> Any:
    """容错解析 JSON 字段。类型必须与 fallback 一致，否则也退回。"""
    if isinstance(raw, (dict, list)):
        got = raw
    else:
        try:
            got = json.loads(raw or "")
        except (json.JSONDecodeError, TypeError):
            return fallback
    if isinstance(fallback, list):
        return got if isinstance(got, list) else fallback
    return got if isinstance(got, dict) else fallback


def _num(v: Any, fallback: float, lo: float, hi: float) -> float:
    """数值字段的收敛。落在区间外就用默认值 —— 而不是夹到边界，
    因为「字号 0.3」几乎一定是填错了，按默认渲染比按 0.3 渲染更有用。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return fallback
    return f if lo <= f <= hi else fallback


def normalize_paper(raw: Any) -> dict:
    src = _loads(raw, {})
    out = dict(DEFAULT_PAPER)

    out["subtitle"] = str(src.get("subtitle") or "").strip()
    out["exam_note"] = str(src.get("exam_note") or "").strip()
    out["score_table"] = bool(src.get("score_table"))
    out["instructions_title"] = str(src.get("instructions_title") or "").strip() or "注意事项"

    ins = src.get("instructions")
    out["instructions"] = [str(x).strip() for x in ins if str(x).strip()] if isinstance(ins, list) else []

    # fill_fields 允许显式给空数组（= 不印姓名栏），所以不能用 `or` —— 空数组是**有效值**
    ff = src.get("fill_fields")
    if isinstance(ff, list):
        out["fill_fields"] = [str(x).strip() for x in ff if str(x).strip()]
    return out


def normalize_style(raw: Any) -> dict:
    src = _loads(raw, {})
    out = dict(DEFAULT_STYLE)

    out["font_size"] = _num(src.get("font_size"), 11.5, 6, 20)
    out["title_size"] = _num(src.get("title_size"), 16.0, 9, 30)
    out["section_size"] = _num(src.get("section_size"), 13.0, 8, 24)
    out["line_height"] = _num(src.get("line_height"), 1.8, 1.0, 3.0)
    out["question_gap"] = _num(src.get("question_gap"), 14.0, 0, 60)
    out["answer_space"] = int(_num(src.get("answer_space"), 0, 0, 30))
    out["score_unit"] = str(src.get("score_unit") or "分").strip() or "分"
    out["show_score"] = bool(src.get("show_score"))

    mm = src.get("margin_mm")
    if isinstance(mm, (list, tuple)) and len(mm) == 2:
        out["margin_mm"] = [_num(mm[0], 16.0, 5, 40), _num(mm[1], 14.0, 5, 40)]

    ns = str(src.get("number_style") or "1.").strip()
    out["number_style"] = ns if ns in NUMBER_STYLES else "1."

    ats = src.get("answer_space_types")
    if isinstance(ats, list):
        out["answer_space_types"] = [str(x).strip() for x in ats if str(x).strip()]
    return out


def num_text(n: int, style: dict) -> str:
    """按卷种把题号渲染成文字的形态。分区不改编号 —— 真卷（高考 / DSE）在
    分区之间都是连续编号的，各分区重新从 1 开始反而不像真卷。"""
    ns = (style or {}).get("number_style") or "1."
    if ns == "1、":
        return f"{n}、"
    if ns == "（1）":
        return f"（{n}）"
    if ns == "Q1":
        return f"Q{n}"
    if ns == "Question 1":
        return f"Question {n}"
    return f"{n}."


def normalize_sections(raw: Any) -> list[dict]:
    """分区定义。空数组 = 不分区（题目按用户排的顺序平铺）。

    一个分区有两个可选的匹配维度，**都必须同时命中**才算属于它：
      qtypes        —— 按题型分（高考 / 中考的「一、选择题」）
      difficulties  —— 按难度分（DSE Paper 1 的 Section A(1)/A(2)/B 是难度阶梯）

    两个都为空 = 兜底分区不再参与匹配（否则会把后面的题全吃掉）。
    之所以要难度这一维：DSE 和 A-Level 的 Section 是按难度阶梯切的，
    只按题型分的话，一套解答题全挤进同一个 Section，还原不了真卷结构。
    """
    src = _loads(raw, [])
    out: list[dict] = []
    for i, s in enumerate(src):
        if not isinstance(s, dict):
            continue

        def _list(key: str) -> list[str]:
            v = s.get(key)
            return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

        out.append({
            "title": str(s.get("title") or "").strip(),
            "note": str(s.get("note") or "").strip(),
            "qtypes": _list("qtypes"),
            "difficulties": _list("difficulties"),
            "page_break": bool(s.get("page_break")),
            "order": i,
        })
    return out


def resolve(tpl: Any) -> dict:
    """把一行 paper_templates（ORM 对象或 dict）解析成 {paper, style, sections}。

    两种入参都收：接口里拿到的是 ORM 行，而测试与渲染层给的是普通 dict。
    """
    def field(name: str) -> Any:
        if tpl is None:
            return None
        if isinstance(tpl, dict):
            return tpl.get(name)
        return getattr(tpl, name, None)

    return {
        "paper": normalize_paper(field("paper")),
        "style": normalize_style(field("style")),
        "sections": normalize_sections(field("sections")),
    }


# ---------------------------------------------------------------- 分区与编号
def layout(items: list[dict], sections: list[dict]) -> list[dict]:
    """把题目按分区归组，返回 [{"title","note","page_break","items"}]。

    两条硬约束：
      1. **一道题都不能丢。** 没被任何 qtypes 匹配走的题目落到末尾的兜底组里，
         而不是消失 —— 悄悄少一道题是最难被发现的事故。
      2. 没写 sections 的模板返回**单个无标题组**，渲染层当没有分区处理，
         于是输出跟改动前完全一致。
    """
    if not sections:
        return [{"title": "", "note": "", "page_break": False, "items": list(items)}]

    buckets: list[list[dict]] = [[] for _ in sections]
    fallback: list[dict] = []

    for it in items:
        qtype = (it.get("qtype") or "").strip()
        diff = (it.get("difficulty") or "").strip()
        hit = None
        for idx, sec in enumerate(sections):
            if not sec["qtypes"] and not sec["difficulties"]:
                continue                      # 两个维度都空 = 兜底分区，不参与匹配
            if sec["qtypes"] and qtype not in sec["qtypes"]:
                continue
            if sec["difficulties"] and diff not in sec["difficulties"]:
                continue
            hit = idx
            break
        if hit is None:
            fallback.append(it)
        else:
            buckets[hit].append(it)

    # 空分区直接丢掉。否则用「高考模版」出一份没有选择题的卷子时，会印出一个
    # 「一、选择题　本题共 0 小题，共 0 分」的空标题 —— 比不印更糟。
    # 注意兜底组不在此列：它只在有题时才存在，且无标题（见下）。
    groups = [
        {"title": sec["title"], "note": sec["note"], "page_break": sec["page_break"], "items": bucket}
        for sec, bucket in zip(sections, buckets)
        if bucket
    ]
    if fallback:
        # 兜底组不带标题：它接的是模板没覆盖到的题型，硬起个名字会误导
        groups.append({"title": "", "note": "", "page_break": False, "items": fallback})
    return groups


def section_note(note: str, items: list[dict], style: dict) -> str:
    """分区说明里支持 {count}（题数）与 {total}（总分，只有每题都有分值时才算得出）。

    做这点替换是因为真卷的分区说明几乎都是「本题共 N 小题，每小题 X 分，共 Y 分」——
    写死数字的话，老师增删一道题就得回去改模板，那就没人用这个功能了。
    """
    if not note:
        return ""
    text = note.replace("{count}", str(len(items)))
    if "{total}" in text:
        scores = [it.get("score") for it in items]
        if scores and all(isinstance(s, (int, float)) for s in scores):
            total = sum(scores)
            # 整数分值不显示 ".0"
            shown = int(total) if float(total).is_integer() else round(total, 2)
            text = text.replace("{total}", f"{shown}")
        else:
            # 有题没填分值 → 算不出总分。宁可把这句去掉，也不能印一个错的数字。
            text = text.replace("{total}", "—")
    return text
