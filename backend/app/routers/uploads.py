# -*- coding: utf-8 -*-
"""图片上传 与 「已有截图」列表 —— 只服务「给已录题目补答案」这条路。

⚠️ 为什么单独一个路由，而不是把上传塞回录题页：
   录题页刻意**没有**「选图片插入」—— 那是明确要求去掉的。
   理由是题干必须来自试卷本身的框选/行标记，随手插图会让题库质量不可控。
   但「补答案」是另一个场景：这时没有「当前试卷」可圈（答案可能在另一份答案文档里，
   也可能就是电脑里的一张截图），所以必须允许从外部拿图。
   结论：能力收在这个路由里，只被补答案界面调用，录题页保持干净。

存哪：CROPS_DIR。这样与题干 image / answer_image 完全同一种形态（库里只存
/api/crops/xxx.png 这类 URL），于是：
  · 现成的 GET /api/crops/{name} 直接就能服务，不用再加一条静态路由
  · 删题目时的「孤儿截图清理」自动把这些图算进去，不用写第二套清理逻辑
文件名加 up_ 前缀，与框选生成的 12 位十六进制名区分开，便于排查。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from .. import config

router = APIRouter(prefix="/api", tags=["uploads"])

MAX_IMAGE_BYTES = 20 * 1024 * 1024
LIST_LIMIT = 300

# 只认魔数，不信任扩展名：若按扩展名判断，把任意文件改名成 .png 就能存进来，
# 而 GET /api/crops/{name} 会按扩展名猜 MIME 把它原样发出去。
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)


def _sniff_ext(head: bytes) -> str | None:
    """按文件头判断真实格式，返回扩展名；认不出来返回 None。"""
    for sig, ext in _SIGNATURES:
        if head.startswith(sig):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":     # WEBP: RIFF....WEBP
        return ".webp"
    return None


@router.post("/uploads/image")
async def upload_image(file: UploadFile = File(...)):
    """上传一张本地图片，返回可直接当 image / answer_image 用的 URL。"""
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise HTTPException(422, "文件是空的")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"图片不能超过 {MAX_IMAGE_BYTES // 1024 // 1024} MB")

    ext = _sniff_ext(data[:16])
    if ext is None:
        raise HTTPException(422, "这不是可识别的图片（支持 PNG / JPG / GIF / WEBP / BMP）")

    config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"up_{uuid.uuid4().hex[:12]}{ext}"
    (config.CROPS_DIR / name).write_bytes(data)
    return {"url": f"/api/crops/{name}", "name": name, "size_bytes": len(data)}


@router.get("/uploads/images")
def list_images(limit: int = Query(LIST_LIMIT, ge=1, le=1000)):
    """列出 crops 目录里已有的图（新的在前），供「从已有截图里选」用。"""
    if not config.CROPS_DIR.exists():
        return {"items": [], "total": 0}
    files = [p for p in config.CROPS_DIR.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    items = []
    for p in files[:limit]:
        st = p.stat()
        items.append({
            "name": p.name,
            "url": f"/api/crops/{p.name}",
            "size_bytes": st.st_size,
            "created_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            # up_ 前缀 = 从电脑上传的，否则是页面上框选出来的
            "uploaded": p.name.startswith("up_"),
        })
    return {"items": items, "total": len(files)}
