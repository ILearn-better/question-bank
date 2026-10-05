# -*- coding: utf-8 -*-
"""vision.py 的自检：纯函数 + 提示词完整性。**默认不联网、不花钱。**

跑法：
    cd backend && ./.venv/Scripts/python.exe test_vision.py

真实识别（需要联网 + 已配置 AI + samples 里有真题）：
    cd backend && ./.venv/Scripts/python.exe test_vision.py --real
    会渲染 samples/2025真题 里的页面，走一遍 /api/ai/recognize，打印耗时与用量。
    samples/ 不入仓库，所以这一步在其他机器上会自动跳过。
"""
from __future__ import annotations

import base64
import io
import sys

fails: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}\n      期望 {want!r}\n      实际 {got!r}")
        fails.append(name)


def truthy(name: str, v) -> None:
    check(name, bool(v), True)


def rejected(name: str, fn) -> None:
    """确认某个输入被**拒绝**。不报错反而是 bug —— 那意味着脏数据会往下走。"""
    from app.services import vision
    try:
        fn()
    except vision.VisionError:
        print(f"  ✓ {name} 被正确拒绝")
        return
    except Exception as e:                      # noqa: BLE001
        print(f"  ✗ {name} 抛了非 VisionError：{type(e).__name__}: {e}")
        fails.append(name)
        return
    print(f"  ✗ {name} 竟被接受（应该报错）")
    fails.append(name)


def main() -> int:
    from app.services import vision

    print("\n[1] 提示词完整性 —— Python 有没有把 LaTeX 反斜杠吃掉")
    print("    （这是本模块最大的静默风险：写错一个 \\f，模型收到的提示词就面目全非，")
    print("      但它照样有输出，只是公式规范变差，看不出是哪里的问题）")
    for mode, prompt in vision.PROMPTS.items():
        # 转义事故两个模式都要查
        check(f"{mode}: 无换页符 \\f", "\f" in prompt, False)
        check(f"{mode}: 无制表符 \\t", "\t" in prompt, False)
        check(f"{mode}: 无退格符 \\b", "\b" in prompt, False)
        check(f"{mode}: 无垂直制表符 \\v", "\v" in prompt, False)
        truthy(f"{mode}: 说明了 $...$ 定界方式", "$...$" in prompt)
    # 「常用写法清单」只在 question 模式里给：formula 模式的任务是「把看到的式子
    # 转成 LaTeX」，不需要教它 \frac 长什么样，列了反而是噪音。
    q = vision.PROMPTS["question"]
    for cmd in ("\\frac", "\\sqrt", "\\int", "\\sum", "\\alpha"):
        truthy(f"question: 含字面 {cmd}", cmd in q)
    check("模式清单", sorted(vision.PROMPTS), ["answer", "formula", "question"])
    # 答案模式独有的一条硬要求：必须交代「保留换行与步骤顺序」。
    # 少了它，整段解题过程会被挤成一坨，看不出推导到哪一步 —— 这是答案和题干
    # 最大的差别，也是这个模式存在的理由。
    truthy("answer: 要求保留换行", "换行" in vision.PROMPTS["answer"])

    print("\n[2] base64 解码")
    raw = b"\x89PNG\r\n\x1a\n" + b"payload" * 4
    b64 = base64.b64encode(raw).decode("ascii")
    check("裸 base64", vision._decode_data_uri(b64), raw)
    check("data URI 前缀", vision._decode_data_uri("data:image/png;base64," + b64), raw)
    check("含换行的 base64（FileReader 会这样）",
          vision._decode_data_uri(b64[:8] + "\n" + b64[8:]), raw)
    rejected("空图片", lambda: vision._decode_data_uri(""))
    rejected("不是 base64 的垃圾串", lambda: vision._decode_data_uri("!!!not base64!!!"))

    print("\n[3] 图片压缩")
    from PIL import Image

    def make(w: int, h: int, mode: str = "RGB") -> bytes:
        buf = io.BytesIO()
        Image.new(mode, (w, h), (250, 250, 250)).save(buf, "PNG")
        return buf.getvalue()

    def decoded(uri: str) -> Image.Image:
        return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))

    uri, original = vision.prepare_image(make(3200, 2400))
    check("回传的是原图尺寸（预览要用）", original, (3200, 2400))
    im = decoded(uri)
    check("长边压到 1600", max(im.size), 1600)
    truthy("压缩后仍等比", abs(im.size[0] / im.size[1] - 3200 / 2400) < 0.02)
    truthy("输出是 JPEG（体积小得多）", uri.startswith("data:image/jpeg;base64,"))

    # 截图工具产出的 PNG 常常带 alpha —— 少了 convert("RGB") 这一步，save 会直接抛 OSError
    uri_rgba, _ = vision.prepare_image(make(400, 300, "RGBA"))
    truthy("带 alpha 的 PNG 能转成 JPEG", decoded(uri_rgba).mode == "RGB")

    # 小图不该被放大：放大只会让请求体白涨，分辨率并不会凭空变高
    uri_small, _ = vision.prepare_image(make(300, 200))
    check("小图不被放大", decoded(uri_small).size, (300, 200))

    rejected("空字节", lambda: vision.prepare_image(b""))
    rejected("不是图片的内容", lambda: vision.prepare_image(b"definitely not an image"))

    print("\n[4] 剥代码围栏")
    check("没有围栏就原样返回（只去首尾空白）",
          vision._strip_fence("  $x^2+1$  "), "$x^2+1$")
    check("剥掉 ```latex 围栏", vision._strip_fence("```latex\n$x^2$\n```"), "$x^2$")
    check("剥掉裸 ``` 围栏", vision._strip_fence("```\n$x$\n```"), "$x$")
    check("多行正文不被破坏",
          vision._strip_fence("$a$\n$b$"), "$a$\n$b$")

    print("\n[5] 错误归类")
    truthy("VisionError 继承 AiError（路由层靠它统一转 502）",
           issubclass(vision.VisionError, vision.ai_polish.AiError))
    # mode 校验发生在读配置之前，所以 db=None 也不会碰到数据库
    rejected("非法 mode", lambda: vision.recognize(None, "x", mode="nope"))

    if "--real" in sys.argv:
        run_real()

    print()
    if fails:
        print(f"✗ {len(fails)} 项未通过：{fails}")
        return 1
    print("✓ 全部通过")
    return 0


def run_real() -> None:
    """真实识别：渲染 samples 里的真题，走一遍接口。需要服务在 8000 且已配 AI。"""
    import json
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    print("\n[6] 真实识别（--real）")
    samples = Path(__file__).resolve().parent.parent / "samples" / "2025真题"
    if not samples.is_dir():
        print(f"  – 跳过：没有 {samples}（samples/ 不入仓库，属正常）")
        return
    pdf = samples / "2025 P1.pdf"
    if not pdf.exists():
        print(f"  – 跳过：{pdf.name} 不在")
        return

    import pymupdf
    doc = pymupdf.open(pdf)
    img = doc[2].get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")
    body = json.dumps({
        "image": base64.b64encode(img).decode("ascii"),
        "mode": "question",
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/ai/recognize", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"  – 跳过：接口不可达（{e.reason}）—— 服务起了吗？")
        return

    text = data.get("text") or ""
    usage = data.get("usage") or {}
    print(f"  原图 {len(img) / 1024:.0f} KB，耗时 {time.time() - t0:.1f}s")
    print(f"  token: prompt={usage.get('prompt')} completion={usage.get('completion')}")
    truthy("识别出内容", text.strip())
    truthy("公式带 $ 定界符", "$" in text)
    # reasoning_effort 生效时输出很小；没生效会到三四千（推理吃掉了绝大部分）
    completion = usage.get("completion") or 0
    truthy(f"输出预算没被推理吃掉（completion={completion} < 2000）", completion < 2000)
    print("  ---- 识别结果 ----")
    for line in text.splitlines()[:4]:
        print("   ", line[:140])


if __name__ == "__main__":
    sys.exit(main())
