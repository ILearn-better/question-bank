# -*- coding: utf-8 -*-
"""批注里插入的图片（0039）：字段收口、上传落盘、删除不误删文件。

用户 2026-11-06 的要求：
    「在作业批改处加入文件插图图片功能（笔画、文本框、图片谁先出现就谁放下面），
      并且要求能够删除图片、撤销插入（快捷键 ctrl z）、鼠标拖拽调整图片大小」

前端那半（层级、拖、缩、Ctrl+Z）在 `frontend/grade_mount_check.js` 与
`frontend/grade_draft_check.js` 里量。**这个脚本只管后端**，守四件事：

  ① **image 字段真的落库、也真的读得回来**（`mark_dict` 漏了它 → 前端永远「图丢了」）。
  ② **只认自家上传端点发出的 URL**：`http://别的站/x.png`、`/api/mark-images/../a.png`
     一律收成空串。这个是安全问题 —— 这个字段会直接进前端 `img.src`，
     放任外部 URL 等于「打开一份批改」自动去拉外部资源（跟踪像素 / 内网探测）。
  ③ **kind != image 时不收图**：否则库里会出现一条「永远不显示、却占着盘」的记录。
  ④ **删批注不删文件**：Ctrl+Z 撤销之后要把同一张图重新指回来 —— 文件没了就永远回不来。
     这一条最容易在「顺手清理一下」时被改坏，所以连文件是否还在都要断言。

⚠️ 不起服务、不碰真库：临时库 + 直调 service（AI 层完全不参与）。
⚠️ 临时路径必须在 import app.* 之前设好 —— engine 在 import 时绑定。
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

TMP = Path(tempfile.gettempdir()) / "shike_mark_image_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
# ⚠️ 必须在 import app.* 之前设
os.environ["SHIKE_DB_PATH"] = str(TMP / "mark_image.db")
os.environ["SHIKE_DATA_DIR"] = str(TMP / "data")
os.environ["SHIKE_AUTO_BACKUP"] = "0"

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

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
from app.services import images                        # noqa: E402
from app.services import grade_store as G              # noqa: E402

# 临时库 / 临时归档真的生效了吗（没生效就会写进真库，那就不是测试了）
assert str(config.DB_PATH) == str(TMP / "mark_image.db"), f"临时库没生效：{config.DB_PATH}"
assert str(config.GRADE_MARKS_DIR).startswith(str(TMP)), f"图片目录没落临时区：{config.GRADE_MARKS_DIR}"

total = 0
fails: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> bool:
    global total
    total += 1
    if ok:
        print(f"  [OK ] {name}")
    else:
        print(f"  [FAIL] {name}" + (f"   <- {detail}" if detail != "" else ""))
        fails.append(name)
    return bool(ok)


def make_png(w: int = 24, h: int = 18) -> bytes:
    """手写一张最小 PNG。**不引第三方库**：这里只要一段「魔数能被认出」的字节。"""
    raw = b"".join(b"\x00" + bytes([200, 220, 240] * w) for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _rel(p: Path) -> str:
    from app.services import storage
    return storage.rel(p)


def mk_grade(db, tag: str):
    stu = Student(name=f"插图测试-{tag}")
    db.add(stu)
    db.flush()
    ls = Lesson(student_id=stu.id, start_at="2026-11-06T19:00", duration_min=60)
    db.add(ls)
    db.flush()
    p = config.UPLOAD_DIR / "students" / f"u{stu.id}" / "hw" / f"{tag}.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(make_png(30, 40))
    lf = LessonFile(lesson_id=ls.id, name=f"{tag}.png", stored=_rel(p),
                    role="homework", kind="image", size_bytes=p.stat().st_size)
    db.add(lf)
    db.flush()
    g = G.create_grade(db, GradeIn(lesson_id=ls.id, file_ids=[lf.id]))
    db.commit()
    return g


# ---------------------------------------------------------------- ① 上传
def t_upload() -> None:
    print("\n=== ① 上传：落地 + URL 形态 + 坏文件被挡 ===")
    info = G.save_mark_image(make_png())
    check("返回里带 url", str(info.get("url", "")).startswith(G.MARK_IMAGE_PREFIX), info)
    check("文件名在 GRADE_MARKS_DIR 下（不是 crops —— 那里归孤儿清理管）",
          (config.GRADE_MARKS_DIR / info["name"]).is_file(),
          str(config.GRADE_MARKS_DIR / info["name"]))
    check("文件内容与上传的一致",
          (config.GRADE_MARKS_DIR / info["name"]).read_bytes() == make_png(),
          info)
    check("mark_image_path 能取回同一个文件",
          G.mark_image_path(info["name"]).is_file(), info["name"])

    bad = False
    try:
        G.save_mark_image(b"this is not an image at all")
    except images.ImageRejected as e:
        bad = True
        print(f"       （拒绝原因：{e}）")
    check("★ 不是图片的字节会被拒绝（不能让任何文件混进图片目录）", bad)

    # 路径穿越：服务端点按 basename 取，函数本身也要兜住
    check("★ mark_image_path 只取 basename（../ 穿越不出去）",
          G.mark_image_path("../../etc/passwd").parent == config.GRADE_MARKS_DIR,
          str(G.mark_image_path("../../etc/passwd")))


# ---------------------------------------------------------------- ② 字段收口
def t_clean_url() -> None:
    print("\n=== ② URL 收口：只认自家端点 ===")
    good = "/api/mark-images/gm_abc.png"
    cases = [
        (good, good, "自家 URL 原样保留"),
        ("http://evil.example/x.png", "", "★ 外部 URL 被清空（否则打开批改就自动拉外部资源）"),
        ("//evil.example/x.png", "", "协议相对 URL 被清空"),
        ("/api/mark-images/../secret.png", "", "★ 带 .. 的路径被清空"),
        ("/api/mark-images/sub/x.png", "", "带子目录的被清空"),
        ("/api/mark-images/", "", "只有前缀没有文件名 → 空"),
        ("", "", "空串还是空串"),
        (good, good, "同一个好 URL 重复清洗结果稳定"),
    ]
    for raw, want, name in cases:
        got = G._clean_mark_image(raw)
        check(f"{name}: {raw or '(空)'} → {got!r}", got == want, got)


# ---------------------------------------------------------------- ③ 落库 / 读回
def t_store_roundtrip() -> None:
    print("\n=== ③ 建标注 → mark_dict 读回来 ===")
    db = SessionLocal()
    try:
        g = mk_grade(db, "A")
        up = G.save_mark_image(make_png())
        geo = [100, 120, 460, 420]

        m = G.add_mark(db, g.id, GradeMarkIn(
            page_no=1, kind="image", image=up["url"], geo=geo, color="#dc2626"))
        d = G.mark_dict(m)
        check("kind=image 落库", d["kind"] == "image", d["kind"])
        check("★★ mark_dict 带 image（漏了它前端永远显示「图丢了」）",
              d["image"] == up["url"], d["image"])
        check("geo 是四个 0~1000 整数（拖/缩都靠它）",
              d["geo"] == geo and all(isinstance(v, int) for v in d["geo"]), d["geo"])
        check("归属记成 teacher（撤销/清空只动老师的）", d["created_by"] == "teacher", d["created_by"])

        # 从库里再读一遍（不靠内存对象）
        db.expire_all()
        row = db.get(HomeworkGradeMark, m.id)
        check("落库之后重新查还是那张图", (row.image or "") == up["url"], row.image)

        # ④ kind != image 不收图
        m2 = G.add_mark(db, g.id, GradeMarkIn(
            page_no=1, kind="ink", image=up["url"], geo=[1, 2, 3, 4], points=[1, 2, 3, 4]))
        check("★ kind=ink 时 image 被丢掉（否则库里躺着一条永远不显示的图）",
              (m2.image or "") == "", repr(m2.image))

        # ⑤ 外部 URL 进不来
        m3 = G.add_mark(db, g.id, GradeMarkIn(
            page_no=1, kind="image", image="http://evil.example/x.png", geo=[5, 6, 7, 8]))
        check("★ 外部 URL 落库时被清空（安全边界，不是洁癖）",
              (m3.image or "") == "", repr(m3.image))

        # ⑥ 批量补发（老师画完就关页面那条路）也要带上图
        made = G.add_marks(db, g.id, GradeMarksIn(marks=[
            GradeMarkIn(page_no=1, kind="image", image=up["url"], geo=[10, 20, 210, 260]),
        ]))
        check("批量补发落的 image 也在", bool(made) and made[0].image == up["url"],
              [x.image for x in made])
    finally:
        db.close()


# ---------------------------------------------------------------- ④ 拖 / 缩 / 删
def t_patch_and_delete() -> None:
    print("\n=== ④ 改大小（PATCH geo）+ 删除不误删文件 ===")
    db = SessionLocal()
    try:
        g = mk_grade(db, "B")
        up = G.save_mark_image(make_png())
        m = G.add_mark(db, g.id, GradeMarkIn(
            page_no=1, kind="image", image=up["url"], geo=[100, 100, 400, 300]))
        mid = m.id

        # 拖右下角改大小：与文本框走同一条 PATCH（geo 四个数）
        from app.schemas import GradeMarkPatch
        G.patch_mark(db, mid, GradeMarkPatch(geo=[100, 100, 700, 620]))
        db.expire_all()
        row = db.get(HomeworkGradeMark, mid)
        check("★★ 拖右下角改大小：geo 的右下角真的变了",
              (row.geo or "") == "[100, 100, 700, 620]", row.geo)
        check("★ 改大小不动图片本身（还是那张图）", row.image == up["url"], row.image)

        # 拖动改位置
        G.patch_mark(db, mid, GradeMarkPatch(geo=[200, 160, 800, 680]))
        db.expire_all()
        check("拖动改位置生效",
              (db.get(HomeworkGradeMark, mid).geo or "") == "[200, 160, 800, 680]", "")

        # 删除：行没了，文件还在
        img_path = config.GRADE_MARKS_DIR / up["name"]
        G.delete_mark(db, mid)
        db.expire_all()
        check("★★ 删除后库里那行没了",
              db.scalar(select(HomeworkGradeMark).where(HomeworkGradeMark.id == mid)) is None)
        check("★★ 删除**不**删图片文件（Ctrl+Z 撤销要把同一张图重新指回来）",
              img_path.is_file(), str(img_path))
    finally:
        db.close()


def main() -> int:
    Base.metadata.create_all(engine)
    t_upload()
    t_clean_url()
    t_store_roundtrip()
    t_patch_and_delete()

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ 失败 {len(fails)} / {total} 项：")
        for name in fails:
            print("   · " + name)
        return 1
    print(f"✅ 全部通过（{total} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
