# -*- coding: utf-8 -*-
"""收件箱：把卷子 / 截图 / 插件转出来的 md 丢进 `vault/收件箱/<体系>/`，变成题目 md。

## 收进来的东西分两条路

  · **`.md`（Obsidian 插件转出来的）** → `vault.ingest_md()`：补 frontmatter、
    把图搬进 `附件/`、按 `---` 切成一题一段，入库后在 `题库/` 下写出标准模板。
    纯本地解析，**不花 AI 额度**，几毫秒就跑完。
  · **PDF / Word / 截图** → 拾课自己的整页识别（`auto_import`）→ 识别成题目再导出 md。

老师选定的转换方式是「在 Obsidian 里用插件把 PDF 变成 md」。插件路线的好处是
老师全程不离开 Obsidian、也不用等 AI；代价是插件转出来的东西不可靠（公式、图、
双栏都会走样），所以拾课这边**完全不挑**：能吃就吃，吃不下的明说哪里不行
（比如整卷没有分隔线，就不入库、把原因写进报告）。

PDF/截图那条路留着：插件转不动的卷子，直接丢 PDF 进来一样出题。

## 体系按**子目录**认，不猜

    vault/收件箱/DSE/2024-P1.pdf    → 按 DSE 体系识别
    vault/收件箱/2024-P1.pdf        → **不处理**，报告里提示挪进子文件夹

为什么不给个「默认体系」：一份卷子一套体系，猜错的代价是整批知识点挂到错误的树上，
而那种错不会报错，只在出卷时发现「知识点全对不上」（同 routers/auto.py 的判断）。

**md 是例外，不要求子目录**：它的知识点来自 frontmatter 里的 `knowledge:`，
体系对解析没用。硬要求放子目录只是多一道仪式，还会让人以为「放错地方就不认」。

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
# 插件转出来的 md 走另一条路（本地解析，不识别）。
MD_EXT = {".md", ".markdown"}
POLL_SECONDS = 20
# 同时最多跑几个识别任务。识别是整页视觉模型（一页几秒到几十秒），
# 一次丢十个 PDF 进去全开，机器和 API 额度都会炸。
MAX_ACTIVE = 2
# queued 也算「在跑」？**不算**：真正的在跑是 recognizing/fixing/approving。
# 但 queued 说明已经排队了，占一个名额 —— 否则同一个文件会被开两次任务。
ACTIVE = {"queued", "recognizing", "fixing", "approving"}

# ⚠️ 状态文件是「读 → 改 → 写」，后台轮询线程和 HTTP 请求**会同时进来**。
#    不加锁的经典后果不是丢计数：网页侧刚登记好，轮询线程用几秒前读到的旧内容写回去，
#    登记就消失了 → 下一轮看到文件还在收件箱 → 再开一次任务 → **同一份卷子入两遍库**。
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
    ok = EXTS | MD_EXT
    for p in box.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in ok:
            continue
        parts = p.relative_to(box).parts
        if parts and parts[0] in (DONE_DIR, FAIL_DIR):
            continue
        out.append(p)
    return sorted(out)


def pending_split() -> tuple[list[Path], list[Path]]:
    """待处理的文件分成 (卷子/截图, md)。两条路走的东西不一样，计数也得分开。"""
    docs: list[Path] = []
    mds: list[Path] = []
    for p in pending_files():
        (mds if p.suffix.lower() in MD_EXT else docs).append(p)
    return docs, mds


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


def scan_once(db: Session, *, enqueue: bool = True) -> dict:
    """跑一轮：先收尾已经跑完的任务，再给新文件开任务。

    顺序不能反 —— 先开新的会让「同时最多 2 个」这个上限失效。
    """
    with _lock:
        return _scan_locked(db, enqueue=enqueue)


def _scan_locked(db: Session, *, enqueue: bool = True) -> dict:
    vault.ensure_dirs()
    state = _load()
    report: dict = {"started": [], "finished": [], "failed": [], "ignored": [],
                    "waiting": [], "ingested": []}

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
            src = vault.root() / key
            moved = ""
            if src.is_file():
                try:
                    moved = vault.rel(_move(src, DONE_DIR))
                except OSError as exc:
                    _log.warning("收件箱：挪走 %s 失败：%s", src, exc)
            report["finished"].append({"file": key, "task_id": task.id, "status": task.status,
                                       "questions": len(ids), "moved_to": moved,
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
        key = vault.rel(path)
        if key in state["tasks"]:
            continue
        # md 走本地摄取：不识别、不花 AI 额度、也不占识别名额。
        if path.suffix.lower() in MD_EXT:
            report["ingested"].append(_ingest_one(db, path, key, dry_run=not enqueue))
            continue
        if active >= MAX_ACTIVE:
            report["waiting"].append({"file": key, "task_id": None, "status": "排队"})
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

    _save(state)
    return report


def _ingest_one(db: Session, path: Path, key: str, *, dry_run: bool = False) -> dict:
    """收件箱里的一份 md → 题目（或者一条「得先拆开」的说明）。

    源文件怎么处置：
      · 进去了题 → `_已处理/`。留在收件箱会被下一轮再吃一遍 —— 同一批题入两遍库。
      · 一道都没进去 → `_失败/`。老师加好分隔线再拖回收件箱，就能重新处理。
    """
    try:
        if vault.is_question_md(vault.read_md(path).text):
            # 已经是标准模板（frontmatter 里有 shike: question）。
            # ⚠️ 这种文件**不能**走 ingest：它会被「同步」和「摄取」各入一遍库 → 一道题变两道。
            #    交给 scan，这里只负责把它从收件箱挪到 题库/。
            return _file_to_shelf(path, key, dry_run=dry_run)
        res = vault.ingest_md(db, path, dry_run=dry_run)
    except Exception as exc:                           # noqa: BLE001
        db.rollback()
        _log.warning("收件箱：摄取 %s 失败：%s", key, exc)
        moved = ""
        if not dry_run and path.is_file():
            try:
                moved = vault.rel(_move(path, FAIL_DIR))
            except OSError:
                pass
        return {"file": key, "error": str(exc), "moved_to": moved,
                "units": 0, "created": [], "skipped": [], "warnings": [],
                "needs_split": False}
    if dry_run:
        return {"file": key, "dry": True, **res}
    moved = ""
    if path.is_file():
        try:
            moved = vault.rel(_move(path, DONE_DIR if res["created"] else FAIL_DIR))
        except OSError as exc:
            _log.warning("收件箱：挪走 %s 失败：%s", path, exc)
    return {"file": key, "moved_to": moved, **res}


def _file_to_shelf(path: Path, key: str, *, dry_run: bool = False) -> dict:
    """把收件箱里一份**已是标准模板**的 md 挪进 `题库/`（重名加 -2），不解析不入库。

    入库交给「同步」（`vault.scan`），这样「怎么入库」只有一条路。

    ⚠️ **同一道题不许在题库里出现两份**。重名就加 `-2` 本来是为了不覆盖别人的文件，
    但文件名不是身份 —— `shike_id` 才是。老师把一份已经在题库里的题又拖进收件箱
    （或者从题库复制一份过来），加个 `-2` 搬进去就成了「同一道题两个文件」，
    之后**每次同步都有一条失败**（「这个 id 已经对应另一个文件」），而老师完全
    不知道那份多出来的是自己几时拖进来的。所以按 id 撞上了就不搬。
    """
    empty: dict = {"file": key, "path": vault.rel(path), "units": 0, "created": [],
                   "skipped": [], "warnings": [], "needs_split": False,
                   "note": "已经是标准题目模板，挪进 题库/ 交给同步"}
    if dry_run:
        return {**empty, "dry": True, "moved_to": ""}

    twin = vault.file_of_qid(vault.qid_of(path), exclude=path)
    if twin is not None:
        moved = ""
        try:
            moved = vault.rel(_move(path, DONE_DIR))
        except OSError as exc:
            _log.warning("收件箱：把重复的 %s 挪进 %s 失败：%s", key, DONE_DIR, exc)
        return {**empty, "moved_to": moved,
                "note": f"题库里已经有这道题（{vault.rel(twin)}），这份是副本，没再入一份"}

    dest = vault.shelf_dir() / path.name
    n = 1
    while dest.exists():
        n += 1
        dest = vault.shelf_dir() / f"{path.stem}-{n}{path.suffix}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
    except OSError as exc:
        _log.warning("收件箱：把 %s 挪进题库失败：%s", key, exc)
        return {**empty, "moved_to": "", "error": str(exc)}
    return {**empty, "moved_to": vault.rel(dest)}


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
