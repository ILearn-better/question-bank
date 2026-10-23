# -*- coding: utf-8 -*-
"""答案与解析补录任务表 answer_backfills

Revision ID: 0036
Revises: 0035

**需求**（用户 2026-10-23）：对已录入但尚无答案和解析的题目，支持重新录入、
补充或更新答案与解析。

为什么需要这张表：补录是「重读原页 → 出建议 → 老师过目 → 应用」两段式，
建议必须落盘才能跨请求存活（识别 40 多页要十几分钟，不能挂在一次 HTTP 请求里）。

## 与 batch_jobs 的分工

    batch_jobs  → batch_items（待审条目），通过后**新建** questions
    answer_backfills → proposals（建议 JSON），应用后**修改**已有 questions 的两列

两张表形状接近但**落点相反**，复用一张会让 approve 在两套语义之间分叉。

## 取值口径

    status: queued / running / done / partial / failed
    total : 待补题数（建任务时定下）
    done  : 已处理的**页数**（一页可能对应多道题，所以 done ≠ 处理题数）
    proposals: JSON 数组，每条含 qid/page/answer/analysis/score/state/msg
    scope : 建任务时的范围快照（overwritten 与否以它为准，免得应用时口径漂移）

⚠️ 纯新建表，不碰任何既有表/列，老库升级零风险。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "answer_backfills",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scope", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("proposals", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.Column("finished_at", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # 有损：丢的是「识别出来但还没应用」的建议稿。已经写进 questions 的
    # 答案/解析不受影响；要再补一次，重跑一遍补录即可（原文档还在）。
    op.drop_table("answer_backfills")
