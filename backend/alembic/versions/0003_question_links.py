"""M1 题库多体系化：questions 扩展 + question_nodes

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-20

knowledge_points(JSON 数组) 保留以兼容旧数据；
新关系走 question_nodes 表 —— 可以建索引、可以算加权掌握度，
这是裸 ALTER ADD COLUMN 永远做不到的「结构变更」。

⚠️ 为什么要写原生 SQL（这个坑值得记下来）：
  SQLite 自 3.35 起原生支持 `ALTER TABLE ADD COLUMN ... REFERENCES`，
  但 **Alembic 的 SQLite 方言会拦住它**，报：
      NotImplementedError: No support for ALTER of constraints in SQLite dialect
  另一条路 batch_alter_table 会重建整张表，而它要求所有约束都有名字，
  现有 questions 表的约束是匿名的，一样走不通。
  所以这里直接用 op.execute 发原生 DDL —— 这正是 SQLite 实际支持的能力。
  代价：这些外键在库里是匿名的。SQLite 运行期不在意；
  若将来要 batch 重建这些表，需要先补名字。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ADD_COLUMNS = (
    "ALTER TABLE questions ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE questions ADD COLUMN curriculum_id INTEGER REFERENCES curricula(id)",
    "ALTER TABLE questions ADD COLUMN node_id INTEGER REFERENCES nodes(id)",
    "ALTER TABLE questions ADD COLUMN source TEXT",
    "ALTER TABLE questions ADD COLUMN year INTEGER",
    "ALTER TABLE questions ADD COLUMN usage_count INTEGER DEFAULT 0",
    "ALTER TABLE questions ADD COLUMN last_used_at TEXT",
    "ALTER TABLE questions ADD COLUMN stem_format TEXT DEFAULT 'text'",
)


def upgrade() -> None:
    for ddl in ADD_COLUMNS:
        op.execute(ddl)
    op.create_index("idx_questions_curr", "questions", ["curriculum_id", "node_id"])

    op.create_table(
        "question_nodes",
        sa.Column("question_id", sa.String(), sa.ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("weight", sa.REAL(), server_default="1.0"),
    )
    op.create_index("idx_qn_node", "question_nodes", ["node_id"])


def downgrade() -> None:
    op.drop_index("idx_qn_node", table_name="question_nodes")
    op.drop_table("question_nodes")
    op.drop_index("idx_questions_curr", table_name="questions")
    for ddl in reversed(ADD_COLUMNS):
        column = ddl.split("ADD COLUMN ", 1)[1].split(" ", 1)[0]
        op.execute(f"ALTER TABLE questions DROP COLUMN {column}")
