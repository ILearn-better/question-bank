# -*- coding: utf-8 -*-
"""整体规划 —— 学生级的「一份文档」，可新建 / 查看 / 修改 / 删除。

需求原话（用户 2026-10-16）：
「学生信息界面添加一下"整体规划",放在"考试情况"上方,要能够新建规划,删除规划,
  查看规划,修改规划。我会将文本或者文本文件放到规划,我点开某个规划要能够查看渲染好的
  文件,常见的 markdown、word 这些。(每个规划点开才能详细查看)」

六条贯穿本文件的设计（**改这块之前请先读**）：

① **正文是 Markdown 文本，存在库里**，不是「挂一个文件」。
   需求说「文本**或者**文本文件」—— 两种情况都得能存。正文若只是个文件路径，
   「直接打字写一份规划」这条路就没地方落脚了（总不能再造一个小文件）。
   所以：打字 → 直接进 content；传文件 → 转成 Markdown 也进 content。
   ⚠️ 转换器复用 `services/doc_to_note`（那是**给人看**的那一套：标题、列表、表格、
   图片位置都保留），不是 `file_text`（那套是**给 AI 看**的，只留连续文字，打开就是
   一坨）。两者很容易混，选错了界面上看着「没坏」，只是排版全丢了。

② **转换失败不阻断上传**。Word 里全是图、扫描版 PDF 没有文字层、.zip 根本没有正文 ——
   这些都**照常归档原件**，只是不产出正文，并把原因写在 `PlanFile.note` 里照实显示。
   静默丢弃文件才是最糟的：老师以为存进去了。

③ **原件一并归档**（`students/<学生>/plans/<标题>/`）。转换一定失真，
   正文是「能读的那部分」，原件才是「一定没丢的那部分」—— 与笔记导入文档同一个道理。

④ **原件先进暂存区，保存时才归档**（同 exams 那套）。归档目录名从**标题**来，
   而标题随时会改：边填边落正式目录的话，每改一次标题就得连人带文件搬一次家。
   ⚠️ 传了没保存就关窗会留下暂存目录，由 `_prune_cache()` 按 48 小时清掉。

⑤ **列表只给标题 + 摘要，点开才渲染正文**（需求原话：「每个规划点开才能详细查看」）。
   摘要是**剥掉 Markdown 标记的纯文本**：列表里渲染几十块 KaTeX 会明显卡，
   而且一个学生页被一条几千字的规划撑满之后，往下找考试记录就很难受。

⑥ **附带图片的 Word 会被抽图**，图落在笔记资源目录（`doc_to_note._save_asset` 的固定行为），
   正文里引用成 `/api/notes/files/<name>`。⚠️ 所以 `routers/notes.py` 的
   `_used_images()` **必须把规划正文也算进引用计数** —— 否则「某篇笔记刚好不再引用那张图」
   时会把规划正在用的图画掉，界面上就是一张破图，而且**完全不报错**。

⑦ **正文在学生文件夹里也留一份**（`students/<学生>/plans/<标题>/规划.md`）：
   老师会在文件管理器里翻学生资料，也希望这份规划能被别的工具（或别的电脑）直接读。
   ⚠️ 它是**单向副本** —— 在文件里改了不会同步回库，文件头那行注释就是提示这件事的。
   ⚠️ 文件名**刻意不带标题**（改标题时目录会搬家，文件名跟着变只会多一层麻烦）；
   靠文件头的标记认领「这是上次写的那份」，否则每次保存都会算出 `规划_2.md`、`规划_3.md`。
   ⚠️ 写盘失败（磁盘满、文件被占用）**只记日志，绝不让保存失败**：正文已经在库里了。
"""
from __future__ import annotations

import logging
import mimetypes
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import PlanFile, Student, StudentPlan
from ..schemas import PlanCacheRemoveIn, PlanFileIn, PlanIn, PlanPatch
from ..services import doc_to_note, file_text, storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["plans"])

# 规划是文档，不是整卷扫描件 —— 20MB 足够（考试那边才放到 50MB）。
MAX_BYTES = 20 * 1024 * 1024
# 暂存文件活 48 小时，与考试附件一致。
CACHE_TTL_SECONDS = 48 * 3600

# 能转成正文的类别（= doc_to_note.to_markdown 认识的那些）。
# 其余（图片、压缩包、视频、快捷方式…）**照收不误**，只是不产出正文，见设计②。
CONVERTIBLE = {"word", "html", "pdf", "ppt", "text", "word-legacy", "rtf", "ppt-legacy"}

# 列表摘要的长度。够看清「这条规划是讲什么的」，又不至于把列表撑开。
PREVIEW_CHARS = 140

# 正文在学生文件夹里的那份副本（设计⑦）。名字刻意不带标题：改标题时目录会搬家，
# 文件名跟着变只会多一层麻烦；靠文件头的标记认领「这是上次写的那份」。
BODY_MARK = "<!-- 拾课 · 规划正文（这是库里的正文同步出来的一份副本，改这里不会同步回去） -->"
BODY_NAME = "规划.md"


# ---------------------------------------------------------------- 小工具
def _student_or_404(db: Session, sid: int) -> Student:
    s = db.get(Student, sid)
    if s is None:
        raise HTTPException(404, "学生不存在")
    return s


def _plan_or_404(db: Session, pid: int) -> StudentPlan:
    p = db.get(StudentPlan, pid)
    if p is None:
        raise HTTPException(404, "这条规划不存在")
    return p


_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_MARKS_RE = re.compile(r"(?m)^\s{0,3}(?:#{1,6}|[-*+>]|\d+\.)\s*")
_MD_INLINE_RE = re.compile(r"(\*\*|__|`{1,3}|~~|\*|_)")
_MATH_RE = re.compile(r"\${1,2}([^$]*)\${1,2}")


def preview_of(content: str, n: int = PREVIEW_CHARS) -> str:
    """把 Markdown 正文压成一句**纯文本**摘要，给列表用。

    为什么要剥标记而不是直接截原文：列表里露出一句 `## 一、总体目标` 或者
    `$\\frac{a}{b}$` 对老师毫无信息量 —— 他要的是「这条规划在说什么」。
    公式只留里面的字（`$x^2$` → `x^2`），链接只留文字。

    ⚠️ **永不抛异常**，空正文返回空串：摘要只是列表上的辅助信息，
    它算不出来不该让整个学生页打不开。
    """
    s = str(content or "")
    try:
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = _MD_LINK_RE.sub(r"\1", s)                 # [文字](地址) / ![图](地址) → 文字
        s = re.sub(r"(?s)```.*?```", " ", s)          # 代码块整块略去
        s = _MATH_RE.sub(r"\1", s)                    # $x^2$ → x^2
        s = _MD_MARKS_RE.sub("", s)                   # 行首的 # / - / 1. / > 等
        s = _MD_INLINE_RE.sub("", s)                  # 加粗、行内代码等标记
        s = re.sub(r"<[^>]{0,80}>", " ", s)           # 残留的 HTML 标签
        s = re.sub(r"\s+", " ", s).strip()
    except Exception:                                     # noqa: BLE001
        return ""
    return s if len(s) <= n else s[:n] + "…"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def title_from_content(content: str) -> str:
    """标题没写时，拿正文**第一行非空内容**当名字。

    为什么取第一行而不是整段摘要：老师写规划的习惯就是把标题写在第一行
    （`## 暑期冲刺`）。整段摘要当标题会变成「暑期冲刺 每天两小时 周一三五…」这种
    一长条，列表上看着像出了故障。截到 30 字，还是要认不出来就退「未命名规划」。
    """
    for line in str(content or "").split("\n"):
        s = preview_of(line, 30).rstrip("…").strip()
        if s:
            return s
    return "未命名规划"


def _file_out(f: PlanFile) -> dict:
    return {
        "id": f.id,
        "plan_id": f.plan_id,
        "name": f.name,
        # 落盘名（可能因同名加了 _2）：老师在文件夹里看到的名字和界面上是同一个
        "stored_name": Path(f.stored).name if f.stored else "",
        "kind": f.kind,
        "kind_cn": file_text.KIND_CN.get(f.kind, f.kind),
        "size_bytes": f.size_bytes,
        "converted": bool(f.converted),
        "note": f.note or "",
        "url": f"/api/plan-files/{f.id}/raw",
        "created_at": f.created_at,
    }


def _brief(db: Session, p: StudentPlan) -> dict:
    """列表用：**不含正文**（几千字塞进列表会让学生页很重），只给摘要。"""
    n = len(db.scalars(select(PlanFile.id).where(PlanFile.plan_id == p.id)).all())
    content = p.content or ""
    return {
        "id": p.id,
        "student_id": p.student_id,
        "title": p.title or "",
        "chars": len(content),
        "lines": len([x for x in content.split("\n") if x.strip()]),
        "preview": preview_of(content),
        "file_count": n,
        "has_content": bool(content.strip()),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    }


def _out(db: Session, p: StudentPlan) -> dict:
    """详情用：带完整正文，供前端渲染（渲染在浏览器里做，见 mathRender.js）。"""
    files = db.scalars(
        select(PlanFile).where(PlanFile.plan_id == p.id).order_by(PlanFile.id)
    ).all()
    d = _brief(db, p)
    d["content"] = p.content or ""
    d["files"] = [_file_out(f) for f in files]
    d["dir_rel"] = p.dir_rel or ""
    return d


# ---------------------------------------------------------------- 暂存区
def _prune_cache(ttl: int = CACHE_TTL_SECONDS) -> int:
    """清掉超过 ttl 没被动过的暂存目录（同 exams 的 `_prune_cache`）。

    暂存的前提就是「传了可能最终不保存」。这些文件**不在数据库里**，
    任何按记录的清理逻辑都碰不到它们，所以必须有一处按时间的兜底。
    在上传时顺手清（而不是开后台任务）：单机自用，这一处就够。
    """
    root = config.PLAN_CACHE_DIR
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    for p in list(root.iterdir()):
        try:
            if now - p.stat().st_mtime <= ttl:
                continue
        except OSError:
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                continue
        removed += 1
    return removed


def _cache_path(key: str | None) -> Path | None:
    """规划暂存区的安全路径。⚠️ 必须传 PLAN_CACHE_NAME，
    否则拿考试的暂存区根去校验这份 key —— 两个区的文件会互相看不见。"""
    return storage.safe_cache_path(key, config.PLAN_CACHE_DIR)


def _check_cache(items) -> list[tuple[Path, object]]:
    """检查暂存文件都还在，返回 [(磁盘路径, 入参)]；缺任何一个就 422。

    缺文件要**整个拦下来**而不是静默跳过：老师已经选好的文件如果悄悄没了，
    他会以为存进去了。宁可让他重传。
    """
    prepared, missing = [], []
    for it in (items or []):
        key = (getattr(it, "cache_key", "") or "").strip()
        if not key:
            continue
        src = _cache_path(key)
        if src is None or not src.is_file():
            missing.append(Path(key).name or key)
            continue
        prepared.append((src, it))
    if missing:
        raise HTTPException(
            422,
            "这些文件已经不在暂存区了（可能超过 48 小时被自动清理）："
            + "、".join(missing) + "。请重新上传。")
    return prepared


def _ensure_dir_rel(stu: Student, p: StudentPlan) -> str:
    """这条规划的归档目录（相对 UPLOAD_DIR）。**第一次落文件时才确定** ——
    没传过文件的规划不该在磁盘上留一个空目录。
    同名（两条规划都叫「暑期计划」）加 `_2`，靠 storage.unique_dir_name。
    """
    if p.dir_rel:
        return p.dir_rel
    folder = storage.student_folder(stu)
    parent = config.STUDENTS_DIR / folder / "plans"
    name = storage.unique_dir_name(parent, storage.plan_dir_label(p))
    p.dir_rel = f"students/{folder}/plans/{name}"
    return p.dir_rel


def _commit_files(db: Session, stu: Student, p: StudentPlan, items) -> int:
    """把暂存区的原件搬进正式归档目录并登记，返回搬了几个。"""
    prepared = _check_cache(items)
    if not prepared:
        return 0
    rel = _ensure_dir_rel(stu, p)
    folder = storage.safe_join(rel)
    if folder is None:
        raise HTTPException(500, "归档路径不合法，没法保存原件")
    folder.mkdir(parents=True, exist_ok=True)
    n = 0
    for src, it in prepared:
        try:
            size = src.stat().st_size
        except OSError:
            size = 0
        stored_name = storage.unique_name(folder, src.name)
        shutil.move(str(src), str(folder / stored_name))
        # 原名以界面回传的为准（暂存文件名已经过安全洗字，极端名字会差一两个字符）
        orig = Path(getattr(it, "name", "") or src.name).name or src.name
        db.add(PlanFile(
            owner_id=config.OWNER_ID, plan_id=p.id, name=orig,
            stored=f"{rel}/{stored_name}",
            kind=file_text.kind_for_archive(orig), size_bytes=size,
            converted=1 if getattr(it, "converted", False) else 0,
            note=str(getattr(it, "note", "") or "")[:400],
        ))
        n += 1
    storage.prune_empty_dirs({src.parent for src, _ in prepared}, stop=config.PLAN_CACHE_DIR)
    return n


def _find_body_file(folder: Path) -> Path | None:
    """在这条规划的归档目录里找出**上次写的正文副本**（靠文件头的标记认）。

    为什么要标记而不是按名字找：目录里可能本来就有老师自己传的「规划.md」，
    按名字认会把人家的原件当成自己的副本覆盖掉。也只认 `.md`，别的一概不碰。
    """
    try:
        for f in sorted(folder.glob("*.md")):
            try:
                head = f.read_text(encoding="utf-8", errors="ignore")[:200]
            except OSError:
                continue
            if BODY_MARK in head:
                return f
    except OSError:
        return None
    return None


def _sync_body_file(stu: Student, p: StudentPlan) -> None:
    """把正文同步一份到学生文件夹（设计⑦）：`students/<学生>/plans/<标题>/规划.md`。

    ⚠️ 纯附加动作：正文**已经**在库里了，这里任何意外都只记日志 ——
    磁盘满、文件被别的程序占着，都不该让老师丢掉刚写完的东西。
    """
    has_body = bool((p.content or "").strip())
    if not has_body and not p.dir_rel:
        return                      # 既没正文也没原件：不在磁盘上留空目录
    if not p.dir_rel:
        _ensure_dir_rel(stu, p)     # 只写了正文的规划，也得有个目录放它
    folder = storage.safe_join(p.dir_rel)
    if folder is None:
        return
    old = _find_body_file(folder)
    try:
        if not has_body:
            # 正文被清空了 → 把旧副本删掉，别在文件夹里留一份和库里对不上的旧版本
            if old is not None:
                old.unlink(missing_ok=True)
            return
        folder.mkdir(parents=True, exist_ok=True)
        # 上次那份接着用（覆盖同名副本）；第一次写才落「规划.md」，撞上老师的原件就 _2
        target = old if old is not None else folder / storage.unique_name(folder, BODY_NAME)
        target.write_text(BODY_MARK + "\n\n" + p.content, encoding="utf-8", newline="\n")
    except OSError:
        logger.exception("规划正文副本写盘失败（库里那份没受影响）：%s", p.dir_rel)


def _retarget_dir(db: Session, stu: Student, p: StudentPlan) -> str:
    """标题改了 → 归档目录跟着改名（含磁盘上的文件与库里的 stored）。

    不搬的后果：同一条规划的原件会散在两个文件夹里（改名前的还在旧目录），
    而**界面上一切正常** —— 只能靠人翻文件夹时发现「怎么少了一半」。
    """
    if not p.dir_rel:
        return ""
    folder = storage.student_folder(stu)
    parent = config.STUDENTS_DIR / folder / "plans"
    old_name = Path(p.dir_rel).name
    canonical = storage.plan_dir_label(p)
    # ★ 关键的一步：目录名已经是「canonical」或「canonical_2 / _3」就算跟得上，直接收工。
    #   少了它会出现灾难：某条规划因撞名被分到「暑期计划_2」，之后每次保存都重算目录名
    #   —— 而 canonical 已被别的规划占着，于是 _3、_4、_5 一路涨上去，每保存一次搬一次家。
    #   数字后缀是**这个目录的身份**（谁先占就是谁的），改名时要把它带过去。
    if old_name == canonical or re.fullmatch(rf"{re.escape(canonical)}_\d+", old_name):
        return p.dir_rel
    # ignore=old_name：把自己排除掉，否则算出来的「新名字」可能撞上自己
    new_name = storage.unique_dir_name(parent, canonical, ignore=old_name)
    if new_name == old_name:
        return p.dir_rel
    base = f"students/{folder}/plans"
    old_rel, new_rel = p.dir_rel, f"{base}/{new_name}"
    old_dir, new_dir = storage.safe_join(old_rel), storage.safe_join(new_rel)
    if old_dir is None or new_dir is None:
        return p.dir_rel
    moved = storage.move_dir_contents(old_dir, new_dir)
    for f in db.scalars(select(PlanFile).where(PlanFile.plan_id == p.id)).all():
        old_stored = Path(f.stored or "").name
        if old_stored in moved:
            f.stored = f"{new_rel}/{moved[old_stored]}"
        # 没搬成功的（OSError）保持旧路径不动 —— 把库里改成不存在的路径更糟
    p.dir_rel = new_rel
    return new_rel


def _student_stop(stu: Student | None) -> Path | None:
    """回收空目录时的止步线（学生文件夹本身）。

    ⚠️ 必须显式传给 storage.unlink_rels：它默认一路收到 STUDENTS_DIR 为止，
    会把「刚删空的学生文件夹」也收掉 —— 那个目录老师自己也在用（放讲义、翻资料），
    删掉它看起来就像学生的资料全丢了。
    """
    return None if stu is None else (config.STUDENTS_DIR / storage.student_folder(stu))


def _prune_plan_dirs(stu: Student | None, dir_rel: str) -> None:
    if stu is None or not dir_rel:
        return
    d = storage.safe_join(dir_rel)
    if d is None:
        return
    storage.prune_empty_dirs([d], stop=config.STUDENTS_DIR / storage.student_folder(stu))


# ---------------------------------------------------------------- 上传（转正文 + 原件暂存）
def convert_to_markdown(path: Path, name: str) -> tuple[str, str]:
    """把一份文件转成 Markdown 正文。返回 (markdown, 说明)。

    **失败不抛异常**（设计②）：转不出来就返回空正文 + 一句人话原因，
    原件照样归档。这里的文案会直接显示给老师，所以要说清「为什么」和「怎么办」。
    """
    kind = file_text.kind_of(name)
    if kind is None:
        ext = Path(name).suffix.lower()
        if ext in file_text.ARCHIVE_KINDS:
            return "", "这是快捷方式，本身没有正文可读，已作为原件存好"
        return "", f"这种格式（{ext or '无扩展名'}）不转正文，已作为原件存好"
    if kind in file_text.IMAGE_KINDS:
        return "", "图片不转正文（要看出图里写了什么得靠识别，本机不做），已作为原件存好"
    if kind not in CONVERTIBLE:
        return "", f"{file_text.KIND_CN.get(kind, kind)}不转正文，已作为原件存好"
    try:
        r = doc_to_note.to_markdown(path, name)
    except Exception as e:                                  # noqa: BLE001
        # doc_to_note 抛出的消息本身就是给人看的（它把 file_text 那套文案转过来了）
        return "", str(e) or f"转正文失败：{type(e).__name__}"
    md = str(r.get("markdown") or "")
    warns = [str(w) for w in (r.get("warnings") or [])]
    note = f"已转成正文（{r.get('kind_cn', '')}，{r.get('chars', len(md))} 字）"
    if warns:
        note += "；" + "；".join(warns)
    return md, note


@router.post("/plans/upload")
# 同步 def（不是 async）：下面那句 convert_to_markdown 走 Word COM / PyMuPDF 真转码，
# 秒级同步活。async def 会把它按在事件循环上，单进程服务整站跟着卡。
def upload_plan_file(file: UploadFile = File(...)):
    """传一份文件到规划的暂存区，**顺手转一次 Markdown** 返回给前端。

    为什么上传时就转（而不是保存时才转）：老师要**当场**看见「这份 Word 转出了什么」，
    转不出来也得当场知道为什么（比如扫描版 PDF）。等保存完再说，他就已经退出这一屏了。
    转出来的 Markdown 由前端决定怎么进正文（填进去 / 追加到末尾），服务端不猜。

    ⚠️ 这一步**不写数据库、也不动正式归档目录** —— 保存这条规划时才搬（设计④）。
    """
    _prune_cache()                       # 顺手清过期暂存，不做后台任务
    name = Path(file.filename or "未命名").name         # 只取文件名，防路径穿越
    data = file.file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(422, f"文件太大了（超过 {MAX_BYTES // 1024 // 1024}MB）。"
                                 "规划是文档，确认一下选错文件没有？")
    token = uuid.uuid4().hex[:12]
    d = config.PLAN_CACHE_DIR / token
    d.mkdir(parents=True, exist_ok=True)
    stored_name = storage.unique_name(
        d, storage.safe_token(Path(name).stem, "file") + Path(name).suffix.lower())
    tmp = d / stored_name
    tmp.write_bytes(data)
    markdown, note = convert_to_markdown(tmp, name)
    kind = file_text.kind_for_archive(name)
    return {
        "cache_key": f"{token}/{stored_name}",
        "name": name,
        "size_bytes": len(data),
        "kind": kind,
        "kind_cn": file_text.KIND_CN.get(kind, kind),
        "markdown": markdown,
        "chars": len(markdown),
        "converted": bool(markdown.strip()),
        "note": note,
    }


@router.post("/plans/upload/remove")
def remove_plan_cache(payload: PlanCacheRemoveIn):
    """真删掉暂存区里一份还没保存的原件。

    只在界面上把它从列表里拿掉是**没用的**：文件不在数据库里，
    任何按记录的清理都碰不到它，会一直躺在硬盘上。
    """
    p = _cache_path(payload.cache_key)
    if p is None:
        raise HTTPException(422, "这不是暂存区里的文件")
    deleted = False
    if p.is_file():
        try:
            p.unlink()
            deleted = True
        except OSError:
            deleted = False
    storage.prune_empty_dirs([p.parent], stop=config.PLAN_CACHE_DIR)
    return {"deleted": deleted}


# ---------------------------------------------------------------- 规划 CRUD
@router.get("/students/{sid}/plans")
def list_plans(sid: int, db: Session = Depends(get_db)):
    """这个学生的规划，**最近动过的在最前**。

    为什么按更新时间而不按创建时间：规划是会被反复改的活文档
    （这周补一句、下周改一段），「我上次改的那份」才是要接着看的那份。
    """
    _student_or_404(db, sid)
    rows = db.scalars(select(StudentPlan).where(StudentPlan.student_id == sid)).all()
    rows = sorted(rows, key=lambda p: (p.updated_at or "", p.id), reverse=True)
    return {"items": [_brief(db, p) for p in rows]}


@router.get("/plans/{pid}")
def get_plan(pid: int, db: Session = Depends(get_db)):
    """一条规划的**完整正文** + 原件清单 —— 点开某条规划时才调它（需求原话：
    「每个规划点开才能详细查看」）。列表不带正文，就是为了不让这一步白白变重。"""
    return _out(db, _plan_or_404(db, pid))


@router.post("/plans")
def create_plan(payload: PlanIn, db: Session = Depends(get_db)):
    stu = _student_or_404(db, payload.student_id)
    title = (payload.title or "").strip()
    content = payload.content or ""
    if not title and not content.strip():
        raise HTTPException(422, "规划总得有点内容：写个标题，或者写点正文")
    if not title:
        # 标题空着时用正文第一行兜底：列表里总得有个能认得出来的名字。
        # 与笔记列表同一个习惯（笔记也没有强制标题）。
        title = title_from_content(content)
    p = StudentPlan(owner_id=config.OWNER_ID, student_id=stu.id,
                    title=title, content=content)
    db.add(p)
    db.flush()                                  # 要 p.id 才能登记原件
    _commit_files(db, stu, p, payload.files)
    _sync_body_file(stu, p)                     # 正文在学生文件夹里也留一份副本（设计⑦）
    db.commit()
    return _out(db, p)


@router.patch("/plans/{pid}")
def patch_plan(pid: int, payload: PlanPatch, db: Session = Depends(get_db)):
    """改一条规划。**没传的字段 = 不改**（想清空就显式传空串）。

    顺序有讲究：先落原件、再搬目录。反过来先搬目录的话，万一原件校验失败
    （暂存文件被清了 → 422），磁盘已经搬过了而事务没提交 —— 库和磁盘就对不上了。
    """
    p = _plan_or_404(db, pid)
    stu = _student_or_404(db, p.student_id)
    given = payload.model_dump(exclude_unset=True)

    if "title" in given:
        p.title = (payload.title or "").strip() or p.title
    if "content" in given:
        p.content = payload.content or ""

    if payload.files:
        _commit_files(db, stu, p, payload.files)
    if "title" in given:
        _retarget_dir(db, stu, p)
    # 副本要跟着最新的标题与正文走：目录先搬好，再往里写（搬的时候副本文件会一起搬）。
    _sync_body_file(stu, p)

    p.updated_at = _now()
    db.commit()
    return _out(db, p)


@router.delete("/plans/{pid}")
def delete_plan(pid: int, db: Session = Depends(get_db)):
    """删掉这条规划 —— **连同它的原件一起**。

    为什么敢连文件一起删：原件是这条规划的专属材料（不是共享的题库资源），
    「删掉这条规划」在老师心里就是「这摊东西不要了」。
    删之前先把路径查出来，否则 CASCADE 删完就再也找不到磁盘上该删哪个文件
    （数据库管不了文件系统）。
    """
    p = _plan_or_404(db, pid)
    stu = db.get(Student, p.student_id)
    rels = [f.stored for f in db.scalars(
        select(PlanFile).where(PlanFile.plan_id == pid)).all() if f.stored]
    dir_rel = p.dir_rel or ""
    # ⚠️ 正文副本**不在** plan_files 里（它不是原件），但就躺在同一个目录下 ——
    #   不顺手删掉的话，目录永远不是空的，`_prune_plan_dirs` 不会回收它，
    #   磁盘上就留下一个「规划已删、正文还在」的文件夹。
    body = _find_body_file(storage.safe_join(dir_rel)) if dir_rel else None
    if body is not None:
        try:
            rels.append(storage.rel(body))
        except ValueError:                              # 极少数：路径不在 UPLOAD_DIR 下
            pass
    db.execute(delete(PlanFile).where(PlanFile.plan_id == pid))
    db.delete(p)
    db.commit()
    storage.unlink_rels(rels, stop=_student_stop(stu))
    _prune_plan_dirs(stu, dir_rel)
    return {"ok": True}


# ---------------------------------------------------------------- 原件（已归档）
@router.delete("/plan-files/{fid}")
def delete_plan_file(fid: int, db: Session = Depends(get_db)):
    """删一份已归档的原件（磁盘上的文件一起删）。"""
    f = db.get(PlanFile, fid)
    if f is None:
        raise HTTPException(404, "文件不存在")
    p = db.get(StudentPlan, f.plan_id)
    stu = db.get(Student, p.student_id) if p is not None else None
    stored = f.stored
    dir_rel = p.dir_rel if p is not None else ""
    db.delete(f)
    db.commit()
    storage.unlink_rels([stored] if stored else [], stop=_student_stop(stu))
    _prune_plan_dirs(stu, dir_rel)
    return {"ok": True}


@router.get("/plan-files/{fid}/raw")
def download_plan_file(fid: int, db: Session = Depends(get_db)):
    """取回原件。

    一律 attachment（**没有 inline 开关**，与考试附件不同）：规划的原件是
    Word / PDF / Markdown，浏览器就地打开大多是白屏或者一堆乱码；
    真要预览，正文里已经有转好的那份了。
    """
    f = db.get(PlanFile, fid)
    if f is None or not f.stored:
        raise HTTPException(404, "文件不存在")
    p = storage.safe_join(f.stored)
    if p is None or not p.exists() or not p.is_file():
        raise HTTPException(404, "原件已不在磁盘上")
    name = f.name or p.name
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return FileResponse(
        str(p), media_type=media, filename=name,
        headers={"Content-Disposition":
                 f"attachment; filename=file; filename*=UTF-8''{quote(name)}",
                 "X-Content-Type-Options": "nosniff"})
