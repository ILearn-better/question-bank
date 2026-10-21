"""批改文件留存：一份批改完成后的存档记录（学生资料 → 改作业 的数据源）

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-20

**需求依据**（用户 2026-10-20）：

> 「批改后的文件按「学生/日期」目录结构保存……在「学生资料 → 改作业」中
>   自动生成批改记录，关联学生、日期、作业、批改人与文件；
>   点开该记录即可预览学生本次作业的批改情况」

## 为什么是新表，而不是在 homework_grades 上再加一列

    ① **一份批改可以存多次**。老师存了 PDF，发现漏看一页，改完再存一次 ——
       第二次不能把第一次覆盖掉（存档的意义就是「当时的那个版本」）。
       加列只能存「最新一次」。
    ② **也可以完全没有批改**。线下批好的卷子直接拍照传进存档，那种记录
       没有 grade 可挂，硬塞进 homework_grades 等于造假数据。
    ③ 批改任务本身是可以被清理的中间产物（重批、换模型），
       存档是**另一份工作成果**，生命周期不该绑在一起。

## 字段上的几个决定

    · `archived_on` 存 `YYYY-MM-DD` 文本，与目录名**同一个值**。
      ⚠️ 刻意不存 datetime：目录按天分，同一天存两次是两次记录、同一个文件夹，
         存到秒会让「按日期分组」多一层格式化，还容易与目录名对不上。
    · `rel_path` 存**相对 UPLOAD_DIR 的路径**（同 storage.rel），不存绝对路径 ——
      换机器、搬数据目录都不会失效（开发文档 §12.1 第 2 条）。
    · `stored_name` 与 `name` 分开：老师看到的原名可能带 `:` `/` 这类字符，
      落盘必须洗干净；避重后的 `_2` 也得记下来，否则删文件时找不到。
    · `owner_id`：与全库一致（自用固定 1），将来做多租户不必再改表。

纯新建表，不动任何既有列，老库升级零风险、老记录零影响。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = "(datetime('now','localtime'))"


def upgrade() -> None:
    op.create_table(
        "grade_archives",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("grade_id", sa.Integer(), nullable=True),
        sa.Column("lesson_id", sa.Integer(), nullable=True),
        sa.Column("archived_on", sa.String(), nullable=False, server_default=""),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("stored_name", sa.String(), nullable=False, server_default=""),
        sa.Column("rel_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(), nullable=False, server_default="grade"),
        sa.Column("grader", sa.String(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=sa.text(NOW)),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        # 可空 + SET NULL：批改任务被清理时，存档是另一份工作成果，不该跟着消失
        sa.ForeignKeyConstraint(["grade_id"], ["homework_grades.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lesson_id"], ["lessons.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    # 「学生资料」页的查询就是 student_id + 按日期倒序，索引照着它建
    op.create_index(
        "idx_garachive_student", "grade_archives", ["student_id", "archived_on", "id"]
    )
    op.create_index("idx_garachive_grade", "grade_archives", ["grade_id"])


def downgrade() -> None:
    # ⚠️ 只删表，**不删磁盘上的存档文件** —— 那些是老师批改的劳动成果，
    #    降级一次就把它们抹掉是不可接受的。要清文件得在界面上逐条删（会真删）。
    op.drop_index("idx_garachive_grade", table_name="grade_archives")
    op.drop_index("idx_garachive_student", table_name="grade_archives")
    op.drop_table("grade_archives")
