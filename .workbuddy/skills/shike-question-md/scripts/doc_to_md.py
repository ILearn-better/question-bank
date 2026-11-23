# -*- coding: utf-8 -*-
"""把 PDF / Word / 图片 转成「拾课」题库**可直接导入**的题目 md。

产出物长这样（老师拿去 Obsidian 里改一改，点同步就入库）：

    题库/抛物线的焦点.md
        ---
        shike: question
        qtype: 选择题
        difficulty: 中档
        knowledge:
          - 抛物线的定义与标准方程
        tags:
          - 焦点弦
        ---
        已知抛物线 $y^2=4x$ 的焦点为 $F$……（题干）

        ![](<../附件/抛物线的焦点-p03-02-fig.png>)

        ## 答案

        B

    附件/抛物线的焦点-p03-02-fig.png      ← 从第 3 页卷面上裁下来的那幅图

为什么不是「直接入库」而是先出 md：老师要能在 Obsidian 里改题干、补答案、换图，
改完点同步才进库。这一步把「识别 + 裁图 + 拼格式」的机械活干掉，判断留给人。

## 复用了谁

    app/adapters/pdf.render_page      PDF 页 → PNG（带缓存）
    app/adapters/office.word_to_pdf   .docx → PDF（本机 Word COM）
    app/services/batch_page.recognize_page  整页一次调用 → 多道题 + 配图框
    app/services/knowledge.resolve_kp       知识点名 → 知识树节点
    app/services/vault.render               一道题 → md 正文（**与网页导出同一份实现**）

最后一条是关键：md 的格式只有 `vault.render` 一个出处。这里另写一份拼装代码，
迟早会出现「skill 出的 md 同步时解析不了」—— 那种不同步不报错，只是字段丢。

## 三条硬约束（踩过的坑）

1. **图必须裁到 `附件/` 下，链接写成 `![](<../附件/x.png>)`**。写成 Obsidian 的
   `![[x.png]]` 只有 Obsidian 认，Typora / VSCode 里就是一段纯文本（老师报过
   「导出的 md 看不到图」）。`<>` 不能省：文件名里有空格/中文时会被截断。
2. **新题的 md 里不能有 `shike_id` 行**。同步时看到非空 shike_id 就当成「已有题」，
   找不到就报警。这道题还没入库，不该有 id —— 第一次同步时由拾课自己写进去。
3. **重跑不能造第二份**。同名文件已存在就跳过（要覆盖加 `--overwrite`）。
   vault 里同一道题两份 md 会让此后每次同步都红一条，而老师不知道那份哪来的。

## 用法

    cd backend
    .venv/Scripts/python.exe <skill>/scripts/doc_to_md.py "D:/卷子.pdf"
    .venv/Scripts/python.exe <skill>/scripts/doc_to_md.py "卷子.docx" --pages 1-10
    .venv/Scripts/python.exe <skill>/scripts/doc_to_md.py "卷子.pdf" --dry-run

退出码：0 = 全部生成且无疑点；2 = 有题要人看（图没框到 / 跨页 / 空题干）；
1 = 没跑起来（文件读不了、AI 未配置、整份失败）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import types
from pathlib import Path

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
DOC_EXT = {".docx", ".doc"}
PDF_EXT = {".pdf"}

# 附件名与 md 文件名里要洗掉的字符。`[](){}#%&` 会让 Markdown 的 `![](路径)` 与
# Obsidian 的 `![[名字]]` 一起被截断（`![](<../附件/(1) x.png>)` 里的 `)` 提前收尾）。
_BAD_NAME = re.compile(r'[\\/:*?"<>|\[\](){}#%&\r\n\t]+')


def safe_stem(text: str, fallback: str, limit: int = 40) -> str:
    """题干 → 能当文件名的短标题（去公式、洗字符、截断）。"""
    s = re.sub(r"\$[^$]*\$", "", text or "")
    s = _BAD_NAME.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        return fallback
    return s[:limit].strip(" .") or fallback


def log(msg: str = "") -> None:
    """进度一律走 stderr —— stdout 留给报告 / JSON，别混在一起。"""
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 定位 backend
def resolve_backend(explicit: str | None) -> Path:
    """找出拾课的 backend 目录（含 app/services/vault.py 的那个）。"""
    from os import environ

    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    if environ.get("SHIKE_BACKEND"):
        cands.append(Path(environ["SHIKE_BACKEND"]))
    here = Path(__file__).resolve()
    for p in here.parents:              # 从脚本往上逐层试：<仓库>/backend
        cands.append(p / "backend")
        cands.append(p)
    cands.append(Path.cwd())
    for c in cands:
        try:
            if (c / "app" / "services" / "vault.py").is_file():
                return c.resolve()
        except OSError:
            continue
    raise SystemExit(
        "找不到拾课的 backend 目录（要能看到 app/services/vault.py）。\n"
        "用 --backend 指一下，例如 --backend \"Q:/.../question-bank/backend\""
    )


# ---------------------------------------------------------------- 页号
def parse_pages(spec: str | None, total: int) -> list[int]:
    """`1-10,15` → [1..10, 15]。空 = 全部。越界截断并在日志里说清楚。"""
    if not spec or not spec.strip():
        return list(range(1, total + 1))
    out: set[int] = set()
    for chunk in spec.replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            out |= {p for p in range(min(lo, hi), max(lo, hi) + 1) if 1 <= p <= total}
        else:
            try:
                p = int(chunk)
            except ValueError:
                continue
            if 1 <= p <= total:
                out.add(p)
    return sorted(out) or list(range(1, total + 1))


# ---------------------------------------------------------------- 源文件 → PDF
def fingerprint(path: Path) -> str:
    """源文件的指纹：路径 + 大小 + 修改时间。改了文件就换缓存目录。"""
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def to_pdf(src: Path, work: Path) -> Path:
    """任何输入 → 一个 PDF（缓存里那份可复用）。"""
    suffix = src.suffix.lower()
    if suffix in PDF_EXT:
        return src
    if suffix in IMG_EXT:
        # 单张图：包成 1 页 PDF，后面所有流程（渲染/裁图/识别）都不用分叉
        import pymupdf

        out = work / "source.pdf"
        if not out.is_file():
            with pymupdf.open() as doc:
                img = pymupdf.open(src)
                rect = img[0].rect
                page = doc.new_page(width=rect.width, height=rect.height)
                page.insert_image(rect, filename=str(src))
                doc.save(out)
                img.close()
        return out
    if suffix in DOC_EXT:
        from app.adapters import office

        out = work / "source.pdf"
        if out.is_file():
            return out
        # 手边常有一份配套的 PDF（卷子大多两种都有）—— 有就直接用，
        # 省掉一次 Word 转换：那条路要走 COM，慢且容易撞上「文档打开失败」。
        besides = src.with_suffix(".pdf")
        if besides.is_file() and besides.stat().st_size > 1024:
            log(f"  旁边的 {besides.name} 就是这份 Word，直接用 PDF 那一份")
            return besides
        if not office.is_available():
            raise SystemExit(
                "这份是 Word，但本机没有可用的 Word 来做版式渲染：\n  "
                + office.availability_note()
                + "\n请先另存为 PDF 再转 —— 用 Word 转 PDF 能保住公式与图形的位置，"
                "这是后续裁图不出错的前提。"
            )
        log("  Word → PDF（版式渲染，稍等）…")
        try:
            office.word_to_pdf(src, out)
        except Exception as exc:                # noqa: BLE001
            raise SystemExit(
                "这份 Word 转 PDF 失败：\n  " + str(exc)[:300]
                + "\n\n改用 PDF 再跑是**唯一可靠**的路：用 WPS / Word 打开它 →"
                "\n「另存为 → PDF」→ 把那个 PDF 丢给我。"
                "\n（不直接解析 .docx 的原因：Word 里的公式是 OMML 对象，取出来是一堆"
                "乱码，题目的公式全废；渲染成页面图再识别才准。）"
                "\n如果这份 docx 旁边已经有同名 PDF，我会自动用它，不用你做什么。"
            ) from exc
        return out
    raise SystemExit(f"认不出这种文件：{src.name}（支持 pdf / docx / 图片）")


# ---------------------------------------------------------------- 识别（带缓存）
def knowledge_hint(db, curriculum_id: int | None) -> tuple[str, int]:
    """把知识树的节点名交给模型，让它**照抄原名**。

    为什么值得花这几百 token：模型自由起的名字和树上的叫法对不上 ——
    「导数的几何意义」vs 树上的「导数的概念与几何意义」、「空间线面位置关系」
    vs「点、直线、平面的位置关系」。对不上就挂不了树，那道题在「按知识点筛题」
    和「掌握度统计」里等于不存在，而且**不报错**。

    不写全路径：同步时 `resolve_kp` 是按节点名精确匹配的，全路径反而匹配不上。
    """
    from sqlalchemy import select

    from app.models import Node

    if not curriculum_id:
        return "", 0
    names = [n for n in db.scalars(
        select(Node.name).where(Node.curriculum_id == curriculum_id).order_by(Node.id)
    ).all() if n]
    if not names:
        return "", 0
    return ("\n\n【知识树】knowledge_point 请**从这个清单里挑一个原样的名字**（一字不改地照抄）："
            + "、".join(names)
            + "\n清单里确实没有对应的，才自己写一个（12 字以内的通用叫法）。"), len(names)


def recognize_page_cached(db, pdf_path: Path, pno: int, work: Path, zoom: float,
                          fresh: bool, hint: str = "") -> list[dict]:
    """识别一页。结果缓存在磁盘上：调试 / 断点续跑不重复烧 AI 调用。"""
    from app.adapters import pdf as pdf_adapter
    from app.services import batch_page

    page_dir = work / "pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    cache = page_dir / f"p{pno}.json"
    png = page_dir / f"p{pno}.png"

    hint_stamp = hashlib.sha256(hint.encode("utf-8")).hexdigest()[:8]
    if cache.is_file() and not fresh:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            # 提示词变了（比如知识树清单变了）就得重认 —— 否则缓存会把旧结果
            # 一直端回来，而老师以为「加了清单怎么还是挂不上」。
            if (data.get("zoom") == zoom and data.get("hint") == hint_stamp
                    and data.get("items") is not None):
                log(f"  [p{pno:02d}] 用缓存：{len(data['items'])} 道")
                return data["items"]
        except (OSError, ValueError):
            pass

    if not png.is_file() or fresh:
        pdf_adapter.render_page(str(pdf_path), pno, str(page_dir), zoom=zoom)
    items = batch_page.recognize_page(db, str(png), hint)
    cache.write_text(json.dumps({"zoom": zoom, "hint": hint_stamp, "items": items},
                                ensure_ascii=False), encoding="utf-8")
    log(f"  [p{pno:02d}] {len(items)} 道")
    return items


# ---------------------------------------------------------------- 裁图
def page_figures(pdf_path: Path, pno: int, items: list[dict]) -> list[dict]:
    """这一页的配图决策：每道题该裁哪一块（或没有）。

    走项目里已经调好的「**模型指路 + 几何定框**」：模型说「这题有图、大概在这一带」，
    真正的边界由 PDF 里的图形元素坐标定。

    ⚠️ 为什么不能直接用模型给的框：实测它整体偏高约 1/8 页 —— 裁出来只剩图的上半截，
    箭头和坐标轴标签全被切掉。而数字版 PDF 里每条线每个点都有精确坐标，两者一拼就够用了。
    扫描版 PDF 里整页只有一张位图、查不到任何候选，这时 `resolve_figure_box` 会自动
    退回模型粗框（图会歪一点，但总比没有图强）。
    """
    from app.adapters import pdf as pdf_adapter
    from app.services import batch_page

    empty = [{"rect": None, "box": None, "mode": "", "dup": False, "shared": False}
             for _ in items]
    try:
        W, H = pdf_adapter.page_size(str(pdf_path), pno)
        cands = pdf_adapter.figure_candidates(str(pdf_path), pno)
    except Exception as exc:                    # noqa: BLE001
        log(f"  [p{pno:02d}] 取不到版面几何信息（{str(exc)[:60]}），配图一律按人工处理")
        return empty
    if not W:
        return empty
    decisions = batch_page.plan_page_figures(items, cands, W, H)
    for d in decisions:
        b = d.get("box")
        d["area"] = ((b[2] - b[0]) * (b[3] - b[1]) / 1e6) if b else 0.0
    return decisions


def crop_figure(pdf_path: Path, pno: int, rect, dest: Path) -> tuple[bool, str]:
    """按点坐标从 PDF 上裁一块，写成 PNG。返回 (成不成, 说明)。

    从 PDF 直接裁而不是裁页图：pdf 里的图形是矢量的，3 倍分辨率出来打印也不糊。
    """
    from app.adapters import pdf as pdf_adapter

    try:
        data = pdf_adapter.render_region_png(
            str(pdf_path), [(pno, rect[0], rect[1], rect[2], rect[3])], zoom=3.0)
    except Exception as exc:                    # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:100]}"
    if not data:
        return False, "裁出来的区域是空的"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True, ""


# ---------------------------------------------------------------- 跨页合并
# 一道题跨两页时，整页识别会把它切成两半：上一页是「题干写到一半」，
# 下一页是「从半句开始」。两半各自成题 = 两道废题（一道没结尾、一道没开头，
# 而且后半段连知识点都判不出来）。所以拼回去。
#
# ⚠️ 判据必须两头都硬，否则会把两道无关的题粘在一起 —— 那种错误比不合并糟得多
#    （老师看不出来，因为读起来"像一句话"）：
#      ① 上一页那条确实是**该页最后一条**（位置）
#      ② 下一页那条确实是**该页第一条**（位置）
#      ③ 两边的 note 都说了截断（证据）
#      ④ 后半段的开头**不像一道新题**（不以 1. /（1）/ 例 3 / 【 开头）
_CUT_WORDS = ("截断", "未完", "未结束", "延续", "本页底部", "不完全")
_CONT_WORDS = ("上一页", "上页", "前一页", "续", "本页顶部")
_NEW_Q_RE = re.compile(r"^\s*(?:\d+\s*[.．、]|[（(]\s*\d+\s*[)）]|例\s*\d|【)")


def _has_note(it: dict, words: tuple[str, ...]) -> bool:
    note = (it.get("note") or "")
    return any(w in note for w in words)


def merge_cross_pages(page_items: list[tuple[int, list[dict]]]) -> tuple[list, int]:
    """把「上一页末尾 + 下一页开头」的同一道题拼回一道。返回 (页列表, 拼了几处)。"""
    out: list[tuple[int, list[dict]]] = []
    merged = 0
    for pno, items in page_items:
        items = list(items)
        if out and items:
            prev_pno, prev_items = out[-1]
            if prev_items:
                a, b = prev_items[-1], items[0]
                if (_has_note(a, _CUT_WORDS) and _has_note(b, _CONT_WORDS)
                        and not _NEW_Q_RE.match((b.get("content") or "").strip())):
                    one = dict(a)
                    one["content"] = ((a.get("content") or "").rstrip() + "\n\n"
                                      + (b.get("content") or "").lstrip())
                    for key in ("answer", "analysis", "knowledge_point", "difficulty"):
                        if not one.get(key) and b.get(key):
                            one[key] = b[key]
                    if not one.get("figure_box") and b.get("figure_box"):
                        one["figure_box"] = b["figure_box"]
                    one["note"] = ((a.get("note") or "").rstrip("。 ")
                                   + f"；已自动拼上第 {pno} 页开头那半段。")
                    one["flags"] = list(a.get("flags") or []) + ["cross_page_merged"]
                    prev_items[-1] = one
                    items = items[1:]
                    merged += 1
                    log(f"  跨页题：p{prev_pno} 末题 ＋ p{pno} 首条 → 拼成一道")
        if items:
            out.append((pno, items))
    return out, merged


# ---------------------------------------------------------------- 一道题 → md
def build_question(db, it: dict, *, pno: int, idx: int, decision: dict, pdf_path: Path,
                   attach_dir: Path, md_dir: Path, curriculum_id: int | None,
                   overwrite: bool, dry_run: bool) -> dict:
    """一条识别结果 → 一份 md（+ 裁出来的图）。返回这一题的处理结果。"""
    from app.models import Node
    from app.services import knowledge, vault

    row: dict = {
        "page": pno, "index": idx,
        "title": "", "path": "", "figure": "",
        "qtype": it.get("qtype") or "", "difficulty": it.get("difficulty") or "",
        "knowledge": "", "confidence": it.get("confidence") or "",
        "flags": list(it.get("flags") or []), "needs_human": [],
        "skipped": "",
    }

    content = (it.get("content") or "").strip()
    if not content:
        row["skipped"] = "题干是空的（模型没认出内容）"
        row["needs_human"].append("这一条题干是空的，删掉或重认")
        return row

    stem = safe_stem(content.split("\n", 1)[0], f"题-p{pno:02d}-{idx:02d}")
    row["title"] = stem

    # ---- 知识点：认到知识树上，写**节点名** ----
    # 写全路径的话，同步时 `resolve_kp` 按名字精确匹配不到节点，只剩模糊匹配兜着 ——
    # 挂不上树的知识点在「按知识点筛题」「掌握度统计」里等于不存在。
    kp_raw = (it.get("knowledge_point") or "").strip()
    kp_names: list[str] = []
    if kp_raw:
        names, node_id, _curr = knowledge.resolve_kp(db, [kp_raw], curriculum_id, fuzzy=True)
        if node_id:
            node = db.get(Node, node_id)
            kp_names = [node.name] if node else names
            row["flags"].append("kp_matched")
        else:
            kp_names = names or [kp_raw]
            row["flags"].append("kp_not_on_tree")
    row["knowledge"] = "、".join(kp_names)

    # ---- 配图 ----
    # 「模型估的框」不能无条件信：卷面上一张表格，几何候选里查不到（长横竖线被
    # 判定为表格排除了），于是退回模型框 —— 而模型的框会偏大半页，裁出来是上一题
    # 的正文 + 题干本身。表格还有一层：模型已经把表**转成文字**放进题干了，再配一张
    # 表图是重复的。这两条都是「配了不如不配」。
    fig_name = ""
    rect = (decision or {}).get("rect")
    wanting = bool(it.get("needs_figure") or it.get("figure_box"))
    rough = (decision or {}).get("mode") == "rough"
    area = (decision or {}).get("area") or 0.0
    md_table = sum(1 for line in content.split("\n") if line.strip().startswith("|")) >= 2

    if rect and rough and area > 0.20:
        row["flags"].append("figure_rejected_oversize")
        row["needs_human"].append(
            f"模型圈的图区占了整页 {area * 100:.0f}%（像把题干框进来了），所以没配图 —— "
            "要图自己截一张放进 附件/"
        )
        rect, wanting = None, False
    elif rect and rough and md_table:
        row["flags"].append("table_as_text")
        row["needs_human"].append("这题要的是表格，模型已把表格转成文字写进题干，就没再配图")
        rect, wanting = None, False

    if wanting:
        if rect:
            target = attach_dir / f"{stem[:24]}-p{pno:02d}-{idx:02d}-fig.png"
            if dry_run:
                fig_name = target.name
                row["figure"] = f"(预演，未落盘) {target.name}"
            else:
                okay, why = crop_figure(pdf_path, pno, rect, target)
                if okay:
                    fig_name = target.name
                    row["figure"] = str(target)
                else:
                    row["flags"].append("figure_crop_failed")
                    row["needs_human"].append(f"配图裁不出来（{why}），到 Obsidian 里自己补图")
            if decision.get("shared"):
                row["needs_human"].append("同一页有好几道题都指向这一处图形，图给了最近的那道，核对一下")
        else:
            row["flags"].append("figure_box_missing")
            row["needs_human"].append(
                "这题说要配图，但没定位到图"
                + (f"（模型说：{it.get('figure_note')}）" if it.get("figure_note") else "")
                + " → 多半在下一页，自己去截一张放进 附件/"
            )
    if (decision or {}).get("dup"):
        row["flags"].append("figure_dup")
        row["needs_human"].append("同页另一道题占用了这处图形，这题的图可能得自己补")

    # ---- 正文 ----
    # 用 SimpleNamespace 而不是 Question 实例：这道题**还没入库**，
    # 造一个 ORM 对象容易被后面某次 flush 顺手带进库里。
    q = types.SimpleNamespace(
        id="",                              # 还没入库，写完前面那行 shike_id 会被删掉
        content=content,
        answer=(it.get("answer") or "").strip(),
        analysis=(it.get("analysis") or "").strip(),
        qtype=it.get("qtype") or "",
        difficulty=it.get("difficulty") or "",
        score=None,
        year=None,
        source=None,
        knowledge_points=json.dumps(kp_names, ensure_ascii=False),
        tags=json.dumps(it.get("tags") or [], ensure_ascii=False),
        render_prefer="auto",
    )
    try:
        rel = os.path.relpath(attach_dir, md_dir).replace("\\", "/")
    except ValueError:                      # 不同盘符（Windows）
        rel = ""
    text = vault.render(q, fig_name=fig_name or None,
                        fig_dir=rel if fig_name else None)
    # 新题不许带 shike_id（见文件头第 2 条坑）
    text = "\n".join(l for l in text.split("\n") if not l.startswith("shike_id:"))

    # ---- 落盘 ----
    path = md_dir / f"{stem}.md"
    if path.exists() and not overwrite:
        existing = path.read_text(encoding="utf-8", errors="replace")
        same = (content.split("\n", 1)[0][:30] in existing)
        if same:
            row["skipped"] = "题库里已经有同名且内容相同的题（重跑不会造第二份）"
            return row
        n = 2
        while (md_dir / f"{stem}-{n}.md").exists():
            n += 1
        path = md_dir / f"{stem}-{n}.md"
        row["flags"].append("name_conflict")
    row["path"] = str(path)
    if not dry_run:
        vault.write_md(path, text)

    # ---- 哪些要人看一眼 ----
    if "低" in str(it.get("confidence") or "") or it.get("confidence") == "low":
        row["needs_human"].append("模型自评「不确定」（公式复杂或字迹模糊）")
    note = (it.get("note") or "").strip()
    if note:
        row["needs_human"].append(note)
    if not q.answer:
        row["flags"].append("no_answer")
    return row


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="doc_to_md",
        description="PDF / Word / 图片 → 拾课题库可直接导入的题目 md（含配图）",
    )
    p.add_argument("src", help="源文件：.pdf / .docx / 图片")
    p.add_argument("--out", default="", help="md 写到哪（默认 <vault>/题库）")
    p.add_argument("--attach", default="", help="图片写到哪（默认 <out 同级>/附件）")
    p.add_argument("--backend", default="", help="拾课 backend 目录（默认自动找）")
    p.add_argument("--pages", default="", help="只处理这几页，如 1-10,15（默认全部）")
    p.add_argument("--curriculum", default="", help="体系：id / code / 名称（默认国内高中数学）")
    p.add_argument("--zoom", type=float, default=2.0, help="页图渲染倍率（默认 2.0）")
    p.add_argument("--limit", type=int, default=0, help="最多出几道题（调试用，0=不限）")
    p.add_argument("--overwrite", action="store_true", help="同名 md 已存在就覆盖")
    p.add_argument("--fresh", action="store_true", help="不用识别缓存，重新调模型")
    p.add_argument("--no-kp-list", action="store_true",
                   help="不把知识树清单交给模型（识别更快，但知识点不容易挂树）")
    p.add_argument("--dry-run", action="store_true", help="只识别不动文件")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return p


def pick_curriculum(db, want: str) -> tuple[int | None, str]:
    """体系：用户给的（id/code/名称）→ 否则国内高中数学 → 否则第一个。"""
    from sqlalchemy import select

    from app.models import Curriculum

    rows = db.scalars(select(Curriculum)).all()
    if not rows:
        return None, "(库里没有体系)"
    if want.strip():
        w = want.strip().lower()
        for c in rows:
            if w in {str(c.id), (c.code or "").lower(), (c.name or "").lower()}:
                return c.id, c.name
        log(f"⚠ 没找到体系「{want}」，先按国内高中数学走")
    for c in rows:
        if "高中" in (c.name or "") and "国内" in (c.name or ""):
            return c.id, c.name
    return rows[0].id, rows[0].name


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    src = Path(args.src).expanduser()
    if not src.is_file():
        log(f"找不到文件：{src}")
        return 1

    backend = resolve_backend(args.backend or None)
    sys.path.insert(0, str(backend))
    os.chdir(backend)                       # app.config 里有些相对路径按 backend 解析

    from app import config
    from app.db import SessionLocal
    from app.services import vault
    from app.adapters import pdf as pdf_adapter

    vault.ensure_dirs()
    out_dir = Path(args.out).expanduser().resolve() if args.out else vault.shelf_dir()
    attach_dir = (Path(args.attach).expanduser().resolve() if args.attach
                  else (out_dir.parent / vault.ATTACH))
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        attach_dir.mkdir(parents=True, exist_ok=True)

    work = config.UPLOAD_DIR / "mdconv" / f"{safe_stem(src.stem, 'src', 24)}-{fingerprint(src)}"
    work.mkdir(parents=True, exist_ok=True)

    log(f"源文件  {src}")
    log(f"工作区  {work}")

    pdf_path = to_pdf(src, work)
    total = pdf_adapter.page_count(str(pdf_path))
    pages = parse_pages(args.pages, total)
    log(f"共 {total} 页 → 处理 {len(pages)} 页（{pages[0]}~{pages[-1]}）")

    db = SessionLocal()
    report: dict = {
        "src": str(src), "pages": pages, "out": str(out_dir),
        "attach": str(attach_dir), "curriculum": "", "questions": [],
        "pages_failed": [], "skipped": 0, "merged": 0, "dry_run": bool(args.dry_run),
    }
    try:
        curr_id, curr_name = pick_curriculum(db, args.curriculum)
        report["curriculum"] = curr_name
        log(f"体系    {curr_name}")
        hint, n_hint = ("", 0) if args.no_kp_list else knowledge_hint(db, curr_id)
        if n_hint:
            log(f"知识点  已把 {n_hint} 个树节点名交给模型照抄")

        rows: list[dict] = []
        page_items: list[tuple[int, list[dict]]] = []
        for pno in pages:
            items: list[dict] = []
            for attempt in (1, 2):
                try:
                    items = recognize_page_cached(db, pdf_path, pno, work, args.zoom,
                                                  args.fresh, hint)
                    break
                except Exception as exc:                    # noqa: BLE001
                    if attempt == 2:
                        report["pages_failed"].append({"page": pno, "error": str(exc)[:300]})
                        log(f"  [p{pno:02d}] 失败：{str(exc)[:120]}")
                    else:
                        log(f"  [p{pno:02d}] 出错，2 秒后重试：{str(exc)[:80]}")
                        time.sleep(2)
            if items:
                page_items.append((pno, items))

        # 先全部识别完，再拼跨页题 —— 只识别一页时看不出「这道题是不是下一页还有」。
        page_items, merged = merge_cross_pages(page_items)
        report["merged"] = merged

        for pno, items in page_items:
            decisions = page_figures(pdf_path, pno, items)
            for i, it in enumerate(items, 1):
                row = build_question(
                    db, it, pno=pno, idx=i, decision=decisions[i - 1], pdf_path=pdf_path,
                    attach_dir=attach_dir, md_dir=out_dir, curriculum_id=curr_id,
                    overwrite=args.overwrite, dry_run=args.dry_run,
                )
                if row["skipped"]:
                    report["skipped"] += 1
                else:
                    rows.append(row)
                if args.limit and len(rows) >= args.limit:
                    break
            if args.limit and len(rows) >= args.limit:
                log(f"  到 --limit {args.limit} 了，后面的页不再处理")
                break
        report["questions"] = rows
    finally:
        db.rollback()
        db.close()

    # ---------------- 报告 ----------------
    made = [r for r in report["questions"]]
    with_fig = [r for r in made if r["figure"]]
    human = [r for r in made if r["needs_human"]]
    report["count"] = {"questions": len(made), "figures": len(with_fig),
                       "needs_human": len(human), "pages_failed": len(report["pages_failed"])}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'（预演，没有写任何文件）' if args.dry_run else '已生成'}："
              f"{len(made)} 道题，其中 {len(with_fig)} 道带配图")
        print(f"题库目录：{out_dir}")
        print(f"附件目录：{attach_dir}")
        if report["skipped"]:
            print(f"跳过：{report['skipped']} 条（同名或空题干，见上）")
        if report.get("merged"):
            print(f"跨页：{report['merged']} 处（上一页末尾与下一页开头是同一道题，已拼成一道）")
        if human:
            print(f"\n要你看一眼的 {len(human)} 条：")
            for r in human:
                tag = "  [已配图]" if r["figure"] else ""
                print(f"  · p{r['page']:02d} 第 {r['index']} 题《{r['title'][:22]}》{tag}")
                for h in r["needs_human"]:
                    print(f"      - {h}")
        if report["pages_failed"]:
            print(f"\n有 {len(report['pages_failed'])} 页没识别出来：")
            for f in report["pages_failed"]:
                print(f"  · p{f['page']}：{f['error'][:100]}")
        if not args.dry_run and made:
            print("\n下一步：题库页点「同步」→ 这些题进库；"
                  "想先看再改就在 Obsidian 里打开 题库/ 改完再同步。")

    if report["pages_failed"] and not made:
        return 1
    return 2 if (human or report["pages_failed"]) else 0


if __name__ == "__main__":
    sys.exit(main())
