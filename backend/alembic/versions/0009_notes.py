"""notes 表：笔记（Markdown 正文 + 板书笔画）

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-24

一张表装完：正文 content 是 Markdown 文本，ink 是笔画数组的 JSON。
笔画不渲染成图片存，因为橡皮/换色/调粗细/撤销都要能重画，
而且矢量数据小、换窗口大小不糊。详见 models.Note 的注释。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    op.create_table(
        "notes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("title", sa.String(), nullable=False, server_default="未命名笔记"),
        sa.Column("content", sa.Text(), server_default=""),
        sa.Column("ink", sa.Text(), server_default="[]"),
        sa.Column("pinned", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
        sa.Column("updated_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_notes_updated", "notes", ["updated_at"])


def downgrade() -> None:
    op.drop_index("idx_notes_updated", table_name="notes")
    op.drop_table("notes")
