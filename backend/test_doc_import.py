# -*- coding: utf-8 -*-
"""「把现成文档导入成笔记」的测试。

跑在**独立临时服务器**（8001，自己的 DB + 自己的 uploads）上 —— 导入会写库、还会把
文档里的图片落盘，这种功能不该在真库上试。真库只被读。

用的样本都是真文件：仓库 samples/ 下的 docx/PDF/扫描版样例卷，加上现场造的一个 HTML。

跑之前先起临时服务器（另一个终端，别把真库的服务器停掉）：

    cd backend
    $scratch = "$env:TEMP\notes_scratch"
    $env:SHIKE_DATA_DIR = $scratch          # 连 uploads 一起隔离
    $env:SHIKE_DB_PATH = "$scratch\notes.db"
    .\\.venv\\Scripts\\python.exe -m uvicorn main:app --port 8001

脚本第一步会清空临时库，可反复跑；跑完删掉 $scratch 即可。
（注意：管道给 Select-String 时先设 `$env:PYTHONIOENCODING="utf-8"`，
否则 Python 用 GBK 写 stdout，打印「⓪」这类字符会直接 UnicodeEncodeError。）
"""
import io
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

SCRATCH = "http://127.0.0.1:8001"
ROOT = Path(__file__).resolve().parent.parent
SCRATCH_DIR = Path(os.environ["TEMP"]) / "notes_scratch"
SCRATCH_DB = SCRATCH_DIR / "notes.db"
SCRATCH_NOTES = SCRATCH_DIR / "uploads" / "notes"
SAMPLES = ROOT / "samples"

fails = []


def check(label, got, want=True):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    url = SCRATCH + urllib.parse.quote(path, safe="/?=&")
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode("utf-8", "replace")


def import_doc(path: Path, folder_id=None, filename=None):
    blob = path.read_bytes()
    name = filename or path.name
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + blob + f"\r\n--{b}--\r\n".encode()
    q = "" if folder_id is None else f"?folder_id={folder_id}"
    r = urllib.request.Request(SCRATCH + "/api/notes/import-doc" + q, data=body, method="POST",
                               headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode("utf-8", "replace")


def raw_status(path):
    """取二进制文件（原件/图片）只看状态码 —— 别拿 JSON 去解它。"""
    try:
        with urllib.request.urlopen(SCRATCH + urllib.parse.quote(path, safe="/?=&"), timeout=30) as r:
            return r.status, r.headers.get("content-type"), len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, "", 0


def upload_image(filename: str):
    """往笔记资源目录塞一张真 PNG（走配图上传接口，和服务端命名规则一致）。"""
    import pymupdf
    doc = pymupdf.open()
    pg = doc.new_page(width=100, height=60)
    pg.insert_text((8, 30), "img", fontsize=10)
    blob = pg.get_pixmap().tobytes("png")
    doc.close()
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: image/png\r\n\r\n").encode() + blob + f"\r\n--{b}--\r\n".encode()
    r = urllib.request.Request(SCRATCH + "/api/notes/image", data=body, method="POST",
                               headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, json.loads(resp.read())["name"]


def note_body(nid):
    st, n = req("GET", f"/api/notes/{nid}")
    return n


print("==== ⓪ 清空临时库 ====")
con = sqlite3.connect(str(SCRATCH_DB))
con.execute("delete from notes")
con.execute("delete from note_folders where is_root = 0")
con.commit()
con.close()
if SCRATCH_NOTES.is_dir():
    for p in SCRATCH_NOTES.glob("*"):
        p.unlink()
print("  （临时库已清）")

print("\n==== ① Word（.docx）→ 笔记：标题/加粗/列表/表格/图片 ====")
docx = SAMPLES / "高二数学专项测试卷.docx"
st, r1 = import_doc(docx)
check("导入成功", st, 200)
print("  返回:", {k: v for k, v in r1.items() if k not in ("markdown",)})
n1 = note_body(r1["id"])
md = n1["content"]
check("标题取自文件名", n1["title"], "高二数学专项测试卷")
check("开头写了来源与原件链接", md.startswith("> 从「高二数学专项测试卷.docx」导入") and "原始文件：" in md)
check("转出了 Markdown 标题", bool(re.search(r"^#{1,3} .+", md, re.M)))
check("转出了列表或表格（结构没丢）", ("|" in md and "---|" in md) or bool(re.search(r"^\s*[-*\d]+[.\s]", md, re.M)))
check("正文有实质内容", r1["chars"] > 300)
check("原件也存下来了", (SCRATCH_NOTES / r1["original"]).is_file())
st2, ctype2, size2 = raw_status(f"/api/notes/files/{r1['original']}")
check("原件能下载回来", (st2, size2 > 1000), (200, True))
check("  而且是 Word 的 MIME", "wordprocessingml" in (ctype2 or ""), True)

print("\n==== ② 有文字层的 PDF ====")
st, r2 = import_doc(SAMPLES / "高二数学专项测试卷.pdf")
check("导入成功", st, 200)
md2 = note_body(r2["id"])["content"]
check("PDF 按页给了小标题", "## 第 1 页" in md2 or r2["chars"] > 100)
print("  返回:", {k: v for k, v in r2.items() if k != "markdown"})

print("\n==== ③ 扫描版 PDF（没有文字层）→ 按页转图，不算失败 ====")
st, r3 = import_doc(SAMPLES / "扫描版样例卷.pdf")
check("导入成功（不是报错）", st, 200)
n3 = note_body(r3["id"])
check("报告里说了是扫描件", r3.get("kind_cn"), "PDF（扫描件）")
check("转成了图片", r3["images"] > 0)
check("正文里是图片引用", f"/api/notes/files/" in n3["content"] and "![第 1 页]" in n3["content"])
check("明说了图片不能搜索/能标注", any("没有文字层" in w for w in r3["warnings"]))
first_img = n3["content"].split("/api/notes/files/")[1].split(")")[0]
check("图片真的落在磁盘上", (SCRATCH_NOTES / first_img).is_file())
st3, ctype3, _ = raw_status(f"/api/notes/files/{first_img}")
check("图片能取到", st3, 200)

print("\n==== ④ HTML → 笔记：标签结构、内嵌图、外链图 ====")
html = """<!doctype html><html><head><title>忽略</title><style>p{color:red}</style></head>
<body>
<h1>幂的运算</h1>
<p>同底数幂相乘，底数不变，指数<strong>相加</strong>，即 <em>a^m·a^n=a^(m+n)</em>。</p>
<h2>例题</h2>
<ol><li>计算 <code>a^2·a^3</code></li><li>化简 <code>(x^2)^3</code></li></ol>
<blockquote>注意符号</blockquote>
<table><tr><th>题型</th><th>要点</th></tr><tr><td>选择</td><td>看指数</td></tr></table>
<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==" alt="内嵌小图">
<img src="https://example.com/a.png" alt="外链图">
<p>末尾一行</p>
</body></html>"""
tmp_html = Path(os.environ["TEMP"]) / "test_note_import.html"
tmp_html.write_text(html, encoding="utf-8")
st, r4 = import_doc(tmp_html, filename="幂的运算整理.html")
check("导入成功", st, 200)
md4 = note_body(r4["id"])["content"]
check("h1 → #", "# 幂的运算" in md4)
check("h2 → ##", "## 例题" in md4)
check("加粗保留", "**相加**" in md4)
check("有序列表保留编号", "1. 计算" in md4)
check("表格转成 Markdown 表格", "| 题型 | 要点 |" in md4)
check("引用块保留", "> 注意符号" in md4)
check("style/head 里的东西没混进来", "color:red" not in md4)
check("内嵌 data: 图片存进了笔记", r4["images"] == 1)
check("外链图保留成链接 + 提示", "https://example.com/a.png" in md4
      and any("不联网" in w for w in r4["warnings"]))

print("\n==== ⑤ 纯文本 / Markdown 直接当笔记 ====")
tmp_md = Path(os.environ["TEMP"]) / "test_note_plain.md"
tmp_md.write_text("# 手写的小结\n\n- 第一条\n- 第二条\n", encoding="utf-8")
st, r5 = import_doc(tmp_md, filename="手写的小结.md")
check("导入成功", st, 200)
check("正文原样保留（Markdown 不该被再转一遍）", "# 手写的小结" in note_body(r5["id"])["content"])

print("\n==== ⑥ 落进指定目录 ====")
st, t = req("GET", "/api/note-folders/tree")
if not any(x["name"] == "DSE 数学" for x in t["roots"]):
    # 一级分组现在是用户自己建的（不再按体系自动生成），测试自己造一个
    req("POST", "/api/note-folders", {"name": "DSE 数学"})
    st, t = req("GET", "/api/note-folders/tree")
dse = next(x for x in t["roots"] if x["name"] == "DSE 数学")
st, folder = req("POST", "/api/note-folders", {"parent_id": dse["id"], "name": "导入测试"})
st, r6 = import_doc(tmp_md, folder_id=folder["id"], filename="放进目录.md")
check("报告里的目录是它", r6["folder_id"], folder["id"])
st, t2 = req("GET", "/api/note-folders/tree")
dse2 = next(x for x in t2["roots"] if x["name"] == "DSE 数学")
check("树里能看到这篇", any(x["title"] == "放进目录" for c in dse2["children"] for x in c["notes"]))

print("\n==== ⑦ 不支持 / 坏文件要说清楚 ====")
st, r = import_doc(tmp_md, filename="照片.png")          # 图片：明确引导去贴图
check("图片 → 422 且指路", st, 422)
check("  提示里让人直接贴图", "Ctrl+V" in (r.get("detail") or ""))
bad = Path(os.environ["TEMP"]) / "test_note_broken.docx"
bad.write_bytes(b"this is not a docx")
st, r = import_doc(bad)
check("坏 docx → 422", st, 422)
print("  ", (r.get("detail") or "")[:60])
empty = Path(os.environ["TEMP"]) / "test_note_empty.txt"
empty.write_text("   \n\n  ", encoding="utf-8")
st, r = import_doc(empty, filename="空的.txt")
check("空文件 → 422（不能静默建一篇空笔记）", st, 422)

print("\n==== ⑧ 删掉笔记时，原件与配图一起清掉 ====")
before = len(list(SCRATCH_NOTES.glob("*")))
st, r = req("DELETE", f"/api/notes/{r3['id']}")           # 扫描版那篇：图片最多
after = len(list(SCRATCH_NOTES.glob("*")))
check("删笔记顺带清了它的图片", after < before, True)
print(f"  文件数 {before} → {after}，接口回: {r}")

print("\n==== ⑨ 把正文里的图片引用删掉 → 磁盘文件跟着清（引用计数归零才清） ====")
# 造一张图 + 一篇引用它的笔记
st, img = upload_image("清扫测试图.png")
check("图片上传成功", st, 200)
ref = f"![](/api/notes/files/{img})"
st, n_a = req("POST", "/api/notes", {"title": "引用这张图的笔记", "content": f"看图：\n\n{ref}\n"})
check("笔记建好了", st, 200)
check("文件在磁盘上", (SCRATCH_NOTES / img).is_file())

# ① 去掉引用 → 文件应该被清掉
st, r_save = req("PATCH", f"/api/notes/{n_a['id']}", {"content": "看图：\n\n（图删了）\n"})
check("保存后报告清掉了 1 个文件", r_save.get("images_removed"), 1)
check("磁盘上真没了", (SCRATCH_NOTES / img).is_file(), False)

# ② 两个笔记引用同一张图 → 只删一处不能把文件删掉
st, img2 = upload_image("共享图.png")
ref2 = f"![](/api/notes/files/{img2})"
st, na = req("POST", "/api/notes", {"title": "共享图笔记甲", "content": f"{ref2}\n"})
st, nb = req("POST", "/api/notes", {"title": "共享图笔记乙", "content": f"{ref2}\n"})
st, r1 = req("PATCH", f"/api/notes/{na['id']}", {"content": "甲不要图了\n"})
check("拿掉一处：不动文件", (r1.get("images_removed"), (SCRATCH_NOTES / img2).is_file()), (0, True))
st, r2 = req("PATCH", f"/api/notes/{nb['id']}", {"content": "乙也不要了\n"})
check("拿掉最后一处：才删", (r2.get("images_removed"), (SCRATCH_NOTES / img2).is_file()), (1, False))

# ③ 导入文档的原件：删掉那行链接**不**销毁原件（原件是「一定没丢的那部分」）
st, r_doc = import_doc(SAMPLES / "高二数学专项测试卷.pdf", filename="原件保留测试.pdf")
doc_file = SCRATCH_NOTES / r_doc["original"]
check("原件在磁盘上", doc_file.is_file())
full = note_body(r_doc["id"])
kept = re.sub(r"> 原始文件：\[[^\]]+\]\(/api/notes/files/[^)]+\)\n", "", full["content"])
st, r_del = req("PATCH", f"/api/notes/{r_doc['id']}", {"content": kept})
check("删掉正文里的原件链接：原件**保留**（保存路径只收配图）",
      ((r_del.get("images_removed"), doc_file.is_file())), (0, True))

# 引用还在的时候删整篇笔记 → 原件一起收（这是「删掉这篇，它的东西一并没」的本意）
st, _ = req("PATCH", f"/api/notes/{r_doc['id']}", {"content": full["content"]})
st, r_gone = req("DELETE", f"/api/notes/{r_doc['id']}")
check("引用还在时删整篇笔记：原件一起收",
      ((r_gone.get("images_removed"), doc_file.is_file())), (1, False))

# ④ 安全边界：不认识的文件名一律不动
stranger = SCRATCH_NOTES / "keep_me_please.txt"
stranger.write_text("这不是笔记的配图", encoding="utf-8")
st, n_c = req("POST", "/api/notes", {"title": "乱引用一封", "content": "![](/api/notes/files/keep_me_please.txt)\n"})
st, r_sweep = req("PATCH", f"/api/notes/{n_c['id']}", {"content": "不要了\n"})
check("不在回收名单里的文件名：不删", (r_sweep.get("images_removed"), stranger.is_file()), (0, True))
st, _ = req("DELETE", f"/api/notes/{n_c['id']}")
stranger.unlink(missing_ok=True)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} 项失败: {fails}"))
