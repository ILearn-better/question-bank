# -*- coding: utf-8 -*-
"""讲义一条命令跑完全程的任务表 auto_tasks

Revision ID: 0037
Revises: 0036

**需求**（用户 2026-10-23）：做一个方便 agent 调用的接口，
后续只需要把讲义交给 agent，就能自动「录入 → 检查 → 问题处理 → 无误入库」。

## 与 batch_jobs 的分工

    batch_jobs  → 一次「识别」任务（最多 30 页，一批一个 job）
    auto_tasks  → 一次「交付」（一份讲义，可能拆成好几个 batch_job）

两者是 1:N：讲义 44 页 > MAX_PAGES(30) 时必须拆批，而老师只关心
「这份讲义处理完了吗、入库了多少、还剩几条要我看」—— 那是 auto_tasks 的口径。

## 为什么不复用 batch_jobs 加列

阶段不一样：batch_jobs 只有「识别」一个动作（queued/running/done/partial/failed），
auto_tasks 还有「自动修图」「自动入库」「整理报告」三个阶段。
混在一张表里，`status` 会同时表示两种语义，前端与接口都得靠 `if` 猜。
（同 0036 answer_backfills 不复用 batch_jobs 的理由：落点不同。）

## 取值口径

    status: queued / recognizing / fixing / approving / done / partial / failed
    stage : 人读的一行「现在在做什么」，只用于展示，判断一律看 status
    job_ids: JSON 数组，这份讲义拆出来的 batch_job id（按页序）
    pages  : JSON 数组，要处理的**全部**页号（跨 job 合并）
    approved / needs_human / skipped: 条目级计数，识别完才准
    report : JSON，给 agent 读的完整报告（含每条待处理的直达链接）
    error  : 整体失败原因（单页失败不算，那在校验计数里）

⚠️ 纯新建表，不碰任何既有表/列，老库升级零风险。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "auto_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(), nullable=False, server_default=""),
        sa.Column("src_filename", sa.String(), nullable=True),
        sa.Column("document_id", sa.String(), nullable=True),
        sa.Column("curriculum_id", sa.Integer(), nullable=True),
        sa.Column("job_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("pages", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("total_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_items", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("approved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("needs_human", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("report", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
    )
    op.create_index("ix_auto_tasks_status", "auto_tasks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_auto_tasks_status", table_name="auto_tasks")
    op.drop_table("auto_tasks")
