"""笔记归档：目录树（体系 → 目录… → 笔记）

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-28

需求（用户）：① 按体系把笔记归总起来，树结构「体系 → 笔记」；② 笔记目录能自由调整层级。

做法：新增一棵**自己的**目录树，而不是复用题库那棵 `nodes`。
理由：`nodes` 是官方大纲（体系→模块→节→知识点→考点），学习路径是定死的；
而笔记目录是老师**自己的整理习惯**（「一、有理数 / 1.1 认识有理数」），要能随手拖。
两件事混在一棵树上，结果就是两边都不能动。

表结构的关键决定：

  · `note_folders.is_root` —— 每个体系一棵，由「体系根」这一行承载。
    为什么要有根行、而不是让笔记的 `folder_id` 挂着空：那样「笔记属于哪个体系」就有了
    两个来源（自己找父目录 / 猜），一处漏判就会把笔记分错体系。
    有根行之后，整棵树只有一种结构，笔记一律有父，归属沿着 parent_id 往上走就能定。

  · `note_folders.curriculum_id` —— **只有根行有值**，非根行一律 NULL。
    它是「这棵树属于哪个体系」的标记，不是每一行的冗余属性；冗余就得在移动目录时
    同步整棵子树，多一个能写歪的地方。体系被删时置空（ON DELETE SET NULL），
    那棵树退化成「未归档」那棵，笔记不丢。

  · `notes.folder_id` —— 可空，但应用层总是写值。留可空是**安全网**：
    迁移万一没跑到、或将来有人手动删了目录行，笔记会退回「未归档」而不是消失。

现有数据：库里 2 篇笔记都没有归属 → 全部落进迁移时建好的「未归档」根。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOW = sa.text("(datetime('now','localtime'))")

UNFILED_NAME = "未归档"


def upgrade() -> None:
    op.create_table(
        "note_folders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), nullable=False, server_default="1"),
        # 只有根行有值：这棵树属于哪个体系（NULL = 「未归档」那棵）
        sa.Column("curriculum_id", sa.Integer(),
                  sa.ForeignKey("curricula.id", ondelete="SET NULL")),
        # 父目录。删父目录时子行不该跟着消失 —— 应用层会把内容上移（见 note_folders.delete_folder），
        # 这里的 CASCADE 只是兜底：真被级联删了，笔记那边还有 SET NULL 接着。
        sa.Column("parent_id", sa.Integer(),
                  sa.ForeignKey("note_folders.id", ondelete="CASCADE")),
        sa.Column("name", sa.String(), nullable=False),
        # 体系根：不可改名、不可删、不可移动（由体系本身决定）
        sa.Column("is_root", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Text(), server_default=NOW),
    )
    op.create_index("idx_note_folders_parent", "note_folders", ["parent_id", "sort_order"])
    op.create_index("idx_note_folders_curriculum", "note_folders", ["curriculum_id"])

    # ⚠️ notes 上加列必须走 batch 模式：SQLite 的 `ALTER TABLE ADD COLUMN` **不能带外键约束**，
    # 直接 add_column 会报「No support for ALTER of constraints in SQLite dialect」。
    # batch 模式内部是「建新表 → 拷数据 → 换名」，所以这一列能带上真外键
    # （模型里怎么写的，库里就怎么样，不会出现「模型有外键、库里没有」这种对不上的情况）。
    with op.batch_alter_table("notes") as batch:
        # 外键**必须起名字**：batch 模式是重建表，重建时约束要靠名字才能对上，
        # 匿名外键会直接报「Constraint must have a name」。
        batch.add_column(sa.Column("folder_id", sa.Integer(),
                                   sa.ForeignKey("note_folders.id", ondelete="SET NULL",
                                                 name="fk_notes_folder")))
        # 同一个目录里的手排顺序。默认 0：老数据靠 updated_at 做并列时的次选，
        # 所以现有笔记的相对顺序不会变（原排序就是 updated_at desc）。
        # 拖动只改 sort_order，**不动 updated_at** —— 那是「最后编辑」。
        batch.add_column(sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))
        # ⚠️ 索引必须建在 batch 块**里面**：batch 的「重建表」是延迟到 with 退出时才做的，
        # 放在外面 create_index 会建在旧表上 —— 然后旧表被换掉丢掉，索引就这么没了
        # （现象：列都在、idx_notes_updated 也在，就只有新建的那个索引不见了）。
        batch.create_index("idx_notes_folder", ["folder_id"])

    # 建「未归档」根，把现有笔记都放进去 —— 迁移不猜体系，让老师自己拖
    #
    # 拿自增主键用 inserted_primary_key。三个坑记在这儿，省得下次重踩：
    # ① 不用 `lastrowid`（SQLAlchemy 1.4 起废弃，走 text() 语句也拿不到主键信息）；
    # ② 表里 id 必须声明成主键，否则 inserted_primary_key 是空元组 → IndexError；
    # ③ `sa.column()` 不接受 primary_key 参数（那是 Column 的），要用 sa.Table + sa.Column。
    conn = op.get_bind()
    nf = sa.Table(
        "note_folders", sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("owner_id", sa.Integer),
        sa.Column("curriculum_id", sa.Integer),
        sa.Column("parent_id", sa.Integer),
        sa.Column("name", sa.String),
        sa.Column("is_root", sa.Integer),
        sa.Column("sort_order", sa.Integer),
    )
    res = conn.execute(sa.insert(nf).values(
        owner_id=1, curriculum_id=None, parent_id=None,
        name=UNFILED_NAME, is_root=1, sort_order=0,
    ))
    root_id = res.inserted_primary_key[0]
    conn.execute(
        sa.text("UPDATE notes SET folder_id = :fid WHERE folder_id IS NULL"),
        {"fid": root_id},
    )


def downgrade() -> None:
    # 同样要走 batch：SQLite 不允许 DROP COLUMN 掉一个出现在外键定义里的列
    # （直接 op.drop_column 会报「unknown column folder_id in foreign key definition」）。
    #
    # 索引先在 batch 外面用 IF EXISTS 掉：batch 的 drop_index 找不到索引会直接抛错，
    # 而「索引恰好不在」并不是什么错误状态（半途失败过的库就是这样），不该卡住回退。
    op.execute("DROP INDEX IF EXISTS idx_notes_folder")
    with op.batch_alter_table("notes") as batch:
        batch.drop_column("sort_order")
        batch.drop_column("folder_id")
    op.drop_index("idx_note_folders_curriculum", table_name="note_folders")
    op.drop_index("idx_note_folders_parent", table_name="note_folders")
    op.drop_table("note_folders")
