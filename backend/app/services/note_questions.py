# -*- coding: utf-8 -*-
"""把题库里的题插进笔记：块语法 + 快照 + 一份笔记出两版。

块长这样（两段注释把题干与答案分开）：

    <!--q:sq1-->            引用的是哪道题（可溯源）
    因式分解：x^2-1          题干（Markdown，可含公式与图片）
    <!--qa-->
    (x-1)(x+1)              答案（**学生版会把这一整段去掉**）
    <!--qe-->

两个关键决定：

  · **为什么用注释分隔而不是写死文本**：导出时要能按版本决定答案渲不渲染，
    而注释在 Markdown 预览里是**不显示**的（marked 会把 HTML 注释剥掉），
    所以老师看到的就是正常内容，不是一堆标记。
    学生版是「从文件里删掉答案」，不是「渲染时隐藏」——
    docx 是个 zip，写进去再藏是藏不住的。

  · **为什么图片要复制一份（快照）**：`delete_question` 会按引用计数删掉
    `/api/crops/` 里的截图。笔记里只留 URL 的话，老师删一道题，
    **学生讲义里的图会静默消失**。复制进笔记资源目录（`nb_` 前缀）之后，
    它受笔记自己的引用计数保护（保存正文时按引用清理），
    而且 `notes_export.add_picture()` 直接就能内嵌（它只认 `nb_`）。
"""
from __future__ import annotations

import re

from .. import config
from . import images as image_svc

# 块标记。三处（插入、渲染预览、导出）都用这一份定义 —— 各写一份迟早会不一致。
BEGIN_TMPL = "<!--q:{qid}-->"
ANS_MARK = "<!--qa-->"
END_MARK = "<!--qe-->"

BEGIN_RE = re.compile(r"<!--q:([A-Za-z0-9_\-]+)-->")
# 整块：题干 + 可选答案。非贪婪，避免把后面别的块一起吃掉。
BLOCK_RE = re.compile(
    r"<!--q:([A-Za-z0-9_\-]+)-->(.*?)(?:<!--qa-->(.*?))?<!--qe-->", re.S)


def has_block(md: str) -> bool:
    return bool(BLOCK_RE.search(md or ""))


def snapshot_image(url: str | None) -> str:
    """把题库截图复制进笔记资源目录，返回新的 URL。

    复制而不是引用：见模块开头。取不到文件（比如已经被清理掉）就返回空串 ——
    插题不该因为一张图丢了而整件事失败，缺图在正文里看得见。
    """
    if not url:
        return ""
    name = url.rsplit("/", 1)[-1]
    src = config.CROPS_DIR / name
    if not src.is_file():
        return ""
    try:
        saved = image_svc.save_image(src.read_bytes(), config.NOTES_DIR, prefix="nb")
    except Exception:
        return ""
    return f"/api/notes/files/{saved['name']}"


def build_block(q, index: int | None = None) -> str:
    """把一道题变成块文本。`index` 只影响标题里的序号（学生版看着像练习册）。"""
    stem = (q.content or "").strip()
    img = snapshot_image(q.image)
    if img:
        stem = (stem + "\n\n" if stem else "") + f"![题目]({img})"
    answer = (q.answer or "").strip()
    aimg = snapshot_image(q.answer_image)
    if aimg:
        answer = (answer + "\n\n" if answer else "") + f"![答案]({aimg})"
    head = f"**{'练习 ' + str(index)}**：" if index else ""
    out = [BEGIN_TMPL.format(qid=q.id), f"{head}{stem}".strip()]
    if answer:
        out.append(ANS_MARK)
        out.append(answer)
    out.append(END_MARK)
    return "\n".join(out)


def unmark(md: str) -> str:
    """教师版：把标记换成正常排版（答案前面加一行「答案：」）。

    注意加粗的写法是 `**答案**：` 而不是 `**答案：**`：中文紧跟在收尾的 `**` 后面时
    不符合 CommonMark 的收尾规则，渲染出来会是**字面的星号**（浏览器里实测到的）。
    """
    def sub(m):
        qid, stem, answer = m.group(1), m.group(2), m.group(3)
        body = stem.strip()
        if answer and answer.strip():
            body += "\n\n**答案**：" + answer.strip()
        return body
    return BLOCK_RE.sub(sub, md or "")


def strip_answers(md: str) -> str:
    """学生版：**把答案那一整段删掉**（连标记一起），不是隐藏。"""
    def sub(m):
        qid, stem = m.group(1), m.group(2)
        return stem.strip()
    return BLOCK_RE.sub(sub, md or "")


def render(md: str, with_answer: bool) -> str:
    """导出前的预处理：交给 notes_export 的永远是**普通 Markdown**，
    所以那条 Markdown → docx/pdf 的管线一行都不用改。"""
    return unmark(md) if with_answer else strip_answers(md)
