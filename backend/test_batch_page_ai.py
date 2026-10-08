# -*- coding: utf-8 -*-
"""AI 整页识别（实验流程）解析层测试 —— 纯函数：不起服务、不联网、不烧 API。

为什么单独一个文件而不是塞进 test_batch_page.py：
    那个文件是**前端静态自检**（抠 batch.html 的源码做断言），
    这个是**后端解析逻辑**（真实调用 parse_page_items）。两回事，
    混在一起之后「到底跑了哪个」就得靠猜。

覆盖的是整页模式最容易坏、又最不容易被发现的地方：
  ① 正常形状 {"questions":[…]}   ② 顶层直接是数组
  ③ **被 max_tokens 截断**（抢救 —— 这是整页模式独有的新风险）
  ④ 模型明确说「这页没题」        ⑤ 完全垃圾输入
  ⑥ JSON 前后夹废话              ⑦ LaTeX 花括号 / 字符串里的 } 不干扰切分
  ⑧ needs_figure 三态            ⑨ 枚举回落与 flags

判据：退出码即结果（照项目里其它自包含测试脚本的约定）。
"""
from __future__ import annotations

import sys

from app import taxonomy
from app.services import batch_page

fails: list[str] = []
total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global total
    total += 1
    mark = "OK " if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail and not ok:
        line += f"   <- {detail}"
    print(line)
    if not ok:
        fails.append(name)


def main() -> int:
    print("=" * 60)
    print("AI 整页识别 · 解析层测试（纯函数）")
    print("=" * 60)

    # ---------------------------------------------------------- ① 正常形状
    print("\n==== ① 正常形状（我们要求的 {'questions':[…]}） ====")
    t = ('{"questions":[{"content":"题目甲","qtype":"选择题","difficulty":"基础",'
         '"needs_figure":true},{"content":"题目乙"}]}')
    r = batch_page.parse_page_items(t)
    check("切出 2 道题", len(r) == 2, f"got {len(r)}")
    check("题干按顺序对应", r[0]["content"] == "题目甲" and r[1]["content"] == "题目乙")
    check("合法枚举原样保留", r[0]["qtype"] == "选择题" and r[0]["difficulty"] == "基础")
    check("缺字段的题回落成默认值 + flag",
          r[1]["qtype"] == taxonomy.DEFAULT_QTYPE and "qtype_fallback" in r[1]["flags"])
    check("needs_figure=true 保留", r[0]["needs_figure"] is True)

    # ---------------------------------------------------------- ② 顶层数组
    print("\n==== ② 顶层直接是数组（模型没套那层壳） ====")
    t = '[{"content":"一"},{"content":"二"},{"content":"三"}]'
    r = batch_page.parse_page_items(t)
    check("切出 3 道题", len(r) == 3, f"got {len(r)}")

    # ---------------------------------------------------------- ③ 截断抢救
    print("\n==== ③ 输出被 max_tokens 截断（抢救） ====")
    # 第三道题断在半截 —— 这是整页模式最常见的坏法
    t = '{"questions":[{"content":"第一题完整"},{"content":"第二题完整"},{"content":"第三题还'
    r = batch_page.parse_page_items(t)
    check("救出前两道完整的题", len(r) == 2, f"got {len(r)}")
    check("救出来的标了 json_salvaged（老师能看到这条可能不完整）",
          all("json_salvaged" in x["flags"] for x in r))
    check("题干内容正确", [x["content"] for x in r] == ["第一题完整", "第二题完整"])

    # 连外层壳都断了（模型刚写完 [ 就没了）
    r = batch_page.parse_page_items('{"questions":[')
    check("只断了外壳：给出 1 条占位而不是空列表（这页必须被看见）",
          len(r) == 1 and "bad_json" in r[0]["flags"])

    # ---------------------------------------------------------- ④ 明确"没题"
    print("\n==== ④ 模型明确说「这页没有题」 ====")
    check("空数组 → 空列表（不是占位）", batch_page.parse_page_items('{"questions":[]}') == [])
    check("空数组 + 额外说明 → 仍是空列表",
          batch_page.parse_page_items('{"questions":[],"note":"这页是封面"}') == [])
    check("带空格的空数组也算数", batch_page.parse_page_items('{"questions": [ ]}') == [])

    # ---------------------------------------------------------- ⑤ 垃圾输入
    print("\n==== ⑤ 完全解析不出来 ====")
    r = batch_page.parse_page_items("抱歉，这张图我看不清。")
    check("返回 1 条占位（宁可让老师驳回，也不让整页静默消失）", len(r) == 1)
    check("标了 bad_json", "bad_json" in r[0]["flags"])
    check("占位题带一句人话解释", "解析" in (r[0]["note"] or ""))

    # ---------------------------------------------------------- ⑥ 夹废话
    print("\n==== ⑥ JSON 前后夹了话 ====")
    t = '好的，以下是识别结果：\n{"questions":[{"content":"夹缝里的题"}]}\n希望有帮助。'
    r = batch_page.parse_page_items(t)
    check("仍能正确解析出 1 道题", len(r) == 1 and r[0]["content"] == "夹缝里的题", f"got {r}")

    # ---------------------------------------------------------- ⑦ 花括号
    print("\n==== ⑦ LaTeX 花括号 / 字符串里的 } / 转义引号 ====")
    # content 里全是花括号（\frac{}{}），切分器必须只在字符串外数括号
    t = r'{"questions":[{"content":"$\\frac{1}{2}+\\sqrt{x}$ 求值"},{"content":"第二题"}]}'
    r = batch_page.parse_page_items(t)
    check("LaTeX 花括号不干扰切分", len(r) == 2, f"got {len(r)}")
    check("公式原样保留", r[0]["content"] == r"$\frac{1}{2}+\sqrt{x}$ 求值", repr(r[0]["content"]))

    # 字符串里出现 } —— 若按字符计数会把对象提前截断
    t = r'{"questions":[{"content":"集合 {a,b} 的补集"},{"content":"第二题"}]}'
    r = batch_page.parse_page_items(t)
    check("字符串里的 } 不干扰切分", len(r) == 2, f"got {len(r)}")
    check("内容完整", r[0]["content"] == "集合 {a,b} 的补集", repr(r[0]["content"]))

    # 转义引号
    t = r'{"questions":[{"content":"他说 \"好\" 的"}]}'
    r = batch_page.parse_page_items(t)
    check("转义引号不打断字符串", len(r) == 1 and r[0]["content"] == '他说 "好" 的', repr(r))

    # 截断 + 花括号同时出现（两种情况叠在一起）
    t = r'{"questions":[{"content":"$\\frac{a}{b}$ 第一题"},{"content":"$\\sqrt{x}$ 第二'
    r = batch_page.parse_page_items(t)
    check("花括号 + 截断叠加：仍救出第一道", len(r) == 1 and r[0]["content"] == r"$\frac{a}{b}$ 第一题",
          repr(r))

    # ---------------------------------------------------------- ⑧ 配图三态
    print("\n==== ⑧ needs_figure 三态（本功能的地基） ====")
    t = ('{"questions":[{"content":"有图题","needs_figure":true},'
         '{"content":"空串题","needs_figure":""},'
         '{"content":"缺字段题"},'
         '{"content":"显式否","needs_figure":false},'
         '{"content":"字符串真","needs_figure":"true"}]}')
    r = batch_page.parse_page_items(t)
    check("五道题都切出来", len(r) == 5, f"got {len(r)}")
    check("true → True 且不打 unknown", r[0]["needs_figure"] is True and "figure_unknown" not in r[0]["flags"])
    check("空串 → False **但打 figure_unknown**（空串不是「否」）",
          r[1]["needs_figure"] is False and "figure_unknown" in r[1]["flags"])
    check("缺字段 → False **但打 figure_unknown**",
          r[2]["needs_figure"] is False and "figure_unknown" in r[2]["flags"])
    check("显式 false → False 且不打 unknown",
          r[3]["needs_figure"] is False and "figure_unknown" not in r[3]["flags"])
    check("字符串 \"true\" 也认（模型常给字符串）",
          r[4]["needs_figure"] is True, repr(r[4]["needs_figure"]))

    # ---------------------------------------------------------- ⑨ 枚举回落
    print("\n==== ⑨ 枚举与其它字段的回落 ====")
    t = ('{"questions":[{"content":"题","qtype":"解答","difficulty":"中等",'
         '"confidence":"很高","knowledge_point":"函数的零点",'
         '"tags":["含参讨论","含参讨论","函数的零点","这是一个特别特别长的标签啊"]}]}')
    r = batch_page.parse_page_items(t)
    q = r[0]
    check("自造题型回落 + qtype_fallback",
          q["qtype"] == taxonomy.DEFAULT_QTYPE and "qtype_fallback" in q["flags"])
    check("自造难度回落 + difficulty_fallback",
          q["difficulty"] == taxonomy.DEFAULT_DIFFICULTY and "difficulty_fallback" in q["flags"])
    check("自造置信度回落 + confidence_fallback",
          q["confidence"] == taxonomy.DEFAULT_CONFIDENCE and "confidence_fallback" in q["flags"])
    # ⚠️ 标签清洗在**代码里只做三件事**：去重、剔除与知识点重复的、剔除超长（>12 字）。
    #    「不要放『数学』这类没有区分度的词」是**提示词层面**的要求，代码不强制 ——
    #    断言按代码的真实行为写，别按期望写，否则测的是自己的想象。
    check("标签：去重 + 剔除与知识点重复的 + 剔除超长（>12 字）",
          q["tags"] == ["含参讨论"], repr(q["tags"]))
    # 上限 4 个：模型很爱一次吐十个标签，那样每个标签都只挂一两道题，筛不出东西
    t = '{"questions":[{"content":"题","tags":["甲一","乙二","丙三","丁四","戊五","己六"]}]}'
    q2 = batch_page.parse_page_items(t)[0]
    check("标签：最多留 4 个", q2["tags"] == ["甲一", "乙二", "丙三", "丁四"], repr(q2["tags"]))

    # ---------------------------------------------------------- ⑩ 单题直出
    print("\n==== ⑩ 模型没套 questions 壳、直接给一道题 ====")
    r = batch_page.parse_page_items('{"content":"裸题","qtype":"填空题"}')
    check("也接受（这页确实只有一题）", len(r) == 1 and r[0]["content"] == "裸题")

    # ---------------------------------------------------------- ⑪ 配图框（figure_box）
    print("\n==== ⑪ 配图框：figure_box 解析（自动裁图的入口） ====")

    def fb(raw):
        """跑一遍解析并取第 1 题的 figure_box + flags。"""
        import json as _json
        t = _json.dumps({"questions": [{"content": "题", "figure_box": raw}]},
                        ensure_ascii=False)
        q = batch_page.parse_page_items(t)[0]
        return q["figure_box"], q["flags"]

    box, fl = fb([50, 220, 400, 380])
    check("正常数组原样保留", box == [50, 220, 400, 380], repr(box))
    # 只断言「没多出跟配图框有关的 flag」—— 这个精简对象本来缺 qtype/difficulty，
    # 那三个回落 flag 是**应该**出现的（断言成全空会把无关行为也锁进来）
    check("正常值不产生任何 figure_box_* flag",
          not [x for x in fl if x.startswith("figure_box")], repr(fl))

    box, fl = fb([0.05, 0.22, 0.40, 0.38])
    check("0~1 比例自动放大成 0~1000", box == [50, 220, 400, 380], repr(box))
    check("放大时标 figure_box_fraction", "figure_box_fraction" in fl, repr(fl))

    box, _ = fb({"x0": 50, "y0": 220, "x1": 400, "y1": 380})
    check("对象形态 {x0,y0,x1,y1} 也认", box == [50, 220, 400, 380], repr(box))

    box, _ = fb({"xmin": 50, "ymin": 220, "xmax": 400, "ymax": 380})
    check("对象形态 {xmin,ymin,xmax,ymax} 也认", box == [50, 220, 400, 380], repr(box))

    box, _ = fb("50, 220, 400, 380")
    check("字符串形态也认（模型常把数组写成字符串）", box == [50, 220, 400, 380], repr(box))

    box, _ = fb([400, 380, 50, 220])
    check("坐标反序自动纠正（x1<x0 也裁得出来）", box == [50, 220, 400, 380], repr(box))

    box, fl = fb([-10, 100, 1200, 900])
    check("越界夹回 0~1000", box == [0, 100, 1000, 900], repr(box))
    check("夹取时标 figure_box_clamped（老师才知道框可能不对）",
          "figure_box_clamped" in fl, repr(fl))

    box, fl = fb([100, 100, 105, 108])
    check("过小的框丢弃（不是图）", box is None, repr(box))
    check("丢弃时标 figure_box_tiny", "figure_box_tiny" in fl, repr(fl))

    box, _ = fb([])
    check("空数组 = 没有图", box is None, repr(box))
    box, _ = fb("图在左上角")
    check("纯文字描述 → 丢弃，不猜", box is None, repr(box))
    box, _ = fb([None, 1, 2, 3])
    check("含 None → 丢弃", box is None, repr(box))
    box, _ = fb([float("nan"), 0, 100, 100])
    check("NaN → 丢弃（不能让 NaN 流到裁剪层）", box is None, repr(box))

    # 缺字段 / 解析失败时 figure_box 必须**存在且为 None** ——
    # 绝不能没有这个键：调用方按 `r.get("figure_box")` 取值，
    # 键名打错一次就会静默变成「永远没有配图」，而没有任何报错。
    r = batch_page.parse_page_items('{"questions":[{"content":"无框"}]}')
    check("缺 figure_box 字段 → None（键必须存在）",
          "figure_box" in r[0] and r[0]["figure_box"] is None, repr(r[0].get("figure_box")))
    r = batch_page.parse_page_items("这根本不是 JSON")
    check("解析失败占位题也带 figure_box=None",
          "figure_box" in r[0] and r[0]["figure_box"] is None)

    # ---------------------------------------------------------- ⑫ 几何吸附
    print("\n==== ⑫ 几何吸附：模型指路 + 页面几何定框 ====")
    W, H = 595.3, 841.9          # A4，与真实那一页一致
    cands = [{"kind": "img", "rect": (54.0, 277.0, 176.0, 397.0)}]

    rect, mode = batch_page.resolve_figure_box([50, 220, 400, 380], cands, W, H)
    check("模型框与候选区不重叠时靠中心距离吸附（实测就是这种）",
          mode == "snapped", mode)
    check("吸附后边界＝候选区边界（±留白 3pt）",
          [round(v, 1) for v in rect] == [51.0, 274.0, 179.0, 400.0], repr(rect))

    rect2, mode2 = batch_page.resolve_figure_box([91, 329, 295, 471], cands, W, H)
    check("重叠度高时直接吸附", mode2 == "snapped")

    _, mode3 = batch_page.resolve_figure_box(None, cands, W, H, allow_only=True)
    check("模型没给框 + 页级判定「只有一条题要图」→ 用它", mode3 == "snapped_only", mode3)

    _, mode3b = batch_page.resolve_figure_box(None, cands, W, H)
    check("**没拿到这个授权时绝不猜**（默认 False —— 实测被误用成「一图贴多题」）",
          mode3b == "", mode3b)

    _, mode4 = batch_page.resolve_figure_box(None, cands + [
        {"kind": "img", "rect": (400.0, 700.0, 500.0, 800.0)}], W, H)
    check("有多处图形又没框 → 不猜（宁缺勿错）", mode4 == "", mode4)

    _, mode5 = batch_page.resolve_figure_box([50, 220, 400, 380], [], W, H)
    check("页面查不到候选（扫描版）→ 回退模型框并标 rough", mode5 == "rough", mode5)

    rect6, mode6 = batch_page.resolve_figure_box([50, 220, 400, 380],
                                                 [{"kind": "img", "rect": (520.0, 800.0, 590.0, 840.0)}],
                                                 W, H)
    check("候选区离模型框太远（不同一处的图）→ 也回退模型框",
          mode6 == "rough", mode6)
    check("回退时用的是模型框而不是远处那个候选",
          abs(rect6[0] - (50 / 1000 * W - 3)) < 0.01, repr(rect6))

    _, mode7 = batch_page.resolve_figure_box(None, [], W, H)
    check("既没框也没候选 → 不裁（交人工）", mode7 == "", mode7)

    # 吸附阈值本身：IoU 刚好在门槛附近不该抖
    check("IoU 计算正确（完全重合=1、不相交=0）",
          batch_page._iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
          and batch_page._iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0)

    # ---------------------------------------------------------- 汇总
    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)}/{total} 项未通过：")
        for f in fails:
            print("   -", f)
        print("=" * 60)
        return 1
    print(f"✅ 全部通过（{total} 项）")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
