"""批量入库：batch_jobs + batch_items 两张新表

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-05

批量入库走的是**全图路线**：老师在页面上一次性画完全部分割线（或框选），
一次提交 → 服务端把所有题块并行送视觉模型 → 模型返回**结构化字段**
（题干 LaTeX / 题型 / 难度 / 知识点 / 标签 / 置信度 / 备注）→ 落进待审表。

为什么待审条目单独一张表，而不是给 questions 加 status：
  · questions 的语义保持纯粹（「已确认可用的题」），出卷/作业/反馈/笔记插题
    这些下游一行都不用改；
  · 驳回的、识别失败的脏数据永远进不了题库；
  · 批量导入中途失败只影响这张表，不会在 questions 里留下半截题目。

⚠️ 表名用 batch 而不是 import：`import` 是 Python 关键字，
   表名/模块名带上它，处处别扭。

⚠️ 这次是**纯新增表**，不动任何既有表 —— 老库升级零风险。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW_DEFAULT = "(datetime('now','localtime'))"


def upgrade() -> None:
    op.create_table(
        "batch_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("curriculum_id", sa.Integer(), sa.ForeignKey("curricula.id")),
        sa.Column("document_id", sa.String()),
        sa.Column("doc_filename", sa.String()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.Text()),
        sa.Column("finished_at", sa.Text()),
    )

    op.create_table(
        "batch_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(),
            sa.ForeignKey("batch_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("document_id", sa.String()),
        sa.Column("doc_filename", sa.String()),
        sa.Column("page_no", sa.Integer()),
        sa.Column("region", sa.Text()),
        sa.Column("image", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text()),
        sa.Column("qtype", sa.String()),
        sa.Column("difficulty", sa.String()),
        sa.Column("knowledge_point", sa.String()),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("nodes.id")),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("confidence", sa.String()),
        sa.Column("note", sa.Text()),
        sa.Column("flags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("raw", sa.Text()),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("question_id", sa.String()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.Text()),
        sa.Column("reviewed_at", sa.Text()),
    )
    # 审核页永远按「任务 + 状态」筛（待审 / 已通过 / 已驳回），这个复合索引是它的护栏
    op.create_index("idx_batch_items_job", "batch_items", ["job_id", "status"])


def downgrade() -> None:
    op.drop_index("idx_batch_items_job", table_name="batch_items")
    op.drop_table("batch_items")
    op.drop_table("batch_jobs")
