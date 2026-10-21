# -*- coding: utf-8 -*-
"""批改文件留存：批改完成后归档到「学生资料 → 改作业」+ 预览。

需求原话（用户 2026-10-20）：

> 「1. 存储与接口：新增文件上传接口，批改后的文件按「学生/日期」目录结构保存
>      （如 uploads/{studentId}/{yyyy-MM-dd}/{fileName}）；接口需校验文件类型与大小、
>      避免同名覆盖，并返回文件的存储路径与访问标识。
>   2. 按钮入口：…… 在批改作业完成后，页面新增「导出到作业存档」按钮……
>   3. 记录与预览：…… 在「学生资料 → 改作业」中自动生成批改记录，关联学生、日期、
>      作业、批改人与文件；点开该记录即可预览学生本次作业的批改情况……
>   4. 目标流程：上交作业 → 批改作业 → 批改记录归档 → 点开预览。」

这个测试要守住的（也是改这块最容易踩坏的）：

  · **目录是「学生 / 日期 / 批改」三层**，日期用的是**批改那天**而不是今天 ——
    目录是给人翻的，按存档日期分会出现「同一节课的卷子散在好几天里」。
  · **类型走白名单**：`.exe`、`.svg`（能被浏览器执行）、无扩展名一律拒。
  · **大小上限**、**0 字节**拒 —— 空文件存进去，点开是一张白纸，比报错更迷惑。
  · **同名不覆盖**：同一天存两次是两条记录、两个文件（存档的意义是「当时那一版」）。
  · **文件名必须洗干净**：`../../evil.pdf` 不能写到归档目录外面去。
  · **读回只认库里的 rel_path**：它被人为改坏（`../..`）时必须返回 None，不能兜底。
  · **删记录连文件一起删**，且**先删库后删文件**（反过来一旦 commit 失败就是死记录）。
  · **权限**：owner_id 不是自己的记录读不到（404，不是 403 —— 不泄露存在性）。
  · **批改必须属于同一个学生**：`grade_id` 指到别人的卷子要 422，不能静默挂上去。
  · **generate 走真导出**：真的调 `grade_export`，产出的文件以 `%PDF` 开头 ——
    否则「点了一下、提示成功、打开是空的」这种错测不出来。

为什么不起服务：落盘 / 避重 / 校验 / 列表都是几张表 + 磁盘上的纯逻辑，
把 router 里的函数**直接调用**就能全覆盖（同 test_plans.py 的做法）。

⚠️ 临时库必须在 import app.db **之前**设好 —— engine 绑定在 import 时定死。
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_archive_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.environ["SHIKE_DB_PATH"] = str(TMP / "archive.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

from fastapi import HTTPException                        # noqa: E402
from sqlalchemy import select                            # noqa: E402
from starlette.datastructures import UploadFile          # noqa: E402

from app import config                                   # noqa: E402
from app.db import SessionLocal, engine                  # noqa: E402
from app.models import (                                 # noqa: E402
    Base,
    GradeArchive,
    HomeworkGrade,
    HomeworkGradeItem,
    Lesson,
    LessonFile,
    Student,
)
from app.routers import grade_archives as AR             # noqa: E402
from app.services import archive_store as AS             # noqa: E402
from app.services import grade_export, grade_store       # noqa: E402
from app.services import storage                         # noqa: E402

assert str(config.DB_PATH) == str(TMP / "archive.db"), f"临时库没生效：{config.DB_PATH}"
assert str(config.UPLOAD_DIR).startswith(str(TMP)), f"归档目录没落到临时区：{config.UPLOAD_DIR}"

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"

total = 0
fails: list[str] = []


def _css_code(src: str) -> str:
    """去掉 CSS 注释 —— 反面断言（「样式不许再有 X」）必须看规则、不看注释。

    ⚠️ 这个项目踩过：解释某个坑的那句注释会把「不许出现 X」的断言弄红
       （test_homework_grades.code_only 同一个道理）。
    """
    return re.sub(r"(?s)/\*.*?\*/", "", src)


def check(name: str, ok: bool, detail: str = "") -> None:
    global total
    total += 1
    line = f"  [{'OK ' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"   <- {detail}"
    print(line)
    if not ok:
        fails.append(name)


def _rel(p: Path) -> str:
    return storage.rel(p)


def mk_pdf_bytes(pages: int = 1) -> bytes:
    """造一份最小 PDF（内存里，不落盘）。"""
    import pymupdf         # 只在测试里用；生产代码的 AGPL 边界在 adapters/pdf.py

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 120), f"page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def mk_student(db, name: str = "李芹旭") -> Student:
    s = Student(name=name)
    db.add(s)
    db.flush()
    return s


def mk_lesson(db, stu: Student, start_at: str = "2026-10-20T19:00") -> Lesson:
    ls = Lesson(student_id=stu.id, start_at=start_at, duration_min=60, topic="二次函数")
    db.add(ls)
    db.flush()
    return ls


def mk_grade(db, stu: Student, ls: Lesson, status: str = "reviewing") -> HomeworkGrade:
    # ⚠️ 日期**必须不是今天**：否则「按批改那天」与「按今天」分不开，
    #    断言永远是绿的（反向验证时真的踩到了 —— 2026-10-20 当天跑，两边同值）。
    g = HomeworkGrade(student_id=stu.id, lesson_id=ls.id, status=status,
                      created_at="2026-10-19T21:30:00", total=1, done=1)
    db.add(g)
    db.flush()
    return g


def mk_pdf_submission(db, ls: Lesson, pages: int = 1, name: str = "两页卷.pdf") -> LessonFile:
    p = config.UPLOAD_DIR / "students" / f"u{ls.student_id}_x" / "hw" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(mk_pdf_bytes(pages))
    lf = LessonFile(lesson_id=ls.id, name=name, stored=_rel(p), role="homework",
                    kind="pdf", size_bytes=p.stat().st_size, pages=pages)
    db.add(lf)
    db.flush()
    return lf


# ================================================================ ① 目录与名字
def t_paths() -> None:
    print("\n① 目录结构 / 文件名净化 / 日期规范化")
    db = SessionLocal()
    try:
        stu = mk_student(db, "李芹旭")
        d = AS.archive_dir(stu, "2026-10-20")
        rel = storage.rel(d)
        check("★ 存档目录是「学生 / 日期 / 批改」三层",
              rel == f"students/u{stu.id}_李芹旭/2026-10-20/批改", rel)
        check("★ 目录真的建出来了", d.is_dir(), str(d))
        check("学生目录带 id（两个「小李」不会撞）", f"u{stu.id}_" in rel, rel)

        # 文件名净化
        check("★★ 砍掉路径部分（../../evil.pdf 落到归档目录里）",
              AS.clean_name("../../evil.pdf") == "evil.pdf", AS.clean_name("../../evil.pdf"))
        check("★ Windows 不允许的字符被洗掉",
              ":" not in AS.clean_name("作业:第1次.pdf")
              and "/" not in AS.clean_name("作业/第1次.pdf"),
              AS.clean_name("作业:第1次.pdf"))
        check("扩展名统一小写（.PDF 也是 pdf）",
              AS.kind_of(AS.clean_name("卷子.PDF")) == "pdf", AS.clean_name("卷子.PDF"))
        check("没有扩展名 → 不在白名单（不会悄悄存成无类型文件）",
              AS.kind_of(AS.clean_name("作业")) is None, AS.clean_name("作业"))
        check("空文件名有兜底（不是空串，空文件名建不出来）",
              bool(AS.clean_name("")), repr(AS.clean_name("")))

        # 日期
        check("合法日期原样保留", AS.day_of("2026-10-20") == "2026-10-20", AS.day_of("2026-10-20"))
        check("★ 不存在的日期回落到兜底（2026-02-31）",
              AS.day_of("2026-02-31", "2026-10-20") == "2026-10-20",
              AS.day_of("2026-02-31", "2026-10-20"))
        check("★ 乱写也回落到兜底，不报错（文件是刚批完的，丢不起）",
              AS.day_of("上周三", "2026-10-20") == "2026-10-20", AS.day_of("上周三", "2026-10-20"))
        check("兜底也没有 → 今天", AS.day_of(None, None) == AS.day_of(None), AS.day_of(None))
    finally:
        db.close()


# ================================================================ ② 校验
def t_validate() -> None:
    print("\n② 类型 / 大小校验")
    def bad(name: str, size: int) -> int:
        try:
            AS.validate(name, size)
            return 0
        except AS.ArchiveError as e:
            return e.status

    check("★★ .exe 被拒（422）", bad("木马.exe", 100) == 422, str(bad("木马.exe", 100)))
    check("★★ .svg 被拒 —— 能被浏览器执行，内嵌预览等于开 XSS 口子",
          bad("图.svg", 100) == 422, str(bad("图.svg", 100)))
    check("★ .html 被拒", bad("页.html", 100) == 422, str(bad("页.html", 100)))
    check("★ 0 字节被拒（存进去点开是一张白纸，比报错更迷惑）",
          bad("空.pdf", 0) == 422, str(bad("空.pdf", 0)))
    check("★ 超过上限被拒", bad("大.pdf", AS.MAX_BYTES + 1) == 422,
          str(bad("大.pdf", AS.MAX_BYTES + 1)))
    check("刚好等于上限可以（边界值不该误杀）", bad("刚好.pdf", AS.MAX_BYTES) == 0)
    check("pdf / png / docx / zip 都在白名单里",
          all(bad(n, 10) == 0 for n in ("a.pdf", "b.png", "c.docx", "d.zip")),
          "有一个被拒了")
    n, k = AS.validate("卷子.PDF", 100)
    check("通过的返回 (洗干净的名字, kind)", n == "卷子.pdf" and k == "pdf", f"{n} / {k}")


# ================================================================ ③ 落盘与避重
def t_create() -> None:
    print("\n③ 落盘：相对路径入库 / 同名不覆盖")
    db = SessionLocal()
    try:
        stu = mk_student(db, "存档同名")
        data = b"%PDF-1.4 fake"
        r1 = AS.create(db, student=stu, filename="批改卷.pdf", data=data, on="2026-10-20")
        check("★ 记录里是**相对路径**（换机器不失效，库里没有盘符）",
              r1.rel_path.startswith("students/") and ":" not in r1.rel_path, r1.rel_path)
        check("★ 相对路径与日期对得上",
              f"/2026-10-20/批改/" in r1.rel_path, r1.rel_path)
        p1 = AS.abs_path(r1)
        check("★ 文件真的在磁盘上", p1 is not None and p1.is_file(), str(p1))
        check("大小与 kind 记对了", r1.size_bytes == len(data) and r1.kind == "pdf",
              f"{r1.size_bytes} / {r1.kind}")
        check("来源默认是 upload", r1.source == "upload", r1.source)
        check("访问地址按 id 拼", r1.id and f"/{r1.id}/raw" in
              AS.brief(r1)["url"], AS.brief(r1)["url"])

        r2 = AS.create(db, student=stu, filename="批改卷.pdf", data=data, on="2026-10-20")
        check("★★ 同名第二次不覆盖（加 _2，两条记录两个文件）",
              r2.stored_name == "批改卷_2.pdf", r2.stored_name)
        check("★★ 第一条还在（存档的意义就是「当时那一版」）",
              AS.abs_path(r1) is not None and AS.abs_path(r1).is_file(), r1.rel_path)
        check("第二条也在", AS.abs_path(r2) is not None and AS.abs_path(r2).is_file(), r2.rel_path)
        check("原名照旧记着（界面显示老师看到的那个）", r1.name == "批改卷.pdf", r1.name)

        # 日期兜底：不传就今天
        r3 = AS.create(db, student=stu, filename="今天的.pdf", data=data)
        import datetime
        check("不传日期 → 今天", r3.archived_on == datetime.date.today().isoformat(),
              r3.archived_on)
    finally:
        db.close()


# ================================================================ ④ 从批改导出
def t_generate_stub() -> None:
    print("\n④ 从批改导出：关联学生/日期/作业/批改人")
    db = SessionLocal()
    try:
        stu = mk_student(db, "导出学生")
        ls = mk_lesson(db, stu)
        g = mk_grade(db, stu, ls)
        # 桩掉真正的导出（这里只测归档这一段；真导出在 ⑤）
        pdf = TMP / "stub_export.pdf"
        pdf.write_bytes(mk_pdf_bytes(1))
        orig = grade_export.export_grade
        grade_export.export_grade = lambda db_, gid_, fmt="pdf", page=1: (
            pdf, "批改_导出学生_20261020.pdf")
        try:
            rec = AS.generate_from_grade(db, g.id, grader="周老师", note="第一次批")
            # 显式传日期覆盖（**必须还在桩里**：撤了桩就会去调真导出，
        # 而这份 grade 没挂提交件，会报「没有可导出的提交件」）
            rec2 = AS.generate_from_grade(db, g.id, on="2026-10-01")
        finally:
            grade_export.export_grade = orig

        check("★ 关联到了批改", rec.grade_id == g.id, str(rec.grade_id))
        check("★ 关联到了课时（列表上要显示「哪节课的作业」）", rec.lesson_id == ls.id,
              str(rec.lesson_id))
        check("★ 来源记成 grade（与手传区分开）", rec.source == "grade", rec.source)
        check("★ 批改人记下来了", rec.grader == "周老师", rec.grader)
        check("备注记下来了", rec.note == "第一次批", rec.note)
        import datetime
        check("（自证）测试用的批改日期不是今天，这条断言才有分辨力",
              "2026-10-19" != datetime.date.today().isoformat(),
              "测试数据日期正好是今天，换个日期")
        check("★★ 日期取**批改那天**（2026-10-19），不是今天",
              rec.archived_on == "2026-10-19", rec.archived_on)
        check("★ 文件名来自导出建议名（带学生与日期，脱离目录能自证）",
              "批改_导出学生_20261020" in rec.stored_name, rec.stored_name)
        check("文件真的落盘了", (AS.abs_path(rec) or Path("/x")).is_file(), rec.rel_path)

        check("★ 显式传日期就按传的来（补存上周的卷子）",
              rec2.archived_on == "2026-10-01", rec2.archived_on)

        # 学生不在了 —— 用伪 grade（真改 student_id 会撞外键约束，那是库在正常工作）
        class _Fake:
            id = 999123
            student_id = 999999
            lesson_id = None
            created_at = "2026-10-20T10:00:00"

        orig_get = grade_store.get_grade
        grade_store.get_grade = lambda db_, gid_: _Fake()
        grade_export.export_grade = lambda db_, gid_, fmt="pdf", page=1: (pdf, "x.pdf")
        try:
            AS.generate_from_grade(db, 999123)
            check("★ 学生不存在时要报错", False, "居然成功了")
        except AS.ArchiveError as e:
            check("★ 学生不存在时要报错（404）", e.status == 404, str(e.status))
        finally:
            grade_store.get_grade = orig_get
            grade_export.export_grade = orig
    finally:
        db.close()


def t_generate_real() -> None:
    print("\n⑤ 真导出一次（产出的必须是能打开的 PDF）")
    db = SessionLocal()
    try:
        stu = mk_student(db, "真导出")
        ls = mk_lesson(db, stu, "2026-10-18T19:00")
        lf = mk_pdf_submission(db, ls, pages=1, name="真卷.pdf")
        g = HomeworkGrade(student_id=stu.id, lesson_id=ls.id, status="reviewing",
                          created_at="2026-10-18T21:00:00", total=1, done=1)
        db.add(g)
        db.flush()
        # 提交件挂上（grade_store 靠它找页图）
        from app.models import HomeworkGradeFile
        db.add(HomeworkGradeFile(grade_id=g.id, lesson_file_id=lf.id, name=lf.name,
                                 role="submission", kind="pdf", size_bytes=lf.size_bytes,
                                 pages=1))
        db.flush()

        try:
            rec = AS.generate_from_grade(db, g.id)
        except AS.ArchiveError as e:
            check("真导出没跑起来（多半是 pymupdf 缺了）", False, str(e))
            return
        p = AS.abs_path(rec)
        head = p.read_bytes()[:5] if p and p.is_file() else b""
        check("★★ 导出的文件以 %PDF 开头（不是 0 字节的空壳）", head == b"%PDF-", str(head))
        check("★ 大小可观（真画过页图，不是占位文件）",
              rec.size_bytes > 1000, str(rec.size_bytes))
        check("★★ 日期跟着批改走（2026-10-18）", rec.archived_on == "2026-10-18",
              rec.archived_on)
    finally:
        db.close()


# ================================================================ ⑥ 列表 / 详情
def t_list_detail() -> None:
    print("\n⑥ 列表（按学生过滤 / 新的在前）与详情")
    db = SessionLocal()
    try:
        a = mk_student(db, "列表甲")
        b = mk_student(db, "列表乙")
        la = mk_lesson(db, a)
        ga = mk_grade(db, a, la)
        AS.create(db, student=a, filename="甲的1.pdf", data=b"%PDF-1", on="2026-10-01")
        AS.create(db, student=a, filename="甲的2.pdf", data=b"%PDF-1", on="2026-10-20")
        AS.create(db, student=b, filename="乙的.pdf", data=b"%PDF-1", on="2026-10-15")

        ra = AS.list_for_student(db, a.id)
        check("★ 只列这个学生的（2 份）", ra["total"] == 2, str(ra["total"]))
        check("★★ 新的在前（按日期倒序）",
              [x["name"] for x in ra["items"]] == ["甲的2.pdf", "甲的1.pdf"],
              str([x["name"] for x in ra["items"]]))
        rb = AS.list_for_student(db, b.id)
        check("另一个学生只有自己那份", rb["total"] == 1, str(rb["total"]))
        check("列表带课时主题（哪节课的作业）",
              all("lesson_topic" in x for x in ra["items"]), "少了字段")
        check("列表不带二进制内容（只有路径与地址）",
              all("rel_path" in x and "content" not in x for x in ra["items"]), "字段不对")

        # 详情：can_preview / exists / 判定统计
        rec = AS.create(db, student=a, filename="甲的可看.pdf", data=b"%PDF-1",
                        on="2026-10-21", grade=ga, source="grade")
        # ⚠️ item 的主键是 uuid4().hex[:12]（**字符串**，不是自增 int）——
        #    不显式给 id 会撞 NOT NULL。这是本项目踩过的坑，测试里也一样。
        db.add(HomeworkGradeItem(id="a1b2c3d4e5f6", grade_id=ga.id, page_no=1, seq=0,
                                 verdict="correct"))
        db.add(HomeworkGradeItem(id="0abc9def5678", grade_id=ga.id, page_no=1, seq=1,
                                 verdict="wrong"))
        db.flush()
        d = AS.detail(db, rec.id)
        check("★ PDF 可以预览（can_preview）", d["can_preview"] is True, str(d.get("can_preview")))
        check("★ 详情说文件还在", d["exists"] is True, str(d.get("exists")))
        check("★ 详情带判定分布（点开就知道该先看哪里）",
              d.get("verdicts", {}).get("wrong") == 1 and d.get("item_count") == 2,
              str(d.get("verdicts")))
        check("详情带 inline 地址（预览用）", "inline=1" in d["inline_url"], d["inline_url"])

        # 「作业存档要放到对应课下」（用户 2026-10-21）——
        # 前端按 lesson_id 分组，所以列表必须把课时信息带全：
        # lesson_id 决定「挂到哪节课」，lesson_start_at 让人在总览里认得出是哪节课。
        check("★★ 详情带上课日期（不是归档日期：今天可以补存上周的卷子）",
              d.get("lesson_start_at") == "2026-10-20T19:00", str(d.get("lesson_start_at")))

        ra2 = AS.list_for_student(db, a.id)
        by_id = {x["id"]: x for x in ra2["items"]}
        check("★★ 导出得到的存档带 lesson_id（前端按它挂到课下）",
              by_id[rec.id]["lesson_id"] == la.id, str(by_id[rec.id].get("lesson_id")))
        check("★★ 列表也带上课日期（总览里要认得出哪节课）",
              by_id[rec.id]["lesson_start_at"] == "2026-10-20T19:00",
              str(by_id[rec.id].get("lesson_start_at")))
        # 手传的两条（「甲的1/2.pdf」）没有 grade → lesson_id 空 → 不进任何一节课下。
        # 这是用户 2026-10-21 明确要的：「手传一律不挂课」。
        hand = [x for x in ra2["items"] if x["id"] != rec.id]
        check("★★ 手传的存档 lesson_id 为空（不进任何课下）",
              hand and all(x["lesson_id"] is None for x in hand),
              str([(x["name"], x["lesson_id"]) for x in hand]))

        w = AS.create(db, student=a, filename="卷.docx", data=b"PK\x03\x04", on="2026-10-22")
        dw = AS.detail(db, w.id)
        check("★ Word 不能内嵌预览（拖进 iframe 只会白屏）",
              dw["can_preview"] is False, str(dw.get("can_preview")))

        # 越权：别人的记录读不到，而且是 404 不是 403（不泄露存在性）
        rec.owner_id = 2
        db.flush()
        try:
            AS.detail(db, rec.id)
            check("★★ 别人的存档读不到", False, "居然读到了")
        except AS.ArchiveError as e:
            check("★★ 别人的存档读不到（404，不泄露存在性）", e.status == 404, str(e.status))
        rec.owner_id = config.OWNER_ID
        db.flush()

        # 学生不存在
        try:
            AS.list_for_student(db, 999999)
            check("★ 学生不存在 → 404", False, "居然成功了")
        except AS.ArchiveError as e:
            check("★ 学生不存在 → 404", e.status == 404, str(e.status))
    finally:
        db.close()


# ================================================================ ⑦ 读原件 / 越界
def t_raw() -> None:
    print("\n⑦ 读原件：只认库里的路径，越界一律拒绝")
    db = SessionLocal()
    try:
        stu = mk_student(db, "读原件")
        rec = AS.create(db, student=stu, filename="读我.pdf", data=b"%PDF-1.4 x", on="2026-10-20")
        check("正常能还原成磁盘路径", AS.abs_path(rec) is not None, "None")

        good = rec.rel_path
        rec.rel_path = "../../etc/passwd"
        check("★★ 被改成 ../.. 时返回 None（不兜底，也不「尽力而为」）",
              AS.abs_path(rec) is None, str(AS.abs_path(rec)))
        rec.rel_path = "C:/Windows/system.ini"
        check("★ 绝对路径也越界（不能借道读系统文件）",
              AS.abs_path(rec) is None, str(AS.abs_path(rec)))
        rec.rel_path = ""
        check("空路径返回 None", AS.abs_path(rec) is None, "None")
        rec.rel_path = good
        db.flush()

        # 磁盘上的文件被手动删了：记录还在，但 exists=False（界面要如实说）
        p = AS.abs_path(rec)
        p.unlink()
        d = AS.detail(db, rec.id)
        check("★ 文件被手动删掉时 exists=False（界面能如实提示）",
              d["exists"] is False, str(d.get("exists")))
    finally:
        db.close()


# ================================================================ ⑧ 删
def t_remove() -> None:
    print("\n⑧ 删除：记录与文件一起走，空目录收掉")
    db = SessionLocal()
    try:
        stu = mk_student(db, "删除学生")
        rec = AS.create(db, student=stu, filename="删我.pdf", data=b"%PDF-1", on="2026-09-09")
        p = AS.abs_path(rec)
        daydir = p.parent.parent
        aid = rec.id
        out = AS.remove(db, aid)
        check("★ 返回删成功了", out["deleted"] is True, str(out))
        check("★★ 文件真的从磁盘上没了", not p.exists(), str(p))
        check("★ 空掉的「批改 / 日期」目录被收掉（不留空壳）", not daydir.exists(),
              str(daydir))
        check("库里也没了", db.get(GradeArchive, aid) is None, "还在")
        try:
            AS.remove(db, aid)
            check("★ 再删一次 → 404", False, "居然成功了")
        except AS.ArchiveError as e:
            check("★ 再删一次 → 404", e.status == 404, str(e.status))
    finally:
        db.close()


# ================================================================ ⑨ 路由层
def t_router() -> None:
    print("\n⑨ 路由层：上传入参 / 批改归属校验 / 错误码")
    db = SessionLocal()
    try:
        a = mk_student(db, "路由甲")
        b = mk_student(db, "路由乙")
        la = mk_lesson(db, a)
        ga = mk_grade(db, a, la)

        def up(data: bytes, name: str, sid: int, gid: int | None = None):
            return asyncio.run(AR.upload_archive(
                file=UploadFile(io.BytesIO(data), filename=name),
                student_id=sid, archived_on=None, grade_id=gid,
                grader="王老师", note="手传的", db=db,
            ))

        r = up(b"%PDF-1.4 zz", "手传.pdf", a.id, ga.id)
        check("★ 上传成功并返回存储路径", r["rel_path"].startswith("students/"), str(r))
        check("★ 返回访问标识（id + url）", r["id"] and f"/{r['id']}/raw" in r["url"], str(r))
        check("手传的来源是 upload", r["source"] == "upload", r["source"])
        check("批改人 / 备注都带上了", r["grader"] == "王老师" and r["note"] == "手传的",
              f"{r['grader']} / {r['note']}")

        # 类型不对
        try:
            up(b"MZ", "木马.exe", a.id)
            check("★★ 上传 .exe → 422", False, "居然成功了")
        except HTTPException as e:
            check("★★ 上传 .exe → 422", e.status_code == 422, str(e.status_code))

        # 学生不存在
        try:
            up(b"%PDF-1", "x.pdf", 999999)
            check("★ 学生不存在 → 404", False, "居然成功了")
        except HTTPException as e:
            check("★ 学生不存在 → 404", e.status_code == 404, str(e.status_code))

        # ★★ 把 A 的批改挂到 B 的存档上
        try:
            up(b"%PDF-1", "x.pdf", b.id, ga.id)
            check("★★ 批改不属于这个学生时 422（不能静默挂上去）", False, "居然成功了")
        except HTTPException as e:
            check("★★ 批改不属于这个学生时 422（不能静默挂上去）",
                  e.status_code == 422, str(e.status_code))

        # 列表接口
        lst = AR.list_archives(a.id, limit=200, db=db)
        check("★ 列表接口能用", lst["total"] >= 1, str(lst["total"]))

        # 详情 / 不存在
        try:
            AR.get_archive(999999, db=db)
            check("★ 详情不存在 → 404", False, "居然成功了")
        except HTTPException as e:
            check("★ 详情不存在 → 404", e.status_code == 404, str(e.status_code))
    finally:
        db.close()


# ================================================================ ⑩ 迁移
def t_migration() -> None:
    print("\n⑩ 迁移 0032")
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config(str(ROOT / "backend" / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        # 断言「0032 在 head 的祖先链上」，而不是「head 恰好就是 0032」：
        # 后者每新增一条迁移就假红一次（加 0033 笔迹粗细那次就红过一次）。
        chain, rev = [], script.get_revision(head)
        while rev is not None:
            chain.append(rev.revision)
            down = rev.down_revision
            rev = script.get_revision(down[0] if isinstance(down, tuple) else down) \
                if down else None
        check("★ 0032 在迁移链上且 head 是链头",
              "0032" in chain and chain[0] == head, f"head={head} 链={chain[:4]}")
    except Exception as e:                     # noqa: BLE001
        check("读 alembic 版本链", False, f"{type(e).__name__}: {e}")

    src = (ROOT / "backend" / "alembic" / "versions" / "0032_grade_archives.py")
    check("迁移文件在", src.is_file(), str(src))
    text = src.read_text(encoding="utf-8")
    check("★ 建了 grade_archives 表", 'create_table("grade_archives"' in text or
          "create_table(\n        \"grade_archives\"" in text, "没找到 create_table")
    check("★ grade_id 是 SET NULL（批改清理时存档不该跟着消失）",
          'ondelete="SET NULL"' in text, "没有 SET NULL")
    check("★ student_id 是 CASCADE（学生删了，他的存档也没意义了）",
          'ondelete="CASCADE"' in text, "没有 CASCADE")
    check("★★ downgrade 明确不删磁盘文件（降级不该抹掉老师的劳动成果）",
          "不删磁盘" in text and "drop_table" in text, "看不到这条约定")

    # 真跑过：临时库在 import 时已由 conftest 迁到 head（所以表一定在）
    from app import migrate
    with engine.connect() as c:
        import sqlalchemy as sa
        cols = [r[1] for r in c.execute(sa.text("PRAGMA table_info(grade_archives)"))]
        check("★★ 临时库里真有这张表（15 列）", len(cols) == 15, f"{len(cols)} 列")
    check("migrate 模块可用（当前版本读得出来）", callable(migrate.current_revision))


# ================================================================ ⑪ 前端
def t_frontend() -> None:
    print("\n⑪ 前端接线")
    wb = (FE / "workbench.html").read_text(encoding="utf-8")
    hg = (FE / "src" / "views" / "HomeworkGrade.js").read_text(encoding="utf-8")
    sd = (FE / "src" / "views" / "StudentDetail.js").read_text(encoding="utf-8")
    api = (FE / "src" / "api.js").read_text(encoding="utf-8")

    check("★ 工作台有「导出到作业存档」按钮", "导出到作业存档" in wb, "没找到按钮")
    check("★ 工作台按钮调的是 /grade-archives/generate",
          "/api/grade-archives/generate" in wb, "没找到调用")
    check("★★ 工作台存档前先冲本机笔迹（否则刚画的几笔不在 PDF 里）",
          "await this.flushPending();" in wb and
          wb.index("await this.flushPending();") < wb.index("grade-archives/generate"),
          "顺序不对或没冲")
    check("★ 存档按钮有 loading（防重复点）", 'archiving' in wb and ':loading="archiving"' in wb,
          "没找到 loading")

    check("★ A 路线（HomeworkGrade.js）也有存档按钮", "导出到作业存档" in hg, "没找到")
    check("★ A 的存档也先补发本机笔迹", "await flushPending()" in hg, "没找到")
    check("★ A 的按钮在 setup 里暴露了", "archiveGrade" in hg.split("return {")[-1],
          "没在 return 里")

    check("★ api.js 有 archiveApi", "export const archiveApi" in api, "没找到")
    check("★ gradeApi.archive 走同一个接口",
          "archive: (gid, body)" in api or "archive: (gid" in api, "没找到")
    check("★ inline 地址拼对了（预览用）", "inline=1" in api, "没找到")

    # ---- 存档的显示（2026-10-21 起：只挂在对应课下，不再有平铺总览）----
    check("★★ 点开用 iframe 看 PDF", "<iframe" in sd and "archiveOpen.kind === 'pdf'" in sd,
          "没找到 iframe 分支")
    check("★ 图片走 <img>", "archiveIsImg" in sd, "没找到")
    check("★★ 不能预览的类型给下载（不是白屏）",
          "不能在页面里看" in sd and "下载下来看" in sd, "没找到降级提示")
    check("★ 存档列表来自 archiveApi.list", "archiveApi.list" in sd, "没找到")
    check("★★ 「改作业」总览卡片已去掉（用户 2026-10-21）",
          'class="archive-row"' not in sd and "archiveApi.upload" not in sd,
          "平铺列表或上传入口还在")
    check("★★ 没有第三份平铺列表（v-for=\"a in archives\" 已经没了）",
          'v-for="a in archives"' not in sd, "还有平铺列表")
    check("★ 删存档会问一句（不可恢复）", "window.confirm" in sd, "没找到确认")

    # 模板字面量里不许出现反引号（会把模板提前截断，只有真挂载才炸）
    i = sd.index("template:")
    check("★★ StudentDetail 模板里反引号正好 2 个（多了就是被截断了）",
          sd[i:].count("`") == 2, str(sd[i:].count("`")))
    i2 = hg.index("template:")
    check("★★ HomeworkGrade 模板里反引号正好 2 个", hg[i2:].count("`") == 2,
          str(hg[i2:].count("`")))

    css_code = _css_code((FE / "app.css").read_text(encoding="utf-8"))
    check("★ app.css 有存档行样式（.archive-line）", ".archive-line" in css_code,
          "没找到 .archive-line")
    check("★★ 总览卡片那套样式已清掉（.archive-row / -main / -head 都没了）",
          ".archive-row" not in css_code and ".archive-main" not in css_code
          and ".archive-head" not in css_code, "样式还有残留")

    # ---- 「作业存档要放到对应课下」（用户 2026-10-21）----
    # 前端按 lesson_id 把 archives 分组，再在「上课记录」每节课里取这节课那组。
    check("★★ 存档按课时分组（computed archivesByLesson）",
          "const archivesByLesson = computed(" in sd, "没找到分组")
    check("★★ 没挂课的存档不进任何课下（手传一律不挂课）",
          "if (!a.lesson_id) continue;" in sd, "没找到过滤")
    check("★★ 每节课下引用这节课的存档",
          "archivesByLesson[item.lesson.id]" in sd, "模板里没引用")
    # ⚠️ 只查字符串在不在不够：`archivesByLesson` 在 computed 定义里也出现。
    #    要比位置 —— 引用必须落在「上课记录」时间轴那条 v-for 之后。
    tl = sd.find('v-for="item in timeline"')
    ax = sd.find("archivesByLesson[item.lesson.id]")
    check("★★ 引用在「上课记录」时间轴内部（不是拼到别处去了）",
          tl != -1 and ax != -1 and ax > tl, f"timeline@{tl} archive@{ax}")
    check("★★ 没挂课的存档有兜底入口（删过课的卷子不会在页面上凭空消失）",
          "orphanArchives" in sd and "未挂到具体课时的存档" in sd, "没找到孤儿行")
    # 孤儿行与课下行是同一套 .archive-line，页面上一共只有这两处（2 次）——
    # 多出来就说明又有人加了一份平铺列表。
    check("★★ 存档只出现两处：课下行 + 孤儿行（没有第三份列表）",
          sd.count('class="archive-line"') == 2, f"{sd.count('class=\"archive-line\"')} 处")
    check("★ 课下行有独立样式（.archive-line，虚线分隔不撑散时间轴）",
          'class="archive-line"' in sd and
          ".archive-line" in (FE / "app.css").read_text(encoding="utf-8"), "没找到")


# ================================================================ main
def main() -> int:
    print("批改文件留存 · 自包含检查（不起服务、不碰真库）")
    # ⚠️ **不要**先 create_all 再 run_migrations：临时库是空的，create_all 会把
    #    表建出来，随后 alembic 从 0001 跑起就会撞「table already exists」。
    #    空库直接跑迁移即可，它会一路建到 head（含 0032 的 grade_archives）。
    from app import migrate
    migrate.run_migrations(verbose=False)

    t_paths()
    t_validate()
    t_create()
    t_generate_stub()
    t_generate_real()
    t_list_detail()
    t_raw()
    t_remove()
    t_router()
    t_migration()
    t_frontend()

    print(f"\n共 {total} 项，失败 {len(fails)} 项")
    if fails:
        for f in fails:
            print("  - " + f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
