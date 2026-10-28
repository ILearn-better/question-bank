# -*- coding: utf-8 -*-
r"""R6：agent 拿到的「退出码 / 报告」必须说实话。

这个文件守的是**同一件事的三个出口**，它们现在的共同毛病是「静默地说没事」：

  ① 退出码：`auto_import_cli` 里同一套判断散在三处（`:194 / :201 / :232`），
     其中一处连 `status == "failed"` 都不看。抽成 `exit_code_of(task) -> int` 之后，
     四档语义必须钉死：0 = 全好了 / 1 = 一条没成 / 2 = 有要人看的 / 3 = 有页没识别成。
     ⚠️ 最容易漏的是「跑完了、但**一条都没入库**」（30 页全 `no_content`）——
     页没失败、`failed_pages` 是空的，任何靠页失败的判据都抓不住它，只能靠 approved==0。
  ② 页失败清单：`_failed_pages` 只在 `job.error and not job.done` 时记录，
     而真实的部分失败（`done=28, failed=2`）是 **else 分支**，只写 status **不写 error**。
     于是「3 页没识别成」被报成「全部入库」，`status` 还是 `done`。
  ③ 报告里的 `status`：`run_task` 先建报告、后改状态，报告里那个 `approving`
     会一直往 agent 手里递，而任务其实早就 `partial` 了。

为什么不起服务：这几条都是纯函数 / 单表读写，直调就能覆盖。
AI 与页图都用桩替换，**不联网、不动真库**（临库在 `$TEMP`，import app.db **之前**设好）。

⚠️ 本文件的期望值来自**契约**，不是从实现反推的。实现和契约不一致 → 报红，不改期望。
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import sys
import tempfile
import tokenize
from pathlib import Path

# ---------------------------------------------------------------- ⓪ 临库（必须在 import app.db 之前）
TMP = Path(tempfile.gettempdir()) / "shike_auto_contract"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
os.environ["SHIKE_DB_PATH"] = str(TMP / "contract.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

BACKEND = Path(__file__).resolve().parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException                      # noqa: E402

from app import config                                 # noqa: E402
from app.db import SessionLocal, engine                # noqa: E402
from app.models import AutoTask, Base, BatchJob, Document   # noqa: E402
from app.routers import auto as A                      # noqa: E402
from app.services import auto_import as AI             # noqa: E402

assert str(config.DB_PATH) == str(TMP / "contract.db"), f"临时库没生效：{config.DB_PATH}"
assert str(config.DATA_DIR).startswith(str(TMP)), f"data 没落到临时区：{config.DATA_DIR}"

Base.metadata.create_all(engine)

fails: list[str] = []
total = 0


def check(label: str, got, want) -> None:
    global total
    total += 1
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}")
    if not okk:
        print(f"       得到 {got!r}")
        print(f"       期望 {want!r}")
        fails.append(label)


# ---------------------------------------------------------------- ① 退出码八档
print("==== ① exit_code_of(task) 四档语义（8 个 case） ====")

_EXIT_OK = True
_EXIT_ERR = ""
try:
    from auto_import_cli import exit_code_of as _exit_code_of
except Exception as e:                                  # noqa: BLE001
    _EXIT_OK = False
    _EXIT_ERR = f"{type(e).__name__}: {e}"
    _exit_code_of = None


def task_dict(done, status, approved=0, needs_human=0, failed_pages=None, jobs=None):
    """按 `routers/auto._task_dict()` 的形状造 task dict（agent 拿到的就是这个）。"""
    return {
        "done": done,
        "status": status,
        "counts": {"total": 50, "approved": approved, "needs_human": needs_human, "skipped": 0},
        "report": None if failed_pages is None else {"failed_pages": failed_pages},
        "jobs": jobs or [],
    }


CASES = [
    # (期望, 说明, task)
    (0, "全好了：done/done、无遗留、无页失败",
     task_dict(True, "done", 50, 0, [])),
    (2, "有 3 条要人看（needs_human）",
     task_dict(True, "done", 50, 3, [])),
    (3, "有页没识别成（report 里有 failed_pages）",
     task_dict(True, "partial", 50, 0, [{"job_id": "j", "failed": 2}])),
    (3, "report=None 且 failed_pages 拿不到 → 只能靠 jobs[].failed",
     task_dict(True, "partial", 50, 0, None, [{"id": "j", "failed": 2, "done": 28}])),
    (0, "还在跑（done=False）：不能判成失败",
     task_dict(False, "recognizing", 0, 0, None)),
    (1, "整批失败：status=failed",
     task_dict(True, "failed", 0, 0, None)),
    (1, "**跑完了但一条都没入库**（approved=0 且无要人看的）→ 必须 1，不能 0",
     task_dict(True, "partial", 0, 0, [{"job_id": "j", "failed": 1}])),
    (3, "页失败(3) 压过 needs_human(2)",
     task_dict(True, "partial", 50, 3, [{"job_id": "j", "failed": 2}])),
]

if not _EXIT_OK:
    # 取不到函数就 8 条全红，但**继续跑后面的段** —— 一次把红看全，别崩在 import 行
    for want, why, _ in CASES:
        total += 1
        print(f"  [FAIL] {why}")
        print(f"       auto_import_cli.exit_code_of 取不到（{_EXIT_ERR}）—— R6-① 还没落地")
        fails.append(why)
else:
    for want, why, t in CASES:
        check(why, _exit_code_of(t), want)

# ---------------------------------------------------------------- ② 页失败清单
print("\n==== ② _failed_pages：真实的部分失败形态（done>0, failed>0, error=None） ====")

db = SessionLocal()
stamp = os.urandom(4).hex()
MADE: list[str] = []
MADE_TASKS: list[str] = []
try:
    def mk_job(suffix, *, done, failed, error=None, pages=30):
        jid = f"r6_{stamp}_{suffix}"
        db.add(BatchJob(id=jid, status="partial", total=pages, done=done, failed=failed,
                        error=error, pages=json.dumps(list(range(1, pages + 1)))))
        db.commit()
        MADE.append(jid)
        return jid

    # A：真实形态 —— batch_page._finalize 的 else 分支只写 status，**不写 error**
    jid_a = mk_job("a", done=28, failed=2, error=None)
    got_a = AI._failed_pages(db, [jid_a])
    check("A（done=28/failed=2/error=None）要报 1 条", len(got_a), 1)
    check("A 带上失败页数 failed=2", got_a[0].get("failed") if got_a else None, 2)
    check("A 带上 pages_total=30", got_a[0].get("pages_total") if got_a else None, 30)
    # ↑ A 探针才是真实形态。B 只是为了证明「靠 error 也能过」并不等于修对了。
    #   只放宽 job.error 那半边，B 会绿、A 还是红。

    jid_b = mk_job("b", done=0, failed=30, error="整批识别都失败了：接口超时")
    check("B（整批失败，error 有值）也要报 1 条", len(AI._failed_pages(db, [jid_b])), 1)

    jid_c = mk_job("c", done=30, failed=0, error=None)
    check("C（全成，done=30/failed=0）不能报（对照组，别把正常批次也报出来）",
          len(AI._failed_pages(db, [jid_c])), 0)

    # ------------------------------------------------------------ ③ build_report
    print("\n==== ③ build_report：failed_pages 要进报告，next 不许说「处理完了」 ====")
    rows = [{"verdict": "pass"}]

    def mk_task(suffix, status):
        """真插一行 AutoTask，而不是 SimpleNamespace ——
        桩少一个属性就 AttributeError，而真实任务永远带全字段；用真行才叫「输入和真实产物一样」。"""
        tid = f"r6task_{stamp}_{suffix}"
        db.add(AutoTask(id=tid, status=status, stage="report", src_filename="x.pdf",
                        job_ids=json.dumps([jid_a]), pages=json.dumps([1, 2, 3]),
                        total_pages=30, total_items=1, approved=0, needs_human=0, skipped=0))
        db.commit()
        MADE_TASKS.append(tid)
        return db.get(AutoTask, tid)

    fp = [{"job_id": "x", "failed": 2, "done": 28, "pages_total": 30}]
    rep = AI.build_report(mk_task("main", "partial"), rows, failed_pages=fp)
    check("report 里带上 failed_pages（长度 1）", len(rep.get("failed_pages") or []), 1)
    nxt = str(rep.get("next") or "")
    check("next 里点了「有页没识别成」（含「没识别成」或「页」之一，不锁整句）",
          ("没识别成" in nxt) or ("页" in nxt), True)
    check("next 不许说「全部处理完」", "全部处理完" in nxt, False)

    rep2 = AI.build_report(mk_task("two", "done"), rows)   # 两参调用：别的地方还在这么用
    check("两参调用不崩，failed_pages 默认空数组", rep2.get("failed_pages"), [])
    # 0 是**合法值**，键必须在 —— 挡住以后有人把 `if pages_lost:` 写成 `pages_lost or None`
    check("两参调用 pages_lost == 0（键要在，别退化成 None）", rep2.get("pages_lost"), 0)

    # ------------------------------------------------------------ ③b pages_lost
    # ⚠️ **必须两批合并才验得出来**。单批时新旧算法结果相同：
    #    只给 J1(30/28/2) → 新 Σ(total-done)=2、旧 Σ(failed)=2 —— 一样，测不出。
    #    单给 J2(2/0/0)   → 新 2、旧 0 —— 能分，但那不是「两批同时出事」的真实形态。
    # 两批同时给才把两者拉开：新 2+2=4，旧 2+0=2。
    fp2 = [
        {"job_id": "J1", "done": 28, "failed": 2, "pages_total": 30, "error": ""},
        {"job_id": "J2", "done": 0, "failed": 0, "pages_total": 2,
         "error": "整批识别都失败了"},
    ]
    rep3 = AI.build_report(mk_task("lost", "partial"), rows, failed_pages=fp2)
    check("pages_lost = 4（Σ(pages_total-done)，不是 Σ(failed)=2）", rep3.get("pages_lost"), 4)
    check("next 里念的也是 4 页（页数只有一处出处）",
          "共 4 页" in str(rep3.get("next") or ""), True)

    # ------------------------------------------------------------ ⑤ 报告 status 快照
    # 行为断言（重的一票）：build_report 只是把 task.status 抄进报告，不做修正。
    # 所以调用方**必须**先定 status 再建报告 —— 下面用位置断言守这个顺序。
    check("build_report 的 status 是 task.status 的快照（不替调用方改成终值）",
          AI.build_report(mk_task("snap", "approving"), rows).get("status"), "approving")
finally:
    # 只删自己插的 id，绝不按条件批量删
    for jid in MADE:
        j = db.get(BatchJob, jid)
        if j is not None:
            db.delete(j)
    for tid in MADE_TASKS:
        t = db.get(AutoTask, tid)
        if t is not None:
            db.delete(t)
    db.commit()
    db.close()

# ---------------------------------------------------------------- ④ 页范围 422
print("\n==== ④ _precheck_pages：写法认不出 / 越界都必须 422，正常写法必须放行 ====")

db = SessionLocal()
DOC_ID = f"r6doc_{stamp}"
db.add(Document(id=DOC_ID, filename="fake.pdf", filetype=".pdf", block_count=0, blocks="[]",
                scanned=0, created_at="2026-10-27T00:00:00"))
db.commit()
try:
    doc = db.get(Document, DOC_ID)
    orig_src, orig_cnt = A._page_source, A.pdf_adapter.page_count
    A._page_source = lambda _db, _id: Path("C:/fake.pdf")     # 页图来源打桩（不真读文件）
    A.pdf_adapter.page_count = lambda _p: 10                  # 共 10 页
    try:
        try:
            pages = A._precheck_pages(db, doc, "1-3")
            check("正常写法 1-3 → [1,2,3]（正面对照，别把整条都改成 422）", pages, [1, 2, 3])
        except HTTPException as e:
            check("正常写法 1-3 不该抛", f"{e.status_code} {e.detail}", "[1, 2, 3]")

        for spec in ("三到五", "1-99"):
            code = None
            try:
                A._precheck_pages(db, doc, spec)
            except HTTPException as e:
                code = e.status_code
            check(f"页范围 {spec!r} → 422", code, 422)
    finally:
        A._page_source, A.pdf_adapter.page_count = orig_src, orig_cnt
finally:
    d = db.get(Document, DOC_ID)
    if d is not None:
        db.delete(d)
        db.commit()
    db.close()


# ---------------------------------------------------------------- ⑤ 顺序断言（源码）
print("\n==== ⑤ 报告里的 status 必须是终值：先定 status、再建报告 ====")

SRC_AI = (BACKEND / "app" / "services" / "auto_import.py").read_text(encoding="utf-8")


def py_tokens(src: str, keep_strings: bool = False) -> str:
    """剥掉注释（可选连字符串一起剥）后拼成的 token 串 —— 顺序就是代码顺序。

    为什么要剥：注释里解释这个坑的那句话本身会把断言弄红（项目里踩过三次）。
    """
    drop = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
            tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}
    if not keep_strings:
        drop.add(tokenize.STRING)
    out = []
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type in drop:
            continue
        out.append(t.string)
    return " ".join(out)


def fn_src(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


for fname in ("run_task", "resume"):
    seg = py_tokens(fn_src(SRC_AI, fname))
    if not seg:
        check(f"{fname}：找不到函数体", "找不到", "有函数体")
        continue
    m_status = re.search(r"task\s*\.\s*status\s*=", seg)
    m_report = re.search(r"build_report\s*\(", seg)
    check(f"{fname}：有 task.status 赋值", bool(m_status), True)
    check(f"{fname}：有 build_report( 调用", bool(m_report), True)
    if m_status and m_report:
        # 两种正确实现都过：先定后建 / 建完回填（回填那行位置更靠后）。
        # 挡住的是「两个都没有」—— 那正是原 bug（报告里一直递 approving）。
        okk = (m_report.start() > m_status.start()) or ("report [ \"status\" ] =" in seg)
        check(f"{fname}：build_report 的位置晚于 task.status 赋值（或建完再回填）", okk, True)

# 反面：resume 不许再「无条件」把**终态**写成某个字面量。
# 注意 1：契约里的写法是 assertNotIn('task.status = "done"', body)，那对**引号/空格**极敏感
#         （写成单引号或换行就假绿）。这里换成「整行就是一句字面量赋值」的正则，语义相同但稳。
# 注意 2：只挡终态 done/partial，**不挡中间态**。`task.status = "fixing"` 是 resume
#         一开始无条件写的「我正在补配图」，它必须无条件 —— 把 transient 也算进来
#         会把正确实现判红（第一版就是这么红的）。
body_resume = fn_src(SRC_AI, "resume")
body_resume_nc = "\n".join(ln for ln in body_resume.split("\n")
                           if not ln.lstrip().startswith("#"))
uncond = re.findall(r"^\s*task\.status\s*=\s*[\"'](?:done|partial)[\"']\s*$", body_resume_nc, re.M)
check("resume 里没有「无条件 task.status = 'done'/'partial'」（终态必须按实际结果定）",
      uncond, [])

# ---------------------------------------------------------------- ⑦ 人能看见的那几行
print("\n==== ⑦ _print_report：退出码 3 只有脚本看得见，人看的是这几行 ====")

_CLI_OK = True
_CLI_ERR = ""
try:
    import auto_import_cli as CLI
except Exception as e:                                  # noqa: BLE001
    _CLI_OK = False
    _CLI_ERR = f"{type(e).__name__}: {e}"
    CLI = None

PRINT_DICT = {
    "id": "r6print", "status": "partial", "src_filename": "x.pdf",
    "curriculum": {"name": "高考"},
    "total_pages": 30, "progress": {"pages_done": 28, "pages_total": 30},
    "counts": {"total": 1, "approved": 0, "needs_human": 0, "skipped": 0},
    "report": {
        "failed_pages": [
            {"job_id": "J1", "done": 28, "failed": 2, "pages_total": 30, "error": ""},
            {"job_id": "J2", "done": 0, "failed": 0, "pages_total": 2,
             "error": "整批识别都失败了"},
        ],
        "pages_lost": 4,
    },
    # 顶层 error 有值但 status 已不是 failed 的样子：resume 不清历史错误，正是这种形态
    "error": "整批识别都失败了",
}


def printed(d):
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        CLI._print_report(d, 0)
    return buf.getvalue()


if not _CLI_OK:
    for why in ("历史 error 标注", "不许印裸的「错误：」", "页失败块", "页数", "failed 对照 ×2"):
        total += 1
        print(f"  [FAIL] {why}")
        print(f"       auto_import_cli 导不进来（{_CLI_ERR}）")
        fails.append(why)
else:
    out = printed(PRINT_DICT)
    # 只断言**关键子串在不在**，不锁整句（换个措辞不该红）
    check("状态已恢复时，历史 error 要标成「之前的错误（本轮已重跑）」",
          "之前的错误（本轮已重跑）" in out, True)
    check("不许印成裸的「错误：」（老师会读成本轮失败）", "\n错误：" in out, False)
    check("页失败要印「有 2 批没识别成」", "有 2 批没识别成" in out, True)
    check("摘要里带 pages_lost 的数（（共 4 页）这一格）", "（共 4 页）" in out, True)

    # 正面对照：真的是 failed 时，必须印「错误：」，不许永远打「之前的」
    out_f = printed({**PRINT_DICT, "status": "failed"})
    check("status=failed 时印「错误：」（对照组）", "\n错误：" in out_f, True)
    check("status=failed 时不印「之前的错误」", "之前的错误" in out_f, False)

    # 旧任务（这一轮之前的报告）：有 failed_pages、**没有 pages_lost 键**。
    # 回退去 Σ failed 只能数到「页跑过但挂了」那部分，整批没跑成的批贡献 0 ——
    # 印出来是个**已知偏小**的数，等于把少报的量当事实说。所以必须说「页数不详」。
    no_key = {**PRINT_DICT, "report": {"failed_pages": PRINT_DICT["report"]["failed_pages"]}}
    out_old = printed(no_key)
    check("旧任务缺 pages_lost → 印「页数不详」", "页数不详" in out_old, True)
    # 断言里带括号是**故意的**：摘要那格是「（共 N 页）」，而下面每行批次明细是
    # 「完成 X 页 / 失败 Y 页 / 共 Z 页」—— 不带括号的话，「共 2 页」会被 J2 的明细行
    # 命中，断言就变成永真/永假。第一版就是这么假红了一次。
    check("旧任务不许印「（共 2 页）」（Σ failed 的已知偏小值）", "（共 2 页）" in out_old, False)

    # 0 是**合法值**：必须走「共 0 页」，绝不能因为真值判断落进「页数不详」那支。
    # 这跟断言层那条 `pages_lost == 0` 是同一个坑的两个面（`is None` vs `or`）。
    out_zero = printed({**PRINT_DICT, "report": {**PRINT_DICT["report"], "pages_lost": 0}})
    check("pages_lost=0 → 印「（共 0 页）」", "（共 0 页）" in out_zero, True)
    check("pages_lost=0 不许落进「页数不详」", "页数不详" in out_zero, False)

# ---------------------------------------------------------------- 结果
print("\n" + "=" * 60)
if fails:
    print(f"❌ {len(fails)} 项未通过 / 共 {total} 项：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print(f"✅ 全部通过（{total} 项）")
sys.exit(0)
