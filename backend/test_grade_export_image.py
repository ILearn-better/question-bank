# -*- coding: utf-8 -*-
"""导出带批改痕迹的副本时，把老师插的图片也画上去（2026-11-06 补）。

插图片这功能上线的当天就留了一个断点：`grade_export._render_page()` 只认
`text` 和图形，`kind="image"` 落到 `_draw_one()` 里 —— 那里对「没有框也没有点列」
的标注直接 `return`，于是**导出的副本里那张图凭空消失**，屏幕上明明有。
老师不会知道，会把缺图的副本直接发给家长。所以这个脚本守的是：

  ① 图画上去了，位置/大小跟屏幕上看到的同一个框（0~1000 归一化 × 页图真实像素）。
  ② **拉伸铺满（fill）、跟屏幕 `object-fit: fill` 同一条规则** —— 这边擅自
     等比缩放，导出的图会比看到的窄一圈，图里手绘的箭头就指偏了。
  ③ 层序 = 创建先后（跟前端 `zOf()` 的 ord 一个意思）：先画的笔在后插的图下面。
  ④ 图丢了 / 坏图 / 穿越路径 → **不崩、也不静默**：画红虚框写明「图片丢失」。
     静默跳过等于「少一张图没人发现」。
  ⑤ 端到端：真跑一遍 `export_grade(fmt="png"/"pdf")`，读回文件验像素。

⚠️ 不起服务、不碰真库：临时库 + 直调 service，页图用 `page_png` 打桩绕开渲染。
⚠️ 临时路径必须在 import app.* 之前设 —— engine 在 import 时绑定。
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from types import SimpleNamespace

TMP = Path(tempfile.gettempdir()) / "shike_export_image_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.* 之前设
os.environ["SHIKE_DB_PATH"] = str(TMP / "export_image.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from PIL import Image                                  # noqa: E402
from sqlalchemy import select                          # noqa: E402

from app import config                                 # noqa: E402
from app.db import SessionLocal, engine                # noqa: E402
from app.models import (                               # noqa: E402
    Base,
    HomeworkGradeMark,
    Lesson,
    LessonFile,
    Student,
)
from app.schemas import GradeIn, GradeMarkIn, GradeMarksIn   # noqa: E402
from app.services import grade_export as EXP           # noqa: E402
from app.services import grade_store as G              # noqa: E402

assert str(config.DB_PATH) == str(TMP / "export_image.db"), f"临时库没生效：{config.DB_PATH}"
assert str(config.GRADE_MARKS_DIR).startswith(str(TMP)), f"图片目录没落临时区：{config.GRADE_MARKS_DIR}"

total = 0
fails: list[str] = []

# 页图口径：1000×1400 → kx = 1.0、ky = 1.4，手工核坐标时一眼能算
PAGE_W, PAGE_H = 1000, 1400
WHITE = (255, 255, 255)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
RED_INK = (220, 38, 38)          # DEFAULT_COLOR["ink"] = #dc2626


def check(name: str, ok: bool, detail: object = "") -> bool:
    global total
    total += 1
    if ok:
        print(f"  [OK ] {name}")
    else:
        print(f"  [FAIL] {name}" + (f"   <- {detail}" if detail != "" else ""))
        fails.append(name)
    return bool(ok)


def white_page(path: Path) -> Path:
    Image.new("RGB", (PAGE_W, PAGE_H), WHITE).save(path)
    return path


def two_tone(path: Path, w: int = 200, h: int = 100) -> Path:
    """左半绿、右半蓝，**故意跟目标框不同宽高比**（2:1 vs 框的 400:560）。

    等比缩放的实现会在上下留白 → 四角断言立刻红；fill 左绿右蓝正好还顺带
    验了「没有左右翻转」。
    """
    im = Image.new("RGB", (w, h), GREEN)
    for x in range(w // 2, w):
        for y in range(h):
            im.putpixel((x, y), BLUE)
    im.save(path)
    return path


def dot_on_clear(path: Path, size: int = 120) -> Path:
    """中间纯绿、四周全透明。用来验「透明区用自身 mask，不该变成白块」。"""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for x in range(size // 3, size * 2 // 3):
        for y in range(size // 3, size * 2 // 3):
            im.putpixel((x, y), GREEN + (255,))
    im.save(path)
    return path


def px(im: Image.Image, x: int, y: int) -> tuple[int, int, int]:
    return tuple(im.convert("RGB").getpixel((int(x), int(y)))[:3])       # type: ignore[return-value]


def near(a, b, tol: int = 18) -> bool:
    return all(abs(a[i] - b[i]) <= tol for i in range(3))


def mark(**kw) -> SimpleNamespace:
    """`_render_page` 只按属性取值，不需要真 ORM 行。"""
    base = dict(id=1, kind="image", geo="[100,100,500,500]", color="", image="",
                text="", points="[]")
    base.update(kw)
    return SimpleNamespace(**base)


def mk_grade(db, tag: str):
    stu = Student(name=f"导出插图-{tag}")
    db.add(stu)
    db.flush()
    ls = Lesson(student_id=stu.id, start_at="2026-11-06T19:00", duration_min=60)
    db.add(ls)
    db.flush()
    p = config.UPLOAD_DIR / "students" / f"u{stu.id}" / "hw" / f"{tag}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(min_png(30, 40))
    lf = LessonFile(lesson_id=ls.id, name=f"{tag}.png", stored=rel(p),
                    role="homework", kind="image", size_bytes=p.stat().st_size)
    db.add(lf)
    db.flush()
    g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[lf.id]))
    db.commit()
    return g


def rel(p: Path) -> str:
    from app.services import storage
    return storage.rel(p)


def min_png(w: int, h: int) -> bytes:
    """手写最小 PNG（同 test_grade_mark_image.py）：只要求魔数能被认出。"""
    raw = b"".join(b"\x00" + bytes([200, 220, 240] * w) for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- ① 画上去
def t_place() -> None:
    print("\n=== ① 图画上去了，位置和大小就是屏幕上那个框 ===")
    page = white_page(TMP / "page1.png")
    src = two_tone(TMP / "two_tone.png")
    url = G.MARK_IMAGE_PREFIX + src.name
    shutil.copy2(src, config.GRADE_MARKS_DIR / src.name)

    # geo [100,100,500,500] → 像素 (100,140)-(500,700)；框 400×560
    im = EXP._render_page(page, [mark(geo="[100,100,500,500]", image=url)])
    check("导出结果是 RGB（Pillow 存 PDF 只吃这个）", im.mode == "RGB", im.mode)

    check("框内左半是源图的绿", near(px(im, 250, 300), GREEN), px(im, 250, 300))
    check("框内右半是源图的蓝", near(px(im, 400, 300), BLUE), px(im, 400, 300))
    check("框外还是白纸（没糊出去）", near(px(im, 60, 300), WHITE), px(im, 60, 300))

    # ★ fill：源图 200×100 塞进 400×560 的框，等比缩放会在上下留一大条白
    check("★ 拉伸铺满：框的左上角是图（不是白边）", near(px(im, 103, 143), GREEN), px(im, 103, 143))
    check("★ 拉伸铺满：框的左下角也是图", near(px(im, 103, 697), GREEN), px(im, 103, 697))
    check("★ 拉伸铺满：框的右上角也是图", near(px(im, 497, 143), BLUE), px(im, 497, 143))
    check("★ 拉伸铺满：框的右下角也是图", near(px(im, 497, 697), BLUE), px(im, 497, 697))

    # 边界外一像素就该是白的 —— 证明框没被画大
    check("框右边一像素是白（框没被画宽）", near(px(im, 510, 300), WHITE), px(im, 510, 300))
    check("框下边一像素是白（框没被画高）", near(px(im, 300, 710), WHITE), px(im, 300, 710))


# ---------------------------------------------------------------- ② 层序
def t_order() -> None:
    print("\n=== ② 层序 = 创建先后（跟屏幕上 zOf 的 ord 同一个意思）===")
    page = white_page(TMP / "page2.png")
    src = two_tone(TMP / "two_tone2.png")
    shutil.copy2(src, config.GRADE_MARKS_DIR / src.name)
    url = G.MARK_IMAGE_PREFIX + src.name

    # 一条横穿整页 y=300 的红笔迹；像素 y = 300 × ky(1.4) = 420
    ink = dict(kind="ink", points="[[0,300],[1000,300]]", color="#dc2626", geo="[]")

    # 先画笔、后插图 → 图在上，交叠处应该是图的蓝
    im = EXP._render_page(page, [mark(id=1, **ink), mark(id=2, geo="[100,100,500,500]", image=url)])
    check("★ 先画笔、后插图 → 交叠处是图的颜色（图盖住笔）",
          near(px(im, 400, 420), BLUE), px(im, 400, 420))
    check("   图以外的地方笔迹照旧（没有被整体吞掉）",
          near(px(im, 800, 420), RED_INK), px(im, 800, 420))

    # 先插图、后画笔 → 笔在上
    im2 = EXP._render_page(page, [mark(id=1, geo="[100,100,500,500]", image=url), mark(id=2, **ink)])
    check("★ 先插图、后画笔 → 交叠处是笔迹的颜色（笔盖住图）",
          near(px(im2, 400, 420), RED_INK), px(im2, 400, 420))
    check("   同一张图在笔迹之外仍是蓝（图还在，只是被压了一笔）",
          near(px(im2, 400, 200), BLUE), px(im2, 400, 200))


# ---------------------------------------------------------------- ③ 透明 / 坏图
def t_transparency_and_broken() -> None:
    print("\n=== ③ 透明区不白块；图丢了/坏图要看得见 ===")
    page = white_page(TMP / "page3.png")

    src = dot_on_clear(TMP / "dot.png")
    shutil.copy2(src, config.GRADE_MARKS_DIR / src.name)
    im = EXP._render_page(page, [mark(geo="[100,100,500,500]",
                                     image=G.MARK_IMAGE_PREFIX + src.name)])
    check("透明 PNG 贴在卷面上，中心是它的绿", near(px(im, 300, 420), GREEN), px(im, 300, 420))
    check("透明区透出底下的纸（自身 mask 贴，没把整块糊成实色）",
          near(px(im, 110, 150), WHITE), px(im, 110, 150))

    # 图丢了：文件根本不存在
    im_miss = EXP._render_page(page, [mark(geo="[100,100,500,500]",
                                          image=G.MARK_IMAGE_PREFIX + "gm_nothere.png")])
    red_pix = sum(1 for x in range(100, 501, 5) for y in range(140, 701, 5)
                  if near(px(im_miss, x, y), (220, 38, 38), 40))
    check("★ 图丢了不静默：框上画出提示（有红色像素）", red_pix > 20, f"红像素采样 {red_pix} 个")
    check("   图丢了整页还是导出成功（不抛异常）", im_miss.size == (PAGE_W, PAGE_H), im_miss.size)

    # 坏图：文件在，但不是图片
    bad = config.GRADE_MARKS_DIR / "gm_broken.png"
    bad.write_bytes(b"not an image at all, just bytes")
    ok = True
    im_bad = None
    try:
        im_bad = EXP._render_page(page, [mark(geo="[100,100,500,500]",
                                             image=G.MARK_IMAGE_PREFIX + bad.name)])
    except Exception as e:                                    # noqa: BLE001
        ok = False
        print(f"       （抛了：{type(e).__name__}: {e}）")
    check("★ 坏图不崩（一条画坏了不该让整页导不出来）", ok and im_bad is not None)
    check("   坏图也给提示，不是默默空着", im_bad is not None
          and sum(1 for x in range(100, 501, 5) for y in range(140, 701, 5)
                  if near(px(im_bad, x, y), (220, 38, 38), 40)) > 20)

    # 零面积 geo：老数据/坏坐标不该崩；而且跟前端 `Math.max(w, 40)` 一样有最小边长
    tt = two_tone(TMP / "two_tone_min.png")
    shutil.copy2(tt, config.GRADE_MARKS_DIR / tt.name)
    ok2 = True
    im_min = None
    try:
        im_min = EXP._render_page(page, [mark(geo="[0,0,0,0]",
                                             image=G.MARK_IMAGE_PREFIX + tt.name)])
    except Exception as e:                                    # noqa: BLE001
        ok2 = False
        print(f"       （抛了：{type(e).__name__}: {e}）")
    check("零面积 geo 不崩（老数据容错）", ok2 and im_min is not None)
    check("★ 零面积 geo 仍按最小 40 单位画（跟前端 Math.max(w,40) 对齐，不是 1px）",
          im_min is not None and near(px(im_min, 5, 5), GREEN),
          im_min and px(im_min, 5, 5))


# ---------------------------------------------------------------- ④ 穿越
def t_traversal() -> None:
    print("\n=== ④ `../` 不许读到图片目录外面 ===")
    page = white_page(TMP / "page4.png")
    # 在 grade_marks 的**上一级**放一张图，名字与穿越目标一致
    outside = config.GRADE_MARKS_DIR.parent / "evil.png"
    Image.new("RGB", (50, 50), (255, 0, 255)).save(outside)
    im = EXP._render_page(page, [mark(geo="[100,100,500,500]",
                                     image=G.MARK_IMAGE_PREFIX + "../evil.png")])
    magenta = sum(1 for x in range(100, 501, 5) for y in range(140, 701, 5)
                  if near(px(im, x, y), (255, 0, 255), 40))
    check("★ 穿越路径不会被当成图片贴上（外面那张洋红一个像素都没进来）",
          magenta == 0, f"洋红像素采样 {magenta} 个")
    red_pix = sum(1 for x in range(100, 501, 5) for y in range(140, 701, 5)
                  if near(px(im, x, y), (220, 38, 38), 40))
    check("   穿越被当成「图片丢了」处理（有提示）", red_pix > 20, red_pix)


# ---------------------------------------------------------------- ⑤ 端到端
def t_end_to_end() -> None:
    print("\n=== ⑤ 端到端：真跑 export_grade，导出的文件里真有这张图 ===")
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        g = mk_grade(db, "e2e")
        src = two_tone(TMP / "e2e_src.png")
        shutil.copy2(src, config.GRADE_MARKS_DIR / src.name)
        G.add_marks(db, g.id, GradeMarksIn(marks=[
            GradeMarkIn(page_no=1, kind="image", image=G.MARK_IMAGE_PREFIX + src.name,
                        geo=[100, 100, 500, 500], color="#2563eb"),
        ]))
        marks = db.scalars(select(HomeworkGradeMark)).all()
        check("标注真的落库了（kind=image）",
              len(marks) == 1 and marks[0].kind == "image", [m.kind for m in marks])

        # 页图打桩：export_grade 只按像素画，页图长什么样跟本脚本无关
        old = G.page_png
        G.page_png = lambda _db, _gid, _pno: WHITE_PNG
        try:
            png_path, name = EXP.export_grade(db, g.id, fmt="png", page=1)
            check("导出的 PNG 存在", Path(png_path).is_file(), png_path)
            check("文件名是人话（带学生名/日期）", "批改_" in name and name.endswith(".png"), name)
            with Image.open(png_path) as out:
                im = out.convert("RGB")
                check("★ 导出的 PNG 里那张图在（左半绿）", near(px(im, 250, 300), GREEN), px(im, 250, 300))
                check("★ 导出的 PNG 里那张图在（右半蓝）", near(px(im, 400, 300), BLUE), px(im, 400, 300))

            pdf_path, pdf_name = EXP.export_grade(db, g.id, fmt="pdf")
            check("导出的 PDF 存在且非空",
                  Path(pdf_path).is_file() and Path(pdf_path).stat().st_size > 1000,
                  Path(pdf_path).stat().st_size if Path(pdf_path).is_file() else "缺失")
            check("PDF 文件名是人话", "批改_" in pdf_name and pdf_name.endswith(".pdf"), pdf_name)
        finally:
            G.page_png = old
    finally:
        db.close()


WHITE_PNG = white_page(TMP / "page_e2e.png")


def main() -> int:
    print("导出批改痕迹里的插图（0039 补）")
    print(f"临时区：{TMP}")
    config.GRADE_MARKS_DIR.mkdir(parents=True, exist_ok=True)
    t_place()
    t_order()
    t_transparency_and_broken()
    t_traversal()
    t_end_to_end()

    print(f"\n{'=' * 60}")
    if fails:
        print(f"结果：{total} 项，{len(fails)} 项失败")
        for n in fails:
            print(f"  - {n}")
        return 1
    print(f"结果：{total} 项全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
