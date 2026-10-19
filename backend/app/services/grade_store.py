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
    HomeworkGradeRegion,
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
    GradeRegionsPut,
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

# ---------------------------------------------------------------- 一题一区域
# 裁切时框向外扩多少（单位是 0~1000 的归一化坐标，12 ≈ 纸面 1.2%）。
# 为什么要外扩：老师画框会贴着字画，正好切掉笔画的上缘/下缘（尤其是分数线、
# 根号的上横线）；不同学生的照片还有平移，拷过来的框本来就偏。
# 宁可多带一点页边 —— 多带的空白模型会忽略，切掉的笔画是永久丢失。
REGION_PAD = 12

# 框太小就当没画（0~1000 制下 20 ≈ 纸面 2%）。比这更小的框裁出来只是一团糊字，
# 送模型等于花钱买一个「读不清」。
REGION_MIN_SIDE = 20

# 单页最多几个框。与 MAX_ITEMS_PER_PAGE 同量级：正常一页 10~40 题。
MAX_REGIONS_PER_PAGE = 60

# 一次补发多少条标注（批改页本地草稿队列的上限，与前端 gradeDraft.MAX_MARKS 同值）。
# 设上限不是怕模型，是怕「本机攒了三个月没发出去的东西」一次性灌进来。
MAX_MARKS_PER_BATCH = 200

# 裁出来的小块放大上限。放太大只是把 JPEG 噪声一起放大、还更贵；
# 4 倍足够把 300px 宽的一小块提到 1200px（模型实际吃的是 GRADE_MAX_EDGE）。
REGION_MAX_ZOOM = 4.0

# 跨页题拼接时，PDF 点 → 像素的放大倍数。A4 宽 595pt × 3 ≈ 1785px；
# 两段竖拼后高度约 4000px，再由 grade_ai 统一压到 GRADE_MAX_EDGE，
# 于是每段大约还有 1100px 高 —— 仍明显优于整页判（整页 2200 高要摊给一整页）。
REGION_PDF_ZOOM = 3.0

# 跨页拼接的两段之间留的白缝（像素）。不留缝会让人以为「上一段最后一行
# 和下一段第一行是同一行」，模型也容易把两段的字连成一句。
REGION_GAP = 8

# 可以直接当"一页"渲染的图片格式（与 routers/documents.py 的 PAGE_IMAGE_EXTS 同制）。
# 不进那个常量是分层问题：services 不该 import routers。
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _is_image_name(name: str) -> bool:
    return Path(str(name or "")).suffix.lower() in _IMAGE_EXTS


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
        # 图上读到的东西。**必须给前端**：没有它，老师无法核对「模型说有直角符号」
        # 到底是不是真有，而这条已经从「一律不采信」改成「可作判定依据」了。
        "figure_note": it.figure_note or "",
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
    # 老师画的「一题一区域」框。有框的页按框逐题批，没框的页保持整页一次判。
    out["regions"] = list_regions(db, grade.id)
    # pages 是 total 的别名：前端按 1..pages 逐张去取 /page/{pno}。
    # 不另立第二套"总页数"概念 —— 两个字段各存一份，迟早有一个忘更新。
    out["pages"] = grade.total
    # 判定依据的原文，给批改页回显用（老师切到「AI 自己解」时也要能看见自己填过的答案，
    # 所以这里**不受 answer_mode 影响** —— respect_mode=False）。
    out["answer_text"], out["rubric_text"] = _answer_texts(db, grade, respect_mode=False)
    # 答案/细则**引用的文件**与**真正的图片**。前端要能回显「这次用的是哪张照片」——
    # 只给文字的话，老师传了照片却看到答案框空着，会以为没生效。
    out["answer_file_ids"] = [
        int(f.lesson_file_id) for f in files
        if f.lesson_file_id and f.role in ("answer", "rubric")
    ]
    out["answer_images"] = _answer_images(db, grade)
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
    # 答案文件要么**能读出文字**，要么是**图片**（走视觉通道）。
    # 都不是就当场拦住：一份既没文字、又没法当图看的文件等于没给答案，
    # 而模型会退回"自己解题"却仍然显示「按老师答案判」—— 那是拿错标准批学生。
    # （2026-10 起放行图片：`_answer_images` 会把图真的送进模型，
    #   不再是「说按老师答案判、其实没看见答案」。）
    for lf in ans_files:
        if (lf.text or "").strip() or _is_image_name(lf.name):
            continue
        raise GradeError(
            422,
            f"答案文件「{lf.name}」里没有可读的文字"
            f"（{lf.reason or '这种格式不抽文字'}）—— 请粘贴文字，或直接传一张答案照片",
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

    # 换/补**引用文件**（答案照片、细则照片、答案 PDF）。整份替换，粘贴的 txt 不动 ——
    # 它们是老师刚敲的字，跟这批文件是两回事。
    if payload.answer_file_ids is not None:
        db.execute(delete(HomeworkGradeFile).where(
            HomeworkGradeFile.grade_id == grade.id,
            HomeworkGradeFile.role.in_(("answer", "rubric")),
            HomeworkGradeFile.lesson_file_id.is_not(None),
        ))
        ans_role = "rubric" if (payload.answer_mode or grade.answer_mode) == "rubric" else "answer"
        for fid in dict.fromkeys(int(x) for x in payload.answer_file_ids):
            lf = _lesson_file(db, fid, grade.lesson_id, "答案文件")
            if not ((lf.text or "").strip() or _is_image_name(lf.name)):
                raise GradeError(
                    422,
                    f"答案文件「{lf.name}」里没有可读的文字（{lf.reason or '这种格式不抽文字'}）"
                    "—— 请粘贴文字，或直接传一张答案照片",
                )
            db.add(HomeworkGradeFile(
                owner_id=config.OWNER_ID, grade_id=grade.id, lesson_file_id=lf.id,
                name=lf.name, stored="", role=ans_role, kind=lf.kind,
                size_bytes=lf.size_bytes or 0, pages=lf.pages or 0,
            ))

    db.flush()
    # 改完重新校验：切到 key/rubric 却没依据，等于拿「AI 自己解」冒充「按老师答案判」——
    # 那是拿错标准批学生，必须在开批前拦住。**图也算依据**（会真的送进模型）。
    key_text, rub_text = _answer_texts(db, grade)
    imgs = _answer_images(db, grade)
    if grade.answer_mode == "key" and not (key_text or imgs["answer"]):
        raise GradeError(422, "选了「老师给答案」，但答案还是空的 —— 粘一段文字，或传一张答案照片")
    if grade.answer_mode == "rubric" and not (rub_text or imgs["rubric"]):
        raise GradeError(422, "选了「评分细则」，但细则还是空的 —— 写下来，或传一张细则照片")

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


# ---------------------------------------------------------------- 一题一区域
def region_dict(r: HomeworkGradeRegion) -> dict:
    q = (r.q_no or "").strip()
    return {
        "id": r.id,
        "page_no": int(r.page_no or 1),
        "seq": int(r.seq or 0),
        "seg": int(r.seg or 0),
        "q_no": q,
        # 给老师看、也写进提示词帮模型定位。**不参与题号绑定**（绑定看 q_no）。
        # 同一个 seq 的几段 label 相同 —— 它们本来就是同一道题。
        "label": q or str(int(r.seq or 0) + 1),
        "box": _json_list(r.box),
    }


def _regions_of(db: Session, gid: int) -> list[HomeworkGradeRegion]:
    return list(db.scalars(
        select(HomeworkGradeRegion)
        .where(HomeworkGradeRegion.grade_id == int(gid))
        .order_by(HomeworkGradeRegion.seq, HomeworkGradeRegion.seg,
                  HomeworkGradeRegion.page_no, HomeworkGradeRegion.id)
    ).all())


def list_regions(db: Session, gid: int) -> list[dict]:
    return [region_dict(r) for r in _regions_of(db, gid)]


def _seg_key(seg: dict) -> tuple:
    """拼接顺序的排序键：先页、再上边、最后段号。

    为什么不信前端给的 `seg`：段序是**几何事实**（谁在上面谁在前面），
    让界面去维护它，迟早出现「两段 seg 都是 0」或「seg=2 却画在 seg=1 上面」。
    后端按坐标重排，前端只管「哪几段是同一题」。
    """
    box = seg.get("box") or [0, 0, 0, 0]
    try:
        top = float(box[1])
    except (TypeError, ValueError, IndexError):
        top = 0.0
    return (int(seg.get("page_no") or 1), top, int(seg.get("seg") or 0))


def blocks_of(db: Session, gid: int) -> list[dict]:
    """把「段」聚合成「题」：**一道题 = `seq` 相同的 1..N 段**（可跨页）。

    这是与批量录题 `blocks` 对齐的地方：那边也是「一段或多段合成一道题」，
    只不过那边要的是录进题库的题目，这边要的是一条判定 ——
    管道相同，喂进去的内容和要回来的形状不同。

    返回按 `seq` 排序的块列表，每块：
        seq / q_no / label / segments[{page_no, box, seg}] / pages / cross_page
    """
    groups: dict[int, list] = {}
    for r in _regions_of(db, gid):
        groups.setdefault(int(r.seq or 0), []).append(r)

    out: list[dict] = []
    for seq in sorted(groups):
        segs = [region_dict(r) for r in groups[seq]]
        segs.sort(key=_seg_key)
        q_no = next((s["q_no"] for s in segs if s["q_no"]), "")
        pages = [s["page_no"] for s in segs]
        out.append({
            "seq": seq,
            "q_no": q_no,
            "label": q_no or str(seq + 1),
            # seg 重排成 0..n-1：前端给的值只当提示，真正顺序由几何决定。
            "segments": [{**s, "seg": i} for i, s in enumerate(segs)],
            "pages": pages,
            "cross_page": len(set(pages)) > 1,
        })
    return out


def blocks_by_page(db: Session, gid: int) -> dict[int, list[dict]]:
    """{起始页: [块, …]}。

    **块归它的第一段所在页** —— 跨页题只在起始页那一个线程里处理，
    否则两页线程都会去批同一道题，结果互相覆盖。
    """
    out: dict[int, list[dict]] = {}
    for b in blocks_of(db, gid):
        first = b["segments"][0]["page_no"] if b["segments"] else 1
        out.setdefault(int(first), []).append(b)
    return out


def regions_by_page(db: Session, gid: int) -> dict[int, list[dict]]:
    """{页号: [段, …]}。**保留扁平口径**：给「只想知道哪页有段」的调用方用。"""
    out: dict[int, list[dict]] = {}
    for r in _regions_of(db, gid):
        out.setdefault(int(r.page_no or 1), []).append(region_dict(r))
    return out


def _region_box(values) -> list[int] | None:
    """校验并规整成一个框。**位置反了自动纠正，太小就判无效**。

    太小不是「凑合能用」而是真的不能用：0~1000 制下 20 ≈ 纸面 2%，
    比这更小的框裁出来只是一团糊字，送模型等于花钱买一句「读不清」。
    """
    try:
        v = [int(round(float(x))) for x in (values or [])]
    except (TypeError, ValueError):
        return None
    if len(v) != 4:
        return None
    x0, x1 = sorted((max(0, min(1000, v[0])), max(0, min(1000, v[2]))))
    y0, y1 = sorted((max(0, min(1000, v[1])), max(0, min(1000, v[3]))))
    if (x1 - x0) < REGION_MIN_SIDE or (y1 - y0) < REGION_MIN_SIDE:
        return None
    return [x0, y0, x1, y1]


def put_regions(db: Session, gid: int, payload: GradeRegionsPut) -> list[dict]:
    """**整份替换**老师画的框（段）。

    为什么不是逐条增删、也不是按页替换：跨页题的两段天然落在不同页上，
    按页提交的话，「一题两段」这个组合没有任何一个时刻能被原子地写进去。
    整份替换的结果永远等于老师屏幕上看到的样子，不会出现「界面 5 段、库里 7 条」。

    段的顺序（`seg`）由**后端按几何重排**：先页、再上边。前端只负责说
    「这几段是同一题」（同一个 `seq`），不用维护拼接顺序。
    """
    grade = get_grade(db, gid)
    if grade.status == "running":
        raise GradeError(409, "正在批改，等这一遍跑完再改框 —— 否则这一批用的框是哪一版说不清")

    rows = list(payload.regions or [])
    pages = max(1, int(grade.total or 1))
    limit = MAX_REGIONS_PER_PAGE * pages
    if len(rows) > limit:
        raise GradeError(422, f"这份作业最多画 {limit} 个框（每页 {MAX_REGIONS_PER_PAGE} 个）")

    # ⚠️ 分组意愿必须由调用方**显式**给（`grouped`），不靠猜：
    #    「所有 seq 都是 0」既可能是「一题两段」，也可能是「老调用方的 8 个独立框」，
    #    猜错的方向是把 8 道题并成一道，比多传一个布尔字段糟得多。
    seqs = [int(r.seq or 0) for r in rows]
    if not payload.grouped:
        seqs = list(range(len(rows)))

    keep: list[dict] = []
    for i, reg in enumerate(rows):
        page_no = int(reg.page_no or 1)
        if page_no < 1 or page_no > pages:
            raise GradeError(422, f"第 {i + 1} 个框画在第 {page_no} 页上，但这份作业只有 {pages} 页")
        box = _region_box(reg.box)
        if box is None:
            raise GradeError(
                422,
                f"第 {i + 1} 个框太小或位置不对 —— 要能框住一整道题"
                f"（宽高都不能小于纸面的 {REGION_MIN_SIDE / 10:.0f}%）",
            )
        keep.append({
            "page_no": page_no,
            "seq": seqs[i],
            "q_no": str(reg.q_no or "").strip()[:16],
            "box": box,
        })

    # 同一题内的拼接顺序：几何位置说了算，前端给的 seg 只当没有返回值时的兜底
    by_seq: dict[int, list[dict]] = {}
    for row in keep:
        by_seq.setdefault(row["seq"], []).append(row)
    for segs in by_seq.values():
        segs.sort(key=_seg_key)
        for i, row in enumerate(segs):
            row["seg"] = i

    db.execute(delete(HomeworkGradeRegion).where(HomeworkGradeRegion.grade_id == grade.id))
    for row in sorted(keep, key=lambda x: (x["seq"], x["seg"])):
        db.add(HomeworkGradeRegion(
            owner_id=config.OWNER_ID,
            grade_id=grade.id,
            page_no=row["page_no"],
            seq=row["seq"],
            seg=row["seg"],
            q_no=row["q_no"],
            box=json.dumps(row["box"]),
            created_at=_now(),
        ))
    db.commit()
    return list_regions(db, grade.id)


def copy_regions(db: Session, gid: int, from_gid: int) -> list[dict]:
    """把**同一节课另一份**的框照抄过来（一套卷只画一次）。

    ⚠️ 拷的是坐标本身，不是「模板」。不同学生拍照会有平移/旋转，框会偏 ——
    所以界面必须提示「宁大勿小」，且 `_crop_region` 会按 `REGION_PAD` 外扩兜一层。
    跨课时不拷：不同卷子的排版不一样，拷过去只会把框画在错误的题上。
    """
    grade = get_grade(db, gid)
    if grade.status == "running":
        raise GradeError(409, "正在批改，等这一遍跑完再改框")
    if int(from_gid) == grade.id:
        raise GradeError(422, "不能从自己这一份抄框")
    src = get_grade(db, from_gid)
    if int(src.lesson_id) != int(grade.lesson_id):
        raise GradeError(422, "只能沿用同一节课里的框 —— 不同卷子的排版不一样")
    src_rows = _regions_of(db, src.id)
    if not src_rows:
        raise GradeError(422, "那一份还没有画框，没有可沿用的")

    db.execute(delete(HomeworkGradeRegion).where(HomeworkGradeRegion.grade_id == grade.id))
    for r in src_rows:
        db.add(HomeworkGradeRegion(
            owner_id=config.OWNER_ID,
            grade_id=grade.id,
            page_no=int(r.page_no or 1),
            seq=int(r.seq or 0),
            seg=int(r.seg or 0),
            q_no=r.q_no or "",
            box=r.box or "[]",
            created_at=_now(),
        ))
    db.commit()
    return list_regions(db, grade.id)


def _crop_region(png_path: str, box, out_dir: Path, seq: int) -> Path | None:
    """按 0~1000 的框从页图上裁一块，外扩 `REGION_PAD`，放大后落盘。

    返回新文件路径；框不可用 / 图读不出来时返回 **None**（不抛）：
    一个框坏了不该毁掉一整页的批改结果。
    """
    from PIL import Image      # 与 grade_export 同一条依赖，不引新库

    b = _region_box(box)
    if b is None:
        return None
    try:
        # 与 grade_ai 的送模型分辨率对齐。延迟 import：grade_ai 缺席时整条链路
        # 应该走「AI 模块没就位」的既有分支，而不是在这里 ImportError。
        try:
            from .grade_ai import GRADE_MAX_EDGE
        except Exception:                        # noqa: BLE001
            GRADE_MAX_EDGE = 2200
        with Image.open(png_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w < 8 or h < 8:
                return None
            x0 = max(0, b[0] - REGION_PAD)
            y0 = max(0, b[1] - REGION_PAD)
            x1 = min(1000, b[2] + REGION_PAD)
            y1 = min(1000, b[3] + REGION_PAD)
            px0 = int(w * x0 / 1000.0)
            px1 = int(w * x1 / 1000.0)
            py0 = int(h * y0 / 1000.0)
            py1 = int(h * y1 / 1000.0)
            # 至少 2px：极小框在极端比例下会算出 0 宽，crop 会抛
            px1 = max(px0 + 2, min(w, px1))
            py1 = max(py0 + 2, min(h, py1))
            px0 = min(px0, px1 - 2)
            py0 = min(py0, py1 - 2)
            tile = im.crop((px0, py0, px1, py1))
            zoom = min(REGION_MAX_ZOOM,
                       max(1.0, float(GRADE_MAX_EDGE) / float(max(tile.size))))
            if zoom > 1.01:
                tile = tile.resize(
                    (max(1, int(tile.width * zoom)), max(1, int(tile.height * zoom))),
                    Image.LANCZOS,
                )
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"region_{int(seq):03d}.png"
            tile.save(out, "PNG")
            return out
    except Exception:                            # noqa: BLE001
        return None


def _stack_images(tiles: list, gap: int = REGION_GAP):
    """把几张 Pillow 图竖着拼成一张（宽度取最宽，白底填充）。

    只在「跨文件 / 照片」这条兜底路径上用。PDF 的多段拼接走
    `pdf_adapter.crop_regions` —— 那边是渲染级拼接，比贴图更干净。
    """
    from PIL import Image

    if not tiles:
        return None
    w = max(t.width for t in tiles)
    h = sum(t.height for t in tiles) + gap * (len(tiles) - 1)
    canvas = Image.new("RGB", (max(1, w), max(1, h)), (255, 255, 255))
    y = 0
    for t in tiles:
        canvas.paste(t, (0, y))
        y += t.height + gap
    return canvas


def _fit_to_edge(path: Path) -> Path:
    """把已落盘的一张图缩放到长边 ≈ `GRADE_MAX_EDGE`（与 `_crop_region` 同一把尺子）。

    为什么跨页拼图也要过这一道：单段走 `_crop_region` 会把图放大到 2200px，
    而 `crop_regions` 只是按 zoom 渲染、不放大。不过这一道的话，
    **跨页题反而比单页题糊** —— 它内容更多、本来更需要分辨率。
    放大上限与单段一致（`REGION_MAX_ZOOM`）：再大只是把噪声一起放大。
    """
    from PIL import Image

    try:
        from .grade_ai import GRADE_MAX_EDGE
    except Exception:                            # noqa: BLE001
        GRADE_MAX_EDGE = 2200
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            edge = max(w, h)
            if edge <= 0:
                return path
            scale = float(GRADE_MAX_EDGE) / float(edge)
            if scale > 1.0:
                scale = min(scale, REGION_MAX_ZOOM)
            if abs(scale - 1.0) < 0.02:
                return path
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                           Image.LANCZOS)
            im.save(path, "PNG")
        return path
    except Exception:                            # noqa: BLE001
        return path                              # 缩放失败就用原图，别因此判整块失败


def _crop_block(block: dict, slots_by_pno: dict, out_dir: Path) -> Path | None:
    """裁一个**块**（一道题，可含多段）成一张图。返回落盘路径，失败返回 None。

    三条路径，按「能不能少造轮子」排：

      1. **单段** → `_crop_region`（Pillow 从页图裁）。页图本来就是缓存的，
         不需要把 0~1000 换算成 PDF 点。
      2. **多段且都在同一个 PDF 上** → 复用 `pdf_adapter.crop_regions`。
         它本来就是「多个区域（可跨页）竖着拼成一张 PNG」，批量录题的
         跨页题原貌图走的就是它。这里只负责把归一化框换成点坐标。
      3. **多段但跨文件 / 是照片** → 逐段 Pillow 裁再竖拼。
         照片一页一张，理论上不会跨页；这条是兜底，保证不崩而不是主路径。

    ⚠️ 与 `_crop_region` 一样：坏一个块不该毁一整页，所以**全部失败返回 None**，
    由调用方补一条 ungradable 占位题。
    """
    segs = block.get("segments") or []
    if not segs:
        return None
    seq = int(block.get("seq") or 0)

    slots: list[dict] = []
    for s in segs:
        slot = slots_by_pno.get(int(s.get("page_no") or 1))
        if slot is None or slot.get("path") is None:
            return None
        slots.append(slot)

    if len(segs) == 1:
        try:
            png = pdf_adapter.render_page(
                str(slots[0]["path"]), int(slots[0]["local"]), str(slots[0]["cache_dir"]))
        except Exception:                        # noqa: BLE001
            return None
        return _crop_region(png, segs[0].get("box"), out_dir, seq)

    src = str(slots[0]["path"])
    same_src = len({str(s["path"]) for s in slots}) == 1
    if same_src and src.lower().endswith(".pdf"):
        regions = []
        try:
            for seg, slot in zip(segs, slots):
                W, H = pdf_adapter.page_size(src, int(slot["local"]))
                b = _region_box(seg.get("box"))
                if b is None or W <= 0 or H <= 0:
                    return None
                pad_x = W * REGION_PAD / 1000.0
                pad_y = H * REGION_PAD / 1000.0
                regions.append((
                    int(slot["local"]),
                    max(0.0, b[0] / 1000.0 * W - pad_x),
                    max(0.0, b[1] / 1000.0 * H - pad_y),
                    min(W, b[2] / 1000.0 * W + pad_x),
                    min(H, b[3] / 1000.0 * H + pad_y),
                ))
            out_dir.mkdir(parents=True, exist_ok=True)
            name = pdf_adapter.crop_regions(
                src, regions, str(out_dir), zoom=REGION_PDF_ZOOM, gap=REGION_GAP)
            return _fit_to_edge(out_dir / name)
        except Exception:                        # noqa: BLE001
            return None

    from PIL import Image

    tiles = []
    try:
        for seg, slot in zip(segs, slots):
            png = pdf_adapter.render_page(
                str(slot["path"]), int(slot["local"]), str(slot["cache_dir"]))
            b = _region_box(seg.get("box"))
            if b is None:
                return None
            with Image.open(png) as im:
                im = im.convert("RGB")
                w, h = im.size
                x0 = max(0, b[0] - REGION_PAD)
                y0 = max(0, b[1] - REGION_PAD)
                x1 = min(1000, b[2] + REGION_PAD)
                y1 = min(1000, b[3] + REGION_PAD)
                px0, py0 = int(w * x0 / 1000.0), int(h * y0 / 1000.0)
                px1, py1 = int(w * x1 / 1000.0), int(h * y1 / 1000.0)
                px1 = max(px0 + 2, min(w, px1))
                py1 = max(py0 + 2, min(h, py1))
                px0 = min(px0, px1 - 2)
                py0 = min(py0, py1 - 2)
                tiles.append(im.crop((px0, py0, px1, py1)).copy())
        canvas = _stack_images(tiles)
        if canvas is None:
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"region_{seq:03d}.png"
        canvas.save(out, "PNG")
        return _fit_to_edge(out)
    except Exception:                            # noqa: BLE001
        return None


def _region_placeholder(block: dict, reason: str) -> dict:
    """框里什么都没批出来时的**占位题**。

    必须留一条，否则老师只看到「这页比预期少一题」，会以为是自己漏画了框。
    verdict 给 ungradable（不是 wrong）：没读到 ≠ 学生没写。

    参数是**块**（一道题，可能多段）。`region_box` 保留第一段，是为了不破坏
    已经按它断言的老测试；多段的完整列表放在 `region_boxes`。
    """
    boxes = [s.get("box") for s in (block.get("segments") or [])] or [[]]
    return {
        "q_no": str(block.get("q_no") or ""),
        "qtype": None,
        "stem": "",
        "student_answer": "",
        "correct_answer": "",
        "verdict": "ungradable",
        "basis": reason,
        "confidence": "low",
        "kp_name": "",
        "error_reason": "",
        "figure_note": "",
        "flags": ["region_empty"],
        "raw": {
            "question": {
                "region_box": boxes[0],
                "region_boxes": boxes,
                "region_seq": block.get("seq"),
            },
            "meta": {"model": "", "finish_reason": "", "mode": "", "sample": 1},
        },
    }


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


def _answer_images(db: Session, grade: HomeworkGrade) -> dict[str, list[str]]:
    """「老师给的答案 / 评分细则」里的**图片**，按 role 分组返回磁盘路径。

    为什么需要这条路：老师手上常常只有一张**答案照片**（参考答案页拍一张、
    或者自己手写的细则拍一张）。以前只读抽取出来的文字，照片抽不出字 →
    判据为空 → `create_grade` 干脆把这种文件拦在门外（422，让你去粘贴文字）。
    现在把图**真的送进模型**，那条硬拦才不再需要。

    规则：**已经有文字的文件不再送图**。同一份 PDF 答案既有文字层又要送图，
    等于把 token 花两遍，而文字比图更省、更准。
    `ai` 模式返回空 —— 老师没给依据，就不该往 ctx 里塞东西。
    """
    out: dict[str, list[str]] = {"answer": [], "rubric": []}
    if grade.answer_mode == "ai":
        return out
    rows = db.scalars(
        select(HomeworkGradeFile)
        .where(HomeworkGradeFile.grade_id == grade.id,
               HomeworkGradeFile.role.in_(("answer", "rubric")))
        .order_by(HomeworkGradeFile.id)
    ).all()
    for f in rows:
        role = f.role if f.role in out else "answer"
        text = ""
        path: Path | None = None
        if f.lesson_file_id:
            lf = db.get(LessonFile, f.lesson_file_id)
            if lf is not None:
                text = lf.text or ""
                path = storage.safe_join(lf.stored)
        elif (f.stored or "").strip():
            path = storage.safe_join(f.stored)
            if path is not None and path.exists() and path.suffix.lower() == ".txt":
                try:
                    text = path.read_bytes().decode("utf-8-sig")
                except (OSError, UnicodeDecodeError):
                    text = ""
        if (text or "").strip():
            continue
        if path is not None and path.exists() and path.suffix.lower() in _IMAGE_EXTS:
            out[role].append(str(path))
    return out


def _ctx_base(db: Session, grade: HomeworkGrade) -> dict:
    stu = db.get(Student, grade.student_id)
    key_text, rubric_text = _answer_texts(db, grade)
    imgs = _answer_images(db, grade)
    return {
        "student": (stu.name if stu is not None else "") or "",
        "answer_mode": grade.answer_mode,
        "answer_text": key_text,
        "rubric_text": rubric_text,
        # 答案/细则的**图片**（老师拍的照片）。没有文字依据时才非空，
        # 由 grade_ai 作为额外图一起送进模型。
        "answer_images": imgs["answer"],
        "rubric_images": imgs["rubric"],
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


def _recognize_region(png: str, ctx: dict) -> tuple[list[dict], str]:
    """调一次「单题区域」识别。契约与 `_recognize_page` 完全一致（两者互斥）。"""
    ready, why = _ai_ready()
    if not ready:
        return [], why
    from .grade_ai import recognize_region
    try:
        items, err = recognize_region(png, ctx)
    except Exception as e:                           # noqa: BLE001
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
        figure_note=str(r.get("figure_note") or ""),
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
        # 框必须在关 session 之前读成纯 dict —— 线程里不许碰 ORM 对象。
        # 空 dict = 一页都没画框 = 整条流程退回老口径（整页一次判）。
        # 这里读成**块**（一题可多段），跨页题的几段由起始页那个线程一起处理。
        by_page = blocks_by_page(db, gid)
    finally:
        db.close()

    slots_by_pno = {int(s["pno"]): s for s in slots}
    done = failed = 0
    first_err = ""
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {
                pool.submit(_run_one_page, slot, base_ctx,
                            by_page.get(int(slot["pno"])), slots_by_pno):
                    slot["pno"]
                for slot in slots
            }
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


def _run_one_page(slot: dict, ctx_base: dict,
                  blocks: list[dict] | None = None,
                  slots_by_pno: dict | None = None) -> tuple[list[dict], str]:
    """在一个工作线程里处理一页：渲染页图 + 调 AI。**不碰数据库**，纯 I/O。

    两种口径：
      · **有块**（`blocks` 非空）→ 一块一裁一切。有效分辨率翻几倍、
        题号由块绑定、块外内容根本看不见（跨题污染从物理上消失）。
        跨页题的几段在这里**竖拼成一张图**再送 —— 与批量录题的 blocks 同一套语义。
      · **没块** → 保持老行为：整页一次判。老记录、没画框的人零影响。

    ⚠️ 块只归它的**第一段所在页**（`blocks_by_page` 就是这么分的），
    否则跨页题会被两页线程各批一次、结果互相覆盖。
    ⚠️ 单块失败**不毁整页**：失败的那块留一条 ungradable 占位（老师看得见），
    其余块照常出结果。只有**一个都没成**才把整页算失败 —— 那种情况多半是
    接口挂了 / 没配，重试整页才有意义。
    """
    path = slot["path"]
    if path is None or not Path(path).exists():
        return [], "原件不在了（可能已被删除）"
    try:
        # 页图走适配层（AGPL 的 pymupdf 只准在 adapters/pdf.py 里 import）。
        # 缓存目录按 gid + 文件分，见 grade_dir 的注释。
        png = pdf_adapter.render_page(str(path), int(slot["local"]), str(slot["cache_dir"]))
    except Exception as e:                           # noqa: BLE001
        return [], f"页图渲染不出来（{type(e).__name__}: {e}）"

    pno = int(slot["pno"])
    if not blocks:
        return _recognize_page(png, {**ctx_base, "page_no": pno})

    lookup = dict(slots_by_pno or {})
    lookup.setdefault(pno, slot)

    items: list[dict] = []
    good = 0
    first_err = ""
    for block in blocks:
        label = str(block.get("label") or (int(block.get("seq") or 0) + 1))
        tile = _crop_block(block, lookup, Path(slot["cache_dir"]))
        if tile is None:
            first_err = first_err or f"第 {label} 题的框不可用（太小、画在纸外，或原件读不出来）"
            items.append(_region_placeholder(block, "这个框裁不出内容 —— 框可能画到了纸外或太小"))
            continue
        ctx = {**ctx_base, "page_no": pno, "region": True, "region_label": label}
        if block.get("cross_page"):
            # 让提示词知道「这一张图是两页拼起来的」：否则模型看到中间的白缝
            # 会以为题目到此为止，把下半段当另一道题丢掉。
            ctx["span_pages"] = list(block.get("pages") or [])
        try:
            got, err = _recognize_region(str(tile), ctx)
        except Exception as e:                       # noqa: BLE001
            got, err = [], f"{type(e).__name__}: {e}"
        if err:
            first_err = first_err or err
            items.append(_region_placeholder(block, err))
            continue
        if not got:
            # 框画在空白处 / 这一块确实没题。**不是错误**，但要留痕。
            items.append(_region_placeholder(block, "这个框里没读到题目 —— 框画空了？"))
            continue
        it = got[0]
        # 题号绑定：老师填的 q_no 一律优先，模型认的那个只当兜底。
        # 这是「消掉题号错配」的关键一步 —— 学生把第 3 题写成第 8 题也不影响归属。
        if str(block.get("q_no") or "").strip():
            it["q_no"] = str(block["q_no"]).strip()
        elif not str(it.get("q_no") or "").strip():
            it["q_no"] = label
        good += 1
        items.append(it)

    if good == 0 and first_err:
        return [], first_err
    return items, ""


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
    # 有块（画过框）的页必须走「按块批」—— 否则重批会退回整页口径，题号对不上，
    # 还会用一次便宜的整页结果覆盖掉当初按块批出来的、更准的那一版。
    by_page = blocks_by_page(db, grade.id)
    blocks = by_page.get(int(it.page_no))
    if blocks is None:
        # 这道题可能是**跨页块的下半段**：它入库时记在起始页，但页号也可能
        # 因为老数据/手工改过而对不上。扫一遍：哪个块的段跨到了这一页，
        # 就用那个块的起始页 —— 否则会静默退回整页口径，重批反而更差。
        for pno, bls in by_page.items():
            if any(any(int(s["page_no"]) == int(it.page_no) for s in b["segments"])
                   for b in bls):
                blocks = bls
                slot = next((s for s in slots if s["pno"] == pno), slot)
                ctx = {**ctx, "page_no": int(pno)}
                break
    items, err = _run_one_page(slot, ctx, blocks or [],
                               {int(s["pno"]): s for s in slots})
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
    it.figure_note = str(hit.get("figure_note") or "")
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


def _mark_row(grade: HomeworkGrade, payload: GradeMarkIn, created_by: str) -> HomeworkGradeMark:
    """一条标注的**构造**（不 commit）。单条与批量走同一个构造器 ——
    两份各写一遍的话，`_norm_geo` 早晚只有一处被改到，表现是「批量补发的坐标夹不住」。"""
    return HomeworkGradeMark(
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


def add_mark(db: Session, gid: int, payload: GradeMarkIn, created_by: str = "teacher") -> HomeworkGradeMark:
    grade = get_grade(db, gid)
    if payload.item_id:
        it = db.get(HomeworkGradeItem, payload.item_id)
        if it is None or it.grade_id != grade.id:
            raise GradeError(422, "这条标注挂的题不属于这份作业")
    m = _mark_row(grade, payload, created_by)
    db.add(m)
    db.commit()
    return m


def add_marks(db: Session, gid: int, payload, created_by: str = "teacher") -> list[HomeworkGradeMark]:
    """一次补发多条标注（批改页本地草稿队列的出口）。**一次 commit**。

    ⚠️ **不去重**：补发方（前端 `reconcile`）负责只发「服务端还没有的」。
       服务端要是自己猜「这条是不是已经有了」，猜错的方向是把老师的两笔
       并成一笔 —— 而重一点的代价只是多一条，重一笔的代价是**少一笔批注**。
    ⚠️ 顺序即落库顺序：同一位置后画的要盖在上面，所以按传入顺序 add。
    """
    grade = get_grade(db, gid)
    rows = list(getattr(payload, "marks", None) or [])
    if not rows:
        return []
    if len(rows) > MAX_MARKS_PER_BATCH:
        raise GradeError(
            422,
            f"一次最多补发 {MAX_MARKS_PER_BATCH} 条标注，这次有 {len(rows)} 条"
            "（本地草稿攒太多了，先分两次补）",
        )

    # item_id 一次查完：逐条 db.get 在补发几十条时会变成几十次往返
    ids = {str(p.item_id) for p in rows if p.item_id}
    if ids:
        alive = {
            str(it.id) for it in db.scalars(
                select(HomeworkGradeItem).where(
                    HomeworkGradeItem.grade_id == grade.id,
                    HomeworkGradeItem.id.in_(ids),
                )
            ).all()
        }
        missing = ids - alive
        if missing:
            raise GradeError(422, "有标注挂的题不属于这份作业（可能这一遍已经重批过了）")

    made = []
    for p in rows:
        m = _mark_row(grade, p, created_by)
        db.add(m)
        made.append(m)
    db.commit()
    return made


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
