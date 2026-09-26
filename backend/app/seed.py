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
from .models import Curriculum, FeedbackTemplate, Node

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


# ---------------------------------------------------------------- 反馈模板
# 内置四套，覆盖四种常见场景。短语写得越具体，家长越觉得「老师真的在看我的孩子」；
# seeds 是骨架文本（可空），选了模板且四段为空时直接填进去，省掉从零组织语言。
# 注意：这些只是**起点**，用户可另存自己的模板；内置模板允许改文案但不允许删。
BUILTIN_TEMPLATES: list[dict] = [
    {
        "name": "常规课（推荐）",
        "phrases": {
            "performance": ["状态不错，配合度高", "前半段注意力较集中", "主动提问，思路跟得紧", "略疲倦，节奏放慢后好转"],
            "problems": ["计算跳步导致失分", "审题不够仔细，条件看漏", "步骤书写不规范", "知识点迁移能力偏弱"],
            "homework": ["课后练习 P32 第 1-8 题", "错题重做一遍", "本周完成一套限时训练", "暂无，先巩固课上内容"],
            "next_plan": ["下节课讲函数单调性", "先复习错题再进入新内容", "下次带模考卷来讲解", "继续完成本章剩余题型"],
        },
        "seeds": {},
    },
    {
        "name": "考后讲评",
        "phrases": {
            "performance": ["订正态度认真，追问到位", "对失分点接受度高", "情绪略低，已做疏导", "能自己找到错因"],
            "problems": ["时间分配不合理，后面大题没写完", "选择填空失分偏多", "大题步骤分丢得多", "会做的题抄错/算错"],
            "homework": ["本次试卷重做一遍（限时）", "错题整理到错题本", "按错因分类订正", "针对弱项做 10 道专项"],
            "next_plan": ["下节课专项讲评第 18-20 题", "复盘时间分配策略", "针对薄弱知识点做一轮专项", "下次课做一次同难度模考"],
        },
        "seeds": {
            "performance": "本次测试得分 ______ / 满分 ______，排名/等级 ______。",
            "problems": "主要失分集中在：",
        },
    },
    {
        "name": "作业辅导",
        "phrases": {
            "performance": ["能独立完成大部分题目", "卡住的题愿意先自己想", "提示后能自行完成", "主动标记了不会的题"],
            "problems": ["同一类题反复错", "喜欢直接看答案", "过程写得过于简略", "基础运算还不熟练"],
            "homework": ["错题重做并写出思路", "把卡住的题整理成一张纸", "明天先交作业再讲新内容", "补完上次缺的练习"],
            "next_plan": ["先把作业里的错题讲透", "放慢节奏，先把基础打牢", "按题型分类集中练", "下次抽查错题本"],
        },
        "seeds": {},
    },
    {
        "name": "家长沟通（简短）",
        "phrases": {
            "performance": ["本节状态正常", "比上次进步明显", "今天效率很高", "情绪不错"],
            "problems": ["基础题还是粗心", "这周练习量不够", "需要家长帮忙盯一下作业", "最近状态有点松"],
            "homework": ["本周完成 ______", "每天 15 分钟计算练习", "完成错题重做", "周末做一套卷子"],
            "next_plan": ["按原计划继续", "下节课先检查作业", "下周安排一次小测", "有问题随时联系我"],
        },
        "seeds": {},
    },
]


def ensure_feedback_templates(db: Session) -> int:
    """内置模板只补不覆盖：已存在同名内置模板就跳过（用户可能改过它的文案）。"""
    existing = {t.name for t in db.scalars(select(FeedbackTemplate))}
    added = 0
    for i, spec in enumerate(BUILTIN_TEMPLATES):
        if spec["name"] in existing:
            continue
        db.add(FeedbackTemplate(
            owner_id=config.OWNER_ID,
            name=spec["name"],
            phrases=json.dumps(spec["phrases"], ensure_ascii=False),
            seeds=json.dumps(spec["seeds"], ensure_ascii=False),
            is_builtin=1,
            sort_order=i,
        ))
        added += 1
    if added:
        db.commit()
    return added


def seed_all(db: Session) -> dict:
    return {
        "curricula_added": ensure_curricula(db),
        "nodes_imported": import_legacy_tree(db),
        "templates_added": ensure_feedback_templates(db),
    }
