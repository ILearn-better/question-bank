"""录题：questions.render_prefer —— 出卷时这一题印文本还是印原貌图

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-05

背景：录题时同一道题常常**同时**存下两样东西 ——
  · content  识别出来的文本（Markdown + $LaTeX$）
  · image    原貌截图
之前出卷会把两者一起印出来（题干文字下面再压一整张原貌图），既占版面又重复。

现在改成二选一，由老师决定印哪个：
  auto  —— 有文本用文本，没文本用图（默认）
  text  —— 强制文本
  image —— 强制图

默认 'auto' 是**纯增量**的：老数据全落在 auto 上，渲染结果可预期；
出卷接口另有一个整卷覆盖参数，不改库也能临时换。

⚠️ 这是一处**有意的行为变化**：升级前「文本+图都印」，升级后 auto 只印文本。
   已录的题如果文本是从 PDF 文字层抽的（格式乱），请在出卷时把整卷切成「图片」。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "questions",
        sa.Column("render_prefer", sa.String(), server_default="auto", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("questions", "render_prefer")
