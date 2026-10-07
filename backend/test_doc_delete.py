# -*- coding: utf-8 -*-
"""删除文档：删干净、但不误伤。

    cd backend && ./.venv/Scripts/python.exe test_doc_delete.py

8000 的服务要在跑（脚本打 HTTP，不直连数据库）。自己造文档、自己造图、自己清场，
跑完不在库里留任何东西。

验的是这几条：
  · 影响预演（delete-impact）报的数字和实际删掉的一致 —— 确认框里的账不能是假账
  · 删文档**默认不删题**：题目留在题库里，只是 document_id 被清空
  · with_questions=true 才连题一起删
  · 文件真的没了：原件、页面缓存（这两个最容易「以为删了其实还在」）
  · **共享截图不能误伤**：同一张图被别的题引用时必须留着（引用计数）
  · 不存在的文档报 404，不是 500

⚠️ 两个坑写在这里，别再踩：
   1. 截图自己造（CROPS_DIR 下临时文件），绝不借用库里已有的 —— 删文档会回收
      「无人引用」的截图，借来的图会真被删掉。
   2. 删文档会触发一次自动备份（migrate.backup_db），所以 data/backups/ 会多快照，
      盘上留几份是预期行为（超出保留份数会自动清最旧的）。
"""
import io
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import config  # noqa: E402  只为拿 CROPS_DIR / PAGES_CACHE，不连数据库

BASE = "http://127.0.0.1:8000"
fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)


def req(method: str, path: str, body=None):
    """发请求。响应不是 JSON（比如页面图是 PNG 二进制）时原样返回 bytes 的文本形式。

    刻意宽一些地接住解析失败：这个脚本要顺手取一次页面图来确认缓存真的产生了，
    二进制响应不能让它崩在 JSON 解析上。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            return resp.status, _decode(raw)
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())


def _decode(raw: bytes):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def upload(data: bytes, filename: str):
    """multipart 上传（用标准库拼，不引第三方依赖）。"""
    boundary = "----shikeTest" + uuid.uuid4().hex[:12]
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/pdf\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    r = urllib.request.Request(
        BASE + "/api/documents", data=head + data + tail, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def make_pdf() -> bytes:
    """造一份单页 PDF（真文件，能让后端解析出文字块与页数）。

    用 pymupdf —— 项目自己的 PDF 库，必然装着；不为了造个测试文件再引一个依赖。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 700), "shike doc-delete test")
    return doc.tobytes()


def make_crop(name: str) -> Path:
    """造一张只属于本次测试的截图，返回磁盘路径。"""
    from PIL import Image

    config.CROPS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.CROPS_DIR / name
    buf = io.BytesIO()
    Image.new("RGB", (80, 50), (250, 250, 250)).save(buf, "PNG")
    path.write_bytes(buf.getvalue())
    return path


def mk_question(doc_id: str, doc_name: str, image: str = "") -> str:
    st, r = req("POST", "/api/questions",
                {"document_id": doc_id, "doc_filename": doc_name,
                 "content": "临时题 $a+b$", "image": image, "answer": "x"})
    assert st == 200, (st, r)
    return r["id"]


def fetch(qid: str) -> dict:
    """按 id 取题（没有单题接口，从列表里挑）。"""
    _, lst = req("GET", "/api/questions")
    return next((q for q in lst if q["id"] == qid), {})


def doc_ids() -> set[str]:
    _, lst = req("GET", "/api/documents")
    return {d["id"] for d in lst}


def cleanup_questions(qids: list[str]) -> None:
    for q in qids:
        req("DELETE", f"/api/questions/{q}")


def main() -> int:
    shared = make_crop("t_doc_delete_shared.png")     # 会被「文档内」与「文档外」两道题共用
    sole = make_crop("t_doc_delete_sole.png")         # 只被文档内那一题用
    print(f"   测试图：{shared.name} / {sole.name}")

    created_q: list[str] = []      # 所有临时题，最后兜底清场
    created_doc: list[str] = []    # 所有临时文档，异常时兜底清场
    try:
        print("\n==== ① 不存在的文档：404，不是 500 ====")
        st, _ = req("GET", "/api/documents/no-such-doc/delete-impact")
        check("预演 404", st == 404, str(st))
        st, _ = req("DELETE", "/api/documents/no-such-doc")
        check("删除 404", st == 404, str(st))

        print("\n==== ② 上传一份临时卷子 ====")
        st, up = upload(make_pdf(), "t_doc_delete_测试卷.pdf")
        check("上传成功", st == 200, str(up)[:80] if st != 200 else "")
        if st != 200:
            return 1
        doc1 = up["id"]
        created_doc.append(doc1)

        # 让页面缓存真的产生（不然「清缓存」这条测了个寂寞）
        req("GET", f"/api/documents/{doc1}/pages/1/image")
        cache_dir = config.PAGES_CACHE / doc1
        check("页面缓存已生成", cache_dir.is_dir(), str(cache_dir))

        print("\n==== ③ 挂两道题到它下面 + 一道不属于它的题 ====")
        qa = mk_question(doc1, "t_doc_delete_测试卷.pdf", f"/api/crops/{shared.name}")
        qb = mk_question(doc1, "t_doc_delete_测试卷.pdf")
        q_out = mk_question("t-doc-delete-outside", "测试用", f"/api/crops/{shared.name}")
        created_q += [qa, qb, q_out]

        st, imp = req("GET", f"/api/documents/{doc1}/delete-impact")
        check("预演：题目数 = 2", imp.get("questions") == 2, str(imp.get("questions")))
        check("预演：共享图不算可回收（别处还用着）", imp.get("crops") == 0, str(imp.get("crops")))
        src = next((f for f in imp.get("files", []) if f["kind"] == "source"), None)
        check("预演：列出了原件", src is not None)
        check("预演：列出了页面缓存",
              any(f["kind"] == "pages" for f in imp.get("files", [])))
        if not src:
            return 1

        print("\n==== ④ 删文档但保留题目 ====")
        st, r = req("DELETE", f"/api/documents/{doc1}")
        check("删除成功", st == 200, str(r)[:80])
        check("没删题（questions_removed=0）", r.get("questions_removed") == 0, str(r.get("questions_removed")))
        check("报出保留了几道题", r.get("questions_kept") == 2, str(r.get("questions_kept")))
        check("题目还在题库里", fetch(qa).get("id") == qa and fetch(qb).get("id") == qb)
        check("题的 document_id 已清空", fetch(qa).get("document_id") is None,
              str(fetch(qa).get("document_id")))
        check("来源名 doc_filename 还留着", fetch(qa).get("doc_filename") == "t_doc_delete_测试卷.pdf")
        check("文档列表里没有了", doc1 not in doc_ids())
        check("原始文件已删", not (config.UPLOAD_DIR / src["name"]).exists(), src["name"])
        check("页面缓存目录已删", not cache_dir.exists())
        check("共享图没被误删（别的题还用着）", shared.exists())
        check("留了一份数据库备份", bool(r.get("backup")), str(r.get("backup")))
        created_doc.remove(doc1)

        print("\n==== ⑤ 删文档 + 连题一起删 ====")
        st, up = upload(make_pdf(), "t_doc_delete_测试卷2.pdf")
        doc2 = up["id"]
        created_doc.append(doc2)
        qc = mk_question(doc2, "t_doc_delete_测试卷2.pdf", f"/api/crops/{sole.name}")
        qd = mk_question(doc2, "t_doc_delete_测试卷2.pdf", f"/api/crops/{shared.name}")
        created_q += [qc, qd]

        st, imp = req("GET", f"/api/documents/{doc2}/delete-impact")
        check("预演：可回收 1 张（独占那张）", imp.get("crops") == 1, str(imp.get("crops")))

        st, r = req("DELETE", f"/api/documents/{doc2}?with_questions=true")
        check("删除成功", st == 200, str(r)[:80])
        check("题被一起删了（2 道）", r.get("questions_removed") == 2, str(r.get("questions_removed")))
        check("题目确实没了", not fetch(qc) and not fetch(qd))
        check("独占截图已回收", not sole.exists())
        check("共享截图仍留着", shared.exists())
        check("文档记录已删", doc2 not in doc_ids())
        created_q = [q for q in created_q if q not in (qc, qd)]
        created_doc.remove(doc2)

        print("\n==== ⑥ 引用计数：最后一个引用消失时才回收 ====")
        # 此刻引用共享图的还有两道题：qa（第④步保留下来的）和 q_out（路外的那道）
        st, r = req("DELETE", f"/api/questions/{q_out}")
        check("删掉路外那道题", st == 200, str(r)[:60])
        check("还有题在引用 → 共享图仍留着", shared.exists())
        check("这次不该回收任何图", r.get("crops_removed") == 0, str(r.get("crops_removed")))
        created_q.remove(q_out)

        st, r = req("DELETE", f"/api/questions/{qa}")
        check("删掉最后一个引用它的题", st == 200, str(r)[:60])
        check("此时共享图才被回收", not shared.exists())
        created_q.remove(qa)
    finally:
        cleanup_questions(created_q)
        for d in created_doc:
            req("DELETE", f"/api/documents/{d}?with_questions=true")
        for p in (shared, sole):
            if p.exists():
                p.unlink()
                print(f"     兜底删掉了 {p.name}")

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
