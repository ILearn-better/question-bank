# -*- coding: utf-8 -*-
"""AI 批改：**一页**学生作业图进去，逐题「转录 + 判定」的结构化结果出来。

和 batch_page（整页录题）的关系：
    同一个调用层（`ai_polish.post_json` / `chat_url` / `extract_content`）、
    同一个模型、同一套「三级降级重试」与「截断抢救」。
    差别在**任务本身**：录题要把题切成记录，批改要「先把学生写了什么逐字录下来，
    再判对错」。两份提示词的角色设定几乎是相反的 ——
    录题明确要求忽略手写笔迹，批改必须一字不漏地读手写。
    放在同一个文件里，改哪一份都得跨过另一份几百行，还极容易「顺手统一措辞」
    把「禁止修正学生写法」这条弄丢。所以另开一个文件。

为什么这个文件里没有 sqlalchemy / 没有 db 参数：
    本模块是 AI 核心层，对外只暴露纯函数。上层的 router 与任务线程负责开会话、
    读文件、写库、并发 —— 这样这一层可以**脱机单测**（喂固定 JSON 给
    `parse_grade_items`），也不会把「SQLite 单写者」那套坑带进 HTTP 调用线程。
    · 唯一的例外是 `_load_cfg` 里「读一次设置」：对外签名
      `recognize_page(png_path, ctx)` 里没有 db（契约由上层定的，不能改），
      而配置只存在设置表里 —— 只能临时开一个**只读**会话读出来，读完立刻关，
      不写任何东西。想彻底脱库的调用方传 `ctx["cfg"] = {...}` 即可。

⚠️ 三条判定原则（比任何「提高准确率」的技巧都值钱，改提示词时先读）

  1. **先转录、后判定，顺序不能颠倒。**
     边读边判会漏读中间步骤：一旦模型认定「这题他错了」，后面几行会被下意识跳过，
     而错因往往正好在那几行里。

  2. **逐字转录，禁止按上下文「修正」学生的写法。**
     学生把 3 写成 8 就是 8。模型替他改对之后，错误就从「可检索」变成「隐形」——
     这题会被判成对的，老师永远不知道他不会。这一条一旦松掉，
     整套批改的价值就从「找出不会的地方」退化成「给个大概的对错」。

  3. **判错必须给出学生原文（`evidence_quote`），而且由本文件自己校验。**
     光在提示词里写「必须引用原文」是没有用的 —— 模型编一条
     「由①②得 x=2」成本极低，而老师不会真去核。只有字符串匹配能拦住编造。
     校验不过 → 该题降级 `ungradable`，绝不凭一条编造的引用判学生错。

⚠️ 一期**只判对错、不给分**：`score` / `full_score` 一律 None。
   「AI 判这道题对不对」有标准答案可核对，而「给这个孩子打 3 分」是另一回事。
   模型自己输出 score 时也有意忽略（并打 `score_ignored`）。

items 里 `flags` 的取值（系统会给的那几个；模型自己给的标记也会并进来）：
    json_repaired        尾随逗号修复过，JSON 整体仍可用
    json_salvaged        输出被截断/夹了废话，是从里面逐题抢救出来的
    unreadable_char      题内有看不清的字符（模型标，显示成 ⍰）
    insufficient_evidence 判错但依据在原文里找不到 → **已降级 ungradable**
    reference_unverified 参考解是模型自己做的（answer_mode="ai"），没经老师核对
    unstable             两次采样 verdict 在 correct ↔ wrong 之间翻转
    transcription_conflict 两次采样转录不一致（二期：两遍都跑时才可能出）
    qtype_unknown        题型不在 taxonomy.QTYPES 里 → qtype 给 None
    verdict_invalid      verdict 不是枚举值 → 已回落 ungradable
    score_ignored        模型自己给了分数（一期不收）
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import unicodedata
from pathlib import Path

from PIL import Image

from .. import taxonomy
from . import ai_polish
from .images import MAX_IMAGE_BYTES

# ---------------------------------------------------------------- 常量
# 送模型前的分辨率。**刻意不复用 vision.MAX_EDGE（1600）**：
#   1600 是「印刷体整页 + 复杂公式」上量出来的基线，那块内容冗余度高，错了能靠上下文纠。
#   批改读的是**手写**：低冗余，一个 3 看成 8 整题就废了。
#   ⚠️ 不要去改 vision.py 里的 MAX_EDGE —— 那是公式识别的实测基线，
#      改它等于把别人的回归基线一起挪了。批改要更高分辨率就在本文件里另定。
GRADE_MAX_EDGE = 2200

# 一次最多附几张「老师给的答案 / 细则」图。2 张是成本与够用的折中：
# 一张答案 + 一张细则已经是常见上限，再多就是在为「拍糊了重拍一张」付费。
_MAX_ANSWER_IMAGES = 2

# JPEG 质量。同样因为手写：细笔画在 q85 上会被振铃吃掉，q90 的码率代价很小。
GRADE_JPEG_QUALITY = 90

# 输出预算。一页可能七八道题、每题都要转录 + 判定 + 引用原文，JSON 很长。
# ⚠️ 下限 2000 是硬约束（vision.py 头部实测）：低于它推理会把预算吃满，
#    接口回 200、正文是空字符串、一个错都不报 —— 这类失败最难查。
GRADE_MAX_TOKENS = max(2000, int(os.getenv("SHIKE_GRADE_MAX_TOKENS", "8000")))

# 截断重试时的天花板。翻倍就够（推理预算是浮动的，同一张图第二次常常刚好够），
# 但别无限翻 —— 有些服务商的 max_tokens 上限就在 8k/16k，超了直接 400。
GRADE_TOKEN_CEILING = 16000

# 一页批改允许等多久。比识别慢（要吐长 JSON），但超过 4 分钟基本是服务端卡住了，
# 这时候重试一次比继续等更划算。
GRADE_TIMEOUT_MAX = 240
GRADE_TIMEOUT_MIN = 60

# 单页最多认几道题。防的是模型把一行字拆成一道题的失控输出（JSON 里能塞几百条），
# 正常一页 10~40 题，够用。
MAX_ITEMS_PER_PAGE = 60

# 文本长度上限。只为挡住「模型陷入重复循环」那种离谱输出（会把库和前端撑爆），
# 正常题干/作答远小于这些值。
_STEM_LIMIT = 4000
_ANSWER_LIMIT = 4000
_BASIS_LIMIT = 600
_REASON_LIMIT = 300
# 图上要点可能是一整张数表（直方图十几个区间），比 basis 需要更长。
_FIGURE_LIMIT = 1200
_KP_LIMIT = 30
# 尾部情况是一句话（「底部约 1/10 是页码与页脚；这道题疑似还没写完」），
# 比 error_reason 短。给到 200 已经够宽松，再长就说明模型在写小作文。
_TAIL_NOTE_LIMIT = 200

# ---------------------------------------------------------------- 分类标签（0031）
# 用户 2026-10-20 点名要的：「ai 输出中要附带对这道题的标签（学段 / 知识点 /
# 体系 / 考法等）」。这四个是**一组**，界面上并排显示，所以打包成一个对象。
#
# ⚠️ 只有 stage 是枚举（学段是客观事实，枚举能让它可筛）；system / exam 刻意留成
#    自由短词 —— 「体系」和「考法」的叫法随教材变（人教 / 北师大 / DSE / IGCSE
#    各有一套），硬编枚举会让模型在拿不准时瞎选一个，那还不如让它写原话。
LABEL_KEYS: tuple[str, ...] = ("stage", "system", "kp", "exam")
# 学段。**认不出就留空**，不猜 —— 猜错了会进筛选条件，比空着有害得多。
LABEL_STAGES: tuple[str, ...] = ("小学", "初中", "高中", "大学", "其他")
# 每个值的长度上限。这些是要**并排显示在题卡上一行**的，长了会把卡片撑变形。
_LABEL_VALUE_LIMIT = 24
_LABEL_COUNT_LIMIT = 8

_VERDICTS = ("correct", "partial", "wrong", "blank", "ungradable")

# verdict 的常见别写。模型对枚举极不老实：要 "correct" 它给 "正确"/"√"/"right"。
# 认不出来的一律回落 ungradable（**不是** wrong）—— 见下面的原则 3。
_VERDICT_WORDS: dict[str, tuple[str, ...]] = {
    "correct": ("correct", "right", "true", "ok", "对", "正确", "全对", "√", "✓", "✔"),
    "partial": ("partial", "partly", "部分正确", "部分对", "部分", "半对"),
    "wrong": ("wrong", "incorrect", "false", "错", "错误", "不对", "全错", "×", "✗", "✘"),
    "blank": ("blank", "empty", "未作答", "没作答", "空", "空白", "未写", "没写", "无"),
    "ungradable": ("ungradable", "unable", "unknown", "无法判断", "判不了", "无法批改", "n/a", "na"),
}
_VERDICT_LOOKUP = {w: v for v, words in _VERDICT_WORDS.items() for w in words}

# 字段别名。模型偶尔改键名（录题那边也踩过，见 batch_page.parse_page_items
# 对 "items"/"data"/"list" 的容忍）。只容忍最可能出现的几种，不做万能匹配 ——
# 别名越多，「读错字段」的概率也越大。
_ALIAS: dict[str, tuple[str, ...]] = {
    "q_no": ("q_no", "qno", "no", "number", "question_no", "num"),
    "qtype": ("qtype", "type", "question_type"),
    "stem": ("stem", "content", "question_text", "question"),
    "student_answer": ("student_answer", "student", "answer", "student_work"),
    "correct_answer": ("correct_answer", "reference_answer", "reference", "right_answer"),
    "verdict": ("verdict", "result", "judgement", "judgment"),
    "basis": ("basis", "reason", "explain", "explanation"),
    "evidence_quote": ("evidence_quote", "evidence", "quote"),
    "confidence": ("confidence", "conf"),
    "kp_name": ("kp_name", "knowledge_point", "kp"),
    "error_reason": ("error_reason", "error", "mistake"),
    "figure_note": ("figure_note", "figure", "diagram_note"),
    "flags": ("flags", "flag"),
}


class _GradeError(RuntimeError):
    """内部用：消息已经是给老师看的人话。recognize_page 会把它变成 err 返回。"""


# ---------------------------------------------------------------- 提示词
# ⚠️ 下面是**普通字符串**，不是 r-string —— LaTeX 的反斜杠必须写双份（`\\frac`）。
#    写单份会被 Python 吃掉（`\f` → 换页符、`\t` → 制表符），提示词送到模型手里
#    已经面目全非，而模型照样有输出、只是公式规范变差 —— 典型的「不报错但不对」。
#    这个坑 vision.py / batch_import.py / batch_page.py 头部都记着，改完务必跑一遍离线解析测试。
_ENUM_QTYPE = "、".join(taxonomy.QTYPES)
_ENUM_CONFIDENCE = "、".join(taxonomy.CONFIDENCES)

_ROLE = (
    "你是一位经验丰富的数学老师，正在批改学生交上来的**一页**作业（照片或扫描件）。\n"
    "你的工作分两步，**顺序不能颠倒**：\n"
    "  第一步 转录：把卷面上每一道题的题干、以及学生在这一题下面写的**每一个字符**照原样录下来；\n"
    "  第二步 判定：转录**全部做完之后**，再逐题判断学生写得对不对。\n"
    "为什么必须分两步：边读边判会漏读中间步骤 —— 一旦你先认定「这题他错了」，"
    "后面那几行会被你下意识跳过，而错因往往正好在那几行里。"
)

_TRANSCRIBE_RULES = (
    "\n\n## 转录规则（第一步）\n"
    "1. **逐字转录，不补全、不猜测、不美化**。学生写成什么样就录成什么样，"
    "包括写错的字、写了一半的步骤、划掉的内容。\n"
    "2. 看不清的字符输出一个 `⍰`，**不要用上下文去猜它本来是什么**。"
    "题里有 `⍰` 时，flags 里必须带上 \"unreadable_char\"。\n"
    "3. **禁止根据上下文「修正」学生的写法。** 这一条比任何「提高准确率」的技巧都值钱："
    "学生把 3 写成 8 就是 8，把负号漏了就是漏了 —— 你替他改对，"
    "这道题就会被判成对的，老师永远不知道他不会。\n"
    "   · 数学上明显不成立的写法**照录**（例如学生写 $x^2=-1$，"
    "$\\Rightarrow x=\\pm 1$，就照他写的录，不要改成「无实根」）；\n"
    "   · 字迹潦草但看着像某个常见写法 → 照你看到的写，不要写成「正确的那一个」；\n"
    "   · 学生没写的东西不要替他补（不要补「解：」「由题意得」这类套话）；\n"
    "   · 划掉的内容保留并注明（如「（划掉：$x=3$）」），不要当成没写。\n"
    "4. 公式写成标准 LaTeX，用 $...$ 包住；分数写 \\frac{}{}、根式写 \\sqrt{}、"
    "上下标记作 ^{} 与 _{}、角写 \\angle、三角形写 \\triangle、度数写 ^{\\circ}。\n"
    "5. 多行步骤用换行分隔，**保留原有的行序与行数** —— 解题过程一行一步，"
    "挤成一行就看不出是在哪一步出的错了。\n"
    "6. 选择题：只记录学生**勾 / 圈 / 涂 / 写了哪个选项字母**（写进 student_answer，例如 \"B\"）。"
    "看不清勾了哪个 → verdict 给 ungradable，**不要靠题目内容去猜他可能选哪个**。\n"
    "7. 几何图（**这一条 2026-10 改过，别按老口径判**）：\n"
    "   · figure_note 里写**结构化的**图信息，不要只写「画了一个三角形」。尽量交代四样：\n"
    "     ① 图上出现的点与字母（含学生新命名的）；\n"
    "     ② 连线 —— 尤其是学生**自己加的辅助线**，要写成可核对的形式"
    "（「过 $D$ 作 $DP\\perp BP$，垂足 $P$」），不要只写「加了一条线」；\n"
    "     ③ 标出的角、直角符号、等长记号、平行记号、圆弧记号；\n"
    "     ④ 图上写出来的数值与单位。\n"
    "   · 每一句后面标把握：`（确定）` 或 `（看不清）`。**看不清的不许猜** —— "
    "猜一个不存在的记号，比说「看不清」危害大得多。\n"
    "   · 一句一件事，用「；」隔开，方便后面逐句引用。\n"
    "   · 图上一个记号都没有（纯示意图）就照实写「除字母外无任何标记」，"
    "**不要**把「看起来像直角」写成直角。\n"
    "8. 统计图表 / 数据表（频率分布直方图、茎叶图、箱线图、散点图、折线图、"
    "列联表、分布列…）：\n"
    "   · **先把图上的数读成一张表，再算**。绝不用「柱子大概这么高」代替具体数值 —— "
    "「大概」在求频率、求距离、求四分位时会直接算错，而且错得很像对的。\n"
    "   · 表写进 figure_note：直方图写「区间:频数」，茎叶图写「茎:叶 叶 叶」，"
    "箱线图写「最小/下四分位/中位/上四分位/最大」，并注明轴名、单位、刻度间隔。\n"
    "   · 图上**没有印数字**（纯图形）时必须写明「刻度需按格数推算」，"
    "并写出你数到的格数与换算过程，不要直接抛一个凭感觉的数。\n"
    "   · 读不出来的格子写「读不清」并标 `（看不清）`，**不要用题型常识补一个看着合理的数**。\n"
)

_JUDGE_RULES = (
    "\n\n## 判定规则（第二步）\n"
    "1. 只看**学生实际写出来的东西**，不要替他补步骤、不要顺着他的思路替他找补。\n"
    "2. 写法不同但数学上等价 → correct。不要因为格式、顺序、记号不同就判错。\n"
    "3. 判 wrong / partial 时，basis 必须指出**错在哪一步**"
    "（例如「第 2 步两边同时除以 $x$ 时没有讨论 $x=0$」），"
    "不要写「过程有误」「答案不对」这种没法核对的话。\n"
    "4. 判 wrong / partial 时，evidence_quote 必须给出**对应的那段学生原文**"
    "（从 student_answer 里原样复制一行，不要改写、不要加解释）。"
    "系统会拿它做字符串校验，对不上这道题会被判成「判不了」。\n"
    "5. **判不了就写 ungradable**：字迹读不清、图题看不出画了什么、题干残缺、"
    "或者依据本身缺失。**不要**把它落成 wrong —— 冤枉一个学生，"
    "比这题暂时没批完糟糕得多。\n"
    "6. 学生整题空白（没写任何字、也没勾选）→ verdict 给 blank，不要给 wrong。\n"
    "7. **图上画的可以作数，但要按把握分档**（这是唯一允许拿图当依据的地方）。\n"
    "   老口径是「图形描述一律不得作为判错依据」，那条已经在 2026-10 废止 —— "
    "它把「过 $D$ 作 $DP\\perp BP$」这类**作图得分点**整片判死了，几何题因此长期漏判。\n"
    "   · figure_note 里标了 `（确定）`、且与题干不矛盾的图形信息 → **可以**作为判定依据。"
    "例如题干要求作 $DP\\perp BP$，figure_note 写「$DP\\perp BP$，有直角符号（确定）」，"
    "这一得分点就算写到了。\n"
    "   · 标了 `（看不清）` 的 → 只能作**疑似**：verdict 照常给，但 confidence 降一档，"
    "并在 basis 里写明「图形此处未看清」。\n"
    "   · 图上根本没看见的 → **不等于是「学生没写」**。缺这一项证据时按 ungradable 处理，"
    "不要判 wrong —— 你没看见不等于他没画。\n"
    "   · 仍然**不因画得丑、比例不对、线条歪而扣分**。判的是「这条线在不在、"
    "是不是垂直/平行/过某点」，不是画得好不好。\n"
    "   → 一句话：**看见的能作数，看不清的只作疑，没看见的不作否**。\n"
)

_FIELD_RULES = (
    "\n\n## 输出格式\n"
    '只输出**一个 JSON 对象**：{"questions": [ {...}, {...} ]} —— '
    "不要解释、不要客套、不要 Markdown 代码围栏、不要 JSON 之外的任何文字。\n"
    "这一页有几道题就输出几个元素，一道都不能漏；这一页确实没有题目就输出 "
    '{"questions": []}。页眉、页脚、页码、条形码、「请在此作答」这类说明'
    "**不是题目**，不要变成 questions 的元素。\n"
    "\n每个元素的字段固定为下面这些，一个都不能少、也不能改名"
    "（取不到值用空字符串 \"\" / 空数组 []，不要省略字段、不要写 null）：\n"
    # `block_kind` / `tail_note` 只在区域模式要求（见 _REGION_BLOCK）——
    # 整页模式里"这块是什么"没有意义（整页本来就有很多块），加了只是多两个字段要填。
    '  "q_no"            字符串。这一题在**卷面上印的题号**（如 "3"、"12"、"20(2)"）。'
    "不要自己重新编号，不要写成「第 3 题」。读不出来给空字符串。\n"
    '  "qtype"           字符串。**必须**是下面之一：' + _ENUM_QTYPE + "。"
    "拿不准就按题干形式选最接近的一个，不要自造「解答」「计算题」这类词。\n"
    '  "stem"            字符串。题干全文（含选项，选项里的公式同样转成 LaTeX）。'
    "读不出来就留空字符串。\n"
    '  "student_answer"  字符串。学生在这一题下写的**全部内容**，按上面的转录规则逐字转录。'
    "完全空白（没写任何字、也没有勾选）就给空字符串。\n"
    '  "correct_answer"  字符串。正确的答案 / 参考步骤（来源见下面「本次的依据」）。'
    "解答题写完整的解题步骤，一步一行。\n"
    '  "verdict"         字符串。**必须**是下面之一：correct、partial、wrong、blank、ungradable。\n'
    "      · correct     答对了（与参考等价即可，写法不同不影响）\n"
    "      · partial     思路对、做对了一部分，但有关键步骤错或漏\n"
    "      · wrong       结论错，或关键推理不成立\n"
    "      · blank       学生这道题完全没作答（卷面空白）\n"
    "      · ungradable  判不了（字迹不清 / 图题 / 题干残缺 / 依据缺失）\n"
    '  "basis"           字符串。判定依据，一句话。wrong / partial 必写「错在哪一步」；'
    "correct 可以写一句你是怎么核对的；blank / ungradable 写一句原因。\n"
    '  "evidence_quote"  字符串。支撑上面那句依据的**学生原文片段**'
    "（从 student_answer 里原样复制，可以只取一行）。"
    "wrong / partial 必给；correct / blank / ungradable 给空字符串。\n"
    '  "confidence"      字符串。**必须**是 ' + _ENUM_CONFIDENCE + " 之一 —— "
    "你对这次判定（尤其是转录）的把握。字迹清楚、对比明确给 high；"
    "有任何一处看不清就给 low。\n"
    '  "kp_name"         字符串。这道题考的**最主要那一个**考点，中文短词，'
    "10 个汉字以内（例如「一元二次方程」「线面垂直的判定」）。判断不了给空字符串。\n"
    '  "error_reason"    字符串。错因，人话，10~30 字（例如「漏了根的取舍」「移项没变号」）。'
    "判 correct 或 blank 时给空字符串。\n"
    '  "labels"          对象。**这一题的分类标签**（学段 / 体系 / 知识点 / 考法），'
    "四个键，都拿不准就给空字符串：\n"
    "                      · `stage`  学段。**必须**是下面之一："
    + "、".join(LABEL_STAGES) + "。"
    "拿不准就**留空字符串**，不要猜 —— 猜错了会进筛选条件，比空着有害得多。\n"
    "                      · `system` 体系 / 知识领域，中文短词，12 字以内"
    "（例如「数与代数」「图形与几何」「统计与概率」「综合与实践」「香港 DSE 数学」）。"
    "按**这份卷子所属的课程体系**写，不要自造。\n"
    "                      · `kp`     知识点，中文短词，12 字以内。"
    "**与上面的 `kp_name` 写同一个说法** —— 两者会被当成同一个东西，"
    "写两个说法界面上就会出现自相矛盾的两处。\n"
    "                      · `exam`   考法 / 解题路子，中文短词，12 字以内"
    "（例如「直接计算」「公式变形」「数形结合」「分类讨论」「方程建模」"
    "「逻辑推理」「空间想象」「数据分析」）。\n"
    "                    例：\"labels\": {\"stage\": \"初中\", \"system\": \"数与代数\", "
    "\"kp\": \"一元二次方程\", \"exam\": \"公式变形\"}\n"
    '  "figure_note"     字符串。**这一题图上的关键信息**（不是「有没有图」的开关）：\n'
    "                       · 几何图 → 点与字母；连的线（**学生后加的辅助线要写成可核对的形式**，"
    "如「过 $D$ 作 $DP\\perp BP$」）；角/直角/等长/平行/圆弧记号；图上写的数值。"
    "**每个分句后用 `（确定）` 或 `（看不清）` 标把握**，用「；」分隔。\n"
    "                       · 统计图表 / 数据表 → 读出来的数表（「区间:频数」「茎:叶」"
    "「五数概括」），含轴名、单位、刻度间隔。\n"
    "                       · 没有图、也不需要读图 → 空字符串。\n"

    '  "flags"           字符串数组。上面规则要求打的标记放这里（例如 ["unreadable_char"]），'
    "没有就给 []。\n"
    "\n## 三条不要\n"
    "1. **不要给分数。** 不要输出 score、得分、满分、扣几分之类的字段 —— "
    "这一期只判对错，分数由老师定。\n"
    "2. 不要输出上面没列出的字段，不要写注释、不要在 JSON 前后加任何说明。\n"
    "3. 跨页的题：本页只见到上半部分时，照实输出本页看到的内容并在 basis 里说明"
    "「疑似在本页底部被截断」，**不要去猜下一页的内容**。\n"
)

_OUTPUT_EXAMPLE = (
    "\n\n## 输出示例（只示意格式与字段，内容以图为准）\n"
    '{"questions":['
    '{"q_no":"3","qtype":"解答题",'
    '"stem":"解方程 $x^2-3x+2=0$。",'
    '"student_answer":"$(x-1)(x-2)=0$\\n所以 $x=1$ 或 $x=2$",'
    '"correct_answer":"$x^2-3x+2=(x-1)(x-2)=0$\\n所以 $x_1=1$，$x_2=2$",'
    '"verdict":"correct","basis":"因式分解正确，两个根都在",'
    '"evidence_quote":"","confidence":"high","kp_name":"一元二次方程",'
    '"error_reason":"","figure_note":"","flags":[]},'
    '{"q_no":"4","qtype":"解答题","stem":"解方程 $x^2=3x$。",'
    '"student_answer":"两边同除以 $x$，得 $x=3$",'
    '"correct_answer":"$x^2-3x=0$，$x(x-3)=0$，所以 $x_1=0$，$x_2=3$",'
    '"verdict":"wrong",'
    '"basis":"第 1 步两边同除以 $x$ 时没有讨论 $x=0$，丢了一个根",'
    '"evidence_quote":"两边同除以 $x$，得 $x=3$","confidence":"medium",'
    '"kp_name":"一元二次方程","error_reason":"除未知数时漏了 $x=0$ 的情况",'
    '"labels":{"stage":"初中","system":"数与代数","kp":"一元二次方程","exam":"公式变形"},'
    '"figure_note":"","flags":[]},'
    # 几何题的示例**必须有**：figure_note 的「分句 + 确定性标注」这个写法，
    # 只靠上面几行文字描述，模型多半会退回成「画了一个三角形」那种空话。
    '{"q_no":"5","qtype":"解答题",'
    '"stem":"如图，在 $\\triangle ABC$ 中 $AB=AC$，$D$ 是 $BC$ 的中点。求证：$AD\\perp BC$。",'
    '"student_answer":"因为 $AB=AC$，$D$ 是 $BC$ 中点，所以 $BD=CD$；\\n'
    '又 $AD=AD$，所以 $\\triangle ABD\\cong\\triangle ACD$；\\n'
    '所以 $\\angle ADB=\\angle ADC=90^{\\circ}$，即 $AD\\perp BC$",'
    '"correct_answer":"$BD=CD$、$AD=AD$、$AB=AC$ ⇒ $\\triangle ABD\\cong\\triangle ACD$（SSS）'
    '⇒ $\\angle ADB=\\angle ADC$，又两者互补 ⇒ 各 $90^{\\circ}$ ⇒ $AD\\perp BC$",'
    '"verdict":"correct","basis":"SSS 用对了，最后用补角得直角也写到了",'
    '"evidence_quote":"","confidence":"high","kp_name":"等腰三角形性质",'
    '"error_reason":"",'
    '"figure_note":"$D$ 在 $BC$ 上，$AD$ 已画出（确定）；'
    '$AB$、$AC$ 上有等长记号（确定）；$\\angle ADB$ 处有直角符号（确定）",'
    '"labels":{"stage":"初中","system":"图形与几何","kp":"等腰三角形性质",'
    '"exam":"逻辑推理"},'
    '"flags":[]}'
    "]}\n"
)

# 裁出来的一道题专用的那一段。**只在 ctx["region"] 为真时插进提示词** ——
# 整页流程看到这段会以为「一张图只能出一道题」，把整页里其余题全丢掉。
_REGION_BLOCK = (
    "\n## 这一张图是**一道题**（不是一整页）\n"
    "这张图是从一页作业上裁下来的**一道题**：题干 + 学生在它下面的作答，图题还带图。\n"
    "· **只输出一个** questions 元素。裁切边缘若带进相邻题的一两行字，**忽略它们**，"
    "不要为它另开一条。\n"
    "· `q_no` 一律给空字符串 —— 题号由系统按「这是第几个框」绑定，你不用认它。\n"
    "· `stem` 只录**这一道题**的题干，相邻题的残字不要混进来。\n"
    "· 边缘被切掉半行时，在 basis 里说明「疑似被裁切」，**不要去猜**被切掉的内容。\n"
    "· 这一块里**确实没有题目**（裁到卷头 / 页脚 / 只有「四、解答题；共 77 分」这种结构说明"
    " / 整块空白）→ **仍然输出一个元素**：`stem` 与 `student_answer` 给空字符串，"
    "`verdict` 给 `ungradable`，`basis` 写一句「这一块是卷头/页脚/结构说明，不含题目」，"
    "并把 `block_kind` 标对。**不要输出空数组** —— 空数组在界面上什么都不留，"
    "老师只会觉得「这块丢了」。\n"
    "\n### 这块图**本身**是什么、底部有没有问题（两个额外字段）\n"
    "除了上面的判定，这一张图还要回两个**结构信号**。它们是给老师看提示用的，"
    "不参与判定 —— 但少了它们，老师只能靠猜「这块为什么判不出来」。\n"
    '· `block_kind`：这张图属于什么。**必须**是下面之一：\n'
    "    · `question`  一道真正的练习题（有题干、或学生在这一块里作答）\n"
    "    · `header`    卷头：姓名 / 班级 / 学号 / 考试标题 / 「本试卷共 N 页」这类\n"
    "    · `section`   只有结构说明、本身没有题目："
    "「四、解答题；本题共 5 小题，共 77 分」这种小题号小标题\n"
    "    · `footer`    页脚：页码 / 条形码 / 「请在此作答」/ 印刷厂信息\n"
    "    · `blank`     这一块是空白（裁空了，或者学生整块没写且没有题干）\n"
    "    · `other`     说不清\n"
    "  ⚠️ **拿不准就给 `question`。** 判成非题会被老师当成「这块可以不用判」，"
    "判错一块等于丢一道题。\n"
    '· `tail_note`：这一块的**底部**情况，一句话，没异常就给空字符串 ""。'
    "只在这三种情况里写（可叠加，用「；」分隔）：\n"
    "    ① 底部被**页脚 / 页码 / 印刷说明**占掉一部分"
    "（这时要写明占了多少，例如「底部约 1/10 是页码与页脚」）；\n"
    "    ② 底部**疑似被页切掉** —— 这道题看着还没写完、后面应该还有"
    "（这时不要自己去补下一页的内容）；\n"
    "    ③ 底部混进了**相邻题的一两行残字**（这时不要把它算进 student_answer）。\n"
    "  ⚠️ 宁可不写也别瞎写：`tail_note` 会被做成界面提示，"
    "写错会引导老师去重切一块本来没问题的图。\n"
)

# 放在**最后**（输出示例之后）：示例里正好是三道题，只在前面的规则里说「只输出一个」
# 压不住它 —— 模型更爱照抄示例的形状，把一道题拆成三条。
_ONE_ITEM_TAIL = (
    "\n\n## 最后再说一遍：你只需要一道题\n"
    "上面的示例是「一整页」长什么样。你现在这张图是从一页上**裁下来的一道题**，"
    "所以 questions 数组里**恰好一个**元素。`q_no` 给空字符串。\n"
    "这一块不是题（卷头 / 页脚 / 结构说明 / 空白）时也一样只给**一个**元素 —— "
    "把 `block_kind` 标出来、`verdict` 给 `ungradable`，"
    "**不要**改成空数组、也不要为相邻题的残字多开一条。\n"
    # ⚠️ 必须在这里豁免：_FIELD_RULES 在 _REGION_BLOCK **之后**，里面那句
    #    「不要输出上面没列出的字段」会把这两个字段直接压掉（实测模型极听话）。
    "另外：`block_kind` 和 `tail_note` 这两个字段**只在这个流程里要** —— "
    "前面「不要输出上面没列出的字段」那条对它们**例外**，两个都必须给。"
    "其余的字段一个都不能少。\n"
)


def _images_block(ctx: dict) -> str:
    """一次送了多张图时，**必须说清每张图是什么**。

    不说的话最常见的错是「把答案照片当成学生作答」→ 判出全对，
    而且看起来完全合理（老师给的答案当然对）。这个错比漏判更危险，
    因为它是「自信地判对」。
    """
    mode = _as_str(ctx.get("answer_mode")) or "ai"
    if mode == "key":
        extra = list(ctx.get("answer_images") or [])
        what = "老师的标准答案"
    elif mode == "rubric":
        extra = list(ctx.get("rubric_images") or [])
        what = "老师的评分细则"
    else:
        return ""
    if not extra:
        return ""
    lines = [f"\n\n## 这一批图（共 {len(extra) + 1} 张，按顺序）",
             "第 1 张：**学生作答** —— 要判的就是它。"]
    for i in range(len(extra)):
        lines.append(
            f"第 {i + 2} 张：{what} —— 这是**判据，不是学生写的**。"
            "不要把它上面的内容当成 student_answer，也不要因为它写对了就判学生对。"
        )
    return "\n".join(lines)


def _context_block(ctx: dict) -> str:
    """只写「批改需要的上下文」，**不写学生姓名**。

    姓名对判定毫无用处（图和字迹已经指向他本人了），而它是最直接的隐私面 ——
    提示词会把内容发到第三方，能少给一个字段就少给一个。
    上层需要人名时自己有，不必经过模型。

    两种口径：整页（默认）与**裁出来的一道题**（ctx["region"]）。后者不能再说
    「这一页可能有几道题」—— 那句话会让模型在裁切残留的相邻题上多加一条。
    """
    page_no = ctx.get("page_no")
    if ctx.get("region"):
        where = f"第 {page_no} 页" if page_no else "一页作业"
        label = str(ctx.get("region_label") or "").strip()
        tip = f"（系统把这一块标成了「{label}」）" if label else ""
        span = ctx.get("span_pages") or []
        cross = ""
        if span:
            # 跨页题：一张图里是两页的段落竖拼的。不说清的话，中间的接缝
            # 会被当成「题目到此结束」，下半段直接被丢掉。
            cross = (
                f" ⚠️ 这一张图是**跨页拼起来的**（{ '、'.join('第 %s 页' % p for p in span) }"
                " 上的几段上下拼接），中间的白缝不是题目结束 —— 它是一道完整的题。"
            )
        return f"\n\n## 这一张图\n这是从{where}上裁下来的一道题{tip}。{cross}" + _images_block(ctx)

    lines = ["\n\n## 这一页"]
    if page_no:
        lines.append(f"卷面第 {page_no} 页。")
    lines.append(
        "题号一律写**卷面上印的题号**（不要自己从 1 开始重编）。"
        "这一页可能有几道题，也可能只是一道大题的一小部分。"
    )
    return "\n".join(lines) + _images_block(ctx)


def _reference_block(mode: str, ctx: dict) -> str:
    """三种 answer_mode 的差别**只在于参考依据从哪来**，判定口径完全一样。"""
    if mode == "key":
        text = (ctx.get("answer_text") or "").strip()
        if not text and (ctx.get("answer_images") or []):
            # 老师给的是**答案照片**（没粘贴文字）。这里必须交代清楚，
            # 否则「〔老师的答案〕」下面空着，模型会退回自己解题却仍声称按老师答案判。
            text = "（答案在附图上 —— 第 2 张起来的图是老师给的答案，不是学生写的）"
        steps = ctx.get("answer_steps") or None
        block = (
            "\n\n## 本次的依据：老师提供的标准答案\n"
            "下面是老师给的答案，**以它为准**。\n"
            "〔老师的答案〕\n" + text + "\n"
        )
        if steps:
            # 老师给了结构化步骤就一并用上：分步的对照比一整段文字准得多
            try:
                block += "〔答案的结构化步骤〕\n" + json.dumps(
                    steps, ensure_ascii=False) + "\n"
            except (TypeError, ValueError):
                pass
        block += (
            "判定口径：\n"
            "· 学生写法与老师答案**数学上等价**就算 correct"
            "（$x=2$ 与 $2=x$、先通分与直接约分，都算等价）；\n"
            "· 老师答案可能只是简写、也可能有笔误：如果它明显算错，以数学上正确的为准，"
            "并在 basis 里写明「老师答案此处疑有笔误」；\n"
            "· 老师只给了最终结果、没给过程时，解答题只看最终结果："
            "过程错但结果对 → partial。\n"
            "· 老师的答案里对不上这一题的，correct_answer 留空，verdict 给 ungradable。"
        )
        return block

    if mode == "rubric":
        text = (ctx.get("rubric_text") or "").strip()
        if not text and (ctx.get("rubric_images") or []):
            text = "（评分细则在附图上 —— 第 2 张起来的图是老师给的细则，不是学生写的）"
        return (
            "\n\n## 本次的依据：评分细则（分步给分点 / 扣分条件）\n"
            "下面是老师给的评分细则，**以它为准**。\n"
            "〔评分细则〕\n" + text + "\n"
            "判定口径：\n"
            "· 按细则里的分步给分点逐条对照：学生写到了哪一步、漏了哪一步；\n"
            "· basis 里点名**违反了哪一条**，尽量照抄细则里那一条的措辞；\n"
            "· correct_answer 写细则里这一题对应的给分点 / 关键步骤；\n"
            "· **这一期不给分**：不要输出得分、扣分，只判 correct / partial / wrong；\n"
            "· 细则没覆盖的写法，只要数学上正确就不要判错。"
        )

    return (
        "\n\n## 本次的依据：你自己解\n"
        "这一页**没有**老师提供的标准答案。请对每一道题**先自己完整解一遍**，"
        "把步骤写进 correct_answer；然后再回头看学生的作答、判对错。\n"
        "⚠️ 顺序很重要，**先解后判**：先看了学生的写法再「顺着他的思路找答案」，"
        "你会不自觉地认为他是对的。\n"
        "⚠️ 你的解法是模型自己给的，老师会拿它当参考。拿不准的题（多解题、超纲题、"
        "题干读不清的题）verdict 给 ungradable，不要硬判。"
    )


def _prompt_for(mode: str, ctx: dict) -> str:
    # ⚠️ 顺序有讲究：_REGION_BLOCK 紧跟 _context_block，**必须在 _FIELD_RULES 之前** ——
    #    _FIELD_RULES 里「这一页有几道题就输出几个元素」那句是整页口径，
    #    区域口径的那段要压在它前面才不会被它盖过去（模型更认靠后的指令）。
    #    所以区域模式下 _REGION_BLOCK 还额外把「只输出一个」重复了一遍。
    return (
        _ROLE
        + _context_block(ctx)
        + (_REGION_BLOCK if ctx.get("region") else "")
        + _TRANSCRIBE_RULES
        + _JUDGE_RULES
        + _FIELD_RULES
        + _reference_block(mode, ctx)
        + _OUTPUT_EXAMPLE
        + (_ONE_ITEM_TAIL if ctx.get("region") else "")
    )


# ---------------------------------------------------------------- 图片
def _prepare_image(path) -> str:
    """把磁盘上的页图压成 data URI。

    为什么不直接调 `vision.prepare_image_file`：它内部写死了 `MAX_EDGE=1600`
    （公式识别的实测基线）。批改读手写需要更高分辨率，而那个常量不能动 ——
    所以这里自己压一遍，只借用同一个 `images.MAX_IMAGE_BYTES` 上限。
    """
    p = Path(path) if path else None
    if p is None or not p.exists():
        raise _GradeError("这一页的图片不在了，可能已被清理，请重新上传")
    raw = p.read_bytes()
    if not raw:
        raise _GradeError("这一页的图片是空的，请重新上传")
    if len(raw) > MAX_IMAGE_BYTES:
        raise _GradeError(f"图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as e:                      # noqa: BLE001  PIL 的异常类型很杂
        raise _GradeError("这一页的图片读不出来，可能不是图片文件") from e

    # ⚠️ JPEG 不接受 alpha 与调色板模式，必须先 convert("RGB")：
    #    截图工具产出的 PNG 常带 alpha，少了这一步 save 会直接抛 OSError。
    im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > GRADE_MAX_EDGE:
        scale = GRADE_MAX_EDGE / max(w, h)
        im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=GRADE_JPEG_QUALITY, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _image_uris(png_path, ctx: dict) -> list[str]:
    """学生作答那张 + 老师给的答案/细则图（如果有）。**顺序不能变**。

    顺序就是提示词里「第 1 张 / 第 2 张」的指向，`_images_block` 按同一顺序写说明。
    答案图先压到 `GRADE_MAX_EDGE`，与页图同一条通道 —— 一张 4000px 的手机
    照片不压会直接超上限。

    ⚠️ 单张答案图坏掉**不整单失败**：跳过它比让老师「整份批改失败、
    还看不出是答案照片的问题」要好。但主图（学生作答）坏了必须抛 ——
    没有作答就没法判。
    """
    uris = [_prepare_image(png_path)]
    mode = _as_str(ctx.get("answer_mode")) or "ai"
    if mode == "key":
        extra = list(ctx.get("answer_images") or [])
    elif mode == "rubric":
        extra = list(ctx.get("rubric_images") or [])
    else:
        extra = []
    for p in extra[:_MAX_ANSWER_IMAGES]:
        try:
            uris.append(_prepare_image(p))
        except _GradeError:
            continue
    return uris


# ---------------------------------------------------------------- 配置
def _env_cfg() -> dict:
    """读不到设置表时的退路：只认环境变量（与 ai_polish.get_config 同名）。"""
    try:
        timeout = int(os.getenv("SHIKE_AI_TIMEOUT") or 60)
    except ValueError:
        timeout = 60
    cfg = {
        "base_url": (os.getenv("SHIKE_AI_BASE_URL") or "").strip(),
        "model": (os.getenv("SHIKE_AI_MODEL") or "").strip(),
        "api_key": (os.getenv("SHIKE_AI_API_KEY") or "").strip(),
        "timeout": timeout,
    }
    cfg["configured"] = bool(cfg["base_url"] and cfg["model"] and cfg["api_key"])
    return cfg


def _load_cfg(ctx: dict) -> dict:
    """拿配置。优先级：`ctx["cfg"]` > 应用设置（含环境变量覆盖）> 只有环境变量。

    这里那一次「临时开会话」是刻意保留的例外，理由写在模块头部：
    对外签名没有 db，而配置只存在设置表里。**只 SELECT、不写、立刻关。**
    """
    override = ctx.get("cfg")
    if isinstance(override, dict):
        cfg = {
            "base_url": str(override.get("base_url") or "").strip(),
            "model": str(override.get("model") or "").strip(),
            "api_key": str(override.get("api_key") or "").strip(),
            "timeout": override.get("timeout") or 60,
        }
        try:
            cfg["timeout"] = int(cfg["timeout"])
        except (TypeError, ValueError):
            cfg["timeout"] = 60
        cfg["configured"] = bool(cfg["base_url"] and cfg["model"] and cfg["api_key"])
        return cfg

    try:
        from ..db import SessionLocal          # 延迟导入：别把 sqlalchemy 拖进本层的模块级依赖
        db = SessionLocal()
        try:
            return ai_polish.get_config(db)
        finally:
            db.close()
    except Exception:                           # noqa: BLE001  没有库 / 没迁移 / 库被锁住
        return _env_cfg()


# ---------------------------------------------------------------- 解析（含抢救）
# 下面这一小段是 batch_page 同名工具的**副本**，刻意不 import 它：
#   · batch_page 正在被别人改，跨模块引用它的私有名会跟着一起断；
#   · batch_page 模块级就 import 了 sqlalchemy 与 models，而本层要保持脱库。
# 代价是两份实现，所以改这里的括号/截断处理时，记得对照 batch_page。
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        t = t.rstrip()
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _iter_json_objects(s: str):
    """逐个吐出 s 里的**顶层** `{...}`（考虑字符串内的括号与转义）。

    为什么不能用正则：题干与作答的 LaTeX 里全是花括号（`\\frac{a}{b}`），
    正则分不清「JSON 结构的括号」和「字符串内容里的括号」。
    为什么遇到不闭合不放弃：输出被截断时，最外层那个 `{"questions":[` 永远不闭合，
    直接放弃的话里面完整的题一道都救不出来。代价是 O(n²)，输出只有几 KB，无所谓。
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
            i += 1
            continue
        i = j + 1


def _loads(candidate: str):
    """尽力把这段文本解释成 JSON。返回 (obj | None, 用了修复手段?)。obj 可能是 dict 或 list。

    两种修复，都只在第一次失败后才试：
      · `strict=False` —— 允许字符串里有**裸换行/制表符**。这一条专治手写批改里最常见
        的一种坏 JSON：模型把学生一行一步的作答直接敲成了真换行，而不是 `\\n`。
        严格模式会把**整段**判死，而实际上一个字都没错，只是转义没做。
      · 尾随逗号 —— 模型写 JSON 时最典型的语法错，也是最便宜的修法。
    """
    for i, fixed in enumerate((candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate))):
        try:
            return json.loads(fixed, strict=False), i == 1
        except json.JSONDecodeError:
            continue
    return None, False


def _extract_object(text: str) -> str | None:
    """从返回里抠出 JSON 对象本体（模型常先来一句「好的，结果如下：」）。"""
    t = _strip_fence(text)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    return t[i: j + 1]


def _looks_like_item(o) -> bool:
    """抢救出来的对象得长得像一道题，别把 `{"note":"..."}` 这类碎片也收进来。"""
    if not isinstance(o, dict):
        return False
    return any(k in o for k in ("q_no", "verdict", "student_answer", "qtype", "stem"))


def _questions_list(obj: dict) -> list | None:
    """从整体对象里取题目数组。键名容忍与 batch_page 同一套理由：
    模型偶尔改用 items / data / result，能认就认，省一次返工。"""
    if isinstance(obj.get("questions"), list):
        return obj["questions"]
    if isinstance(obj.get("questions"), dict):
        # 只批了一道题时模型常把 questions 写成单个对象（不是数组）
        return [obj["questions"]]
    for k in ("items", "data", "list", "result", "problems", "gradings"):
        if isinstance(obj.get(k), list):
            return obj[k]
    return None


def _is_trusted_empty(text: str) -> bool:
    """模型是否**明确说过**「这页没有题」（而不是我们没解析出来）。

    刻意写窄：宽了会把「解析失败」误判成「这页本来没题」，
    那正是最坏的结果 —— 一整页静默消失，老师完全不知道漏了一页。
    """
    t = _strip_fence(text).replace(" ", "").replace("\n", "")
    if not re.search(r'"questions"\s*:\s*\[\s*\]', t):
        return False
    try:
        obj = json.loads(t)
        return isinstance(obj, dict) and obj.get("questions") == []
    except json.JSONDecodeError:
        return False


def _bad_json_reason(text: str) -> str:
    """给老师的「为什么没解析出来」，比一句「解析失败」有用得多。"""
    t = (text or "").strip()
    if not t:
        return "模型返回的是空内容（常见原因：max_tokens 被推理吃满，或这个模型不接受图片）"
    if "{" not in t:
        return "返回里没有任何 JSON 对象"
    if t.count("{") > t.count("}") and not _strip_fence(t).endswith("}"):
        return "输出被截断（最后一个对象没闭合），可以把这一页拆成两半再批"
    return "整体不是合法 JSON，也没有可抢救的单题对象"


# ---------------------------------------------------------------- 字段归一
def _as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "\n".join(_as_str(x) for x in v if _as_str(x))
    return ""


def _pick(o: dict, *keys):
    for k in keys:
        if k in o and o[k] not in (None, ""):
            return o[k]
    return None


def _clip(s: str, n: int) -> str:
    """截断离谱长文本。只为挡住模型陷入重复循环的输出，正常内容远小于上限。"""
    return s if len(s) <= n else s[:n] + "…"


def _labels(o: dict, kp_name: str = "") -> dict[str, str]:
    """模型给的「学段 / 体系 / 知识点 / 考法」四个分类标签 → 干净的 dict。

    ⚠️ 三条刻意的设计：

    ① **只收 `LABEL_KEYS` 里的键**。模型很容易顺手加一个 `"difficulty"` 或把
       整段解释塞进来 —— 那会让题卡上多出一个说不清的标签。多余的一律丢掉。

    ② **`kp` 不给就回填 `kp_name`**。知识点有两个来源会让同一道题出现两个说法；
       `kp_name` 是权威值（要拿去挂知识树），所以以它为准。

    ③ **`stage` 认不出就留空，不猜**。理由同 `_qtype`：空着老师自己看，
       猜错了会进筛选条件，比空着有害。

    `labels` 也可能是字符串（模型偶尔把对象写成一行 JSON），那时整体当空处理 ——
    不试图去 parse，parse 出来的键更容易乱。
    """
    raw = o.get("labels")
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, str] = {}
    for k in LABEL_KEYS:
        v = _as_str(raw.get(k)).strip().strip("。；;,. ")
        if not v:
            continue
        out[k] = _clip(v, _LABEL_VALUE_LIMIT)
        if len(out) >= _LABEL_COUNT_LIMIT:
            break
    stage = out.get("stage", "")
    if stage and stage not in LABEL_STAGES:
        # 不在枚举里就丢掉（不猜）。「高中一年级」这类也不收 —— 学段只要四档，
        # 年级不是学段，混进来就没法按学段筛了。
        out.pop("stage", None)
    if not out.get("kp") and kp_name:
        out["kp"] = _clip(kp_name.strip(), _LABEL_VALUE_LIMIT)
    return out


def _qtype(o: dict) -> tuple[str | None, bool]:
    raw = _as_str(_pick(o, *_ALIAS["qtype"]))
    if raw in taxonomy.QTYPES:
        return raw, False
    # 不认识就给 None（不是回落成 DEFAULT_QTYPE）：批改场景里「题型不明」
    # 比「猜一个最常见的」诚实，上层可以照 None 显示、让老师自己选。
    return None, bool(raw)


def _verdict(o: dict) -> tuple[str, bool]:
    raw = _as_str(_pick(o, *_ALIAS["verdict"])).lower()
    if raw in _VERDICTS:
        return raw, False
    hit = _VERDICT_LOOKUP.get(raw)
    if hit:
        return hit, False
    if not raw:
        return "ungradable", True
    return "ungradable", True


def _confidence(o: dict) -> str:
    raw = _as_str(_pick(o, *_ALIAS["confidence"])).lower()
    if raw in taxonomy.CONFIDENCES:
        return raw
    # 认不出来时给 low 而不是 medium：批改的置信度是给老师排队用的，
    # 说不清自己有多大把握的题，应该排到最前面让人看。
    return "low"


def _jsonable(o):
    """保证 raw 能原样落进 JSON 列（模型偶尔给非 JSON 类型的东西）。"""
    try:
        return json.loads(json.dumps(o, ensure_ascii=False, default=str))
    except Exception:                          # noqa: BLE001
        return {"_unserializable": str(o)[:500]}


# ---------------------------------------------------------------- 证据校验
# 全角/半角、减号的各种变体。数学作答里 `−`（U+2212）、`–`、`—`、`－` 混用极常见，
# 而 NFKC 只把全角 `－` 折成 `-`，其余几个原样留着 —— 不补这张表就会「明明在原文里
# 却校验不过」，那会让一批本该可信的判定白白降级。
_DASHES = str.maketrans({"−": "-", "–": "-", "—": "-", "﹣": "-", "－": "-"})
_WS_RE = re.compile(r"\s+")


def _norm_evidence(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_DASHES).replace("$", "")
    return _WS_RE.sub("", s)


def verify_evidence(quote: str, student_text: str) -> bool:
    """这句「引用原文」到底在不在学生的作答里。

    归一化：全半角（NFKC）+ 去 `$` + 去所有空白（含全角空格）+ 减号变体统一，
    然后做**纯子串**匹配。

    为什么这一步不能省：提示词里写「必须引用原文」是拦不住编造的。
    模型编一句「由①②得 x=2」成本极低，而老师不会真去核对 ——
    一次凭编造的引用判错，老师就再也不信这一整页了。
    只有字符串匹配能把这种错误挡住。

    ⚠️ 刻意不加「最短长度」之类的门槛：选择题的合法证据就是 "B" 这一个字符，
    门槛一加，所有选择题都会被判成「证据不足」。
    """
    q = _norm_evidence(quote)
    t = _norm_evidence(student_text)
    if not q or not t:
        return False
    if len(q) > len(t):
        return False
    return q in t


def _apply_evidence_guard(it: dict) -> dict:
    """判错/扣分必须有学生原文撑着，否则降级 `ungradable`。

    宁可判不了，也不能凭一条编造的引用判学生错 —— 这是「宽着判」的具体落法：
    冤判（对的被判错）会一次性摧毁信任，漏判只是让老师多点一下。
    """
    if it["verdict"] not in ("wrong", "partial"):
        return it
    raw_q = it.get("raw") or {}
    obj = raw_q.get("question") if isinstance(raw_q, dict) else None
    quote = _as_str(_pick(obj, *_ALIAS["evidence_quote"])) if isinstance(obj, dict) else ""
    if verify_evidence(quote, it["student_answer"]):
        return it

    it["verdict"] = "ungradable"
    it["flags"] = list(it["flags"]) + ["insufficient_evidence"]
    it["basis"] = (
        "模型给的依据在原文里找不到，这题判不了"
        + (f"（它引用的是「{_clip(quote, 40)}」）" if quote else "（它没有给出可核对的原文）")
    )
    # 既然依据不成立，错因也就无从谈起；置信度同样不能还挂着 high。
    it["error_reason"] = ""
    it["confidence"] = "low"
    return it


# ---------------------------------------------------------------- 单题归一
def _item_from_obj(o: dict, born: list[str]) -> dict:
    """模型的一道题 → items 的一项（字段名固定，上层直接落库）。"""
    flags: list[str] = list(born)

    qtype, bad_qtype = _qtype(o)
    if bad_qtype:
        flags.append("qtype_unknown")

    verdict, bad_verdict = _verdict(o)
    if bad_verdict:
        flags.append("verdict_invalid")

    # 模型偶尔会自作主张给分。一期不收，但要留个痕迹（老师看得到「它想给分」）。
    if any(k in o for k in ("score", "points", "得分", "full_score", "max_score")):
        flags.append("score_ignored")

    # 模型自己给的 flags 也并进来，但只收「像标记的」字符串：
    # 这个字段要落库、要在前端显示，不能让模型往里塞句子。
    for f in (o.get("flags") or []) if isinstance(o.get("flags"), list) else []:
        f = _as_str(f)
        if f and re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", f) and f not in flags:
            flags.append(f)

    error_reason = _clip(_as_str(_pick(o, *_ALIAS["error_reason"])), _REASON_LIMIT)
    if verdict == "correct":
        # 判对就没有错因。模型常在这时塞一句「无」/「没有错误」，那会在前端变成噪音。
        error_reason = ""

    # 知识点单独取一次：`labels.kp` 缺省时要拿它回填（见 `_labels`），
    # 免得同一道题的知识点在两处说法不一致。
    kp_name = _clip(_as_str(_pick(o, *_ALIAS["kp_name"])), _KP_LIMIT)

    item = {
        "q_no": _clip(_as_str(_pick(o, *_ALIAS["q_no"])), 16),
        "qtype": qtype,
        "stem": _clip(_as_str(_pick(o, *_ALIAS["stem"])), _STEM_LIMIT),
        # ⚠️ 学生的作答**不改写**（提示词要求逐字转录，这里也不做任何清洗）：
        #    只截断离谱长度，别的一律原样。
        "student_answer": _clip(_as_str(_pick(o, *_ALIAS["student_answer"])), _ANSWER_LIMIT),
        "correct_answer": _clip(_as_str(_pick(o, *_ALIAS["correct_answer"])), _ANSWER_LIMIT),
        "verdict": verdict,
        # 一期**一律 None**：既不给分，也不给满分。给 0 会把统计口径带偏。
        "score": None,
        "full_score": None,
        "basis": _clip(_as_str(_pick(o, *_ALIAS["basis"])), _BASIS_LIMIT),
        "confidence": _confidence(o),
        "kp_name": kp_name,
        "error_reason": error_reason,
        "flags": flags,
        # 图上读到的东西（几何要点 / 统计图表的数表）。**必须落库并给老师看**：
        # 2026-10 之前它只躺在 raw 里，于是「凭图判分」的结论对老师完全不可核对 ——
        # 他没法判断模型说的「有直角符号」是不是真有。
        "figure_note": _clip(_as_str(_pick(o, *_ALIAS["figure_note"])), _FIGURE_LIMIT),
        # 两个结构信号（0030，只在区域模式要求模型给；整页模式没有这两个字段 → 空）。
        # ⚠️ 不在这一层做合法性判断：`block_kind` 的枚举归一在 store 的 `_block_kind`，
        #    与「认不出就归空」的策略放在一处，免得两处规则漂移。
        "block_kind": _as_str(o.get("block_kind")).strip()[:16],
        "tail_note": _clip(_as_str(o.get("tail_note")), _TAIL_NOTE_LIMIT),
        # 分类标签（0031）：学段 / 体系 / 知识点 / 考法。归一化全在 `_labels` 里，
        # 这里只负责取 —— 规则放一处，免得与提示词里的说法漂移。
        "labels": _labels(o, kp_name),
        # 模型这一题的原始输出：复核/重放用。meta 里补「哪个模型、有没有被截断、哪种依据」，
        # 但不塞 prompt（一整段提示词几十 KB，存进每一行毫无意义）。
        "raw": {
            "question": _jsonable(o),
            "meta": {"model": "", "finish_reason": "", "mode": "", "sample": 1},
        },
    }
    # 证据校验放在**这一层**、不放在 recognize_page 里：否则同一个 JSON 走离线解析
    # （parse_grade_items）与走真调用会得到不同结果 —— 那种不一致会让离线测试
    # 测出一批实际不会发生的通过。
    return _apply_evidence_guard(item)


def _parse_grade_text(text: str) -> tuple[list[dict], str]:
    """返回 (items, err)。err 非空 = 这页没批成。**永不抛异常。**

    三条路依次尝试，越往后越「脏」：
      ① 整体解析我们要求的 `{"questions":[...]}`（或顶层直接是一个题对象）
      ② 顶层就是一个数组（模型没套那层壳）
      ③ 抢救：扫顶层 `{...}` 逐个解析 —— 专治输出被截断 / JSON 前后夹废话

    返回空列表 + 空 err 只有一种情况：模型**明确**说了这页没题（`{"questions":[]}`）。
    那与「解析失败」必须区分：前者是正常结果（空白页、页尾），后者要让老师看见。
    """
    body = _strip_fence(text or "")
    flags0: list[str] = []
    objs: list[dict] = []

    candidate = _extract_object(body)
    if candidate:
        obj, repaired = _loads(candidate)
        if isinstance(obj, dict):
            if repaired:
                flags0.append("json_repaired")
            qs = _questions_list(obj)
            if qs is not None:
                objs = [x for x in qs if isinstance(x, dict)]
            elif _looks_like_item(obj):
                # 这页确实只有一道题时，模型有时不套 questions 壳 —— 也接受
                objs = [obj]

    if not objs:
        i, j = body.find("["), body.rfind("]")
        if 0 <= i < j:
            arr, _repaired = _loads(body[i: j + 1])
            if isinstance(arr, list):
                objs = [x for x in arr if isinstance(x, dict)]

    salvaged = False
    if not objs:
        for cand in _iter_json_objects(body):
            o, _repaired = _loads(cand)
            if _looks_like_item(o):
                objs.append(o)
        salvaged = bool(objs)
        if salvaged:
            flags0.append("json_salvaged")

    if not objs:
        if _is_trusted_empty(body):
            return [], ""
        return [], "模型没给可用的 JSON：" + _bad_json_reason(body)

    out: list[dict] = []
    seen: set[str] = set()
    for o in objs[:MAX_ITEMS_PER_PAGE]:
        it = _item_from_obj(o, flags0)
        # 同一题号重复出现（模型把一道题输出两遍）只留第一条：
        # 落库后两条同号记录会让老师以为卷面有两道 3 题。
        key = it["q_no"]
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(it)
    return out, ""


def parse_grade_items(text: str) -> list[dict]:
    """公开的解析入口：把模型返回的**一段文本**解析成 items。**永不抛异常。**

    上层的离线测试就喂固定 JSON 给它（不必有 key、不必联网、不必碰库）。
    ⚠️ 解析不出来与「这页真的没题」都返回 []；要区分请用 recognize_page 的 err。
    """
    items, _err = _parse_grade_text(text)
    return items


# ---------------------------------------------------------------- 采样稳定性
# 一期**不主动跑第二遍**（成本与延迟都翻倍，且没有实测数据说明值得）。
# 但契约先留好：ctx["samples"]=2 时 recognize_page 会换温度再跑一遍，结果进下面这个函数。
_OPPOSITE = {"correct": "wrong", "wrong": "correct"}


def apply_stability(items: list[dict], other: list[dict]) -> list[dict]:
    """把同页两次独立采样的结果对一遍，verdict 翻转的题打 `unstable`。

    只抓 `correct ↔ wrong` 这一种翻转 —— 那是「判反了」，对老师伤害最大。
    partial / blank / ungradable 之间的差别本来就是渐变的，
    把它们也算成不稳定，只会让 flags 变成噪音。

    不原地改入参（返回新的 list），这样调用方留着原结果还能做别的比较。
    """
    by_no: dict[str, dict] = {}
    for o in other or []:
        k = _as_str(o.get("q_no"))
        if k and k not in by_no:
            by_no[k] = o
    out: list[dict] = []
    for it in items or []:
        peer = by_no.get(_as_str(it.get("q_no")))
        if peer is not None and _OPPOSITE.get(it.get("verdict")) == peer.get("verdict"):
            it = dict(it)
            it["flags"] = list(it.get("flags") or []) + ["unstable"]
            it["confidence"] = "low"
        out.append(it)
    return out


# ---------------------------------------------------------------- 模型调用
def _finish_reason(data: dict) -> str:
    """取 finish_reason。

    ⚠️ 全项目此前**没有一处读它**，这是已知缺陷：输出被 max_tokens 截断时
    接口照样回 200，正文是一段截掉一半的 JSON —— 不读这个字段就永远发现不了，
    表现是「某些页莫名其妙少了几道题」。
    """
    try:
        choices = data.get("choices") or []
        if not choices:
            return ""
        return str((choices[0] or {}).get("finish_reason") or "")
    except (AttributeError, IndexError, TypeError):
        return ""


def _call_model(uris, prompt: str, cfg: dict, temperature: float = 0) -> tuple[str, str]:
    """发一次请求（含三级降级 + 截断重试）。返回 (正文, finish_reason)。

    `uris` 可以是单张也可以是**一串**：串里第 1 张永远是学生作答，
    后面的是老师给的答案/细则图（顺序与 `_images_block` 的说明一一对应）。

    降级顺序照抄 batch_page.recognize_page，理由一样：这些参数都是「锦上添花」，
    宁可退化成靠提示词约束，也不能让整条链路因为某个服务商不认某个参数而不可用：
        去掉 response_format → 去掉 reasoning_effort → 降 max_tokens
    """
    if isinstance(uris, str):
        uris = [uris]
    body = {
        "model": cfg["model"],
        "messages": [{
            "role": "user",
            # 图在前、要求在后：先看清内容，再读该怎么输出（与 vision/batch_page 一致）
            "content": [
                *({"type": "image_url", "image_url": {"url": u}} for u in uris),
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": temperature,       # 0 = 判定要稳、不要发挥；第二次采样才调高
        "max_tokens": GRADE_MAX_TOKENS,   # 低于 2000 会静默返回空正文（vision.py 头部）
        "reasoning_effort": "low",        # 不传的话推理吃掉 95% 预算（同上）
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    try:
        timeout = int(cfg.get("timeout") or 60)
    except (TypeError, ValueError):
        timeout = 60
    timeout = min(max(GRADE_TIMEOUT_MIN, timeout), GRADE_TIMEOUT_MAX)

    def _post(b: dict) -> dict:
        return ai_polish.post_json(ai_polish.chat_url(cfg["base_url"]), b, headers, timeout)

    try:
        data = _post(body)
    except ai_polish.AiError as e:
        msg = str(e)
        dropped = False
        if "response_format" in body and "response_format" in msg:
            body.pop("response_format")
            dropped = True
        if "reasoning_effort" in body and "reasoning_effort" in msg:
            # 去掉它意味着退回 high：推理会吃掉大部分预算，很可能被判为截断。
            # 但那是「有可能成」与「一定不成」的区别，值得试。
            body.pop("reasoning_effort")
            dropped = True
        if "max_tokens" in body and "max_tokens" in msg:
            body["max_tokens"] = 2000       # 服务商上限低于我们给的值时的保底
            dropped = True
        if not dropped:
            raise
        data = _post(body)

    finish = _finish_reason(data)
    if finish == "length":
        # 重试一次，预算翻倍：推理开销是浮动的，同一张图第二次常常刚好够。
        bigger = dict(body)
        try:
            bigger["max_tokens"] = min(GRADE_TOKEN_CEILING, int(body.get("max_tokens") or 2000) * 2)
        except (TypeError, ValueError):
            bigger["max_tokens"] = GRADE_TOKEN_CEILING
        data = _post(bigger)
        finish = _finish_reason(data)
        if finish == "length":
            # ⚠️ 这里**不**把抢救出来的半页题返回去：老师看到「这一页批完了」会
            #    以为剩下的题是空白/没做。整页失败、让老师重试或拆半，比半真半假强。
            raise _GradeError(
                "这一页的输出被截断了（finish_reason=length，已重试一次）。"
                "可以把这一页拆成两半再批，或到设置里把这个模型的输出预算调大。"
            )

    try:
        text = ai_polish.extract_content(data)
    except ai_polish.AiError as e:
        raise _GradeError(
            "模型返回了空内容。常见原因：① max_tokens 被推理吃满；"
            "② 当前模型不接受图片输入（到「设置 → AI 润色」换一个支持识图的）。"
        ) from e
    return text, finish


# ---------------------------------------------------------------- 对外入口
def _recognize(png_path: str, ctx: dict) -> tuple[list[dict], str]:
    answer_mode = _as_str(ctx.get("answer_mode")) or "ai"
    if answer_mode not in ("ai", "key", "rubric"):
        raise _GradeError(
            f"不认识的批改依据「{answer_mode}」，只能是 ai / key / rubric"
        )
    if answer_mode == "key" and not (_as_str(ctx.get("answer_text"))
                                     or (ctx.get("answer_images") or [])):
        raise _GradeError("选择了「按老师答案批改」，但没有读到答案内容")
    if answer_mode == "rubric" and not (_as_str(ctx.get("rubric_text"))
                                        or (ctx.get("rubric_images") or [])):
        raise _GradeError("选择了「按评分细则批改」，但没有读到细则内容")

    cfg = _load_cfg(ctx)
    model = _as_str(ctx.get("model")) or cfg.get("model") or ""
    if model:
        cfg["model"] = model
    if not cfg.get("configured"):
        raise _GradeError(
            "还没有配置 AI 接口（地址 / 模型 / 密钥），请到「设置 → AI 润色」里填写。"
            "批改和公式识别共用这一份配置。"
        )

    uris = _image_uris(png_path, ctx)
    prompt = _prompt_for(answer_mode, ctx)
    text, finish = _call_model(uris, prompt, cfg, temperature=0)

    items, err = _parse_grade_text(text)
    if err:
        return [], err

    if ctx.get("region") and len(items) > 1:
        # 框画大了，把下一题的抬头也裁了进来。只留第一条（题干在上面），并打标 ——
        # **不能静默丢**：老师要知道「这个框框多了」，否则会以为邻题被判进了这一题。
        items = items[:1]
        if "region_multi" not in items[0]["flags"]:
            items[0]["flags"].append("region_multi")

    for it in items:
        it["raw"]["meta"].update({
            "model": cfg["model"],
            "finish_reason": finish,
            "mode": answer_mode,
            "sample": 1,
        })
        if answer_mode == "ai":
            # 参考解是模型自己做的，没经老师核对。**必须**标出来：
            # 模型解错了就是整题判错，这个差别不给老师看见就是灾难。
            if "reference_unverified" not in it["flags"]:
                it["flags"].append("reference_unverified")

    # 两次采样：默认不跑（成本翻倍），ctx["samples"]>=2 时才跑第二遍。
    try:
        samples = int(ctx.get("samples") or 1)
    except (TypeError, ValueError):
        samples = 1
    if samples >= 2 and items:
        try:
            text2, finish2 = _call_model(uris, prompt, cfg, temperature=0.7)
            items2, _err2 = _parse_grade_text(text2)
            if items2:
                for it in items2:
                    it["raw"]["meta"].update({
                        "model": cfg["model"], "finish_reason": finish2,
                        "mode": answer_mode, "sample": 2,
                    })
                    if answer_mode == "ai" and "reference_unverified" not in it["flags"]:
                        it["flags"].append("reference_unverified")
                items = apply_stability(items, items2)
        except Exception:                       # noqa: BLE001
            # 第二次采样失败不该连累第一次的结果 —— 它只是加固，不是必需。
            pass

    return items, ""


def recognize_page(png_path: str, ctx: dict) -> tuple[list[dict], str]:
    """批改**一页**。返回 (items, err)。

    ctx = {"student": "李四", "answer_mode": "ai"|"key"|"rubric",
           "answer_text": "...", "rubric_text": "...", "page_no": 1,
           "answer_steps": [...](可选，老师答案的结构化步骤), "model": "(可选覆盖)"}

    成功：items 是 list[dict]，err == ""
    失败：items == []，err 是「这页没批成」的人话原因（不是堆栈）
    ⚠️ 永不抛异常。

    「成功」有两种：有题（items 非空），和这页确实没题（`{"questions":[]}` → items 为空
    但 err 为空）。两者都不该让老师看到一条错误，所以**调用方只看 err 判成败**，
    不要用 `if not items` 当失败。

    学生姓名只用于上层（见 _context_block：它**不进提示词**）。
    """
    try:
        return _recognize(png_path, ctx or {})
    except _GradeError as e:
        return [], str(e)
    except Exception as e:                      # noqa: BLE001  这一层是兜底，什么都得接住
        # 不把 traceback 抛给上层：调用方是后台线程，抛出去的表现是「这页凭空消失」。
        return [], f"这一页批改失败：{type(e).__name__}: {_clip(str(e), 200)}"


def recognize_region(png_path: str, ctx: dict) -> tuple[list[dict], str]:
    """批改**一个框**（一道题）。契约与 `recognize_page` 完全一致。

    ctx 多两个键：`region=True`（固定）、`region_label`（"第 3 题"，只写进提示词
    帮模型定位，**不参与题号绑定**）。

    返回 0 条**不是错误**：框画在了空白处 / 那一块确实没题。上层会补一条
    「这个框里没读到题」的占位，让老师看见而不是让它凭空消失。
    """
    try:
        return _recognize(png_path, {**(ctx or {}), "region": True})
    except _GradeError as e:
        return [], str(e)
    except Exception as e:                      # noqa: BLE001
        return [], f"这一题批改失败：{type(e).__name__}: {_clip(str(e), 200)}"
