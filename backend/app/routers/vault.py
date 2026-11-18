# -*- coding: utf-8 -*-
"""题库 vault 接口：扫描磁盘上的题目 md、把题库里的题导出成 md。

为什么把这些单独开一个路由，而不是塞进 questions 或 notes：
  它操作的对象既不是「库里的一道题」也不是「一篇笔记」，而是**磁盘上一个文件夹**。
  前缀 `/api/vault/*`，和题库（/api/questions）、笔记（/api/notes）平级，
  将来要加「讲义目录」「学生资料目录」也不会在这两个路由里越堆越乱。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import AutoTask, Curriculum, Question
from ..services import vault

router = APIRouter(prefix="/api/vault", tags=["vault"])


class ExportIn(BaseModel):
    qids: list[str] = Field(default_factory=list, description="要导出的题；空 = 所有还没有 md 来源的题")
    limit: int = Field(200, ge=1, le=2000)


# ---------------------------------------------------------------- 状态
@router.get("")
def vault_status(db: Session = Depends(get_db)):
    """vault 在哪、里面有什么、库里有多少题挂在这上面。

    前端第一件事就是问这个：老师要拿 `root` 这个路径去 Obsidian 里「打开文件夹作为库」。
    """
    root = vault.root()
    md_total = md_questions = 0
    if root.is_dir():
        for p in vault._iter_md():
            md_total += 1
            try:
                if vault.is_question_md(vault.read_md(p).text):
                    md_questions += 1
            except OSError:
                continue
    linked = db.scalar(select(func.count()).select_from(Question)
                       .where(Question.md_path.isnot(None))) or 0
    # 「收件箱里还有东西没处理」是老师最容易困惑的一件事：拖进去了却半天没变成题目。
    # 两条路的计数分开报 —— md 是本地解析（几秒），卷子要识别（几分钟），
    # 混成一个数会让人以为 md 卡住了。
    from ..services import vault_inbox
    docs, mds = vault_inbox.pending_split()
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "writable": _writable(root),
        "md_total": md_total,
        "md_questions": md_questions,
        "linked": linked,
        "inbox_pending": len(docs),
        "inbox_md": len(mds),
        "dirs": {"shelf": vault.SHELF, "attach": vault.ATTACH, "inbox": vault.INBOX},
    }


def _writable(p: Path) -> bool:
    """vault 建得出来吗。老师把路径指到一个只读盘时，早点说比同步时报错好。"""
    try:
        vault.ensure_dirs()
        probe = p / ".shike-write-test"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 扫描
@router.post("/scan")
def scan_vault(dry_run: bool = Query(False, description="只出报告，不落库也不写文件"),
               db: Session = Depends(get_db)):
    """扫一遍 vault，把改过的题目 md 同步进题库。

    `dry_run=true` 是给「我改了一堆文件，先看看会变成什么」用的：
    它**不**改库、不拷图、不写回 `shike_id`。
    """
    if not vault.root().is_dir():
        raise HTTPException(404, f"vault 目录还不存在：{vault.root()}。先点「初始化」建目录。")
    try:
        report = vault.scan(db, dry_run=dry_run)
    except Exception as exc:                       # noqa: BLE001 —— 让前端看到原因，别只给 500
        raise HTTPException(500, f"同步失败：{type(exc).__name__}: {exc}") from exc
    report["dry_run"] = dry_run
    report["root"] = str(vault.root())
    return report


# ---------------------------------------------------------------- 导出
@router.post("/export")
def export_to_vault(payload: ExportIn, db: Session = Depends(get_db)):
    """把题库里的题写成 md。

    不传 qids 时只导出**还没有 md 来源的**题 —— 那是「第一次建立 vault」的动作。
    已经有来源的题不在这里重写：那些文件老师可能已经在 Obsidian 里改过，
    批量覆盖会把他的改动冲掉。想让库里改动落到文件上，是在网页上改题时自动写回的
    （见 `questions.update_question` → `vault.sync_back`）。
    """
    vault.ensure_dirs()
    stmt = select(Question)
    if payload.qids:
        stmt = stmt.where(Question.id.in_(payload.qids))
    else:
        stmt = stmt.where(Question.md_path.is_(None))
    rows = db.scalars(stmt.limit(payload.limit)).all()
    if not rows:
        return {"exported": [], "failed": [], "note": "没有需要导出的题"}

    exported, failed = [], []
    for q in rows:
        try:
            path = vault.export_question(db, q)
            exported.append({"id": q.id, "path": vault.rel(path)})
        except Exception as exc:                   # noqa: BLE001 —— 一道题失败不该中断整批
            db.rollback()
            failed.append({"id": q.id, "error": str(exc)})
    db.commit()
    return {"exported": exported, "failed": failed,
            "note": "" if len(rows) < payload.limit else f"只导出了前 {payload.limit} 道，再点一次接着导"}


# ---------------------------------------------------------------- 模板与目录
TEMPLATE = """---
shike: question
shike_id: {qid}
qtype: 选择题
difficulty: 基础
score: 3
knowledge:
  - 待填知识点
tags:
  - 待填标签
---

（题干写在这里，可以直接写 LaTeX 公式，比如 $x^2-1$）

（题目里有图就拖进来，图片会按位置存到「题干配图」）

A．选项一
B．选项二

## 答案

A

## 解析

（解的过程写在这里）
"""


@router.post("/seed-template")
def seed_template(db: Session = Depends(get_db)):
    """在 `题库/` 下放一份可以照着抄的模板。

    这东西的价值不在「生成文件」，在**它本身就是格式说明**：
    老师打开它、复制它、改内容，比读一份文档快得多。
    已经有模板文件时不覆盖（那是他自己改过的）。
    """
    vault.ensure_dirs()
    path = vault.shelf_dir() / "题目模板（照这个写）.md"
    existed = path.is_file()
    if not existed:
        vault.write_md(path, TEMPLATE.format(qid="（留空，第一次同步时自动填）"))
    return {"path": vault.rel(path), "existed": existed,
            "note": "已经有一份了，没覆盖" if existed else "已生成"}


@router.post("/open")
def open_vault():
    """在资源管理器里打开 vault —— 老师要拿这个路径去 Obsidian 里「打开文件夹作为库」。

    路径由配置推出，不接任何外部传入的路径（和 students.open_student_folder 同一个理由）。
    """
    root = vault.ensure_dirs()
    if not hasattr(os, "startfile"):        # 非 Windows
        raise HTTPException(400, f"这个系统上不能自动打开文件夹，路径是：{root}")
    try:
        os.startfile(str(root))              # noqa: S606 —— 本机自用，路径是我们自己的配置
    except OSError as exc:
        raise HTTPException(500, f"打开文件夹失败：{exc}") from exc
    return {"ok": True, "path": str(root)}


@router.get("/source/{qid}")
def question_source(qid: str, db: Session = Depends(get_db)):
    """这道题来自哪个 md（给题目详情页显示「在 Obsidian 里打开」用）。

    返回 `uri` 是 Obsidian 的协议链接。用 `path=` 而不是 `vault=名字`：
    vault 名字是用户在 Obsidian 里自己起的，服务端不知道；绝对路径一定对得上。
    """
    q = db.get(Question, qid)
    if q is None:
        raise HTTPException(404, "题目不存在")
    if not q.md_path:
        return {"qid": qid, "md_path": None, "exists": False, "uri": None}
    try:
        full = vault.resolve(q.md_path)
        exists = full.is_file()
    except ValueError:
        return {"qid": qid, "md_path": q.md_path, "exists": False, "uri": None}
    from urllib.parse import quote
    uri = "obsidian://open?path=" + quote(str(full), safe="") if exists else None
    return {"qid": qid, "md_path": q.md_path, "abs": str(full), "exists": exists, "uri": uri}


# ---------------------------------------------------------------- 收件箱
@router.get("/inbox")
def inbox_status(db: Session = Depends(get_db)):
    """收件箱里有什么、跑着几个任务。

    「拖进去了却半天没变成题目 md」是这条路上最容易困惑的一刻，
    所以要把「识别中 / 排队 / 失败原因」都摆出来，而不是只回一个文件数。
    """
    from ..services import vault_inbox

    state = vault_inbox._load()
    tasks = []
    for key, info in state["tasks"].items():
        row = {"file": key, "task_id": info.get("task_id"), "status": "", "stage": "",
               "error": "", "approved": 0}
        t = db.get(AutoTask, info.get("task_id") or "")
        if t is not None:
            row.update(status=t.status, stage=t.stage or "", error=t.error or "",
                       approved=t.approved or 0)
        tasks.append(row)
    docs, mds = vault_inbox.pending_split()
    return {
        "dir": str(vault.inbox_dir()),
        "hint": "卷子放 收件箱/<体系>/（子文件夹名就是体系）；插件转出来的 md 放收件箱里任意位置都行",
        "pending": [vault.rel(p) for p in docs],
        "pending_md": [vault.rel(p) for p in mds],
        "tasks": tasks,
        "curricula": [c.code or c.name for c in db.scalars(select(Curriculum)).all()],
        "enabled": config.VAULT_INBOX,
    }


@router.post("/inbox/run")
def inbox_run(db: Session = Depends(get_db)):
    """立刻跑一轮收件箱（正常情况下后台每 20 秒自己跑，这个按钮是「别等了，现在跑」）。

    不会让已经跑着的任务重来：`scan_once` 会跳过状态里已有的文件。
    """
    from ..services import vault_inbox

    vault.ensure_dirs()
    try:
        return vault_inbox.scan_once(db)
    except Exception as exc:                       # noqa: BLE001
        raise HTTPException(500, f"收件箱处理失败：{type(exc).__name__}: {exc}") from exc
