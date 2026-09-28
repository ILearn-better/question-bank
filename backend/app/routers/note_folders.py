# -*- coding: utf-8 -*-
"""笔记目录树：体系 → 目录… → 笔记。

一条贯穿整个文件的纪律：**目录树不能被拖坏**。
「自由调整层级」听起来只是前端拖一下，但真正会出事的都在服务端：

  · 把目录拖进**自己的后代**里 → 那棵子树从根上断掉，界面上整段消失（数据还在，但找不回来）。
    所以移动前必须走一遍 `_is_ancestor` 检查，前端做了同样的判断也只是为了体验。
  · 跨体系拖动 → 体系由**根行**承载，非根行不带 curriculum_id，
    所以跨体系其实是「换了个父节点」，不需要同步什么，也不会出现半新半旧的子树。
  · 删目录 → **绝不删内容**：子目录与笔记一律上移到父目录（同 Windows 里「剪切到上一级」），
    并在确认框里说清移了几样。静默删掉别人的笔记是最不可原谅的一种「省事」。

排序约定：每个目录里**先列子目录、再列笔记**（两串 sort_order 互不干扰）。
这样拖动只会在同类之间重排，不会出现「目录的 0,1,2 和笔记的 0,1,2 交错」这种没法解释的顺序。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Curriculum, Note, NoteFolder
from ..schemas import NoteFolderIn, NoteFolderPatch

router = APIRouter(prefix="/api/note-folders", tags=["note-folders"])

UNFILED_NAME = "未归档"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _folder_or_404(db: Session, fid: int) -> NoteFolder:
    f = db.get(NoteFolder, fid)
    if f is None:
        raise HTTPException(404, "目录不存在")
    return f


# ================================================================
# 树：一次给全（体系根 + 目录 + 每个目录下的笔记）
#
# ⚠️ 这个路由**必须声明在任何 /{fid} 之前**：FastAPI 里 `{fid}` 的类型校验发生在
# 路由匹配之后，所以后声明的 /tree 会被 /{fid} 抢走，然后报「不是合法整数」。
# ================================================================
def _ensure_roots(db: Session) -> list[NoteFolder]:
    """把「每个体系一棵根 + 未归档一棵根」补齐。

    为什么要一次性把 4 个体系的根都建出来（哪怕某个体系一篇笔记都没有）：
    树要能**往里拖**。只为有内容的体系建根，用户就没法把笔记拖到「国内初中」去。
    多出来的也就是 4 行空目录。
    """
    rows = db.scalars(select(NoteFolder).where(NoteFolder.is_root == 1)).all()
    have = {r.curriculum_id for r in rows}
    changed = False
    for c in db.scalars(select(Curriculum).order_by(Curriculum.sort_order, Curriculum.id)).all():
        if c.id not in have:
            db.add(NoteFolder(curriculum_id=c.id, name=c.name, is_root=1, sort_order=c.sort_order or 0))
            changed = True
    if None not in have:
        db.add(NoteFolder(curriculum_id=None, name=UNFILED_NAME, is_root=1, sort_order=9999))
        changed = True
    if changed:
        db.commit()
        rows = db.scalars(select(NoteFolder).where(NoteFolder.is_root == 1)).all()
    names = {c.id: c.name for c in db.scalars(select(Curriculum)).all()}
    return sorted(rows, key=lambda r: (r.sort_order, r.id if r.curriculum_id else 10 ** 6))


def _all_notes(db: Session) -> list[Note]:
    """树的显示顺序：置顶优先，然后手排顺序，最后拿更新时间兜底（新建的笔记排最前）。"""
    return list(db.scalars(
        select(Note).order_by(Note.pinned.desc(), Note.sort_order, Note.updated_at.desc())
    ).all())


def unfiled_root(db: Session) -> NoteFolder:
    """「未归档」那棵的根。新建笔记没指定目录时落这里 —— 永远有地方放，不会丢归属。"""
    return next(r for r in _ensure_roots(db) if r.curriculum_id is None)


def folder_paths(db: Session) -> dict[int, str]:
    """目录 id -> 「DSE 数学 / 一、有理数」这样的人话路径。

    搜索结果里要显示「这篇在哪」，不然同名笔记分不出来是哪个「1.1」。
    根行的名字从 curricula 现取（体系改名后不会留下旧名字）。
    """
    cname = {c.id: c.name for c in db.scalars(select(Curriculum)).all()}
    name: dict[int, str] = {}
    parent: dict[int, int | None] = {}
    for r in _ensure_roots(db):
        name[r.id] = cname.get(r.curriculum_id) or r.name
        parent[r.id] = None
    for f in db.scalars(select(NoteFolder).where(NoteFolder.is_root == 0)).all():
        name[f.id] = f.name
        parent[f.id] = f.parent_id
    out: dict[int, str] = {}
    for fid in name:
        parts: list[str] = []
        cur, seen = fid, set()
        while cur is not None and cur not in seen:      # seen 防脏数据绕圈
            seen.add(cur)
            parts.append(name.get(cur, "?"))
            cur = parent.get(cur)
        out[fid] = " / ".join(reversed(parts))
    return out


def place_note(db: Session, note: Note, position: int) -> None:
    """把笔记放到所在目录的第 position 位（按界面的显示顺序，置顶的排前面）。

    顺带把 pinned 对齐：界面顺序是「置顶优先 → 手排顺序」。若拖放的目标位置落在
    置顶那一段里，就顺带置顶 —— 不然它会「弹」回原位，看着像拖了没反应。
    这比「悄悄清掉置顶」和「拖不动」都好解释。
    """
    sibs = [n for n in db.scalars(
        select(Note).where(Note.folder_id == note.folder_id, Note.id != note.id)
        .order_by(Note.pinned.desc(), Note.sort_order, Note.updated_at.desc())
    ).all()]
    pos = max(0, min(position, len(sibs)))
    note.pinned = 1 if pos < sum(1 for n in sibs if n.pinned) else 0
    _renumber(sibs[:pos] + [note] + sibs[pos:])


@router.get("/tree")
def note_tree(db: Session = Depends(get_db)):
    from .notes import _brief          # 局部导入：两边互相引用，模块级导入会成环

    roots = _ensure_roots(db)
    root_ids = {r.id for r in roots}
    folders = db.scalars(
        select(NoteFolder).where(NoteFolder.is_root == 0)
        .order_by(NoteFolder.sort_order, NoteFolder.id)
    ).all()
    notes = _all_notes(db)

    # 只有一个真正的来源：notes.folder_id。归属不做二次推断。
    by_folder: dict[int | None, list[Note]] = {}
    for n in notes:
        by_folder.setdefault(n.folder_id, []).append(n)

    nodes: dict[int, dict] = {}
    for r in roots:
        nodes[r.id] = {
            "id": r.id, "name": r.name, "is_root": True,
            "curriculum_id": r.curriculum_id, "count": 0,
            "children": [], "notes": [],
        }
    for f in folders:
        nodes[f.id] = {
            "id": f.id, "name": f.name, "is_root": False,
            "curriculum_id": None, "count": 0,
            "children": [], "notes": [],
        }

    # 挂上父节点；父节点不存在的（脏数据）当作未归档，别让它凭空消失
    unfiled = next((r.id for r in roots if r.curriculum_id is None), None)
    for f in folders:
        parent = nodes.get(f.parent_id) or nodes.get(unfiled)
        if parent is not None:
            parent["children"].append(nodes[f.id])

    for n in notes:
        holder = nodes.get(n.folder_id) or nodes.get(unfiled)
        if holder is not None:
            holder["notes"].append(_brief(n))

    def count(node: dict) -> int:
        """递归数笔记篇数（子树合计），目录行右边显示的就是它。"""
        node["count"] = len(node["notes"]) + sum(count(c) for c in node["children"])
        return node["count"]

    for r in roots:
        count(nodes[r.id])

    tree = [nodes[r.id] for r in roots]
    cname = {c.id: c.name for c in db.scalars(select(Curriculum)).all()}
    for r in roots:
        # 根的名字现取体系名：体系改名后，树上的旧名字不能留在那儿
        nodes[r.id]["name"] = cname.get(r.curriculum_id) or r.name
    return {
        "roots": tree,
        "unfiled_root_id": unfiled,
        # 给「新建目录/新建笔记」兜底：没选任何目录时放这里，永远有地方放
        "total": len(notes),
    }


# ================================================================
# 目录 CRUD
# ================================================================
def _is_ancestor(db: Session, ancestor_id: int, node_id: int | None) -> bool:
    """node 是否在 ancestor 的子树里（含自己）。往上走到根，带 visited 防脏数据绕圈。

    这是拖拽唯一真正危险的一步：把目录拖进自己的后代，子树会从根上断掉 ——
    数据还在磁盘上，但界面上整段消失。所以服务端必须自己判一次，不信前端。
    """
    seen: set[int] = set()
    cur = node_id
    while cur is not None and cur not in seen:
        if cur == ancestor_id:
            return True
        seen.add(cur)
        row = db.get(NoteFolder, cur)
        cur = row.parent_id if row else None
    return False


def _siblings(db: Session, parent_id: int, exclude: int | None = None) -> list[NoteFolder]:
    rows = list(db.scalars(
        select(NoteFolder)
        .where(NoteFolder.parent_id == parent_id, NoteFolder.is_root == 0)
        .order_by(NoteFolder.sort_order, NoteFolder.id)
    ).all())
    return [f for f in rows if f.id != exclude]


def _dedupe_name(db: Session, parent_id: int, name: str, exclude: int | None = None) -> str:
    """同级重名自动加「 2」。同名兄弟是目录树最难用的一种混乱 ——
    「1.1 认识有理数」出现两次，之后每次点开都得试。"""
    take = {f.name for f in _siblings(db, parent_id, exclude)}
    if name not in take:
        return name
    i = 2
    while f"{name} {i}" in take:
        i += 1
    return f"{name} {i}"


def _renumber(rows: list) -> None:
    """按给定顺序重写 sort_order 为 0..n。

    为什么整段重写、而不是算「中值/插空」：序号只要参与比较就会被插坏
    （两个 1.5 之间的第 3 个插不进去）。整段重写永远干净，代价只是一次循环。
    """
    for i, r in enumerate(rows):
        r.sort_order = i


@router.post("")
def create_folder(payload: NoteFolderIn, db: Session = Depends(get_db)):
    parent = _folder_or_404(db, payload.parent_id)
    name = (payload.name or "").strip() or "新目录"
    siblings = _siblings(db, parent.id)
    f = NoteFolder(
        owner_id=parent.owner_id,
        parent_id=parent.id,
        name=_dedupe_name(db, parent.id, name),
        sort_order=(siblings[-1].sort_order + 1) if siblings else 0,
    )
    db.add(f)
    db.commit()
    return {"id": f.id, "name": f.name, "parent_id": f.parent_id}


@router.patch("/{fid}")
def update_folder(fid: int, payload: NoteFolderPatch, db: Session = Depends(get_db)):
    """改名 / 移动 / 重排，三件事共用一个接口（拖动只发 parent_id + position）。"""
    f = _folder_or_404(db, fid)
    data = payload.model_dump(exclude_unset=True)
    if f.is_root:
        # 体系根由体系本身决定：名字跟着体系走，位置由体系顺序决定
        raise HTTPException(422, f"「{f.name}」是体系根，不能改名或移动。要整理请动它下面的目录。")

    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise HTTPException(422, "目录名不能为空")
        f.name = _dedupe_name(db, f.parent_id, name, exclude=f.id)

    new_parent = None
    if "parent_id" in data and data["parent_id"] is not None:
        if data["parent_id"] == f.id:
            raise HTTPException(422, "不能把目录放进它自己里面")
        if _is_ancestor(db, f.id, data["parent_id"]):
            raise HTTPException(422, "不能把目录放进它自己的子目录里（那一段会从树上掉下来）")
        new_parent = _folder_or_404(db, data["parent_id"])
        if new_parent.id != f.parent_id:
            f.parent_id = new_parent.id
            f.name = _dedupe_name(db, new_parent.id, f.name, exclude=f.id)

    if "position" in data and data["position"] is not None:
        parent_id = f.parent_id
        sibs = _siblings(db, parent_id, exclude=f.id)
        pos = max(0, min(int(data["position"]), len(sibs)))
        _renumber(sibs[:pos] + [f] + sibs[pos:])

    db.commit()
    return {"id": f.id, "name": f.name, "parent_id": f.parent_id}


@router.delete("/{fid}")
def delete_folder(fid: int, db: Session = Depends(get_db)):
    """删目录，**但一样东西都不删**：子目录与笔记全部上移到父目录。

    为什么要单独回传移了几样：界面要能把这句话说出来（「3 篇笔记已移到上一级」）。
    静默删内容最不可原谅；静默搬走而不说，也让人以为东西没了。
    """
    f = _folder_or_404(db, fid)
    if f.is_root:
        raise HTTPException(422, f"「{f.name}」是体系根，不能删除。")

    parent = _folder_or_404(db, f.parent_id)
    kids = list(db.scalars(select(NoteFolder).where(NoteFolder.parent_id == f.id)).all())
    notes = list(db.scalars(select(Note).where(Note.folder_id == f.id)).all())

    # 承接顺序：先接父目录里已有的，再按原顺序接上搬过来的 —— 位置变化可预期
    base = len(_siblings(db, parent.id, exclude=f.id))
    for i, k in enumerate(kids):
        k.parent_id = parent.id
        k.sort_order = base + i
    for i, n in enumerate(notes):
        n.folder_id = parent.id
        n.sort_order = base + i

    db.delete(f)
    db.commit()
    return {"ok": True, "moved_folders": len(kids), "moved_notes": len(notes),
            "to": parent.name}
