# -*- coding: utf-8 -*-
"""待审列表里的「上移 / 下移 / 并入上一题」接口自检。

跑法（服务得在跑；默认打实验副本 8010，可用 SHIKE_BASE 换）：
    cd backend
    ./.venv/Scripts/python.exe test_batch_merge.py

⚠️ 本测试**一次 AI 都不调**，也**绝不碰库里已有的数据**：
  · 任务用 `POST /api/batches` 的 `start=False` 建（接口定义里就写了它是给测试用的）
  · 原貌图 / 配图都是**自己现造**的 PNG，文件名带 `t_batch_merge_` 前缀
  · 收尾按 **job_id** 删任务、按**自己造的文件名**删图 —— 不写「清掉所有某个东西」
  · 不调 approve：那会在题库里真建出一道题，测试不该留这种东西

覆盖的是「跨页题在待审里收口」这条新路（2026-10-08）：
    一页一页识别时，一道跨页的题被页界切成上下两半 → 点「并入上一题」接回去。
"""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from app import config                                   # noqa: E402  只为 CROPS_DIR

BASE = os.environ.get("SHIKE_BASE", "http://127.0.0.1:8010")

fails: list[str] = []
state: dict = {"jobs": [], "files": []}


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  [OK ] {name}")
    else:
        print(f"  [FAIL] {name}" + (f" —— {detail}" if detail else ""))
        fails.append(name)


def req(method: str, path: str, body=None, timeout: int = 30):
    """返回 (status, data)。**4xx/5xx 不抛异常** —— 边界拒绝正是要断言的东西。"""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, raw.decode("utf-8", "replace")[:200]


def crop_file(url: str) -> Path:
    return config.CROPS_DIR / url.rsplit("/", 1)[-1]


def make_png(name: str, w: int = 100, h: int = 80) -> str:
    """现造一张只属于本次测试的图，返回它的 URL。"""
    from PIL import Image

    config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CROPS_DIR / name
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    state["files"].append(name)
    return f"/api/crops/{name}"


def ids_of(items: list[dict]) -> list[str]:
    return [i["id"] for i in items]


def seqs_of(items: list[dict]) -> list[int]:
    return [i["seq"] for i in items]


def cleanup() -> None:
    """收尾：按 id 删任务；再兜底删自己造的那几张图（已被回收的就不用删了）。"""
    print("\n==== 收尾（只删本次自造的东西）====")
    for jid in state["jobs"]:
        st, r = req("DELETE", f"/api/batches/{jid}")
        print(f"  删任务 {jid} -> {st} {str(r)[:80]}")
    for name in state["files"]:
        p = config.CROPS_DIR / name
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass


def run() -> None:
    # ---------------------------------------------------------- ① 前置
    print("\n==== ① 前置：拿一个真实体系 id（不硬编码）====")
    st, curr = req("GET", "/api/curricula")
    check("GET /api/curricula 返回 200", st == 200, f"实为 {st}")
    if st != 200 or not isinstance(curr, list) or not curr:
        check("库里至少有一个体系（否则建不了任务）", False, "先建一个体系再跑本测试")
        return
    cid = curr[0]["id"]
    print(f"  用体系 id={cid}（{curr[0].get('name')}）")

    # ---------------------------------------------------------- ② 建任务
    print("\n==== ② 建任务（start=false，不触发识别；图是自己造的）====")
    imgs = [make_png(f"t_batch_merge_{i}.png", 100 + i * 5, 80) for i in range(4)]
    items_in = [{"image": u, "page_no": i + 1, "region": [40, 90, 550, 300]}
                for i, u in enumerate(imgs)]
    st, job = req("POST", "/api/batches", {
        "curriculum_id": cid, "items": items_in, "start": False,
    })
    check("POST /api/batches 返回 200", st == 200, f"实为 {st} {str(job)[:150]}")
    if st != 200:
        return
    jid = job["id"]
    state["jobs"].append(jid)
    st, items = req("GET", f"/api/batches/{jid}/items")
    check("新建任务有 4 条、seq 1..4", len(items) == 4 and seqs_of(items) == [1, 2, 3, 4],
          f"实为 {seqs_of(items)}")
    if len(items) != 4:
        return
    A, B, C, D = ids_of(items)
    check("新建条目都是待审", all(i["status"] == "pending" for i in items))

    # 给每条写上可辨认的题干，后面靠它验证拼接
    texts = {A: "第一题：已知函数 $f(x)=x^2-2x$，", B: "求它在 $[0,3]$ 上的最小值。",
             C: "第三题：数列 $\\{a_n\\}$ 中 $a_1=1$。", D: "第四题：如图，求阴影面积。"}
    for iid, t in texts.items():
        st, r = req("PATCH", f"/api/batches/items/{iid}", {"content": t})
        check(f"写入题干 {iid[:6]} -> {st}", st == 200)

    # ---------------------------------------------------------- ③ 上下移动
    print("\n==== ③ 上下移动（只和相邻的待审条目换位）====")
    st, r = req("POST", f"/api/batches/items/{D}/move?direction=up")
    check("D 上移返回 200", st == 200, f"实为 {st} {str(r)[:120]}")
    if st == 200:
        got = ids_of(r["items"])
        check("D 和 C 换了位", got == [A, B, D, C], f"实为 {[x[:6] for x in got]}")
        check("seq 重排成 1..4", seqs_of(r["items"]) == [1, 2, 3, 4], f"实为 {seqs_of(r['items'])}")
    st, r = req("POST", f"/api/batches/items/{D}/move?direction=down")
    check("D 下移复位", st == 200 and ids_of(r["items"]) == [A, B, C, D],
          f"实为 {st} {[x[:6] for x in ids_of(r['items'])] if st == 200 else r}")

    st, r = req("POST", f"/api/batches/items/{A}/move?direction=up")
    check("第一条再上移 -> 422", st == 422, f"实为 {st} {str(r)[:100]}")
    st, r = req("POST", f"/api/batches/items/{D}/move?direction=down")
    check("最后一条再下移 -> 422", st == 422, f"实为 {st} {str(r)[:100]}")
    st, r = req("POST", f"/api/batches/items/{A}/move?direction=sideways")
    check("direction 乱填 -> 422", st == 422, f"实为 {st}")
    st, r = req("POST", "/api/batches/items/nope/move?direction=up")
    check("不存在的条目 -> 404", st == 404, f"实为 {st}")

    # 已驳回的条目不能动，也不能被跨过去
    st, _ = req("POST", f"/api/batches/items/{B}/reject")
    check("先把 B 驳回", st == 200, f"实为 {st}")
    st, r = req("POST", f"/api/batches/items/{B}/move?direction=up")
    check("已驳回的条目不能移动 -> 409", st == 409, f"实为 {st} {str(r)[:100]}")
    st, r = req("POST", f"/api/batches/items/{A}/move?direction=down")
    check("不能跨过已驳回的下一条 -> 422", st == 422, f"实为 {st} {str(r)[:100]}")
    st, r = req("POST", f"/api/batches/items/{C}/move?direction=up")
    check("不能跨过已驳回的上一条 -> 422", st == 422, f"实为 {st} {str(r)[:100]}")
    st, _ = req("POST", f"/api/batches/items/{B}/reset")
    check("B 退回待审", st == 200, f"实为 {st}")

    # ---------------------------------------------------------- ④ 合并
    print("\n==== ④ 并入上一题：题干接到上一条后面，本条删掉 ====")
    st, r = req("POST", f"/api/batches/items/{A}/merge-up")
    check("第一条没有上一题 -> 422", st == 422, f"实为 {st} {str(r)[:100]}")

    st, r = req("POST", f"/api/batches/items/{C}/merge-up")
    check("C 并入 B 返回 200", st == 200, f"实为 {st} {str(r)[:150]}")
    if st == 200:
        check("条目少了一条（4 -> 3）", len(r["items"]) == 3, f"实为 {len(r['items'])}")
        check("C 已经不在了", C not in ids_of(r["items"]))
        check("seq 收口成 1..3", seqs_of(r["items"]) == [1, 2, 3], f"实为 {seqs_of(r['items'])}")
        st2, items2 = req("GET", f"/api/batches/{jid}/items")
        b = next(i for i in items2 if i["id"] == B)
        check("B 的题干 = B 原文 + 空行 + C 原文",
              b["content"] == texts[B] + "\n\n" + texts[C],
              f"实为 {b['content']!r}")
        check("merge 返回值里的 items 与 GET 一致",
              [(i["id"], i["seq"]) for i in r["items"]] == [(i["id"], i["seq"]) for i in items2])
    st, r = req("POST", f"/api/batches/items/{B}/merge-up")
    check("B 并入 A 返回 200", st == 200, f"实为 {st} {str(r)[:150]}")
    if st == 200:
        check("条目 3 -> 2", len(r["items"]) == 2, f"实为 {len(r['items'])}")
        st2, items2 = req("GET", f"/api/batches/{jid}/items")
        a = next(i for i in items2 if i["id"] == A)
        check("A 的题干 = A + (B + C)",
              a["content"] == texts[A] + "\n\n" + texts[B] + "\n\n" + texts[C],
              f"实为 {a['content']!r}")

    # 现在的顺序是 [A, D]
    st, cur = req("GET", f"/api/batches/{jid}/items")
    check("此时剩 [A, D]", ids_of(cur) == [A, D], f"实为 {[x[:6] for x in ids_of(cur)]}")

    # ---------------------------------------------------------- ⑤ 原貌图回收
    print("\n==== ⑤ 合并掉的条目，它独有的原貌图要按引用计数回收 ====")
    check("C 的原貌图已被回收（C 没了）", not crop_file(imgs[2]).exists())
    check("B 的原貌图已被回收（B 没了）", not crop_file(imgs[1]).exists())
    check("A 的原貌图还在（A 还在）", crop_file(imgs[0]).exists(),
          "合并后把宿主自己的图删了，那是数据丢失")
    check("D 的原貌图还在（D 还在）", crop_file(imgs[3]).exists())

    # ---------------------------------------------------------- ⑥ 配图
    print("\n==== ⑥ 配图：能带走的带走，带不走的拒绝（绝不静默丢一张）====")
    fig_keep = make_png("t_batch_merge_fig_keep.png", 60, 60)
    fig_lose = make_png("t_batch_merge_fig_lose.png", 60, 60)

    st, r = req("PATCH", f"/api/batches/items/{D}", {"figure_image": fig_keep,
                                                    "needs_figure": True})
    check("给 D 挂上配图", st == 200, f"实为 {st} {str(r)[:120]}")
    st, r = req("PATCH", f"/api/batches/items/{A}", {"figure_image": fig_lose})
    check("给 A 也挂一张不同的配图", st == 200, f"实为 {st}")

    st, r = req("POST", f"/api/batches/items/{D}/merge-up")
    check("上下两条都有配图 -> 422 拒绝", st == 422, f"实为 {st} {str(r)[:150]}")
    check("拒绝之后两条都还在（没有半途改坏）", st == 422 and True)
    st2, items2 = req("GET", f"/api/batches/{jid}/items")
    check("拒绝后条目数没变", len(items2) == 2, f"实为 {len(items2)}")
    check("拒绝后 D 的配图还在", next(i for i in items2 if i["id"] == D)["figure_image"] == fig_keep)

    st, r = req("PATCH", f"/api/batches/items/{A}", {"figure_image": ""})
    check("撤掉 A 的配图", st == 200, f"实为 {st}")
    check("撤掉的那张按引用计数回收了", not crop_file(fig_lose).exists())

    st, r = req("POST", f"/api/batches/items/{D}/merge-up")
    check("再合并返回 200", st == 200, f"实为 {st} {str(r)[:150]}")
    if st == 200:
        check("figure_moved = True（配图跟着过去了）", r.get("figure_moved") is True, f"实为 {r.get('figure_moved')}")
        check("条目 2 -> 1", len(r["items"]) == 1, f"实为 {len(r['items'])}")
        a = r["items"][0]
        check("宿主是 A", a["id"] == A)
        check("配图迁移到了 A", a["figure_image"] == fig_keep, f"实为 {a['figure_image']!r}")
        check("needs_figure 被置上（有图就得印）", a["needs_figure"] is True)
        check("迁移过去的那张图**还在盘上**（没被当成孤儿删掉）", crop_file(fig_keep).exists(),
              "这是最难发现的坑：先删后数引用，刚迁移的图会被回收")
        check("D 的原貌图已被回收", not crop_file(imgs[3]).exists())
        check("A 的题干 = A + B + C + D",
              a["content"] == "\n\n".join([texts[A], texts[B], texts[C], texts[D]]),
              f"实为 {a['content']!r}")

    # ---------------------------------------------------------- ⑦ 收尾
    print("\n==== ⑦ 删任务 -> 条目独有截图一并回收 ====")
    st, r = req("DELETE", f"/api/batches/{jid}")
    check("DELETE /api/batches/{id} 返回 200", st == 200, f"实为 {st} {str(r)[:120]}")
    state["jobs"].remove(jid)            # 已删，别再删一次
    check("A 的原貌图随任务回收了", not crop_file(imgs[0]).exists())
    check("已迁移的配图也随任务回收了", not crop_file(fig_keep).exists())
    st, r = req("GET", f"/api/batches/{jid}")
    check("任务确实没了 -> 404", st == 404, f"实为 {st}")


def main() -> int:
    print(f"目标服务：{BASE}")
    print(f"截图目录：{config.CROPS_DIR}")
    try:
        st, _ = req("GET", "/api/batch/options", timeout=5)
        if st != 200:
            print(f"\n❌ 服务没在跑或不是拾课（GET /api/batch/options -> {st}）")
            return 2
    except Exception as e:                                    # noqa: BLE001
        print(f"\n❌ 连不上 {BASE}：{type(e).__name__} {e}")
        return 2

    try:
        run()
    finally:
        cleanup()

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项失败：")
        for f in fails:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
