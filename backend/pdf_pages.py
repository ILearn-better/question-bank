# -*- coding: utf-8 -*-
"""PDF 页面服务（兼容层）。

实现已迁移到 `app/adapters/pdf.py`，这里只做转发，
让仓库里既有的调试脚本（test_visual.py / test_scanned.py）继续可用。
**新代码请直接 `from app.adapters import pdf`。**
"""
from app.adapters.pdf import (  # noqa: F401
    crop_region,
    crop_regions,
    page_count,
    page_lines,
    render_page,
)
