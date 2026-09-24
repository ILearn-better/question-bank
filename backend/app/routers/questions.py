# -*- coding: utf-8 -*-
"""题库接口（原 main.py 的题目接口，响应字段保持向后兼容）。"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from .. import config
from ..db import get_db
from ..models import Node, Question, QuestionNode
from ..schemas import QuestionIn, QuestionPatch

router = APIRouter(prefix="/api", tags=["questions"])


def _parse_tags(raw: str | None) -> list[str]:
    """tags 存的是 JSON 数组字符串。解析失败就当空 —— 老数据或手工改库都可能写坏。"""
    try:
        v = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(t) for t in v] if isinstance(v, list) else []


def _clean_tags(tags) -> list[str]:
    """去空白、去空串、去重，保持输入顺序。"""
    out: list[str] = []
    for t in tags or []:
        s = str(t).strip()
        if s and s not in out:
            out.append(s)
    return out


def _escape_like(s: str) -> str:
    """转义 LIKE 的通配符 —— 标签里出现 % 或 _ 时，不转义会误命中一大片。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _node_path(db: Session, node_id: int | None) -> str | None:
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


def _resolve_kp(db: Session, names, curriculum_id: int | None):
    """把「知识点名字」解析成知识树节点。

    为什么由服务端解析：前端只送名字 —— 用户既能从树里挑节点，也能自己敲一个树里
    没有的。后者不是容错，是**刚需**：现在只有国内高中数学有知识树（119 个节点），
    初中/其他体系都是空的，不给手填就等于不让人记知识点。

    能对上同名节点就顺手把关系也建了（将来算掌握度靠它），对不上就纯文本存着。
    返回 (存进 knowledge_points 的名字列表, node_id, curriculum_id)。
    """
    cleaned = _clean_tags(names)
    if not cleaned:
        return [], None, curriculum_id
    q = select(Node).where(Node.name == cleaned[0])
    node = db.scalar(q.where(Node.curriculum_id == curriculum_id).limit(1)) if curriculum_id else None
    if node is None:
        node = db.scalar(q.limit(1))
    if node is None:
        return cleaned, None, curriculum_id
    # 用户显式选了体系就以他的为准，不因为同名节点在别的体系就给他改掉
    return cleaned, node.id, curriculum_id or node.curriculum_id


def _serialize(q: Question) -> dict:
    try:
        kps = json.loads(q.knowledge_points or "[]")
    except json.JSONDecodeError:
        kps = []
    return {
        "id": q.id,
        "document_id": q.document_id,
        "doc_filename": q.doc_filename,
        "start_block": q.start_block,
        "end_block": q.end_block,
        "content": q.content,
        "qtype": q.qtype,
        "difficulty": q.difficulty,
        "knowledge_points": kps,
        "answer": q.answer,
        "analysis": q.analysis,
        "image": q.image,
        "answer_image": q.answer_image or "",
        "created_at": q.created_at,
        # 新增字段（纯增量，不影响旧前端）
        "curriculum_id": q.curriculum_id,
        "node_id": q.node_id,
        "source": q.source,
        "year": q.year,
        "usage_count": q.usage_count,
        "stem_format": q.stem_format,
        "tags": _parse_tags(q.tags),
    }


@router.post("/questions")
def create_question(payload: QuestionIn, db: Session = Depends(get_db)):
    if not payload.content.strip() and not payload.image:
        raise HTTPException(422, "题目内容不能为空（文本或截图至少一项）")

    qid = uuid.uuid4().hex[:12]
    # 知识点：前端送名字（可能来自知识树，也可能是自己敲的）。能对上节点就把关系也建上，
    # 对不上就纯文本存 —— 知识树没有的体系（初中就是）也得让老师能记知识点。
    kps, node_id, curriculum_id = _resolve_kp(db, payload.knowledge_points, payload.curriculum_id)
    if payload.node_id:                       # 兼容直接传 node_id 的调用方，优先采用
        node_id = payload.node_id
        path = _node_path(db, node_id)
        if path and path not in kps:
            kps.insert(0, path)
    db.add(
        Question(
            id=qid,
            owner_id=config.OWNER_ID,
            document_id=payload.document_id,
            doc_filename=payload.doc_filename,
            start_block=payload.start_block,
            end_block=payload.end_block,
            content=payload.content,
            qtype=payload.qtype,
            difficulty=payload.difficulty,
            knowledge_points=json.dumps(kps, ensure_ascii=False),
            tags=json.dumps(_clean_tags(payload.tags), ensure_ascii=False),
            answer=payload.answer,
            analysis=payload.analysis,
            image=payload.image,
            answer_image=payload.answer_image,
            created_at=datetime.now().isoformat(timespec="seconds"),
            curriculum_id=curriculum_id,
            node_id=node_id,
            source=payload.source,
            year=payload.year,
            stem_format=payload.stem_format,
        )
    )
    # 主知识点同时写入关系表 —— 将来算掌握度靠它，靠 JSON 数组是算不动的
    if node_id:
        db.add(QuestionNode(question_id=qid, node_id=node_id, weight=1.0))
    db.commit()
    return {"id": qid}


@router.get("/questions/knowledge-points")
def list_knowledge_points(db: Session = Depends(get_db)):
    """题库里**实际用过**的知识点及次数 —— 出卷页的筛选列表用它。

    与 /questions/tags 一个思路：个人题库量级，扫全表在 Python 里统计最快，
    也就不必单独维护一张表。要的效果是「录题时写了什么，出卷时就能按什么筛」。
    """
    counter: dict[str, int] = {}
    for (raw,) in db.execute(select(Question.knowledge_points)).all():
        for kp in _parse_tags(raw):
            counter[kp] = counter.get(kp, 0) + 1
    items = [{"kp": k, "count": v}
             for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"items": items}


@router.get("/questions/tags")
def list_tags(db: Session = Depends(get_db)):
    """库里**实际用过**的标签及次数 —— 给录题页的自动补全和出卷页的筛选列表用。

    没有单独的标签表，所以扫全表在 Python 里统计。个人题库量级（几千条）
    一次全表扫是毫秒级，比为此专门维护一张表划算得多。
    """
    counter: dict[str, int] = {}
    for (raw,) in db.execute(select(Question.tags)).all():
        for t in _parse_tags(raw):
            counter[t] = counter.get(t, 0) + 1
    items = [{"tag": k, "count": v}
             for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {"items": items}


@router.patch("/questions/{qid}")
def update_question(qid: str, payload: QuestionPatch, db: Session = Depends(get_db)):
    """部分更新题库里的题 —— 主要是「给已录的题补答案」，也能改标签/知识点/题型难度。

    只处理**显式传进来**的字段（exclude_unset）：没传的一律不动。
    这样补答案时不会误伤题干图，改标签时也不会把已有的答案图抹掉。
    """
    q = db.get(Question, qid)
    if q is None:
        raise HTTPException(404, "题目不存在")

    data = payload.model_dump(exclude_unset=True)

    # 这几列是 NOT NULL（server_default ''），显式传 null 要落成空串而非 NULL
    for field in ("content", "answer", "answer_image", "analysis", "image",
                  "qtype", "difficulty", "source"):
        if field in data:
            setattr(q, field, data[field] or "")
    if "year" in data:
        q.year = data["year"]
    if "tags" in data:
        q.tags = json.dumps(_clean_tags(data["tags"]), ensure_ascii=False)

    # 知识点：可以传名字（knowledge_points），也可以直接传 node_id。
    # 名字能对上节点就把关系也建上，对不上就纯文本存着。
    if "knowledge_points" in data or "node_id" in data or "curriculum_id" in data:
        names = data.get("knowledge_points", _parse_tags(q.knowledge_points))
        cur = data.get("curriculum_id", q.curriculum_id)
        if data.get("node_id"):                      # 直接给了节点 id，优先采用
            node_id = data["node_id"]
            path = _node_path(db, node_id)
            if path and path not in (names or []):
                names = [path] + list(names or [])
        else:
            names, node_id, cur = _resolve_kp(db, names, cur)
        db.execute(delete(QuestionNode).where(QuestionNode.question_id == qid))
        q.node_id = node_id
        if node_id:
            db.add(QuestionNode(question_id=qid, node_id=node_id, weight=1.0))
        q.knowledge_points = json.dumps(_clean_tags(names), ensure_ascii=False)
        q.curriculum_id = cur

    db.commit()
    db.refresh(q)
    return _serialize(q)


@router.get("/questions")
def list_questions(
    document_id: str | None = None,
    curriculum_id: int | None = None,
    node_id: int | None = None,
    db: Session = Depends(get_db),
):
    stmt = select(Question)
    if document_id:
        stmt = stmt.where(Question.document_id == document_id).order_by(Question.created_at)
    elif node_id:
        stmt = (
            select(Question)
            .join(QuestionNode, QuestionNode.question_id == Question.id)
            .where(QuestionNode.node_id == node_id)
            .order_by(Question.created_at.desc())
        )
    else:
        stmt = stmt.order_by(Question.created_at.desc())
    if curriculum_id:
        stmt = stmt.where(Question.curriculum_id == curriculum_id)
    return [_serialize(q) for q in db.scalars(stmt).all()]


def _subtree_ids(db: Session, root_id: int) -> list[int]:
    """一棵知识点的子树 id（含自己）。按「章节」筛也能筛出它下面小节的题，
    否则老师得把整棵子树逐个勾一遍。"""
    rows = db.execute(select(Node.id, Node.parent_id)).all()
    kids: dict[int | None, list[int]] = {}
    for nid, pid in rows:
        kids.setdefault(pid, []).append(nid)
    out: list[int] = []
    stack = [root_id]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.append(cur)
        stack.extend(kids.get(cur, []))
    return out


@router.get("/questions/search")
def search_questions(
    keyword: str | None = None,
    curriculum_id: int | None = None,
    node_id: int | None = None,
    kp: str | None = None,
    qtype: str | None = None,
    difficulty: str | None = None,
    has_image: bool | None = None,
    with_answer: bool | None = None,
    tags: str | None = None,
    sort: str = Query("created", description="created / oldest / usage"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """按条件筛题库（出卷用），响应 {total, items}。

    与 GET /questions 的区别：多了题型/难度/知识点/关键词等条件、分页、以及排序，
    因为出卷页要显示「共 N 题」并翻页。**旧接口一行没动**，录题页不受影响。
    """
    conds = []
    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        conds.append(or_(
            Question.content.like(like),
            Question.answer.like(like),
            Question.source.like(like),
            Question.doc_filename.like(like),
        ))
    if curriculum_id:
        conds.append(Question.curriculum_id == curriculum_id)
    if node_id:
        ids = _subtree_ids(db, node_id)
        linked = select(QuestionNode.question_id).where(QuestionNode.node_id.in_(ids))
        conds.append(or_(Question.node_id.in_(ids), Question.id.in_(linked)))
    if kp:
        # 知识点按「名字」筛，两种存法都要能命中：
        #   · 自己敲的 -> 存在 knowledge_points 文本里
        #   · 从树里挑的 -> 存在 node_id 关系里
        # 所以除了文本 LIKE，还要把同名节点的整棵子树也算上 ——
        # 选「平面向量」时，挂在它子节点下的题也该出来。
        conds_kp = [Question.knowledge_points.like(f"%{_escape_like(kp)}%", escape="\\")]
        hit = db.scalar(select(Node).where(Node.name == kp).limit(1))
        if hit is not None:
            sub = _subtree_ids(db, hit.id)
            linked = select(QuestionNode.question_id).where(QuestionNode.node_id.in_(sub))
            conds_kp.append(or_(Question.node_id.in_(sub), Question.id.in_(linked)))
        conds.append(or_(*conds_kp))
    if qtype:
        conds.append(Question.qtype == qtype)
    if difficulty:
        conds.append(Question.difficulty == difficulty)
    if has_image is not None:
        # coalesce 是为了 NULL 安全：老数据里 image 可能是 NULL
        cond = func.coalesce(Question.image, "") != ""
        conds.append(cond if has_image else ~cond)
    if with_answer is not None:
        has_ans = or_(
            func.coalesce(Question.answer, "") != "",
            func.coalesce(Question.answer_image, "") != "",
        )
        conds.append(has_ans if with_answer else ~has_ans)
    if tags:
        want = [t.strip() for t in tags.split(",") if t.strip()]
        if want:
            # 任一命中即可（出卷时「这几个标签里有一个就算」更实用）
            # tags 存的是 JSON 数组，所以匹配带引号的整项；LIKE 通配符必须转义，
            # 否则标签里带 % 或 _ 会误命中一片。
            conds.append(or_(*[
                Question.tags.like(f'%"{_escape_like(t)}"%', escape="\\") for t in want
            ]))

    total = db.scalar(select(func.count()).select_from(Question).where(*conds)) or 0

    order = {
        "created": Question.created_at.desc(),
        "oldest": Question.created_at.asc(),
        # 少用的排前面：出过卷的题尽量别再出，usage_count 就是为这个留的
        "usage": func.coalesce(Question.usage_count, 0).asc(),
    }.get(sort, Question.created_at.desc())

    rows = db.scalars(
        select(Question).where(*conds)
        .order_by(order, Question.created_at.desc())
        .limit(limit).offset(offset)
    ).all()
    return {"total": total, "items": [_serialize(q) for q in rows]}


def _crop_files(*urls: str | None) -> set[str]:
    """从 image / answer_image 的 URL 里取出截图文件名（只认文件名，不认目录）。"""
    names: set[str] = set()
    for u in urls:
        if u:
            name = os.path.basename(u.split("?")[0].strip())
            if name:
                names.add(name)
    return names


def _all_crop_refs(db: Session) -> set[str]:
    """当前库里**所有**题目引用到的截图文件名。"""
    refs: set[str] = set()
    for img, ans in db.execute(select(Question.image, Question.answer_image)).all():
        refs |= _crop_files(img, ans)
    return refs


@router.delete("/questions/{qid}")
def delete_question(qid: str, db: Session = Depends(get_db)):
    """删题，并顺手清掉只有它引用的截图。

    以前只删数据库行，截图永远留在 data/uploads/crops/ 里：实测题库 0 行、
    磁盘上却有 92 张图、3.4 MB。录了又删的题会一直占盘，而且那是学生试卷的
    截图 —— 用户以为删掉了，文件其实还在。

    这里按「引用计数」删而不是直接删：image 与 answer_image 可能指向同一张图
    （同一份原貌图既当题干又当答案），先收齐全库引用，再删没人用的那些。
    """
    q = db.get(Question, qid)
    if q is None:
        raise HTTPException(404, "题目不存在")
    mine = _crop_files(q.image, q.answer_image)

    # 先删关系表（外键现在是真生效的，顺序错了会撞约束）
    db.execute(delete(QuestionNode).where(QuestionNode.question_id == qid))
    db.execute(delete(Question).where(Question.id == qid))

    # 上面的 DELETE 已经在本事务里生效，所以这次查询不会再算进这一条
    removed = 0
    for name in (mine - _all_crop_refs(db)) if mine else ():
        try:
            (config.CROPS_DIR / name).unlink()
            removed += 1
        except OSError:
            pass          # 文件本来就不在就算了，不该因为清理失败让删题也跟着失败

    db.commit()
    return {"ok": True, "crops_removed": removed}
