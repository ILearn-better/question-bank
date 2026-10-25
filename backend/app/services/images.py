# -*- coding: utf-8 -*-
"""图片落盘的小工具 —— 答案配图与笔记配图共用。

⚠️ 只认魔数、**不信任扩展名**：按扩展名判断的话，把任意文件改名成 .png 就能存进来，
而读取接口又按扩展名猜 MIME 原样发出去 —— 等于给本机开了个任意文件托管。
这段逻辑是安全相关，所以只留一份，不让两个路由各写各的。
"""
from __future__ import annotations

import io
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


def save_image(data: bytes, dest_dir: Path, prefix: str = "img", stem: str | None = None) -> dict:
    """校验并落盘，返回 {name, size_bytes}。文件名由服务端生成，不用原始文件名。

    stem 给了就用它当主名（不含扩展名），**扩展名仍然按魔数决定** ——
    这是安全边界，不能因为调用方说"这是 png"就信。
    反馈配图会传 `学生_日期` 当 stem：图片脱离归档目录后（被转发、被下载）
    还得能自证是谁的、哪天的；重名自动加 _2、_3。
    """
    if not data:
        raise ImageRejected("文件是空的")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected(f"图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")
    ext = sniff_ext(data[:16])
    if ext is None:
        raise ImageRejected("这不是可识别的图片（支持 PNG / JPG / GIF / WEBP / BMP）")

    dest_dir.mkdir(parents=True, exist_ok=True)
    base = (stem or f"{prefix}_{uuid.uuid4().hex[:12]}").strip() or f"{prefix}_{uuid.uuid4().hex[:12]}"
    name, n = f"{base}{ext}", 1
    while (dest_dir / name).exists():          # 同一天传两张截图是完全正常的
        n += 1
        name = f"{base}_{n}{ext}"
    (dest_dir / name).write_bytes(data)
    return {"name": name, "size_bytes": len(data)}


# 拼接后的总高度上限。超过就整体等比缩小 —— 
# 一张 60000px 高的图浏览器的 <img> 也扛不住，而且识别时本来就会压到长边 1600。
STITCH_MAX_H = 16000


def stitch_vertical(blobs: list[bytes], dest_dir: Path, gap: int = 0,
                    prefix: str = "stitch") -> dict:
    """多张图**竖着拼成一张** —— 一道题的答案跨页时用（p10 下半页 + p11 上半页）。

    为什么在服务端拼而不是前端 canvas 拼：
        前端拼图要额外处理 EXIF 方向、色彩空间、超大画布的浏览器上限（Safari 是
        4096px 见方），每个浏览器还不一样。服务端只有一份 Pillow 行为。

    宽度取**最宽那张**，窄的居中、白底补齐。**不拉伸** —— 拉伸会让公式变形，
    而这张图是老师要看着核对的。

    ⚠️ 拼接结果仍然走 `save_image` 落盘：魔数校验和大小上限必须共用一份，
       否则这里就成了「绕过校验往 crops 目录写任意文件」的后门。

    @returns {{name, width, height, scaled}}  scaled=True 表示因为太高而等比缩过
    """
    from PIL import Image

    if not blobs:
        raise ImageRejected("没有图可以拼")
    if len(blobs) > 20:
        raise ImageRejected("一次最多拼 20 张（再多就不是「跨页」而是「整本」了）")

    ims = []
    for i, b in enumerate(blobs, 1):
        if not b:
            raise ImageRejected(f"第 {i} 张图是空的")
        if len(b) > MAX_IMAGE_BYTES:
            raise ImageRejected(f"第 {i} 张图超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")
        if sniff_ext(b[:16]) is None:
            raise ImageRejected(f"第 {i} 张不是可识别的图片")
        try:
            im = Image.open(io.BytesIO(b))
            im.load()
        except Exception as e:                     # noqa: BLE001  PIL 异常类型很杂
            raise ImageRejected(f"第 {i} 张图读不出来") from e
        ims.append(im.convert("RGB"))              # JPEG 不吃 alpha / 调色板

    gap = max(0, min(int(gap), 60))
    W = max(im.width for im in ims)
    total_h = sum(im.height for im in ims) + gap * (len(ims) - 1)

    scaled = False
    if total_h > STITCH_MAX_H:
        # 等比缩到上限内。只缩不改宽高比：答案里的公式一旦被压扁就会看错。
        k = STITCH_MAX_H / float(total_h)
        ims = [im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                         Image.LANCZOS) for im in ims]
        W = max(im.width for im in ims)
        total_h = sum(im.height for im in ims) + gap * (len(ims) - 1)
        scaled = True

    canvas = Image.new("RGB", (W, total_h), "white")
    y = 0
    for im in ims:
        canvas.paste(im, ((W - im.width) // 2, y))   # 居中，两侧留白
        y += im.height + gap

    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    info = save_image(buf.getvalue(), dest_dir, prefix=prefix)
    return {"name": info["name"], "width": W, "height": total_h, "scaled": scaled}
