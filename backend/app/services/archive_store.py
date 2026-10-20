# -*- coding: utf-8 -*-
"""批改文件留存 —— 「学生资料 → 改作业」的存储层。

布局（沿用 `services/storage.py` 的「学生 / 日期」归档，只多一层子目录）：

    data/uploads/students/
      u1_李芹旭/                      ← 与讲义、反馈配图同一个学生目录（带 id，重名不撞）
        2026-10-20/                   ← **作业那天**，不是存档那天（见下）
          讲义.pdf                    ← 上课材料（既有）
          批改/                       ← 本模块：这份作业的批改存档
            批改_李芹旭_20261020.pdf
            批改_李芹旭_20261020_2.pdf   ← 同一天存第二版，加 _2（不覆盖）

## 为什么日期用「作业/批改那天」而不是「点存档那天」

这个目录是给人翻的。老师过两周要回顾「10 月 20 号那节课的作业」，按作业日期分才找得到；
按存档日期分会出现「同一节课的卷子散在好几天里」（`storage.py` 文件头对这件事有同样的解释）。
所以 `on` 的兜底取 `grade.created_at[:10]`，而不是 `date.today()`；老师也可以显式传一个日期。

## 为什么多一层 `批改/`

那个日期格里已经躺着讲义、反馈配图、作业原件快照。批改存档虽然也是 PDF，
但它是**一次批改的产物**，混进去之后「这格里有几份东西」就说不清了。
多一层只在路径上多十来个字符，换来的是打开文件夹一眼能分清。

## 三条硬约束（改这里之前先读）

① **类型走白名单**（`ALLOWED`），不是黑名单。存档文件会被浏览器内嵌预览，
   放进 `.svg` / `.html` 等于给自己开一个 XSS 口子（上传者能控制内容）；
   `.exe` / `.bat` 同理，没有存进来的理由。
② **文件名一律过 `storage.safe_token`**。老师传的可能是 `..\\..\\shike.db`，
   也可能是 `作业:2026/10/20.pdf` —— 冒号与斜杠在 Windows 上直接建不出文件。
③ **读回只认库里的 `rel_path`**，绝不接受客户端传路径。`storage.safe_join`
   是唯一入口，越界（`..`、绝对路径、指到 UPLOAD_DIR 外）一律返回 None。
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..models import GradeArchive, HomeworkGrade, HomeworkGradeItem, Lesson, Student
from . import storage

# 存档在「学生 / 日期」那一格下的子目录名
SUBDIR = "批改"

# 单个文件上限。一页批改 PDF 约 1~2MB，14 页也就 20MB 上下，60MB 留足余量；
# 不设上限的话，老师手滑选中一个视频就会把整份作业的存档流程卡住几十秒。
MAX_BYTES = 60 * 1024 * 1024

# 扩展名 -> 规范化的 kind。**白名单**（理由见文件头 ①）。
# 刻意没有 svg / html / htm / exe / bat / js —— 这些要么能被浏览器执行，
# 要么根本不是「批改存档」该有的东西。
ALLOWED: dict[str, str] = {
    ".pdf": "pdf",
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".webp": "webp",
    ".gif": "gif",
    ".bmp": "bmp",
    ".doc": "doc",
    ".docx": "docx",
    ".xls": "xls",
    ".xlsx": "xlsx",
    ".ppt": "ppt",
    ".pptx": "pptx",
    ".txt": "txt",
    ".md": "md",
    ".zip": "zip",
}

# 能**在页面里直接看**的：图片 + PDF。
# PDF 加进来是本模块的核心诉求（「支持在线查看可预览的文件类型」）——
# 浏览器内置 PDF 阅读器不执行脚本，内嵌是安全的，与 svg/html 完全两回事。
# Word / Excel / 压缩包一律给下载按钮：拖进 iframe 只会是白屏或乱码，比给个按钮糟得多。
PREVIEW_KINDS = {"pdf", "png", "jpg", "webp", "gif", "bmp"}

ALLOWED_NOTE = ("可以存 PDF / 图片（png、jpg、webp、gif、bmp）/ "
                "Word / Excel / PPT / txt / md / zip。")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ArchiveError(Exception):
    """业务错误：路由层翻成 HTTP（与 `grade_store.GradeError` 同一个形状）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- 名字与日期
def day_of(raw: str | None, fallback: str | None = None) -> str:
    """把日期规范成 `YYYY-MM-DD`。

    ⚠️ 不合法时**回落到兜底值**而不是报错：老师手填一个「2026-2-3」不该让整次
       存档失败（文件是他刚批完的，丢不起）。兜底是「批改那天」，再没有才是今天。
    """
    s = str(raw or "").strip()[:10]
    if _DATE_RE.match(s):
        try:
            date.fromisoformat(s)        # 挡掉 2026-02-31 这种「格式对、日期不存在」
        except ValueError:
            pass
        else:
            return s
    fb = str(fallback or "").strip()[:10]
    if _DATE_RE.match(fb):
        try:
            date.fromisoformat(fb)
            return fb
        except ValueError:
            pass
    return date.today().isoformat()


def clean_name(raw: str | None) -> str:
    """把上传时的原名变成**能落盘的单个文件名**（保留扩展名）。

    两步：先 `Path(...).name` 砍掉任何目录部分（`C:\\x\\y.pdf` → `y.pdf`），
    再让 `safe_token` 洗掉 Windows 不允许的字符与控制字符。
    空名兜底成「作业存档」，**不是**空串 —— 空文件名在磁盘上根本建不出来。
    """
    name = Path(str(raw or "").replace("\\", "/")).name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = storage.safe_token(stem, "作业存档", 80)
    ext = "." + storage.safe_token(ext, "", 12).lower() if ext else ""
    return stem + ext


def kind_of(name: str) -> str | None:
    """扩展名 -> 规范化 kind；不在白名单里返回 None。"""
    return ALLOWED.get(Path(str(name or "")).suffix.lower())


def validate(filename: str, size: int) -> tuple[str, str]:
    """校验并返回 (洗干净的文件名, kind)。不通过抛 `ArchiveError(422)`。"""
    name = clean_name(filename)
    kind = kind_of(name)
    if kind is None:
        ext = Path(name).suffix or "无扩展名"
        raise ArchiveError(422, f"这种格式不能存进作业存档（{ext}）。{ALLOWED_NOTE}")
    if size <= 0:
        raise ArchiveError(422, "这个文件是空的（0 字节），像是没选对文件")
    if size > MAX_BYTES:
        raise ArchiveError(
            422,
            f"文件太大了（{size / 1024 / 1024:.1f}MB，上限 {MAX_BYTES // 1024 // 1024}MB）。"
            "确认一下是不是选错文件了？",
        )
    return name, kind


# ---------------------------------------------------------------- 落盘
def archive_dir(student: Student, on: str) -> Path:
    """这个学生这一天的批改存档目录（并确保存在）。

    ⚠️ 三层拼接全部来自**服务端**：学生目录名走 `storage.student_folder`（带 id），
       日期走 `day_of`（正则化过），子目录是常量。客户端一个字符也进不来。
    """
    d = config.UPLOAD_DIR / "students" / storage.student_folder(student) / on / SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(directory: Path, name: str, data: bytes) -> Path:
    """写盘：同名加 `_2`，且先写临时文件再 rename —— 中途失败不会留半截文件。"""
    stored = storage.unique_name(directory, name)
    dest = directory / stored
    tmp = directory / f".tmp_{uuid4().hex}"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    except OSError as e:
        raise ArchiveError(500, f"写文件失败：{e}") from e
    finally:
        # 失败路径上把临时文件收掉；成功时 rename 之后它已经不存在了
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return dest


def create(db: Session, *, student: Student, filename: str, data: bytes,
           on: str | None = None, grade: HomeworkGrade | None = None,
           source: str = "upload", grader: str = "", note: str = "") -> GradeArchive:
    """存一份归档并建记录。**校验 → 落盘 → 入库**，顺序不能反：

    先入库再落盘的话，磁盘写失败会留下一条指向空气的记录
    （界面上显示「有一份存档」，点开 404）；反过来最坏只是多一个没人引用的文件，
    那种能被下次存档的避重逻辑自然绕过。
    """
    name, kind = validate(filename, len(data))
    day = day_of(on, (grade.created_at or "")[:10] if grade else "")
    dest = _write(archive_dir(student, day), name, data)
    rec = GradeArchive(
        owner_id=config.OWNER_ID,
        student_id=student.id,
        grade_id=grade.id if grade else None,
        lesson_id=grade.lesson_id if grade else None,
        archived_on=day,
        name=Path(filename or name).name or name,
        stored_name=dest.name,
        rel_path=storage.rel(dest),
        kind=kind,
        size_bytes=len(data),
        source=source,
        grader=(grader or "").strip(),
        note=(note or "").strip(),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


def generate_from_grade(db: Session, gid: int, *, on: str | None = None,
                        grader: str = "", note: str = "") -> GradeArchive:
    """把一份批改**导出成带笔迹的 PDF** 并存进学生目录。

    复用 `grade_export.export_grade`（它已经在出「导出 PDF」那份副本），
    不另写一套渲染 —— 两套画法迟早会出现「页面上下载的和存档里的是两张图」。

    ⚠️ 延迟 import：`grade_export` 会拉 `grade_store`（那是个大模块），
       顶层 import 会让「只想知道 kind 怎么判」的调用方也跟着拖一堆东西进来。
    """
    from . import grade_export, grade_store      # noqa: PLC0415 —— 见上

    try:
        path, suggested = grade_export.export_grade(db, gid, "pdf")
    except grade_store.GradeError as e:
        raise ArchiveError(e.status, str(e)) from e
    grade = grade_store.get_grade(db, gid)
    student = db.get(Student, grade.student_id) if grade.student_id else None
    if student is None:
        raise ArchiveError(404, "这份批改对应的学生已经不在了，没法归档到他的资料里")
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise ArchiveError(500, f"导出的 PDF 读不出来：{e}") from e
    return create(db, student=student, filename=suggested or "批改.pdf", data=data,
                  on=on, grade=grade, source="grade",
                  grader=grader, note=note)


# ---------------------------------------------------------------- 读
def get_or_404(db: Session, aid: int) -> GradeArchive:
    rec = db.get(GradeArchive, aid)
    if rec is None or rec.owner_id != config.OWNER_ID:
        raise ArchiveError(404, "这条批改存档不在了（可能已经被删掉）")
    return rec


def student_or_404(db: Session, sid: int) -> Student:
    stu = db.get(Student, sid)
    if stu is None:
        raise ArchiveError(404, "学生不存在")
    return stu


def abs_path(rec: GradeArchive) -> Path | None:
    """记录 -> 磁盘路径。**唯一入口**，越界返回 None（`safe_join` 保证）。"""
    return storage.safe_join(rec.rel_path)


def brief(rec: GradeArchive, topic: str = "") -> dict:
    """一条存档的摘要（列表用）—— 不带任何二进制，也不去猜预览能力之外的东西。"""
    return {
        "id": rec.id,
        "student_id": rec.student_id,
        "grade_id": rec.grade_id,
        "lesson_id": rec.lesson_id,
        "archived_on": rec.archived_on,
        "name": rec.name,
        "stored_name": rec.stored_name,
        "rel_path": rec.rel_path,
        "kind": rec.kind,
        "size_bytes": rec.size_bytes,
        "source": rec.source,
        "grader": rec.grader,
        "note": rec.note,
        "created_at": rec.created_at,
        # 前端据此决定「给个预览按钮」还是「给个下载按钮」。
        # ⚠️ 判断结果由后端给，不让前端按后缀自己猜 —— 两处规则迟早会不一致。
        "can_preview": rec.kind in PREVIEW_KINDS,
        "url": f"/api/grade-archives/{rec.id}/raw",
        "inline_url": f"/api/grade-archives/{rec.id}/raw?inline=1",
        "lesson_topic": topic,
    }


def list_for_student(db: Session, sid: int, limit: int = 200) -> dict:
    """这个学生的批改存档，**新的在前**（日期倒序，同一天按 id 倒序）。

    只给摘要（`brief`）：一条记录可能挂着 20MB 的 PDF，列表里绝不能带内容。
    """
    student_or_404(db, sid)
    rows = list(db.scalars(
        select(GradeArchive)
        .where(GradeArchive.student_id == sid, GradeArchive.owner_id == config.OWNER_ID)
        .order_by(GradeArchive.archived_on.desc(), GradeArchive.id.desc())
        .limit(max(1, min(limit, 1000)))
    ).all())

    # 课时主题一次查完（列表里要显示「哪节课的作业」）。逐条 db.get 是 N+1，
    # 一个学生攒够一年有两三百条存档，那会明显卡。
    lids = {r.lesson_id for r in rows if r.lesson_id}
    topics: dict[int, str] = {}
    if lids:
        for lid, topic in db.execute(
            select(Lesson.id, Lesson.topic).where(Lesson.id.in_(lids))
        ).all():
            topics[lid] = topic or ""

    items = [brief(r, topics.get(r.lesson_id or 0, "")) for r in rows]
    return {"items": items, "total": len(items)}


def detail(db: Session, aid: int) -> dict:
    """一条存档的完整信息 —— 含它关联的批改（判了几题、对几错几）。

    「点开记录就能看到学生这次作业的批改情况」：文件本身是正文，
    但这几行统计能让老师在打开 PDF 之前就知道该重点看哪里。
    """
    rec = get_or_404(db, aid)
    lesson = db.get(Lesson, rec.lesson_id) if rec.lesson_id else None
    out = brief(rec, (lesson.topic or "") if lesson else "")
    out["exists"] = bool((p := abs_path(rec)) and p.is_file())

    if rec.grade_id:
        g = db.get(HomeworkGrade, rec.grade_id)
        if g is not None:
            out["grade"] = {
                "id": g.id,
                "status": g.status,
                "done": g.done,
                "total": g.total,
                "failed": g.failed,
                "score": g.score,
                "full_score": g.full_score,
                "error": g.error,
                "reviewed_at": g.reviewed_at,
            }
            # 逐判定计数：一条 SQL 拿完，不把 items 全捞进内存（一份卷子几十题 × 字段一堆）
            rows = db.execute(
                select(HomeworkGradeItem.verdict, func.count())
                .where(HomeworkGradeItem.grade_id == g.id)
                .group_by(HomeworkGradeItem.verdict)
            ).all()
            out["verdicts"] = {str(v or "unknown"): int(n) for v, n in rows}
            out["item_count"] = sum(out["verdicts"].values())
    return out


# ---------------------------------------------------------------- 删
def remove(db: Session, aid: int) -> dict:
    """删一条存档 —— **连磁盘上的文件一起删**（那是这条记录的全部意义）。

    ⚠️ 顺序是「库先改、文件后删」（项目里凡是要同时动两处都这么排）：
       反过来一旦 commit 失败，库里会留一条指向已删文件的记录，
       而界面点开会 404 —— 那种错比「文件没删掉」难查得多。
    """
    rec = get_or_404(db, aid)
    p = abs_path(rec)
    db.delete(rec)
    db.commit()

    removed = False
    if p is not None and p.is_file():
        try:
            p.unlink()
            removed = True
        except OSError:
            removed = False
        # 删完把空掉的「批改 /」「日期 /」目录收掉（只删空目录，碰不到任何数据）
        storage.prune_empty_dirs([p.parent], stop=config.STUDENTS_DIR)
    return {"deleted": True, "file_removed": removed}


def known_grader_default() -> str:
    """默认批改人。自用单机没有登录体系，所以固定一个可读的值，
    前端可以传别的（比如「妈妈批的」这种代批场景）。"""
    return "我"
