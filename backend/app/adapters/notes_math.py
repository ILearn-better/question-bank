# -*- coding: utf-8 -*-
"""公式渲染适配层：LaTeX -> MathML -> OMML（Word 原生公式）。

为什么需要这一层：
    笔记正文里全是 `$…$`。导出 Word / PDF 时如果直接印 `$…$` 原文，这份导出基本没用。
    要得到**真公式**（Word 里还能双击编辑的那种），唯一可行的链路是：
        LaTeX --KaTeX--> MathML --Office 的 MML2OMML.XSL--> OMML --> 塞进 docx
    OMML 是 Word 自己的公式格式，塞进去之后 Word 排版、转 PDF 都是原生效果。

为什么用 node + KaTeX 而不是 Python 的 latex2mathml：
    1. 前端预览用的就是同一份 frontend/vendor/katex/katex.min.js，
      **同一个引擎**出 MathML，导出的公式结构和屏幕上看到的一致，
      不会出现"预览里是对的、导出变了样"这种最难查的问题；
    2. 不需要新增 Python 依赖，也不需要联网装包（这台机器装包不稳）。
    代价是导出公式需要本机有 node（探测不到就退回纯文本，见下）。

降级策略（重要）：
    本模块任何一环不可用（没 node / 没 KaTeX / 没装 Office / XSL 缺失）时，
    **不抛异常**，而是返回 None，由调用方把公式按纯文本写上。
    导出功能本身永远不能因为"公式渲染不了"而整个失败 —— 正文、图片、板书照常导出。

⚠️ node 必须用**脚本文件**方式启动（katex_mathml.js），不能 `node -e`：
   实测这台机器上 `node -e` 会在启动期崩（Assertion failed: ncrypto::CSPRNG），
   `node file.js` 正常。另外 subprocess 必须显式 encoding="utf-8"，
   否则 Windows 默认用 GBK 解码，中文/特殊符号直接抛 UnicodeDecodeError。

⚠️ 线程安全：FastAPI 同步路由跑在线程池里，所以这里用锁保护 XSLT 编译与子进程调用。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from .. import config

# Office 里那份 MathML -> OMML 的样式表。位置历代 Office 都差不多，
# 按可能性从高到低找；也可以用 SHIKE_MML2OMML_XSL 直接指定。
_XSL_CANDIDATES = (
    r"C:\Program Files\Microsoft Office\root\Office16\MML2OMML.XSL",
    r"C:\Program Files (x86)\Microsoft Office\root\Office16\MML2OMML.XSL",
    r"C:\Program Files\Microsoft Office\Office16\MML2OMML.XSL",
    r"C:\Program Files (x86)\Microsoft Office\Office16\MML2OMML.XSL",
    r"C:\Program Files\Microsoft Office\root\Office15\MML2OMML.XSL",
    r"C:\Program Files (x86)\Microsoft Office\root\Office15\MML2OMML.XSL",
)

# node 子进程超时：正常一整篇笔记的公式一次调用几秒内完成，60s 是很宽的上限
_NODE_TIMEOUT = 60

_LOCK = threading.Lock()

# 可用性探测结果缓存（探测要起 node 进程、编译 XSLT，只做一次）
_PROBE: dict = {"done": False, "ok": False, "reason": ""}

# 编译好的 XSLT（构建一次复用；lxml 的 XSLT 对象不是线程安全的，用锁保护）
_XSLT = None


def katex_path() -> Path:
    """前端 vendored 的 KaTeX —— 和后端共用同一份文件，不另外下载。"""
    return config.FRONTEND_DIR / "vendor" / "katex" / "katex.min.js"


def js_helper_path() -> Path:
    return Path(__file__).resolve().parent / "katex_mathml.js"


def _find_xsl() -> Path | None:
    env = os.getenv("SHIKE_MML2OMML_XSL")
    if env:
        p = Path(env)
        return p if p.exists() else None
    for c in _XSL_CANDIDATES:
        p = Path(c)
        if p.exists():
            return p
    return None


def _probe() -> tuple[bool, str]:
    """探测公式渲染链路是否可用。只做一次，结果缓存。"""
    if _PROBE["done"]:
        return _PROBE["ok"], _PROBE["reason"]

    _PROBE["done"] = True

    node = shutil.which("node")
    if not node:
        _PROBE["reason"] = "本机没有 node，公式会以 $…$ 原文导出（装 node 后即可自动启用）"
        return _PROBE["ok"], _PROBE["reason"]

    if not katex_path().exists():
        _PROBE["reason"] = f"缺少 KaTeX 文件 {katex_path()}，公式会以原文导出"
        return _PROBE["ok"], _PROBE["reason"]

    xsl = _find_xsl()
    if xsl is None:
        _PROBE["reason"] = "未找到 Office 的 MML2OMML.XSL，公式会以原文导出"
        return _PROBE["ok"], _PROBE["reason"]

    try:
        _xslt(xsl)
    except Exception as e:  # noqa: BLE001
        _PROBE["reason"] = f"XSLT 编译失败（{type(e).__name__}: {e}），公式会以原文导出"
        return _PROBE["ok"], _PROBE["reason"]

    _PROBE["ok"] = True
    _PROBE["reason"] = ""
    return _PROBE["ok"], _PROBE["reason"]


def _xslt(xsl: Path):
    """编译并缓存 XSLT（调用方须持锁或接受竞态 —— 首次编译重复一两次无害）。"""
    global _XSLT
    if _XSLT is None:
        from lxml import etree
        _XSLT = etree.XSLT(etree.parse(str(xsl)))
    return _XSLT


def availability() -> tuple[bool, str]:
    """(是否可用, 不可用原因)。路由层用它告诉前端「导出时公式会怎样」。"""
    return _probe()


def _run_node(texs: list[str]) -> list[str] | None:
    """一次调用把所有公式转成 MathML。返回 None 表示这次整体失败。"""
    try:
        proc = subprocess.run(
            ["node", str(js_helper_path()), str(katex_path())],
            input=json.dumps(texs),
            capture_output=True,
            # 必须显式 utf-8：Windows 默认 GBK，中文/数学符号会解码失败
            encoding="utf-8",
            errors="replace",
            timeout=_NODE_TIMEOUT,
            cwd=str(js_helper_path().parent),
        )
    except Exception:  # noqa: BLE001  （超时、node 崩了等，一律当作"这次没成"）
        return None
    if proc.returncode != 0:
        return None
    try:
        out = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(out, list) or len(out) != len(texs):
        return None
    return [str(x) for x in out]


def latex_to_omml(texs: list[str]) -> list[str | None]:
    """把一批 LaTeX 转成 OMML 字符串。

    返回与输入等长的列表：失败项为 None（调用方按纯文本处理）。
    空输入直接返回空列表，不会去起 node 进程。
    """
    if not texs:
        return []

    ok, _ = _probe()
    if not ok:
        return [None] * len(texs)

    with _LOCK:
        mmls = _run_node(texs)
        if mmls is None:
            return [None] * len(texs)

        from lxml import etree
        xslt = _xslt(_find_xsl())
        out: list[str | None] = []
        for tex, mml in zip(texs, mmls):
            if not mml:
                out.append(None)
                continue
            try:
                result = xslt(etree.fromstring(mml.encode("utf-8")))
            except Exception:  # noqa: BLE001
                out.append(None)
                continue
            # 立刻序列化成字符串：XSLT 结果树绑定在 xslt 对象上，留着元素不保险
            s = str(result).strip()
            out.append(s or None)
        return out


# ---------------------------------------------------------------- OMML 拆分
# 返回 **bytes** 而不是 lxml 元素：python-docx 的 parse_xml() 只吃字符串/字节，
# 喂给它一个 lxml 元素会直接抛 TypeError。曾经因为外层 except 把它吞了，
# 结果「转换成功、注入失败」，导出的 Word 里全是 $…$ 原文 —— 排查成本极高。
def omml_display(omml: str) -> bytes:
    """独立公式：整体是 oMathPara（自己占一个显示段落）。"""
    return omml.encode("utf-8")


def omml_inline(omml: str) -> bytes:
    """内联公式：只取 oMath。

    oMathPara 是“独立成行的公式段落”容器，塞进正文段落里会把那一行拆开；
    内联要的是 oMath（它才是真正的公式内容）。
    """
    from lxml import etree
    root = etree.fromstring(omml.encode("utf-8"))
    for el in root.iter():
        if etree.QName(el).localname == "oMath":
            return etree.tostring(el)
    return etree.tostring(root)
