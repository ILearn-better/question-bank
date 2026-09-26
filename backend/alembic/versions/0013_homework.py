"""学生作业：作业记录 / 作业维度 / 作业评分，以及 lesson_files 的角色列

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-26

需求：「在写反馈旁边建『交作业』」—— 一节课一格，记这次作业交没交、上传作业原件、
老师按几个维度打分（1-5）。文档见 docs/学生作业上传与评分方案.md。

三张表的理由：

  · `homeworks` —— 作业**必须能独立于反馈存在**。老师完全可能只记「作业没交」而先不写反馈，
    也可能先收了作业照片、隔天再写反馈、再打分。挂在 feedbacks 上就会互相绑死。
    仍然**一节一条**（lesson_id 唯一）：粒度跟反馈一致，老师的心智模型是「这次课的作业」。

  · `homework_dims` —— **独立于 ability_dims**（用户明确定的「分开」）。
    不复用 ability_dims 加个 kind 列的代价是：每条现有查询都得记得加过滤，
    漏一处就是课堂分和作业分混进同一张雷达图，很隐蔽。

  · `homework_scores` —— 构造与 ability_scores 完全对齐（student × dim × 1-5），
    所以雷达图、「本次 vs 上次」、趋势的代码可以照抄，同时不可能混。
    **只存真正打过的分**：没打分的维度就是没有行，不要存 0。

  · `lesson_files.role` —— 作业原件复用上课文件那套管线（上传 / 抽文字 / 归档 / 清理），
    只加一列区分「上课材料」与「作业」。默认 material，老数据语义不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")

# 作业维度默认值。
# 选这五个的标准：老师一眼能判断（不用翻旧记录）、彼此不重复、打完分能指导下一步动作。
# 刻意不含「按时交」—— 那是**事实**，用 homeworks.status 记，不该混进 1-5 星的主观判断里。
DEFAULT_DIMS = [
    ("完成度", 1),        # 做没做全、有没有漏题/空白
    ("正确率", 2),        # 结果对错
    ("过程与规范", 3),    # 步骤是否完整、书写与卷面（数学的真实失分点）
    ("独立完成", 4),      # 是否自己做的 —— 决定「正确率」这一栏可不可信
    ("订正与复盘", 5),    # 上次错题改没改；唯一跨次才见效的维度
]


def upgrade() -> None:
    op.create_table(
        "homeworks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("student_id", sa.Integer(),
                  sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", sa.Integer(),
                  sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        # submitted（已交）/ late（迟交）/ missing（未交）。
        # 「没布置 / 不用记」= 没有这一行，而不是存一个空状态 —— 省掉「空状态算不算分母」的纠结。
        sa.Column("status", sa.String(), nullable=False, server_default="submitted"),
        sa.Column("note", sa.Text(), server_default=""),          # 老师的批改备注
        sa.Column("created_at", sa.Text(), server_default=NOW),
        sa.Column("updated_at", sa.Text(), server_default=NOW),
        sa.UniqueConstraint("lesson_id", name="uq_homeworks_lesson"),
    )
    op.create_index("idx_homeworks_student", "homeworks", ["student_id", "id"])

    homework_dims = op.create_table(
        "homework_dims",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("curriculum_id", sa.Integer(), sa.ForeignKey("curricula.id")),
        sa.Column("sort_order", sa.Integer(), server_default="0"),
        sa.Column("active", sa.Integer(), server_default="1"),
    )
    # 与 ability_dims 一致的种子方式：迁移里一次性写入（0004 就是这么种下那 5 个课堂维度的）。
    # 不用 feedback_templates 那种「每次启动按名字补缺」—— 维度是可改可停用的，
    # 补缺式的种子会把老师删掉的维度又装回来。
    op.bulk_insert(
        homework_dims,
        [{"owner_id": 1, "name": name, "curriculum_id": None,
          "sort_order": order, "active": 1} for name, order in DEFAULT_DIMS],
    )

    op.create_table(
        "homework_scores",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("homework_id", sa.Integer(),
                  sa.ForeignKey("homeworks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("student_id", sa.Integer(),
                  sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False),
        sa.Column("dim_id", sa.Integer(), sa.ForeignKey("homework_dims.id"), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_hwscore_student", "homework_scores", ["student_id", "dim_id"])

    # 作业原件复用上课文件管线，只加一列区分角色；默认 material，老数据语义不变
    op.add_column(
        "lesson_files",
        sa.Column("role", sa.String(), nullable=False, server_default="material"),
    )
    op.create_index("idx_lesson_files_role", "lesson_files", ["lesson_id", "role"])


def downgrade() -> None:
    op.drop_index("idx_lesson_files_role", table_name="lesson_files")
    op.drop_column("lesson_files", "role")
    op.drop_index("idx_hwscore_student", table_name="homework_scores")
    op.drop_table("homework_scores")
    op.drop_table("homework_dims")
    op.drop_index("idx_homeworks_student", table_name="homeworks")
    op.drop_table("homeworks")
