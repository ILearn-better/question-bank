# -*- coding: utf-8 -*-
"""题库 md（vault）同步的验收测试。

    backend/.venv/Scripts/python.exe test_vault_md.py     # 退出码即结果

## 这一套在守什么

老师的用法是「在 Obsidian 里改题 → 点同步 → 题库跟着变」。这条链上有四个
一旦静默出错就很难发现的点，所以每组都对着一个具体后果验：

  A 解析/序列化 —— 格式认错一个字，整道题就悄悄变样（答案变题干、图变文字）
  B 路径沙箱   —— 前端/文件里来的路径能跑出 vault，就是任意文件读
  C 扫描幂等   —— 每次都「有更新」会把 updated_at 刷乱；重复入库会把题变两份
  D 图片三列   —— 题干图 / 答案图 / 原貌图混淆，出卷时印错或丢图
  E 导出回写   —— 网页改了不写回文件，下次同步就把改动盖掉
  F 冲突       —— 复制 md 不改 id，后扫到的会抢走题的归属（不报错）
  G 收件箱     —— 子目录认体系；根目录文件不猜体系
  K 模板       —— 「照这个写」的模板本身被当题入库（还会把 id 写进模板，复制全撞 id）
  L 重复文件   —— 同一道题在 vault 里变成两份（收件箱搬运 / 导出各有一条路会造），
                  之后每次同步都有一条失败，老师不知道那份副本哪来的

⚠️ 全程用临时目录（`SHIKE_DB_PATH` / `SHIKE_DATA_DIR` / `SHIKE_VAULT` 指到 TEMP），
   **必须在 import app.* 之前设置**，否则 engine 已经钉在真库上了。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="shike_vault_"))
os.environ["SHIKE_DB_PATH"] = str(TMP / "t.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_VAULT"] = str(TMP / "vault")
os.environ["SHIKE_VAULT_INBOX"] = "0"          # 别让后台线程在测试里跑起来
os.environ["SHIKE_AUTO_MIGRATE"] = "1"

sys.path.insert(0, str(Path(__file__).parent))

from app import config, migrate                                  # noqa: E402
from app.db import SessionLocal                                   # noqa: E402
from app.models import Curriculum, Question                       # noqa: E402
from app.services import vault, vault_inbox                       # noqa: E402

fails: list[str] = []


def check(name: str, ok, detail=None) -> bool:
    print(("[OK]   " if ok else "[FAIL] ") + name + (f"   ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        fails.append(name)
    return bool(ok)


def truthy(name: str, value, detail=None) -> bool:
    return check(name, bool(value), detail if detail is not None else value)


def falsy(name: str, value, detail=None) -> bool:
    return check(name, not value, value if detail is None else detail)


# ---------------------------------------------------------------- A 解析与序列化
MINIMAL = """---
shike: question
---

求 $x$ 的值。
"""

FULL = """---
shike: question
shike_id: abc123def456
qtype: 选择题
difficulty: 基础
score: 3
year: 2024
source: 2024 DSE Paper 1 Q7
knowledge:
  - 二次方程
  - 判别式
tags:
  - 易错
---

第一行题干 $x^2-1$
第二行

![[图A.png]]

A．1
B．2

## 答案

B

![[图B.png]]

## 解析

略。
"""


def part_a() -> None:
    print("\n==== A 解析与序列化（纯函数，不碰盘）====")

    p = vault.parse(MINIMAL)
    truthy("最小 md 能解析", p.ok)
    check("最小 md 的题干对", p.stem == "求 $x$ 的值。", p.stem)
    check("没写答案就是空", p.answer == "", p.answer)
    check("没写字段就不出现在 fields（不会覆盖库里的值）", p.fields == {}, p.fields)

    p = vault.parse(FULL)
    truthy("完整 md 能解析", p.ok)
    check("多段题干保留换行", p.stem == "第一行题干 $x^2-1$\n第二行\n\nA．1\nB．2", repr(p.stem))
    check("题干里的图片语法被摘掉", "![[" not in p.stem, p.stem)
    check("题干图进了 stem_imgs", p.stem_imgs == ["图A.png"], p.stem_imgs)
    check("答案对", p.answer == "B", p.answer)
    check("答案图进了 ans_imgs", p.ans_imgs == ["图B.png"], p.ans_imgs)
    check("解析对", p.analysis == "略。", p.analysis)
    check("分值读成数字", p.fields.get("score") == 3.0, p.fields.get("score"))
    check("年份读成整数", p.fields.get("year") == 2024, p.fields.get("year"))
    check("知识点是列表", p.fields.get("knowledge") == ["二次方程", "判别式"], p.fields.get("knowledge"))
    check("标签是列表", p.fields.get("tags") == ["易错"], p.fields.get("tags"))
    falsy("没有多余的警告", p.warnings, p.warnings)

    # 英文标题：DSE 卷是英文的，老师多半会写 Answer / Solution
    en = "---\nshike: question\n---\n\nFind $x$.\n\n## Answer\n\n3\n\n## Solution\n\n因为…\n"
    p = vault.parse(en)
    check("## Answer 认成答案", p.answer == "3", p.answer)
    check("## Solution 认成解析", p.analysis == "因为…", p.analysis)

    # 只有解析没有答案
    p = vault.parse("---\nshike: question\n---\n\n题干\n\n## 解析\n\n详解\n")
    check("只有解析时题干仍然完整", p.stem == "题干", p.stem)
    check("只有解析时答案为空", p.answer == "", p.answer)
    check("只有解析时解析有位", p.analysis == "详解", p.analysis)

    # 标题顺序反了：解析写在前、答案写在后 —— 按位置切，各归各位
    rev = "---\nshike: question\n---\n\n题干\n\n## 解析\n\n先讲道理\n\n## 答案\n\n42\n"
    p = vault.parse(rev)
    check("顺序反了：解析还是解析", p.analysis == "先讲道理", p.analysis)
    check("顺序反了：答案还是答案", p.answer == "42", p.answer)

    # 逗号写法的列表（Obsidian 属性面板里敲逗号很自然）
    p = vault.parse("---\nshike: question\nknowledge: 甲, 乙\ntags: 丙\n---\n\n题干\n")
    check("knowledge 逗号写法认成两个", p.fields.get("knowledge") == ["甲", "乙"], p.fields.get("knowledge"))
    check("tags 单个字符串认成列表", p.fields.get("tags") == ["丙"], p.fields.get("tags"))

    # 坏值不该被当成「用户想清空」
    p = vault.parse("---\nshike: question\nscore: 三分\nrender_prefer: 随便\n---\n\n题干\n")
    check("score 读不出数字 → 不进 fields", "score" not in p.fields, p.fields)
    check("render_prefer 非法 → 不进 fields", "render_prefer" not in p.fields, p.fields)
    truthy("但会给出警告", any("score" in w for w in p.warnings) and any("render_prefer" in w for w in p.warnings),
           p.warnings)

    # 不是题目 md
    falsy("没有 shike 标记的不当题目", vault.is_question_md("# 随便一篇笔记\n"))
    falsy("shike 写别的值也不行", vault.is_question_md("---\nshike: note\n---\n\nx\n"))
    truthy("shike: question 认出来", vault.is_question_md(MINIMAL))

    # YAML 坏掉
    p = vault.parse("---\nshike: question\nknowledge: [未闭合\n---\n\n题干\n")
    falsy("坏 YAML 不通过", p.ok)
    truthy("坏 YAML 有警告", bool(p.warnings), p.warnings)

    # 外链图片：本机不联网。**正文里必须摘掉**（否则那一串 URL 会印到卷子上），
    # 但也不能当它不存在 —— 扫描阶段要明确报「外链没有下载」。
    p = vault.parse("---\nshike: question\n---\n\n见图 ![](https://example.com/a.png)\n")
    check("外链图片也从正文里摘掉", p.stem_imgs == ["https://example.com/a.png"], p.stem_imgs)
    falsy("题干文字里不残留外链", "example.com" in p.stem, p.stem)
    loc, why = vault._locate("https://example.com/a.png", TMP, {})
    check("扫描阶段拒绝外链并说明原因", loc is None and "外链" in why, why)

    # 用例：round-trip
    q = Question(id="rt0000000001", content="题干 $a+b$\n第二行", answer="B\n理由",
                 analysis="详解", qtype="选择题", difficulty="基础", score=2.0, year=2023,
                 source="某卷", knowledge_points='["甲","乙"]', tags='["丙"]', render_prefer="auto")
    md = vault.render(q, fig_name="f-rt0000000001-fig.png", ans_name="f-rt0000000001-ans.png")
    p = vault.parse(md)
    check("round-trip：题干", p.stem == q.content.strip(), p.stem)
    check("round-trip：答案", p.answer == q.answer.strip(), p.answer)
    check("round-trip：解析", p.analysis == q.analysis.strip(), p.analysis)
    check("round-trip：id", p.qid == q.id, p.qid)
    check("round-trip：分值", p.fields.get("score") == 2.0, p.fields.get("score"))
    check("round-trip：知识点", p.fields.get("knowledge") == ["甲", "乙"], p.fields.get("knowledge"))
    check("round-trip：题干图", p.stem_imgs == ["f-rt0000000001-fig.png"], p.stem_imgs)
    check("round-trip：答案图", p.ans_imgs == ["f-rt0000000001-ans.png"], p.ans_imgs)
    check("render 两次结果一致（写回不会每次产生 diff）",
          md == vault.render(q, fig_name="f-rt0000000001-fig.png", ans_name="f-rt0000000001-ans.png"))

    # 文件名清洗
    check("公式不进文件名", "$" not in vault._safe_stem("求 $x^2$ 的值", "abcdef123456"))
    check("危险字符被换掉", "/" not in vault._safe_stem("a/b:c*d", "abcdef123456"))
    check("空标题退回 id", vault._safe_stem("", "abcdef123456") == "题-abcdef")
    check("Windows 保留名被顶开", not vault._safe_stem("CON", "abcdef123456").upper().startswith("CON"))
    check("超长被截断", len(vault._safe_stem("长" * 200, "abcdef123456")) <= 40)

    # 换行规范化
    check("CRLF 与 LF 算同一个指纹",
          vault._digest("---\r\nshike: question\r\n---\r\n") == vault._digest("---\nshike: question\n---\n"))


# ---------------------------------------------------------------- B 路径沙箱
def part_b() -> None:
    print("\n==== B 路径沙箱 ====")
    vault.ensure_dirs()
    for bad in ("../x.md", "../../etc/passwd", "/etc/passwd", "C:/Windows/x.md", "c:\\x.md", ""):
        try:
            vault.resolve(bad)
            check(f"拒绝 {bad or '(空)'}", False, "没抛异常")
        except ValueError:
            check(f"拒绝 {bad or '(空)'}", True)
    ok_path = vault.resolve("题库/甲.md")
    truthy("正常相对路径能解析", str(ok_path).replace("\\", "/").endswith("vault/题库/甲.md"), ok_path)


# ---------------------------------------------------------------- 铺测试数据
SAMPLE_DOC = """---
shike: question
shike_id: {qid}
qtype: 选择题
difficulty: 基础
score: 3
knowledge:
  - 棱柱的结构特征
tags:
  - 三棱柱
---

元旦，小颖想给妈妈一个惊喜，她亲手制作了一个如图所示的三棱柱形礼盒．以下关于三棱柱的说法，不正确的是（　　）

![[{img}]]

A．它有 $9$ 条棱
B．它有 $6$ 个面
C．它的所有侧棱长都相等
D．它有 $6$ 个顶点

## 答案

B

## 解析

解：三棱柱有 $9$ 条棱……
"""

ANOTHER = """---
shike: question
qtype: 填空题
---

第二道题的题干。

## 答案

42
"""


def _make_png(path: Path, seed: bytes = b"a") -> None:
    """造一张真的 PNG（1x1，纯色）。走魔数校验，纯字节写文件会被拒。"""
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + zlib.crc32(tag + data).to_bytes(4, "big"))

    raw = b"\x00" + seed * 3
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", (1).to_bytes(4, "big") * 2 + bytes([8, 2, 0, 0, 0]))
           + chunk(b"IDAT", zlib.compress(raw))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def seed_vault() -> None:
    vault.ensure_dirs()
    (vault.shelf_dir() / "初一几何").mkdir(parents=True, exist_ok=True)
    _make_png(vault.attach_dir() / "三棱柱礼盒.png", b"\x10")
    (vault.shelf_dir() / "初一几何" / "三棱柱形礼盒.md").write_text(
        SAMPLE_DOC.format(qid="", img="三棱柱礼盒.png"), encoding="utf-8")
    (vault.shelf_dir() / "另一道.md").write_text(ANOTHER, encoding="utf-8")
    # 一篇普通笔记：绝不能被当题目
    (vault.shelf_dir() / "随笔.md").write_text("# 随手记\n\n这不是题。\n", encoding="utf-8")


# ---------------------------------------------------------------- C 扫描与幂等
def part_c(db) -> Question:
    print("\n==== C 扫描 / 幂等 / 认回原题 ====")
    r1 = vault.scan(db)
    check("第一次扫描新建 2 道", len(r1["created"]) == 2, r1["created"])
    falsy("第一次扫描没有报错", r1["errors"], r1["errors"])
    check("非题目 md 被跳过", len(r1["skipped"]) == 1, r1["skipped"])

    rows = db.query(Question).all()
    check("库里正好 2 道", len(rows) == 2, len(rows))
    tri = next((q for q in rows if "元旦" in (q.content or "")), None)
    truthy("找到带图那道", tri is not None)
    if tri is None:
        return None

    check("题干对", "三棱柱形礼盒" in (tri.content or ""))
    check("答案对", tri.answer == "B", tri.answer)
    check("解析对", "三棱柱有 $9$ 条棱" in (tri.analysis or ""))
    check("分值对", tri.score == 3.0, tri.score)
    check("题型对", tri.qtype == "选择题", tri.qtype)
    check("知识点对", "棱柱的结构特征" in (tri.knowledge_points or ""), tri.knowledge_points)
    check("标签对", "三棱柱" in (tri.tags or ""), tri.tags)
    falsy("题干里没有残留图片语法", "![[" in (tri.content or ""), tri.content)
    check("配图进了 figure_image", (tri.figure_image or "").startswith("/api/crops/"), tri.figure_image)
    truthy("配图文件真的落盘了",
           (config.CROPS_DIR / Path(tri.figure_image or "x").name).is_file())
    check("记下了来源 md", (tri.md_path or "").endswith("三棱柱形礼盒.md"), tri.md_path)

    p = vault.shelf_dir() / "初一几何" / "三棱柱形礼盒.md"
    truthy("shike_id 已写回文件", tri.id in p.read_text(encoding="utf-8"))

    r2 = vault.scan(db)
    falsy("第二次扫描没有新建", r2["created"], r2["created"])
    falsy("第二次扫描没有更新", r2["updated"], r2["updated"])
    check("第二次扫描 unchanged=2", r2["unchanged"] == 2, r2["unchanged"])

    # 改答案 → 只这一道更新
    p.write_text(p.read_text(encoding="utf-8").replace("## 答案\n\nB", "## 答案\n\nC"), encoding="utf-8")
    r3 = vault.scan(db)
    check("改答案后只更新 1 道", len(r3["updated"]) == 1, r3["updated"])
    db.refresh(tri)
    check("库里的答案跟着变", tri.answer == "C", tri.answer)

    # frontmatter 没写的字段不能被抹掉（只补答案不该动分值）
    p.write_text(p.read_text(encoding="utf-8").replace("score: 3\n", ""), encoding="utf-8")
    vault.scan(db)
    db.refresh(tri)
    check("md 里删掉 score 不会清空库里的分值", tri.score == 3.0, tri.score)

    # 文件挪走：认回同一道题，不新建
    moved = vault.shelf_dir() / "挪走的题.md"
    p.rename(moved)
    before = db.query(Question).count()
    r4 = vault.scan(db)
    falsy("挪文件不新建题", r4["created"], r4["created"])
    check("总数不变", db.query(Question).count() == before, db.query(Question).count())
    db.refresh(tri)
    check("md_path 跟着更新", tri.md_path == vault.rel(moved), tri.md_path)

    # 文件不见了：报出来，但绝不自动删题
    moved.unlink()
    r5 = vault.scan(db)
    truthy("文件丢了会被报出来", any(m["id"] == tri.id for m in r5["missing"]), r5["missing"])
    truthy("但没有自动删题", db.get(Question, tri.id) is not None)
    # 复原，后面几组还要用
    moved.write_text(
        SAMPLE_DOC.format(qid=tri.id, img="三棱柱礼盒.png"), encoding="utf-8")
    vault.scan(db)
    db.refresh(tri)
    return tri


# ---------------------------------------------------------------- D 图片三列
def part_d(db, tri: Question) -> None:
    print("\n==== D 图片按位置分流 ====")
    path = vault.resolve(tri.md_path)

    # 答案里加一张图 → answer_image，figure_image 不动
    _make_png(vault.attach_dir() / "答案图.png", b"\x20")
    txt = path.read_text(encoding="utf-8").replace("## 答案\n\nB", "## 答案\n\nB\n\n![[答案图.png]]")
    truthy("测试数据里找到了答案段", "答案图" in txt, txt[-200:])
    path.write_text(txt, encoding="utf-8")
    vault.scan(db)
    db.refresh(tri)
    truthy("答案图进了 answer_image", (tri.answer_image or "").startswith("/api/crops/"), tri.answer_image)
    truthy("题干图还在", (tri.figure_image or "").startswith("/api/crops/"), tri.figure_image)

    # 删掉题干图 → 库里也清空（正文是真源）
    old_fig = tri.figure_image
    txt = path.read_text(encoding="utf-8").replace("![[三棱柱礼盒.png]]\n\n", "")
    path.write_text(txt, encoding="utf-8")
    vault.scan(db)
    db.refresh(tri)
    check("题干配图被清空", tri.figure_image == "", tri.figure_image)
    truthy("答案图不受影响", (tri.answer_image or "").startswith("/api/crops/"), tri.answer_image)

    # 引用写了但文件找不到 → **保留库里原来那张**，不能当成「用户想删图」。
    # （先把图放回去再试：不然上一步刚清空，这条会变成恒真的假绿。）
    txt = path.read_text(encoding="utf-8").replace(
        "A．它有 $9$ 条棱", "![[三棱柱礼盒.png]]\n\nA．它有 $9$ 条棱")
    path.write_text(txt, encoding="utf-8")
    vault.scan(db)
    db.refresh(tri)
    truthy("先把题干图放回来", (tri.figure_image or "").startswith("/api/crops/"), tri.figure_image)
    keep = tri.figure_image
    txt = path.read_text(encoding="utf-8").replace(
        "A．它有 $9$ 条棱", "![[不存在的图.png]]\n\nA．它有 $9$ 条棱")
    path.write_text(txt, encoding="utf-8")
    r = vault.scan(db)
    db.refresh(tri)
    check("图找不到时保留原有配图（不静默丢图）", tri.figure_image == keep,
          f"原来 {keep}，现在 {tri.figure_image}")
    truthy("并且报告里说了找不到",
           any("找不到图片" in m for w in r["warnings"] for m in w["messages"]), r["warnings"])

    # 多张题干图：只取第一张 + 报告
    _make_png(vault.attach_dir() / "图一.png", b"\x30")
    _make_png(vault.attach_dir() / "图二.png", b"\x40")
    base = path.read_text(encoding="utf-8").replace("![[不存在的图.png]]\n\n", "")
    path.write_text(base.replace("A．它有 $9$ 条棱", "![[图一.png]]\n\n![[图二.png]]\n\nA．它有 $9$ 条棱"),
                    encoding="utf-8")
    r = vault.scan(db)
    db.refresh(tri)
    truthy("多图时用了第一张", (tri.figure_image or "").startswith("/api/crops/"), tri.figure_image)
    # 报告里要能看到「哪张被丢下了」—— 只报个数等于没说
    truthy("多出来的那张写进了报告",
           any("图二.png" in x["images"] for x in r["extra_images"]), r["extra_images"])


# ---------------------------------------------------------------- E 导出与回写
def part_e(db, tri: Question) -> None:
    print("\n==== E 导出 / 网页改题回写 ====")
    out = vault.export_question(db, tri)
    db.commit()
    md = out.read_text(encoding="utf-8")
    truthy("导出文件在题库目录下", str(out).replace("\\", "/").find("/vault/题库/") >= 0, out)
    truthy("导出内容带 shike 标记", "shike: question" in md)
    truthy("导出内容带答案", "## 答案" in md)
    truthy("导出内容带题干", "元旦" in md)
    truthy("导出把图写成标准 markdown 图片", "![](<../附件/" in md, md[-300:])
    truthy("附件名认得出是谁的", f"-{tri.id}-fig." in md or "-fig." in md, md[:400])
    truthy("附件文件在附件目录里", any(vault.attach_dir().glob(f"*-{tri.id}-fig.*")))

    # 「导出成 md 看不到图」就出在这里：以前写的是 `![[图.png]]`，只有 Obsidian 认，
    # Typora / VSCode / 资源管理器预览里全是空链接。标准写法还要能**按路径找到那张图**。
    refs = vault.parse(md).stem_imgs
    check("导出的 md 里有一条图片引用", len(refs) == 1, refs)
    if refs:
        hit, why = vault._locate(refs[0], out.parent, vault._index_images())
        truthy("这条引用能解析到附件里的那个文件",
               hit is not None and hit.parent == vault.attach_dir(), (refs[0], why, hit))
        falsy("图片引用里没有会截断链接的括号",
              any(c in Path(refs[0].strip("<>")).name for c in "()[]{}#%&"), refs[0])

    r = vault.scan(db)
    falsy("导出后再扫一遍不会当成改动", r["updated"], r["updated"])
    truthy("至少一道是 unchanged", r["unchanged"] >= 1, r["unchanged"])

    # 网页改题 → 写回文件
    old = out.read_text(encoding="utf-8")
    tri.answer = "D"
    tri.difficulty = "较难"
    truthy("sync_back 说它写了", vault.sync_back(db, tri))
    db.commit()
    new = out.read_text(encoding="utf-8")
    truthy("文件真的变了", new != old)
    truthy("文件里有新答案", "## 答案\n\nD" in new, new[-200:])
    truthy("文件里有新难度", "difficulty: 较难" in new, new[:300])

    # 没有 md 来源的题：sync_back 什么都不该做（网页录的题不主动往 vault 里塞文件）
    bare = Question(id="bare00000001", owner_id=1, content="网页上录的题", tags="[]",
                    knowledge_points="[]")
    db.add(bare)
    db.commit()
    falsy("不来自 md 的题不会被擅自写成文件", vault.sync_back(db, bare))


# ---------------------------------------------------------------- F 冲突
def part_f(db) -> None:
    print("\n==== F 复制文件 / 同 id 冲突 ====")
    srcs = [q for q in db.query(Question).filter(Question.md_path.isnot(None)).all()]
    truthy("有可用的来源题", bool(srcs))
    if not srcs:
        return
    q = srcs[0]
    src = vault.resolve(q.md_path)
    dup = src.with_name("副本-" + src.name)
    dup.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    r = vault.scan(db)
    # 措辞有两路（「同一个 id 被两个文件声明」「这个路径已被另一道题占了」），
    # 这里要的是「冲突被拦下来了」，不是某一句特定的文案。
    truthy("复制 md 时被拦下来并说明了原因",
           any("shike_id" in e["error"] for e in r["errors"]), r["errors"])
    dup.unlink()

    # 一个路径塞两道题（手工把 shike_id 换了但路径没动）—— 不可能，因为 md_path 唯一。
    # 这里验的是「数据库唯一索引真的在」：两行同 md_path 应当写不进去。
    from itertools import count
    other = db.query(Question).filter(Question.md_path.is_(None)).first()
    if other is not None:
        other.md_path = q.md_path
        try:
            db.flush()
            check("同 md_path 的第二行写不进去", False, "居然成功了")
        except Exception:
            check("同 md_path 的第二行写不进去", True)
        finally:
            db.rollback()


# ---------------------------------------------------------------- G 收件箱
def part_g(db) -> None:
    print("\n==== G 收件箱：认体系 / 不猜 ====")
    vault.ensure_dirs()
    curr = db.query(Curriculum).first()
    truthy("库里有体系", curr is not None)
    if curr is None:
        return
    code = curr.code or curr.name

    # 根目录下的文件：不处理，给出提示
    (vault.inbox_dir() / "随便一份.pdf").write_bytes(b"%PDF-1.4\n")
    r = vault_inbox.scan_once(db, enqueue=False)
    truthy("根目录文件被忽略",
           any(i["file"].endswith("随便一份.pdf") for i in r["ignored"]), r["ignored"])
    truthy("忽略理由里提到了子文件夹",
           any("子文件夹" in i["reason"] for i in r["ignored"]), r["ignored"])

    # 子目录名对不上体系
    (vault.inbox_dir() / "不存在的体系").mkdir(parents=True, exist_ok=True)
    (vault.inbox_dir() / "不存在的体系" / "卷子.pdf").write_bytes(b"%PDF-1.4\n")
    r = vault_inbox.scan_once(db, enqueue=False)
    truthy("认不出的子目录被忽略",
           any("不存在的体系" in i["file"] for i in r["ignored"]), r["ignored"])

    # 待处理清单只列真文件、不列归档目录里的
    (vault.inbox_dir() / vault_inbox.DONE_DIR).mkdir(parents=True, exist_ok=True)
    (vault.inbox_dir() / vault_inbox.DONE_DIR / "旧的.pdf").write_bytes(b"%PDF-1.4\n")
    names = [p.name for p in vault_inbox.pending_files()]
    falsy("已处理目录里的不算待处理", "旧的.pdf" in names, names)
    truthy("待处理里包含未处理的 pdf", "随便一份.pdf" in names, names)

    check("体系子目录名能被认出来", vault_inbox._curriculum(db, code) is not None)
    check("体系 code 不分大小写", vault_inbox._curriculum(db, code.upper()) is not None)
    check("乱写的名字认不出", vault_inbox._curriculum(db, "绝对不存在的体系xyz") is None)


# ---------------------------------------------------------------- I 插件转出来的 md
# 老师定的转换方式是「在 Obsidian 里用插件把 PDF 变成 md」。插件产物没有 frontmatter、
# 图片写的是 `![](media/image1.png)` 这种相对路径，还常常把整份卷子放一个文件。
# 这一组守的就是：这种文件丢进收件箱，能不能变成「一题一个标准模板」。
PLUGIN_DOC = """2024 年卷一

1. 如图，三棱柱的棱数是（ ）

![](media/image1.png)

A．6　B．9　C．12　D．15

---

2. 化简 $\\frac{x^2-1}{x-1}$。

![](<我的 图二.png>)

## 答案

$x+1$
"""


def part_i(db) -> None:
    print("\n==== I 插件转出来的 md（宽松摄取）====")

    # ---- 纯函数：路径清洗（这些壳都是真实转换器会写出来的）
    check("Pandoc 的 media/ 路径原样保留", vault._clean_ref("media/image1.png") == "media/image1.png")
    check("尖括号包住的空格路径剥掉尖括号", vault._clean_ref("<我的 图二.png>") == "我的 图二.png")
    check("URL 编码解开", vault._clean_ref("a%20b.png") == "a b.png")
    check("锚点不算路径", vault._clean_ref("x.png#center") == "x.png")
    check("Markdown 的标题部分剥掉", vault._clean_ref('y.png "图 1"') == "y.png")
    check("反斜杠转正斜杠", vault._clean_ref("att\\b.png") == "att/b.png")
    check("外链留给 _locate 去拒绝", vault._clean_ref("https://a.com/x.png").startswith("http"))

    check("整卷按 --- 切成一题一段", len(vault.split_units(PLUGIN_DOC)) == 2,
          len(vault.split_units(PLUGIN_DOC)))
    check("一道题里的装饰线不切",
          len(vault.split_units("求 $x$。\n\n---\n\n## 答案\n\nB")) == 1)
    check("题号数得出来（1.5 米不算题号）",
          vault.count_questions("1. 甲\n2. 乙\n3.2 米") == 2,
          vault.count_questions("1. 甲\n2. 乙\n3.2 米"))

    # 转换器会自作主张改图片大小写（`IMG_0012.PNG` → 引用里写 `img_0012.png`），
    # Windows 上那本来就是同一个文件 —— 精确匹配会让图「凭空找不到」。
    _make_png(vault.attach_dir() / "CASE.PNG", b"\x70")
    hit, why = vault._locate("case.png", vault.inbox_dir(), vault._index_images())
    truthy("图片名大小写不同也能找到", hit is not None, why)

    # ---- 真的走一遍收件箱
    # G 组留下的两个假 PDF 先清掉：接着跑会真去开识别任务（要 AI 配置）
    for name in ("随便一份.pdf", "不存在的体系/卷子.pdf"):
        (vault.inbox_dir() / name).unlink(missing_ok=True)

    box = vault.inbox_dir()
    (box / "media").mkdir(parents=True, exist_ok=True)
    _make_png(box / "media" / "image1.png", b"\x50")
    _make_png(box / "我的 图二.png", b"\x60")
    src = box / "2024 年卷一.md"
    src.write_text(PLUGIN_DOC, encoding="utf-8")

    before = db.query(Question).count()
    r = vault_inbox.scan_once(db)
    ing = [x for x in r["ingested"] if x["file"].endswith("2024 年卷一.md")]
    truthy("收件箱认出了这份 md", ing, r.get("ingested"))
    if not ing:
        return
    one = ing[0]
    check("切成两段就入了两道题", len(one["created"]) == 2, one["created"])
    check("题库题数 +2", db.query(Question).count() == before + 2, db.query(Question).count())

    moved = one.get("moved_to") or ""
    truthy("源文件挪进了 _已处理/", vault_inbox.DONE_DIR in moved, moved)
    falsy("源文件不在收件箱根下了", src.is_file(), moved)

    paths = [c.get("md") or "" for c in one["created"]]
    truthy("两道题都在 题库/ 下写了标准模板",
           all(p.startswith(vault.SHELF + "/") and p.endswith(".md") for p in paths), paths)
    body1 = (vault.root() / paths[0]).read_text(encoding="utf-8") if paths and paths[0] else ""
    truthy("模板里有 shike: question 标记", vault.is_question_md(body1), body1[:120])
    truthy("模板里写回了 shike_id", "shike_id:" in body1, body1[:200])
    falsy("图片引用没原样留在正文里", "![](media/" in body1, body1[-200:])
    truthy("图片变成了标准 markdown 引用（带相对路径）", "![](<../附件/" in body1, body1[-200:])
    refs1 = vault.parse(body1).stem_imgs
    if refs1:
        hit, why = vault._locate(refs1[0], vault.root() / Path(paths[0]).parent,
                                 vault._index_images())
        truthy("这条引用真的指到附件里的文件",
               hit is not None and hit.is_file(), (refs1[0], why))

    qs = [db.get(Question, c["id"]) for c in one["created"] if c.get("id")]
    truthy("两道题都进库了", all(q is not None for q in qs) and len(qs) == 2, len(qs))
    if len(qs) == 2:
        truthy("第一道题的配图导进来了",
               (qs[0].figure_image or "").startswith("/api/crops/"), qs[0].figure_image)
        check("第二道题的答案搬对了", (qs[1].answer or "").strip() == "$x+1$", qs[1].answer)
        falsy("题干里没有残留的图片语法", "![" in (qs[0].content or ""), qs[0].content[:200])

    # 再跑一轮：源文件已归档，不许重复入库
    n_after = db.query(Question).count()
    r2 = vault_inbox.scan_once(db)
    check("再跑一轮不重复入库", db.query(Question).count() == n_after, db.query(Question).count())
    falsy("也不会再报一次这份 md",
           any((x.get("file") or "").endswith("2024 年卷一.md") for x in r2["ingested"]), r2["ingested"])

    # ---- 整卷但没有分隔线：不入库，把话说明白
    n = db.query(Question).count()
    (box / "没分隔线.md").write_text("1. 甲题\n\n2. 乙题\n\n3. 丙题\n", encoding="utf-8")
    r3 = vault_inbox.scan_once(db)
    bad = [x for x in r3["ingested"] if x["file"].endswith("没分隔线.md")]
    # ⚠️ 这里不能只断言「文件出现在报告里」—— 那在「照常入库」时同样成立（假绿）。
    #    要断言的是**被拦下了**：标了 needs_split，而且一道都没入库。
    truthy("整卷没分隔线时不入库、标 needs_split",
           bool(bad) and bad[0].get("needs_split") is True and not bad[0].get("created"),
           bad[0] if bad else r3["ingested"])
    if bad:
        truthy("报告里标了 needs_split", bad[0].get("needs_split"), bad[0])
        check("一道都没入库", db.query(Question).count() == n, db.query(Question).count())
        truthy("源文件挪进了 _失败/（改好再拖回来）",
               vault_inbox.FAIL_DIR in (bad[0].get("moved_to") or ""), bad[0].get("moved_to"))
        truthy("拦下时把原因写清楚了",
               any("分隔线" in s.get("reason", "") for s in bad[0].get("skipped", [])),
               bad[0].get("skipped"))

    # ---- 只有一道题（一处题号）的 md：照常入库
    (box / "单题.md").write_text("1. 求 $2+3$ 的值。\n", encoding="utf-8")
    r4 = vault_inbox.scan_once(db)
    single = [x for x in r4["ingested"] if x["file"].endswith("单题.md")]
    truthy("单题也处理了", single, r4["ingested"])
    if single:
        check("单题入了一道", len(single[0]["created"]) == 1, single[0]["created"])

    # ---- 已经是标准模板的 md 丢进收件箱：只挪到 题库/，**不入库**
    #      否则「同步」和「摄取」会各入一遍 → 一道题变两道
    n = db.query(Question).count()
    (box / "手工模板.md").write_text(
        "---\nshike: question\n---\n\n手工写的一道题。\n", encoding="utf-8")
    r5 = vault_inbox.scan_once(db)
    hand = [x for x in r5["ingested"] if x["file"].endswith("手工模板.md")]
    truthy("标准模板被收件箱认出来了", hand, r5["ingested"])
    if hand:
        falsy("标准模板没被摄取入库", hand[0].get("created"), hand[0].get("created"))
        truthy("标准模板挪进了 题库/",
               (hand[0].get("moved_to") or "").startswith(vault.SHELF + "/"), hand[0].get("moved_to"))
    check("摄取这一步没让题数变多", db.query(Question).count() == n, db.query(Question).count())
    # 接着同步：入库一次，且来源是 题库/ 下的那份
    vault.scan(db)
    moved_q = db.query(Question).filter(Question.content.like("手工写的一道题%")).first()
    truthy("同步后入库了这道手工模板", moved_q is not None)
    if moved_q:
        truthy("它挂的来源是 题库/ 下那份",
               (moved_q.md_path or "").startswith(vault.SHELF + "/"), moved_q.md_path)
    check("同步也只入了一道（不是两道）",
          db.query(Question).filter(Question.content.like("手工写的一道题%")).count() == 1,
          db.query(Question).filter(Question.content.like("手工写的一道题%")).count())


# ---------------------------------------------------------------- J 知识点挂树
def part_j(db) -> None:
    """从 md 入库的题，知识点必须挂到知识树节点上。

    漏掉这步**不报错**：题照常入库、`knowledge_points` 里也有字，只是 `node_id` 空着 ——
    按知识点筛题筛不到它，掌握度统计也不算它。老师会以为「我明明写了知识点啊」。
    """
    print("\n==== J 知识点要挂到知识树上 ====")
    import json as _json

    from sqlalchemy import select

    from app.models import Node, QuestionNode
    from app.services import knowledge as kx

    nodes = db.scalars(select(Node).where(Node.level == 2).order_by(Node.id)).all()
    truthy("测试库里得有知识树节点（靠 seed_all 建）", nodes, len(nodes))
    if not nodes:
        return
    first, second = nodes[0], nodes[-1]

    path = vault.shelf_dir() / "知识点题.md"

    def write_q(kp_name: str) -> None:
        path.write_text(
            "---\nshike: question\nqtype: 填空题\ndifficulty: 基础\nscore: 3\n"
            f"knowledge:\n  - {kp_name}\n---\n\n题干。\n\n## 答案\n\n42\n",
            encoding="utf-8")

    write_q(first.name)
    rep = vault.scan(db)
    created = [x for x in rep["created"] if str(x["path"]).endswith("知识点题.md")]
    truthy("这道题入库了", created, rep["created"])
    if not created:
        return
    q = db.get(Question, created[0]["id"])

    check("node_id 挂到了节点上", q.node_id == first.id, (q.node_id, first.id))
    want = kx.node_path(db, first.id)
    check("knowledge_points 换成了节点的全路径",
          _json.loads(q.knowledge_points or "[]") == [want],
          (_json.loads(q.knowledge_points or "[]"), want))
    check("体系由命中的节点带出来",
          q.curriculum_id == first.curriculum_id, (q.curriculum_id, first.curriculum_id))
    check("question_nodes 里有一条挂载记录",
          db.query(QuestionNode).filter_by(question_id=q.id).count() == 1,
          db.query(QuestionNode).filter_by(question_id=q.id).count())

    # 改成另一个知识点：节点要跟着换，旧记录**必须拆掉**（否则一题挂两个知识点，
    # 统计时同一道题被算两次）
    write_q(second.name)
    vault.scan(db)
    db.expire_all()
    q = db.get(Question, created[0]["id"])
    check("换了知识点后 node_id 跟着换", q.node_id == second.id, (q.node_id, second.id))
    check("旧的挂载记录被拆掉了（只剩一条）",
          db.query(QuestionNode).filter_by(question_id=q.id).count() == 1,
          db.query(QuestionNode).filter_by(question_id=q.id).count())


# ---------------------------------------------------------------- main
def part_k(db) -> None:
    """「照这个写」的模板本身**不能入库**。

    模板必须带 `shike: question`（不然它不是题目 md、没法当格式说明），
    于是扫描会把它当成一道题：一道题干是「（题干写在这里）」、知识点「待填知识点」
    的废题就进了题库 —— 而且入库时还会把 `shike_id` 写进模板，之后老师照着它
    复制出来的每一份都带着这个 id，同步时报「这个 id 已经对应另一个文件」。
    整条链路里不会有任何报错，只有题库里慢慢多出来的废题。
    """
    print("\n==== K 模板本身不入库 ====")
    shelf = vault.shelf_dir()

    tpl = shelf / vault.TEMPLATE_NAME
    body = ("---\nshike: question\nshike_id: （留空，第一次同步时自动填）\n"
            "qtype: 选择题\ndifficulty: 基础\nscore: 3\nknowledge:\n  - 待填知识点\n"
            "---\n\n（题干写在这里）\n\nA．选项一\n\n## 答案\n\nA\n")
    tpl.write_text(body, encoding="utf-8")
    # 另存一份（老师会另存成别的名，也还是模板）
    copy_tpl = shelf / "题目模板.md"
    copy_tpl.write_text(body, encoding="utf-8")
    # 一条真题，用来证明「跳过模板」没有顺手把正常题也跳了
    real = shelf / "导数真题.md"
    real.write_text("---\nshike: question\nqtype: 选择题\n---\n\n真题干。\n\n## 答案\n\nB\n",
                    encoding="utf-8")

    rep = vault.scan(db)
    assert not rep["errors"], rep["errors"]
    made = [x["path"] for x in rep["created"]]
    check("真题照常入库", any(str(p).endswith("导数真题.md") for p in made), made)
    check("模板本身没被入库", not any("题目模板" in str(p) for p in made), made)

    skipped = {s["path"]: s["reason"] for s in rep["skipped"] if "题目模板" in s["path"]}
    check("模板进了 skipped 且说明原因",
          len(skipped) == 2 and all("模板" in r for r in skipped.values()), skipped)

    # 关键：模板文件不能被写回 shike_id（那是「复制后全部撞 id」的根源）
    check("模板文件一个字都没被改", tpl.read_text(encoding="utf-8") == body,
          tpl.read_text(encoding="utf-8")[:80])
    check("另存的那份模板也没被改", copy_tpl.read_text(encoding="utf-8") == body)

    n = db.query(Question).filter(Question.md_path.like("%题目模板%")).count()
    check("库里没有任何题的来源是模板文件", n == 0, n)


# ---------------------------------------------------------------- L 一道题两份文件
def part_l(db) -> None:
    """一道题在 vault 里只许有一份文件。

    文件名不是身份，`shike_id` 才是。有两条路会造出「同一道题两份文件」：
      · 收件箱搬运（`_file_to_shelf`）：重名就加 `-2`，搬进去的副本带着同一个 id；
      · 导出（`export_question`）：`md_path` 空 + 同名文件已存在，也加 `-2`。
    后果完全一样：之后**每次同步都有一条失败**（「这个 id 已经对应另一个文件」），
    而老师不知道那份多出来的是几时从哪来的。所以三条都要守住。
    """
    print("\n==== L 同一道题不许有两份文件 ====")
    import uuid
    from datetime import datetime

    shelf = vault.shelf_dir()

    # ---- ① 导出两次，必须落在同一份文件上
    q = Question(id="lt" + uuid.uuid4().hex[:8], owner_id=config.OWNER_ID,
                 created_at=datetime.now().isoformat(timespec="seconds"),
                 tags="[]", knowledge_points="[]", content="L组导出复用测试题干，别和别的题撞名。",
                 answer="A", analysis="", qtype="选择题")
    db.add(q)
    db.flush()
    first = vault.export_question(db, q)
    q.md_path = None                       # ingest_md 就是这么干的：清掉再导出
    db.flush()
    second = vault.export_question(db, q)
    check("同一道题导出两次落在同一个文件上", first == second, (first.name, second.name))
    falsy("没有造出 -2 副本", (first.parent / f"{first.stem}-2{first.suffix}").exists(),
          sorted(p.name for p in shelf.rglob("*-2.md")))

    # ---- ② 收件箱里来了一份题库已有的题 → 不搬，别造第二份
    src = db.query(Question).filter(Question.md_path.isnot(None),
                                    Question.id != q.id).first()
    truthy("有一道现成的来源题", src is not None)
    if src is None:
        return
    src_path = vault.resolve(src.md_path)
    inbox = vault.inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    dup = inbox / src_path.name
    dup.write_bytes(src_path.read_bytes())
    res = vault_inbox._file_to_shelf(dup, f"{vault.INBOX}/{dup.name}")
    same_id = [p for p in vault.shelf_dir().rglob("*.md")
               if p.is_file() and vault.qid_of(p) == src.id]
    check("题库里带着这个 id 的文件还是只有一份", len(same_id) == 1,
          [vault.rel(p) for p in same_id])
    truthy("结果里说清了「是副本」", "副本" in (res.get("note") or ""), res)
    falsy("收件箱里那份没有留在原地", dup.exists(), vault.rel(dup))

    # ---- ③ 同步撞上两份同 id 文件：内容一样 vs 已经不一样，提示必须分清
    twin = shelf / "L组副本.md"
    twin.write_bytes(src_path.read_bytes())
    rep = vault.scan(db)
    errs = [e for e in rep["errors"] if e["path"].endswith("L组副本.md")]
    truthy("两份同 id 文件被拦下（不静默挑一份）", bool(errs), rep["errors"])
    truthy("内容相同时说「删掉这一份就行」",
           bool(errs) and "完全相同" in errs[0]["error"], errs)
    twin.write_text(twin.read_text(encoding="utf-8") + "\n\n（改过一行，内容已经和正本不同）\n",
                    encoding="utf-8")
    rep = vault.scan(db)
    errs = [e for e in rep["errors"] if e["path"].endswith("L组副本.md")]
    truthy("内容不同时说「先决定留哪一份」",
           bool(errs) and "不一样" in errs[0]["error"], errs)
    twin.unlink()


def main() -> int:
    print("临时目录:", TMP)
    part_a()
    part_b()

    print("\n--- 建库（跑迁移到 0040 + 补体系骨架）---")
    info = migrate.run_migrations(verbose=False)
    print("  ", info)
    check("迁移到 0040",
          (migrate.current_revision() or "").startswith("0040"), migrate.current_revision())
    # 体系由 seed 补（迁移只建表）。收件箱那组要靠子目录名认体系，没有体系测不了。
    from app import seed
    db0 = SessionLocal()
    try:
        seed.seed_all(db0)
    finally:
        db0.close()

    seed_vault()
    db = SessionLocal()
    try:
        tri = part_c(db)
        if tri is not None:
            part_d(db, tri)
            part_e(db, tri)
            part_f(db)
        part_g(db)
        part_i(db)
        part_j(db)
        part_k(db)
        part_l(db)
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
