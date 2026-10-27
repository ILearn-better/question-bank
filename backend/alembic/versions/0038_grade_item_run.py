"""作业批改：逐题结果按「轮」编号，重批不再先删旧结果

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-27

**要解决的事**（`docs/项目优化建议-2026-10-26.md` R2）：

    `grade_store._run_grade` 以前一进门就

        delete(HomeworkGradeItem where grade_id == gid) + commit

    然后才起线程调 AI。中途任何一页失败 → 上一次的逐题判读**连同挂在上面的批注**
    一起没了，而且没有历史版本、没有回收站。老师一晚上的复核就是这么白干的。

## 为什么加两列，而不是「把旧数据搬到归档表」

    需要的是「两代结果能共存，但界面上只看得到一代」。共存就必须能区分谁是谁：

      · `homework_grade_items.run_no`  —— 这行属于第几轮（只增，不复用）
      · `homework_grades.item_run`     —— 哪一轮是**显示中的**那一代（指针）

    选「指针」而不是「给每张表加 retired_at、让所有查询都带 `retired_at IS NULL`」：
    后者要改 `detail` / `confirm_grade` / `archive_store` 统计等每一处读路径，
    漏一处就把旧一代的题漏到界面上，而漏掉的地方**不会报错**。
    指针只有一个，读路径跟着它走，"哪一代可见"在库里就是一条可断言的事实。

    也考虑过「内存快照 + 失败回填」，不采用：批注的外键是
    `homework_grade_marks.item_id → homework_grade_items.id ON DELETE CASCADE`，
    题行删掉时标注已经被数据库连带删了，回填题行**拿不回标注** —— 而标注正是这件事
    最贵的部分。进程崩了还会把快照一起丢掉。

## 老数据怎么兼容：两列都是 0

    迁移前的题行 `run_no=0`、迁移前的批改 `item_run=0` → 天然就是"当时唯一那一代、
    也就是显示中的那一代"。**不需要任何回填脚本**，也不需要业务代码判空。

    `server_default="0"` 同时兜住「有人手写 INSERT 不带这一列」的情况。

## 幂等

    纯 `add_column` + `server_default`，无数据搬动；重复执行只会在列已存在时报错
    （Alembic 已记录版本，不会重跑），与 0033 同一套写法。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "homework_grade_items",
        sa.Column("run_no", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "homework_grades",
        sa.Column("item_run", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # 有损但可接受：丢的是「这是第几轮写的」。代价是重批的两代分不开 ——
    # 退回改动前的口径（以库里的题行为准，谁在就是谁）。
    op.drop_column("homework_grades", "item_run")
    op.drop_column("homework_grade_items", "run_no")
