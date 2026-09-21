"""M3 课表与课时费：lessons / feedbacks / ability_scores

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20

lessons 是整个系统的枢纽实体：
  student_id → 学生（M2）
  node_ids   → 讲了哪些知识点（M1）
  rate/amount→ 计费（M3）
一张表就把三个模块串起来了，不需要微服务、不需要消息队列。

⚠️ rate 是「单价快照」：学生后来涨价，不改历史课时的金额。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    op.create_table(
        "lessons",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("students.id"), nullable=False),
        sa.Column("curriculum_id", sa.Integer(), sa.ForeignKey("curricula.id")),
        sa.Column("start_at", sa.String(), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("status", sa.String(), nullable=False, server_default="scheduled"),
        sa.Column("mode", sa.String()),
        sa.Column("location", sa.String()),
        sa.Column("rate", sa.REAL()),
        sa.Column("billable", sa.Integer(), server_default="1"),
        sa.Column("amount", sa.REAL()),
        sa.Column("topic", sa.String()),
        sa.Column("node_ids", sa.Text(), server_default="[]"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_lessons_student_time", "lessons", ["student_id", "start_at"])
    op.create_index("idx_lessons_time", "lessons", ["start_at"])

    op.create_table(
        "feedbacks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lesson_id", sa.Integer(), sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False),
        sa.Column("performance", sa.Text()),
        sa.Column("problems", sa.Text()),
        sa.Column("homework", sa.Text()),
        sa.Column("next_plan", sa.Text()),
        sa.Column("rating", sa.Integer()),
        sa.Column("share_to_parent", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
        sa.UniqueConstraint("lesson_id", name="uq_feedback_lesson"),
    )
    op.create_index("idx_feedbacks_student", "feedbacks", ["student_id"])

    op.create_table(
        "ability_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dim_id", sa.Integer(), sa.ForeignKey("ability_dims.id"), nullable=False),
        sa.Column("lesson_id", sa.Integer(), sa.ForeignKey("lessons.id", ondelete="CASCADE")),
        sa.Column("period", sa.String()),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_abscore_student", "ability_scores", ["student_id", "dim_id"])


def downgrade() -> None:
    op.drop_index("idx_abscore_student", table_name="ability_scores")
    op.drop_table("ability_scores")
    op.drop_index("idx_feedbacks_student", table_name="feedbacks")
    op.drop_table("feedbacks")
    op.drop_index("idx_lessons_time", table_name="lessons")
    op.drop_index("idx_lessons_student_time", table_name="lessons")
    op.drop_table("lessons")
