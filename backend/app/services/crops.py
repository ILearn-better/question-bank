# -*- coding: utf-8 -*-
"""截图的引用计数 —— 决定一张图能不能真的从磁盘上抹掉。

`data/uploads/crops/` 里的图是「框选那一刻」落盘的，数据库里只留一个 URL。
引用它的那行记录一没，图就再没有入口 —— 成了谁也看不见、却一直占着盘的垃圾。
实测过一次：题库 0 行，磁盘上 92 张图 3.4 MB，全是学生试卷的截图。
用户以为删掉了，文件其实还在。

所以删除一律走**引用计数**，不直接删单个引用：同一张图可能被题干和答案同时引用
（一份原貌图既当题目又当答案），也可能被别的题的答案图用着。先收齐全库还剩谁在用，
再抹没人要的那些。

删题（routers/questions.py）与删文档连带删题（routers/documents.py）都走这里 ——
这套判断只留一份，不让两个路由各写各的，否则迟早出现「从这边删干净、从那边删漏」。

⚠️ 时序：算「释放了哪些图」必须在**数据库行的 DELETE 生效之后**再算，
否则刚删掉的那几行还会被算成「有人引用」，图就永远留着。
预演（还没删、想先看会释放什么）则相反 —— 用 `exclude_qids` 把待删的那批排除掉。
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import BatchItem, Question


def crop_names(*urls: str | None) -> set[str]:
    """从 image / answer_image 的 URL 里取出截图文件名（只认文件名，不认目录）。"""
    names: set[str] = set()
    for u in urls:
        if u:
            name = os.path.basename(u.split("?")[0].strip())
            if name:
                names.add(name)
    return names


def referenced_crops(db: Session, exclude_qids: Sequence[str] = ()) -> set[str]:
    """当前库里**所有**还活着的引用指向的截图文件名。

    三类引用都要算：
      · questions.image / answer_image —— 已入库的题（题干与答案的原貌图）
      · **questions.figure_image** —— 题干配图（从原卷上框出来的那一幅）。
        ⚠️ 漏了它：老师辛苦框出来的配图会在删题时被当成「无人引用」真删掉 ——
        而删题时看到「crops_removed: N」只会以为删的是原貌图。
      · batch_items.image —— 批量入库的**待审条目**。⚠️ 漏了它就会出事：
        批量任务刚跑完、老师还没审，这时去删那份源卷子，文档删除会按引用计数
        回收截图 —— 如果只数 questions，这些原貌图全是「无人引用」，
        会被真删掉，待审列表里立刻变成一片碎图。
        （`batch_jobs` 删除时 items 连带删除，那时它们才该被回收。）
      · batch_items.figure_image —— 待审条目上已经框好的配图，同理。

    exclude_qids：把这几道题当作已经不存在。仅用于「删之前预演会释放哪些图」——
    这时行还在库里，只能靠排除法算出删完之后的样子。
    """
    stmt = select(Question.image, Question.answer_image, Question.figure_image)
    if exclude_qids:
        stmt = stmt.where(Question.id.notin_(list(exclude_qids)))
    refs: set[str] = set()
    for img, ans, fig in db.execute(stmt).all():
        refs |= crop_names(img, ans, fig)
    for img, fig in db.execute(select(BatchItem.image, BatchItem.figure_image)).all():
        refs |= crop_names(img, fig)
    return refs


def purge(candidates: Iterable[str]) -> int:
    """删掉这些截图文件，返回真正删掉的个数。

    文件本来就不在、或删不动时静默跳过：清理失败不该让「删题 / 删文档」跟着失败 ——
    库里那行已经删了，因为一个残留文件就回滚，用户会觉得「怎么删不掉」。
    """
    removed = 0
    for name in candidates:
        try:
            (config.CROPS_DIR / name).unlink()
            removed += 1
        except OSError:
            pass
    return removed
