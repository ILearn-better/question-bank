# -*- coding: utf-8 -*-
"""AI 整页识别（实验）：**一整页**图进去，**多道题**的结构化记录出来。

用户 2026-10-08 的想法（原话）：
    「文档转图片 → 图片喂给大模型 ai → ai 根据指定格式和 key 输出内容
      （加入判断本题有无图像）→ 输出到文本框 → 人工加入（截取）有配图需求
      题目的图片 → 入库」，特点是「**去掉了人工分割题目步骤**」。

与 batch_import.py（块模式）的关系：
    同一个调用层（`vision.prepare_image_file` + `ai_polish.post_json`）、
    同一个模型、同一套九字段与枚举回落（复用 `fields_from_obj`）。
    差别只有两处：

      块模式：老师先画分界线 → 裁出 N 张块图 → 每张一次调用（一次一题）
      整页模式：整页一张图 → **一次调用** → 模型自己把这一页切成 N 道题

为什么单开一个模块而不是塞进 batch_import：
    · batch_import 已经 600+ 行，且它那份提示词的角色设定是
      「把**这一道**题填成一条记录」（明确写「忽略相邻题目片段」）。
      整页模式的角色设定恰恰相反 —— 必须切分、必须一道不漏。
      两份提示词放在同一个文件里，改哪一份都得先跨过另一份两百行，
      而且很容易「顺手统一措辞」把切分要求弄丢。
    · 整页模式有它**独有**的风险与配套逻辑（见下），不该混进块模式。

⚠️ 整页模式最大的新风险：**输出被 max_tokens 截断**。
   一页五六道解答题，JSON 很长。截断之后 `json.loads` 整段必失败，
   但前面几道题其实**完整且可用**。所以解析层刻意做了「抢救」：
   整体解析失败 → 扫描顶层 `{...}` 逐个救，能救几道是几道（`parse_page_items`）。
   不做这一步的话，老师看到的是「这一页全军覆没」，
   而实际上只差最后一道题 —— 那种体验比多花一秒钟解析糟得多。

⚠️ 另一处与块模式**语义不同**的地方：整页模式**不裁原貌图**。
   题干文本是模型直接给的，原貌图（整块截图）在这个流程里没有下游用途
   （出卷走文本形态），留着只会往 crops/ 里攒没人引用的图。
   条目 `image` 因此为空 —— 需要在待审页看清「这道题在卷面哪一块」时，
   用 `page_no` 调 `region-image` 现渲染（不落盘），见 test_batch_page.py 的说明。

## 配图也自动裁（2026-10-08 第二轮）

原话里「人工加入（截取）有配图需求题目的图片」这一步**也不再需要人工**：
模型在识别时顺便给出那一幅图的位置（`figure_box`，归一化 0~1000），
服务端据此裁好写进 `figure_image`，老师在待审页只用**看一眼**。

做法是「模型指路 + 几何定框」，理由见下面「配图定位」那一节 ——
一句话：模型能指出图在哪一带，但它的框**边界不准**（实测偏高约 1/8 页）；
而数字版 PDF 里每个图形元素都带精确坐标，两者一拼就够用了。
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
from ..models import BatchItem, BatchJob
from . import ai_polish, knowledge, vision
# 复用块模式那套已经调好的低层工具。都是同包内的私有名，
# 抄一份的代价是「两边解析行为不一致」，那比跨模块引用私有名糟得多。
from .batch_import import (
    RETRY,
    MAX_WORKERS,
    RAW_KEEP,
    BatchError,
    _TRAILING_COMMA_RE,
    _extract_object,
    _loads,
    _strip_fence,
    fields_from_obj,
)

# 一次最多识别多少页。理由与块模式的 MAX_JOB_ITEMS 同源：
# **一页 = 一次 AI 请求**，页数就是账单与等待时间。
MAX_PAGES = max(1, int(os.getenv("SHIKE_PAGE_MAX_PAGES", "30")))

# 整页多题的输出预算。块模式单题给 4096 够用，整页要装五六道题的完整 JSON，
# 给小了会在中途截断（截断本身有抢救逻辑，但救出来的必然比完整短）。
# ⚠️ 下限约束同样适用：低于 2000 会因为推理吃满而**静默返回空正文**（vision.py 头部有实测）。
PAGE_MAX_TOKENS = max(2000, int(os.getenv("SHIKE_PAGE_MAX_TOKENS", "8000")))


# ---------------------------------------------------------------- 提示词
# ⚠️ 普通字符串，不是 r-string —— LaTeX 的反斜杠必须写双份（`\\frac`）。
#    单份会被 Python 吃掉（`\f` → 换页符），提示词送到模型手里已经面目全非，
#    而模型照样有输出、只是公式规范变差 —— 典型的「不报错但不对」。
#    同一个坑 vision.py / batch_import.py 头部都记着，改这段后务必跑 test_batch_page.py。
_ENUM_QTYPE = "、".join(taxonomy.QTYPES)
_ENUM_DIFFICULTY = "、".join(taxonomy.DIFFICULTIES)
_ENUM_FIGURE_WORDS = "」「".join(taxonomy.FIGURE_KEYWORDS)

# 整页识别要的字段 —— **顺序 = 提示词里列出的顺序**（测试按这个逐个核对）。
# 加字段时只需要两处：① 这里加个名字 ② _FIELD_RULES 里加一行描述。
# 开头那句「字段固定为下面十二个」和硬要求里的「十二个字段必须全部出现」
# 都由 len(PAGE_FIELDS) 算出来 —— 手工维护迟早会出现
# 「说十个却列了十二个」这种自相矛盾，而模型会照着那个数字偷懒省字段。
PAGE_FIELDS = (
    "content", "answer", "analysis", "qtype", "difficulty", "knowledge_point",
    "tags", "confidence", "needs_figure", "figure_note", "figure_box", "note",
)
_CN_NUM = {8: "八", 9: "九", 10: "十", 11: "十一", 12: "十二", 13: "十三", 14: "十四"}
_N_FIELDS = _CN_NUM.get(len(PAGE_FIELDS), str(len(PAGE_FIELDS)))

# 角色与块模式**刻意不同**：块模式是「把这一道题填成记录」，
# 整页模式是「把这一页切成若干道题，每道填成记录」。
_ROLE = (
    "你是一个数学试卷的结构化录入工具。给你的图是**一整页**试卷，"
    "上面可能有**两道以上**的题目，也可能混着页眉、页脚、页码、条形码、"
    "「四、解答题；本题共 5 小题，共 77 分」这类**分区说明**、"
    "以及「请在此作答」这样的作答提示。\n"
    "你要做的是：把这一页上**每一道真正的题目**分别提取出来，各填成一条结构化记录。"
)

_FIELD_RULES = (
    "\n\n请把图中这一页上的**所有题目**录入成结构化数据。\n"
    '只输出**一个 JSON 对象**，形如 {"questions": [ {...}, {...} ]} —— '
    "不要解释、不要客套、不要 Markdown 代码围栏、不要 JSON 之外的任何文字。\n"
    "\nquestions 里每个元素是**一道题**，字段固定为下面" + _N_FIELDS
    + "个，一个都不能少：\n"
    '  "content"         字符串。这一道题的题干全文。行内公式用 $...$ 包住，'
    "独立成行的公式用 $$...$$ 包住；分数写 \\frac{}{}、根式写 \\sqrt{}、"
    "上下标记作 ^{} 与 _{}，一律用标准 LaTeX 命令。"
    "选择题的选项（(A)(B)(C)(D) 或 A. B. C. D.）要保留，选项里的公式同样转成 LaTeX。"
    "**题干里只放题目本身**：答案、解析、分值、以及「（25-26 七年级上·浙江杭州·期末）」"
    "这类来源标注都不要混进来。\n"
    '  "answer"          字符串。这道题的**最终答案**，只写答案本身 —— '
    "选择题写选项字母（例如 \"B\"）；填空题写填进去的内容（多个空按顺序用「；」隔开）；"
    "解答题写最终结论（例如 \"$x=1$ 或 $x=3$\"）。"
    "**不要**写「故选 B」「解：」这类过程性的话，也不要把题干再抄一遍。"
    "本页没有这道题的答案时给空字符串 \"\"。"
    "⚠️ 选择题题干里的 (A)(B)(C)(D) 是**选项**不是答案 —— "
    "卷面上没有【答案】字样标出时，answer 就留空，不要自己算一个填进去。\n"
    '  "analysis"        字符串。这道题的**解析过程**：卷面上标着【分析】【详解】'
    "【点睛】【解】【点评】的段落，按原顺序合并成一段（段与段之间空一行），"
    "照录、不要改写也不要概括。公式同样用 $...$ / $$...$$ 包住。"
    "**不要**把题干和答案重复抄进来。本页没有解析时给空字符串 \"\"。\n"
    '  "qtype"          字符串。**必须**是下面之一：' + _ENUM_QTYPE + "。\n"
    '  "difficulty"     字符串。**必须**是下面之一：' + _ENUM_DIFFICULTY + "。\n"
    '  "knowledge_point" 字符串。这道题考查的**最主要**那一个知识点，'
    "用教材或课标里的通用叫法（例如「三角函数的图像与性质」「导数的几何意义」），"
    "10 到 20 个汉字。判断不了就给空字符串。\n"
    '  "tags"           字符串数组，0 到 4 个，每个 2 到 6 个汉字，'
    '例如 ["含参讨论","恒成立"]。不要与 knowledge_point 重复，不要放「数学」「高中」这类'
    "没有区分度的词。\n"
    '  "confidence"     字符串。**必须**是 high、medium、low 之一 —— '
    "你对 content 转录准确程度的自评：公式复杂、字迹模糊、跨页截断时给低。\n"
    '  "needs_figure"    布尔值，只能写 true 或 false（不要写成字符串 "true"）。'
    "判断**这道题是不是真的配着一幅图**。只有两种情况给 true："
    "① 这一页的卷面上确实画着这幅图（那你就必须同时给出 figure_box）；"
    "② 题干明确指向一幅图（出现「" + _ENUM_FIGURE_WORDS + "」这类措辞），"
    "而那幅图印在**别的页**上、本页没有。\n"
    "     ⚠️ 仅仅在文字里**提到**某种图形或几何体 —— 比如「正三棱柱 $ABC-A_1B_1C_1$」"
    "「抛物线 $C$」「圆上一点」—— 而题干并没有指向任何一幅具体的图时，"
    "给 false。这种题在卷面上根本没有图可框，判 true 只会让老师多核对一次。\n"
    '  "figure_note"     字符串。needs_figure 为 true 时，用**一句话**说明这张图是什么、'
    "大致长什么样（例如「一个开口向上的抛物线，与 x 轴交于两点」）；"
    "为 false 时给空字符串。\n"
    '  "figure_box"      数组，形如 [x0,y0,x1,y1]，**归一化坐标**：'
    "取值 0 到 1000 的整数，原点在**整页图的左上角**，x 向右、y 向下。"
    "needs_figure 为 true 时，给出**那一幅图本身**在图上的位置 —— 只框住图形本身"
    "（含坐标轴、箭头、虚线、图形上的字母与数字标注），"
    "**不要**把题干文字、题号、「如图」这两个字、选项框进去。"
    "图在**下一页**（本页只见到「如图」而图不在本页）、或者你定位不了时，给空数组 []，"
    "并在 note 里写一句是哪一页 / 为什么给不出。"
    "needs_figure 为 false 时也给 []。\n"
    '  "note"           字符串。看不清、有歧义、图形无法用文字表达、'
    "或者题目在本页边缘被截断的地方，用一句话说明；没有就给空字符串。\n"
    "\n七条硬要求：\n"
    "1. **必须切分**。这一页有几道题，questions 里就放几个元素。"
    "不要把两道题合并成一条，也不要漏掉任何一道。\n"
    "2. **不属于题目的东西一律不要**。页眉、页脚、页码、条形码、"
    "「四、解答题；本题共 5 小题，共 77 分」这种**只有分区说明、没有题干**的行、"
    "「请在此作答」这类提示，都**不要**成为 questions 的元素。"
    "（这一条最容易被忽略 —— 把分区说明录成一道题，老师还得手动驳回。）\n"
    "3. qtype 与 difficulty **只能**从上面给出的取值里原样照抄，不要写同义词"
    "（不要写「解答」「证明」「中等」「简单」「较难」）。\n"
    "4. " + _N_FIELDS + "个字段**必须全部出现**。取不到值时用空字符串 \"\"、空数组 [] 或 false，"
    "不要省略字段、不要写 null。\n"
    "5. **跨页的题照常输出**：某道题在图的最下边缘被截断（明显未完）时，"
    "仍然输出它，并在 note 里写「疑似在本页底部被截断」。"
    "它后面那半页在下一页，会由下一页的识别另行给出 —— 你不要去猜下半页的内容。\n"
    "6. **答案与解析只收本页的**：题目后面紧跟【答案】【详解】【分析】【点睛】的，"
    "分别收进 answer 与 analysis；**本页没有就留空** —— 不要自己解题，"
    "更不要把上一题或下一题的答案挪过来。答案印在下一页的，留给下一页。\n"
    "7. **解析到下一题为止**：解析区结束、下一道题的题干开始时必须切开，"
    "下一题的题干和它的答案都不能混进本题的 analysis。"
    "如果整页只有「参考答案」（只有「1.B 2.C 3.A」这种，根本看不到题干），"
    "输出 {\"questions\":[]} —— 不要凭答案反过来编题。\n"
    "\n输出示例（仅示意格式与字段，内容以图为准）：\n"
    '{"questions":['
    '{"content":"已知函数 $f(x)=x^2+2x-3$，求 $f(x)$ 的零点。",'
    '"answer":"$x=-3$ 或 $x=1$",'
    '"analysis":"令 $f(x)=0$ 得 $x^2+2x-3=0$，因式分解为 $(x+3)(x-1)=0$，'
    '所以 $x=-3$ 或 $x=1$。","qtype":"解答题",'
    '"difficulty":"基础","knowledge_point":"函数的零点","tags":["一元二次方程"],'
    '"confidence":"high","needs_figure":false,"figure_note":"","figure_box":[],"note":""},'
    '{"content":"如图，在 $\\triangle ABC$ 中，$AB=AC$，求 $\\angle B$。",'
    '"answer":"","analysis":"",'
    '"qtype":"解答题",'
    '"difficulty":"中档","knowledge_point":"等腰三角形的性质","tags":[],'
    '"confidence":"high","needs_figure":true,"figure_note":"一个等腰三角形，顶角在上",'
    '"figure_box":[120,300,460,640],"note":""}'
    "]}"
)

PAGE_PROMPT = _ROLE + _FIELD_RULES


# ---------------------------------------------------------------- 解析（含抢救）
def _iter_json_objects(s: str):
    """逐个吐出 s 里的**顶层** `{...}`（考虑字符串内的括号与转义）。

    为什么不能用正则：题干的 LaTeX 里全是花括号（`\\frac{a}{b}`），
    正则分不清「JSON 结构的括号」和「字符串内容里的括号」，
    会把一道题切成好几段。
    为什么遇到不闭合**不 return**：输出被截断时，最外层那个
    `{"questions":[` 永远不闭合；若直接放弃，里面的题目一道都救不出来。
    继续往后扫才能命中每一道完整的题对象。代价是 O(n²) —— 输出只有几 KB，无所谓。
    """
    n = len(s)
    i = 0
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        closed = False
        while j < n:
            c = s[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield s[i: j + 1]
                    closed = True
                    break
            j += 1
        if not closed:
            # 这一层没有闭合 → 从下一个字符继续找（内层的题目对象在等着被救）
            i += 1
            continue
        i = j + 1


def _looks_like_question(o: dict) -> bool:
    """抢救出来的对象得长得像一道题，避免把 `{"note":"..."}` 这类碎片也收进来。"""
    return isinstance(o, dict) and ("content" in o or "qtype" in o)


def parse_page_items(text: str) -> list[dict]:
    """把整页的模型返回解析成**多道题**。返回 list[dict]（fields_from_obj 的结果）。

    **永不抛异常**。三条路依次尝试，越往后越"脏"：

      ① 整体解析成对象：我们要求的 `{"questions":[...]}`（或顶层直接是数组）
      ② 顶层是数组（模型没套那层壳）
      ③ **抢救**：扫描顶层 `{...}` 逐个解析 —— 专治输出被 max_tokens 截断
         （前半段的题目其实是完整的），以及模型在 JSON 前后夹了废话的情况

    返回空列表 = 模型明确说了「这页没有题」（`{"questions": []}`），
    这与「解析失败」**必须区分**：前者是正常结果（空白页、页尾），
    后者要让老师看见。所以解析失败时返回**一条**带 `bad_json` 的占位题 ——
    宁可让老师驳回一条空的，也不要让一整页悄无声息地消失。
    """
    flags0: list[str] = []
    objs: list[dict] = []

    # ① 整体解析（我们要求的形状）
    candidate = _extract_object(text)
    if candidate:
        obj, repaired = _loads(candidate)
        if obj is not None:
            if repaired:
                flags0.append("json_repaired")
            qs = obj.get("questions")
            if qs is None:
                # 模型偶尔改用别的键名。能认就认，省一次人工返工。
                for k in ("items", "data", "list", "result", "problems"):
                    if isinstance(obj.get(k), list):
                        qs = obj[k]
                        break
            if isinstance(qs, list):
                objs = [x for x in qs if isinstance(x, dict)]
            elif _looks_like_question(obj):
                # 模型只给了一道题（这页确实只有一题）—— 也接受
                objs = [obj]

    # ② 顶层就是一个数组
    if not objs:
        i, j = text.find("["), text.rfind("]")
        if 0 <= i < j:
            try:
                arr = json.loads(_TRAILING_COMMA_RE.sub(r"\1", text[i: j + 1]))
                if isinstance(arr, list):
                    objs = [x for x in arr if isinstance(x, dict)]
            except json.JSONDecodeError:
                pass

    # ③ 抢救：逐个扫描顶层对象（截断 / 夹废话都靠这条）
    salvaged = False
    if not objs:
        for cand in _iter_json_objects(_strip_fence(text)):
            o, _ = _loads(cand)
            if o is not None and _looks_like_question(o):
                objs.append(o)
        salvaged = bool(objs)

    if not objs:
        # 走到了这里只有两种可能：这页真的没题（模型给了空数组），或者彻底解析不出。
        # 「真的没题」在 ① 里就能识别出来（qs 是空列表 → objs 为空但**没有**报错信息），
        # 用一个哨兵区分：整体解析成功过、且 questions 是列表 → 空列表是可信的。
        trusted_empty = "json_repaired" in flags0 or _is_trusted_empty(text)
        if trusted_empty:
            return []
        flags = flags0 + ["bad_json", "empty_stem"]
        placeholder = fields_from_obj({}, flags)
        placeholder["note"] = "这一页没能解析出内容（可能是输出被截断或模型没按格式返回）"
        placeholder["figure_box"] = None
        return [placeholder]

    flags0 = flags0 + (["json_salvaged"] if salvaged else [])
    out = []
    for i, o in enumerate(objs):
        # 每一题各自一份 flags —— 抢救出来的那些要标出来，老师才知道「这条可能不完整」
        f = list(flags0)
        r = fields_from_obj(o, f)
        # 配图框不放 fields_from_obj 里：那是**块模式与整页模式共用**的函数，
        # 而块模式根本没有「图上哪一幅图」这个概念（它拿到的就是整块图）。
        r["figure_box"] = norm_figure_box(o.get("figure_box"), f)
        r["raw"] = None
        out.append(r)
    return out


def _is_trusted_empty(text: str) -> bool:
    """模型是否**明确说过**「这页没有题」（而不是我们没解析出来）。

    判据：文本里有一个合法的 `{"questions":[]}`（允许键名/空白差异）。
    刻意写窄 —— 宽了会把「解析失败」误判成「这页本来就没题」，
    那正是最坏的结果：整页静默消失，老师完全不知道漏了一页。
    """
    t = _strip_fence(text).replace(" ", "").replace("\n", "")
    if not re.search(r'"questions"\s*:\s*\[\s*\]', t):
        return False
    try:
        obj = json.loads(t)
        return isinstance(obj, dict) and obj.get("questions") == []
    except json.JSONDecodeError:
        return False


# ---------------------------------------------------------------- 配图定位（模型指路 + 几何定框）
# 目标：配图**自动裁好**，老师在待审页只用看一眼，不再需要自己框。
#
# 为什么不直接把模型给的框拿来裁 —— 实测过（2025 高考全国一卷第 2 页的坐标系图）：
#   模型给的框 y 方向整体偏高一截，只盖住表格下半 + 图形的顶部。
#   模型能指出「这题有图、大概在这一带」，但**边界不可用**。
# 而数字版 PDF 里每个图形元素都带着精确坐标（位图 `get_image_info`、
# 矢量 `get_drawings`，见 `pdf.figure_candidates`）——
# 所以把两者的长处拼起来：模型负责**认出哪道题有图、图在哪一带**，
# 几何负责**边界**。实测那一页：模型粗框 → 吸附到候选区 → 恰好是那幅坐标系图。
#
# ⚠️ 扫描版没有几何可依据（整页就是一张位图，被 figure_candidates 的 MAX 规则排掉）：
#    那种情况只能退回模型自己的框，并打 `figure_rough` 标记 ——
#    框是估的，得让老师一眼看出来（否则「自动配的图」会被默认成可信的）。
_FIGURE_PAD_PT = 3.0        # 裁图时四周各留一点，别把图形边缘（箭头尖、描边）切掉
_FIGURE_MIN_NORM = 15       # 归一化框最短边下限（0~1000 制）。比这还小的框不是图
_SNAP_IOU = 0.10            # 与候选区 IoU 达到这个值 → 就是它
_SNAP_NEAR = 0.25           # 或者中心点在这一比例内（页宽/页高）也算同一幅图
_CLAIM_DECISIVE = 1.5       # 两题抢同一处图形时，「赢家明显更近」的倍数（见 plan_page_figures）


def _box4(v) -> list[float] | None:
    """把 `figure_box` 的各种形态读成 4 个数：数组 / 对象 / "x0,y0,x1,y1" 字符串。

    为什么要容错到这一步：模型对「数组」这件事的理解很飘 ——
    见过给成 `{"x0":..,"y0":..}` 的、给成 `"[50,220,400,380]"` 字符串的。
    这些都是一次重试就能救回来的内容，没必要让它变成「这题没配图」。
    """
    if v is None:
        return None
    if isinstance(v, dict):
        for ks in (("x0", "y0", "x1", "y1"), ("xmin", "ymin", "xmax", "ymax"),
                   ("left", "top", "right", "bottom"), ("x", "y", "x2", "y2")):
            if all(k in v for k in ks):
                v = [v[k] for k in ks]
                break
        else:
            return None
    if isinstance(v, str):
        nums = re.findall(r"-?\d+(?:\.\d+)?", v)
        if len(nums) != 4:
            return None
        v = nums
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    out = []
    for x in v:
        try:
            f = float(x)
        except (TypeError, ValueError):
            return None
        if f != f or f in (float("inf"), float("-inf")):    # NaN / inf
            return None
        out.append(f)
    return out


def norm_figure_box(raw, flags: list[str]) -> list[int] | None:
    """模型给的图框 → 统一的「归一化 0~1000 整数框」；不可信时返回 None。

    **永不抛异常**，坏值一律退化 + 打 flag —— 与解析层同一个原则：
    一条框坏了不该让这页其余题目跟着失败，更不该写进一张错的图。
    """
    vals = _box4(raw)
    if vals is None:
        return None
    # 有的模型习惯用 0~1 的比例。判据用「全部绝对值 ≤ 1.2」——
    # 0~1000 制下没有任何一幅图会整个落在 0~1.2 里，所以不会误判。
    if max(abs(x) for x in vals) <= 1.2:
        vals = [x * 1000.0 for x in vals]
        flags.append("figure_box_fraction")
    if any(x < 0 or x > 1000 for x in vals):
        # 超出 0~1000：可能是像素坐标、也可能是模型乱写。夹取后**必须标记**，
        # 否则老师看到一张裁歪的图，完全不知道为什么。
        flags.append("figure_box_clamped")
        vals = [min(1000.0, max(0.0, x)) for x in vals]
    x0, x1 = sorted((vals[0], vals[2]))          # 模型可能给成 x1 < x0
    y0, y1 = sorted((vals[1], vals[3]))
    if x1 - x0 < _FIGURE_MIN_NORM or y1 - y0 < _FIGURE_MIN_NORM:
        flags.append("figure_box_tiny")
        return None
    return [int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))]


def _iou(a, b) -> float:
    """两个矩形（x0,y0,x1,y1 元组）的交并比。0 表示不相交。

    ⚠️ 刻意用纯元组算，**不 import pymupdf** —— 全项目只有 adapters/pdf.py
    可以 import 它（AGPL 许可约束，见那个文件头部）。这条规矩在这里最容易被破：
    顺手 `import pymupdf` 算个矩形，代码照样跑，但许可边界就被捅了个洞。
    """
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _padded(rect, W, H):
    """四周留白并夹进页面范围。"""
    return (max(0.0, rect[0] - _FIGURE_PAD_PT), max(0.0, rect[1] - _FIGURE_PAD_PT),
            min(W, rect[2] + _FIGURE_PAD_PT), min(H, rect[3] + _FIGURE_PAD_PT))


def resolve_figure_box(box_norm, cands, W, H, allow_only: bool = False):
    """决定**实际要裁的点坐标框**。返回 (框 | None, 方式)。

    方式取值：
      "snapped"       吸附到页面几何候选区（数字版 PDF）—— 边框就是图形本身的边框
      "snapped_only"  模型没给框，但整页只有**一处**图形，只能是它
      "rough"         页面查不到候选（扫描版），只能用模型估的框
      ""              没有可用依据 → 不裁（留空，等老师自己框）

    ⚠️ `allow_only` 由调用方在**页级**算好传进来（见 _write_page_items）：
       这条兜底的判断依据是「这一页有几道题要图」，单条调用者看不到。
       实测踩过：模型说 4 道题有图却没给框，兜底就把**同一幅图贴给了 4 道题** ——
       4 张一模一样的图挂在 4 道不相干的题上，比不配图还糟。

    ⚠️⚠️ 但**只把 allow_only 算对还不够**：调用方必须**只对「这道题自己要图」的条目**
       调这个函数（`needs_figure` 为真，或模型给了框）。实测踩过第二次 ——
       p6 上只有一道题 `needs_figure=true`（allow_only 因此为真），可**每一条**都走了裁图，
       于是另外 3 道 `needs_figure=false` 的题也落进这条兜底，
       全拿到了同一张图（flags 是 `figure_only_candidate`、`figure_note` 还是空的）。
       判断依据是页级的，授权却按条生效 —— 两个粒度混用就漏。
    """
    if cands:
        rects = [tuple(c["rect"]) for c in cands]
        if box_norm:
            b = (box_norm[0] / 1000.0 * W, box_norm[1] / 1000.0 * H,
                 box_norm[2] / 1000.0 * W, box_norm[3] / 1000.0 * H)
            ious = [_iou(b, r) for r in rects]
            k = max(range(len(rects)), key=lambda i: ious[i])
            if ious[k] >= _SNAP_IOU:
                return _padded(rects[k], W, H), "snapped"
            # IoU 为 0 也还有救 —— 模型整体偏了一截时（实测就是这种情况），
            # 两个框可能一点都不重叠。这时改用「中心点距离」判是不是同一幅图。
            bcx, bcy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            dists = [(abs((r[0] + r[2]) / 2 - bcx) / W, abs((r[1] + r[3]) / 2 - bcy) / H)
                     for r in rects]
            k2 = min(range(len(rects)), key=lambda i: dists[i][0] + dists[i][1])
            if dists[k2][0] <= _SNAP_NEAR and dists[k2][1] <= _SNAP_NEAR:
                return _padded(rects[k2], W, H), "snapped"
            # 页码多、图也多时可能挑错，但挑错的代价只是「图不对，重框一下」，
            # 而放着模型那个歪框不用的代价是「框歪了也得重框」—— 前者至少可能对。
            return _padded(b, W, H), "rough"
        if len(rects) == 1 and allow_only:
            return _padded(rects[0], W, H), "snapped_only"
        return None, ""
    if box_norm:
        return _padded((box_norm[0] / 1000.0 * W, box_norm[1] / 1000.0 * H,
                        box_norm[2] / 1000.0 * W, box_norm[3] / 1000.0 * H), W, H), "rough"
    return None, ""


def _norm_box(rect, W, H) -> list[int]:
    """点坐标框 → 归一化 0~1000 的整数框（落库用的就是它）。"""
    return [int(round(rect[0] / W * 1000)), int(round(rect[1] / H * 1000)),
            int(round(rect[2] / W * 1000)), int(round(rect[3] / H * 1000))]


def _claim_score(box_norm, rect, W, H) -> tuple[float, float]:
    """这道题「认领」这处图形的可信度。返回 `(是否不如图形高, 中心距离)`，越小越可信。

    ⚠️ 只看中心距离**不够**，得先看「框在图形的上方还是下方」：
       这类讲义的排版是**题目在上、图在下**，所以「框在图形上方」的那道题才可能是主人。
       实测 p12：那处横排是「将下图中的立体图形分类」题的 8 个几何体，
       可它的框整体偏上（距离 0.159），下面那道「分别观察下列几何体」的框指向答案段落、
       反而更近（0.137）—— 光比距离就把图判给了下面那道。
       加上「上下」这一维之后，两道题各归各位。

    顺序这么做还解决了 p11：第 2 题（棱锥的四个选项）离**第 3 题**的实物图 0.235，
    而第 3 题自己离它是 0.118 —— 距离和上下都指向第 3 题，判得对。
    """
    if not box_norm:
        return (0.0, 0.0)     # 没给框却拿到候选，只可能是「整页仅此一题要图」
    bcx = (box_norm[0] + box_norm[2]) / 2000.0
    bcy = (box_norm[1] + box_norm[3]) / 2000.0
    rcx = (rect[0] + rect[2]) / 2 / W
    rcy = (rect[1] + rect[3]) / 2 / H
    return (0.0 if bcy <= rcy else 1.0, abs(bcx - rcx) + abs(bcy - rcy))


def plan_page_figures(items: list[dict], cands: list[dict], W: float, H: float) -> list[dict]:
    """页级配图分配 —— **纯函数**，不裁图也不写库。返回与 `items` 等长的决策列表。

    每项 `{"rect": 点坐标框 | None, "box": 归一化框 | None, "mode": 方式, "dup": 是否被别人占了}`。

    为什么要单拎出来：配图的三件事（要不要图 / 框在哪 / 谁占哪一处）**全是几何判断**，
    跟 AI 没关系。拎成纯函数之后，单测和「离线重放」都能直接调它 ——
    不会出现「重放脚本自己抄一份逻辑、和真正跑的那份悄悄不一致」。

    三条规则，每条都对应一次实测踩坑：
    ① `needs_figure` 为假、也没给框 → 不配图。页级算出来的 `allow_only` **不借给**这类题。
    ② `allow_only` 只在「这一页只有一道题要图、且只有一处候选」时为真。
    ③ 一处几何位置一页只归一道题，**离得最近的那道赢**，其余留空并标 `dup`。

    ⚠️ 第③条的「最近」只在**明显更近**时才敢下定论（`_CLAIM_DECISIVE`）。
       实测 p12：那处横排确实是「将下图中的立体图形分类」题的 8 个几何体，
       可它的框整体偏上（0.159），下面那道「分别观察下列几何体」的框指向答案段落、
       反而更近（0.137）—— 光比距离会判错。所以差距不够大时不硬判：
       图照给赢家，但**赢家也打上 `shared`**，让老师看一眼再决定。
       宁可多一个待核对标记，也不要把「错了但看着像对的」塞进题库。
    """
    wanting = [(i, r) for i, r in enumerate(items)
               if r.get("needs_figure") or r.get("figure_box")]
    allow_only = (len(wanting) == 1 and len(cands) == 1)

    claims = []
    for i, r in wanting:
        rect, mode = resolve_figure_box(r.get("figure_box"), cands, W, H,
                                        allow_only=allow_only)
        if rect is None:
            continue
        claims.append((_claim_score(r.get("figure_box"), rect, W, H), i, rect, mode))

    # 同一处框有多道题要 → 只留一道；其余 `dup`，赢家如果赢得不干脆则 `shared`
    by_key: dict[tuple, list] = {}
    for score, i, rect, mode in claims:
        by_key.setdefault(tuple(_norm_box(rect, W, H)), []).append((score, i, rect, mode))

    best: dict[int, tuple] = {}
    for lst in by_key.values():
        lst.sort(key=lambda t: t[0])
        score, i, rect, mode = lst[0]
        shared = False
        if len(lst) > 1:
            (a0, d0), (a1, d1) = lst[0][0], lst[1][0]
            # 一道在图形上方、一道在下方 → 上面那道才是主人，判得干脆；
            # 同一侧就要看距离差得多不多，差得不多就不硬判（标 shared 请老师核）。
            shared = not (a0 < a1 or (a0 == a1 and d0 * _CLAIM_DECISIVE < d1))
        best[i] = (rect, mode, shared)

    out: list[dict] = []
    for i, r in enumerate(items):
        if i in best:
            rect, mode, shared = best[i]
            out.append({"rect": rect, "box": _norm_box(rect, W, H),
                        "mode": mode, "dup": False, "shared": shared})
        elif r.get("needs_figure") or r.get("figure_box"):
            out.append({"rect": None, "box": None, "mode": "", "dup": True, "shared": False})
        else:
            out.append({"rect": None, "box": None, "mode": "", "dup": False, "shared": False})
    return out


def crop_figure_for_item(src: str, pno: int, rect, mode: str, W: float, H: float,
                         flags: list[str]):
    """按 `plan_page_figures` 已定好的框裁图。返回 (url | None, 归一化框 | None)。

    落盘走 `pdf.crop_region` → `data/uploads/crops/`（**资产目录**：
    每张图都有数据库行引用它，删的时候靠引用计数回收，见 services/crops.py）。
    这张图被写进了 `batch_items.figure_image`，所以它是有主的，不是垃圾。
    """
    if rect is None:
        return None, None
    from ..adapters import pdf as pdf_adapter
    name = pdf_adapter.crop_region(src, pno, list(rect), str(config.CROPS_DIR))
    flags.append({
        "snapped": "figure_snapped",
        "snapped_only": "figure_only_candidate",
    }.get(mode, "figure_rough"))
    return f"/api/crops/{name}", _norm_box(rect, W, H)


# ---------------------------------------------------------------- 兜底：整题原貌图
# 有些题目配图**永远定位不到**：扫描页没有几何候选、纯线框图形不含斜线曲线、
# 或者模型干脆没给框（库里实测这类占「要配图的题」的 15.7%）。
# 与其让老师一张张手工框，不如把**整道题**裁成图 —— 图一定在里面，
# 代价是这道题出卷改用图片形态（`render_prefer=image`，见 batch_import.approve_item），
# 于是题干不能再编辑、题库里也搜不到。这个取舍是用户点名要的：
# 「配图怎么都框不准就整题原貌图，零人工」。
_WHOLE_FOOTER_CUT = 0.96     # 页底留一点：页码 / 水印别裁进来
_WHOLE_MIN_H = 12.0          # 比这还矮说明定位串了（两道题的首行 y 几乎相同）

# 这张图「还可疑 / 还没图」的几个标记。**唯一出处。**
# 三个地方要用它：①审核页摘标记（routers/batches）、②界面分档（batch.html 的 figTierOf）、
# ③自动导入判「能不能直接入库」（services/auto_import）。②是 JS 没法共享，
# 靠 frontend/batch_fig_check.js 比对；①③必须都从这里取，否则会出现
# 「界面显示可放心、自动导入却不肯入库」这种自相矛盾。
#
# 语义都是「这张图没人核过」，所以人核过一次就该全部消掉。
DOUBT_FIGURE_FLAGS = (
    "figure_rough",              # 框是模型估的，可能歪
    "figure_shared_candidate",   # 同页还有别的题也要这处图形，赢家赢得不干脆
    "figure_dup_candidate",      # 这处图形被判给了同页别的题，本条目因此没图
    "figure_box_missing",        # 模型说有图但没给可用框
    "figure_crop_failed",        # 按框裁图失败
    "figure_cross_page",         # 图多半在下一页（跨页题的上半段）
)


def plan_whole_regions(starts: list, W: float, H: float,
                       stops: list | None = None, bottom: float | None = None) -> list:
    """每题首行 y → 每题的版面范围 `(x0, y0, x1, y1)`。**纯函数**，不裁图不写库。

    下边界取三者里**最靠上**的那个：
      ① 下一个认得出的题首行 —— 再往下就是别人的题
      ② 本题之后最近的段落结束标记（`【答案】`/`【详解】`/`必记结论`，见 `pdf.page_marks`）
      ③ 正文下界 `bottom`（已去掉页脚页码）
    最后一题没有 ①，就靠 ②③ 收口。

    为什么非要 ②：**页内最后一题后面紧跟答案和详解**。只按 ① 的话最后一题
    一路裁到页底，答案就跟着印进卷子 —— 实测 p6/p14/p29 三张全中，
    比留空更糟。③ 同理：原来按页高 96% 收，页码「4 / 44」正好卡在里面。

    为什么上边界取首行、不再往上找：上一题的图常常贴在本页顶部、紧挨着本题首行；
    再往上扩会把隔壁题的图裁进来 —— 而这张图要印在卷子上。

    ⚠️ `starts[i]` 为 None（这题的首行没认出来）→ 本题**不兜底**，
       但它的 y 也不参与别人的边界计算（见下一条）。
    ⚠️ 下边界要找**第一个认得出**的后续题，不能只看 i+1：
       中间的题认不出时，若退回页底，本题会把后面几道题一起裁进来。
    ⚠️ 边界先取「任何比本题更靠下的首行」，再看算出来的高度够不够一道题 ——
       不能要求「至少差 _WHOLE_MIN_H 才算边界」：那样两道题首行挨得极近时
       （定位串到同一个文字块上）会直接退到页底，把整页后半段都裁进这一题里。
    """
    floor = H * _WHOLE_FOOTER_CUT if bottom is None else min(H * _WHOLE_FOOTER_CUT, float(bottom))
    stops = sorted(s for s in (stops or []) if s is not None)
    out: list = [None] * len(starts)
    for i, y0 in enumerate(starts):
        if y0 is None:
            continue
        end = None
        for j in range(i + 1, len(starts)):
            if starts[j] is not None and starts[j] > y0 + 0.5:
                end = float(starts[j])
                break
        if end is None:
            end = floor
        # 答案/详解/知识点框：本题范围内第一个标记就是硬边界
        for s in stops:
            if s > y0 + 0.5:
                end = min(end, s)
                break
        if end - y0 < _WHOLE_MIN_H:
            continue
        out[i] = (0.0, max(0.0, y0 - 2.0), W, min(H, end - 1.0))
    return out


def merge_row_candidates(cands: list[dict], W: float) -> list[dict]:
    """把**同一横排**的候选并成一条「整排」。**纯函数**，只算矩形。

    为什么需要：选择题的 A/B/C/D 常是四幅分开的小图，几何上就是四个独立候选。
    老师要的是「四幅一起」那张 —— 只点其中一幅，卷子上就少三个选项。
    自动配图以前也可能踩这个：吸附到 A 那一幅，看着像配好了，其实少了三个选项。

    判据（两条都要满足）：
      · 纵向区间重叠 ≥ 较矮那个的一半 —— 同一行（不是上下两道题的图）
      · 横向间隙 ≤ 页宽 25% —— 隔太远的是两件事，不是一排选项

    只返回**并了 2 个以上**的排：单张并出来的和原候选一模一样，多列一遍只会让人困惑。

    ⚠️ **必须两段式**：先按纵向重叠「聚成排」，排内再按 x 排序判横向间隙。
    一段式（先按 y0 全局排序、边扫边并）会漏并 —— 四幅选项图的 y0 天然错开几个点
    （实测 100/110/120），排序后处理顺序变成「最左 → 最右 → 中间」，中间那幅被判成
    横向隔太远，整排就从第二幅起算，卷面上还是少一个选项。
    """
    items = [tuple(c["rect"]) for c in cands if c.get("rect")]
    if len(items) < 2:
        return []

    # ---- ① 纵向聚排：谁的纵向区间跟排里任一成员重叠够多，就进这一排 ----
    clusters: list[list[tuple]] = []
    for r in sorted(items, key=lambda r: (r[1], r[0])):
        for cl in clusters:
            if any(_same_row(o, r) for o in cl):
                cl.append(r)
                break
        else:
            clusters.append([r])

    # ---- ② 排内按 x 扫，横向间隙够小就并成一段 ----
    out: list[dict] = []
    for cl in clusters:
        if len(cl) < 2:
            continue
        cl.sort(key=lambda r: r[0])
        cur = None
        cnt = 0
        for r in cl:
            if cur is not None and r[0] - cur[2] <= W * 0.25:
                cur = [min(cur[0], r[0]), min(cur[1], r[1]),
                       max(cur[2], r[2]), max(cur[3], r[3])]
                cnt += 1
                continue
            if cnt >= 2:
                out.append({"kind": "row", "rect": (cur[0], cur[1], cur[2], cur[3])})
            cur, cnt = [r[0], r[1], r[2], r[3]], 1
        if cnt >= 2:
            out.append({"kind": "row", "rect": (cur[0], cur[1], cur[2], cur[3])})
    return out


def _same_row(a, b) -> bool:
    """两个矩形是不是「同一横排」——纵向重叠 ≥ 较矮那个的一半。"""
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    shorter = min(a[3] - a[1], b[3] - b[1])
    return shorter > 0 and overlap >= shorter * 0.5


def _range_has_figure(rect, cands) -> bool:
    """这段范围里**确实有一处图形候选** —— 兜底才有意义。

    ⚠️ 不验这一步会出「假兜底」：跨页题的图在下一页，本题范围内只有题干文字，
       裁出来的「整题图」里根本没有图。老师看到有图就放心了，卷子印出来还是缺图 ——
       这比留空更糟（留空至少还挂着「没定位到图」的提示）。
       实测一份 44 页讲义：没配到图的 14 条里，11 条都是这种跨页情况。
    """
    if rect is None or not cands:
        return False
    for c in cands:
        r = c["rect"]
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        if rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]:
            return True
    return False


def crop_whole_question(src: str, pno: int, rect, flags: list[str]) -> str:
    """把整道题裁成原貌图，返回 URL（失败给空串）。"""
    if rect is None:
        return ""
    from ..adapters import pdf as pdf_adapter
    try:
        name = pdf_adapter.crop_region(src, pno, list(rect), str(config.CROPS_DIR))
    except Exception:                       # noqa: BLE001
        return ""
    flags.append("figure_whole_question")
    return f"/api/crops/{name}"


# ---------------------------------------------------------------- 单页识别
def recognize_page(db: Session, image_path, hint: str = "") -> list[dict]:
    """识别**一整页**，返回该页的题目列表（0 到 N 道）。

    图片预处理与 HTTP 调用完全复用 vision / ai_polish 那一层 ——
    压缩到长边 1600、JPEG q85、reasoning_effort=low 这些硬约束
    在 vision.py 头部写着，这里一个字都不重写。
    """
    cfg = ai_polish.get_config(db)
    if not cfg["configured"]:
        raise vision.VisionError(
            "还没有配置 AI 接口（地址 / 模型 / 密钥），请到「设置 → AI 润色」里填写。"
        )

    uri = vision.prepare_image_file(image_path)

    prompt = PAGE_PROMPT
    if (hint or "").strip():
        prompt += "\n\n老师的补充说明（以此为准）：" + hint.strip()

    body = {
        "model": cfg["model"],
        "messages": [{
            "role": "user",
            # 图在前、要求在后：先看清内容，再读该怎么输出（与 vision.py 一致）
            "content": [
                {"type": "image_url", "image_url": {"url": uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0,                 # 转录要还原，不要发挥
        "max_tokens": PAGE_MAX_TOKENS,    # 整页多题，比单题给得多（见常量注释）
        "reasoning_effort": "low",        # 不传的话推理吃掉 95% 预算（vision.py 头部）
        "stream": False,
        # 顶层是对象（questions 是数组），所以 json_object 模式仍然适用。
        # 不认它的服务商回 400 → 自动去掉重试（与 recognize_fields 同一套降级）。
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    timeout = min(int(cfg["timeout"] or 60), 180)   # 整页比单题慢，给到 3 分钟

    def _call(b: dict) -> dict:
        return ai_polish.post_json(ai_polish.chat_url(cfg["base_url"]), b, headers, timeout)

    try:
        data = _call(body)
    except ai_polish.AiError as e:
        msg = str(e)
        dropped = False
        if "response_format" in body and "response_format" in msg:
            body.pop("response_format")
            dropped = True
        if "reasoning_effort" in body and "reasoning_effort" in msg:
            body.pop("reasoning_effort")
            dropped = True
        # max_tokens 超了服务商上限也一样：降回单题那个保守值再试一次，
        # 总比整页直接失败强（截断有抢救逻辑兜着）。
        if "max_tokens" in body and "max_tokens" in msg:
            body["max_tokens"] = vision.MAX_TOKENS
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

    items = parse_page_items(text)
    raw = text[:RAW_KEEP]
    for r in items:
        r["raw"] = raw
    return items


# ---------------------------------------------------------------- 任务
def create_page_job(
    db: Session,
    pages: list[int],
    curriculum_id: int | None = None,
    document_id: str | None = None,
    doc_filename: str | None = None,
) -> BatchJob:
    """建一个**整页模式**任务。不预建条目 —— 条目是识别出来的。

    ⚠️ 这里与块模式最大的结构差别：**建任务时不知道会有几条条目**。
       「这一页有几道题」在识别之前没人知道。
       所以 job.total 记的是**页数**（进度以页计），
       而待审列表的条数是识别完成后才出现的。
       两者不是一回事，_job_dict 里分别给（total/done vs pending/approved）。
    """
    pages = sorted({int(p) for p in (pages or []) if int(p) > 0})
    if not pages:
        raise BatchError("没有选择要识别的页面")
    if not document_id:
        raise BatchError("整页识别需要知道页图从哪来 —— 请先选一份文档")
    if not curriculum_id:
        raise BatchError("请先选择体系 —— 知识点要挂到对应体系的知识树上")
    if len(pages) > MAX_PAGES:
        raise BatchError(
            f"一次最多 {MAX_PAGES} 页，这次选了 {len(pages)} 页。"
            "（页数 = AI 请求次数）请分批提交。"
        )

    job = BatchJob(
        id=uuid.uuid4().hex[:12],
        owner_id=config.OWNER_ID,
        status="queued",
        total=len(pages),
        ai_mode=1,
        pages=json.dumps(pages),
        curriculum_id=curriculum_id,
        document_id=document_id,
        doc_filename=doc_filename,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )
    db.add(job)
    db.commit()
    return job


def _run_one_page(pno: int, src: str, cache_dir: str, hint: str) -> tuple[int, list[dict] | None, str | None]:
    """在一个工作线程里识别一页。**只用自己新建的会话** —— 请求会话不能跨线程。"""
    db = SessionLocal()
    try:
        for attempt in range(RETRY + 1):
            try:
                from ..adapters import pdf as pdf_adapter
                # 页面图当场渲染。**有磁盘缓存**（页面视图早就渲染过同一页），
                # 第二次进来是直接命中文件，不会重复渲染。
                png = pdf_adapter.render_page(src, pno, cache_dir)
                return pno, recognize_page(db, png, hint), None
            except Exception as e:                       # noqa: BLE001 什么都得接住
                if attempt >= RETRY:
                    return pno, None, str(e)[:500]
    finally:
        db.close()
    return pno, None, "未知错误"


def _write_page_items(job_id: str, doc_id: str, pno: int, items: list[dict],
                      src: str | None = None) -> int:
    """把一页识别出的题落成待审条目。返回写入条数。

    seq 用 `pno * 1000 + 页内序号`：页是**并行**完成的，完成顺序不等于页序，
    直接自增会让列表顺序随机。给每页一段独立的编号空间，
    排序天然正确；最后 _renumber() 再收口成 1..N。

    `src` 是页面图的数据源（原件 / Word 转出的 PDF）。给了它才做**配图自动裁切** ——
    裁图要从原页面上按坐标裁，没有源文件就无从裁起（测试里就常不传）。
    """
    db = SessionLocal()
    try:
        job = db.get(BatchJob, job_id)
        if job is None:
            return 0
        cur_id = job.curriculum_id
        doc_name = job.doc_filename
        now = datetime.now().isoformat(timespec="seconds")

        # 几何信息**每页只取一次**（一页可能好几道题，都要比同一批候选区）。
        W = H = 0.0
        cands: list[dict] = []
        if src:
            try:
                from ..adapters import pdf as pdf_adapter
                W, H = pdf_adapter.page_size(src, pno)
                cands = pdf_adapter.figure_candidates(src, pno)
            except Exception:                       # noqa: BLE001
                # 取不到几何信息（页渲染源坏了、页码越界）**不该拖垮这一页的条目** ——
                # 那会让老师看到「这页整页消失」。退化成「不做自动配图」。
                W = H = 0.0
                cands = []

        # 配图决定（要不要图 / 框在哪 / 哪一处归谁）全在 plan_page_figures 里 ——
        # 纯几何、与 AI 无关，所以离线重放脚本也能直接调它，不会各抄一份。
        # ⚠️ 实测踩过两次：①模型说 4 道题有图却没给框，兜底把同一幅图贴给 4 道题；
        #    ②p6 只有一道题要图（allow_only 因此为真），可裁图是**逐条**跑的，
        #      于是 `needs_figure=false` 的题也进兜底、一起拿走那处图形。
        decisions = (plan_page_figures(items, cands, W, H) if (src and W)
                     else [{"rect": None, "box": None, "mode": "", "dup": False,
                            "shared": False} for _ in items])

        # ---- 兜底：配图定位不到的题 → 整题原貌图（见 plan_whole_regions）----
        # 只在真有这种题时才去解析文字层：多数页一条都不缺，没必要白读一遍。
        whole: list = [None] * len(items)
        need_whole = [k for k, d in enumerate(decisions)
                      if d["rect"] is None
                      and (items[k].get("needs_figure") or items[k].get("figure_box"))]
        if src and W and need_whole:
            try:
                from ..adapters import pdf as pdf_adapter
                starts = pdf_adapter.question_starts(
                    src, pno, [items[k].get("content") or "" for k in range(len(items))])
                # 下边界不能只靠「下一题首行」：页内最后一题后面跟着答案/详解/
                # 知识点框，一路裁到页底就把答案印进卷子了。页脚页码同理。
                stops, bottom = pdf_adapter.page_marks(src, pno)
                whole = plan_whole_regions(starts, W, H, stops=stops, bottom=bottom)
            except Exception:                        # noqa: BLE001
                # 文字层读不到（扫描版 / 文件损坏）→ 不兜底，退回「等老师框」
                whole = [None] * len(items)

        for idx, r in enumerate(items):
            flags = list(r["flags"])
            fig_url = None
            fig_box = None
            d = decisions[idx]
            if d["dup"]:
                # 那处图形被同页更可信的一道题占了 —— **不能悄悄留空**，
                # 否则老师以为这题压根不用配图（见图要自己框）。
                flags.append("figure_dup_candidate")
            elif d["rect"] is not None:
                try:
                    fig_url, fig_box = crop_figure_for_item(
                        src, pno, d["rect"], d["mode"], W, H, flags)
                    if d.get("shared"):
                        # 同页还有别的题也指着这处图形，赢家赢得不干脆 → 让老师核一眼
                        flags.append("figure_shared_candidate")
                except Exception:                   # noqa: BLE001
                    # 裁不出来（区域退化、磁盘写不了）→ 留个标记，条目照常进待审。
                    # 配图是「锦上添花」，不能因为它丢掉整道题。
                    flags.append("figure_crop_failed")

            needs = 1 if r.get("needs_figure") else 0
            if fig_url:
                # 裁出来了 → 这题确实有图。与 figure-crop 接口同一个口径：
                # 「框出了图」本身就是「确认有图」。
                needs = 1
            elif needs:
                # 模型说有图，却没给出可用的框 → 明确标出来，别让老师以为「不用配图」。
                flags.append("figure_box_missing")

            # 兜底：配图没定位到，就把**整道题**裁下来当原貌图 —— 图一定在里面，
            # 只是这道题出卷会改用图片形态（见 batch_import.approve_item）。
            # ⚠️ 两个前提缺一不可：①范围算出来了 ②**范围内真的有一处图形**。
            #    跨页题的图在下一页，只满足①的话裁出来是一张没图的「整题图」（见 _range_has_figure）。
            # ⚠️ 只在 fig_url 为空时才做：配图已经拿到了就没必要再存一张大图。
            whole_url = ""
            if not fig_url and whole[idx] is not None:
                if _range_has_figure(whole[idx], cands):
                    whole_url = crop_whole_question(src, pno, whole[idx], flags)
                elif whole[idx][3] >= H * 0.9:
                    # 本题顶到页底、页内又没有图 → 图八成在下一页（跨页题的上半段）。
                    # 打标记让界面能说清原因，并让「选现成的图」把下一页的也列出来。
                    flags.append("figure_cross_page")

            it = BatchItem(
                id=uuid.uuid4().hex[:12],
                job_id=job_id,
                seq=pno * 1000 + idx,
                document_id=doc_id,
                doc_filename=doc_name,
                page_no=pno,
                region=None,
                # ⚠️ 整页模式一般**不裁原貌图**：题干文本模型直接给了，出卷走文本形态，
                #    原貌图没有下游用途，裁了只会往 crops/ 里攒没人引用的图。
                #    唯一的例外是「配图定位不到」的兜底（见上面的 whole_url）——
                #    那种题没有这张图就会丢图，非裁不可。
                image=whole_url,
                status="pending",
                content=r["content"],
                # 答案 / 解析：模型本页找不到就是空串（题目卷、解析在下一页）。
                # 空串**不是失败**，所以不打 flag。
                answer=r.get("answer") or "",
                analysis=r.get("analysis") or "",
                qtype=r["qtype"],
                difficulty=r["difficulty"],
                knowledge_point=r["knowledge_point"] or None,
                tags=json.dumps(r["tags"], ensure_ascii=False),
                confidence=r["confidence"],
                note=r["note"] or None,
                needs_figure=needs,
                figure_note=r.get("figure_note") or None,
                figure_image=fig_url or "",
                figure_box=json.dumps(fig_box) if fig_box else None,
                raw=r.get("raw"),
                flags=json.dumps(flags, ensure_ascii=False),
                created_at=now,
            )
            # 知识点挂树 —— 与块模式同一套：**匹配不上不是错误**
            # （四个体系里只有一个有知识树，另外三个必然匹配不上）。
            if it.knowledge_point:
                kps, node_id, _cur = knowledge.resolve_kp(
                    db, [it.knowledge_point], cur_id, fuzzy=True
                )
                if node_id:
                    it.node_id = node_id
                    it.knowledge_point = kps[0] if kps else it.knowledge_point
            db.add(it)
        db.commit()
        return len(items)
    finally:
        db.close()


def _write_page_error(job_id: str, doc_id: str, pno: int, err: str) -> None:
    """一页识别失败时，**留一条看得见的痕迹**。

    为什么不静默跳过：失败页不产出任何条目的话，老师在待审列表里看到的就是
    「这页凭空不见了」。整页模式下一个任务动辄十几页，少一页很难被发现 ——
    而当失败的那页恰好是解答题大题的所在页时，代价是整道大题没进题库。
    留一条标了 `ai_failed` 的占位项，老师要么照着原卷补写、要么驳回，
    至少**知道这页出过问题**（驳回也留着痕迹，比"从来没有过"强）。
    """
    db = SessionLocal()
    try:
        job = db.get(BatchJob, job_id)
        if job is None:
            return
        db.add(BatchItem(
            id=uuid.uuid4().hex[:12],
            job_id=job_id,
            seq=pno * 1000,
            document_id=doc_id,
            doc_filename=job.doc_filename,
            page_no=pno,
            image="",
            status="pending",
            content="",
            qtype=taxonomy.DEFAULT_QTYPE,
            difficulty=taxonomy.DEFAULT_DIFFICULTY,
            confidence=taxonomy.DEFAULT_CONFIDENCE,
            tags="[]",
            flags=json.dumps(["ai_failed"], ensure_ascii=False),
            note=f"第 {pno} 页识别失败",
            error=err,
            needs_figure=0,
            created_at=datetime.now().isoformat(timespec="seconds"),
        ))
        db.commit()
    finally:
        db.close()


def _renumber(job_id: str) -> None:
    """把条目的 seq 收口成 1..N（按「页号 → 页内顺序」）。

    为什么不直接用 pno*1000+idx：那个编号只保证**有序**，
    读出来是一串 1000/1001/2000 这样跳跃的数。
    最后统一收一次口，代价是一条 ORDER BY + 若干 UPDATE。
    """
    db = SessionLocal()
    try:
        rows = db.scalars(
            select(BatchItem).where(BatchItem.job_id == job_id).order_by(BatchItem.seq)
        ).all()
        for i, it in enumerate(rows, start=1):
            it.seq = i
        db.commit()
    finally:
        db.close()


def run_page_job(job_id: str) -> None:
    """后台线程入口：**逐页并行**识别，由本线程串行写库。

    与块模式的 run_job 三处不同：
      ① 单位是**页**不是块 —— 页图当场渲染（命中页面视图的磁盘缓存）
      ② 一页回来是**多道题** → 一页写 N 条条目
      ③ 条目**识别时才创建** —— 「这页有几道题」之前不知道

    「识别并行、写库串行」的理由与块模式完全一样（SQLite 单写者），
    这里只是把「一张块图」换成「一页图」。
    """
    db = SessionLocal()
    try:
        job = db.get(BatchJob, job_id)
        if job is None:
            return
        if job.ai_mode != 1:
            _finalize(job_id, 0, 0, "这个任务不是 AI 整页模式")
            return
        job.status = "running"
        db.commit()
        pages = json.loads(job.pages or "[]")
        doc_id = job.document_id or ""
        # 页面来源（.pdf 直接用原件 / .docx 先导出 PDF）只有 documents.py 判过一份，
        # 延迟导入避免 services ↔ routers 的循环引用。
        from ..routers.documents import _page_source
        try:
            src = str(_page_source(db, doc_id))
        except Exception as e:                           # noqa: BLE001
            _finalize(job_id, 0, 0, f"读不到文档页面：{e}")
            return
        cache_dir = str(config.PAGES_CACHE / doc_id)
    finally:
        db.close()

    done = failed = 0
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_run_one_page, int(pno), src, cache_dir, ""): int(pno)
                for pno in pages
            }
            for fut in as_completed(futures):
                pno = futures[fut]
                try:
                    _pno, items, err = fut.result()
                except Exception as e:                   # noqa: BLE001
                    _pno, items, err = pno, None, str(e)[:500]
                if items is None:
                    _write_page_error(job_id, doc_id, pno, err or "识别失败")
                    failed += 1
                else:
                    # src 传下去：配图自动裁切要从原页面上按坐标裁
                    _write_page_items(job_id, doc_id, pno, items, src=src)
                    done += 1
                _bump(job_id, done, failed)
    except Exception as e:                               # noqa: BLE001
        _renumber(job_id)
        _finalize(job_id, done, failed, f"任务异常终止：{e}")
        return
    _renumber(job_id)
    _finalize(job_id, done, failed, None)


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
    """收尾。**与块模式同一套状态机**（done / partial / failed），前端不用分叉。

    一个整页模式特有的情形：某页识别出**0 道题**（空白页 / 纯页眉页）。
    它算 done（模型确实读完了这一页），只是没产出条目 ——
    done 是「页数」而不是「条目数」，两者本来就不是一回事。
    """
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
            job.status = "partial"
        db.commit()
    finally:
        db.close()
