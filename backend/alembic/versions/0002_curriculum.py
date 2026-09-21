"""M0 共享内核：curricula / nodes / resources

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-20

多体系是一等公民：新增体系只插数据，不改代码。
nodes 承载知识点树（替代原来的单体系 JSON 文件），resources 归档教材与资料。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    op.create_table(
        "curricula",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("region", sa.String()),
        sa.Column("stage", sa.String()),
        sa.Column("subject", sa.String(), nullable=False, server_default="math"),
        sa.Column("color", sa.String()),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_table(
        "nodes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("curriculum_id", sa.Integer(), sa.ForeignKey("curricula.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("nodes.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("code", sa.String()),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_nodes_curr", "nodes", ["curriculum_id", "level"])
    op.create_index("idx_nodes_parent", "nodes", ["parent_id"])

    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("curriculum_id", sa.Integer(), sa.ForeignKey("curricula.id")),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("nodes.id")),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("file_path", sa.Text()),
        sa.Column("doc_id", sa.String()),
        sa.Column("file_size", sa.Integer()),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_res_curr_kind", "resources", ["curriculum_id", "kind"])


def downgrade() -> None:
    op.drop_table("resources")
    op.drop_index("idx_nodes_parent", table_name="nodes")
    op.drop_index("idx_nodes_curr", table_name="nodes")
    op.drop_table("nodes")
    op.drop_table("curricula")
