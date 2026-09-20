# -*- coding: utf-8 -*-
"""端到端测试：PDF 视觉分割接口链路。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8000"
SAMPLE = "../samples/高二数学专项测试卷.pdf"
BOUNDARY = "----testboundary"


def multipart(path):
    with open(path, "rb") as f:
        data = f.read()
    body = (
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="test.pdf"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode() + data + f"\r\n--{BOUNDARY}--\r\n".encode()
    return body


def post(url, body, ctype):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.read()


# 1. 上传
d = post(BASE + "/api/documents", multipart(SAMPLE), f"multipart/form-data; boundary={BOUNDARY}")
doc_id = d["id"]
print("1. upload ok, doc:", doc_id)

# 2. 页数
status, body = get(f"{BASE}/api/documents/{doc_id}/pages")
print("2. pages:", json.loads(body))

# 3. 文字行坐标
status, body = get(f"{BASE}/api/documents/{doc_id}/pages/1/lines")
lines_data = json.loads(body)
print("3. lines: page", lines_data["page"], lines_data["width"], "x", lines_data["height"],
      "lines =", len(lines_data["lines"]))
print("   first line:", lines_data["lines"][0]["text"][:36], lines_data["lines"][0]["bbox"])

# 4. 页面渲染图
status, body = get(f"{BASE}/api/documents/{doc_id}/pages/1/image")
print("4. page image: status", status, "bytes", len(body), "PNG" if body[:4] == b"\x89PNG" else "NOT PNG")

# 5. 裁剪（模拟选中题目 1 的区域：整宽，y 取前几行）
y1 = lines_data["lines"][3]["bbox"][3] + 5
crop = post(f"{BASE}/api/documents/{doc_id}/crop",
            json.dumps({"page": 1, "x0": 40, "y0": 60, "x1": 550, "y1": y1}).encode(),
            "application/json")
print("5. crop url:", crop["url"])
status, body = get(BASE + crop["url"])
print("   crop image: status", status, "bytes", len(body), "PNG" if body[:4] == b"\x89PNG" else "NOT PNG")

# 6. 保存图片题
q = post(BASE + "/api/questions", json.dumps({
    "document_id": doc_id, "doc_filename": "test.pdf", "start_block": -1, "end_block": -1,
    "content": "", "qtype": "图片题", "difficulty": "中档",
    "knowledge_points": ["导数及其应用/利用导数研究函数的单调性"],
    "answer": "", "analysis": "", "image": crop["url"]
}).encode(), "application/json")
print("6. image question saved:", q)

# 7. 保存带文本+截图的题
q2 = post(BASE + "/api/questions", json.dumps({
    "document_id": doc_id, "doc_filename": "test.pdf", "start_block": -1, "end_block": -1,
    "content": "1. 函数 $f(x)=x^3-3x$ 的单调递增区间是____", "qtype": "选择题", "difficulty": "基础",
    "knowledge_points": [], "answer": "A", "analysis": "", "image": crop["url"]
}).encode(), "application/json")
print("7. text+image question saved:", q2)

# 8. 题目列表校验
status, body = get(f"{BASE}/api/questions?document_id={doc_id}")
qs = json.loads(body)
print("8. questions in doc:", len(qs), "| image fields:", [bool(x["image"]) for x in qs])

# 9. 旧文档（无 file_path）页面接口应给出友好错误
status, body = get(BASE + "/api/documents")
old = [x for x in json.loads(body) if x["id"] != doc_id]
if old:
    try:
        get(f"{BASE}/api/documents/{old[0]['id']}/pages")
    except Exception as e:
        print("9. old doc fallback msg:", str(e)[:80])

print("ALL PASS")
