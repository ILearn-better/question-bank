# -*- coding: utf-8 -*-
"""数据库迁移与备份 —— 自用单机形态下替用户把 Alembic 跑起来。

为什么要有这一层：
  Alembic 的标准用法是手工敲 `alembic upgrade head`。但你用的是单机桌面形态，
  「装好就能用、升级自动完成」才是对的。所以启动时自动做三件事：
    1. 备份当前库（用 SQLite 官方 backup API，不是复制文件 —— 见下面注释）
    2. 老库先 stamp 到基线（标记为「已经是 0001」，不动数据）
    3. upgrade head
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from . import config


# ---------------------------------------------------------------- 备份
def backup_db(tag: str = "") -> Path | None:
    """用 SQLite 官方备份接口留一份快照。

    ⚠️ 不能简单地 `copyfile(shike.db)`：
    开了 WAL 之后，最近提交的数据可能还在 `shike.db-wal` 里没合并回主库，
    只拷主文件会拷走一份「缺一块」的数据。
    `Connection.backup()` 会自动把所有已提交内容一致地写进目标文件。
    """
    if not config.DB_PATH.exists():
        return None
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    dst = config.BACKUP_DIR / f"shike-{stamp}{suffix}.db"

    src = sqlite3.connect(str(config.DB_PATH))
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()

    _prune_backups()
    return dst


def _prune_backups() -> None:
    if config.BACKUP_KEEP <= 0:
        return
    files = sorted(config.BACKUP_DIR.glob("shike-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[config.BACKUP_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- 迁移
def _alembic_config() -> Config:
    cfg = Config(str(config.BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{config.DB_PATH}")
    return cfg


def _table_names() -> set[str]:
    if not config.DB_PATH.exists():
        return set()
    con = sqlite3.connect(str(config.DB_PATH))
    try:
        return {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def is_legacy_db() -> bool:
    """重构前的老库：有 documents 表，但没有任何迁移版本记录。"""
    names = _table_names()
    return "documents" in names and "alembic_version" not in names


def is_fresh_db() -> bool:
    return not _table_names()


def run_migrations(verbose: bool = True) -> dict:
    """把数据库结构升到最新。返回一个可打印的结果摘要。"""
    result: dict = {"legacy": False, "backup": None, "from": None, "to": "head"}

    legacy = is_legacy_db()
    result["legacy"] = legacy

    cfg = _alembic_config()

    # 只在「结构真的要动」时才备份。
    # 这里以前是 `if not is_fresh_db()` —— 只要库不是空的就备份，于是每次启动
    # 都白存一份 before-migrate，backups/ 里堆成一片，看着像迁移在反复跑。
    # 实际情况是绝大多数启动都已经在 head 上，根本不会改一行结构。
    # SHIKE_AUTO_BACKUP=0 可以连这个自动备份一起关掉（「设置」页的手工备份不受影响）。
    if not is_fresh_db() and (legacy or _needs_upgrade(cfg)):
        if config.AUTO_BACKUP:
            path = backup_db(tag="before-migrate")
            result["backup"] = str(path) if path else None
        else:
            result["auto_backup_off"] = True

    if legacy:
        # 老库已经在「0001 基线」描述的状态上，标记它，不要重跑建表
        command.stamp(cfg, "0001")
        result["from"] = "0001(stamped)"
    command.upgrade(cfg, "head")

    if verbose:
        print(f"[migrate] legacy={legacy} backup={result['backup']} -> head")
    return result


def current_revision() -> str | None:
    """当前库所处的迁移版本。"""
    if not config.DB_PATH.exists():
        return None
    con = sqlite3.connect(str(config.DB_PATH))
    try:
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()


def _needs_upgrade(cfg: Config) -> bool:
    """库的结构是否真的落后于 alembic 的 head。

    「库存在」不等于「要迁移」—— 平时启动都已经在 head 上，那种时候备份毫无意义。
    取不到 head 时返回 True：宁可多备份一份，也不要漏备份（真有问题的话，
    紧接着的 upgrade 会自己报出来，不会被这里吞掉）。
    """
    try:
        head = ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:
        return True
    return current_revision() != head if head else True
