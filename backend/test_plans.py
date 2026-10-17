# -*- coding: utf-8 -*-
"""整体规划：学生级的「一份文档」+ 上传文本文件转正文 + 原件归档。

需求原话（用户 2026-10-16）：
「学生信息界面添加一下"整体规划",放在"考试情况"上方,要能够新建规划,删除规划,
  查看规划,修改规划。我会将文本或者文本文件放到规划,我点开某个规划要能够查看渲染好的
  文件,常见的 markdown、word 这些。(每个规划点开才能详细查看)」

这个测试要守住的（也是改这块最容易踩坏的）：
  · **正文是 Markdown 文本**（打字的 / 由文件转来的都进 content），不是「挂一个文件路径」——
    否则「直接打字写规划」这条路就没地方放。
  · **上传即转正文**：Word / Markdown / 文本 / PDF / PPT 转成 Markdown 返回给前端；
    转换失败**不阻断上传**（原件照存，原因写在 note 里）—— 图片、压缩包一律走这条。
  · **列表不带正文**（只给摘要），点开某条才 `GET /api/plans/{id}` 取完整正文。
    摘要必须**剥掉 Markdown 标记**且**永不抛异常**（算不出来不该让学生页打不开）。
  · **原件先进暂存区，保存时才归档**到 `students/<学生>/plans/<标题>/`；
    改标题 → 目录跟着改名（且**不会一路 _2/_3 涨上去**），否则文件会散在两个文件夹里。
  · **删规划 = 连原件一起删**（那些是这条规划的专属材料）。
  · 越界 cache_key 必须被拒 —— 否则能借道删掉正式归档里的文件。
  · ⚠️ 带图的 Word 抽出来的图落在**笔记资源目录**，正文引用成 /api/notes/files/<name>，
    所以 notes 的引用计数必须把规划正文也算上，否则会误删规划正在用的图（且不报错）。

为什么不起服务：CRUD / 转换 / 归档 / 改名都是几张表 + 磁盘上的纯逻辑，把 router 里的函数
**直接调用**就能全覆盖 —— 比 uvicorn 快一个量级，不占端口，也不会「服务没起」假失败。

⚠️ 临时库必须在 import app.db **之前**设好 —— engine 绑定在 import 时定死。
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_plan_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.db 之前设
os.environ["SHIKE_DB_PATH"] = str(TMP / "plan.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

from fastapi import HTTPException                     # noqa: E402
from sqlalchemy import select                         # noqa: E402
from starlette.datastructures import UploadFile       # noqa: E402

from app import config                                # noqa: E402
from app.db import SessionLocal, engine               # noqa: E402
from app.models import Base, PlanFile, Student, StudentPlan   # noqa: E402
from app.routers import notes as NT                   # noqa: E402
from app.routers import plans as PL                   # noqa: E402
from app.schemas import (                             # noqa: E402
    PlanCacheRemoveIn,
    PlanFileIn,
    PlanIn,
    PlanPatch,
)
from app.services import storage                      # noqa: E402

assert str(config.DB_PATH) == str(TMP / "plan.db"), f"临时库没生效：{config.DB_PATH}"
assert str(config.UPLOAD_DIR).startswith(str(TMP)), f"归档目录没落到临时区：{config.UPLOAD_DIR}"

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"

fails: list[str] = []
total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global total
    total += 1
    line = f"  [{'OK ' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"   <- {detail}"
    print(line)
    if not ok:
        fails.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def node_bin() -> str | None:
    for cand in (shutil.which("node"),
                 r"C:/Users/6/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"):
        if cand and Path(cand).exists():
            return str(cand)
    return None


def extract_template(path: Path) -> str | None:
    """把 `template: \\`...\\`` 那段抠出来（SPA 视图都这么写）。"""
    src = read(path)
    key = "template: `"
    i = src.find(key)
    if i < 0:
        return None
    j = src.find("`", i + len(key))
    if j < 0:
        return None
    return src[i + len(key):j]


def code_only(src: str) -> str:
    """去掉 `//` 行注释与 HTML 注释再断言。

    ⚠️ 反面断言（「这句话已删掉」）必须只看**代码**：注释里解释这个坑的那句话
    本身就会把断言弄红（在 test_paper_ui 里踩过一模一样的）。
    """
    out = []
    for line in src.split("\n"):
        if line.lstrip().startswith("//"):
            continue
        out.append(line)
    s = "\n".join(out)
    return re.sub(r"(?s)<!--.*?-->", "", s)


# ================================================================ 库 / 数据
def reset() -> None:
    """每次都从干净的临时库 + 干净的 uploads 开始（否则第二次跑会假失败）。"""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    shutil.rmtree(TMP / "data" / "uploads", ignore_errors=True)


def mk_student(db, name: str = "张同学") -> Student:
    stu = Student(owner_id=config.OWNER_ID, name=name)
    db.add(stu)
    db.commit()
    return stu


def uf(filename: str, data: bytes = b"x") -> UploadFile:
    """造一个够 router 用的 UploadFile（真上传走 multipart，这里只要 .read()）。"""
    return UploadFile(io.BytesIO(data), filename=filename)


def upload(filename: str, data: bytes = b"hello") -> dict:
    return asyncio.run(PL.upload_plan_file(file=uf(filename, data)))


def mk_plan(db, stu, **kw) -> dict:
    return PL.create_plan(PlanIn(student_id=stu.id, **kw), db)


def expect_422(fn) -> tuple[bool, str]:
    try:
        fn()
    except HTTPException as e:
        return e.status_code == 422, f"HTTP {e.status_code} {e.detail}"
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return False, "没有抛异常"


def student_dir(stu) -> Path:
    return config.STUDENTS_DIR / storage.student_folder(stu)


def _all_plan_files(stu) -> list[Path]:
    d = student_dir(stu) / "plans"
    return sorted(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else []


def _is_body_copy(p: Path) -> bool:
    """是不是我们写的**正文副本** —— 认文件头的标记，不认文件名。

    为什么按标记认：老师可能自己往那个目录里放一份「规划.md」，
    按名字认就会把人家的东西当成自己的副本。
    """
    if p.suffix != ".md":
        return False
    try:
        return PL.BODY_MARK in p.read_text(encoding="utf-8", errors="ignore")[:200]
    except OSError:
        return False


def body_files_on_disk(stu) -> list[Path]:
    """正文副本：库里的正文在学生文件夹里同步的那一份（设计⑦）。"""
    return [p for p in _all_plan_files(stu) if _is_body_copy(p)]


def plan_files_on_disk(stu) -> list[Path]:
    """归档目录下的**原件**（不含正文副本 —— 副本不是老师传上来的东西）。"""
    return [p for p in _all_plan_files(stu) if not _is_body_copy(p)]


def plan_dirs_on_disk(stu) -> list[Path]:
    d = student_dir(stu) / "plans"
    return sorted(p for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


# ---------------------------------------------------------------- 样本文件
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(blocks: list[tuple[str, str]]) -> bytes:
    """现场造一个最小 .docx（只放 word/document.xml —— 转换器对 rels/numbering 是容错的）。

    blocks: [("h2", "一、总体目标"), ("p", "把计算提上去"), ("b", "重点：因式分解"),
             ("li", "每周三次"), ("math", "x^2-5x+6=0")]
    """
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def para(kind: str, text: str) -> str:
        t = esc(text)
        if kind == "h2":
            return (f'<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
                    f'<w:r><w:t>{t}</w:t></w:r></w:p>')
        if kind == "b":
            return f'<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{t}</w:t></w:r></w:p>'
        if kind == "li":
            return (f'<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
                    f'<w:r><w:t>{t}</w:t></w:r></w:p>')
        if kind == "math":
            return (f'<w:p><m:oMath xmlns:m="http://schemas.openxmlformats.org/'
                    f'officeDocument/2006/math"><m:t>{t}</m:t></m:oMath></w:p>')
        return f'<w:p><w:r><w:t>{t}</w:t></w:r></w:p>'

    body = "".join(para(k, v) for k, v in blocks)
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


# ================================================================ ① 摘要纯函数
def t_preview() -> None:
    section("① 列表摘要（Markdown → 一句纯文本）")
    s = PL.preview_of("## 一、总体目标\n\n把 **计算** 提上去，公式 $x^2+1$ 要熟。\n\n- 每天 20 题")
    check("行首的 # 被剥掉（不该在列表里露出「## 」）", "##" not in s, s)
    check("加粗标记被剥掉", "**" not in s, s)
    check("列表符号被剥掉（- 不该出现在摘要里）", not s.startswith("-"), s)
    check("公式只留里面的字（$x^2+1$ → x^2+1）", "x^2+1" in s and "$" not in s, s)
    check("正文还在（没有把内容也一起吃掉）", "总体目标" in s and "每天 20 题" in s, s)

    link = PL.preview_of("详见 [讲义](http://x/y.pdf) 与 ![图](/api/notes/files/a.png)")
    check("链接只留文字（不留地址）", "讲义" in link and "http" not in link, link)
    check("图片语法整段吃掉（不留 ![」与地址）", "![" not in link and ".png" not in link, link)

    noise = PL.preview_of("> 从「讲义.docx」导入（Word，2 页）")
    check("引用块（导入说明那种）不留 「> 」", not noise.startswith(">"), noise)

    code = PL.preview_of("```\nx = 1\n```\n正文在这里")
    check("代码块整块略去（片段进摘要没有信息量）", "x = 1" not in code and "正文在这里" in code, code)

    check("空串 / None 都不抛，返回空串",
          PL.preview_of("") == "" and PL.preview_of(None) == "" and PL.preview_of("   ") == "")
    longm = PL.preview_of("字" * 400)
    check(f"太长会截断（≤ {PL.PREVIEW_CHARS + 1} 字）", len(longm) <= PL.PREVIEW_CHARS + 1, str(len(longm)))
    check("截断用省略号收尾", longm.endswith("…"))
    check("摘要里不会出现换行（列表要的是一行）",
          "\n" not in PL.preview_of("# 标题\n正文\n\n- 一\n- 二"))


# ================================================================ ② 目录名 / 暂存区安全
def t_paths() -> None:
    section("② 归档目录名 + 暂存区路径安全")
    class P:
        title = "初三全年规划"
    check("目录名 = 规划标题", storage.plan_dir_label(P) == "初三全年规划", storage.plan_dir_label(P))

    class Q:
        title = "   "
    check("标题空着给兜底名（目录名空着比「未命名规划」更困惑）",
          storage.plan_dir_label(Q) == "未命名规划", storage.plan_dir_label(Q))

    class R:
        title = "a/b\\c:d*?"
    got = storage.plan_dir_label(R)
    check("标题里的非法字符被洗掉（不能建出跨目录的名字）",
          "/" not in got and "\\" not in got, got)

    check("规划暂存区的根与考试暂存区**不是**同一个目录",
          config.PLAN_CACHE_DIR != config.EXAM_CACHE_DIR
          and config.PLAN_CACHE_DIR.name == storage.PLAN_CACHE_NAME,
          f"{config.PLAN_CACHE_DIR.name} vs {storage.EXAM_CACHE_NAME}")

    # ★ 越界 key 必须被拒：否则一个构造出来的 key 就能借道删掉正式归档里的文件。
    #   先在外面放一个**真文件**，这样「拒绝」不是靠「反正那路径不存在」蒙对的。
    reset()
    decoy_dir = config.UPLOAD_DIR / "students" / "u1_x"
    decoy_dir.mkdir(parents=True, exist_ok=True)
    decoy = decoy_dir / "secret.txt"
    decoy.write_text("正式归档里的东西", encoding="utf-8")
    for bad in ("../students/u1_x/secret.txt", "a/../../students/u1_x/secret.txt",
                "../../../shike.db", "..", "", None):
        check(f"越界 key {bad!r} 被拒（借道删正式归档这条路要堵死）",
              PL._cache_path(bad) is None, str(PL._cache_path(bad)))
    check("被拒的那些 key 之后，那个正式文件还在", decoy.is_file())

    r = upload("正文.md", "# 标题\n正文".encode("utf-8"))
    got = PL._cache_path(r["cache_key"])
    check("正常 key 能解析到暂存区里那个文件",
          got is not None and str(got).startswith(str(config.PLAN_CACHE_DIR)), str(got))
    check("★ 解析出来的是**规划**暂存区，不是考试那个（两个区不能互相看见）",
          str(got).startswith(str(config.PLAN_CACHE_DIR))
          and not str(got).startswith(str(config.EXAM_CACHE_DIR)), str(got))
    PL.remove_plan_cache(PlanCacheRemoveIn(cache_key=r["cache_key"]))
    check("撤回后暂存文件真的没了", not got.exists(), str(got))


# ================================================================ ③ 上传 → 转正文
def t_upload_convert() -> None:
    section("③ 上传：转 Markdown（含转不出来的那几种）")

    # ---- Markdown：原样当正文 ----
    md = "# 初三全年规划\n\n- 每周 3 次\n- 每次 20 题\n\n公式 $x^2$"
    r = upload("规划.md", md.encode("utf-8"))
    check("Markdown：转成功（标题/列表原样留着）",
          r["converted"] and "初三全年规划" in r["markdown"] and "- 每周 3 次" in r["markdown"], r["note"])
    check("Markdown：kind_cn 说得清是什么（.md 归「文本」一类）",
          r["kind_cn"] == "文本" and r["kind"] == "text", f"{r['kind']}/{r['kind_cn']}")
    check("Markdown：note 里写了字数（老师想知道转出多少）", "字" in r["note"], r["note"])
    check("返回了 cache_key（保存时要靠它把原件搬进归档目录）", bool(r["cache_key"]), str(r))
    check("原件真的落到了暂存区", (config.PLAN_CACHE_DIR / r["cache_key"]).is_file())
    PL.remove_plan_cache(PlanCacheRemoveIn(cache_key=r["cache_key"]))

    # ---- 纯文本 ----
    r = upload("随手记.txt", "第一段\n第二段".encode("utf-8"))
    check("纯文本：转成正文", r["converted"] and "第一段" in r["markdown"], r["note"])
    PL.remove_plan_cache(PlanCacheRemoveIn(cache_key=r["cache_key"]))

    # ---- Word：真造一个 docx（标题 / 加粗 / 列表 / 公式）----
    docx = make_docx([("h2", "一、总体目标"), ("b", "重点：因式分解"),
                      ("li", "每周三次专项"), ("math", "x2-5x+6=0"), ("p", "收尾一句")])
    r = upload("初三规划.docx", docx)
    m = r["markdown"]
    check("Word：转成功", r["converted"], r["note"])
    check("Word：标题层级留住了（## 一、总体目标）", "## 一、总体目标" in m, m[:80])
    check("Word：加粗留住了（**重点…**）", "**重点：因式分解**" in m, m[:120])
    check("Word：列表留住了（- 每周三次专项）", "- 每周三次专项" in m, m[:160])
    check("Word：公式只留文字形式，并且**明说**了（不静默丢）",
          "公式" in r["note"] and "文字形式" in r["note"], r["note"])
    check("Word：kind_cn = Word", r["kind_cn"] == "Word", r["kind_cn"])
    PL.remove_plan_cache(PlanCacheRemoveIn(cache_key=r["cache_key"]))

    # ---- 内容全是图的 Word：转不出来，但**不是失败** ----
    blank = make_docx([])
    r = upload("全是图的规划.docx", blank)
    check("空 Word：转换失败但上传成功（converted=False）", not r["converted"], str(r)[:200])
    check("空 Word：原因写得能照着做（不是一句「失败」）", len(r["note"]) > 8, r["note"])
    check("空 Word：原件照样收下（绝不静默丢弃）",
          (config.PLAN_CACHE_DIR / r["cache_key"]).is_file())
    PL.remove_plan_cache(PlanCacheRemoveIn(cache_key=r["cache_key"]))

    # ---- 图片 / 压缩包：本来就没有正文可读 ----
    r = upload("拍照的规划.jpg", b"\xff\xd8\xff\xe0fakejpeg")
    check("图片：不转正文（说清为什么）", not r["converted"] and "图片" in r["note"], r["note"])
    check("图片：原件留着（这条规划里能点开看）",
          (config.PLAN_CACHE_DIR / r["cache_key"]).is_file())
    check("图片：kind = image（界面上标签不能说「未知」）", r["kind"] == "image", r["kind"])
    PL.remove_plan_cache(PlanCacheRemoveIn(cache_key=r["cache_key"]))

    r = upload("讲义合集.zip", b"PK\x03\x04fake")
    check("压缩包：不转正文，也不报错", not r["converted"] and bool(r["note"]), r["note"])
    check("压缩包：kind 兜底成 ZIP（永不为空）", r["kind"] == "ZIP", r["kind"])
    PL.remove_plan_cache(PlanCacheRemoveIn(cache_key=r["cache_key"]))

    # ---- 太大 ----
    ok, detail = expect_422(lambda: upload("big.md", b"x" * (PL.MAX_BYTES + 10)))
    check("超过上限直接拦下（20MB）", ok, detail)

    # ---- 撤回暂存：真删 ----
    r = upload("临时.md", b"# x")
    p = PL._cache_path(r["cache_key"])
    PL.remove_plan_cache(PlanCacheRemoveIn(cache_key=r["cache_key"]))
    check("撤回没保存的暂存文件 = 真删（不是只从列表里拿掉）", not p.exists(), str(p))
    check("暂存目录也收掉了（不留空壳）", not p.parent.exists(), str(p.parent))
    ok, detail = expect_422(
        lambda: PL.remove_plan_cache(PlanCacheRemoveIn(cache_key="../../students/x.txt")))
    check("撤回接口也拒绝越界 key", ok, detail)


# ================================================================ ④ CRUD
def t_crud() -> None:
    section("④ 新建 / 列表 / 详情 / 修改 / 删除")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        other = mk_student(db, "李同学")

        check("刚建的学生没有任何规划", PL.list_plans(stu.id, db)["items"] == [])

        d = mk_plan(db, stu, title="初三全年规划", content="# 目标\n\n把计算提上去")
        check("新建成功，返回完整对象", d["id"] > 0 and d["title"] == "初三全年规划", str(d)[:120])
        check("新建后正文就在（不是等保存第二次才进去）", "把计算提上去" in d["content"])
        check("有正文的规划会建目录（正文要在学生文件夹里留一份副本，见设计⑦）",
              d["dir_rel"] != "", d["dir_rel"])

        # 标题空着 → 用正文**第一行**兜底（列表里总得认得出来）
        d2 = mk_plan(db, stu, title="   ", content="## 暑期冲刺\n\n每天两小时")
        check("标题空着用正文第一行兜底（不是一长条摘要、也不是「未命名」）",
              d2["title"] == "暑期冲刺", d2["title"])
        check("第一行是长句时截到 30 字（列表上不能铺一大条）",
              len(PL.title_from_content("# " + "字" * 60)) <= 30,
              str(len(PL.title_from_content("# " + "字" * 60))))
        check("正文全空时才退「未命名规划」",
              PL.title_from_content("\n\n   \n") == "未命名规划", PL.title_from_content("\n \n"))
        # 标题与正文都空 → 拦下
        ok, detail = expect_422(lambda: mk_plan(db, stu, title="", content="   "))
        check("标题和正文都空 → 422（不许建出点开是空白的规划）", ok, detail)

        # ---- 列表：不带正文，带摘要 ----
        lst = PL.list_plans(stu.id, db)
        check("列表有 2 条", len(lst["items"]) == 2, str(len(lst["items"])))
        one = lst["items"][0]
        check("★ 列表**不含**正文（几千字塞进列表会让学生页很重）",
              "content" not in one, str(list(one)))
        check("列表带摘要（剥掉标记的一句纯文本）", one["preview"] and "#" not in one["preview"], one["preview"])
        check("列表带字数与原件数（列表上要说得清这条有多大）",
              one["chars"] > 0 and one["file_count"] == 0, str({k: one[k] for k in ('chars', 'file_count')}))
        check("列表按最近修改排序（刚建的在前）", lst["items"][0]["id"] == d2["id"], str([x["id"] for x in lst["items"]]))
        check("别的学生看不见这两条", PL.list_plans(other.id, db)["items"] == [])

        # ---- 详情：带正文 ----
        full = PL.get_plan(d["id"], db)
        check("★ 点开某条才有完整正文", full["content"].startswith("# 目标"), full["content"][:20])
        check("详情带原件清单（哪怕是空的）", isinstance(full["files"], list) and full["files"] == [], str(full["files"]))

        # ---- 改：没传的字段 = 不改 ----
        u = PL.patch_plan(d["id"], PlanPatch(title="初三全年规划（修订）"), db)
        check("只传标题：标题改了", u["title"] == "初三全年规划（修订）", u["title"])
        check("★ 只传标题：正文一个字都没动",
              u["content"] == d["content"], u["content"][:30])
        u = PL.patch_plan(d["id"], PlanPatch(content="换一版正文"), db)
        check("只传正文：正文改了", u["content"] == "换一版正文", u["content"])
        check("只传正文：标题保持不动", u["title"] == "初三全年规划（修订）", u["title"])
        u = PL.patch_plan(d["id"], PlanPatch(content=""), db)
        check("想清空就显式传空串（「传了」和「没传」是两件事）", u["content"] == "", u["content"])
        u = PL.patch_plan(d["id"], PlanPatch(title="   "), db)
        check("标题传空白 → 保持原名（不许把标题清成空）", u["title"] == "初三全年规划（修订）", u["title"])
        check("改过之后 updated_at 跟上了",
              u["updated_at"] >= d["updated_at"], f"{d['updated_at']} -> {u['updated_at']}")

        # ---- 删 ----
        PL.delete_plan(d2["id"], db)
        check("删掉后列表少一条", len(PL.list_plans(stu.id, db)["items"]) == 1)
        ok, detail = expect_422(lambda: PL.delete_plan(99999, db))
        check("删不存在的规划 → 404", detail.startswith("HTTP 404"), detail)
        ok, detail = expect_422(lambda: PL._plan_or_404(db, 99999))
        check("取不存在的规划 → 404（详情接口同一套）", detail.startswith("HTTP 404"), detail)
        ok, detail = expect_422(lambda: PL.list_plans(99999, db))
        check("不存在的学生 → 404", detail.startswith("HTTP 404"), detail)

        # 放到最后建：它会多占一条列表，不能插在中间把计数断言搅了
        d_empty = mk_plan(db, stu, title="只有标题、没正文没文件的规划")
        check("只有标题（没正文、没文件）→ dir_rel 仍为空（不该在磁盘上留空目录）",
              d_empty["dir_rel"] == "", d_empty["dir_rel"])
    finally:
        db.close()


# ================================================================ ⑤ 原件归档 / 改名搬目录 / 删除
def t_files() -> None:
    section("⑤ 原件归档 + 改标题搬目录 + 删规划连原件一起删")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        docx = make_docx([("h2", "一、总体目标"), ("p", "把计算提上去")])
        up = upload("初三规划.docx", docx)

        d = mk_plan(db, stu, title="初三全年规划", content=up["markdown"],
                    files=[PlanFileIn(cache_key=up["cache_key"], name="初三规划.docx",
                                      converted=True, note=up["note"][:120])])
        check("保存后原件从暂存区搬走了", not (config.PLAN_CACHE_DIR / up["cache_key"]).exists())
        disk = plan_files_on_disk(stu)
        check("原件落到 students/<学生>/plans/<标题>/ 下", len(disk) == 1, str(disk))
        check("★ 目录名就是规划标题（老师在文件夹里认得出）",
              disk and disk[0].parent.name == "初三全年规划", str(disk[0].parent) if disk else "无")
        check("原件保留了**原名**（不是 nb_xxx 那种乱码名）",
              disk and disk[0].name == "初三规划.docx", disk[0].name if disk else "无")
        check("详情里 dir_rel 有了（界面上要能告诉老师文件在哪）", bool(d["dir_rel"]), d["dir_rel"])
        f = d["files"][0]
        check("原件行带转换标记（已转正文）", f["converted"] is True, str(f))
        check("原件行带类型标签（Word）", f["kind_cn"] == "Word", f["kind_cn"])
        check("原件行有下载地址", f["url"] == f"/api/plan-files/{f['id']}/raw", f["url"])
        check("原件行不吃正文（kind / size 都在）", f["size_bytes"] > 100, str(f["size_bytes"]))

        # ---- 改标题 → 目录跟着改名（含库里的 stored）----
        before = d["dir_rel"]
        u = PL.patch_plan(d["id"], PlanPatch(title="初三全年规划（2026 秋）"), db)
        check("★ 改标题后归档目录跟着改名", u["dir_rel"].endswith("/初三全年规划（2026 秋）"), u["dir_rel"])
        check("旧目录不在了（不留一个装着孤儿的旧目录）",
              not (config.UPLOAD_DIR / before).exists(), before)
        check("库里的 stored 也同步了（否则界面点开 404）",
              u["files"][0]["stored_name"] == "初三规划.docx"
              and u["files"][0]["stored_name"] in u["dir_rel"] + "/" + u["files"][0]["stored_name"],
              str(u["files"][0]["stored_name"]))
        check("磁盘上文件还在新目录里", len(plan_files_on_disk(stu)) == 1)

        # ---- 反复保存同一个标题**不该**一路 _2/_3（踩过的坑）----
        names = set()
        for _ in range(3):
            uu = PL.patch_plan(d["id"], PlanPatch(title=u["title"]), db)
            names.add(Path(uu["dir_rel"]).name)
        check("★ 标题没变时反复保存不会一路 _2/_3（目录名不涨）",
              names == {"初三全年规划（2026 秋）"}, str(names))
        check("反复保存后目录还是一个", len(plan_dirs_on_disk(stu)) == 1,
              str(plan_dirs_on_disk(stu)))

        # ---- 两条规划同标题 → 第二条拿到 _2，且自己改标题不会把 _2 丢掉 ----
        up2 = upload("另一份.docx", docx)
        d2 = mk_plan(db, stu, title="初三全年规划（2026 秋）",
                     files=[PlanFileIn(cache_key=up2["cache_key"], name="另一份.docx",
                                       converted=True)])
        check("同标题的第二条规划拿到 _2 目录（两次的附件不该混进同一个文件夹）",
              Path(d2["dir_rel"]).name == "初三全年规划（2026 秋）_2", Path(d2["dir_rel"]).name)
        d2b = PL.patch_plan(d2["id"], PlanPatch(content="补一句"), db)
        check("只改正文时目录不动", Path(d2b["dir_rel"]).name == "初三全年规划（2026 秋）_2",
              Path(d2b["dir_rel"]).name)
        check("两条规划的文件各在各的目录里",
              len(plan_files_on_disk(stu)) == 2
              and len({p.parent.name for p in plan_files_on_disk(stu)}) == 2,
              str([str(p.parent.name) for p in plan_files_on_disk(stu)]))

        # ---- 增量提交：再传一份文件只是追加，不会重搬已有的 ----
        up3 = upload("补充说明.md", "# 补充\n\n再加一条".encode("utf-8"))
        u3 = PL.patch_plan(d["id"], PlanPatch(
            files=[PlanFileIn(cache_key=up3["cache_key"], name="补充说明.md", converted=True)],
            content=u["content"]), db)
        check("增量提交：已有的原件没被重搬（还在同一目录）",
              len(PL.get_plan(d["id"], db)["files"]) == 2, str(len(u3["files"])))
        check("增量提交：新文件进的是同一条规划的目录",
              len({p.parent.name for p in plan_files_on_disk(stu)
                   if p.name in ("初三规划.docx", "补充说明.md")}) == 1,
              str([str(p.parent) for p in plan_files_on_disk(stu)]))

        # ---- 暂存文件丢了 → 整个拦下（不能让老师以为存进去了）----
        up4 = upload("会丢的.md", b"# x")
        (config.PLAN_CACHE_DIR / up4["cache_key"]).unlink()
        okk, detail = expect_422(lambda: PL.patch_plan(d["id"], PlanPatch(
            content="x", files=[PlanFileIn(cache_key=up4["cache_key"], name="会丢的.md")]), db))
        check("暂存文件不见了 → 422 拦下并说清（不是静默跳过）",
              okk and "重新上传" in detail, detail)
        check("文件校验失败时**没有**先动磁盘（事务没提交，库和磁盘不能对不上）",
              len(plan_files_on_disk(stu)) == 3, str(len(plan_files_on_disk(stu))))

        # ---- 删单份原件 ----
        first = PL.get_plan(d["id"], db)["files"][0]
        PL.delete_plan_file(first["id"], db)
        check("删单份原件：库里没了", len(PL.get_plan(d["id"], db)["files"]) == 1)
        check("删单份原件：磁盘上也真删了（不是只从列表里拿掉）",
              all(p.name != "初三规划.docx" for p in plan_files_on_disk(stu)),
              str([p.name for p in plan_files_on_disk(stu)]))

        # ---- 删规划 = 连原件带目录一起走 ----
        rel = PL.get_plan(d["id"], db)["dir_rel"]
        PL.delete_plan(d["id"], db)
        check("删规划：库里的记录没了", not db.get(StudentPlan, d["id"]))
        check("删规划：原件一起删", all(pname != "补充说明.md"
                                    for pname in [p.name for p in plan_files_on_disk(stu)]),
              str([p.name for p in plan_files_on_disk(stu)]))
        check("★ 删规划：它那个归档目录也收掉了（不留空目录）",
              not (config.UPLOAD_DIR / rel).exists(), rel)
        check("★ 别的规划的目录**没被误删**（止步线停在学生文件夹内、不误伤邻居）",
              Path(d2["dir_rel"]).name in [p.name for p in plan_dirs_on_disk(stu)],
              str([p.name for p in plan_dirs_on_disk(stu)]))
        # 学生文件夹本身必须还在（老师自己也在用）
        check("学生文件夹还在（删空规划不该铲掉它）", student_dir(stu).is_dir())
    finally:
        db.close()


# ================================================================ ⑥ 引用计数
def t_asset_refcount() -> None:
    section("⑥ 笔记资源回收：规划正文引用的图**不能**被误删")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        # 带图的 Word 抽出来的图落在**笔记资源目录**，正文引用成 /api/notes/files/<name>。
        # 模拟一份这样的正文（真造一张图进 docx 成本高，引用形态一样）。
        name = "nb_0123456789ab.png"
        (config.NOTES_DIR).mkdir(parents=True, exist_ok=True)
        (config.NOTES_DIR / name).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        mk_plan(db, stu, title="带图规划",
                content=f"# 规划\n\n![结构图](/api/notes/files/{name})\n\n后面还有话")
        used = NT._used_images(db)
        check("★ 规划正文引用的配图被算进引用计数（否则会被回收逻辑删掉）",
              name in used, str(sorted(used)))
        check("没被引用到的名字不在里面（引用计数不能变成「全留」）",
              "nb_ffffffffffff.png" not in used, str(sorted(used)))

        src = read(ROOT / "backend" / "app" / "routers" / "notes.py")
        check("引用计数里确实读了 student_plans.content（改表名时这里要一起改）",
              "StudentPlan.content" in src, "")

        # 反面：真正的回收仍然会收掉没人引用的图（引用计数没被改成「一律不删」）
        orphan = "nb_aaaaaaaaaaaa.png"
        (config.NOTES_DIR / orphan).write_bytes(b"x")
        n = NT._sweep_assets(db, {orphan, name}, images_only=True)
        check("回收仍然会删「谁都不要」的图（不是干脆不删）", n == 1, str(n))
        check("但规划正在用的那张**还在**", (config.NOTES_DIR / name).is_file())
    finally:
        db.close()


# ================================================================ ⑦ 前端守卫
def t_frontend() -> None:
    section("⑦ 前端守卫（接线 / 位置 / 文案）")
    dlg = FE / "src" / "components" / "PlanDialog.js"
    sd = FE / "src" / "views" / "StudentDetail.js"
    api = FE / "src" / "api.js"
    css = FE / "app.css"
    for p in (dlg, sd, api, css):
        check(f"{p.relative_to(ROOT)} 存在", p.exists(), str(p))

    src = read(dlg)
    tpl = extract_template(dlg) or ""
    code = code_only(src)

    # ---- 渲染接线（这是这个需求的核心：正文要是**渲染好的**）----
    check("引了渲染管线 mathRender（不自己写 marked / katex 调用）",
          "from '../mathRender.js'" in src and "renderRichEl" in src and "ensureLibs" in src, "")
    check("★ 正文容器是**空 div + 函数/template ref**（KaTeX 会换掉容器里的文本节点）",
          re.search(r'class="plan-rich"\s+ref="richEl"></div>', tpl) is not None,
          str(re.search(r'class="plan-rich"[^>]*>[^<]*', tpl)))
    check("渲染容器里没有 Vue 插值（有插值就会被 KaTeX 顶掉）",
          bool(re.search(r'class="plan-rich"[^>]*></div>', tpl)), "")
    check("没有正文时说清楚下一步怎么走（不留一块空白）", "这条规划还没有正文" in tpl, "")
    check("重绘用 flush:'post'（等容器真的挂上去）", "flush: 'post'" in src, "")
    check("切「看/改」两态后也会重绘（切回来的是新挂的容器）",
          re.search(r"watch\(\[mode, content, detail\]", src) is not None, "")

    # ---- 两态：默认看、能切改、能回来 ----
    check("默认先给人**看**（需求原话：每个规划点开才能详细查看）",
          "props.plan && !props.startEdit ? 'view' : 'edit'" in src, "")
    check("列表上的「改」能直接进编辑态（startEdit 这个开关）",
          "startEdit: { type: Boolean, default: false }" in src and "'edit'" in src, "")
    check("看的那一面有「改」", "toEdit" in tpl, "")
    check("编辑态有「返回查看」（改完能回得去）", "返回查看" in tpl, "")
    check("看的那一面有「删除规划」", "删除规划" in tpl and "removePlan" in src, "")
    check("删除带二次确认（删规划会连原件一起删）",
          "删掉就没了" in code, "")

    # ---- 传文件那条路 ----
    check("上传走 plansApi.upload", "plansApi.upload" in src, "")
    check("正文空着就直接填；已有内容**问一句**是追加还是只要原件",
          "确定 = 追加到正文末尾" in code and "取消 = 只把原件存进这条规划" in code, "")
    check("追加时留可见的分界线（过两天看得出这段是哪来的，也能删掉）",
          "以下内容来自" in code, "")
    check("★ 文件输入框**不设 accept**（设了文件对话框会藏住 .lnk 和截图）",
          bool(re.search(r'<input ref="fileEl" type="file"[^>]*>', tpl))
          and "accept" not in (re.search(r'<input ref="fileEl" type="file"[^>]*>', tpl).group(0)
                               if re.search(r'<input ref="fileEl" type="file"[^>]*>', tpl) else ""),
          str(re.search(r'<input ref="fileEl" type="file"[^>]*>', tpl)))
    check("转不出正文时**照实说**（不能在界面上看起来像成功了）",
          "没有转出正文" in code or "warn(r.note" in code, "")
    check("「已转正文 / 只存原件」两种状态分开显示",
          "'已转正文'" in tpl and "'只存原件'" in tpl, "")
    check("删除原件时提醒「正文里那段不会跟着删」（避免误解）",
          "不会**跟着删" in src or "不会" in code and "跟着删" in code, "")
    check("关窗不删暂存（误触删掉老师刚传的文件比留两天垃圾糟得多）",
          "不删暂存" in src, "")

    # ---- 学生详情页 ----
    sd_src = read(sd)
    sd_tpl = extract_template(sd) or ""
    check("StudentDetail 引了 PlanDialog 与 plansApi",
          "import PlanDialog" in sd_src and "plansApi" in sd_src, "")
    check("PlanDialog 挂上了（带 startEdit）",
          '<PlanDialog v-if="planDialog"' in sd_tpl and ':start-edit="planDialog.startEdit"' in sd_tpl, "")
    check("列表只调 plansApi.list（正文走弹窗里的 get，别在列表页拉一堆正文）",
          "plansApi.list" in sd_src and "plansApi.get" not in sd_src, "")
    check("丢了规划不该让学生页打不开（catch 里退化成空数组）",
          re.search(r"async function loadPlans\(\)[\s\S]{0,220}plans\.value = \[\]", sd_src) is not None, "")

    # ★ 位置：需求点名「放在考试情况上方」
    i_plan = sd_tpl.find("整体规划")
    i_exam = sd_tpl.find("考试情况")
    check("★ 「整体规划」卡片在「考试情况」**上方**",
          i_plan > 0 and i_exam > 0 and i_plan < i_exam, f"整体规划@{i_plan} 考试情况@{i_exam}")
    check("有「＋ 新建规划」入口", "＋ 新建规划" in sd_tpl, "")
    check("没有规划时给一句能照着做的空态", "还没有规划" in sd_tpl and "自动转成正文" in sd_tpl, "")
    check("每条规划有三个动作：查看 / 改 / 删",
          "openPlan(p)" in sd_tpl and "editPlan(p)" in sd_tpl and "removePlan(p)" in sd_tpl, "")
    check("★ 列表里的摘要**不渲染**（几十块 KaTeX 会明显卡），走纯文本 preview",
          'class="plan-preview"' in sd_tpl and "p.preview" in sd_tpl, "")
    check("列表上也标出「还没有正文」（空规划一眼看得出来）", "还没有正文" in sd_tpl, "")
    check("修好之后会刷新列表（onPlanSaved → loadPlans）",
          "onPlanSaved" in sd_src and "await loadPlans()" in sd_src, "")

    # ---- api.js ----
    api_src = read(api)
    plan_api = api_src.split("plansApi")[1][:1600] if "plansApi" in api_src else ""
    for m in ("list:", "get:", "create:", "update:", "remove:", "upload:", "removeCache:",
              "removeFile:", "rawUrl:"):
        check(f"plansApi 有 {m[:-1]}", m in plan_api, "")
    check("plansApi 打的是 /api/plans 那一套地址",
          "'/api/plans'" in plan_api and "/api/plans/upload" in plan_api
          and "/api/students/${studentId}/plans" in plan_api
          and "/api/plan-files/" in plan_api and "'/api/plans/upload/remove'" in plan_api,
          plan_api[:200])

    # ---- CSS ----
    css_src = read(css)
    for sel in (".plan-row", ".plan-main", ".plan-preview", ".plan-rich", ".plan-title", ".plan-editor"):
        check(f"app.css 有 {sel}", sel in css_src, "")
    check("看的那一面正文**不设高度上限**（专门点开来看的，限高等于把内容藏起来）",
          re.search(r"\.plan-rich\s*\{[^}]*max-height:\s*62vh", css_src) is not None
          and ".line-clamp" not in css_src.split(".plan-rich")[1][:400], "")
    check("摘要压成一行（它是线索不是正文）",
          re.search(r"\.plan-preview\s*\{[^}]*text-overflow:\s*ellipsis", css_src) is not None, "")
    check("编辑框用等宽字体（写表格时缩进看得清）",
          re.search(r"\.plan-editor\s*\{[^}]*monospace", css_src) is not None, "")
    check("正文里的表格有边框（Markdown 表格不画线就看不成表）",
          ".plan-rich th" in css_src and ".plan-rich td" in css_src, "")

    # ---- 迁移 ----
    mig = ROOT / "backend" / "alembic" / "versions" / "0026_plans.py"
    check("迁移 0026 存在", mig.exists(), str(mig))
    if mig.exists():
        m = read(mig)
        check("迁移建了两张表（student_plans / plan_files）",
              '"student_plans"' in m and '"plan_files"' in m, "")
        check("迁移 down_revision 接在 0025 上", 'down_revision: Union[str, None] = "0025"' in m, "")
        check("迁移顺序也是先建后删（downgrade 反着来）",
              m.find("def upgrade") < m.find("def downgrade")
              and m.find('op.create_table(\n        "student_plans"') > 0, "")
        check("两张表都带学生/规划的级联（删学生 → 规划跟着走）",
              'ondelete="CASCADE"' in m, "")

    main_py = read(ROOT / "backend" / "main.py")
    check("路由注册上了（plans.router）", "plans.router" in main_py and "import" in main_py, "")


# ================================================================ ⑧ 模板编译 + node --check
def t_template_compile() -> None:
    section("⑧ Vue 真编译器校验模板 + node --check")
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上，跳过模板编译校验")
        return
    vue = FE / "vendor" / "vue.global.prod.js"
    check("vendor 里有 vue.global.prod.js", vue.exists(), str(vue))
    if not vue.exists():
        return

    files = [FE / "src" / "components" / "PlanDialog.js",
             FE / "src" / "views" / "StudentDetail.js"]
    for f in files:
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        check(f"node --check {f.name}（抓「模板能编译但 setup 有语法错 → 白屏」）",
              r.returncode == 0, (r.stderr or "")[-200:])

    script = r"""
const fs=require('fs'),vm=require('vm');
const VUE=process.argv[2];
// ⚠️ compiler-dom 遇属性值里的 `&`（`a && b.length`）会去 decodeEntities，需要一个
//    像样的 document；stub 太简陋会直接抛 "Cannot read properties of undefined (reading '0')"
//    —— 看起来像模板错了，其实是环境问题。这套 stub 与 test_paper_ui.py 同一份。
function mkDoc() {
  const el=()=>{const o={_raw:'',_foo:'',_text:''};
    Object.defineProperty(o,'innerHTML',{set(v){o._raw=v;const m=/foo="([\s\S]*)"/.exec(v);
o._foo=m?m[1]:'';o._text=v;},get(){return o._raw;}});
    Object.defineProperty(o,'children',{get(){const dec=s=>String(s).replace(/&quot;/g,'"')
.replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
      return [{getAttribute:()=>dec(o._foo)}];}});
    Object.defineProperty(o,'textContent',{get(){return String(o._text)
.replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"');}});
    return o;};
  return {createElement:()=>el(),querySelector:()=>null,createTextNode:()=>({})};
}
const ctx={console,document:mkDoc(),window:{}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(VUE,'utf8'),ctx);
function comp(tpl){try{ctx.Vue.compile(tpl);return 'OK';}
catch(e){return 'FAIL '+String(e.message).slice(0,160);}}
// 自证：这两行必须一个 FAIL 一个 OK，否则说明这套校验在空转
console.log('SELFTEST ' + comp('<div><template v-else-if="b">x</template></div>'));
console.log('SELFTEST_GOOD ' + comp('<div><p v-if="a">1</p><p v-else>2</p></div>'));
for (const f of process.argv.slice(3)) {
  console.log('TPL ' + f + ' ' + comp(fs.readFileSync(f,'utf8')));
}
"""
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "compile_check.js"
        sp.write_text(script, encoding="utf-8")
        # 先把模板抠出来单独存文件：模板里本来就有反引号 / 反斜杠，直接当参数喂给
        # node 会在 Windows 的命令行转义上翻车（踩过）。
        tpl_files = []
        for f in files:
            t = extract_template(f)
            if not t:
                continue
            out = Path(td) / (f.stem + "_template.html")
            out.write_text(t, encoding="utf-8")
            tpl_files.append(out)
        r = subprocess.run([node, str(sp), str(vue)] + [str(p) for p in tpl_files],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    check("自证：故意写错的模板必须报错（否则说明校验在空转）",
          re.search(r"SELFTEST FAIL", out) is not None, out[-200:])
    check("自证：正常的 v-if/v-else 必须能编译", re.search(r"SELFTEST_GOOD OK", out) is not None,
          out[-200:])
    ok_tpl = re.findall(r"^TPL .+ OK$", out, re.M)
    check("两份模板都用真编译器过了一遍", len(ok_tpl) == len(tpl_files) and len(tpl_files) == 2,
          out.strip()[-300:])
    check("没有编译不过的模板（compiler-30 这类错会让整页白屏）", "FAIL" not in out.replace("SELFTEST FAIL", ""),
          out.strip()[-300:])


# ================================================================ ⑩ 正文副本（学生文件夹里那份）
# 定义挨着上面的归档逻辑写；执行顺序在 main 里（排在真挂载检查之后）。
def t_body_copy() -> None:
    section("⑩ 正文副本：库里的正文在学生文件夹里也存一份（设计⑦）")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        # ---- 只有正文、没传原件：也要有目录，正文才有地方放 ----
        d = mk_plan(db, stu, title="暑期冲刺", content="# 暑期冲刺\n\n每天两小时 $x^2$。")
        check("没传原件的规划也会建目录（正文得有地方放）", bool(d["dir_rel"]), d["dir_rel"])
        body = body_files_on_disk(stu)
        check("★ 正文副本落在学生文件夹下", len(body) == 1, str(body))
        if not body:
            return
        txt = body[0].read_text(encoding="utf-8")
        check("★ 副本内容 = 库里的正文（公式原文照搬，渲染交给打开它的工具）",
              "每天两小时" in txt and "$x^2$" in txt, txt[:60])
        check("副本文件名叫「规划.md」（老师在文件夹里一眼认得出）",
              body[0].name == "规划.md", body[0].name)
        check("副本**不**进「原件」列表（它不是老师传上来的东西）",
              PL.get_plan(d["id"], db)["files"] == [], str(PL.get_plan(d["id"], db)["files"]))
        row = [i for i in PL.list_plans(stu.id, db)["items"] if i["id"] == d["id"]]
        check("副本也不算进「N 份原件」（界面上不该把它数进去）",
              bool(row) and row[0]["file_count"] == 0, str(row[:1]))

        # ---- 反复保存：覆盖同一份，不该长出 规划_2 / 规划_3 ----
        for _ in range(3):
            PL.patch_plan(d["id"], PlanPatch(content="# 暑期冲刺\n\n改过了"), db)
        body = body_files_on_disk(stu)
        check("★ 反复保存只覆盖那一份，不会长出 规划_2/规划_3（靠文件头标记认领）",
              len(body) == 1 and body[0].name == "规划.md", str([p.name for p in body]))
        check("副本跟着正文一起更新", "改过了" in body[0].read_text(encoding="utf-8"))

        # ---- 改标题：目录搬家，副本跟着走 ----
        old_dir = config.UPLOAD_DIR / d["dir_rel"]
        u = PL.patch_plan(d["id"], PlanPatch(title="暑期冲刺（第二阶段）"), db)
        new_dir = config.UPLOAD_DIR / u["dir_rel"]
        check("★ 改标题后副本跟着目录一起搬走（旧目录不留孤儿）",
              not old_dir.exists() and (new_dir / "规划.md").is_file(), str(new_dir))

        # ---- 目录里本来就有老师自己传的「规划.md」→ 不许覆盖人家的东西 ----
        (new_dir / "规划.md").unlink()
        (new_dir / "规划.md").write_text("# 老师自己写的", encoding="utf-8")
        PL.patch_plan(d["id"], PlanPatch(content="# 暑期冲刺\n\n再来一次"), db)
        check("★ 同名的原件**不被覆盖**（认文件头标记，不认文件名）",
              "老师自己写的" in (new_dir / "规划.md").read_text(encoding="utf-8"),
              str([p.name for p in sorted(new_dir.iterdir())]))
        b2 = body_files_on_disk(stu)
        check("副本另起一个名字，两份都在",
              len(b2) == 1 and b2[0].name != "规划.md", str([p.name for p in sorted(new_dir.iterdir())]))

        # ---- 正文清空 → 副本删掉（不留一份和库里对不上的旧版本）----
        PL.patch_plan(d["id"], PlanPatch(content=""), db)
        check("正文清空后副本也没了（不留旧版本）", body_files_on_disk(stu) == [],
              str(body_files_on_disk(stu)))
        check("目录留着不主动回收（回收会让「库里有路径、磁盘上没目录」更容易撞名）",
              new_dir.is_dir(), str(new_dir))

        # ---- 删规划：副本连目录一起走 ----
        PL.patch_plan(d["id"], PlanPatch(content="# 回来"), db)
        rel = PL.get_plan(d["id"], db)["dir_rel"]
        # 先把上面为了验「不覆盖」而造的那份老师的文件清掉 —— 它是测试的干扰物，
        # 留着的话目录本来就不空，验不出「删规划有没有把目录收干净」。
        (new_dir / "规划.md").unlink(missing_ok=True)
        PL.delete_plan(d["id"], db)
        check("★ 删规划后副本连目录一起收掉（不留「规划已删、正文还在」的文件夹）",
              not (config.UPLOAD_DIR / rel).exists(), rel)
        check("学生文件夹本身还在", student_dir(stu).is_dir())
    finally:
        db.close()


# ================================================================ ⑨ 真挂载
def t_mount() -> None:
    section("⑨ 真挂载（jsdom 把 PlanDialog 挂起来，数渲染痕迹）")
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上")
        return
    script = FE / "plan_mount_check.js"
    check("frontend/plan_mount_check.js 存在", script.exists(), str(script))
    if not script.exists():
        return
    r = subprocess.run([node, str(script)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if "[SKIP]" in out:
        print("  [SKIP] 没装 jsdom（可选依赖）")
        return
    check("挂载检查全过（正文真渲染出 .katex、图片指对、两态都能切）",
          "全部通过" in out, out.strip()[-400:])
    check("node 侧输出完整（进程没中途挂掉）",
          "SELFTEST" not in out and "共 " in out, out.strip()[-200:])


# ================================================================ main
def main() -> int:
    print("整体规划 · 自包含检查（不起服务、不碰真库）")
    t_preview()
    t_paths()
    t_upload_convert()
    t_crud()
    t_files()
    t_asset_refcount()
    t_frontend()
    t_template_compile()
    t_mount()
    t_body_copy()
    print(f"\n共 {total} 项，失败 {len(fails)} 项")
    if fails:
        for f in fails:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
