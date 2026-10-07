"""题干配图：batch_items 三个字段 + questions.figure_image

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-06

**问题**：批量入库拿到的是一整块的原貌图 + 一段识别文本。
出卷时如果这一题走**文本形态**（识别过的题文本更清楚、公式更准），
原貌图整张都不印 —— 于是题干里那句「如图」的图就没了。
老师拿到卷子才发现，而这时没人知道那幅图原本长什么样。

**做法**：入库时就让模型顺手判断「这题要不要图」（batch_items.needs_figure +
figure_note），再审时老师可以在原卷页面上**把那一幅图框出来**（figure_image）。
通过后这幅图跟着题目走（questions.figure_image），出卷时按形态取用。

为什么 needs_figure 是 int 而不是 boolean：
  SQLite 没有真正的布尔列，SQLAlchemy 的 Boolean 会存成 0/1 但迁移写法上
  各家版本对 server_default 的处理不一致（`"0"` / `text("0")` / `sa.false()`）。
  这里统一用 Integer + 0/1，跨库迁移最省事，读的时候 `bool(x)` 一样用。

⚠️ 纯新增列，全部带 server_default，老库升级零风险（既有行自动填 0 / 空串）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "batch_items",
        sa.Column("needs_figure", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("batch_items", sa.Column("figure_note", sa.Text()))
    op.add_column(
        "batch_items",
        sa.Column("figure_image", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "questions",
        sa.Column("figure_image", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("questions", "figure_image")
    op.drop_column("batch_items", "figure_image")
    op.drop_column("batch_items", "figure_note")
    op.drop_column("batch_items", "needs_figure")
