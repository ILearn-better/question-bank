# -*- coding: utf-8 -*-
"""批量入库：接口层的端到端自检（分割 → 建任务 → 待审 → 通过/驳回 → 删任务）。

    cd backend && ./.venv/Scripts/python.exe test_batch_import.py

8000 的服务要在跑（打 HTTP，不直连数据库）。自建自删，不碰已有题目。

⚠️ **本测试一次 AI 都不调**。全部用 `start=false` 建任务（接口定义里就写了它是
   「给测试用的」），所以跑一遍不花钱、不依赖模型是否可用、也不会因为模型今天
   心情不同而飘。真正调模型的那一段（`recognize_fields`）靠
   `test_batch_fields.py` 用真真的坏输入压解析层来覆盖 —— 分工就是这样。

⚠️ 截图**自己造、自己删**，绝不借用库里已有的图片文件。
   踩过一次（test_render_prefer.py 头部记着）：图省事从库里挑一张图来用，
   结果删题时「清理无人引用的截图」把它当无人引用真删了。

这个测试最想守住的两条不变量：
  1. **在跑批量识别的路上，写到库里的东西必须能对账** —— 建了几条、通过几条、
     驳回几条、删完之后剩几条，都要能从接口读回来对上。
  2. **删任务不会带走已被题库引用的原貌图**（引用计数），但会带走没人再用的。
     这条错了是静默的：老师的截图要么被误删、要么永远堆在盘上。

收尾用 try/finally 包住：中途任何一步失败，也不能把测试数据留在老师库里。
"""
import io
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import config                                   # noqa: E402  只为 CROPS_DIR

# 默认打哪个端口，**按检出目录名判定** —— 这个仓库曾经有一份实验副本
# （目录名 `question-bank-ai`，端口 8010），副本里的测试必须验副本自己的代码。
# 写死任一个端口都会出事：写死 8000 时副本里去测原项目（改坏了也照样全绿）；
# 写死 8010 时主线里根本连不上。用 SHIKE_BASE 可以临时指到别处。
BASE = os.environ.get(
    "SHIKE_BASE",
    "http://127.0.0.1:8010" if Path(__file__).resolve().parents[1].name == "question-bank-ai"
    else "http://127.0.0.1:8000",
)

# 假的 document_id：测试建的题不该挂在老师真实的卷子下面，
# 否则从他的「按文档看题」里会冒出一堆测试题（test_render_prefer 的做法）
T_DOC = "t-batch-import"
T_DOCNAME = "批量入库自检（可删）"

# 库里知识树最全的体系：只有 国内高中数学 有节点（119 个）
CURRICULUM_WITH_TREE = 3

fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)


def skip(name: str, why: str) -> None:
    print(f"  [skip] {name}：{why}")


def req(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode("utf-8", "replace")


def crop_file(url: str) -> Path:
    """把 /api/crops/xxx.png 换算成磁盘路径 —— 判断「文件到底还在不在」只能看盘。"""
    return config.CROPS_DIR / url.rsplit("/", 1)[-1]


def crop_count() -> int:
    return len(list(config.CROPS_DIR.glob("*"))) if config.CROPS_DIR.exists() else 0


def req_raw(method: str, path: str):
    """拿**原始字节**（区域预览返回的是 PNG，不是 JSON）。返回 (状态码, 字节, Content-Type)。

    ⚠️ 不能拿 req() 凑合：它会把响应体按 utf-8 decode —— 二进制一旦走了那条路，
       「到底返回了多大的图」就没法断言了，而这里要验的正是它真的渲染出了一张图。
    """
    r = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type")


def pdf_page_count(doc_id: str) -> int | None:
    st, r = req("GET", f"/api/documents/{doc_id}/pages")
    if st != 200 or not isinstance(r, dict):
        return None
    try:
        return int(r.get("page_count") or 0)
    except (TypeError, ValueError):
        return None


def pick_pdf_doc() -> dict | None:
    """挑一份能进「页面视图」的 PDF 文档来试 batch-crop。

    返回值里带上 `page_count` —— 跨页那条断言要用它决定怎么构造测试块。

    ⚠️ 只按「是不是 PDF」挑是不够的：库里排在最前面的完全可能是**单页**卷子，
       那样跨页块的第 2 段必然越界，拼图只有第一段那么高，
       断言就会以「看起来像代码 bug」的方式挂掉（这条踩过一次）。
    """
    st, docs = req("GET", "/api/documents")
    if st != 200 or not isinstance(docs, list):
        return None
    cands = [d for d in docs
             if (d.get("filetype") or "").lower() == ".pdf" and d.get("block_count")]
    # 优先找多页的；找不到就退回首份单页 PDF（跨页那段会退化成同页两段来测）
    for d in cands[:5]:
        n = pdf_page_count(d["id"])
        if n is None:
            continue
        if n >= 2:
            return dict(d, page_count=n)
    first = cands[0] if cands else None
    if first is None:
        return None
    return dict(first, page_count=pdf_page_count(first["id"]) or 1)


def make_own_crops(n: int, state: dict) -> list[str]:
    """自造 n 张只属于本次测试的原貌图（不裁真实文档）。"""
    from PIL import Image

    config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        name = f"t_batch_import_{i}.png"
        path = config.CROPS_DIR / name
        buf = io.BytesIO()
        Image.new("RGB", (120 + i * 10, 60), (255, 255, 255)).save(buf, "PNG")
        path.write_bytes(buf.getvalue())
        state["crops"].append(name)
        out.append(f"/api/crops/{name}")
    return out


# ==================================================================== 主体
def run(state: dict) -> None:
    # ---------------------------------------------------------- ① 选项
    print("\n==== ① 固定取值由服务端给（前端不写死）====")
    st, opt = req("GET", "/api/batch/options")
    check("GET /api/batch/options 返回 200", st == 200, f"实为 {st}")
    if st == 200:
        check("题型齐全且不是空列表",
              isinstance(opt.get("qtypes"), list) and len(opt["qtypes"]) >= 7)
        check("难度是基础/中档/拔高", opt.get("difficulties") == ["基础", "中档", "拔高"])
        check("置信度是 high/medium/low",
              opt.get("confidences") == ["high", "medium", "low"])
        check("并发数是正整数", isinstance(opt.get("workers"), int) and opt["workers"] >= 1)
        check("标签限制下发下来了",
              isinstance(opt.get("max_tags"), int) and isinstance(opt.get("max_tag_chars"), int))

    # ---------------------------------------------------------- ② batch-crop
    print("\n==== ② 一次裁多块（含跨页与越界）====")
    doc = pick_pdf_doc()
    crop_urls: list[str] = []
    if doc is None:
        skip("batch-crop", "库里没有可用的 PDF 文档，改用自造图走完后面的流程")
    else:
        print(f"   用文档：{doc['filename'][:40]}（{doc['id']}，{doc.get('page_count')} 页）")
        page1 = [{"page": 1, "x0": 40, "y0": 90, "x1": 550, "y1": 300}]
        page1b = [{"page": 1, "x0": 40, "y0": 320, "x1": 550, "y1": 520}]
        # 多段竖拼：第 1 页下半 + 第 2 页上半。
        # 库里只有单页卷子时退化成「同一页里两个不连续的带」——
        # 「多个矩形拼成一张原貌图」这条路一样测到，只是不跨页码。
        multi = (doc.get("page_count") or 1) >= 2
        cross = ([{"page": 1, "x0": 40, "y0": 540, "x1": 550, "y1": 800},
                  {"page": 2, "x0": 40, "y0": 90, "x1": 550, "y1": 320}] if multi else
                 [{"page": 1, "x0": 40, "y0": 540, "x1": 550, "y1": 800},
                  {"page": 1, "x0": 40, "y0": 90, "x1": 550, "y1": 320}])
        if not multi:
            print("   （库里没有 ≥2 页的 PDF，跨页那段降级为同页两段）")
        # 整块落在纸外 —— 应逐块报错，而不是让整次提交 500
        outside = [{"page": 1, "x0": 5000, "y0": 5000, "x1": 6000, "y1": 6000}]
        st, res = req("POST", f"/api/documents/{doc['id']}/batch-crop",
                      {"blocks": [{"regions": page1}, {"regions": page1b},
                                  {"regions": cross}, {"regions": outside}], "gap": 14})
        check("batch-crop 返回 200", st == 200, f"实为 {st} {str(res)[:120]}")
        if st == 200:
            check("total 与提交块数一致", res.get("total") == 4, f"实为 {res.get('total')}")
            check("ok = 3（越界那块不该算成功）", res.get("ok") == 3, f"实为 {res.get('ok')}")
            items = res.get("items") or []
            check("逐块返回、顺序与提交一致",
                  [x.get("index") for x in items] == [0, 1, 2, 3])
            check("越界块带 error 而不是消失、也没有 url",
                  len(items) == 4 and bool(items[3].get("error")) and not items[3].get("url"))
            ok_items = items[:3] if len(items) >= 3 else []
            check("前三块都有 url 且无 error",
                  len(ok_items) == 3 and all(x.get("url") and not x.get("error") for x in ok_items))
            crop_urls = [x["url"] for x in ok_items]
            check("三张图都真的落盘了",
                  len(crop_urls) == 3 and all(crop_file(u).exists() for u in crop_urls))
            # 交给收尾兜底 —— 万一后面中途失败，这几张也不该留在盘上
            state["crops"] += [crop_file(u).name for u in crop_urls]
            if len(crop_urls) == 3:
                # 跨页拼图应明显比单页块高 —— 证明真的竖拼了，而不是只裁了第一段
                from PIL import Image
                with Image.open(crop_file(crop_urls[2])) as im:
                    h_cross = im.height
                with Image.open(crop_file(crop_urls[0])) as im:
                    h_single = im.height
                check("多段拼图明显高于单段（确实竖拼了）",
                      h_cross > h_single * 1.3, f"多段 {h_cross}px vs 单段 {h_single}px")

    # ---------------------------------------------------------- ③ 建任务
    print("\n==== ③ 建任务（start=false，不触发识别）====")
    urls = (crop_urls + make_own_crops(1, state)) if crop_urls else make_own_crops(4, state)
    state["batches"] = urls                       # 记下来收尾兜底
    # ⚠️ 有真文档就用真文档的 id：题干配图那一段要回到**原卷页面**上框，
    #    捏一个假 id 会在那里 404。真实用法里这个字段本来就来自所选文档。
    task_doc = doc["id"] if doc else T_DOC
    task_docname = doc["filename"] if doc else T_DOCNAME
    items_in = [
        {"image": u, "page_no": i + 1, "document_id": task_doc, "doc_filename": task_docname,
         "region": [40, 90, 550, 300]}
        for i, u in enumerate(urls)
    ]
    st, job = req("POST", "/api/batches", {
        "curriculum_id": CURRICULUM_WITH_TREE,
        "document_id": task_doc, "doc_filename": task_docname,
        "items": items_in, "start": False,
    })
    check("POST /api/batches 返回 200", st == 200, f"实为 {st} {str(job)[:150]}")
    if st != 200:
        return
    job_id = job["id"]
    state["jobs"].append(job_id)
    check("状态是 queued（没有真去跑识别）", job.get("status") == "queued",
          f"实为 {job.get('status')}")
    check("total = 提交条数", job.get("total") == 4, f"实为 {job.get('total')}")
    check("done / failed 都是 0", job.get("done") == 0 and job.get("failed") == 0)
    check("finished 是 false（前端据此继续轮询）", job.get("finished") is False)
    check("带出了体系名", job.get("curriculum_name") == "国内高中数学",
          f"实为 {job.get('curriculum_name')}")
    check("pending 计数 = 4", job.get("pending") == 4, f"实为 {job.get('pending')}")
    check("approved / rejected 都是 0",
          job.get("approved") == 0 and job.get("rejected") == 0)

    st, items = req("GET", f"/api/batches/{job_id}/items")
    check("取条目返回 200", st == 200)
    check("条目数 = 4", isinstance(items, list) and len(items) == 4)
    if not (isinstance(items, list) and len(items) == 4):
        return
    check("seq 从 1 连续编号", [x["seq"] for x in items] == [1, 2, 3, 4])
    check("全部是 pending", all(x["status"] == "pending" for x in items))
    check("题干还没识别（空）", all(x["content"] == "" for x in items))
    check("没有 error（确认真的没调模型）", all(x["error"] is None for x in items))
    check("默认题型/难度/置信度已填（前端表单不留空）",
          all(x["qtype"] and x["difficulty"] and x["confidence"] for x in items))
    check("原貌图 URL 带回来了",
          all(x["image"].startswith("/api/crops/") for x in items))

    st, only_pending = req("GET", f"/api/batches/{job_id}/items?status=pending")
    check("按状态过滤可用", st == 200 and len(only_pending) == 4)
    st, only_rej = req("GET", f"/api/batches/{job_id}/items?status=rejected")
    check("过滤没命中时返回空数组", st == 200 and only_rej == [])

    # ---------------------------------------------------------- ④ 参数校验
    print("\n==== ④ 建任务的参数校验（422 而不是静默建成空任务）====")
    st, r = req("POST", "/api/batches",
                {"curriculum_id": None, "items": items_in, "start": False})
    check("不给体系 → 422", st == 422, f"实为 {st}")
    st, r = req("POST", "/api/batches",
                {"curriculum_id": CURRICULUM_WITH_TREE, "items": [], "start": False})
    check("空 items → 422", st == 422, f"实为 {st}")
    st, r = req("POST", "/api/batches", {
        "curriculum_id": CURRICULUM_WITH_TREE,
        "items": [{"image": "/api/crops/definitely-not-here.png"}], "start": False})
    check("原貌图不存在 → 422（不是建了任务再在识别时炸）", st == 422, f"实为 {st}")
    st, r = req("POST", "/api/batches", {
        "curriculum_id": CURRICULUM_WITH_TREE, "items": [{"image": ""}], "start": False})
    check("原貌图为空 → 422", st == 422, f"实为 {st}")

    # ---------------------------------------------------------- ⑤ 审核改字段
    print("\n==== ⑤ 待审时改字段 ====")
    i0, i1 = items[0]["id"], items[1]["id"]

    st, it = req("PATCH", f"/api/batches/items/{i0}", {
        "content": "已知函数 $f(x)=x^2+2x-3$，求 $f(x)$ 的零点。",
        "qtype": "解答题", "difficulty": "基础",
        "knowledge_point": "函数零点",          # 树里的名字是「函数零点与方程的根」
        "tags": ["一元二次方程", "因式分解"],
    })
    check("PATCH 返回 200", st == 200, f"实为 {st} {str(it)[:150]}")
    check("题干改进去了", (it.get("content") or "").startswith("已知函数"))
    check("题型/难度改进去了", it.get("qtype") == "解答题" and it.get("difficulty") == "基础")
    check("标签改进去了", it.get("tags") == ["一元二次方程", "因式分解"])
    check("知识点原话留着（老师敲什么就是什么）", it.get("knowledge_point") == "函数零点")
    # 这一条是模糊匹配的核心价值：模型给「函数零点」，树里叫「函数零点与方程的根」
    check("知识点模糊匹配上了知识树（node_id 非空）", bool(it.get("node_id")),
          f"node_id={it.get('node_id')}")
    check("带回了全路径（列表里能直接显示）", bool(it.get("node_path")),
          f"node_path={it.get('node_path')}")
    if it.get("node_path"):
        check("挂到的是含「零点」的那个节点", "零点" in it["node_path"], it["node_path"])

    st, it2 = req("PATCH", f"/api/batches/items/{i0}", {"difficulty": "拔高"})
    check("只传一个字段时其它字段不动",
          st == 200 and it2["content"] == it["content"] and it2["difficulty"] == "拔高"
          and it2["qtype"] == it["qtype"] and it2["knowledge_point"] == it["knowledge_point"])
    req("PATCH", f"/api/batches/items/{i0}", {"difficulty": "基础"})   # 改回去

    for bad in ({"qtype": "解答"}, {"qtype": "选择题型"}, {"difficulty": "中等"},
                {"difficulty": "困难"}):
        st, r = req("PATCH", f"/api/batches/items/{i1}", bad)
        check(f"非法取值被挡：{bad}", st == 422, f"实为 {st}")

    st, r = req("PATCH", "/api/batches/items/nope", {"content": "x"})
    check("改不存在的条目 → 404", st == 404, f"实为 {st}")

    # 给后面 approve / reject 用
    req("PATCH", f"/api/batches/items/{i1}",
        {"content": "求 $\\int_0^1 x^2\\,dx$。", "qtype": "填空题",
         "difficulty": "中档", "knowledge_point": "定积分"})

    # ---------------------------------------------------------- ⑥ 题干配图
    print("\n==== ⑥ 题干配图：框选 / 替换 / 撤掉 + 只读预览 ====")
    fig_url = None
    if doc is None:
        skip("题干配图", "库里没有可用的 PDF 文档，没法在页面上框")
    else:
        st, rows = req("GET", f"/api/batches/{job_id}/items")
        row = next((x for x in rows if x["id"] == i0), None) if st == 200 else None
        check("条目带上了配图三件套字段",
              bool(row) and {"needs_figure", "figure_note", "figure_image"} <= set(row),
              f"实为 {sorted(row) if row else row}")
        check("默认是「没图、没框」（老库升上来也是这个值）",
              bool(row) and row["needs_figure"] is False and row["figure_image"] == "")

        # 人工那个开关：AI 给的只是初值，这里代表「老师说了算」
        st, r = req("PATCH", f"/api/batches/items/{i0}", {"needs_figure": True})
        check("PATCH needs_figure=true 生效",
              st == 200 and r.get("needs_figure") is True, f"实为 {st} {str(r)[:120]}")
        st, r = req("PATCH", f"/api/batches/items/{i0}", {"needs_figure": False})
        check("再改回 false 也生效", st == 200 and r.get("needs_figure") is False)

        n0 = crop_count()
        st, r = req("POST", f"/api/batches/items/{i0}/figure-crop",
                    {"regions": [{"page": 1, "x0": 60, "y0": 60, "x1": 300, "y1": 200}]})
        check("figure-crop 返回 200", st == 200, f"实为 {st} {str(r)[:150]}")
        if st == 200:
            fig_url = r.get("url")
            check("拿到了配图 url", bool(fig_url) and fig_url.startswith("/api/crops/"))
            check("配图真的落盘了", bool(fig_url) and crop_file(fig_url).exists())
            check("框图顺手把 needs_figure 置 true（框图＝确认有图）",
                  r["item"]["needs_figure"] is True)
            state["crops"].append(crop_file(fig_url).name)
            check("盘上正好多了一张", crop_count() == n0 + 1, f"{n0} -> {crop_count()}")

        # 重新框 → 旧图必须当场回收，不能攒着
        if fig_url:
            st, r2 = req("POST", f"/api/batches/items/{i0}/figure-crop",
                         {"regions": [{"page": 1, "x0": 60, "y0": 60, "x1": 500, "y1": 400}]})
            check("重新框换了新 url",
                  st == 200 and r2.get("url") and r2["url"] != fig_url, f"实为 {st} {str(r2)[:120]}")
            if st == 200:
                check("旧配图被回收（不攒垃圾）", not crop_file(fig_url).exists())
                check("回收数报出来了", r2.get("replaced") == 1, f"实为 {r2.get('replaced')}")
                check("盘上张数没变（换了一张不是多了一张）",
                      crop_count() == n0 + 1, f"实为 {crop_count()}")
                fig_url = r2["url"]
                state["crops"].append(crop_file(fig_url).name)

        # 多段（跨页）框一张图：图象画在下一页顶上是真卷里常见的事
        if fig_url and (doc.get("page_count") or 1) >= 2:
            st, r3 = req("POST", f"/api/batches/items/{i0}/figure-crop",
                         {"regions": [{"page": 1, "x0": 60, "y0": 500, "x1": 300, "y1": 700},
                                      {"page": 2, "x0": 60, "y0": 80, "x1": 300, "y1": 260}]})
            check("跨页框配图（两段竖拼）也支持",
                  st == 200 and r3.get("url"), f"实为 {st} {str(r3)[:120]}")
            if st == 200:
                from PIL import Image
                with Image.open(crop_file(r3["url"])) as im:
                    h_two = im.height
                check("跨页配图确实拼起来了（比 200px 高）", h_two > 200, f"{h_two}px")
                fig_url = r3["url"]
                state["crops"].append(crop_file(fig_url).name)

        # 裁不出来时必须 422，不能存一张空图
        st, r = req("POST", f"/api/batches/items/{i0}/figure-crop",
                    {"regions": [{"page": 1, "x0": 9000, "y0": 9000, "x1": 9500, "y1": 9500}]})
        check("整块落在纸外 → 422", st == 422, f"实为 {st}")
        st, r = req("POST", f"/api/batches/items/{i0}/figure-crop", {"regions": []})
        check("不给区域 → 422", st == 422, f"实为 {st}")
        st, r = req("POST", "/api/batches/items/nope/figure-crop",
                    {"regions": [{"page": 1, "x0": 1, "y0": 1, "x1": 50, "y1": 50}]})
        check("给不存在的条目框图 → 404", st == 404, f"实为 {st}")

        # 只读的区域预览 —— 右栏每块的即时缩略图走的就是它
        n_before = crop_count()
        st, blob, ctype = req_raw("GET", f"/api/documents/{doc['id']}/region-image?spec=1:40,80,550,420")
        check("region-image 返回 200 且是 PNG",
              st == 200 and "image/png" in (ctype or ""), f"实为 {st} {ctype}")
        check("返回了非空图片（真的渲染出来了）", len(blob) > 1000, f"{len(blob)} 字节")
        check("★ 预览不落盘（crops 张数没变）—— 否则盘上会堆满没人引用的预览图",
              crop_count() == n_before, f"{n_before} -> {crop_count()}")
        st, _, _ = req_raw("GET", f"/api/documents/{doc['id']}/region-image?spec=1:40,540,550,800;2:40,80,550,340")
        check("多段预览也 200（跨页块缩略图要它）", st == 200, f"实为 {st}")
        st, _, _ = req_raw("GET", f"/api/documents/{doc['id']}/region-image?spec=abc")
        check("写法错 → 422（不静默给一张空图）", st == 422, f"实为 {st}")
        st, _, _ = req_raw("GET", f"/api/documents/{doc['id']}/region-image?spec=99:1,1,2,2")
        check("页码越界 → 422", st == 422, f"实为 {st}")
        st, _, _ = req_raw("GET", "/api/documents/nope/region-image?spec=1:1,1,2,2")
        check("文档不存在 → 404", st == 404, f"实为 {st}")

    # ---------------------------------------------------------- ⑦ 通过入库
    print("\n==== ⑦ 通过 → 写进正式题库 ====")
    st, ap = req("POST", f"/api/batches/items/{i0}/approve")
    check("approve 返回 200", st == 200, f"实为 {st} {str(ap)[:150]}")
    qid = ap.get("question_id") if st == 200 else None
    if qid:
        state["questions"].append(qid)
    check("拿到了新题目 id", bool(qid))
    check("条目状态变成 approved", st == 200 and ap["item"]["status"] == "approved")
    check("条目记下了 question_id", st == 200 and ap["item"]["question_id"] == qid)

    if qid:
        st, qs = req("GET", "/api/questions")
        q = next((x for x in qs if x["id"] == qid), None) if st == 200 else None
        check("题目真的在题库里", q is not None)
        if q:
            check("题干带过去了", "零点" in (q.get("content") or ""))
            check("题型/难度带过去了",
                  q.get("qtype") == "解答题" and q.get("difficulty") == "基础")
            check("原貌图带过去了", (q.get("image") or "").startswith("/api/crops/"))
            check("知识点挂上了知识树", bool(q.get("node_id")))
            check("知识点路径进了 knowledge_points",
                  any("零点" in str(k) for k in (q.get("knowledge_points") or [])))
            check("出卷形态默认 auto（有文本用文本）",
                  q.get("render_prefer") == "auto", str(q.get("render_prefer")))
            # 配图必须跟着题目走 —— 出卷走文本形态时，原貌图整张不印，
            # 题里那句「如图」就全靠这一张。断在这里，是因为它只在 approve 那一步搬过去。
            check("题干配图跟着题目进了题库",
                  (q.get("figure_image") or "").startswith("/api/crops/"),
                  f"实为 {q.get('figure_image')!r}")
            if fig_url:
                check("带过去的正是框选那一张", q.get("figure_image") == fig_url,
                      f"{q.get('figure_image')} vs {fig_url}")

    st, r = req("POST", f"/api/batches/items/{i0}/approve")
    check("重复 approve → 409（不会入库两道一样的题）", st == 409, f"实为 {st}")
    st, r = req("POST", f"/api/batches/items/{i0}/reject")
    check("驳回已入库的条目 → 409（要先删题）", st == 409, f"实为 {st}")
    st, after = req("GET", f"/api/batches/{job_id}/items")
    check("被挡下的那两次都没改动状态",
          st == 200 and next(x for x in after if x["id"] == i0)["status"] == "approved")

    # ---------------------------------------------------------- ⑦ 驳回 / 退回
    print("\n==== ⑧ 驳回与退回 ====")
    st, r = req("POST", f"/api/batches/items/{i1}/reject")
    check("reject 返回 200 且状态变 rejected",
          st == 200 and r["item"]["status"] == "rejected")
    check("驳回时记了审核时间", bool(r["item"]["reviewed_at"]) if st == 200 else False)
    st, after = req("GET", f"/api/batches/{job_id}/items")
    check("驳回后条目还在（留档，不删）", st == 200 and len(after) == 4)

    st, r = req("POST", f"/api/batches/items/{i1}/reset")
    check("reset 退回 pending 且清掉审核时间",
          st == 200 and r["item"]["status"] == "pending" and r["item"]["reviewed_at"] is None)

    st, r = req("POST", "/api/batches/items/nope/reject")
    check("操作不存在的条目 → 404", st == 404, f"实为 {st}")

    st, j = req("GET", f"/api/batches/{job_id}")
    check("任务汇总计数对得上：approved=1 / rejected=0 / pending=3",
          st == 200 and (j["approved"], j["rejected"], j["pending"]) == (1, 0, 3),
          f"实为 {(j.get('approved'), j.get('rejected'), j.get('pending'))}")

    # ---------------------------------------------------------- ⑧ 重跑护栏
    print("\n==== ⑨ 重跑：护栏必须挡住「白白再烧一遍 API」====")
    st, r = req("POST", f"/api/batches/{job_id}/rerun?only_failed=true")
    check("没有失败项 → 422（不发起任何识别）", st == 422, f"实为 {st}")
    st, j2 = req("GET", f"/api/batches/{job_id}")
    check("被挡下时任务状态没被改动", st == 200 and j2["status"] == "queued")
    st, r = req("POST", "/api/batches/nope/rerun")
    check("重跑不存在的任务 → 404", st == 404, f"实为 {st}")

    # ---------------------------------------------------------- ⑨ 删任务
    print("\n==== ⑩ 删任务：引用计数决定谁被回收 ====")
    st, after = req("GET", f"/api/batches/{job_id}/items")
    approved = [x for x in after if x["status"] == "approved"]
    keep = crop_file(approved[0]["image"]) if approved else None
    keep_fig = (crop_file(approved[0]["figure_image"])
                if approved and approved[0].get("figure_image") else None)
    loose = [crop_file(x["image"]) for x in after if x["status"] != "approved"]

    st, r = req("DELETE", f"/api/batches/{job_id}")
    check("DELETE 返回 200", st == 200, f"实为 {st} {str(r)[:120]}")
    state["jobs"].remove(job_id)
    check("正好回收 3 张（4 张原貌图里那张被题目引用的留住）",
          r.get("crops_removed") == 3, f"实为 {r.get('crops_removed')}")
    if keep is not None:
        check("被已入库题目引用的那张**没被删**", keep.exists(), str(keep))
    if keep_fig is not None:
        # ★ 配图也在引用计数里。漏了它，删任务会把老师刚框好的配图一起抹掉，
        #   而返回的 crops_removed 只多不少，看不出少了什么。
        check("已入库题目的**配图**也没被删（配图在引用计数里）",
              keep_fig.exists(), str(keep_fig))
    still = [p for p in loose if p.exists()]
    check("没人再引用的那些被回收了", not still,
          "剩下了 " + ", ".join(p.name for p in still))

    st, r = req("GET", f"/api/batches/{job_id}")
    check("任务查不到了（404）", st == 404, f"实为 {st}")
    st, r = req("GET", f"/api/batches/{job_id}/items")
    check("条目也跟着查不到了", st == 404, f"实为 {st}")
    st, r = req("DELETE", f"/api/batches/{job_id}")
    check("重复删 → 404", st == 404, f"实为 {st}")

    if qid:
        st, qs = req("GET", "/api/questions")
        check("删任务**没有**带走已入库的题目",
              st == 200 and any(x["id"] == qid for x in qs))

    st, r = req("GET", "/api/batches")
    check("任务列表长度回到测试之前", st == 200 and len(r) == state["jobs_before"],
          f"{len(r)} vs 之前 {state['jobs_before']}")


# ==================================================================== 收尾
def cleanup(state: dict) -> None:
    print("\n==== ⑪ 收尾与对账 ====")
    for jid in list(state["jobs"]):
        st, _ = req("DELETE", f"/api/batches/{jid}")
        print(f"   清掉任务 {jid} → {st}")
    for qid in list(state["questions"]):
        st, r = req("DELETE", f"/api/questions/{qid}")
        n = r.get("crops_removed") if isinstance(r, dict) else "?"
        print(f"   清掉题目 {qid} → {st}（回收截图 {n}）")

    # 兜底：任务/题目都被清掉后，测试造过的图不该有任何一张还留在盘上。
    # ⚠️ 动手前先问一次题库「还有没有人在用这张」—— 万一上面删题那步失败了，
    #    这里把图删了就会在库里留下一个指向空文件的 image。
    st, qs = req("GET", "/api/questions")
    used: set[str] = set()
    if st == 200:
        for q in qs:
            # figure_image 必须算进去：漏了它，兜底会删掉题库还在引用的配图，
            # 库里就留下一个指向空文件的字段
            used |= {crop_file(u).name
                     for u in (q.get("image"), q.get("answer_image"), q.get("figure_image")) if u}

    left = []
    for name in state["crops"]:
        p = config.CROPS_DIR / name
        if not p.exists():
            continue
        if name in used:
            left.append(f"{name}（题库还在引用 —— 说明上面删题那步失败了）")
            continue
        try:
            p.unlink()
            print(f"   兜底删掉 {name}")
        except OSError as e:
            left.append(f"{name}: {e}")
    check("测试造过的图一张都没留在盘上", not left,
          "" if not left else "；".join(left))

    check("截图目录回到测试前的数量", crop_count() == state["crops_before"],
          f"{crop_count()} vs 之前 {state['crops_before']}")
    st, r = req("GET", "/api/batches")
    check("库里没有残留的测试任务", st == 200 and len(r) == state["jobs_before"],
          f"实为 {len(r) if st == 200 else '?'}")
    st, qs = req("GET", "/api/questions")
    check("题库题目数回到测试之前",
          st == 200 and len(qs) == state["questions_before"],
          f"{len(qs) if st == 200 else '?'} vs 之前 {state['questions_before']}")

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
    else:
        print("✅ 全部通过")


def main() -> int:
    st, jobs = req("GET", "/api/batches")
    st2, qs = req("GET", "/api/questions")
    state = {
        "jobs": [], "questions": [], "crops": [],
        "jobs_before": len(jobs) if st == 200 else 0,
        "questions_before": len(qs) if st2 == 200 else 0,
        "crops_before": crop_count(),
    }
    print(f"   跑前：任务 {state['jobs_before']} 个 / 题目 {state['questions_before']} 道 "
          f"/ 截图 {state['crops_before']} 张")

    try:
        run(state)
    except Exception:                                    # noqa: BLE001
        traceback.print_exc()
        check("测试过程没有抛异常", False)
    finally:
        cleanup(state)

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
