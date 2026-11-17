# -*- coding: utf-8 -*-
"""收件箱：把 PDF / Word / 截图丢进 `vault/收件箱/<体系>/`，自动变成题目 md。

## 为什么要有这一层

老师选定的用法是「在 Obsidian 里把 PDF 变成题目模板 md」。但 Obsidian 本身不会把
含公式的数学卷子转成 Markdown —— 社区插件都在抠 PDF 的文字层，而数学卷的公式在文字层里
基本是废的（这条在本项目的识别方案里已经确认过：一律走视觉模型）。

拾课这边**已经**有一条跑通的整页识别链路（配图定位、跨页判断、兜底整题图）。
所以「在 Obsidian 里转换」的实际实现是：文件进了 vault 的收件箱 → 拾课后台识别 →
识别出来的题写成 `题库/` 下的 md。老师看到的就是「拖进去，过一会儿就有了」。
他也确实全程没离开 Obsidian 那个文件夹。

## 体系按**子目录**认，不猜

    vault/收件箱/DSE/2024-P1.pdf    → 按 DSE 体系识别
    vault/收件箱/2024-P1.pdf        → **不处理**，报告里提示挪进子文件夹

为什么不给个「默认体系」：一份卷子一套体系，猜错的代价是整批知识点挂到错误的树上，
而那种错不会报错，只在出卷时发现「知识点全对不上」（同 routers/auto.py 的判断）。

## 状态放在哪

`vault/.shike-inbox.json` 记「哪个文件起了哪个任务」，用来在任务跑完后找到它产生的题、
导出成 md、再把源文件挪进 `_已处理/`。放 vault 根而不是收件箱里：收件箱里多出来的
任何文件都可能被当成待处理文件，一个 json 被当成讲义重命名保存就说不清了。
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import AutoTask, BatchItem, Curriculum, Question
from . import vault

_log = logging.getLogger("shike")

STATE_NAME = ".shike-inbox.json"
DONE_DIR = "_已处理"
FAIL_DIR = "_失败"
# 收件箱能收的东西：卷子（PDF/Word）与直接拍下来的题（截图）。
EXTS = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg"}
POLL_SECONDS = 20
# 同时最多跑几个识别任务。识别是整页视觉模型（一页几秒到几十秒），
# 一次丢十个 PDF 进去全开，机器和 API 额度都会炸。
MAX_ACTIVE = 2
# queued 也算「在跑」？**不算**：真正的在跑是 recognizing/fixing/approving。
# 但 queued 说明已经排队了，占一个名额 —— 否则同一个文件会被开两次任务。
ACTIVE = {"queued", "recognizing", "fixing", "approving"}

# 状态文件（读 → 改 → 写）与「开任务」的互斥锁。后台轮询线程与 HTTP 请求都会进来，
# 不加锁最坏的结果不是丢一个计数，而是**同一份卷子被识别两遍、题入两遍库**。
_lock = threading.Lock()


def _state_path() -> Path:
    return vault.root() / STATE_NAME


def _load() -> dict:
    p = _state_path()
    if not p.is_file():
        return {"tasks": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        # 状态文件坏掉的后果是「重复建任务」，不是丢数据 —— 不为此让整轮停摆
        _log.warning("收件箱状态文件读不了，按空的处理：%s", p)
        return {"tasks": {}}
    if not isinstance(data.get("tasks"), dict):
        data["tasks"] = {}
    return data


def _save(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def _curriculum(db: Session, name: str) -> Curriculum | None:
    """子目录名 → 体系。code 不分大小写，name 必须完全一致（同 auto._resolve_curriculum）。"""
    text = (name or "").strip()
    if not text:
        return None
    low = text.lower()
    for c in db.scalars(select(Curriculum)).all():
        if (c.code or "").lower() == low or (c.name or "") == text:
            return c
    return None


def pending_files() -> list[Path]:
    """收件箱里等着处理的文件（不含已归档的两个子目录）。"""
    box = vault.inbox_dir()
    if not box.is_dir():
        return []
    out = []
    for p in box.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in EXTS:
            continue
        parts = p.relative_to(box).parts
        if parts and parts[0] in (DONE_DIR, FAIL_DIR):
            continue
        out.append(p)
    return sorted(out)


def _move(src: Path, sub: str) -> Path:
    """把处理完的源文件挪进 `_已处理/` 或 `_失败/`，**保留它在收件箱里的相对层级**。

    保留层级是为了「DSE/2024-P1.pdf 处理完了」还能看出它是哪份卷子；
    全拍平到 `_已处理/` 下，两份同名卷子就会互相覆盖。
    """
    box = vault.inbox_dir()
    try:
        relp = src.relative_to(box)
    except ValueError:
        relp = Path(src.name)
    dest = box / sub / relp
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while dest.exists():
        n += 1
        dest = box / sub / relp.with_name(f"{relp.stem}-{n}{relp.suffix}")
    shutil.move(str(src), str(dest))
    return dest


def _export_task_questions(db: Session, task: AutoTask) -> list[str]:
    """把某个任务**已经入库**的题写进 vault，返回写了哪些。

    入库走的是 `batch_import.approve_item`，题已经在库里了；这里只做「写文件」。
    一条题失败不该带走整批 —— 别的题还是该拿到自己的 md。
    """
    try:
        job_ids = json.loads(task.job_ids or "[]")
    except ValueError:
        job_ids = []
    if not job_ids:
        return []
    items = db.scalars(
        select(BatchItem).where(BatchItem.job_id.in_(job_ids), BatchItem.status == "approved")
    ).all()
    written = []
    for it in items:
        if not it.question_id:
            continue
        q = db.get(Question, it.question_id)
        if q is None or q.md_path:
            # 已经有 md 的不重写：那个文件可能已经在 Obsidian 里被改过
            continue
        try:
            vault.export_question(db, q)
            written.append(q.id)
        except Exception as exc:                   # noqa: BLE001
            _log.warning("收件箱：题目 %s 导出 md 失败：%s", q.id, exc)
    db.commit()
    return written


def _copy_origin(it: BatchItem) -> str:
    """把这道题的原貌截图复制进 `附件/`，返回文件名（拿不到就空串）。

    待审稿为什么要把原貌图也放过去：老师在 Obsidian 里改题时最需要的就是
    「这题原本长什么样」—— 尤其配图疑点的题，光看识别出来的文字根本判断不了。
    文件名带条目前缀，和题目附件不会互相覆盖。
    """
    if not it.image:
        return ""
    src = config.CROPS_DIR / Path(it.image).name
    if not src.is_file():
        return ""
    name = f"原貌-{it.id}{src.suffix or '.png'}"
    try:
        shutil.copyfile(src, vault.attach_dir() / name)
    except OSError:
        return ""
    return name


def _export_pending_items(db: Session, task: AutoTask) -> list[str]:
    """把**有疑点、但内容没坏**的条目写进 `题库/_待审/`，返回写了哪些路径。

    为什么值得落成文件：识别不可能总是完美（配图框歪了、某页模型返回坏 JSON）。
    这些条目以前只躺在网页待审列表里，而老师日常就在 Obsidian 里 ——
    把题摊到他面前，他能随手截图贴图、对着原貌图改公式，比在网页上逐条框图省事。
    改完把文件从 `_待审/` 拖进 `题库/`，下一次同步就入库。

    ⚠️ **判据用 `auto_import.judge`，别自己写**：什么算「内容本身坏了」（bad_json /
       parse_failed / empty_stem）已经有唯一出处。内容坏了的条目导出成 md 毫无意义 ——
       那里面是占位文本，老师改不出东西来。

    ⚠️ 稿子**不写 `shike_id`**（见 `vault.render` 的 `with_id`）：这题还没入库，
       写个不存在的 id 只会让「拖进题库 → 同步」时多一句白报。
    """
    from . import auto_import, batch_import

    try:
        job_ids = json.loads(task.job_ids or "[]")
    except ValueError:
        job_ids = []
    if not job_ids:
        return []
    items = db.scalars(
        select(BatchItem).where(BatchItem.job_id.in_(job_ids))
        .order_by(BatchItem.seq)
    ).all()
    written: list[str] = []
    for it in items:
        if it.status == "approved" or not (it.content or "").strip():
            continue
        verdict, reason = auto_import.judge(
            it.content, it.flags, bool(it.needs_figure), it.figure_image or "")
        if verdict == "skip":
            # 内容本身坏了（坏 JSON / 解析失败 / 空题干）—— 那是占位文本，导出成 md
            # 老师也改不出东西来，只能重跑那一页。它留在网页待审列表里更合适。
            #
            # ⚠️ 判据是 `== "skip"` 而不是 `!= "pass"`：`repair`（配图有疑点/没配上）
            #    和 `human`（自动修图也失败）**正是**最该落到 Obsidian 里让人改的一类。
            continue
        try:
            qid = uuid.uuid4().hex[:12]
            q, _node = batch_import.build_question(db, it, qid)   # 与正式入库同一份字段映射
            todo = "拾课识别没把握，先放这里等你看一眼 —— " + auto_import.reason_text(reason)
            origin = _copy_origin(it)
            if origin:
                todo += f"。原貌图（识别时的整题截图）在 附件/{origin}"
            if it.note:
                todo += f"。模型自述：{it.note[:200]}"
            todo += "。改好这个文件后，把它拖进 题库/ 再点「同步」就入库"
            path = vault.write_new(q, folder=vault.PENDING,
                                   extra_fm={"shike_todo": todo}, with_id=False)
            written.append(vault.rel(path))
        except Exception as exc:                   # noqa: BLE001 —— 一条失败不带走整批
            _log.warning("收件箱：条目 %s 写待审稿失败：%s", it.id, exc)
            db.rollback()
    return written


def scan_once(db: Session, *, enqueue: bool = True) -> dict:
    """跑一轮：先收尾已经跑完的任务，再给新文件开任务。

    顺序不能反 —— 先开新的会让「同时最多 2 个」这个上限失效。
    """
    vault.ensure_dirs()
    report: dict = {"started": [], "finished": [], "failed": [], "ignored": [],
                    "waiting": [], "drafts": []}
    # ⚠️ 状态文件是「读 → 改 → 写」，后台轮询线程和 HTTP 请求**会同时进来**。
    #    不加锁的经典后果：网页上传刚登记好，轮询线程用几秒前读到的旧内容写回去，
    #    登记就消失了 → 下一轮看到文件还在收件箱 → 再开一次任务 → **同一份卷子入两遍库**。
    with _lock:
        state = _load()
        _scan_locked(db, state, report, enqueue=enqueue)
        _save(state)
    return report


def _scan_locked(db: Session, state: dict, report: dict, *, enqueue: bool = True) -> None:

    # ---- ① 收尾：看已开跑的任务
    active = 0
    for key, info in list(state["tasks"].items()):
        task = db.get(AutoTask, info.get("task_id") or "")
        if task is None:
            _log.warning("收件箱：任务 %s 已经不在了，从状态里清掉", info.get("task_id"))
            state["tasks"].pop(key, None)
            continue
        if task.status in ACTIVE:
            active += 1
            report["waiting"].append({"file": key, "task_id": task.id, "status": task.status,
                                      "stage": task.stage})
            continue
        if task.status in ("done", "partial"):
            ids = _export_task_questions(db, task)
            # 零疑点的题已经入库并写成了 md；有疑点的题也不能就这么留在网页里 ——
            # 写成 `题库/_待审/` 的稿子，老师在 Obsidian 里改完拖进来才算数。
            drafts = _export_pending_items(db, task)
            report["drafts"].extend(drafts)
            src = vault.root() / key
            moved = ""
            if src.is_file():
                try:
                    moved = vault.rel(_move(src, DONE_DIR))
                except OSError as exc:
                    _log.warning("收件箱：挪走 %s 失败：%s", src, exc)
            report["finished"].append({"file": key, "task_id": task.id, "status": task.status,
                                       "questions": len(ids), "drafts": len(drafts),
                                       "moved_to": moved,
                                       "approved": task.approved or 0})
            state["tasks"].pop(key, None)
            continue
        # failed
        src = vault.root() / key
        moved = ""
        if src.is_file():
            try:
                moved = vault.rel(_move(src, FAIL_DIR))
            except OSError:
                pass
        report["failed"].append({"file": key, "task_id": task.id, "error": task.error or task.stage,
                                 "moved_to": moved})
        state["tasks"].pop(key, None)

    # ---- ② 开新任务
    #
    # `enqueue=False` 是「只看会开什么、不开」：校验一样要跑完（认体系、挡根目录文件），
    # 只是不真的去识别。否则预检时永远看到一片空白，等于没预检。
    for path in pending_files():
        if active >= MAX_ACTIVE:
            report["waiting"].append({"file": vault.rel(path), "task_id": None, "status": "排队"})
            continue
        key = vault.rel(path)
        if key in state["tasks"]:
            continue
        box = vault.inbox_dir()
        try:
            parts = path.relative_to(box).parts
        except ValueError:
            parts = ()
        if len(parts) < 2:
            report["ignored"].append({
                "file": key,
                "reason": f"放在收件箱根下了。请挪进以体系命名的子文件夹"
                          f"（例如 收件箱/{_first_code(db) or 'DSE'}/）",
            })
            continue
        curr = _curriculum(db, parts[0])
        if curr is None:
            report["ignored"].append({
                "file": key,
                "reason": f"子文件夹 {parts[0]} 对不上任何体系。可用的：{_codes(db)}",
            })
            continue
        if not enqueue:
            report["started"].append({"file": key, "task_id": None,
                                      "curriculum": curr.code or curr.name, "dry": True})
            continue
        try:
            task_id = _kickoff(db, path, curr)
        except Exception as exc:                   # noqa: BLE001 —— 一个文件坏了不影响别的
            _log.warning("收件箱：%s 起任务失败：%s", key, exc)
            report["failed"].append({"file": key, "task_id": None, "error": str(exc),
                                     "moved_to": ""})
            continue
        state["tasks"][key] = {
            "task_id": task_id, "curriculum": curr.code or curr.name,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        report["started"].append({"file": key, "task_id": task_id,
                                  "curriculum": curr.code or curr.name})
        active += 1


def enqueue(db: Session, path: Path, curr: Curriculum) -> dict:
    """把**一个**已经放进收件箱的文件立刻开识别任务（网页上传走这条）。

    为什么要绕回收件箱：这样「网页上传的」和「老师从 Obsidian 拖进收件箱的」
    是同一种东西 —— 收尾、挪 `_已处理/`、导出 md、写待审稿全走同一段代码。
    另起一套「转换任务」等于把这条链复制一遍，两套的半成品迟早不一样。
    """
    key = vault.rel(path)
    with _lock:
        state = _load()
        if key in state["tasks"]:
            return {"file": key, "task_id": state["tasks"][key].get("task_id"),
                    "already": True}
        task_id = _kickoff(db, path, curr)
        state["tasks"][key] = {
            "task_id": task_id, "curriculum": curr.code or curr.name,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        _save(state)
    return {"file": key, "task_id": task_id, "curriculum": curr.code or curr.name}


def _codes(db: Session) -> str:
    rows = db.scalars(select(Curriculum)).all()
    return "、".join((c.code or c.name or "") for c in rows) or "（库里的体系列表是空的）"


def _first_code(db: Session) -> str:
    c = db.scalars(select(Curriculum)).first()
    return (c.code or c.name) if c else ""


def _kickoff(db: Session, path: Path, curr: Curriculum) -> str:
    """文件 → 文档 → 识别任务。返回 task id。

    ⚠️ `register_document` / `_precheck_pages` 在 routers/documents.py、routers/auto.py 里。
       从这里 import router 有点倒挂，但 `routers/auto.py` 已经这么用了（同一个函数、
       同一个理由：它做的解析与落库本来就该是服务，只是当年没搬）。再抄一份等于给
       「文档怎么建」「页数怎么数」各留两个出处 —— 那种重复迟早会分叉。
    """
    from ..routers.auto import _precheck_pages
    from ..routers.documents import check_ext, register_document
    from . import auto_import

    ext = path.suffix.lower()
    check_ext(ext)
    # 复制进 uploads 而不是引用 vault 里的路径：题库的资产归自己管。
    # 老师整理 vault 时把 PDF 删了，不该让已经入库的题失去原卷。
    save_path = config.UPLOAD_DIR / f"{path.stem}-{int(time.time())}{ext}"
    shutil.copy2(path, save_path)
    try:
        doc, _blocks = register_document(db, path.name, ext, save_path)
    except Exception:
        save_path.unlink(missing_ok=True)
        raise
    # 页数不从 doc 字段猜（那是 Word 页面视图的缓存，PDF 常常是 0）。
    # `_precheck_pages` 会先把 .docx 转成 PDF 再数页 —— 这正是「页图拿不拿得到」的预检，
    # 放在这里做，失败能在开任务之前就说清楚，而不是让后台跑到第 1 页才炸。
    pages = _precheck_pages(db, doc, None)
    task = auto_import.start(db, document_id=doc.id, pages=pages,
                             curriculum_id=curr.id, src_filename=path.name)
    return task.id


# ---------------------------------------------------------------- 后台线程
_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop() -> None:
    from ..db import SessionLocal

    _log.info("收件箱：后台轮询已启动（每 %s 秒，最多同时 %s 个识别任务）",
              POLL_SECONDS, MAX_ACTIVE)
    while not _stop.is_set():
        try:
            with SessionLocal() as db:
                r = scan_once(db)
            n = len(r["started"])
            if n or r["finished"] or r["failed"]:
                _log.info("收件箱：开任务 %d，完成 %d，失败 %d", n, len(r["finished"]), len(r["failed"]))
        except Exception:                          # noqa: BLE001 —— 轮询线程绝不能死
            _log.exception("收件箱：这一轮扫描出错，下一轮继续")
        _stop.wait(POLL_SECONDS)


def start_worker() -> bool:
    """起后台线程。已经起了就什么都不做（服务重载时 lifespan 会被调多次）。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="shike-inbox", daemon=True)
    _thread.start()
    return True


def stop_worker() -> None:
    _stop.set()
