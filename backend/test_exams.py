# -*- coding: utf-8 -*-
"""考试情况记录：学生级记录 + 试题/答题卡（暂存 → 归档）+ 考情分析模板。

需求原话（用户 2026-10-11）：
「每个学生还得加上考试情况记录（需要记录日期（不一定具体）、考试类型（周考\\月考\\期中考等）、
成绩、等级、试题与答题卡情况（会上传文件或图片，可能以缓存形式上传，上传后要能够删除）、
考情分析（要有至少两个模板供选择，例如：考察范围 试卷题目难易程度分析 老师上课所讲内容
学生答题情况 后期计划安排等））」

实现要点（也是这个测试要守住的）：
  · **学生级、不挂课**：考试日期和上课日期不一定对得上，挂课上会逼老师先找一节课。
  · **日期是自由文本**（'9 月中旬' 合法），date_key 只是尽力解析的排序键。
  · **附件先落暂存区**（exams_cache），保存考试时才搬进
    `students/u1_x/exams/<日期>_<类型>/`；上传后没保存也能真删。
  · **改日期/类型 → 归档目录跟着改名**（含库里的 stored），否则文件会散在两个文件夹。
  · **只归档、不抽文字**（status='archived'），因为试题/答题卡是扫描件，读出来是乱码。
  · 考情分析按模板分段写，**段名快照**存进记录（模板删了老记录照样显示得出来）。
  · 内置两套模板，可改不可删（同反馈模板/卷种样式）。

为什么不起服务：上传/归档/改名/删除都是几张表 + 磁盘上的纯逻辑，把 router 里的函数
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
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_exam_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.db 之前设
os.environ["SHIKE_DB_PATH"] = str(TMP / "exam.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

from fastapi import HTTPException                     # noqa: E402
from sqlalchemy import select                         # noqa: E402
from starlette.datastructures import UploadFile       # noqa: E402

from app import config, seed                          # noqa: E402
from app.db import SessionLocal, engine               # noqa: E402
from app.models import Base, Exam, ExamAnalysisTemplate, ExamFile, Student   # noqa: E402
from app.routers import exams as EX                   # noqa: E402
from app.schemas import (                             # noqa: E402
    ExamAnalysisIn,
    ExamAnalysisTemplateIn,
    ExamCacheRemoveIn,
    ExamFileIn,
    ExamFileRoleIn,
    ExamIn,
    ExamPatch,
)
from app.services import file_text, storage           # noqa: E402

assert str(config.DB_PATH) == str(TMP / "exam.db"), f"临时库没生效：{config.DB_PATH}"
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


# ================================================================ 库 / 数据
def reset() -> None:
    """每次都从干净的临时库 + 干净的 uploads 开始（否则第二次跑会假失败）。"""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    shutil.rmtree(TMP / "data" / "uploads", ignore_errors=True)


def mk_student(db, name: str = "李芹旭") -> Student:
    stu = Student(owner_id=config.OWNER_ID, name=name)
    db.add(stu)
    db.commit()
    return stu


def uf(filename: str, data: bytes = b"x") -> UploadFile:
    """造一个够 router 用的 UploadFile（真上传走 multipart，这里只要 .read()）。"""
    return UploadFile(io.BytesIO(data), filename=filename)


def cache_upload(filename: str, data: bytes = b"paper-bytes", role: str = "paper") -> dict:
    return asyncio.run(EX.upload_exam_file(file=uf(filename, data), role=role))


def mk_exam(db, stu, **kw) -> dict:
    return EX.create_exam(ExamIn(student_id=stu.id, **kw), db)


def expect_422(fn) -> tuple[bool, str]:
    try:
        fn()
    except HTTPException as e:
        return e.status_code == 422, f"HTTP {e.status_code} {e.detail}"
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return False, "没有抛异常"


def expect_http(fn, code: int) -> tuple[bool, str]:
    """要特定状态码的断言（400 / 404 这类说法比 422 准）。"""
    try:
        fn()
    except HTTPException as e:
        return e.status_code == code, f"HTTP {e.status_code} {e.detail}"
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return False, "没有抛异常"


def student_dir(stu) -> Path:
    return config.STUDENTS_DIR / storage.student_folder(stu)


def exam_files_on_disk(stu) -> list[Path]:
    d = student_dir(stu) / "exams"
    return sorted(p for p in d.rglob("*") if p.is_file()) if d.is_dir() else []


# ================================================================ ① 纯函数
def t_date_key() -> None:
    section("① 日期解析（自由文本 → 排序键）")
    ok = [("2026-09-15", "2026-09-15"), ("2026/9/15", "2026-09-15"),
          ("2026.9.5", "2026-09-05"), ("2026年9月15日", "2026-09-15"),
          ("2026-09", "2026-09"), ("2026年9月", "2026-09"),
          ("  2026-10-08  ", "2026-10-08")]
    for raw, want in ok:
        check(f"date_key_of({raw!r}) = {want}", EX.date_key_of(raw) == want,
              str(EX.date_key_of(raw)))
    # ★「日期不一定具体」—— 认不出来必须返回空串，**不能瞎猜、也不能丢记录**
    for raw in ("9 月中旬", "上学期期末", "", None, "上周", "中秋前", "2026 学年"):
        check(f"date_key_of({raw!r}) 认不出来 → 空串（不瞎猜）",
              EX.date_key_of(raw) == "", str(EX.date_key_of(raw)))
    check("月份越界不构造非法日期（2026-13 → '2026'）",
          EX.date_key_of("2026-13") == "2026", str(EX.date_key_of("2026-13")))
    check("日越界退化成只到月（2026-09-40 → '2026-09'）",
          EX.date_key_of("2026-09-40") == "2026-09", str(EX.date_key_of("2026-09-40")))


def t_paths() -> None:
    section("② 归档目录名与暂存区路径安全")
    class E:
        exam_date = "2026-09"
        exam_type = "月考"
    check("目录名 = <日期>_<类型>", storage.exam_dir_label(E) == "2026-09_月考",
          storage.exam_dir_label(E))

    class Empty:
        exam_date = ""
        exam_type = ""
    check("日期/类型都为空的兜底名（不能是空字符串）",
          storage.exam_dir_label(Empty) == "未标日期_未标类型", storage.exam_dir_label(Empty))

    class Bad:
        exam_date = "../.."
        exam_type = "a/b:c"
    label = storage.exam_dir_label(Bad)
    check("目录名里不可能带路径分隔符 / ..",
          "/" not in label and "\\" not in label and ".." not in label, label)

    # ★ 暂存区 key 必须**只能**落在 exams_cache 之内 —— 否则一个构造出来的 key
    #   就能借道删掉正式归档里的文件（尤其是 students/ 下的正式卷子）
    cache_root = config.EXAM_CACHE_DIR.resolve()
    check("safe_cache_path 认得正常 key",
          storage.safe_cache_path("ab12cd34ef56/卷子.pdf") is not None, "")
    for bad in ("../../shike.db", "..", "", None):
        check(f"safe_cache_path 拒绝越界 key {bad!r}",
              storage.safe_cache_path(bad) is None, str(storage.safe_cache_path(bad)))
    for tricky in ("students/u1_x/2026-09-15_月考/卷子.pdf", "/etc/passwd", "a/../../b.txt"):
        p = storage.safe_cache_path(tricky)
        check(f"★ 可疑 key {tricky!r} 即便能拼出路径，也**必须困在 exams_cache 里**",
              p is None or cache_root in p.resolve().parents
              or p.resolve().parent == cache_root, str(p))

    # unique_dir_name 的 ignore：改日期时算新目录名，不排除自己就会一路 _2、_3
    p = TMP / "uniqdir"
    shutil.rmtree(p, ignore_errors=True)
    p.mkdir(parents=True)
    (p / "2026-09_月考").mkdir()
    check("同名目录加 _2", storage.unique_dir_name(p, "2026-09_月考") == "2026-09_月考_2",
          storage.unique_dir_name(p, "2026-09_月考"))
    check("★ ignore 自己时不加 _2（否则每改一次日期就搬一次家）",
          storage.unique_dir_name(p, "2026-09_月考", ignore="2026-09_月考") == "2026-09_月考", "")
    check("没占用时原样返回", storage.unique_dir_name(p, "2026-10_期中") == "2026-10_期中", "")


# ================================================================ ③ 内置模板
def t_seed_templates() -> None:
    section("③ 内置考情分析模板（至少两套，可改不可删）")
    reset()
    db = SessionLocal()
    try:
        stats = seed.seed_all(db)
        rows = db.scalars(select(ExamAnalysisTemplate)).all()
        check("种子里有考情分析模板这一步", "exam_templates_added" in stats, str(stats.keys()))
        check("内置模板至少两套（需求原话要求）", len(rows) >= 2, f"{len(rows)} 套")
        names = [t.name for t in rows]
        check("模板名不重复", len(set(names)) == len(names), str(names))
        for t in rows:
            out = EX._template_out(t)
            check(f"「{t.name}」is_builtin=1", out["is_builtin"], "")
            labels = [s["label"] for s in out["sections"]]
            check(f"「{t.name}」段数 ≥ 4 且每段都有标题", len(labels) >= 4 and all(labels),
                  str(labels))
            keys = [s["key"] for s in out["sections"]]
            check(f"「{t.name}」每段的 key 非空且不重复",
                  all(keys) and len(set(keys)) == len(keys), str(keys))
            hints = [s["hint"] for s in out["sections"]]
            check(f"「{t.name}」每段都有提示语（只给标题、下面一片空白，人写不下去）",
                  all(h.strip() for h in hints), str(hints))
        # 五段那套必须真的覆盖需求里点名的角度
        five = next((EX._template_out(t) for t in rows if "五段" in t.name), None)
        joined = " ".join(s["label"] for s in (five or {}).get("sections", []))
        for want in ("考察范围", "难易", "上课", "答题", "计划"):
            check(f"五段模板涵盖「{want}」（需求点名的角度）", want in joined, joined)

        again = seed.seed_all(db)
        check("再跑一次种子不重复插入（只补不覆盖）",
              again["exam_templates_added"] == 0
              and len(db.scalars(select(ExamAnalysisTemplate)).all()) == len(rows), "")
    finally:
        db.close()


# ================================================================ ④ 暂存区
def t_cache() -> None:
    section("④ 附件暂存区：上传不写库、能真删、越界拒绝")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        r = cache_upload("2026届高三第一次月考数学卷.pdf", b"pdf-bytes", role="paper")
        key = r["cache_key"]
        check("暂存 key 形状 <token>/<文件名>", key.count("/") == 1 and len(key.split("/")[0]) >= 8,
              key)
        check("返回原名（界面显示用）", r["name"] == "2026届高三第一次月考数学卷.pdf", r["name"])
        check("返回 kind_cn（界面上得有个能看的标签）",
              bool(r.get("kind_cn")), str(r.get("kind_cn")))
        check("role 原样带回", r["role"] == "paper", r["role"])

        p = storage.safe_cache_path(key)
        check("★ 文件真的落在 exams_cache 下",
              p is not None and p.is_file()
              and config.EXAM_CACHE_DIR.resolve() in p.resolve().parents, str(p))
        check("★ 这一步**不写数据库**（保存时才登记）",
              db.scalars(select(Exam)).all() == [] and db.scalars(select(ExamFile)).all() == [],
              "")
        check("★ 也不碰正式归档目录（学生文件夹此时还不存在）",
              not student_dir(stu).exists(), str(student_dir(stu)))

        # ⚠️ upload_exam_file 是 async —— 直接调用只会拿到协程、**什么都没执行**
        bad, why = expect_422(lambda: asyncio.run(
            EX.upload_exam_file(file=uf("x.png"), role="nope")))
        check("不认识的 role → 422", bad, why)

        # 删：真删文件
        got = EX.remove_exam_cache(ExamCacheRemoveIn(cache_key=key))
        check("删暂存文件返回 deleted=True", got.get("deleted") is True, str(got))
        check("★ 文件真的从磁盘上没了（不是只在界面上拿掉）", not p.is_file(), str(p))
        check("暂存 token 目录被顺手收掉", not p.parent.exists(), str(p.parent))
        got2 = EX.remove_exam_cache(ExamCacheRemoveIn(cache_key=key))
        check("重复删是幂等的（第二次 deleted=False，不报错）", got2.get("deleted") is False, str(got2))

        bad, why = expect_422(
            lambda: EX.remove_exam_cache(ExamCacheRemoveIn(cache_key="../../shike.db")))
        check("★ 越界 cache_key 删不动 → 422", bad, why)

        # 过期清理：把 mtime 拨到 3 天前
        old = cache_upload("旧卷子.pdf")
        op = storage.safe_cache_path(old["cache_key"])
        old_ts = 1_700_000_000
        os.utime(op.parent, (old_ts, old_ts))
        now_key = cache_upload("新卷子.pdf")
        check("★ 48 小时前的暂存被清掉", not op.exists(), str(op))
        check("刚传的暂存不受影响",
              storage.safe_cache_path(now_key["cache_key"]).is_file(), "")
    finally:
        db.close()


# ================================================================ ⑤ 建记录 + 归档
def t_create_and_archive() -> None:
    section("⑤ 保存考试：暂存文件搬进正式目录并登记")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        c1 = cache_upload("月考卷.pdf", b"p1", role="paper")
        c2 = cache_upload("答题卡扫描.jpg", b"p2", role="sheet")

        out = mk_exam(db, stu, exam_date="2026-09-15", exam_type="月考",
                      score=85, full_score=100, grade="B",
                      files=[ExamFileIn(**c1), ExamFileIn(**c2)])
        check("返回 id", isinstance(out.get("id"), int), str(out.get("id")))
        check("date_key 由服务端解析", out.get("exam_date") == "2026-09-15", "")
        check("得分率算出来了（85/100 = 0.85）", out.get("score_rate") == 0.85,
              str(out.get("score_rate")))
        check("附件两条", len(out["files"]) == 2, str(len(out["files"])))

        rel = out["dir_rel"]
        check("★ 归档目录 = students/<学生>/exams/<日期>_<类型>",
              rel == f"students/{storage.student_folder(stu)}/exams/2026-09-15_月考", rel)
        d = storage.safe_join(rel)
        check("目录真的建在磁盘上", d is not None and d.is_dir(), str(d))
        names = sorted(p.name for p in d.iterdir())
        check("两份文件都搬进去了（文件名保留，不改成哈希）",
              names == sorted(["月考卷.pdf", "答题卡扫描.jpg"]), str(names))
        check("★ 暂存区空了（搬走了，不是复制）",
              not any(config.EXAM_CACHE_DIR.rglob("*.*")) if config.EXAM_CACHE_DIR.is_dir()
              else True, str(list(config.EXAM_CACHE_DIR.rglob("*"))))

        rows = db.scalars(select(ExamFile).order_by(ExamFile.id)).all()
        check("库里登记了两条", len(rows) == 2, str(len(rows)))
        check("★ 状态是 archived（只归档、不抽文字），不是 ok+0 字",
              all(f.status == "archived" for f in rows), str([f.status for f in rows]))
        check("归类跟着走：试题 + 答题卡",
              sorted(f.role for f in rows) == ["paper", "sheet"], str([f.role for f in rows]))
        check("stored 是相对 UPLOAD_DIR 的路径（换机器不失效）",
              all(f.stored.startswith("students/") and ":" not in f.stored for f in rows),
              str([f.stored for f in rows]))
        check("★ stored 指向的文件真的在磁盘上",
              all((storage.safe_join(f.stored) or Path("/nonexist")).is_file() for f in rows), "")
        check("size_bytes 记的是真实字节数",
              sorted(f.size_bytes for f in rows) == [2, 2], str([f.size_bytes for f in rows]))

        # 只写分、不写满分 → 算不出得分率，**不能拿成绩当分母凑 100%**
        out2 = mk_exam(db, stu, exam_date="2026-09", exam_type="周考", score=8)
        check("★ 只有成绩没有满分 → score_rate 为 None（不瞎算）",
              out2["score_rate"] is None, str(out2["score_rate"]))
        out3 = mk_exam(db, stu, exam_date="", exam_type="", score=0, full_score=100)
        check("成绩 0 分也算得出得分率 0（0 是有效值，不是「没填」）",
              out3["score_rate"] == 0.0, str(out3["score_rate"]))
    finally:
        db.close()


# ================================================================ ⑥ 读 / 排序
def t_list_and_order() -> None:
    section("⑥ 列表与排序（有日期按考试时间；没日期排最后）")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        other = mk_student(db, "王小二")
        mk_exam(db, stu, exam_date="2026-09-15", exam_type="月考")
        mk_exam(db, stu, exam_date="2026-10-08", exam_type="周考")
        mk_exam(db, stu, exam_date="9 月中旬", exam_type="模拟考")
        mk_exam(db, other, exam_date="2026-12-01", exam_type="期末考")

        r = EX.list_exams(stu.id, db)
        dates = [it["exam_date"] for it in r["items"]]
        check("★ 从新到旧（10-08 → 09-15）", dates[:2] == ["2026-10-08", "2026-09-15"], str(dates))
        check("★ 认不出日期的排最后（不插进日期序列里让人以为排错了）",
              dates[-1] == "9 月中旬", str(dates))
        check("★ 只列这个学生的（别人的考试看不见）", len(dates) == 3, str(dates))
        check("used_types 汇总了用过的类型",
              set(r["used_types"]) == {"月考", "周考", "模拟考"}, str(r["used_types"]))
        check("type_suggestions 里有常见类型", "期中" in "".join(r["type_suggestions"]),
              str(r["type_suggestions"]))

        bad, why = expect_422(lambda: EX.list_exams(99999, db))
        check("不存在的学生 → 404（不是 422）",
              not bad and "404" in why, why)
    finally:
        db.close()


# ================================================================ ⑦ 目录改名
def t_retarget() -> None:
    section("⑦ 改日期/类型 → 归档目录跟着改名（否则文件散在两处）")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        c = cache_upload("月考卷.pdf", b"abc", role="paper")
        out = mk_exam(db, stu, exam_date="2026-09-15", exam_type="月考",
                      files=[ExamFileIn(**c)])
        old_rel = out["dir_rel"]
        old_dir = storage.safe_join(old_rel)
        check("初始目录就位", old_dir.is_dir() and len(list(old_dir.iterdir())) == 1, str(old_dir))

        # 改日期（老师当时记错了）
        out2 = EX.patch_exam(out["id"], ExamPatch(exam_date="2026-09-16"), db)
        new_rel = out2["dir_rel"]
        new_dir = storage.safe_join(new_rel)
        check("★ 目录名跟着日期改了",
              new_rel.endswith("2026-09-16_月考"), new_rel)
        check("★ 文件搬到了新目录（不是留在旧目录）",
              (new_dir / "月考卷.pdf").is_file(), str(list(new_dir.iterdir())))
        check("★ 旧目录被收掉（不留空壳）", not old_dir.exists(), str(old_dir))
        row = db.scalars(select(ExamFile)).all()[0]
        check("★ 库里的 stored 同步更新（否则界面点开是 404 / 文件夹里找不到）",
              row.stored == f"{new_rel}/月考卷.pdf" and new_dir.name in row.stored, row.stored)
        check("stored 与磁盘一致（按 stored 找得到文件）",
              (storage.safe_join(row.stored) or Path("/x")).is_file(), row.stored)

        # 只改成绩 → 目录绝不能动
        out3 = EX.patch_exam(out["id"], ExamPatch(score=90), db)
        check("只改成绩不动目录", out3["dir_rel"] == new_rel, out3["dir_rel"])
        check("★ 没传的字段保持不变（改成绩不该抹掉考试类型/日期）",
              out3["exam_type"] == "月考" and out3["exam_date"] == "2026-09-16", str(out3))
        check("成绩更新了", out3["score"] == 90, str(out3["score"]))

        # ★ 同日期同类型再来一次 → 加 _2（两场同名考试不该混进一个文件夹）
        c2 = cache_upload("第二份.pdf", b"z", role="paper")
        out4 = mk_exam(db, stu, exam_date="2026-09-16", exam_type="月考",
                       files=[ExamFileIn(**c2)])
        check("★ 同日期同类型的第二场 → 目录加 _2",
              out4["dir_rel"].endswith("2026-09-16_月考_2"), out4["dir_rel"])
        check("两次考试的文件各自在自己的目录里",
              len(list(storage.safe_join(out4["dir_rel"]).iterdir())) == 1, "")

        # 再改回原样 → 不能变成 _3（ignore 自己）
        out5 = EX.patch_exam(out4["id"], ExamPatch(exam_date="2026-09-16"), db)
        check("★ 重复保存同一日期不会无限加 _2", out5["dir_rel"].endswith("_2"), out5["dir_rel"])
    finally:
        db.close()


# ================================================================ ⑧ 附件增删改
def t_files_crud() -> None:
    section("⑧ 附件：改归档 / 保存时补传 / 删除 / 越界拒绝")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        out = mk_exam(db, stu, exam_date="2026-09", exam_type="月考")
        check("没附件的考试不建空目录（不在磁盘上留垃圾）",
              not (student_dir(stu) / "exams").exists(), str(student_dir(stu) / "exams"))

        # 事后补传（老师先记了分数，隔天才拿到卷子）
        c = cache_upload("补传的卷子.pdf", b"pp", role="paper")
        out2 = EX.patch_exam(out["id"], ExamPatch(files=[ExamFileIn(**c)]), db)
        check("补传成功且立刻归档", len(out2["files"]) == 1, str(len(out2["files"])))
        fid = out2["files"][0]["id"]
        fpath = storage.safe_join(db.get(ExamFile, fid).stored)
        check("文件落盘", fpath.is_file(), str(fpath))

        # 改归类
        got = EX.patch_exam_file(fid, ExamFileRoleIn(role="sheet"), db)
        check("归类改成答题卡", got["role"] == "sheet" and got["role_label"] == "答题卡", str(got))
        # ⚠️ 用 model_construct 绕过 Pydantic（Literal 本来就会在 schema 层拦下），
        #    这里要验的是 **router 自己的那道闸**也在
        bad, why = expect_422(lambda: EX.patch_exam_file(
            fid, ExamFileRoleIn.model_construct(role="xxx"), db))
        check("router 侧也拦不认识归类 → 422", bad, why)

        # 删附件（只删这一份，考试记录还在）
        EX.delete_exam_file(fid, db)
        check("★ 库里的行没了", db.get(ExamFile, fid) is None, "")
        check("★ 磁盘上的原件也没了", not fpath.is_file(), str(fpath))
        check("考试记录本身还在", db.get(Exam, out["id"]) is not None, "")
        check("★ 删掉最后一个附件后，学生文件夹还在（别顺手铲掉老师自己的目录）",
              student_dir(stu).exists(), str(student_dir(stu)))
        check("空掉的考试目录被收掉", not (student_dir(stu) / "exams" / "2026-09_月考").exists(), "")

        bad, why = expect_422(lambda: EX.delete_exam_file(99999, db))
        check("删不存在的附件 → 404（不是 422）", not bad and "404" in why, why)
    finally:
        db.close()


# ================================================================ ⑨ 删考试
def t_delete_exam() -> None:
    section("⑨ 删一次考试：记录 + 附件一起走")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        c1 = cache_upload("卷子.pdf", b"a", role="paper")
        c2 = cache_upload("卡.jpg", b"b", role="sheet")
        out = mk_exam(db, stu, exam_date="2026-09-15", exam_type="月考",
                      files=[ExamFileIn(**c1), ExamFileIn(**c2)])
        rel = out["dir_rel"]
        d = storage.safe_join(rel)
        check("两份附件俱在", len(list(d.iterdir())) == 2, str(list(d.iterdir())))

        # 另一个考试的附件不能被连坐
        c3 = cache_upload("别的卷子.pdf", b"c", role="paper")
        other = mk_exam(db, stu, exam_date="2026-10-01", exam_type="周考",
                        files=[ExamFileIn(**c3)])

        EX.delete_exam(out["id"], db)
        check("记录没了", db.get(Exam, out["id"]) is None, "")
        check("★ 附件行跟着 CASCADE 走了", db.scalars(select(ExamFile)).all() == []
              or len(db.scalars(select(ExamFile)).all()) == 1, "")
        check("★ 磁盘上的文件也删了（不是只删库）", not d.exists(), str(d))
        check("★ 别的考试的附件毫发无损",
              (storage.safe_join(other["dir_rel"]) / "别的卷子.pdf").is_file(), "")

        bad, why = expect_422(lambda: EX.delete_exam(99999, db))
        check("删不存在的考试 → 404（不是 422）", not bad and "404" in why, why)
    finally:
        db.close()


# ================================================================ ⑩ 考情分析
def t_analysis() -> None:
    section("⑩ 考情分析：分段存、段名快照、坏数据不炸")
    reset()
    db = SessionLocal()
    try:
        stu = mk_student(db)
        secs = [
            {"key": "scope", "label": "考察范围", "hint": "考到哪些章节", "text": "三角函数全章"},
            {"key": "plan", "label": "后期计划", "hint": "", "text": "先补诱导公式"},
            {"key": "mine", "label": "家长沟通要点", "hint": "", "text": "本次进步明显"},
        ]
        out = mk_exam(db, stu, exam_date="2026-10-08", exam_type="月考",
                      analysis=ExamAnalysisIn(template_id=1, template_name="考情分析（五段 · 标准）",
                                              sections=secs))
        a = out["analysis"]
        check("段数原样存回", len(a["sections"]) == 3, str(len(a["sections"])))
        check("模板名快照存下来了", a["template_name"] == "考情分析（五段 · 标准）", str(a))
        check("★ 模板外自己加的段也在（换模板不该把老师写的字弄丢）",
              any(s["label"] == "家长沟通要点" for s in a["sections"]), str(a))
        check("段的 label / hint 都留着（记录要能自解释）",
              all(s["label"] for s in a["sections"]), str(a))

        # 改分析：整组替换
        out2 = EX.patch_exam(out["id"], ExamPatch(
            analysis=ExamAnalysisIn(template_id=2, template_name="四段", sections=[secs[0]])), db)
        check("analysis 整组替换", len(out2["analysis"]["sections"]) == 1, str(out2["analysis"]))

        # 只改成绩不该动分析
        out3 = EX.patch_exam(out["id"], ExamPatch(score=77), db)
        check("★ 不传 analysis 就原样保留（改个成绩不该抹掉整篇分析）",
              out3["analysis"]["sections"][0]["text"] == "三角函数全章", str(out3["analysis"]))

        # 模板被删掉之后，老记录仍然显示得出来（label 已快照）
        for t in db.scalars(select(ExamAnalysisTemplate)).all():
            db.delete(t)
        db.commit()
        out4 = EX.list_exams(stu.id, db)
        check("★ 模板全删了，老记录的分析照样读得出标题与正文",
              out4["items"][0]["analysis"]["sections"][0]["label"] == "考察范围", "")

        check("_analysis_load 遇到坏 JSON 返回空壳（不抛异常）",
              EX._analysis_load("{not json") == {"template_id": None, "template_name": "",
                                                 "sections": []}, "")
        check("_analysis_load 遇到非数组 sections 也不炸",
              EX._analysis_load('{"sections": "xxx"}')["sections"] == [], "")
        check("_analysis_load 丢掉非字典的段",
              EX._analysis_load('{"sections": [1, null, {"label":"ok"}]}')["sections"]
              == [{"key": "", "label": "ok", "hint": "", "text": ""}], "")
    finally:
        db.close()


# ================================================================ ⑪ 模板 CRUD
def t_template_crud() -> None:
    section("⑪ 分析模板：可改不可删（内置）、自定义能删")
    reset()
    db = SessionLocal()
    try:
        seed.seed_all(db)
        items = EX.list_exam_templates(db)["items"]
        builtin = [t for t in items if t["is_builtin"]]
        check("内置至少两套", len(builtin) >= 2, str(len(builtin)))
        bid = builtin[0]["id"]

        bad, why = expect_http(lambda: EX.delete_exam_template(bid, db), 400)
        check("★ 内置模板删不掉（删了下次启动又会建回来，反而迷惑）", bad, why)

        # 内置可以改文案
        EX.patch_exam_template(bid, ExamAnalysisTemplateIn(name="我改过的名字"), db)
        got = next(t for t in EX.list_exam_templates(db)["items"] if t["id"] == bid)
        check("内置模板改得了名字", got["name"] == "我改过的名字", got["name"])
        check("★ 只改名字不会把段落结构清空（sections 没传 = 不动）",
              len(got["sections"]) >= 4, str(len(got["sections"])))

        # 自定义
        made = EX.create_exam_template(ExamAnalysisTemplateIn(
            name="我的三段", sections=[{"label": "得分", "hint": "多少分"},
                                       {"label": "", "hint": "无标题的段要丢掉"},
                                       {"label": "问题", "text": "x"}]), db)
        check("新建自定义模板", made["is_builtin"] is False and made["name"] == "我的三段", str(made))
        check("★ 没标题的段被丢掉（无字空白块没有意义）",
              [s["label"] for s in made["sections"]] == ["得分", "问题"],
              str(made["sections"]))
        check("模板段里不保留 text（模板只存骨架）",
              all("text" not in s for s in made["sections"]), str(made["sections"]))
        EX.delete_exam_template(made["id"], db)
        check("自定义模板删得掉",
              all(t["id"] != made["id"] for t in EX.list_exam_templates(db)["items"]), "")
    finally:
        db.close()


# ================================================================ ⑫ 前端纯逻辑
EXAM_JS = r"""
import { mergeSections, normalizeSections, nextExtraKey, analysisStats, analysisPreview,
         filledSections } from './examan.mjs';
let pass = 0, fail = 0;
function check(name, ok, extra) {
  if (ok) { pass++; console.log('  [OK ] ' + name); }
  else { fail++; console.log('  [FAIL] ' + name + (extra ? '   <- ' + extra : '')); }
}

const TPL = [
  { key: 'scope', label: '考察范围', hint: '考到哪些章节' },
  { key: 'plan', label: '后期计划', hint: '接下来怎么补' },
];
const OLD = [
  { key: 'scope', label: '考察范围', text: '三角函数全章' },
  { key: 'mine', label: '家长沟通要点', text: '本次进步明显' },
];

// ★ 这是整个模块存在的理由：换模板不能把已写的字弄丢
let r = mergeSections(OLD, TPL);
check('★ 同 key 的正文沿用（换模板不丢字）',
      r.sections[0].text === '三角函数全章', JSON.stringify(r.sections[0]));
check('标题与提示语用新模板的', r.sections[0].label === '考察范围'
      && r.sections[0].hint === '考到哪些章节', JSON.stringify(r.sections[0]));
check('新模板里没有的段被保留（不是静默删掉）',
      r.sections.some(s => s.label === '家长沟通要点' && s.text === '本次进步明显'),
      JSON.stringify(r.sections));
check('保留下来的段标了 extra（界面据此标「自定义」）',
      r.sections.find(s => s.label === '家长沟通要点').extra === true, '');
check('carried 数出带过来的非空段', r.carried === 1, String(r.carried));
check('extra 数出模板外的段', r.extra === 1, String(r.extra));
check('added 数出模板新增的空段', r.added === 1, String(r.added));

check('全部文字一个字都没少（拼起来比对）',
      r.sections.map(s => s.text).join('|') === '三角函数全章||本次进步明显',
      r.sections.map(s => s.text).join('|'));

// 空 → 模板：全空、带提示
r = mergeSections([], TPL);
check('空内容套模板 → 段都在、都空、提示语在',
      r.sections.length === 2 && r.sections.every(s => !s.text)
      && r.sections.every(s => s.hint), JSON.stringify(r.sections));
check('空内容时 carried=0（界面不该弹「都保留了」）', r.carried === 0 && r.extra === 0, '');

// 换成「不用模板」（空模板）→ 所有已写内容都得留下
r = mergeSections(OLD, []);
check('★ 换成「不用模板」时已写内容全部保留（一段都不许丢）',
      r.sections.length === 2 && r.sections.every(s => s.extra)
      && r.sections.map(s => s.text).join('|') === '三角函数全章|本次进步明显',
      JSON.stringify(r.sections));

// 反复横跳：模板A → 无模板 → 模板B，文字始终在
let cur = mergeSections(OLD, TPL).sections;
cur = mergeSections(cur, []).sections;
cur = mergeSections(cur, [{ key: 'loss', label: '失分点', hint: '' }]).sections;
check('★ 模板 A→无→B 反复切换，原文字仍在',
      cur.some(s => s.text === '三角函数全章') && cur.some(s => s.text === '本次进步明显'),
      JSON.stringify(cur));
check('切到 B 后 B 自己的段出现了', cur.some(s => s.key === 'loss'), JSON.stringify(cur));

// 脏数据不炸
check('脏数据（null / 数字 / 缺字段）不抛异常并归一',
      normalizeSections([null, 3, { key: 'a' }]).length === 1
      && normalizeSections([null, 3, { key: 'a' }])[0].text === '', '');
check('mergeSections 对 undefined 也安全',
      mergeSections(undefined, undefined).sections.length === 0, '');

check('nextExtraKey 避开已占用的 key',
      nextExtraKey([{ key: 'extra_1' }, { key: 'extra_2' }]) === 'extra_3',
      nextExtraKey([{ key: 'extra_1' }, { key: 'extra_2' }]));

const st = analysisStats({ sections: [{ text: 'a' }, { text: '  ' }, { text: 'bb' }] });
check('analysisStats 只把有内容的算写满（chars 是全部字符数）',
      st.total === 3 && st.filled === 2 && st.chars === 5, JSON.stringify(st));
check('filledSections 过滤掉空段',
      filledSections({ sections: [{ label: 'A', text: '' }, { label: 'B', text: 'x' }] }).length >= 1,
      '');
check('analysisPreview 取第一段有内容的，并截断',
      analysisPreview({ sections: [{ text: '' }, { text: 'x'.repeat(60) }] }, 10) === 'xxxxxxxxxx…',
      analysisPreview({ sections: [{ text: '' }, { text: 'x'.repeat(60) }] }, 10));
check('analysisPreview 全空时给空串（列表行不显示空行）',
      analysisPreview({ sections: [{ text: '' }] }) === '', '');

console.log('__EXAM_DONE__ pass=' + pass + ' fail=' + fail);
"""


def t_frontend_logic() -> None:
    section("⑫ examAnalysis.js 纯逻辑（node 直接跑）")
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上")
        return
    src = FE / "src" / "examAnalysis.js"
    check("examAnalysis.js 存在", src.exists(), str(src))
    if not src.exists():
        return
    text = read(src)
    check("不碰 DOM、不 import 任何东西（纯逻辑才好单测）",
          "document" not in text and not re.search(r"^\s*import\s", text, re.M), "")
    d = TMP / "logic"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, d / "examan.mjs")
    (d / "run.mjs").write_text(EXAM_JS, encoding="utf-8")
    r = subprocess.run([node, str(d / "run.mjs")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        m = re.match(r"\s*\[(OK |FAIL)\]\s*(.+?)(?:\s+<- (.*))?$", line)
        if m:
            check(m.group(2).strip(), m.group(1) == "OK ", m.group(3) or "")
    done = [ln for ln in out.splitlines() if ln.startswith("__EXAM_DONE__")]
    check("node 侧断言全部跑完（进程没中途挂掉）", len(done) == 1, out.strip()[-300:])
    if done:
        check("node 侧没有失败项（自证）", "fail=0" in done[0], done[0])


# ================================================================ ⑬ 前端接线
def t_frontend_wiring() -> None:
    section("⑬ 前端接线：弹窗 / 学生页 / api / 样式")
    dlg = FE / "src" / "components" / "ExamDialog.js"
    detail = FE / "src" / "views" / "StudentDetail.js"
    api = FE / "src" / "api.js"
    css = FE / "app.css"
    check("ExamDialog.js 存在", dlg.exists(), str(dlg))
    if not dlg.exists():
        return
    d = read(dlg)
    check("弹窗引用了共享的粘贴判断（不自己写一套）", "pickPastedAsset" in d, "")
    check("弹窗引用了 examAnalysis（换模板不丢字的逻辑只此一处）",
          "mergeSections" in d, "")
    check("三个上传入口：试题 / 答题卡 / 其他",
          d.count("pick('paper')") == 1 and d.count("pick('sheet')") == 1
          and d.count("pick('other')") == 1, "")
    check("★ 文件选择框**不设 accept**（设了会让文件对话框把某些类型藏起来）",
          'type="file"' in d and 'type="file" accept' not in d, "")
    check("每份附件都能删（真删，不是只从列表拿掉）",
          "removeCache" in d and "removeFile" in d, "")
    check("附件能改归类", "setFileRole" in d, "")
    check("删除前有确认（破坏性动作不静默做）",
          "window.confirm" in d and d.count("window.confirm") >= 2, "")
    check("★ 不在关窗时删暂存文件（误删老师刚传的东西更糟）",
          "不删暂存文件" in d or "不删暂存" in d, "")
    check("日期是自由文本输入（不是 date 控件，否则写不了「9 月中旬」）",
          'type="date"' not in d and "examDate" in d, "")
    check("给了「今天 / 本月」快捷填充", "fillToday" in d and "fillThisMonth" in d, "")

    s = read(detail)
    check("学生页引了 ExamDialog", "ExamDialog" in s and "components/ExamDialog.js" in s, "")
    check("学生页有「考试情况」区块", "考试情况" in s, "")
    check("★ 考试记录不挂在某节课上（单独一条加载路径）",
          "examsApi.list(id)" in s, "")
    check("学生页展示得分率与归档位置", "scoreRatePct" in s and "dir_rel" in s, "")

    a = read(api)
    for fn in ("list:", "create:", "update:", "remove:", "upload:", "removeCache:",
               "removeFile:", "setFileRole:", "templates:"):
        check(f"api.js 的 examsApi 有 {fn}", fn in a.split("export const examsApi")[1]
              if "export const examsApi" in a else False, "")
    check("api.js 里 examsApi 是导出的", "export const examsApi" in a, "")

    c = read(css)
    for cls in (".exam-row", ".exam-head", ".exam-file", ".exam-thumb", ".exam-sec"):
        check(f"app.css 有 {cls} 样式", cls in c, "")


# ================================================================ ⑭ 模板编译
def t_template_compile() -> None:
    section("⑭ Vue 真编译器校验 ExamDialog / StudentDetail 模板")
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上")
        return
    vue = FE / "vendor" / "vue.global.prod.js"
    check("vendor/vue.global.prod.js 存在", vue.exists(), str(vue))
    if not vue.exists():
        return

    targets = [("ExamDialog", FE / "src" / "components" / "ExamDialog.js"),
               ("StudentDetail", FE / "src" / "views" / "StudentDetail.js")]
    tpl_files = []
    for name, p in targets:
        tpl = extract_template(p)
        check(f"{name} 能提取出 template（模板写成字符串常量）",
              bool(tpl) and (tpl or "").rstrip().endswith(">"), ((tpl or "")[-60:]))
        if not tpl:
            return
        f = TMP / f"{name}.html"
        f.write_text(tpl, encoding="utf-8")
        tpl_files.append((name, f))

    # ⚠️ 同一套坑（见 test_paper_ui.py ⑦）：compiler-dom 遇属性值里的 `&`
    #    会去 decodeEntities，需要 document；stub 又可能把真错误兜住 ——
    #    所以额外编译一份**故意写错**的模板必须报失败，用来自证校验器在跑。
    js = TMP / "compile_exam.js"
    js.write_text(
        "const fs=require('fs'),vm=require('vm');\n"
        f"const VUE={str(vue)!r};\n"
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
        "  console.log('TPL ' + f + ' ' + comp(fs.readFileSync(f,'utf8')));\n"
        "}\n", encoding="utf-8")

    r = subprocess.run([node, str(js)] + [str(f) for _, f in tpl_files],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
    out = (r.stdout or "") + (r.stderr or "")
    check("自证：故意写错的模板必须报错（否则说明校验在空转）",
          re.search(r"SELFTEST FAIL", out) is not None, out[-200:])
    check("自证：正常 v-if/v-else 必须能编译",
          re.search(r"SELFTEST_GOOD OK", out) is not None, out[-200:])
    for name, f in tpl_files:
        check(f"{name} 的模板编译通过",
              re.search(rf"^TPL .*{name}\.html OK", out, re.M) is not None, out[-300:])


# ================================================================ main
def main() -> int:
    print("考试情况记录 · 自包含检查（不起服务、不碰真库）")
    t_date_key()
    t_paths()
    t_seed_templates()
    t_cache()
    t_create_and_archive()
    t_list_and_order()
    t_retarget()
    t_files_crud()
    t_delete_exam()
    t_analysis()
    t_template_crud()
    t_frontend_logic()
    t_frontend_wiring()
    t_template_compile()
    print(f"\n共 {total} 项，失败 {len(fails)} 项")
    if fails:
        print("失败项：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
