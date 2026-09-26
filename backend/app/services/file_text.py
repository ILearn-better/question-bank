# -*- coding: utf-8 -*-
"""把上课文件（PDF / Word / PPT / 文本）抽成**文字**，好让 AI 读懂。

为什么是「抽文字」而不是「把文件发给 AI」：
    本项目接的是 OpenAI 兼容的 /chat/completions，这是**纯文本**协议 ——
    deepseek-chat、qwen-plus 这类主流模型都不接受文件本身，只接受 messages 里的文字。
    所以做法是：在本机把文字抽出来，再把文字发出去。
    好处是任何文本模型都能用，而且**发出去的东西在界面上看得见**（符合本项目
    「数据存本机、发什么都要能审」的一贯做法）。
    代价是扫描版 PDF（没有文字层）和纯图片课件抽不出内容 —— 这种必须
    **明确告诉用户为什么、能怎么办**，绝不能静默当成空文件跳过。

零新依赖：
    · PDF   → pymupdf（已在用）
    · docx  → zipfile + lxml 直接读 OOXML（**用 XML 走一遍 w:p，能连表格和文本框里的字一起拿到**；
              python-docx 的 document.paragraphs 看不到文本框里的内容）
    · pptx  → 同上，读 ppt/slides/slideN.xml（注意按**数字**排序，否则 slide10 会排在 slide2 前面）
    · 老式 .doc/.rtf → 交给本机 Word 转成 PDF 再抽（adapters/office.py），没装 Word 就说清楚
"""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

# 单个文件抽出的文字上限。超了就截断 —— 但必须**告诉用户被截断了**，
# 静默截断会让人以为 AI 看全了。
MAX_CHARS = 20000

# 扩展名 -> 内部类别
KINDS = {
    ".pdf": "pdf",
    ".docx": "word",
    ".doc": "word-legacy",
    ".rtf": "rtf",
    ".pptx": "ppt",
    ".ppt": "ppt-legacy",
    ".txt": "text",
    ".md": "text",
}
# 送给前端的可读说明
KIND_CN = {
    "pdf": "PDF", "word": "Word", "word-legacy": "Word（旧格式）", "rtf": "RTF",
    "ppt": "PPT", "ppt-legacy": "PPT（旧格式）", "text": "文本",
}
ACCEPT = ",".join(sorted(KINDS))
SUPPORTED_NOTE = "支持 PDF / Word(.docx) / PPT(.pptx) / 文本；老式 .doc / .ppt / .rtf 需要本机装有 Word"

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


class ExtractError(RuntimeError):
    """抽不出文字。消息要写得**能照着做**，因为它会直接显示给用户。"""


def kind_of(filename: str) -> str | None:
    return KINDS.get(Path(filename or "").suffix.lower())


def _clip(text: str) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS], True
    return text, False


def _norm(text: str) -> str:
    """压掉多余空行。PDF 抽出来的文本经常一段里一堆空行，白占 token。"""
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- 各格式
def _pdf(path: Path) -> tuple[str, dict]:
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        pages = doc.page_count
        chunks = []
        for page in doc:
            t = page.get_text().strip()
            if t:
                chunks.append(t)
    text = _norm("\n\n".join(chunks))
    if not text:
        # 这是最常见的「说不清为什么没反应」的情况，所以话要说透
        raise ExtractError(
            f"这个 PDF（{pages} 页）没有文字层 —— 多半是扫描件或图片导出的 PDF。"
            "当前 AI 接口是纯文本的，看不到图里的内容。"
            "可以先用任意 OCR（微信/QQ 的「提取文字」也行）转成文本再上传，"
            "或者把要点直接打字写进反馈。"
        )
    return text, {"pages": pages}


def _ooxml_paragraphs(path: Path, member: str, ns: str) -> list[str]:
    """从 OOXML 部件里按**文档顺序**取所有段落文字。

    走 XML 而不是 python-docx 的 paragraphs：表格里的字、文本框里的字
    都在 w:p 里，用 XML 一次全拿到；python-docx 只给正文段落，会漏掉它们。
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read(member)
    from lxml import etree

    root = etree.fromstring(xml)
    out = []
    for p in root.iter(ns + "p"):
        s = "".join(t.text or "" for t in p.iter(ns + "t"))
        out.append(s)
    return out


def _docx(path: Path) -> tuple[str, dict]:
    try:
        lines = _ooxml_paragraphs(path, "word/document.xml", W_NS)
    except KeyError as e:
        raise ExtractError(f"这个 .docx 里找不到正文（文件可能损坏）：{e}") from e
    text = _norm("\n".join(lines))
    if not text:
        raise ExtractError(
            "这个 Word 文档里没有文字（可能内容全是图片截图）。"
            "当前 AI 接口是纯文本的，读不了图片里的内容，请把要点打字写进反馈。"
        )
    return text, {"paragraphs": len([x for x in lines if x.strip()])}


def _pptx(path: Path) -> tuple[str, dict]:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        if not names:
            raise ExtractError(
                "这个 PPT 里找不到幻灯片（可能是 .ppt 旧格式被改了扩展名，或文件损坏）。"
                f"{SUPPORTED_NOTE}"
            )
        # 必须按数字排：字符串排序会把 slide10 排到 slide2 前面
        names.sort(key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        chunks = []
        for n in names:
            idx = int(re.search(r"(\d+)", n).group(1))
            from lxml import etree

            root = etree.fromstring(z.read(n))
            lines = []
            for p in root.iter(A_NS + "p"):
                s = "".join(t.text or "" for t in p.iter(A_NS + "t"))
                if s.strip():
                    lines.append(s.strip())
            if lines:
                chunks.append(f"【第 {idx} 页】\n" + "\n".join(lines))
    text = _norm("\n\n".join(chunks))
    if not text:
        raise ExtractError(
            f"这个 PPT（{len(names)} 页）里没有文字 —— 内容可能全在图片里。"
            "当前 AI 接口是纯文本的，读不了图片，请把要点打字写进反馈。"
        )
    return text, {"pages": len(names)}


def _plain(path: Path) -> tuple[str, dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):     # 中文 txt 很可能是 GBK
        try:
            return _norm(raw.decode(enc)), {"encoding": enc}
        except UnicodeDecodeError:
            continue
    raise ExtractError("这个文本文件不是常见的编码（试过 UTF-8 / GB18030），换个方式导出试试。")


def _via_word(path: Path) -> tuple[str, dict]:
    """老式 .doc / .rtf / .ppt：交给本机 Word 转 PDF，再按 PDF 抽。"""
    import tempfile

    from ..adapters import office

    if not office.is_available():
        raise ExtractError(
            f"读不了这个旧格式文件：{office.availability_note()}。"
            "把它在 Word 里「另存为 .docx / .pptx」再上传即可。"
        )
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "converted.pdf")
        try:
            office.word_to_pdf(path, out)
        except (office.OfficeUnavailable, office.WordConvertError) as e:
            raise ExtractError(f"用本机 Word 打开这个文件失败：{e}") from e
        text, meta = _pdf(Path(out))
        meta["converted_by"] = "word"
        return text, meta


def extract(path: str | os.PathLike, filename: str | None = None) -> dict:
    """抽文字。返回可直接给前端/提示词用的结构；抽不出来就抛 ExtractError（消息可直接展示）。

    {kind, kind_cn, text, chars, truncated, pages?, paragraphs?, encoding?}
    """
    p = Path(path)
    name = filename or p.name
    kind = kind_of(name)
    if kind is None:
        raise ExtractError(
            f"暂不支持这种格式（{Path(name).suffix or '无扩展名'}）。{SUPPORTED_NOTE}"
        )

    try:
        if kind == "pdf":
            text, meta = _pdf(p)
        elif kind == "word":
            text, meta = _docx(p)
        elif kind == "ppt":
            text, meta = _pptx(p)
        elif kind == "text":
            text, meta = _plain(p)
        else:
            text, meta = _via_word(p)
    except ExtractError:
        raise
    except zipfile.BadZipFile as e:
        raise ExtractError(f"文件不是有效的 Office 文档（可能下载不完整或已损坏）：{e}") from e
    except Exception as e:  # noqa: BLE001
        raise ExtractError(f"解析这个文件时出错：{type(e).__name__}: {e}") from e

    text, truncated = _clip(text)
    if not text:
        # 抽出来是空的同样要报错：静默收下一个「0 字的文件」比失败更糟 ——
        # 用户会以为 AI 已经看过这份材料了。
        raise ExtractError(
            f"这个文件里没有可用的文字（{KIND_CN.get(kind, kind)}）。"
            "请确认内容是不是全在图片里；如果是，请把要点打字写进反馈。"
        )
    return {
        "kind": kind,
        "kind_cn": KIND_CN.get(kind, kind),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        **meta,
    }
