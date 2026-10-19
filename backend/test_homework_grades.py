# -*- coding: utf-8 -*-
"""AI 批改作业（迁移 0027）：建任务 / 判定依据 / 逐题结果 / 标注 / 确认 / 回存题库。

需求原话（用户 2026-10-17）：
「我想引入 ai 批改作业，需要能够批量批改，并且能够在文件（图片或者文件）上做对应标记
（对/错、考点、错误原因），题目主要是数学题目，可能有几何图，有公式，题型可能有选择题、
填空题、解答题（要写过程，圈画出学生步骤）；可以 ai 自主解答后判断，也可根据老师上传的
答案判断题目是否正确；对于解答题部分题目需要根据老师提供的参考步骤和评分细则去批改；
审核时，对于批改有问题的，老师也可手动改正。后续该部分可能会加入到"改作业"处，
每个学生交完自动批改。拓展功能：对于学生的错题，能够打标签，保存到对应题目」

这个测试要守住的（也是改这块最容易踩坏的）：

  · **判定依据三种，而且 AI 自解不是默认口径**（A1 已拍板：答案是老师提供的）：
    开批之前可以改（`PATCH /api/homework-grades/{gid}`），**开批之后就锁住** ——
    批过的结果按旧依据算，改了依据却留着旧判定，比不改更糟。
    切到 key/rubric 却没给内容 → 必须 422，不能拿「AI 自己解」冒充「按老师答案判」。
  · **一期只判对错、不给分**（A3）：`score` / `full_score` 恒为 None，绝不 `or 0`。
  · **`ungradable` 是一等状态**：识别不清 / 图题 / 没作答都走它，不许落进 `wrong`
    （一旦允许，系统就开始冤枉人）。
  · **改学生作答 = 旧判定作废**：老师把识别错的 `3` 改成 `8` 之后，旧 `evidence_quote`
    已经不存在于新文本里了，必须清判定并 `transcription_version + 1`，
    否则导出时会出现「引用了一句不存在的话」。
  · **`total` 是页数**，不是题目数（batch_jobs 踩过这个歧义）。
  · **确认之前不进统计**：`status` 不到 done，掌握度 / 家长稿都不该读。
  · **标注坐标一律 0~1000**（按页归一化），与 `batch_items.figure_box` 同制。
  · **AGPL 边界**：`grade_ai.py` / `grade_store.py` 里不许出现 pymupdf。
  · **静默失败要能看见**：`finish_reason == "length"` 必须当失败（全项目此前没有一处读它）。

为什么不起服务：这些是几张表 + 磁盘上几份文件的纯逻辑，把 `grade_store` 里的函数
**直接调用**就能全覆盖 —— 比 uvicorn 快一个量级，不占端口，也不会「服务没起」假失败。
AI 那一层用桩替换（`_recognize_page`），所以**不花钱、不联网**也能跑完整条链路。

⚠️ 临时库必须在 import app.db **之前**设好 —— engine 绑定在 import 时定死。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_grade_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.db 之前设
os.environ["SHIKE_DB_PATH"] = str(TMP / "grade.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

from sqlalchemy import select                          # noqa: E402

from app import config                                 # noqa: E402
from app.db import SessionLocal, engine                # noqa: E402
from app.models import (                               # noqa: E402
    Base,
    HomeworkGrade,
    HomeworkGradeFile,
    HomeworkGradeItem,
    HomeworkGradeMark,
    Lesson,
    LessonFile,
    Question,
    Student,
)
from app.schemas import (                              # noqa: E402
    GradeIn,
    GradeItemPatch,
    GradeMarkIn,
    GradeMarkPatch,
    GradeMarksIn,
    GradeRegionIn,
    GradeRegionsPut,
    GradeSetupPatch,
    GradeToBankIn,
    GradeConfirmIn,
)
from app.services import grade_store as G              # noqa: E402

assert str(config.DB_PATH) == str(TMP / "grade.db"), f"临时库没生效：{config.DB_PATH}"
assert str(config.UPLOAD_DIR).startswith(str(TMP)), f"归档没落到临时区：{config.UPLOAD_DIR}"

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"
BE = ROOT / "backend"

total = 0
fails: list[str] = []


def check(a, b, detail: object = "") -> bool:
    """`check(条件, 名称)` 和 `check(名称, 条件)` 两种写法都认。

    ⚠️ 为什么这么宽容：这个文件是我分几段写的，两种顺序都出现过 ——
    而混用的后果是**假通过**（条件位拿到一句非空文案 = 永远为真，输出还显示 [OK ] True）。
    与其漏改一条，不如在测试基建这一层收口。**生产代码不要学这个。**
    """
    if isinstance(a, str) and isinstance(b, bool):
        name, ok = a, b
    else:
        ok, name = a, b
    global total
    total += 1
    if ok:
        print(f"  [OK ] {name}")
    else:
        print(f"  [FAIL] {name}" + (f"   <- {detail}" if detail != "" else ""))
        fails.append(name)
    return bool(ok)


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def code_only(src: str) -> str:
    """去掉行注释与 HTML 注释 —— 反面断言（「不许出现 X」）必须看代码，不看注释。

    ⚠️ 这个项目踩过：解释某个坑的那句注释，会把「不许出现 X」的断言弄红。
    """
    src = re.sub(r"(?m)^\s*#.*$", "", src)
    src = re.sub(r"(?s)/\*.*?\*/", "", src)
    src = re.sub(r"(?s)<!--.*?-->", "", src)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def expect_err(fn, *a, **kw) -> tuple[int, str]:
    try:
        fn(*a, **kw)
        return 0, "没有抛错"
    except G.GradeError as e:
        return int(e.status), str(e)
    except Exception as e:                             # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 造数据
def make_png(path: Path, w: int = 60, h: int = 80) -> None:
    """手写一张最小 PNG（不引第三方库）。批改只要求「能被适配层打开、算得出页数」。"""
    raw = b"".join(b"\x00" + bytes([240, 240, 240] * w) for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def mk_student(db, name: str = "批改测试学生") -> Student:
    s = Student(name=name)
    db.add(s)
    db.flush()
    return s


def mk_lesson(db, stu: Student) -> Lesson:
    ls = Lesson(student_id=stu.id, start_at="2026-10-15T19:00", duration_min=60)
    db.add(ls)
    db.flush()
    return ls


def mk_submission(db, ls: Lesson, name: str = "作业.png") -> LessonFile:
    p = config.UPLOAD_DIR / "students" / f"u{ls.student_id}_x" / "hw" / name
    make_png(p)
    lf = LessonFile(lesson_id=ls.id, name=name, stored=_rel(p),
                    role="homework", kind="image", size_bytes=p.stat().st_size)
    db.add(lf)
    db.flush()
    return lf


def _rel(p: Path) -> str:
    from app.services import storage
    return storage.rel(p)


def item_payload(**kw) -> dict:
    base = {
        "q_no": "3", "qtype": "解答题", "stem": "解方程 $x^2-5x+6=0$",
        "student_answer": "由 $x^2-5x+6=0$ 得 $(x-2)(x-3)=0$，所以 $x=2$",
        "correct_answer": "$x=2$ 或 $x=3$",
        "verdict": "partial", "score": None, "full_score": None,
        "basis": "漏了一个根", "confidence": "high", "kp_name": "一元二次方程",
        "error_reason": "只写了一个根", "flags": [], "raw": {"q": 1},
    }
    base.update(kw)
    return base


# ================================================================ ① 契约
def t_contract() -> None:
    print("\n=== ① 契约：表 / schema / 路由 / 迁移 / 边界 ===")
    tables = Base.metadata.tables
    for n in ("homework_grades", "homework_grade_files",
              "homework_grade_items", "homework_grade_marks"):
        check(n in tables, f"表 {n} 存在")
    tg = tables["homework_grades"]
    for col in ("student_id", "lesson_id", "homework_id", "answer_mode",
                "total", "done", "failed", "score", "full_score", "status", "dir_rel"):
        check(col in tg.columns, f"homework_grades.{col} 存在")
    # 一节课可以有多次批改 —— 绝不能像 homeworks 那样唯一
    uniq = [c for c in tg.constraints if c.__class__.__name__ == "UniqueConstraint"]
    check("lesson_id 上没有唯一约束（重批是常态）", not uniq, uniq)
    check("homework_id 可空（可以「先批了再补记交没交」）", tg.columns["homework_id"].nullable)
    check("score / full_score 可空（一期不给分，空≠0）",
          tg.columns["score"].nullable and tg.columns["full_score"].nullable)
    ti = tables["homework_grade_items"]
    check("verdict 默认 ungradable（判不了是一等状态，不是错误分支）",
          (ti.columns["verdict"].server_default.arg or "") == "ungradable",
          ti.columns["verdict"].server_default)
    check("有 transcription_version（改字要能作废旧判定）",
          "transcription_version" in ti.columns)
    check("有 evidence 的载体 raw / flags", "raw" in ti.columns and "flags" in ti.columns)
    tm = tables["homework_grade_marks"]
    check("标注表有 geo / points（0~1000 归一化坐标）",
          "geo" in tm.columns and "points" in tm.columns)
    check("标注可以挂整页（item_id 可空）", tm.columns["item_id"].nullable)

    from app.schemas import GradeIn as _GI
    check("GradeIn 有 answer_mode / answer_text / rubric_text",
          {"answer_mode", "answer_text", "rubric_text"} <= set(_GI.model_fields))
    from app.schemas import GradeSetupPatch as _SP
    check("GradeSetupPatch 三个字段都可空（None = 不改）",
          all(_SP.model_fields[k].default is None for k in
              ("answer_mode", "answer_text", "rubric_text")))

    from app.routers import homework_grades as R
    paths = {(getattr(r, "path", ""), tuple(sorted(getattr(r, "methods", set()))))
             for r in R.router.routes}
    must = [
        ("/api/homework-grades", ("POST",)),
        ("/api/homework-grades", ("GET",)),
        ("/api/homework-grades/{gid}", ("GET",)),
        ("/api/homework-grades/{gid}", ("PATCH",)),      # 开批前改判定依据
        ("/api/homework-grades/{gid}/run", ("POST",)),
        ("/api/homework-grades/{gid}/confirm", ("POST",)),
        ("/api/homework-grades/{gid}/marks", ("POST",)),
        ("/api/homework-grades/{gid}/marks/batch", ("POST",)),   # 本机草稿队列补发
        ("/api/homework-grades/{gid}/page/{pno}", ("GET",)),
        ("/api/grade-items/{iid}", ("PATCH",)),
        ("/api/grade-items/{iid}/rerun", ("POST",)),
        ("/api/grade-items/{iid}/to-bank", ("POST",)),
        ("/api/grade-marks/{mid}", ("PATCH",)),
        ("/api/grade-marks/{mid}", ("DELETE",)),
        # 一题一区域（0028/0029）+ 0030 的两条
        ("/api/homework-grades/{gid}/regions", ("GET",)),
        ("/api/homework-grades/{gid}/regions", ("PUT",)),
        ("/api/homework-grades/{gid}/regions/copy", ("POST",)),
        ("/api/homework-grades/{gid}/region-image", ("GET",)),     # 切出来的图长什么样
        ("/api/homework-grades/{gid}/skipped-pages", ("PATCH",)),  # 整页不送 AI
    ]
    for p, m in must:
        check(f"路由 {m[0]} {p}", (p, m) in paths)

    main_py = code_only(read(BE / "main.py"))
    check("main.py 注册了 homework_grades", "include_router(homework_grades.router)" in main_py)

    mig = read(BE / "alembic" / "versions" / "0027_homework_grades.py")
    check("迁移 0027 存在且接在 0026 后面",
          'revision: str = "0027"' in mig and 'down_revision: Union[str, None] = "0026"' in mig)
    check("迁移只建新表、不 alter 既有表",
          "add_column" not in mig and "drop_column" not in mig)

    # 0030：不送 AI 的两级 + 模型的两个结构信号。**纯加列**，老记录零影响。
    mig30 = read(BE / "alembic" / "versions" / "0030_grade_skip_and_signals.py")
    check("迁移 0030 接在 0029 后面",
          'revision: str = "0030"' in mig30 and 'down_revision: Union[str, None] = "0029"' in mig30)
    # ⚠️ 只看 upgrade 那半段：downgrade 里当然有 drop_column（那是回滚）。
    up30 = code_only(mig30).split("def downgrade")[0]
    check("0030 的 upgrade 是纯加列（不删不改既有列）",
          up30.count("add_column") == 4 and "drop_column" not in up30,
          up30.count("add_column"))
    check("0030 每一列都有 server_default（旧行不许读成 NULL）",
          up30.count("server_default=") == 4, up30.count("server_default="))

    # AGPL 边界：pymupdf 只能出现在 adapters/pdf.py。
    # ⚠️ 这里看的是**真正的 import 语句**，不是字面出现 —— 注释里解释这条约束的那句话
    #    会把「不许出现 pymupdf」的断言弄红（这个项目踩过好几次）。
    for f in ("grade_ai.py", "grade_store.py"):
        src = code_only(read(BE / "app" / "services" / f))
        check(re.search(r"(?m)^\s*(?:import|from)\s+(?:pymupdf|fitz)\b", src) is None,
              f"{f} 不 import pymupdf（AGPL 只能待在 adapters/pdf.py）",
              re.findall(r"(?m)^\s*(?:import|from)\s+.*", src)[:3])
    ai = read(BE / "app" / "services" / "grade_ai.py")
    check("grade_ai 读 finish_reason（否则截断会静默变成「没批」）",
          "finish_reason" in ai)
    check("grade_ai 有 reasoning_effort 的低档设置", "reasoning_effort" in ai)
    # GRADE_MAX_EDGE 是批改专用常量；要防的是有人另立一个顶格的 MAX_EDGE
    # （或去改 vision.MAX_EDGE —— 那是公式识别的实测基线）。
    # ⚠️ 只看**顶格赋值**：docstring 里"写死了 MAX_EDGE=1600"那句会被宽松正则误伤。
    check("grade_ai 有自己的 GRADE_MAX_EDGE，且没有另立一个模块级 MAX_EDGE",
          "GRADE_MAX_EDGE" in ai
          and re.search(r"(?m)^MAX_EDGE\s*=", code_only(ai)) is None)
    check("grade_ai 提示词里 LaTeX 反斜杠是双份（单份会被 Python 吃掉）",
          "\\\\\\\\" in ai or "\\\\frac" in ai)
    check("grade_ai 不许给分：源码里没有给 score 赋值的分支",
          "score" not in code_only(ai) or "score_ignored" in ai)


# ================================================================ ② 纯函数
def t_pure() -> None:
    print("\n=== ② 纯函数：证据校验 / 解析容错 ===")
    from app.services import grade_ai as A
    check(A.verify_evidence("x=2", "由①②得 x = 2，所以"),
          "证据校验：去空白后是子串 → True")
    check(not A.verify_evidence("x=9", "由①②得 x = 2"),
          "证据校验：编造的引用 → False")
    check(A.verify_evidence("x^2=4", "所以 $x^2 = 4$ 成立"),
          "证据校验：去掉 $ 后仍能匹配")
    check(not A.verify_evidence("", "随便写的什么"),
          "证据校验：空引用一律 False（空引用等于没给依据）")
    check(A.verify_evidence("AB=AC", "因为 AB=AC\n所以"),
          "证据校验：全角空格/换行不影响")

    # 缺文件/没配置时：给 err，不抛
    items, err = A.recognize_page(str(TMP / "nope.png"), {"answer_mode": "ai"})
    check("recognize_page 遇到不存在的图 → 返回人话 err 而不是抛异常",
          items == [] and bool(err), err)
    check("recognize_page 的 err 不是堆栈（不出现 Traceback）", "Traceback" not in err, err)


# ================================================================ ③ 建任务 + 判定依据
def t_setup() -> None:
    print("\n=== ③ 建任务 + 判定依据（A1：答案是老师给的） ===")
    db = SessionLocal()
    try:
        stu = mk_student(db)
        ls = mk_lesson(db, stu)
        s1 = mk_submission(db, ls, "作业.png")
        db.commit()

        # —— 没选文件 ——
        st, msg = expect_err(G.create_grade, db, GradeIn(lesson_id=ls.id, file_ids=[]))
        check("没选提交件 → 422", st == 422, msg)

        # —— key 模式却没给答案 ——
        st, msg = expect_err(G.create_grade, db, GradeIn(
            lesson_id=ls.id, file_ids=[s1.id], answer_mode="key"))
        check("选「用我给的答案」但答案是空的 → 422（不能拿 AI 自解冒充）", st == 422, msg)
        check("422 的文案是给老师看的人话（提到答案）", "答案" in msg, msg)

        # —— rubric 模式却没给细则 ——
        st, msg = expect_err(G.create_grade, db, GradeIn(
            lesson_id=ls.id, file_ids=[s1.id], answer_mode="rubric"))
        check("选「用评分细则」但细则是空的 → 422", st == 422, msg)

        # —— 正常建（先按 AI 自解建，再在批改页改）——
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        check("建任务成功且状态是 queued（建了不自动跑）", g.status == "queued", g.status)
        check("total 是页数（一张图 = 1 页）", g.total == 1, g.total)
        check("done / failed 从 0 起", g.done == 0 and g.failed == 0)
        check("答不出分时 score 是 None，不是 0", g.score is None)
        check("归档目录已建", bool(g.dir_rel) and (config.UPLOAD_DIR / g.dir_rel).is_dir(), g.dir_rel)

        d = G.detail(db, g)
        check("详情里能回显判定依据（answer_mode + 两段文本）",
              d.get("answer_mode") == "ai" and d.get("answer_text") == "")
        check("详情里带 items / files / marks / pages",
              {"items", "files", "marks", "pages"} <= set(d))
        check("提交件是引用而非复制（lesson_file_id 指回 lesson_files）",
              d["files"][0].get("role") == "submission" and s1.id in
              [f.lesson_file_id for f in db.scalars(
                  select(HomeworkGradeFile).where(HomeworkGradeFile.grade_id == g.id)).all()])

        # —— 开批前改依据（A1 的落地口子）——
        G.patch_setup(db, g.id, GradeSetupPatch(
            answer_mode="key", answer_text="3. x=2 或 x=3\n4. 略"))
        db.refresh(g)
        check("开批前能改成「用我给的答案」", g.answer_mode == "key", g.answer_mode)
        key_text, _rub = G._answer_texts(db, g)
        check("答案文本落盘并能读回（不是只存在内存里）", "x=2 或 x=3" in key_text, key_text[:40])
        txt = list((config.UPLOAD_DIR / g.dir_rel).glob("answer.txt"))
        check("粘贴的答案在任务目录里存了一份 answer.txt（老师翻得到）", len(txt) == 1)
        d = G.detail(db, g)
        check("详情能回显刚填的答案", "x=2" in (d.get("answer_text") or ""))

        # 切回 ai 之后，回显仍然保留（respect_mode=False 的意义）
        G.patch_setup(db, g.id, GradeSetupPatch(answer_mode="ai"))
        d2 = G.detail(db, g)
        check("切回「AI 自己解」后，回显里答案还在（不然切一下就没了）",
              "x=2" in (d2.get("answer_text") or ""), d2.get("answer_text"))
        _k, _r = G._answer_texts(db, g)
        check("但喂给 AI 的依据是空（ai 模式不该塞答案）", _k == "" and _r == "")

        # 切到 key 但清空答案 → 422
        st, msg = expect_err(G.patch_setup, db, g.id,
                             GradeSetupPatch(answer_mode="key", answer_text=""))
        check("切到「用我给的答案」却把答案清空 → 422", st == 422, msg)

        # 重写答案会覆盖旧的 txt（不该攒出一堆 answer_2.txt）
        G.patch_setup(db, g.id, GradeSetupPatch(answer_mode="key", answer_text="3. x=3"))
        G.patch_setup(db, g.id, GradeSetupPatch(answer_mode="key", answer_text="3. x=2"))
        txt2 = list((config.UPLOAD_DIR / g.dir_rel).glob("answer*.txt"))
        check("反复改答案不会攒出 answer_2.txt", len(txt2) == 1, [p.name for p in txt2])
        k2, _ = G._answer_texts(db, g)
        check("反复改答案后读到的是最后一次", "x=2" in k2 and "x=3" not in k2.replace("x=2", ""), k2)

        # 开批后锁住
        g.status = "reviewing"
        g.done = 1
        db.commit()
        st, msg = expect_err(G.patch_setup, db, g.id, GradeSetupPatch(answer_mode="ai"))
        check("开始批过之后改依据 → 422（旧判定是按旧依据算的）", st == 422, msg)
        check("422 文案提示去「重新批改」", "重新批改" in msg, msg)
        g.status = "queued"
        g.done = 0
        db.commit()
    finally:
        db.close()


# ================================================================ ④ 逐题结果
def t_items() -> None:
    print("\n=== ④ 逐题结果：改字作废 / 回存题库 / 确认 ===")
    db = SessionLocal()
    try:
        stu = mk_student(db, "逐题测试学生")
        ls = mk_lesson(db, stu)
        s1 = mk_submission(db, ls)
        db.commit()
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()

        # 直接塞一条结果（模拟 AI 跑完）
        it = G._item_row(item_payload(), g.id, 1, 1)
        db.add(it)
        g.status = "reviewing"
        g.done = 1
        db.commit()

        d = G.detail(db, g)
        row = d["items"][0]
        check("逐题结果是 pending（老师没确认前不算数）", row["status"] == "pending", row["status"])
        check("flags 从 TEXT 还原成 list（不是字符串）", isinstance(row["flags"], list), row["flags"])
        check("一期不给分：score 是 None", row["score"] is None, row["score"])
        check("ungradable 能被存下来（一等状态）",
              G.patch_item(db, it.id, GradeItemPatch(verdict="ungradable")).verdict == "ungradable")

        # —— 老师改判定 ——
        G.patch_item(db, it.id, GradeItemPatch(verdict="wrong", error_reason="漏根"))
        db.refresh(it)
        check("老师能把判定改掉", it.verdict == "wrong")
        check("改判定会记 reviewed_at", bool(it.reviewed_at))

        # —— 老师改识别错的字 → 旧判定必须作废 ——
        v0 = it.transcription_version
        G.patch_item(db, it.id, GradeItemPatch(student_answer="由 $x^2-5x+6=0$ 得 $x=2,3$"))
        db.refresh(it)
        check("改学生作答 → 判定作废回 ungradable", it.verdict == "ungradable", it.verdict)
        check("改学生作答 → transcription_version +1", it.transcription_version == v0 + 1,
              f"{v0} -> {it.transcription_version}")
        check("改学生作答 → 错因清空（旧错因是按旧文本写的）", not it.error_reason, it.error_reason)
        flags = json.loads(it.flags or "[]")
        check("改学生作答 → flags 里留下 retranscribed", "retranscribed" in flags, flags)

        # 同样的文本再传一次不该再作废（否则点保存就冲掉已复核的判定）
        G.patch_item(db, it.id, GradeItemPatch(verdict="correct"))
        v1 = it.transcription_version
        G.patch_item(db, it.id, GradeItemPatch(student_answer="由 $x^2-5x+6=0$ 得 $x=2,3$"))
        db.refresh(it)
        check("文本没变就不重新作废（避免点保存冲掉已复核的判定）",
              it.transcription_version == v1 and it.verdict == "correct",
              f"v={it.transcription_version} verdict={it.verdict}")

        # —— 回存题库：只挂已有题，不自动建题 ——
        q = Question(id="q_grade_test", content="解方程 $x^2-5x+6=0$", qtype="解答题",
                     knowledge_points="[]", tags="[]")
        db.add(q)
        db.commit()
        st, msg = expect_err(G.to_bank, db, it.id, GradeToBankIn(question_id="不存在的题"))
        check("回存题库：题号不存在 → 报错（不偷偷建一道）", st == 404, msg)
        G.to_bank(db, it.id, GradeToBankIn(question_id=q.id, tags=["计算错", "漏根"]))
        db.refresh(it)
        check("回存题库：挂到已有题上", it.question_id == q.id, it.question_id)
        check("回存题库：标签写进这一题", "漏根" in (it.tags or ""), it.tags)
        db.refresh(q)
        check("回存题库：标签同步到题库那道题（不然题库筛不出来）", "漏根" in (q.tags or ""), q.tags)
        check("回存后这一题标记为 approved", it.status == "approved", it.status)

        # —— 确认 ——
        g.status = "running"          # 没批完不许确认
        db.commit()
        st, msg = expect_err(G.confirm_grade, db, g.id, GradeConfirmIn())
        check("还在跑的时候不能确认（不能确认半成品）", st == 409, (st, msg))
        g.status = "reviewing"
        db.commit()
        G.confirm_grade(db, g.id, GradeConfirmIn(note="看过一遍"))
        db.refresh(g)
        check("确认之后状态到 done", g.status == "done", g.status)
        check("确认会记 reviewed_at", bool(g.reviewed_at))
        check("确认不会把没判的分算成 0（一期本来就不给分）", g.score is None, g.score)
    finally:
        db.close()


# ================================================================ ⑤ 标注
def t_marks() -> None:
    print("\n=== ⑤ 标注：0~1000 归一化坐标 ===")
    db = SessionLocal()
    try:
        stu = mk_student(db, "标注测试学生")
        ls = mk_lesson(db, stu)
        s1 = mk_submission(db, ls)
        db.commit()
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()

        m = G.add_mark(db, g.id, GradeMarkIn(page_no=1, kind="cross",
                                            geo=[100, 200, 300, 260],
                                            text="这步错", color="#d00"))
        check("加一条标注成功", m.id > 0)
        # 一期 AI 只回文字结论、不产坐标（模型给的框实测偏高约 1/8 页），
        # 所以标注一律是老师放的 —— 等哪天模型真能给框了，这里才该出现 ai。
        check("标注归属是 teacher（一期 AI 不产坐标）", m.created_by == "teacher", m.created_by)

        # 越界坐标要被夹回 0~1000（否则会画到纸外）
        m2 = G.add_mark(db, g.id, GradeMarkIn(page_no=1, kind="box",
                                              geo=[-50, 1200, 999, 2000]))
        geo = json.loads(m2.geo or "[]")
        check("越界坐标被夹回 0~1000", all(0 <= v <= 1000 for v in geo), geo)

        G.patch_mark(db, m.id, GradeMarkPatch(geo=[10, 20, 40, 60]))
        db.refresh(m)
        check("拖动后坐标改了", json.loads(m.geo or "[]") == [10, 20, 40, 60], m.geo)
        check("拖动后归属变成老师（AI 的框只可信到「哪一带」）",
              m.created_by == "teacher", m.created_by)

        d = G.detail(db, g)
        check("详情里带出标注", len(d["marks"]) == 2, len(d["marks"]))
        check("标注有 page_no（多页要按页分组）", d["marks"][0]["page_no"] == 1)

        G.delete_mark(db, m.id)
        check("删标注成功", db.get(HomeworkGradeMark, m.id) is None)
    finally:
        db.close()


# ================================================== ⑤b 批量补发标注（退出再回来的出口）
def t_marks_batch() -> None:
    """「画了一笔就退出，记录就没了」那条的正解里，服务端这一半。

    前端做法是**先落本机草稿、再补发**（见 `frontend/src/gradeDraft.js` 文件头），
    所以这个接口必须满足三件事，缺一条就等于白做：

      · **顺序即落库顺序** —— 同一位置后画的要盖在上面。乱序落库会让老师看到
        「后画的那一笔被先画的盖住」，还以为是丢笔了。
      · **原子** —— 一批里有一条不合法（比如挂的题已经被重批掉了），
        必须**一条都不写**。半批写进去 = 界面上出现「一半的笔迹」，
        下一轮 `reconcile` 又会把整批再发一次，越滚越乱。
      · **有上限** —— 本地草稿攒了几百条时不能一口气塞进一次请求/一次事务。
    """
    print("\n=== ⑤b 批量补发标注：顺序 / 原子 / 上限 ===")
    db = SessionLocal()
    try:
        stu = mk_student(db, "补发测试学生")
        ls = mk_lesson(db, stu)
        s1 = mk_submission(db, ls, "补发.png")
        db.commit()
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()

        # 落一道题，测「挂错题」那条（item_id 属于别的作业）
        it = G._item_row(item_payload(), g.id, 1, 0)
        db.add(it)
        stu2 = mk_student(db, "补发测试学生2")
        ls2 = mk_lesson(db, stu2)
        s2 = mk_submission(db, ls2, "别人的.png")
        db.commit()
        g2 = G.create_grade(db, GradeIn(lesson_id=ls2.id, file_ids=[s2.id], answer_mode="ai"))
        db.commit()
        other = G._item_row(item_payload(), g2.id, 1, 0)
        db.add(other)
        db.commit()

        before = len(G.detail(db, g)["marks"])
        made = G.add_marks(db, g.id, GradeMarksIn(marks=[
            GradeMarkIn(page_no=1, kind="check", geo=[100, 100, 30, 30]),
            GradeMarkIn(page_no=1, kind="cross", geo=[200, 200, 30, 30]),
            GradeMarkIn(page_no=1, kind="text", geo=[300, 300], text="步骤跳了"),
        ]))
        check("三条一次补发成功", len(made) == 3, len(made))
        check("库里的标注多了三条", len(G.detail(db, g)["marks"]) == before + 3,
              len(G.detail(db, g)["marks"]))

        # 顺序：id 递增 == 传入顺序（同秒写入，created_at 分不出来，只能看自增 id）
        ids = [m.id for m in made]
        check("★ 补发的顺序就是落库顺序（后画的盖在上面）", ids == sorted(ids), ids)
        got = {json.loads(m.geo)[0]: m.kind for m in made}
        check("★ 三笔的坐标没串位", got.get(100) == "check" and got.get(200) == "cross"
              and got.get(300) == "text", got)

        # 共用 `_mark_row`：批量补发的坐标同样要夹在 0~1000
        one = G.add_marks(db, g.id, GradeMarksIn(marks=[
            GradeMarkIn(page_no=1, kind="box", geo=[-9, 1200, 40, 80]),
        ]))[0]
        geo = json.loads(one.geo or "[]")
        check("★ 批量走的也是同一个构造器（越界坐标一样夹住）",
              all(0 <= v <= 1000 for v in geo), geo)

        # 空批：前端 `reconcile` 每轮都会调用，绝不能炸
        check("空批直接返回 []（前端每轮都会调，不许抛）",
              G.add_marks(db, g.id, GradeMarksIn(marks=[])) == [])

        # 原子性：混进一条挂别人题的 → 422，且**一条都不许落库**
        n_before = len(G.detail(db, g)["marks"])
        code, msg = expect_err(G.add_marks, db, g.id, GradeMarksIn(marks=[
            GradeMarkIn(page_no=1, kind="check", geo=[10, 10, 20, 20]),
            GradeMarkIn(page_no=1, kind="check", geo=[20, 20, 20, 20], item_id=other.id),
        ]))
        check("挂到别的作业的题上 → 422", code == 422, code)
        check("错误话说清了（不是一句 Internal Server Error）", "不属于这份" in msg, msg)
        check("★ 整批被拒 → 一条都没落库（半批写进去会让笔迹越滚越多）",
              len(G.detail(db, g)["marks"]) == n_before, len(G.detail(db, g)["marks"]))

        # 上限：MAX_MARKS_PER_BATCH 之上要拒
        cap = G.MAX_MARKS_PER_BATCH
        code2, _ = expect_err(G.add_marks, db, g.id, GradeMarksIn(marks=[
            GradeMarkIn(page_no=1, kind="check", geo=[1, 1, 2, 2])
            for _ in range(cap + 1)
        ]))
        check(f"超过 {cap} 条一次补发 → 422", code2 == 422, code2)
        check("恰好到上限就放行（边界别多拦一条）",
              len(G.add_marks(db, g.id, GradeMarksIn(marks=[
                  GradeMarkIn(page_no=1, kind="check", geo=[1, 1, 2, 2])
                  for _ in range(cap)
              ]))) == cap)
    finally:
        db.close()


# ================================================================ ⑥ 跑一页（AI 用桩）
def t_run_with_stub() -> None:
    print("\n=== ⑥ 跑一整份（AI 层用桩，不联网不花钱） ===")
    db = SessionLocal()
    try:
        stu = mk_student(db, "跑批测试学生")
        ls = mk_lesson(db, stu)
        s1 = mk_submission(db, ls, "作业1.png")
        db.commit()
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()

        calls = {"n": 0}
        orig = G._recognize_page

        def fake(png, ctx):
            calls["n"] += 1
            check("跑批时 ctx 里带上了 answer_mode", ctx.get("answer_mode") == "ai", ctx)
            # 留一点窗口：好让「正在跑的时候再点一次」这件事真的被验证到
            time.sleep(0.5)
            return ([item_payload(q_no="1", verdict="correct", error_reason=""),
                     item_payload(q_no="2", verdict="ungradable",
                                  basis="", error_reason="字迹看不清")], "")

        G._recognize_page = fake
        try:
            G.launch(db, g)
            # 立刻再点一次：这时线程还在跑，必须被挡住（前端连点两下不该跑两遍）
            again = G.launch(db, g)
            check("正在跑的时候再点一次 → 不会重复开线程", again is False, again)
            ok = False
            for _ in range(150):                      # 最多等 15 秒
                db.refresh(g)
                if g.status in ("reviewing", "done", "partial", "failed"):
                    ok = True
                    break
                time.sleep(0.1)
            check("批改线程跑完了（没有卡在 running）", ok, g.status)
            check("跑完状态是 reviewing（等人核对，不是直接 done）",
                  g.status == "reviewing", g.status)
            check("done 按页计（1 页 = 1）", g.done == 1, g.done)
            check("1 页只调了 1 次 AI（幂等真的生效，不是靠运气）",
                  calls["n"] == 1, calls["n"])
            items = db.scalars(select(HomeworkGradeItem)
                               .where(HomeworkGradeItem.grade_id == g.id)
                               .order_by(HomeworkGradeItem.seq)).all()
            check("逐题结果落库了（2 题）", len(items) == 2, len(items))
            check("题号原样保留", [i.q_no for i in items] == ["1", "2"], [i.q_no for i in items])
            check("判不了的题保留 ungradable 且带原因",
                  items[1].verdict == "ungradable" and "看不清" in (items[1].error_reason or ""),
                  (items[1].verdict, items[1].error_reason))
            check("一期一律不给分（score 为 NULL）",
                  all(i.score is None for i in items), [i.score for i in items])
        finally:
            G._recognize_page = orig

        # —— 一页失败 → partial，且错误要说得出来 ——
        g2 = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()

        def bad(png, ctx):
            return [], "这一页的输出被截断了"

        G._recognize_page = bad
        try:
            G.launch(db, g2)
            for _ in range(120):
                db.refresh(g2)
                if g2.status in ("reviewing", "partial", "failed", "done"):
                    break
                time.sleep(0.1)
            check("整页没批成 → 状态是 failed 或 partial（不是静默成功）",
                  g2.status in ("partial", "failed"), g2.status)
            check("失败原因写下来了（老师看得见）",
                  "截断" in (g2.error or ""), g2.error)
            check("失败页计入 failed", g2.failed >= 1, g2.failed)
        finally:
            G._recognize_page = orig
    finally:
        db.close()


# ================================================================ ⑦ 前端
def t_frontend() -> None:
    print("\n=== ⑦ 前端：接线 / 零构建陷阱 / 模板编译 ===")
    view = read(FE / "src" / "views" / "HomeworkGrade.js")
    layer = read(FE / "src" / "components" / "GradeMarkLayer.js")
    api = read(FE / "src" / "api.js")
    router = read(FE / "src" / "router.js")
    hw = read(FE / "src" / "components" / "HomeworkDialog.js")
    css = read(FE / "app.css")

    check("批改台视图存在", len(view) > 2000, len(view))
    check("api.js 有 gradeApi", "export const gradeApi" in api)
    check("gradeApi 有 patchSetup（开批前改判定依据）", "patchSetup" in api)
    check("api.js 里 systemApi.info 还在（别误删既有行）", "info:" in api)
    check("router.js 注册了 /grade/:id", "/grade/:id" in router)
    check("作业弹窗能发起批改（emit grade）", "'grade'" in hw or '"grade"' in hw)
    check("作业弹窗有「保存并批改」按钮", "批改" in hw)
    check("学生页接住了 @grade", "@grade" in read(FE / "src" / "views" / "StudentDetail.js"))
    check("工作台接住了 @grade", "@grade" in read(FE / "src" / "views" / "Dashboard.js"))
    check("app.css 有判改台样式", ".grade-wrap" in css and ".grade-setup" in css)

    # 零构建陷阱：自定义组件不能自闭合。
    # ⚠️ 必须看代码不看注释 —— 源码里解释这个坑的那行注释本身就写着 `<GradeMarkLayer />`，
    #    用原文断言会被它弄红（这个项目踩过好几次）。
    tpl_code = code_only(view)
    check("GradeMarkLayer 成对写（自闭合会把后面的节点吞掉、整页白屏）",
          "</GradeMarkLayer>" in tpl_code
          and re.search(r"<GradeMarkLayer[^>]*/>", tpl_code) is None)
    check("标注层组件存在且不是空的", "template:" in layer)

    # 渲染容器：空 div + 函数 ref，容器里不能有插值
    check("用 renderRichEl（渲染管线唯一出处）", "renderRichEl" in view)
    check("渲染容器用函数 ref", "setRich" in view)
    ref_tags = re.findall(r'<div[^>]*:ref="el => setRich\([^"]*"[^>]*>(.*?)</div>', view, re.S)
    check("渲染容器里没有 {{ }} 插值（KaTeX 会把节点换掉）",
          all("{{" not in t for t in ref_tags), [t.strip()[:40] for t in ref_tags][:3])
    check("至少有一个空渲染容器", any(not t.strip() for t in ref_tags), len(ref_tags))

    # ★ 「在不在跑」的判据（2026-10-17 用户实际踩到**两次**）：
    #   ① 新建任务后 status 就是 queued。若把 queued 也算「在跑」，按钮永久 disabled、
    #      页面上还写着「排队中」→ 老师点不动、只能干等，表现成「跑了半天没出来」。
    #   ② 第一版修法改成「服务端说 running **或本地正在轮询**」→ 从「保存并批改」跳进来
    #      状态就是 queued，一挂载就 startPolling → polling=true → 按钮又变灰。
    #      同一个 bug 换个地方复发。现在只认「服务端说 running」或「本次会话点过开始批改」。
    #   注意 queued 仍要留在 RUNNING 里（刚点开批的一瞬间服务端还来不及写 running，
    #   否则轮询会立刻停），所以只能约束「用哪个判据」，不能禁止 queued 出现。
    m_run = re.search(r"const running = computed\(\(\) => \{(.*?)\n    \}\);", view, re.S)
    blk_run = m_run.group(1) if m_run else ""
    check("在不在跑只认 running / 本次点过开始批改，**不认轮询标志**",
          "runAsked" in blk_run and "status === 'running'" in blk_run
          and "polling.value" not in blk_run, blk_run.strip()[:80])
    m_sp = re.search(r"function startPolling\(\) \{(.*?)\n    \}", view, re.S)
    blk_sp = m_sp.group(1) if m_sp else ""
    check("queued 不自动轮询（startPolling 只跟 running 走）",
          "running.value" in blk_sp and "RUNNING.includes" not in blk_sp,
          blk_sp.strip()[:80])
    check("queued 不说「排队中」（会让人以为已经在跑，其实没人启动）",
          "排队中" not in tpl_code and "未开始" in tpl_code)
    check("顶栏说清「还没开始，点右边按钮」（不点永远不会跑）",
          "还没开始批改" in tpl_code)
    check("「还没开始」那句要排除 running（刚点开批时服务端也还是 queued）",
          'v-if="!running && detail.status === \'queued\'' in tpl_code)

    # ★ 用户 2026-10-17 明确要求：「点保存并批改只做跳转，我点开始批改才开始」
    #   所以两个入口只建任务 + 跳转；**唯一**真正开批的地方是批改页的 run()。
    for f, label in ((FE / "src" / "views" / "StudentDetail.js", "学生页"),
                     (FE / "src" / "views" / "Dashboard.js", "工作台")):
        src = read(f)
        m = re.search(r"async function onGrade\([^)]*\) \{(.*?)\n    \}", src, re.S)
        seg = m.group(1) if m else ""
        check(f"{label}的「保存并批改」只建任务 + 跳转，不自动开批",
              "gradeApi.create" in seg and "gradeApi.run" not in seg,
              seg.strip()[:70])
    m_mount = re.search(r"onMounted\(async \(\) => \{(.*?)\n    \}\);", view, re.S)
    blk_mount = m_mount.group(1) if m_mount else ""
    check("批改页挂载时不自动开批（只 load + startPolling）",
          "gradeApi.run" not in blk_mount and "load()" in blk_mount,
          blk_mount.strip()[:70])

    # 判定枚举必须与后端一致（踩过：前端传 reviewed，后端只认 approved）
    check("核对状态用 approved（不是自造的 reviewed）",
          "'approved'" in view and "reviewed'" not in view)

    # 一期只判对错：界面上不能出现「给分」入口
    check("界面写明「不给分」", "不给分" in view)
    check("判不了必须能显示原因（不能只显示一个灰点）", "naReason" in view)

    # 文件选择不能设 accept（会挡掉 .lnk 之类）
    check("文件 input 没设 accept", "accept=" not in view and "accept=" not in layer)

    # ★ 手写批改（2026-10-17 用户要求：能画 / 能擦 / 能放文本框 / 手动批改 / 导出）
    check("工具条有画笔 / 橡皮 / 文字框",
          all(k in tpl_code for k in ("画笔", "橡皮", "文字框")))
    check("还有 √ / × 快放（判对错是最高频动作，不该逼老师先画框）",
          "setTool('check')" in tpl_code and "setTool('cross')" in tpl_code)
    check("标注层接了指针事件（画不画得出来全看这三条）",
          all(ev in layer for ev in ("@pointerdown", "@pointermove", "@pointerup")))
    check("标注层把笔迹 / 删除 / 文字 / 快放交出去",
          all(k in layer for k in ("emit('create'", "emit('erase'",
                                   "emit('text-at'", "emit('stamp'")))
    check("父组件把模式与颜色传给标注层",
          ':mode="tool"' in tpl_code and ':color="inkColor"' in tpl_code)
    check("父组件接住那四个事件",
          all(k in tpl_code for k in ('@create="addMark"', '@erase="eraseMarks"',
                                      '@text-at="onTextAt"', '@stamp="addMark"')))
    check("笔迹做了抽稀（一笔几千点会把 mark 表撑爆）", "MIN_STEP" in layer)
    check("橡皮按线段距离命中（用包围盒会擦掉一大片）",
          "segDist(" in layer and "function segDist(" not in layer)   # 用的是 markGeom 里那份
    check("坐标换算只走 ../markGeom.js（组件里不许再写一套 clientX / rect）",
          "from '../markGeom.js'" in layer and "pagePoint(" in layer
          and "clamp(((e.clientX" not in layer, "")
    check("图片不参与拖拽（拖一下就断了画笔）", 'draggable="false"' in tpl_code)
    css = read(FE / "app.css")
    check("画/擦模式下整层接事件，同时标注失去指针",
          ".gmark-layer.mode-ink .gmark" in css
          and re.search(r"\.gmark-layer\.mode-ink[^{]*\{[^}]*pointer-events:\s*auto", css) is not None,
          "")
    check("顶栏有「导出 PDF」与「本页图」", "导出 PDF" in tpl_code and "本页图" in tpl_code)
    api_src = read(FE / "src" / "api.js")
    check("导出地址拼成 /export?fmt=&page=",
          "exportUrl" in api_src and "export?fmt=" in api_src)
    exp_src = code_only(read(BE / "app" / "services" / "grade_export.py"))
    check("导出服务不碰 pymupdf（AGPL 只能待在 adapters/pdf.py）",
          "pymupdf" not in exp_src and "fitz" not in exp_src)
    check("导出按 x/y 分别缩放（同一个比例会把字压扁）",
          "kx, ky = w / 1000.0, h / 1000.0" in exp_src)

    # ★ 拖动标注改位置（2026-10-18 用户要求：「这个文本框要能够自由拖动」）
    #   反面断言一律看代码不看注释（本项目踩过：解释坑的那行注释把自己弄红）。
    layer_code = code_only(layer)
    view_code = code_only(view)
    check("标注层接上了拖动三件套 + cancel（手势被打断要能退回）",
          all(k in layer_code for k in ("@pointerdown=\"startDrag($event, s)\"",
                                       "@pointermove=\"dragTo\"",
                                       "@pointerup=\"endDrag\"",
                                       "@pointercancel=\"cancelDrag\"")))
    m_mk = re.search(r"const MOVE_KINDS = \[(.*?)\]", layer_code)
    check("能拖的只有框类，笔迹 ink 不在里面（点列几百点，一来一回太大）",
          bool(m_mk) and "ink" not in m_mk.group(1)
          and all(k in m_mk.group(1) for k in ("text", "check", "cross", "box")),
          m_mk.group(1) if m_mk else "没找到 MOVE_KINDS")
    check("拖动位移走 markGeom.shiftGeo（组件里再写一套 map 加 dx，x/y 会串）",
          "shiftGeo(" in layer_code
          and not re.search(r"geo\.map\(\s*\(?\w+\)?\s*=>", layer_code), "")
    check("拖动中只改本地 offsets，松手才 emit 一次（每帧一个请求会拖垮 SQLite）",
          "offsets.value = next" in layer_code and layer_code.count("emit('move'") == 1,
          layer_code.count("emit('move'"))
    check("手抖阈值：没动够 DRAG_MIN 不算拖动（否则点一下标注就被挪走）",
          "DRAG_MIN" in layer_code and "Math.hypot(px, py) < DRAG_MIN" in layer_code)
    check("拖完紧跟的那一下 click 要被吞掉（不然拖个框顺手把右栏跳走）",
          "suppressPickUntil" in layer_code)
    check("本地位移按 geo 快照自动退场（服务端回来了就不该再叠加）",
          "geoKey" in layer_code and "base" in layer_code)
    check("提交失败能把本地位移退回（不能停在「看着拖走了、库里没变」）",
          "expose({ clearOffsets })" in layer_code)
    check("父组件接住拖动并 PATCH geo（坐标不在父组件再算一遍）",
          '@move="moveMark"' in tpl_code and "patchMark(mark.id, { geo })" in view_code)
    check("父组件 PATCH 失败时调标注层回滚", "clearOffsets" in view_code
          and 'ref="layerEl"' in tpl_code)
    check("可拖的标注给抓手光标（不给提示没人知道能拖）", ".gmark.mv { cursor: grab; }" in css)
    check("文字框拖动时不让浏览器选字 / 拖图片", "user-select: none" in css
          and "touch-action: none" in css)
    check("界面上写明能拖动", "拖动挪位置" in tpl_code)

    # 轮询要在结束时停掉
    check("轮询有停表逻辑（不会一直打着接口）", "stopPolling" in view and "clearInterval" in view)

    # —— 模板真编译（Vue.compile 需要 document；stub 太简陋会抛 undefined[0]）——
    node = shutil.which("node") or "node"
    tpl = extract_template(view)
    check("能抠出 template", bool(tpl) and tpl.rstrip().endswith(">"), (tpl or "")[-50:])
    if not tpl:
        return
    vue_js = FE / "vendor" / "vue.global.prod.js"
    if not vue_js.exists():
        print("  [SKIP] 没有 vendor/vue.global.prod.js，跳过模板编译")
        return
    js = TMP / "compile_check.js"
    js.write_text(
        "const fs=require('fs'),vm=require('vm');\n"
        f"const VUE={str(vue_js)!r};\n"
        "function mkDoc() {\n"
        "  const el=()=>{const o={_raw:'',_foo:'',_text:''};\n"
        "    Object.defineProperty(o,'innerHTML',{set(v){o._raw=v;const m=/foo=\"([\\s\\S]*)\"/.exec(v);"
        "o._foo=m?m[1]:'';o._text=v;},get(){return o._raw;}});\n"
        "    Object.defineProperty(o,'children',{get(){const dec=s=>String(s).replace(/&quot;/g,'\"')"
        ".replace(/&#39;/g,\"'\").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');\n"
        "      return [{getAttribute:()=>dec(o._foo)}];}});\n"
        "    Object.defineProperty(o,'textContent',{get(){return String(o._text)"
        ".replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'\"');}});\n"
        "    return o;};\n"
        "  return {createElement:()=>el(),querySelector:()=>null,createTextNode:()=>({})};\n"
        "}\n"
        "const ctx={console,document:mkDoc(),window:{}};vm.createContext(ctx);\n"
        "vm.runInContext(fs.readFileSync(VUE,'utf8'),ctx);\n"
        "function comp(tpl){try{ctx.Vue.compile(tpl);return 'OK';}"
        "catch(e){return 'FAIL '+String(e.message).slice(0,160);}}\n"
        "console.log('SELFTEST ' + comp('<div><template v-else-if=\"b\">x</template></div>'));\n"
        "console.log('SELFTEST_GOOD ' + comp('<div><p v-if=\"a\">1</p><p v-else>2</p></div>'));\n"
        "for (const f of process.argv.slice(2)) {\n"
        "  const nm = require('path').basename(f);\n"
        "  console.log('TPL ' + nm + ' ' + comp(fs.readFileSync(f,'utf8')));\n"
        "}\n",
        encoding="utf-8")
    tpl_file = TMP / "grade_template.html"
    tpl_file.write_text(tpl, encoding="utf-8")
    layer_tpl = extract_template(layer)
    layer_tpl_file = TMP / "layer_template.html"
    layer_tpl_file.write_text(layer_tpl or "", encoding="utf-8")
    try:
        r = subprocess.run([node, str(js), str(tpl_file), str(layer_tpl_file)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
        out = (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  [SKIP] 跑不了 node：{e}")
        return
    check("自证：故意写错的模板必须报错（否则校验在空转）",
          re.search(r"SELFTEST FAIL", out) is not None, out[-200:])
    check("自证：正常 v-if/v-else 能编译",
          re.search(r"SELFTEST_GOOD OK", out) is not None, out[-200:])
    check("批改台的模板编译通过",
          re.search(r"TPL grade_template\.html OK", out) is not None, out[-300:])
    check("标注层的模板编译通过（笔迹模式一改就白屏，得逐版编译）",
          re.search(r"TPL layer_template\.html OK", out) is not None, out[-300:])

    # ⚠️ **模板字面量里不能出现反引号**（写在 HTML 注释里也算）。
    #    它会把模板提前截断：剩下的半截 `xxx`.foo - regions 变成一个合法的 JS 表达式，
    #    于是 —— `node --check` 语法照样过、`Vue.compile` 也照样过（它只看到被截短的那一小段，
    #    而那一小段本身是合法的），**只有真挂载时才炸** `ReferenceError: regions is not defined`。
    #    2026-10-19 一小时内踩到两次（`id` 和 `.gmark-regions` 各一次），都是这么漏过去的。
    #    判据：`template:` 之后（含收尾那个）反引号**只能是 2 个**（开一个、闭一个）。
    for src_text, label in ((view, "批改台"), (layer, "标注层")):
        head = src_text.find("template: `")
        ticks = src_text[head:].count("`") if head >= 0 else -1
        check(f"{label}的模板字面量里没有多余反引号（有就会静默截断模板）", ticks == 2,
              f"template 之后有 {ticks} 个反引号")

    # node --check（语法）。⚠️ `--input-type=module` 只认 stdin，不能跟文件路径
    # （会报 ERR_INPUT_TYPE_NOT_ALLOWED）；而直接对 .js 跑 --check 会按 CommonJS 解析，
    # 遇到 import 就报错。所以把源码喂进 stdin。
    for f, label in ((FE / "src" / "views" / "HomeworkGrade.js", "批改台"),
                     (FE / "src" / "components" / "GradeMarkLayer.js", "标注层"),
                     (FE / "src" / "markGeom.js", "标注几何"),
                     (FE / "src" / "api.js", "api.js"),
                     (FE / "src" / "router.js", "router.js")):
        try:
            src = read(f)
            r = subprocess.run([node, "--input-type=module", "--check"], input=src,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
            check(r.returncode == 0, f"{label} 语法通过", (r.stderr or "")[-200:])
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  [SKIP] {label} 的 node --check 没跑成：{e}")

    # ---- 标注几何：node 直接量（纯函数，不用建浏览器） ----
    geo_check = FE / "mark_geom_check.js"
    check("有标注几何的 node 单测", geo_check.exists(), str(geo_check))
    if geo_check.exists():
        try:
            r = subprocess.run([node, str(geo_check)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
            check("标注几何单测全过（坐标换算 / 点到线段 / 包围盒）",
                  r.returncode == 0, (r.stdout or r.stderr or "")[-400:])
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  [SKIP] mark_geom_check 没跑成：{e}")

    # ---- 批改台真挂载（jsdom 挂 HomeworkGrade.js，数渲染痕迹）----
    # 上面那一层（模板编译 + node --check + 静态断言）**证明不了**三件事，
    # 而这三件事恰好是这一屏最容易坏的地方：
    #   ① 首次打开时题干/作答是不是**空的**（paintAll 跑在 loading 关掉之前 → 容器不在 DOM 里）
    #   ② 框题模式下服务器上的框、刚画的草稿框，是不是真画出来了、删中间那个会不会删错格子
    #   ③ 图上读到的（figure_note）有没有上屏、没有这道题的卡片会不会多出空壳
    # 找不到 jsdom 就跳过（脚本自己退 0），不会把回归弄红。
    mount_check = FE / "grade_mount_check.js"
    check("有批改台的真挂载检查", mount_check.exists(), str(mount_check))
    if mount_check.exists():
        try:
            r = subprocess.run([node, str(mount_check)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            check("批改台真挂载全过（首次渲染 / 框题 / 图上读数 / 判定接线）",
                  r.returncode == 0, " | ".join(tail[-6:]))
        except (OSError, subprocess.SubprocessError) as e:
            print(f"  [SKIP] grade_mount_check 没跑成：{e}")


def extract_template(src: str) -> str | None:
    """把 `template: \\`...\\`` 那段抠出来（SPA 视图都这么写）。传的是文件内容，不是路径。"""
    key = "template: `"
    i = src.find(key)
    if i < 0:
        return None
    j = src.find("`", i + len(key))
    return None if j < 0 else src[i + len(key):j]


# ================================================================ ⑧ 手写批改 + 导出
def t_ink_export() -> None:
    print("\n=== ⑧ 手写批改（画 / 擦 / 文字框）+ 导出副本 ===")
    import hashlib

    from PIL import Image

    from app.services import grade_export as E

    db = SessionLocal()
    try:
        stu = mk_student(db, "手写批改学生")
        ls = mk_lesson(db, stu)
        lf = mk_submission(db, ls, "手写.png")
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[lf.id], answer_mode="ai"))

        def det() -> dict:
            """每次都重新取一遍详情 —— 直接读 g 上的关系会拿到过期的 session 状态。"""
            return G.detail(db, G.get_grade(db, g.id))

        # ---- 笔迹：点列必须原样存回来（存成框就变成「画了个矩形」） ----
        m = G.add_mark(db, g.id, GradeMarkIn(page_no=1, kind="ink",
                                            points=[100, 100, 300, 120, 500, 400],
                                            geo=[100, 100, 500, 400], color="#dc2626"))
        check("笔迹（ink）能入库", m.kind == "ink" and m.id > 0)
        one = [x for x in det()["marks"] if x["id"] == m.id][0]
        check("笔迹的点列原样存回来（不是被压成一个框）",
              one["points"] == [100, 100, 300, 120, 500, 400], one["points"])
        check("笔迹归属 teacher（撤销/清本页只退老师的）",
              one["created_by"] == "teacher", one["created_by"])

        m2 = G.add_mark(db, g.id, GradeMarkIn(page_no=1, kind="ink",
                                             points=[-50, 2000], geo=[-10, -10, 1200, 1200]))
        two = [x for x in det()["marks"] if x["id"] == m2.id][0]
        check("越界的笔迹点被夹回 0~1000（否则会画到纸外）",
              two["points"] == [0, 1000], two["points"])

        png = Path(G.page_png(db, g.id, 1))
        check("页图渲染出来了（导出的前提）", png.exists() and png.stat().st_size > 0, str(png))

        # 文字框 + √：把三种绘制分支都走一遍
        G.add_mark(db, g.id, GradeMarkIn(page_no=1, kind="text", geo=[40, 240, 40, 240],
                                        text="第 3 步漏了讨论 x=0", color="#2563eb"))
        G.add_mark(db, g.id, GradeMarkIn(page_no=1, kind="check", geo=[700, 600, 760, 660]))

        src = config.UPLOAD_DIR / lf.stored
        h0 = hashlib.md5(src.read_bytes()).hexdigest()

        before = Image.open(png).convert("RGB")
        out_png, name = E.export_grade(db, g.id, "png", 1)
        check("导出 PNG 生成了", Path(out_png).exists(), str(out_png))
        check("文件名带学生名与页码", "手写批改学生" in name and "第1页" in name, name)
        after = Image.open(out_png).convert("RGB")
        check("导出图尺寸与页图一致（没有偷偷缩放）", after.size == before.size,
              (before.size, after.size))

        w, h = before.size
        diff = sum(1 for y in range(h) for x in range(w)
                   if before.getpixel((x, y)) != after.getpixel((x, y)))
        check(f"导出图上真的有墨迹（差异像素 {diff} 个）", diff > 0, diff)

        def near(img, nx, ny, rgb, tol=70, r=4):
            """0~1000 坐标附近的像素里有没有这个颜色（给抗锯齿留半径）。"""
            px, py = int(nx / 1000 * img.size[0]), int(ny / 1000 * img.size[1])
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    qx, qy = px + dx, py + dy
                    if 0 <= qx < img.size[0] and 0 <= qy < img.size[1]:
                        p = img.getpixel((qx, qy))
                        if all(abs(p[i] - rgb[i]) <= tol for i in range(3)):
                            return True
            return False

        check("红笔那条线真的落在坐标处（不是画在 0,0）",
              near(after, 300, 120, (220, 38, 38)))
        check("√ 用绿色画（与前端 COLORS.check 同一个绿）",
              near(after, 730, 630, (22, 163, 74)))
        check("文字框带半透明白底（扫描件底色深也读得清）",
              near(after, 45, 232, (254, 254, 254), tol=6, r=14))

        # ★ 这条是「不烧回原件」的硬保证
        h1 = hashlib.md5(src.read_bytes()).hexdigest()
        check("★ 原件一个字节都没动（标注只存坐标，导出另存副本）", h0 == h1, f"{h0} -> {h1}")

        out_pdf, pname = E.export_grade(db, g.id, "pdf")
        head = Path(out_pdf).read_bytes()[:5]
        check("导出 PDF 生成了，且是 PDF（%PDF 头）", head.startswith(b"%PDF"), head)
        check("PDF 文件名是 .pdf 且带学生名",
              pname.endswith(".pdf") and "手写批改学生" in pname, pname)
        check("PDF 不是空壳（页图真被写进去了）", Path(out_pdf).stat().st_size > 800,
              Path(out_pdf).stat().st_size)

        outdir = Path(out_png).parent
        n0 = len(list(outdir.iterdir()))
        E.export_grade(db, g.id, "png", 1)
        check("反复导出不堆垃圾（同名覆盖）", len(list(outdir.iterdir())) == n0,
              f"{n0} -> {len(list(outdir.iterdir()))}")

        st, msg = expect_err(E.export_grade, db, g.id, "png", 9)
        check("导出页码越界 → 422 人话（不能静默导另一页）",
              st == 422 and "没有第" in msg, msg)

        # ---- 橡皮 = 删掉那条 mark ----
        n_before = len(det()["marks"])
        G.delete_mark(db, m.id)
        n_after = len(det()["marks"])
        check("橡皮删掉一条 mark（整笔消失，不是擦出一段缺口）",
              n_after == n_before - 1, f"{n_before} -> {n_after}")
    finally:
        db.close()


# ================================================================ ⑨ 一题一区域
def t_regions() -> None:
    """一题一区域（2026-10 调研后加的第二条口径）。

    守的四件事：

      1. **不画框的人零影响**：有框才按框批，没框仍然整页一次判。
      2. **题号由框绑定，不信模型认的题号**。学生把第 3 题写成第 8 题、
         模型把 6 读成 8，都不该影响归属 —— 否则「判错人」比「判错分」糟得多。
      3. **单框失败不毁整页**，但要留占位（ungradable + region_empty），
         不能让老师看到「怎么少了一题」还以为是自己漏画了框。
      4. **坐标是 0~1000 的归一化整数**，太小/越界要被挡掉，不能静默画到纸外。
    """
    print("\n=== ⑨ 一题一区域（按框裁切、按框批） ===")

    # ---------------- 纯几何校验（不碰库） ----------------
    check("正常框原样通过", G._region_box([100, 200, 500, 600]) == [100, 200, 500, 600])
    check("左上右下写反了自动纠正", G._region_box([500, 600, 100, 200]) == [100, 200, 500, 600],
          G._region_box([500, 600, 100, 200]))
    check("越界坐标夹回 0~1000", G._region_box([-50, -20, 1200, 1100]) == [0, 0, 1000, 1000],
          G._region_box([-50, -20, 1200, 1100]))
    check("太小的框判无效（不是凑合能用）", G._region_box([10, 10, 20, 20]) is None)
    check("不是 4 个数判无效", G._region_box([1, 2, 3]) is None)
    check("非数字判无效", G._region_box(["a", 1, 2, 3]) is None)

    db = SessionLocal()
    try:
        stu = mk_student(db, "框题测试学生")
        ls = mk_lesson(db, stu)
        s1 = mk_submission(db, ls, "框题.png")
        db.commit()
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()

        # ---------------- 存框 / 整页替换 ----------------
        out = G.put_regions(db, g.id, GradeRegionsPut(regions=[
            GradeRegionIn(q_no="1", box=[50, 100, 900, 300]),
            GradeRegionIn(q_no="2", box=[50, 320, 900, 520]),
            GradeRegionIn(q_no="", box=[50, 540, 900, 700]),
        ]))
        check("三个框都存下来了", len(out) == 3, len(out))
        check("框的顺序 = 老师画的顺序（seq 从 0 起）",
              [r["seq"] for r in out] == [0, 1, 2], [r["seq"] for r in out])
        check("题号原样保留", [r["q_no"] for r in out] == ["1", "2", ""], [r["q_no"] for r in out])
        check("没填题号的框也有可读标签（退回它的序号）", out[2]["label"] == "3", out[2]["label"])
        check("框坐标规整成整数", out[0]["box"] == [50, 100, 900, 300], out[0]["box"])
        check("detail 里能拿到 regions（前端要靠它画框）",
              len(G.detail(db, g)["regions"]) == 3)

        out2 = G.put_regions(db, g.id, GradeRegionsPut(regions=[
            GradeRegionIn(q_no="1", box=[40, 90, 940, 380]),
        ]))
        check("再存一次是**整页替换**（3 → 1，不叠加）", len(out2) == 1, len(out2))
        out3 = G.put_regions(db, g.id, GradeRegionsPut(regions=[]))
        check("传空数组 = 清空这一页的框（退回整页批）", out3 == [], out3)

        # ---------------- 拒绝无效输入 ----------------
        st, msg = expect_err(G.put_regions, db, g.id, GradeRegionsPut(regions=[GradeRegionIn(box=[10, 10, 20, 20])]))
        check("框太小 → 422", st == 422, (st, msg))
        check("并且说人话（指出第几个框、要框住一整题）",
              "一整道题" in msg and "第 1 个框" in msg, msg)

        g.total = 1
        db.commit()
        st, msg = expect_err(G.put_regions, db, g.id, GradeRegionsPut(
            regions=[GradeRegionIn(page_no=2, box=[50, 100, 900, 300])]))
        check("页号超出这份作业 → 422", st == 422 and "只有 1 页" in msg, (st, msg))

        g.status = "running"
        db.commit()
        st, msg = expect_err(G.put_regions, db, g.id, GradeRegionsPut(regions=[]))
        check("正在批改时不许改框 → 409", st == 409, (st, msg))
        g.status = "reviewing"
        db.commit()

        # ---------------- 沿用同课时另一份的框 ----------------
        s2 = mk_submission(db, ls, "框题2.png")
        db.commit()
        g2 = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s2.id], answer_mode="ai"))
        db.commit()
        G.put_regions(db, g.id, GradeRegionsPut(regions=[
            GradeRegionIn(q_no="1", box=[50, 100, 900, 300]),
            GradeRegionIn(q_no="2", box=[50, 320, 900, 520]),
        ]))
        copied = G.copy_regions(db, g2.id, g.id)
        check("能沿用同课时另一份的框", len(copied) == 2, len(copied))
        check("沿用时题号一起过来（不是只剩坐标）",
              [r["q_no"] for r in copied] == ["1", "2"], [r["q_no"] for r in copied])
        check("沿用时坐标一致", copied[0]["box"] == [50, 100, 900, 300], copied[0]["box"])
        st, _msg = expect_err(G.copy_regions, db, g2.id, g2.id)
        check("从自己这一份抄 → 422", st == 422, st)

        stu2 = mk_student(db, "另一个班的学生")
        ls2 = mk_lesson(db, stu2)
        s3 = mk_submission(db, ls2, "别的卷.png")
        db.commit()
        g3 = G.create_grade(db, GradeIn(lesson_id=ls2.id, file_ids=[s3.id], answer_mode="ai"))
        db.commit()
        st, _msg = expect_err(G.copy_regions, db, g3.id, g.id)
        check("跨课时不许沿用（版面不一样）→ 422", st == 422, st)

        # ---------------- 裁图：真的裁出一块并放大 ----------------
        page = TMP / "page_for_crop.png"
        make_png(page, 400, 600)
        tile = G._crop_region(str(page), [200, 200, 500, 400], TMP / "crops", 0)
        check("按框裁出了一张图", tile is not None and Path(tile).exists(), tile)
        from PIL import Image
        with Image.open(page) as im:
            W0, H0 = im.size
        with Image.open(tile) as im:
            TW, TH = im.size
        # 独立按「归一化 × 页宽」算一遍期望的像素范围（不复用被测函数的代码）
        cw = int((500 + G.REGION_PAD) / 1000 * W0) - int((200 - G.REGION_PAD) / 1000 * W0)
        ch = int((400 + G.REGION_PAD) / 1000 * H0) - int((200 - G.REGION_PAD) / 1000 * H0)
        # ⚠️ 比的是**裁切区域**，不是放大后的成品：放大 4 倍之后成品可能比整页还大，
        #    那是刻意的（有效分辨率），不是裁错。
        check("裁的是那一小块（框选区域比整页小得多）",
              cw < W0 * 0.6 and ch < H0 * 0.6, (cw, ch, W0, H0))
        check("裁完被放大（有效分辨率翻几倍，这是这条路线的全部意义）",
              TW > cw and TH > ch, (TW, cw, TH, ch))
        check("框太小/画到纸外 → 裁不出来时返回 None（不抛）",
              G._crop_region(str(page), [0, 0, 5, 5], TMP / "crops", 1) is None)

        # ---------------- 按框批：一框一次调用 ----------------
        g4 = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()
        G.put_regions(db, g4.id, GradeRegionsPut(regions=[
            GradeRegionIn(q_no="1", box=[50, 100, 900, 300]),
            GradeRegionIn(q_no="2", box=[50, 320, 900, 520]),
        ]))
        seen: list[tuple] = []
        orig_r = G._recognize_region

        def fake_region(png, ctx):
            seen.append((ctx.get("region"), ctx.get("region_label")))
            # 模型**故意**把题号读错：检验「题号由框绑定」这条是不是真的成立
            return ([item_payload(q_no="9", verdict="correct",
                                  figure_note="$AD\\perp BC$，有直角符号（确定）")], "")

        G._recognize_region = fake_region
        try:
            G.launch(db, g4)
            for _ in range(150):
                db.refresh(g4)
                if g4.status in ("reviewing", "done", "partial", "failed"):
                    break
                time.sleep(0.1)
        finally:
            G._recognize_region = orig_r
        check("有框时走的是按框批（两个框 = 两次调用，不是一次整页）",
              len(seen) == 2, len(seen))
        check("每次调用都标了 region=True（AI 层靠它换成单题口径）",
              all(s[0] is True for s in seen), seen)
        check("框的题号写进了上下文（帮模型定位，但不参与绑定）",
              [s[1] for s in seen] == ["1", "2"], seen)
        items4 = db.scalars(select(HomeworkGradeItem)
                            .where(HomeworkGradeItem.grade_id == g4.id)
                            .order_by(HomeworkGradeItem.seq)).all()
        check("★ 题号用框绑定的，不用模型读出来的（模型给的是 9）",
              [i.q_no for i in items4] == ["1", "2"], [i.q_no for i in items4])
        check("两个框仍然只算 1 页 done（done 是页数，不是题数）",
              g4.done == 1 and g4.total == 1, (g4.done, g4.total))
        check("★ figure_note 落库了（图形证据不能让老师看不见）",
              "直角符号" in (items4[0].figure_note or ""), items4[0].figure_note)
        check("figure_note 也通过接口给出去了（前端要显示）",
              "直角符号" in (G.item_dict(items4[0]).get("figure_note") or ""))

        # ---------------- 单框失败不毁整页 ----------------
        g5 = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()
        G.put_regions(db, g5.id, GradeRegionsPut(regions=[
            GradeRegionIn(q_no="1", box=[50, 100, 900, 300]),
            GradeRegionIn(q_no="2", box=[50, 320, 900, 520]),
        ]))
        seq_calls = {"n": 0}

        def half(png, ctx):
            seq_calls["n"] += 1
            if seq_calls["n"] == 1:
                return [], ""                      # 第一个框画空了
            return ([item_payload(q_no="2", verdict="wrong")], "")

        G._recognize_region = half
        try:
            G.launch(db, g5)
            for _ in range(150):
                db.refresh(g5)
                if g5.status in ("reviewing", "done", "partial", "failed"):
                    break
                time.sleep(0.1)
        finally:
            G._recognize_region = orig_r
        items5 = db.scalars(select(HomeworkGradeItem)
                            .where(HomeworkGradeItem.grade_id == g5.id)
                            .order_by(HomeworkGradeItem.seq)).all()
        check("框画空 → 仍留一条占位（不是静默少一题）", len(items5) == 2, len(items5))
        check("占位是 ungradable，**不是 wrong**（没读到 ≠ 没写）",
              items5[0].verdict == "ungradable", items5[0].verdict)
        check("占位带 region_empty 标记（界面能一眼看出是框的问题）",
              "region_empty" in json.loads(items5[0].flags or "[]"), items5[0].flags)
        check("另一半照常出结果，这一页仍算批完（不因一个框失败整页废掉）",
              g5.done == 1 and g5.failed == 0, (g5.done, g5.failed))

        # ---------------- 全失败 → 整页失败 ----------------
        g6 = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()
        G.put_regions(db, g6.id, GradeRegionsPut(regions=[
            GradeRegionIn(q_no="1", box=[50, 100, 900, 300]),
        ]))

        def allfail(png, ctx):
            return [], "接口 401 未授权"

        G._recognize_region = allfail
        try:
            G.launch(db, g6)
            for _ in range(150):
                db.refresh(g6)
                if g6.status in ("reviewing", "done", "partial", "failed"):
                    break
                time.sleep(0.1)
        finally:
            G._recognize_region = orig_r
        check("一个框都没批成 → 这一页算失败（这种情况重试整页才有意义）",
              g6.failed >= 1, g6.failed)
        check("失败原因写下来了", "401" in (g6.error or ""), g6.error)



        # ---------------- 提示词：两种口径不能串 ----------------
        from app.services import grade_ai as A
        p_page = A._prompt_for("ai", {"page_no": 1})
        p_reg = A._prompt_for("ai", {"page_no": 1, "region": True, "region_label": "第 2 题"})
        check("区域口径：明确「只输出一个」", "恰好一个" in p_reg)
        check("区域口径：q_no 交给系统绑定", "一律给空字符串" in p_reg)
        check("区域口径：带上了这个框是第几题", "第 2 题" in p_reg)
        check("整页口径里**不能**出现区域那段（否则整页只出一题）",
              "裁下来的一道题" not in p_page)
        check("整页口径里不能出现「最后再说一遍」（同上）", "最后再说一遍" not in p_page)

        # ---------------- 图形规则：老口径必须真的删掉 ----------------
        check("★ 图形证据三分档写进提示词", "看见的能作数" in p_page)
        check("★ 老口径「图形描述不得作为判错依据」已经不在源码里",
              "图形描述不得作为判错" not in code_only(read(BE / "app" / "services" / "grade_ai.py")))
        check("看不清的只作疑、没看见的不作否",
              "看不清的只作疑" in p_page and "没看见的不作否" in p_page)
        check("仍然不因画得丑扣分（这条不能被三分档带没了）",
              "画得丑" in p_page)
        check("★ 统计图表题要求先读成表再算，不许「大概这么高」",
              "先把图上的数读成一张表" in p_page and "大概这么高" in p_page)
        check("figure_note 要求标确定性", "（确定）" in p_page and "（看不清）" in p_page)

        # ---------------- 前端接线 ----------------
        view = read(FE / "src" / "views" / "HomeworkGrade.js")
        layer = read(FE / "src" / "components" / "GradeMarkLayer.js")
        api = read(FE / "src" / "api.js")
        check("批改台有「框题」按钮", "setTool('region')" in view and ">框题</button>" in view)
        check("框的草稿交给标注层画出来（:regions）", ":regions=" in view)
        check("标注层画完框能回传（@region）", '@region="addRegionBox"' in view)
        check("框题面板能保存", "gradeApi.putRegions" in view and "putRegions:" in api)
        check("能沿用同一节课另一份的框",
              "gradeApi.copyRegions" in view and "regions/copy" in api)
        check("★ 前后端的最小框边长必须同值（漂了就是前端以为能画、后端拒收）",
              f"const REGION_MIN = {G.REGION_MIN_SIDE}" in layer, G.REGION_MIN_SIDE)
        check("框题模式下已有标注失去指针（否则起手那一下就画不出框）",
              ".gmark-layer.mode-region .gmark" in read(FE / "app.css"))
    finally:
        db.close()


# ============================================ ⑨b 不送 AI（0030）+ 两个结构信号
def t_skips_and_signals() -> None:
    """「没办法指定哪些图不传给 AI」这一问的正解，以及配套的模型输出。

    要守住的四件事：

      · **跳过 ≠ 删框**。删了要重画；跳过只是「别送模型」，框还得留着能改。
      · **跨页题按整块跳过**。只跳上半段 = 只批半道题，比不批还糟。
      · **一页的块全跳过时不能退回「整页一次判」**。那是老口径的兜底，
        不拦住的结果正好相反：老师把整页都设成不判 → 整页被送给模型。
      · **模型说的「这块像页脚」只是提示，绝不自动跳过**。判错一块等于丢一道题。
    """
    print("\n=== ⑨b 不送 AI（按块 / 按页）+ 模型的两个结构信号 ===")
    db = SessionLocal()
    try:
        stu = mk_student(db, "跳过测试学生")
        ls = mk_lesson(db, stu)
        s1 = mk_submission(db, ls, "跳过.png")
        db.commit()
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()

        # ---------------- 按块跳过：落库、读回、不删框 ----------------
        out = G.put_regions(db, g.id, GradeRegionsPut(regions=[
            GradeRegionIn(q_no="1", box=[50, 100, 900, 300]),
            GradeRegionIn(q_no="", box=[50, 320, 900, 420], skipped=True),   # 结构说明段
            GradeRegionIn(q_no="3", box=[50, 460, 900, 700]),
        ]))
        check("三个框都还在（跳过不删框）", len(out) == 3, len(out))
        check("★ 第二块标了 skipped，另外两块没被带上",
              [r["skipped"] for r in out] == [False, True, False],
              [r["skipped"] for r in out])
        blocks = G.blocks_of(db, g.id)
        check("聚合到题上也是 skipped",
              [b["skipped"] for b in blocks] == [False, True, False],
              [b["skipped"] for b in blocks])

        # 跨页题：只跳了上半段也要整块跳（只批半道题更糟）
        out2 = G.put_regions(db, g.id, GradeRegionsPut(grouped=True, regions=[
            GradeRegionIn(seq=0, page_no=1, box=[50, 600, 900, 800], skipped=True),
            GradeRegionIn(seq=0, page_no=1, box=[50, 820, 900, 950]),
        ]))
        check("★ 同一题只跳了一段 → 两段都算跳（不批半道题）",
              [r["skipped"] for r in out2] == [True, True],
              [(r["page_no"], r["skipped"]) for r in out2])
        check("这一段也没被丢掉（框还在）", len(out2) == 2, len(out2))

        # ---------------- 按页跳过 ----------------
        got = G.set_page_skips(db, g.id, [1])
        check("整页跳过存下来了", got["skipped_pages"] == [1], got)
        db.refresh(g)
        check("detail 里带出来（前端要显示「这页不看」）",
              G.detail(db, g)["skipped_pages"] == [1], G.detail(db, g)["skipped_pages"])
        check("页号会去重排序（哪怕前端传得乱）",
              G.set_page_skips(db, g.id, [1, 1])["skipped_pages"] == [1])
        st, msg = expect_err(G.set_page_skips, db, g.id, [9])
        check("不存在的页 → 422（不是悄悄忽略）", st == 422 and "只有 1 页" in msg, (st, msg))
        check("传空数组 = 一页都不跳", G.set_page_skips(db, g.id, [])["skipped_pages"] == [])

        g.status = "running"
        db.commit()
        st, _msg = expect_err(G.set_page_skips, db, g.id, [1])
        check("正在批改时不许改「不看哪几页」→ 409", st == 409, st)
        g.status = "queued"
        db.commit()

        # ---------------- _apply_skips：三种页要区分清楚 ----------------
        kept, noop = G._apply_skips(
            {1: [{"skipped": False}], 2: [{"skipped": True}], 3: []}, set())
        check("块全跳过的页 2 → 记成 noop（不许退回整页批）", noop == {2}, noop)
        check("还有块的页 1 照常批", list(kept) == [1], list(kept))
        check("本来就没块的页 3 → 不进 noop（老口径，整页一次判）", 3 not in noop, noop)
        kept2, noop2 = G._apply_skips({1: [{"skipped": False}]}, {1})
        check("整页跳过 → 一个都不批也不进 noop（由 skip_pages 自己占位）",
              kept2 == {} and noop2 == set(), (kept2, noop2))

        # ---------------- 真跑一遍：跳过的不调模型 ----------------
        g7 = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()
        G.put_regions(db, g7.id, GradeRegionsPut(regions=[
            GradeRegionIn(q_no="1", box=[50, 100, 900, 300]),
            GradeRegionIn(q_no="", box=[50, 320, 900, 420], skipped=True),
            GradeRegionIn(q_no="3", box=[50, 460, 900, 700]),
        ]))
        calls: list[str] = []

        def fake_region(png, ctx):
            calls.append(str(ctx.get("region_label") or ""))
            return ([item_payload()], "")

        orig = G._recognize_region
        G._recognize_region = fake_region
        try:
            G.launch(db, g7)
            for _ in range(150):
                db.refresh(g7)
                if g7.status in ("reviewing", "done", "partial", "failed"):
                    break
                time.sleep(0.1)
        finally:
            G._recognize_region = orig
        check("★ 标记跳过的那一块根本没调模型（不是调完再丢）",
              calls == ["1", "3"], calls)
        check("跳过的块也不产生题目卡片（不占一个假结果）",
              len(G.detail(db, g7)["items"]) == 2, len(G.detail(db, g7)["items"]))
        check("这一页仍然算批完（跳过的块不该让 done 差一页）",
              g7.done == 1 and g7.failed == 0, (g7.done, g7.failed))

        # 整页跳过 → 一次模型调用都不该有，但任务要能正常收尾
        g8 = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[s1.id], answer_mode="ai"))
        db.commit()
        G.set_page_skips(db, g8.id, [1])
        calls2: list[str] = []

        def fake_page(png, ctx):
            calls2.append("page")
            return ([item_payload()], "")

        orig_p = G._recognize_page
        G._recognize_page = fake_page
        try:
            G.launch(db, g8)
            for _ in range(150):
                db.refresh(g8)
                if g8.status in ("reviewing", "done", "partial", "failed"):
                    break
                time.sleep(0.1)
        finally:
            G._recognize_page = orig_p
        check("★ 整页跳过 → 连整页那次模型调用都不发生", calls2 == [], calls2)
        check("★ 但任务必须能收尾（done 追得上 total，不能卡在 running）",
              g8.status in ("reviewing", "done") and g8.done == g8.total,
              (g8.status, g8.done, g8.total))

        # ---------------- 预览裁图：保存前就要能看 ----------------
        png = G.region_preview_png(db, g.id, "1:50,100,900,300")
        check("★ 预览接口能直接给出一张 PNG", png[:8] == b"\x89PNG\r\n\x1a\n", png[:8])
        png2 = G.region_preview_png(db, g.id, "1:50,100,900,300;1:50,460,900,700")
        check("跨页/多段的预览也能出图（竖拼）", png2[:4] == b"\x89PNG", png2[:4])
        st, msg = expect_err(G.region_preview_png, db, g.id, "")
        check("没给区域 → 422 说人话", st == 422, (st, msg))
        st, msg = expect_err(G.region_preview_png, db, g.id, "1:0,0,5,5")
        check("框太小 → 422（与存框同一条尺子）", st == 422 and "太小" in msg, (st, msg))
        st, _msg = expect_err(G.region_preview_png, db, g.id, "9:50,100,900,300")
        check("页号不存在 → 报错（不是裁一张空白图）", st in (404, 422), st)

        # ---------------- 模型的两个结构信号 ----------------
        from app.services import grade_ai as A
        it = A._item_from_obj({"verdict": "ungradable", "block_kind": "Footer",
                               "tail_note": "底部约 1/10 是页码与页脚"}, [])
        check("block_kind 带出来了（大小写归一交给 store）", it["block_kind"] == "Footer", it["block_kind"])
        check("tail_note 带出来了", "页码" in it["tail_note"], it["tail_note"])
        check("★ block_kind 认不出就归空（不猜成 question，也不猜成 header）",
              G._block_kind("nonsense") == "", G._block_kind("nonsense"))
        check("空值也归空（整页口径没有这个字段）", G._block_kind(None) == "")
        check("合法值原样保留（小写化）", G._block_kind(" SECTION ") == "section")

        row = G._item_row({"block_kind": "footer", "tail_note": "底部是页码"}, g.id, 1, 0)
        check("两列真的落到了行对象上", (row.block_kind, row.tail_note) == ("footer", "底部是页码"),
              (row.block_kind, row.tail_note))
        db.add(row)
        db.commit()
        check("item_dict 把它们给到前端（界面要显示提示）",
              (G.item_dict(row)["block_kind"], G.item_dict(row)["tail_note"]) == ("footer", "底部是页码"))

        # 提示词里必须真的要求了这两项，而且**豁免**了「不要输出没列出的字段」
        src = read(BE / "app" / "services" / "grade_ai.py")
        check("区域提示词要求 block_kind", "block_kind" in src)
        check("区域提示词要求 tail_note", "tail_note" in src)
        check("★ 明确豁免了「不要输出上面没列出的字段」（不豁免模型就不会给）",
              "例外" in src and "只在这个流程里要" in src)
        check("★ 不是题时要求「仍然输出一个元素」（空数组会让这块凭空消失）",
              "仍然输出一个元素" in src)
    finally:
        db.close()


# ================================================================ ⑩ 跨页题（一题多段）
def make_pdf(path: Path, pages: int = 2) -> None:
    """手造一张多页 PDF。跨页题**只可能**发生在 PDF 上（照片一页一张）。"""
    import pymupdf         # 只在测试里用；生产代码的 AGPL 边界在 adapters/pdf.py

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 120), f"page {i + 1}")
    doc.save(str(path))
    doc.close()


def mk_pdf_submission(db, ls: Lesson, pages: int = 2, name: str = "两页卷.pdf") -> LessonFile:
    p = config.UPLOAD_DIR / "students" / f"u{ls.student_id}_x" / "hw" / name
    make_pdf(p, pages)
    lf = LessonFile(lesson_id=ls.id, name=name, stored=_rel(p), role="homework",
                    kind="pdf", size_bytes=p.stat().st_size, pages=pages)
    db.add(lf)
    db.flush()
    return lf


def t_cancel() -> None:
    """终止批改（2026-10-20 加的）。

    为什么需要：一份 6 页的作业要十几分钟。跑起来发现分割切错了、或者模型配置
    不对，唯一的办法是刷新页面 —— 而刷新之后线程还在跑，回来又看到它在动。

    守的几件事：

      1. **只在真的在跑时才受理**。没在跑却返回成功，前端会一直等一个永远不会
         来的状态变化 —— 那是「点了没反应」的经典来源。
      2. **终止不算失败**。老师点终止多半是因为太慢，已批的那几页正是他要的
         结果；标成 failed 的话那些结果在列表里显示为失败，等于白等。
      3. **标记不能留到下一次开批**。线程被强杀时 `_cancel.discard` 走不到，
         所以 `launch` 里必须再清一次 —— 否则下次点开始「一秒钟就失败」。
      4. 一页都没批成就终止 → failed（reviewing 一个空结果更迷惑人）。
    """
    print("\n-- ⑨c 终止批改 --")

    # ① 没在跑 → 不受理（这一步最关键：受理了就等于骗前端）
    check(G.request_cancel(999999) is False,
          "★ 没在跑的时候点终止 → 不受理（不能骗前端说受理了）")
    check(G.is_canceled(999999) is False,
          "★ 不受理时也不立旗（否则下次开批立刻自我终止）")

    # ② 立了旗 → 看得见，且 launch 会清掉
    with G._run_lock:
        G._running.add(999999)
        G._cancel.add(999999)          # 模拟「上次线程被强杀，旗没清掉」
    check(G.is_canceled(999999) is True, "★ 立了旗 → is_canceled 看得见")

    db = SessionLocal()
    try:
        st = Student(id=90021, name="终止测试")
        db.add(st)
        ls = Lesson(id=90021, student_id=90021, start_at="2026-10-20T10:00:00",
                    duration_min=60)
        db.add(ls)
        db.flush()
        lf = mk_pdf_submission(db, ls, pages=2, name="终止卷.pdf")
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[lf.id], answer_mode="ai"))
        gid = g.id
        db.commit()
        G.launch(db, g)
        # ⚠️ launch 必须把上一次残留的「终止」旗清掉。不清的话老师下次点开始，
        #    第一个页还没提交就被 is_canceled 拦下 → 「一秒钟就失败」。
        check(G.is_canceled(gid) is False,
              "★ 开批时清掉上一次残留的终止旗（否则一点开始就自我终止）")

        # ③ 在跑 → 受理
        check(G.request_cancel(gid) is True, "★ 真的在跑 → 受理终止")
        check(G.is_canceled(gid) is True, "★ 受理后旗立起来了")

        # ④ 收尾：批了几页 → reviewing（保留结果，不是 failed）
        g.total = 3
        db.commit()
        G._finalize(gid, done=1, canceled=True)
        db.refresh(g)
        check(g.status == "reviewing",
              "★ 终止时已批了页 → reviewing（已出的结果留着复核，不算失败）", g.status)
        check("已手动终止" in (g.error or ""),
              "★ error 写清是「手动终止」，不是「批改失败」", g.error)
        check("1/3" in (g.error or ""),
              "★ error 写清批了几页 / 共几页", g.error)

        # ⑤ 收尾：一页都没批成 → failed（给 reviewing 一个空结果更迷惑）
        G._finalize(gid, done=0, canceled=True)
        db.refresh(g)
        check(g.status == "failed",
              "★ 终止时一页都没批成 → failed（空结果不该显示成「待复核」）", g.status)

        # ⑥ 收尾后旗要清掉（run_grade 的 finally 负责；这里手动走一遍防漏）
        with G._run_lock:
            G._running.discard(gid)
            G._cancel.discard(gid)
        check(G.is_canceled(gid) is False, "★ 收尾后旗清掉（不带到下一次）")
        check(G.request_cancel(gid) is False, "★ 收尾后再点终止 → 不受理")
    finally:
        db.close()
        with G._run_lock:
            G._running.discard(999999)
            G._cancel.discard(999999)

    # ⑦ 接口契约：路由表里有这条 POST
    src = (BE / "app" / "routers" / "homework_grades.py").read_text(encoding="utf-8")
    check('@router.post("/homework-grades/{gid}/cancel")' in src,
          "★ 有 POST /homework-grades/{gid}/cancel 这条路由")
    check("没在批改" in src,
          "★ 不受理时报的是人话「没在批改」（不是干巴巴一个 409）")
    # ⑧ 提示词/收尾里「终止」的措辞：已批的保留
    check("已手动终止" in (BE / "app" / "services" / "grade_store.py").read_text(encoding="utf-8"),
          "★ 收尾文案里写「已手动终止」（老师能分辨是自己停的还是系统出错）")


def t_cross_page() -> None:
    """跨页题（0029）：一题两段，竖拼成一张图、只调一次模型。

    需求原话（用户 2026-10-19）：
    「参考该项目批量录题的部分逻辑吧，即可分割、可框图，还有跨页题目分割方案，
      区别在给 AI 的内容和输出结果不同，部分功能完全没必要重新造轮子」

    守的几件事：

      1. 同一 `seq` 的多段 = **一道题**，跨页题只调一次模型（不是两次）。
      2. 拼接顺序由**几何**决定（先页、再上边），不信前端给的 `seg` ——
         段序是事实，让界面维护它迟早出错。
      3. 跨页块只在**起始页**处理。否则两页线程各批一次同一道题，结果互相覆盖。
      4. 跨页裁切走 `pdf_adapter.crop_regions`（批量录题的原貌图就是它），
         不自己写拼接 —— 这是「不重造轮子」最具体的一处。
      5. 照片提交件不假装能跨页：一页一张，页号越界要被挡住。
    """
    print("\n=== ⑩ 跨页题（一题多段竖拼） ===")
    db = SessionLocal()
    try:
        stu = mk_student(db, "跨页测试学生")
        ls = mk_lesson(db, stu)
        lf = mk_pdf_submission(db, ls, pages=2)
        db.commit()
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[lf.id], answer_mode="ai"))
        db.commit()
        check("两页 PDF 建出来的 total = 2（页数，不是题数）", g.total == 2, g.total)

        out = G.put_regions(db, g.id, GradeRegionsPut(grouped=True, regions=[
            GradeRegionIn(page_no=1, seq=0, q_no="1", box=[50, 600, 950, 980]),
            GradeRegionIn(page_no=2, seq=0, q_no="1", box=[50, 20, 950, 400]),
            GradeRegionIn(page_no=2, seq=1, q_no="2", box=[50, 450, 950, 900]),
        ]))
        check("三段都存下来了", len(out) == 3, len(out))

        blocks = G.blocks_of(db, g.id)
        check("★ 同一 seq 的多段合成一题（3 段 → 2 题）", len(blocks) == 2, len(blocks))
        check("第 1 题被判成跨页题", blocks[0]["cross_page"] is True, blocks[0])
        check("跨页题的两段按「先页、再上边」排（拼出来才对得上）",
              [s["page_no"] for s in blocks[0]["segments"]] == [1, 2], blocks[0]["segments"])
        check("段的 seg 被重排成 0,1（不信前端给的顺序）",
              [s["seg"] for s in blocks[0]["segments"]] == [0, 1],
              [s["seg"] for s in blocks[0]["segments"]])
        check("第 2 题只有一段、不是跨页（单页题不受影响）",
              len(blocks[1]["segments"]) == 1 and blocks[1]["cross_page"] is False, blocks[1])

        bp = G.blocks_by_page(db, g.id)
        check("★ 跨页题只归起始页（第 1 页），第 2 页不会重复批",
              len(bp.get(1) or []) == 1 and len(bp.get(2) or []) == 1,
              {k: len(v) for k, v in bp.items()})

        # 前端给的 seg 故意写反：后端必须按几何纠正
        G.put_regions(db, g.id, GradeRegionsPut(grouped=True, regions=[
            GradeRegionIn(page_no=2, seq=0, seg=0, box=[50, 20, 950, 400]),
            GradeRegionIn(page_no=1, seq=0, seg=1, box=[50, 600, 950, 980]),
        ]))
        fixed = G.blocks_of(db, g.id)[0]
        check("★ 前端把 seg 写反了也会被几何纠正（第 1 页那段排在前）",
              [s["page_no"] for s in fixed["segments"]] == [1, 2], fixed["segments"])

        # ---------------- 裁切：复用 pdf_adapter.crop_regions ----------------
        G.put_regions(db, g.id, GradeRegionsPut(grouped=True, regions=[
            GradeRegionIn(page_no=1, seq=0, q_no="1", box=[50, 600, 950, 980]),
            GradeRegionIn(page_no=2, seq=0, q_no="1", box=[50, 20, 950, 400]),
            GradeRegionIn(page_no=2, seq=1, q_no="2", box=[50, 450, 950, 900]),
        ]))
        blocks = G.blocks_of(db, g.id)
        slots = {s["pno"]: s for s in G._page_slots(db, g)}
        outdir = TMP / "crops_cross"
        tiled = G._crop_block(blocks[0], slots, outdir)
        check("跨页块能裁出一张图", tiled is not None and Path(tiled).exists(), tiled)
        single = G._crop_block(blocks[1], slots, outdir)
        check("单段块也能裁出来（这条路没被跨页改坏）",
              single is not None and Path(single).exists(), single)

        from PIL import Image
        with Image.open(tiled) as im:
            tw, th = im.size
        with Image.open(single) as im:
            sw, sh = im.size
        # ⚠️ 不能比像素高度：单段与跨页图都会缩放到长边 GRADE_MAX_EDGE，
        #    比出来一样高（那不是没拼）。要比**高宽比** —— 两段竖拼后明显更竖。
        check("★ 拼出来的图明显更竖（两段真的竖拼了，不是只裁了第一段）",
              (th / tw) > (sh / sw) * 1.5, (th, tw, sh, sw))

        # ---------------- 一题一次调用 ----------------
        seen: list[dict] = []
        orig = G._recognize_region

        def fake(png, ctx):
            seen.append({"span": ctx.get("span_pages"), "label": ctx.get("region_label"),
                         "page": ctx.get("page_no"), "region": ctx.get("region")})
            return ([item_payload(q_no="9", verdict="correct")], "")

        G._recognize_region = fake
        try:
            G.launch(db, g)
            for _ in range(200):
                db.refresh(g)
                if g.status in ("reviewing", "done", "partial", "failed"):
                    break
                time.sleep(0.1)
        finally:
            G._recognize_region = orig

        check("★ 3 段 2 题 → 只调 2 次模型（跨页题不是两题）", len(seen) == 2, seen)
        check("每次都是单题口径（region=True）", all(s["region"] is True for s in seen), seen)
        check("★ 跨页那一次带上了 span_pages（提示词要说明图是拼起来的）",
              any(s["span"] == [1, 2] for s in seen), seen)
        check("单段那一次没有 span_pages（不冒充跨页）",
              any(s["span"] is None for s in seen), seen)

        items = db.scalars(select(HomeworkGradeItem)
                           .where(HomeworkGradeItem.grade_id == g.id)
                           .order_by(HomeworkGradeItem.seq)).all()
        check("落库还是 2 题（跨页题没被拆成两条）", len(items) == 2, len(items))
        check("题号用块绑定的（模型给的是 9）", [i.q_no for i in items] == ["1", "2"],
              [i.q_no for i in items])

        # ---------------- 提示词：跨页要说清 ----------------
        from app.services import grade_ai as A
        p_cross = A._prompt_for("ai", {"page_no": 1, "region": True,
                                       "region_label": "1", "span_pages": [1, 2]})
        check("★ 提示词说明「这一张图是跨页拼起来的」", "跨页拼起来" in p_cross)
        check("并且点明白缝不是题目结束（否则下半段会被丢）", "白缝不是题目结束" in p_cross)
        check("单段口径里不出现跨页那段（不能无中生有）",
              "跨页拼起来" not in A._prompt_for("ai", {"page_no": 1, "region": True}))

        # ---------------- 前端接线：跨页得靠界面才画得出来 ----------------
        view = read(FE / "src" / "views" / "HomeworkGrade.js")
        check("前端有「并入上一题」（跨页题的收口动作）",
              "并入上一题" in view and "mergeRegion" in view)
        check("★ 保存时显式声明 grouped=true（不传就退成「一框一题」，跨页题会被拆两题）",
              "grouped: true" in view)
        check("★ 草稿是整份按页分桶的（editAll）—— 否则翻一下页上半段就丢了",
              "const editAll = ref({})" in view)
        check("★ 翻页不再弹「未保存」（跨页画框本来就要来回翻）",
              "翻页就丢了" not in code_only(view))
        check("面板上标出跨页（跨第 N + M 页）", "跨第" in view and "pages.join" in view)
    finally:
        db.close()


# ================================================================ ⑪ 判据传图
def mk_answer_image(db, ls: Lesson, name: str = "答案.png") -> LessonFile:
    p = config.UPLOAD_DIR / "students" / f"u{ls.student_id}_x" / "hw" / name
    make_png(p, 200, 260)
    lf = LessonFile(lesson_id=ls.id, name=name, stored=_rel(p), role="homework",
                    kind="image", size_bytes=p.stat().st_size, text="",
                    reason="照片/扫描件不抽文字")
    db.add(lf)
    db.flush()
    return lf


def t_answer_images() -> None:
    """判据能传图（老师给的答案/细则是一张照片）。

    需求原话：「判定依据中的『用我给的答案』『用我的评分细则』，要求能够传图」

    以前的做法是**把这种文件拦在门外**（422：照片不抽文字，请粘贴文字）。
    那个拦截本身是对的 —— 模型看不见答案却仍然显示「按老师答案判」是拿错标准批学生。
    真正的解法不是放宽校验，而是**把图真的送进去**，所以这里三处一起改：

      1. `create_grade` 放行图片答案（不再 422）；
      2. `_ctx_base` 带上 `answer_images` / `rubric_images`；
      3. `grade_ai` 多图一起发，并在提示词里**说清第几张是什么**。
    """
    print("\n=== ⑪ 判据传图（答案/细则可以是照片） ===")
    db = SessionLocal()
    try:
        stu = mk_student(db, "判据传图学生")
        ls = mk_lesson(db, stu)
        sub = mk_submission(db, ls, "作业.png")
        ans = mk_answer_image(db, ls, "答案照片.png")
        db.commit()

        # ---------------- 建任务：图片答案不再被拦 ----------------
        g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[sub.id],
                                       answer_mode="key", answer_file_ids=[ans.id]))
        db.commit()
        check("★ 图片答案能建任务（以前直接 422）", g.id is not None, g.id)

        ctx = G._ctx_base(db, g)
        check("ctx 里带上了答案图", len(ctx.get("answer_images") or []) == 1,
              ctx.get("answer_images"))
        check("答案文字仍然为空（图没被当成文字）", (ctx.get("answer_text") or "") == "",
              ctx.get("answer_text"))
        check("rubric 模式那份不混进答案图", (ctx.get("rubric_images") or []) == [],
              ctx.get("rubric_images"))

        # ---------------- AI 层：多图 + 说清第几张 ----------------
        from app.services import grade_ai as A
        uri_ctx = {**ctx, "answer_mode": "key"}
        uris = A._image_uris(str(TMP / "page_for_crop.png"), uri_ctx)
        check("★ 送模型的是 2 张图（学生作答 + 老师答案）", len(uris) == 2, len(uris))
        check("两张都是 data URI", all(u.startswith("data:image/jpeg;base64,") for u in uris),
              [u[:24] for u in uris])

        p_key = A._prompt_for("key", uri_ctx)
        check("★ 提示词说清「第 1 张是学生作答」", "第 1 张：**学生作答**" in p_key, p_key[:200])
        check("★ 提示词说清「第 2 张是老师答案，不是学生写的」",
              "第 2 张" in p_key and "不是学生写的" in p_key)
        check("并且明说不要把答案算进 student_answer",
              "不要把它上面的内容当成 student_answer" in p_key)
        check("答案正文为空时点明「在附图上」（不能说空着就按老师答案判）",
              "答案在附图上" in p_key)

        # rubric 走另一组字段，不能串
        ans2 = mk_answer_image(db, ls, "细则照片.png")
        db.commit()
        g_rub = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[sub.id],
                                          answer_mode="rubric", answer_file_ids=[ans2.id]))
        db.commit()
        ctx_r = G._ctx_base(db, g_rub)
        check("rubric 模式的图进 rubric_images", len(ctx_r.get("rubric_images") or []) == 1,
              ctx_r.get("rubric_images"))
        check("并且不混进 answer_images", (ctx_r.get("answer_images") or []) == [],
              ctx_r.get("answer_images"))
        p_rub = A._prompt_for("rubric", {**ctx_r, "answer_mode": "rubric"})
        check("细则的提示词点明「在附图上」", "评分细则在附图上" in p_rub)
        check("细则模式下第 2 张被叫作评分细则（不是答案）",
              "老师的评分细则" in p_rub and "老师的标准答案" not in p_rub)

        # ---------------- 有文字的文件不重复送图 ----------------
        g_txt = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[sub.id],
                                           answer_mode="key", answer_text="答案：x=2 或 x=3"))
        db.commit()
        ctx_txt = G._ctx_base(db, g_txt)
        check("粘贴了文字 → 不再额外送图（同一份依据别花两遍 token）",
              (ctx_txt.get("answer_images") or []) == [], ctx_txt.get("answer_images"))
        check("文字依据原样在 ctx 里", "x=2 或 x=3" in (ctx_txt.get("answer_text") or ""),
              ctx_txt.get("answer_text"))

        # ---------------- 开批前换图 ----------------
        g_sw = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[sub.id],
                                          answer_mode="key", answer_text="先给个占位"))
        db.commit()
        G.patch_setup(db, g_sw.id, GradeSetupPatch(answer_file_ids=[ans.id]))
        check("★ 开批前能把答案换成照片（以前只能重建任务）",
              len((G._ctx_base(db, g_sw) or {}).get("answer_images") or []) == 1)
        G.patch_setup(db, g_sw.id, GradeSetupPatch(answer_file_ids=[]))
        check("传 [] 能清空（回到只有文字）",
              (G._ctx_base(db, g_sw).get("answer_images") or []) == [])
        check("清空文件后粘贴的文字还在（两件事互不覆盖）",
              "占位" in (G._ctx_base(db, g_sw).get("answer_text") or ""))

        # ---------------- 既没文字、又不是图片：仍然要拦 ----------------
        dummy = config.UPLOAD_DIR / "students" / f"u{stu.id}_x" / "hw" / "答案.docx"
        dummy.parent.mkdir(parents=True, exist_ok=True)
        dummy.write_bytes(b"x")
        bad = LessonFile(lesson_id=ls.id, name="答案.docx",
                         stored=_rel(dummy), role="homework",
                         kind="docx", size_bytes=10, text="")
        db.add(bad)
        db.commit()
        st, msg = expect_err(G.create_grade, db, GradeIn(
            lesson_id=ls.id, file_ids=[sub.id], answer_mode="key", answer_file_ids=[bad.id]))
        check("★ 既没文字又不能当图看 → 仍然 422（不能偷偷按 AI 自解批）", st == 422, (st, msg))
        check("并且给了可执行的出路（粘文字 / 传照片）",
              "粘贴文字" in msg and "答案照片" in msg, msg)

        # ai 模式不该带任何依据
        g_ai = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[sub.id], answer_mode="ai"))
        db.commit()
        ctx_ai = G._ctx_base(db, g_ai)
        check("ai 模式下不塞任何答案图（老师没给依据）",
              (ctx_ai.get("answer_images") or []) == [] and
              (ctx_ai.get("answer_text") or "") == "", ctx_ai)

        # ---------------- 前端：设置区能传照片 ----------------
        view = read(FE / "src" / "views" / "HomeworkGrade.js")
        check("设置区有「直接传照片」入口", "直接传照片" in view)
        check("★ 上传后立刻把文件 id 挂进判定依据（不是等老师再点一次保存）",
              "answer_file_ids: setupAnswerFiles.value" in view)
        check("★ file input 不设 accept（本机经验：accept 会把老师要选的文件藏起来）",
              "accept=" not in view)
        check("上传走这一课的文件接口（role=homework，与提交件同一份清单）",
              "lessonFilesApi.upload(detail.value.lesson_id, fd, 'homework')" in view)
    finally:
        db.close()


# ================================================================ main
def main() -> int:
    Base.metadata.create_all(engine)
    print("=" * 72)
    print("AI 批改作业（0027）")
    print("=" * 72)
    t_contract()
    t_pure()
    t_setup()
    t_items()
    t_marks()
    t_marks_batch()
    t_skips_and_signals()
    t_cancel()
    t_run_with_stub()
    t_frontend()
    t_ink_export()
    t_regions()
    t_cross_page()
    t_answer_images()

    print("\n" + "=" * 72)
    print(f"共 {total} 项，失败 {len(fails)} 项")
    if fails:
        for f in fails:
            print(f"  ✗ {f}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
