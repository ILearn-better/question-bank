# -*- coding: utf-8 -*-
r"""公式写法归一化：\(…\) 与 \[…\] → $…$ 与 $$…$$。

为什么要有这个：Markdown 里 `\(` 是「转义的左括号」，marked / 我们的导出解析都会
先把反斜杠吃掉 —— 从讲义、网页、PDF 粘过来的公式到这一步已经变成 `(x)`，
光在公式渲染那头多配几个定界符是没用的（实测就是这个原因）。
所以归一化必须发生在**解析之前**，而且**前后端各一份**：
  · 前端 frontend/src/views/Notes.js 的 normalizeMath（预览用）
  · 后端 backend/app/services/notes_export.py 的 normalize_math（导出 Word/PDF 用）

这个文件只测后端那份（纯函数，不需要服务器）；前端那份的行为在此逐条对应说明。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.services.notes_export import normalize_math   # noqa: E402

fails = []


def check(label, got, want):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}\n       得到 {got!r}" + ("" if okk else f"\n       期望 {want!r}"))
    if not okk:
        fails.append(label)


print("==== ① 行内 \\( \\) 与独立 \\[ \\] 都要转成美元号形式 ====")
check("行内", normalize_math("行内 \\(x^2\\) 结束"), "行内 $x^2$ 结束")
check("独立", normalize_math("\\[\\frac{1}{2}\\]"), "$$\\frac{1}{2}$$")
check("中文紧贴（中文紧跟 ** 那种坑在公式里不存在）", normalize_math("即\\(a^{1}\\)可渲染"), "即$a^{1}$可渲染")
check("已经是 $ 写法的原样不动", normalize_math("$a^{1}$ 与 $$b$$"), "$a^{1}$ 与 $$b$$")

print("==== ② 代码块 / 行内代码里的内容**不能**被改写 ====")
fenced = "```\n\\(x\\)\n```"
check("围栏代码块原样", normalize_math(fenced), fenced)
inline = "示例 `\\(x\\)` 保持"
check("行内代码原样", normalize_math(inline), inline)
mixed = "正文 \\(a\\)\n\n```\n\\(b\\)\n```\n\n尾部 \\(c\\)"
check("正文改、代码块不改", normalize_math(mixed), "正文 $a$\n\n```\n\\(b\\)\n```\n\n尾部 $c$")

print("==== ③ 不该误伤的 ====")
check("转义美元号 \\$", normalize_math("价格 \\$5 与 \\$6"), "价格 \\$5 与 \\$6")
check("普通括号", normalize_math("(a+b) 与 [1,2]"), "(a+b) 与 [1,2]")
check("没配对的半截（只转能配对的那半）", normalize_math("半截 \\[a"), "半截 \\[a")
check("空串", normalize_math(""), "")
check("None", normalize_math(None), "")

print("==== ④ 多行公式（跨行） ====")
check("跨行 \\[ \\]",
      normalize_math("\\[\na = b\n\\]"),
      "$$\na = b\n$$")
check("一次多个", normalize_math("\\(a\\)\\(b\\) \\(c\\)"), "$a$$b$ $c$")

print()
if fails:
    print(f"❌ {len(fails)} 项没过：" + "；".join(fails))
else:
    print("✅ ALL PASS")


# 退出码即结果（0 = 全过）。以前这里只打印不设码 —— 单跑时人看得出来，
# 但脚本化批量回归会把失败当成通过，静默漏掉一整轮。
raise SystemExit(1 if fails else 0)
