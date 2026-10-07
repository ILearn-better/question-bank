# -*- coding: utf-8 -*-
r"""反馈「课程内容」段（迁移 0021）的回归测试。

需求（用户 2026-10-07）：反馈表单从四段变五段，加一段「课程内容」——
老师手写本次讲了什么，润色时随其它四段一起发给 AI，让推荐润色模板里的
【本次课堂内容】栏有料可写。

三块：
  ① 纯函数（不起服务、不联网）—— ai_polish 的 FIELDS/FIELD_CN/assemble_draft、
     feedback_export 的 SECTIONS 都把 course_content 排在最前，且空段不出现。
  ② 前端静态守卫 —— FeedbackDialog.js 的五段在各处（FIELDS/form/payload/snapshot/
     模板 seeds/textarea 列表）都没漏。
  ③ 8001 集成（起独立临时服务）—— 建学生+课时 → 存五段反馈 → 查回 →
     导出 txt 里出现【课程内容】+ 正文 → 归档快照里也有。

    cd backend
    export SHIKE_DATA_DIR="$TEMP/notes_scratch" SHIKE_DB_PATH="$TEMP/notes_scratch/notes.db"
    ./.venv/Scripts/python.exe -m uvicorn main:app --port 8001
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REAL = "http://127.0.0.1:8000"
S = "http://127.0.0.1:8001"
SCRATCH = Path(os.environ["TEMP"]) / "notes_scratch"
DB = SCRATCH / "notes.db"

fails = []


def check(label, got, want):
    okk = got == want
    print(("  OK  " if okk else "  FAIL") + f"  {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


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


# ---------------------------------------------------------------- ① 纯函数
print("① 纯函数：ai_polish / feedback_export 都认得「课程内容」")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.services import ai_polish, feedback_export  # noqa: E402

check("FIELDS 是五段", len(ai_polish.FIELDS), 5)
check("课程内容排最前", ai_polish.FIELDS[0], "course_content")
check("FIELD_CN 里叫「课程内容」", ai_polish.FIELD_CN["course_content"], "课程内容")
check("SECTIONS 是五段", len(feedback_export.SECTIONS), 5)
check("导出的第一栏是课程内容", feedback_export.SECTIONS[0], ("course_content", "课程内容"))

d = ai_polish.assemble_draft(
    {"course_content": "讲了二次函数的图像与性质", "performance": "状态不错"},
    {"学生": "小明", "上课时间": "2026-10-07 19:00"})
check("draft 里有课程内容标签", "〔课程内容〕" in d, True)
check("draft 里有课程内容正文", "讲了二次函数的图像与性质" in d, True)

d2 = ai_polish.assemble_draft({"performance": "只写了表现"}, {})
check("空段不出现（没写课程内容就不印这个标题）", "课程内容" not in d2, True)

d3 = ai_polish.assemble_draft({}, {"学生": "小明"})
check("一个字没写 → 空串（不能只凭课程信息就去润色）", d3, "")

# ---------------------------------------------------------------- ② 前端静态守卫
print("\n② 前端：FeedbackDialog.js 五段在各处都没漏")
ROOT = Path(__file__).resolve().parent.parent
FD = (ROOT / "frontend" / "src" / "components" / "FeedbackDialog.js").read_text(encoding="utf-8")

import re  # noqa: E402
fields_block = re.search(r"const FIELDS = \[(.*?)\];", FD, re.S).group(1)
check("FIELDS 数组含 course_content", "course_content" in fields_block, True)
check("FIELDS 里 course_content 排最前",
      fields_block.strip().split("\n")[0].strip(), "{ key: 'course_content', label: '课程内容' },")

check("form 初始化有 course_content", "course_content: ''," in FD, True)
check("load 回填 fb.course_content", "form.course_content = fb.course_content" in FD, True)
check("persist payload 带 course_content", "course_content: form.course_content || null" in FD, True)
check("snapshot 带 course_content", "course_content: form.course_content || ''" in FD, True)
check("存模板 seeds 带 course_content", "course_content: form.course_content || ''," in FD, True)
check("applyTemplate 的 keys 含 course_content",
      "'course_content', 'performance', 'problems', 'homework', 'next_plan'" in FD, True)
check("快捷短语有 course_content 组", "course_content: ['讲评上次作业 + 新课讲解'" in FD, True)
check("textarea 列表含课程内容（最前）",
      "key:'course_content', label:'课程内容'" in FD, True)
check("willSend 走 FIELDS（自动带上课程内容）",
      "for (const f of FIELDS) {" in FD, True)

# api.js 里 templates 注释也跟上了（可选，不硬断）
AJ = (ROOT / "frontend" / "src" / "api.js").read_text(encoding="utf-8")
check("api.js 五段注释", "五段的可复用文本" in AJ, True)

# ---------------------------------------------------------------- ③ 8001 集成
print("\n③ 8001：建学生+课时 → 存五段反馈 → 查回 → 导出 txt")
con = sqlite3.connect(str(DB))
con.execute("delete from feedbacks")
con.execute("delete from lessons")
con.execute("delete from students")
con.commit()
con.close()

st, stu, _ = js(S, "POST", "/api/students", {"name": "课程内容测试生", "grade": "初二"})
check("建学生", st, 200)
sid = stu["id"]
st, les, _ = js(S, "POST", "/api/lessons", {
    "student_id": sid, "start_at": "2026-10-07T15:00", "duration_min": 60,
    "topic": "二次函数", "status": "done"})
check("建课时", st, 200)
lid = les["id"]

st, fb, _ = js(S, "PUT", f"/api/lessons/{lid}/feedback", {
    "course_content": "讲了二次函数的图像与性质",
    "performance": "状态不错",
    "problems": "对称轴求错",
    "homework": "P58 第 1-6 题",
    "next_plan": "下节讲单调性",
    "rating": 4,
})
check("存五段反馈 HTTP", st, 200)
check("返回值里有 course_content", fb.get("course_content"), "讲了二次函数的图像与性质")
check("archive_txt 写成功", (fb.get("archive_txt") or {}).get("ok"), True)

st, got, _ = js(S, "GET", f"/api/lessons/{lid}/feedback")
check("查回 course_content 原样", got.get("course_content"), "讲了二次函数的图像与性质")
check("其余四段也在", [got.get("performance"), got.get("problems"),
                      got.get("homework"), got.get("next_plan")],
      ["状态不错", "对称轴求错", "P58 第 1-6 题", "下节讲单调性"])

st, raw, hdr = call(S, "GET", f"/api/lessons/{lid}/feedback/export?format=txt&source=fields")
check("导出 txt HTTP", st, 200)
txt = raw.decode("utf-8-sig")
check("txt 里有【课程内容】栏", "【课程内容】" in txt, True)
check("txt 里有课程内容正文", "讲了二次函数的图像与性质" in txt, True)
check("txt 里课程内容排在最前（在课堂表现之前）",
      txt.index("课程内容") < txt.index("课堂表现"), True)

# 归档快照文件里也应该有（翻文件夹就看得见）
arch = fb.get("archive_txt") or {}
arch_p = SCRATCH / "uploads" / (arch.get("path") or "")
if arch.get("ok") and arch_p.is_file():
    arch_txt = arch_p.read_bytes().decode("utf-8-sig")
    check("归档 txt 里也有课程内容", "讲了二次函数的图像与性质" in arch_txt, True)
else:
    check("归档 txt 文件存在", False, True)

# 老字段语义没被破坏：不带 course_content 的请求（旧前端/脚本）不再 500（那才是真 bug），
# 字段随之置空 —— 与其余四段「带了就覆盖、没带就清空」的行为一致（同属老师手写的原料段）。
st, fb2, _ = js(S, "PUT", f"/api/lessons/{lid}/feedback", {
    "performance": "改了一下表现"})
check("不带 course_content 的请求也能存（不再 500）", st, 200)
check("course_content 随之置空（与其余四段行为一致）", fb2.get("course_content"), "")
check("其它字段照常覆盖", fb2.get("performance"), "改了一下表现")

print("\n④ 收尾：删课时（级联带走反馈），真库只读核对")
check("删课时", js(S, "DELETE", f"/api/lessons/{lid}")[0], 200)
con = sqlite3.connect(str(DB))
check("反馈行跟着没了", con.execute("select count(*) from feedbacks").fetchone()[0], 0)
con.close()

st, real_students, _ = js(REAL, "GET", "/api/students")
check("真库没有我造的学生", any(s.get("name") == "课程内容测试生" for s in real_students), False)

print()
print("ALL PASS" if not fails else f"{len(fails)} 项失败: {fails}")
raise SystemExit(1 if fails else 0)
