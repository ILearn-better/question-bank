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

退出码（**这就是结果**，agent 直接看它；判据只在 `exit_code_of()` 一处定义）
----
    0  全部入库，没有遗留
    2  跑完了，成果是**全的**，但还有 needs_human 需要人看（报告里有直达链接）
    3  **有页没识别成**：内容缺了，得决定重跑还是手工补（看报告 failed_pages）
    1  失败（服务没起 / 文件不认 / 页范围认不出 / 后台整批挂了 /
       **跑完了但一条都没出来**）

为什么 0、2、3 必须分开：
    「跑完了干净」「成果全但有几条要人点头」「内容本身就缺了」在自动化里是
    三种不同的结局，对应的下一步动作完全不同（继续下一份 / 停下来找人 /
    先决定重跑）。混成一个 0，agent 只能去解析 JSON。

    2 和 3 **尤其**不能合并。走 2 的批次，`needs_human` 列表里是有东西可点的；
    走 3 的批次，那份列表**通常是空的**（页挂了，条目压根没生成）。合进 2 的话
    agent 会照着 2 的语义去读 `needs_human`，读到空，然后回过头告诉用户
    「没有待处理的，已完成」—— 那只是把静默失败从 CLI 搬进了 agent 嘴里。
    新增一个码的代价，远低于一句说错的结论。

为什么 3 优先于 2：两者同时成立时（有页挂了、也有条目待审），页失败更严重
    ——「内容缺了」比「内容全但要人点头」更该被先看见。优先级 1 > 3 > 2 > 0。

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


def exit_code_of(task: dict | None) -> int:
    """任务字典 → 退出码。**唯一的判定处**（上传 / --status / --resume 三处共用）。

    入参故意收**整个任务字典**而不是 `report`：`report` 可能是 `null`
    （任务刚建、还没跑到写报告那一步，`t.report` 是空串），
    而 `counts` / `status` / `done` 在顶层一直有。

    四档判据（顺序不能换）：
      1  `status == "failed"` —— 后台整体挂了（错误在 `error` 字段）
      1  `approved == 0` 且 `needs_human == 0` —— **跑完了但一条都没出来**
      3  有页没识别成 —— `report.failed_pages` 非空，或任一 `jobs[].failed > 0`
         （两者取或：报告缺失时还有 jobs 兜底，`failed` 是**页数**）
      2  `counts.needs_human > 0` —— 成果是全的，但要人点头
      0  其余

    「一条都没出来」为什么必须是 1、且必须判在 3 前面：
      30 页全识别成功（`done=30, failed=0`），但模型把 30 页全判成 `no_content`
      （扫描件糊了 / 本来就是空白页）—— `_failed_pages()` 是空的（页确实跑完了），
      所以那道闸门抓不住它。此时 `approved=0, needs_human=0, skipped=30`，
      没有这一档就会落到最后的 `return 0`，对 agent 说「全部入库，没有遗留」，
      而真相是一条都没入库。
      而 `approved=0` 且**有**页失败时也不能报 3：3 的语义是「成果是全的、
      只差这几页」，`approved=0` 根本没有成果，agent 会照着去「补那几页」，
      方向就错了 —— 该说「这活没成」。

    `done == false`（还在跑）返回 0：既不是失败也不是完成，别误报成 1。
    注意 `approved == 0` 且 `needs_human == 3` 是**码 2，不是 1** ——
    那是「跑完了，三条要人看」，不是「跑失败了」。
    """
    if not task:
        return 1
    if task.get("status") == "failed":
        return 1
    if not task.get("done"):
        return 0

    counts = task.get("counts") or {}
    approved = int(counts.get("approved") or 0)
    needs_human = int(counts.get("needs_human") or 0)
    # 「一条都没出来」必须先于码 3 判，理由见 docstring。
    # 判据是 approved == 0 **且** needs_human == 0：只要有东西可点（needs_human>0），
    # 那就是「跑完了、有人要看的」，还是 2。
    if approved == 0 and needs_human == 0:
        return 1

    report = task.get("report") or {}
    failed_pages = report.get("failed_pages") or []
    jobs_failed = sum(int(j.get("failed") or 0) for j in (task.get("jobs") or []))
    if failed_pages or jobs_failed > 0:
        return 3

    if needs_human > 0:
        return 2
    return 0


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
    # 页失败必须印出来：退出码 3 只有脚本看得见，人看的是这几行。
    # 不印的话「44 页里 2 页没识别成」在人眼里跟「全部完成」一模一样。
    lost_pages = rep.get("failed_pages") or []
    if lost_pages:
        # 页数只读报告里的 `pages_lost`（由 build_report 算好），这里不再重算 ——
        # 同一件事两个出处，迟早一个改了另一个没改。
        lost = rep.get("pages_lost")
        if lost is None:
            # 旧任务（本轮之前的报告）没有这个键。回退到 Σ failed 只能数到
            # 「页跑过但挂了」的那部分，整批没跑成的批贡献 0 —— 印出来是个
            # **已知偏小**的数。宁可说「页数不详」，也不要把少报的数当事实说；
            # 每行下面还有「完成 X 页 / 共 Y 页」，需要细节的人能自己看。
            print("\n--- 有 %d 批没识别成（旧任务，页数不详）---" % len(lost_pages))
        else:
            print("\n--- 有 %d 批没识别成（共 %d 页）---" % (len(lost_pages), lost))
        for f in lost_pages:
            print("  批次 %s  完成 %s 页 / 失败 %s 页 / 共 %s 页  %s" % (
                f.get("job_id"), f.get("done"), f.get("failed"),
                f.get("pages_total"), f.get("error") or ""))
        print("  这些页的题**不会**出现在结果里：要补齐只能把这几个页范围"
              "重新导入一次（或手工补录）。--resume 只补入库，不会重新识别。")

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
        # status 已经不是 failed 时，这条 error 是**之前**那轮的残留（resume 不清它）。
        # 必须标出来，否则「错误：整批识别都失败了」会被读成本轮失败 —— 而本轮
        # 刚刚跑完，只是前面那次挂过。
        stale = d.get("status") != "failed"
        print("\n%s：%s" % ("之前的错误（本轮已重跑）" if stale else "错误", d["error"]))
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
        return exit_code_of(d)

    if args.resume:
        d = api("/api/auto/imports/%s/resume" % args.resume, body={})
        print(json.dumps(d, ensure_ascii=False, indent=2) if args.json else "")
        if not args.json:
            _print_report(d, 0)
        return exit_code_of(d)

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

    return exit_code_of(d)


if __name__ == "__main__":
    sys.exit(main())
