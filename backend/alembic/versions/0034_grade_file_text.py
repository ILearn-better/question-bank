# -*- coding: utf-8 -*-
"""判定依据的正文列

Revision ID: 0034
Revises: 0033

「按我给的答案 / 按我的评分细则」这两条判定模式，依据可能是一份 **PDF / Word /
Excel**。以前只有两条路：粘贴的文字落成 `answer.txt`（正文在磁盘上），
或者引用 `lesson_files`（正文在 `lesson_files.text`）—— 后者要求文件挂在课时下，
会出现在「交作业」清单里，一份答案 PDF 混进提交件后，下次建批改会被**默认勾上**
当学生作业批。

现在依据文件落在**批改自己的目录**（`<grade dir>/basis/`）：`stored` 指向原件
（老师要能点开预览），这份 `text` 存抽出来的正文（喂给模型）。两者分开，
原件不必是文本格式。

## 取值口径

    `""` = 没抽出文字。两种正当情况：
      · 图片（答案照片）—— 本来就没有文字层，原图直接送进模型看；
      · 扫描版 PDF —— 抽不出字，界面会如实说「模型看不见这份」。
    所以**不能用「空串」判断上传失败**：上传失败的判定在写入那一刻就做完了。

⚠️ 纯加列 + server_default，旧记录零影响：老那批依据行 text 落空串，
   `_answer_texts` 退回原来的读法（`.txt` 念 stored、其余读 lesson_files.text）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "homework_grade_files",
        sa.Column("text", sa.TEXT(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    # 有损：丢的是「依据文件抽出来的正文」。原件还在磁盘上（stored 没动），
    # 真想找回可以重传一次 —— 所以可以接受。
    op.drop_column("homework_grade_files", "text")
