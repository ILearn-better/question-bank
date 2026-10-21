"""作业批改：一题可跨页（homework_grade_regions.seg）

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-19

**需求依据**（用户 2026-10-19）：

> 「参考该项目批量录题的部分逻辑吧，即可分割、可框图，还有跨页题目分割方案，
>   区别在给 AI 的内容和输出结果不同，部分功能完全没必要重新造轮子」

0028 的框是「一框一题」，一道解答题跨页就被页界切成两半，只能当成两道题判 ——
这跟批量录题早就解决的问题是同一个（那里叫 `blocks`：一段或多段合成一道题，
`pdf_adapter.crop_regions` 负责把多段（可跨页）竖着拼成一张图）。

所以这里把语义从「框 = 题」改成「**段 = 一框，题 = 一到多段**」：

    seq=1 seg=0 page=1 box=…   第 1 题的上半段
    seq=1 seg=1 page=2 box=…   第 1 题的下半段
    seq=2 seg=0 page=2 box=…   第 2 题

  · `seq` 仍是**题序号**（批改顺序），同一道题的几段共用它；
  · 新增 `seg` 是**段内序号**（拼接顺序），单页题恒为 0。

⚠️ 升级是**纯加列**（server_default="0"）+ 换索引：
  · 旧记录 seg 全落 0 → 每道题仍然"一段"，行为与 0028 一字不差；
  · 索引从 (grade_id, page_no, seq) 换成 (grade_id, seq, seg) ——
    因为主要查询变成「按题聚合、题内按段排序」，page_no 不再是分组键。
    旧索引不删也不影响正确性，但留着是死重量，这里一并换掉。

⚠️ 照片提交件不可能跨页（一张照片就是一页）；跨页只发生在 PDF 提交件上。
  所以这条能力不是"所有情况都变强"，而是补上 PDF 那条断掉的路径。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "homework_grade_regions",
        sa.Column("seg", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_index("idx_hwregions_grade", table_name="homework_grade_regions")
    op.create_index(
        "idx_hwregions_grade", "homework_grade_regions", ["grade_id", "seq", "seg"]
    )


def downgrade() -> None:
    op.drop_index("idx_hwregions_grade", table_name="homework_grade_regions")
    op.create_index(
        "idx_hwregions_grade", "homework_grade_regions", ["grade_id", "page_no", "seq"]
    )
    # ⚠️ 降级会丢掉「哪些段属于同一题」的信息（多段行降级后变成独立的题）。
    # 这是可接受的有损降级：0028 的表结构本身表达不了跨页题，只能退成"各自一题"。
    op.drop_column("homework_grade_regions", "seg")
