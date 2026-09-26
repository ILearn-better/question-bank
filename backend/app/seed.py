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
from .models import Curriculum, FeedbackDocTemplate, FeedbackTemplate, Node

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


# ---------------------------------------------------------------- 润色模板（整篇正文）
# 这里存的是「AI 把反馈整理成文时该仿照的样子」，整段会进提示词。
#
# ⚠️ 内置模板里的示例**必须脱敏**：绝不能拿某位真实学生的反馈当范例 ——
#    它会进版本库、也会被别的老师看到。所以示例里的姓名、章节、成绩一律用占位写法，
#    只保留「结构长什么样、语气有多具体」这两件真正需要被模仿的事。
BUILTIN_DOC_TEMPLATES: list[dict] = [
    {
        "name": "完整课堂反馈（推荐）",
        "content": """【结构】按下面的栏目顺序组织全文，栏目用【】包住，栏目内可以分条。
整体语气：具体、专业、面向家长，像一个真的旁听了一节课的老师在说话。
不要写「表现很好」「继续加油」这种放在谁身上都成立的空话，凡是评价都要带着依据。

第一行是抬头，用「学生名-日期 课堂反馈」的写法；下面紧跟几行基本信息：
科目 / 任课老师 / 上课时间 / 上次作业布置时间（没有的就省略这一行）。

【完成情况】一两句话。用「良好」这类词给个总体结论，后面可以补一句限定。
【存在问题】一两句话。没有重大问题就如实说明「无全新重大知识漏洞」，并提示详见【易错内容】。
【本次课堂内容】
  先写作业讲评情况（讲了哪部分、针对什么共性问题做了订正）。
  然后按小节逐块写，每块写成「章节号+小节名：要点」的形式，一块一段，涵盖：
  推导/讲解的核心结论、练了哪些题型、典型例题的作用、易错点的强化训练。
【本次课堂表现】一段连贯的话（不分条）。把下面几件事串起来写：
  上课状态与互动、作业完成质量、哪些知识点已掌握、哪里还需要提示、
  需要改进的具体问题、课下的具体要求、下一阶段的训练计划、最后一句鼓励。
【易错内容】逐条列出具体到知识点的薄弱处（例如公式记忆、符号运算、比例对应关系）。
【准时度】用「优秀 / 良好 / 一般」这类词给结论。
【本次作业】逐条列出，写清题号范围与书写/步骤要求。
【作业预计时长】用「1.5h」这种写法给个估值。

【示例（仅示意语气与详略，内容与本学生无关）】
【本次课堂表现】本次课 X 同学表现很好，上课听讲认真，积极互动，遇到问题给予提示后便能回忆起对应解题思路；主要问题集中在少部分题目书写格式不规范、计算粗心。课下需要将本次错题再回顾、重做一遍，下节课会抽查。做此类证明题时，建议先画草图、设清楚坐标、理清证明逻辑再书写。后续会开展综合大题训练。课下希望能吸收巩固今天所讲内容，认真完成所布置作业。
【易错内容】计算粗心，代入公式时符号运算不熟练；公式中系数与线段的对应关系容易混淆。""",
    },
    {
        "name": "简洁四段式（当前默认）",
        "content": """【结构】按四个栏目组织，栏目名用【】，每栏 1-3 句，不列点。
【课堂表现】【存在问题】【作业布置】【下次安排】
语气：简洁、对家长友好，一句话说清一件事，不要写空话。
全文控制在 200 字以内 —— 这是发给家长手机上看的东西。
【示例】
【课堂表现】状态不错，配合度高，函数单调性的判断能自己说清思路。
【存在问题】知识点迁移能力偏弱，换一个情境就要提示。
【作业布置】1. 讲义划线部分；2. 作业校对。
【下次安排】先讲透本次错题，再进入下一个知识点。""",
    },
]


def ensure_feedback_doc_templates(db: Session) -> int:
    """内置润色模板只补不覆盖（用户可能改过它的文案）。"""
    existing = {t.name for t in db.scalars(select(FeedbackDocTemplate))}
    added = 0
    for i, spec in enumerate(BUILTIN_DOC_TEMPLATES):
        if spec["name"] in existing:
            continue
        db.add(FeedbackDocTemplate(
            owner_id=config.OWNER_ID,
            name=spec["name"],
            content=spec["content"],
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
        "doc_templates_added": ensure_feedback_doc_templates(db),
    }
