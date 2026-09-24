# -*- coding: utf-8 -*-
"""图片落盘的小工具 —— 答案配图与笔记配图共用。

⚠️ 只认魔数、**不信任扩展名**：按扩展名判断的话，把任意文件改名成 .png 就能存进来，
而读取接口又按扩展名猜 MIME 原样发出去 —— 等于给本机开了个任意文件托管。
这段逻辑是安全相关，所以只留一份，不让两个路由各写各的。
"""
from __future__ import annotations

import uuid
from pathlib import Path

MAX_IMAGE_BYTES = 20 * 1024 * 1024

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def sniff_ext(head: bytes) -> str | None:
    """按文件头判断真实格式，返回扩展名；认不出来返回 None。"""
    for sig, ext in _SIGNATURES:
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":     # WEBP: RIFF....WEBP
        return ".webp"
    return None


class ImageRejected(ValueError):
    """图片不合格（空文件 / 太大 / 根本不是图片）。消息可以直接给用户看。"""


def save_image(data: bytes, dest_dir: Path, prefix: str) -> dict:
    """校验并落盘，返回 {name, size_bytes}。文件名由服务端生成，不用原始文件名。"""
    if not data:
        raise ImageRejected("文件是空的")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected(f"图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")
    ext = sniff_ext(data[:16])
    if ext is None:
        raise ImageRejected("这不是可识别的图片（支持 PNG / JPG / GIF / WEBP / BMP）")

    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}_{uuid.uuid4().hex[:12]}{ext}"
    (dest_dir / name).write_bytes(data)
    return {"name": name, "size_bytes": len(data)}
