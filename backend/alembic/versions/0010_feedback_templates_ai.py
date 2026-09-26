"""反馈增强：配图字段 + 反馈模板表 + AI 配置表

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-26

三件事：
  1. feedbacks 加 images —— 配图 URL 的 JSON 数组（和 questions.tags / notes.ink 同一套路数：
     图片没有额外属性、也不参与查询，建关联表是过度设计）
  2. feedback_templates —— 反馈模板存数据库而不是写死在代码里，
     每个老师行文习惯差很多，写死等于逼所有人用同一个腔调
  3. ai_settings —— AI 润色走 OpenAI 兼容的 /chat/completions，
     所以只需 base_url + model + api_key，不用为每家写适配器

注意 images 用 server_default='[]' 回填：老反馈读出来就是空数组，
而不是 None —— 省得每个读取处都要判空。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    # ---- 1) 反馈配图
    op.add_column(
        "feedbacks",
        sa.Column("images", sa.Text(), nullable=False, server_default="[]"),
    )

    # ---- 2) 反馈模板
    op.create_table(
        "feedback_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("phrases", sa.Text(), server_default="{}"),
        sa.Column("seeds", sa.Text(), server_default="{}"),
        sa.Column("is_builtin", sa.Integer(), server_default="0"),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_fb_templates_owner", "feedback_templates", ["owner_id", "sort_order"])

    # ---- 3) AI 配置（单行；owner_id 唯一）
    op.create_table(
        "ai_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, unique=True, server_default="1"),
        sa.Column("base_url", sa.String(), server_default=""),
        sa.Column("model", sa.String(), server_default=""),
        sa.Column("api_key", sa.Text(), server_default=""),
        sa.Column("timeout", sa.Integer(), server_default="60"),
        sa.Column("updated_at", sa.Text(), server_default=NOW),
    )


def downgrade() -> None:
    op.drop_table("ai_settings")
    op.drop_index("idx_fb_templates_owner", table_name="feedback_templates")
    op.drop_table("feedback_templates")
    op.drop_column("feedbacks", "images")
