"""考试情况记录：exams + exam_files + exam_analysis_templates

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-11

**需求**（用户 2026-10-11）：每个学生要能记考试情况 ——
日期（不一定具体）/ 考试类型（周考 月考 期中考…）/ 成绩 / 等级 /
试题与答题卡（上传文件或图片，能删）/ 考情分析（至少两套模板可选）。

设计（为什么三张表、为什么都不挂在课上）：

  · **exams 是学生级、不挂 lesson**。考试日期和上课日期本来就不一定对得上，
    挂在课上等于逼老师先去找一节课 —— 而那个前提往往不成立。
  · exam_date 存**自由文本**（「2026-09-15」「2026-09」「9 月中旬」都行），
    date_key 是尽力解析出来的排序键，只用于排序；界面显示的永远是原文。
    「日期不一定具体」是这个需求的原话，用 DATE 列就把它挡在门外了。
  · score 与 full_score 分开、都可为空：只写 85 看不出是 85/100 还是 85/150。
    ⚠️ 空就是空，**绝不用 0 顶替** —— 那会把得分率和趋势一起带偏。
  · analysis 存 JSON 快照（含段名 label）。段名一起快照，模板日后改名/删掉后
    老记录仍显示得出来 —— 与 lesson_prep_items.title 同一个道理。
  · dir_rel：归档目录（相对 UPLOAD_DIR），**第一次落文件时才写**。
    没传过附件的考试不该在磁盘上留空目录；老师改日期/类型时目录会跟着改名，
    所以它必须存在库里，不能每次由日期现推。
  · exam_files 独立成表（**不复用 lesson_files**）：那张表的 lesson_id 是
    NOT NULL 指向某节课，而考试根本不挂课。要么改既有表的约束（风险大），
    要么凭空造一节课（往库里塞假数据）。形状只保留归档部分。
    状态一律 'archived'：试题/答题卡只存档备查，**刻意不抽文字**。
  · exam_analysis_templates：内置两套（可改不可删，与反馈模板/卷种样式同规矩）。

⚠️ 全是新建表，不碰任何既有表，老库升级零风险。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "exams",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("student_id", sa.Integer(), nullable=False),
        # 自由文本日期：'2026-09-15' / '2026-09' / '9 月中旬' 都合法
        sa.Column("exam_date", sa.Text(), nullable=False, server_default=""),
        # 尽力解析出来的排序键（'2026-09-15' / '2026-09' / 空），只用于排序
        sa.Column("date_key", sa.String(), nullable=False, server_default=""),
        sa.Column("exam_type", sa.String(), nullable=False, server_default=""),
        sa.Column("score", sa.REAL(), nullable=True),
        sa.Column("full_score", sa.REAL(), nullable=True),
        sa.Column("grade", sa.String(), nullable=False, server_default=""),
        sa.Column("analysis", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("dir_rel", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_exams_student", "exams", ["student_id", "date_key"])

    op.create_table(
        "exam_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("exam_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("stored", sa.Text(), nullable=False, server_default=""),
        sa.Column("role", sa.String(), nullable=False, server_default="other"),
        sa.Column("kind", sa.String(), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="archived"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.ForeignKeyConstraint(["exam_id"], ["exams.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_exam_files_exam", "exam_files", ["exam_id", "role"])

    op.create_table(
        "exam_analysis_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("sections", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("is_builtin", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
    )
    op.create_index("idx_exam_templates_owner", "exam_analysis_templates",
                    ["owner_id", "sort_order"])


def downgrade() -> None:
    op.drop_index("idx_exam_templates_owner", table_name="exam_analysis_templates")
    op.drop_table("exam_analysis_templates")
    op.drop_index("idx_exam_files_exam", table_name="exam_files")
    op.drop_table("exam_files")
    op.drop_index("idx_exams_student", table_name="exams")
    op.drop_table("exams")
