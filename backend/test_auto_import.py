# -*- coding: utf-8 -*-
"""自动导入（讲义一条命令跑完）的判据测试。

**只测纯函数** —— 不连库、不调 AI、不写任何东西。
那条真链路（识别 → 修图 → 入库）很大很慢，靠 `test_auto_import_live.py` 端到端验；
这里守的是「哪条能自动入库」这个**最要命的判断**，它判错一次就往题库里塞错题。

判据一旦漂了，症状是静默的：报告说「全部入库」，而题库里多了几条带答案的整题图、
或者少了配图。所以这几条断言必须钉死：

  · 整题兜底要**先于**怀疑标记判定（兜底条目天然带 figure_box_missing）
  · 候选多处时必须放弃（宁可不配，不能配错）
  · 跨页候选不能把本页的唯一候选挤掉

跑法：
    cd backend && .venv/Scripts/python.exe test_auto_import.py
退出码 0 = 全过。
"""
from __future__ import annotations

import os
import sys
import tempfile

# ⚠️ 必须在 import app.* 之前指走库路径：app.db 在 import 时就建 engine。
_SCRATCH = os.path.join(tempfile.gettempdir(), "shike_auto_import_scratch")
os.makedirs(_SCRATCH, exist_ok=True)
os.environ.setdefault("SHIKE_DB_PATH", os.path.join(_SCRATCH, "auto.db"))
os.environ.setdefault("SHIKE_DATA_DIR", _SCRATCH)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.models import AutoTask                      # noqa: E402
from app.services import auto_import as A            # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail="") -> None:
    if ok:
        print("  [OK ] %s" % name)
    else:
        print("  [FAIL] %s   %s" % (name, detail))
        fails.append(name)


# ================================================================ ① 页范围解析
def t_parse_pages() -> None:
    print("\n==== ① 页范围解析（agent 传 '1-30,35' 这种写法） ====")
    check("全部：'all' → 1..12",
          A.parse_pages("all", 12) == list(range(1, 13)), A.parse_pages("all", 12))
    check("不给 = 全部",
          A.parse_pages(None, 12) == list(range(1, 13)))
    check("空串 = 全部",
          A.parse_pages("", 12) == list(range(1, 13)))
    check("区间 + 单页：'2-5,9'",
          A.parse_pages("2-5,9", 20) == [2, 3, 4, 5, 9], A.parse_pages("2-5,9", 20))
    check("中文逗号也认（语音输入常带）：'2-4，7'",
          A.parse_pages("2-4，7", 20) == [2, 3, 4, 7], A.parse_pages("2-4，7", 20))
    check("倒序区间 9-2 自动纠正",
          A.parse_pages("9-2", 20) == [2, 3, 4, 5, 6, 7, 8, 9], A.parse_pages("9-2", 20))
    check("数组原样去重排序",
          A.parse_pages([5, 3, 3, 1], 20) == [1, 3, 5])
    check("越界不在这里报错（交给调用方带 total 提示）：'1-999' 全给出",
          len(A.parse_pages("1-999", 20)) == 999)
    # 反面：认不出的写法要抛，不能静默当「全部」—— 否则会误处理整份讲义
    bad = False
    try:
        A.parse_pages("abc", 20)
    except ValueError:
        bad = True
    check("认不出的写法要抛 ValueError（别静默当全部）", bad)


# ================================================================ ② 拆批
def t_plan_batches() -> None:
    print("\n==== ② 拆批（44 页 > 一次 30 页上限） ====")
    got = A.plan_batches(list(range(1, 45)), 30)
    check("44 页 / 上限 30 → 两批", len(got) == 2, [len(g) for g in got])
    check("批内容连续且不重不漏", got[0] == list(range(1, 31)) and got[1] == list(range(31, 45)),
          [g[0] for g in got])
    check("刚好整除不多出一批", A.plan_batches(list(range(1, 31)), 30) == [list(range(1, 31))])
    check("limit<=0 也不会死循环", A.plan_batches([1, 2, 3], 0) == [[1], [2], [3]])


# ================================================================ ③ 入库判据
def t_judge() -> None:
    print("\n==== ③ 能不能自动入库（最要命的一条） ====")
    J = A.judge

    # --- 该放行的 ---
    check("有图 + 无怀疑标记 → 直接入库",
          J("求圆柱体积", ["figure_snapped"], True, "/api/crops/a.png")
          == ("pass", "figure_ok"), J("求圆柱体积", ["figure_snapped"], True, "/api/crops/a.png"))
    check("明确不需要图 → 直接入库",
          J("1+1=?", [], False, "") == ("pass", "no_figure_needed"),
          J("1+1=?", [], False, ""))
    # ★ 关键顺序：整题兜底的条目天然带 figure_box_missing（它就是「定位不到」才兜底的），
    #   若先判怀疑标记，兜底永远不会被放行 —— 52 条里会全部卡住等人工。
    check("整题兜底 → 放行（哪怕同时带 figure_box_missing）",
          J("求阴影面积（图略）", ["figure_box_missing", "figure_whole_question"], True, "")
          == ("pass", "whole_question"),
          J("求阴影面积（图略）", ["figure_box_missing", "figure_whole_question"], True, ""))
    check("flags 传 JSON 字符串也认",
          J("题干", '["figure_snapped"]', True, "/api/crops/a.png") == ("pass", "figure_ok"))

    # --- 该跳过的（连人都不必看）---
    check("内容空 → skip",
          J("   ", [], False, "") == ("skip", "no_content"))
    check("bad_json → skip（整页要重跑）",
          J("占位", ["bad_json", "empty_stem"], False, "") == ("skip", "bad_json"),
          J("占位", ["bad_json", "empty_stem"], False, ""))
    check("empty_stem → skip",
          J("占位", ["empty_stem"], True, "") == ("skip", "empty_stem"),
          J("占位", ["empty_stem"], True, ""))

    # --- 该去修的 ---
    check("要图但没图 → repair（先试自动重裁）",
          J("如图", ["figure_box_missing"], True, "") == ("repair", "figure_missing"),
          J("如图", ["figure_box_missing"], True, ""))
    check("有图但框是估的 → repair",
          J("如图", ["figure_rough"], True, "/api/crops/b.png") == ("repair", "figure_doubt"))
    check("有图但和别人抢同一处 → repair",
          J("如图", ["figure_shared_candidate"], True, "/api/crops/b.png")
          == ("repair", "figure_doubt"))
    check("跨页题（图在下一页）→ repair",
          J("如图", ["figure_cross_page"], True, "") == ("repair", "figure_missing"),
          J("如图", ["figure_cross_page"], True, ""))
    # 模型说不要图、却有配图标记 → 两边打架，让它去修一次
    check("说不要图但有 dup 标记 → repair（两边打架别硬信）",
          J("如图", ["figure_dup_candidate"], False, "") == ("repair", "figure_doubt"),
          J("如图", ["figure_dup_candidate"], False, ""))


# ================================================================ ④ 挑候选
def t_pick() -> None:
    print("\n==== ④ 自动挑候选（宁可不配，不能配错） ====")
    P = A.pick_candidate

    def c(box, page=1, kind="", w=1000, h=1000):
        return {"box": box, "page": page, "kind": kind, "width": w, "height": h}

    # 原框被候选包住 → 这是「把框修准了」，取面积最小的那个
    got = P([100, 100, 200, 200], [c([90, 90, 210, 210]), c([60, 60, 260, 260])])
    check("原框被两处包住 → 取面积最小的（最贴的）",
          got is not None and got["box"] == [90, 90, 210, 210], got)

    # 候选比原框大太多 → 那是「另一件事」，不认
    huge = P([100, 100, 200, 200], [c([0, 0, 1000, 1000])])
    check("候选比原框大 50 倍 → 不认（不是同一处）", huge is None, huge)

    # 原框与候选基本重合
    check("原框与候选 IoU 高 → 选它",
          P([100, 100, 200, 200], [c([102, 102, 198, 198])]) is not None)

    # 原框和候选完全不搭 → 不猜
    check("原框和候选全不搭 → None（模型指错了位置，不替它猜）",
          P([10, 10, 50, 50], [c([600, 600, 800, 800])]) is None)

    # 没有原框（模型没给框）：只有一处才敢配
    check("没原框 + 恰好 1 处候选 → 选",
          P(None, [c([100, 100, 300, 300])]) is not None)
    check("没原框 + 3 处候选 → None（这页有三幅图，选哪个都是猜）",
          P(None, [c([10, 10, 100, 100]), c([200, 200, 300, 300]), c([400, 400, 500, 500])])
          is None)
    check("没有候选 → None", P([1, 1, 2, 2], []) is None)

    # 跨页：本页有候选就只在本页挑
    cross = [c([100, 100, 300, 300], page=5), c([100, 100, 400, 400], page=6)]
    check("跨页题：本页有候选就只在本页挑（别被下一页的挤掉）",
          P(None, cross, prefer_page=5) is not None
          and P(None, cross, prefer_page=5)["page"] == 5,
          P(None, cross, prefer_page=5))
    # 本页一处都没有时，下一页的候选才是唯一线索；但**两处以上仍然不猜** ——
    # 跨页题的图也可能不止一幅，选错就等于把下一页别的题的图贴过来。
    check("跨页题：本页没候选 + 下一页有两处 → None（还是猜）",
          P(None, cross, prefer_page=9) is None, P(None, cross, prefer_page=9))
    check("跨页题：本页没候选 + 下一页恰一处 → 选它（这是唯一线索）",
          P(None, [c([100, 100, 300, 300], page=6)], prefer_page=5) is not None)


# ================================================================ ⑤ 报告
def t_report() -> None:
    print("\n==== ⑤ 报告（agent 直接读这个） ====")
    task = AutoTask(id="t1", status="done", src_filename="讲义.pdf",
                    document_id="doc1", job_ids='["job-a"]',
                    pages="[1,2,3]", total_pages=3)
    rows = [
        {"item_id": "i1", "seq": 1, "page_no": 1, "verdict": "pass",
         "reason": "figure_ok", "job_id": "job-a", "question_id": "q1"},
        {"item_id": "i2", "seq": 2, "page_no": 2, "verdict": "human",
         "reason": "figure_repair_failed", "job_id": "job-a"},
        {"item_id": "i3", "seq": 3, "page_no": 2, "verdict": "skip",
         "reason": "no_content", "job_id": "job-a"},
        {"item_id": "i4", "seq": 4, "page_no": 3, "verdict": "repair",
         "reason": "figure_doubt", "job_id": "job-a"},
        # resume 场景：上一轮已经入库的，这一轮 reason=already，qid 必须跟着带出来
        {"item_id": "i5", "seq": 5, "page_no": 3, "verdict": "pass",
         "reason": "already", "job_id": "job-a", "question_id": "q0"},
    ]
    rep = A.build_report(task, rows)
    check("计数对得上", rep["counts"] == {"total": 5, "approved": 2,
                                        "needs_human": 2, "skipped": 1}, rep["counts"])
    check("repair 也算 needs_human（不能漏进已入库）",
          rep["counts"]["approved"] == 2, rep["counts"])
    # ★ 报告里必须列全「这份讲义一共进了题库哪些题」——包含 resume 之前入库的那批。
    #   只列这一轮新入库的，agent 会以为前面那些没成功。
    check("approved_question_ids 含 resume 之前入库的（reason=already 也要带 qid）",
          rep["approved_question_ids"] == ["q1", "q0"], rep["approved_question_ids"])
    check("待处理清单含 human 与 repair 两类", len(rep["needs_human"]) == 2,
          [r["item_id"] for r in rep["needs_human"]])
    check("跳过清单单列", len(rep["skipped"]) == 1)
    first = rep["needs_human"][0]
    check("每条带人能点的 open", (first.get("open") or "").startswith("/batch.html?job="),
          first.get("open"))
    check("每条带 agent 能调的 fix_with（直达这一条的接口）",
          first["fix_with"].endswith("/figure-crop") and "i2" in first["fix_with"],
          first["fix_with"])
    check("每条带一句人话 message（不是把 reason 原样吐出来）",
          "配图" in (first.get("message") or "") and first["message"] != first["reason"],
          first.get("message"))
    check("next 指向 resume 收尾", "/resume" in (rep.get("next") or ""), rep.get("next"))

    clean = A.build_report(task, [rows[0]])
    check("没有待处理时 next 直接说入库完了",
          "已入库" in (clean.get("next") or ""), clean.get("next"))


# ================================================================ ⑥ 迁移链
def t_migration() -> None:
    print("\n==== ⑥ 迁移链（不写死版本号，只验 0037 在祖先链上） ====")
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        cfg = Config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "alembic.ini"))
        script = ScriptDirectory.from_config(cfg)
        chain = [r.revision for r in script.walk_revisions()]
        check("0037 在迁移链上", "0037" in chain, chain[:4])
        check("0037 的上一版是 0036",
              script.get_revision("0037").down_revision == "0036")
        check("auto_tasks 表在 0037 里建（不是靠 create_all 偷偷建）",
              "auto_tasks" in open(os.path.join(
                  os.path.dirname(os.path.abspath(__file__)),
                  "alembic", "versions", "0037_auto_tasks.py"), encoding="utf-8").read())
    except Exception as e:                               # noqa: BLE001
        check("迁移链可读", False, repr(e))


def main() -> int:
    print("自动导入判据测试（不连库、不调 AI）")
    t_parse_pages()
    t_plan_batches()
    t_judge()
    t_pick()
    t_report()
    t_migration()
    print()
    if fails:
        print("失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
