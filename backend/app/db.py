# -*- coding: utf-8 -*-
"""数据库连接层 —— 全项目唯一的 SQLite 接入点。

为什么要集中（开发文档 §8 任务 0.1 / 0.4）：
  `PRAGMA foreign_keys` 是 **按连接** 生效的，不是写进文件里的。
  如果每个路由各开各的连接，只要有一处漏设，那个接口的外键约束就静默失效 ——
  删学生不会级联删课时、删课时不会级联删反馈，而且不报错。
  收敛到这一个文件后，「漏了」这件事就不存在了。

两条 PRAGMA 的区别（值得记住）：
  journal_mode = WAL   → 写进数据库文件，永久生效，设一次即可
  foreign_keys = ON    → 只对当前连接有效，每次新建连接都要重设
"""
from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from . import config

config.ensure_dirs()

engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 15},
    future=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
    cur = dbapi_conn.cursor()
    # 读写互不阻塞：写先进 -wal 文件，读继续读主库快照
    cur.execute("PRAGMA journal_mode=WAL")
    # 外键约束：SQLite 默认关闭，不显式打开则 REFERENCES 形同虚设
    cur.execute("PRAGMA foreign_keys=ON")
    # WAL 下 NORMAL 是安全的（断电最多丢最近一次提交，不会损坏库）
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=15000")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话，用完即关。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def pragma_report() -> dict:
    """自检用：确认 WAL 与外键真的生效了（验收命令）。"""
    with engine.connect() as conn:
        return {
            "journal_mode": conn.execute(text("PRAGMA journal_mode")).scalar(),
            "foreign_keys": conn.execute(text("PRAGMA foreign_keys")).scalar(),
            "db_path": str(config.DB_PATH),
        }
