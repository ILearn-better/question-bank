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
    "\nquestions 里每个元素是**一道题**，字段固定为下面十个，一个都不能少：\n"
    '  "content"         字符串。这一道题的题干全文。行内公式用 $...$ 包住，'
    "独立成行的公式用 $$...$$ 包住；分数写 \\frac{}{}、根式写 \\sqrt{}、"
    "上下标记作 ^{} 与 _{}，一律用标准 LaTeX 命令。"
    "选择题的选项（(A)(B)(C)(D) 或 A. B. C. D.）要保留，选项里的公式同样转成 LaTeX。\n"
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
    "\n五条硬要求：\n"
    "1. **必须切分**。这一页有几道题，questions 里就放几个元素。"
    "不要把两道题合并成一条，也不要漏掉任何一道。\n"
    "2. **不属于题目的东西一律不要**。页眉、页脚、页码、条形码、"
    "「四、解答题；本题共 5 小题，共 77 分」这种**只有分区说明、没有题干**的行、"
    "「请在此作答」这类提示，都**不要**成为 questions 的元素。"
    "（这一条最容易被忽略 —— 把分区说明录成一道题，老师还得手动驳回。）\n"
    "3. qtype 与 difficulty **只能**从上面给出的取值里原样照抄，不要写同义词"
    "（不要写「解答」「证明」「中等」「简单」「较难」）。\n"
    "4. 十个字段**必须全部出现**。取不到值时用空字符串 \"\"、空数组 [] 或 false，"
    "不要省略字段、不要写 null。\n"
    "5. **跨页的题照常输出**：某道题在图的最下边缘被截断（明显未完）时，"
    "仍然输出它，并在 note 里写「疑似在本页底部被截断」。"
    "它后面那半页在下一页，会由下一页的识别另行给出 —— 你不要去猜下半页的内容。\n"
    "\n输出示例（仅示意格式与字段，内容以图为准）：\n"
    '{"questions":['
    '{"content":"已知函数 $f(x)=x^2+2x-3$，求 $f(x)$ 的零点。","qtype":"解答题",'
    '"difficulty":"基础","knowledge_point":"函数的零点","tags":["一元二次方程"],'
    '"confidence":"high","needs_figure":false,"figure_note":"","figure_box":[],"note":""},'
    '{"content":"如图，在 $\\triangle ABC$ 中，$AB=AC$，求 $\\angle B$。","qtype":"解答题",'
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


def crop_figure_for_item(src: str, pno: int, box_norm, cands, W, H, flags: list[str],
                         allow_only: bool = False):
    """裁出配图。返回 (url | None, 实际用的归一化框 | None)，并把方式记进 flags。

    落盘走 `pdf.crop_region` → `data/uploads/crops/`（**资产目录**：
    每张图都有数据库行引用它，删的时候靠引用计数回收，见 services/crops.py）。
    这张图被写进了 `batch_items.figure_image`，所以它是有主的，不是垃圾。
    """
    rect, mode = resolve_figure_box(box_norm, cands, W, H, allow_only=allow_only)
    if rect is None:
        return None, None
    from ..adapters import pdf as pdf_adapter
    name = pdf_adapter.crop_region(src, pno, list(rect), str(config.CROPS_DIR))
    flags.append({
        "snapped": "figure_snapped",
        "snapped_only": "figure_only_candidate",
    }.get(mode, "figure_rough"))
    used = [int(round(rect[0] / W * 1000)), int(round(rect[1] / H * 1000)),
            int(round(rect[2] / W * 1000)), int(round(rect[3] / H * 1000))]
    return f"/api/crops/{name}", used


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

        # 「整页只有一处图形」这条兜底**只能发给一道题** ——
        # 它的依据是页级的（这一页有几道题在要图），所以必须在页级算。
        # ⚠️ 实测踩过：模型说 4 道题有图却没给框，兜底把**同一幅图**贴给了这 4 道题 ——
        #    待审列表里 4 张一模一样的图挂在 4 道不相干的题上，比不配图还糟。
        unboxed = sum(1 for r in items
                      if r.get("needs_figure") and not r.get("figure_box"))
        allow_only = (unboxed == 1 and len(cands) == 1)

        for idx, r in enumerate(items):
            flags = list(r["flags"])
            fig_url = None
            fig_box = None
            if src and W:
                try:
                    fig_url, fig_box = crop_figure_for_item(
                        src, pno, r.get("figure_box"), cands, W, H, flags,
                        allow_only=allow_only)
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

            it = BatchItem(
                id=uuid.uuid4().hex[:12],
                job_id=job_id,
                seq=pno * 1000 + idx,
                document_id=doc_id,
                doc_filename=doc_name,
                page_no=pno,
                region=None,
                # ⚠️ 整页模式**不裁原貌图**：题干文本模型直接给了，出卷走文本形态，
                #    原貌图没有下游用途，裁了只会往 crops/ 里攒没人引用的图。
                image="",
                status="pending",
                content=r["content"],
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
