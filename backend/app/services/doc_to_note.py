# -*- coding: utf-8 -*-
"""把一份现成的文档（Word / PDF / HTML / PPT / 纯文本）变成一篇**笔记**。

和 `file_text.py` 的区别，别混用：
  · `file_text` 是**给 AI 看的**：只要能抽出连续文字就行，结构无所谓。
  · 这里是**给人看的**：笔记正文是 Markdown，标题、加粗、列表、表格、图片位置
    都保留下来，打开才像一份笔记而不是一坨文字。

两种失败必须分清楚（这个项目的纪律，见 file_text 的注释）：

  · **真的没有内容**（空文档 / 全是图的 Word）→ 报错，说清为什么、能怎么办。
  · **没有文字层但不是空的**（扫描版 PDF）→ **不是失败**：按页转成图片放进笔记，
    并明说「这是按页存的图片，不是文字」。老师要的正是能在上面用画笔标注。

转换注定有失真，所以每条都**写在笔记开头**，而不是悄悄丢掉：
  · Word 里的公式（OMML）→ 只留它的文字形式，不转成 `$…$`（反向 XSLT 没做）
  · HTML 里的**外链图片**不进笔记（本机不联网），保留成链接 + 提示
  · PDF/PPT 的版面（分栏、页眉页脚）还原不了，按页给个小标题

原始文件**一并存下来**并在笔记里给链接：转出来的 Markdown 只是"能读的那部分"，
原件才是"一定没丢的那部分"。
"""
from __future__ import annotations

import re
import uuid
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from .. import config
from . import file_text
from .file_text import ExtractError

# 超过这个页数就只转图片的那部分：一份 200 页的扫描件塞进一篇笔记没人看得动
MAX_SCAN_PAGES = 40
SCAN_ZOOM = 2.0
MAX_CHARS = 300_000

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
_MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
             "image/bmp": ".bmp", "image/webp": ".webp"}


class DocImportError(RuntimeError):
    """导入不了，消息可以直接给用户看。"""


def _save_asset(data: bytes, ext: str) -> str:
    """把配图/原件落到笔记的资源目录，返回文件名（正文里用 /api/notes/files/<name> 引用）。"""
    config.NOTES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"nb_{uuid.uuid4().hex[:12]}{ext}"
    (config.NOTES_DIR / name).write_bytes(data)
    return name


def _safe_title(filename: str) -> str:
    stem = Path(filename or "").stem or "导入的文档"
    return re.sub(r"\s+", " ", stem).strip()[:80] or "导入的文档"


def _clip(md: str) -> tuple[str, bool]:
    if len(md) > MAX_CHARS:
        return md[:MAX_CHARS] + "\n\n（内容太长，已截断）", True
    return md, False


# ================================================================ Word
def _rels(z: zipfile.ZipFile) -> dict[str, str]:
    """word/_rels/document.xml.rels -> {rId: 目标}（外部链接的 TargetMode=External 也留着）。"""
    out: dict[str, str] = {}
    try:
        from lxml import etree
        root = etree.fromstring(z.read("word/_rels/document.xml.rels"))
    except (KeyError, Exception):        # noqa: B014  文件损坏时当作没有关系表
        return out
    for rel in root.iter(PKG_REL + "Relationship"):
        out[rel.get("Id")] = rel.get("Target") or ""
    return out


def _numbering(z: zipfile.ZipFile) -> dict[str, str]:
    """numId -> 列表格式（decimal / bullet / …）。用来决定该写 `1.` 还是 `-`。

    不查这张表的话，Word 里的有序列表会被一律写成 `-`，条理就丢了。
    """
    fmts: dict[str, str] = {}
    try:
        from lxml import etree
        root = etree.fromstring(z.read("word/numbering.xml"))
    except (KeyError, Exception):        # noqa: B014
        return fmts
    abstract = {}
    for an in root.iter(W + "abstractNum"):
        lvl = an.find(W + "lvl")
        fmt = lvl.find(W + "numFmt") if lvl is not None else None
        abstract[an.get(W + "abstractNumId")] = (fmt.get(W + "val") if fmt is not None else "bullet")
    for num in root.iter(W + "num"):
        ref = num.find(W + "abstractNumId")
        if ref is not None:
            fmts[num.get(W + "numId")] = abstract.get(ref.get(W + "val"), "bullet")
    return fmts


def _para_md(p, rels: dict[str, str], z: zipfile.ZipFile, saved: dict) -> tuple[str, bool]:
    """一个 w:p -> 一行 Markdown。返回 (文本, 是否含公式)。"""
    from lxml import etree
    ppr = p.find(W + "pPr")
    text_parts: list[str] = []
    has_math = False

    def runs_in(node) -> None:
        nonlocal has_math
        for child in node:
            tag = child.tag
            if tag == W + "r":
                rpr = child.find(W + "rPr")
                bold = rpr is not None and rpr.find(W + "b") is not None
                ital = rpr is not None and rpr.find(W + "i") is not None
                buf = []
                for t in child.iter(W + "t"):
                    buf.append(t.text or "")
                for b in child.iter(W + "br"):
                    buf.append(" ")
                for d in child.iter(W + "drawing"):        # 图片：就地插进来
                    buf.append(_drawing_md(d, rels, z, saved))
                s = "".join(buf)
                if not s:
                    continue
                s = s.replace("**", "")                    # 防止把 Markdown 语法搞乱
                if bold and s.strip():
                    s = f"**{s.strip()}**" if s.strip() else s
                if ital and s.strip() and not bold:
                    s = f"*{s.strip()}*"
                text_parts.append(s)
            elif tag == W + "hyperlink":                   # 超链接：保留文字与地址
                inner = []
                for t in child.iter(W + "t"):
                    inner.append(t.text or "")
                label = "".join(inner)
                url = rels.get(child.get(R + "id") or "", "")
                text_parts.append(f"[{label}]({url})" if url.startswith("http") and label else label)
            elif tag == M + "oMath" or tag == M + "oMathPara":
                has_math = True
                text_parts.append("".join(t.text or "" for t in child.iter(M + "t")))
            else:
                runs_in(child)

    runs_in(p)
    text = "".join(text_parts).strip()

    style = ""
    if ppr is not None:
        st = ppr.find(W + "pStyle")
        style = ((st.get(W + "val") or "") if st is not None else "")
    low = style.lower()
    if low.startswith("heading") or low in ("标题",) or re.match(r"^heading\s*\d", low):
        n = re.search(r"(\d)", low)
        level = min(int(n.group(1)) if n else 2, 6)
        return (f"{'#' * level} {text}" if text else ""), has_math
    if low in ("title", "标题1") or low == "title":
        return (f"# {text}" if text else ""), has_math

    numpr = ppr.find(W + "numPr") if ppr is not None else None
    if numpr is not None:
        ilvl = numpr.find(W + "ilvl")
        numid = numpr.find(W + "numId")
        level = int(ilvl.get(W + "val") or 0) if ilvl is not None else 0
        nid = (numid.get(W + "val") or "") if numid is not None else ""
        fmt = z_cache["numbering"].get(nid, "bullet")
        marker = "1." if fmt in ("decimal", "lowerLetter", "upperLetter", "lowerRoman",
                                 "upperRoman", "chineseCounting", "japaneseCounting") else "-"
        return f"{'  ' * level}{marker} {text}", has_math
    return text, has_math


def _drawing_md(d, rels: dict[str, str], z: zipfile.ZipFile, saved: dict) -> str:
    """w:drawing 里的图 -> Markdown 图片（图片字节从包里的 media 取，落进笔记资源目录）。"""
    for blip in d.iter(A + "blip"):
        rid = blip.get(R + "embed") or blip.get(R + "link")
        target = rels.get(rid or "", "")
        if not target:
            continue
        if target.startswith(("http://", "https://")):
            saved["external"] += 1
            return ""                                   # 不联网，外链图片直接不要
        member = "word/" + target.lstrip("/")
        try:
            data = z.read(member)
        except KeyError:
            continue
        ext = Path(member).suffix.lower()
        if ext not in IMG_EXT:
            continue
        name = _save_asset(data, ext)
        saved["images"] += 1
        return f"![图片](/api/notes/files/{name})"
    return ""


def _table_md(tbl, rels, z, saved) -> str:
    rows = []
    for tr in tbl.findall(W + "tr"):
        cells = []
        for tc in tr.findall(W + "tc"):
            parts = []
            for p in tc.iter(W + "p"):
                s, _ = _para_md(p, rels, z, saved)
                s = re.sub(r"^[#>\-\*\s]+", "", s).replace("\n", " ").strip()
                if s:
                    parts.append(s)
            cells.append(" / ".join(parts).replace("|", "\\|"))
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


z_cache: dict = {}          # 一次转换里的临时缓存（numbering 表等）


def docx_to_markdown(path: Path) -> dict:
    from lxml import etree

    try:
        # ⚠️ 必须用 with 把 zip 关掉：Windows 上句柄没关时**临时目录都删不掉**，
        # 导入接口收尾时会报「另一个程序正在使用此文件」（WinError 32）→ 整个请求 500。
        # 这个坑在 Linux 上根本不会出现，所以更值得写死在注释里。
        with zipfile.ZipFile(path) as z:
            try:
                xml = z.read("word/document.xml")
            except KeyError as e:
                raise DocImportError(f"这个 .docx 里找不到正文（文件可能损坏）：{e}") from e

            rels = _rels(z)
            z_cache["numbering"] = _numbering(z)
            saved = {"images": 0, "external": 0, "math": 0}
            root = etree.fromstring(xml)
            body = root.find(W + "body")
            if body is None:
                raise DocImportError("这个 .docx 里没有正文")

            blocks: list[str] = []
            for el in body:
                if el.tag == W + "p":
                    line, has_math = _para_md(el, rels, z, saved)
                    if has_math:
                        saved["math"] += 1
                    if line:
                        blocks.append(line)
                elif el.tag == W + "tbl":
                    t = _table_md(el, rels, z, saved)
                    if t:
                        blocks.append(t)
    except zipfile.BadZipFile as e:
        raise DocImportError("这个文件不是有效的 .docx（可能下载不完整或已损坏）") from e

    warnings = []
    if saved["math"]:
        warnings.append(f"文档里有 {saved['math']} 处公式，只保留了它的文字形式，"
                        "没有转成可编辑的公式")
    if saved["external"]:
        warnings.append(f"文档里有 {saved['external']} 张外链图片没有存进来（本机不联网），"
                        "如果重要请手动截图贴进来")
    if not any(b.strip() for b in blocks):
        raise DocImportError(
            "这个 Word 文档里没有文字（内容可能全是图片截图）。"
            "如果是纯图片的卷子，请把图片直接贴进笔记（Ctrl+V），或用「交作业」上传原件。"
        )
    return {"markdown": "\n\n".join(blocks), "images": saved["images"], "warnings": warnings,
            "kind_cn": "Word"}


# ================================================================ HTML
class _HtmlToMd(HTMLParser):
    """HTML -> Markdown 的一个够用子集。

    刻意不引 html2text 之类的库：零新依赖是这个项目的一条硬要求（依赖全部本地化），
    而常见教学文档用到的标签就那么十几个。
    """

    SKIP = {"script", "style", "head", "meta", "link", "noscript"}
    BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "figure",
             "figcaption", "dl", "dt", "dd", "tr", "table", "tbody", "thead"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        self.list_stack: list[str] = []          # 'ul' / 'ol'
        self.ol_counters: list[int] = []
        self.in_pre = False
        self.table_rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.href: str | None = None
        self.saved = {"images": 0, "remote": 0}
        # 空标签要用：`<strong></strong>` 不能变成 `****`（那在 Markdown 里是四个星号）。
        # 开标记时记下当时已写入的正文长度，闭合时没变就说明里面一个字都没有 → 撤掉开标记。
        self.text_chars = 0
        self.inline_open: list[tuple[str, int]] = []

    # ---- 小工具
    def _buf(self) -> list:
        return self.cell if self.cell is not None else self.out

    def _nl(self, n=1):
        if self.out and not self.out[-1].endswith("\n" * n):
            self.out.append("\n" * n)

    def _txt(self, s):
        if s:
            self._buf().append(s)

    def _open(self, mark):
        self.inline_open.append((mark, self.text_chars))
        self._txt(mark)

    def _close(self, mark):
        if not self.inline_open:
            return
        _, at = self.inline_open.pop()
        if self.text_chars == at:              # 里面是空的：把开标记撤回去
            if self._buf() and self._buf()[-1] == mark:
                self._buf().pop()
            return
        self._txt(mark)

    # ---- 标签
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl(2)
            self._txt("#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self._nl(2)
        elif tag == "br":
            self._nl()
        elif tag in ("strong", "b"):
            self._open("**")
        elif tag in ("em", "i"):
            self._open("*")
        elif tag == "code":
            if not self.in_pre:
                self._open("`")
        elif tag == "pre":
            self._nl(2)
            self._txt("```\n")
            self.in_pre = True
        elif tag == "blockquote":
            self._nl(2)
            self._txt("> ")
        elif tag in ("ul", "ol"):
            self._nl()
            self.list_stack.append(tag)
            self.ol_counters.append(0)
        elif tag == "li":
            self._nl()
            depth = max(0, len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1] == "ol":
                self.ol_counters[-1] += 1
                self._txt("  " * depth + f"{self.ol_counters[-1]}. ")
            else:
                self._txt("  " * depth + "- ")
        elif tag == "a":
            self.href = a.get("href") or ""
            if self.href.startswith("http"):
                self._open("[")
        elif tag == "img":
            self._img(a)
        elif tag == "hr":
            self._nl(2)
            self._txt("---")
            self._nl(2)
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in ("strong", "b"):
            self._close("**")
        elif tag in ("em", "i"):
            self._close("*")
        elif tag == "code":
            if not self.in_pre:
                self._close("`")
        elif tag == "pre":
            self._txt("\n```")
            self._nl(2)
            self.in_pre = False
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self.ol_counters.pop()
            self._nl()
        elif tag == "a":
            if self.href and self.href.startswith("http"):
                _, at = self.inline_open.pop() if (self.inline_open
                                                   and self.inline_open[-1][0] == "[") else (None, None)
                if at is None:
                    pass
                elif self.text_chars == at:                 # 空的链接：别留个光秃秃的 []
                    if self._buf() and self._buf()[-1] == "[":
                        self._buf().pop()
                else:
                    self._txt(f"]({self.href})")
            self.href = None
        elif tag in ("td", "th"):
            if self.row is None:
                self.row = []
            self.row.append(" ".join("".join(self.cell or []).split()))
            self.cell = None
        elif tag == "tr":
            if self.row:
                self.table_rows.append(self.row)
            self.row = None
        elif tag == "table":
            self._flush_table()
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6") or tag in self.BLOCK:
            self._nl(2)

    def handle_data(self, data):
        if self.skip_depth or not data:
            return
        if self.in_pre:
            self._txt(data)
            self.text_chars += len(data.strip())
        else:
            # ⚠️ 这里**不能**给每个文本节点补一个尾空格：
            # `<strong>相加</strong>` 会变成 `**相加 **`，而 Markdown 的结束标记前面
            # 不允许有空格 —— 加粗就默默失效了（踩过）。只把连续空白压成一个空格即可。
            s = re.sub(r"\s+", " ", data)
            self._txt(s)
            self.text_chars += len(s.strip())

    def _img(self, a):
        src = (a.get("src") or "").strip()
        alt = a.get("alt") or "图片"
        if src.startswith("data:"):
            # 内嵌图（html 里存了 base64）—— 内容就在文件里，落盘存进笔记
            import base64
            m = re.match(r"data:(image/[a-z+]+);base64,(.+)", src, re.S)
            if m and m.group(1).lower() in _MIME_EXT:
                try:
                    name = _save_asset(base64.b64decode(m.group(2)), _MIME_EXT[m.group(1).lower()])
                    self.saved["images"] += 1
                    self._txt(f"\n\n![{alt}](/api/notes/files/{name})\n\n")
                    return
                except Exception:                     # noqa: BLE001  坏的 base64 就当没有这张图
                    pass
        elif src.startswith(("http://", "https://")):
            # 外链图：本机不联网，取不回来。留个链接，别静默丢
            self.saved["remote"] += 1
            self._txt(f"[{alt}]({src})")
            return
        self._txt(f"![{alt}]({src})")

    def _flush_table(self):
        if not self.table_rows:
            return
        width = max(len(r) for r in self.table_rows)
        rows = [r + [""] * (width - len(r)) for r in self.table_rows]
        self._nl(2)
        self._txt("| " + " | ".join(rows[0]) + " |\n")
        self._txt("|" + "---|" * width + "\n")
        for r in rows[1:]:
            self._txt("| " + " | ".join(r) + " |\n")
        self.table_rows = []
        self._nl(2)

    def result(self) -> str:
        self._flush_table()
        s = "".join(self.out)
        s = re.sub(r"\*\*[ \t]*\*\*", "", s)      # 只清真正空的加粗（正常的 `**a** **b**` 不会被误删）
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()


def html_to_markdown(path: Path) -> dict:
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise DocImportError("这个 HTML 不是常见的编码（试过 UTF-8 / GB18030）")
    parser = _HtmlToMd()
    parser.feed(text)
    md = parser.result()
    if not md.strip():
        raise DocImportError("这个 HTML 里没有可读的正文（可能全是脚本或图片）")
    warnings = []
    if parser.saved["remote"]:
        warnings.append(f"页面里有 {parser.saved['remote']} 张外链图片没取回来（本机不联网），"
                        "已保留成链接；重要的图请手动截图贴进来")
    return {"markdown": md, "images": parser.saved["images"], "warnings": warnings, "kind_cn": "HTML"}


# ================================================================ PDF / PPT
def pdf_to_markdown(path: Path) -> dict:
    """有文字层就抽文字；没有就**按页转成图片**（这不是失败，是这份 PDF 的形态）。"""
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        pages = doc.page_count
        chunks, scanned = [], 0
        for i, page in enumerate(doc, start=1):
            t = file_text._norm(page.get_text() or "")
            if t:
                chunks.append(f"## 第 {i} 页\n\n{t}" if pages > 1 else t)
            else:
                scanned += 1
        if chunks:
            warnings = []
            if scanned:
                warnings.append(f"有 {scanned} 页没有文字层（图片页），这些页的内容没有转成文字")
            return {"markdown": "\n\n".join(chunks), "images": 0, "warnings": warnings,
                    "kind_cn": "PDF", "pages": pages}

        # 整份都没有文字 → 按页存图。扫描的卷子/讲义就该这么看
        if scanned == 0:
            raise DocImportError("这个 PDF 里没有内容")
        warnings = []
        limit = min(pages, MAX_SCAN_PAGES)
        imgs = []
        for i in range(limit):
            pix = doc[i].get_pixmap(matrix=pymupdf.Matrix(SCAN_ZOOM, SCAN_ZOOM))
            name = _save_asset(pix.tobytes("png"), ".png")
            imgs.append(f"### 第 {i + 1} 页\n\n![第 {i + 1} 页](/api/notes/files/{name})")
        if pages > limit:
            warnings.append(f"共 {pages} 页，只转了前 {limit} 页（再多一篇笔记就看不动了）")
        warnings.append("这份 PDF 没有文字层（扫描件或图片导出），已按页转成图片存进笔记 —— "
                        "图片不能搜索/不能复制文字，但可以直接用画笔在上面标注")
        return {"markdown": "\n\n".join(imgs), "images": len(imgs), "warnings": warnings,
                "kind_cn": "PDF（扫描件）", "pages": pages, "scanned": True}


def pptx_to_markdown(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        if not names:
            raise DocImportError(f"这个 PPT 里找不到幻灯片。{file_text.SUPPORTED_NOTE}")
        names.sort(key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        from lxml import etree
        from .file_text import A_NS
        chunks = []
        for n in names:
            idx = int(re.search(r"(\d+)", n).group(1))
            root = etree.fromstring(z.read(n))
            lines = []
            for p in root.iter(A_NS + "p"):
                s = "".join(t.text or "" for t in p.iter(A_NS + "t")).strip()
                if s:
                    lines.append(s)
            if lines:
                chunks.append(f"## 第 {idx} 页\n\n" + "\n\n".join(lines))
    if not chunks:
        raise DocImportError("这个 PPT 里没有文字 —— 内容可能全在图片里。"
                             "可以把它另存为 PDF 再导入，扫描/图片页会按页存成图片。")
    return {"markdown": "\n\n".join(chunks), "images": 0, "warnings": [], "kind_cn": "PPT"}


# ================================================================ 入口
def to_markdown(path: Path, filename: str) -> dict:
    """按类型分发。返回 {markdown, kind_cn, warnings, images, pages?}。"""
    z_cache.clear()
    kind = file_text.kind_of(filename) or ""
    try:
        if kind == "word":
            r = docx_to_markdown(path)
        elif kind == "html":
            r = html_to_markdown(path)
        elif kind == "pdf":
            r = pdf_to_markdown(path)
        elif kind == "ppt":
            r = pptx_to_markdown(path)
        elif kind == "text":
            text, meta = file_text._plain(path)
            if Path(filename).suffix.lower() == ".md":
                r = {"markdown": text, "images": 0, "warnings": [], "kind_cn": "Markdown"}
            else:
                r = {"markdown": text, "images": 0, "warnings": [], "kind_cn": "文本"}
        elif kind in ("word-legacy", "rtf", "ppt-legacy"):
            # 旧格式交给本机 Word 转 PDF，再走 PDF 那条路（这样扫描/图片页也能按页存图）
            r = _legacy_via_pdf(path, filename)
        else:
            raise DocImportError(f"暂时不支持导入这种格式（{Path(filename).suffix or '无扩展名'}）。"
                                 f"{file_text.SUPPORTED_NOTE}")
    except DocImportError:
        raise
    except file_text.ExtractError as e:      # 复用那套会直接展示给人看的文案
        raise DocImportError(str(e)) from e
    except Exception as e:                   # noqa: BLE001
        raise DocImportError(f"解析这个文件时出错：{type(e).__name__}: {e}") from e
    md, truncated = _clip(r.pop("markdown"))
    if not md.strip():
        # 空文件不能静默变成一篇「只有来源说明」的空笔记 —— 那比失败更让人糊涂。
        # 和 file_text 同一条纪律：抽不出来就说清为什么。
        raise DocImportError(
            f"这个文件里没有可读的内容（{r.get('kind_cn', '文件')}）。"
            "如果内容都在图片里，请把图片直接贴进笔记（Ctrl+V），或用「交作业」上传原件。"
        )
    if truncated:
        r.setdefault("warnings", []).append("内容太长，已截断到 30 万字")
    r["markdown"] = md
    r["chars"] = len(md)
    return r


def _legacy_via_pdf(path: Path, filename: str) -> dict:
    import os
    import tempfile

    from ..adapters import office
    from .file_text import ExtractError

    if not office.is_available():
        raise DocImportError(f"读不了这个旧格式文件：{office.availability_note()}。"
                             "把它在 Word 里「另存为 .docx / .pptx」再导入即可。")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "converted.pdf")
        try:
            office.word_to_pdf(path, out)
        except (office.OfficeUnavailable, office.WordConvertError) as e:
            raise ExtractError(f"用本机 Word 打开这个文件失败：{e}") from e
        r = pdf_to_markdown(Path(out))
        r["warnings"] = list(r.get("warnings", [])) + ["旧格式（.doc/.rtf）先由本机 Word 转成 PDF 再导入"]
        return r


def save_original(data: bytes, filename: str) -> str:
    """把原件也存进笔记资源目录，返回文件名。原件才是「一定没丢的那部分」。"""
    ext = Path(filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext or ""):
        ext = ".bin"
    return _save_asset(data, ext)


def header_block(filename: str, kind_cn: str, info: dict, original: str) -> str:
    """笔记开头那段「这份笔记是怎么来的」。失真与警告都写在这一段里，不藏着。

    写进正文而不是只弹个提示：过两天再打开这篇笔记时，得能看出它是导入的、
    原件在哪、哪部分不准。要嫌碍事，直接删掉这段引用块即可。
    原件的文件名一定是 ASCII（nb_<hex>.<ext>），所以链接不需要转义。
    """
    parts = [kind_cn]
    if info.get("pages"):
        parts.append(f"{info['pages']} 页")
    if info.get("images"):
        parts.append(f"含 {info['images']} 张图")
    parts.append(f"{info.get('chars', 0)} 字")
    lines = [f"> 从「{filename}」导入（{'，'.join(parts)}）",
             f"> 原始文件：[{filename}](/api/notes/files/{original})"]
    for w in info.get("warnings", []):
        lines.append(f"> ⚠️ {w}")
    return "\n".join(lines) + "\n\n"
