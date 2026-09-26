"""反馈整篇正文 + 润色模板

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-26

两件事：
  1. feedbacks 加 doc —— 「整篇正文」。四段字段（performance/problems/homework/next_plan）
     仍然是老师按下手就写的速记原料；doc 是整理成文、可以直接发给家长的成品。
     两者并存，导出时二选一（默认有整篇就用整篇）。
  2. feedback_doc_templates —— 润色时「仿照的模板」。和 feedback_templates 分开建表，因为
     两者形状完全不同：那边是按字段分的快捷短语，这边是一整篇文档的格式与文风参考。

doc 用 server_default='' 回填：老反馈读出来是空串而不是 None，省得每处都判空。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    # ---- 1) 整篇正文
    op.add_column(
        "feedbacks",
        sa.Column("doc", sa.Text(), nullable=False, server_default=""),
    )

    # ---- 2) 润色模板（仿照的格式与文风）
    op.create_table(
        "feedback_doc_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), server_default=""),
        sa.Column("is_builtin", sa.Integer(), server_default="0"),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index(
        "idx_fb_doc_templates_owner", "feedback_doc_templates", ["owner_id", "sort_order"]
    )


def downgrade() -> None:
    op.drop_index("idx_fb_doc_templates_owner", table_name="feedback_doc_templates")
    op.drop_table("feedback_doc_templates")
    op.drop_column("feedbacks", "doc")
