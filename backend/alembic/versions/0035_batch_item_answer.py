# -*- coding: utf-8 -*-
"""待审条目存答案与解析

Revision ID: 0035
Revises: 0034

整页识别原来只问 10 个字段（content / qtype / difficulty / knowledge_point /
tags / confidence / needs_figure / figure_note / figure_box / note）——
**没有 answer、没有 analysis**。于是「解析版」讲义（题目后面紧跟【答案】
【详解】）录进题库时，答案和解析全被丢掉：`approve_item` 里写死
`answer=""`、`analysis=""`，`questions.answer` 永远是空的。

现在识别时就问这两项，待审页能看能改，通过时原样搬进 `questions`。

## 取值口径

    `""` = 本页没有这道题的答案 / 解析。**这是正常情况，不是失败**：
       · 题目卷（答案另成册）本来就没有；
       · 解析印在下一页，那半页由下一页的识别另行给出。
    所以**不能**用「空串」判断识别是否失败。同理，`NULL` 也不该出现 ——
    列给 server_default=""，模型给不出就是空串。

老记录（0035 之前识别出的待审条目）两列落空串，行为与以前完全一致；
它们大多已经通过或驳回，补不回来也不必补。

⚠️ 只加列，不动任何既有列，也不用回填。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "batch_items",
        sa.Column("answer", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "batch_items",
        sa.Column("analysis", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    # 有损：丢的是「识别出来但还没通过审核的答案/解析」。
    # 已经通过的那批在 questions.answer/analysis 里，不受影响；
    # 还在待审的重新识别一次即可（原文档还在）。
    op.drop_column("batch_items", "analysis")
    op.drop_column("batch_items", "answer")
