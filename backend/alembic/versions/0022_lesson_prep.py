"""课前备课：lesson_preps + lesson_prep_items

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-07

**需求**（用户 2026-10-07）：每个学生已有「写反馈 / 交作业 / 课后补充」，再加一个
「课前备课」—— 老师上课前做的准备，与那三样并列，都挂在一节课上。

设计（为什么是两张表、为什么 note_id 可空）：

  · **一节一条**（lesson_id 唯一）：跟反馈/作业一致。备课这件事一节课备一次，
    改了就是改这一份，不像课后补充那样一节课能有好几批。
  · 结构化字段四个：goal（教学目标）/ key_points（重点难点）/ flow（教学流程）/
    materials（准备材料）。与反馈五段式同一套心智：分字段写，导出/同步时按字段拼。
  · lesson_prep_items = 备课时挑的「这节课要用的材料」（题 / 笔记），
    复用 LessonSupplementItem 的 shape（kind/ref_id/标题快照），但独立一张表——
    语义不同（那边是发给学生的，这边是老师自己备的），不硬塞进 supplement_items。
  · note_id（可空）：备课与笔记库**双向**联动的枢纽。
      - 从笔记库选了一篇 → note_id 指向它（「这份备课基于/引用了这篇笔记」）。
      - 写完点「存成笔记」→ 服务端把结构化内容拼成 Markdown 写进笔记库，
        再把新/旧笔记的 id 写回 note_id。
    所以 note_id 可能指向「选取的已有笔记」，也可能指向「从备课生成的笔记」。

⚠️ 全是新建表，不碰任何既有表，老库升级零风险。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lesson_preps",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False, server_default=""),
        sa.Column("key_points", sa.Text(), nullable=False, server_default=""),
        sa.Column("flow", sa.Text(), nullable=False, server_default=""),
        sa.Column("materials", sa.Text(), nullable=False, server_default=""),
        sa.Column("note_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("lesson_id", name="uq_lesson_preps_lesson"),
    )
    op.create_index("idx_lesson_preps_student", "lesson_preps", ["student_id", "id"])

    op.create_table(
        "lesson_prep_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("prep_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.ForeignKeyConstraint(["prep_id"], ["lesson_preps.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_lesson_prep_items", "lesson_prep_items", ["prep_id", "sort_order"])


def downgrade() -> None:
    op.drop_index("idx_lesson_prep_items", table_name="lesson_prep_items")
    op.drop_table("lesson_prep_items")
    op.drop_index("idx_lesson_preps_student", table_name="lesson_preps")
    op.drop_table("lesson_preps")
