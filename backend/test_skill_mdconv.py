# -*- coding: utf-8 -*-
"""「试卷 → 题库格式 md」那条 skill 的验收测试。

    backend/.venv/Scripts/python.exe test_skill_mdconv.py     # 退出码即结果

## 这一套在守什么

    A 跨页合并   —— 一道题跨两页被切成两半，各自成题就是两道废题（一道没结尾、
                    一道连知识点都判不出来）；但把两道无关的题粘起来更糟 ——
                    读起来"像一句话"，老师发现不了
    B md 格式    —— 新题带 shike_id 会被当成「已有题」；图片写成 ![[…]] 只有
                    Obsidian 认；图跑到 ## 答案 之后会变成答案配图（只教师版印）
    C 知识点     —— 写模型的原话而不是树上的节点名 → 挂不上树，按知识点筛题
                    和掌握度统计里等于没有这道题，且不报错
    D 重跑       —— 同一份文件跑两遍造出两份 md → 此后每次同步都红一条
    E 配图判据   —— 模型估的框占了大半页（把题干框进来了）、表格已经转成文字，
                    这两种配了不如不配

⚠️ 不调 AI：识别结果一律自己造。一条 AI 调用都不发，跑一次几秒钟。

⚠️ 全程用临时目录（`SHIKE_DB_PATH` / `SHIKE_DATA_DIR` / `SHIKE_VAULT` 指到 TEMP），
   **必须在 import app.* 之前设置**，否则 engine 已经钉在真库上了。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shike_mdconv_"))
os.environ["SHIKE_DB_PATH"] = str(TMP / "t.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_VAULT"] = str(TMP / "vault")
os.environ["SHIKE_VAULT_INBOX"] = "0"
os.environ["SHIKE_AUTO_MIGRATE"] = "1"

BACKEND = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

from app import config, migrate                                   # noqa: E402
from app.db import SessionLocal                                   # noqa: E402
from app.models import Curriculum, Node, Question                 # noqa: E402
from app.services import vault                                    # noqa: E402
from sqlalchemy import select, func                               # noqa: E402

SKILL_PY = BACKEND.parent / ".workbuddy" / "skills" / "shike-question-md" / "scripts" / "doc_to_md.py"

fails: list[str] = []


def check(name: str, ok, detail=None) -> bool:
    print(("[OK]   " if ok else "[FAIL] ") + name
          + (f"   ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        fails.append(name)
    return bool(ok)


def truthy(name: str, value, detail=None) -> bool:
    return check(name, bool(value), detail if detail is not None else value)


def load_skill():
    spec = importlib.util.spec_from_file_location("skill_mdconv", SKILL_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 造一页 PDF
def make_pdf() -> Path:
    """一页 A4，中间画一个带斜线的方框 —— 够 build_question 裁一次图就行。"""
    import pymupdf

    out = TMP / "fake.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(pymupdf.Rect(80, 160, 320, 380), color=(0, 0, 0), width=1.2)
    page.draw_line(pymupdf.Point(80, 380), pymupdf.Point(320, 160), color=(0, 0, 0), width=1)
    doc.save(out)
    doc.close()
    return out


def item(**kw) -> dict:
    """一条识别结果（字段与 batch_page.parse_page_items 的输出对齐）。"""
    base = {
        "content": "已知抛物线 $C:y^2=6x$ 的焦点为 $F$。", "answer": "", "analysis": "",
        "qtype": "选择题", "difficulty": "中档", "knowledge_point": "", "tags": [],
        "confidence": "high", "needs_figure": False, "figure_note": "",
        "figure_box": None, "note": "", "flags": [],
    }
    base.update(kw)
    return base


# ================================================================ A 跨页合并
def part_a(m) -> None:
    print("\n==== A 跨页合并 ====")
    cut = item(content="……过 $A$ 作直线 $l:x=-3$ 的垂", note="题干在本页底部被截断，疑似未完")
    cont = item(content="线，垂足为 $D$，则（ ）", note="题干主体在上一页，本页只余尾部")
    page_items, n = m.merge_cross_pages([(2, [item(content="第 4 题"), cut]), (3, [cont, item(content="第 6 题")])])
    check("两边都说了截断 → 拼成一道", n == 1, n)
    check("拼完第 3 页只剩 1 条", len(page_items[1][1]) == 1, len(page_items[1][1]))
    joined = page_items[0][1][-1]["content"]
    truthy("后半段真的接在后半句后面", joined.endswith("线，垂足为 $D$，则（ ）"), joined[-30:])
    truthy("拼过的题留下记号", "cross_page_merged" in page_items[0][1][-1]["flags"])

    # 后半段自己带题号 → 那是新题，不是续写
    fresh = item(content="13. 已知函数 $f(x)=x^2$，求其最小值。",
                 note="题干主体在上一页，本页只余尾部")
    _, n2 = m.merge_cross_pages([(2, [cut]), (3, [fresh])])
    check("后半段以「13.」开头 → 不拼", n2 == 0, n2)

    # 只有一边提到截断 → 证据不够
    _, n3 = m.merge_cross_pages([(2, [item(content="……过 $A$ 作垂")]), (3, [cont])])
    check("上一页没说截断 → 不拼", n3 == 0, n3)

    # 位置不对：截断的那条不在页末
    _, n4 = m.merge_cross_pages([(2, [cut, item(content="第 5 题")]), (3, [cont])])
    check("截断的题不是该页最后一条 → 不拼", n4 == 0, n4)


# ================================================================ B/C/D/E 生成
def part_make(m, db, pdf: Path, curriculum_id: int) -> None:
    out = TMP / "题库"
    attach = TMP / "附件"
    out.mkdir(parents=True, exist_ok=True)
    attach.mkdir(parents=True, exist_ok=True)

    def build(it, decision=None, dry_run=False, overwrite=False):
        return m.build_question(
            db, it, pno=1, idx=2, decision=decision or {"rect": None, "box": None, "mode": "",
                                                        "dup": False, "shared": False, "area": 0.0},
            pdf_path=pdf, attach_dir=attach, md_dir=out, curriculum_id=curriculum_id,
            overwrite=overwrite, dry_run=dry_run,
        )

    print("\n==== B md 格式 ====")
    row = build(item(knowledge_point="抛物线的定义与标准方程"))
    text = Path(row["path"]).read_text(encoding="utf-8")
    check("生成了 md 文件", Path(row["path"]).is_file(), row["path"])
    truthy("带 shike: question 标记", "shike: question" in text)
    check("没有 shike_id 行（新题不该带 id）", "shike_id" not in text,
          [l for l in text.split("\n") if "shike_id" in l])
    check("没用 Obsidian 的 ![[…]] 写法（只有它认）", "![[" not in text,
          [l for l in text.split("\n") if "![[" in l])

    # 带图那一道
    row2 = build(item(content="如图，在正方体中，求异面直线夹角。", needs_figure=True,
                      figure_box=[100, 150, 400, 450]),
                 decision={"rect": (60, 120, 340, 400), "box": [100, 140, 570, 470],
                           "mode": "snapped", "dup": False, "shared": False, "area": 0.15})
    text2 = Path(row2["path"]).read_text(encoding="utf-8")
    check("配图裁出来了", bool(row2["figure"]) and Path(row2["figure"]).is_file(), row2["figure"])
    truthy("链接是 ![](<../附件/…>)", "![](<../附件/" in text2,
           [l for l in text2.split("\n") if "![" in l])
    stem_part, _, ans_part = text2.partition("## 答案")
    truthy("配图在题干里（不在 ## 答案 之后）", "![](" in stem_part and "![](" not in ans_part)
    falsy_name = Path(row2["figure"]).name
    truthy("附件名里没有会截断链接的字符",
           not any(c in falsy_name for c in "[](){}#%&"), falsy_name)

    print("\n==== C 知识点 ====")
    truthy("树上有的知识点写成了节点名",
           "  - 抛物线的定义与标准方程" in text,
           [l for l in text.split("\n") if l.startswith("  - ")])
    row3 = build(item(content="树上没有这个知识点的题。", knowledge_point="量子纠缠与导数"))
    text3 = Path(row3["path"]).read_text(encoding="utf-8")
    truthy("树上没有的保留原话（不瞎猜）", "量子纠缠与导数" in text3, text3[:200])
    truthy("并打了 kp_not_on_tree 记号", "kp_not_on_tree" in row3["flags"], row3["flags"])

    print("\n==== D 重跑不造第二份 ====")
    n_before = len(list(out.glob("*.md")))
    again = build(item(knowledge_point="抛物线的定义与标准方程"))
    truthy("重跑被跳过（说明里有原因）", bool(again["skipped"]), again["skipped"])
    check("磁盘上没有多出文件", len(list(out.glob("*.md"))) == n_before,
          (n_before, len(list(out.glob("*.md")))))

    print("\n==== E 配图判据 ====")
    big = build(item(content="题干里全是字的一道题。", needs_figure=True, figure_box=[50, 60, 950, 900]),
                decision={"rect": (30, 50, 566, 758), "box": [50, 60, 950, 900],
                          "mode": "rough", "dup": False, "shared": False, "area": 0.81})
    check("模型框占了大半页 → 不配图", not big["figure"], big["figure"])
    truthy("并且说了为什么", any("占了整页" in h for h in big["needs_human"]), big["needs_human"])

    table_content = "得到如下列联表：\n\n| 组别 | 人数 |\n|---|---|\n| 甲 | 10 |\n"
    tbl = build(item(content=table_content, needs_figure=True, figure_box=[100, 400, 800, 600]),
                decision={"rect": (60, 340, 500, 600), "box": [100, 400, 800, 600],
                          "mode": "rough", "dup": False, "shared": False, "area": 0.15})
    check("表格已在题干里 → 不再配图", not tbl["figure"], tbl["figure"])
    truthy("说明了是表格转文字", any("表格" in h for h in tbl["needs_human"]), tbl["needs_human"])

    miss = build(item(content="如图，求阴影面积。", needs_figure=True, figure_note="一个半圆"),
                 decision={"rect": None, "box": None, "mode": "", "dup": False,
                           "shared": False, "area": 0.0})
    check("说要图但没定位到 → 不配图", not miss["figure"], miss["figure"])
    truthy("告诉人去哪补", any("附件" in h for h in miss["needs_human"]), miss["needs_human"])


# ================================================================ main
def main() -> int:
    print("临时目录:", TMP)
    migrate.run_migrations(verbose=False)
    from app import seed

    db0 = SessionLocal()
    try:
        seed.seed_all(db0)          # 体系骨架由 seed 补（迁移只建表）
    finally:
        db0.close()
    db = SessionLocal()
    try:
        curr = Curriculum(name="测试体系", code="test-curr")
        db.add(curr)
        db.flush()
        db.add(Node(name="抛物线的定义与标准方程", curriculum_id=curr.id, level=2))
        db.add(Node(name="导数的概念与几何意义", curriculum_id=curr.id, level=2))
        db.commit()

        m = load_skill()
        check("skill 脚本能加载", callable(m.build_question))
        check("skill 脚本路径存在", SKILL_PY.is_file(), SKILL_PY)

        pdf = make_pdf()
        vault.ensure_dirs()

        part_a(m)
        part_make(m, db, pdf, curr.id)
    finally:
        db.rollback()
        db.close()

    print("\n==== 结果 ====")
    if fails:
        print(f"失败 {len(fails)} 条：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
