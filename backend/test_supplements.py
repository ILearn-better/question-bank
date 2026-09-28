# -*- coding: utf-8 -*-
r"""课后补充：从挑材料到「下次上课打印」的全流程。

跑在**独立临时服务器**上（8001，自己的 DB + 自己的 uploads）——
会建学生/课时/批次，这些都不能在真库上试。

    cd backend
    $scratch = "$env:TEMP\notes_scratch"
    $env:SHIKE_DATA_DIR = $scratch
    $env:SHIKE_DB_PATH = "$scratch\notes.db"
    .\.venv\Scripts\python.exe -m uvicorn main:app --port 8001

脚本自己造、自己删，可反复跑（真库只读核对学生数与题库数）。

验的重点不是 CRUD，而是那两条容易做歪的：
  ① **给学生的 PDF 里不能有答案**（真读 PDF 文本找答案，不是看有没有传参数）
  ② 打完就把状态置成「已给」、并把文件落进学生的归档目录（三个月后还能查到当时给了什么）
"""
import io
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REAL = "http://127.0.0.1:8000"
S = "http://127.0.0.1:8001"
SCRATCH = Path(os.environ["TEMP"]) / "notes_scratch"
DB = SCRATCH / "notes.db"

fails = []


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + urllib.parse.quote(path, safe="/?=&,"),
                                 data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def js(base, method, path, body=None):
    st, raw, hdr = call(base, method, path, body)
    try:
        return st, json.loads(raw or b"null"), hdr
    except Exception:
        return st, raw, hdr


def check(label, got, want):
    okk = got == want
    print(("  OK  " if okk else "  FAIL") + f"  {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


print("⓪ 清空临时库（只留一个「未归档」分组），再造两道题")
con = sqlite3.connect(str(DB))
con.execute("delete from lesson_supplement_items")
con.execute("delete from lesson_supplements")
con.execute("delete from lessons")            # 会级联带走反馈/作业/上课文件
con.execute("delete from students")
con.execute("delete from notes")
con.execute("delete from note_folders where is_unfiled = 0")
con.execute("delete from questions")
for qid, content, answer in [("sq1", "因式分解：x^2-1", "(x-1)(x+1)"),
                             ("sq2", "因式分解：x^2+2x+1", "(x+1)^2")]:
    con.execute("insert into questions (id, owner_id, content, answer, qtype, difficulty,"
                " knowledge_points, tags, usage_count) values (?,1,?,?,?,?,?,?,0)",
                (qid, content, answer, "解答题", "中档", '["因式分解"]', '["错题重练"]'))
con.commit()
con.close()
print("     造好 sq1 / sq2")

print("\n① 一个学生 + 一节课")
st, stu, _ = js(S, "POST", "/api/students", {"name": "补充测试生", "grade": "初二"})
check("建学生", st, 200)
sid = stu["id"]
st, les, _ = js(S, "POST", "/api/lessons", {
    "student_id": sid, "start_at": "2026-09-29T15:00", "duration_min": 60,
    "topic": "因式分解", "status": "done"})
check("建课时", st, 200)
lid = les["id"]
st, note, _ = js(S, "POST", "/api/notes", {"title": "因式分解三步法", "content": "# 三步法\n"})
nid = note["id"]

print("\n② 挑一批：2 题 + 1 篇笔记（带知识点与备注）")
st, batch, _ = js(S, "POST", f"/api/lessons/{lid}/supplements", {
    "items": [{"kind": "question", "ref_id": "sq1"},
              {"kind": "question", "ref_id": "sq2"},
              {"kind": "note", "ref_id": nid}],
    "focus": "因式分解", "note": "这两题是课本 P42 的变式"})
check("HTTP", st, 200)
check("状态默认「还没打印」", batch["status_label"], "还没打印")
check("标题快照存下来了", [i["title"] for i in batch["items"]],
      ["因式分解：x^2-1", "因式分解：x^2+2x+1", "因式分解三步法"])
check("题目 id 分开给了（打印要用）", batch["questions"], ["sq1", "sq2"])
check("笔记 id 也给了", batch["notes"], [nid])

print("\n③ 列表 / 待给提醒")
st, lst, _ = js(S, "GET", f"/api/lessons/{lid}/supplements")
check("这节课有 1 批", len(lst["items"]), 1)
check("其中 1 批待给", lst["pending"], 1)
st, pend, _ = js(S, "GET", f"/api/students/{sid}/supplements/pending")
check("待给提醒", pend["count"], 1)
check("提醒里带上课日期", pend["items"][0]["lesson_date"], "2026-09-29")

print("\n④ ★ 打印学生版：PDF 里不能有答案，且要落进归档目录")
st, pdf, hdr = call(S, "POST", f"/api/supplements/{batch['id']}/print")
check("HTTP", st, 200)
check("是 PDF", pdf[:4], b"%PDF")
check("响应头里状态已变", hdr.get("x-supplement-status"), "given")
archived = urllib.parse.unquote(hdr.get("x-supplement-archived", ""))
print("     归档到：", archived)
check("归档路径在学生的日期目录下", archived.startswith(f"students/u{sid}_补充测试生/2026-09-29/"), True)
check("磁盘上真有这个文件", (SCRATCH / "uploads" / archived).is_file(), True)

import pymupdf                                   # venv 里有
doc = pymupdf.open(stream=pdf, filetype="pdf")
text = "\n".join(p.get_text() for p in doc)
doc.close()
check("★ 题干在（PDF 真的排了题）", "因式分解：x^2-1" in text, True)
check("★ 答案不在（学生版不能带答案）", "(x-1)(x+1)" in text or "(x+1)^2" in text, False)

st, lst2, _ = js(S, "GET", f"/api/lessons/{lid}/supplements")
check("打完自动变成已给", lst2["items"][0]["status_label"], "已给")
check("待给归零", lst2["pending"], 0)

print("\n⑤ 状态流转与校验")
st, r, _ = js(S, "PATCH", f"/api/supplements/{batch['id']}", {"status": "returned"})
check("改成已交回", st, 200)
check("查回来的状态", r["status_label"], "已交回")
check("乱传状态 → 422", js(S, "PATCH", f"/api/supplements/{batch['id']}", {"status": "??"})[0], 422)
check("没传的字段保持原样（focus 还在）",
      js(S, "GET", f"/api/lessons/{lid}/supplements")[1]["items"][0]["focus"], "因式分解")

print("\n⑥ 一节课可以有好几批")
st, b2, _ = js(S, "POST", f"/api/lessons/{lid}/supplements",
               {"items": [{"kind": "question", "ref_id": "sq1"}]})
check("再记一批（同一道题也可以）", st, 200)
st, lst3, _ = js(S, "GET", f"/api/lessons/{lid}/supplements")
check("现在是 2 批", len(lst3["items"]), 2)
check("新的一批排在最前", lst3["items"][0]["id"], b2["id"])

print("\n⑦ 边界")
check("空的 items → 422", js(S, "POST", f"/api/lessons/{lid}/supplements", {"items": []})[0], 422)
check("不认识的类型 → 422", js(S, "POST", f"/api/lessons/{lid}/supplements",
                              {"items": [{"kind": "doc", "ref_id": "x"}]})[0], 422)
check("题不存在 → 404", js(S, "POST", f"/api/lessons/{lid}/supplements",
                          {"items": [{"kind": "question", "ref_id": "nope"}]})[0], 404)
st, b3, _ = js(S, "POST", f"/api/lessons/{lid}/supplements",
               {"items": [{"kind": "question", "ref_id": "sq1"}, {"kind": "question", "ref_id": "sq1"}]})
check("同一题选两次只算一项", len(b3["items"]), 1)
check("只有笔记的批次不能打印 → 422",
      js(S, "POST", "/api/supplements/" + str(js(S, "POST", f"/api/lessons/{lid}/supplements",
         {"items": [{"kind": "note", "ref_id": nid}]})[1]["id"]) + "/print")[0], 422)

print("\n⑧ 删题库里的题：记录仍在，但会标明「内容已删」")
con = sqlite3.connect(str(DB))
con.execute("delete from questions where id = 'sq2'")
con.commit()
con.close()
st, lst4, _ = js(S, "GET", f"/api/lessons/{lid}/supplements")
alive = {i["ref_id"]: i["exists"] for b in lst4["items"] for i in b["items"]}
check("被删的题标记为不存在（不是消失）", alive.get("sq2"), False)
check("标题快照还在", [i["title"] for b in lst4["items"] for i in b["items"] if i["ref_id"] == "sq2"],
      ["因式分解：x^2+2x+1"])

print("\n⑨ 删批次：不碰题库与笔记")
dead = st, b4, _ = js(S, "POST", f"/api/lessons/{lid}/supplements",
                      {"items": [{"kind": "question", "ref_id": "sq1"}]})
bid = b4["id"]
check("删掉", js(S, "DELETE", f"/api/supplements/{bid}")[0], 200)
check("再查就没了", js(S, "GET", f"/api/lessons/{lid}/supplements")[1]["items"]
      and all(x["id"] != bid for x in js(S, "GET", f"/api/lessons/{lid}/supplements")[1]["items"]), True)
con = sqlite3.connect(str(DB))
check("题还在题库", con.execute("select count(*) from questions where id='sq1'").fetchone()[0], 1)
check("笔记还在", con.execute("select count(*) from notes where id=?", (nid,)).fetchone()[0], 1)
con.close()

print("\n⑩ 删课时：批次跟着走（它是「这节课的」东西）")
check("删课时", js(S, "DELETE", f"/api/lessons/{lid}")[0], 200)
con = sqlite3.connect(str(DB))
check("批次行也没了", con.execute("select count(*) from lesson_supplements").fetchone()[0], 0)
check("条目行也没了", con.execute("select count(*) from lesson_supplement_items").fetchone()[0], 0)
check("题和笔记都还在", (con.execute("select count(*) from questions").fetchone()[0],
                    con.execute("select count(*) from notes").fetchone()[0]), (1, 1))
con.close()

print("\n⑪ 真库（只读）：没被动过")
st, real_students, _ = js(REAL, "GET", "/api/students")
print("     真库学生数：", len(real_students))
check("真库没有我造的学生", any(s.get("name") == "补充测试生" for s in real_students), False)

print()
print("ALL PASS" if not fails else f"{len(fails)} 项失败: {fails}")
