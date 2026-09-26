"""上课文件（讲义 / 课件 / 试卷）表

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-26

AI 润色时可以把上课文件当参考资料。接的是 OpenAI 兼容的 /chat/completions ——
**纯文本协议**，模型不接受文件本身，所以这里存的是**在本机抽出来的文字**，
原件也留一份（抽文字失败时能换解析器重试，也算个出处）。

为什么单独建表而不是塞进 feedbacks：
  · 一个课时可能带多个文件，塞一个 TEXT 列就得自己拼 JSON；
  · 文件属于**课时**而不是反馈 —— 老师完全可能课还没上完就先传了课件；
  · 按文件记录「抽没抽出来、为什么没抽出来」，失败原因要能原样显示给用户。

lesson_id 带 CASCADE：删课时时行会一起走（磁盘上的原件的删除由路由负责，
数据库管不了文件系统）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")


def upgrade() -> None:
    op.create_table(
        "lesson_files",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lesson_id", sa.Integer(),
                  sa.ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),          # 用户看到的原始文件名
        sa.Column("stored", sa.String(), server_default=""),      # 磁盘上的名字（lf_ 前缀）
        sa.Column("kind", sa.String(), server_default=""),
        sa.Column("size_bytes", sa.Integer(), server_default="0"),
        sa.Column("pages", sa.Integer()),
        sa.Column("chars", sa.Integer(), server_default="0"),
        sa.Column("truncated", sa.Integer(), server_default="0"),
        sa.Column("status", sa.String(), server_default="ok"),    # ok / failed
        sa.Column("reason", sa.Text(), server_default=""),        # 失败原因（直接显示给用户）
        sa.Column("text", sa.Text(), server_default=""),          # 抽出来的文字
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_lesson_files_lesson", "lesson_files", ["lesson_id", "id"])


def downgrade() -> None:
    op.drop_index("idx_lesson_files_lesson", table_name="lesson_files")
    op.drop_table("lesson_files")
