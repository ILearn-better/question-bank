# -*- coding: utf-8 -*-
"""Word 渲染适配层 —— **全项目唯一 import win32com 的地方**。

为什么需要它：
    用户要的是「自己在页面上画框选区」——文本选择 + 图片选择。
    要做到这一点，必须先把 .docx 渲染成**带版式的页面图**，
    否则只能像以前那样由后端先把文档切成段落块、用户被动选块范围。

为什么用本机 Word 而不是别的：
    Microsoft Word 的 COM 接口导出 PDF 的版式保真度最高（公式、图形、分页都准），
    而且用户机器上已经装了 Word，不需要额外下载 LibreOffice。

⚠️ 收敛纪律（与 adapters/pdf.py 完全一致）：
    Word COM 只在 Windows + 装了 Word 的机器上可用。将来交付给同行时，
    若目标机器没有 Word，**只需替换本文件**（换 LibreOffice
    `soffice --headless --convert-to pdf`，或换成纯解析实现），
    路由层与前端一行都不用改。这是把它做成适配层的全部理由。

线程约束（重要）：
    Word 是单线程单元（STA）对象，且 COM 要求每个调用线程先 CoInitialize。
    FastAPI 的同步路由跑在线程池里，所以这里既做 CoInitialize，
    又用全局锁把并发调用串起来 —— 两个老师同时转同一个文档不是本工具的形态，
    但一次误触导致两个转换撞车是有的。
"""
from __future__ import annotations

import os
import threading
import uuid

# Word 的 ExportAsFixedFormat: 17 = wdExportFormatPDF
WD_EXPORT_FORMAT_PDF = 17
# 页面统计: 2 = wdStatisticPages
WD_STAT_PAGES = 2

# 全局串行锁：Word COM 不可并发调用
_LOCK = threading.Lock()

# 可用性探测结果缓存（探测本身要启动 Word，代价高，只做一次）
_PROBE: dict = {"done": False, "ok": False, "reason": ""}


class OfficeUnavailable(RuntimeError):
    """本机不具备 Word 渲染能力（未装 pywin32 / 未装 Word）。"""


class WordConvertError(RuntimeError):
    """Word 装了，但这次转换失败（被占用、文件损坏、超时等）。"""


def _probe() -> tuple[bool, str]:
    """探测本机 Word 是否可用。结果缓存，避免每次转换都白等一次启动。"""
    if _PROBE["done"]:
        return _PROBE["ok"], _PROBE["reason"]

    _PROBE["done"] = True

    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
    except ImportError as e:
        _PROBE["ok"] = False
        _PROBE["reason"] = f"未安装 pywin32（{e.name}），无法调用 Word 渲染：pip install pywin32"
        return _PROBE["ok"], _PROBE["reason"]

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        ver = ""
        try:
            ver = str(word.Version)
        except Exception:  # noqa: BLE001
            pass
        _PROBE["ok"] = True
        _PROBE["reason"] = f"Microsoft Word {ver}".strip()
    except Exception as e:  # noqa: BLE001
        _PROBE["ok"] = False
        _PROBE["reason"] = f"未检测到可用的 Microsoft Word（{type(e).__name__}）。请安装 Word，或改用 PDF 上传。"
    finally:
        if word is not None:
            try:
                word.Quit(0)
            except Exception:  # noqa: BLE001
                pass
        pythoncom.CoUninitialize()

    return _PROBE["ok"], _PROBE["reason"]


def is_available() -> bool:
    return _probe()[0]


def availability_note() -> str:
    return _probe()[1]


def _convert_locked(src: str, out_pdf: str) -> int:
    """真正的转换动作（调用方必须已持锁且已 CoInitialize）。返回页数。"""
    import win32com.client

    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        # 全部静默：不出可见窗口、不弹对话框、不自动更新链接、不执行宏
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            word.AutomationSecurity = 3          # msoAutomationSecurityForceDisable
        except Exception:  # noqa: BLE001
            pass
        try:
            word.Options.UpdateLinksAtOpen = False
            word.Options.ConfirmConversions = False
        except Exception:  # noqa: BLE001
            pass

        doc = word.Documents.Open(
            src,
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
            Visible=False,
        )
        # 命名参数是 pywin32 动态派发生成的 DISPPARAMS，实测可用
        doc.ExportAsFixedFormat(OutputFileName=out_pdf, ExportFormat=WD_EXPORT_FORMAT_PDF)
        try:
            pages = int(doc.ComputeStatistics(WD_STAT_PAGES))
        except Exception:  # noqa: BLE001
            pages = 0
        return pages
    finally:
        if doc is not None:
            try:
                doc.Close(0)          # wdDoNotSaveChanges
            except Exception:  # noqa: BLE001
                pass
        if word is not None:
            try:
                word.Quit(0)
            except Exception:  # noqa: BLE001
                pass


def word_to_pdf(src: str | os.PathLike, out_pdf: str | os.PathLike) -> int:
    """把 .docx 导出为 PDF（带版式），返回页数。

    失败时抛 OfficeUnavailable（本机没这个能力）或 WordConvertError（这次没转成）。
    调用方据此决定：回退到内容块模式，还是提示用户重试。

    注意：输出先写临时文件再改名 —— 避免「转换到一半被中断」留下一个
    半截 PDF 被后续请求当成有效缓存。
    """
    ok, reason = _probe()
    if not ok:
        raise OfficeUnavailable(reason)

    src = str(src)
    out_pdf = str(out_pdf)
    os.makedirs(os.path.dirname(out_pdf), exist_ok=True)
    tmp = os.path.join(os.path.dirname(out_pdf), f".{uuid.uuid4().hex[:8]}.tmp.pdf")

    import pythoncom

    with _LOCK:
        pythoncom.CoInitialize()          # 线程池线程必须先初始化 COM
        try:
            try:
                pages = _convert_locked(src, tmp)
            except Exception as e:  # noqa: BLE001
                raise WordConvertError(
                    f"Word 渲染失败：{type(e).__name__}: {e}。"
                    "若是 Word 正被占用，请关闭 Word 窗口后重试。"
                ) from e
        finally:
            pythoncom.CoUninitialize()

    if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        raise WordConvertError("Word 没有产出 PDF（文档可能为空或已损坏）")

    # 校验产出的 PDF 真的能打开，再放行
    try:
        import pymupdf

        with pymupdf.open(tmp) as d:
            real_pages = d.page_count
    except Exception as e:  # noqa: BLE001
        os.remove(tmp)
        raise WordConvertError(f"产出的 PDF 无法解析：{e}") from e

    if real_pages == 0:
        os.remove(tmp)
        raise WordConvertError("产出的 PDF 没有页面（文档可能为空）")

    os.replace(tmp, out_pdf)              # 原子替换，缓存永远指向完整文件
    return pages or real_pages
