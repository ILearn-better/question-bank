# -*- coding: utf-8 -*-
r"""卷种样式模板的接口端到端（P2）：CRUD + 带模板导出。

和 `test_paper_style.py` 的分工：
  · `test_paper_style.py` —— **纯函数**，测解析 / 分区归组 / 三种格式的渲染函数，不起服务
  · 本文件 —— **打 HTTP 接口**，测复制 / 改名 / 删除 / 导出这些真实链路

⚠️ 跑之前 **8000 的服务必须在运行**：
    cd backend && ./.venv/Scripts/python.exe -m uvicorn main:app --port 8000
本文件会临时插几道题来验证导出，题目内容都带 `_P2TEST_` 标记，跑完自动删干净；
即使中途报错，`finally` 里也会清。

    cd backend && ./.venv/Scripts/python.exe test_paper_templates.py
"""
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

BASE = "http://127.0.0.1:8000"
fails = []
created = []


def req(method, path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            if "json" in resp.headers.get("Content-Type", ""):
                return resp.status, json.loads(raw)
            return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:  # noqa: BLE001
            return e.code, None
    except urllib.error.URLError as e:
        print(f"\n❌ 连不上 {BASE} —— 请先启动服务：\n"
              f"   cd backend && ./.venv/Scripts/python.exe -m uvicorn main:app --port 8000\n"
              f"   原因：{e.reason}")
        sys.exit(2)


def check(label, got, want):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}" + ("" if okk else f"  得到 {got!r} 期望 {want!r}"))
    if not okk:
        fails.append(label)


def truthy(label, got):
    okk = bool(got)
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}")
    if not okk:
        fails.append(label)


def export(ids, fmt="html", tid=None, title="测试卷"):
    p = f"/api/papers/export?ids={ids}&format={fmt}&title={quote(title)}"
    if tid:
        p += f"&template_id={tid}"
    return req("GET", p)


try:
    # ------------------------------------------------------------ 列表
    print("==== ① 四套内置样式 ====")
    st, d = req("GET", "/api/paper-templates")
    check("HTTP 200", st, 200)
    tpls = d["items"]
    check("四套内置", len(tpls), 4)
    check("顺序", [t["code"] for t in tpls], ["gaokao", "zhongkao", "dse", "alevel"])
    print("     名字:", [t["name"] for t in tpls])
    gk = tpls[0]
    check("返回里带**解析后**的三大块",
          all(k in gk for k in ("paper", "style", "sections")), True)
    check("高考抬头解析出来了", bool(gk["paper"]["subtitle"]), True)
    check("DSE 分区按难度（不是题型）",
          all(s["difficulties"] for s in tpls[2]["sections"]), True)
    check("A-Level 题号是 Question N", tpls[3]["style"]["number_style"], "Question 1")

    print("\n==== ② 单条读取 ====")
    st, one = req("GET", f"/api/paper-templates/{gk['id']}")
    check("HTTP 200", st, 200)
    check("名字一致", one["name"], gk["name"])
    check("不存在 → 404", req("GET", "/api/paper-templates/999999")[0], 404)

    print("\n==== ③ 复制（改样式的正确入口）====")
    st, cp = req("POST", "/api/paper-templates/copy",
                 {"source_id": gk["id"], "name": "XX 中学月考"})
    check("HTTP 200", st, 200)
    check("副本不是内置", cp["is_builtin"], 0)
    check("继承了抬头", cp["paper"]["subtitle"], gk["paper"]["subtitle"])
    check("继承了题号形态", cp["style"]["number_style"], gk["style"]["number_style"])
    check("继承了分区数", len(cp["sections"]), len(gk["sections"]))
    st, cp2 = req("POST", "/api/paper-templates/copy", {"source_id": gk["id"]})
    check("不传名字 → 自动带（副本）", cp2["name"], f"{gk['name']}（副本）")
    st, cp3 = req("POST", "/api/paper-templates/copy", {"source_id": gk["id"]})
    check("再复制 → 自动加序号", cp3["name"], f"{gk['name']}（副本） 2")

    print("\n==== ④ 改名（没传的块不能被重置）====")
    st, patched = req("PATCH", f"/api/paper-templates/{cp['id']}", {"name": "改过名字"})
    check("HTTP 200", st, 200)
    check("名字改了", patched["name"], "改过名字")
    check("没传 style → 排版没被重置", patched["style"]["number_style"], cp["style"]["number_style"])
    check("没传 paper → 抬头还在", patched["paper"]["subtitle"], cp["paper"]["subtitle"])

    print("\n==== ⑤ 改样式与越界值收敛 ====")
    st, p2 = req("PATCH", f"/api/paper-templates/{cp['id']}",
                 {"style": {"number_style": "（1）", "font_size": 14}})
    check("题号形态改成功", p2["style"]["number_style"], "（1）")
    check("字号改成功", p2["style"]["font_size"], 14)
    check("字号 999 越界 → 退回默认 11.5",
          req("PATCH", f"/api/paper-templates/{cp['id']}", {"style": {"font_size": 999}})[1]["style"]["font_size"],
          11.5)

    print("\n==== ⑥ 内置可改不可删（与反馈模板同口径）====")
    st, body = req("DELETE", f"/api/paper-templates/{gk['id']}")
    check("删内置 → 400", st, 400)
    print("     提示语:", (body or {}).get("detail"))

    print("\n==== ⑦ 删除自己复制的 ====")
    for t in (cp, cp2, cp3):
        check(f"删「{t['name']}」→ 200", req("DELETE", f"/api/paper-templates/{t['id']}")[0], 200)
    check("删完只剩四套内置", len(req("GET", "/api/paper-templates")[1]["items"]), 4)

    # ------------------------------------------------------------ 导出
    print("\n==== ⑧ 造 3 道临时题（选择题/填空题/解答题）====")
    specs = [
        {"document_id": "_P2TEST_", "qtype": "选择题", "difficulty": "基础", "score": 5,
         "content": r"_P2TEST_ 已知 $f(x)=x^2-2x$，则最小值为（　）",
         "answer": "B", "analysis": r"配方得 $(x-1)^2-1$", "tags": ["函数"]},
        {"document_id": "_P2TEST_", "qtype": "填空题", "difficulty": "中档", "score": 5,
         "content": r"_P2TEST_ 若 $\frac{a}{b}=2$，则 $\frac{a+b}{b}=$ ______",
         "answer": "3", "tags": ["代数"]},
        {"document_id": "_P2TEST_", "qtype": "解答题", "difficulty": "中档", "score": 12,
         "content": r"_P2TEST_ 求证：$\sin^2\theta+\cos^2\theta=1$",
         "answer": "见解析", "analysis": "用单位圆", "tags": ["三角"]},
    ]
    for s in specs:
        st, q = req("POST", "/api/questions", s)
        check(f"建题 {s['qtype']}", st in (200, 201), True)
        created.append(q["id"])
    rows = req("GET", "/api/questions")[1]
    by_id = {q["id"]: q for q in (rows if isinstance(rows, list) else rows.get("items", []))}
    for s, qid in zip(specs, created):
        check(f"  {s['qtype']} 的 score 落库", by_id.get(qid, {}).get("score"), s["score"])
    ids = ",".join(created)

    print("\n==== ⑨ 同一批题切四套样式 ====")
    for label, idx, sec, num in [
        ("高考", 0, "一、选择题", "<b>1.</b>"),
        ("DSE", 2, "Section A(1)", "<b>1.</b>"),
        ("A-Level", 3, "Section A", "<b>Question 1</b>"),
    ]:
        tid = tpls[idx]["id"]
        st, body = export(ids, "html", tid)
        check(f"{label} 导出 → 200", st, 200)
        text = body.decode("utf-8") if isinstance(body, bytes) else ""
        truthy(f"{label} 含分区标题「{sec}」", sec in text)
        truthy(f"{label} 题号是 {num}", num in text)
        truthy(f"{label} 带 KaTeX 自动渲染", "renderMathInElement" in text)

    print("\n==== ⑩ 分值：中文卷 vs 英文卷写法不同 ====")
    gk_t = export(ids, "html", tpls[0]["id"], "T")[1].decode()
    al_t = export(ids, "html", tpls[3]["id"], "T")[1].decode()
    truthy("高考分区说明带「每小题 5 分」", "每小题 5 分" in gk_t)
    truthy("高考不逐题标分值", "（5分）" not in gk_t)
    truthy("A-Level 逐题标西文 (5 marks)", "(5 marks)" in al_t)

    print("\n==== ⑪ 三大格式都出得来 ====")
    for f in ("html", "docx", "pdf"):
        st, body = export(ids, f, tpls[0]["id"])
        check(f"{f} → 200", st, 200)
        truthy(f"{f} 内容非空（{len(body or b'')} 字节）", len(body or b"") > 1500)

    print("\n==== ⑫ 不传模板 = 与加样式之前一致（回归）====")
    st, body = export(ids, "html")
    check("200", st, 200)
    t = body.decode()
    truthy("不印分区标题", "一、选择题" not in t)
    truthy("题号仍是 1.", "<b>1.</b>" in t)

    print("\n==== ⑬ 不存在的模板 → 404，不静默降级 ====")
    check("404", export(ids, "html", 999999)[0], 404)

finally:
    print("\n==== 清理临时题 ====")
    for i in created:
        req("DELETE", f"/api/questions/{i}")
    rows = req("GET", "/api/questions")[1]
    left = [q for q in (rows if isinstance(rows, list) else rows.get("items", []))
            if "_P2TEST_" in (q.get("content") or "")]
    print(f"     删了 {len(created)} 道，残留 {len(left)} 道")
    if left:
        fails.append("临时题有残留")

print("\n" + "=" * 60)
if fails:
    print(f"❌ {len(fails)} 项未通过：")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
