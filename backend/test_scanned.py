# -*- coding: utf-8 -*-
"""端到端测试：扫描版 PDF 上传 → 纯截图模式（页面图 / 空行 / 裁剪 / 图片题入库）。"""
import json
import time
import urllib.request
import os
from pathlib import Path

# 免代理 opener（契约第六节第 2 条）：本机 http_proxy 会把 127.0.0.1 的请求也送去代理，
# 回来是 502 Bad Gateway —— 于是「服务没起」被误读成「服务坏了」，
# 更坏的是 502 的响应体被当成数据往下跑（报 byte indices 之类，完全误导）。
# 本机 HTTP 一律走这个 opener；连不上就该是 ConnectionRefused，红得明白。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

BASE = os.environ.get("SHIKE_BASE") or (
    "http://127.0.0.1:8010" if Path(__file__).resolve().parents[1].name == "question-bank-ai"
    else "http://127.0.0.1:8000")   # 主仓 8000 / -ai 检出 8010，SHIKE_BASE 可覆盖
PY = "C:/Users/6/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

for i in range(15):
    try:
        _OPENER.open(BASE + "/api/tree"); break
    except Exception:
        time.sleep(1)

def multipart(path, field="file"):
    fn = path.replace("\\", "/").split("/")[-1]
    with open(path, "rb") as f:
        content = f.read()
    boundary = "----testboundary123"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{fn}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/documents", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.loads(_OPENER.open(req).read())

def post_json(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(_OPENER.open(req).read())

def get(url):
    return _OPENER.open(url)

# 1. 上传扫描版 PDF
SAMPLES = "Q:/Program Files/workbuddy/2026-08-15-01-58-43/question-bank/samples"
doc = multipart(SAMPLES + "/扫描版样例卷.pdf")
assert doc["scanned"] == 1 and doc["block_count"] == 0, doc
print("1. 上传扫描版: OK  scanned=%d blocks=%d" % (doc["scanned"], doc["block_count"]))

# 2. 页面接口
meta = json.loads(get(f"{BASE}/api/documents/{doc['id']}/pages").read())
assert meta["page_count"] >= 1
r = get(f"{BASE}/api/documents/{doc['id']}/pages/1/image")
assert r.headers["Content-Type"] == "image/png"
lines = json.loads(get(f"{BASE}/api/documents/{doc['id']}/pages/1/lines").read())
assert lines["lines"] == [], "扫描版不应有文字行"
print("2. 页面图/行接口: OK  page_count=%d lines=0 width=%s" % (meta["page_count"], lines["width"]))

# 3. 裁剪截图
crop = post_json(f"{BASE}/api/documents/{doc['id']}/crop",
                 {"page": 1, "x0": 20, "y0": 30, "x1": 560, "y1": 320})
r = get(BASE + crop["url"])
assert r.headers["Content-Type"] == "image/png"
print("3. 高清裁剪: OK", crop["url"])

# 4. 图片题入库（纯图片，无文本）
q = post_json(BASE + "/api/questions", {
    "document_id": doc["id"], "doc_filename": "扫描版样例卷.pdf",
    "start_block": -1, "end_block": -1, "content": "",
    "qtype": "图片题", "difficulty": "中档",
    "knowledge_points": ["函数/函数的概念与性质"], "answer": "", "analysis": "",
    "image": crop["url"]})
print("4. 图片题入库: OK  id=%s" % q["id"])

# 5. 正常 PDF 不受影响（回归）
d2 = multipart(SAMPLES + "/高二数学专项测试卷.pdf")
assert d2["scanned"] == 0 and d2["block_count"] > 0
lines2 = json.loads(get(f"{BASE}/api/documents/{d2['id']}/pages/1/lines").read())
assert len(lines2["lines"]) > 0
print("5. 正常PDF回归: OK  scanned=0 blocks=%d lines=%d" % (d2["block_count"], len(lines2["lines"])))

print("\n全部通过 ✅")
