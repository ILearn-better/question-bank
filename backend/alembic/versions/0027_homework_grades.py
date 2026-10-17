"""作业批改：homework_grades + grade_files + grade_items + grade_marks

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-17

**需求**（用户 2026-10-17）：引入 AI 批改作业。
批量批改学生交上来的作业（图片/PDF），在原件上标注对错 / 考点 / 错误原因；
数学为主（公式、几何图、解答题过程）；题型有选择、填空、解答；
判定依据三种：AI 自己解 / 老师上传的答案 / 老师给的参考步骤与评分细则；
老师复核时可手动改正；错题打标签回存题库；将来接到「改作业」入口、学生交完自动批改。

已拍板（见 docs/AI批改作业-实现计划.md §10）：
  · A1 答案来源 = 老师上传（一期）；复用题库已有答案放二期
  · A2 「批量」= 一个学生一次交多页/多份；跨学生批量放二期
  · A3 一期**只判对错 + 错因，不给分**（score 可空就是为这个）
  · B1 错题**不自动建题**，只挂已有题 + 打标签
  · B2 入口在「交作业」流程里（作业弹窗保存后出现「批改」）
  · C4 5 维主观分仍只有老师能打，存在 homework_scores，本迁移不碰

设计（为什么四张表、为什么不塞进既有表）：

  · `homework_grades` 不塞进 `homeworks`：后者有 UniqueConstraint("lesson_id")
    ——一节课一条。批改若也唯一，**重批一次就 IntegrityError**，而重批是常态。
    这里只挂可空 FK（homework_id, SET NULL）：批改可以先于「交没交」的记录存在，
    撤销作业记录也不该毁掉批改结果。
  · 不复用 `batch_jobs` / `batch_items`：那两张表的语义是「题库入库的待审中间态」，
    唯一出口是 questions。批改产物同时落在题库（错题）与作业记录两处，硬塞会污染语义。
    只借它的轮询形状（total/done/failed），不借表。
  · 标注坐标一律 **0~1000 的整数、按页归一化**（与 batch_items.figure_box 同制）。
    不存 PDF 点坐标：图片是 96dpi 折点、Word 转出的 PDF 又是另一套页面尺寸。
  · `verdict` 里 `ungradable` 是一等状态（识别不清/图题/未作答）。允许它落进 wrong
    就会开始冤枉人，所以字段默认值就是 ungradable。
  · `transcription_version`：老师把识别错的字改对之后，旧判定引用的原文已不存在，
    必须让这一题作废重跑 —— 否则导出时会出现「引用了一句不存在的话」。
    这是数据模型决策，越晚加越贵。
  · 缺数据就是 NULL，绝不用 0：没判 / 未给分的题 score 为 NULL，
    给 0 会把正确率和雷达图一起带偏（同 0025 的教训）。

⚠️ 全是新建表，不碰任何既有表与列，老库升级零风险。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    op.create_table(
        "homework_grades",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("lesson_id", sa.Integer(), nullable=False),
        # 可空 + SET NULL：作业记录被撤销，批改结果不该跟着消失
        sa.Column("homework_id", sa.Integer(), nullable=True),
        sa.Column("curriculum_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        # ai / key / rubric
        sa.Column("answer_mode", sa.String(), nullable=False, server_default="key"),
        # total 是**页数**，不是题目数（batch_jobs 踩过这个歧义）
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        # 一期只判对错不给分 → 两列都可空
        sa.Column("score", sa.REAL(), nullable=True),
        sa.Column("full_score", sa.REAL(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("dir_rel", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), server_default=NOW),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["homework_id"], ["homeworks.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["curriculum_id"], ["curricula.id"]),
    )
    op.create_index("idx_hwgrades_lesson", "homework_grades", ["lesson_id", "id"])
    op.create_index("idx_hwgrades_student", "homework_grades", ["student_id", "id"])

    op.create_table(
        "homework_grade_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("grade_id", sa.Integer(), nullable=False),
        # 提交件直接引用既有原件，不复制一份一样的
        sa.Column("lesson_file_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("stored", sa.Text(), nullable=False, server_default=""),
        # submission / answer / rubric
        sa.Column("role", sa.String(), nullable=False, server_default="submission"),
        sa.Column("kind", sa.String(), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
        sa.ForeignKeyConstraint(["grade_id"], ["homework_grades.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lesson_file_id"], ["lesson_files.id"], ondelete="SET NULL"),
    )
    op.create_index("idx_hwgrade_files_grade", "homework_grade_files", ["grade_id", "role"])

    op.create_table(
        "homework_grade_items",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("grade_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("q_no", sa.String(), nullable=False, server_default=""),
        sa.Column("qtype", sa.String(), nullable=True),
        sa.Column("stem", sa.Text(), nullable=False, server_default=""),
        sa.Column("student_answer", sa.Text(), nullable=False, server_default=""),
        sa.Column("correct_answer", sa.Text(), nullable=False, server_default=""),
        # correct / partial / wrong / blank / ungradable —— 默认 ungradable 是刻意的
        sa.Column("verdict", sa.String(), nullable=False, server_default="ungradable"),
        sa.Column("score", sa.REAL(), nullable=True),
        sa.Column("full_score", sa.REAL(), nullable=True),
        sa.Column("basis", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.String(), nullable=False, server_default="low"),
        sa.Column("kp_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("node_id", sa.Integer(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("error_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("flags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("raw", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("transcription_version", sa.Integer(), nullable=False, server_default="1"),
        # pending / approved / rejected —— 老师没确认前不进任何统计
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("question_id", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), server_default=NOW),
        sa.ForeignKeyConstraint(["grade_id"], ["homework_grades.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"]),
        sa.ForeignKeyConstraint(["question_id"], ["questions.id"], ondelete="SET NULL"),
    )
    op.create_index("idx_hwgrade_items_grade", "homework_grade_items", ["grade_id", "seq"])

    op.create_table(
        "homework_grade_marks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("grade_id", sa.Integer(), nullable=False),
        # 可空：整页批注（如页脚一句「书写要工整」）不挂在某道题上
        sa.Column("item_id", sa.String(), nullable=True),
        sa.Column("page_no", sa.Integer(), nullable=False, server_default="1"),
        # box / ellipse / check / cross / arrow / text
        sa.Column("kind", sa.String(), nullable=False, server_default="box"),
        sa.Column("geo", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("points", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("color", sa.String(), nullable=False, server_default=""),
        # ai / teacher —— 模型给的框只可信到「哪一带」，界面默认允许拖动
        sa.Column("created_by", sa.String(), nullable=False, server_default="ai"),
        sa.Column("flags", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
        sa.ForeignKeyConstraint(["grade_id"], ["homework_grades.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["homework_grade_items.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_hwgrade_marks_grade", "homework_grade_marks", ["grade_id", "page_no"])


def downgrade() -> None:
    op.drop_index("idx_hwgrade_marks_grade", table_name="homework_grade_marks")
    op.drop_table("homework_grade_marks")
    op.drop_index("idx_hwgrade_items_grade", table_name="homework_grade_items")
    op.drop_table("homework_grade_items")
    op.drop_index("idx_hwgrade_files_grade", table_name="homework_grade_files")
    op.drop_table("homework_grade_files")
    op.drop_index("idx_hwgrades_student", table_name="homework_grades")
    op.drop_index("idx_hwgrades_lesson", table_name="homework_grades")
    op.drop_table("homework_grades")
