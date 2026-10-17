# -*- coding: utf-8 -*-
"""把「页图 + 老师标注」烧成一份可以发出去的副本（PNG / PDF）。

三条设计（动之前先读）：

① **绝不把标注写回原件**。原件是学生真迹，烧上去就回不去了；而且同一份作业
   可能要出两版（这次红笔、下次蓝笔）。所以导出是**另存副本**，源件永远干净。

② **不碰 PyMuPDF**（AGPL，只准 `adapters/pdf.py` 用）。页图 PNG 由
   `grade_store.page_png()` 产出，这里只做「在 PNG 上画」+「PNG 拼 PDF」，
   用 Pillow（MIT-CMU）。所以导出不会把 AGPL 带进主链。

③ **坐标系只有一套**：库里 0~1000 按页归一化，这里乘页图真实像素宽高。
   ⚠️ x、y 的缩放**不一样**（前端 `preserveAspectRatio="none"`，非等比铺满），
   所以必须 `w/1000` 与 `h/1000` 分开算 —— 用同一个比例画，字和圆都会歪。
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import HomeworkGradeMark, Student
from . import grade_store as store

# 与前端 GradeMarkLayer.COLORS 同值：导出的颜色必须和屏幕上看到的一致，
# 否则老师按屏幕签字、发出的 PDF 是另一个颜色。
DEFAULT_COLOR = {
    "box": "#dc2626",
    "ellipse": "#dc2626",
    "check": "#16a34a",
    "cross": "#dc2626",
    "arrow": "#2563eb",
    "text": "#2563eb",
    "ink": "#dc2626",
}

# 中文字体优先级。msyh（微软雅黑）在 Windows 一定有；找不到就退黑体/宋体，
# 再找不到只能 load_default（中文会变方框，但不至于让导出失败）。
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

MIN_BOX = 30          # 与前端 buildShape 的「最小可视尺寸」一致（勾/叉常只有一个小点）


def _rgb(css: str, fallback: str) -> tuple[int, int, int]:
    s = (css or "").strip() or fallback
    if not s.startswith("#"):
        s = fallback
    h = s[1:]
    try:
        if len(h) == 3:
            return tuple(int(c * 2, 16) for c in h)          # type: ignore[return-value]
        if len(h) >= 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        pass
    return _rgb(fallback, "#dc2626")


@lru_cache(maxsize=12)
def _font(size: int):
    for p in _FONT_CANDIDATES:
        try:
            if Path(p).exists():
                return ImageFont.truetype(p, size)
        except (OSError, ValueError):
            continue
    return ImageFont.load_default()


def _pair_list(raw) -> list[tuple[float, float]]:
    """points 可能是 JSON 字符串、扁平数组或 [[x,y],…]，三种都收（库里只存扁平）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except (ValueError, TypeError):
            raw = []
    vals = raw if isinstance(raw, list) else []
    if vals and isinstance(vals[0], (list, tuple)):
        out = [(float(p[0]), float(p[1])) for p in vals if len(p) >= 2]
    else:
        nums = []
        for v in vals:
            try:
                nums.append(float(v))
            except (TypeError, ValueError):
                pass
        out = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
    return [(max(0.0, min(1000.0, x)), max(0.0, min(1000.0, y))) for x, y in out]


def _quad(raw) -> tuple[float, float, float, float] | None:
    obj = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw or "[]")
        except (ValueError, TypeError):
            obj = []
    if not isinstance(obj, list) or len(obj) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in obj[:4])
    except (TypeError, ValueError):
        return None
    return (max(0.0, min(1000.0, x0)), max(0.0, min(1000.0, y0)),
            max(0.0, min(1000.0, x1)), max(0.0, min(1000.0, y1)))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, maxw: float) -> list[str]:
    """按像素宽度断行。中文没有空格可断，只能逐字量 —— 一行文字量一次，不贵。"""
    lines: list[str] = []
    for para in str(text or "").split("\n"):
        cur = ""
        for ch in para:
            trial = cur + ch
            try:
                wpx = draw.textlength(trial, font=font)
            except (AttributeError, OSError):
                wpx = len(trial) * 12
            if cur and wpx > maxw:
                lines.append(cur)
                cur = ch
            else:
                cur = trial
        lines.append(cur)
    return lines or [""]


def _draw_one(d: ImageDraw.ImageDraw, m: HomeworkGradeMark,
              w: int, h: int, kx: float, ky: float) -> None:
    kind = (m.kind or "box").strip()
    color = _rgb(m.color, DEFAULT_COLOR.get(kind, "#dc2626"))
    lw = max(2, int(round(w * 0.003)))              # A4 @1600px ≈ 5px，屏幕上 2.2 单位的手感
    q = _quad(m.geo)
    pts = _pair_list(m.points)

    # 没框只有点列：拿包围盒当锚点（与前端 buildShape 同法）
    if q is None and pts:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        q = (min(xs), min(ys), max(xs), max(ys))

    if kind == "ink" or (pts and kind not in ("text",)):
        if len(pts) >= 2:
            d.line([(p[0] * kx, p[1] * ky) for p in pts], fill=color, width=lw, joint="curve")
        elif len(pts) == 1:
            r = lw * 1.4
            x, y = pts[0][0] * kx, pts[0][1] * ky
            d.ellipse([x - r, y - r, x + r, y + r], fill=color)
        if kind == "ink":
            return

    if q is None:
        return
    x0, y0, x1, y1 = q
    ax, ay, bx, by = x0 * kx, y0 * ky, x1 * kx, y1 * ky
    left, top = min(ax, bx), min(ay, by)
    right, bottom = max(ax, bx), max(ay, by)
    cx, cy = (left + right) / 2, (top + bottom) / 2
    bw = max(right - left, MIN_BOX * kx)
    bh = max(bottom - top, MIN_BOX * ky)

    if kind == "box":
        d.rectangle([left, top, right, bottom], outline=color, width=lw)
    elif kind == "ellipse":
        d.ellipse([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], outline=color, width=lw)
    elif kind == "check":
        # 与前端完全同一组比例，否则屏幕上看到的勾和导出的勾不是一个形状
        b0x, b0y = cx - bw / 2, cy - bh / 2
        d.line([(b0x + bw * 0.22, b0y + bh * 0.55),
                (b0x + bw * 0.43, b0y + bh * 0.80),
                (b0x + bw * 0.85, b0y + bh * 0.18)], fill=color, width=lw, joint="curve")
    elif kind == "cross":
        d.line([(left, top), (right, bottom)], fill=color, width=lw)
        d.line([(right, top), (left, bottom)], fill=color, width=lw)
    elif kind == "arrow":
        # 方向取 geo 的两个角（不取规范化后的左上/右下），否则箭尖永远朝右下
        ang = math.atan2(by - ay, bx - ax) or 0.0
        head, spread = 26 * kx, 0.45
        tip = (bx, by)
        p1 = (tip[0] - head * math.cos(ang - spread), tip[1] - head * math.sin(ang - spread))
        p2 = (tip[0] - head * math.cos(ang + spread), tip[1] - head * math.sin(ang + spread))
        d.line([(ax, ay), (bx, by)], fill=color, width=lw)
        d.polygon([p1, tip, p2], fill=color)


def _draw_text(d: ImageDraw.ImageDraw, m: HomeworkGradeMark,
               w: int, h: int, kx: float, ky: float) -> None:
    """文本框。字号按页宽算，**不按 0~1000 的 y 缩放** —— 否则竖版页面上字会被压扁。"""
    text = (m.text or "").strip()
    if not text:
        return
    q = _quad(m.geo) or (0.0, 0.0, 0.0, 0.0)
    color = _rgb(m.color, DEFAULT_COLOR["text"])
    fs = max(13, int(round(w * 0.019)))
    font = _font(fs)

    maxw = w * 0.46                 # 与 CSS .gmark-label 的 max-width 对齐
    lines = _wrap(d, text, font, maxw)
    lh = int(fs * 1.34)
    block_h = lh * len(lines)
    x = q[0] * kx
    y = q[1] * ky - block_h         # 前端 label 是 translate(0,-100%)：底边落在锚点
    if y < 0:
        y = q[1] * ky

    pad = int(fs * 0.28)
    widest = 0
    for ln in lines:
        try:
            widest = max(widest, d.textlength(ln, font=font))
        except (AttributeError, OSError):
            widest = max(widest, len(ln) * fs * 0.6)
    box = [x, y, x + widest + pad * 2, y + block_h + pad]
    # 半透明白底：扫描件底色深、或学生铅笔字压在下面时，纯色字看不清
    d.rounded_rectangle(box, radius=pad, fill=(255, 255, 255, 232), outline=color, width=max(1, lw_hint(w)))
    for i, ln in enumerate(lines):
        d.text((x + pad, y + pad + i * lh), ln, font=font, fill=color)


def lw_hint(w: int) -> int:
    return max(1, int(round(w * 0.0015)))


def _render_page(png: Path, marks: list[HomeworkGradeMark]) -> Image.Image:
    base = Image.open(png).convert("RGBA")
    w, h = base.size
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    kx, ky = w / 1000.0, h / 1000.0
    for m in marks:
        try:
            if (m.kind or "") == "text":
                _draw_text(d, m, w, h, kx, ky)
            else:
                _draw_one(d, m, w, h, kx, ky)
        except Exception:          # noqa: BLE001 —— 一条画坏了不该让整页导不出来
            continue
    return Image.alpha_composite(base, layer).convert("RGB")


def _safe_name(s: str, fallback: str) -> str:
    out = "".join(ch for ch in str(s or "") if ch not in '\\/:*?"<>|\r\n\t').strip()
    return out[:40] or fallback


def export_grade(db: Session, gid: int, fmt: str = "pdf", page: int = 1) -> tuple[Path, str]:
    """合成并返回 (文件路径, 建议文件名)。业务错误抛 `GradeError`（路由层翻成 HTTP）。"""
    grade = store.get_grade(db, gid)
    slots = store._page_slots(db, grade)          # noqa: SLF001 —— 同包内，页序只有这里算得对
    if not slots:
        raise store.GradeError(422, "这份作业没有可导出的提交件")
    # ⚠️ 不拿 `grade.total` 当门槛：那是「批改跑过没」的进度，不是「页图有没有」。
    #    图片/PDF 提交件没批改也能渲染页图，老师想先导一份空白卷也说得通。
    #    真正取不到图时 `page_png` 会自己抛人话错误。

    marks = db.scalars(
        select(HomeworkGradeMark).where(HomeworkGradeMark.grade_id == grade.id)
    ).all()
    by_page: dict[int, list[HomeworkGradeMark]] = {}
    for m in marks:
        by_page.setdefault(int(m.page_no or 1), []).append(m)

    stu = db.get(Student, grade.student_id) if grade.student_id else None
    who = _safe_name(stu.name if stu else "", f"学生{grade.student_id or ''}")
    stamp = (grade.created_at or "")[:10].replace("-", "")
    outdir = store.grade_dir(grade.id) / "export"
    outdir.mkdir(parents=True, exist_ok=True)

    fmt = (fmt or "pdf").lower()
    if fmt == "png":
        pno = int(page or 1)
        if pno < 1 or pno > len(slots):
            # 不静默夹到最后一页：老师传错页码时，导出的是「另一页」，比报错危险
            raise store.GradeError(422, f"没有第 {page} 页（共 {len(slots)} 页）")
        png = store.page_png(db, gid, pno)
        img = _render_page(png, by_page.get(pno, []))
        out = outdir / f"p{pno}.png"
        img.save(out, "PNG")
        return out, f"批改_{who}_{stamp}_第{pno}页.png"

    pages: list[Image.Image] = []
    for pno in range(1, len(slots) + 1):
        png = store.page_png(db, gid, pno)
        pages.append(_render_page(png, by_page.get(pno, [])))
    out = outdir / "grade.pdf"
    first, rest = pages[0], pages[1:]
    # Pillow 自带 PDF 输出：不需要 reportlab，也不会把 AGPL 拖进来。
    # resolution 取 144 = `adapters/pdf.render_page` 的 zoom 2.0 × 72dpi，
    # 这样导出的 PDF 页面尺寸和原始卷面一样大，打印不会被缩放。
    first.save(out, "PDF", save_all=True, append_images=rest, resolution=144.0)
    return out, f"批改_{who}_{stamp}.pdf"
