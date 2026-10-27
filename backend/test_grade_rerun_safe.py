# -*- coding: utf-8 -*-
"""重新批改的安全性（R2）：**不许先删旧结果**；已确认的批改重跑**必须先问过老师**。

这个脚本只管一件事的两面，但它是最贵的一件事 —— 老师一晚上的复核和批注：

  ① 重跑失败时，**上一轮的逐题结果与标注必须还在**。
     现状（`grade_store._run_grade`）一进门就
     `delete(HomeworkGradeItem where grade_id == gid)` + commit，然后才调 AI。
     那一页没批成就等于把上一次的判读连同挂在题上的批注一起抹掉，
     而且**不可回退**（没有历史版本，也没有回收站）。

  ② `status == "done"` 或 `reviewed_at` 非空时，重跑要一道**显式确认**的门，
     没确认就 409，并说清「会清掉逐题复核与批注」。
     现状没有任何门：前端那个「重新批改」按钮点一下就开跑。

为什么不起服务：这是几张表的纯粹状态迁移，直调 service 就能全覆盖 ——
比 uvicorn 快一个量级、不占端口，也不会「服务没起」假失败。
AI 那一层用桩替换（`_recognize_page` 抛异常 / 返回固定结果），**不花钱、不联网**。

⚠️ 临时库必须在 import app.db **之前**设好 —— engine 绑定在 import 时定死。
⚠️ 只写真库以外的东西：本脚本的清理只按本次创建的 id 锚定，绝不按条件批量删。
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_rerun_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.db 之前设
os.environ["SHIKE_DB_PATH"] = str(TMP / "rerun.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

from sqlalchemy import select                          # noqa: E402

from app import config                                 # noqa: E402
from app.db import SessionLocal, engine                # noqa: E402
from app.models import (                               # noqa: E402
    Base,
    HomeworkGradeItem,
    HomeworkGradeMark,
    Lesson,
    LessonFile,
    Student,
)
from app.schemas import GradeConfirmIn, GradeIn, GradeMarkIn   # noqa: E402
from app.services import grade_store as G               # noqa: E402

assert str(config.DB_PATH) == str(TMP / "rerun.db"), f"临时库没生效：{config.DB_PATH}"
assert str(config.UPLOAD_DIR).startswith(str(TMP)), f"归档没落到临时区：{config.UPLOAD_DIR}"

total = 0
fails: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    global total
    total += 1
    if ok:
        print(f"  [OK ] {name}")
    else:
        print(f"  [FAIL] {name}" + (f"   <- {detail}" if detail != "" else ""))
        fails.append(name)
    return bool(ok)


def _rel(p: Path) -> str:
    from app.services import storage
    return storage.rel(p)


def make_png(path: Path, w: int = 60, h: int = 80) -> None:
    """手写一张最小 PNG（不引第三方库）。批改只要求「能被适配层打开、算得出页数」。"""
    raw = b"".join(b"\x00" + bytes([240, 240, 240] * w) for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def item_payload(**kw) -> dict:
    base = {
        "q_no": "3", "qtype": "解答题", "stem": "解方程 $x^2-5x+6=0$",
        "student_answer": "由 $(x-2)(x-3)=0$ 得 $x=2$",
        "correct_answer": "$x=2$ 或 $x=3$",
        "verdict": "partial", "score": None, "full_score": None,
        "basis": "漏了一个根", "confidence": "high", "kp_name": "一元二次方程",
        "error_reason": "只写了一个根", "flags": [], "raw": {"q": 1},
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- 造数据
def mk_grade(db, tag: str):
    stu = Student(name=f"重批安全测试-{tag}")
    db.add(stu)
    db.flush()
    ls = Lesson(student_id=stu.id, start_at="2026-10-20T19:00", duration_min=60)
    db.add(ls)
    db.flush()
    p = config.UPLOAD_DIR / "students" / f"u{stu.id}" / "hw" / f"{tag}.png"
    make_png(p)
    lf = LessonFile(lesson_id=ls.id, name=f"{tag}.png", stored=_rel(p),
                    role="homework", kind="image", size_bytes=p.stat().st_size)
    db.add(lf)
    db.flush()
    g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[lf.id]))
    db.commit()
    return g


def run_round(gid: int, *, items=None, boom: str = "") -> None:
    """同步跑一轮（不开线程、不起服务）。`boom` 非空 = 让识别函数抛异常。"""
    orig_rec, orig_ready = G._recognize_page, G._ai_ready

    if boom:
        def fake(png, ctx):
            raise RuntimeError(boom)
    else:
        def fake(png, ctx):
            return (items or [], "")

    G._recognize_page = fake
    G._ai_ready = lambda: (True, "")
    try:
        G._run_grade(int(gid))
    finally:
        G._recognize_page = orig_rec
        G._ai_ready = orig_ready


def items_of(db, gid: int) -> list:
    return list(db.scalars(
        select(HomeworkGradeItem)
        .where(HomeworkGradeItem.grade_id == gid)
        .order_by(HomeworkGradeItem.seq)).all())


def marks_of(db, gid: int) -> list:
    return list(db.scalars(
        select(HomeworkGradeMark).where(HomeworkGradeMark.grade_id == gid)).all())


# ---------------------------------------------------------------- ① 失败不删旧结果
def t_failed_rerun_keeps_old() -> None:
    print("\n=== ① 新一轮失败 → 上一轮的逐题结果与标注必须还在 ===")
    db = SessionLocal()
    try:
        g = mk_grade(db, "preserve")
        gid = int(g.id)
        run_round(gid, items=[item_payload(q_no="1", verdict="correct", error_reason=""),
                              item_payload(q_no="2")])
        db.refresh(g)
        first = items_of(db, gid)
        check("第一轮真的批出了 2 道题（后面才有东西可丢）", len(first) == 2, len(first))
        ids_first = [i.id for i in first]

        m = G.add_mark(db, gid, GradeMarkIn(
            page_no=1, kind="check", geo=[100, 100, 200, 200], item_id=ids_first[0]))
        check("老师在题上画了一个标注（作为「要保住的东西」）", bool(m.id))
        mark_ids = [x.id for x in marks_of(db, gid)]

        # —— 第二轮：AI 全线失败 ——
        run_round(gid, boom="模拟：模型接口 502")

        after = items_of(db, gid)
        check("重批失败后，上一轮的逐题结果还在（不是 0 题）",
              len(after) == len(ids_first),
              f"现在 {len(after)} 题，之前 {len(ids_first)} 题 —— 旧结果被删掉了")
        check("重批失败后，题号没被换掉（还是原来那几道题）",
              sorted(i.q_no for i in after) == ["1", "2"],
              [i.q_no for i in after])

        marks = marks_of(db, gid)
        check("重批失败后，老师画的标注还在",
              len(marks) == len(mark_ids), f"现在 {len(marks)} 条，之前 {len(mark_ids)} 条")
        alive = {i.id for i in after}
        check("标注仍挂在同一道题上（没变成孤儿标注）",
              all(x.item_id in alive for x in marks) and bool(marks),
              [(x.id, x.item_id) for x in marks])

        db.refresh(g)
        check("失败要看得见（status=failed 且 error 非空）",
              g.status == "failed" and bool(g.error), (g.status, g.error))
    finally:
        db.close()


# ---------------------------------------------------------------- ③ 成功也换代
def t_success_rerun_replaces() -> None:
    """重跑**成功**的那一半：新一代顶替旧一代，不许两批并存。

    ⚠️ 这一节在改动前后都应该是**绿的**（现在的实现是先全删再写，天然只有一代）。
       它存在的意义是「改失败路径时别把成功路径改坏」：一旦为了「失败不删旧行」
       改成两代共表，很容易忘了在成功时把旧一代真正清掉 —— 那一晚老师会看到
       两道题号相同的题，导出还会重复。
    """
    print("\n=== ③ 重跑成功 → 新一代顶替旧一代（不许两批并存） ===")
    db = SessionLocal()
    try:
        g = mk_grade(db, "success")
        gid = int(g.id)
        run_round(gid, items=[item_payload(q_no="1", stem="第一轮 A"),
                              item_payload(q_no="2", stem="第一轮 B")])
        db.refresh(g)
        old = items_of(db, gid)
        old_ids = {i.id for i in old}
        check("前置：第一轮 2 题", len(old) == 2, len(old))

        # 一条挂在题上的标注 + 一条页级标注（item_id 为空 = 老师写在纸上的话）
        m_on_item = G.add_mark(db, gid, GradeMarkIn(
            page_no=1, kind="check", geo=[100, 100, 200, 200], item_id=old[0].id))
        m_on_page = G.add_mark(db, gid, GradeMarkIn(
            page_no=1, kind="text", text="页脚批注：书写要工整", geo=[10, 900, 300, 960]))
        run_no_before = getattr(g, "item_run", None)

        # —— 第二轮：成功，且只出 1 题 ——
        run_round(gid, items=[item_payload(q_no="1", stem="第二轮 A")])
        db.refresh(g)

        d = G.detail(db, g)
        check("重跑成功后，页面只看到新一轮的题（1 题，不是 3 题并存）",
              len(d["items"]) == 1, [x.get("q_no") for x in d["items"]])
        check("旧一代的题不再出现在页面上",
              not (set(x["id"] for x in d["items"]) & old_ids),
              sorted(set(x["id"] for x in d["items"]) & old_ids))
        check("题库里也只留新一代（旧行真的被删掉，不是靠过滤藏起来）",
              len(items_of(db, gid)) == 1,
              f"表里还有 {len(items_of(db, gid))} 行 —— 两代并存了？")

        if run_no_before is not None and hasattr(g, "item_run"):
            check("grade.item_run 换代指针变大了",
                  int(g.item_run) > int(run_no_before), (run_no_before, g.item_run))
        else:
            print("  [info] 这一版还没有 item_run 世代指针，跳过那条"
                  "（R2 的失败路径断言不依赖它）")

        marks = marks_of(db, gid)
        visible = {m["id"] for m in d["marks"]}
        check("旧一代题上的标注被清掉（它跟着那道旧题一起走）",
              m_on_item.id not in visible and all(x.id != m_on_item.id for x in marks),
              [x.id for x in marks])
        check("页级标注（不挂在任何题上）必须留下 —— 那是老师写在纸上的话",
              m_on_page.id in visible and any(x.id == m_on_page.id for x in marks),
              [x.id for x in marks])
    finally:
        db.close()


# ---------------------------------------------------------------- ④ 确认门
class _FakeThread:
    """把 launch 里那个后台线程拦住：只记「有没有真的起线程」，不跑。"""
    started: list = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self) -> None:
        _FakeThread.started.append(self)


def try_launch(db, grade) -> tuple[int, str, int]:
    """返回 (状态码, 说明, 起的线程数)。0 = 没抛错（= 门没拦住）。"""
    import threading as _th
    _FakeThread.started = []
    orig = _th.Thread
    _th.Thread = _FakeThread
    code, msg = 0, ""
    try:
        try:
            G.launch(db, grade)
        except G.GradeError as e:
            code, msg = int(e.status), str(e)
        except Exception as e:                         # noqa: BLE001
            code, msg = -1, f"{type(e).__name__}: {e}"
    finally:
        _th.Thread = orig
    return code, msg, len(_FakeThread.started)


def t_confirm_gate() -> None:
    print("\n=== ④ 已确认/已复核的批改，重跑要先问过老师 ===")
    db = SessionLocal()
    try:
        # —— A：老师点过「确认整份」（status=done + reviewed_at 非空）——
        g = mk_grade(db, "gate-done")
        gid = int(g.id)
        run_round(gid, items=[item_payload(q_no="1")])
        db.refresh(g)
        G.confirm_grade(db, gid, GradeConfirmIn())
        db.refresh(g)
        check("前置：这份批改已经是 done 且 reviewed_at 非空",
              g.status == "done" and bool(g.reviewed_at), (g.status, g.reviewed_at))

        code, msg, threads = try_launch(db, g)
        check("已确认的批改重跑 → 返回 409（要先确认）", code == 409, f"实际 {code}，说明：{msg}")
        check("409 的说明里讲清了会清掉什么（复核/批注）",
              any(k in msg for k in ("确认", "批注", "复核", "标注")), msg)
        check("被拦下时没有偷偷起批改线程", threads == 0, f"起了 {threads} 个")

        # —— B：只有 reviewed_at、status 不是 done 的旧数据 ——
        g2 = mk_grade(db, "gate-reviewed")
        gid2 = int(g2.id)
        run_round(gid2, items=[item_payload(q_no="1")])
        db.refresh(g2)
        g2.status = "reviewing"
        g2.reviewed_at = G._now()
        db.commit()
        code2, msg2, threads2 = try_launch(db, g2)
        check("reviewed_at 非空（哪怕 status 不是 done）也要 409",
              code2 == 409, f"实际 {code2}，说明：{msg2}")
        check("这种情况也没起线程", threads2 == 0, f"起了 {threads2} 个")

        # —— C：一轮都没批成的（failed）不该被拦：那是修好接口后重试的唯一出口 ——
        g3 = mk_grade(db, "gate-failed")
        gid3 = int(g3.id)
        run_round(gid3, boom="模拟：首次就失败")
        db.refresh(g3)
        check("前置：这份是 failed（没有可丢的复核）", g3.status == "failed", g3.status)
        code3, msg3, _ = try_launch(db, g3)
        check("failed 的批改可以直接重跑（不许把重试的路也堵死）",
              code3 == 0, f"实际 {code3}，说明：{msg3}")
    finally:
        db.close()


def main() -> int:
    Base.metadata.create_all(engine)
    print("=" * 72)
    print("重新批改的安全性（R2）：失败不删旧结果 / 已确认要先问")
    print("=" * 72)
    t_failed_rerun_keeps_old()
    t_success_rerun_replaces()
    t_confirm_gate()

    print("\n" + "=" * 72)
    print(f"共 {total} 项，失败 {len(fails)} 项")
    if fails:
        for f in fails:
            print(f"  x {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
