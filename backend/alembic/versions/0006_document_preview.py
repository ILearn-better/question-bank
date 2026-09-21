"""documents 增加 Word 页面视图字段：preview_pdf / preview_error / preview_pages

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-20

背景：录题要支持「自己在页面上画框选区」（文本选择 / 图片选择），
这要求 Word 文档也能渲染成带版式的页面图 —— 做法是用本机 Word
把 .docx 导出成 PDF，PDF 作为页面视图的数据源。

为什么把「转换结果」入库而不是每次去猜文件在不在：
  1. 转换要启动 Word，代价高（秒级）。失败原因必须记住，
     否则每次打开这个文档都要白等一次超时。
  2. 列表页要能一眼看出哪些 Word 还没有页面图。
三个字段都是纯 nullable 普通列，不涉及约束，SQLite 直接 ADD COLUMN 即可。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("preview_pdf", sa.Text()))      # 相对 DATA_DIR
    op.add_column("documents", sa.Column("preview_error", sa.Text()))
    op.add_column("documents", sa.Column("preview_pages", sa.Integer()))


def downgrade() -> None:
    op.drop_column("documents", "preview_pages")
    op.drop_column("documents", "preview_error")
    op.drop_column("documents", "preview_pdf")
