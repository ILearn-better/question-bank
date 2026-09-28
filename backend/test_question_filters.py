# -*- coding: utf-8 -*-
r"""按体系筛题：可选的 curriculum_id 有没有真的改变结果与计数。

跑在**独立临时服务器**（8001，自己的 DB + 自己的 uploads）上 —— 会造题、改题。

    cd backend
    $scratch = "$env:TEMP\notes_scratch"
    $env:SHIKE_DATA_DIR = $scratch
    $env:SHIKE_DB_PATH = "$scratch\notes.db"
    .\.venv\Scripts\python.exe -m uvicorn main:app --port 8001

想验的其实只有一件事：**下拉上写的次数要和筛出来的结果对得上**。
选了体系之后标签/知识点仍显示全库次数的话，界面会显示「错题重练（5）」
而结果只有 1 条 —— 老师会以为搜索坏了。所以断言里把「计数 == 实际搜出来的条数」也写上了。
"""
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

S = "http://127.0.0.1:8001"
DB = Path(os.environ["TEMP"]) / "notes_scratch" / "notes.db"

fails = []


def check(label, got, want=True):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


def get(path):
    r = urllib.request.Request(S + urllib.parse.quote(path, safe="/?=&"), method="GET")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read().decode("utf-8", "replace")}


print("==== ⓪ 用现成的两个体系造题：DSE 侧 2 道、另一个体系 1 道 ====")
con = sqlite3.connect(str(DB))
con.execute("delete from questions")
# ⚠️ 不动 curricula：这个临时库是和别的套件共用的（本脚本自己造的数据自己清，
#    别人的东西一概不碰）。体系 id 从库里读，别写死 —— 写死了换个库就错位。
ids = [r[0] for r in con.execute("select id from curricula order by id").fetchall()]
while len(ids) < 2:
    con.execute("insert into curricula (owner_id, code, name, subject, sort_order)"
                " values (1, ?, ?, 'math', 0)", (f"test-c{len(ids)}", f"测试体系{len(ids) + 1}"))
    con.commit()
    ids = [r[0] for r in con.execute("select id from curricula order by id").fetchall()]
A, B = ids[0], ids[1]
data = [
    ("fq1", A, "DSE 二次函数求顶点", "解答题", '["函数"]', '["课堂练习"]'),
    ("fq2", A, "DSE 二次函数图像", "解答题", '["函数"]', '["课堂练习"]'),
    ("fq3", B, "国内：三角比化简", "解答题", '["三角函数"]', '["课后作业"]'),
]
for qid, cid, content, qtype, kps, tags in data:
    con.execute("insert into questions (id, owner_id, content, qtype, difficulty,"
                " knowledge_points, tags, curriculum_id, usage_count)"
                " values (?,1,?,?,?,?,?,?,0)", (qid, content, qtype, "中档", kps, tags, cid))
con.commit()
con.close()
print(f"     体系 A={A}（DSE 侧 2 道）、B={B}（1 道）")

print("==== ① 不传体系：全库（出卷页现在就是这么用的，行为不能变） ====")
allt = get("/api/questions/tags")["items"]
allk = get("/api/questions/knowledge-points")["items"]
check("全库标签 = 课堂练习2 / 课后作业1", {i["tag"]: i["count"] for i in allt},
      {"课堂练习": 2, "课后作业": 1})
check("全库知识点 = 函数2 / 三角函数1", {i["kp"]: i["count"] for i in allk},
      {"函数": 2, "三角函数": 1})
check("全库共 3 道", get("/api/questions/search")["total"], 3)

print("==== ② 传体系：结果与计数都只算这个体系 ====")
dse_t = get(f"/api/questions/tags?curriculum_id={A}")["items"]
dse_k = get(f"/api/questions/knowledge-points?curriculum_id={A}")["items"]
cn_t = get(f"/api/questions/tags?curriculum_id={B}")["items"]
check("A 的标签只剩课堂练习2", {i["tag"]: i["count"] for i in dse_t}, {"课堂练习": 2})
check("A 的知识点只剩函数2", {i["kp"]: i["count"] for i in dse_k}, {"函数": 2})
check("B 的标签 = 课后作业1", {i["tag"]: i["count"] for i in cn_t}, {"课后作业": 1})
check("A 搜出 2 道", get(f"/api/questions/search?curriculum_id={A}")["total"], 2)
check("B 搜出 1 道", get(f"/api/questions/search?curriculum_id={B}")["total"], 1)
check("A 搜出来的都是 A 的",
      {q["curriculum_id"] for q in get(f"/api/questions/search?curriculum_id={A}")["items"]}, {A})

print("==== ③ 计数要和「按这个标签搜出来」的条数一致（界面上就是并排显示的） ====")
for item in dse_t:
    n = get(f"/api/questions/search?curriculum_id={A}&tags={item['tag']}")["total"]
    check(f"标签「{item['tag']}」在 A 里的计数 == 搜出来的条数", item["count"], n)
for item in dse_k:
    n = get(f"/api/questions/search?curriculum_id={A}&kp={item['kp']}")["total"]
    check(f"知识点「{item['kp']}」在 A 里的计数 == 搜出来的条数", item["count"], n)

print("==== ④ 体系 + 标签/关键字一起用 ====")
r = get(f"/api/questions/search?curriculum_id={B}&tags=课后作业&keyword=三角")
check("B + 课后作业 + 关键字「三角」→ 1 道", r["total"], 1)
check("B + 课堂练习（那是 A 的标签）→ 0 道",
      get(f"/api/questions/search?curriculum_id={B}&tags=课堂练习")["total"], 0)
check("关键字搜不因体系而漏（A 里搜「顶点」→ 1 道）",
      get(f"/api/questions/search?curriculum_id={A}&keyword=顶点")["total"], 1)

print()
if fails:
    print(f"❌ {len(fails)} 项没过：" + "；".join(fails))
else:
    print("✅ ALL PASS")
