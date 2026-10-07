# -*- coding: utf-8 -*-
"""公式与题干识别：一张图进去，带 $LaTeX$ 的文本出来。

为什么不塞进 ai_polish：
    润色和识别是两类任务 —— 一个改文字，一个看图。混在一起之后，
    改润色的提示词要跨过两百行识别代码才能找到，两边还会共用一个
    「temperature 该给多少」的地方，早晚互相绊住。
    共用的是**调用层**（ai_polish.chat_url / post_json / extract_content），不是业务。

为什么不走「PDF 文字层优先」：
    电子版试卷确实带文字层，但对公式是废的。实测 2025 高考全国一卷：
    原式 `x^2+(y+2)^2=r^2(r>0)` 取出来是一串散落的单字符
    `2 / 2 / 2 / ( / 2) / ( / 0) / x / y / r / r` —— 顺序全乱、正负号丢失。
    解析版 docx 更干脆，公式是内嵌对象，段落文本里是空的。
    所以**哪怕电子版也直接送视觉模型**，不做文本层正则。

⚠️ 三条硬约束，都是在 DeepSeek deepseek-flash 上实测量出来的（2026-10-05）。
   改这段代码前先读，否则会踩到「接口 200、不报错、但结果不对」：
    1. 必须传 `reasoning_effort: "low"`。不传时模型默认 high，会先把 95% 的
       输出预算用在推理上 —— 实测 4097 tokens 里 3912 是推理，正文只剩 158。
       改传 low 后总输出 402、推理 175，**识别结果一样准**。
       （参数名是 reasoning_effort；写成 effort 会被**静默忽略**，不报错。）
    2. `max_tokens` 不能小于 2000。给 2000 时推理正好把预算吃满，接口返回 200、
       正文是**空字符串**，一个错都不报 —— 这类失败最难查。
    3. 只发送压缩过的图。页面渲染图是 3 倍分辨率（2000×3000 量级），
       base64 之后还要再涨 33%，请求体白白翻几倍，模型那边也会自己降采样。
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image
from sqlalchemy.orm import Session

from . import ai_polish
from .images import MAX_IMAGE_BYTES

# 送模型前把长边压到这个值。1600 对公式识别够用：
# 实测整页试卷（1600px 量级）题干与公式全部正确，再高只是徒增请求体。
MAX_EDGE = 1600
JPEG_QUALITY = 85

# 输出预算。低于 2000 会因推理吃满而静默返回空正文（见模块开头第 2 条）。
MAX_TOKENS = 4096


class VisionError(ai_polish.AiError):
    """识别失败。继承 AiError，调用方 catch 哪一种都能接住。消息可直接给用户看。"""


# ---------------------------------------------------------------- 提示词
# ⚠️ 下面是**普通字符串**，不是 r-string —— 所以 LaTeX 的反斜杠必须写双份。
#    这里踩过一次真的：`\frac` 里的 `\f` 被 Python 当成换页符、
#    `\theta` 里的 `\t` 被当成制表符，提示词送到模型手里已经面目全非，
#    而模型照样有输出，只是公式规范差了 —— 典型的「不报错但不对」。
#    改动这段文字之后，务必用 test_vision.py 跑一遍真实样例。
_ROLE = (
    "你是一个数学试卷的文本转录工具。图里可能有印刷体、手写批注、作答横线、"
    "页眉页脚、条形码、以及「请在此作答」这类说明。"
    "你要做的只有一件事：把题目（或答案）本身准确转录出来，"
    "并把所有数学公式写成标准 LaTeX。"
)

_QUESTION_RULES = (
    "\n\n请把图中这道题的题干完整转录成一段纯文本：\n"
    "1. 行内公式用 $...$ 包住，例如 $x^2+(y+2)^2=r^2$；\n"
    "2. 分数用 \\frac{}{}、根式用 \\sqrt{}、上下标写 ^{} 和 _{}、\n"
    "   定积分写 \\int_{a}^{b}、求和写 \\sum_{n=1}^{\\infty}、\n"
    "   希腊字母写 \\alpha \\beta \\theta \\pi 这类，一律用标准 LaTeX 命令；\n"
    "3. 选择题的选项（(A)(B)(C)(D) 或 A. B. C. D.）要保留，"
    "选项里的公式同样转成 LaTeX；\n"
    "4. 不要抄答题横线、页码、页眉页脚、条形码、「请在此作答」这类说明，"
    "也不要抄题号本身（题号由系统另外编号）；\n"
    "5. 分值标注也不要抄 —— 像右对齐的 (3 marks)、（5 分）这类，"
    "分值有单独的字段，抄进题干里会在导出卷子时变成两份；\n"
    "6. 只输出题干文本本身 —— 不要解释、不要客套、不要用 Markdown 代码围栏包起来。"
)

_FORMULA_RULES = (
    "\n\n图中是一个或多个数学公式。请把它们转成 LaTeX：\n"
    "1. 行内公式用 $...$ 包住；需要独立成行单独排版的用 $$...$$ 包住；\n"
    "2. 多个公式之间用换行分隔，保持图上原有的先后顺序；\n"
    "3. **只要数学式子** —— 题号、题干文字、选项字母、页眉页脚都不要抄。\n"
    "   实测过：不写这条，它会连「2. 已知集合…」的题号一起抄进来；\n"
    "4. 只输出 LaTeX 本身 —— 不要编号、不要解释、不要用代码围栏包起来。"
)

_ANSWER_RULES = (
    "\n\n图中是一道题的标准答案（可能只是简短的结果，也可能是整段解题过程）。"
    "请把它完整转录成文本：\n"
    "1. 行内公式用 $...$ 包住；独立成行的公式用 $$...$$ 包住；\n"
    "2. 分数用 \\frac{}{}、根式用 \\sqrt{}、上下标写 ^{} 和 _{}，"
    "一律用标准 LaTeX 命令；\n"
    "3. **保留原有的换行与步骤顺序** —— 解题过程是一步一行的，"
    "挤成一整段就看不出推导到哪一步了，这是答案和题干最大的差别；\n"
    "4. 分步书写时可以用 Markdown 有序列表（1. 2. 3.）把步骤标出来，"
    "结论句单独一行；\n"
    "5. 不要抄题号、分值标注（(3 marks) / （5 分））、页眉页脚、条形码这类东西；\n"
    "6. 只输出答案文本本身 —— 不要解释、不要客套、不要用 Markdown 代码围栏包起来。"
)

PROMPTS = {
    # 单题：从框选区或整页里挑一道题（录题页那个按钮用的就是它）
    "question": _ROLE + _QUESTION_RULES,
    # 纯公式：只认式子，不要题干（补答案、改公式时用）
    "formula": _ROLE + _FORMULA_RULES,
    # 答案：整段解题过程。和 question 最大的差别是**要保住换行与步骤顺序** ——
    # 答案挤成一段就看不出推导到哪一步了（录题页「② 选答案」用）
    "answer": _ROLE + _ANSWER_RULES,
}


# ---------------------------------------------------------------- 图片预处理
def _decode_data_uri(s: str) -> bytes:
    """接受 `data:image/png;base64,xxx` 或裸 base64。两种都收，
    是因为前端可能来自 FileReader（带前缀），也可能是自己拼的（不带）。"""
    s = (s or "").strip()
    if not s:
        raise VisionError("没有拿到图片 —— 请先在左侧框选出一道题")
    if s.startswith("data:"):
        _, _, s = s.partition(",")
    s = "".join(s.split())          # FileReader 的输出可能带换行
    try:
        return base64.b64decode(s, validate=True)
    except Exception as e:          # noqa: BLE001  binascii.Error 等
        raise VisionError("图片数据不是合法的 base64，请重试") from e


def prepare_image(raw: bytes) -> tuple[str, tuple[int, int]]:
    """压缩并转成 data URI。返回 (uri, (原图宽, 原图高))。

    ⚠️ JPEG 不接受 alpha 通道和调色板模式，必须先 convert("RGB")。
       截图工具产出的 PNG 常常带 alpha，少了这一步 save 会直接抛 OSError。
    """
    if not raw:
        raise VisionError("图片是空的")
    if len(raw) > MAX_IMAGE_BYTES:
        raise VisionError(f"图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as e:          # noqa: BLE001  PIL 的异常类型很杂
        raise VisionError("这张图读不出来，可能不是图片文件") from e

    original = im.size
    im = im.convert("RGB")
    w, h = im.size
    if max(w, h) > MAX_EDGE:
        scale = MAX_EDGE / max(w, h)
        im = im.resize(
            (max(1, round(w * scale)), max(1, round(h * scale))),
            Image.LANCZOS,
        )
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=JPEG_QUALITY, optimize=True)
    uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return uri, original


def prepare_image_file(path) -> str:
    """从磁盘上的图直接出 data URI。

    批量识别用的就是它 —— 那边的题块在提交时就已经裁好落盘了（`crops/` 里），
    再让前端把图读成 base64 传一遍纯属浪费：一张 3 倍分辨率的原貌图
    经过浏览器 → JSON → 服务端，往返几百 KB。
    """
    p = Path(path)
    if not p.exists():
        raise VisionError("原貌图不见了，可能已被清理，请重新分割")
    return prepare_image(p.read_bytes())[0]


def _strip_fence(text: str) -> str:
    """剥掉模型可能加上的 ``` 代码围栏。围栏进了题干就是脏数据 ——
    它会一路带到导出卷子里，印在纸上。"""
    t = text.strip()
    if not t.startswith("```"):
        return t
    t = t.split("\n", 1)[-1] if "\n" in t else t
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    return t.strip()


# ---------------------------------------------------------------- 主入口
def recognize(
    db: Session,
    image_b64: str,
    mode: str = "question",
    hint: str = "",
    timeout_cap: int | None = None,
) -> dict:
    """识别一张图，返回 {text, mode, model, usage}。

    mode: question（题干）/ answer（答案，保留分步换行）/ formula（纯公式）
    hint: 老师额外给的一句话提示（可空）。允许它存在，是因为模型偶尔会读错
          一个手写字母，而老师一眼就知道该是什么 —— 与其让他反复重试，
          不如让他直接说一句「这是 a 不是 alpha」。
    """
    if mode not in PROMPTS:
        raise VisionError(f"不支持的识别模式 {mode}（可选 {' / '.join(PROMPTS)}）")

    cfg = ai_polish.get_config(db)
    if not cfg["configured"]:
        raise VisionError(
            "还没有配置 AI 接口（地址 / 模型 / 密钥），请到「设置 → AI 润色」里填写。"
            "公式识别和润色共用同一份配置。"
        )

    uri, _size = prepare_image(_decode_data_uri(image_b64))

    prompt = PROMPTS[mode]
    if (hint or "").strip():
        prompt += "\n\n老师的补充说明（以此为准）：" + hint.strip()

    body = {
        "model": cfg["model"],
        "messages": [{
            "role": "user",
            # 图在前、要求在后：让模型先看清内容，再读该怎么输出。
            "content": [
                {"type": "image_url", "image_url": {"url": uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0,            # 转录要还原，不要发挥
        "max_tokens": MAX_TOKENS,    # 低于 2000 会静默返回空正文
        # 不传这个的话推理会吃掉 95% 预算（见模块开头第 1 条）
        "reasoning_effort": "low",
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    timeout = int(timeout_cap or cfg["timeout"])

    def _call(b: dict) -> dict:
        return ai_polish.post_json(
            ai_polish.chat_url(cfg["base_url"]), b, headers, timeout
        )

    try:
        data = _call(body)
    except ai_polish.AiError as e:
        # reasoning_effort 是 DeepSeek 特有的调优参数，别的服务商
        # （智谱 / Kimi / 硅基流动 / 本机 Ollama…）可能直接回 400「未知参数」。
        # 那就去掉它重试一次 —— 宁可多花一点推理预算，也不能让整条识别链路
        # 因为一个「省钱的开关」而彻底不可用。
        if "reasoning_effort" in body and "reasoning_effort" in str(e):
            body.pop("reasoning_effort")
            data = _call(body)
        else:
            raise

    text = _strip_fence(ai_polish.extract_content(data))
    if not text:
        raise VisionError(
            "模型返回了空内容。两个常见原因："
            "① max_tokens 给得太小，推理把预算占满了（本模块已给到 "
            f"{MAX_TOKENS}，若你改过就改回来）；"
            "② 当前模型不接受图片输入 —— 到「设置 → AI 润色」换一个支持识图的"
            "（DeepSeek 的 deepseek-flash 实测可用）。"
        )

    usage = data.get("usage") or {}
    return {
        "text": text,
        "mode": mode,
        "model": cfg["model"],
        # 回报用量：自付费的服务老师会关心这一次花了多少
        "usage": {
            "prompt": usage.get("prompt_tokens"),
            "completion": usage.get("completion_tokens"),
        },
    }
