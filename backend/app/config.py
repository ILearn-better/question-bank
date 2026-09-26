# -*- coding: utf-8 -*-
"""集中配置：所有路径与可变参数都从这里取，不散落在业务代码里。

设计约束（开发文档 §12.1 第 2、3 条）：
  - 不把绝对路径写进数据库（库里只存相对路径，根目录由配置决定）
  - 配置外置：全部环境变量统一 SHIKE_ 前缀，便于将来交付给同行时改配置不改代码
"""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent          # 仓库根目录


def _env_path(key: str, default: Path) -> Path:
    """读环境变量里的路径，相对路径一律相对 BACKEND_DIR 解析。"""
    raw = os.getenv(key)
    if not raw:
        return default
    p = Path(raw)
    return p if p.is_absolute() else (BACKEND_DIR / p)


# ---------- 数据与文件 ----------
DATA_DIR = _env_path("SHIKE_DATA_DIR", BACKEND_DIR / "data")
DB_PATH = _env_path("SHIKE_DB_PATH", BACKEND_DIR / "shike.db")
UPLOAD_DIR = DATA_DIR / "uploads"
PAGES_CACHE = UPLOAD_DIR / "pages"      # PDF 页面渲染图缓存
CROPS_DIR = UPLOAD_DIR / "crops"        # 框选截图 / 区域存档
CONVERTED_DIR = UPLOAD_DIR / "converted"  # Word 转出的 PDF（页面视图的数据源）
NOTES_DIR = UPLOAD_DIR / "notes"        # 笔记里插入的图片
# 上课资料归档根目录：按「学生 / 上课日期」分子目录（见 services/storage.py）。
# 反馈配图与上课文件都归到这里 —— 图片名也是「学生_日期」，脱离目录还能自证身份。
# （原先那个扁平的 uploads/feedback 目录已废弃：所有图片现在都走归档布局。）
STUDENTS_DIR = UPLOAD_DIR / "students"
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR = DATA_DIR / "backups"

FRONTEND_DIR = _env_path("SHIKE_FRONTEND_DIR", ROOT_DIR / "frontend")
LEGACY_TREE_PATH = ROOT_DIR / "data" / "math-knowledge-tree.json"

# ---------- 单用户标识 ----------
# 自用阶段固定为 1；将来接入登录体系时，只需把 owner 的来源从配置换成登录态。
OWNER_ID = int(os.getenv("SHIKE_OWNER_ID", "1"))

# ---------- 行为开关 ----------
# 迁移前自动备份数据库。注意「只在库结构确实要升级时才触发」，不是每次启动都备份。
# 设 SHIKE_AUTO_BACKUP=0 可关掉它；「设置」页里的手工备份不受这个开关影响。
AUTO_BACKUP = os.getenv("SHIKE_AUTO_BACKUP", "1") != "0"
# 备份保留份数，超出后从最旧的开始删
BACKUP_KEEP = int(os.getenv("SHIKE_BACKUP_KEEP", "10"))
# 启动时自动把库升到最新结构（自用单机形态下比手工跑 alembic 更合适）
AUTO_MIGRATE = os.getenv("SHIKE_AUTO_MIGRATE", "1") != "0"


def ensure_dirs() -> None:
    for d in (DATA_DIR, UPLOAD_DIR, PAGES_CACHE, CROPS_DIR, CONVERTED_DIR, EXPORT_DIR,
              BACKUP_DIR, NOTES_DIR, STUDENTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def rel_to_data(path: Path | str) -> str:
    """把绝对路径转成相对 DATA_DIR 的路径再入库（换机器后路径依然有效）。"""
    try:
        return str(Path(path).resolve().relative_to(DATA_DIR.resolve()))
    except ValueError:
        return str(path)


def abs_from_data(rel: str | None) -> Path | None:
    """把库里存的相对路径还原成绝对路径。"""
    if not rel:
        return None
    p = Path(rel)
    return p if p.is_absolute() else (DATA_DIR / p)
