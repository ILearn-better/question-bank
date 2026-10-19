# -*- coding: utf-8 -*-
"""作业批改的 HTTP 层 —— 只做「收参数 / 翻错误码 / 输出 dict」，逻辑全在 services/grade_store。

路径与返回形状是和三方（AI 层 / 前端）对齐过的**契约**，改之前先看
docs/AI批改作业-实现计划.md §5：前端已经在照这个写。

两条贯穿全文的约定：

  · **错误一律给老师能看懂的人话**。这个功能会撞上"答案没给、文件是 Word、
    模型这页没读出来"这些很具体的状况，回一句 `422 Unprocessable Entity`
    等于什么都没说。业务错误由 `GradeError` 带上 status + message 上来。
  · **不在这里开线程 / 不在这里 new session**。批改线程由 store 认领
    （它要保证幂等），这里只等结果。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import (
    GradeConfirmIn,
    GradeIn,
    GradeItemPatch,
    GradeMarkIn,
    GradeMarkPatch,
    GradeMarksIn,
    GradePageSkipsIn,
    GradeRegionsCopy,
    GradeRegionsPut,
    GradeSetupPatch,
    GradeToBankIn,
)
from ..services import grade_export
from ..services import grade_store as store

router = APIRouter(prefix="/api", tags=["homework-grades"])


def _fail(e: store.GradeError) -> HTTPException:
    return HTTPException(e.status, str(e))


# ---------------------------------------------------------------- 批改任务
@router.post("/homework-grades")
def create_grade(payload: GradeIn, db: Session = Depends(get_db)):
    """建一次批改任务。**建完不自动跑** —— 跑是 `POST /{gid}/run`。

    分开的理由：老师点「AI 批改」那一刻要先看到批改页（带页图、带已选的提交件），
    再决定是不是真的开批。建与跑合在一起的话，误点一次就是十几秒的模型调用。
    """
    try:
        grade = store.create_grade(db, payload)
    except store.GradeError as e:
        raise _fail(e) from e
    return store.detail(db, grade)


@router.get("/homework-grades")
def list_grades(lesson_id: int | None = Query(None), db: Session = Depends(get_db)):
    """批改记录列表（简报，**不含 items**）。一节课可能有多条 —— 重批是常态。"""
    return store.list_grades(db, lesson_id)


@router.get("/homework-grades/{gid}")
def get_grade(gid: int, db: Session = Depends(get_db)):
    """详情：逐题结果 + 标注 + 页清单。前端靠它做整张复习/复核页。"""
    try:
        grade = store.get_grade(db, gid)
    except store.GradeError as e:
        raise _fail(e) from e
    return store.detail(db, grade)


@router.patch("/homework-grades/{gid}")
def patch_grade_setup(gid: int, payload: GradeSetupPatch, db: Session = Depends(get_db)):
    """开批**之前**改「判定依据」：AI 自己解 / 老师给的答案 / 老师给的评分细则。

    对应 A1 那个决定（答案是老师提供的）。老师是先跳到批改页、看到页图之后才决定
    "这次按我的答案判"，所以必须允许建完再补 —— 建任务那一步没有填答案的地方。

    ⚠️ 已经批出结果的不能再改（422）：那批判定是按旧依据批的，
    改了依据却留着旧判定，比不改更糟。要改就点「重新批改」。
    """
    try:
        grade = store.patch_setup(db, gid, payload)
    except store.GradeError as e:
        raise _fail(e) from e
    return store.detail(db, grade)


@router.post("/homework-grades/{gid}/run")
def run_grade(gid: int, db: Session = Depends(get_db)):
    """开始（或重跑）批改。**幂等**：已经在跑就直接返回当前进度，不会再起一个线程。

    返回的是立刻可用的详情：`status` 可能是 `queued`（刚认领）或 `running`，
    前端拿它开始轮询 `GET /{gid}` 即可。
    """
    try:
        grade = store.get_grade(db, gid)
    except store.GradeError as e:
        raise _fail(e) from e
    store.launch(db, grade)          # 已在跑时 launch 自己会什么都不做
    return store.detail(db, grade)


@router.post("/homework-grades/{gid}/confirm")
def confirm_grade(gid: int, payload: GradeConfirmIn | None = None, db: Session = Depends(get_db)):
    """老师确认整份。**确认之前，这里的判定只是 AI 初稿**，任何统计口径都不该读它。

    body 可以整个不传（`note` 目前没有落点，见 store.confirm_grade 的 TODO）——
    别让一条"只是点了个确认"的请求因为没带 `{}` 就被 422 顶回来。
    """
    try:
        grade = store.confirm_grade(db, gid, payload or GradeConfirmIn())
    except store.GradeError as e:
        raise _fail(e) from e
    return store.detail(db, grade)


@router.get("/homework-grades/{gid}/page/{pno}")
def grade_page_image(gid: int, pno: int, db: Session = Depends(get_db)):
    """第 pno 页的 PNG（整份作业的页号，1 起）。

    页图**不复制**到批改目录下当资产：那是缓存的衍生物，源件在 lesson_files。
    缓存落在本任务自己的目录里（store.grade_dir），不同任务不会串页。
    """
    try:
        png = store.page_png(db, gid, pno)
    except store.GradeError as e:
        raise _fail(e) from e
    return FileResponse(str(png), media_type="image/png", filename=f"p{pno}.png")


@router.get("/homework-grades/{gid}/export")
def export_grade_file(gid: int, fmt: str = Query("pdf"), page: int = Query(1),
                      db: Session = Depends(get_db)):
    """导出**带批改痕迹的副本**：`fmt=pdf` 整份多页，`fmt=png` 单页。

    ⚠️ 只改副本，**原件一个字不动** —— 学生真迹要留档，老师下次可能还要换支笔再批一遍。
    """
    try:
        path, name = grade_export.export_grade(db, gid, fmt, page)
    except store.GradeError as e:
        raise _fail(e) from e
    media = "application/pdf" if path.suffix.lower() == ".pdf" else "image/png"
    return FileResponse(str(path), media_type=media, filename=name)


# ---------------------------------------------------------------- 逐题
@router.patch("/grade-items/{iid}")
def patch_item(iid: str, payload: GradeItemPatch, db: Session = Depends(get_db)):
    """改一道题的判定 / 得分 / 考点 / 错因。

    ⚠️ 传了**新的** `student_answer` 就会让这一题的旧判定作废
    （verdict→ungradable、分数清空、`transcription_version`+1），这是服务层的硬规则，
    前端只要把老师改好的文本传上来，不要自己"保留旧判定"。
    """
    try:
        it = store.patch_item(db, iid, payload)
    except store.GradeError as e:
        raise _fail(e) from e
    return store.item_dict(it)


@router.post("/grade-items/{iid}/rerun")
def rerun_item(iid: str, db: Session = Depends(get_db)):
    """只重批这一题（老师改完错字、或觉得模型看错了）。

    同步返回：单题重批是老师盯着屏幕等的操作，返回时结果已经落库了。
    代价是会阻塞一次模型调用（十秒级），路由是同步 def，跑在线程池里，不堵事件循环。
    """
    try:
        it = store.rerun_item(db, iid)
    except store.GradeError as e:
        raise _fail(e) from e
    return store.item_dict(it)


@router.post("/grade-items/{iid}/to-bank")
def item_to_bank(iid: str, payload: GradeToBankIn, db: Session = Depends(get_db)):
    """错题挂到题库已有题上（B1：**不自动建题**，找不到就引导老师去录题）。"""
    try:
        it = store.to_bank(db, iid, payload)
    except store.GradeError as e:
        raise _fail(e) from e
    return store.item_dict(it)


# ---------------------------------------------------------------- 标注
@router.post("/homework-grades/{gid}/marks")
def add_mark(gid: int, payload: GradeMarkIn, db: Session = Depends(get_db)):
    """加一条标注。坐标是**按页归一化的 0~1000 整数**，越界会被夹回去。"""
    try:
        m = store.add_mark(db, gid, payload)
    except store.GradeError as e:
        raise _fail(e) from e
    return store.mark_dict(m)


@router.post("/homework-grades/{gid}/marks/batch")
def add_marks(gid: int, payload: GradeMarksIn, db: Session = Depends(get_db)):
    """一次补发多条标注 —— 给批改页的**本地草稿队列**用。

    为什么有这一条：老师画完一笔就关页面时，那一个 POST 会被浏览器取消。
    前端把没送出去的存在本机（`frontend/src/gradeDraft.js`），下次打开再补发；
    攒了三十笔就发三十次的话，在「刚打开、还在拉详情」的那几秒里很容易又断。
    """
    try:
        rows = store.add_marks(db, gid, payload)
    except store.GradeError as e:
        raise _fail(e) from e
    return {"marks": [store.mark_dict(m) for m in rows]}


@router.patch("/grade-marks/{mid}")
def patch_mark(mid: int, payload: GradeMarkPatch, db: Session = Depends(get_db)):
    """拖动 / 改颜色 / 改文字。**没传的字段 = 不改**。"""
    try:
        m = store.patch_mark(db, mid, payload)
    except store.GradeError as e:
        raise _fail(e) from e
    return store.mark_dict(m)


@router.delete("/grade-marks/{mid}")
def delete_mark(mid: int, db: Session = Depends(get_db)):
    try:
        store.delete_mark(db, mid)
    except store.GradeError as e:
        raise _fail(e) from e
    return {"ok": True}


# ---------------------------------------------------------------- 一题一区域
# 这一段是「整页一次判所有题」之外的另一条口径：老师框出每道题的作答在哪，
# 每框单独裁切、单独调模型。**没有框的页仍然走整页**，所以是纯增量。
@router.get("/homework-grades/{gid}/regions")
def list_regions(gid: int, db: Session = Depends(get_db)):
    """这份作业画过的所有框（按页号 + 页内顺序）。"""
    try:
        store.get_grade(db, gid)
    except store.GradeError as e:
        raise _fail(e) from e
    return {"regions": store.list_regions(db, gid)}


@router.put("/homework-grades/{gid}/regions")
def put_regions(gid: int, payload: GradeRegionsPut, db: Session = Depends(get_db)):
    """**整份替换**这份作业画的框（每项是「一段」，同一 `seq` 的几段合成一题）。

    为什么从「按页替换」改成「整份替换」：跨页题的两段天然落在不同页上，
    按页提交时「一题两段」这个组合没有任何一个时刻能被原子写进去。
    传空数组 = 清空全部框（整份退回整页批）。
    """
    try:
        regions = store.put_regions(db, gid, payload)
    except store.GradeError as e:
        raise _fail(e) from e
    return {"regions": regions}


@router.post("/homework-grades/{gid}/regions/copy")
def copy_regions(gid: int, payload: GradeRegionsCopy, db: Session = Depends(get_db)):
    """沿用同一节课里另一份的框（一套卷只画一次）。

    ⚠️ 拷的是坐标，不是模板 —— 不同学生的照片有平移/旋转，框会偏。
    界面要提示「宁大勿小」，拷完建议扫一眼。
    """
    try:
        regions = store.copy_regions(db, gid, payload.from_grade_id)
    except store.GradeError as e:
        raise _fail(e) from e
    return {"regions": regions}


@router.get("/homework-grades/{gid}/region-image")
def grade_region_image(gid: int, spec: str, zoom: float = Query(1.6, ge=0.5, le=3.0),
                       db: Session = Depends(get_db)):
    """把一块（可跨页多段）现裁成 PNG **直接返回、不落盘** —— 给「切出来的到底是什么」做即时预览。

    这是「分割截图完后看不到图」那一问的正解：**保存前**就要能看（所以按坐标 spec
    而不是按 region id），老师才能当场发现「这一刀切歪了」。

    `spec` 形如 `1:40,540,1000,800`，跨页用 `;` 连接（与 documents 的同名接口同一套语法，
    量纲同为 0~1000）。
    """
    try:
        png = store.region_preview_png(db, gid, spec, zoom)
    except store.GradeError as e:
        raise _fail(e) from e
    return Response(
        content=png,
        media_type="image/png",
        # 同一份卷子的同一块切出来是确定的 → 让浏览器别重复问。
        # private：这是学生卷子，不进中间缓存。
        headers={"Cache-Control": "private, max-age=600"},
    )


@router.patch("/homework-grades/{gid}/skipped-pages")
def patch_skipped_pages(gid: int, payload: GradePageSkipsIn, db: Session = Depends(get_db)):
    """整页不送 AI（0030）：封页、空白作答页、草稿页。

    **整份替换**，与框同一套口径 —— 前端手上就有完整集合，
    发一次替换不会出现「删了第 2 页、第 5 页却被旧值带回来」。
    只存页号、**不动框**：取消跳过后框还在，不用重画。
    """
    try:
        return store.set_page_skips(db, gid, payload.pages)
    except store.GradeError as e:
        raise _fail(e) from e
