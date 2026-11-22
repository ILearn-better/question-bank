"""讲义 md：一篇笔记里既写知识、也放题目，题目能单独进题库

Revision ID: 0041
Revises: 0040
Create Date: 2026-11-23

**要解决的事**（用户 2026-11-23 提的需求）：

    「做一个讲义 md 模板：划分知识和题目部分，知识部分不会被题库识别，
      能够正常将题目导入题库。想实现的效果：给一份满足该模板的讲义，
      既能够在笔记中显示，也能将其中题目导入题库。」

## 为什么只加一列

    0040 的 `md_path` 语义是「**整个**文件就是这一道题」，并且带唯一索引 ——
    一篇讲义里有 5 道题，5 行都写同一个路径会直接把唯一索引撞翻。

    于是「正文住在哪里」变成两种互斥的归属：

      md_path   一整个文件 = 一道题（`题库/` 下的标准题目 md）
      note_path 一份讲义里躺着好几道题（`shike: lecture` 的笔记）

    两者都只存**相对 vault 根的 posix 路径**，和 0040 同一套口径。
    `note_path` **不加**唯一索引：一份讲义天生对应多道题。

## 为什么不复用 md_path + 把唯一索引删掉

    唯一索引是 0040 特意加的：「一个 md 文件只能对应一道题」这条约束一旦松掉，
    扫描时同一篇会被写成两道题、来回打架（见 0040 的说明）。为讲义松掉它，
    等于把已经守住的东西退回去 —— 宁可多一列。

## 这块的真源是谁

    讲义（note_path）的题，**正文真源是那篇讲义里的那一段**：
      · 网页改题 → `vault.sync_back` 只重写那一段，**绝不整篇重写**
        （整篇重写会把老师的知识部分全冲掉）；
      · 「导出成 md」跳过这些题 —— 它们已经有文件了，再写一份到 `题库/`
        就变成同一道题两份正文，之后谁盖谁全看顺序。

## 老数据怎么兼容

    nullable，迁移前每一行都是「不来自讲义」，正好是事实。没有数据搬动，
    纯 add_column，与 0033 / 0038 / 0039 / 0040 同一套写法。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("note_path", sa.Text(), nullable=True))


def downgrade() -> None:
    # 有损：丢掉的是「这道题是从哪篇讲义里来的」。讲义本身不动，
    # 但那几道题从此没有任何文件归属 —— 下次导出会在 `题库/` 下另写一份。
    # 真要回退，先把 vault 目录整个备份走。
    op.drop_column("questions", "note_path")
