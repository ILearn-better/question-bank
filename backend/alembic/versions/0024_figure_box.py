"""配图自动裁切：batch_items 加 figure_box

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-08

**背景**（用户 2026-10-08 的想法）：
整页识别已经能把一页切成多道题，但**配图还要老师自己框**。
这次让模型顺便给出「那一幅图在哪」（`figure_box`），服务端据此自动裁好 ——
老师在待审页只需看一眼，不再需要框选。

为什么要在库里存这个框（而不是裁完就把坐标丢掉）：
  · **预填**：老师觉得自动裁的图不对时，框选弹窗直接预填这个框，
    拖一下边就能改 —— 「重新框一遍」和「微调一下」的工作量差着好几倍。
  · **追溯**：图裁歪了能看出当时用的是哪个框（模型估的 / 吸附到几何候选的）。

⚠️ 与 items.region 的区别，别混用：
    region 是**块模式**里题块的位置（PDF 点坐标），figure_box 是**整页模式**里
    配图的位置（归一化 0~1000）。两者单位都不同，所以单独一列，不复用。

只加一列、可空、不动任何既有列 —— 老库升级零风险，老条目读出来是 NULL
（语义：这条没有「自动框」信息，配图靠人工框）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("batch_items", sa.Column("figure_box", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("batch_items", "figure_box")
