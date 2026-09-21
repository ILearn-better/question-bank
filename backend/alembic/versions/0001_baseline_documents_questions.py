"""baseline：登记既有的 documents / questions 两张表

Revision ID: 0001
Revises:
Create Date: 2026-09-20

这一步不改动任何数据，只是给「现状」编上一个版本号。
已有的库请用 `alembic stamp 0001` 标记为已在此版本，不要执行 upgrade。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("filename", sa.String()),
        sa.Column("filetype", sa.String()),
        sa.Column("block_count", sa.Integer()),
        sa.Column("blocks", sa.Text()),
        sa.Column("created_at", sa.Text()),
        sa.Column("file_path", sa.Text()),
        sa.Column("scanned", sa.Integer(), server_default="0"),
    )
    op.create_table(
        "questions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("document_id", sa.String()),
        sa.Column("doc_filename", sa.String()),
        sa.Column("start_block", sa.Integer(), server_default="-1"),
        sa.Column("end_block", sa.Integer(), server_default="-1"),
        sa.Column("content", sa.Text()),
        sa.Column("qtype", sa.String()),
        sa.Column("difficulty", sa.String()),
        sa.Column("knowledge_points", sa.Text(), server_default="[]"),
        sa.Column("answer", sa.Text()),
        sa.Column("analysis", sa.Text()),
        sa.Column("created_at", sa.Text()),
        sa.Column("image", sa.Text(), server_default=""),
    )


def downgrade() -> None:
    op.drop_table("questions")
    op.drop_table("documents")
