"""作业批改：笔迹的**粗细**也落库（一行一笔一种粗细）

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-21

**需求依据**（用户 2026-10-21）：

> 「保存时须将文档原始内容与画笔批改痕迹一并持久化，包含笔迹所在的页码/位置、
>   坐标、颜色、**粗细**等数据，确保笔迹与文档内容不错位」

## 为什么必须在库里，而不是前端常量

    在加这一列之前，`homework_grade_marks` 已经存了 `page_no`（页码）、`geo`/`points`
    （0~1000 的归一化坐标）、`color`（颜色）—— 唯独粗细是 CSS 里一个常量
    `stroke-width: 2.2`，**所有笔迹一样粗**。

    那样做的问题是「换台设备、换个浏览器、或者哪天把 CSS 调细一点」，
    历史笔迹的观感就一起变了；而且老师想用粗笔写「重做」、细笔圈句子是做不到的。
    粗细和颜色是同一类东西（**这一笔长什么样**），不是渲染参数，所以跟 color 一样存。

## 取值口径：0 = 用默认粗细

    `0` 不是「没有线宽」——SVG 里 `stroke-width:0` 会让笔迹**整条消失**。
    它是「这一笔没指定粗细，交给样式表」（旧记录全是 0，行为与改动前完全一样）。
    前端 `GradeMarkLayer` 绑的是 `:stroke-width="s.width > 0 ? s.width : null"`：
    为 0 时**不输出该属性**，CSS 的 `stroke-width: 2.2` 照旧生效。

    ⚠️ 单位是**屏幕像素**（不是 0~1000 坐标单位）。前端模板上配了
       `vector-effect: non-scaling-stroke`，所以页图放大缩小时笔画粗细不变 ——
       老师画的时候多粗，复核、导出时还是多粗，这才是「笔迹与文档不错位」。

## 为什么是 REAL 而不是 INTEGER

    2.2 这种半档粗细是真实需求（默认档就是 2.2）。取整会变成 2 或 3，
    与改动前已有的笔迹对不齐。

⚠️ 纯加列 + server_default，旧记录零影响：旧标注的 width 落 0 → 走 CSS 默认。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "homework_grade_marks",
        sa.Column("width", sa.REAL(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # 有损但可接受：丢的只是「每笔多粗」。位置/颜色/形状都在，
    # 页面退回统一的默认粗细（与 0033 之前的表现一致）。
    op.drop_column("homework_grade_marks", "width")
