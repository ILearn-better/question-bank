# -*- coding: utf-8 -*-
"""课后补充：lesson_supplements（批次）+ lesson_supplement_items（条目）

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-29

老师上完课、改完作业，觉得学生某块不行，就在题库和笔记里挑几样东西，
存进**今天那节课**里，下次上课打印（学生版，不含答案）给他。

为什么是一课多条（两张表）而不是塞进 homeworks：
  · homeworks 是「收」（他交回来的 + 我的评价，一课一条）；
    这是「发」（我准备下次给他的），方向相反。
  · 一节课可以有好几批：先发 3 题，晚上又想加 2 题。
    条目直接挂课时的话，「哪几题是那次挑的」分不出来，状态也只能整条课共用一个。

两列都是空的也能存（focus / note 只是给未来的自己留线索），
「挑完就走」是这条链路能不能被用起来的关键。
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

NOW = sa.text("(datetime('now'))")


def upgrade() -> None:
    op.create_table(
        "lesson_supplements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lesson_id", sa.Integer(),
                  sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("focus", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(), nullable=False, server_default="todo"),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=NOW),
    )
    op.create_index("idx_supplements_lesson", "lesson_supplements", ["lesson_id", "id"])

    op.create_table(
        "lesson_supplement_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("supplement_id", sa.Integer(),
                  sa.ForeignKey("lesson_supplements.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=NOW),
    )
    op.create_index("idx_supplement_items", "lesson_supplement_items",
                    ["supplement_id", "sort_order"])


def downgrade() -> None:
    # 先删条目（有外键指向批次），再删批次
    op.drop_index("idx_supplement_items", table_name="lesson_supplement_items")
    op.drop_table("lesson_supplement_items")
    op.drop_index("idx_supplements_lesson", table_name="lesson_supplements")
    op.drop_table("lesson_supplements")
