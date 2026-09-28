# -*- coding: utf-8 -*-
"""note_folders 加 is_unfiled：「未归档」不再靠猜

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-29

背景：原来「未归档」是靠「is_root=1 且 curriculum_id IS NULL」认出来的 ——
那时一级节点必然来自体系，所以这个认法成立。但笔记的一级分组要跟体系解绑
（用户 2026-09-28 提的：不该为了加一个「IB 数学」先去建体系），
解绑之后普通一级分组的 curriculum_id 也是空的，两者就分不出来了。

所以把这件事显式化：只有「未归档」一行 is_unfiled=1。
它是**兜底容器**（新建笔记没选目录时的落点、删掉某个分组后内容的去处），
不能删、不能改名。旧数据里那一行原样标记过去；没有这一行也不补 ——
运行时 unfiled_root() 会按需建一个（带新标记）。
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 无外键、无约束的普通列：SQLite 直接 ALTER TABLE ADD COLUMN 就行，
    # 不需要 batch_alter_table（那条路只在加带外键的列时才必须走，见 0014 的注释）
    op.add_column("note_folders", sa.Column(
        "is_unfiled", sa.Integer(), nullable=False, server_default="0"))
    op.execute(
        "UPDATE note_folders SET is_unfiled = 1 "
        "WHERE is_root = 1 AND curriculum_id IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("note_folders") as batch:
        batch.drop_column("is_unfiled")
