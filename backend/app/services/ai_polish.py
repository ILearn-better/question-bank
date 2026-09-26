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

SYSTEM_PROMPT = (
    "你是一位经验丰富的中学数学老师，正在把课后反馈整理成发给家长看的文字。\n"
    "要求：\n"
    "1. 只做语言润色：把口语、零碎、重复的表达整理通顺，不要改变任何事实；\n"
    "2. 绝不编造学生没做过的事、没考过的分数、没布置的作业；原文没提的信息一律不补；\n"
    "3. 保持原有的分条/分段结构，原文是列点的仍然列点；\n"
    "4. 语气专业、具体、对家长友好，不空泛地说「很好」「要加油」；\n"
    "5. 只输出 JSON，不要任何解释文字。"
)


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


def _parse_json_block(text: str) -> dict:
    """模型经常把 JSON 包在 ```json 围栏里，或者前后带一句解释 —— 都要能剥出来。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        s = s[start:end + 1]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        raise AiError(f"模型返回的内容不是合法 JSON（{e.msg}）：{text[:200]}") from e
    if not isinstance(obj, dict):
        raise AiError(f"模型返回的 JSON 不是对象：{text[:200]}")
    return obj


def polish(db: Session, fields: dict, context: dict, style: str | None = None) -> dict:
    """润色。只处理 FIELDS 里出现且有内容的字段，返回同样键名的结果。"""
    cfg = get_config(db)
    if not cfg["configured"]:
        raise AiError("还没有配置 AI 接口（地址 / 模型 / 密钥），请到「设置 → AI 润色」里填写")

    payload_fields = {k: (v or "").strip() for k, v in (fields or {}).items() if k in FIELDS}
    payload_fields = {k: v for k, v in payload_fields.items() if v}
    if not payload_fields:
        raise AiError("没有需要润色的内容 —— 四个字段都是空的")

    ctx_lines = [f"- {k}：{v}" for k, v in (context or {}).items() if v]
    body_user = [
        "## 本节背景",
        "\n".join(ctx_lines) if ctx_lines else "（无）",
        "",
        "## 待润色的反馈（原样保留段落结构）",
    ]
    for k, v in payload_fields.items():
        body_user.append(f"### {FIELD_CN[k]}\n{v}")
    body_user += [
        "",
        "## 输出要求",
        "返回一个 JSON 对象，键固定为：" + "、".join(FIELD_CN[k] + f"（{k}）" for k in payload_fields),
        "只包含上面给出的这几段，不要新增段落，不要输出解释。",
    ]
    if style:
        body_user.append(f"## 风格要求\n{style.strip()}")

    body = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(body_user)},
        ],
        "temperature": 0.3,          # 润色要稳，不要发挥
        "stream": False,
    }
    data = _post_json(
        _chat_url(cfg["base_url"]),
        body,
        {"Authorization": f"Bearer {cfg['api_key']}"},
        int(cfg["timeout"]),
    )
    obj = _parse_json_block(_extract_content(data))
    out = {}
    for k in payload_fields:
        val = obj.get(k)
        if val is None:
            val = obj.get(FIELD_CN[k])        # 少数模型会回中文键
        if isinstance(val, list):
            val = "\n".join(str(x) for x in val)
        out[k] = str(val).strip() if val else ""
    usage = data.get("usage") or {}
    return {
        "fields": out,
        # 顺带回报用量：让老师知道这次花了多少 token（自付费用的服务会关心）
        "usage": {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")},
        "model": cfg["model"],
    }


def test_connection(db: Session) -> dict:
    """设置页的「测试连接」：发一句最短的请求，确认地址/密钥/模型都对。"""
    cfg = get_config(db)
    if not cfg["configured"]:
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
