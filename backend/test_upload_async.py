# -*- coding: utf-8 -*-
"""上传路由不许用 `async def` 做同步 CPU 重活（R3）。

为什么这不是风格问题：FastAPI 里 `async def` 跑在**事件循环线程**上。上传端点
拿到 `await file.read()` 之后紧接着做的是纯同步的慢活 ——
PyMuPDF 解析 PDF、Word COM 抽文字、PDF/DOCX 转 Markdown、openpyxl 读整张表、
解 zip 逐篇写笔记（以及几个端点的几 MB 同步落盘）。
这些活动辄几百毫秒到几十秒，期间整个事件循环被占住：
**这台机器上所有页面、所有轮询一起卡住**（单进程服务，没有第二个 worker 兜）。
`def`（同步）由线程池执行，同样的慢活只卡那一个请求。

⚠️ 事实更正（2026-10-27，fixer 指出、主控复核、我自己读了 `services/images.py:39-62`）：
   `images.save_image` **没有 PIL 重编码**，它只做魔数嗅探 `sniff_ext` + `write_bytes`，
   是**纯转手**。原来这三条的说明写成「PIL 重编码」是错的（照抄了一句不准的任务描述），
   已经改成真实理由，见 TARGETS 里的注释 —— 错的注释比没有注释更坏：
   下一个人会照着错前提做判断。

判据（全部静态、不需要起服务，也不需要真上传）：

  ① 这些函数**不是** `AsyncFunctionDef`；
  ② 函数体里没有 `Await` 节点（同步函数里写 await 本来就是语法错，这条是防漏改）；
  ③ 没有漏改的 `file.read(`（`await file.read(n)` → 必须是 `file.file.read(n)`。
     只删 `await` 不改 `file.read` 不报错，但拿到的是协程对象 ——
     落盘的会是一个 co 对象，**上传成功但文件是坏的**，比 500 更难查）；
  ④ 慢活那一句还在（防止「把 async 去掉」时顺手把功能删了）。

⚠️ 只对**核实过确有同步重活**的端点断言。`grade_archives.upload_archive`
   只是 校验 + 落盘 + 入库，没有解析，改它没有收益 —— 所以这里不对它断言
   （写死进去就成了「为了凑数改代码」，与本轮纪律相反）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# (文件, 函数名, 里面那句慢活, 人话说明)
#
# 名单是**核实过**的：一个一个读函数体，追到那句慢活里，看它到底是解析/落盘/解压。
# 核实结论写在这里，防止下一个人（包括我自己）又凭印象重排一遍：
#
#   · 解析类（真 CPU 活）：documents / auto → PyMuPDF + docx；notes:import_document →
#     to_markdown；lesson_files → file_text.extract；homework_grades → grade_basis.extract；
#     lesson_import → openpyxl；plans → convert_to_markdown；notes_transfer → 解 zip。
#   · 落盘类（uploads / notes 的 upload_image、feedbacks 的 upload_feedback_image）：
#     `images.save_image` 只是 `sniff_ext` + `write_bytes`，**纯转手，没有任何重编码（没有 PIL）**。
#     ⚠️ 它们留在名单里的理由，**不是**「落盘也很慢」——那比 Word COM 解析弱两个数量级，
#        单看性能不值得单独改。真正的原因是**同一个功能有三个入口**：
#           uploads.py: upload_image          通用上传
#           notes.py:   upload_image          笔记配图
#           feedbacks.py: upload_feedback_image 反馈配图
#        三处做的是同一件事（存一张图）。如果只把解析类那几个改同步、留这三个 async，
#        同一个功能在不同入口下行为就不一样，而这个不一致**没有任何注释能解释** ——
#        下一个人只会更困惑：为什么这个 async 安全、那个不安全？
#        要么三个全改、要么三个全不改，不能挑（主控 2026-10-27 拍板：全改）。
#     加上全局口径（11 个上传端点全同步，以后不用逐个判断），这条就站得住了。
TARGETS = [
    # —— 第一批（阶段门 ①）：确有同步重活 ——
    ("app/routers/documents.py", "upload_document", "register_document",
     "PyMuPDF 解析 PDF / docx 抽块"),
    ("app/routers/uploads.py", "upload_image", "images.save_image",
     "纯转手（无 CPU 活）—— 改它是因为存图有三个入口，不能挑着改"),
    ("app/routers/notes.py", "upload_image", "images.save_image",
     "纯转手（无 CPU 活）—— 改它是因为存图有三个入口，不能挑着改"),
    ("app/routers/notes.py", "import_document", "doc_to_note.to_markdown",
     "Word / PDF / PPT / HTML 转 Markdown"),
    ("app/routers/lesson_files.py", "upload_file", "file_text.extract",
     "抽文字（PDF / docx / pptx 解析）"),
    ("app/routers/homework_grades.py", "upload_basis", "add_basis_file",
     "判定依据抽文字（PDF / Word / Excel）"),

    # —— 第二批（主控扩围）：同一个 bug、同一处改法 ——
    # auto 的上传和 documents 是**同一条重活**（都走 register_document）；
    # 它「立刻开始导入」用的是 services/auto_import.start 里的 threading.Thread，
    # **不是 FastAPI 的 BackgroundTasks**，所以摘掉 async 不影响后台任务。
    ("app/routers/auto.py", "auto_import_upload", "register_document",
     "PyMuPDF 解析 PDF / docx 抽块（与 documents 同一条）"),
    ("app/routers/feedbacks.py", "upload_feedback_image", "images.save_image",
     "纯转手（无 CPU 活）—— 改它是因为存图有三个入口，不能挑着改"),
    ("app/routers/plans.py", "upload_plan_file", "convert_to_markdown",
     "上传即转 Markdown（Word / PDF 解析）"),
    ("app/routers/lesson_import.py", "preview", "parse_workbook",
     "openpyxl 解析课表 + 逐行查学生/查重"),
    ("app/routers/notes_transfer.py", "import_notes", "import_zip",
     "解 zip + 逐篇写笔记"),
]

# 核实后**确认没有同步重活**、本轮不收的（写在这里是为了防止下次又要重新排查一遍）：
#   · grade_archives.py: upload_archive   → validate + 落盘 + 入库，没有解析
#   · exams.py: upload_exam_file          → 落盘 + kind_for_archive（只看扩展名）

total = 0
fails: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    global total
    total += 1
    if ok:
        print(f"  [OK ] {name}")
    else:
        print(f"  [FAIL] {name}" + (f"   <- {detail}" if detail != "" else ""))
        fails.append(name)
    return bool(ok)


def find_fn(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def walk_async_defs(tree: ast.Module) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            out.append(node.name)
    return out


def main() -> int:
    print("=" * 72)
    print("上传端点必须是同步 def（R3）")
    print("=" * 72)

    print("\n=== ① 确有同步重活的上传端点：不能是 async def ===")
    for rel, fname, slow, why in TARGETS:
        p = ROOT / rel
        if not p.exists():
            check(f"{rel} 存在", False, "文件不见了")
            continue
        src = p.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(p))
        node = find_fn(tree, fname)

        if node is None:
            check(f"{rel}:{fname} 存在", False, "找不到这个函数（改名了？）")
            continue

        seg = ast.get_source_segment(src, node) or ""

        check(f"{rel}:{fname} 是同步 def（原因：{why}）",
              not isinstance(node, ast.AsyncFunctionDef),
              "现在还是 async def：慢活在事件循环线程上跑，整站会跟着卡住")

        awaits = [n for n in ast.walk(node) if isinstance(n, ast.Await)]
        check(f"{rel}:{fname} 函数体里没有 await", not awaits,
              f"还有 {len(awaits)} 处 await")

        # 只删 await、不改 file.read → 拿到协程对象，上传「成功」但文件是坏的
        bad = [ln.strip() for ln in seg.splitlines()
               if "file.read(" in ln and "file.file.read(" not in ln]
        check(f"{rel}:{fname} 没有漏改的 file.read(（应是 file.file.read(）",
              not bad, "；".join(bad[:3]))

        check(f"{rel}:{fname} 里那句慢活还在（{slow}）", slow in seg,
              "找不到这句 —— 是不是把功能一起删了")

    print("\n=== ② 全库还剩哪些 async def（下列是**本轮不改**的，只做记录） ===")
    left = []
    for p in sorted((ROOT / "app" / "routers").glob("*.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        except SyntaxError as e:                        # noqa: PERF203
            check(f"{p.name} 能解析", False, str(e))
            continue
        for name in walk_async_defs(tree):
            left.append(f"{p.name}:{name}")
    for item in left:
        print(f"  [info] {item}")
    print(f"  （共 {len(left)} 个 async def，其中 {len(TARGETS)} 个是本次要摘掉的）")

    print("\n" + "=" * 72)
    print(f"共 {total} 项，失败 {len(fails)} 项")
    if fails:
        for f in fails:
            print(f"  x {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
