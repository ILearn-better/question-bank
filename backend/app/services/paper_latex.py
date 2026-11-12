# -*- coding: utf-8 -*-
"""出卷 PDF：真正的 LaTeX 排版（XeLaTeX / tectonic），不是「把公式渲成图片再贴上去」。

为什么换掉 PyMuPDF 那条路
--------------------------
旧实现用 `insert_htmlbox` + matplotlib 把公式渲成 PNG，问题不是"不够好看"而是
**根本不是排版**：公式图的字重、字号、基线都是近似的，行内公式会把行距顶得参差，
`\\begin{cases}` 这类环境 mathtext 又不认，只能自己拼字。老师看一眼就知道不像真卷。

现在这条路：题库里存的本来就是 LaTeX，直接把 `$…$` 交给 LaTeX —— **数学零转换**，
出来就是 Computer Modern 的真排版。中文走 xeCJK + 系统字体（SimSun / SimHei）。

外部依赖
--------
`tectonic.exe`（单文件 19MB，随用随下宏包并缓存到 %LOCALAPPDATA%\\TectonicProject）。
- 首次编译要联网拉宏包（本机实测约 5 分钟），之后约 7 秒；缓存过后 `--only-cached` 可离线跑。
- 找不到引擎 / 编译失败时**返回 None**，由 `paper_export.build_pdf` 回退到旧的 PyMuPDF
  路径 —— 导出宁可旧一点，也不能直接报错（老师正在出卷时崩掉最糟）。
- 二进制不进仓库（见 .gitignore 的 `backend/vendor/`），用 `tools/安装-LaTeX引擎.bat` 装。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import config

# 编译超时。40 题实测 7 秒；给足余量，但不能无限等（老师会以为卡死）。
TIMEOUT = 240

# 走到这里说明 tectonic 自己出错了：把它当"没有引擎"处理，回退旧路径。
_LAST_ERROR = ""


def last_error() -> str:
    """上一次失败的原因（给日志用）。不静默失败 —— 出了问题要能查到为什么。"""
    return _LAST_ERROR


# ---------------------------------------------------------------- 引擎定位
def tectonic_bin() -> Path | None:
    """按优先级找 tectonic：环境变量 → 仓库内 vendor/ → PATH。"""
    env = os.environ.get("SHIKE_TECTONIC")
    if env:
        p = Path(env)
        if p.is_file():
            return p
    # backend/app/services → backend/vendor
    vendor = Path(__file__).resolve().parents[2] / "vendor"
    for name in ("tectonic.exe", "tectonic"):
        p = vendor / name
        if p.is_file():
            return p
    which = shutil.which("tectonic")
    return Path(which) if which else None


def available() -> bool:
    return tectonic_bin() is not None


# ---------------------------------------------------------------- 文本转义
# LaTeX 的十个特殊字符。**只对数学之外的文字做** —— 数学段原样保留，
# 否则 `x_1` 会被转成 `x\_1`（下划线变字面量），公式全废。
_TEXT_ESC = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_TEXT_ESC_RE = re.compile(r"[\\&%$#_{}~^]")


def escape_text(s: str | None) -> str:
    return _TEXT_ESC_RE.sub(lambda m: _TEXT_ESC[m.group(0)], s or "")


def _tight(s: str) -> str:
    """局部关掉「中西文自动加缝」，让「甲部(1)」「（4分）」紧排。

    xeCJK 的设置是跟着组走的，包一层花括号就只影响这一段。
    """
    return "{\\xeCJKsetup{CJKecglue={}}%s}" % s


def tex_body(text: str | None) -> str:
    """题干 / 答案：文字转义，`$…$` 与 `$$…$$` 原样交给 LaTeX。

    分段靠 `latex_img.split_math`（和 DOCX / 旧 PDF 同一条分段规则）——
    自己再写一份正则，迟早跟另外两条路段的边界不一样。
    """
    from . import latex_img

    out: list[str] = []
    for part in latex_img.split_math(text or ""):
        if part.get("t") == "math":
            tex = (part.get("tex") or "").strip()
            if not tex:
                continue
            out.append(f"\\[{tex}\\]" if part.get("display") else f"${tex}$")
        else:
            # `\n` 必须显式换成 `\\`：LaTeX 源码里的**单个换行只是空格**，
            # 不换成断行的话，题库里分段存的题干（每行一句）会全挤成一段。
            out.append(escape_text(part.get("v")).replace("\n", "\\\\\n"))
    return "".join(out)


# ---------------------------------------------------------------- 图片
def _copy_for_tex(src: Path | None, work: Path, name: str) -> str | None:
    """把图片拷进编译目录，返回可写进 \\includegraphics 的相对文件名。

    必须拷：tectonic 只保证能读编译目录（工作根）里的文件，
    直接写绝对路径在带空格 / 中文的目录下会踩坑（本机就是 `Program Files`）。
    """
    if not src or not Path(src).is_file():
        return None
    suffix = Path(src).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".pdf"):
        suffix = ".png"
    dst = work / f"{name}{suffix}"
    try:
        shutil.copyfile(src, dst)
    except OSError:
        return None
    return dst.name


# ---------------------------------------------------------------- LaTeX 源
# ⚠️ 这个常量后面要过 `%` 格式化，**里面不能出现裸的 `%`**（注释也不行）——
#    要写百分号得写 `%%`。加注释一律写在上面，别塞进导言区。
# 中西文之间 xeCJK 会自动加一点空隙，正文里这样最好看（「满分 150 分」）。
# 但卷种里有两处必须紧排，否则不像真卷：「甲部(1)」会被排成「甲部 (1)」、
# 「（4分）」会被排成「（4 分）」—— 这两处单独用 `_tight()` 局部关掉，
# ⚠️ 千万别图省事在导言区全局关：那会把老师自己打的空格一起吃掉。
_PREAMBLE = r"""\documentclass[11pt,a4paper]{article}
\usepackage[top=%(top).1fmm,bottom=%(top).1fmm,left=%(side).1fmm,right=%(side).1fmm]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{graphicx}
\usepackage{xeCJK}
\setmainfont{Times New Roman}
\setCJKmainfont{SimSun}[AutoFakeBold=2.5]
\setsansfont{Microsoft YaHei}
\linespread{%(linespread).2f}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\setlength{\emergencystretch}{2em}
\pagestyle{plain}
\makeatletter
\renewcommand\@makefntext[1]{\parindent 0pt #1}
\makeatother
\begin{document}
"""


def _score_table(cols: int) -> str:
    """卷头「题号 / 得分」登分表。"""
    head = " & ".join([""] + [str(i) for i in range(1, cols - 1)])
    return (
        "\\begin{center}\\small\n"
        f"\\begin{{tabular}}{{|c|{'c|' * (cols - 1)}}}\n\\hline\n"
        f"题号{head} \\\\ \\hline\n"
        f"得分{' & ' * (cols - 1)} \\\\ \\hline\n"
        "\\end{tabular}\n\\end{center}\n\\vspace{0.4em}\n"
    )


def _answer_lines(n: int, gap: float) -> str:
    """解答题下方留的作答横线（和旧 PDF 的 rule() 一个观感）。

    每条都必须自成一段（结尾 `\\par`）：只写 `\\vspace` + `\\rule` 的话，相邻两条会
    落进同一个段落里，TeX 不给它们加 baselineskip —— 表现是「有的地方两条挤在一起、
    有的地方很开」，这种不匀比不留白更显眼。
    """
    if n <= 0:
        return ""
    one = "\\vspace{%.1fpt}\\noindent\\rule{\\linewidth}{0.4pt}\\par\n" % max(12.0, gap * 2.2)
    return one * n


def build(title: str, items: list[dict], opts: dict, tpl=None) -> bytes | None:
    """生成 LaTeX 源并编译成 PDF。失败返回 None（调用方回退旧路径）。"""
    global _LAST_ERROR
    _LAST_ERROR = ""

    exe = tectonic_bin()
    if exe is None:
        _LAST_ERROR = "找不到 tectonic（装一个：tools/安装-LaTeX引擎.bat，或设 SHIKE_TECTONIC）"
        return None

    # 这几个函数在 paper_export 里（它是调用方）—— 函数内 import 避开循环引用
    from . import paper_export as pe
    from . import paper_style

    t = paper_style.resolve(tpl)
    paper, style = t["paper"], t["style"]
    top_mm, side_mm = style["margin_mm"]

    work = Path(tempfile.mkdtemp(prefix="shike_tex_"))
    try:
        tex: list[str] = [_PREAMBLE % {
            "top": top_mm,
            "side": side_mm,
            # 模板给的是「倍数」（1.8 = 1.8 倍行距）。LaTeX 的 linespread 也是倍数，
            # 但基线本身已含 1.2 的 leading，所以按 1.2 折算才不会比预览松一大截。
            "linespread": max(0.8, float(style["line_height"]) / 1.2),
        }]

        # ---------------- 抬头区（与 HTML / DOCX 同一份 head_blocks 数据）
        tex.append("\\begin{center}\n{__TITLE__}\n\\end{center}\n")
        for b in pe.head_blocks(paper, opts):
            if b["kind"] in ("subtitle", "note"):
                tex.append("\\begin{center}\\small %s\\end{center}\n" % escape_text(b["text"]))
            elif b["kind"] == "fill":
                fields = b["fields"]
                cells = "\\qquad ".join(
                    "%s：\\underline{\\hspace{3cm}}" % escape_text(f) for f in fields)
                tex.append("\\begin{center}\\small %s\\end{center}\n" % cells)
            elif b["kind"] == "instructions":
                tex.append("\\vspace{0.3em}\n{\\small\\bfseries %s}\n\n\\begin{enumerate}\n"
                           "\\setlength{\\itemsep}{0.1em}\\small\n" % escape_text(b["title"]))
                for x in b["items"]:
                    tex.append("  \\item %s\n" % escape_text(x))
                tex.append("\\end{enumerate}\n\\vspace{0.3em}\n")
            elif b["kind"] == "score_table":
                tex.append(_score_table(int(b["cols"])))

        img_no = 0
        # ---------------- 卷面
        for g in paper_style.layout(items, t["sections"]):
            if g["title"]:
                if g["page_break"]:
                    tex.append("\\clearpage\n")
                tex.append("\n\\vspace{1.0em}\n{\\size_section\\bfseries %s}\\par\n\\vspace{0.25em}\n"
                           % _tight(escape_text(g["title"])))
            note = paper_style.section_note(g["note"], g["items"], style) if (g["title"] or g["note"]) else ""
            if note:
                tex.append("{\\small %s}\\par\\vspace{0.35em}\n" % escape_text(note))

            for it in g["items"]:
                tag = ""
                if opts.get("show_tags", True):
                    line = pe._tag_line(it)
                    if line:
                        tag = "（%s）" % escape_text(line)
                no = paper_style.num_text(it["n"], style)
                score = pe.score_text(it, style)
                form = pe.body_form(it, opts.get("render_mode"))

                if form == "image":
                    tex.append("\n\\noindent\\textbf{%s}%s%s\n\n"
                               % (escape_text(no), _tight(escape_text(score)), tag))
                    img_no += 1
                    name = _copy_for_tex(pe.image_path(it.get("image")), work, f"q{img_no}")
                    if name:
                        tex.append("\\includegraphics[width=\\linewidth]{%s}\n" % name)
                else:
                    tex.append("\n\\noindent\\textbf{%s} %s%s%s\n\n"
                               % (escape_text(no), tex_body(pe._stem_text(it)),
                                  _tight(escape_text(score)), tag))
                    img_no += 1
                    fname = _copy_for_tex(pe.figure_path(it), work, f"q{img_no}")
                    if fname:
                        tex.append("\\begin{center}\\includegraphics[width=0.64\\linewidth]{%s}\\end{center}\n" % fname)
                tex.append(_answer_lines(pe.blank_lines(it, style), float(style["question_gap"])))

        # ---------------- 参考答案
        if opts.get("show_answer"):
            tex.append("\n\\clearpage\n\\begin{center}{\\large\\bfseries 参考答案与解析}\\end{center}\n\\vspace{0.5em}\n")
            for it in paper_style.flat(items, t["sections"]):
                if pe.answer_form(it, opts.get("render_mode")) == "image":
                    tex.append("\n\\noindent\\textbf{%s}\n\n" % escape_text(paper_style.num_text(it["n"], style)))
                    img_no += 1
                    name = _copy_for_tex(pe.image_path(it.get("answer_image")), work, f"a{img_no}")
                    if name:
                        tex.append("\\includegraphics[width=0.6\\linewidth]{%s}\n" % name)
                else:
                    tex.append("\n\\noindent\\textbf{%s} %s\n\n"
                               % (escape_text(paper_style.num_text(it["n"], style)),
                                  tex_body(it.get("answer") or "（未填）")))
                if opts.get("show_analysis") and it.get("analysis"):
                    tex.append("{\\small 解析：%s}\n\n" % tex_body(it["analysis"]))

        tex.append("\\end{document}\n")

        src = "".join(tex).replace(
            "__TITLE__",
            "\\fontsize{%(ts).1fpt}{%(tls).1fpt}\\selectfont\\bfseries %(t)s" % {
                "ts": float(style["title_size"]),
                "tls": float(style["title_size"]) * 1.35,
                "t": escape_text(title),
            },
        )
        # 分区标题字号：模板可调，但 LaTeX 里没有"只改这一处"的全局量，
        # 用一条 \fontsize 命令就地套（放在正文里而不是导言区）。
        src = src.replace("\\size_section", "\\fontsize{%(s).1fpt}{%(sl).1fpt}\\selectfont" % {
            "s": float(style["section_size"]),
            "sl": float(style["section_size"]) * 1.4,
        })

        (work / "paper.tex").write_text(src, encoding="utf-8")
        cmd = [str(exe), "-X", "compile", "paper.tex", "-o", "."]
        try:
            proc = subprocess.run(cmd, cwd=str(work), capture_output=True,
                                  timeout=TIMEOUT, text=True, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            _LAST_ERROR = f"tectonic 编译超时（>{TIMEOUT}s）"
            return None
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
            _LAST_ERROR = "tectonic 编译失败：%s" % " / ".join(tail)
            return None
        out = work / "paper.pdf"
        if not out.is_file():
            _LAST_ERROR = "tectonic 说成功了但没生成 paper.pdf"
            return None
        return out.read_bytes()
    finally:
        shutil.rmtree(work, ignore_errors=True)
