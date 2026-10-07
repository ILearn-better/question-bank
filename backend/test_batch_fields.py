# -*- coding: utf-8 -*-
"""批量入库：提示词卫生 + 返回值解析与回落。**纯函数自检，不联网、不起服务。**

    cd backend && ./.venv/Scripts/python.exe test_batch_fields.py

为什么要单独有这个测试（而不是只在打接口的 test_batch_import.py 里验）：
批量这条链路的**绝大部分风险在解析层，而不在接口层**。模型返回的是一段自由文本，
它可能：
  · 先来一句「好的，这是识别结果：」再给 JSON
  · 套一层 ```json 围栏
  · 尾随逗号
  · 题型写「解答」而不是「解答题」
  · 一口气吐十个标签
  · 干脆吐一段完全不是 JSON 的东西
这些情况接口层全都返 200（识别本身成功了），脏数据是**静默**流进待审列表的。
所以要用真真的坏输入一条条压过去 —— 而这类测试不该依赖模型是否肯配合，
更不该每次都花一次 API 调用。纯函数最合适。

⚠️ 这里还盯着一个真的踩过的坑：**提示词里的 LaTeX 反斜杠必须写双份**。
   `vision.py` 头部记过：`\f` 会变成换页符（0x0c）、`\t` 变成制表符，
   提示词送到模型手里已经面目全非，而模型照样有输出，只是公式规范变差 ——
   从任何日志上都看不出来。这里断言整份提示词里没有控制字符。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import taxonomy                                    # noqa: E402
from app.services import batch_import as bi                 # noqa: E402
from app.services import knowledge                          # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- 取值出处
def test_taxonomy() -> None:
    print("\n==== ① 取值唯一出处 taxonomy ====")
    check("题型非空且无重复", len(taxonomy.QTYPES) == len(set(taxonomy.QTYPES)) and taxonomy.QTYPES)
    check("难度是基础/中档/拔高",
          taxonomy.DIFFICULTIES == ("基础", "中档", "拔高"))
    check("置信度是 high/medium/low",
          taxonomy.CONFIDENCES == ("high", "medium", "low"))
    # 回落值必须在合法集合里 —— 否则回落完还是非法值，会再挂一次
    check("DEFAULT_QTYPE 在 QTYPES 里", taxonomy.DEFAULT_QTYPE in taxonomy.QTYPES)
    check("DEFAULT_DIFFICULTY 在 DIFFICULTIES 里",
          taxonomy.DEFAULT_DIFFICULTY in taxonomy.DIFFICULTIES)
    check("DEFAULT_CONFIDENCE 在 CONFIDENCES 里",
          taxonomy.DEFAULT_CONFIDENCE in taxonomy.CONFIDENCES)
    # 一个中文卷的分值分区要能对上难度档（paper_style 的 Section 用这三个词）
    check("标签上限是正整数", taxonomy.MAX_TAGS >= 1 and taxonomy.MAX_TAG_CHARS >= 2)


# ---------------------------------------------------------------- 提示词
def test_prompt() -> None:
    print("\n==== ② 提示词卫生 ====")
    p = bi.FIELD_PROMPT

    # ★ 这条是这个测试存在的主要理由
    bad = [c for c in "\t\f\b\r\v\a\x00" if c in p]
    check("提示词里没有控制字符（反斜杠写双份了）", not bad,
          "" if not bad else "混进了 " + ", ".join(f"0x{ord(c):02x}" for c in bad))
    check("LaTeX 命令活着：\\frac{}{}", "\\frac{}{}" in p)
    check("LaTeX 命令活着：\\sqrt{}", "\\sqrt{}" in p)
    check("转义说明活着：\\n / \\\"", "\\n" in p and '\\"' in p)

    # 字段名一个都不能少 —— 少一个模型就不会给，「字段必须全部出现」就成了空话
    fields = ["content", "qtype", "difficulty", "knowledge_point",
              "tags", "confidence", "needs_figure", "figure_note", "note"]
    missing = [f for f in fields if f'"{f}"' not in p]
    check("九个字段名都在提示词里", not missing, "" if not missing else f"缺 {missing}")

    # 枚举要原样写进提示词，模型才可能原样照抄
    lack = [v for v in list(taxonomy.QTYPES) + list(taxonomy.DIFFICULTIES) if v not in p]
    check("题型与难度的全部取值都写进了提示词", not lack,
          "" if not lack else f"缺 {lack}")
    check("要求只输出 JSON 对象", "JSON" in p and "围栏" in p)

    # 「要不要配图」的判据措辞也要原样写进去 —— 只说「判断有没有图」它会给得很随意
    lack_fig = [w for w in taxonomy.FIGURE_KEYWORDS if w not in p]
    check("配图判据的措辞写进了提示词", not lack_fig,
          "" if not lack_fig else f"缺 {lack_fig}")
    check("说明了拿不准时偏 true（漏图比多标严重）", "拿不准" in p and "true" in p)

    # 角色刻意与 vision 的「转录」角色分开：共用会让模型在「抄成一段文本」与
    # 「填一张表单」之间摇摆（实测：说了只输出 JSON，它仍然给带小标题的转录稿）
    from app.services import vision                        # noqa: E402
    check("角色与 vision 的转录角色不是同一份",
          bi._ROLE.strip() != vision.PROMPTS["question"].strip())
    check("角色里明确说了「填成一条结构化记录」", "结构化" in bi._ROLE)


# ---------------------------------------------------------------- 抠 JSON
def test_extract() -> None:
    print("\n==== ③ 从自由文本里抠 JSON ====")
    check("去掉 ```json 围栏",
          bi._strip_fence('```json\n{"a":1}\n```') == '{"a":1}')
    check("去掉无语言标记的围栏",
          bi._strip_fence('```\n{"a":1}\n```') == '{"a":1}')
    check("没有围栏时原样返回（并 strip）",
          bi._strip_fence('  {"a":1}  ') == '{"a":1}')

    check("前面有寒暄也能抠出来",
          bi._extract_object('好的，这是识别结果：\n{"a":1}') == '{"a":1}')
    check("后面有解释也能抠出来",
          bi._extract_object('{"a":1}\n希望有帮助！') == '{"a":1}')
    check("围栏 + 寒暄 + 尾注一起也能抠出来",
          bi._extract_object('```json\n{"a":1}\n```\n以上。') == '{"a":1}')
    check("完全没有花括号 → None", bi._extract_object("我看不清这张图") is None)
    check("只有左花括号 → None", bi._extract_object("结果是 {") is None)

    check("正常 JSON 走快路（没动修复）", bi._loads('{"a":1}') == ({"a": 1}, False))
    obj, repaired = bi._loads('{"a":1,}')
    check("尾随逗号被修掉且标出来", obj == {"a": 1} and repaired is True)
    check("数组不算对象", bi._loads("[1,2]") == (None, False))


# ---------------------------------------------------------------- 正常返回
def test_good() -> None:
    print("\n==== ④ 正常返回：一个 flag 都不该有 ====")
    raw = json.dumps({
        "content": "已知函数 $f(x)=x^2+2x-3$，求 $f(x)$ 的零点。",
        "qtype": "解答题",
        "difficulty": "基础",
        "knowledge_point": "函数的零点",
        "tags": ["一元二次方程"],
        "confidence": "high",
        # 九字段一个不少 —— 少给 needs_figure 就该被打 figure_unknown
        "needs_figure": False,
        "figure_note": "",
        "note": "",
    }, ensure_ascii=False)
    r = bi.parse_fields(raw)
    check("无 flags", r["flags"] == [], f"实为 {r['flags']}")
    check("题干原样带出", r["content"].startswith("已知函数 $f(x)=x^2+2x-3$"))
    check("题型/难度原样带出", r["qtype"] == "解答题" and r["difficulty"] == "基础")
    check("知识点原样带出", r["knowledge_point"] == "函数的零点")
    check("标签是列表", r["tags"] == ["一元二次方程"])
    check("置信度高", r["confidence"] == "high")
    check("note 空串", r["note"] == "")
    check("字段齐全（十个键）", set(r) == {
        "content", "qtype", "difficulty", "knowledge_point",
        "tags", "confidence", "needs_figure", "figure_note", "note", "flags"})


# ---------------------------------------------------------------- 脏返回
def test_dirty() -> None:
    print("\n==== ⑤ 脏返回：修得动的修，修不动的标出来 ====")

    # 5.1 围栏 + 寒暄 + 尾随逗号
    messy = (
        "好的，这是识别结果：\n```json\n"
        '{"content":"求 $x^2=1$ 的解。","qtype":"解答",'
        '"difficulty":"中等","knowledge_point":"","tags":[],'
        '"confidence":"HIGH","note":"",}\n```\n希望对你有帮助。'
    )
    r = bi.parse_fields(messy)
    check("围栏+寒暄+尾随逗号：JSON 仍被修复", "json_repaired" in r["flags"])
    check("围栏+寒暄：没被判成 bad_json", "bad_json" not in r["flags"])
    check("题干取到了（说明抠出来了）", r["content"] == "求 $x^2=1$ 的解。")
    check("qtype「解答」非法 → 回落 + flag",
          r["qtype"] == taxonomy.DEFAULT_QTYPE and "qtype_fallback" in r["flags"])
    check("difficulty「中等」非法 → 回落 + flag",
          r["difficulty"] == taxonomy.DEFAULT_DIFFICULTY
          and "difficulty_fallback" in r["flags"])
    check("confidence 大写 HIGH → 归一成 high，不回落",
          r["confidence"] == "high" and "confidence_fallback" not in r["flags"])

    # 5.2 完全不是 JSON：不能抛异常，要给一份可入库的兜底
    r = bi.parse_fields("这张图我看不清，抱歉。")
    check("完全非 JSON：不抛异常", isinstance(r, dict))
    check("完全非 JSON：标 bad_json", "bad_json" in r["flags"])
    check("完全非 JSON：九个字段都还在（键齐全）",
          set(r) == {"content", "qtype", "difficulty", "knowledge_point",
                     "tags", "confidence", "needs_figure", "figure_note",
                     "note", "flags"})
    check("完全非 JSON：判不出有没有图 → 记 figure_unknown（不是静默 false）",
          r["needs_figure"] is False and "figure_unknown" in r["flags"])
    check("完全非 JSON：题型难度置信度全回落",
          r["qtype"] == taxonomy.DEFAULT_QTYPE
          and r["difficulty"] == taxonomy.DEFAULT_DIFFICULTY
          and r["confidence"] == taxonomy.DEFAULT_CONFIDENCE)
    check("完全非 JSON：题干空 + empty_stem",
          r["content"] == "" and "empty_stem" in r["flags"])
    check("完全非 JSON：没有半个字段是 None",
          all(v is not None for v in r.values()))

    # 5.3 空返回（就是 vision.py 那个 max_tokens 坑的表现）
    r = bi.parse_fields("")
    check("空字符串：bad_json + empty_stem 齐全",
          "bad_json" in r["flags"] and "empty_stem" in r["flags"])

    # 5.4 只有空题干的合法 JSON —— 能入库（有原貌图兜底），但要标出来
    r = bi.parse_fields(json.dumps({"content": "   ", "qtype": "选择题",
                                    "difficulty": "拔高",
                                    "knowledge_point": "导数",
                                    "tags": [], "confidence": "low",
                                    "note": "公式太密"},
                                   ensure_ascii=False))
    check("空题干被标 empty_stem", "empty_stem" in r["flags"])
    check("空题干不影响其它字段", r["qtype"] == "选择题" and r["difficulty"] == "拔高")
    check("空题干不触发枚举回落",
          not {"qtype_fallback", "difficulty_fallback"} & set(r["flags"]))

    # 5.5 完全缺字段的 JSON（模型只在给得出时才给）
    r = bi.parse_fields("{}")
    check("空对象：全回落且不抛", r["qtype"] == taxonomy.DEFAULT_QTYPE
          and r["difficulty"] == taxonomy.DEFAULT_DIFFICULTY
          and r["confidence"] == taxonomy.DEFAULT_CONFIDENCE)
    check("空对象：键仍齐全（九字段 + flags）", len(r) == 10)
    check("空对象：配图也判不出来 → figure_unknown",
          r["needs_figure"] is False and "figure_unknown" in r["flags"])


# ---------------------------------------------------------------- 标签清洗
def test_tags() -> None:
    print("\n==== ⑥ 标签清洗：模型最容易在这里放飞 ====")
    kp = "函数的零点"
    check("超量截到 MAX_TAGS",
          bi._clean_tags([f"标签{i}" for i in range(10)], kp) == ["标签0", "标签1", "标签2", "标签3"])
    check("超长丢掉",
          bi._clean_tags(["短", "这个标签实在太长了肯定超过十二个字"], kp) == ["短"])
    check("去重（保序）",
          bi._clean_tags(["甲", "乙", "甲"], kp) == ["甲", "乙"])
    check("与知识点重复的丢掉",
          bi._clean_tags([kp, "含参讨论"], kp) == ["含参讨论"])
    check("空串与空白丢掉",
          bi._clean_tags(["", "   ", "甲"], kp) == ["甲"])
    check("逗号分隔的字符串也能吃（模型常这样给）",
          bi._clean_tags("含参讨论,恒成立、分类讨论", kp)
          == ["含参讨论", "恒成立", "分类讨论"])
    check("不是列表也不是字符串 → 空",
          bi._clean_tags({"a": 1}, kp) == [])
    check("None → 空", bi._clean_tags(None, kp) == [])
    check("数字被转成字符串留下",
          bi._clean_tags([2025], kp) == ["2025"])

    # 与 knowledge.clean_tags 的分工：那个只做去空白去重，不做限量
    check("knowledge.clean_tags 不做限量",
          len(knowledge.clean_tags([f"t{i}" for i in range(10)])) == 10)


# ---------------------------------------------------------------- 知识点归一
def test_normalize() -> None:
    print("\n==== ⑦ 知识点名字的归一化 ====")
    n = knowledge.normalize
    check("抹掉「的/和/与/及」", n("三角函数的图像与性质") == "三角函数图像性质")
    check("抹掉标点与空白", n(" 函数、 零点 ") == "函数零点")
    check("抹掉括号与连字符", n("函数（零点）-1") == "函数零点1")
    check("只差连接词与标点的一对，归一后相等（模糊匹配的依据）",
          n("三角函数的图像与性质") == n("三角函数图像与性质")
          and n("函数、零点") == n("函数零点"))

    # ⚠️ 刻意不做同义词：归一化只管「加字/加标点」，不管「换字」。
    #    所以 图像/图象 这一对**归一后仍不相等** —— 这是有意的，不是漏了。
    #    靠的是 match_node 的「互相包含」那一档去捡，而不是靠猜。
    check("刻意不做同义词（图/象 归一后仍不同）", n("函数图像") != n("函数图象"))
    check("接上一条：包含关系才是那档的手段",
          n("三角函数") in n("三角函数的图像与性质"))
    check("空名字归一后是空串", n(None) == "" and n("   ") == "")
    check("不相关的名字不会被抹成同一个", n("数列求和") != n("立体几何"))


# ---------------------------------------------------------------- 配图判断
def test_figure() -> None:
    print("\n==== ⑧ 「这题要不要配图」的解析 ====")

    # 8.1 模型对布尔字段极不老实：要 JSON 布尔值，它给字符串、给中文、给 0/1
    true_cases = [True, "true", "TRUE", "是", "有", "需要", "1", 1]
    false_cases = [False, "false", "False", "否", "没有", "不需要", "0", 0]
    bad_true = [v for v in true_cases if bi._as_bool(v) is not True]
    bad_false = [v for v in false_cases if bi._as_bool(v) is not False]
    check("真值写法都认（true/是/有/需要/1）", not bad_true, f"没认出来 {bad_true}")
    check("假值写法都认（false/否/没有/0）", not bad_false, f"没认出来 {bad_false}")

    # 8.2 ★ 看不懂（含空串、null、缺字段）时必须是 None，不能是 False。
    #     当成 False 的后果：一条**本该被复核**的条目会看起来像「模型确认没图」，
    #     老师不会去看，图就永远丢了 —— 这正是用户担心的「ai 有可能判断出现问题」。
    unknown = [None, "", "   ", "大概有吧", "不确定", "看图", [], {}]
    leaked = [v for v in unknown if bi._as_bool(v) is not None]
    check("空串/null/看不懂 都返回 None（不是 False）", not leaked, f"误判成布尔的是 {leaked}")
    check("None 在真值判断里仍算 false（能直接进库）", not bi._as_bool(None))

    # 8.3 解析结果里的三个配图字段
    r = bi.parse_fields('{"content":"如图，在四棱锥 $P-ABCD$ 中……",'
                        '"needs_figure":true,"figure_note":"一个四棱锥的立体图"}')
    check("needs_figure=true 解析出来", r["needs_figure"] is True)
    check("figure_note 原样带出", r["figure_note"] == "一个四棱锥的立体图")
    check("有图时不打 figure_unknown", "figure_unknown" not in r["flags"])

    r = bi.parse_fields('{"content":"求 $x^2=1$ 的解。","needs_figure":false}')
    check("needs_figure=false 解析出来", r["needs_figure"] is False)
    check("模型主动给了 false → 不算 unknown", "figure_unknown" not in r["flags"])

    r = bi.parse_fields('{"content":"求 $x^2=1$ 的解。"}')
    check("字段缺失 → false + figure_unknown（等老师确认）",
          r["needs_figure"] is False and "figure_unknown" in r["flags"])

    r = bi.parse_fields('{"content":"…","needs_figure":"看图"}')
    check("值看不懂 → false + figure_unknown", "figure_unknown" in r["flags"])

    # 8.4 判据措辞表本身
    check("判据措辞不含空格或标点（前端做子串匹配，带标点会漏）",
          all(w.strip() == w and w for w in taxonomy.FIGURE_KEYWORDS))
    check("「如图」「图象」这类最常见的必须在内",
          {"如图", "图象", "图中"} <= set(taxonomy.FIGURE_KEYWORDS))


def main() -> int:
    test_taxonomy()
    test_prompt()
    test_extract()
    test_good()
    test_dirty()
    test_tags()
    test_normalize()
    test_figure()

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
