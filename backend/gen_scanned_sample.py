# -*- coding: utf-8 -*-
"""生成扫描版样例 PDF：把普通样例卷每页渲染成图片，再拼成纯图片 PDF（无文字层）。"""
import os

import pymupdf

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(BASE), "samples", "高二数学专项测试卷.pdf")
OUT = os.path.join(os.path.dirname(BASE), "samples", "扫描版样例卷.pdf")

doc = pymupdf.open(SRC)
out = pymupdf.open()
for page in doc:
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
    img = pix.tobytes("png")
    np = out.new_page(width=page.rect.width, height=page.rect.height)
    np.insert_image(np.rect, stream=img)
out.save(OUT)
print("saved:", OUT)

# 验证无文字层
check = pymupdf.open(OUT)
total_text = sum(len(p.get_text().strip()) for p in check)
print("text chars:", total_text, "| pages:", check.page_count)
