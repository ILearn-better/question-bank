"""作业批改：AI 输出要带的标签（学段 / 知识点 / 体系 / 考法）

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-20

**需求依据**（用户 2026-10-20）：

> 「ai输出中要附带对这道题的标签(例如:学段\\知识点\\体系\\考法等)」

## 为什么是**一个 JSON 列**而不是四个列

    ① 这四个东西是一组，永远**一起读、一起显示**（题卡上一行标签）。
       分成四列的话，每次加一个维度就要再迁移一次。
    ② 它们的取值**随体系变**（DSE 的「体系」和国内人教版的「体系」不是一套词），
       枚举会一直在改。JSON 里多一个键不需要动表结构。
    ③ 只有「学段」是稳定的枚举，那个在代码里校验（`grade_store._labels`），
       不在数据库层约束 —— SQLite 的 CHECK 改起来比代码麻烦得多。

## 为什么不复用已有的 `tags` 列

`tags` 是**自由标签**（老师随手打的、可改），`labels` 是**模型给的结构化分类**
（只读、重批就换）。混在一起会出现「老师删了一个标签，重批之后它又回来了」，
而且没法按「学段」筛 —— 那个是列表里第几个是不定的。分开存，各管一段。

## 与 `kp_name` 的关系

`kp_name` 是**知识点**的权威值（要拿去挂知识树，`knowledge.resolve_kp` 会模糊
匹配到节点上）。所以 `labels.kp` 缺省时**回填 `kp_name`**，不让同一个知识点
在两处不一致。界面上「知识点」这一格显示的也是同一个来源。

⚠️ 纯加列，带 server_default，旧记录零影响：旧题目的 `labels` 落 "{}" →
   界面上那行标签不显示（`labelsOf()` 返回空数组），老记录照旧。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "homework_grade_items",
        sa.Column("labels", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    # 有损但可接受：丢掉的是模型给的分类标签，判定结果（对错/依据）都在。
    op.drop_column("homework_grade_items", "labels")
