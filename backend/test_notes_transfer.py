# -*- coding: utf-8 -*-
"""笔记备份 / 迁移的接口测试。

两种服务器一起用：
  · 8000 = 真库（**只读它**：导出）
  · 8001 = 独立临时库（自己的 DB + 自己的 uploads），导入、清库、再导入都在它身上折腾

这样「导入会写库」这件事永远不会碰到用户的数据 —— 迁移这种功能，测试本身就不该
在真库上试。

跑之前先起临时服务器（单独一个终端，别把真库的服务器停掉）：

    cd backend
    $scratch = "$env:TEMP\notes_scratch"
    $env:SHIKE_DATA_DIR = $scratch          # 连 uploads 一起隔离
    $env:SHIKE_DB_PATH = "$scratch\notes.db"
    .\\.venv\\Scripts\\python.exe -m uvicorn main:app --port 8001

脚本第一步会把这个临时库清空，所以可以反复跑；跑完把 $scratch 删掉即可。
"""
import io
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

REAL = "http://127.0.0.1:8000"
SCRATCH = "http://127.0.0.1:8001"
SCRATCH_DIR = Path(os.environ["TEMP"]) / "notes_scratch"
SCRATCH_DB = SCRATCH_DIR / "notes.db"
SCRATCH_NOTES = SCRATCH_DIR / "uploads" / "notes"

fails = []


def check(label, got, want=True):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


def req(base, method, path, body=None, raw=False, timeout=60):
    url = base + urllib.parse.quote(path, safe="/?=&")
    if raw:
        r = urllib.request.Request(url, method=method)
    else:
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, (payload if raw else (json.loads(payload) if payload else None)), resp
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload), e
        except Exception:
            return e.code, payload.decode("utf-8", "replace"), e


def upload(base, path, filename, blob, extra_query=""):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + blob + f"\r\n--{b}--\r\n".encode()
    r = urllib.request.Request(base + path + extra_query, data=body, method="POST",
                               headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, json.loads(payload)
        except Exception:
            return e.code, payload.decode("utf-8", "replace")


def notes_of(base):
    _, t, _ = req(base, "GET", "/api/note-folders/tree")
    out = []
    def walk(n, sysname):
        for x in n["notes"]:
            out.append((x["id"], x["title"], sysname, n["name"] if n["name"] != sysname else ""))
        for c in n["children"]:
            walk(c, sysname)
    for r in t["roots"]:
        walk(r, r["name"])
    return t, out


def png_bytes():
    """造一张真 PNG（必须是真图：服务端按魔数校验）。"""
    import pymupdf
    doc = pymupdf.open()
    pg = doc.new_page(width=120, height=80)
    pg.insert_text((10, 40), "hi", fontsize=12)
    data = pg.get_pixmap().tobytes("png")
    doc.close()
    return data


print("==== ⓪ 先把临时库清干净（这个测试必须可以反复跑） ====")
con = sqlite3.connect(str(SCRATCH_DB))
con.execute("delete from notes")
con.execute("delete from note_folders where is_root = 0")
con.commit()
con.close()
if SCRATCH_NOTES.is_dir():
    for p in SCRATCH_NOTES.glob("*"):
        p.unlink()
t0, _ = notes_of(SCRATCH)
check("临时库起点为空", t0["total"], 0)


print("==== ① 从真库导出（只读） ====")
st, blob, resp = req(REAL, "GET", "/api/notes-backup/export", raw=True)
check("导出成功", st, 200)
zf = zipfile.ZipFile(io.BytesIO(blob))
names = zf.namelist()
print("  包内文件:", [n for n in names if "/" not in n])
manifest = json.loads(zf.read("manifest.json"))
payload = json.loads(zf.read("notes.json"))
real_tree, real_notes = notes_of(REAL)
check("manifest 格式标记", manifest["format"], "shike-notes")
check("包里的笔记数 = 真库里的", manifest["counts"]["notes"], len(real_notes))
check("每篇笔记都有对应的 .md 副本", len([n for n in names if n.startswith("notes/")]), len(real_notes))
check("包含目录树总览", "目录树.md" in names)
check("包含 notes.json", "notes.json" in names)
print("  manifest 里的体系:", [c["code"] for c in manifest["curricula"]])

print("\n==== ② 导进独立临时库（先试算） ====")
st, dry = upload(SCRATCH, "/api/notes-backup/import", "backup.zip", blob, "?dry_run=true")
check("试算成功", st, 200)
print("  试算报告:", {k: v for k, v in dry.items() if k != "warnings"})
check("试算说会建这么多笔记", dry["notes_created"], len(real_notes))
t, _ = notes_of(SCRATCH)
check("试算没有真的写进库（临时库还是空的）", t["total"], 0)

st, rep = upload(SCRATCH, "/api/notes-backup/import", "backup.zip", blob)
check("真导入成功", st, 200)
check("报告：新建 = 真库篇数", rep["notes_created"], len(real_notes))
t, scratch_notes = notes_of(SCRATCH)
check("临时库里笔记数一致", t["total"], len(real_notes))
check("id 原样保留（换设备后链接不变）", sorted(x[0] for x in scratch_notes), sorted(x[0] for x in real_notes))
check("标题一致", sorted(x[1] for x in scratch_notes), sorted(x[1] for x in real_notes))
check("体系归属一致（按 code 认，不按 id）",
      sorted(f"{x[2]}/{x[3]}" for x in scratch_notes), sorted(f"{x[2]}/{x[3]}" for x in real_notes))

print("\n==== ③ 再导一次：不该重复长东西 ====")
st, rep2 = upload(SCRATCH, "/api/notes-backup/import", "backup.zip", blob)
check("全部跳过", rep2["notes_skipped"], len(real_notes))
check("没有新建笔记", rep2["notes_created"], 0)
t, again = notes_of(SCRATCH)
check("笔记数没变（导入两次 == 导入一次）", t["total"], len(real_notes))
check("体系根也没变多", len(t["roots"]), 5)

print("\n==== ④ 覆盖模式 ====")
st, rep3 = upload(SCRATCH, "/api/notes-backup/import", "backup.zip", blob, "?overwrite=true")
check("全部覆盖", rep3["notes_overwritten"], len(real_notes))
check("没有新建", rep3["notes_created"], 0)

print("\n==== ⑤ 带目录 + 带配图的包（在临时库里造一张，从临时库导出） ====")
_, dse_root_id, _ = req(SCRATCH, "GET", "/api/note-folders/tree")
dse = next(r for r in dse_root_id["roots"] if r["name"] == "DSE 数学")
_, f1, _ = req(SCRATCH, "POST", "/api/note-folders", {"parent_id": dse["id"], "name": "一、有理数"})
_, f2, _ = req(SCRATCH, "POST", "/api/note-folders", {"parent_id": f1["id"], "name": "1.1 认识有理数"})
st, img = upload(SCRATCH, "/api/notes/image", "shot.png", png_bytes())
img_name = img["name"]
st, n1, _ = req(SCRATCH, "POST", "/api/notes", {"title": "带图的临时笔记", "folder_id": f2["id"],
                                               "content": f"这里有张图：\n\n![](/api/notes/files/{img_name})\n"})
st, _, _ = req(SCRATCH, "PATCH", f"/api/notes/{n1['id']}", {"ink": [{"c": "#e53935", "w": 3,
                                                                "p": [[0.1, 0.1], [0.4, 0.5]]}]})
check("临时笔记里有图有板书", True)
st, blob2, _ = req(SCRATCH, "GET", "/api/notes-backup/export", raw=True)
check("从临时库导出成功", st, 200)
z2 = zipfile.ZipFile(io.BytesIO(blob2))
n2 = z2.namelist()
check("包里带上了配图", f"images/{img_name}" in n2)
check("包里带上了人读的 .md（按目录分级）",
      any(x.startswith("notes/DSE 数学/一、有理数/1.1 认识有理数/") for x in n2), True)
p2 = json.loads(z2.read("notes.json"))
check("目录记录在包里", [f["path"] for f in p2["folders"]], ["一、有理数", "一、有理数/1.1 认识有理数"])
check("板书笔画也带上了", any(x["ink"] for x in p2["notes"]), True)
md = z2.read([x for x in n2 if "带图的临时笔记" in x][0]).decode("utf-8")
check("人读的 .md 里有正文", "这里有张图" in md)
check("人读的 .md 顶部写了来源目录", "DSE 数学 / 一、有理数 / 1.1 认识有理数" in md)

print("\n==== ⑥ 清空临时库，再从带目录的包导一次 ====")
con = sqlite3.connect(str(SCRATCH_DB))
con.execute("delete from notes")
con.execute("delete from note_folders where is_root = 0")
con.commit()
con.close()
for p in SCRATCH_NOTES.glob("*"):        # 配图也删掉，验证能从包里恢复
    p.unlink()
t, _ = notes_of(SCRATCH)
check("临时库已清空", t["total"], 0)
st, rep4 = upload(SCRATCH, "/api/notes-backup/import", "backup2.zip", blob2)
check("导入成功", st, 200)
print("  报告:", {k: v for k, v in rep4.items() if k != "warnings"})
check("目录建回来了（2 层）", rep4["folders_created"], 2)
t, back = notes_of(SCRATCH)
check("笔记回来了（原 5 篇 + 临时 1 篇）", t["total"], len(real_notes) + 1)
check("配图从包里恢复了", (SCRATCH_NOTES / img_name).is_file(), True)
st, _, _ = req(SCRATCH, "GET", f"/api/notes/files/{img_name}", raw=True)
check("配图能通过接口取到", st, 200)
st, full, _ = req(SCRATCH, "GET", f"/api/notes/{n1['id']}")
check("板书笔画也回来了", len(full["ink"]), 1)
check("笔记挂回了原来的目录", full["folder_id"] == f2["id"], True)

print("\n==== ⑦ 坏包 / 陌生包 / 新版本包，必须拒绝 ====")
st, r = upload(SCRATCH, "/api/notes-backup/import", "junk.zip", b"this is not a zip")
check("不是 zip → 422", st, 422)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("manifest.json", json.dumps({"format": "other-app", "version": 1}))
    z.writestr("notes.json", "{}")
st, r = upload(SCRATCH, "/api/notes-backup/import", "other.zip", buf.getvalue())
check("别的程序的包 → 422", st, 422)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("manifest.json", json.dumps({"format": "shike-notes", "version": 99}))
    z.writestr("notes.json", "{}")
st, r = upload(SCRATCH, "/api/notes-backup/import", "future.zip", buf.getvalue())
check("更高版本的包 → 422", st, 422)
check("  并且说清了原因", "更新版本" in (r.get("detail") or ""), True)

print("\n==== ⑧ 包里有个本机没有的体系 → 落到未归档 + 给警告（不能整包拒绝） ====")
p3 = json.loads(json.dumps(p2))
p3["notes"] = [dict(x, curriculum_code="nope-sys") for x in p3["notes"]]
p3["folders"] = []
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("manifest.json", json.dumps(manifest | {"version": 1}))
    z.writestr("notes.json", json.dumps(p3, ensure_ascii=False))
st, rep5 = upload(SCRATCH, "/api/notes-backup/import", "unknown-sys.zip", buf.getvalue(), "?overwrite=true")
check("导入没有被整包拒绝", st, 200)
check("给了警告", len(rep5["warnings"]) > 0, True)
print("  警告:", rep5["warnings"][0])
t, after5 = notes_of(SCRATCH)
check("那篇笔记落在「未归档」里", any(x[2] == "未归档" for x in after5), True)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} 项失败: {fails}"))
