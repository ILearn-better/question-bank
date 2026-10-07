# -*- coding: utf-8 -*-
"""前端静态自检：两个入口页的 JS 语法 + 模板标签配对 + 关键标识存在性。

    cd backend && ./.venv/Scripts/python.exe test_entry_page.py

**为什么值得有这么一个测试**：录题页（entry.html）和工作台（src/views/*.js）
都是「零构建」的手写前端 —— 没有打包器替我们挡住语法错误，改错一个括号
或一个中文标点，页面会整块白掉，而服务端接口测试全绿，一点都看不出来。
这里用 node --check 补上这一道闸，跑一次不到 1 秒。

不联网、不起服务、不写任何文件（临时文件落在系统临时目录）。
node 不在 PATH 时只跳过语法检查，结构检查照跑。
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "frontend" / "entry.html"
PAPERS = ROOT / "frontend" / "src" / "views" / "Papers.js"

fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)


def node_check(path: Path, label: str) -> None:
    """把 JS 交给 node 做纯语法检查（不执行）。"""
    if not shutil.which("node"):
        print(f"  [skip] {label}：没找到 node，跳过语法检查")
        return
    dst = Path(tempfile.gettempdir()) / (path.stem + "_syntax_check.mjs")
    dst.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(dst)], capture_output=True, text=True)
    check(f"{label} JS 语法", r.returncode == 0)
    if r.returncode:
        print("        " + (r.stderr or r.stdout).strip().replace("\n", "\n        ")[:600])


def main() -> int:
    print("==== ① 录题页 entry.html ====")
    src = ENTRY.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", src)
    check("有且只有一个内联 script 块", len(blocks) == 1, f"实为 {len(blocks)}")
    if blocks:
        tmp = Path(tempfile.gettempdir()) / "entry_syntax_check.js"
        tmp.write_text(blocks[-1], encoding="utf-8")
        if shutil.which("node"):
            r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
            check("主逻辑 JS 语法", r.returncode == 0)
            if r.returncode:
                print("        " + (r.stderr or r.stdout).strip()[:600])
        else:
            print("  [skip] 主逻辑 JS 语法：没找到 node")

    # 标签配对（自闭合的 <tag/> 要减掉，否则会把正常写法误报成不配对）
    for tag in ("template", "el-form-item", "el-radio-group", "el-input",
                "el-button", "el-tag", "el-form", "el-select", "el-row", "el-col",
                "el-dialog", "el-checkbox", "el-alert", "div"):
        opened = len(re.findall(rf"<{tag}[\s>]", src))
        closed = len(re.findall(rf"</{tag}>", src))
        selfc = len(re.findall(rf"<{tag}\b[^>]*/>", src))
        check(f"<{tag}> 配对", opened == closed + selfc, f"开 {opened} 闭 {closed} 自闭合 {selfc}")

    # 关键能力的存在性。改这块时最容易「只改了一处」——
    # 比如加了渲染框却忘了从 methods 里暴露 clearText，页面会在运行时才报错。
    print("\n==== ② 录题页关键能力 ====")
    need = {
        "Markdown 渲染器（marked）": "marked.min.js",
        "消毒（DOMPurify）": "purify.min.js",
        "LaTeX 定界符归一": "normalizeMath",
        "渲染函数": "renderRich",
        "答案渲染区": "answerPreview",
        "答案渲染调用": "renderAnswerMath",
        "选区指纹（防止冲掉用户改的字）": "lastSelKey",
        "每题出卷偏好": "render_prefer",
        "清空文本": "clearText",
        "选段自动出原貌图": "refreshFigure()",
        "删除文档入口": "askDeleteDoc",
        "删除前的影响预演": "delete-impact",
        "连题一起删的勾选框": "withQuestions",
        "删完复位左侧状态": "afterDocDelete",
    }
    for label, token in need.items():
        check(label, token in src)

    print("\n==== ③ 出卷页 Papers.js ====")
    node_check(PAPERS, "Papers.js")
    pj = PAPERS.read_text(encoding="utf-8")
    for label, token in {
        "整卷呈现方式选项": "render_mode",
        "选项说明文案": "renderModeHint",
        "导出时带上该参数": "render_mode: opts.render_mode",
    }.items():
        check(label, token in pj)

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
