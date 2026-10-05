"""出卷样式：paper_templates 卷种模板表 + questions.score 每题分值

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-05

两件事，都是为了「出卷样式可拓展」：

  1. paper_templates —— 卷种样式存数据库，不写死在渲染代码里。
     高考 / 中考 / DSE / A-Level 只是起点：同一场考试不同年份、不同省份、
     不同学校的抬头和注意事项都不一样，写死等于每次改一行说明都要改代码。
     四个 JSON 字段的形状与解析都在 services/paper_style.py：
       paper    抬头区（副标题 / 考试说明 / 注意事项 / 姓名栏字段 / 登分表）
       style    排版（字号 / 行距 / 页边距 / 题间距 / 题号形态 / 答题留白）
       sections 分区（按题型或按难度把题目分组）
       sample   一句「这套长什么样」的说明，只在界面上给人看

  2. questions.score —— 每题分值。可空：老数据没有分值，
     渲染时按「不标分值」处理，绝不会凭空印一个假的分数。

注意 sections 的默认值是 '[]'（空数组）而不是 '{}' —— 它存的是列表。
默认空数组 = 不分区，导出结果跟加这个功能之前完全一致。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    op.create_table(
        "paper_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("code", sa.String(), server_default=""),
        sa.Column("paper", sa.Text(), server_default="{}"),
        sa.Column("style", sa.Text(), server_default="{}"),
        sa.Column("sections", sa.Text(), server_default="[]"),
        sa.Column("sample", sa.Text(), server_default=""),
        sa.Column("is_builtin", sa.Integer(), server_default="0"),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_paper_templates_owner", "paper_templates", ["owner_id", "sort_order"])

    # 可空：没填分值的题照常出卷，只是不标分值
    op.add_column("questions", sa.Column("score", sa.REAL(), nullable=True))


def downgrade() -> None:
    op.drop_column("questions", "score")
    op.drop_index("idx_paper_templates_owner", table_name="paper_templates")
    op.drop_table("paper_templates")
