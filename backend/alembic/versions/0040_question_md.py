"""题库 md：题目在磁盘上的那一份，与题库双向同步

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-18

**要解决的事**（用户 2026-11-18 提的需求）：

    「obsidian 和拾课笔记连通……笔记能够弄个题库模板 md，对于这个模板，
      录题系统能够直接读取并且自动录为题目存入题库。重点解决有图片的题目。
      后期题目若要修改或者补充答案，能够直接通过修改对应 md 进行变动入库题目。」

    也就是说：老师在 Obsidian 里看到一个文件夹，里面一道题一个 .md，
    改完保存，拾课把改动同步进题库。

## 为什么只需要两列

    `md_path` —— 这道题来自 vault 里哪个文件（相对 vault 根的 posix 路径）。
    `md_hash` —— 上次同步时那个文件的内容指纹。

    别的都能从文件本身读出来，不需要冗余到库里。唯一必须存的是**身份**
    （哪个文件）和**上次同步到哪了**（要不要再读一遍）。少了 hash 就得每次
    全量重解析所有文件，几百个 md 也能忍，但「文件变了」这件事会变成
    「每题都更新一遍」—— 每次同步都刷新全部 updated_at，等于把这个字段废了。

## 为什么 md_path 用 NULL 而不是空串

    项目里别处的可空文本列一律 `server_default=""`，这里是**故意的例外**：
    `''` 和 `NULL` 都能表示「不来自 md」，但只有 NULL 能让**唯一索引**正常工作
    （SQLite 的唯一索引允许多个 NULL，不允许多个 `''`）。而唯一索引是必要的：
    一个 md 文件只能对应一道题，否则扫描时同一篇会被写成两道题、来回打架。

## 为什么 md_hash 存 16 位就够

    sha256 前 16 个 hex（64 bit）。它要回答的是「这份文件跟上次同步时是不是同一份」，
    不是「有没有人恶意构造碰撞」。用全文哈希只是让报告看着专业一点，代价是多存 48 字节/行。

## 老数据怎么兼容

    两列都 nullable，迁移前每一行都是「不来自 md」，正好是事实。
    没有任何数据搬动，纯 add_column + 建索引，与 0033 / 0038 / 0039 同一套写法。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("questions", sa.Column("md_path", sa.Text(), nullable=True))
    op.add_column("questions", sa.Column("md_hash", sa.String(length=16), nullable=True))
    # partial unique index：只在 md_path 非空时生效。
    # SQLite 支持，MySQL 不支持 —— 但本项目就是 SQLite（见 config.DB_PATH），
    # 不为了一个可能永远不发生的迁库把约束降级成「应用层记得查一下」。
    op.create_index(
        "uq_questions_md_path",
        "questions",
        ["md_path"],
        unique=True,
        sqlite_where=sa.text("md_path IS NOT NULL"),
    )


def downgrade() -> None:
    # 有损：丢掉的是「这道题对应哪个 md 文件」。文件本身不动，
    # 但库与文件之间的那条线断了 —— 下次扫描会把它们当成**新题**再入库一遍。
    # 真要回退，先把 vault 目录整个备份走。
    op.drop_index("uq_questions_md_path", table_name="questions")
    op.drop_column("questions", "md_hash")
    op.drop_column("questions", "md_path")
