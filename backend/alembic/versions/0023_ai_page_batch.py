"""AI 整页识别（实验）：batch_jobs 加 ai_mode / pages

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-08

**背景**（用户 2026-10-08 的想法）：
把批量入库从「人工画分界线 → 裁题块 → 逐块识别」改成
「整页图直接喂模型 → 模型按固定字段输出**多道**题 → 人工只补配图」。

为什么**不新建一张表**，只在 batch_jobs 上加两列：
  · 新流程的**产物**与旧流程完全一样 —— 都是 batch_items（一题一条）。
    待审列表、配图框选、通过入库、驳回重跑这些下游一行都不用改。
    差别只在「条目是怎么来的」：旧的是老师裁出来的块，
    新的是模型从整页里切出来的题。
  · 所以任务壳子复用，只补两个描述性字段：

      ai_mode  0 = 旧的块模式（默认值，老任务不受任何影响）
               1 = AI 整页模式
      pages    JSON 数组，整页模式下要识别的页号（如 [1,2,3]）。
               块模式下为 NULL —— 那时要识别什么在提交前就定好了。

⚠️ 只加列、不改任何既有列、不动 batch_items：
   老库升级零风险；老任务 ai_mode 默认 0，读出来就是旧行为。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 与 0006 / 0007 / 0008 / 0010 一样直接用 op.add_column ——
    # SQLite 原生支持 ADD COLUMN，不必走 batch_alter_table 重建表
    # （重建 batch_jobs 会被 batch_items 的外键绊住）。
    op.add_column(
        "batch_jobs",
        sa.Column("ai_mode", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("batch_jobs", sa.Column("pages", sa.Text()))


def downgrade() -> None:
    op.drop_column("batch_jobs", "pages")
    op.drop_column("batch_jobs", "ai_mode")
