# -*- coding: utf-8 -*-
"""出卷：把库里的题目按顺序拼成一份 A4 试卷并导出。

与录题页的分工：录题页负责「把一道题从原卷里抠出来入库」，
这里负责「把库里的题再拼成一份新卷子」。两者共用 questions 表，但互不依赖。

为什么导出用 GET 而不是 POST：
  前端可以直接 window.open(url) 预览/下载，不必把二进制读进 fetch 再自己造 Blob 存盘
  —— 少一层能出错的地方，也让「在新标签页预览」变成一行代码。
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Question
from ..services import paper_export

router = APIRouter(prefix="/api", tags=["papers"])

# 格式 → (MIME, 扩展名, 是否直接内联预览)
FORMATS: dict[str, tuple[str, str, bool]] = {
    "html": ("text/html; charset=utf-8", "html", True),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx", False),
    "pdf": ("application/pdf", "pdf", False),
}


def _safe_name(title: str) -> str:
    """文件名去掉路径分隔符等非法字符，免得下载时出现怪名字。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (title or "试卷")).strip(" .") or "试卷"
    return name[:60]


@router.get("/papers/export")
def export_paper(
    ids: str = Query(..., description="题目 id，逗号分隔；顺序即卷面顺序"),
    format: str = Query("html", description="html / docx / pdf"),
    title: str = Query("试卷"),
    show_answer: bool = Query(True, description="末尾附参考答案与解析"),
    show_analysis: bool = Query(False, description="答案里是否带解析"),
    show_tags: bool = Query(True, description="题号后是否标注题型·难度"),
    show_meta: bool = Query(True, description="是否留姓名/班级/日期填写栏"),
    db: Session = Depends(get_db),
):
    fmt = (format or "html").lower()
    if fmt not in FORMATS:
        raise HTTPException(422, f"不支持的格式 {format}（可选 html / docx / pdf）")

    qids = [s.strip() for s in (ids or "").split(",") if s.strip()]
    if not qids:
        raise HTTPException(422, "没有选中任何题目")
    if len(qids) > 200:
        raise HTTPException(422, "一次最多导出 200 道题")

    rows = {q.id: q for q in db.scalars(select(Question).where(Question.id.in_(qids))).all()}
    missing = [q for q in qids if q not in rows]
    if missing:
        raise HTTPException(404, f"有 {len(missing)} 道题不存在（可能已被删除），请刷新后重选")

    # 卷面顺序由前端决定，必须按传入顺序编号 —— 按数据库顺序编号会让「上移/下移」白做
    items = [
        {
            "n": n,
            "qtype": rows[qid].qtype,
            "difficulty": rows[qid].difficulty,
            "content": rows[qid].content,
            "image": rows[qid].image,
            "answer": rows[qid].answer,
            "answer_image": rows[qid].answer_image or "",
            "analysis": rows[qid].analysis,
            "source": rows[qid].source,
        }
        for n, qid in enumerate(qids, start=1)
    ]
    opts = {
        "show_answer": show_answer,
        "show_analysis": show_analysis,
        "show_tags": show_tags,
        "show_meta": show_meta,
    }

    if fmt == "html":
        body = paper_export.build_html(title, items, opts).encode("utf-8")
    elif fmt == "docx":
        body = paper_export.build_docx(title, items, opts)
    else:
        body = paper_export.build_pdf(title, items, opts)

    # 记一次使用。questions.usage_count / last_used_at 就是为「组卷去重」留的：
    # 出卷次数攒起来，才看得出哪些题被反复用、该换掉。
    db.execute(
        update(Question)
        .where(Question.id.in_(qids))
        .values(usage_count=func.coalesce(Question.usage_count, 0) + 1,
                last_used_at=datetime.now().isoformat(timespec="seconds"))
    )
    db.commit()

    media, ext, inline = FORMATS[fmt]
    fname = f"{_safe_name(title)}.{ext}"
    disposition = "inline" if inline else "attachment"
    return Response(
        content=body,
        media_type=media,
        headers={
            # 中文文件名必须给 filename*（RFC 5987），同时保留 ASCII 兜底给老客户端
            "Content-Disposition": (
                f'{disposition}; filename="paper.{ext}"; '
                f"filename*=UTF-8''{quote(fname)}"
            )
        },
    )
