"""作业批改：一题一区域（homework_grade_regions）

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-19

**需求依据**（2026-10-19 调研 + 实测，见 geo-grade-lab/out/00-总结论.md）：

  1. 整页理解是当前最大的系统性误差源 —— 整页照片里单题作答只占很小一块，
     视觉编码器一降采样，上下标/角度符号/小数点就糊了。裁出来再放大，
     是「不动模型、不花钱」就见效的一条。
  2. 题号错配：模型认学生手写的「第 3 题」并不可靠（会把 6 读成 8）。
     改为**题号由框绑定**，模型不再负责「这是第几题」。
  3. 跨题污染：上一题的字迹伸进下一题区域会让分组错乱。框外内容模型看不见，
     这个问题从物理上消失。

设计：

  · 框落在 **grade 上**，不另建「卷面模板」实体；一套卷复用框 = 从同课时另一份
    `copy_from` 拷过来。少一层实体，也就少一处「模板改了旧记录跟不跟」的歧义。
    **代价**要记住：不同学生拍照有平移/旋转，拷来的框会偏 →
    前端提示「宁大勿小」、裁切按 REGION_PAD 外扩、框偏到裁掉作答时右栏看到
    「转写为空」，老师能当场发现。
  · `box` 用**按页归一化的 0~1000 整数**（与标注、figure_box 同制）。
    绝不存像素：页图尺寸随渲染 dpi 变，存像素换个 dpi 就全错位。
  · 没有框的页**保持原行为**（整页一次判），所以老记录、老流程都不受影响，
    升级是纯新增。

⚠️ 新建一张表 + 给 homework_grade_items 加一列（figure_note，带 server_default=""），
不删不改任何既有列，老库升级零风险、老记录零影响。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = "(datetime('now','localtime'))"


def upgrade() -> None:
    op.create_table(
        "homework_grade_regions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("grade_id", sa.Integer(), nullable=False),
        sa.Column("page_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("q_no", sa.String(), nullable=False, server_default=""),
        sa.Column("box", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=sa.text(NOW)),
        sa.ForeignKeyConstraint(["grade_id"], ["homework_grades.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_hwregions_grade", "homework_grade_regions", ["grade_id", "page_no", "seq"]
    )

    # 「图形证据要能被老师核对」—— figure_note 原来只躺在 raw JSON 里，界面看不到。
    # 加一列而不是从 raw 里现算：raw 是模型原样输出，字段名会被模型改来改去。
    op.add_column(
        "homework_grade_items",
        sa.Column("figure_note", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("homework_grade_items", "figure_note")
    op.drop_index("idx_hwregions_grade", table_name="homework_grade_regions")
    op.drop_table("homework_grade_regions")
