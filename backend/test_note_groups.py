# -*- coding: utf-8 -*-
r"""一级分组跟体系解绑：能建 / 能改名 / 能删 / 能排序，且不牵连别处。

跑在**独立临时服务器**上（8001，自己的 DB + 自己的 uploads）——
跟 test_notes_transfer.py / test_doc_import.py 用同一个临时库：
一级分组会被建/改名/删，这些都不能在真库上试。

    cd backend
    $scratch = "$env:TEMP\notes_scratch"
    $env:SHIKE_DATA_DIR = $scratch
    $env:SHIKE_DB_PATH = "$scratch\notes.db"
    .\.venv\Scripts\python.exe -m uvicorn main:app --port 8001

脚本自己造、自己删，可反复跑。真库（8000）**只读**两处：
  · 核对「改分组名不会连带改体系表」—— 这是本次解绑的核心断言
  · 核对真库那几行分组还在、名字没变（我确实没碰它）
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
SCRATCH_DB = Path(os.environ["TEMP"]) / "notes_scratch" / "notes.db"

fails = []


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + urllib.parse.quote(path, safe="/?=&"),
                                 data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def check(label, got, want):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


def roots(base=S):
    return call(base, "GET", "/api/note-folders/tree")[1]["roots"]


def root_id(name, base=S):
    return next((r["id"] for r in roots(base) if r["name"] == name), None)


def notes(base=S):
    return {x["id"]: x for x in call(base, "GET", "/api/notes")[1]["items"]}


print("⓪ 先把临时库清干净（这个测试必须能反复跑）")
# 真库基线：这个套件对真库只发 GET，所以开始与结束时的数字必须一样。
# ⚠️ 别把真库的数字写死（“应该是 3 篇”）—— 用户随时会加笔记、改体系名，
#    那种断言迟早会假报错，而假报错会让人养成「红了也先不管」的毛病。
real_notes_at_start = len(call(REAL, "GET", "/api/notes")[1]["items"])
real_roots_at_start = sorted(r["name"] for r in roots(REAL))
if SCRATCH_DB.exists():
    con = sqlite3.connect(str(SCRATCH_DB))
    con.execute("delete from notes")
    con.execute("delete from note_folders where is_unfiled = 0")
    con.commit()
    con.close()
print("     已清空（只留「未归档」那一行）")

print("\n① 全新库：只有一个「未归档」分组 —— 系统不再替你决定该有哪些")
r0 = roots()
check("分组列表", [r["name"] for r in r0], ["未归档"])
check("未归档带 is_unfiled 标记", r0[0]["is_unfiled"], True)
check("体系表照样有 4 个（但笔记这边不跟着造分组了）",
      len(call(S, "GET", "/api/curricula")[1]), 4)

print("\n② 新建分组：不传 parent_id 即可")
st, g = call(S, "POST", "/api/note-folders", {"name": "IB 数学"})
check("HTTP", st, 200)
check("新建的排最前（界面上输入框就在最上面）", roots()[0]["name"], "IB 数学")
check("未归档仍在最后", roots()[-1]["name"], "未归档")
dse = call(S, "POST", "/api/note-folders", {"name": "DSE 数学"})[1]
check("再建一个（与某个体系同名是允许的）", roots()[0]["name"], "DSE 数学")

print("\n③ 重名与保留名")
check("重名自动加 2", call(S, "POST", "/api/note-folders", {"name": "IB 数学"})[1]["name"], "IB 数学 2")
check("叫「未归档」→ 422", call(S, "POST", "/api/note-folders", {"name": "未归档"})[0], 422)
check("空名字 → 422", call(S, "POST", "/api/note-folders", {"name": "   "})[0], 422)

print("\n④ ★ 改分组名只改笔记这一边：体系表一个字都不动")
real_before = [(c["id"], c["name"]) for c in call(REAL, "GET", "/api/curricula")[1]]
scratch_before = [(c["id"], c["name"]) for c in call(S, "GET", "/api/curricula")[1]]
check("改名 HTTP", call(S, "PATCH", f"/api/note-folders/{dse['id']}", {"name": "DSE 数学（笔记叫法）"})[0], 200)
check("树上换成新名字", root_id("DSE 数学（笔记叫法）"), dse["id"])
check("临时库体系表没被动", [(c["id"], c["name"]) for c in call(S, "GET", "/api/curricula")[1]],
      scratch_before)
check("★ 真库体系表一个字没动", [(c["id"], c["name"]) for c in call(REAL, "GET", "/api/curricula")[1]],
      real_before)
check("分组不因此挂上 curriculum_id",
      [r["curriculum_id"] for r in roots() if r["id"] == dse["id"]], [None])

print("\n⑤ 排序：position 在分组之间生效，「未归档」固定最后")
ib = root_id("IB 数学")
print("     挪之前：", [r["name"] for r in roots()])
# position 的语义跟旧目录一模一样：去捶被拖的那个之后，它排在第几位（0-based）
check("把 IB 挪回第 2 位", call(S, "PATCH", f"/api/note-folders/{ib}", {"position": 1})[0], 200)
check("顺序", [r["name"] for r in roots()],
      ["IB 数学 2", "IB 数学", "DSE 数学（笔记叫法）", "未归档"])
check("想让未归档往前 → 被拒", call(S, "PATCH", f"/api/note-folders/{root_id('未归档')}",
                                {"position": 0})[0], 422)
check("未归档永远最后", roots()[-1]["name"], "未归档")

print("\n⑥ 边界：一级分组不能被放进目录；未归档不能改/排/删")
folder = call(S, "POST", "/api/note-folders", {"parent_id": dse["id"], "name": "子目录"})[1]
check("分组放进目录 → 422", call(S, "PATCH", f"/api/note-folders/{ib}", {"parent_id": folder["id"]})[0], 422)
check("目录放进自己 → 422", call(S, "PATCH", f"/api/note-folders/{folder['id']}",
                                {"parent_id": folder["id"]})[0], 422)
unf = root_id("未归档")
check("未归档改名 → 422", call(S, "PATCH", f"/api/note-folders/{unf}", {"name": "别的"})[0], 422)
check("未归档删除 → 422", call(S, "DELETE", f"/api/note-folders/{unf}")[0], 422)

print("\n⑦ 删分组：内容一样都不删，全部移到「未归档」")
sub = call(S, "POST", "/api/note-folders", {"parent_id": ib, "name": "里层"})[1]
n1 = call(S, "POST", "/api/notes", {"title": "分组里的笔记", "content": "# a\n", "folder_id": ib})[1]["id"]
n2 = call(S, "POST", "/api/notes", {"title": "里层的笔记", "content": "# b\n", "folder_id": sub["id"]})[1]["id"]
st, rep = call(S, "DELETE", f"/api/note-folders/{ib}")
check("HTTP", st, 200)
check("报告了移走几样", (rep["moved_folders"], rep["moved_notes"]), (1, 1))
check("去了哪", rep["to"], "未归档")
left = notes()
check("分组里的笔记挂到未归档", left[n1]["folder_id"], unf)
check("里层的笔记还在里层", left[n2]["folder_id"], sub["id"])
check("里层目录上移到了未归档",
      len([c for r in roots() if r["id"] == unf for c in r["children"] if c["id"] == sub["id"]]), 1)
check("两篇笔记都还在", len([x for x in left.values() if x["id"] in (n1, n2)]), 2)
check("分组没了", root_id("IB 数学"), None)

print("\n⑧ 分组名会出现在搜索路径里（不再从体系现取）")
lst = call(S, "GET", "/api/notes?keyword=笔记")[1]["items"]
paths = sorted(x["path"] for x in lst)
print("     删完分组后：", paths)
check("被删分组的笔记现在跟在未归档下", paths, ["未归档", "未归档 / 里层"])
# 再在一级分组下放一篇，证明路径的首段就是分组名（而不是体系名）
n4 = call(S, "POST", "/api/notes",
          {"title": "分组名路径测试", "content": "# d\n", "folder_id": dse["id"]})[1]["id"]
path4 = [x["path"] for x in call(S, "GET", "/api/notes?keyword=分组名路径")[1]["items"]]
check("路径首段 = 分组名", path4, ["DSE 数学（笔记叫法）"])
check("确实不是体系名", [p for p in path4 if p.startswith("DSE 数学 ")], [])

print("\n⑨ 回归：普通目录的重名合并、父子环、删目录不删笔记")
a = call(S, "POST", "/api/note-folders", {"parent_id": dse["id"], "name": "甲"})[1]
b = call(S, "POST", "/api/note-folders", {"parent_id": a["id"], "name": "乙"})[1]
check("目录重名加 2", call(S, "POST", "/api/note-folders", {"parent_id": dse["id"], "name": "甲"})[1]["name"], "甲 2")
check("父放进子 → 422", call(S, "PATCH", f"/api/note-folders/{a['id']}",
                            {"parent_id": b["id"]})[0], 422)
n3 = call(S, "POST", "/api/notes", {"title": "甲的笔记", "content": "# c\n", "folder_id": a["id"]})[1]["id"]
rep2 = call(S, "DELETE", f"/api/note-folders/{a['id']}")[1]
check("删目录报移走 1 篇", rep2["moved_notes"], 1)
check("移到父目录", notes()[n3]["folder_id"], dse["id"])

print("\n⑩ 真库（只读核对）：分组行还在，而且全程没被我改过")
real_roots = [r["name"] for r in roots(REAL)]
print("     ", real_roots)
# 「未归档」固定排最后（新规则；以前它在数据里是 sort_order=0，所以显示在最前）
check("真库最后一行是「未归档」", real_roots[-1], "未归档")
# 这里以前写的是「非未归档的分组 >= 4」（假定真库有 4 个体系分组）。
# 真库实际只有「未归档」一个顶层分组，于是这条**永远红** —— 正是上面
# 「别把真库的数字写死」那句注释要防的事。改成跟「开始时」比个数。
check("真库的分组一个没少（名字归用户改，只数个数）",
      len(real_roots), len(real_roots_at_start))
check("真库分组名与开始时一字不差", sorted(real_roots), real_roots_at_start)
check("真库笔记数没变（全程只读过它）",
      len(call(REAL, "GET", "/api/notes")[1]["items"]), real_notes_at_start)

print()
print("ALL PASS" if not fails else f"{len(fails)} 项失败: {fails}")


# 退出码即结果（0 = 全过）。以前这里只打印不设码 —— 单跑时人看得出来，
# 但脚本化批量回归会把失败当成通过，静默漏掉一整轮。
raise SystemExit(1 if fails else 0)
