# -*- coding: utf-8 -*-
"""题库 vault 接口：扫描磁盘上的题目 md、把题库里的题导出成 md、
**把 PDF / Word / 图片转成题目 md**。

为什么把这些单独开一个路由，而不是塞进 questions 或 notes：
  它操作的对象既不是「库里的一道题」也不是「一篇笔记」，而是**磁盘上一个文件夹**。
  前缀 `/api/vault/*`，和题库（/api/questions）、笔记（/api/notes）平级，
  将来要加「讲义目录」「学生资料目录」也不会在这两个路由里越堆越乱。

关于「PDF → markdown 由谁做」（老师 2026-11-18 点明的一条）：
  不靠 Obsidian 插件。社区插件都在抠 PDF 的**文字层**，而数学卷里的公式在文字层里
  基本是废的（本项目已经确认过：含公式的卷一律走视觉模型）。让插件转出来的 md
  公式是乱的、配图是没有的 —— 那正是「效果无法严格保证」的来源。
  所以转换这一步留在拾课：它已有跑通的整页识别（配图定位、跨页判断、兜底整题图），
  转出来的 md 才带得上公式与图。Obsidian 那边只负责**编辑**。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
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
    inbox = 0
    if vault.inbox_dir().is_dir():
        # 「收件箱里还有东西没处理」是老师最容易困惑的一件事：
        # 拖进去了却半天没变成题目 md —— 得能直接看到还剩几个。
        inbox = sum(1 for p in vault.inbox_dir().rglob("*")
                    if p.is_file() and p.suffix.lower() in (".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg"))
    return {
        "root": str(root),
        "exists": root.is_dir(),
        "writable": _writable(root),
        "md_total": md_total,
        "md_questions": md_questions,
        "linked": linked,
        "inbox_pending": inbox,
        # 识别出来但没把握的稿子（`题库/_待审/`）。它们**还没入库** —— 老师改好拖进
        # `题库/` 再同步才算。这个数要摆在页面上，否则老师永远不知道有东西在等他。
        "pending_review": len(_pending_review()),
        "dirs": {"shelf": vault.SHELF, "attach": vault.ATTACH, "inbox": vault.INBOX,
                 "pending": vault.PENDING},
    }


def _pending_review() -> list[Path]:
    """`题库/_待审/` 下的题目 md。"""
    d = vault.pending_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if p.is_file())


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


# ---------------------------------------------------------------- 转换（文档 → 题目 md）
_BAD_FILE = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _clean_filename(filename: str, ext: str) -> str:
    """上传来的文件名 → 能落盘的名字（去路径分隔符、去 Windows 保留字符、截长度）。

    不保留原扩展名以外的东西：老师传的可能是 `2024 P1（真题）(1).pdf`，
    括号和空格都留着没问题，但反斜杠、斜杠、冒号、星号这些会让 `write_bytes` 抛错。
    """
    stem = _BAD_FILE.sub(" ", Path(filename or "上传").stem).strip(" .") or "上传"
    return f"{stem[:60].strip(' .') or '上传'}{ext}"


def _unique(directory: Path, name: str) -> Path:
    """同名文件不覆盖：老师可能第二次传同一份卷子的修订版。"""
    path = directory / name
    n = 1
    while path.exists():
        n += 1
        path = directory / f"{Path(name).stem}-{n}{Path(name).suffix}"
    return path


@router.post("/convert")
def convert_to_md(
    file: UploadFile = File(...),
    curriculum: str | None = Form(None, description="体系：id / code / name 任一"),
    db: Session = Depends(get_db),
):
    """上传一份 PDF / Word / 图片 → 识别 → 写成 `题库/` 下的题目 md。

    **转换由拾课做，不靠 Obsidian 插件。** 插件抠的是 PDF 的文字层，数学卷的公式在
    文字层里基本是废的；拾课这边走视觉模型，公式原样落成 `$…$`、配图单独裁出来。
    这样转出来的 md 才能「符合模板、直接录入题库」。

    落点：文件先进 `收件箱/<体系 code>/`（和老师从 Obsidian 拖进去是同一条路），
    跑完由收件箱的收尾逻辑写 md —— 零疑点的进 `题库/`，没把握的进 `题库/_待审/`。
    这样两种来源（网页上传 / 拖进文件夹）不会有两套半成品。

    同步 def（不是 async）：`_kickoff` 里有 PyMuPDF / Word 的同步解析，几百毫秒到几十秒。
    """
    from ..services import vault_inbox
    from .auto import _resolve_curriculum
    from .documents import check_ext

    curr = _resolve_curriculum(db, curriculum)          # 先校验体系：别为无效调用存文件
    filename = file.filename or "upload"
    ext = os.path.splitext(filename)[1].lower()
    check_ext(ext)                                      # 不认识的后缀在这里就 422

    vault.ensure_dirs()
    dest_dir = vault.inbox_dir() / (curr.code or curr.name or str(curr.id))
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique(dest_dir, _clean_filename(filename, ext))
    dest.write_bytes(file.file.read())

    try:
        res = vault_inbox.enqueue(db, dest, curr)
    except Exception as exc:                            # noqa: BLE001
        # 开不了任务就别把文件留在收件箱：下一轮扫描会再试一次，然后又是同样的报错，
        # 而老师从页面上看不出「其实已经失败了」。删掉 + 把原因返回去。
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"转换失败：{exc}") from exc

    res.update({
        "curriculum": curr.code or curr.name,
        "dir": vault.rel(dest_dir),
        "hint": "识别在后台跑（一页几秒）。跑完题目 md 出现在 题库/ 下；"
                "拾课没把握的会进 题库/_待审/，等在 Obsidian 里看过再拖进 题库/。",
    })
    return res


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
    files = [vault.rel(p) for p in vault_inbox.pending_files()]
    return {
        "dir": str(vault.inbox_dir()),
        "hint": "把卷子放进 收件箱/<体系 code>/ 里（子文件夹名就是体系）",
        "pending": files,
        "tasks": tasks,
        # 识别完但没把握的稿子：在 Obsidian 里看、改，然后拖进 题库/ 再点同步
        "review": [vault.rel(p) for p in _pending_review()],
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
