# -*- coding: utf-8 -*-
"""在**干净的**临时库上把 0014 双向跑通（不碰真库）。

为什么要单独一个脚本：SQLite 的 DDL 不是事务性的，迁移跑一半抛异常会留下
「表建了、版本没动」的半成品，下一次就撞 already exists。之前在本机真库上
反复试就是被这个半成品挡住，越试越乱。干净库 + 一次跑完才算数。

验的事：
  ① 0013 → 0014：两个新列都在、外键在、**新索引也在**（batch 里建索引那个坑）、
     老笔记被放进「未归档」根。
  ② 0014 → 0013：能干净退回去（带外键的列必须走 batch，否则 SQLite 拒绝 DROP COLUMN）。
  ③ 再来一次 0013 → 0014：可重复，不是一次性的。
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
SCRATCH = Path(os.environ["TEMP"]) / "note_tree_scratch.db"
for suffix in ("", "-wal", "-shm"):
    Path(str(SCRATCH) + suffix).unlink(missing_ok=True)

env = dict(os.environ, SHIKE_DB_PATH=str(SCRATCH))
fails = []


def alembic(*args):
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  !! alembic {' '.join(args)} 失败：")
        print("  " + "\n  ".join((r.stdout + r.stderr).strip().splitlines()[-12:]))
        fails.append(f"alembic {' '.join(args)}")
    return r.returncode == 0


def check(label, got, want):
    okk = got == want
    print(f"  [{'OK ' if okk else 'FAIL'}] {label}: {got!r}" + ("" if okk else f"  期望 {want!r}"))
    if not okk:
        fails.append(label)


def info():
    c = sqlite3.connect(str(SCRATCH))
    ver = c.execute("select version_num from alembic_version").fetchone()[0]
    cols = [r[1] for r in c.execute("pragma table_info(notes)")]
    fks = c.execute("pragma foreign_key_list(notes)").fetchall()
    idx = sorted(r[0] for r in c.execute(
        "select name from sqlite_master where type='index' and sql is not null and tbl_name='notes'"))
    tabs = sorted(r[0] for r in c.execute(
        "select name from sqlite_master where type='table' and name like '%note%'"))
    c.close()
    return ver, cols, fks, idx, tabs


def notes_rows():
    c = sqlite3.connect(str(SCRATCH))
    try:
        rows = c.execute("select id,title,folder_id,sort_order from notes order by id").fetchall()
    except sqlite3.OperationalError:
        # 0013 没有这两列 —— 补成 None，让断言不必分两种形状
        rows = [r + (None, None) for r in c.execute("select id,title from notes order by id")]
    roots = []
    if "note_folders" in [r[0] for r in c.execute("select name from sqlite_master where type='table'")]:
        try:
            roots = c.execute("select id,curriculum_id,name,is_root from note_folders").fetchall()
        except sqlite3.OperationalError:
            roots = []
    c.close()
    return rows, roots


print("① 空库升到 0013（= 迁移前的结构）")
assert alembic("upgrade", "0013")
ver, cols, fks, idx, tabs = info()
check("版本", ver, "0013")
check("notes 没有新列", [c for c in cols if c in ("folder_id", "sort_order")], [])
check("note_folders 不存在", tabs, ["notes"])

# 造两篇「老笔记」，模拟用户现有的数据（迁移要能接住它们）
c = sqlite3.connect(str(SCRATCH))
c.execute("insert into notes (id,owner_id,title,content,ink,pinned) values ('aaa','1','老笔记甲','正文甲','[]',0)")
c.execute("insert into notes (id,owner_id,title,content,ink,pinned) values ('bbb','1','老笔记乙','正文乙','[]',1)")
c.commit()
c.close()
print("  造了 2 篇老笔记（一篇标了置顶）")

print("② 0013 → 0014")
assert alembic("upgrade", "head")
ver, cols, fks, idx, tabs = info()
check("版本", ver, "0014")
check("两个新列都在", [c for c in cols if c in ("folder_id", "sort_order")], ["folder_id", "sort_order"])
check("folder_id 是真外键（指向 note_folders）", (fks[0][2], fks[0][6]) if fks else None,
      ("note_folders", "SET NULL"))
check("新索引建上了（batch 里建的那个）", "idx_notes_folder" in idx, True)
check("老索引 idx_notes_updated 还在（重建表不能把它弄丢）", "idx_notes_updated" in idx, True)
check("表齐了", tabs, ["note_folders", "notes"])
rows, roots = notes_rows()
check("两篇老笔记都进了「未归档」（folder_id=1）", [(r[0], r[2]) for r in rows], [("aaa", 1), ("bbb", 1)])
check("「未归档」根建好了", [(r[0], r[1], r[2], r[3]) for r in roots], [(1, None, "未归档", 1)])
check("标题/置顶没被迁移弄坏", [r[1] for r in rows], ["老笔记甲", "老笔记乙"])

print("③ 0014 → 0013（回退）")
assert alembic("downgrade", "0013")
ver, cols, fks, idx, tabs = info()
check("版本退回", ver, "0013")
check("新列没了", [c for c in cols if c in ("folder_id", "sort_order")], [])
check("note_folders 没了", tabs, ["notes"])
check("索引清理干净", [i for i in idx if i != "idx_notes_updated"], [])
check("笔记还在（回退不删数据）", [r[0] for r in notes_rows()[0]], ["aaa", "bbb"])

print("④ 再升一次（可重复）")
assert alembic("upgrade", "head")
ver, cols, fks, idx, tabs = info()
check("版本", ver, "0014")
check("新索引这次也在", "idx_notes_folder" in idx, True)
check("两篇笔记又回到未归档", [(r[0], r[2]) for r in notes_rows()[0]], [("aaa", 1), ("bbb", 1)])

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} 项失败: {fails}"))
print(f"（临时库：{SCRATCH}）")
