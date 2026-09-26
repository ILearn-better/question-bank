# -*- coding: utf-8 -*-
"""AI 润色：调用 OpenAI 兼容的 /chat/completions 接口。

为什么只做「OpenAI 兼容」这一种协议：
    DeepSeek、通义千问、Kimi、智谱、本地 Ollama / vLLM / LM Studio……全部实现这个格式。
    只存 base_url + model + api_key 就能对接绝大多数服务，不必为每家写一个适配器。
    这也让「换一家更便宜的」变成改一个输入框的事，而不是改代码。

**不加任何 HTTP 依赖**：用标准库 urllib。这个功能一天也就调用几次，
为此引入 httpx/requests 不值得；FastAPI 的同步路由本身跑在线程池里，阻塞没问题。

⚠️ 隐私：润色会把反馈正文（通常含学生姓名）**发到第三方**。这与本项目「数据存本机」
   的定位冲突。所以：
     · 界面上必须明确提示「内容会发送到你配置的 AI 服务」
     · 必须由老师主动点按钮才会发生，绝不在保存时自动润色
     · api_key 只存本机、接口一律脱敏返回
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import AiSetting

# 反馈的四段字段（润色只处理这几个键，别的键原样返回）
FIELDS = ("performance", "problems", "homework", "next_plan")
FIELD_CN = {
    "performance": "课堂表现",
    "problems": "存在问题",
    "homework": "作业布置",
    "next_plan": "下次安排",
}

DOC_SYSTEM_PROMPT = (
    "你是一位经验丰富的中学数学老师，正在把一节课的零散记录整理成一篇可以发给家长的正式文档。\n"
    "你会拿到【模板】和【原始记录】两部分。请严格按下面的要求做：\n"
    "1. 仿照模板的结构、栏目顺序、栏目名与详略程度来组织全文，语气也要和模板一致；\n"
    "2. 只整理与润色：把口语、零碎、重复的表达写通顺，把重复的合并，\n"
    "   但绝不改变任何事实，绝不添加原始记录里没有的信息；\n"
    "3. 原始记录里没有对应内容的栏目，直接把整个栏目省略 ——\n"
    "   不要留一个空标题、不要写「无」「待补充」「暂无」，\n"
    "   更不要编造一个看起来像真的数字、日期、分数或章节号；\n"
    "4. 原始记录里有、但模板里没有的内容不要丢掉，另起一个合适的栏目如实写上；\n"
    "5. 「课程信息」里给的姓名、时间、课程可以直接用在文档抬头；\n"
    "6. 只输出文档正文本身，不要解释、不要寒暄、不要用 Markdown 的 # 或 ** 或代码围栏，\n"
    "   栏目名一律用【】包住；\n"
    "7. 如果还给了【上课文件】（讲义/课件/试卷的正文），它只是**参考材料**，用来核实\n"
    "   本次讲了哪些章节、布置了什么作业、有哪些易错点；\n"
    "   绝**不能**从材料里推断学生的表现、态度、分数或评价 —— 那些只能来自【原始记录】；\n"
    "   材料里与该学生无关的东西（例题答案、其他章节、页眉页脚、版权页）不要写进正文。"
)


# ---------------------------------------------------------------- 服务商预设
# 「地址填哪个、模型叫什么」是接第三方接口最容易错的地方。两个真实踩法：
#   · 把**控制台网址**当 API 地址填（platform.deepseek.com 是看用量/充值的页面，
#     不是接口地址）—— 结果是连不上或 404；
#   · 模型名记成别的产品 —— 结果是 model not found。
# 所以这里把常见服务商的默认值备好，选一下自动填上。
#
# ⚠️ 预设只是「起点」：模型名会随服务商上新而过时，输入框始终可改。
#    真填错了会收到一条明确的报错，改掉即可，不影响其他功能。
PROVIDERS = [
    {
        "id": "deepseek",
        "name": "DeepSeek（便宜、中文好）",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "润色这种活 deepseek-chat 就够，价格便宜。deepseek-reasoner 更贵、"
                "擅长推理题，润色用不上。地址填 https://api.deepseek.com 即可，"
                "别填 platform.deepseek.com（那是控制台网页，不是接口）。",
        "keys_url": "https://platform.deepseek.com/api_keys",
    },
    {
        "id": "ollama",
        "name": "本机 Ollama（完全离线，隐私最好）",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "models": ["qwen2.5:7b", "qwen2.5:3b", "llama3.1:8b"],
        "note": "内容一步都不出本机，最符合「数据存本机」的定位，也免费。"
                "前提是本机已装好 Ollama 并拉取了模型（如 ollama pull qwen2.5:7b）。"
                "Ollama 不校验密钥，API 密钥随便填几个字符即可（但不能不填）。"
                "本机 6GB 显存跑 7B 模型偏紧，慢但能用。",
        "keys_url": "",
        "key_placeholder": "ollama（随便填，不校验）",
    },
    {
        "id": "dashscope",
        "name": "阿里百炼 · 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max"],
        "note": "必须用兼容模式这个地址（结尾 /compatible-mode/v1），"
                "不是阿里云控制台的地址。qwen-turbo 最便宜。",
        "keys_url": "https://bailian.console.aliyun.com/",
    },
    {
        "id": "moonshot",
        "name": "月之暗面 · Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
        "note": "长文本是它的强项；润色这么短的文本用 8k 版本就够。",
        "keys_url": "https://platform.moonshot.cn/console/api-keys",
    },
    {
        "id": "zhipu",
        "name": "智谱 · GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus"],
        "note": "glm-4-flash 通常是最便宜的档位，适合润色这种轻任务。",
        "keys_url": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    {
        "id": "siliconflow",
        "name": "硅基流动（聚合多家模型）",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "note": "一个号能切很多开源模型，有免费额度的型号。模型名要带厂商前缀。",
        "keys_url": "https://cloud.siliconflow.cn/account/ak",
    },
    {
        "id": "openai",
        "name": "OpenAI（需自行解决网络）",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "note": "国内直连通常不通，除非本机网络已能访问。",
        "keys_url": "https://platform.openai.com/api-keys",
    },
]


def providers() -> list[dict]:
    """给设置页的服务商预设。纯静态，**不含任何密钥**，可以放心返回。"""
    return PROVIDERS


class AiError(RuntimeError):
    """AI 调用失败（配置缺失、网络、协议、返回格式等）。消息可直接给用户看。"""


# ---------------------------------------------------------------- 配置
def _row(db: Session) -> AiSetting | None:
    return db.scalar(select(AiSetting).where(AiSetting.owner_id == config.OWNER_ID))


def get_config(db: Session) -> dict:
    """读配置。环境变量优先 —— 不想把 key 落库的人可以只设 SHIKE_AI_API_KEY。

    环境变量名：SHIKE_AI_BASE_URL / SHIKE_AI_MODEL / SHIKE_AI_API_KEY / SHIKE_AI_TIMEOUT
    """
    row = _row(db)
    cfg = {
        "base_url": (row.base_url if row else "") or "",
        "model": (row.model if row else "") or "",
        "api_key": (row.api_key if row else "") or "",
        "timeout": (row.timeout if row else 60) or 60,
        "from_env": [],
    }
    for env_key, field in (("SHIKE_AI_BASE_URL", "base_url"),
                           ("SHIKE_AI_MODEL", "model"),
                           ("SHIKE_AI_API_KEY", "api_key"),
                           ("SHIKE_AI_TIMEOUT", "timeout")):
        val = os.getenv(env_key)
        if val:
            if field == "timeout":
                try:
                    cfg[field] = int(val)
                except ValueError:
                    continue
            else:
                cfg[field] = val
            cfg["from_env"].append(env_key)
    cfg["configured"] = bool(cfg["base_url"] and cfg["model"] and cfg["api_key"])
    return cfg


def masked(cfg: dict) -> dict:
    """对外返回的形态：**绝不回传完整 key**。只给「有没有」和末 4 位，够辨认就够了。"""
    key = cfg.get("api_key") or ""
    return {
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "timeout": cfg.get("timeout") or 60,
        "has_key": bool(key),
        "key_hint": f"…{key[-4:]}" if len(key) >= 8 else ("已设置" if key else ""),
        "configured": bool(cfg.get("configured")),
        "from_env": cfg.get("from_env") or [],
    }


def save_config(db: Session, payload, keep_key_when_empty: bool = True) -> dict:
    row = _row(db)
    if row is None:
        row = AiSetting(owner_id=config.OWNER_ID)
        db.add(row)
    data = payload.model_dump(exclude_unset=True)
    if data.get("clear_key"):
        # 显式的「清掉密钥」：空串只能表示「不改」，区分不了删除意图
        row.api_key = ""
    if "base_url" in data and data["base_url"] is not None:
        row.base_url = data["base_url"].strip()
    if "model" in data and data["model"] is not None:
        row.model = data["model"].strip()
    if "timeout" in data and data["timeout"] is not None:
        row.timeout = int(data["timeout"])
    if "api_key" in data and data["api_key"] is not None and not data.get("clear_key"):
        new_key = data["api_key"].strip()
        # 空串 = 不改（前端拿到的是脱敏值，回填会把真 key 覆盖成 "…abcd"）
        if new_key or not keep_key_when_empty:
            row.api_key = new_key
    db.commit()
    return masked(get_config(db))


def _chat_url(base_url: str) -> str:
    """把用户填的地址补全成 chat/completions。

    允许填 `https://api.deepseek.com`、`.../v1`、甚至完整的 `.../v1/chat/completions`，
    这里统一补齐 —— 这是接第三方接口最容易填错的一处，不该让用户去猜。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise AiError("还没有配置 AI 接口地址，请到「设置 → AI 润色」里填写")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"


def _post_json(url: str, body: dict, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise AiError(f"AI 接口返回 {e.code}：{detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise AiError(f"连不上 AI 接口（{e.reason}）。请检查地址与网络。") from e
    except TimeoutError as e:
        raise AiError(f"AI 接口超时（{timeout}s）。可以到设置里调大超时时间。") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise AiError(f"AI 接口返回的不是 JSON：{raw[:200]}") from e


def _extract_content(data: dict) -> str:
    """从响应里取正文。兼容 OpenAI 风格与少数只回 content 字符串的实现。"""
    try:
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        content = msg.get("content")
        if isinstance(content, list):      # 有些实现回的是分段数组
            content = "".join(
                (c.get("text") or "") if isinstance(c, dict) else str(c) for c in content
            )
        if content:
            return str(content)
    except (AttributeError, IndexError, TypeError):
        pass
    raise AiError(f"AI 接口响应里没有找到正文：{json.dumps(data, ensure_ascii=False)[:200]}")


def assemble_draft(fields: dict, context: dict, draft: str | None = None) -> str:
    """把四段记录 + 课程信息拼成一篇「原始记录」，交给 AI 整理。

    刻意**不**在这里排成模板的样子：怎么排是 AI 的活（它才读得懂模板要什么）。
    这里只负责把信息如实、完整地传过去，并标明哪一段是什么 ——
    少标一个标签，模型就可能把「作业布置」当成「课堂表现」混进正文里。

    给了 draft（老师自己写的整篇草稿）就优先用它：那种情况下再拼四段是多余的。
    """
    if draft and draft.strip():
        return draft.strip()

    filled = {k: (fields.get(k) or "").strip() for k in FIELDS}
    filled = {k: v for k, v in filled.items() if v}
    if not filled:
        # 老师一个字都没写：返回空串，让调用方给出「没内容可润色」的提示。
        # ⚠️ 不能只判断「拼出来的字符串是不是空」—— 课程信息那几行永远非空，
        #    那样就会把一篇什么都没有的记录发出去，模型除了编没有别的办法。
        return ""

    lines: list[str] = []
    ctx_lines = [f"{k}：{v}" for k, v in (context or {}).items() if v]
    if ctx_lines:
        lines.append("## 课程信息")
        lines.extend(ctx_lines)
        lines.append("")
    lines.append("## 老师填写的记录（分段、尚未整理）")
    for key in FIELDS:
        if key not in filled:
            continue
        lines.append(f"〔{FIELD_CN[key]}〕")
        lines.append(filled[key])
        lines.append("")
    return "\n".join(lines).strip()


def polish_document(
    db: Session,
    draft: str,
    template_content: str | None = None,
    style: str | None = None,
    materials: str | None = None,
    timeout_cap: int | None = None,
) -> dict:
    """整篇润色：一篇原始记录进去，一篇正式文档出来。

    输出是**纯文本**而不是 JSON —— 以前按字段返回 JSON，是因为结果要回填四个输入框；
    现在结果是整篇文章（栏目数由模板决定，可能七八个），硬塞进固定 JSON 结构只会
    逼模型裁剪内容。所以让模型直接写文档，前端拿去给老师过目、编辑、采用。
    """
    cfg = get_config(db)
    if not cfg["configured"]:
        raise AiError("还没有配置 AI 接口（地址 / 模型 / 密钥），请到「设置 → AI 润色」里填写")
    if not (draft or "").strip():
        raise AiError("没有需要润色的内容 —— 四个字段都是空的")

    parts: list[str] = []
    if (template_content or "").strip():
        parts.append(
            "## 模板（请仿照它的结构、栏目顺序、语气与详略；模板里的示例只是文风参考，"
            "其中的姓名、章节、分数都不得出现在结果里）\n" + template_content.strip()
        )
    else:
        parts.append(
            "## 模板\n（老师没有指定模板。请用【课堂表现】【存在问题】【作业布置】"
            "【下次安排】四个栏目组织，简洁、面向家长，200 字以内。）"
        )
    parts.append("## 原始记录（请整理成上面模板的样子）\n" + draft.strip())
    if (materials or "").strip():
        # 材料放在原始记录**之后**：先让学生情况占住位置，材料只当核实用的旁证
        parts.append(
            "## 上课文件（参考资料：用于核实讲了什么、布置了什么作业、有哪些易错点。\n"
            "再次强调：里面没有任何学生表现信息，不得据此推断）\n" + materials.strip()
        )
    if (style or "").strip():
        parts.append("## 额外要求\n" + style.strip())

    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": DOC_SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        "temperature": 0.3,          # 润色要稳，不要发挥
        "stream": False,
    }
    data = _post_json(
        _chat_url(cfg["base_url"]),
        body,
        {"Authorization": f"Bearer {cfg['api_key']}"},
        int(timeout_cap or cfg["timeout"]),
    )
    text = _extract_content(data).strip()
    if text.startswith("```"):        # 有些模型习惯包个代码块，剥掉
        text = text.split("\n", 1)[-1] if "\n" in text else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    if not text:
        raise AiError("模型返回了空内容，可以换个模型或稍后再试")
    usage = data.get("usage") or {}
    return {
        "text": text,
        # 顺带回报用量：让老师知道这次花了多少 token（自付费用的服务会关心）
        "usage": {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")},
        "model": cfg["model"],
    }


def test_connection(db: Session, override: dict | None = None) -> dict:
    """设置页的「测试连接」：发一句最短的请求，确认地址/密钥/模型都对。

    override 是**表单里还没保存**的值。用户填完就想先试一下是自然的，
    不该强迫他「先保存才能测」—— 那样他会看到「还没填全」而莫名其妙（真实反馈）。
    api_key 留空则沿用已保存的那把（界面从不回填明文，所以空≠没填）。
    """
    cfg = get_config(db)
    for key in ("base_url", "model", "api_key", "timeout"):
        val = (override or {}).get(key)
        if val not in (None, ""):
            cfg[key] = int(val) if key == "timeout" else str(val).strip()
    if not (cfg["base_url"] and cfg["model"] and cfg["api_key"]):
        raise AiError("地址 / 模型 / 密钥还没填全，先填完再测试")
    data = _post_json(
        _chat_url(cfg["base_url"]),
        {
            "model": cfg["model"],
            "messages": [{"role": "user", "content": "回复两个字：正常"}],
            "temperature": 0,
            "stream": False,
        },
        {"Authorization": f"Bearer {cfg['api_key']}"},
        min(30, int(cfg["timeout"])),
    )
    return {"ok": True, "reply": _extract_content(data)[:80], "model": cfg["model"]}
