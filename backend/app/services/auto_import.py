# -*- coding: utf-8 -*-
"""讲义 → 题库：**一条命令跑完全程**（录入 → 检查 → 问题处理 → 无误入库）。

## 这个模块存在的理由

之前要做完一份讲义，得在界面上点七八步：上传 → 选页 → 选体系 → 建任务 →
等进度 → 逐条看配图 → 框歪的重框 → 通过 → 再下一条。自动化只做到「识别」为止，
剩下全是人工。

用户 2026-10-23 的诉求：「弄一个方便 agent 调用的接口，后续只需要把讲义传给 agent，
就能自动调用接口录入 → 检查 → 问题处理 → 无误入库」。

所以这里的定位是**编排层**：它不加任何新的识别/裁剪能力，只是把已有的四段能力
串成一个幂等的后台任务，并把「哪里出了问题、该怎么下一步」写成机器可读的报告。

## 四段与各自的唯一出处

    ① 录入   services/batch_page.create_page_job + run_page_job（识别）
    ② 检查   services/batch_page.plan_page_figures（配图分配，识别时已算好）
    ③ 问题处理 routers/batches.crop_item_figure（**唯一**的配图写入路径）+ pick_candidate
    ④ 入库   services/batch_import.approve_item（**唯一**的入库路径）

⚠️ ③④ 一律**调用**既有函数，不在这里重写一份。重写的后果不是报错，是漂：
   比如「旧图按引用计数回收」「整题兜底图连带撤掉」这些副作用，漏一处就静默丢图。

## 严格口径（最重要的一条）

自动入库只收**零疑点**的条目：

    · 内容非空、没有 bad_json / parse_failed / empty_stem
    · 配图状态确定：有图且无怀疑标记 / 明确不需要图 / 有整题兜底图

其余一律留在 `needs_human`，附带一句人话原因和一个直达链接。
**拿不准就不入库** —— 错题会一路印到发给学生的卷子上；
而「少入库一条」老师看报告就知道是哪条、为什么。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import SessionLocal
from ..models import AutoTask, BatchItem, BatchJob, Curriculum
from . import batch_import, batch_page


# ================================================================ 判据（纯函数）
# 「这条内容本身坏了」—— 不是配图问题，自动流程修不了，只能老师补。
# 和配图无关，所以单独一组：混进 DOUBT_FIGURE_FLAGS 会让「重框一下图」变成万能药。
FATAL_FLAGS = (
    "bad_json",        # 模型返回的不是合法 JSON，这一页的题是占位
    "parse_failed",    # 解析失败
    "empty_stem",      # 没识别出题干
    "page_error",      # 这一页整体识别失败
)

# 一句话解释「为什么这条要人看」。key 与 judge_iter 返回的 reason 一一对应。
_REASON_TEXT = {
    "no_content": "模型没给出题干内容",
    "bad_json": "这一页模型返回的内容解析不出来（整页要重跑）",
    "parse_failed": "这一页识别失败",
    "empty_stem": "没识别出题干",
    "page_error": "这一页整体识别失败，需要重跑",
    "figure_ambiguous": "要配图，但这页有几处图形，判不出哪一处是本题的",
    "figure_none": "要配图，但这页找不到任何图形",
    "figure_repair_failed": "配图自动重裁试过了，还是没修好",
    "figure_doubt": "配图有疑点（框歪了 / 和别的题抢同一处图形）",
}

# 「拿不准但也不算出错」的标记：放它入库，但报告里点名，让老师知道这批弱一点。
_SOFT_FLAGS = ("figure_only_candidate", "figure_rough")


def _as_list(raw) -> list:
    """flags / pages 这类列可能是 JSON 字符串，也可能已经是 list。"""
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return v if isinstance(v, list) else []


def judge(content: str, flags, needs_figure: bool, figure_image: str) -> tuple[str, str]:
    """一条待审条目 → `(verdict, reason)`。**纯函数**，不碰库。

    verdict：
      pass   零疑点，直接入库
      repair 配图有疑点 / 没有图 → 先试自动重裁，修好再入库
      human  内容坏了或修不好 → 留给人
      skip   内容为空，**连人都不必看**（模型这页确实没读出题）

    ⚠️ 判据顺序不能换。最典型的坑：整题兜底的条目**同时**带 `figure_box_missing`
       （它就是「配图定位不到」才兜底的），若先判怀疑标记，兜底永远不会被放行。
    """
    fl = set(_as_list(flags))

    if not (content or "").strip():
        return "skip", "no_content"
    for f in FATAL_FLAGS:
        if f in fl:
            return "skip", f

    # 整题兜底：这条的图就是**整道题**（题干+图+选项），出卷按图片印。
    # 它必然带着配图相关的怀疑标记，先判它。
    if "figure_whole_question" in fl:
        return "pass", "whole_question"

    doubtful = [f for f in batch_page.DOUBT_FIGURE_FLAGS if f in fl]

    if figure_image:
        if doubtful:
            return "repair", "figure_doubt"
        return "pass", "figure_ok"
    if needs_figure:
        return "repair", "figure_missing"
    if doubtful:
        # 模型说不需要图、却有配图相关标记 —— 两边打架，让它先去修一次
        return "repair", "figure_doubt"
    return "pass", "no_figure_needed"


def _area(b) -> float:
    return max(0.0, float(b[2]) - float(b[0])) * max(0.0, float(b[3]) - float(b[1]))


def _contains(outer, inner, tol: float = 8.0) -> bool:
    """`inner` 是否整个落在 `outer` 里（归一化框，容 8/1000 的边）。"""
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def pick_candidate(current, cands, prefer_page: int | None = None) -> dict | None:
    """从候选里挑**唯一说得通**的那一处。挑不出就给 None。**纯函数**。

    为什么这么保守：配错了图会印到卷子上发给学生；而少一张图老师看报告就知道。
    两者代价不对等，所以默认答案是「不配」。

    判据（有原框时）：
      ① 原框被某个候选**包住**、且候选面积 ≤ 原框 3 倍 → 那是「把框修准了」，取面积最小的
      ② 和某个候选 IoU ≥ 0.6 → 同一处，取它
      ③ 都不搭 → None（原框和候选全不是一回事，说明是模型看错了位置，不猜）

    没有原框时（模型没给框 / 跨页题继承下一页）：只有候选**恰好一处**才敢选。

    `prefer_page`：跨页题会把下一页的候选也列出来。本页有候选就只在本页挑 ——
    否则「本页 1 处 + 下一页 1 处」会被当成两处而放弃，其实本页那处就是答案。
    """
    cands = [c for c in (cands or []) if c.get("box")]
    if prefer_page is not None:
        same = [c for c in cands if c.get("page") == prefer_page]
        if same:
            cands = same
    if not cands:
        return None

    if current:
        cur = [float(x) for x in current]
        inside = [c for c in cands
                  if _contains(c["box"], cur) and _area(c["box"]) <= max(_area(cur), 1.0) * 3.0]
        if inside:
            return min(inside, key=lambda c: (_area(c["box"]), c["page"]))
        near = [c for c in cands if _iou(c["box"], cur) >= 0.6]
        if len(near) == 1:
            return near[0]
        return None

    if len(cands) == 1:
        return cands[0]
    # 多处候选里，若**只有一条是「整排」**而别处都是零散单幅 —— 仍可能是别的题的图，
    # 所以也不替老师决定。这里只留一个例外：整排之外再无候选。
    return None


def plan_batches(pages, limit: int) -> list[list[int]]:
    """把页号按 `limit` 切片（`limit` = 一次识别的最大页数）。**纯函数**。

    为什么要切：一次识别最多 MAX_PAGES 页（=多少次 AI 请求）。44 页的讲义必须拆两批。
    拆批是编排层的活 —— agent 不该自己算。
    """
    ps = [int(p) for p in (pages or [])]
    limit = max(1, int(limit))
    return [ps[i:i + limit] for i in range(0, len(ps), limit)]


def parse_pages(spec, total: int) -> list[int]:
    """把页范围写法解析成有序页号列表。**纯函数**。

    接受：
        None / "" / "all"        → 1..total（全部）
        "1-30,35" / "1-30，35"   → [1..30, 35]（逗号中文也认，语音输入常带）
        "3"                      → [3]
        [1, 2, 3]                → 原样（去重排序）
        "1-999"                  → 不在这里报错，越界由调用方带着 total 提示

    为什么允许「全部」这个默认：用户的原话是「我只需要将讲义传给 agent」——
    不该逼他每份都算页数。要抽页才传 spec。
    """
    import re

    if spec is None or spec == "" or (isinstance(spec, str) and spec.strip().lower() == "all"):
        return list(range(1, max(0, int(total)) + 1))
    if isinstance(spec, (list, tuple)):
        return sorted({int(p) for p in spec if str(p).strip().isdigit() and int(p) > 0})

    out: set[int] = set()
    for part in re.split(r"[,，;；\s]+", str(spec).strip()):
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[-~—]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            lo, hi = min(a, b), max(a, b)
            # 上限兜一下：`1-99999` 这类手滑不该生成十万个页号
            out.update(range(lo, min(hi, lo + 3000) + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            raise ValueError("页范围写法认不出：%r（支持 1-30,35 或 all）" % part)
    return sorted(p for p in out if p > 0)


def build_report(task: AutoTask, rows: list[dict]) -> dict:
    """把逐条判定结果拼成**机器可读**的报告。**纯函数**。

    `rows` 每项：{item_id, seq, page_no, verdict, reason, note, job_id}

    报告要能被 agent 直接消费，所以：①计数和清单都有；②每条待处理项带
    `open`（人能点）+ `fix_with`（agent 能调）+ 一句 `message`（人话）。
    """
    jobs = _as_list(task.job_ids)
    counts = {"total": len(rows), "approved": 0, "needs_human": 0, "skipped": 0}
    for r in rows:
        if r["verdict"] == "pass":
            counts["approved"] += 1
        elif r["verdict"] == "skip":
            counts["skipped"] += 1
        else:
            counts["needs_human"] += 1

    reasons: dict[str, int] = {}
    for r in rows:
        if r["verdict"] in ("human", "repair"):
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1

    def _row(r: dict) -> dict:
        item_id = r["item_id"]
        return {
            "item_id": item_id,
            "seq": r.get("seq"),
            "page_no": r.get("page_no"),
            "reason": r["reason"],
            "message": r.get("note") or _REASON_TEXT.get(r["reason"], r["reason"]),
            # 人能点开的入口（审核页定位到这一批）
            "open": "/batch.html?job=%s" % r.get("job_id", ""),
            # agent 能调的下一步（具体坐标要它自己从 figure-candidates 里选）
            "fix_with": "POST /api/batches/items/%s/figure-crop" % item_id,
            "candidates": "GET /api/batches/items/%s/figure-candidates" % item_id,
        }

    todo = [_row(r) for r in rows if r["verdict"] in ("human", "repair")]
    skipped = [_row(r) for r in rows if r["verdict"] == "skip"]
    # 入库的题目 id：agent 拿到能直接引用（出卷、挂作业）；出错时也能**精确回滚**，
    # 不用靠「删掉今天所有题」这种危险动作。
    approved_ids = [r["question_id"] for r in rows if r.get("question_id")]

    if not todo:
        nxt = "全部处理完，%d 条已入库。" % counts["approved"]
    else:
        nxt = ("%d 条需要处理：打开 /batch.html?job=%s 逐条看；处理完再调 "
               "POST /api/auto/imports/%s/resume 收尾入库。"
               % (len(todo), jobs[0] if jobs else "", task.id))

    return {
        "task_id": task.id,
        "status": task.status,
        "src_filename": task.src_filename,
        "document_id": task.document_id,
        "jobs": jobs,
        "pages": _as_list(task.pages),
        "counts": counts,
        "reasons": reasons,
        "needs_human": todo,
        "skipped": skipped,
        "approved_question_ids": approved_ids,
        "next": nxt,
    }


# ================================================================ 建任务 / 起线程
def start(db: Session, *, document_id: str, pages: list[int],
          curriculum_id: int, src_filename: str | None = None) -> AutoTask:
    """建一个自动导入任务并**立刻**起后台线程跑。返回任务行（此时还没开跑）。

    ⚠️ 这里只做「校验 + 落库 + 起线程」，真正的活全在 `run_task`。
       理由是整份讲义要跑十几到几十分钟，不能挂在一次 HTTP 请求里。
    """
    pages = sorted({int(p) for p in (pages or []) if int(p) > 0})
    if not pages:
        raise ValueError("没有要处理的页面")
    task = AutoTask(
        id=uuid.uuid4().hex[:12],
        owner_id=config.OWNER_ID,
        status="queued",
        stage="排队中",
        src_filename=src_filename,
        document_id=document_id,
        curriculum_id=curriculum_id,
        job_ids="[]",
        pages=json.dumps(pages),
        total_pages=len(pages),
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    db.add(task)
    db.commit()
    threading.Thread(target=run_task, args=(task.id,), daemon=True).start()
    return task


def _touch(task_id: str, **fields) -> None:
    """改任务状态。**每次开新会话** —— 后台线程不能复用请求会话。"""
    db = SessionLocal()
    try:
        t = db.get(AutoTask, task_id)
        if t is None:
            return
        for k, v in fields.items():
            setattr(t, k, v)
        db.commit()
    finally:
        db.close()


# ================================================================ 主流程
def run_task(task_id: str) -> None:
    """后台线程入口：识别 → 修图 → 入库 → 报告。

    每一步都是**幂等**的：中途崩了重跑一次不会写出第二份题库条目
    （入库前会跳过已 approved 的条目，见 `_pass_and_approve`）。
    """
    db = SessionLocal()
    try:
        task = db.get(AutoTask, task_id)
        if task is None:
            return
        pages = _as_list(task.pages)
        doc_id = task.document_id or ""
        curriculum_id = task.curriculum_id
        src_name = task.src_filename or ""
    finally:
        db.close()

    try:
        # ---- ① 录入：拆批 → 逐批识别（批间串行，批内由 run_page_job 并行）----
        batches = plan_batches(pages, batch_page.MAX_PAGES)
        job_ids: list[str] = []
        for i, chunk in enumerate(batches, 1):
            _touch(task_id, status="recognizing",
                   stage="正在识别第 %d/%d 批（共 %d 页）"
                         % (i, len(batches), len(pages)))
            db = SessionLocal()
            try:
                job = batch_page.create_page_job(
                    db, chunk, curriculum_id=curriculum_id,
                    document_id=doc_id, doc_filename=src_name)
                job_id = job.id
            finally:
                db.close()
            job_ids.append(job_id)
            _touch(task_id, job_ids=json.dumps(job_ids))
            # run_page_job 是**同步**的（内部 ThreadPoolExecutor 跑完才返回），
            # 所以这里直接调用就等于「等这批跑完」。
            batch_page.run_page_job(job_id)

        # ---- ②③ 检查 + 问题处理 ----
        _touch(task_id, status="fixing", stage="正在检查识别结果、自动修配图")
        rows = _judge_all(db, task_id, job_ids)

        # ---- ④ 入库 ----
        _touch(task_id, status="approving",
               stage="正在入库（%d 条零疑点）"
                     % sum(1 for r in rows if r["verdict"] == "pass"))
        rows = _pass_and_approve(db, task_id, rows)

        # ---- 报告 ----
        db = SessionLocal()
        try:
            task = db.get(AutoTask, task_id)
            report = build_report(task, rows)
            failed = _failed_pages(db, job_ids)
            report["failed_pages"] = failed
            task.status = "partial" if failed else "done"
            task.stage = ("完成，%d 条有页失败" % len(failed)) if failed else "完成"
            task.total_items = len(rows)
            task.approved = report["counts"]["approved"]
            task.needs_human = report["counts"]["needs_human"]
            task.skipped = report["counts"]["skipped"]
            task.report = json.dumps(report, ensure_ascii=False)
            task.finished_at = datetime.now().isoformat(timespec="seconds")
            db.commit()
        finally:
            db.close()

    except Exception as e:                                   # noqa: BLE001
        # 整体失败才落到这里。单页失败是正常情况（在校验计数里），不抛。
        _touch(task_id, status="failed", stage="失败",
               error="%s: %s" % (type(e).__name__, str(e)[:400]),
               finished_at=datetime.now().isoformat(timespec="seconds"))


def _failed_pages(db: Session, job_ids: list[str]) -> list[dict]:
    """哪些页没识别成（job.failed 是页数，不是条目数，所以要说清是哪几页）。"""
    out = []
    for jid in job_ids:
        job = db.get(BatchJob, jid)
        if job is None:
            continue
        if job.error and not (job.done or 0):
            out.append({"job_id": jid, "error": job.error[:300]})
    return out


def _judge_all(db: Session, task_id: str, job_ids: list[str]) -> list[dict]:
    """逐条判 + 对 `repair` 的尝试自动修图。返回逐条判定行（不含入库）。"""
    rows: list[dict] = []
    items = db.scalars(
        select(BatchItem).where(BatchItem.job_id.in_(job_ids)).order_by(BatchItem.seq)
    ).all()
    for it in items:
        if it.status == "approved":
            # 已经入库过（resume 重跑时会遇到）→ 算 pass，别重复入库。
            # ⚠️ 必须把**原来的** question_id 带上：不带的话 resume 后的报告里
            #    approved_question_ids 只剩「这一轮新入库的」那几条，
            #    agent 会以为之前那些没成功（明明是它先入库的）。
            rows.append({"item_id": it.id, "seq": it.seq, "page_no": it.page_no,
                         "verdict": "pass", "reason": "already", "job_id": it.job_id,
                         "question_id": it.question_id or ""})
            continue

        verdict, reason = judge(it.content, it.flags, bool(it.needs_figure),
                                it.figure_image or "")
        note = ""
        if verdict == "repair":
            ok, note = _repair_figure(db, it)
            if ok:
                db.refresh(it)
                verdict, reason = judge(it.content, it.flags, bool(it.needs_figure),
                                        it.figure_image or "")
                if verdict == "repair":
                    # 修完还是「有疑点」→ 别骗自己，交给老师
                    verdict, reason = "human", "figure_repair_failed"
            else:
                verdict, reason = "human", "figure_repair_failed"

        rows.append({"item_id": it.id, "seq": it.seq, "page_no": it.page_no,
                     "verdict": verdict, "reason": reason, "note": note,
                     "job_id": it.job_id})
    return rows


def _repair_figure(db: Session, it: BatchItem) -> tuple[bool, str]:
    """试着自动把配图修对。返回 `(修好了没, 一句话说明)`。

    ⚠️ 三个环节都走既有实现，**不另写**：
       · 候选几何 → routers/batches.item_figure_candidates（含「整排」与跨页候选）
       · 写入     → routers/batches.crop_item_figure（唯一写入路径，带旧图回收）
       · 挑选     → 本模块 pick_candidate（纯函数，可单测）
       延迟导入是因为 routers 已经 import 了 services，顶层 import 会循环。
    """
    from ..routers.batches import crop_item_figure, item_figure_candidates
    from ..schemas import BatchRegionIn, FigureCropIn
    from fastapi import HTTPException

    try:
        meta = item_figure_candidates(it.id, db)
    except HTTPException as e:
        return False, "读不到这页的图形几何：%s" % e.detail
    except Exception as e:                                   # noqa: BLE001
        return False, "读不到这页的图形几何：%s" % e

    cands = meta.get("candidates") or []
    if not cands:
        return False, "这一页一处图形候选都没有"
    cur = _as_list(it.figure_box) if it.figure_box else None
    pick = pick_candidate(cur, cands, prefer_page=it.page_no)
    if pick is None:
        return False, ("这页有 %d 处图形候选，挑不出唯一说得通的那一处" % len(cands))

    # 候选框是归一化 0~1000，而 crop_item_figure 要的是**点坐标**。
    # ⚠️ 用**候选自己的** width/height，不是本页的 —— 跨页候选来自下一页。
    b = pick["box"]
    pw, ph = float(pick["width"]), float(pick["height"])
    region = BatchRegionIn(
        page=int(pick["page"]),
        x0=b[0] / 1000.0 * pw, y0=b[1] / 1000.0 * ph,
        x1=b[2] / 1000.0 * pw, y1=b[3] / 1000.0 * ph,
    )
    try:
        crop_item_figure(it.id, FigureCropIn(regions=[region], gap=0), db)
    except HTTPException as e:
        return False, "重裁失败：%s" % e.detail
    except Exception as e:                                   # noqa: BLE001
        return False, "重裁失败：%s" % e
    return True, "已按候选重裁（第 %d 页 %s）" % (pick["page"], b)


def _pass_and_approve(db: Session, task_id: str, rows: list[dict]) -> list[dict]:
    """把 `pass` 的条目写进题库；其余原样留着。

    ⚠️ 入库走 `batch_import.approve_item`（唯一路径）。这里**只做筛选**，
       不自己拼 Question 行 —— 那样「整题兜底要 render_prefer=image」
       这种规则会漏，而漏了不报错，只是出卷时图没了。
    """
    for r in rows:
        if r["verdict"] != "pass":
            continue
        it = db.get(BatchItem, r["item_id"])
        if it is None:
            r["verdict"], r["reason"] = "human", "item_missing"
            continue
        if it.status == "approved":
            continue
        try:
            r["question_id"] = batch_import.approve_item(db, it)
        except Exception as e:                               # noqa: BLE001
            db.rollback()
            r["verdict"] = "human"
            r["reason"] = "approve_failed"
            r["note"] = "入库失败：%s" % str(e)[:200]
    return rows


# ================================================================ 收尾（人工处理之后）
def resume(task_id: str) -> dict:
    """人工把剩下的处理完 → **再跑一次判定与入库**。

    与 `run_task` 的区别：不重新识别（那些 AI 调用已经花过了），
    只重扫这个任务的所有条目，把现在能过的入库。老师框好图后调它收尾。
    """
    db = SessionLocal()
    try:
        task = db.get(AutoTask, task_id)
        if task is None:
            raise ValueError("任务不存在")
        job_ids = _as_list(task.job_ids)
        if not job_ids:
            raise ValueError("这个任务还没有识别结果，没什么可收的")
        task.status = "fixing"
        task.stage = "重新判定待审条目"
        db.commit()
        rows = _judge_all(db, task_id, job_ids)
        rows = _pass_and_approve(db, task_id, rows)
        task = db.get(AutoTask, task_id)
        report = build_report(task, rows)
        task.status = "done"
        task.stage = "完成"
        task.total_items = len(rows)
        task.approved = report["counts"]["approved"]
        task.needs_human = report["counts"]["needs_human"]
        task.skipped = report["counts"]["skipped"]
        task.report = json.dumps(report, ensure_ascii=False)
        task.finished_at = datetime.now().isoformat(timespec="seconds")
        db.commit()
        return report
    finally:
        db.close()
