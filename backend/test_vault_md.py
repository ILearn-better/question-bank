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
    truthy("导出把图写成了 Obsidian 嵌入", "![[" in md, md[:400])
    truthy("附件名认得出是谁的", f"-{tri.id}-fig." in md or "-fig." in md, md[:400])
    truthy("附件文件在附件目录里", any(vault.attach_dir().glob(f"*-{tri.id}-fig.*")))

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


# ---------------------------------------------------------------- H 转换产物（待审稿）
def part_h(db) -> None:
    """识别出来的题怎么变成 md：没把握的先放 `题库/_待审/`，人看过才入库。

    这一组守的是「PDF/Word → 题目 md」这条链的三个关键判据：
      ① 待审稿**不写 shike_id**（这题还没入库，写个不存在的 id 只会在同步时报白）
      ② `_待审/` 里的文件**不参与扫描**（扫进来就失去了「人看过才入库」的意义）
      ③ 拖进 `题库/` 之后再扫就入库，内容跟稿子一致
    外加一条最容易漂的：`build_question`（待审稿用）与 `approve_item`（正式入库用）
    必须写出**同一道题** —— 两条出口各写一份字段映射，迟早分叉，而分叉不报错。
    """
    import json as _json

    from app.models import AutoTask, BatchItem, BatchJob, Question
    from app.services import batch_import

    print("\n==== H 转换产物：待审稿 → 人看过 → 入库 ====")
    curr = db.query(Curriculum).first()
    truthy("库里有体系（后面造条目不挂体系不行）", curr is not None)
    if curr is None:
        return

    # ---- ① render 的待审稿语义
    q = Question(
        id="pend00000001", owner_id=config.OWNER_ID,
        content="求 $x^2-1=0$ 的解", answer="B", analysis="因式分解即可",
        qtype="选择题", difficulty="基础", tags='["一元二次"]',
        knowledge_points='["一元二次方程"]', image="", figure_image="",
        answer_image="", render_prefer="auto", created_at="2026-11-18T00:00:00",
    )
    draft_text = vault.render(q, extra_fm={"shike_todo": "配图有疑点"}, with_id=False)
    falsy("待审稿不写 shike_id", "shike_id:" in draft_text, draft_text.split("---")[1])
    truthy("待审稿带 shike_todo 说明", "shike_todo:" in draft_text and "配图有疑点" in draft_text)
    p = vault.parse(draft_text)
    truthy("待审稿仍被认成题目 md", p.ok and draft_text.startswith("---"))
    check("parse 不给待审稿编造 qid", p.qid == "", repr(p.qid))
    falsy("shike_todo 不会被当成题目字段", "shike_todo" in p.fields, p.fields)
    check("题干/答案/解析都在",
          (p.stem.strip(), p.answer.strip(), p.analysis.strip())
          == ("求 $x^2-1=0$ 的解", "B", "因式分解即可"),
          (p.stem.strip(), p.answer.strip(), p.analysis.strip()))

    # ---- ② 写进 题库/_待审/：扫描不碰
    draft = vault.write_new(q, folder=vault.PENDING,
                            extra_fm={"shike_todo": "配图有疑点"}, with_id=False)
    check("待审稿落在 题库/_待审/ 下", draft.parent.name == vault.PENDING, draft.parent.name)
    # 文件名取题干首行，但**公式会被去掉**（`$x^2-1=0$` 进不了 Windows 文件名）
    check("文件名来自题干、公式被去掉", draft.name == "求 的解.md", draft.name)
    scanned = [vault.rel(x) for x in vault._iter_md()]
    falsy("★ _待审 下的 md 不参与扫描", vault.rel(draft) in scanned)

    before = db.query(Question).count()
    r = vault.scan(db)
    check("扫描之后题库题数没变", db.query(Question).count() == before,
          db.query(Question).count())
    falsy("报告的 created 里没有它",
          any(x["path"] == vault.rel(draft) for x in r["created"]), r["created"])
    truthy("文件还在原地（没被当成新题）", draft.is_file())

    # ---- ③ 拖进 题库/ 之后：再扫就入库
    moved = vault.shelf_dir() / draft.name
    draft.replace(moved)                      # 老师在 Obsidian 里拖一下 = 移动文件
    r2 = vault.scan(db)
    hit = [x for x in r2["created"] if x["path"] == vault.rel(moved)]
    truthy("★ 拖进 题库/ 后同步就入库了", hit, r2["created"])
    got = db.get(Question, hit[0]["id"]) if hit else None
    truthy("入库的那道题真在库里", got is not None)
    if got is not None:
        check("题干搬对了", (got.content or "").strip() == "求 $x^2-1=0$ 的解", got.content)
        check("答案搬对了", got.answer == "B", got.answer)
        check("解析搬对了", got.analysis == "因式分解即可", got.analysis)
        check("知识点搬对了", _json.loads(got.knowledge_points or "[]") == ["一元二次方程"],
              got.knowledge_points)
        truthy("第一次同步把 shike_id 补回文件（以后改名也认得出）",
               "shike_id:" in vault.read_md(moved).text)

    # ---- ④ build_question 与 approve_item 必须写出同一道题
    job = BatchJob(id="testjob00001", owner_id=config.OWNER_ID, status="done",
                   total=1, done=1, failed=0, curriculum_id=curr.id,
                   document_id="doc-test", doc_filename="卷.pdf",
                   created_at="2026-11-18T00:00:00")
    db.add(job)
    db.commit()
    it = BatchItem(
        id="testitem0001", job_id=job.id, seq=1, document_id="doc-test",
        doc_filename="卷.pdf", page_no=1, content="造出来的题", answer="C",
        analysis="因为这样", qtype="选择题", difficulty="基础",
        knowledge_point="一元二次方程", tags='["标签"]',
        image="/api/crops/origin.png", figure_image="/api/crops/fig.png",
        needs_figure=1, flags='["figure_whole_question"]', status="pending",
        note="模型说这里有图但不确定",
    )
    db.add(it)
    db.commit()

    built, _node = batch_import.build_question(db, it, "built00000001")
    falsy("build_question 只是造对象，不塞进库", db.get(Question, "built00000001"))
    qid = batch_import.approve_item(db, db.get(BatchItem, it.id))
    real = db.get(Question, qid)
    truthy("approve_item 真入库了", real is not None)
    if real is not None:
        for f in ("content", "answer", "analysis", "qtype", "difficulty", "image",
                  "figure_image", "render_prefer", "tags", "knowledge_points"):
            check("字段 %s 两条出口一致" % f, getattr(built, f) == getattr(real, f),
                  (getattr(built, f), getattr(real, f)))
        check("整题兜底 → 出卷按图片印（两条出口都这样）",
              (built.render_prefer, real.render_prefer) == ("image", "image"),
              (built.render_prefer, real.render_prefer))

    # ---- ⑤ 收尾时真的会写待审稿（接线，不只是函数本身对）
    doubt = BatchItem(
        id="testitem0002", job_id=job.id, seq=2, page_no=1, content="有疑点的题",
        needs_figure=1, figure_image="", flags='["figure_ambiguous"]',
        status="pending", tags="[]", note="页上有两处图形",
    )
    broken = BatchItem(
        id="testitem0003", job_id=job.id, seq=3, page_no=1, content="",
        flags='["bad_json"]', status="pending", tags="[]",
    )
    db.add(doubt)
    db.add(broken)
    db.commit()

    task = AutoTask(id="testtask00001", owner_id=config.OWNER_ID, status="done",
                    stage="完成", src_filename="卷.pdf", document_id="doc-test",
                    curriculum_id=curr.id, job_ids=_json.dumps([job.id]),
                    pages="[1]", total_pages=1, approved=1, needs_human=1,
                    created_at="2026-11-18T00:00:00")
    db.add(task)
    db.commit()

    written = vault_inbox._export_pending_items(db, task)
    check("只有「有疑点」的那条落稿（内容坏成占位文本的不落）", len(written) == 1, written)
    if written:
        text = vault.read_md(vault.resolve(written[0])).text
        check("落在 题库/_待审/ 下", written[0].rsplit("/", 2)[-2] == vault.PENDING, written[0])
        truthy("写了疑点说明（shike_todo）", "shike_todo:" in text)
        truthy("说明了当时为什么没把握", "图形" in text or "疑点" in text or "配图" in text)
        truthy("告诉了老师下一步怎么入库（拖进 题库/ 再同步）",
               "拖进" in text and "同步" in text)
        falsy("待审稿不带 shike_id", "shike_id:" in text)
        check("待审稿里就是识别出来的题干", vault.parse(text).stem.strip() == "有疑点的题",
              vault.parse(text).stem)

    # 接线：收件箱收尾那一段真的会调它（不是只有函数自身能用）
    state_path = vault.root() / vault_inbox.STATE_NAME
    state_path.write_text(_json.dumps(
        {"tasks": {"收件箱/某体系/卷.pdf": {"task_id": task.id}}}, ensure_ascii=False),
        encoding="utf-8")
    r3 = vault_inbox.scan_once(db, enqueue=False)
    truthy("★ 收件箱收尾时报告里带上了待审稿",
           len(r3["drafts"]) >= 1, r3.get("drafts"))
    truthy("★ 跑完这条任务从状态里摘掉了",
           "收件箱/某体系/卷.pdf" not in vault_inbox._load()["tasks"])

    # ---- ⑥ 接口：上传 → 落进 收件箱/<体系>/ → 立刻开识别任务
    # 这一段**不真的识别**（把 enqueue 换成桩）：这里要验的是「文件落在哪、重名怎么办、
    # 拒收时会不会在收件箱留垃圾」，不是 AI 认得好不好。
    import io as _io

    from fastapi import HTTPException, UploadFile

    from app.routers import vault as vault_router

    code = curr.code or curr.name
    box = vault.inbox_dir() / code
    calls: list[tuple] = []
    real_enqueue = vault_inbox.enqueue
    try:
        vault_inbox.enqueue = lambda db_, path, curr_: (
            calls.append((path, curr_.id))
            or {"file": vault.rel(path), "task_id": "fake0001"})

        def _up(name: str, body: bytes = b"%PDF-1.4\n") -> UploadFile:
            return UploadFile(file=_io.BytesIO(body), filename=name)

        res = vault_router.convert_to_md(file=_up("2024 P1.pdf"), curriculum=code, db=db)
        check("接口回了 task_id", res.get("task_id") == "fake0001", res)
        check("落在 收件箱/<体系>/ 下", res.get("dir") == vault.rel(box), res.get("dir"))
        landed = box / "2024 P1.pdf"
        truthy("★ 文件真的落进去了", landed.is_file())
        check("文件内容没坏", landed.read_bytes()[:4] == b"%PDF", landed.read_bytes()[:8])
        check("★ 立刻开了识别任务（没等下一轮轮询）", len(calls) == 1, len(calls))
        sent = Path(calls[0][0]).name if calls else ""
        check("交给任务的就是刚落的那份", sent == "2024 P1.pdf", sent)

        vault_router.convert_to_md(file=_up("2024 P1.pdf", b"%PDF-1.4\nsecond"),
                                   curriculum=code, db=db)
        truthy("同名再传一次不覆盖（落成 -2）", (box / "2024 P1-2.pdf").is_file())

        try:
            vault_router.convert_to_md(file=_up("旧卷.doc"), curriculum=code, db=db)
            falsy("旧版 .doc 应当被拒", True)
        except HTTPException as e:
            check("旧版 .doc 被拒（422）", e.status_code == 422, e.status_code)
        falsy("被拒的文件没在收件箱里留下垃圾", (box / "旧卷.doc").exists())

        try:
            vault_router.convert_to_md(file=_up("卷.pdf"), curriculum="绝对不存在的体系", db=db)
            falsy("乱写的体系应当被拒", True)
        except HTTPException as e:
            check("乱写的体系被拒（422）", e.status_code == 422, e.status_code)
        falsy("体系认不出时也不留文件", (box / "卷.pdf").exists())
    finally:
        vault_inbox.enqueue = real_enqueue


# ---------------------------------------------------------------- main
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
        part_h(db)
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
