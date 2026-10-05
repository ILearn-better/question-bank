# -*- coding: utf-8 -*-
"""AI 能力的接口：配置、连通性测试、以及公式/题干识别。

为什么 AI 的接口一度在 `feedbacks.py` 里：
    这个能力最初是为「课后反馈润色」做的，就顺手挂在了反馈路由下。
    后来它长出了第二个用途（公式识别，属于题库而不是反馈），
    两个业务挤在一个文件里，改任何一个都要先跨过另一个 —— 所以拆出来。
    **接口路径没有变**（还是 /api/ai/*），前端一行都不用改。

配置只有一份，识别与润色共用：
    `ai_settings` 单行记录。deepseek-flash 实测既能润色又能识图，
    所以没有「润色用 A 家、识图用 B 家」的分叉 —— 真需要时再加，
    现在加只会让设置页多出一半要填的框。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import AiSettingIn, AiTestIn, RecognizeIn
from ..services import ai_polish, vision

router = APIRouter(prefix="/api", tags=["ai"])


# ---------------------------------------------------------------- 配置
@router.get("/ai/providers")
def get_ai_providers():
    """服务商预设（地址 / 模型名 / 申请密钥的入口）。纯静态、无密钥。"""
    return ai_polish.providers()


@router.get("/ai/settings")
def get_ai_settings(db: Session = Depends(get_db)):
    return ai_polish.masked(ai_polish.get_config(db))


@router.put("/ai/settings")
def put_ai_settings(payload: AiSettingIn, db: Session = Depends(get_db)):
    """保存配置。api_key 传空串表示「不改」—— 前端拿到的是脱敏值，回填会把真 key 冲掉。"""
    return ai_polish.save_config(db, payload)


@router.post("/ai/test")
def test_ai(payload: Optional[AiTestIn] = None, db: Session = Depends(get_db)):
    """测试连接。可以带上还没保存的表单值 —— 填完就能试，不必先保存。"""
    try:
        return ai_polish.test_connection(db, payload.model_dump() if payload else None)
    except ai_polish.AiError as e:
        raise HTTPException(502, str(e)) from e


# ---------------------------------------------------------------- 识别
@router.post("/ai/recognize")
def recognize_image(payload: RecognizeIn, db: Session = Depends(get_db)):
    """把一张图识别成带 $LaTeX$ 的文本。

    返回的是**候选**，不是最终结果：前端必须让老师过目、可编辑、确认后才写进题干。
    这一点和润色是同一条原则 —— 识别偶尔会看错一个手写字母或正负号，
    悄悄替他决定了，错误就会一路带到印出来的卷子上。

    ⚠️ 会联网把这张图发到配置的 AI 服务商。必须由老师主动点按钮触发。
    """
    try:
        return vision.recognize(
            db,
            payload.image,
            mode=payload.mode or "question",
            hint=payload.hint or "",
        )
    except ai_polish.AiError as e:
        # VisionError 继承自 AiError，所以超时 / 空正文 / 参数错误都在这里统一成 502：
        # 对前端来说「AI 那边出问题了」，和「你请求写错了」是两类，
        # 前者应该提示重试，后者才是要改代码的。
        raise HTTPException(502, str(e)) from e
