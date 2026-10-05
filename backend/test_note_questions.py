# -*- coding: utf-8 -*-
r"""「笔记里插入题库的题」：块语法、图片快照、一份笔记出两版。

跑在**独立临时服务器**（8001，自己的 DB + 自己的 uploads）上 ——
会建题、建笔记、删题，这些都不能在真库上试。

    cd backend
    $scratch = "$env:TEMP\notes_scratch"
    $env:SHIKE_DATA_DIR = $scratch
    $env:SHIKE_DB_PATH = "$scratch\notes.db"
    .\.venv\Scripts\python.exe -m uvicorn main:app --port 8001

验的重点是三个容易做歪的地方（不是 CRUD 本身）：
  ① **学生版里读不到答案** —— 真去读导出的 docx 文本，不是看有没有传参数；
  ② **图是快照** —— 之后把题库里那道题删掉，笔记里的图还在（只留 URL 就会静默丢图）；
  ③ 块要能过备份/迁移（内容就是普通 Markdown，不该有特殊待遇）。
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

S = "http://127.0.0.1:8001"
SCRATCH = Path(os.environ["TEMP"]) / "notes_scratch"
DB = SCRATCH / "notes.db"
NOTES_DIR = SCRATCH / "uploads" / "notes"
CROPS_DIR = SCRATCH / "uploads" / "crops"

Q1 = "nq1"          # 有题干图 + 答案图
Q2 = "nq2"          # 纯文字
ANSWER_MARK = "答对了才算数"      # 只在答案里出现的字符串，用它判断学生版漏没漏答案
STEM_TEXT = "解方程 x^2-5x+6=0"

fails = []


def check(label, got, want=True):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(S + urllib.parse.quote(path, safe="/?=&"),
                               data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode("utf-8", "replace")


def raw(path):
    """拿二进制（导出文件 / 图片）：只看状态码与字节数，别拿 JSON 去解。"""
    try:
        with urllib.request.urlopen(S + urllib.parse.quote(path, safe="/?=&"), timeout=120) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def png_bytes(tag="q"):
    """造一张真 PNG（用 pymupdf 渲染，不手写魔数 —— 服务端按魔数校验）。"""
    import pymupdf
    doc = pymupdf.open()
    pg = doc.new_page(width=120, height=60)
    pg.insert_text((8, 30), tag, fontsize=12)
    blob = pg.get_pixmap().tobytes("png")
    doc.close()
    return blob


def docx_text(blob: bytes) -> str:
    """从 docx 里抠出正文文字（去标签），用来断言答案到底在不在文件里。"""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml)


print("==== ⓪ 清空临时库，造题（一道带图带答案，一道纯文字） ====")
CROPS_DIR.mkdir(parents=True, exist_ok=True)
NOTES_DIR.mkdir(parents=True, exist_ok=True)
q1_img = "qcrop_nq1_stem.png"
q1_ans = "qcrop_nq1_ans.png"
(CROPS_DIR / q1_img).write_bytes(png_bytes("stem"))
(CROPS_DIR / q1_ans).write_bytes(png_bytes("answer"))

con = sqlite3.connect(str(DB))
con.execute("delete from notes")
con.execute("delete from note_folders where is_unfiled = 0")
con.execute("delete from questions")
con.execute("insert into questions (id, owner_id, content, answer, qtype, difficulty,"
            " knowledge_points, tags, image, answer_image, usage_count)"
            " values (?,1,?,?,?,?,?,?,?,?,0)",
            (Q1, STEM_TEXT, ANSWER_MARK, "解答题", "中档", '["一元二次方程"]', '["错题重练"]',
             f"/api/crops/{q1_img}", f"/api/crops/{q1_ans}"))
con.execute("insert into questions (id, owner_id, content, answer, qtype, difficulty,"
            " knowledge_points, tags, usage_count) values (?,1,?,?,?,?,?,?,0)",
            (Q2, "化简 a^2-b^2", "（a+b)(a-b)", "解答题", "基础", '["因式分解"]', '["课堂练习"]'))
con.commit()
con.close()

print("==== ① 取题块：题干 + 答案 + 图片快照 ====")
st, r1 = req("POST", "/api/notes/question-block", {"qid": Q1, "index": 1})
check("取块 200", st, 200)
blk = r1.get("block", "") if isinstance(r1, dict) else ""
check("块里有题目锚点 <!--q:nq1-->", "<!--q:nq1-->" in blk)
check("块里有答案分隔 <!--qa-->", "<!--qa-->" in blk)
check("块结尾 <!--qe-->", blk.rstrip().endswith("<!--qe-->"))
check("题干在块里", STEM_TEXT in blk)
check("答案在块里", ANSWER_MARK in blk)
check("序号写进题干（练习 1）", "练习 1" in blk)

# 图片必须是**复制**进笔记目录的 nb_ 文件，而且原图 URL 不该再出现
copies = re.findall(r"/api/notes/files/(nb_[0-9a-f]+\.png)", blk)
check("块里有两张笔记自己的图（题干图 + 答案图）", len(copies), 2)
check("原 /api/crops/ URL 没有被写进笔记", "/api/crops/" in blk, False)
check("两张图都真落到笔记目录了", all((NOTES_DIR / c).is_file() for c in copies))

st, r2 = req("POST", "/api/notes/question-block", {"qid": Q2})
check("纯文字题也能取块", st, 200)
check("没有答案分隔时不留空 <!--qa-->", "<!--qa-->" in (r2.get("block") or ""))
check("不存在的题给 404", req("POST", "/api/notes/question-block", {"qid": "nope"})[0], 404)

print("==== ② 插进笔记：块就是普通 Markdown，正文能存能取 ====")
st, note = req("POST", "/api/notes", {"title": "插题测试", "content": f"# 讲义\n\n{blk}\n"})
check("建笔记 200", st, 200)
nid = note["id"]
full = "# 讲义\n\n" + blk + "\n\n" + (r2.get("block") or "") + "\n"
check("正文写回", req("PATCH", f"/api/notes/{nid}", {"content": full})[0], 200)
st, back = req("GET", f"/api/notes/{nid}")
check("取回的正文与存的一致（块没被改写）", back["content"], full)

print("==== ③ 导出两版：教师版有答案，学生版**读不到**答案 ====")
st, teacher = raw(f"/api/notes/{nid}/export?format=docx&with_answer=true")
check("教师版导出 200", st, 200)
tt = docx_text(teacher)
check("教师版里有题干", STEM_TEXT in tt)
check("教师版里有答案", ANSWER_MARK in tt)
check("教师版文件名没写「学生版」", "学生版" in (req("GET", f"/api/notes/{nid}")[1]["title"]), False)

st, student = raw(f"/api/notes/{nid}/export?format=docx&with_answer=false")
check("学生版导出 200", st, 200)
stt = docx_text(student)
check("学生版里有题干", STEM_TEXT in stt)
check("学生版里**没有**答案", ANSWER_MARK in stt, False)
check("学生版的自己的答案也不漏", "答对了才算数" in stt, False)
check("学生版标题带「（学生版）」", "学生版" in stt)
check("第二题的答案也不漏", "（a+b)(a-b)" in stt, False)
check("学生版比教师版小", len(student) < len(teacher))
# 中文紧跟 ** 时加粗不生效（会渲染成字面星号）—— 这是浏览器里实测踩到的坑，
# 所以这里对「星号有没有被吃掉」也要断言，不能只看文字在不在。
check("教师版里没有字面星号（加粗语法生效）", "**" in tt, False)
check("学生版里没有字面星号", "**" in stt, False)
check("教师版的答案前面是「答案：」", f"答案：{ANSWER_MARK}" in tt.replace(" ", ""), True)

print("==== ④ 图是快照：删掉题库里那道题，笔记里的图还在 ====")
before_files = sorted(p.name for p in NOTES_DIR.glob("nb_*.png"))
st, _ = req("DELETE", f"/api/questions/{Q1}")
check("删题返回 200/204", st in (200, 204))
check("题库里的截图被清走了", (CROPS_DIR / q1_img).exists(), False)
for c in copies:
    check(f"笔记里的图还在（{c[:12]}…）", (NOTES_DIR / c).is_file())
    check(f"图还能访问（{c[:12]}…）", raw(f"/api/notes/files/{c}")[0], 200)
st, again = req("GET", f"/api/notes/{nid}")
check("笔记正文里的图链接没被改", again["content"], full)
st, teacher2 = raw(f"/api/notes/{nid}/export?format=docx&with_answer=true")
check("删题后教师版仍能导出", st, 200)
check("导出里仍然带图（docx 里有图片资源）",
      any(n.startswith("word/media/") for n in zipfile.ZipFile(io.BytesIO(teacher2)).namelist()))
check("没留下多余图片文件", sorted(p.name for p in NOTES_DIR.glob("nb_*.png")), before_files)

print("==== ⑤ 备份 / 迁移：块与快照图一起走 ====")
st, blob = raw("/api/notes-backup/export")
check("备份导出 200", st, 200)
with zipfile.ZipFile(io.BytesIO(blob)) as z:
    names = z.namelist()
    man = json.loads(z.read("manifest.json").decode("utf-8"))
    metas = json.loads(z.read("notes.json").decode("utf-8"))
    mds = [n for n in names if n.startswith("notes/") and n.endswith(".md")]
    md = z.read(mds[0]).decode("utf-8") if mds else ""
# notes.json 的层级结构不是本测试关心的点，关心的是「这篇笔记有没有进去」，
# 所以直接在原文里找它的 id，别把测试和某个字段名绑在一起。
raw_meta = json.dumps(metas, ensure_ascii=False)
check("备份里带了这篇笔记（按标题存成 .md）", mds, ["notes/未归档/插题测试.md"])
check("备份元数据里有这篇笔记的 id", nid in raw_meta)
check("备份的 Markdown 里块标记没被改写", "<!--q:nq1-->" in md and "<!--qe-->" in md)
check("备份里带上了快照图", all(any(n.endswith(c) for n in names) for c in copies))
check("manifest 记了图片数量", man.get("counts", {}).get("images"), 2)
check("manifest 版本 >= 2", man.get("version", 0) >= 2)
check("没有任何 /api/crops/ 泄漏进备份", "/api/crops/" in " ".join(names), False)

print("==== ⑥ LaTeX 惯用写法 \\(…\\) / \\[…\\] 也要能出公式 ====")
# 这条同时守着两件事：归一化在解析前生效（否则反斜杠先被吃掉）、
# 并且真的走到了公式管线（导出的正文里不该留 \\( 原文）。
st, mnote = req("POST", "/api/notes", {"title": "公式写法测试",
                                       "content": "行内 \\(x^2+1\\) 与独立 \\[\\frac{a}{b}\\] 结束"})
check("建笔记 200", st, 200)
mid = mnote["id"]
st, mdoc = raw(f"/api/notes/{mid}/export?format=docx&with_answer=true")
check("导出 200", st, 200)
mt = docx_text(mdoc)
check("导出里没有 \\( 原文（归一化生效了）", "\\(" in mt, False)
check("导出里没有 \\[ 原文", "\\[" in mt, False)
check("公式没有整段丢掉（文字还在）", "行内" in mt and "结束" in mt)
st, back = req("GET", f"/api/notes/{mid}")
check("正文里仍然是用户写的 \\( \\) 原文（不改用户的内容）", "\\(" in back["content"])

print()
if fails:
    print(f"❌ {len(fails)} 项没过：" + "；".join(fails))
else:
    print("✅ ALL PASS")
