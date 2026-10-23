# -*- coding: utf-8 -*-
"""把一份讲义交给题库：**一条命令**跑完录入 → 检查 → 问题处理 → 入库。

给 agent 和脚本用。人在界面上做同样的事要 import 十几次点击。

用法
----
    python auto_import_cli.py 讲义.pdf --curriculum 国内高中数学
    python auto_import_cli.py 讲义.pdf --curriculum cn-senior-math --pages 1-20
    python auto_import_cli.py --json 讲义.pdf --curriculum 3      # 只吐 JSON（agent 用）
    python auto_import_cli.py --status <task_id>                  # 查一个任务
    python auto_import_cli.py --resume <task_id>                  # 老师处理完 → 补入库
    python auto_import_cli.py --list-curricula                    # 有哪些体系可选

退出码（**这就是结果**，agent 直接看它）
----
    0  全部入库，没有遗留
    2  跑完了，但还有 needs_human 需要人看（报告里有直达链接）
    1  失败（服务没起 / 文件不认 / 后台出错）

为什么退出码要区分 0 和 2：
    「跑完了但有 5 条要人看」和「跑完了干净」在自动化里是完全不同的结局 ——
    前者要停下来找人，后者可以继续下一份。混成一个 0，agent 只能去解析 JSON。

⚠️ 这个脚本**不自己算路径/端口**以外的事：所有业务判断都在服务端。
   它只是 HTTP 的薄壳 —— 否则 CLI 和接口会各有一套判据，迟早对不上。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 控制台默认不是 UTF-8，中文会变成乱码 / 直接抛 UnicodeEncodeError。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                    # noqa: BLE001
    pass


def _base() -> str:
    """服务地址。与项目里其它测试脚本同一套判定：

    `SHIKE_BASE` 优先；否则按**检出目录名**分端口（`question-bank-ai` 是 8010 的
    旧副本，其余是 8000）。写死任一个都会在另一份检出上「假通过」。
    """
    env = os.getenv("SHIKE_BASE")
    if env:
        return env.rstrip("/")
    here = Path(__file__).resolve().parent
    root = here.parent
    port = 8010 if "question-bank-ai" in root.name else 8000
    return "http://127.0.0.1:%d" % port


BASE = _base()


def api(path: str, body=None, raw: bytes | None = None,
        headers: dict | None = None, timeout: int = 120):
    url = BASE + path
    hdrs = dict(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:                            # noqa: BLE001
            pass
        raise SystemExit("HTTP %d %s：%s" % (e.code, path, detail or e.reason))
    except urllib.error.URLError as e:
        raise SystemExit("连不上服务（%s）：%s\n先双击「启动拾课.bat」把服务起起来。"
                         % (BASE, e.reason))


def _upload(path: Path, curriculum: str, pages: str | None) -> dict:
    """multipart 上传。手搓而不是引第三方库 —— 这个脚本要能在裸 Python 下跑。"""
    boundary = "----shikeAutoImport%d" % int(time.time() * 1000)
    parts: list[bytes] = []
    for name, value in (("curriculum", curriculum), ("pages", pages or "")):
        if value:
            parts.append(
                ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
                 % (boundary, name, value)).encode("utf-8"))
    parts.append(
        ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
         "Content-Type: application/octet-stream\r\n\r\n"
         % (boundary, path.name)).encode("utf-8"))
    parts.append(path.read_bytes())
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return api("/api/auto/import", raw=b"".join(parts),
               headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})


def _print_report(d: dict, wait_seconds: float) -> None:
    c = d.get("counts") or {}
    print("=" * 66)
    print("讲义：%s" % (d.get("src_filename") or "?"))
    cur = d.get("curriculum") or {}
    print("体系：%s" % (cur.get("name") or "?"))
    print("页数：%s   用时：%.0f 秒" % (
        "%s/%s" % ((d.get("progress") or {}).get("pages_done", 0), d.get("total_pages", 0)),
        wait_seconds))
    print("结果：共 %d 条 → 已入库 %d，待人工 %d，跳过 %d"
          % (c.get("total", 0), c.get("approved", 0),
             c.get("needs_human", 0), c.get("skipped", 0)))

    rep = d.get("report") or {}
    todo = rep.get("needs_human") or []
    if todo:
        print("\n--- 需要你看一眼的 %d 条 ---" % len(todo))
        for r in todo:
            print("  第 %-4s 页  第 %-4s 条  %s" % (
                r.get("page_no"), r.get("seq"), r.get("message") or r.get("reason")))
        print("\n处理完再执行：python auto_import_cli.py --resume %s" % d.get("id"))
        if todo:
            print("打开审核页：%s%s" % (BASE, todo[0].get("open") or "/batch.html"))

    skipped = rep.get("skipped") or []
    if skipped:
        from collections import Counter
        cnt = Counter(r.get("reason") for r in skipped)
        print("\n跳过的 %d 条（模型这页确实没读出题）：%s"
              % (len(skipped), "、".join("%s×%d" % (k, v) for k, v in cnt.items())))

    if d.get("error"):
        print("\n错误：%s" % d["error"])
    print("=" * 66)


def _wait(task_id: str, interval: float, timeout: float, quiet: bool) -> dict:
    t0 = time.time()
    last = ""
    while True:
        d = api("/api/auto/imports/%s" % task_id)
        if not quiet:
            line = "%s / %s" % (d.get("stage") or d.get("status"),
                                (d.get("progress") or {}).get("pages_done"))
            if line != last:
                print("  … %s（%d/%d 页）" % (line, (d.get("progress") or {}).get("pages_done", 0),
                                             d.get("total_pages", 0)),
                      file=sys.stderr)
                last = line
        if d.get("done"):
            return d
        if time.time() - t0 > timeout:
            raise SystemExit("等太久了（%.0f 秒），任务还在跑。用 --status %s 稍后再看。"
                             % (timeout, task_id))
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把一份讲义交给题库：录入 → 检查 → 问题处理 → 入库（一条命令）")
    ap.add_argument("file", nargs="?", help="讲义文件（pdf / docx / 图片）")
    ap.add_argument("--curriculum", "-c", default=None,
                    help="体系：id / code / 名称，任一；不给会列出可选项")
    ap.add_argument("--pages", default=None,
                    help="页范围，如 1-30,35；不传 = 全部")
    ap.add_argument("--json", action="store_true", help="只输出 JSON（给 agent / 管道用）")
    ap.add_argument("--quiet", "-q", action="store_true", help="不打进度")
    ap.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒，默认 5")
    ap.add_argument("--timeout", type=float, default=7200.0, help="最多等多少秒，默认 2 小时")
    ap.add_argument("--status", metavar="TASK_ID", help="查一个已有任务")
    ap.add_argument("--resume", metavar="TASK_ID", help="人工处理完之后补入库")
    ap.add_argument("--list-curricula", action="store_true", help="列出可选体系")
    args = ap.parse_args()

    if args.list_curricula:
        d = api("/api/auto/curricula")
        for c in d.get("curricula") or []:
            print("%-3s %-18s %s" % (c["id"], c["code"], c["name"]))
        return 0

    if args.status:
        d = api("/api/auto/imports/%s" % args.status)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0 if d.get("ok") else (2 if d.get("done") else 0)

    if args.resume:
        d = api("/api/auto/imports/%s/resume" % args.resume, body={})
        print(json.dumps(d, ensure_ascii=False, indent=2) if args.json else "")
        if not args.json:
            _print_report(d, 0)
        return 0 if (d.get("counts") or {}).get("needs_human", 0) == 0 else 2

    if not args.file:
        ap.print_help()
        return 1

    path = Path(args.file).expanduser()
    if not path.is_file():
        print("找不到文件：%s" % path, file=sys.stderr)
        return 1

    t0 = time.time()
    try:
        d = _upload(path, args.curriculum or "", args.pages)
    except SystemExit as e:
        # 体系没给 / 认不出时，服务端的 422 里已经带了可选项，原样透出即可
        print(str(e), file=sys.stderr)
        return 1

    if not args.quiet and not args.json:
        print("已提交：%s（%s 页）" % (d.get("id"), d.get("total_pages")), file=sys.stderr)

    d = _wait(d["id"], args.interval, args.timeout, args.quiet or args.json)
    elapsed = time.time() - t0

    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        _print_report(d, elapsed)

    if d.get("status") == "failed":
        return 1
    return 0 if (d.get("counts") or {}).get("needs_human", 0) == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
