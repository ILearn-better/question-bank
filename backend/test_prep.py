# -*- coding: utf-8 -*-
r"""课前备课：从「写四段 + 挑材料」到「存成笔记」的全流程。

跑在**独立临时服务器**上（8001，自己的 DB + 自己的 uploads）——
会建学生/课时/备课/笔记，这些都不能在真库上试。

    cd backend
    $scratch = "$env:TEMP\notes_scratch"
    $env:SHIKE_DATA_DIR = $scratch
    $env:SHIKE_DB_PATH = "$scratch\notes.db"
    .\.venv\Scripts\python.exe -m uvicorn main:app --port 8001

脚本自己造、自己删，可反复跑（真库只读核对学生数）。

验的重点不是 CRUD，而是那两条容易做歪的：
  ① 结构化四段 + 挑的材料能正确入库、读回（含标题快照）。
  ② 「存成笔记」的双向联动：新建一篇 / 更新已关联的那篇，且 Markdown 里四段与材料都在。
"""
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


print("⓪ 清空临时库，再造两道题")
con = sqlite3.connect(str(DB))
con.execute("delete from lesson_prep_items")
con.execute("delete from lesson_preps")
con.execute("delete from lesson_supplement_items")
con.execute("delete from lesson_supplements")
con.execute("delete from lessons")
con.execute("delete from students")
con.execute("delete from notes")
con.execute("delete from note_folders where is_unfiled = 0")
con.execute("delete from questions")
for qid, content, answer in [("pq1", "解方程：2x+3=7", "x=2"),
                             ("pq2", "解方程：3x-1=8", "x=3")]:
    con.execute("insert into questions (id, owner_id, content, answer, qtype, difficulty,"
                " knowledge_points, tags, usage_count) values (?,1,?,?,?,?,?,?,0)",
                (qid, content, answer, "解答题", "中档", '["一元一次方程"]', '[]'))
con.commit()
con.close()
print("     造好 pq1 / pq2")

print("\n① 一个学生 + 一节课 + 一篇参考笔记")
st, stu, _ = js(S, "POST", "/api/students", {"name": "备课测试生", "grade": "初一"})
check("建学生", st, 200)
sid = stu["id"]
st, les, _ = js(S, "POST", "/api/lessons", {
    "student_id": sid, "start_at": "2026-09-29T15:00", "duration_min": 60,
    "topic": "一元一次方程", "status": "scheduled"})
check("建课时", st, 200)
lid = les["id"]
st, ref_note, _ = js(S, "POST", "/api/notes", {"title": "方程讲义草稿", "content": "# 旧讲义\n"})
check("建参考笔记", st, 200)
ref_nid = ref_note["id"]

print("\n② 存备课：四段 + 3 样材料（2 题 + 1 篇新笔记）")
st, mnote, _ = js(S, "POST", "/api/notes", {"title": "去括号口诀", "content": "先去小再中后大"})
mnid = mnote["id"]
st, prep, _ = js(S, "PUT", f"/api/lessons/{lid}/prep", {
    "goal": "会解一元一次方程",
    "key_points": "移项要变号",
    "flow": "先讲移项，再练 5 题",
    "materials": "打印讲义 2 页",
    "items": [{"kind": "question", "ref_id": "pq1"},
              {"kind": "question", "ref_id": "pq2"},
              {"kind": "note", "ref_id": mnid}],
})
check("HTTP", st, 200)
check("四段都存了", [prep["prep"]["goal"], prep["prep"]["key_points"],
                  prep["prep"]["flow"], prep["prep"]["materials"]],
      ["会解一元一次方程", "移项要变号", "先讲移项，再练 5 题", "打印讲义 2 页"])
check("标题快照", [i["title"] for i in prep["prep"]["items"]],
      ["解方程：2x+3=7", "解方程：3x-1=8", "去括号口诀"])
check("题目 id 分开给了", prep["prep"]["questions"], ["pq1", "pq2"])
check("笔记 id 也给了", prep["prep"]["notes"], [mnid])

print("\n③ 读回备课")
st, got, _ = js(S, "GET", f"/api/lessons/{lid}/prep")
check("HTTP", st, 200)
check("prep 非空", got["prep"] is not None, True)
check("四段读回一致", got["prep"]["goal"], "会解一元一次方程")

print("\n④ ★ 存成笔记（首次 → 新建）")
st, note_r, _ = js(S, "POST", f"/api/lessons/{lid}/prep/save-as-note")
check("HTTP", st, 200)
check("是新建的", note_r["created"], True)
gen_nid = note_r["note_id"]
check("笔记标题带「备课」", "备课" in note_r["note_title"], True)
st, gen_note, _ = js(S, "GET", f"/api/notes/{gen_nid}")
body = gen_note.get("content", "")
check("正文里有教学目标", "## 教学目标" in body, True)
check("正文里有目标内容", "会解一元一次方程" in body, True)
check("正文里有重点难点", "## 重点难点" in body, True)
check("正文里有材料清单", "这节课要用的材料" in body, True)
check("正文里列了题", "解方程：2x+3=7" in body, True)
check("正文里列了笔记", "去括号口诀" in body, True)
check("备课的 note_id 已回写",
      js(S, "GET", f"/api/lessons/{lid}/prep")[1]["prep"]["note_id"], gen_nid)

print("\n⑤ 从笔记库选取：关联那篇参考笔记")
st, prep2, _ = js(S, "PUT", f"/api/lessons/{lid}/prep", {"note_id": ref_nid})
check("HTTP", st, 200)
check("note_id 指向参考笔记", prep2["prep"]["note_id"], ref_nid)
check("note_title 是参考笔记标题", prep2["prep"]["note_title"], "方程讲义草稿")

print("\n⑥ ★ 再存成笔记（已关联 → 更新那篇，不新建）")
before_notes = sqlite3.connect(str(DB)).execute("select count(*) from notes").fetchone()[0]
st, note_r2, _ = js(S, "POST", f"/api/lessons/{lid}/prep/save-as-note")
check("HTTP", st, 200)
check("不是新建", note_r2["created"], False)
check("note_id 还是那篇参考笔记", note_r2["note_id"], ref_nid)
after_notes = sqlite3.connect(str(DB)).execute("select count(*) from notes").fetchone()[0]
check("笔记总数没涨", after_notes, before_notes)
st, ref_after, _ = js(S, "GET", f"/api/notes/{ref_nid}")
check("参考笔记的正文被覆盖成备课内容", "## 教学目标" in ref_after.get("content", ""), True)

print("\n⑦ items 整组替换")
st, p3, _ = js(S, "PUT", f"/api/lessons/{lid}/prep",
               {"items": [{"kind": "question", "ref_id": "pq1"}]})
check("只留 1 项", len(p3["prep"]["items"]), 1)
st, p4, _ = js(S, "PUT", f"/api/lessons/{lid}/prep", {"items": []})
check("空数组 = 清空材料", len(p4["prep"]["items"]), 0)
check("四段没被动（只传 items）", p4["prep"]["goal"], "会解一元一次方程")

print("\n⑧ 边界")
check("题不存在 → 404", js(S, "PUT", f"/api/lessons/{lid}/prep",
                          {"items": [{"kind": "question", "ref_id": "nope"}]})[0], 404)
check("不认识的类型 → 422", js(S, "PUT", f"/api/lessons/{lid}/prep",
                              {"items": [{"kind": "doc", "ref_id": "x"}]})[0], 422)
check("关联不存在的笔记 → 404", js(S, "PUT", f"/api/lessons/{lid}/prep",
                                  {"note_id": "nope"})[0], 404)
st, p5, _ = js(S, "PUT", f"/api/lessons/{lid}/prep", {"note_id": ""})
check("note_id 空串 = 解除关联", p5["prep"]["note_id"], "")
check("没备课的课时 save-as-note → 404",
      js(S, "POST", "/api/lessons/999999/prep/save-as-note")[0], 404)

print("\n⑨ 删备课：不碰题/笔记/关联笔记")
con = sqlite3.connect(str(DB))
con.execute("update lesson_preps set note_id = ? where lesson_id = ?", (ref_nid, lid))
con.commit()
check("删备课", js(S, "DELETE", f"/api/lessons/{lid}/prep")[0], 200)
check("再查就没了", js(S, "GET", f"/api/lessons/{lid}/prep")[1]["prep"], None)
check("题还在", con.execute("select count(*) from questions where id='pq1'").fetchone()[0], 1)
check("参考笔记还在", con.execute("select count(*) from notes where id=?", (ref_nid,)).fetchone()[0], 1)
con.close()

print("\n⑩ 删课时：备课跟着走")
st, p6, _ = js(S, "PUT", f"/api/lessons/{lid}/prep",
               {"goal": "再写一点", "items": [{"kind": "question", "ref_id": "pq1"}]})
check("重写一份备课", st, 200)
check("删课时", js(S, "DELETE", f"/api/lessons/{lid}")[0], 200)
con = sqlite3.connect(str(DB))
check("备课行没了", con.execute("select count(*) from lesson_preps").fetchone()[0], 0)
check("条目行也没了", con.execute("select count(*) from lesson_prep_items").fetchone()[0], 0)
check("题和笔记都还在", (con.execute("select count(*) from questions").fetchone()[0],
                    con.execute("select count(*) from notes").fetchone()[0]), (2, 3))
con.close()

print("\n⑪ 真库（只读）：没被动过")
st, real_students, _ = js(REAL, "GET", "/api/students")
print("     真库学生数：", len(real_students))
check("真库没有我造的学生", any(s.get("name") == "备课测试生" for s in real_students), False)

print()
print("ALL PASS" if not fails else f"{len(fails)} 项失败: {fails}")


# 退出码即结果（0 = 全过）。
raise SystemExit(1 if fails else 0)
