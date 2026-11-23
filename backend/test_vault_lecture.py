# -*- coding: utf-8 -*-
"""讲义 md（`shike: lecture`）的验收测试。

    backend/.venv/Scripts/python.exe test_vault_lecture.py     # 退出码即结果

## 这一套在守什么

老师的用法是「一篇讲义里既写知识、又放题目，点同步 → 知识不动、题目进题库」。
这条链上每一个静默出错的后果都很具体：

  A 切块     —— 知识部分被当成题收进库（老师最不能接受的一种），或者几道题被切成一道
  B 导入     —— 题干/答案/解析串位，或整篇只进去一道
  C 幂等     —— 每点一次同步就把题再入一遍（一道题变两道）
  D 改讲义   —— 改了答案不生效 / 没改的题被刷新 updated_at
  E 配图     —— 讲义的图进错列（出卷时印不出图）
  F 导出     —— 讲义里的题被另写一份到 `题库/`，同一道题两份正文互相盖
  G 冲突     —— 复制整篇讲义就多出两道同 id 的题（不报错，之后每次同步都失败）
  H 模板     —— 「讲义模板」自己那两道示例题进了库
  I 写回     —— 网页改题写回时整篇重写，把老师的知识部分全冲掉
  J 收件箱   —— 讲义被当成「待拆的卷子」搬走 / 按 `---` 切段入库
  K 同篇撞 id —— 两段写着同一个 id，一段静默覆盖另一段

⚠️ 全程用临时目录（`SHIKE_DB_PATH` / `SHIKE_DATA_DIR` / `SHIKE_VAULT` 指到 TEMP），
   **必须在 import app.* 之前设置**，否则 engine 已经钉在真库上了。
"""
from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shike_lecture_"))
os.environ["SHIKE_DB_PATH"] = str(TMP / "t.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_VAULT"] = str(TMP / "vault")
os.environ["SHIKE_VAULT_INBOX"] = "0"          # 别让后台线程在测试里跑起来
os.environ["SHIKE_AUTO_MIGRATE"] = "1"

sys.path.insert(0, str(Path(__file__).parent))

from app import config, migrate, seed                          # noqa: E402
from app.db import SessionLocal                                 # noqa: E402
from app.models import Question                                 # noqa: E402
from app.services import vault, vault_inbox                     # noqa: E402

fails: list[str] = []

# 1×1 的 PNG。配图那条要的是「图能不能被认出来、进对列」，不是画质。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def check(name: str, ok, detail=None) -> bool:
    print(("[OK]   " if ok else "[FAIL] ") + name + (f"   ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        fails.append(name)
    return bool(ok)


def truthy(name: str, value, detail=None) -> bool:
    return check(name, bool(value), detail if detail is not None else value)


def falsy(name: str, value, detail=None) -> bool:
    return check(name, not value, value if detail is None else detail)


# ---------------------------------------------------------------- 素材
# 知识部分故意写成**和题目一模一样的句子形态**（1. 2. 编号、`## 答案` 之类都不缺），
# 这样「按内容嗅探」的实现会立刻翻车，能真正测出「只扫 ## 题目 之后」。
LECTURE = """---
shike: lecture
title: 第一讲 空间几何体的结构
knowledge:
  - 棱柱的结构特征
tags:
  - 讲义
---

# 第一讲 空间几何体的结构

## 知识点

1. 知识专用句子：棱柱的侧面都是平行四边形（课本 P12）。
2. 知识专用句子：棱锥的侧面是三角形。

### 一、棱柱

知识专用段落：棱柱有两个面互相平行。

## 题目

### 题目 1

判断：棱柱的侧面一定是平行四边形。（　　）

A．正确　　B．错误

#### 答案

A

#### 解析

插图说明：因为定义就是这么写的。

### 题目 2

看图回答：这个几何体有几条棱？

![](<../附件/图.png>)

#### 答案

15

## 小结

1. 知识专用句子：小结里的编号也不该被当成题。
"""


def write_lec(name: str, text: str) -> Path:
    p = vault.lecture_dir() / name
    vault.write_md(p, text)
    return p


def stu(text: str) -> str:
    """这个字符串里有没有「知识专用」四个字 —— 知识部分漏进题的判据。"""
    return "知识专用" in (text or "")


# ---------------------------------------------------------------- A 切块（纯函数）
def part_a() -> None:
    print("\n==== A 切块：只认 ## 题目 之后、### 起头的段 ====")
    blocks, why = vault.lecture_blocks(LECTURE)
    check("切出 2 段", len(blocks) == 2, len(blocks))
    check("标题被记下来了（写回时原样保留）",
          [b.heading for b in blocks] == ["### 题目 1", "### 题目 2"], [b.heading for b in blocks])
    check("知识部分没被切进来（含编号列表与 ### 小节）",
          all(not stu(b.body) for b in blocks), [b.body[:40] for b in blocks])
    check("`## 小结` 之后的内容没被切进来",
          all("小结里的编号" not in b.body for b in blocks))
    check("第 1 段切到了答案和解析",
          "#### 答案" in blocks[0].body and "A" in blocks[0].body, blocks[0].body)
    check("第 2 段到下一个三级标题就结束（没吃进小结）",
          "小结" not in blocks[1].body, blocks[1].body)

    _, why = vault.lecture_blocks("---\nshike: lecture\n---\n\n## 知识点\n\n只写知识。\n")
    truthy("没有 `## 题目` 小节 → 说清原因", "题目" in why, why)

    _, why = vault.lecture_blocks("---\nshike: lecture\n---\n\n## 题目\n\n1. 一道题\n---\n2. 另一道\n")
    truthy("有题目小节但没有 `### ` → 提示加三级标题", "### " in why, why)

    fenced = ("---\nshike: lecture\n---\n\n## 题目\n\n### 真题\n\n```\n### 这是代码里的假标题\n```\n\n#### 答案\n\nA\n")
    fb, _ = vault.lecture_blocks(fenced)
    check("代码块里的 `### xxx` 不算一道题", len(fb) == 1, len(fb))

    with_ans3 = "---\nshike: lecture\n---\n\n## 题目\n\n### 题目 1\n\n题干\n\n### 答案\n\nA\n"
    ab, _ = vault.lecture_blocks(with_ans3)
    check("`### 答案` 不会把一题切成两题", len(ab) == 1, len(ab))
    truthy("`### 答案` 仍被认成答案分界", "### 答案" in ab[0].body, ab[0].body)

    commented = ("---\nshike: lecture\n---\n\n## 题目\n\n### 题目 1\n\n题干一句。\n\n"
                 "<!-- 备注：这里该配图，形如 ![](<../附件/图.png>) -->\n\n#### 答案\n\nA\n")
    cb, _ = vault.lecture_blocks(commented)
    body = vault._block_body(cb[0].body)
    falsy("块里的 HTML 注释被去掉（老师的备注不会变成题干）", "备注" in body, body)
    falsy("注释里当例子写的 ![](…) 不会被当成真的图片引用", "![](" in body, body)


# ---------------------------------------------------------------- B 导入
def part_b(db) -> None:
    print("\n==== B 导入：知识部分不进库，题目逐道进库 ====")
    p = write_lec("第一讲.md", LECTURE)
    before = db.query(Question).count()
    prev = vault.scan(db, dry_run=True)
    plc = next((x for x in prev["lectures"] if x["path"] == vault.rel(p)), None)
    truthy("预览：讲义也在报告里", plc is not None, [x["path"] for x in prev["lectures"]])
    check("预览：说会新增 2 道", plc and len(plc["created"]) == 2, plc)
    check("预览没动库", db.query(Question).count() == before, db.query(Question).count())
    falsy("预览没往讲义里写 id", "<!-- shike_id:" in vault.read_md(p).text)

    rep = vault.scan(db)
    lec = next((x for x in rep["lectures"] if x["path"] == vault.rel(p)), None)
    truthy("讲义出现在报告的 lectures 一栏", lec is not None, [x["path"] for x in rep["lectures"]])
    check("新增 2 道", len(lec["created"]) == 2, lec)
    falsy("没有失败", lec["errors"], lec["errors"])
    falsy("没有跳过", lec["skipped"], lec["skipped"])
    check("库里多了 2 道题", db.query(Question).count() == before + 2, db.query(Question).count())
    falsy("讲义没被算成「不是题目 md」",
          [s for s in rep["skipped"] if s["path"] == vault.rel(p)], rep["skipped"])

    rows = db.query(Question).order_by(Question.created_at).all()[-2:]
    q1, q2 = rows
    truthy("题干是题目那句", "棱柱的侧面一定" in (q1.content or ""), q1.content)
    falsy("题干里没有知识部分的文字", stu(q1.content), q1.content)
    falsy("答案没串到题干里", "#### 答案" in (q1.content or ""), q1.content)
    check("答案 = A", (q1.answer or "").strip() == "A", q1.answer)
    truthy("解析进来了", "定义" in (q1.analysis or ""), q1.analysis)
    falsy("解析的内容没串到题干里", "插图说明" in (q1.content or ""))
    check("第 2 题答案是 15", (q2.answer or "").strip() == "15", q2.answer)
    check("知识点从讲义 frontmatter 继承下来",
          "棱柱的结构特征" in (q1.knowledge_points or ""), q1.knowledge_points)
    falsy("md_path 是空的（不占「一文件一题」的位置）", q1.md_path, q1.md_path)
    check("note_path 指到那篇讲义", q1.note_path == vault.rel(p), q1.note_path)

    text = vault.read_md(p).text
    check("两段都写回了 shike_id 注释", text.count("<!-- shike_id:") == 2, text.count("<!-- shike_id:"))
    truthy("写回的 id 就是库里那两道题",
           all(q.id in text for q in (q1, q2)), [q1.id, q2.id])
    truthy("知识部分一个字符没被改掉", "知识专用段落" in text and "知识专用句子" in text)
    _ = rep


# ---------------------------------------------------------------- C 幂等
def part_c(db) -> None:
    print("\n==== C 幂等：再点一次同步不会又入一遍 ====")
    p = vault.lecture_dir() / "第一讲.md"
    before = db.query(Question).count()
    ids = {q.id for q in db.query(Question).all()}
    rep = vault.scan(db)
    lec = next(x for x in rep["lectures"] if x["path"] == vault.rel(p))
    check("两道都算「未变」", lec["unchanged"] == 2, lec)
    falsy("没有新增", lec["created"], lec["created"])
    falsy("没有更新", lec["updated"], lec["updated"])
    check("库里题数没变", db.query(Question).count() == before, db.query(Question).count())
    check("题目 id 没变（不是删了重建）", {q.id for q in db.query(Question).all()} == ids)


# ---------------------------------------------------------------- D 改讲义
def part_d(db) -> None:
    print("\n==== D 改讲义：改了的那道跟着变，没改的不动 ====")
    p = vault.lecture_dir() / "第一讲.md"
    md = vault.read_md(p).text
    md2 = md.replace("#### 答案\n\nA\n", "#### 答案\n\nB\n", 1)
    check("（准备）讲义里确实换掉了答案", md2 != md)
    vault.write_md(p, md2)
    rows = {q.id: q for q in db.query(Question).all()}
    q1 = next(q for q in rows.values() if (q.answer or "").strip() == "A")
    q2 = next(q for q in rows.values() if (q.answer or "").strip() == "15")
    stamp1, stamp2 = q1.md_hash, q2.md_hash

    rep = vault.scan(db)
    lec = next(x for x in rep["lectures"] if x["path"] == vault.rel(p))
    check("只更新 1 道", len(lec["updated"]) == 1, lec["updated"])
    check("另一道算未变", lec["unchanged"] == 1, lec)
    db.expire_all()
    check("答案跟着变成 B", (db.get(Question, q1.id).answer or "").strip() == "B",
          db.get(Question, q1.id).answer)
    check("没改的那道指纹没动", db.get(Question, q2.id).md_hash == stamp2)
    check("改过的那道指纹更新了", db.get(Question, q1.id).md_hash != stamp1)

    rep2 = vault.scan(db)
    lec2 = next(x for x in rep2["lectures"] if x["path"] == vault.rel(p))
    check("再同步一次 → 两道都未变（不会每次都说「更新了」）", lec2["unchanged"] == 2, lec2)


# ---------------------------------------------------------------- E 配图
def part_e(db) -> None:
    print("\n==== E 配图：讲义的图进「题干配图」列 ====")
    q2 = next(q for q in db.query(Question).all() if (q.answer or "").strip() == "15")
    truthy("figure_image 有值", q2.figure_image, q2.figure_image)
    truthy("是个 /api/crops/ 的地址", str(q2.figure_image).startswith("/api/crops/"), q2.figure_image)
    name = str(q2.figure_image).rsplit("/", 1)[-1]
    truthy("截图目录里真有这张图", (config.CROPS_DIR / name).is_file(), name)
    falsy("题干里没留下 `![](...)` 这种字面链接",
          "![](" in (q2.content or ""), q2.content)


# ---------------------------------------------------------------- F 导出名单
def part_f(db) -> None:
    print("\n==== F 导出：讲义里的题不会在 题库/ 再写一份 ====")
    p = vault.lecture_dir() / "第一讲.md"
    out = vault.pending_export(db)
    truthy("待导出名单里没有讲义里的题",
           not [q for q in out if q.note_path == vault.rel(p)], [q.id for q in out])
    # 讲义被删 → 那些题确实没有文件了，退回普通导出（不能永远导不出来）
    shutil.move(str(p), str(TMP / "第一讲-备份.md"))
    out2 = vault.pending_export(db)
    ids = {q.id for q in out2}
    q2 = next(q for q in db.query(Question).all() if (q.answer or "").strip() == "15")
    truthy("讲义没了 → 那些题进待导出名单", q2.id in ids, [q.id for q in out2])
    check("note_path 被清掉", q2.note_path is None, q2.note_path)
    shutil.move(str(TMP / "第一讲-备份.md"), str(p))
    db.rollback()


# ---------------------------------------------------------------- G 冲突
def part_g(db) -> None:
    print("\n==== G 复制整篇讲义：报出来，不许双写 ====")
    p = vault.lecture_dir() / "第一讲.md"
    copy = vault.lecture_dir() / "第一讲-副本.md"
    shutil.copyfile(p, copy)
    before = db.query(Question).count()
    rep = vault.scan(db)
    bad = [x for x in rep["lectures"] if x["path"] == vault.rel(copy)]
    truthy("副本出现在报告里", bad, [x["path"] for x in rep["lectures"]])
    check("两道都报冲突", len(bad[0]["errors"]) == 2, bad[0]["errors"])
    truthy("说的是「已经属于哪篇讲义」", "已经属于" in bad[0]["errors"][0]["error"],
           bad[0]["errors"][0]["error"])
    check("库里没有多出题", db.query(Question).count() == before, db.query(Question).count())
    copy.unlink()


# ---------------------------------------------------------------- H 模板
def part_h(db) -> None:
    print("\n==== H 讲义模板本身不入库 ====")
    p = vault.lecture_dir() / vault.LECTURE_TEMPLATE_NAME
    vault.write_md(p, LECTURE)
    before = db.query(Question).count()
    rep = vault.scan(db)
    truthy("模板被跳过并说清原因",
           [s for s in rep["skipped"] if s["path"] == vault.rel(p)], rep["skipped"])
    check("库里没多题", db.query(Question).count() == before, db.query(Question).count())
    falsy("模板里也没被写回 shike_id", "<!-- shike_id:" in vault.read_md(p).text)
    p.unlink()


# ---------------------------------------------------------------- I 写回
def part_i(db) -> None:
    print("\n==== I 网页改题：只重写讲义里的那一段 ====")
    p = vault.lecture_dir() / "第一讲.md"
    before = vault.read_md(p).text
    q1 = next(q for q in db.query(Question).all() if "棱柱的侧面一定" in (q.content or ""))
    q1.content = "（网页上改过的题干）棱柱的侧面一定是平行四边形吗？"
    q1.answer = "是"
    db.flush()
    wrote = vault.sync_back(db, q1)
    db.commit()
    truthy("写回了", wrote)
    after = vault.read_md(p).text
    truthy("讲义里出现新题干", "（网页上改过的题干）" in after)
    truthy("讲义里出现新答案", "#### 答案\n\n是" in after.replace("\r\n", "\n"))
    truthy("知识部分还在（没被整篇重写冲掉）",
           "知识专用段落" in after and "知识专用句子" in after and "### 一、棱柱" in after)
    truthy("另一道题还在", "这个几何体有几条棱" in after)
    truthy("`## 小结` 还在", "## 小结" in after)
    check("只动了一段（行数差得不多）",
          abs(after.count("\n") - before.count("\n")) <= 2,
          (before.count("\n"), after.count("\n")))
    check("每段仍然只有一条 id 注释", after.count("<!-- shike_id:") == 2, after.count("<!-- shike_id:"))

    rep = vault.scan(db)
    lec = next(x for x in rep["lectures"] if x["path"] == vault.rel(p))
    check("写回之后同步一次 → 都算未变（否则两边来回打架）", lec["unchanged"] == 2, lec)


# ---------------------------------------------------------------- J 收件箱
def part_j(db) -> None:
    print("\n==== J 收件箱不吃讲义 ====")
    box = vault.inbox_dir()
    box.mkdir(parents=True, exist_ok=True)
    p = box / "丢进来的讲义.md"
    vault.write_md(p, LECTURE)
    before = db.query(Question).count()
    res = vault_inbox._ingest_one(db, p, vault.rel(p))
    check("报告里说「留着不用搬」", "讲义" in (res["skipped"][0]["reason"] if res["skipped"] else ""), res)
    truthy("文件还在原地（没被搬进 _已处理）", p.is_file())
    check("没入题（走的是同步那条路）", db.query(Question).count() == before,
          db.query(Question).count())
    p.unlink()


# ---------------------------------------------------------------- K 同篇撞 id
def part_k(db) -> None:
    print("\n==== K 同一篇里两段写着同一个 id ====")
    one = write_lec("撞id.md", """---
shike: lecture
---

## 题目

### 题目 1

第一段题干。

#### 答案

A

### 题目 2

第二段题干。
""")
    rep = vault.scan(db)          # 先正常导入两道，拿回 id
    lec = next(x for x in rep["lectures"] if x["path"] == vault.rel(one))
    check("先正常入 2 道", len(lec["created"]) == 2, lec["created"])
    text = vault.read_md(one).text
    ids = [x["id"] for x in lec["created"]]
    # 把第 2 段的注释改成第 1 段的（模拟「复制一段忘了删注释」）
    text = text.replace(f"<!-- shike_id: {ids[1]} -->", f"<!-- shike_id: {ids[0]} -->")
    vault.write_md(one, text)
    before = db.query(Question).count()
    rep2 = vault.scan(db)
    lec2 = next(x for x in rep2["lectures"] if x["path"] == vault.rel(one))
    truthy("报冲突", lec2["errors"], lec2)
    truthy("说的是「和另一段同一个 id」", "同一个 shike_id" in lec2["errors"][0]["error"],
           lec2["errors"][0]["error"])
    check("两道题的正文没被互相覆盖", db.query(Question).count() == before)
    db.expire_all()
    check("第一段还是第一段的题干",
          "第一段题干" in (db.get(Question, ids[0]).content or ""),
          db.get(Question, ids[0]).content)
    check("第二段还是第二段的题干",
          "第二段题干" in (db.get(Question, ids[1]).content or ""),
          db.get(Question, ids[1]).content)
    one.unlink()
    for qid in ids:
        q = db.get(Question, qid)
        if q is not None:
            db.delete(q)
    db.commit()


# ---------------------------------------------------------------- main
def main() -> int:
    print("临时目录:", TMP)
    part_a()

    print("\n--- 建库（跑迁移到最新 + 补体系骨架）---")
    info = migrate.run_migrations(verbose=False)
    print("  ", info)
    import sqlite3
    con = sqlite3.connect(str(config.DB_PATH))
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(questions)")}
    finally:
        con.close()
    check("迁移加了 note_path 列", "note_path" in cols, sorted(cols))

    vault.ensure_dirs()
    (vault.attach_dir() / "图.png").write_bytes(PNG)

    db = SessionLocal()
    try:
        seed.seed_all(db)
        db.commit()
        part_b(db)
        part_c(db)
        part_d(db)
        part_e(db)
        part_f(db)
        part_g(db)
        part_h(db)
        part_i(db)
        part_j(db)
        part_k(db)
    finally:
        db.close()

    print("\n==== 结果 ====")
    print("FAIL 数:", len(fails))
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    code = main()
    if code == 0:
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print("\n保留现场（便于查）:", TMP)
    sys.exit(code)
