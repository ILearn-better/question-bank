# -*- coding: utf-8 -*-
"""课表 Excel 导入的三个接口：下载模板 / 预览 / 确认入库。

为什么要拆成「预览 + 确认」两步（而不是上传即入库）
--------------------------------------------------
Excel 是老师自己敲的，写错学生名、把 10 月写成 9 月，都是常事。
一次导入几十节课之后再一条条删，比导入前看一眼麻烦得多。
所以：**解析和匹配全部在预览里做完并摊开给老师看**，确认无误才真的写库。

⚠️ 注册顺序：本 router 必须排在 lessons.router **之前**（main.py），
否则 `/api/lessons/import/...` 有被 `/api/lessons/{lid}/...` 吃掉的隐患。
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Lesson, Student
from ..services import schedule_import
from .lessons import _apply_billing

router = APIRouter(prefix="/api/lessons/import", tags=["lessons"])

MAX_UPLOAD = 5 * 1024 * 1024            # 5 MB —— 课表就是几百行，再大肯定是传错了
ALLOWED_STATUS = {"scheduled", "done", "makeup", "leave", "cancelled", "moved"}
ALLOWED_MODE = {"offline", "online"}

TEMPLATE_NAME = "课表导入模板.xlsx"


# ---------------------------------------------------------------- 模板下载
@router.get("/template")
def download_template():
    """下载导入模板（含示例行 + 独立的「填写说明」工作表）。"""
    try:
        body = schedule_import.build_template()
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    # 文件名含中文 → 用 RFC 5987 的 filename*，否则浏览器会存成乱码
    from urllib.parse import quote
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition":
                f"attachment; filename=\"schedule-template.xlsx\"; "
                f"filename*=UTF-8''{quote(TEMPLATE_NAME)}",
            "Cache-Control": "no-cache",
        },
    )


# ---------------------------------------------------------------- 预览
def _match_student(db: Session, name: str, cache: dict) -> Student | None:
    """按姓名找学生。先精确匹配 name，再退一步匹配 nickname。

    刻意**不做**模糊/包含匹配 —— 「张三」和「张三丰」都能匹配上「张三」的话，
    老师根本不会注意到课上错了人。找不到就在预览里标出来让他自己判断。
    """
    key = name.strip()
    if not key:
        return None
    if key in cache:
        return cache[key]
    found = db.scalar(select(Student).where(Student.name == key))
    if found is None:
        found = db.scalar(select(Student).where(Student.nickname == key))
    cache[key] = found
    return found


@router.post("/preview")
async def preview(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """解析 Excel 并做匹配/查重，**不写库**。返回逐行的可导入性与问题清单。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(422, "没收到文件内容")
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, f"文件太大了（{len(raw)/1024/1024:.1f} MB，上限 5 MB）")

    result = schedule_import.parse_workbook(raw)          # 永不抛异常
    if not result.get("ok"):
        return result                                     # 前端按 error 显示，仍然是 200

    cache: dict = {}
    months: set[str] = set()
    unknown: dict[str, int] = {}                          # 学生名 → 出现次数
    seen: dict[tuple, int] = {}                           # (学生, 开始时间) → 首次出现的行号
    n_dup = 0
    n_dup_file = 0
    for row in result["rows"]:
        st = _match_student(db, row["student"], cache)
        row["student_id"] = st.id if st else None
        row["student_exists"] = st is not None
        # 这行最后会用哪个单价入库（表里的单价优先，否则取学生档案的默认单价）。
        # 两边都没有 → 这节课的金额会是空的，课时费汇总里**静默少掉这一节**，
        # 所以要在预览里明确点出来，不能等老师月底对账才发现。
        row["rate_effective"] = row["rate"] if row["rate"] is not None else (
            st.hourly_rate if st else None
        )
        if st is None and row["errors"] == []:
            unknown[row["student"]] = unknown.get(row["student"], 0) + 1

        # ① 表内重复：同一份 Excel 里两行一模一样。
        #    老师复制上一行改内容时最容易漏改日期 —— 这种**必须拦下来**，
        #    否则同一节课会被排两遍，而两条还都「看起来没问题」。
        row["duplicate_kind"] = None
        row["duplicate_in_file"] = None
        if row["start_at"] and row["student"].strip():
            key = (row["student"].strip(), row["start_at"])
            if key in seen:
                row["duplicate_in_file"] = seen[key]
            else:
                seen[key] = row["row_no"]

        # ② 与库里已有的课重复：同一个学生、同一个开始时间
        row["duplicate_of"] = None
        row["duplicate"] = False
        if st is not None and row["start_at"]:
            hit = db.scalar(
                select(Lesson).where(Lesson.student_id == st.id, Lesson.start_at == row["start_at"])
            )
            if hit is not None:
                row["duplicate"] = True
                row["duplicate_of"] = {
                    "id": hit.id,
                    "start_at": hit.start_at,
                    "status": hit.status,
                    "topic": hit.topic or "",
                    "in_file_row": None,
                }

        # 这行能不能直接导。表内重复优先于「与库重复」——它更像是写错了表。
        if row["errors"]:
            row["action"] = "blocked"          # 行本身有硬错，改表再说
        elif row["duplicate_in_file"]:
            row["action"] = "duplicate"
            row["duplicate_kind"] = "file"
            row["duplicate_of"] = {
                "id": None, "start_at": row["start_at"], "status": None, "topic": "",
                "in_file_row": row["duplicate_in_file"],
            }
            n_dup_file += 1
        elif row["duplicate"]:
            row["action"] = "duplicate"        # 行没错，但会重复排课，默认不勾
            row["duplicate_kind"] = "db"
            n_dup += 1
        elif not row["student_exists"]:
            row["action"] = "no_student"       # 名字对不上学生，除非勾「顺手建学生」否则不导
        else:
            row["action"] = "import"

        if row["start_at"]:
            months.add(row["start_at"][:7])

    rows = result["rows"]
    no_rate = [x for x in rows if not x["errors"] and x["rate_effective"] is None]
    result["counts"] = {
        "total": len(rows),
        "problem": sum(1 for x in rows if x["errors"]),
        "duplicate": n_dup + n_dup_file,
        "duplicate_in_file": n_dup_file,
        "no_student": sum(1 for x in rows if x["action"] == "no_student"),
        "unknown_student": sum(1 for x in rows if not x["student_exists"] and not x["errors"]),
        "no_rate": len(no_rate),
        "ready": sum(1 for x in rows if x["action"] == "import"),
    }
    result["unknown_students"] = sorted(unknown, key=lambda k: -unknown[k])
    result["months"] = sorted(months)
    if unknown:
        result["notes"] = list(result.get("notes") or []) + [
            "这些学生系统里没有：" + "、".join(result["unknown_students"]) +
            "。可以先在「学生」里建好再回来导，或导入时勾选「顺手建这几个学生」。"
        ]
    if no_rate:
        result["notes"] = list(result.get("notes") or []) + [
            f"有 {len(no_rate)} 行定不出单价（表里没写，学生档案里也没设默认课时费）—— "
            f"这些课导进去后金额是空的，月底算课时费时不会计入。"
            f"要么在这份表里写上单价，要么先去「学生」里把默认课时费补上。"
        ]
    if n_dup_file:
        result["notes"] = list(result.get("notes") or []) + [
            f"有 {n_dup_file} 行**在表里就写重了**（同一个学生、同一个时间出现了两次），"
            f"默认只留第一次、重复的那行没勾 —— 请回 Excel 确认是不是复制上一行时漏改了日期。"
        ]
    if n_dup:
        result["notes"] = list(result.get("notes") or []) + [
            f"有 {n_dup} 行和已排的课撞了（同一学生、同一时间），默认没勾选 —— 确认要重复排再手动勾上。"
        ]
    return result


# ---------------------------------------------------------------- 确认入库
class ImportRowIn(BaseModel):
    row_no: int | None = None
    date: str | None = None                 # '2026-10-01'
    time: str | None = None                 # '19:00'
    student: str = ""
    student_id: int | None = None
    duration_min: int = 60
    topic: str = ""
    mode: str = "offline"
    location: str = ""
    rate: float | None = None
    status: str = "scheduled"


class ImportCommitIn(BaseModel):
    rows: list[ImportRowIn] = Field(default_factory=list)
    # 学生名匹配不上时是否顺手建学生。默认 False —— 建学生是「新增实体」，
    # 不该因为一次导入而悄悄发生（名字写错就会多出一堆脏学生）。
    create_students: bool = False


@router.post("/commit")
def commit(payload: ImportCommitIn, db: Session = Depends(get_db)):
    """把预览里勾中的行写进库。返回逐行结果。"""
    if not payload.rows:
        raise HTTPException(422, "没有要导入的行")
    if len(payload.rows) > schedule_import.MAX_ROWS:
        raise HTTPException(422, f"一次最多导入 {schedule_import.MAX_ROWS} 行")

    created: list[dict] = []
    skipped: list[dict] = []
    created_students: list[str] = []
    name_to_id: dict[str, int] = {}          # 同一批里重名的学生只建一个

    for row in payload.rows:
        sid = row.student_id
        if sid is None and row.student:
            # 先看看是不是这一批里刚建过的
            if row.student in name_to_id:
                sid = name_to_id[row.student]
            elif payload.create_students:
                s = Student(owner_id=config.OWNER_ID, name=row.student, status="active")
                db.add(s)
                db.flush()
                sid = s.id
                name_to_id[row.student] = sid
                created_students.append(row.student)

        if sid is None:
            skipped.append({"row_no": row.row_no, "student": row.student,
                            "reason": "系统里没有这个学生"})
            continue
        if db.get(Student, sid) is None:
            skipped.append({"row_no": row.row_no, "student": row.student,
                            "reason": f"学生 #{sid} 不存在"})
            continue

        # 日期时间在这里再校验一次：预览的结果可能被改过（前端允许手工修正时间）
        if not row.date or not row.time:
            skipped.append({"row_no": row.row_no, "student": row.student, "reason": "缺日期或时间"})
            continue
        try:
            datetime.strptime(f"{row.date} {row.time}", "%Y-%m-%d %H:%M")
        except ValueError:
            skipped.append({"row_no": row.row_no, "student": row.student,
                            "reason": f"日期时间格式不对（{row.date} {row.time}）"})
            continue

        status = row.status if row.status in ALLOWED_STATUS else "scheduled"
        mode = row.mode if row.mode in ALLOWED_MODE else "offline"
        duration = row.duration_min if 0 < row.duration_min <= 24 * 60 else 60

        student = db.get(Student, sid)
        ls = Lesson(
            owner_id=config.OWNER_ID,
            student_id=sid,
            start_at=f"{row.date}T{row.time}",
            duration_min=duration,
            status=status,
            mode=mode,
            location=row.location or None,
            rate=row.rate if row.rate is not None else student.hourly_rate,  # 单价快照
            billable=1,
            topic=row.topic or None,
            node_ids=json.dumps([]),
        )
        db.add(ls)
        db.flush()
        _apply_billing(db, ls)               # 与手动排课走同一套计费规则，避免两处算法漂移
        created.append({"row_no": row.row_no, "id": ls.id, "start_at": ls.start_at,
                        "student": student.name, "amount": ls.amount})

    db.commit()
    return {
        "ok": True,
        "created": len(created),
        "skipped": len(skipped),
        "created_students": created_students,
        "rows": created,
        "skipped_rows": skipped,
    }
