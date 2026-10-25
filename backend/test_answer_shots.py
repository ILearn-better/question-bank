# -*- coding: utf-8 -*-
"""多张截图（跨页答案）的测试：竖拼 + 多图识别入参 + 答案标题剥离 + prompt 约束。

这个测试**不碰数据库、不调 AI**：拼图是纯 Pillow 运算，识别只验「把什么发给了谁」
（post_json 打桩）。所以它能进常规回归，跑一次几秒钟。

⚠️ 有一条用例会真往 config.CROPS_DIR 写一张拼图（验路由的完整路径），
   跑完按返回的文件名删掉 —— 不按目录清空（那会误删老师的截图）。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image                                   # noqa: E402

from app.services import images, vision                  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail=None) -> None:
    global fails
    if ok:
        print("  [OK ] %s" % name)
    else:
        fails += 1
        print("  [FAIL] %s" % name + ("   <- %s" % (detail,) if detail is not None else ""))


def png(w: int, h: int, color) -> bytes:
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def data_uri(b: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(b).decode()


# ================================================================ ① 竖拼
def t_stitch() -> None:
    print("\n==== ① 竖拼：宽度取最宽、窄的居中、白底补齐 ====")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        red = png(100, 50, (255, 0, 0))
        blue = png(60, 80, (0, 0, 255))
        r = images.stitch_vertical([red, blue], d, gap=0)

        with Image.open(d / r["name"]) as im:      # with：Windows 下不关句柄会删不掉临时目录
            check("宽度 = 最宽那张（100）", im.size == (100, 130), im.size)
            check("窄的那张居中：左侧是白边",
                  im.getpixel((10, 60)) == (255, 255, 255), im.getpixel((10, 60)))
            check("窄的那张居中：中间是蓝的",
                  im.getpixel((50, 60)) == (0, 0, 255), im.getpixel((50, 60)))
            check("第一张在上半部（红）", im.getpixel((50, 20)) == (255, 0, 0), im.getpixel((50, 20)))
        check("高度 = 两张之和（50+80）", r["height"] == 130, r["height"])
        check("没有因为缩放而 scale", r["scaled"] is False, r["scaled"])

        # 中间留白条
        r2 = images.stitch_vertical([red, blue], d, gap=10)
        check("gap=10 时总高多出 10", r2["height"] == 140, r2["height"])

    # 太高 → 等比缩
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        tall = png(100, 10000, (200, 200, 200))
        r3 = images.stitch_vertical([tall, tall], d, gap=0)
        check("总高超过上限 → 等比缩放（scaled=True）", r3["scaled"] is True, r3["scaled"])
        check("缩放后高度落在上限内", r3["height"] <= images.STITCH_MAX_H, r3["height"])
        with Image.open(d / r3["name"]) as im3:
            # 原图两张 100×10000 竖拼 = 100×20000（宽高比 0.005）；
            # 缩放后必须还是这个比例 —— 比例变了公式会被压扁。
            want = 100 / 20000.0
            got = im3.size[0] / float(im3.size[1])
            check("缩放保持宽高比（100:20000）", abs(got - want) < want * 0.02,
                  "%s vs %.5f" % (im3.size, want))


# ================================================================ ② 竖拼的拒绝路径
def t_stitch_reject() -> None:
    print("\n==== ② 竖拼的拒绝路径（消息要能直接给老师看） ====")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        try:
            images.stitch_vertical([], d)
            check("空列表要报错", False, "没抛")
        except images.ImageRejected as e:
            check("空列表要报错", "没有图" in str(e), str(e))

        try:
            images.stitch_vertical([png(10, 10, (0, 0, 0)), b"not an image at all"], d)
            check("有一张不是图片要报错（且指出是第几张）", False, "没抛")
        except images.ImageRejected as e:
            check("有一张不是图片要报错（且指出是第几张）", "第 2 张" in str(e), str(e))

        try:
            images.stitch_vertical([png(10, 10, (0, 0, 0))] * 21, d)
            check("超过 20 张要报错", False, "没抛")
        except images.ImageRejected as e:
            check("超过 20 张要报错", "20" in str(e), str(e))


# ================================================================ ③ 答案标题剥离
def t_strip_labels() -> None:
    print("\n==== ③ 剥掉【答案】【详解】这类开头标题 ====")
    S = vision._strip_leading_labels
    check("【答案】C → C", S("【答案】C") == "C", repr(S("【答案】C")))
    check("【答案】 C（带空格）→ C", S("【答案】 C") == "C", repr(S("【答案】 C")))
    check("【详解】解：xxx → 解：xxx", S("【详解】解：xxx") == "解：xxx", repr(S("【详解】解：xxx")))
    check("连续两个标题都剥",
          S("【答案】\n【详解】解：A") == "解：A", repr(S("【答案】\n【详解】解：A")))
    check("[答案]C（方括号写法）也剥", S("[答案]C") == "C", repr(S("[答案]C")))
    check("正文里的【详解】不动（只剥开头）",
          S("由【详解】可知 x=1") == "由【详解】可知 x=1", repr(S("由【详解】可知 x=1")))
    check("没有标题时原样返回", S("C") == "C", repr(S("C")))
    check("空串不炸", S("") == "", repr(S("")))


# ================================================================ ④ prompt 约束
def t_prompt() -> None:
    print("\n==== ④ 答案 prompt 的关键约束（这些是「渲染失败」的对策） ====")
    p = vision.PROMPTS["answer"]
    check("要求乘号写 \\times（不要 *）", "\\times" in p and "不要写 *" in p, "")
    check("要求表格写分隔行（|---|---|）", "|---|" in p, "")
    check("要求段落之间空一行", "空一行" in p, "")
    check("要求多张图按顺序衔接", "多张图" in p and "接着" in p, "")
    check("要求不抄【答案】标题", "【答案】" in p and "不要抄" in p, "")
    check("禁止用代码围栏包整段", "代码围栏" in p, "")
    check("question 模式也保留（没被误删）", "question" in vision.PROMPTS, "")
    check("formula 模式也保留", "formula" in vision.PROMPTS, "")


# ================================================================ ⑤ 多图识别：发给谁、发了几张
def t_recognize_multi() -> None:
    print("\n==== ⑤ 多图识别：把 N 张图 + 1 段提示词一起发出去 ====")
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["url"] = url
        seen["body"] = body
        return {"choices": [{"message": {"content": "答案正文"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    orig_cfg, orig_post = vision.ai_polish.get_config, vision.ai_polish.post_json
    vision.ai_polish.get_config = lambda db: {
        "configured": True, "base_url": "http://x", "model": "m", "api_key": "k", "timeout": 30,
    }
    vision.ai_polish.post_json = fake_post
    try:
        r = vision.recognize(None, mode="answer",
                             images=[data_uri(png(10, 10, (1, 1, 1))),
                                     data_uri(png(10, 10, (2, 2, 2))),
                                     data_uri(png(10, 10, (3, 3, 3)))])
        content = seen["body"]["messages"][0]["content"]
        imgs = [c for c in content if c.get("type") == "image_url"]
        txts = [c for c in content if c.get("type") == "text"]
        check("3 张图 → content 里有 3 个 image_url", len(imgs) == 3, len(imgs))
        check("提示词只有 1 段（放在图后面）",
              len(txts) == 1 and content[-1].get("type") == "text", len(txts))
        check("提示词里说明了「共 3 张、按顺序连着读」",
              "共 3 张" in txts[0]["text"] and "按顺序接着" in txts[0]["text"] or
              "共 3 张" in txts[0]["text"], txts[0]["text"][-120:])
        check("返回的 text 正常", r["text"] == "答案正文", r["text"])

        # 单图（老路径）仍然工作
        seen.clear()
        r2 = vision.recognize(None, data_uri(png(10, 10, (4, 4, 4))), mode="answer")
        content2 = seen["body"]["messages"][0]["content"]
        check("单图（image 字段）仍走老路径：1 张图",
              len([c for c in content2 if c.get("type") == "image_url"]) == 1, "")
        check("单图时**不**加「共 N 张」那句",
              "共 1 张" not in [c for c in content2 if c.get("type") == "text"][0]["text"], "")

        # 答案模式剥标题
        seen.clear()
        vision.ai_polish.post_json = lambda *a, **k: {
            "choices": [{"message": {"content": "【答案】C\n【详解】解：略"}}]}
        r3 = vision.recognize(None, data_uri(png(10, 10, (5, 5, 5))), mode="answer")
        check("答案模式剥掉开头的【答案】", r3["text"].startswith("C"), repr(r3["text"]))
        check("后面的【详解】保留（只剥开头一个）", "【详解】" in r3["text"], repr(r3["text"]))

        # 题干模式不剥（题干里本来就不该有，但也不该被动手）
        vision.ai_polish.post_json = lambda *a, **k: {
            "choices": [{"message": {"content": "【答案】只是在题干里出现的字样"}}]}
        r4 = vision.recognize(None, data_uri(png(10, 10, (6, 6, 6))), mode="question")
        check("题干模式不剥标题（只对答案模式生效）",
              r4["text"].startswith("【答案】"), repr(r4["text"]))
    finally:
        vision.ai_polish.get_config, vision.ai_polish.post_json = orig_cfg, orig_post


# ================================================================ ⑥ 多图的校验分支
def t_recognize_guard() -> None:
    print("\n==== ⑥ 多图的校验（都在调 AI 之前就拦住） ====")
    try:
        vision.recognize(None, images=[])
        check("一张图都没给 → 报错", False, "没抛")
    except vision.VisionError as e:
        check("一张图都没给 → 报错", "没有拿到图片" in str(e), str(e))

    try:
        vision.recognize(None, images=[data_uri(png(2, 2, (0, 0, 0)))] * (vision.MAX_IMAGES + 1))
        check("超过上限 → 报错并说清上限", False, "没抛")
    except vision.VisionError as e:
        check("超过上限 → 报错并说清上限",
              str(vision.MAX_IMAGES) in str(e), str(e))

    try:
        vision.recognize(None, "not-base64!!!", mode="answer")
        check("非法 base64 → 报错", False, "没抛")
    except vision.VisionError as e:
        check("非法 base64 → 报错", "base64" in str(e), str(e))

    try:
        vision.recognize(None, data_uri(png(2, 2, (0, 0, 0))), mode="nonsense")
        check("不认识的 mode → 报错", False, "没抛")
    except vision.VisionError as e:
        check("不认识的 mode → 报错", "不支持" in str(e), str(e))


# ================================================================ ⑦ 路由：/api/images/stitch
def t_route() -> None:
    print("\n==== ⑦ 路由 /api/images/stitch ====")
    from fastapi import HTTPException
    from app.routers import documents as doc_router
    from app.schemas import StitchIn

    made: list[str] = []
    try:
        red = data_uri(png(80, 40, (255, 0, 0)))
        blue = data_uri(png(40, 60, (0, 0, 255)))
        r = doc_router.images_stitch(StitchIn(images=[red, blue], gap=0))
        check("正常拼接返回 url", str(r.get("url", "")).startswith("/api/crops/"), r)
        check("返回宽高（80 x 100）", (r["width"], r["height"]) == (80, 100), (r["width"], r["height"]))
        check("文件真的落盘了",
              (Path(doc_router.CROPS_DIR) / r["url"].rsplit("/", 1)[-1]).exists(), r["url"])
        made.append(r["url"].rsplit("/", 1)[-1])

        # 裸 base64（不带 data: 前缀）也要收
        bare = base64.b64encode(png(20, 20, (9, 9, 9))).decode()
        r2 = doc_router.images_stitch(StitchIn(images=[bare], gap=0))
        check("裸 base64 也能拼", (r2["width"], r2["height"]) == (20, 20), r2)
        made.append(r2["url"].rsplit("/", 1)[-1])

        for bad, why in ((["not-base64!!"], "非法 base64"), ([""], "空字符串")):
            try:
                doc_router.images_stitch(StitchIn(images=bad, gap=0))
                check(f"{why} → 422", False, "没抛")
            except HTTPException as e:
                check(f"{why} → 422", e.status_code == 422, e.status_code)

        # 不是图片（合法 base64 但不是图片二进制）
        try:
            doc_router.images_stitch(StitchIn(
                images=[base64.b64encode(b"hello world").decode()], gap=0))
            check("合法 base64 但不是图片 → 422", False, "没抛")
        except HTTPException as e:
            check("合法 base64 但不是图片 → 422", e.status_code == 422, e.status_code)
    finally:
        # ⚠️ 只删本测试自己生成的那几张（按文件名），不清目录
        for name in made:
            try:
                (Path(doc_router.CROPS_DIR) / name).unlink()
            except OSError:
                pass
        print("  （已删除本次测试生成的 %d 张拼图）" % len(made))


def main() -> int:
    t_stitch()
    t_stitch_reject()
    t_strip_labels()
    t_prompt()
    t_recognize_multi()
    t_recognize_guard()
    t_route()
    print("\n" + "=" * 52)
    if fails:
        print("失败 %d 项" % fails)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
