# -*- coding: utf-8 -*-
"""判定依据（答案 / 评分细则）的文件口径：**收哪些格式、抽成什么文字、多少份多大**。

三种判定模式里，`key` / `rubric` 要靠老师给的依据 —— 依据可能是粘贴的文字，
也可能是一份文件。这个文件只管**文件那一半**：格式白名单、抽取、限额。

## 为什么不往 `file_text.py` 里加

`file_text.KINDS` 是「上课材料 / 作业原件」共用的白名单。往那里加 Excel，
**课时页的上传框**、备课材料、批量录题会一起多出 xls/xlsx 选项 ——
那不是这次要动的地方（用户明确：不要改动批改工作台以外的模块）。
而 Excel 的抽取规则（按行拼接单元格）也只对「答案 / 细则」有意义：
一份课件表格拼成一行行字，对润色反馈没有帮助。

## 三条不能松的口径

1. **图片走视觉通道，不抽文字**。答案常常就是一张答案页照片 —— 抽不出字很正当，
   拿「读出 0 字」去报错会吓到老师。真正的处理是 `_answer_images` 把原件送进模型。
2. **抽不出文字必须说出来**。扫描版 PDF 交给 `file_text` 会抛 `ExtractError`，
   这里不往上传失败上翻，而是**收下文件 + 返回 note**，界面上如实显示
   「这份没读出文字，模型看不见它」—— 静默收下一个看不见的依据，
   等于让老师以为「按我的答案判了」，实际模型还是在自己解。
3. **老式 `.xls` 直接拒收**，不假装支持。本机 venv 没有 xlrd（也不打算为它装一个
   只读 .xls 的依赖），收下来只会得到一份 0 字的依据。让老师另存为 .xlsx 更诚实。

⚠️ 本文件**不 import pymupdf**（AGPL 只在 `adapters/pdf.py` 一处）。
   PDF 的抽取借 `file_text`，图片只交给上层去送模型。
"""
from __future__ import annotations

from pathlib import Path

from . import file_text

# 一次批改**最多挂几份**依据。10 份够一个老师把整份答案 + 细则 + 几张补充照片放完；
# 再多，模型上下文的钱是次要的，主要是一份份看不过来 —— 也说明该合并成一个文件了。
MAX_BASIS_FILES = 10

# 单份上限 20MB。与 lesson_files 的 30MB 不同值是有意的：那边收课件视频截图，
# 这边只收「文字答案」，20MB 已经很宽（一张手机拍的答案页通常 2~4MB）。
MAX_BASIS_BYTES = 20 * 1024 * 1024

# 图片：**原样送进模型看**（不抽文字）。与 grade_store._IMAGE_EXTS 同集合。
IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic", ".heif"})

# 表格类：本文件自己抽（openpyxl / csv），不走 file_text。
# ⚠️ 刻意不含 `.xls`：没有解析库，收下来就是一份 0 字的依据 —— 见文件头第 3 条。
SHEET_EXTS = frozenset({".xlsx", ".xlsm", ".csv"})

# 其余格式全部交给 file_text（PDF / Word / PPT / 文本 / 网页）。
_DELEGATED = frozenset(file_text.KINDS) - IMAGE_EXTS

EXTS = frozenset(IMAGE_EXTS | SHEET_EXTS | _DELEGATED)

# 给 `<input type="file">` 与界面文案用。⚠️ 前端**不设** accept 属性（本机经验：
# accept 会把老师想选的文件藏起来，比选错更烦），这里只用于「能选什么」的说明。
ACCEPT = ",".join(sorted(EXTS))

SUPPORTED_NOTE = (
    "支持图片（照片/截图）、PDF、Word(.docx)、Excel(.xlsx/.csv)、PPT(.pptx)、文本(.txt/.md)；"
    "老式 .doc/.ppt 需要本机装有 Word，老式 .xls 请另存为 .xlsx"
)

# 抽取出来的文字上限。与 file_text 同一个值 —— 一份答案不该有 2 万字，
# 真到那个量级说明挑错文件了，让模型读半份不如让老师换一份。
MAX_CHARS = file_text.MAX_CHARS


def ext_of(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def kind_of(filename: str) -> str | None:
    """这份文件归哪一类；不认识的返回 None（调用方据此 422）。"""
    ext = ext_of(filename)
    if ext in IMAGE_EXTS:
        return "image"
    if ext in SHEET_EXTS:
        return "sheet"
    return file_text.kind_of(filename)


def is_image(filename: str) -> bool:
    return ext_of(filename) in IMAGE_EXTS


def is_legacy_xls(filename: str) -> bool:
    """`.xls` 单独认出来：它不是「不认识的格式」，而是「认识但解不了」，
    提示要说清怎么办（另存为 .xlsx），不能跟「不支持这种格式」混成一句。"""
    return ext_of(filename) == ".xls"


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text[:MAX_CHARS]


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))          # 3.0 → 3：答案里写成 3.0 会让人以为精度有意义
    return str(v).strip()


def _sheet_text(path: Path) -> tuple[str, str]:
    """把一张工作簿读成「工作表名 + 一行一格」的纯文本。

    为什么按行拼、而不是把单元格一个个列出来：答案表常见的形状是
    「题号 | 答案 | 分值」，按行拼能保住这个结构；打平成 `A1=1 A2=x=3`
    反而丢掉了「谁跟谁是一行」这件事，模型更容易把第 3 题的答案读到第 4 题上。
    """
    ext = path.suffix.lower()
    if ext == ".csv":
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "gbk", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        return _clip("\n".join(lines)), ""

    try:
        import openpyxl
    except ImportError:
        return "", "这台机器没装 openpyxl，读不了 Excel（可以另存为 .csv 再传）"
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as e:                        # noqa: BLE001
        return "", f"这个 Excel 打不开（{type(e).__name__}: {e}）"
    try:
        blocks: list[str] = []
        for ws in wb.worksheets:
            rows: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [_cell(c) for c in row]
                while cells and not cells[-1]:
                    cells.pop()
                if not any(cells):
                    continue
                rows.append(" | ".join(cells))
            if rows:
                blocks.append(f"### 工作表 {ws.title}\n" + "\n".join(rows))
        if not blocks:
            return "", "这个 Excel 里没有可读的内容（全空）"
        return _clip("\n\n".join(blocks)), ""
    finally:
        try:
            wb.close()
        except Exception:                          # noqa: BLE001
            pass


def _no_text_note(kind: str, why: str) -> str:
    """「收下了，但模型看不见它」时的提示。**必须说清下一步怎么办**。"""
    if kind == "pdf":
        return ("这份 PDF 里没有可复制的文字（多半是扫描件）—— 模型看不见里面的内容。"
                "请改成**拍成照片传上来**（png/jpg 会直接送进模型看），"
                "或者把答案打字出来。")
    if kind in ("word-legacy", "ppt-legacy", "rtf"):
        return f"这种老格式读不出文字（{why}）—— 请另存为 .docx / .pptx / PDF 再传。"
    return f"这份文件里没读出文字（{why}）—— 模型暂时看不见它，换一份或把答案打字出来。"


def extract(path: Path, name: str) -> dict:
    """抽出一份依据文件的**文字**。**永不抛异常** —— 失败也返回 note。

    返回 `{kind, text, note}`：
      · `kind` —— 界面上的类型标签（image / sheet / pdf / word / text / …）
      · `text` —— 送给模型的文字；图片恒为空串（走视觉通道）
      · `note` —— 非空 = **这份现在等于没给模型看**，界面必须显示出来
    """
    kind = kind_of(name) or ""
    if kind == "image":
        # 不是「抽不出字」，而是本来就没有文字层 —— 图会原样送进模型。
        return {"kind": "image", "text": "", "note": ""}
    if kind == "sheet":
        text, note = _sheet_text(path)
        return {"kind": "sheet", "text": text, "note": note}
    try:
        r = file_text.extract(path, name)
    except file_text.ExtractError as e:
        # ⚠️ 不直接抄 file_text 的原话：那句是给「上课材料」写的
        #    （「请把要点打字写进反馈」），放到判定依据上完全对不上号。
        return {"kind": kind or "", "text": "", "note": _no_text_note(kind, str(e))}
    except Exception as e:                        # noqa: BLE001
        # file_text 说它只抛 ExtractError，但解析器（pymupdf / Word COM）什么都可能抛。
        # 一份依据读不出来不该让整个上传 500 —— 收下，把原因说出来。
        return {"kind": kind or "", "text": "", "note": f"读这个文件时出错（{type(e).__name__}: {e}）"}
    return {"kind": kind or "", "text": _clip(r.get("text") or ""), "note": ""}
