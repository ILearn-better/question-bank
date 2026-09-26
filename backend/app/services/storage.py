# -*- coding: utf-8 -*-
"""上课资料的归档布局 —— 按「学生 / 日期」放，为后续做学生情况分析留下可读的素材。

布局：
    data/uploads/students/
      u1_李芹旭/                    ← 目录名带学生 id（重名不会撞）+ 名字（人翻得懂）
        2026-09-26/                 ← 按**上课日期**分（不是上传日期）
          讲义.pdf                  一节课的材料全在同一格里
          fb_ab12cd34ef56.png      ← 反馈配图（板书照片、作业截图）

为什么按上课日期而不是上传日期：这个目录是给人看的。要回顾「9 月 26 号那节课讲了什么」，
按上课日期分才找得到；按上传日期分会出现「同一节课的材料散在好几天里」。

数据库里存的是**相对 UPLOAD_DIR 的路径**（如 `students/u1_李芹旭/2026-09-26/讲义.pdf`），
不是绝对路径 —— 换机器、搬数据目录都不会失效（和 config.to_rel_path 一个思路）。

⚠️ 路径拼接一律走 safe_join()：文件名与学生姓名都来自用户，必须挡掉 `../` 与绝对路径。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config

# Windows / 类 Unix 都不允许的字符，外加路径分隔符与控制字符
_BAD = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]')
_DATE_OK = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def safe_token(text: str | None, fallback: str = "x", maxlen: int = 40) -> str:
    """把用户给的文字变成安全的**单个路径片段**（不含分隔符）。

    只取 Path.name 不够：中文名、表情、`..` 都可能出现在里面，
    而这个片段要直接拼进磁盘路径，所以统一白名单式地洗一遍。
    """
    s = _BAD.sub("_", str(text or "")).strip(". ")
    s = re.sub(r"_{2,}", "_", s)
    return (s[:maxlen] or fallback)


def student_folder(student) -> str:
    """学生目录名：`u<id>_<姓名>`。

    带 id 是为了重名（两个「小李」）与改名后不与别人混淆；带姓名是为了人能一眼认出来。
    """
    return f"u{student.id}_{safe_token(getattr(student, 'name', ''), 'student')}"


def lesson_date(lesson) -> str:
    """课时的日期串。start_at 形如 '2026-09-26T15:00'。"""
    raw = str(getattr(lesson, "start_at", "") or "")[:10]
    if _DATE_OK.match(raw):
        return raw
    from datetime import date
    return date.today().isoformat()      # 兜底：日期坏了也不能把文件写丢


def dated_rel(student, lesson) -> str:
    """这个学生这一节课的归档目录（相对 UPLOAD_DIR，正斜杠）。"""
    return f"students/{student_folder(student)}/{lesson_date(lesson)}"


def dir_for(student, lesson) -> Path:
    """拿到归档目录并确保存在。"""
    d = config.UPLOAD_DIR / dated_rel(student, lesson)
    d.mkdir(parents=True, exist_ok=True)
    return d


def remove_student_dir(student) -> int:
    """删掉这个学生的整个归档目录，返回删掉了几个文件。

    为什么整目录删、而不是照着数据库逐个文件删：老师删学生时想的是「这个人连同他的资料
    一起没」。而且反馈配图是**上传即落盘、保存反馈才登记**的 —— 上传完没保存就关窗的图，
    数据库里查不到（lesson_files.cleanup_for_lessons 也自然漏掉它们），逐个删必然留下残渣。
    目录名已经过 safe_token 洗过，这里再确认一次落在归档根之内才动手。
    """
    d = config.STUDENTS_DIR / student_folder(student)
    if d == config.STUDENTS_DIR:            # 兜底：宁可删不掉，也绝不能铲到归档根
        return 0
    try:
        d.relative_to(config.STUDENTS_DIR)
    except ValueError:
        return 0
    if not d.is_dir():
        return 0
    n = sum(1 for p in d.rglob("*") if p.is_file())
    shutil.rmtree(d, ignore_errors=True)
    return n


def unique_name(directory: Path, filename: str) -> str:
    """同名文件加 `_2`、`_3` —— 一天里传两份「讲义.pdf」是完全正常的。"""
    name = Path(filename).name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    cand, i = name, 1
    while (directory / cand).exists():
        i += 1
        cand = f"{stem}_{i}{dot}{ext}"
    return cand


def rel(path: Path) -> str:
    """绝对路径 -> 相对 UPLOAD_DIR 的正斜杠路径（入库用）。"""
    return path.resolve().relative_to(config.UPLOAD_DIR.resolve()).as_posix()


def safe_join(relative: str | None) -> Path | None:
    """把库里的相对路径还原成磁盘路径；**任何越界一律返回 None**。

    读取接口的参数是客户端可控的，所以这里是唯一入口：
    去掉前导斜杠、拒绝 `..` 片段、解析后必须仍在 UPLOAD_DIR 之内。
    """
    if not relative:
        return None
    rel_path = str(relative).replace("\\", "/").strip().lstrip("/")
    if not rel_path or ".." in rel_path.split("/"):
        return None
    root = config.UPLOAD_DIR.resolve()
    p = (root / rel_path).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p


def collect_lesson_assets(db: Session, lesson_ids: list[int]) -> list[str]:
    """这些课时名下所有素材的相对路径（反馈配图 + 上课文件）。

    ⚠️ 必须在**删行之前**调用：lesson_files 与 feedbacks 都会随外键 CASCADE 一起消失，
    删完就再也查不到磁盘上该删哪几个文件了。数据库管不了文件系统，这一步只能自己做。
    """
    from ..models import Feedback, LessonFile

    out: list[str] = []
    if not lesson_ids:
        return out
    for (raw,) in db.execute(
        select(Feedback.images).where(Feedback.lesson_id.in_(lesson_ids))
    ).all():
        try:
            import json
            for u in json.loads(raw or "[]"):
                p = url_to_rel(u)
                if p:
                    out.append(p)
        except Exception:  # noqa: BLE001
            continue
    for (stored,) in db.execute(
        select(LessonFile.stored).where(LessonFile.lesson_id.in_(lesson_ids))
    ).all():
        if stored:
            out.append(stored)
    return out


def prune_empty_dirs(dirs, stop: Path | None = None) -> int:
    """把空掉的日期目录 / 学生目录收掉。

    只删**空**目录，所以不可能碰到数据 —— 老师删掉一份文件后，如果那一格空了，
    留一个空壳只会让人以为「这里本来有东西」。往上走到 stop（默认 STUDENTS_DIR）为止。
    """
    stop = (stop or config.STUDENTS_DIR)
    removed = 0
    seen = set()
    for d in dirs or []:
        try:
            cur = Path(d)
        except TypeError:
            continue
        while cur and cur not in seen:
            seen.add(cur)
            try:
                cur.relative_to(stop)
            except ValueError:
                break                       # 已经走出归档根，停手
            if cur == stop:
                break
            try:
                cur.rmdir()                 # 非空会抛 OSError，正好当作「别删」的信号
                removed += 1
            except OSError:
                break
            cur = cur.parent
    return removed


def unlink_rels(rels: list[str]) -> int:
    """删掉这些相对路径对应的文件，返回删掉几个。越界的路径直接忽略。

    顺手把因此空掉的目录也收掉（见 prune_empty_dirs）。
    """
    n = 0
    parents = set()
    for r in rels or []:
        p = safe_join(r)
        if p is None:
            continue
        parents.add(p.parent)
        try:
            if p.is_file():
                p.unlink()
                n += 1
        except OSError:
            pass
    prune_empty_dirs(parents)
    return n


# 反馈配图的 URL 形如 /api/feedbacks/files/students/u1_x/2026-09-26/fb_ab.png
_FEEDBACK_URL_RE = re.compile(r"/api/feedbacks/files/(.+)$")


def url_to_rel(url: str | None) -> str | None:
    """反馈配图的 URL -> 相对路径。只认本项目自己的 URL 前缀。"""
    m = _FEEDBACK_URL_RE.search(str(url or ""))
    if not m:
        return None
    # 允许 URL 里出现百分号编码（前端有时会编码中文学生名）
    from urllib.parse import unquote
    return unquote(m.group(1))
