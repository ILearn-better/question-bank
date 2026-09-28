# -*- coding: utf-8 -*-
"""笔记的备份与迁移：打成一个 zip，能在别的设备上导回来。

为什么是「一整包」而不是「导出 Markdown 文件」了事：
  笔记不只是文本 —— 还有**目录归属、手排顺序、置顶、板书笔画（矢量 JSON）**，
  正文里还嵌着配图。只导 .md 的话这些全丢，导回来是一堆平铺的散文件，
  目录得重排、板书没了。

为什么包里**还要放一份人可读的 .md**（机器导入其实只看 notes.json）：
  备份要能活过这个程序本身。哪天这应用跑不起来了，老师把 zip 解开丢进资源管理器，
  照样能按目录把每一篇读出来 —— 这和「作业原件改名成学生_日期_序号」是同一个用意：
  存下来的东西应当脱离程序也能自证、能读。

包结构：

    manifest.json                     格式与版本、导出时间、数量、涉及的体系（按 code）
    notes.json                        机器导入用：目录 + 每篇的完整数据 + 引用到的配图名
    notes/<体系>/<目录…>/<标题>.md     人看的副本（正文 + 一行来源说明）
    目录树.md                          一页纸的目录总览
    images/                           正文引用到的配图原文件

导入的三条规矩：

  1. **目录按名字合并**：同一个体系下同名目录就当同一个（复用），不重复建。
     于是同一个包导入两次不会长出两套目录。
  2. **体系按 code 认，不按 id**：另一台设备上 `curricula.id` 不一定一样，
     但 `code`（`dse-math` 这类）是稳定的。认不出来就落到「未归档」并给一条警告，
     绝不因为一个体系对不上就把整包拒了。
  3. **笔记 id 相同 = 同一篇**：默认**跳过**（保留本机那份），要覆盖得显式说。
     默认跳过是因为「覆盖」会无声盖掉本机较新的内容 —— 迁移场景下重名 id
     恰恰说明两边同源，留着本机的更安全。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Curriculum, Note, NoteFolder
from . import note_folders
from .note_folders import _dedupe_name, _ensure_roots, UNFILED_NAME

router = APIRouter(prefix="/api/notes-backup", tags=["notes"])

FORMAT = "shike-notes"
VERSION = 1

# 包多大算太大：全量笔记 + 配图，正常几 MB。给个上限免得一口气把内存吃满
MAX_ZIP_BYTES = 300 * 1024 * 1024

# 笔记正文里引用配图的形式：![](/api/notes/files/nb_ab12cd34ef56.png)
# 这是**唯一的定义处**（routers/notes.py 的引用计数也用它）——
# 两处各写一份的话，改了一处另一处会静默漏掉配图，删笔记时就留下垃圾文件。
IMAGE_RE = re.compile(r"/api/notes/files/([A-Za-z0-9_.\-]+)")


def referenced_images(content: str) -> set[str]:
    """正文引用到的配图文件名。"""
    return set(IMAGE_RE.findall(content or ""))


def _safe(name: str, fallback: str = "未命名", maxlen: int = 60) -> str:
    """给「人读的 .md / 目录」用的文件名清洗（挡掉路径分隔符与控制字符）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t\x00-\x1f]', "_", str(name or "")).strip(". ")
    return s[:maxlen] or fallback


# ================================================================ 导出
def export_zip(db: Session) -> tuple[bytes, str]:
    """把全部笔记打成一个 zip。返回 (字节, 建议文件名)。"""
    roots = _ensure_roots(db)          # 顺手把「4 个体系 + 未归档」的根补齐，导出的树才完整
    curr = list(db.scalars(select(Curriculum)).all())
    code_of = {c.id: c.code for c in curr}
    name_of = {c.id: c.name for c in curr}

    folders = list(db.scalars(select(NoteFolder).where(NoteFolder.is_root == 0)).all())
    kids: dict[int | None, list[NoteFolder]] = {}
    for f in folders:
        kids.setdefault(f.parent_id, []).append(f)
    for lst in kids.values():
        lst.sort(key=lambda x: (x.sort_order, x.id))

    meta: dict[int | None, dict] = {}          # 目录/根 id -> {code, path(list), 体系名}
    folder_rows: list[dict] = []
    note_files: list[tuple[str, str]] = []     # (包内路径, 内容) —— 人读的 .md

    def walk(node_id, code, system_name, parts, node) -> None:
        meta[node_id] = {"code": code, "path": list(parts), "system": system_name}
        for c in kids.get(node_id, []):
            p = parts + [c.name]
            folder_rows.append({"curriculum_code": code, "path": "/".join(p),
                                "name": c.name, "sort_order": c.sort_order})
            walk(c.id, code, system_name, p, c)

    for r in roots:
        code = code_of.get(r.curriculum_id)
        walk(r.id, code, (name_of.get(r.curriculum_id) or r.name) if r.curriculum_id else UNFILED_NAME, [], r)

    # 笔记按目录分组，保证同一目录里的顺序就是界面上的顺序
    notes = list(db.scalars(
        select(Note).order_by(Note.folder_id, Note.pinned.desc(), Note.sort_order, Note.updated_at.desc())
    ).all())
    note_rows: list[dict] = []
    images: set[str] = set()
    used_paths: set[str] = set()
    for n in notes:
        where = meta.get(n.folder_id) or meta.get(
            next((r.id for r in roots if r.curriculum_id is None), None))
        if where is None:                   # 树全空（理论上不会）：也照导，只是没有目录信息
            where = {"code": None, "path": [], "system": UNFILED_NAME}
        imgs = sorted(referenced_images(n.content or ""))
        images |= set(imgs)
        note_rows.append({
            "id": n.id, "curriculum_code": where["code"], "folder_path": "/".join(where["path"]),
            "title": n.title, "pinned": 1 if n.pinned else 0, "sort_order": n.sort_order,
            "created_at": n.created_at, "updated_at": n.updated_at,
            "content": n.content or "", "ink": _parse_ink(n.ink), "images": imgs,
        })

        # 人读的副本：同名加「 2」，免得互相盖掉
        rel = f"notes/{_safe(where['system'])}"
        for part in where["path"]:
            rel += f"/{_safe(part)}"
        base = _safe(n.title or n.id)
        cand, i = base, 1
        while f"{rel}/{cand}.md" in used_paths:
            i += 1
            cand = f"{base} {i}"
        path = f"{rel}/{cand}.md"
        used_paths.add(path)
        head = (f"# {n.title}\n\n"
                f"> 拾课笔记 · 目录：{where['system']}"
                + (" / " + " / ".join(where["path"]) if where["path"] else "")
                + (f" · 已置顶" if n.pinned else "")
                + f" · 最后修改 {n.updated_at}\n\n---\n\n")
        note_files.append((path, head + (n.content or "")))

    now = datetime.now()
    manifest = {
        "format": FORMAT, "version": VERSION,
        "exported_at": now.isoformat(timespec="seconds"),
        "counts": {"notes": len(note_rows), "folders": len(folder_rows), "images": len(images)},
        "curricula": [{"code": c.code, "name": c.name} for c in curr],
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr("notes.json", json.dumps(
            {"folders": folder_rows, "notes": note_rows}, ensure_ascii=False, indent=2))
        z.writestr("目录树.md", _tree_markdown(roots, kids, notes, meta, name_of))
        for path, text in note_files:
            z.writestr(path, text)
        for name in sorted(images):
            src = config.NOTES_DIR / name
            if src.is_file():
                z.write(str(src), f"images/{name}")
    return buf.getvalue(), f"拾课笔记备份_{now:%Y%m%d_%H%M}.zip"


def _parse_ink(raw) -> list:
    try:
        v = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return v if isinstance(v, list) else []


def _tree_markdown(roots, kids, notes, meta, name_of) -> str:
    by_folder: dict[int | None, list[Note]] = {}
    for n in notes:
        by_folder.setdefault(n.folder_id, []).append(n)

    def node(nid, depth, out):
        for n in by_folder.get(nid, []):
            out.append(f"{'  ' * depth}- {n.title}")
        for c in kids.get(nid, []):
            out.append(f"{'  ' * depth}- **{c.name}**")
            node(c.id, depth + 1, out)

    out = ["# 笔记目录总览", ""]
    for r in roots:
        title = (name_of.get(r.curriculum_id) or r.name) if r.curriculum_id else UNFILED_NAME
        out.append(f"## {title}")
        node(r.id, 0, out)
        out.append("")
    return "\n".join(out)


# ================================================================ 导入
def _resolve_folder(db, roots_by_curr, code2id, cache, code, path, *, create: bool, report) -> int | None:
    """把「体系 code + 目录路径」落到本机的一个目录 id 上；create=False 时只查不建。

    按名字合并是**故意的**：同一个包导入两次不该长出两套目录，
    而且「DSE 数学/一、有理数」在两台设备上就该是同一个地方。
    """
    key = f"{code}|{path}"
    if key in cache:
        return cache[key]

    cid = code2id.get(code) if code else None
    if code and cid is None:
        report["warnings"].append(f"备份里的体系「{code}」这台设备上没有，相关笔记放进了「未归档」")
    root = roots_by_curr.get(cid) or roots_by_curr.get(None)     # 认不出 → 未归档
    cur = root.id
    cache[f"{code}|"] = cur
    walked: list[str] = []
    for part in [p for p in (path or "").split("/") if p]:
        walked.append(part)
        child = db.scalars(
            select(NoteFolder).where(NoteFolder.parent_id == cur, NoteFolder.name == part)
        ).first()
        if child is None:
            if not create:
                cache[key] = None
                return None
            sibs = note_folders._siblings(db, cur)
            child = NoteFolder(owner_id=root.owner_id, parent_id=cur,
                               name=_dedupe_name(db, cur, part),
                               sort_order=(sibs[-1].sort_order + 1) if sibs else 0)
            db.add(child)
            db.flush()                        # 拿 id（后面还要用它建子目录/挂笔记）
            report["folders_created"] += 1
        else:
            report["folders_reused"] += 1
        cur = child.id
        # 中间层也记进缓存：不然「甲/乙」和「甲/丙」会把「甲」数两遍（报告里的数字会虚高）
        cache[f"{code}|{'/'.join(walked)}"] = cur
    cache[key] = cur
    return cur


def import_zip(db: Session, data: bytes, *, overwrite: bool = False, dry_run: bool = False) -> dict:
    """把备份包导进本机。默认**不覆盖**同 id 的笔记。"""
    report: dict = {
        "dry_run": dry_run, "notes_created": 0, "notes_overwritten": 0, "notes_skipped": 0,
        "folders_created": 0, "folders_reused": 0, "images_added": 0, "images_skipped": 0,
        "warnings": [],
    }
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise HTTPException(422, "这不是一个有效的 zip 包（备份应该是「拾课笔记备份_….zip」）") from e

    names = set(zf.namelist())
    if "manifest.json" not in names or "notes.json" not in names:
        raise HTTPException(422, "包里缺少 manifest.json / notes.json，不像是拾课的笔记备份")
    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    if manifest.get("format") != FORMAT:
        raise HTTPException(422, "这个包不是拾课的笔记备份")
    ver = int(manifest.get("version") or 0)
    if ver > VERSION:
        raise HTTPException(422, f"这个包是更新版本的拾课导出的（v{ver}），当前程序只能读到 v{VERSION}。"
                                 "请先升级再导入。")
    payload = json.loads(zf.read("notes.json").decode("utf-8"))
    report["exported_at"] = manifest.get("exported_at", "")

    roots = _ensure_roots(db)
    roots_by_curr = {r.curriculum_id: r for r in roots}
    code2id = {c.code: c.id for c in db.scalars(select(Curriculum)).all()}
    cache: dict[str, int | None] = {}

    # 先把目录整棵树建好（笔记要往里面挂）
    for f in payload.get("folders", []):
        _resolve_folder(db, roots_by_curr, code2id, cache, f.get("curriculum_code"),
                        f.get("path"), create=not dry_run, report=report)

    need_images: set[str] = set()
    for n in payload.get("notes", []):
        nid = str(n.get("id") or "").strip()
        if not nid:
            report["warnings"].append("包里有笔记缺 id，已跳过")
            continue
        fid = _resolve_folder(db, roots_by_curr, code2id, cache, n.get("curriculum_code"),
                              n.get("folder_path"), create=not dry_run, report=report)
        existing = db.get(Note, nid)
        if existing is not None and not overwrite:
            report["notes_skipped"] += 1
            continue
        imgs = sorted(set(n.get("images") or []) | referenced_images(n.get("content") or ""))
        need_images |= set(imgs)
        missing = [x for x in imgs if f"images/{x}" not in names]
        if missing:
            report["warnings"].append(
                f"「{n.get('title')}」引用的 {len(missing)} 张配图不在包里（{missing[0]}…），"
                "正文里那几处会显示不出来")
        if dry_run:
            if existing is not None:
                report["notes_overwritten"] += 1
            else:
                report["notes_created"] += 1
            continue
        ink = json.dumps(n.get("ink") or [], ensure_ascii=False)
        if existing is None:
            db.add(Note(id=nid, owner_id=config.OWNER_ID, folder_id=fid,
                        title=(n.get("title") or "未命名笔记"),
                        content=n.get("content") or "", ink=ink,
                        pinned=1 if n.get("pinned") else 0,
                        sort_order=int(n.get("sort_order") or 0),
                        created_at=n.get("created_at") or datetime.now().isoformat(timespec="seconds"),
                        updated_at=n.get("updated_at") or datetime.now().isoformat(timespec="seconds")))
            report["notes_created"] += 1
        else:
            existing.folder_id = fid
            existing.title = n.get("title") or existing.title
            existing.content = n.get("content") or ""
            existing.ink = ink
            existing.pinned = 1 if n.get("pinned") else 0
            existing.sort_order = int(n.get("sort_order") or 0)
            report["notes_overwritten"] += 1

    # 配图：名字是随机生成的（内容不同名字才会撞），所以同名就当已经有了
    if not dry_run:
        for name in sorted(need_images):
            if f"images/{name}" not in names:
                continue
            dest = config.NOTES_DIR / _safe_path(name)
            if dest.exists():
                report["images_skipped"] += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(f"images/{name}"))
            report["images_added"] += 1
        db.commit()
    else:
        report["images_added"] = sum(1 for x in need_images
                                     if f"images/{x}" in names and not (config.NOTES_DIR / x).exists())

    zf.close()
    report["notes_total"] = len(payload.get("notes", []))
    return report


def _safe_path(name: str) -> str:
    """配图名只取 basename 再校验形态 —— 包里带 `../` 的文件名一律不认。"""
    base = name.replace("\\", "/").split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", base):
        return "invalid_name"
    return base


# ================================================================ 接口
#
# ⚠️ 前缀特意用 /api/notes-backup 而不是 /api/notes/backup：
# `/api/notes/{nid}/export` 与 `/api/notes/backup/export` 都是「两段路径」，
# 靠注册顺序才能分清楚 —— 那种依赖迟早会被一次重构踩坏（真实踩过：
# /api/notes/tree 会被 /api/notes/{nid} 抢走）。换个前缀就完全没有这个问题。
@router.get("/export")
def export_notes(db: Session = Depends(get_db)):
    """导出全部笔记为一个 zip。"""
    body, fname = export_zip(db)
    from urllib.parse import quote
    return Response(
        content=body, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="notes-backup.zip"; filename*=UTF-8\'\'{quote(fname)}'},
    )


@router.post("/import")
async def import_notes(
    file: UploadFile = File(...),
    overwrite: bool = Query(False, description="同 id 的笔记是否用备份里的覆盖（默认跳过）"),
    dry_run: bool = Query(False, description="只试算不写入"),
    db: Session = Depends(get_db),
):
    """导入备份包。返回一份「干了什么」的报告（试算时是「会干什么」）。"""
    data = await file.read(MAX_ZIP_BYTES + 1)
    if len(data) > MAX_ZIP_BYTES:
        raise HTTPException(422, f"备份包太大了（超过 {MAX_ZIP_BYTES // 1024 // 1024}MB）。")
    if not data:
        raise HTTPException(422, "没有收到文件内容")
    return import_zip(db, data, overwrite=overwrite, dry_run=dry_run)
