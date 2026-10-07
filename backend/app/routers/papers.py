# -*- coding: utf-8 -*-
"""出卷：把库里的题目按顺序拼成一份 A4 试卷并导出。

与录题页的分工：录题页负责「把一道题从原卷里抠出来入库」，
这里负责「把库里的题再拼成一份新卷子」。两者共用 questions 表，但互不依赖。

为什么导出用 GET 而不是 POST：
  前端可以直接 window.open(url) 预览/下载，不必把二进制读进 fetch 再自己造 Blob 存盘
  —— 少一层能出错的地方，也让「在新标签页预览」变成一行代码。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import PaperTemplate, Question
from ..schemas import PaperTemplateCopyIn, PaperTemplateIn
from ..services import paper_export, paper_style

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


def _tags_of(raw: str | None) -> list[str]:
    """解析 tags 的 JSON 数组。

    没去 import questions.py 里的同名函数：两个 router 互相 import 容易绕成环，
    而这只是个 4 行的容错解析，不值得为此建一层公共模块。
    """
    try:
        v = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(t) for t in v] if isinstance(v, list) else []


@router.get("/papers/export")
def export_paper(
    ids: str = Query(..., description="题目 id，逗号分隔；顺序即卷面顺序"),
    format: str = Query("html", description="html / docx / pdf"),
    title: str = Query("试卷"),
    template_id: int | None = Query(None, description="卷种样式模板 id；不传 = 默认样式"),
    show_answer: bool = Query(True, description="末尾附参考答案与解析"),
    show_analysis: bool = Query(False, description="答案里是否带解析"),
    show_tags: bool = Query(True, description="题号后是否标注题型·难度"),
    show_meta: bool = Query(True, description="是否留姓名/班级/日期填写栏"),
    render_mode: str = Query(
        "auto",
        description="整卷呈现方式：auto 按每题各自的偏好 / text 全用文本 / image 全用图片。"
                    "缺那种形态的题会自动退回另一种，不会印出空白题",
    ),
    db: Session = Depends(get_db),
):
    fmt = (format or "html").lower()
    if fmt not in FORMATS:
        raise HTTPException(422, f"不支持的格式 {format}（可选 html / docx / pdf）")
    # 拼错的值不能静默当成 auto —— 那会让老师以为「选了图片怎么没变」，
    # 和模板不存在时报错同一个理由：宁可报一个能看懂的错误。
    if render_mode not in ("auto", "text", "image"):
        raise HTTPException(422, f"不支持的呈现方式 {render_mode}（可选 auto / text / image）")

    qids = [s.strip() for s in (ids or "").split(",") if s.strip()]
    if not qids:
        raise HTTPException(422, "没有选中任何题目")
    if len(qids) > 200:
        raise HTTPException(422, "一次最多导出 200 道题")

    rows = {q.id: q for q in db.scalars(select(Question).where(Question.id.in_(qids))).all()}
    missing = [q for q in qids if q not in rows]
    if missing:
        raise HTTPException(404, f"有 {len(missing)} 道题不存在（可能已被删除），请刷新后重选")

    # 模板不存在时不静默退回默认样式 —— 那会让老师以为「换了样式怎么没变」，
    # 排查半天。宁可报一个能看懂的错误。
    tpl = None
    if template_id:
        tpl = db.get(PaperTemplate, template_id)
        if tpl is None:
            raise HTTPException(404, "卷种样式模板不存在（可能已被删除），请重新选择")

    # 卷面顺序由前端决定，必须按传入顺序编号 —— 按数据库顺序编号会让「上移/下移」白做
    items = [
        {
            "n": n,
            "qtype": rows[qid].qtype,
            "difficulty": rows[qid].difficulty,
            "content": rows[qid].content,
            "image": rows[qid].image,
            # 题干里那幅「如图」的图（老师审核时框出来的）。
            # 文本形态出卷时 image 整张不印，靠它把图补上 —— 少了这行，
            # 卷子上会出现「如图」却找不到图，而且没有任何报错。
            "figure_image": rows[qid].figure_image or "",
            "answer": rows[qid].answer,
            "answer_image": rows[qid].answer_image or "",
            "analysis": rows[qid].analysis,
            "source": rows[qid].source,
            "score": rows[qid].score,
            "tags": _tags_of(rows[qid].tags),
            # 每题自己的呈现偏好；整卷的 render_mode 在 opts 里，覆盖它
            "render_prefer": rows[qid].render_prefer or "auto",
        }
        for n, qid in enumerate(qids, start=1)
    ]
    opts = {
        "show_answer": show_answer,
        "show_analysis": show_analysis,
        "show_tags": show_tags,
        "show_meta": show_meta,
        "render_mode": render_mode,
    }

    if fmt == "html":
        body = paper_export.build_html(title, items, opts, tpl).encode("utf-8")
    elif fmt == "docx":
        body = paper_export.build_docx(title, items, opts, tpl)
    else:
        body = paper_export.build_pdf(title, items, opts, tpl)

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
    # 文件名带版本：两份同名文件是「把教师版发给学生」的头号原因。
    # 版本由 show_answer 决定，不需要新参数 —— 有答案就是教师版。
    version = "教师版" if show_answer else "学生版"
    fname = f"{_safe_name(title)}_{version}.{ext}"
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


# ---------------------------------------------------------------- 卷种样式模板
def _template_out(t: PaperTemplate) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "code": t.code or "",
        "sample": t.sample or "",
        "is_builtin": bool(t.is_builtin),
        "sort_order": t.sort_order,
        # 走同一份解析：界面看到的字段和导出时用的字段必须是同一套，
        # 否则「界面上明明改了却没生效」这类问题根本查不出来。
        **paper_style.resolve(t),
    }


@router.get("/paper-templates")
def list_paper_templates(db: Session = Depends(get_db)):
    """卷种样式列表。内置四套 + 用户自己复制改的，一起返回。"""
    rows = db.scalars(
        select(PaperTemplate)
        .where(PaperTemplate.owner_id == config.OWNER_ID)
        .order_by(PaperTemplate.sort_order, PaperTemplate.id)
    ).all()
    return {"items": [_template_out(t) for t in rows]}


@router.get("/paper-templates/{tid}")
def get_paper_template(tid: int, db: Session = Depends(get_db)):
    t = db.get(PaperTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    return _template_out(t)


@router.post("/paper-templates")
def create_paper_template(payload: PaperTemplateIn, db: Session = Depends(get_db)):
    """新建一套模板（从零）。

    三大块都不传时用 `paper_style` 里那组默认值解析 —— 也就是
    「不分区、不带抬头的朴素卷面」，是个有效的起点。
    但日常用法是走下面的 copy：挑一套最像的内置模板复制再改，比从零填省事得多。
    """
    name = (payload.name or "").strip() or "未命名样式"
    t = PaperTemplate(
        owner_id=config.OWNER_ID,
        name=name,
        code="",
        paper=json.dumps(payload.paper or {}, ensure_ascii=False),
        style=json.dumps(payload.style or {}, ensure_ascii=False),
        sections=json.dumps(payload.sections or [], ensure_ascii=False),
        sample=(payload.sample or "").strip(),
        is_builtin=0,
        sort_order=payload.sort_order or _next_sort_order(db),
    )
    db.add(t)
    db.commit()
    return _template_out(t)


@router.post("/paper-templates/copy")
def copy_paper_template(payload: PaperTemplateCopyIn, db: Session = Depends(get_db)):
    """复制一份模板 —— 这才是「可扩展」真正的用法。

    老师的正确路径不是从零填二十个字段，而是：挑一套最像的内置模板 → 复制 →
    改抬头和注意事项 → 存成「XX 中学月考」。所以复制必须是一条一次的接口，
    而不是让前端把整套 JSON 读出来再 POST 回来（那样前端就得自己拼三大块，
    字段名一旦和后端对不上，复制出来的模板就是坏的）。
    """
    src = db.get(PaperTemplate, payload.source_id) if payload.source_id else None
    if payload.source_id and src is None:
        raise HTTPException(404, "要复制的模板不存在")

    name = (payload.name or "").strip()
    if not name:
        base = f"{src.name}（副本）" if src else "新样式"
        existing = {t.name for t in db.scalars(
            select(PaperTemplate).where(PaperTemplate.owner_id == config.OWNER_ID)
        ).all()}
        name, i = base, 2
        while name in existing:          # 同名会让下拉里出现两个一模一样的选项
            name = f"{base} {i}"
            i += 1

    t = PaperTemplate(
        owner_id=config.OWNER_ID,
        name=name,
        code="",
        # 复制的是**解析后**的值，不是原始串：源模板若有个字段是坏的，
        # 副本拿到的是干净值，不会把坏数据一代代传下去。
        paper=json.dumps(paper_style.normalize_paper(src.paper if src else None), ensure_ascii=False),
        style=json.dumps(paper_style.normalize_style(src.style if src else None), ensure_ascii=False),
        sections=json.dumps(paper_style.normalize_sections(src.sections if src else None), ensure_ascii=False),
        sample=(src.sample or "") if src else "",
        is_builtin=0,
        sort_order=_next_sort_order(db),
    )
    db.add(t)
    db.commit()
    return _template_out(t)


@router.patch("/paper-templates/{tid}")
def patch_paper_template(tid: int, payload: PaperTemplateIn, db: Session = Depends(get_db)):
    """改模板。**没传的块保持原样** —— 只改名字不该把排版重置回默认。

    内置模板也能改（学校自己的抬头才是最有用的那部分），只是不能删。
    """
    t = db.get(PaperTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    if (payload.name or "").strip():
        t.name = payload.name.strip()
    if payload.paper is not None:
        t.paper = json.dumps(payload.paper, ensure_ascii=False)
    if payload.style is not None:
        t.style = json.dumps(payload.style, ensure_ascii=False)
    if payload.sections is not None:
        t.sections = json.dumps(payload.sections, ensure_ascii=False)
    if payload.sample:
        t.sample = payload.sample.strip()
    if payload.sort_order:
        t.sort_order = payload.sort_order
    db.commit()
    return _template_out(t)


@router.delete("/paper-templates/{tid}")
def delete_paper_template(tid: int, db: Session = Depends(get_db)):
    t = db.get(PaperTemplate, tid)
    if t is None:
        raise HTTPException(404, "模板不存在")
    if t.is_builtin:
        # 和反馈模板同一条口径：内置的允许改、不允许删。
        # 删了下次启动种子又会建回来，反而让人以为「删不掉」。
        raise HTTPException(400, "内置样式不能删除；可以改它的抬头，或复制一份再改")
    db.delete(t)
    db.commit()
    return {"ok": True}


def _next_sort_order(db: Session) -> int:
    """新模板排在内置之后（内置 0..3）。用现有最大值 +1，避免并列。"""
    mx = db.scalar(
        select(func.max(PaperTemplate.sort_order)).where(PaperTemplate.owner_id == config.OWNER_ID)
    )
    return int(mx or 0) + 1
