# -*- coding: utf-8 -*-
"""前端**真实挂载**检查：把两个页面喂给 jsdom，走浏览器同一套 HTML 解析器挂一遍。

    cd backend && ./.venv/Scripts/python.exe test_render_page.py

**为什么值得有这么一个测试**：本项目最贵的 bug 类是「**接口测试全绿、页面白屏**」。
`batch.html` / `entry.html` 是零构建手写 HTML，根模板走**浏览器解析器** ——
自定义组件写成 `<x ... />` 时结尾那个 `/` 被忽略 → 当开标签 → 紧跟的 `v-else`
被吞 → Vue compiler-30 → `mount()` 不执行 → 白屏。
而**字符串模板语义下复现不出来**（Vue 自己的解析器认 `/`），所以
`test_batch_page.py` ⑤ 段那个自带分词器只能「模拟」浏览器，这里是真的挂一遍。

三层防线各管一段，别互相替代：
    · `test_entry_page.py` / `test_batch_page.py` ⑤ —— 静态标签配对 + 模拟解析器（快，秒级）
    · **本文件**                                   —— jsdom 真挂载（慢几秒，能抓编译期错误）
    · 用户肉眼                                     —— 最后一道（agent 读不了图，只能交给用户）

不联网、不起服务、不写任何文件。node 或 jsdom 不在就**跳过**（不算失败）——
它只是开发期的加固检查，不该因为某台机器没装 jsdom 就把回归弄红。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
SCRIPT = FRONTEND / "render_check.js"

# 期望 node 输出里出现的「配图状态」文案 —— 本轮的四个分支，一个都不能少
FIGURE_TEXTS = [
    "AI 自动框的图",     # flags 里有 figure_snapped / figure_only_candidate
    "AI 估的图框",       # flags 里有 figure_rough（扫描版，框是估的）
    "没能在这页上定位到它",  # flags 里有 figure_box_missing（让老师自己框）
    "还没框图",          # 有图但还没框（`figureLocateFailed` 为假的那一支）
]

fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)


def main() -> int:
    print("==== ① 检查脚本自身（防止防线被无声删掉）====")
    check("frontend/render_check.js 存在", SCRIPT.is_file())
    if not SCRIPT.is_file():
        print("\n" + "=" * 60)
        print("❌ 检查脚本不在了，后面没法跑")
        return 1

    js = SCRIPT.read_text(encoding="utf-8")

    check("真的用了浏览器 HTML 解析器（JSDOM）", "new JSDOM(" in js)
    check("两个页面都在检查范围里",
          "'batch.html'" in js and "'entry.html'" in js)
    # ⚠️ 判据退化成「只看 innerHTML 长度」= 假通过（未渲染的原始模板就有 1.8 万字符）
    check("判据是**挂载痕迹**而不是长度（数 .el-button）", "el-button" in js and "marks(" in js)
    check("判据包含「不再残留原始模板」", "v-else-if=" in js)
    # ⚠️ JSDOM.fromFile 在路径含空格时基地址解析错 → vendor 全加载不到 → 假通过
    check("给了显式 url（防 fromFile 空格路径假通过）", "pathToFileURL(file).href" in js)
    check("把绝对资源路径改写成相对（防 vendor 加载不到）", "rewriteAssets" in js)
    check("页面异常不杀进程（uncaughtException 兜住）", "uncaughtException" in js)
    check("jsdom 找不到时优雅跳过（退出码 0）", "SKIP" in js)

    for t in FIGURE_TEXTS:
        check(f"断言覆盖配图分支：「{t}」", t in js)
    check("断言覆盖两档 AI 可信度互斥", "互斥" in js)
    check("断言覆盖「图片·1 页」（上传图片的支持）", "图片·1 页" in js)
    check("断言覆盖 isPaged()/isImg()", "isPaged" in js and "isImg" in js)

    print("\n==== ② 真的挂一遍两个页面 ====")
    node = shutil.which("node")
    if not node:
        print("  [skip] 没找到 node，跳过真实挂载检查")
        print("\n" + "=" * 60)
        print(f"{'❌ ' + str(len(fails)) + ' 项未通过：' + str(fails) if fails else '✅ 全部通过（挂载检查已跳过）'}")
        return 1 if fails else 0

    r = subprocess.run(
        [node, str(SCRIPT)], cwd=str(FRONTEND),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    out = (r.stdout or "") + (r.stderr or "")
    print("\n".join("  " + ln for ln in out.strip().splitlines()))

    if "SKIP:" in out and r.returncode == 0:
        print("  [skip] jsdom 没装，跳过（不算失败）")
        print("\n" + "=" * 60)
        print(f"{'❌ ' + str(len(fails)) + ' 项未通过：' + str(fails) if fails else '✅ 全部通过（挂载检查已跳过）'}")
        return 1 if fails else 0

    # 退出码即结果 —— 与项目里其它测试脚本同一套约定
    check("render_check.js 退出码为 0", r.returncode == 0, f"实为 {r.returncode}")
    check("两个页面都渲染出来了（输出含「全部通过」）", "全部通过" in out)
    check("没有页面级未捕获异常", "页面没有未捕获异常" in out)
    check("vendor 脚本都加载上了", "Could not load" not in out)
    # 失败项数不能只是「凑够」
    check("检查项数合理（≥ 25 项）", out.count("\u2713") >= 25, f"实为 {out.count(chr(0x2713))} 项")

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{json.dumps(fails, ensure_ascii=False)}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
