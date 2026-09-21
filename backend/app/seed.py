# -*- coding: utf-8 -*-
"""初始数据：体系骨架 + 知识点树导入。

体系是「数据」不是「代码」：新增一个体系只插一行，不改任何业务逻辑。
这正是用户「后面可能还有其他体系数学」这个要求的实现方式。
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .models import Curriculum, Node

# 用户当前实际教授的体系（2026-09-20 确认）
DEFAULT_CURRICULA: list[dict] = [
    {"code": "dse-math", "name": "DSE 数学", "region": "HK", "stage": "exam", "color": "#534AB7", "sort_order": 1},
    {"code": "alevel-math", "name": "A-Level 数学", "region": "UK", "stage": "exam", "color": "#0F6E56", "sort_order": 2},
    {"code": "cn-senior-math", "name": "国内高中数学", "region": "CN", "stage": "senior", "color": "#185FA5", "sort_order": 3},
    {"code": "cn-junior-math", "name": "国内初中数学", "region": "CN", "stage": "junior", "color": "#BA7517", "sort_order": 4},
]


def ensure_curricula(db: Session) -> int:
    """补齐体系骨架，返回新增条数。已存在的按 code 跳过，不覆盖用户改过的名字。"""
    existing = {c.code for c in db.scalars(select(Curriculum))}
    added = 0
    for spec in DEFAULT_CURRICULA:
        if spec["code"] in existing:
            continue
        db.add(Curriculum(owner_id=config.OWNER_ID, subject="math", **spec))
        added += 1
    if added:
        db.commit()
    return added


def import_legacy_tree(db: Session, curriculum_code: str = "cn-senior-math") -> int:
    """把仓库里原有的 math-knowledge-tree.json 导入 nodes 表。

    只在目标体系下没有任何节点时执行 —— 不做「每次启动都覆盖」这种危险动作，
    否则用户自己整理的知识树会被反复冲掉。
    """
    tree_path = config.LEGACY_TREE_PATH
    if not tree_path.exists():
        return 0

    curr = db.scalar(select(Curriculum).where(Curriculum.code == curriculum_code))
    if curr is None:
        return 0
    count = db.scalar(select(func.count()).select_from(Node).where(Node.curriculum_id == curr.id)) or 0
    if count:
        return 0

    with tree_path.open(encoding="utf-8") as f:
        data = json.load(f)

    added = 0
    for order, child in enumerate(data.get("children", []), start=1):
        added += _insert_subtree(db, curr.id, child, parent_id=None, fallback_level=1, order=order)
    db.commit()
    return added


def _insert_subtree(
    db: Session, curriculum_id: int, raw: dict, parent_id: int | None, fallback_level: int, order: int
) -> int:
    level = int(raw.get("level") or fallback_level)
    node = Node(
        owner_id=config.OWNER_ID,
        curriculum_id=curriculum_id,
        parent_id=parent_id,
        name=str(raw.get("name") or "(未命名)"),
        level=level,
        code=raw.get("code"),
        sort_order=order,
    )
    db.add(node)
    db.flush()          # 取到自增 id 给子节点用
    total = 1
    for child_order, child in enumerate(raw.get("children") or [], start=1):
        total += _insert_subtree(db, curriculum_id, child, node.id, level + 1, child_order)
    return total


def seed_all(db: Session) -> dict:
    return {
        "curricula_added": ensure_curricula(db),
        "nodes_imported": import_legacy_tree(db),
    }
