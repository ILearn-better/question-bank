"""questions 增加 tags：自由标签，出卷时可按标签选题

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-24

为什么存 JSON 数组而不是建一张标签表：
  标签是「随手打的分类」—— 没有层级、没有权重，出卷筛选只需要「任一命中」。
  跟 knowledge_points 一样存 TEXT，SQLite 直接 ADD COLUMN 即可。
  将来真要做标签重命名 / 合并 / 统计，再升级成表，数据迁移也不难。

默认值给 '[]'：老数据的 tags 若留成 NULL，前端解析会报错，
所以既设 server_default，也跟着回填一次。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("tags", sa.Text(), server_default="[]"))
    # 已有的行显式回填一份空数组，别把 NULL 留给前端
    op.execute("UPDATE questions SET tags = '[]' WHERE tags IS NULL")


def downgrade() -> None:
    op.drop_column("questions", "tags")
