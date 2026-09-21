"""questions 增加 answer_image：答案也能配图

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20

背景：有些题的答案是图而不是式子 —— 几何题的解答图、手写推导拍的照片。
原来只有题干有 image 字段，答案只能存文字。

存的是 URL（/api/crops/xxx.png），和题干的 image 完全同一种形态：
图片文件落在 DATA_DIR/uploads/crops/ 下，库里只存 URL，换机器不失效。
纯 nullable 普通列，SQLite 直接 ADD COLUMN 即可。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("answer_image", sa.Text()))


def downgrade() -> None:
    op.drop_column("questions", "answer_image")
