# -*- coding: utf-8 -*-
"""备课材料（role=prep）：任意格式上传 + 只归档、不抽文字 + 落进「学生 / 上课日期」文件夹。

需求原话：「备课部分要能上传文件（既要能上传文件，也要能上传文件快捷方式），
上传的文件（以及文件链接）要能够保存在学生文件夹的对应日期文件夹下」。

实现要点（也是这个测试要守住的）：
  · 备课材料复用 lesson_files / services.storage 那套归档布局，
    只是 role='prep' —— 与「上课文件」（material）、「作业原件」（homework）分成三份
    互相看不见的清单（否则备课材料会被 AI 润色当成参考资料读进去）。
  · **不限格式**：备课要用的东西什么样都有，尤其老师电脑上做好的 .lnk 快捷方式 ——
    它根本不在「能抽出文字」的白名单里，卡白名单就等于把需求挡在门外。
  · **刻意不抽文字**：status 记 'archived'（「没打算读」），不是 'ok' + 0 字
    （那会被界面显示成「读出 0 字」，像出了故障）。
  · 落盘用**原名**（要能一眼看出「三角函数课件.lnk」指向什么），同名加 _2。
  · 归档目录 = students/u<id>_<姓名>/<上课日期>/ —— 日期取课时 start_at，不是上传当天。

为什么不起服务：上传/列清单/删除都是几张表 + 磁盘上的纯逻辑，
把 router 里的函数**直接调用**就能全覆盖 —— 比 uvicorn 快一个量级，不占端口，
也不会因为「服务没起」假失败（本项目在这个坑上栽过）。

⚠️ 临时库必须在 import app.db **之前**设好 —— engine 绑定在 import 时定死。
"""
from __future__ import annotations

import asyncio
import inspect
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_prep_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.db 之前设
os.environ["SHIKE_DB_PATH"] = str(TMP / "prep.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

from fastapi import HTTPException                     # noqa: E402
from sqlalchemy import select                         # noqa: E402
from starlette.datastructures import UploadFile       # noqa: E402

from app import config                                # noqa: E402
from app.db import SessionLocal, engine               # noqa: E402
from app.models import Base, Lesson, LessonFile, Student   # noqa: E402
from app.routers import lesson_files as LF            # noqa: E402
from app.services import file_text, storage           # noqa: E402

assert str(config.DB_PATH) == str(TMP / "prep.db"), f"临时库没生效：{config.DB_PATH}"
assert str(config.UPLOAD_DIR).startswith(str(TMP)), f"归档目录没落到临时区：{config.UPLOAD_DIR}"

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"

fails: list[str] = []
total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global total
    total += 1
    line = f"  [{'OK ' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"   <- {detail}"
    print(line)
    if not ok:
        fails.append(name)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def node_bin() -> str | None:
    for cand in (shutil.which("node"),
                 r"C:/Users/6/.workbuddy/binaries/node/versions/22.22.2-3/node.exe"):
        if cand and Path(cand).exists():
            return str(cand)
    return None


# ================================================================ 库 / 数据
def reset() -> None:
    """每次都从干净的临时库 + 干净的 uploads 开始（否则第二次跑会假失败）。"""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    shutil.rmtree(TMP / "data" / "uploads", ignore_errors=True)


def seed(db, name: str = "李芹旭", start_at: str = "2026-09-26T15:00"):
    stu = Student(owner_id=config.OWNER_ID, name=name)
    db.add(stu)
    db.flush()
    ls = Lesson(owner_id=config.OWNER_ID, student_id=stu.id, start_at=start_at,
                duration_min=60, status="scheduled")
    db.add(ls)
    db.commit()
    return stu, ls


def uf(filename: str, data: bytes = b"x") -> UploadFile:
    """造一个够 router 用的 UploadFile（真上传走 multipart，这里只要 .read()）。"""
    return UploadFile(io.BytesIO(data), filename=filename)


def call(r):
    """端点可能是 `async def` 也可能已改成同步 `def`：统一落地成返回值。

    为什么需要它：R3 把上传端点从 `async def` 改成 `def` 之后，直接
    `asyncio.run(端点(...))` 会以 `ValueError: a coroutine was expected` 崩在
    **测试脚手架**里 —— 端点本身是好的，它已经正常返回了那个 dict。
    """
    return asyncio.run(r) if inspect.isawaitable(r) else r


def up(lid: int, filename: str, data: bytes = b"x", role: str = "prep", db=None) -> dict:
    return call(LF.upload_file(lid, file=uf(filename, data), role=role, db=db))


def expect_422(fn) -> tuple[bool, str]:
    try:
        fn()
    except HTTPException as e:
        return e.status_code == 422, f"HTTP {e.status_code} {e.detail}"
    except Exception as e:                                   # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    return False, "没有抛异常"


# ================================================================ ① 类别映射
def t_kind() -> None:
    section("① 类别映射（纯函数，不碰库）")
    check("ARCHIVE_KINDS 认 .lnk", file_text.ARCHIVE_KINDS.get(".lnk") == "link")
    check("ARCHIVE_KINDS 认 .url", file_text.ARCHIVE_KINDS.get(".url") == "url")
    check("kind_for_archive(.lnk) = link", file_text.kind_for_archive("三角函数课件.lnk") == "link")
    check("kind_for_archive(.url) = url", file_text.kind_for_archive("网盘链接.url") == "url")
    check("kind_for_archive(.zip) 按扩展名兜底 = ZIP",
          file_text.kind_for_archive("材料包.zip") == "ZIP", file_text.kind_for_archive("a.zip"))
    check("kind_for_archive(.mp4) = MP4", file_text.kind_for_archive("录课.mp4") == "MP4")
    check("kind_for_archive(.pdf) 沿用原类别（不另造一套）",
          file_text.kind_for_archive("讲义.pdf") == "pdf")
    check("kind_for_archive 无扩展名也有值（标签不能空着）",
          file_text.kind_for_archive("README") == "FILE", file_text.kind_for_archive("README"))
    check("KIND_CN: link → 快捷方式", file_text.KIND_CN.get("link") == "快捷方式")
    check("KIND_CN: url → 网址快捷方式", file_text.KIND_CN.get("url") == "网址快捷方式")
    check(".lnk **不在**可读白名单里（所以别的 role 会拒它）",
          file_text.kind_of("x.lnk") is None)
    check("ACCEPT_ANY 是空串 —— 设了 accept，文件对话框就会把 .lnk 藏起来",
          file_text.ACCEPT_ANY == "")
    check("归档说明里点名了快捷方式",
          "快捷方式" in file_text.SUPPORTED_NOTE_ARCHIVE, "")
    check("ROLES 里有 prep", "prep" in LF.ROLES, str(LF.ROLES))
    check("ROLES 仍含 material / homework（老角色没被挤掉）",
          {"material", "homework"} <= set(LF.ROLES), str(LF.ROLES))


# ================================================================ ② 上传
def t_upload() -> None:
    section("② 上传（role=prep：任意格式、只归档）")
    reset()
    db = SessionLocal()
    try:
        stu, ls = seed(db)

        r = up(ls.id, "三角函数课件.lnk", b"\x4c\x00\x00\x00FAKE-LNK", db=db)
        check(".lnk 上传成功（不被白名单挡住）", r["name"] == "三角函数课件.lnk", str(r))
        check("status=archived（「没打算读」，不是「读出 0 字」）",
              r["status"] == "archived", r["status"])
        check("archived 不冒充 ok", r["status"] != "ok", "")
        check("chars = 0 且没有正文预览", r["chars"] == 0 and r["preview"] == "",
              f"{r['chars']} / {r['preview']!r}")
        check("kind=link", r["kind"] == "link", r["kind"])
        check("kind_cn=快捷方式（界面上要能看懂）", r["kind_cn"] == "快捷方式", r["kind_cn"])
        check("库里 name 存的是上传时的原名", r["name"] == "三角函数课件.lnk", r["name"])
        check("stored_name 就是文件名（prep 保留原名，不走「学生_日期_序号」）",
              r["stored_name"] == "三角函数课件.lnk", r["stored_name"])

        exp_rel = f"students/u{stu.id}_李芹旭/2026-09-26/三角函数课件.lnk"
        check("落盘在「学生 / 上课日期」目录下", r["stored"] == exp_rel, r["stored"])
        p = config.UPLOAD_DIR / exp_rel
        check("磁盘上真有这个文件", p.is_file(), str(p))
        check("内容就是上传的字节（没被改写）",
              p.read_bytes() == b"\x4c\x00\x00\x00FAKE-LNK", p.read_bytes()[:20].hex())
        check("目录名里的日期取自课时 start_at，不是上传当天",
              "2026-09-26" in r["stored"] and "2026-10-11" not in r["stored"], r["stored"])

        # 原本会被 422 挡掉的格式
        for fn, want in (("错题整理.zip", "ZIP"), ("录课回放.mp4", "MP4"),
                         ("板书照片.heic", "图片"), ("思维导图.xmind", "XMIND")):
            rr = up(ls.id, fn, b"binary-data", db=db)
            check(f"{fn} 也能传（备课材料不限格式）", rr["status"] == "archived", str(rr.get("reason")))
            check(f"{fn} 的类别标签 = {want}", rr["kind_cn"] == want, rr["kind_cn"])

        # 已有类别不另造
        rr = up(ls.id, "讲义.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", db=db)
        check("备课材料里的 .pdf 也归档（不抽文字，所以不校验能不能读懂）",
              rr["status"] == "archived", rr["status"])
        check(".pdf 的 kind_cn 仍是 PDF", rr["kind_cn"] == "PDF", rr["kind_cn"])

        # ---- 别的 role 仍然拒 .lnk：那是要送进 AI 的，读不出文字没意义 ----
        ok422, d = expect_422(lambda: up(ls.id, "x.lnk", b"x", role="material", db=db))
        check("material 角色拒收 .lnk（422）", ok422, d)
        ok422, d = expect_422(lambda: up(ls.id, "x.lnk", b"x", role="homework", db=db))
        check("homework 角色也拒收 .lnk（422）", ok422, d)
        ok422, d = expect_422(lambda: up(ls.id, "x.rar", b"x", role="material", db=db))
        check("material 角色拒收 .rar（旧白名单行为没被改坏）", ok422, d)

        ok422, d = expect_422(lambda: up(ls.id, "x.lnk", b"x", role="nonsense", db=db))
        check("不认识的 role 报 422（不是静默当成 material）", ok422, d)

        ok422, d = expect_422(lambda: up(999999, "x.lnk", b"x", db=db))
        check("课时不存在报 404 而不是崩", not ok422 and "404" in d, d)

        # ---- 大小上限 ----
        old = LF.MAX_BYTES
        LF.MAX_BYTES = 64
        try:
            ok422, d = expect_422(lambda: up(ls.id, "大文件.zip", b"z" * 200, db=db))
            check("超过上限照样拦住（上限也对 prep 生效）", ok422, d)
        finally:
            LF.MAX_BYTES = old
    finally:
        db.close()


# ================================================================ ③ 命名与避重
def t_naming() -> None:
    section("③ 落盘命名（保留原名 + 同名避重）")
    reset()
    db = SessionLocal()
    try:
        stu, ls = seed(db)
        a = up(ls.id, "三角函数课件.lnk", b"A", db=db)
        b = up(ls.id, "三角函数课件.lnk", b"B", db=db)
        check("同一天传两份同名：第二份加 _2", b["stored_name"] == "三角函数课件_2.lnk",
              b["stored_name"])
        check("两份的库内 name 都是原名（界面才对得上）",
              a["name"] == b["name"] == "三角函数课件.lnk", f"{a['name']} / {b['name']}")
        check("两份落在同一个日期目录里",
              a["stored"].rsplit("/", 1)[0] == b["stored"].rsplit("/", 1)[0], a["stored"])
        check("两份内容各自独立（没互相覆盖）",
              (config.UPLOAD_DIR / a["stored"]).read_bytes() == b"A"
              and (config.UPLOAD_DIR / b["stored"]).read_bytes() == b"B", "")
        c = up(ls.id, "三角函数课件.lnk", b"C", db=db)
        check("第三份加 _3", c["stored_name"] == "三角函数课件_3.lnk", c["stored_name"])

        # 带路径分隔符的文件名（浏览器理论上不会给，但接口不设防就等着出事）
        d = up(ls.id, "..\\..\\evil.lnk", b"D", db=db)
        check("文件名里的 ../ 被剥掉（防路径穿越）",
              ".." not in d["stored"] and d["stored"].startswith("students/"), d["stored"])

        # 学生姓名里的非法字符
        stu2, ls2 = seed(db, name="李/芹*旭", start_at="2026-10-02T19:00")
        e = up(ls2.id, "讲义.lnk", b"E", db=db)
        check("学生姓名里的非法字符被洗掉（目录名安全）",
              f"u{stu2.id}_李_芹_旭" in e["stored"], e["stored"])
        check("换一个学生 = 另一个归档目录（不会串）",
              e["stored"] != a["stored"], e["stored"])
    finally:
        db.close()


# ================================================================ ④ 清单隔离
def t_lists() -> None:
    section("④ 三份清单互相看不见")
    reset()
    db = SessionLocal()
    try:
        stu, ls = seed(db)
        up(ls.id, "备课讲义.lnk", b"P", db=db)
        up(ls.id, "上课课件.pdf", b"%PDF-1.4\n", role="material", db=db)
        up(ls.id, "作业照片.jpg", b"\xff\xd8\xff", role="homework", db=db)

        rp = LF.list_files(ls.id, role="prep", db=db)
        rm = LF.list_files(ls.id, role="material", db=db)
        rh = LF.list_files(ls.id, role="homework", db=db)
        check("prep 清单只有 1 份", len(rp["items"]) == 1, str(len(rp["items"])))
        check("prep 清单里就是那份快捷方式",
              rp["items"][0]["name"] == "备课讲义.lnk", str(rp["items"]))
        check("material 清单看不到备课材料",
              all(i["role"] == "material" for i in rm["items"]), str(rm["items"]))
        check("homework 清单看不到备课材料",
              all(i["role"] == "homework" for i in rh["items"]), str(rh["items"]))

        check("prep 返回 archive_only=True（界面据此不显示「抽出 N 字」）",
              rp.get("archive_only") is True, str(rp.get("archive_only")))
        check("material 的 archive_only 是 False",
              rm.get("archive_only") is False, str(rm.get("archive_only")))
        check("prep 的 accept 为空串（前端不设白名单，.lnk 才选得到）",
              rp["accept"] == "", repr(rp["accept"]))
        check("material 的 accept 仍是非空白名单", len(rm["accept"]) > 0, repr(rm["accept"]))
        check("prep 的 supported 文案说明「只归档、不提取文字」",
              "不提取文字" in rp["supported"], rp["supported"])
        check("rel_dir 指向「学生 / 上课日期」",
              rp["rel_dir"] == f"students/u{stu.id}_李芹旭/2026-09-26", rp["rel_dir"])

        ok422, d = expect_422(lambda: LF.list_files(ls.id, role="nope", db=db))
        check("list 也不认识角色时报 422", ok422, d)
    finally:
        db.close()


# ================================================================ ⑤ 删除 / 清理
def t_delete() -> None:
    section("⑤ 删除与连带清理")
    reset()
    db = SessionLocal()
    try:
        stu, ls = seed(db)
        a = up(ls.id, "备课讲义.lnk", b"P", db=db)
        m = up(ls.id, "上课课件.pdf", b"%PDF-1.4\n", role="material", db=db)
        pa = config.UPLOAD_DIR / a["stored"]
        pm = config.UPLOAD_DIR / m["stored"]
        check("两份都在盘上", pa.is_file() and pm.is_file(), "")

        LF.delete_file(a["id"], db=db)
        check("删备课材料：那一份没了", not pa.exists(), str(pa))
        check("删备课材料：同目录的上课文件**没被连坐**", pm.is_file(), str(pm))
        check("库里那行也没了",
              db.scalar(select(LessonFile).where(LessonFile.id == a["id"])) is None, "")

        # 删课时要把 prep 文件一起收掉（数据库管不了文件系统，只能自己算）
        b = up(ls.id, "再来一份.zip", b"ZIP", db=db)
        pb = config.UPLOAD_DIR / b["stored"]
        rels = storage.collect_lesson_assets(db, [ls.id])
        check("collect_lesson_assets 认得出备课材料（删课时能一起收）",
              b["stored"] in rels, str(rels))
        n = storage.unlink_rels(rels)
        check("清掉后盘上真没了", not pb.exists(), str(pb))
        check("确实删掉了文件（返回数 > 0）", n > 0, str(n))

        # 逐条删光之后，空的日期目录应当被收掉
        for it in LF.list_files(ls.id, role="material", db=db)["items"]:
            LF.delete_file(it["id"], db=db)
        d = config.STUDENTS_DIR / f"u{stu.id}_李芹旭" / "2026-09-26"
        check("删空之后日期目录被收掉（不留空壳）", not d.exists(), str(d))
        # 学生目录空了也一并收掉（prune_empty_dirs 的老行为），**再往上绝不能再动**
        check("归档根目录还在（清空目录时绝不允许铲到 STUDENTS_DIR 之上）",
              config.STUDENTS_DIR.exists(), str(config.STUDENTS_DIR))
    finally:
        db.close()


# ================================================================ ⑥ 学生资料清单
def t_student_files() -> None:
    section("⑥ 学生资料清单带上 role（备课材料也要能在这里认出来）")
    reset()
    db = SessionLocal()
    try:
        stu, ls = seed(db)
        up(ls.id, "备课讲义.lnk", b"P", db=db)
        up(ls.id, "上课课件.pdf", b"%PDF-1.4\n", role="material", db=db)
        r = LF.student_files(stu.id, db=db)
        items = r["dates"][0]["items"]
        by_name = {i["name"]: i for i in items}
        check("备课材料出现在学生资料清单里（它就是归档目录的索引）",
              "备课讲义.lnk" in by_name, str(list(by_name)))
        check("清单里带 role=prep", by_name.get("备课讲义.lnk", {}).get("role") == "prep",
              str(by_name.get("备课讲义.lnk")))
        check("上课文件仍是 role=material",
              by_name.get("上课课件.pdf", {}).get("role") == "material", "")
        check("快捷方式的 kind_cn 显示成「快捷方式」",
              by_name.get("备课讲义.lnk", {}).get("kind_cn") == "快捷方式", "")
        check("归档绝对路径指向临时区（没碰真库/真数据）",
              str(TMP) in r["abs_dir"], r["abs_dir"])
    finally:
        db.close()


# ================================================================ ⑦ 前端
def extract_template(path: Path) -> str | None:
    src = path.read_text(encoding="utf-8")
    key = "template: `"
    i = src.find(key)
    if i < 0:
        return None
    j = src.find("`", i + len(key))
    if j < 0:
        return None
    return src[i + len(key):j]


COMPILE_JS = r"""
const fs=require('fs'),vm=require('vm');
const VUE=__VUE__;
function mkDoc() {
  const el=()=>{const o={_raw:'',_foo:'',_text:''};
    Object.defineProperty(o,'innerHTML',{set(v){o._raw=v;const m=/foo="([\s\S]*)"/.exec(v);
      o._foo=m?m[1]:'';o._text=v;},get(){return o._raw;}});
    Object.defineProperty(o,'children',{get(){const dec=s=>String(s).replace(/&quot;/g,'"')
      .replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
      return [{getAttribute:()=>dec(o._foo)}];}});
    Object.defineProperty(o,'textContent',{get(){return String(o._text)
      .replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'"');}});
    return o;};
  return {createElement:()=>el(),querySelector:()=>null,createTextNode:()=>({})};
}
const ctx={console,document:mkDoc(),window:{}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync(VUE,'utf8'),ctx);
function comp(tpl){try{ctx.Vue.compile(tpl);return 'OK';}
  catch(e){return 'FAIL '+String(e.message).slice(0,140);}}
// 自检：这份模板孤立 v-else-if，必须报错，否则说明校验是空转
console.log('SELFTEST ' + comp('<div><template v-else-if="b">x</template></div>'));
console.log('SELFTEST_GOOD ' + comp('<div><p v-if="a">1</p><p v-else>2</p></div>'));
console.log('TPL ' + comp(fs.readFileSync(process.argv[2],'utf8')));
"""


def t_frontend() -> None:
    section("⑦ 前端守卫（结构 + Vue 真编译器校验模板）")
    dlg = FE / "src" / "components" / "PrepDialog.js"
    api = FE / "src" / "api.js"
    check("PrepDialog.js 存在", dlg.exists(), str(dlg))
    if not dlg.exists():
        return
    src = dlg.read_text(encoding="utf-8")

    check("引了 lessonFilesApi", "lessonFilesApi" in src, "")
    check("列清单用 role='prep'", re.search(r"lessonFilesApi\.list\([^)]*'prep'", src) is not None,
          str(re.search(r"lessonFilesApi\.list\([^)]*\)", src)))
    check("上传也用 role='prep'", "'prep'" in src and "lessonFilesApi.upload" in src, "")
    check("上传支持多选（multiple）", re.search(r'type="file"\s+multiple', src) is not None,
          str(re.search(r'type="file"[^>]*', src)))
    # ⚠️ 给 input 设 accept 白名单，文件对话框就会把 .lnk 藏起来 —— 需求直接失效
    check("file input **没有** accept（设了白名单就选不到 .lnk）",
          re.search(r'type="file"[^>]*\baccept=', src) is None,
          str(re.search(r'type="file"[^>]*', src)))
    check("界面上点名「快捷方式」（不然老师不知道 .lnk 能传）",
          "快捷方式" in src, "")
    check("显示归档位置（老师才知道文件去哪儿找）",
          "归档位置" in src and "archiveDir" in src, "")
    check("提示按「学生 / 上课日期」存放",
          "上课日期" in src, "")
    check("逐份上传时分母不为 0（一次选 N 份要能显示进度）",
          "upTotal" in src and "upIndex" in src, "")
    check("删除前有确认（磁盘上的文件会真删）",
          "window.confirm" in src, "")
    check("用的类都是项目已有的（.tag / .btn / .chip），没引新 UI 库",
          all(c in src for c in ("class=\"tag\"", "class=\"btn", "class=\"chip")), "")

    # ---- 题库挑题的题干：渲染成真公式 + 鼠标悬停展开 + **图片题显示原貌图**（2026-10-11）----
    check("引了渲染管线 mathRender（不自己写 marked / katex 调用）",
          "from '../mathRender.js'" in src and "renderRichEl" in src and "ensureLibs" in src, "")
    check("题库行的题干改成渲染容器（q-rich + setRichEl），不再直接插值 brief(r)",
          'class="q-rich"' in src and "setRichEl('q:'" in src
          and "{{ tab === 'question' ? brief(r) : r.title }}" not in src, "")
    # KaTeX 的 auto-render 会把容器里的文本节点整个换掉 —— 容器里绝不能有插值
    check("渲染容器是空 div（里面有插值就会被 KaTeX 换掉，界面卡在第一版）",
          re.search(r'class="q-rich"\s+:ref="[^"]*"></div>', src) is not None,
          str(re.search(r'class="q-rich"[^>]*>[^<]*', src)))
    check("重渲染用 flush:'post'（等 v-for 把容器挂上去）", "flush: 'post'" in src, "")
    check("只在题库页签下渲染（笔记页签的行里没有 q-rich 容器）",
          "if (tab.value === 'question') renderRich()" in src, "")
    check("列表 key 带页签前缀（题库 id 与笔记 id 撞号时 DOM 不会串行）",
          "tab + ':' + r.id" in src, "")
    check("还留着 brief（已选 chips 的 title 仍要纯文本）",
          "brief(r)" in src, "")

    # ★ 图片题：没有文本的题在列表里显示那张原貌图，不再是「（图片题，没有文本）」
    check("★ 形态判定走 questionForm（与出卷页同一份，不在这页里另写一套）",
          "from '../questionForm.js'" in src and "chooseForm" in src, "")
    check("★ 图片题渲染 <img class=\"q-thumb\">，src 取 stemImage(r)",
          'class="q-thumb"' in src and "stemImage(r)" in src, "")
    check("★ 「（图片题，没有文本）」这句占位已删除（注释里提到旧文案不算）",
          "（图片题，没有文本）" not in re.sub(
              r"<!--.*?-->", "", extract_template(dlg) or "", flags=re.S), "")
    check("图片旁边写清「为什么显示成图」（imageNote）", "imageNote(r" in src, "")
    check("缩略图点开能看原图", "q-thumb-link" in src and 'target="_blank"' in src, "")
    check("缩略图懒加载（一次 50 条结果，别一进来就全量拉图）", 'loading="lazy"' in src, "")

    css = (FE / "app.css").read_text(encoding="utf-8")
    check("列表里题干默认只露一截（.pick-row .q-clamp 有 max-height）",
          re.search(r"\.pick-row \.q-clamp\s*\{[^}]*max-height", css) is not None, "")
    check("★ 鼠标移上去自动展开（.pick-row:hover .q-clamp → max-height:none）",
          re.search(r"\.pick-row:hover \.q-clamp\s*\{\s*max-height:\s*none", css) is not None,
          str(re.search(r"\.pick-row:hover[^{]*\{[^}]*", css)))
    check("折起来时底部渐隐，提示「下面还有」",
          ".q-clamp::after" in css and "linear-gradient" in css, "")
    check("题干是多行块，按钮顶对齐（.pick-row-top）",
          ".pick-row.pick-row-top" in css, "")
    check("挑题列表里的题目原貌图有样式（.q-thumb / .q-thumb-sm）",
          re.search(r"^\.q-thumb\b", css, re.M) is not None
          and re.search(r"^\.q-thumb-sm\b", css, re.M) is not None, "")

    api_src = api.read_text(encoding="utf-8")
    check("api.js 注释写明了 prep 是「只归档不抽文字」",
          "prep" in api_src and "不抽文字" in api_src, "")

    # ---- 真编译器：抓 compiler-30 这类模板错误 ----
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上，跳过模板编译校验")
        return
    vue = FE / "vendor" / "vue.global.prod.js"
    check("vendor/vue.global.prod.js 存在", vue.exists(), str(vue))
    if not vue.exists():
        return
    tpl = extract_template(dlg)
    check("能从 PrepDialog.js 抽出 template 字符串", bool(tpl))
    if not tpl:
        return
    tp = TMP / "prep_tpl.html"
    tp.write_text(tpl, encoding="utf-8")
    js = TMP / "compile_check.js"
    js.write_text(COMPILE_JS.replace("__VUE__", repr(str(vue))), encoding="utf-8")
    r = subprocess.run([node, str(js), str(tp)], capture_output=True, text=True, timeout=90)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if line.startswith("SELFTEST "):
            check("自检：故意写错的模板**必须**编译失败（证明校验没空转）",
                  line.split(" ", 1)[1].startswith("FAIL"), line)
        elif line.startswith("SELFTEST_GOOD "):
            check("自检：合法模板编译通过", line.split(" ", 1)[1].strip() == "OK", line)
        elif line.startswith("TPL "):
            v = line[4:].strip()
            check("PrepDialog 模板能被 Vue 真编译器编译（抓 compiler-30 这类错）",
                  v == "OK", v)
    check("node 侧输出完整（进程没中途挂掉）",
          "SELFTEST_GOOD" in out and "TPL " in out, out.strip()[-200:])

    # ---- 真挂载：把 PrepDialog 挂一遍，数渲染痕迹 ----
    # 静态断言只能证明「源码里写了 setRichEl」，证明不了**挂载后容器真的被填上 KaTeX**。
    mount = ROOT / "frontend" / "prep_mount_check.js"
    check("frontend/prep_mount_check.js 存在（SPA 组件的真挂载检查）",
          mount.exists(), str(mount))
    if mount.exists():
        rr = subprocess.run([node, str(mount)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=240)
        mout = (rr.stdout or "") + (rr.stderr or "")
        if "SKIP" in mout and "jsdom" in mout:
            print("  [SKIP] 没找到 jsdom，跳过真挂载检查")
        else:
            check("★ 真挂载：公式渲染出 .katex / 悬停容器在 / 切页签不串行",
                  rr.returncode == 0,
                  (mout.strip().splitlines() or [""])[-1])


# ================================================================ ⑧ 一致性
def t_consistency() -> None:
    section("⑧ 一致性守卫（别人别把这几个前提改坏）")
    fb = (ROOT / "backend" / "app" / "routers" / "feedbacks.py").read_text(encoding="utf-8")
    check("AI 润色只取 material —— 备课材料不会被当成参考资料读进去",
          'LessonFile.role == "material"' in fb, "")
    hw = (ROOT / "backend" / "app" / "routers" / "homeworks.py").read_text(encoding="utf-8")
    check("作业只取 homework", 'LessonFile.role == "homework"' in hw, "")
    st = (ROOT / "backend" / "app" / "services" / "storage.py").read_text(encoding="utf-8")
    check("cleanup 不经 role 过滤（删课时备课材料也会被收走）",
          re.search(r"select\(LessonFile\.stored\)\.where\(LessonFile\.lesson_id\.in_",
                    st) is not None, "")
    lf = (ROOT / "backend" / "app" / "routers" / "lesson_files.py").read_text(encoding="utf-8")
    check("prep 跳过白名单校验（否则 .lnk 传不上来）",
          re.search(r'role\s*!=\s*"prep"\s+and\s+file_text\.kind_of', lf) is not None, "")
    check("prep 不抽文字（status 记 archived）", '"archived"' in lf, "")
    check("落盘命名只分两套（homework 单独一套，material/prep 保留原名）",
          lf.count("homework_file_name(") == 1, str(lf.count("homework_file_name(")))
    model = (ROOT / "backend" / "app" / "models.py").read_text(encoding="utf-8")
    check("lesson_files.role 是纯 String（加 prep 不用改库结构 / 不用迁移）",
          re.search(r'role: Mapped\[str\] = mapped_column\(String', model) is not None, "")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README 有「备课材料」的说明（不是靠别处的「快捷方式」蒙过去的）",
          "备课材料" in readme, "")
    check("README 写明了归档布局按「学生 / 上课日期」",
          "上课日期" in readme, "")
    prep = (ROOT / "frontend" / "src" / "components" / "PrepDialog.js").read_text(encoding="utf-8")
    check("备课挑题的渲染只走 mathRender（别处升级管线时才不会漏掉这一份）",
          "renderMathInElement" not in prep and "window.marked" not in prep, "")
    check("形态判定只有一份（src/questionForm.js）：备课与出卷不再各写一套",
          "from '../questionForm.js'" in prep
          and not re.search(r"function\s+chooseForm", prep), "")


# ================================================================ main
def main() -> int:
    print("备课材料上传 · 自包含检查（临时库、不起服务、不碰真数据）")
    t_kind()
    t_upload()
    t_naming()
    t_lists()
    t_delete()
    t_student_files()
    t_frontend()
    t_consistency()
    print(f"\n共 {total} 项，失败 {len(fails)} 项")
    if fails:
        print("失败项：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
