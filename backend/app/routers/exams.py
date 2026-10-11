# -*- coding: utf-8 -*-
"""考试情况记录 —— 学生级（**不挂课**）的独立记录 + 试题/答题卡归档 + 考情分析。

需求原话（用户 2026-10-11）：
「每个学生还得加上考试情况记录（需要记录日期（不一定具体）、考试类型（周考\\月考\\期中考等）、
成绩、等级、试题与答题卡情况（会上传文件或图片，可能以缓存形式上传，上传后要能够删除）、
考情分析（要有至少两个模板供选择，例如：考察范围 试卷题目难易程度分析 老师上课所讲内容
学生答题情况 后期计划安排等））」

四条贯穿本文件的设计（**改这块之前请先读**）：

① **日期是自由文本**。老师说「9 月中旬那次月考」也是有效输入 —— 用日期控件逼他精确到日，
   他多半会直接放弃填这一栏。所以原样存他写的字，`date_key` 只是尽力解析出来的**排序键**。

② **附件先落暂存区，保存时才归档**。归档目录名里带日期和类型
   （`students/u1_x/exams/2026-09-15_月考/`），而这两样在填表过程中随时会改 ——
   边填边往正式目录里放的话，每改一次日期就得连人带文件搬一次家。
   暂存区的 key 与考试内容无关（随机 token），所以改多少次都不动文件。
   ⚠️ 传了没保存就关窗会留下暂存目录，由 `_prune_cache()` 按 48 小时清掉。

③ **保存时把文件搬进正式目录**；若老师在**已有附件**的情况下改了日期/类型，
   整个目录跟着改名（storage.move_dir_contents + 回写每条 stored）。
   不搬的话，之后再传的卷子会落到新目录，同一次考试的附件就散在两个文件夹里 ——
   这种错误**完全不报错**，只能靠人翻文件夹时发现「怎么少了一半」。

④ **试题/答题卡只归档、不抽文字**（status='archived'）。
   扫描件抽出来是一片乱码，比不读更容易误导；老师要的只是「那次月考的卷子在这儿」。
   所以状态如实写「没打算读」，不冒充「读出 0 字」。
"""
from __future__ import annotations

import json
import mimetypes
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Exam, ExamAnalysisTemplate, ExamFile, Student
from ..schemas import (
    ExamAnalysisTemplateIn,
    ExamCacheRemoveIn,
    ExamFileRoleIn,
    ExamIn,
    ExamPatch,
)
from ..services import file_text, storage

router = APIRouter(prefix="/api", tags=["exams"])

# 试卷扫描件比课件大（整卷双面扫描 PDF 很常见），所以上限放到 50MB。
MAX_BYTES = 50 * 1024 * 1024
# 暂存文件活 48 小时。比「关窗就清」宽松得多：老师可能填到一半去接孩子，
# 回来接着填还得在；但也不至于攒到把磁盘塞满。
CACHE_TTL_SECONDS = 48 * 3600

# 允许「就地显示」的扩展名。只放图片：inline + HTML/SVG 等于开了个 XSS 口子。
INLINE_OK_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 附件的三种归类。前两个直接对应需求里的「试题与答题卡情况」——
# 老师想找卷子和想找他那张答题卡，动机完全不同，合在一起会让两边都难找。
ROLE_LABEL = {"paper": "试题", "sheet": "答题卡", "other": "其他往来材料"}
ROLES = tuple(ROLE_LABEL)

# 常见的考试类型（界面上给 chips 一点即填，也允许自己敲，比如「入学分班考」）。
EXAM_TYPE_SUGGESTIONS = ["周考", "月考", "期中考", "期末考", "模拟考", "联考", "升学考"]

# 从自由文本里尽力抠年月：'2026-09-15' / '2026.9.15' / '2026年9月15日' 都认，
# 只写到月（'2026-09' / '2026年9月'）也认。**必须有月份**才算认出来 ——
# 光一个「2026 学年」说不清是哪次考试，与其排到一个不靠谱的位置，不如当作没填日期。
_DATE_KEY_RE = re.compile(r"(\d{4})\D{0,3}(\d{1,2})(?:\D{0,3}(\d{1,2}))?")


def date_key_of(text: str | None) -> str:
    """把「不一定具体」的日期原文变成**排序键**：'2026-09-15' / '2026-09' / ''。

    只用于排序，界面上显示的永远是老师写的原文。
    解析不出来（'9 月中旬'、'上学期期末'、'2026 学年'）返回空串 ——
    这类记录排到最后（见 list_exams），但**绝不因为「排不了序」就丢掉**。
    """
    m = _DATE_KEY_RE.search(str(text or ""))
    if not m:
        return ""
    year, month = m.group(1), int(m.group(2))
    if not 1 <= month <= 12:
        return year                          # 月份离谱（'2026-13'）时留个年份也比空着强
    day = m.group(3)
    if day and 1 <= int(day) <= 31:
        return f"{year}-{month:02d}-{int(day):02d}"
    return f"{year}-{month:02d}"


# ---------------------------------------------------------------- 小工具
def _student_or_404(db: Session, sid: int) -> Student:
    s = db.get(Student, sid)
    if s is None:
        raise HTTPException(404, "学生不存在")
    return s


def _exam_or_404(db: Session, eid: int) -> Exam:
    e = db.get(Exam, eid)
    if e is None:
        raise HTTPException(404, "这条考试记录不存在")
    return e


def _role_or_422(role: str) -> str:
    if role not in ROLES:
        raise HTTPException(422, f"不认识的附件归类 {role!r}（可选 {' / '.join(ROLES)}）")
    return role


def _student_of(db: Session, exam: Exam) -> Student | None:
    return db.get(Student, exam.student_id)


def _file_out(f: ExamFile) -> dict:
    return {
        "id": f.id,
        "exam_id": f.exam_id,
        "name": f.name,
        # 落盘名（可能因同名加了 _2）。界面显示它，老师在文件夹里看到的名字
        # 才和界面上是同一个；原名退到小字里，仍看得出「我选的是哪份」。
        "stored_name": Path(f.stored).name if f.stored else "",
        "role": f.role,
        "role_label": ROLE_LABEL.get(f.role, f.role),
        "kind": f.kind,
        "kind_cn": file_text.KIND_CN.get(f.kind, f.kind),
        "size_bytes": f.size_bytes,
        "status": f.status,
        "reason": f.reason or "",
        "url": f"/api/exam-files/{f.id}/raw",
        "created_at": f.created_at,
    }


def _analysis_load(raw: str | None) -> dict:
    """读考情分析。**坏数据一律退化成空壳，绝不抛异常** ——
    一份分析读不出来不该让整条考试记录打不开。"""
    try:
        v = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        v = {}
    if not isinstance(v, dict):
        v = {}
    secs = []
    for s in (v.get("sections") or []):
        if not isinstance(s, dict):
            continue
        secs.append({
            "key": str(s.get("key") or ""),
            "label": str(s.get("label") or ""),
            "hint": str(s.get("hint") or ""),
            "text": str(s.get("text") or ""),
        })
    return {
        "template_id": v.get("template_id"),
        "template_name": str(v.get("template_name") or ""),
        "sections": secs,
    }


def _analysis_dump(an) -> str:
    """写入前的归一化：段名（label）与 hint 一起快照存下来。

    模板日后被改名/删掉，这条老记录也得显示得出来 —— 与备课材料的 title 快照同理。
    """
    if an is None:
        return "{}"
    secs = []
    for s in (getattr(an, "sections", None) or []):
        if not isinstance(s, dict):
            continue
        secs.append({
            "key": str(s.get("key") or ""),
            "label": str(s.get("label") or ""),
            "hint": str(s.get("hint") or ""),
            "text": str(s.get("text") or ""),
        })
    return json.dumps({
        "template_id": getattr(an, "template_id", None),
        "template_name": str(getattr(an, "template_name", "") or ""),
        "sections": secs,
    }, ensure_ascii=False)


def _out(db: Session, e: Exam) -> dict:
    files = db.scalars(
        select(ExamFile).where(ExamFile.exam_id == e.id).order_by(ExamFile.id)
    ).all()
    # 得分率：有成绩**且有满分**才算得出来。算不出就给 None ——
    # 拿成绩当分母凑一个「100%」比空着更误导。
    rate = None
    if e.score is not None and e.full_score:
        try:
            rate = round(float(e.score) / float(e.full_score), 4)
        except (TypeError, ZeroDivisionError):
            rate = None
    return {
        "id": e.id,
        "student_id": e.student_id,
        "exam_date": e.exam_date or "",
        "exam_type": e.exam_type or "",
        "score": e.score,
        "full_score": e.full_score,
        "grade": e.grade or "",
        "score_rate": rate,
        "analysis": _analysis_load(e.analysis),
        "files": [_file_out(f) for f in files],
        "dir_rel": e.dir_rel or "",
        "created_at": e.created_at,
        "updated_at": e.updated_at,
    }


# ---------------------------------------------------------------- 暂存区
def _prune_cache(ttl: int = CACHE_TTL_SECONDS) -> int:
    """清掉超过 ttl 没被动过的暂存目录。

    为什么要有这一步：暂存的前提就是「传了可能最终不保存」（填一半关窗、
    换一份重传）。没有清理，这些文件会一直躺在磁盘上，
    而它们**不在数据库里**，任何按记录的清理逻辑都碰不到它们。
    在上传时顺手清（而不是开个后台任务）：单机自用，这一处就足够。
    """
    root = config.EXAM_CACHE_DIR
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for p in list(root.iterdir()):
        try:
            if now - p.stat().st_mtime <= ttl:
                continue
        except OSError:
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                continue
        removed += 1
    return removed


def _check_cache(items) -> list[tuple[Path, object]]:
    """检查暂存文件都还在，返回 [(磁盘路径, 入参)]；缺任何一个就 422。

    为什么缺文件要**整个报错**而不是静默跳过：老师已经选好的卷子如果悄悄没了，
    他会以为存进去了。宁可拦下来让他重传。
    """
    prepared, missing = [], []
    for it in (items or []):
        key = (getattr(it, "cache_key", "") or "").strip()
        if not key:
            continue
        src = storage.safe_cache_path(key)
        if src is None or not src.is_file():
            missing.append(Path(key).name or key)
            continue
        prepared.append((src, it))
    if missing:
        raise HTTPException(
            422,
            "这些文件已经不在暂存区了（可能超过 48 小时被自动清理）："
            + "、".join(missing) + "。请重新上传。")
    return prepared


def _ensure_dir_rel(stu: Student, e: Exam) -> str:
    """这次考试的归档目录（相对 UPLOAD_DIR）。

    **第一次落文件时才确定** —— 没传过附件的考试不该在磁盘上留一个空目录。
    同名（同一学生两场「2026-09_月考」）加 `_2`，靠 storage.unique_dir_name。
    """
    if e.dir_rel:
        return e.dir_rel
    folder = storage.student_folder(stu)
    parent = config.STUDENTS_DIR / folder / "exams"
    name = storage.unique_dir_name(parent, storage.exam_dir_label(e))
    e.dir_rel = f"students/{folder}/exams/{name}"
    return e.dir_rel


def _commit_files(db: Session, stu: Student, e: Exam, items) -> int:
    """把暂存区的附件搬进正式归档目录并登记，返回搬了几个。"""
    prepared = _check_cache(items)
    if not prepared:
        return 0
    rel = _ensure_dir_rel(stu, e)
    folder = storage.safe_join(rel)
    if folder is None:
        raise HTTPException(500, "归档路径不合法，没法保存附件")
    folder.mkdir(parents=True, exist_ok=True)
    n = 0
    for src, it in prepared:
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        stored_name = storage.unique_name(folder, src.name)
        shutil.move(str(src), str(folder / stored_name))
        # 原名以界面回传的为准（暂存文件名已经过安全洗字，极端名字会差一两个字符）
        orig = Path(getattr(it, "name", "") or src.name).name or src.name
        db.add(ExamFile(
            owner_id=config.OWNER_ID, exam_id=e.id, name=orig,
            stored=f"{rel}/{stored_name}", role=_role_or_422(getattr(it, "role", "other")),
            kind=file_text.kind_for_archive(orig), size_bytes=size,
            status="archived",      # 见文件头 ④：只归档，不抽文字
        ))
        n += 1
    # 搬空的暂存目录收掉（只删空目录，碰不到别的）
    storage.prune_empty_dirs({src.parent for src, _ in prepared}, stop=config.EXAM_CACHE_DIR)
    return n


def _retarget_dir(db: Session, stu: Student, e: Exam) -> str:
    """日期/类型改了 → 归档目录跟着改名（含磁盘上的文件与库里的 stored）。

    不搬的后果见文件头 ③：同一次考试的附件会散在两个文件夹里，且毫无提示。
    """
    if not e.dir_rel:
        return ""
    folder = storage.student_folder(stu)
    parent = config.STUDENTS_DIR / folder / "exams"
    old_name = Path(e.dir_rel).name
    canonical = storage.exam_dir_label(e)
    # ★ 关键的一步：目录名已经是「canonical」或「canonical_2 / _3」就算跟得上，直接收工。
    #   少了它就会出现下面这种灾难：某场考试因为撞名被分到 `2026-09-16_月考_2`，
    #   之后每次保存都会重算目录名 —— 而 canonical 已被别的考试占着，
    #   于是 _3、_4、_5 一路涨上去，每保存一次就搬一次家。
    #   数字后缀是**这个目录的身份**（谁先占就是谁的），改名时要把它带过去。
    if old_name == canonical or re.fullmatch(rf"{re.escape(canonical)}_\d+", old_name):
        return e.dir_rel
    # ignore=old_name：把自己排除掉，否则算出来的「新名字」可能撞上自己
    new_name = storage.unique_dir_name(parent, canonical, ignore=old_name)
    if new_name == old_name:
        return e.dir_rel
    base = f"students/{folder}/exams"
    old_rel, new_rel = e.dir_rel, f"{base}/{new_name}"
    old_dir, new_dir = storage.safe_join(old_rel), storage.safe_join(new_rel)
    if old_dir is None or new_dir is None:
        return e.dir_rel
    moved = storage.move_dir_contents(old_dir, new_dir)
    for f in db.scalars(select(ExamFile).where(ExamFile.exam_id == e.id)).all():
        old_stored = Path(f.stored or "").name
        if old_stored in moved:
            f.stored = f"{new_rel}/{moved[old_stored]}"
        # 没搬成功的（OSError）保持旧路径不动 —— 把库里改成不存在的路径更糟
    e.dir_rel = new_rel
    return new_rel


def _prune_exam_dirs(stu: Student | None, dir_rel: str) -> None:
    """收掉空掉的考试目录，但**停在学生文件夹**：绝不因为删了最后一个附件
    就把学生整个文件夹铲掉（那个文件夹是老师自己也在用的地方）。"""
    if stu is None or not dir_rel:
        return
    d = storage.safe_join(dir_rel)
    if d is None:
        return
    storage.prune_empty_dirs([d], stop=config.STUDENTS_DIR / storage.student_folder(stu))


def _student_stop(stu: Student | None) -> Path | None:
    """回收空目录时的止步线（学生文件夹本身）。

    ⚠️ 必须显式传给 storage.unlink_rels：它默认一路收到 STUDENTS_DIR 为止，
    会把「刚删空的学生文件夹」也收掉 —— 那个目录是老师自己也在用的地方
    （放讲义、翻资料），删掉它看起来就像学生的资料全丢了。
    """
    return None if stu is None else (config.STUDENTS_DIR / storage.student_folder(stu))


# ---------------------------------------------------------------- 附件暂存区
@router.post("/exams/upload")
async def upload_exam_file(file: UploadFile = File(...),
                           role: str = Query("other", description="paper / sheet / other")):
    """把一份试题/答题卡先放进暂存区，返回 cache_key。

    ⚠️ **这一步不写数据库、也不动正式归档目录** —— 保存这次考试时才搬过去。
    这样老师填表过程中随便改日期/类型都不会牵动文件（见文件头 ②）。

    上传后没保存也能删（POST /api/exams/upload/remove），所以
    「能贴就得能删」在这里同样成立。
    """
    _role_or_422(role)
    _prune_cache()                       # 顺手清过期暂存，不做后台任务
    name = Path(file.filename or "未命名").name        # 只取文件名，防路径穿越
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(422, f"文件太大了（超过 {MAX_BYTES // 1024 // 1024}MB）。"
                                 "整卷扫描件也常在这个量级以内，确认一下选错文件没有？")
    token = uuid.uuid4().hex[:12]
    d = config.EXAM_CACHE_DIR / token
    d.mkdir(parents=True, exist_ok=True)
    stored_name = storage.unique_name(
        d, storage.safe_token(Path(name).stem, "file") + Path(name).suffix.lower())
    (d / stored_name).write_bytes(data)
    kind = file_text.kind_for_archive(name)
    return {
        "cache_key": f"{token}/{stored_name}",
        "name": name,
        "role": role,
        "size_bytes": len(data),
        "kind": kind,
        "kind_cn": file_text.KIND_CN.get(kind, kind),
    }


@router.post("/exams/upload/remove")
def remove_exam_cache(payload: ExamCacheRemoveIn):
    """真删掉暂存区里一份还没保存的附件。

    和反馈配图那个 /feedbacks/image/remove 是同一个道理：只在界面上把它从列表里
    拿掉是**没用的**，文件会一直躺在硬盘上 —— 它不在数据库里，
    任何按记录的清理都碰不到它。所以「能贴就得能删」必须是真删。
    """
    p = storage.safe_cache_path(payload.cache_key)
    if p is None:
        raise HTTPException(422, "这不是暂存区里的文件")
    deleted = False
    if p.is_file():
        try:
            p.unlink()
            deleted = True
        except OSError:
            deleted = False
    storage.prune_empty_dirs([p.parent], stop=config.EXAM_CACHE_DIR)
    return {"deleted": deleted}


# ---------------------------------------------------------------- 考试记录
@router.get("/students/{sid}/exams")
def list_exams(sid: int, db: Session = Depends(get_db)):
    """这个学生的考试记录，**从新到旧**。

    排序规则（刻意分成两组，这样「为什么它在上面」永远说得清）：
      · 日期解析得出（'2026-09' / '2026-09-15'）→ 按**考试时间**从新到旧排在最前
      · 解析不出（'9 月中旬'、'上学期期末'）→ 排在**最后**，彼此按记录时间排
    不把没日期的混进日期序列里：那样一次「9 月中旬」会插到 10 月考试的上头，
    看起来像排错了（它其实只是不知道排哪）。
    """
    _student_or_404(db, sid)
    rows = list(db.scalars(select(Exam).where(Exam.student_id == sid)).all())
    dated = [e for e in rows if (e.date_key or "").strip()]
    undated = [e for e in rows if not (e.date_key or "").strip()]
    dated.sort(key=lambda e: e.date_key, reverse=True)
    undated.sort(key=lambda e: e.created_at or "", reverse=True)
    ordered = dated + undated
    types = sorted({e.exam_type for e in rows if (e.exam_type or "").strip()})
    return {"items": [_out(db, e) for e in ordered],
            "type_suggestions": EXAM_TYPE_SUGGESTIONS,
            "used_types": types}


@router.post("/exams")
def create_exam(payload: ExamIn, db: Session = Depends(get_db)):
    stu = _student_or_404(db, payload.student_id)
    e = Exam(
        owner_id=config.OWNER_ID,
        student_id=stu.id,
        exam_date=(payload.exam_date or "").strip(),
        exam_type=(payload.exam_type or "").strip(),
        score=payload.score,
        full_score=payload.full_score,
        grade=(payload.grade or "").strip(),
        analysis=_analysis_dump(payload.analysis),
    )
    e.date_key = date_key_of(e.exam_date)
    db.add(e)
    db.flush()                                  # 要 e.id 才能登记附件
    _commit_files(db, stu, e, payload.files)
    db.commit()
    return _out(db, e)


@router.patch("/exams/{eid}")
def patch_exam(eid: int, payload: ExamPatch, db: Session = Depends(get_db)):
    """改一次考试记录。**没传的字段 = 不改**（想清空就显式传 null / 空串）。

    顺序有讲究：先落附件、再搬目录。
    反过来先搬目录的话，万一附件校验失败（暂存文件被清了 → 422），
    磁盘已经搬过了而事务没提交 —— 库和磁盘就对不上了。
    """
    e = _exam_or_404(db, eid)
    stu = _student_or_404(db, e.student_id)
    given = payload.model_dump(exclude_unset=True)

    if "exam_date" in given:
        e.exam_date = (payload.exam_date or "").strip()
        e.date_key = date_key_of(e.exam_date)
    if "exam_type" in given:
        e.exam_type = (payload.exam_type or "").strip()
    if "score" in given:
        e.score = payload.score
    if "full_score" in given:
        e.full_score = payload.full_score
    if "grade" in given:
        e.grade = (payload.grade or "").strip()
    if "analysis" in given:
        e.analysis = _analysis_dump(payload.analysis)

    if payload.files:
        _commit_files(db, stu, e, payload.files)
    if "exam_date" in given or "exam_type" in given:
        _retarget_dir(db, stu, e)

    e.updated_at = datetime.now().isoformat(timespec="seconds")
    db.commit()
    return _out(db, e)


@router.delete("/exams/{eid}")
def delete_exam(eid: int, db: Session = Depends(get_db)):
    """删掉这次考试记录 —— **连同它的试题/答题卡一起**。

    为什么这里敢连文件一起删：这些附件是这次考试的专属材料（不是共享的题库资源），
    「删掉这次考试」在老师心里就是「这摊东西不要了」。删之前先把路径查出来，
    否则 CASCADE 删完就再也找不到磁盘上该删哪个文件（数据库管不了文件系统）。
    """
    e = _exam_or_404(db, eid)
    stu = _student_of(db, e)
    rels = [f.stored for f in db.scalars(
        select(ExamFile).where(ExamFile.exam_id == eid)).all() if f.stored]
    dir_rel = e.dir_rel or ""
    db.execute(delete(ExamFile).where(ExamFile.exam_id == eid))
    db.delete(e)
    db.commit()
    storage.unlink_rels(rels, stop=_student_stop(stu))
    _prune_exam_dirs(stu, dir_rel)
    return {"ok": True}


# ---------------------------------------------------------------- 附件（已归档）
@router.delete("/exam-files/{fid}")
def delete_exam_file(fid: int, db: Session = Depends(get_db)):
    """删一份已归档的附件（磁盘上的原件一起删）。"""
    f = db.get(ExamFile, fid)
    if f is None:
        raise HTTPException(404, "文件不存在")
    exam = db.get(Exam, f.exam_id)
    stu = _student_of(db, exam) if exam is not None else None
    stored = f.stored
    dir_rel = exam.dir_rel if exam is not None else ""
    db.delete(f)
    db.commit()
    storage.unlink_rels([stored] if stored else [], stop=_student_stop(stu))
    _prune_exam_dirs(stu, dir_rel)
    return {"ok": True}


@router.patch("/exam-files/{fid}")
def patch_exam_file(fid: int, payload: ExamFileRoleIn, db: Session = Depends(get_db)):
    """改一份附件的归类（试题 / 答题卡 / 其他）—— 传错了不用删了重传。"""
    f = db.get(ExamFile, fid)
    if f is None:
        raise HTTPException(404, "文件不存在")
    f.role = _role_or_422(payload.role)
    db.commit()
    return _file_out(f)


@router.get("/exam-files/{fid}/raw")
def download_exam_file(
    fid: int,
    inline: bool = Query(False, description="图片可以就地显示（预览用）；其它类型一律下载"),
    db: Session = Depends(get_db),
):
    """取回原件。

    `inline=1` 只对**图片**生效：答题卡拍的照片点一下就该看见，
    而不是先存进下载目录。其它类型一律 attachment ——
    inline + HTML/SVG 等于给自己开一个 XSS 口子（上传者能控制内容）。
    再带一条 `X-Content-Type-Options: nosniff`：不靠浏览器猜类型。
    """
    f = db.get(ExamFile, fid)
    if f is None or not f.stored:
        raise HTTPException(404, "文件不存在")
    p = storage.safe_join(f.stored)
    if p is None or not p.exists() or not p.is_file():
        raise HTTPException(404, "原件已不在磁盘上")
    name = f.name or p.name
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    disp = "inline" if (inline and p.suffix.lower() in INLINE_OK_EXT) else "attachment"
    return FileResponse(
        str(p), media_type=media, filename=name,
        headers={"Content-Disposition":
                 f"{disp}; filename=file; filename*=UTF-8''{quote(name)}",
                 "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------- 考情分析模板
def _template_out(t: ExamAnalysisTemplate) -> dict:
    try:
        secs = json.loads(t.sections or "[]")
    except (json.JSONDecodeError, TypeError):
        secs = []
    if not isinstance(secs, list):
        secs = []
    clean = []
    for s in secs:
        if not isinstance(s, dict):
            continue
        clean.append({
            "key": str(s.get("key") or ""),
            "label": str(s.get("label") or ""),
            "hint": str(s.get("hint") or ""),
        })
    return {
        "id": t.id,
        "name": t.name,
        "sections": clean,
        "is_builtin": bool(t.is_builtin),
        "sort_order": t.sort_order,
    }


def _clean_sections(sections) -> str:
    clean = []
    for s in (sections or []):
        if not isinstance(s, dict):
            continue
        label = str(s.get("label") or "").strip()
        if not label:
            continue                     # 没有标题的段在界面上是一块无字空白，没有意义
        clean.append({
            "key": str(s.get("key") or ""),
            "label": label,
            "hint": str(s.get("hint") or "").strip(),
        })
    return json.dumps(clean, ensure_ascii=False)


@router.get("/exam-analysis-templates")
def list_exam_templates(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ExamAnalysisTemplate)
        .where(ExamAnalysisTemplate.owner_id == config.OWNER_ID)
        .order_by(ExamAnalysisTemplate.sort_order, ExamAnalysisTemplate.id)
    ).all()
    return {"items": [_template_out(t) for t in rows]}


@router.post("/exam-analysis-templates")
def create_exam_template(payload: ExamAnalysisTemplateIn, db: Session = Depends(get_db)):
    """从当前这份分析「另存为模板」也走这里 —— 调顺手了就存下来，是最自然的生产方式。"""
    t = ExamAnalysisTemplate(
        owner_id=config.OWNER_ID,
        name=(payload.name or "").strip() or "未命名模板",
        sections=_clean_sections(payload.sections),
        is_builtin=0,
        sort_order=payload.sort_order or 100 + len(
            db.scalars(select(ExamAnalysisTemplate)).all()),
    )
    db.add(t)
    db.commit()
    return _template_out(t)


@router.patch("/exam-analysis-templates/{tid}")
def patch_exam_template(tid: int, payload: ExamAnalysisTemplateIn,
                        db: Session = Depends(get_db)):
    t = db.get(ExamAnalysisTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    t.name = (payload.name or "").strip() or t.name
    # sections 没传 = 不动（只改名字不该把段落结构清掉）
    if payload.sections is not None:
        t.sections = _clean_sections(payload.sections)
    if payload.sort_order:
        t.sort_order = payload.sort_order
    db.commit()
    return _template_out(t)


@router.delete("/exam-analysis-templates/{tid}")
def delete_exam_template(tid: int, db: Session = Depends(get_db)):
    t = db.get(ExamAnalysisTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    if t.is_builtin:
        # 删了下次启动种子又会建回来，反而让人迷惑 —— 与反馈模板/卷种样式同一规矩
        raise HTTPException(400, "内置模板不能删除；可以改它的段落，或另存一个新模板")
    db.delete(t)
    db.commit()
    return {"ok": True}
