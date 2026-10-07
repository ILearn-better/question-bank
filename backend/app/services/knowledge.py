# -*- coding: utf-8 -*-
"""知识点名字 ↔ 知识树节点 的解析。

原先这三支（`_clean_tags` / `_node_path` / `_resolve_kp`）是 `routers/questions.py`
的私有函数。批量入库也要用同一套判断 —— 模型给出的是**知识点名字**（自由文本），
不是 node_id，落库时要挂到知识树上。与其让路由与路由之间互相 import 私有函数
（删文档那次的先例：抽成 `services/crops.py`），不如抽到这里。

一个关键差别 —— **模糊匹配只给批量开**：
    手动录题时老师是从下拉里挑的，名字能对上就对上、对不上就是他自己敲的新词，
    那时**不该**猜（把「函数」猜成「函数的概念与表示」是替他做主）。
    而模型给的名字是它自己组织的措辞（「三角函数的图像与性质」vs 树里的
    「三角函数的图象和性质」），完全同名纯属运气。
    所以 `resolve_kp(fuzzy=...)` 由调用方决定，默认保持既有行为（不猜）。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Node

# 归一化时要抹掉的噪声字符。
# 目的不是「语义等价」，而是挡掉最常见的**措辞**差异（加字、加标点、加连接词）：
#   「三角函数的图像与性质」→「三角函数图像性质」
#   「函数、零点」          →「函数零点」
# ⚠️ 刻意**不**做同义词表（图↔象、性质↔性质 那种字面替换）—— 那会开始「猜」，
#    而猜错的代价是题目挂错考点，出卷时按考点筛题就会漏。
#    后果要说清楚：「三角函数的图像与性质」与「三角函数的图象和性质」归一化后
#    **仍然不相等**（图像 vs 图象 差一个字），得靠下面「互相包含」那一档去捡；
#    而 三角函数 ⊂ 三角函数的图像与性质，所以那种措辞实际会落到「三角函数」上。
#    种子树里用的是「图像」（见 node 17 / 20 / 32 / 33），不是「图象」。
_NOISE_RE = re.compile(r"[\s·、，,。；;:：/\\（）()\[\]【】「」《》\-—_的和与及]")


def clean_tags(tags) -> list[str]:
    """去空白、去空串、去重，保持输入顺序。"""
    out: list[str] = []
    for t in tags or []:
        s = str(t).strip()
        if s and s not in out:
            out.append(s)
    return out


def node_path(db: Session, node_id: int | None) -> str | None:
    """知识点全路径（"章/节/知识点"），用于冗余进 knowledge_points 供列表直接显示。"""
    if not node_id:
        return None
    names: list[str] = []
    cur = db.get(Node, node_id)
    # 限深，防库里出现环时死循环
    while cur is not None and len(names) < 12:
        names.append(cur.name)
        cur = db.get(Node, cur.parent_id) if cur.parent_id else None
    return "/".join(reversed(names)) if names else None


def normalize(name: str | None) -> str:
    """把名字压成可比较的形式：去噪声字符、去空白。空名字归一化后是空串。"""
    return _NOISE_RE.sub("", name or "").strip()


def match_node(db: Session, curriculum_id: int | None, name: str | None) -> int | None:
    """在**指定体系内**找一个名字最贴近的节点，找不到返回 None。

    只在所选体系内找是刻意的：跨体系同名节点会把「国内高中数学」的考点挂到 DSE 题的
    知识树上 —— 挂错了不会报错，只会让按体系筛题的结果莫名其妙。

    匹配强度依次是：完全同名 → 归一化后同名 → 归一化后互相包含（取名字最长的那个，
    因为更长的那个更具体，如「函数的零点」优于「函数」）。
    """
    raw = (name or "").strip()
    if not raw or not curriculum_id:
        return None
    rows = db.execute(
        select(Node.id, Node.name).where(Node.curriculum_id == curriculum_id)
    ).all()
    if not rows:
        return None

    for nid, nname in rows:                                   # 1) 完全同名
        if (nname or "").strip() == raw:
            return nid

    target = normalize(raw)
    if not target:
        return None

    exact: int | None = None
    best: tuple[int, str] | None = None                       # (id, 归一化名)
    for nid, nname in rows:
        cand = normalize(nname)
        if not cand:
            continue
        if cand == target and exact is None:                  # 2) 归一化后同名
            exact = nid
        elif cand in target or target in cand:                # 3) 互相包含
            if best is None or len(cand) > len(best[1]):
                best = (nid, cand)
    if exact is not None:
        return exact
    return best[0] if best else None


def resolve_kp(
    db: Session,
    names,
    curriculum_id: int | None,
    fuzzy: bool = False,
):
    """把「知识点名字」解析成知识树节点。

    为什么由服务端解析：调用方只送名字 —— 用户既能从树里挑节点，也能自己敲一个树里
    没有的。后者不是容错，是**刚需**：现在只有国内高中数学有知识树（119 个节点），
    初中/其他体系都是空的，不给手填就等于不让人记知识点。

    fuzzy=True 时（批量入库用）：同名对不上再试一次模糊匹配。命中就用**节点的
    标准名与全路径**替换掉模型的原话 —— 否则库里会同时出现「函数的零点」与
    「函数零点」两个知识点，统计掌握度时被劈成两半。

    返回 (存进 knowledge_points 的名字列表, node_id, curriculum_id)。
    """
    cleaned = clean_tags(names)
    if not cleaned:
        return [], None, curriculum_id

    node = None
    if curriculum_id:
        node = db.scalar(
            select(Node).where(Node.name == cleaned[0], Node.curriculum_id == curriculum_id).limit(1)
        )
    if node is None:
        node = db.scalar(select(Node).where(Node.name == cleaned[0]).limit(1))

    if node is None and fuzzy:
        # 模糊匹配**只在所选体系内**做。跨体系去猜比不猜更危险：
        # 「函数」在国内高中数学里是考点，在 DSE 里根本不在同一棵树。
        nid = match_node(db, curriculum_id, cleaned[0])
        if nid:
            node = db.get(Node, nid)

    if node is None:
        return cleaned, None, curriculum_id

    # 命中节点时用节点的标准全路径，别留模型的原话
    path = node_path(db, node.id)
    names_out = [path] if path else [node.name]
    # 用户显式选了体系就以他的为准，不因为同名节点在别的体系就给他改掉
    return names_out, node.id, curriculum_id or node.curriculum_id
