# -*- coding: utf-8 -*-
"""系统信息与备份：给「设置」页用，也是重构后的自检入口。

/api/system/info 直接回答三个问题：
  迁移到哪个版本了？WAL 真的开了吗？外键约束真的生效了吗？
前两个是 Phase 0 的验收项，第三个是最容易被静默吞掉的那个。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__, config, migrate
from ..db import get_db, pragma_report
from ..models import AbilityDim, Curriculum, Feedback, Lesson, Node, Question, Student

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
def system_info(db: Session = Depends(get_db)):
    info = pragma_report()
    db_size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    return {
        "app": "拾课 (shike)",
        "version": __version__,
        "revision": migrate.current_revision(),
        "db": {
            "path": info["db_path"],
            "size_bytes": db_size,
            "journal_mode": info["journal_mode"],
            "foreign_keys": bool(info["foreign_keys"]),
            "wal_files": sorted(
                p.name for p in config.DB_PATH.parent.glob(f"{config.DB_PATH.name}-*")
            ),
        },
        "dirs": {
            "data": str(config.DATA_DIR),
            "uploads": str(config.UPLOAD_DIR),
            "exports": str(config.EXPORT_DIR),
            "backups": str(config.BACKUP_DIR),
        },
        "counts": {
            "curricula": db.scalar(select(func.count()).select_from(Curriculum)) or 0,
            "nodes": db.scalar(select(func.count()).select_from(Node)) or 0,
            "questions": db.scalar(select(func.count()).select_from(Question)) or 0,
            "students": db.scalar(select(func.count()).select_from(Student)) or 0,
            "lessons": db.scalar(select(func.count()).select_from(Lesson)) or 0,
            "feedbacks": db.scalar(select(func.count()).select_from(Feedback)) or 0,
            "ability_dims": db.scalar(select(func.count()).select_from(AbilityDim)) or 0,
        },
    }


@router.get("/foreign-key-check")
def foreign_key_check():
    """跑一次 SQLite 的完整性自检 —— 有孤儿数据会在这里暴露出来。"""
    con = sqlite3.connect(str(config.DB_PATH))
    try:
        rows = con.execute("PRAGMA foreign_key_check").fetchall()
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    return {"ok": integrity == "ok" and not rows, "integrity": integrity, "violations": len(rows)}


@router.post("/backup")
def manual_backup():
    path = migrate.backup_db(tag="manual")
    if path is None:
        return {"ok": False, "message": "数据库文件还不存在，无需备份"}
    return {
        "ok": True,
        "file": path.name,
        "path": str(path),
        "size_bytes": os.path.getsize(path),
    }


@router.get("/backups")
def list_backups():
    if not config.BACKUP_DIR.exists():
        return []
    files = sorted(config.BACKUP_DIR.glob("shike-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"name": p.name, "size_bytes": p.stat().st_size,
         "created_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
        for p in files
    ]
