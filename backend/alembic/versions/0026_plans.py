"""整体规划：student_plans + plan_files

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-16

**需求**（用户 2026-10-16）：学生信息界面加「整体规划」（放在「考试情况」上方），
能新建 / 删除 / 查看 / 修改；正文可以是**打字的文本**，也可以是**文本文件**
（常见的 markdown、word），点开某条规划要能看见**渲染好的**内容；
每条规划点开才详细查看。

设计（为什么两张表、为什么正文在库里）：

  · 正文存 `student_plans.content`（**Markdown 文本**），不是「挂一个文件路径」——
    需求说「文本**或者**文本文件」，两种情况都要有地方放；只存文件路径的话，
    「直接打字写规划」这条路就没地方落脚了。
  · 上传的 Word / PDF / Markdown 在**保存前**就由 `services/doc_to_note` 转成
    Markdown 灌进 content（那是「给人看」的转换器：标题、列表、表格、图片位置都留；
    不是 `file_text` 那套「给 AI 看」的，打开会是一坨）。转换失败**不阻断上传**，
    原件照存，原因写在 `plan_files.note` 里照实显示。
  · `plan_files` 存**原件**（`students/<学生>/plans/<标题>/`）。转换一定失真，
    正文是「能读的那部分」，原件才是「一定没丢的那部分」。
    `converted` 记这份有没有产出正文 —— 界面上要说清「这份只是存着」，
    否则老师会以为转换悄悄失败了。
  · dir_rel：归档目录（相对 UPLOAD_DIR），**第一次落文件时才写**（没传过文件的规划
    不该在磁盘上留空目录）；改了标题目录会跟着改名，所以必须存在库里，
    不能每次由标题现推。
  · 不复用 exam_files：那张表的 exam_id 是 NOT NULL 指向一次考试，
    规划不挂在考试上。要么改既有表约束（风险大），要么造一条假考试记录（往库里塞假数据）。
  · 学生被删时：student_id 上带 ON DELETE CASCADE，两张表一起走（学生整个归档目录
    也由 storage.remove_student_dir 收掉，见 routers/students.py）。

⚠️ 全是新建表，不碰任何既有表，老库升级零风险。
⚠️ 附带图片的 Word 抽出来的图落在**笔记资源目录**（doc_to_note 的固定行为），
   正文引用成 /api/notes/files/<name> —— 所以 routers/notes.py 的引用计数
   必须把规划正文也算上，否则「某篇笔记不再引用那张图」时会误删规划正在用的图。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "student_plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        # 正文：Markdown 文本（打字的 / 由文件转换来的，都是它）
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        # 归档目录（相对 UPLOAD_DIR），第一次落文件时才写
        sa.Column("dir_rel", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.Column("updated_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        # 删学生 → 他的规划一起走。库里带了 ON DELETE CASCADE，
        # 但真正删得干净靠的是 storage.remove_student_dir（文件系统那边数据库管不着）。
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_student_plans_student", "student_plans", ["student_id", "updated_at"])

    op.create_table(
        "plan_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),          # 上传时的原名
        sa.Column("stored", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        # 有没有被转成正文（0/1）—— 界面照实说「这份只是存着，没转出文字」
        sa.Column("converted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), server_default=sa.text("(datetime('now','localtime'))")),
        sa.ForeignKeyConstraint(["plan_id"], ["student_plans.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_plan_files_plan", "plan_files", ["plan_id", "id"])


def downgrade() -> None:
    op.drop_index("idx_plan_files_plan", table_name="plan_files")
    op.drop_table("plan_files")
    op.drop_index("idx_student_plans_student", table_name="student_plans")
    op.drop_table("student_plans")
