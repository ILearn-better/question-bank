# -*- coding: utf-8 -*-
"""体系与知识点树。

/api/tree 是给原录题页用的「级联选择器」格式，必须保持输出形状不变
（value 为路径字符串）。数据源已从 JSON 文件改为数据库，
但结果同构 —— 这样原页面一行代码都不用改，而知识树从此可以在界面里编辑。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Curriculum, Node
from ..schemas import CurriculumIn, NodeIn

router = APIRouter(prefix="/api", tags=["curriculum"])


def _children_of(nodes: list[Node], parent_id: int | None) -> list[Node]:
    return sorted(
        [n for n in nodes if n.parent_id == parent_id],
        key=lambda n: (n.sort_order or 0, n.id),
    )


def _to_cascader(nodes: list[Node], parent_id: int | None, path: str = "") -> list[dict]:
    out = []
    for n in _children_of(nodes, parent_id):
        value = f"{path}/{n.name}" if path else n.name
        item: dict = {"value": value, "label": n.name}
        kids = _to_cascader(nodes, n.id, value)
        if kids:
            item["children"] = kids
        out.append(item)
    return out


def _to_id_tree(nodes: list[Node], parent_id: int | None) -> list[dict]:
    return [
        {
            "id": n.id,
            "name": n.name,
            "level": n.level,
            "code": n.code,
            "children": _to_id_tree(nodes, n.id),
        }
        for n in _children_of(nodes, parent_id)
    ]


def _nodes_of(db: Session, curriculum_id: int) -> list[Node]:
    return list(db.scalars(select(Node).where(Node.curriculum_id == curriculum_id)))


# ---------------------------------------------------------------- 体系
@router.get("/curricula")
def list_curricula(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Curriculum).where(Curriculum.owner_id == config.OWNER_ID)
        .order_by(Curriculum.sort_order, Curriculum.id)
    ).all()
    counts = {}
    for cid in [c.id for c in rows]:
        counts[cid] = len(_nodes_of(db, cid))
    return [
        {
            "id": c.id, "code": c.code, "name": c.name, "region": c.region,
            "stage": c.stage, "subject": c.subject, "color": c.color,
            "sort_order": c.sort_order, "node_count": counts.get(c.id, 0),
        }
        for c in rows
    ]


@router.post("/curricula")
def create_curriculum(payload: CurriculumIn, db: Session = Depends(get_db)):
    if db.scalar(select(Curriculum).where(Curriculum.code == payload.code)):
        raise HTTPException(422, f"体系标识 {payload.code} 已存在")
    c = Curriculum(owner_id=config.OWNER_ID, **payload.model_dump())
    db.add(c)
    db.commit()
    return {"id": c.id}


# ---------------------------------------------------------------- 知识点树
@router.get("/curricula/{cid}/nodes")
def curriculum_nodes(cid: int, db: Session = Depends(get_db)):
    if db.get(Curriculum, cid) is None:
        raise HTTPException(404, "体系不存在")
    return _to_id_tree(_nodes_of(db, cid), None)


@router.post("/curricula/{cid}/nodes")
def create_node(cid: int, payload: NodeIn, db: Session = Depends(get_db)):
    if db.get(Curriculum, cid) is None:
        raise HTTPException(404, "体系不存在")
    level = payload.level
    if level is None:
        if payload.parent_id:
            parent = db.get(Node, payload.parent_id)
            if parent is None:
                raise HTTPException(422, "父节点不存在")
            level = parent.level + 1
        else:
            level = 1
    node = Node(
        owner_id=config.OWNER_ID,
        curriculum_id=cid,
        parent_id=payload.parent_id,
        name=payload.name,
        level=level,
        code=payload.code,
        sort_order=payload.sort_order,
    )
    db.add(node)
    db.commit()
    return {"id": node.id, "level": node.level}


@router.delete("/nodes/{nid}")
def delete_node(nid: int, db: Session = Depends(get_db)):
    node = db.get(Node, nid)
    if node is None:
        raise HTTPException(404, "节点不存在")
    db.delete(node)
    db.commit()
    return {"ok": True}


@router.post("/curricula/{cid}/import-tree")
def import_tree(cid: int, payload: dict, db: Session = Depends(get_db)):
    """从 {"children": [...]} 形状的 JSON 树批量导入（保留迁移能力）。"""
    curr = db.get(Curriculum, cid)
    if curr is None:
        raise HTTPException(404, "体系不存在")
    tree = payload.get("tree") or payload
    added = 0
    for order, child in enumerate(tree.get("children", []), start=1):
        added += _insert(db, cid, child, None, 1, order)
    db.commit()
    return {"imported": added}


def _insert(db: Session, cid: int, raw: dict, parent_id: int | None, fallback: int, order: int) -> int:
    level = int(raw.get("level") or fallback)
    node = Node(
        owner_id=config.OWNER_ID, curriculum_id=cid, parent_id=parent_id,
        name=str(raw.get("name") or "(未命名)"), level=level,
        code=raw.get("code"), sort_order=order,
    )
    db.add(node)
    db.flush()
    total = 1
    for i, child in enumerate(raw.get("children") or [], start=1):
        total += _insert(db, cid, child, node.id, level + 1, i)
    return total


# ---------------------------------------------------------------- 兼容旧录题页
@router.get("/tree")
def knowledge_tree(db: Session = Depends(get_db)):
    curr = db.scalar(select(Curriculum).where(Curriculum.code == "cn-senior-math"))
    if curr is not None:
        nodes = _nodes_of(db, curr.id)
        if nodes:
            return _to_cascader(nodes, None)
    # 兜底：数据库里还没导入时，仍然读原来的 JSON 文件
    if config.LEGACY_TREE_PATH.exists():
        with config.LEGACY_TREE_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return [_legacy_cascader(c) for c in data.get("children", [])]
    return []


def _legacy_cascader(node: dict, path: str = "") -> dict:
    label = node.get("name", "")
    value = f"{path}/{label}" if path else label
    item = {"value": value, "label": label}
    children = node.get("children") or []
    if children:
        item["children"] = [_legacy_cascader(c, value) for c in children]
    return item
