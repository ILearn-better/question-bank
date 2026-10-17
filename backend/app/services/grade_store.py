# -*- coding: utf-8 -*-
"""作业批改的 **I/O 层**：建任务、逐页批改、老师改动落库、标注增删。

边界（docs/AI批改作业-实现计划.md §8）：
  · 本文件**不调模型** —— 那是 `services/grade_ai.py`（并行开发中），这里只按
    `recognize_page(png_path, ctx)` 的签名去调，缺文件时如实失败，不抛 500。
  · 本文件**不认识 HTTP** —— 那是 `routers/homework_grades.py`。业务错误一律抛
    `GradeError`，由路由翻成状态码。
  · 本文件是**唯一**决定「一页怎么变成 N 道题」的地方。

三条和这个功能绑死、改错就不会报错只会慢慢烂掉的不变量：

  1. **`total` / `done` / `failed` 记的是页数，不是题目数**（同 `batch_page` 的坑）。
     一页出 0 道题也算这一页 done —— 空白页确实被读完了。
  2. **进度与逐题结果是两件事**：`status='running'` 时 items 是半截的；
     跑完是 `reviewing`（等人看），老师点确认才是 `done`。
     **没确认前任何地方都不该把这里的结果当定论**，所以确认前绝不回填 `score` 汇总。
  3. **改转写文本 = 旧判定作废**（`_invalidate_transcription`）。这是
     `transcription_version` 存在的唯一理由，别在别处"顺手"改 `student_answer`。
"""
from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config, taxonomy
from ..adapters import pdf as pdf_adapter
from ..db import SessionLocal
from ..models import (
    HomeworkGrade,
    HomeworkGradeFile,
    HomeworkGradeItem,
    HomeworkGradeMark,
    Lesson,
    LessonFile,
    Question,
    Student,
)
from ..schemas import (
    GradeConfirmIn,
    GradeIn,
    GradeItemPatch,
    GradeMarkIn,
    GradeMarkPatch,
    GradeSetupPatch,
    GradeToBankIn,
)
from . import knowledge, storage

# 页 = 一次模型调用。并发数与 batch_page 同一个量级：再高只是把模型的限流打满。
WORKERS = 3
# 一次批改最多多少页。这不是模型限制，是**成本与等待**的护栏：
# 一份作业几十页多半是选错文件（整套练习册），等十分钟不如先问一句。
MAX_PAGES = 30

VERDICTS = ("correct", "partial", "wrong", "blank", "ungradable")

# 可以直接当"一页"渲染的图片格式（与 routers/documents.py 的 PAGE_IMAGE_EXTS 同制）。
# 不进那个常量是分层问题：services 不该 import routers。
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


class GradeError(Exception):
    """业务错误。`message` 是给老师看的人话，路由层原样放进 `detail`。

    ⚠️ 参数顺序是 **(status, message)** —— 全文件 31 处调用都是这个顺序，
    别改成 (message, status)：那样每个错误的状态码都会变成一句中文，
    FastAPI 拿它当 status_code 时会直接炸。
    """

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 路径
def grade_dir(gid: int) -> Path:
    """这份批改自己的目录（粘贴的答案文本 + 页图缓存）。

    ⚠️ **缓存必须按 gid 分目录**：`render_page` 的缓存名是 `p{pno}.png`，
    不同任务共用目录会直接串页（第二份作业的第 1 页命中第一份的图）。
    这里再往下按**文件**分一层，理由一样：一次交两张照片，两张都有"第 1 页"。
    """
    return config.UPLOAD_DIR / "grades" / str(int(gid))


def _page_cache_dir(gid: int, file_id: int) -> Path:
    return grade_dir(gid) / "pages" / f"f{int(file_id)}"


# ---------------------------------------------------------------- 小工具
def _json_list(raw: str | None) -> list:
    """库里 tags/flags/geo 都是 TEXT，读出来一律 list（坏数据当空表，别让渲染整页崩）。"""
    try:
        v = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def _num(v) -> float | None:
    """分数：**缺数据就是 None，绝不用 0**。0 分和"没判"混成一个数会带偏正确率。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _qtype(v) -> str | None:
    v = (str(v or "")).strip()
    return v if v in taxonomy.QTYPES else None


def _verdict(v) -> str:
    """不认识的判定一律落 `ungradable` —— 落进 `wrong` 就开始冤枉人了。"""
    v = str(v or "").strip().lower()
    return v if v in VERDICTS else "ungradable"


def _confidence(v) -> str:
    v = str(v or "").strip().lower()
    return v if v in taxonomy.CONFIDENCES else "low"


# ---------------------------------------------------------------- 序列化
def brief(grade: HomeworkGrade) -> dict:
    """列表用的简报：**不含 items**（一节课几十条批改，带 items 会把响应撑成几 MB）。"""
    return {
        "id": grade.id,
        "student_id": grade.student_id,
        "lesson_id": grade.lesson_id,
        "status": grade.status,
        "answer_mode": grade.answer_mode,
        "total": grade.total,
        "done": grade.done,
        "failed": grade.failed,
        "score": grade.score,
        "full_score": grade.full_score,
        "error": grade.error or "",
        "created_at": grade.created_at,
        "finished_at": grade.finished_at,
        "reviewed_at": grade.reviewed_at,
    }


def file_dict(f: HomeworkGradeFile) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "role": f.role,
        "kind": f.kind,
        "size_bytes": f.size_bytes,
        "pages": f.pages,
    }


def item_dict(it: HomeworkGradeItem) -> dict:
    return {
        "id": it.id,
        "seq": it.seq,
        "page_no": it.page_no,
        "q_no": it.q_no,
        "qtype": it.qtype,
        "stem": it.stem,
        "student_answer": it.student_answer,
        "correct_answer": it.correct_answer,
        "verdict": it.verdict,
        "score": it.score,
        "full_score": it.full_score,
        "basis": it.basis,
        "confidence": it.confidence,
        "kp_name": it.kp_name,
        "tags": _json_list(it.tags),
        "error_reason": it.error_reason,
        "flags": _json_list(it.flags),
        "status": it.status,
        "question_id": it.question_id,
    }


def mark_dict(m: HomeworkGradeMark) -> dict:
    return {
        "id": m.id,
        "item_id": m.item_id,
        "page_no": m.page_no,
        "kind": m.kind,
        "geo": _json_list(m.geo),
        "points": _json_list(m.points),
        "text": m.text,
        "color": m.color,
        "created_by": m.created_by,
    }


def detail(db: Session, grade: HomeworkGrade) -> dict:
    out = brief(grade)
    items = db.scalars(
        select(HomeworkGradeItem)
        .where(HomeworkGradeItem.grade_id == grade.id)
        .order_by(HomeworkGradeItem.seq, HomeworkGradeItem.id)
    ).all()
    files = db.scalars(
        select(HomeworkGradeFile)
        .where(HomeworkGradeFile.grade_id == grade.id)
        .order_by(HomeworkGradeFile.id)
    ).all()
    marks = db.scalars(
        select(HomeworkGradeMark)
        .where(HomeworkGradeMark.grade_id == grade.id)
        .order_by(HomeworkGradeMark.page_no, HomeworkGradeMark.id)
    ).all()
    out["items"] = [item_dict(i) for i in items]
    out["files"] = [file_dict(f) for f in files]
    out["marks"] = [mark_dict(m) for m in marks]
    # pages 是 total 的别名：前端按 1..pages 逐张去取 /page/{pno}。
    # 不另立第二套"总页数"概念 —— 两个字段各存一份，迟早有一个忘更新。
    out["pages"] = grade.total
    # 判定依据的原文，给批改页回显用（老师切到「AI 自己解」时也要能看见自己填过的答案，
    # 所以这里**不受 answer_mode 影响** —— respect_mode=False）。
    out["answer_text"], out["rubric_text"] = _answer_texts(db, grade, respect_mode=False)
    return out


def list_grades(db: Session, lesson_id: int | None = None) -> list[dict]:
    stmt = select(HomeworkGrade).where(HomeworkGrade.owner_id == config.OWNER_ID)
    if lesson_id is not None:
        stmt = stmt.where(HomeworkGrade.lesson_id == int(lesson_id))
    rows = db.scalars(stmt.order_by(HomeworkGrade.id.desc())).all()
    return [brief(g) for g in rows]


def get_grade(db: Session, gid: int) -> HomeworkGrade:
    g = db.get(HomeworkGrade, int(gid))
    if g is None:
        raise GradeError(404, "批改记录不存在")
    return g


# ---------------------------------------------------------------- 建任务
def _lesson_file(db: Session, fid: int, lesson_id: int, what: str) -> LessonFile:
    lf = db.get(LessonFile, int(fid))
    if lf is None:
        raise GradeError(404, f"{what}（文件 #{fid}）不存在")
    if lf.lesson_id != lesson_id:
        raise GradeError(422, f"{what}「{lf.name}」不属于这节课")
    if not (lf.stored or "").strip():
        raise GradeError(422, f"{what}「{lf.name}」在磁盘上没有原件")
    return lf


def _stored_path(lf: LessonFile) -> Path | None:
    return storage.safe_join(lf.stored)


def _page_count(lf: LessonFile) -> int:
    """这份提交件有几页。**在这里算而不是信 `lesson_files.pages`**：
    那个字段是抽文字时顺手记的，图片类压根没抽（pages 是 NULL），而批改的进度是按页算的。
    PDF 问适配层，图片天然是 1 页（pymupdf 打开图片就是单页文档）。
    """
    suffix = Path(lf.name or lf.stored or "").suffix.lower()
    path = _stored_path(lf)
    if path is None or not path.exists():
        raise GradeError(422, f"提交件「{lf.name}」的原件不在磁盘上了，请重新上传")
    if suffix == ".pdf":
        try:
            return max(1, int(pdf_adapter.page_count(str(path))))
        except Exception as e:                       # noqa: BLE001
            raise GradeError(422, f"「{lf.name}」打不开（{type(e).__name__}: {e}）") from e
    if suffix in _IMAGE_EXTS:
        return 1
    # Word / PPT 这类没有现成的页面视图（要拉起 Word 转 PDF），一期不做。
    # **明说**而不是偷偷跳过 —— 悄悄少一份提交件，老师会以为批过了。
    raise GradeError(
        422,
        f"「{lf.name}」这种格式暂时不能按页批改（需要 PDF 或照片）。"
        "Word 可以先另存成 PDF 再交。",
    )


def _add_answer_text_file(db: Session, grade: HomeworkGrade, text: str, role: str, name: str) -> None:
    """把老师**粘贴**的答案/细则落成 grade 目录下的一个小 txt。

    为什么走文件行而不是加列：`homework_grades` 没有 answer_text/rubric_text 两列
    （契约里也没有，加列等于要动迁移），而 `homework_grade_files` 本来就有
    `role='answer'|'rubric'` + `stored`。落成 txt 还有个好处：老师在文件夹里翻得到。
    """
    d = grade_dir(grade.id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{role}.txt"
    p.write_bytes(text.encode("utf-8-sig"))          # 带 BOM：记事本打开中文不乱码
    body = text.encode("utf-8-sig")
    db.add(HomeworkGradeFile(
        owner_id=config.OWNER_ID, grade_id=grade.id, lesson_file_id=None,
        name=name, stored=storage.rel(p), role=role, kind="text",
        size_bytes=len(body), pages=0,
    ))


def create_grade(db: Session, payload: GradeIn) -> HomeworkGrade:
    """建一次批改任务。**只建不跑** —— 跑是 `POST /{gid}/run`。

    为什么不建完就地开跑：批改要花几十秒到几分钟，前端得先拿到 gid 才能跳转、
    才能轮询。而且"建了不跑"这个中间态本身有用（先选好文件，回头再点批）。
    """
    lesson = db.get(Lesson, payload.lesson_id)
    if lesson is None:
        raise GradeError(404, "课时记录不存在")

    sub_ids = list(dict.fromkeys(int(x) for x in payload.file_ids))
    if not sub_ids:
        raise GradeError(422, "还没选要批的作业文件")
    sub_files = [_lesson_file(db, fid, lesson.id, "提交件") for fid in sub_ids]
    ans_files = [_lesson_file(db, fid, lesson.id, "答案文件")
                 for fid in dict.fromkeys(int(x) for x in payload.answer_file_ids)]

    answer_text = (payload.answer_text or "").strip()
    rubric_text = (payload.rubric_text or "").strip()
    if payload.answer_mode == "key" and not (answer_text or ans_files):
        raise GradeError(422, "选了「老师给答案」，但答案还是空的 —— 粘一段文字，或传一份答案文件")
    if payload.answer_mode == "rubric" and not (rubric_text or ans_files):
        raise GradeError(422, "选了「评分细则」，但细则还是空的 —— 每步多少分、什么情况扣分，得写下来")
    # 答案文件必须是**能读出文字**的：批改靠文本比对，照片里的答案没有文字就等于没给，
    # 模型会退回"自己解题"却仍然显示「按老师答案判」—— 那是拿错标准批学生，必须当场拦住。
    for lf in ans_files:
        if not (lf.text or "").strip():
            raise GradeError(
                422,
                f"答案文件「{lf.name}」里没有可读的文字"
                f"（{lf.reason or '照片/扫描件不抽文字'}）—— 请把答案直接粘贴进文本框",
            )

    total = sum(_page_count(lf) for lf in sub_files)
    if total > MAX_PAGES:
        raise GradeError(422, f"一次最多批 {MAX_PAGES} 页，这次选了 {total} 页。请分批批改。")

    grade = HomeworkGrade(
        owner_id=config.OWNER_ID,
        student_id=lesson.student_id,
        lesson_id=lesson.id,
        curriculum_id=payload.curriculum_id,
        status="queued",                    # 建了就等着，跑不跑由 /run 决定
        answer_mode=payload.answer_mode,
        total=total, done=0, failed=0,
        error="",
        created_at=_now(),
    )
    db.add(grade)
    db.flush()                              # 先拿 id：目录名与文件行都要用它

    grade.dir_rel = storage.rel(grade_dir(grade.id))
    grade_dir(grade.id).mkdir(parents=True, exist_ok=True)

    # 提交件**引用不复制**：学生交的照片早就在「交作业」那一步归档过了，
    # 同一张照片在磁盘上只该有一份。stored 留空就是这个意思（下面读的时候回查 lesson_files）。
    for lf in sub_files:
        db.add(HomeworkGradeFile(
            owner_id=config.OWNER_ID, grade_id=grade.id, lesson_file_id=lf.id,
            name=lf.name, stored="", role="submission", kind=lf.kind,
            size_bytes=lf.size_bytes or 0, pages=_page_count(lf),
        ))
    # 答案/细则：粘贴的文字落 txt，引用的文件同样只引用。
    # rubric 模式下这批文件是**细则**，不是答案 —— 两者在 ctx 里走不同字段。
    ans_role = "rubric" if payload.answer_mode == "rubric" else "answer"
    if answer_text:
        _add_answer_text_file(db, grade, answer_text, "answer", "答案（粘贴）")
    if rubric_text:
        _add_answer_text_file(db, grade, rubric_text, "rubric", "评分细则（粘贴）")
    for lf in ans_files:
        db.add(HomeworkGradeFile(
            owner_id=config.OWNER_ID, grade_id=grade.id, lesson_file_id=lf.id,
            name=lf.name, stored="", role=ans_role, kind=lf.kind,
            size_bytes=lf.size_bytes or 0, pages=lf.pages or 0,
        ))

    db.commit()
    return grade


def patch_setup(db: Session, gid: int, payload: GradeSetupPatch) -> HomeworkGrade:
    """开批**之前**改「判定依据」（A1 已定：答案是老师给的，不是模型自己编的）。

    为什么需要这个口子：老师点「AI 批改」时手上还没有填答案的地方 —— 他是跳到批改页、
    看过页图之后才决定「这次按我的答案判」。所以必须允许**建完再补**，
    否则 A1 那个决定在界面上就没有落脚点。

    硬规则：**只有还没批出结果时能改**（queued / failed 且 done == 0）。
    已经批出结果再换依据，那批结果就是"按旧标准批的"，
    必须走「重新批改」把它清掉重来 —— 否则老师会以为依据改了、其实判定还是旧的。
    """
    grade = get_grade(db, gid)
    if grade.done or grade.status not in ("queued", "failed"):
        raise GradeError(422, "这份已经开始批过了 —— 改判定依据请点「重新批改」（会重跑一遍）")

    if payload.answer_mode is not None:
        grade.answer_mode = payload.answer_mode

    # 只重写「粘贴的那份」，引用的 lesson_files 不动（那是另一批行，由 create 负责）
    if payload.answer_text is not None:
        _drop_pasted(db, grade, "answer")
        if payload.answer_text.strip():
            _add_answer_text_file(db, grade, payload.answer_text.strip(), "answer", "答案（粘贴）")
    if payload.rubric_text is not None:
        _drop_pasted(db, grade, "rubric")
        if payload.rubric_text.strip():
            _add_answer_text_file(db, grade, payload.rubric_text.strip(), "rubric", "评分细则（粘贴）")

    db.flush()
    # 改完重新校验：切到 key/rubric 却没依据，等于拿「AI 自己解」冒充「按老师答案判」——
    # 那是拿错标准批学生，必须在开批前拦住。
    key_text, rub_text = _answer_texts(db, grade)
    if grade.answer_mode == "key" and not key_text:
        raise GradeError(422, "选了「老师给答案」，但答案还是空的 —— 粘一段文字再开始批改")
    if grade.answer_mode == "rubric" and not rub_text:
        raise GradeError(422, "选了「评分细则」，但细则还是空的 —— 每步多少分、什么情况扣分，得写下来")

    db.commit()
    return grade


def _drop_pasted(db: Session, grade: HomeworkGrade, role: str) -> None:
    """删掉某个 role 下**粘贴出来的**那几行（`lesson_file_id` 为空的就是），连磁盘上的 txt 一起。

    只删粘贴的：引用的 `lesson_files` 是老师从「交作业」传的答案文件，
    那是另一份归档原件，删了会连带把人家上传的文件抹掉。
    """
    rows = db.scalars(
        select(HomeworkGradeFile).where(
            HomeworkGradeFile.grade_id == grade.id,
            HomeworkGradeFile.role == role,
            HomeworkGradeFile.lesson_file_id.is_(None),
        )
    ).all()
    for f in rows:
        if (f.stored or "").strip():
            p = storage.safe_join(f.stored)
            if p is not None and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass          # 删不掉文件不影响逻辑：行删了就再也不会被读到
        db.delete(f)


# ---------------------------------------------------------------- 页 ↔ 文件
def _page_slots(db: Session, grade: HomeworkGrade) -> list[dict]:
    """把「整份作业的第 N 页」摊平成一张表：第 N 页在哪个文件的第几页。

    为什么要摊平：一次交两张照片 = 两个文件、两页，但前端只认 1..total 的页号。
    页号按文件 id 的顺序连排（与详情里 files 的顺序一致），这样老师看到的
    「第 2 页」和列表里的第二个文件是对得上的。
    """
    rows = db.scalars(
        select(HomeworkGradeFile)
        .where(HomeworkGradeFile.grade_id == grade.id, HomeworkGradeFile.role == "submission")
        .order_by(HomeworkGradeFile.id)
    ).all()
    out: list[dict] = []
    pno = 0
    for f in rows:
        path: Path | None = None
        if f.lesson_file_id:
            lf = db.get(LessonFile, f.lesson_file_id)
            if lf is not None:
                path = storage.safe_join(lf.stored)      # lesson_files.stored 是相对 UPLOAD_DIR
        elif (f.stored or "").strip():
            path = storage.safe_join(f.stored)
        n = max(1, int(f.pages or 1))
        cache = _page_cache_dir(grade.id, f.id)
        for i in range(n):
            pno += 1
            out.append({"pno": pno, "local": i + 1, "path": path,
                        "file_id": f.id, "cache_dir": cache})
    return out


# ---------------------------------------------------------------- 上下文
def _answer_texts(db: Session, grade: HomeworkGrade,
                  respect_mode: bool = True) -> tuple[str, str]:
    """凑出"老师给的答案"与"评分细则"两段文字，喂给 AI 层。

    两处来源拼在一起：粘贴的文字（建任务时落成的 txt）与引用的 lesson_files
    （上传时就抽好文字了）。**ai 模式下给空串**：老师没给依据，就不该往 ctx 里塞东西。

    respect_mode=False 只给界面的回显用：老师切到「AI 自己解」之后，
    之前填过的答案还得在文本框里，不然切一下就没了。
    """
    if respect_mode and grade.answer_mode == "ai":
        return "", ""
    parts: dict[str, list[str]] = {"answer": [], "rubric": []}
    rows = db.scalars(
        select(HomeworkGradeFile)
        .where(HomeworkGradeFile.grade_id == grade.id,
               HomeworkGradeFile.role.in_(("answer", "rubric")))
        .order_by(HomeworkGradeFile.id)
    ).all()
    for f in rows:
        text = ""
        if (f.stored or "").strip():
            p = storage.safe_join(f.stored)
            if p is not None and p.exists():
                try:
                    text = p.read_bytes().decode("utf-8-sig")
                except (OSError, UnicodeDecodeError):
                    text = ""
        elif f.lesson_file_id:
            lf = db.get(LessonFile, f.lesson_file_id)
            text = (lf.text if lf is not None else "") or ""
        if text.strip():
            parts[f.role].append(text.strip())
    return "\n\n".join(parts["answer"]), "\n\n".join(parts["rubric"])


def _ctx_base(db: Session, grade: HomeworkGrade) -> dict:
    stu = db.get(Student, grade.student_id)
    key_text, rubric_text = _answer_texts(db, grade)
    return {
        "student": (stu.name if stu is not None else "") or "",
        "answer_mode": grade.answer_mode,
        "answer_text": key_text,
        "rubric_text": rubric_text,
    }


# ---------------------------------------------------------------- 调 AI（唯一接口）
def _ai_ready() -> tuple[bool, str]:
    """`services/grade_ai.py` 在并行开发，可能还不存在。

    所以这里**延迟 import + 认错**：缺文件时整份落 failed 并把原因说清楚，
    而不是让每个请求 500（那会让整个批改路由看起来"坏了"，其实只是另一个文件没到）。
    """
    try:
        from . import grade_ai  # noqa: F401
    except ImportError as e:
        return False, f"AI 批改模块还没就位（services/grade_ai.py 缺失：{e}）"
    return True, ""


def _recognize_page(png: str, ctx: dict) -> tuple[list[dict], str]:
    """调一次识别。返回 (items, err) —— 两者互斥，err 非空就是"这页没批成"。"""
    ready, why = _ai_ready()
    if not ready:
        return [], why
    from .grade_ai import recognize_page
    try:
        items, err = recognize_page(png, ctx)
    except Exception as e:                           # noqa: BLE001
        # 契约说它永不抛。这里仍然接住：并行开发期真抛了，最多这一页算失败，
        # 不该让整个线程带着 half-written 状态死掉。
        return [], f"AI 层异常（{type(e).__name__}: {e}）"
    if err:
        return [], str(err)
    if not isinstance(items, list):
        return [], "AI 层返回的不是逐题列表"
    return items, ""


# ---------------------------------------------------------------- 逐页写入
def _item_row(r: dict, gid: int, pno: int, seq: int) -> HomeworkGradeItem:
    raw = r.get("raw")
    if isinstance(raw, str):
        raw_txt = raw
    else:
        raw_txt = json.dumps(raw if raw is not None else r, ensure_ascii=False, default=str)
    flags = [str(x) for x in (r.get("flags") or []) if str(x).strip()]
    return HomeworkGradeItem(
        id=uuid.uuid4().hex[:12],
        owner_id=config.OWNER_ID,
        grade_id=gid,
        seq=seq,
        page_no=pno,
        q_no=str(r.get("q_no") or "").strip(),
        qtype=_qtype(r.get("qtype")),
        stem=str(r.get("stem") or ""),
        student_answer=str(r.get("student_answer") or ""),
        correct_answer=str(r.get("correct_answer") or ""),
        verdict=_verdict(r.get("verdict")),
        score=_num(r.get("score")),               # 没判就是 NULL，不给 0
        full_score=_num(r.get("full_score")),
        basis=str(r.get("basis") or ""),
        confidence=_confidence(r.get("confidence")),
        kp_name=str(r.get("kp_name") or "").strip(),
        tags=json.dumps(knowledge.clean_tags(r.get("tags")), ensure_ascii=False),
        error_reason=str(r.get("error_reason") or ""),
        flags=json.dumps(flags, ensure_ascii=False),
        raw=raw_txt,
        transcription_version=1,
        status="pending",                          # AI 初稿，等老师看
        created_at=_now(),
    )


def _write_page_items(gid: int, pno: int, items: list[dict], curriculum_id: int | None) -> None:
    """把一页的结果落库。**只在批改主线程里调**（SQLite 单写者，写必须串行）。"""
    db = SessionLocal()
    try:
        # 重跑时这一页的旧题先清掉（挂在这些题上的标注随外键 CASCADE 一起走）。
        # 不清会在重批后把同一道题留成两行，seq 重排后就并排显示两遍。
        db.execute(delete(HomeworkGradeItem).where(
            HomeworkGradeItem.grade_id == gid, HomeworkGradeItem.page_no == pno))
        for idx, r in enumerate(items):
            it = _item_row(r, gid, pno, pno * 1000 + idx)
            # 考点尽力挂到知识树上 —— 挂不上不是错误（多数体系根本没有知识树），
            # 只是掌握度那边会少一根线。与 batch_page 同一套处理。
            if it.kp_name:
                names, node_id, _cur = knowledge.resolve_kp(
                    db, [it.kp_name], curriculum_id, fuzzy=True)
                if names:
                    it.kp_name = names[0]
                it.node_id = node_id
            db.add(it)
        db.commit()
    finally:
        db.close()


def _renumber(gid: int) -> None:
    """seq 收口成 1..N（按「页号 → 页内顺序」）。页是并行回来的，完成顺序不等于卷面顺序。"""
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(HomeworkGradeItem)
            .where(HomeworkGradeItem.grade_id == gid)
            .order_by(HomeworkGradeItem.page_no, HomeworkGradeItem.seq, HomeworkGradeItem.id)
        ).all()
        for i, it in enumerate(rows, start=1):
            it.seq = i
        db.commit()
    finally:
        db.close()


def _bump(gid: int, done: int, failed: int) -> None:
    db = SessionLocal()
    try:
        g = db.get(HomeworkGrade, gid)
        if g is not None:
            g.done, g.failed = done, failed
            db.commit()
    finally:
        db.close()


def _finalize(gid: int, done: int, first_err: str = "", abort: str = "") -> None:
    """收尾。状态机与 `batch_page._finalize` 同一套（前端不用分叉）：

    全成 → reviewing（**不是 done**：等人看）；全失败 → failed；一半 → partial。
    `failed` 在这里按 `total - done` 补算：线程中途挂了的话，没轮到的页也要算没批成，
    否则前端会看到一个永远停在「3/5」的进度条。
    """
    db = SessionLocal()
    try:
        g = db.get(HomeworkGrade, gid)
        if g is None:
            return
        failed = max(0, int(g.total or 0) - done)
        g.done, g.failed = done, failed
        g.finished_at = _now()
        if done == 0:
            g.status = "failed"
            g.error = abort or f"整份都没批成：{first_err or 'AI 没有返回结果'}"
        elif failed == 0:
            g.status = "reviewing"
            g.error = ""
        else:
            g.status = "partial"
            g.error = (abort or f"{failed} 页没批成") + (f"（{first_err}）" if first_err else "")
        g.error = (g.error or "")[:500]
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- 线程
_run_lock = threading.Lock()
_running: set[int] = set()


def launch(db: Session, grade: HomeworkGrade) -> bool:
    """认领 + 起后台线程。**已在跑就什么都不做**（前端连点两下不该跑两遍）。

    为什么用进程内的集合而不是只看 `status`：进程崩过之后库里会留下一个
    永远 `running` 的僵尸（没人写它了）。以「本进程里真的有这么个线程」为准，
    僵尸就能被重新认领 —— 否则这份作业再也批不了。
    """
    with _run_lock:
        if grade.id in _running:
            return False
        _running.add(grade.id)
    # 重跑＝重来：进度清零，**并且把上一次的"确认"作废**（reviewed_at 清空、回到 queued）。
    # 不这么做的话，重批中的结果会挂着一个旧的"已确认"，统计口径会读到半截数据。
    grade.status = "queued"
    grade.done = grade.failed = 0
    grade.error = ""
    grade.finished_at = None
    grade.reviewed_at = None
    db.commit()
    threading.Thread(target=run_grade, args=(grade.id,), daemon=True).start()
    return True


def run_grade(gid: int) -> None:
    """后台线程入口：**逐页并行识别，本线程串行写库**。

    线程里不碰请求会话（SQLAlchemy 的 Session 不能跨线程），每个写函数自己
    `SessionLocal()` 新建再 close —— 与 `batch_page` 同一套。
    """
    try:
        _run_grade(gid)
    finally:
        with _run_lock:
            _running.discard(gid)


def _run_grade(gid: int) -> None:
    db = SessionLocal()
    try:
        grade = db.get(HomeworkGrade, gid)
        if grade is None:
            return
        ready, why = _ai_ready()
        if not ready:
            # 连模块都没有：**不清旧结果**。清了又没有新结果顶上，
            # 等于把老师上次的判读连痕迹一起抹掉。
            grade.status = "failed"
            grade.error = why
            grade.finished_at = _now()
            db.commit()
            return
        grade.status = "running"
        grade.error = ""
        db.commit()

        slots = _page_slots(db, grade)
        grade.total = len(slots)
        if not slots:
            grade.status = "failed"
            grade.error = "这份作业没有可看的提交件（原件是不是被删了？）"
            grade.finished_at = _now()
            db.commit()
            return
        # 重跑：旧题全清（连同挂在上面的标注）。页级标注（item_id 为空）保留 ——
        # 那些是老师画在纸上的话，与这一遍的 AI 结果无关。
        db.execute(delete(HomeworkGradeItem).where(HomeworkGradeItem.grade_id == gid))
        db.commit()
        base_ctx = _ctx_base(db, grade)
        curriculum_id = grade.curriculum_id
    finally:
        db.close()

    done = failed = 0
    first_err = ""
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(_run_one_page, slot, base_ctx): slot["pno"] for slot in slots}
            for fut in as_completed(futures):
                pno = futures[fut]
                try:
                    items, err = fut.result()
                except Exception as e:               # noqa: BLE001
                    items, err = [], f"{type(e).__name__}: {e}"
                if err:
                    failed += 1
                    first_err = first_err or f"第 {pno} 页：{err}"
                else:
                    try:
                        _write_page_items(gid, pno, items, curriculum_id)
                        # ⚠️ 一页 0 道题也算 done：空白页/纯页眉确实被读完了。
                        #    done 是页数，不是题目数 —— 别在这里改成 len(items)。
                        done += 1
                    except Exception as e:           # noqa: BLE001
                        failed += 1
                        first_err = first_err or f"第 {pno} 页写库失败：{e}"
                _bump(gid, done, failed)
    except Exception as e:                           # noqa: BLE001
        _finalize(gid, done, first_err, abort=f"批改线程异常终止：{e}")
        return
    _renumber(gid)
    _finalize(gid, done, first_err)


def _run_one_page(slot: dict, ctx_base: dict) -> tuple[list[dict], str]:
    """在一个工作线程里处理一页：渲染页图 + 调 AI。**不碰数据库**，纯 I/O。"""
    path = slot["path"]
    if path is None or not Path(path).exists():
        return [], "原件不在了（可能已被删除）"
    try:
        # 页图走适配层（AGPL 的 pymupdf 只准在 adapters/pdf.py 里 import）。
        # 缓存目录按 gid + 文件分，见 grade_dir 的注释。
        png = pdf_adapter.render_page(str(path), int(slot["local"]), str(slot["cache_dir"]))
    except Exception as e:                           # noqa: BLE001
        return [], f"页图渲染不出来（{type(e).__name__}: {e}）"
    return _recognize_page(png, {**ctx_base, "page_no": slot["pno"]})


# ---------------------------------------------------------------- 老师改题
def _invalidate_transcription(it: HomeworkGradeItem, new_text: str) -> None:
    """老师把识别错的字改对了 —— **这一题的旧判定必须整块作废**。

    为什么必须清 basis / error_reason / score：它们全都建立在**旧原文**上。
    留着会出现"依据引用了学生根本没写过的句子"，导出给家长时这是最难看的一种错。
    `transcription_version` +1 是给下游看的信号：这条题的转写已经和模型当初看到的不一样了。
    """
    it.student_answer = new_text
    it.verdict = "ungradable"       # 一等状态，不是错误分支：没依据就不下结论
    it.score = None                 # 旧分数按旧答案算的，留着就是错分的定论
    it.basis = ""
    it.error_reason = ""
    it.confidence = "low"
    it.status = "pending"           # 要重新判，回到待复核
    it.transcription_version = int(it.transcription_version or 1) + 1
    flags = _json_list(it.flags)
    if "retranscribed" not in flags:
        flags.append("retranscribed")
    it.flags = json.dumps(flags, ensure_ascii=False)


def _resolve_node(db: Session, it: HomeworkGradeItem) -> None:
    """考点改名后 node_id 得跟着走，否则掌握度会挂到旧节点上（改了名却统计进旧知识点）。"""
    grade = db.get(HomeworkGrade, it.grade_id)
    names, node_id, _cur = knowledge.resolve_kp(
        db, [it.kp_name], grade.curriculum_id if grade is not None else None, fuzzy=True)
    if names:
        it.kp_name = names[0]
    it.node_id = node_id


def patch_item(db: Session, iid: str, payload: GradeItemPatch) -> HomeworkGradeItem:
    """老师改一道题。**没传的字段 = 不改**（同 ExamPatch / PlanPatch 的语义）。"""
    it = db.get(HomeworkGradeItem, iid)
    if it is None:
        raise GradeError(404, "这道题不存在")
    fields = payload.model_dump(exclude_unset=True)

    if "student_answer" in fields:
        new_text = fields.pop("student_answer") or ""
        # 相同文本不触发作废：老师只是点了保存、或前端把整条回传了一遍，
        # 不该因此把已经看过的判定冲掉。
        if new_text != (it.student_answer or ""):
            _invalidate_transcription(it, new_text)

    if "verdict" in fields:
        it.verdict = _verdict(fields["verdict"])
        # 老师亲手判的，置信度就不该再是模型那个 low/high —— 显式置 high
        it.confidence = "high"
    if "score" in fields:
        it.score = fields["score"]          # 显式传 null = 清空分数，这是有意义的操作
    if "qtype" in fields:
        it.qtype = _qtype(fields["qtype"])
    if "kp_name" in fields:
        it.kp_name = (fields["kp_name"] or "").strip()
        _resolve_node(db, it)
    if "error_reason" in fields:
        it.error_reason = fields["error_reason"] or ""
    if "tags" in fields:
        it.tags = json.dumps(knowledge.clean_tags(fields["tags"]), ensure_ascii=False)
    if "status" in fields:
        it.status = fields["status"]

    it.reviewed_at = _now()
    db.commit()
    return it


def rerun_item(db: Session, iid: str) -> HomeworkGradeItem:
    """只重批一道题：重识别它所在的那一页，然后只换掉**这一题**的结果。

    为什么不整份重跑：老师只改了一道题的错字，别的题（可能已逐题确认过）不该跟着重置。
    同一页的其他题会被顺带识别出来，但**结果丢弃** —— 它们没有要求重批。

    ⚠️ 同步执行（会阻塞 8~35 秒）。单题重批是老师盯着看的操作，起线程反而要
       前端额外做轮询；这里直接等，返回时结果已经落库。

    TODO（依赖 grade_ai 的契约）：
      老师的转写修正无法传给模型（`recognize_page` 的 ctx 只有题干级信息，
      没有"这一题的正确答案文本"）。所以改过字的题重批时，判定仍基于模型的 OCR；
      本期只能保证**不覆盖老师的修正**（见下面的 keep_answer）。
    """
    it = db.get(HomeworkGradeItem, iid)
    if it is None:
        raise GradeError(404, "这道题不存在")
    ready, why = _ai_ready()
    if not ready:
        raise GradeError(503, why)
    grade = db.get(HomeworkGrade, it.grade_id)
    if grade is None:
        raise GradeError(404, "批改记录不存在")
    if grade.status in ("queued", "running"):
        raise GradeError(409, "整份还在批改中，等它跑完再单题重批")

    slots = _page_slots(db, grade)
    slot = next((s for s in slots if s["pno"] == it.page_no), None)
    if slot is None:
        raise GradeError(422, f"找不到第 {it.page_no} 页（原件可能已被删除）")

    ctx = {**_ctx_base(db, grade), "page_no": slot["pno"]}
    items, err = _run_one_page(slot, ctx)
    if err:
        raise GradeError(422, f"这页没批成：{err}")

    q_no = (it.q_no or "").strip()
    hit = next((r for r in items if (str(r.get("q_no") or "").strip() == q_no and q_no)), None)
    if hit is None and len(items) == 1:
        # 题号是模型自己写的，同一页重批时可能从 "3" 变成 "3."，不值得为此报错。
        hit = items[0]
    if hit is None:
        raise GradeError(
            422,
            f"这页重新识别后找不到第 {q_no or '?'} 题"
            f"（模型这次给出 {len(items)} 道题）—— 请整份重批",
        )

    # 老师改过转写（version > 1）：**不让 OCR 盖回去**。模型这次可能还是把 8 读成 3。
    keep_answer = int(it.transcription_version or 1) > 1

    it.qtype = _qtype(hit.get("qtype")) or it.qtype
    it.stem = str(hit.get("stem") or it.stem or "")
    it.correct_answer = str(hit.get("correct_answer") or "")
    it.verdict = _verdict(hit.get("verdict"))
    it.score = _num(hit.get("score"))
    it.full_score = _num(hit.get("full_score"))
    it.basis = str(hit.get("basis") or "")
    it.confidence = _confidence(hit.get("confidence"))
    it.error_reason = str(hit.get("error_reason") or "")
    it.raw = hit.get("raw") if isinstance(hit.get("raw"), str) else json.dumps(
        hit.get("raw") if hit.get("raw") is not None else hit, ensure_ascii=False, default=str)
    flags = [str(x) for x in (hit.get("flags") or []) if str(x).strip()]
    if keep_answer and "retranscribed" not in flags:
        flags.append("retranscribed")
    it.flags = json.dumps(flags, ensure_ascii=False)
    if not keep_answer:
        it.student_answer = str(hit.get("student_answer") or "")
    if (hit.get("kp_name") or "").strip():
        it.kp_name = str(hit["kp_name"]).strip()
        _resolve_node(db, it)
    # 新结果要重新过一遍人眼；question_id 保留（挂哪道题是老师的决定，重批不改它）。
    it.status = "pending"
    it.reviewed_at = None
    db.commit()
    return it


def to_bank(db: Session, iid: str, payload: GradeToBankIn) -> HomeworkGradeItem:
    """把一道错题挂到题库**已有的题**上，并打标签（B1：不自动建题）。

    标签两处都要写：`item.tags` 是这次作业的错因标签，`question.tags` 是题目的
    长期标签 —— 只写一处的话，要么下次批改看不到上次错在哪，要么题库里筛不出来。
    """
    it = db.get(HomeworkGradeItem, iid)
    if it is None:
        raise GradeError(404, "这道题不存在")
    q = db.get(Question, payload.question_id)
    if q is None:
        raise GradeError(404, "题库里没有这道题（错题回存只挂已有题，不自动建题）")

    it.question_id = q.id
    add = knowledge.clean_tags(payload.tags)
    if add:
        it.tags = json.dumps(
            knowledge.clean_tags(_json_list(it.tags) + add), ensure_ascii=False)
        q.tags = json.dumps(
            knowledge.clean_tags(_json_list(q.tags) + add), ensure_ascii=False)
    # 老师把它挂进题库 = 对这一条结果明确采纳，所以置 approved。
    # 但整份批改依然没到 done —— 统计口径读的是 grade.status，不会因此提前生效。
    it.status = "approved"
    it.reviewed_at = _now()
    db.commit()
    return it


# ---------------------------------------------------------------- 标注
def _norm_geo(values) -> list[int]:
    """坐标一律 0~1000 的整数（按页归一化，前端 viewBox 直接吃）。

    纯数值收口，不 import 任何图像库：这里只是在"存"坐标，不画也不量。
    越界的夹回 0~1000 —— 前端算出来的框偶尔会带 -0.4 这种，存进去就会画到纸外。
    """
    out: list[int] = []
    for v in values or []:
        try:
            out.append(int(max(0, min(1000, round(float(v))))))
        except (TypeError, ValueError):
            continue
    return out


def add_mark(db: Session, gid: int, payload: GradeMarkIn, created_by: str = "teacher") -> HomeworkGradeMark:
    grade = get_grade(db, gid)
    if payload.item_id:
        it = db.get(HomeworkGradeItem, payload.item_id)
        if it is None or it.grade_id != grade.id:
            raise GradeError(422, "这条标注挂的题不属于这份作业")
    m = HomeworkGradeMark(
        owner_id=config.OWNER_ID,
        grade_id=grade.id,
        item_id=payload.item_id or None,
        page_no=int(payload.page_no or 1),
        kind=payload.kind,
        geo=json.dumps(_norm_geo(payload.geo)),
        points=json.dumps(_norm_geo(payload.points)),
        text=payload.text or "",
        color=payload.color or "",
        # 这个接口的调用方是批改页（老师），AI 层不写标注 —— 它只回文字结果，
        # 坐标由前端"模型给句子 → 老师点/拖"那条线来定。
        created_by=created_by,
        flags="[]",
        created_at=_now(),
    )
    db.add(m)
    db.commit()
    return m


def _mark_or_404(db: Session, mid: int) -> HomeworkGradeMark:
    m = db.get(HomeworkGradeMark, int(mid))
    if m is None:
        raise GradeError(404, "标注不存在")
    return m


def patch_mark(db: Session, mid: int, payload: GradeMarkPatch) -> HomeworkGradeMark:
    m = _mark_or_404(db, mid)
    fields = payload.model_dump(exclude_unset=True)
    if "geo" in fields:
        m.geo = json.dumps(_norm_geo(fields["geo"]))
    if "points" in fields:
        m.points = json.dumps(_norm_geo(fields["points"]))
    if "text" in fields:
        m.text = fields["text"] or ""
    if "color" in fields:
        m.color = fields["color"] or ""
    if "kind" in fields:
        m.kind = fields["kind"]
    # 老师动过的标注就归老师：模型给的框只可信到"哪一带"，拖动之后不能再算 AI 猜的。
    m.created_by = "teacher"
    db.commit()
    return m


def delete_mark(db: Session, mid: int) -> None:
    m = _mark_or_404(db, mid)
    db.delete(m)
    db.commit()


# ---------------------------------------------------------------- 确认
def confirm_grade(db: Session, gid: int, payload: GradeConfirmIn) -> HomeworkGrade:
    """老师确认整份。**这是唯一让结果进入统计口径的开关**（掌握度 / 家长稿）。

    待复核的题一并置 approved：整份都确认了，还留着 pending 会让下游
    "只数 approved"的口径把题漏掉。明确驳回过的（rejected）保持不动。

    TODO：`payload.note`（老师想留一句话）目前没有落点 —— `homework_grades`
    没有备注列，加列要动迁移，不在本次改动范围内。传上来先收下、不存。
    """
    grade = get_grade(db, gid)
    if grade.status in ("queued", "running"):
        raise GradeError(409, "还在批改中，等这一遍跑完再确认")
    if grade.status == "failed":
        raise GradeError(422, "这份作业整份都没批成，没有可确认的结果")

    now = _now()
    pending = db.scalars(
        select(HomeworkGradeItem).where(
            HomeworkGradeItem.grade_id == grade.id,
            HomeworkGradeItem.status == "pending",
        )
    ).all()
    for it in pending:
        it.status = "approved"
        it.reviewed_at = now
    grade.status = "done"
    grade.reviewed_at = now
    # ⚠️ 不回填 score / full_score：一期只判对错不给分，逐题分数多半是 NULL，
    #    算个总分出来就是把"没判"当成"0 分"（见 models 里 score 可空的理由）。
    db.commit()
    return grade


# ---------------------------------------------------------------- 页图
def page_png(db: Session, gid: int, pno: int) -> Path:
    """取第 pno 页的 PNG（带磁盘缓存）。pno 是**整份作业的页号**，不是文件内的页号。"""
    grade = get_grade(db, gid)
    slots = _page_slots(db, grade)
    if not slots:
        raise GradeError(422, "这份作业没有可看的提交件")
    if pno < 1 or pno > len(slots):
        raise GradeError(404, f"没有第 {pno} 页（共 {len(slots)} 页）")
    slot = slots[pno - 1]
    path = slot["path"]
    if path is None or not Path(path).exists():
        raise GradeError(422, "这一页的原件不在了（可能已被删除）")
    try:
        png = pdf_adapter.render_page(str(path), int(slot["local"]), str(slot["cache_dir"]))
    except ValueError as e:
        # 适配层对越界页码抛 ValueError（负索引会被 pymupdf 当成"最后一页"，只能自己拦）
        raise GradeError(422, str(e)) from e
    return Path(png)
