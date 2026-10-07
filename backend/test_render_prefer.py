# -*- coding: utf-8 -*-
"""录题形态：出卷时这一题印「文本」还是印「原貌图」。

    cd backend && ./.venv/Scripts/python.exe test_render_prefer.py

8000 的服务要在跑（脚本打 HTTP，不直连数据库）。自建自删，不碰已有题目。

验的是这几条：
  · 每题偏好（auto / text / image）存得进、取得出、改得动
  · 出卷时的整卷覆盖能压过单题偏好
  · **缺那种形态时自动退回另一种** —— 这条最要紧：否则「选了图片」
    会印出一道空白题，而且要到卷子发到学生手上才发现
  · 拼错的值报 422 而不是静默降级
  · 老前端（不认识这个字段）行为不变

⚠️ 图片**自己造、自己删**，绝不借用库里已有的截图。
   踩过一次：一开始图省事，从 /api/uploads/images 里挑一张来用，
   结果 delete_question 有「删题时清理无人引用的截图」的逻辑 ——
   测试建题引用了那张图、删题时它就变成了「无人引用」，被真实删掉了。
   测试是来保护数据的，反倒把用户库里的文件删了。这个坑不要再踩。
"""
import io
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import config  # noqa: E402  只为拿 CROPS_DIR，不连数据库

BASE = "http://127.0.0.1:8000"
fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)


def req(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode("utf-8", "replace")


def strip_tags(html: str) -> str:
    """剥标签只看正文 —— 判断「这段字到底印没印出来」，直接子串匹配会被
    <style> 里的样式文本误伤。"""
    return re.sub(r"<[^>]+>", " ", html)


def fetch(qid: str) -> dict:
    """按 id 取一道题。路由里没有单题查询接口，从列表里挑。"""
    _, lst = req("GET", "/api/questions")
    return next((q for q in lst if q["id"] == qid), {})


def mk(content: str, image: str, prefer: str | None = None) -> str:
    body = {"document_id": "t-render-prefer", "doc_filename": "测试用",
            "content": content, "image": image, "answer": "答案 x=1"}
    if prefer:
        body["render_prefer"] = prefer
    st, r = req("POST", "/api/questions", body)
    assert st == 200, (st, r)
    return r["id"]


def export(qid: str, **kw) -> str:
    qs = "".join(f"&{k}={v}" for k, v in kw.items())
    st, html = req("GET", f"/api/papers/export?ids={qid}&format=html{qs}")
    assert st == 200, (st, html)
    return html


def make_test_image() -> tuple[str, Path]:
    """造一张只属于本次测试的图，返回 (url, 磁盘路径)。"""
    from PIL import Image

    config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
    name = "t_render_prefer.png"
    path = config.CROPS_DIR / name
    buf = io.BytesIO()
    Image.new("RGB", (80, 50), (255, 255, 255)).save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    return f"/api/crops/{name}", path


def main() -> int:
    img, img_path = make_test_image()
    print(f"   测试图：{img_path}")
    created: list[str] = []
    try:
        print("\n==== ① 每题偏好：存得进、取得出、改得动 ====")
        qa = mk("设 $x^2=1$，求 $x$。", img)                 # auto
        qb = mk("设 $x^2=1$，求 $x$。", img, "image")        # 强制图
        qc = mk("", img, "text")                             # 只有图却要文本 → 应退回图
        qd = mk("只有文本的题 $a+b$。", "")                    # 只有文本
        created += [qa, qb, qc, qd]

        check("不传 → auto", fetch(qa).get("render_prefer") == "auto")
        check("image 存得住", fetch(qb).get("render_prefer") == "image")
        check("text 存得住", fetch(qc).get("render_prefer") == "text")
        req("PATCH", f"/api/questions/{qa}", {"render_prefer": "image"})
        check("PATCH 改得动", fetch(qa).get("render_prefer") == "image")
        req("PATCH", f"/api/questions/{qa}", {"render_prefer": "auto"})
        check("PATCH 改得回", fetch(qa).get("render_prefer") == "auto")

        print("\n==== ② auto：有文本用文本，不再压一张图 ====")
        h = export(qa)
        check("印了题干文本", "x^2" in strip_tags(h))
        check("没印原貌图（以前是文字+图都印）", "data:image" not in h)

        print("\n==== ③ 单题偏好 image：印图，不印题干文字 ====")
        h = export(qb)
        check("印了原貌图", "data:image" in h)
        check("没印题干文字", "设 x^2=1" not in strip_tags(h).replace("$", ""))

        print("\n==== ④ 整卷覆盖 text：压过单题偏好 ====")
        h = export(qb, render_mode="text")
        check("改印文本", "x^2" in strip_tags(h))
        check("不再印图", "data:image" not in h)

        print("\n==== ⑤ 整卷覆盖 image：压过单题偏好 ====")
        h = export(qa, render_mode="image")
        check("改印图", "data:image" in h)
        check("不再印题干文字", "设 x^2=1" not in strip_tags(h).replace("$", ""))

        print("\n==== ⑥ 缺形态自动退回，绝不印空白题 ====")
        check("偏好文本但没文本 → 退回图", "data:image" in export(qc))
        check("整卷要文本但没文本 → 仍退回图", "data:image" in export(qc, render_mode="text"))
        check("整卷要图但没图 → 退回文本",
              "只有文本的题" in strip_tags(export(qd, render_mode="image")))

        print("\n==== ⑦ 拼错的值报 422，不静默降级 ====")
        st, _ = req("GET", f"/api/papers/export?ids={qa}&format=html&render_mode=png")
        check("非法 render_mode → 422", st == 422, str(st))
        st, _ = req("POST", "/api/questions",
                    {"document_id": "t", "content": "x", "render_prefer": "jpg"})
        check("非法 render_prefer → 422", st == 422, str(st))

        print("\n==== ⑧ 老前端（不认识这个字段）行为不变 ====")
        st, r = req("POST", "/api/questions", {"document_id": "t", "content": "老调用 $y$。"})
        created.append(r["id"])
        check("不传 → auto", fetch(r["id"]).get("render_prefer") == "auto")
        check("照常导得出 HTML", "老调用" in strip_tags(export(r["id"])))
    finally:
        n = sum(1 for qid in created if req("DELETE", f"/api/questions/{qid}")[0] == 200)
        print(f"\n==== 清理 ====\n     删了 {n} 道临时题（共 {len(created)}）")
        # 删题时服务端已经会清掉无人引用的截图；这里再兜一次底，
        # 防止测试中途抛异常、图没被回收（测试不该给用户留下垃圾文件）。
        if img_path.exists():
            img_path.unlink()
            print("     删掉了测试用图")

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
