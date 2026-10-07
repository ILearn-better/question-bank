"""反馈加「课程内容」段：feedbacks.course_content

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-07

**需求**（用户 2026-10-07）：反馈里加一段「课程内容」—— 老师手写本次讲了什么，
润色时随四段一起发给 AI，让模板的【本次课堂内容】栏有料可写（以前 AI 只拿到
topic 一句话 + 上传文件，没传文件时那栏只能空着或编）。

为什么单独一列而不是塞进四段里的某一段：五段各自有语义（讲了什么 / 表现如何 /
有什么问题 / 布置了什么 / 下次安排），导出、润色、模板短语都按段处理，
混进任何一段都会在别的消费方里被当成那一段的内容。

放的位置（业务顺序）：课程内容 → 课堂表现 → 存在问题 → 作业布置 → 下次安排，
与推荐润色模板的栏目顺序一致（先写讲了什么，再写表现）。

⚠️ 纯新增列，带 server_default ''，老库升级零风险（既有行自动为空串 = 没写）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "feedbacks",
        sa.Column("course_content", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("feedbacks", "course_content")
