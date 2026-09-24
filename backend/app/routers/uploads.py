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

from datetime import datetime

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from .. import config
from ..services import images

router = APIRouter(prefix="/api", tags=["uploads"])

LIST_LIMIT = 300


@router.post("/uploads/image")
async def upload_image(file: UploadFile = File(...)):
    """上传一张本地图片，返回可直接当 image / answer_image 用的 URL。"""
    data = await file.read(images.MAX_IMAGE_BYTES + 1)
    try:
        saved = images.save_image(data, config.CROPS_DIR, prefix="up")
    except images.ImageRejected as e:
        raise HTTPException(422, str(e)) from e
    return {"url": f"/api/crops/{saved['name']}", **saved}


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
