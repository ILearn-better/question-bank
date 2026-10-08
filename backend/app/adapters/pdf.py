# -*- coding: utf-8 -*-
"""PDF 能力适配层 —— **全项目唯一 import pymupdf 的地方**。

⚠️ 许可约束（开发文档 §4.1 / §12.3）：
    PyMuPDF 是 **AGPL-3.0**，商业使用需向 Artifex 购买授权。
    自用无碍；一旦要交付给别人使用，必须二选一：
      a) 购买商业授权，或
      b) 换成宽松许可的实现（pypdf / pypdfium2 等）
    把调用点收敛在这个文件里，将来 (b) 方案付出的代价是「改一个文件」，
    而不是「在全项目里找 30 处调用」。

坐标约定：一律使用 PDF 点坐标（左上角为原点，y 向下），与 PyMuPDF 一致。
前端按百分比换算显示，裁剪时把像素坐标换算回 PDF 坐标再提交。
"""
from __future__ import annotations

import os
import uuid

import pymupdf


# ---------------------------------------------------------------- 页面信息
def page_count(path: str | os.PathLike) -> int:
    with pymupdf.open(path) as doc:
        return doc.page_count


def _page(doc, pno: int):
    """取第 pno 页（1-based），**显式校验范围**，越界统一抛 ValueError。

    为什么必须自己校验（踩过）：
      · pymupdf 的 `doc[i]` 对负索引走 Python 语义 —— `doc[-1]` 是最后一页。
        所以 pno=0 / 负数**不报错**，而是悄悄返回另一页；更糟的是渲染结果照样
        按 `p{pno}.png` 落进页面缓存，污染磁盘。
      · 超出上界则抛 IndexError，没人接就是 HTTP 500 + 一屏 traceback。
    两种结果都不对：越界就该明确报错，由路由层翻译成 422。
    """
    if not (1 <= pno <= doc.page_count):
        raise ValueError(f"页码 {pno} 超出范围（本文档共 {doc.page_count} 页）")
    return doc[pno - 1]


def render_page(path: str | os.PathLike, pno: int, cache_dir: str | os.PathLike, zoom: float = 2.0) -> str:
    """渲染第 pno 页（1-based）为 PNG，带磁盘缓存。返回 PNG 文件路径。"""
    os.makedirs(cache_dir, exist_ok=True)
    out = os.path.join(cache_dir, f"p{pno}.png")
    if os.path.exists(out):
        return out
    with pymupdf.open(path) as doc:
        page = _page(doc, pno)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        pix.save(out)
    return out


def page_lines(path: str | os.PathLike, pno: int) -> dict:
    """提取第 pno 页所有文字行及 bbox，供前端吸附分界线与拼接题目文本。"""
    with pymupdf.open(path) as doc:
        page = _page(doc, pno)
        d = page.get_text("dict")
        lines = []
        for block in d.get("blocks", []):
            if block.get("type") != 0:
                continue
            for ln in block.get("lines", []):
                text = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if text:
                    x0, y0, x1, y1 = ln["bbox"]
                    lines.append({
                        "bbox": [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)],
                        "text": text,
                    })
        lines.sort(key=lambda l: l["bbox"][1])
        return {
            "page": pno,
            "width": round(page.rect.width, 1),
            "height": round(page.rect.height, 1),
            "lines": lines,
        }


# ---------------------------------------------------------------- 裁剪
def _clip_pixmaps(path: str | os.PathLike, regions, zoom: float) -> list:
    """把若干区域渲染成 pixmap 列表。越界的页、空区域**跳过**（不抛错）。

    单段裁剪与跨页拼接共用这一份取图逻辑 —— 两处各写一遍的话，
    「区域要 & page.rect 收进页内」这种细节迟早只在一处生效。
    """
    pixmaps = []
    with pymupdf.open(path) as doc:
        for pno, x0, y0, x1, y1 in regions:
            if not (1 <= pno <= doc.page_count):
                continue
            page = doc[pno - 1]
            clip = pymupdf.Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)) & page.rect
            if clip.is_empty:
                continue
            pixmaps.append(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip))
    return pixmaps


def _stack_png(pixmaps, gap: int) -> bytes:
    """把若干 pixmap 竖着拼成一张 PNG 的**字节**。只有一张时直接返回它。

    拼接做法：新建一个 PDF 页，把各区域图按顺序 insert_image 进去再整体渲染。
    不用 Pixmap.copy/set_origin —— 那套 API 的定位语义各家版本行为不一，
    实测会贴出一张全白的图（真实踩过的坑），而 insert_image 的行为是文档化的。
    """
    if len(pixmaps) == 1:
        return pixmaps[0].tobytes("png")

    width = max(p.width for p in pixmaps)
    height = sum(p.height for p in pixmaps) + gap * (len(pixmaps) - 1)

    out_doc = pymupdf.open()
    try:
        page = out_doc.new_page(width=width, height=height)
        y = 0
        for p in pixmaps:
            # 统一成无 alpha 的 RGB，否则 insert_image 会因色彩空间不一致而报错
            rgb = p if (p.alpha == 0 and p.colorspace == pymupdf.csRGB) else pymupdf.Pixmap(pymupdf.csRGB, p)
            page.insert_image(pymupdf.Rect(0, y, rgb.width, y + rgb.height), pixmap=rgb)
            y += rgb.height + gap
        return page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False).tobytes("png")
    finally:
        out_doc.close()


def crop_region(
    path: str | os.PathLike, pno: int, rect, out_dir: str | os.PathLike, zoom: float = 3.0
) -> str:
    """按 PDF 坐标矩形裁剪高清区域图（默认 3 倍分辨率，打印不糊）。返回文件名。"""
    return crop_regions(path, [(pno, rect[0], rect[1], rect[2], rect[3])], out_dir, zoom=zoom)


def crop_regions(
    path: str | os.PathLike, regions, out_dir: str | os.PathLike, zoom: float = 3.0, gap: int = 0
) -> str:
    """把多个区域（可跨页）竖着拼成一张图，**落盘**并返回文件名。

    用途：一道题跨了页（Word/PDF 里很常见，尤其解答题）。
    如果按「一题一张原貌图」来做，跨页题就必须存两张 —— 但题目只有一个 image 字段。
    这里直接拼成一张，跨页题的原貌图也就是一张，数据模型不用动。

    regions 形如 [(page, x0, y0, x1, y1), ...]。
    """
    os.makedirs(out_dir, exist_ok=True)
    pixmaps = _clip_pixmaps(path, regions, zoom)
    if not pixmaps:
        raise ValueError("裁剪区域为空")
    name = f"{uuid.uuid4().hex[:12]}.png"
    with open(os.path.join(out_dir, name), "wb") as fh:
        fh.write(_stack_png(pixmaps, gap))
    return name


def render_region_png(
    path: str | os.PathLike, regions, zoom: float = 1.5, gap: int = 0
) -> bytes:
    """渲染若干区域并竖拼成 PNG，**不落盘**，直接返回字节。

    这是给「预览」用的，与 crop_regions 的关键差别就是**不写文件**：
    老师画完分界线要马上看到这一块切出来什么样，但这时还没提交 ——
    `data/uploads/crops/` 是提交后的资产目录，每张图都要靠引用计数才有
    清理依据。往里塞一堆没人引用的预览图，等于给未来的自己挖坑
    （实测过一次：题库 0 行、盘上 92 张图）。

    分辨率也低一档（默认 1.5 倍）：缩略图看的是「切得对不对」，不是印刷质量。
    """
    pixmaps = _clip_pixmaps(path, regions, zoom)
    if not pixmaps:
        raise ValueError("区域为空，或超出了页面范围")
    return _stack_png(pixmaps, gap)


# ---------------------------------------------------------------- 图形区域探测
# 「这一页上的图在哪」—— 数字版 PDF 里每个图形元素都带着**精确坐标**，
# 比让模型估框靠谱得多（实测：模型估的框整体偏高约 1/8 页，只盖住图的上半截）。
# 所以自动配图走「模型指路 + 几何定框」：模型说「这题有图、大概在这一带」，
# 真正的边界由这里给的候选区定。
#
# ⚠️ 扫描版 PDF 里整页就是**一张**位图 → 这里查不到任何候选（被 MAX 规则排掉），
#    调用方必须能回退到模型给的粗略框，不能假设「一定有候选」。
_FIG_MIN_AREA_RATIO = 0.006     # 小于整页 0.6% 的不要：噪点、公式碎片、小数点
_FIG_MAX_AREA_RATIO = 0.55      # 大于整页 55% 的不要：整页扫描底图、水印底纹
_FIG_MIN_SIDE = 18.0            # 最短边下限（点）：太扁的多半是下划线、分隔线
_FIG_HEADER_RATIO = 0.09        # 页头一律不看：卷名、校徽、组卷网 logo
_FIG_MERGE_GAP = 25.0           # 斜线并簇的容许间隙（点）
_FIG_RULE_MIN_LEN = 40.0        # 「长直线」判定长度（点）—— 表格框线一般远长于此


def page_size(path: str | os.PathLike, pno: int) -> tuple[float, float]:
    """页面宽高（点）。**归一化坐标换算的唯一出处** —— 模型给的是 0~1000 的比例，
    要变成可裁剪的点坐标必须乘真实页宽高；页宽高从别处猜（比如假定 A4）在
    非 A4 的卷子上会整体偏移。
    """
    with pymupdf.open(path) as doc:
        rect = _page(doc, pno).rect
        return rect.width, rect.height


def _cluster_rects(rects: list, gap: float) -> list:
    """把互相挨着（含**传递**挨着）的矩形并成簇。

    为什么用并查集而不是「往前找第一个能并的」：
    单趟合并下 A-B 相邻、B-C 相邻、A-C 不相邻时会留下 A+B 和 C 两簇，
    图形被切成两半 → 裁出来的图缺一块。图形碎片本来就是零散线段，很常见。
    """
    if not rects:
        return []
    parent = list(range(len(rects)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            a, b = rects[i], rects[j]
            if not (a.x1 < b.x0 - gap or a.x0 > b.x1 + gap
                    or a.y1 < b.y0 - gap or a.y0 > b.y1 + gap):
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

    groups: dict[int, object] = {}
    for i, r in enumerate(rects):
        k = find(i)
        groups[k] = r if k not in groups else (groups[k] | r)
    return list(groups.values())


def figure_candidates(path: str | os.PathLike, pno: int) -> list[dict]:
    """该页上**看起来是一幅图**的区域，返回 `[{"kind": "img"|"vec", "rect": (x0,y0,x1,y1)}]`（点坐标）。

    两类来源：

      ① **位图**（`get_image_info`）—— 扫进去的照片、截图。坐标天生精确。
      ② **含斜线或曲线的矢量簇**（`get_drawings`）—— 坐标图、几何图、函数图象。
         ⚠️ 只收「含斜线/曲线」的簇：表格框线与分数线**全是横竖线**，
         按这个判据天然被排除（否则整张表格会被当成一幅图）。
         但光靠这一条还不够：表格**单元格里的表头斜线**（`┌──┐` 里那条斜杠）
         也是一条斜线，会被收成一个小簇 —— 实测踩过（高考真题第 3 页）。
         所以再加一道「长横竖线交叉 = 表格」的判据（见 `_inside_table_grid`）。

    宁可漏报也不误报：漏了只是回退到模型给的粗略框（人工再调一下），
    误报则会往待审卡片上挂一张**错的图** —— 老师不细看就入库了。
    """
    with pymupdf.open(path) as doc:
        page = _page(doc, pno)
        W, H = page.rect.width, page.rect.height
        page_area = W * H

        # 长横/竖直线段 —— 用来识别「这个候选区是不是被表格框着」
        h_rules: list[tuple[float, float, float]] = []   # (y, x_start, x_end)
        v_rules: list[tuple[float, float, float]] = []   # (x, y_start, y_end)
        diag: list = []
        for dr in page.get_drawings():
            for item in dr["items"]:
                if item[0] == "l":
                    p1, p2 = item[1], item[2]
                    dx, dy = abs(p2.x - p1.x), abs(p2.y - p1.y)
                    if dy <= 1.0 and dx >= _FIG_RULE_MIN_LEN:
                        h_rules.append(((p1.y + p2.y) / 2, min(p1.x, p2.x), max(p1.x, p2.x)))
                    elif dx <= 1.0 and dy >= _FIG_RULE_MIN_LEN:
                        v_rules.append(((p1.x + p2.x) / 2, min(p1.y, p2.y), max(p1.y, p2.y)))
                    elif dx > 2.5 and dy > 2.5:
                        diag.append(pymupdf.Rect(p1, p2))
                elif item[0] in ("c", "qu"):     # 贝塞尔/四边形曲线
                    diag.append(pymupdf.Rect(item[1], item[-1]))

        out: list[dict] = []

        def _ok(b) -> bool:
            if b.is_empty or min(b.width, b.height) < _FIG_MIN_SIDE:
                return False
            a = b.width * b.height
            return page_area * _FIG_MIN_AREA_RATIO <= a <= page_area * _FIG_MAX_AREA_RATIO

        for im in page.get_image_info():
            b = pymupdf.Rect(im["bbox"]) & page.rect
            if b.y1 < H * _FIG_HEADER_RATIO:            # 页头 logo
                continue
            if _ok(b):
                out.append({"kind": "img", "rect": (b.x0, b.y0, b.x1, b.y1)})

        for g in _cluster_rects(diag, _FIG_MERGE_GAP):
            b = pymupdf.Rect(g) & page.rect
            if not _ok(b) or _inside_table_grid(b, h_rules, v_rules):
                continue
            # 已经有一张位图盖住这里了（图形常常是「位图 + 描边」的组合）→ 不重复收
            if any(_overlap_ratio(b, pymupdf.Rect(c["rect"])) > 0.75 for c in out):
                continue
            out.append({"kind": "vec", "rect": (b.x0, b.y0, b.x1, b.y1)})

        return out


def _inside_table_grid(b, h_rules, v_rules) -> bool:
    """候选区是不是被表格框线包着（表头斜线那种）。

    判据：候选区的**上下各有一条长横线**、**左右各有一条长竖线**，
    且这些线在候选区的横向/纵向范围内确实横跨过去。
    单元格四边框线正好满足 → 表头斜线被排掉；
    真正的几何图形外面没有这种网格 → 保留。
    """
    def _span(rule, lo, hi, a, b_):
        _, s, e = rule
        over = min(e, b_) - max(s, a)
        return over >= (hi - lo) * 0.6

    h_hit = sum(1 for r in h_rules if b.y0 - 2 <= r[0] <= b.y1 + 2 and _span(r, 0, b.width, b.x0, b.x1))
    v_hit = sum(1 for r in v_rules if b.x0 - 2 <= r[0] <= b.x1 + 2 and _span(r, 0, b.height, b.y0, b.y1))
    return h_hit >= 2 and v_hit >= 2


def _overlap_ratio(a, b) -> float:
    """交集面积 / 较小那个的面积（0~1）。用于判「两个候选其实是同一幅图」。"""
    inter = a & b
    if inter.is_empty:
        return 0.0
    small = min(a.width * a.height, b.width * b.height)
    return (inter.width * inter.height) / small if small > 0 else 0.0


# ---------------------------------------------------------------- 内容块解析
def parse_blocks(path: str | os.PathLike) -> list[dict]:
    """按文本块切分 PDF，保留页码；图片块标记占位。

    无文字层（扫描版）时返回空列表 —— 调用方据此进入截图模式。
    """
    blocks: list[dict] = []
    with pymupdf.open(path) as doc:
        for pno, page in enumerate(doc, start=1):
            for b in page.get_text("blocks"):
                # b = (x0, y0, x1, y1, text, block_no, block_type)  block_type: 0=文本 1=图片
                text = (b[4] or "").strip()
                if b[6] == 1:
                    blocks.append({"page": pno, "type": "image", "text": "[图片]"})
                elif text:
                    blocks.append({"page": pno, "type": "text", "text": text})
    return blocks
