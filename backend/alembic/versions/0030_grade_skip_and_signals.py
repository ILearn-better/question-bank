"""作业批改：可以指定「哪些不送 AI」+ 模型回两个结构信号

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-19

**需求依据**（用户 2026-10-19）：

> 「当前页面设计太难用了，尤其是分割截图完后看不到图，没办法指定哪些图不传给 AI，
>   每页尾部处理也不到位」

「看不到图」是界面问题（见 `/regions/preview` 与右栏卡片缩略图），不用改表。
这一条迁移只解决另外两件**需要落库**的：

  ① **不送 AI** 要能持久化。它在两个粒度上：
     · 按块（题）—— `homework_grade_regions.skipped`：卷头段、纯结构说明段
       （「四、解答题；本题共 5 小题，共 77 分」）、重复页上的段；
     · 按页 —— `homework_grades.skipped_pages`（JSON 数组文本）：整页不想看
       （封面、空白作答页、草稿页）。
     ⚠️ 两者**都不删框**。删了框下次想恢复就得重画一遍；`skipped` 只是"别送模型"，
       框还留在页面上、还能改坐标。这与批量录题的「不提交」是同一个设计。

  ② 模型要能告诉我们「这块到底是什么」和「底部有没有问题」：
     · `homework_grade_items.block_kind` —— question / header / section / footer /
       blank / other。有了它，界面才能在卡片上提示「模型说这块像卷头，可以设为不判」，
       而不是让老师自己盯着每一块猜。
     · `homework_grade_items.tail_note` —— 尾部一句话：底部被页脚占了 / 底部疑似被
       页切掉 / 底部有别题残字。**这正是「每页尾部处理」缺的那条信息**：
       以前模型看到了也不说，老师只能从判定结果反推。

⚠️ 纯加列，全部带 server_default，旧记录零影响：
  · 旧块的 `skipped` 落 0 → 照旧送模型；
  · 旧任务的 `skipped_pages` 落 "[]" → 一页都不跳；
  · 旧题目的 `block_kind` 落 "" → 界面不显示"像不像题"那行提示（只有新批的才有）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 按块跳过（一段一行，同一题的几段各自带 skipped —— 界面上按题设值时一起写）
    op.add_column(
        "homework_grade_regions",
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
    )
    # 按页跳过：JSON 数组文本（"[]" / "[2,5]"）。
    # 为什么不另开一张表：它是一份任务上的一个开关集合，行数不超过页数（几十），
    # 读取永远「整份一次」，单独建表只多一次 join 和一次迁移维护成本。
    op.add_column(
        "homework_grades",
        sa.Column("skipped_pages", sa.Text(), nullable=False, server_default="[]"),
    )
    # 模型回的两个结构信号
    op.add_column(
        "homework_grade_items",
        sa.Column("block_kind", sa.String(), nullable=False, server_default=""),
    )
    op.add_column(
        "homework_grade_items",
        sa.Column("tail_note", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    # 有损但可接受：丢掉的是「哪些块/页不送 AI」和两个提示，判定结果都在。
    op.drop_column("homework_grade_items", "tail_note")
    op.drop_column("homework_grade_items", "block_kind")
    op.drop_column("homework_grades", "skipped_pages")
    op.drop_column("homework_grade_regions", "skipped")
