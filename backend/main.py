# -*- coding: utf-8 -*-
"""拾课（shike）· 独立教师教学管理系统 —— 后端入口。

本文件只做「装配」：创建 app、挂路由、挂静态资源。
所有业务逻辑在 app/routers 与 app/services 里 —— 这是 Phase 0 分层重构的核心目的，
原来 308 行全堆在这一个文件里的写法，撑不住后面还要加的模块。

启动时自动做两件事（自用单机形态下比手工跑命令更合适）：
  1. 把数据库结构升到最新（Alembic，老库先备份再迁移）
  2. 补齐体系骨架与初始知识点树（只补空，不覆盖已有数据）
"""
import logging
import sys
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import __version__, config, migrate, seed          # noqa: E402
from app.db import SessionLocal                             # noqa: E402
from app.routers import (                                   # noqa: E402
    ai,
    batches,
    curriculum,
    dashboard,
    documents,
    feedbacks,
    homeworks,
    lesson_files,
    lessons,
    note_folders,
    notes,
    notes_transfer,
    papers,
    preps,
    questions,
    students,
    supplements,
    system,
    uploads,
)


def _setup_logging() -> None:
    """日志同时落一份到 `data/logs/shike.log`。

    为什么值得单做这件事：服务跑在一个黑底白字的窗口里，报错一闪而过、窗口一关就没了。
    用户说「弹了个报错，然后程序就访问不了了」的时候，没有落盘日志就只能靠猜。
    有了它，直接翻时间点前后几行就知道当时在干什么。
    （文件在 backend/data/ 下，已 gitignore，不会进版本库。）
    """
    try:
        log_dir = config.DATA_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "shike.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return                       # 日志写不了不该拦住服务启动
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))

    log = logging.getLogger("shike")
    log.setLevel(logging.INFO)
    log.addHandler(handler)

    # uvicorn 用自己的 logger，不经过 root —— 单独挂一份，
    # 这样「请求出错」「服务启停」都进同一个文件。
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        lg.propagate = False

    log.info("---- 服务启动 ----")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _setup_logging()
    if config.AUTO_MIGRATE:
        result = migrate.run_migrations(verbose=False)
        if result.get("backup"):
            print(f"[拾课] 迁移前已备份数据库 -> {result['backup']}")
        print(f"[拾课] 数据库结构已就绪，版本 {migrate.current_revision()}")
    else:
        print("[拾课] 已跳过自动迁移（SHIKE_AUTO_MIGRATE=0）")

    db = SessionLocal()
    try:
        stats = seed.seed_all(db)
        if stats["curricula_added"] or stats["nodes_imported"]:
            print(f"[拾课] 初始化：新增体系 {stats['curricula_added']} 个，"
                  f"导入知识点 {stats['nodes_imported']} 个")
    finally:
        db.close()
    yield


app = FastAPI(
    title="拾课 · 独立教师教学管理系统",
    version=__version__,
    description="学生情况记录 · 教学规划 · 家长可视化 —— 本地部署，数据在自己电脑上",
    lifespan=lifespan,
)
# 前端就是本服务托管的（同源），本来用不上 CORS；留着是为了「换个端口起前端」
# （比如用 VS Code 的 Live Preview 预览 frontend/）时不至于打不开。
#
# ⚠️ 但**绝不能放成 `allow_origins=["*"]`**：本服务没有任何鉴权，放开通配符等于
#    声明「任何网页都可以读我的响应」—— 你浏览器里只要打开一个恶意/被挂马的页面，
#    它就能 fetch 本机数据（学生、家长反馈、课时费、题库），而且**读得到内容**。
#    限制成回环地址即可：外站拿不到 localhost 这个 Origin。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled_exception(request, exc: Exception):
    """未处理的异常：记一笔到日志，并把原因回给前端。

    默认行为是回一句干巴巴的 `Internal Server Error` ——
    前端弹出来的就是这个，等于什么都没说。带上原因，至少能看出是哪一步坏的。
    （HTTPException 有自己的处理链，不会走到这里。）
    """
    logging.getLogger("shike").exception(
        "未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务端出错：{type(exc).__name__}: {exc}"},
    )


@app.middleware("http")
async def add_revalidate_header(request, call_next):
    """所有响应都带 `Cache-Control: no-cache`（意思是"每次都回来校验"）。

    `StaticFiles` 只发 ETag / Last-Modified，不发 Cache-Control —— 于是浏览器自己
    按启发式规则决定这些 JS 能直接用多久，改了前端按 F5 都可能还是旧页面。
    踩过好几次：侧栏加了「笔记」，界面上就是不出现（hash 路由切页不会重新拉 App.js，
    整个文档一直是旧的）。

    `no-cache` 不是"不缓存"，而是"每次带 ETag 问一下"：没变 → 304（几乎不耗流量），
    变了 → 200 新内容。本地自用场景这是唯一不会出错的策略。
    """
    response = await call_next(request)
    response.headers.setdefault("Cache-Control", "no-cache")
    return response


# ---- API 路由（按模块拆分）----
app.include_router(dashboard.router)
app.include_router(ai.router)
app.include_router(students.router)
app.include_router(lessons.router)
app.include_router(feedbacks.router)
app.include_router(lesson_files.router)
app.include_router(homeworks.router)
app.include_router(supplements.router)
app.include_router(preps.router)
app.include_router(curriculum.router)
app.include_router(questions.router)
app.include_router(papers.router)
app.include_router(batches.router)
app.include_router(documents.router)
app.include_router(system.router)
app.include_router(uploads.router)
app.include_router(notes.router)
# 笔记目录树。**放在 notes 之后无所谓，但两边各自要先声明自己的子路由**（见 note_folders.note_tree 的说明）
app.include_router(note_folders.router)
# 笔记备份 / 迁移（导出 zip、导入 zip）。前缀是 /api/notes-backup，避开路由歧义
app.include_router(notes_transfer.router)

# ---- 前端静态资源（必须最后挂，否则会吃掉 /api/*）----
if config.FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
