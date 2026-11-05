"""作业批改：标注可以是一张老师插入的图片

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-06

**要解决的事**（用户 2026-11-06 提的需求）：

    「在作业批改处加入文件插图图片功能（笔画、文本框、图片谁先出现就谁放下面），
      并且要求能够删除图片、撤销插入（Ctrl+Z）、鼠标拖拽调整图片大小」

    卷面上的「图」以前只能是原件的页图，老师想把另一张纸上的草稿、题目截图
    贴到某一处，没有任何入口。三种批注（笔迹 / 文本框 / 图片）要按创建先后叠放，
    就必须都在同一张表里、用**同一个 id 序列**说话 —— 所以图片是一条 `kind="image"`
    的标注，而不是另建一张 `homework_grade_mark_images`。

## 为什么单独一列，不塞进 `text`

    `text` 现在是批注文字。借着用的话：
      · 前端渲染要么按 kind 分支去「猜这一串是文字还是 URL」，要么两个都试；
      · 导出 PDF 时 `grade_export` 会把 `text` 当正文排版 —— 一条图片批注会印出
        一长串 `/api/mark-images/gm_xxx.png`；
      · 检索、修数据都得先判 kind 才知道这一列到底是什么。
    一列只服务一种用途，代价只是一次 add_column。

## 老数据怎么兼容：空串

    `server_default=""` + `nullable=False`：迁移前的每一行都是「没有图」，
    正好是事实。前端 `v-if="s.kind === 'image'"` 根本不会去看这一列。

## 幂等

    纯 `add_column`，无数据搬动；重复执行只会报「列已存在」（Alembic 已记版本，
    不会重跑），与 0033 / 0038 同一套写法。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "homework_grade_marks",
        sa.Column("image", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    # 有损：丢掉的是「插在卷面上的那张图」。文件仍在 data/uploads/grade_marks/ 下，
    # 但库里再没有行指向它们（与 delete_mark 保留文件的取舍一致：宁可留图，不误删）。
    op.drop_column("homework_grade_marks", "image")
