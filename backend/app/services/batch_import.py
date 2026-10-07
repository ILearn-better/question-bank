# -*- coding: utf-8 -*-
"""批量入库：一次提交 N 个题块 → 并行送视觉模型 → 结构化字段落进待审表。

和「录题页单个 AI 识别」的关系：
    同一个调用层（`services/vision.py` 的图片预处理 + `ai_polish` 的 HTTP），
    同一个模型、同一个 temperature=0。差别有两处：

    ① **要的输出不一样。** 单个识别只要一段带 $LaTeX$ 的文本；
       批量要的是**结构化记录** —— 题干 + 题型 + 难度 + 知识点 + 标签 + 置信度 + 备注。
       这是本模块存在的主要理由：提示词与解析都在这里，不污染 vision.py。

    ② **一次很多张。** 所以有并发、有单条失败隔离、有进度。

⚠️ 关于「限制模型返回值」这件事（用户原话：关键是对大模型返回值的限制）——
   三层一起上，少一层都不够：

     第一层 · 提示词：把枚举值、字数上限、字段清单**逐条写死**，并给一个输出示例。
             不写枚举的话模型会自造近义词（「解答」「中等」「简单」），
             而这些都是页面下拉里没有的值，存进去老师在下拉里看不到、改不掉。
     第二层 · 请求参数：temperature=0（转录不要发挥）+
             `response_format={"type":"json_object"}`（结构化输出）。
             ⚠️ 后者不是所有服务商都认，回 400 时自动去掉重试一次 ——
             宁可退化成「靠提示词约束」，也不能让整条链路不可用。
     第三层 · 后端校验：解析容错（剥围栏 / 截花括号 / 修尾随逗号）→ 枚举不合法就
             **回落成默认值并记 flag**。回落值绝不静默采信 ——
             审核页会把 flag 标出来，老师才能分辨「这个『解答题』是模型判断的，
             还是枚举不合法被后端兜的」。

   三层里最容易被忽略的是第三层：模型不听话是常态，不是异常。
"""
from __future__ import annotations

import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, taxonomy
from ..db import SessionLocal
from ..models import BatchItem, BatchJob, Question, QuestionNode
from . import ai_polish, knowledge, vision

# 并发数。串行发 30 道题要等一分多钟，全并发又会撞服务商的速率限制，
# 所以给一个可调的中间值：默认 3，环境变量可改。
# （DeepSeek 对 deepseek-flash 的并发限制不严，但通用起见不写死高值。）
MAX_WORKERS = max(1, min(8, int(os.getenv("SHIKE_BATCH_WORKERS", "3"))))

# 单条失败重试次数。网络抖动导致的失败重试一次就够 ——
# 重试太多会把「模型确实读不出这张图」拖成几分钟的等待。
RETRY = 1

# 原始返回存进库时截断到多少字符。只为排查用，不必留全量（一次 30 条约几十 KB）。
RAW_KEEP = 2000

# 单条任务的超时上限。批量里某一张卡住不该拖死整批。
PER_ITEM_TIMEOUT = 90

# 一个任务最多多少条。这个上限的理由很具体：**提交多少块 = 发出多少次 AI 请求**，
# 而自动铺线在版式奇怪的 PDF 上很容易铺出上百条（实测一份 46 页的 PPT 型 PDF 铺出 118 块）。
# 宁可在建任务时就挡住并说清楚，也不要让老师等到 AI 账单出来才发现。
MAX_JOB_ITEMS = int(os.getenv("SHIKE_BATCH_MAX_ITEMS", "80"))


class BatchError(RuntimeError):
    """批量任务层的错误。消息可直接给用户看。"""


# ---------------------------------------------------------------- 提示词
# ⚠️ 普通字符串，不是 r-string —— LaTeX 的反斜杠必须写双份。
#    这里踩过一次真的（vision.py 头部记了）：`\f` 会变成换页符、`\t` 变成制表符，
#    提示词送到模型手里已经面目全非，而模型照样有输出，只是公式规范变差。
_ENUM_QTYPE = "、".join(taxonomy.QTYPES)
_ENUM_DIFFICULTY = "、".join(taxonomy.DIFFICULTIES)
_ENUM_FIGURE_WORDS = "」「".join(taxonomy.FIGURE_KEYWORDS)

# 角色刻意与 vision.PROMPTS["question"] 那份**不同**：
# 那一份要的是「把题转录成一段文本」，这一份要的是「填一张结构化表单」。
# 共用一份角色描述会让模型在两件事之间摇摆（实测：说了「只输出 JSON」，
# 它仍然给出带小标题的转录稿）。所以这里单独写。
_ROLE = (
    "你是一个数学题库的结构化录入工具。图里可能有印刷体、手写批注、作答横线、"
    "页眉页脚、条形码、以及「请在此作答」这类说明，也可能混进相邻题目的片段。"
    "你要做的是：把**这一道题**提取出来，填成一条结构化记录。"
)

_FIELD_RULES = (
    "\n\n请把图中这**一道**题录入成结构化数据。\n"
    "只输出**一个 JSON 对象** —— 不要数组、不要解释、不要 Markdown 代码围栏、"
    "不要 JSON 之外的任何文字。\n"
    "\n字段固定为下面九个，一个都不能少：\n"
    '  "content"         字符串。题干全文。行内公式用 $...$ 包住，独立成行的公式用 $$...$$ 包住；'
    "分数写 \\frac{}{}、根式写 \\sqrt{}、上下标记作 ^{} 与 _{}，一律用标准 LaTeX 命令。"
    "选择题的选项（(A)(B)(C)(D) 或 A. B. C. D.）要保留，选项里的公式同样转成 LaTeX。"
    "不要抄题号，不要抄分值标注（(3 marks)、（5 分）这类），不要抄页眉页脚、条形码、"
    "作答横线、以及「请在此作答」这类说明。\n"
    '  "qtype"          字符串。**必须**是下面之一：' + _ENUM_QTYPE + "。\n"
    '  "difficulty"     字符串。**必须**是下面之一：' + _ENUM_DIFFICULTY + "。\n"
    '  "knowledge_point" 字符串。这道题考查的**最主要**那一个知识点，'
    "用教材或课标里的通用叫法（例如「三角函数的图像与性质」「导数的几何意义」），"
    "10 到 20 个汉字。判断不了就给空字符串。\n"
    '  "tags"           字符串数组，0 到 4 个，每个 2 到 6 个汉字，'
    '例如 ["含参讨论","恒成立"]。不要与 knowledge_point 重复，不要放「数学」「高中」这类'
    "没有区分度的词。\n"
    '  "confidence"     字符串。**必须**是 high、medium、low 之一 —— '
    "你对 content 转录准确程度的自评：公式复杂、字迹模糊、有跨页截断时给低。\n"
    '  "needs_figure"    布尔值，只能写 true 或 false（不要写成字符串 "true"）。'
    "判断**这道题在文字与公式之外，是否还需要一张图才能读懂或作答**："
    "题干里出现「" + _ENUM_FIGURE_WORDS + "」这类指向图形的措辞，"
    "或者要求作图、读图、看图作答时，给 true；"
    "纯文字、纯公式、纯符号运算（只出现函数式而不出现图象）的题给 false。"
    "拿不准时给 true —— 漏掉的那张图老师看不出来，而多标的他取消一下就好。\n"
    '  "figure_note"     字符串。needs_figure 为 true 时，用**一句话**说明这张图是什么、'
    "大致长什么样（例如「一个开口向上的抛物线，与 x 轴交于两点」）；"
    "为 false 时给空字符串。\n"
    '  "note"           字符串。看不清、有歧义、或图形无法用文字表达的地方，'
    "用一句话说明；没有就给空字符串。\n"
    "\n四条硬要求：\n"
    "1. qtype 与 difficulty **只能**从上面给出的取值里原样照抄，不要写同义词或近义词"
    "（不要写「解答」「证明」「中等」「简单」「较难」）。\n"
    "2. 九个字段**必须全部出现**。取不到值时用空字符串 \"\"、空数组 [] 或 false，"
    "不要省略字段、不要写 null。\n"
    "3. JSON 里 content 的换行要写成 \\n，双引号要写成 \\\"，不要出现非法的转义。\n"
    "4. 只描述**这一道题**。图里若混进了相邻题目的片段，忽略它。\n"
    "\n输出示例（仅示意格式，内容以图为准）：\n"
    '{"content":"已知函数 $f(x)=x^2+2x-3$，求 $f(x)$ 的零点。","qtype":"解答题",'
    '"difficulty":"基础","knowledge_point":"函数的零点","tags":["一元二次方程"],'
    '"confidence":"high","needs_figure":false,"figure_note":"","note":""}'
)

FIELD_PROMPT = _ROLE + _FIELD_RULES

# ---------------------------------------------------------------- 解析与校验
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _extract_object(text: str) -> str | None:
    """从返回里抠出 JSON 对象本体。

    为什么必须抠：即使要求「只输出 JSON」，模型仍常常先来一句
    「好的，这是识别结果：」再给对象。直接 json.loads 整段必失败。
    取第一个 `{` 到最后一个 `}` 是最省事也最稳的一刀。
    """
    t = _strip_fence(text)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    return t[i : j + 1]


def _loads(candidate: str):
    """尽力把这段文本解释成 dict。返回 (obj, 用了修复手段?)。失败返回 (None, False)。"""
    try:
        obj = json.loads(candidate)
        return (obj if isinstance(obj, dict) else None), False
    except json.JSONDecodeError:
        pass
    # 修复一号：尾随逗号。模型写 JSON 时最常见的语法错，也是最便宜的修法。
    fixed = _TRAILING_COMMA_RE.sub(r"\1", candidate)
    try:
        obj = json.loads(fixed)
        if isinstance(obj, dict):
            return obj, True
    except json.JSONDecodeError:
        pass
    return None, False


def _as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    return ""


# 「是/否」的中英文与常见别写。模型对布尔字段极不老实 ——
# 要 JSON 布尔值它常常给 "true"、"是"、"需要"、1 这些东西。
_TRUE_WORDS = {"true", "yes", "y", "1", "是", "有", "需要", "需要图", "有图"}
# ⚠️ 这里**刻意没有空串**：空串/缺字段/null 都表示「没判断出来」，见 _as_bool
_FALSE_WORDS = {"false", "no", "n", "0", "否", "没有", "不需要", "无", "无图"}


def _as_bool(v) -> bool | None:
    """把模型的布尔字段解释成 True/False。**看不懂时返回 None** —— 不是 False。

    这个区分是整个配图功能的地基：字段缺失或写了「大概有吧」时，
    我们不能把它当成老师看到的「模型说不需要图」—— 那会让一条本该被复核的
    条目悄悄过关，题干里那句「如图」的图就永远丢了。
    返回 None 时调用方会打 figure_unknown，审核页据此提醒人工确认。
    """
    if v is None:                            # 字段缺失、或 JSON 里就是 null
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):          # 0 / 1 也常见
        return bool(v)
    s = _as_str(v).lower().replace(" ", "")
    if not s:                                # 空串 = 没给，不是「否」
        return None
    if s in _TRUE_WORDS:
        return True
    if s in _FALSE_WORDS:
        return False
    return None


def _clean_tags(v, knowledge_point: str) -> list[str]:
    """标签清洗：只留短词、去重、限量、且不许与知识点重复。

    为什么要卡这么细：模型很爱一次吐十个标签、或者把知识点再抄一遍当标签 ——
    那样标签页就废了（每个标签都只挂一两道题，筛不出东西）。
    """
    if isinstance(v, str):                     # 模型偶尔给逗号分隔的字符串而不是数组
        v = [x for x in re.split(r"[,，、;；]", v)]
    if not isinstance(v, list):
        return []
    out: list[str] = []
    kp = (knowledge_point or "").strip()
    for t in v:
        s = _as_str(t)
        if not s or len(s) > taxonomy.MAX_TAG_CHARS or s in out or s == kp:
            continue
        out.append(s)
        if len(out) >= taxonomy.MAX_TAGS:
            break
    return out


def parse_fields(text: str) -> dict:
    """把模型返回解析成结构化字段。

    **永不抛异常** —— 解析不出来也要给出一份可入库的默认值 + flags。
    理由：一条脏结果不该让整批任务失败，更不该让它悄悄写进题库；
    正确的做法是把它标成异常项放进待审列表，由人决定。
    """
    flags: list[str] = []
    obj, repaired = _loads(_extract_object(text) or "")
    if obj is None:
        flags.append("bad_json")
        obj = {}
    elif repaired:
        flags.append("json_repaired")

    content = _as_str(obj.get("content"))
    if not content:
        flags.append("empty_stem")

    qtype = _as_str(obj.get("qtype"))
    if qtype not in taxonomy.QTYPES:
        flags.append("qtype_fallback")
        qtype = taxonomy.DEFAULT_QTYPE

    difficulty = _as_str(obj.get("difficulty"))
    if difficulty not in taxonomy.DIFFICULTIES:
        flags.append("difficulty_fallback")
        difficulty = taxonomy.DEFAULT_DIFFICULTY

    confidence = _as_str(obj.get("confidence")).lower()
    if confidence not in taxonomy.CONFIDENCES:
        flags.append("confidence_fallback")
        confidence = taxonomy.DEFAULT_CONFIDENCE

    kp = _as_str(obj.get("knowledge_point"))

    # 「这题要不要配图」——AI 只给初值，最终以老师勾的为准（待审页可改）。
    # ⚠️ 判不出来时值取 False 但**必须打标记**：不能让它看起来像「模型确认无图」。
    needs_figure = _as_bool(obj.get("needs_figure"))
    if needs_figure is None:
        flags.append("figure_unknown")
        needs_figure = False

    return {
        "content": content,
        "qtype": qtype,
        "difficulty": difficulty,
        "knowledge_point": kp,
        "tags": _clean_tags(obj.get("tags"), kp),
        "confidence": confidence,
        "needs_figure": needs_figure,
        "figure_note": _as_str(obj.get("figure_note")),
        "note": _as_str(obj.get("note")),
        "flags": flags,
    }


# ---------------------------------------------------------------- 单张识别
def recognize_fields(db: Session, image_path, hint: str = "") -> dict:
    """识别一个题块，返回 parse_fields 的结构 + mode/model/usage/raw。

    与 vision.recognize 的差别只在提示词与「要 JSON」这两点，
    图片预处理与 HTTP 调用完全共用。
    """
    cfg = ai_polish.get_config(db)
    if not cfg["configured"]:
        raise vision.VisionError(
            "还没有配置 AI 接口（地址 / 模型 / 密钥），请到「设置 → AI 润色」里填写。"
        )

    uri = vision.prepare_image_file(image_path)

    prompt = FIELD_PROMPT
    if (hint or "").strip():
        prompt += "\n\n老师的补充说明（以此为准）：" + hint.strip()

    body = {
        "model": cfg["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0,            # 转录要还原，不要发挥
        "max_tokens": vision.MAX_TOKENS,   # 低于 2000 会静默返回空正文
        "reasoning_effort": "low",   # 不传的话推理吃掉 95% 预算（vision.py 头部有实测）
        "stream": False,
        # 结构化输出的第一道保险。不认它的服务商会回 400，届时自动去掉重试。
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    timeout = min(int(cfg["timeout"] or 60), PER_ITEM_TIMEOUT)

    def _call(b: dict) -> dict:
        return ai_polish.post_json(ai_polish.chat_url(cfg["base_url"]), b, headers, timeout)

    try:
        data = _call(body)
    except ai_polish.AiError as e:
        msg = str(e)
        # 两个可降级的参数：都是「锦上添花」，缺了照样能用。
        # 宁可多花一点预算 / 退化成靠提示词约束，也不能让整条链路不可用。
        dropped = False
        if "response_format" in body and "response_format" in msg:
            body.pop("response_format")
            dropped = True
        if "reasoning_effort" in body and "reasoning_effort" in msg:
            body.pop("reasoning_effort")
            dropped = True
        if not dropped:
            raise
        data = _call(body)

    text = ai_polish.extract_content(data)
    if not text.strip():
        raise vision.VisionError(
            "模型返回了空内容。常见原因：① max_tokens 被推理吃满；"
            "② 当前模型不接受图片输入（到「设置 → AI 润色」换一个支持识图的）。"
        )

    out = parse_fields(text)
    usage = data.get("usage") or {}
    out.update({
        "model": cfg["model"],
        "raw": text[:RAW_KEEP],
        "usage": {
            "prompt": usage.get("prompt_tokens"),
            "completion": usage.get("completion_tokens"),
        },
    })
    return out


# ---------------------------------------------------------------- 任务
def create_job(
    db: Session,
    items: list[dict],
    curriculum_id: int | None = None,
    document_id: str | None = None,
    doc_filename: str | None = None,
) -> BatchJob:
    """建任务 + 落条目。**不启动**识别 —— 启动交给调用方（便于测试与重跑）。"""
    if not items:
        raise BatchError("没有要识别的题块 —— 请先在页面上分割出题目")
    if not curriculum_id:
        raise BatchError("请先选择体系 —— 知识点要挂到对应体系的知识树上")
    if len(items) > MAX_JOB_ITEMS:
        raise BatchError(
            f"一次最多 {MAX_JOB_ITEMS} 块，这次有 {len(items)} 块。"
            "多半是分界线切得过碎（比如把页码、说明文字也切成了块）——"
            "请先回页面上核对分界线，或分批提交。"
        )

    job = BatchJob(
        id=uuid.uuid4().hex[:12],
        owner_id=config.OWNER_ID,
        status="queued",
        total=len(items),
        curriculum_id=curriculum_id,
        document_id=document_id,
        doc_filename=doc_filename,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    db.add(job)
    now = datetime.now().isoformat(timespec="seconds")
    for i, it in enumerate(items, start=1):
        region = it.get("region")
        db.add(BatchItem(
            id=uuid.uuid4().hex[:12],
            job_id=job.id,
            seq=i,
            document_id=it.get("document_id") or document_id,
            doc_filename=it.get("doc_filename") or doc_filename,
            page_no=it.get("page_no"),
            region=json.dumps(region) if region else None,
            image=it.get("image") or "",
            status="pending",
            tags="[]",
            flags="[]",
            created_at=now,
        ))
    db.commit()
    return job


def _run_one(item_id: str, image_path, hint: str) -> tuple[str, dict | None, str | None]:
    """在一个工作线程里识别一条。**只用自己新建的会话** —— 请求会话不能跨线程。"""
    db = SessionLocal()
    try:
        for attempt in range(RETRY + 1):
            try:
                return item_id, recognize_fields(db, image_path, hint), None
            except Exception as e:                       # noqa: BLE001 什么都得接住
                if attempt >= RETRY:
                    return item_id, None, str(e)[:500]
    finally:
        db.close()
    return item_id, None, "未知错误"


def run_job(job_id: str, item_ids: list[str] | None = None) -> None:
    """后台线程入口：并行识别整批，**由本线程串行写库**。

    item_ids：只跑这几条（重跑失败项用）。不传 = 任务下全部条目。

    为什么识别并行、写库串行：
        SQLite 是单写者。如果 N 个工作线程各自写自己那一条，就会出现
        「database is locked」互相等（WAL 只让读不被阻塞，写与写仍然互斥）。
        而识别是等网络的那几秒，那才是瓶颈 —— 并行它；
        写库只是几十微秒，串起来没有任何损失。所以工作线程只负责算，不碰库，
        结果汇总回本线程统一落盘。
    """
    db = SessionLocal()
    try:
        job = db.get(BatchJob, job_id)
        if job is None:
            return
        job.status = "running"
        db.commit()

        stmt = select(BatchItem.id, BatchItem.image).where(BatchItem.job_id == job_id)
        if item_ids is not None:
            stmt = stmt.where(BatchItem.id.in_(list(item_ids)))
        rows = db.execute(stmt).all()
        targets = []
        for item_id, image in rows:
            path = config.CROPS_DIR / os.path.basename((image or "").split("?")[0])
            targets.append((item_id, path))
    finally:
        # ⚠️ 这里必须关掉请求会话再进并发阶段：本线程接着还要写库，
        #    但持有太久会让 api 那边的写操作等锁。
        db.close()

    done = failed = 0
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for item_id, path in targets:
                if not path.exists():
                    # 图都没了（被清理过）——直接判失败，不用浪费一次模型调用
                    _finish_item(item_id, None, "原貌图已丢失，请重新分割")
                    failed += 1
                    continue
                futures[pool.submit(_run_one, item_id, path, "")] = item_id

            for fut in as_completed(futures):
                item_id, result, err = fut.result()
                if result is None:
                    _finish_item(item_id, None, err or "识别失败")
                    failed += 1
                else:
                    _finish_item(item_id, result, None)
                    done += 1
                _bump(job_id, done, failed)
    except Exception as e:                               # noqa: BLE001
        _finalize(job_id, done, failed, f"任务异常终止：{e}")
        return
    _finalize(job_id, done, failed, None)


def _finish_item(item_id: str, result: dict | None, err: str | None) -> None:
    """把一条结果写库。**短事务**：开→改→提交→关。"""
    db = SessionLocal()
    try:
        it = db.get(BatchItem, item_id)
        if it is None:
            return
        if result is None:
            it.error = err
            it.flags = json.dumps(["ai_failed"], ensure_ascii=False)
            db.commit()
            return
        it.content = result["content"]
        it.qtype = result["qtype"]
        it.difficulty = result["difficulty"]
        it.knowledge_point = result["knowledge_point"] or None
        it.tags = json.dumps(result["tags"], ensure_ascii=False)
        it.confidence = result["confidence"]
        it.note = result["note"] or None
        # 配图判断：AI 给的是**初值**，待审页可以改（老师说了算）。
        # ⚠️ 这里**不碰** figure_image —— 那是老师框选出来的，重跑识别不该把它抹掉。
        it.needs_figure = 1 if result.get("needs_figure") else 0
        it.figure_note = result.get("figure_note") or None
        # 知识点先试着挂到所选体系的知识树上。
        # ⚠️ 匹配不上**不是错误** —— 四个体系里只有一个有知识树，
        #    另外三个必然匹配不上，文献里那叫「体系还没有知识树」，不叫失败。
        if it.knowledge_point:
            kps, node_id, _cur = knowledge.resolve_kp(
                db, [it.knowledge_point], it.job.curriculum_id if it.job else None, fuzzy=True
            )
            if node_id:
                it.node_id = node_id
                it.knowledge_point = kps[0] if kps else it.knowledge_point
        it.raw = result.get("raw")
        it.flags = json.dumps(result["flags"], ensure_ascii=False)
        it.error = None
        db.commit()
    finally:
        db.close()


def _bump(job_id: str, done: int, failed: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(BatchJob, job_id)
        if job is not None:
            job.done, job.failed = done, failed
            db.commit()
    finally:
        db.close()


def _finalize(job_id: str, done: int, failed: int, error: str | None) -> None:
    db = SessionLocal()
    try:
        job = db.get(BatchJob, job_id)
        if job is None:
            return
        job.done, job.failed = done, failed
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        if error:
            job.status, job.error = "failed", error
        elif failed == 0:
            job.status = "done"
        elif done == 0:
            job.status, job.error = "failed", "整批识别都失败了，请检查 AI 配置或网络"
        else:
            job.status = "partial"       # 有失败但没全废 —— 前端据此提示「N 条需重试」
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- 通过 / 驳回
def approve_item(db: Session, item: BatchItem) -> str:
    """把一条待审条目写进正式题库，返回新题目 id。

    两种形态都带上：`content`（模型识别 + 老师可能改过的文本）与 `image`（原貌图）。
    `render_prefer` 给 auto —— 有文本用文本。理由：**这一条已经被老师审核过了**，
    文本是被确认过的内容；而原貌图始终留着，需要时在出卷页整卷切「图片」即可。
    """
    job = db.get(BatchJob, item.job_id)
    curriculum_id = job.curriculum_id if job else None
    # fuzzy=True 与识别阶段保持一致 —— 否则老师手改过的知识点名又匹配不上了
    kps, node_id, curriculum_id = knowledge.resolve_kp(
        db, [item.knowledge_point] if item.knowledge_point else [], curriculum_id, fuzzy=True
    )
    qid = uuid.uuid4().hex[:12]
    db.add(Question(
        id=qid,
        owner_id=config.OWNER_ID,
        document_id=item.document_id,
        doc_filename=item.doc_filename,
        content=item.content or "",
        qtype=item.qtype,
        difficulty=item.difficulty,
        knowledge_points=json.dumps(kps, ensure_ascii=False),
        tags=item.tags or "[]",
        image=item.image or "",
        # 老师框选出来的补充配图（题干里那幅「如图」的图）。
        # 单独一列而不是拼进 image：文本形态出卷时，原貌图整张都不会用，
        # 而这张图仍然要印 —— 两件事必须能分开取。
        figure_image=item.figure_image or "",
        answer="",
        analysis="",
        created_at=datetime.now().isoformat(timespec="seconds"),
        curriculum_id=curriculum_id,
        node_id=node_id,
        render_prefer="auto",
    ))
    if node_id:
        db.add(QuestionNode(question_id=qid, node_id=node_id, weight=1.0))
    item.status = "approved"
    item.question_id = qid
    item.reviewed_at = datetime.now().isoformat(timespec="seconds")
    db.commit()
    return qid
