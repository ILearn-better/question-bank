"""M2 学生管理：students / student_curricula / ability_dims（含 5 个默认维度）

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20

能力维度是「数据驱动」的：同行若教物理/化学，换一套维度即可，不用改代码。
这正是这套东西将来能交付给别人的前提之一。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")

# 默认 5 维度（2026-09-20 确认）：(名称, 排序)
DEFAULT_DIMS = [
    ("概念理解", 1),    # 听懂了吗：能否说出定义 / 条件 / 适用场景
    ("计算准确", 2),    # 算得对吗：步骤正确率与粗心比例
    ("解题规范", 3),    # 写得规范吗：过程完整、逻辑清晰、不跳步
    ("做题速度", 4),    # 做得快吗：单位时间完成量与考试节奏
    ("学习主动性", 5),  # 状态对吗：提问、订正、作业完成意愿
]


def upgrade() -> None:
    op.create_table(
        "students",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("nickname", sa.String()),
        sa.Column("grade", sa.String()),
        sa.Column("school", sa.String()),
        sa.Column("contact", sa.String()),
        sa.Column("parent_contact", sa.String()),
        sa.Column("hourly_rate", sa.REAL()),
        sa.Column("rate_unit", sa.String(), server_default="hour"),
        sa.Column("status", sa.String(), server_default="active"),
        sa.Column("started_at", sa.String()),
        sa.Column("ended_at", sa.String()),
        sa.Column("remark", sa.Text()),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_students_status", "students", ["status"])

    op.create_table(
        "student_curricula",
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("students.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("curriculum_id", sa.Integer(), sa.ForeignKey("curricula.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("is_primary", sa.Integer(), server_default="0"),
    )

    ability_dims = op.create_table(
        "ability_dims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("curriculum_id", sa.Integer(), sa.ForeignKey("curricula.id")),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("active", sa.Integer(), server_default="1"),
    )

    op.bulk_insert(
        ability_dims,
        [{"owner_id": 1, "name": name, "curriculum_id": None, "sort_order": order, "active": 1}
         for name, order in DEFAULT_DIMS],
    )


def downgrade() -> None:
    op.drop_table("ability_dims")
    op.drop_table("student_curricula")
    op.drop_index("idx_students_status", table_name="students")
    op.drop_table("students")
