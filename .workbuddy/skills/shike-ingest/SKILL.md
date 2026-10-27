---
name: shike-ingest
description: 把一整份讲义/试卷/练习册文件（PDF、.docx、扫描件、拍照页）自动录进「拾课」题库：逐页识别 → 自动修配图 → 零疑点入库 → 给人一份「哪几条要你看」的报告，并给出审核链接与收尾命令。当用户说「把这份讲义录进题库」「这个 PDF 入库」「把卷子导进去」「扫描的练习册加进题库」「这套题帮我录了」「传份卷子进去，入完告诉我」「import this paper / add this handout to the question bank」「OCR 进题库」时使用。也用于查进度（`--status`）和老师改完之后「补入库」（`--resume`）。不用于：只查某道题在不在库里、只想看文件写了什么、要按条件组一份新卷子。
---

# shike-ingest · 讲义 / 试卷自动入库

把一份文件变成题库里的题。**一条命令跑完**：识别 → 检查配图 → 自动修 → 零疑点入库 → 报告。

---

## 1. 什么时候用我

- 「把这份讲义录进题库」`D:\讲义\第三章导数.pdf`
- 「这个 PDF 入库，体系选国内高中数学」
- 「扫描的练习册能加进题库吗？就这本」（给的是拍照/扫描 PDF）
- 「这套卷子帮我录了，只做前 20 页」
- 「上次那份入完了没？」（→ 走本文的 `--status`）
- 「我把图框好了，接着入库吧」（→ 走本文的 `--resume`）

## 2. 什么时候别用我

| 用户其实想要 | 该去哪 |
|---|---|
| 只想知道某道题在不在库里 | 直接查库：`GET /api/questions/search?keyword=…`（见 `shike-paper` 第 3 步，只查不导出） |
| 只想看这份文件写了什么 / 转成 markdown | `shike-doc` |
| 按条件组一份新卷子 / 出讲义 | `shike-paper` |
| 给已有的题补答案 | `POST /api/questions/answer-backfill`（不是本 skill） |

**判据**：用户要的是「题进题库、以后能出卷」→ 我；只是「内容给我看看」→ `shike-doc`。

## 3. 输入

| 项 | 必填 | 说明 |
|---|---|---|
| 文件路径 | ✅ | 本机绝对路径。**用户只给了个文件名/说"桌面上那个"→ 问清完整路径，不要猜** |
| 体系 curriculum | ✅ | id / code / 名称任一。**用户没指定 → 必须问，绝不自动挑**（见下） |
| 页范围 pages | ⭕ | 不传 = 全部。写法 `1-30,35`、`all`、`[1,2,3]`。中文逗号也认 |
| 体系不认识时 | — | 先跑 `--list-curricula`，把选项念给用户挑 |

扩展名白名单：`.pdf` `.docx` `.png` `.jpg` `.jpeg` `.webp` `.bmp`。
`.doc` / `.ppt` **不支持**（先在 Word 里另存为 `.docx`）。

> ⚠️ **体系绝不许猜。** 猜错的后果不是报错，是整批知识点挂到错误的树上，
> 界面一切正常，只在期末出卷时才发现「怎么一道题都对不上」。问一句的成本是零。

## 4. 调用步骤

### 第 0 步：探服务（别只按目录名猜端口）

端口有两套规则并存：`启动拾课.bat` 用 `SHIKE_PORT`（默认 8000），而 CLI / 测试脚本按**检出目录名**
猜（含 `question-bank-ai` → 8010）。两者可能不一致 —— 所以**以探测结果为准**：

```bash
cd "<仓库>/backend"
.venv/Scripts/python.exe -c "
import os, urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 必须绕开代理，否则 127.0.0.1 会被送进代理返 502
env = os.getenv('SHIKE_BASE')
for b in ([env] if env else ['http://127.0.0.1:8000', 'http://127.0.0.1:8010']):
    try:
        with op.open(b + '/api/auto/curricula', timeout=2) as r:
            print('OK', b); break
    except Exception as e:
        print('NO', b, type(e).__name__)
"
```

都没通 → **别自己起服务**（agent 起的进程活不长，跑到第 30 页就断），直接告诉用户：
> 拾课服务没起。请双击项目根目录的「启动拾课.bat」，起来后我再继续。

拿到 `OK` 的地址后，把它固定进 `SHIKE_BASE`，后面所有命令都带它。

### 第 1 步：看体系（必做，除非用户已明确说了体系）

```bash
# ⚠️ 调用前清空代理变量：本机 http_proxy 会让 CLI 报 HTTP 502（看着像服务坏了）
# bash:
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
# cmd:  set http_proxy=  &  set https_proxy=

export SHIKE_BASE=http://127.0.0.1:8000        # 换成探测到的地址
.venv/Scripts/python.exe auto_import_cli.py --list-curricula
```

真实输出（目前库里四套）：
```
1   dse-math          DSE 数学
2   alevel-math       A-Level 数学
3   cn-senior-math    国内高中数学
4   cn-junior-math    国内初中数学
```
把这四个**原样念给用户**，让他挑。用户说「高中数学」不要自动映射成 3 —— 问一句「是『国内高中数学』吗」。

### 第 2 步：起任务 + 等到跑完

```bash
.venv/Scripts/python.exe auto_import_cli.py "<文件绝对路径>" \
    --curriculum <用户选的 id/code/名称> \
    [--pages 1-30] \
    --interval 5 --timeout 7200 --quiet
echo "EXIT=$?"
```

- **超时必须给足**：一页 = 一次视觉模型请求，44 页实测约 30 分钟。`--timeout` 至少 3600，默认 7200 别调小。
- 轮询间隔 5 秒足够（CLI 默认就是 5）。自己写 HTTP 轮询的话同样 5 秒。
- 阶段会依次是：`排队中` → `正在识别第 1/N 批（共 X 页）` → `正在检查识别结果、自动修配图` → `正在入库（N 条零疑点）` → `完成`。
- ⚠️ 一批**默认 30 页**（`SHIKE_PAGE_MAX_PAGES` 可覆盖），44 页会**自动拆两批串行**，不用自己分批。页级并发**默认 3、上限 8**（`SHIKE_BATCH_WORKERS`）。

纯 HTTP 等价写法（不方便用 CLI 时）：

```bash
curl -X POST "$SHIKE_BASE/api/auto/import-path" -H "Content-Type: application/json" \
  -d '{"path":"D:/讲义/第三章.pdf","curriculum":"cn-senior-math","pages":"1-30"}'
# → {"id":"83b5d9425968","status":"queued","done":false,"total_pages":30, ...}
curl "$SHIKE_BASE/api/auto/imports/83b5d9425968"     # 轮询到 done=true
```

⚠️ **提交用单数、查状态用复数 —— 两个路径不对称，是历史遗留，写成一个必错：**

| 干什么 | 路径 | 单复数 |
|---|---|---|
| 传文件（multipart） | `POST /api/auto/import` | **单数** |
| 传本机路径（JSON） | `POST /api/auto/import-path` | **单数** + 连字符 |
| 查状态 / 轮询 | `GET /api/auto/imports/<id>` | **复数** |
| 补入库 | `POST /api/auto/imports/<id>/resume` | **复数** |

`auto/import` 这个词根最容易让人把两处写成同一个。四条里只有前两条是单数。

### 第 3 步：判结果（退出码四档 + 码 1 的话术分流）

**看到退出码 0，先看 `done`：`done=false` 是"还在跑"，`done=true` 才是"全部入库"。**
弄反了就会告诉老师"好了"，而任务其实还在识别。

**表一 · 退出码怎么判**（严格按序、命中即停；字段全部取 `GET /api/auto/imports/{id}` 的**顶层**）

| 序 | 判据 | 码 |
|---|---|---|
| 1 | `done == false`（queued / recognizing / fixing / approving） | 0（**不是完成**） |
| 2 | `status == "failed"` | 1 |
| 3 | `counts.approved == 0` 且 `counts.needs_human == 0` | 1 |
| 4 | `report.failed_pages` 非空 **或** `sum(jobs[].failed) > 0` | 3 |
| 5 | `counts.needs_human > 0` | 2 |
| 6 | 其余 | 0 |

> 码 0 有两个来源：`done=false`（还在跑）与 `done=true`（全部入库）。
> `done` 的定义在 `routers/auto.py:131`：`status in ("done","partial","failed")`。
> CLI 主流程会等到 done 才返回，所以只有 `--status` 会给你前一种。

**四档的含义与「下一步该干什么」**：

| 码 | 含义 | agent 下一步 |
|---|---|---|
| 0 | `done=true`：全部入库，无遗留 / `done=false`：还在跑 | 前者可以继续下一份；后者继续等，**别重起任务**（重跑要烧几十次 AI 调用） |
| 2 | **成果是全的**，只是有几条配图/内容拿不准要人看一眼 | 停下来找人，念 `needs_human` |
| 3 | **成果是缺的**：有页没识别成，那部分内容不在题库里 | 停下来，说清哪一批少了几页；要补只能重跑或手工补录 |
| 1 | 一条都没成 | 停下，按表二选话术 |

> 3 和 2 不能合并：2 是「点一下就能收进来」，3 是「这些题根本不存在」。
> 两者同时存在时报 **3**（缺内容比待确认更严重）。

**表二 · 仅当表一得 1 时，选哪句话**（不参与退出码判定，严格按序命中即停）

| 序 | 判据 | 说什么 |
|---|---|---|
| 1a | `status == "failed"` | 「整批识别失败，一条都没跑成」+ 顶层 `error` + 建议查 AI 配置/日志 |
| 1b | `failed_pages` 非空 **或** `sum(jobs[].failed) > 0` | 「第 X 批 30 页里成功 28、失败 2，这 2 页的题没进库」+ 该批 `error`（**空串就只念页数，别说「错误信息是：」**），**再补一句「而且跑成的那 28 页也没拆出题，最终一条都没入库」** |
| 1c | 其余 且 `counts.skipped > 0` | 「读出来了，但都没能拆成题，需要看一眼原卷」 |
| 1d | 其余 且 `counts.total == 0` | 「这份文件里没识别出题目 —— 是不是传错文件了？」 |
| 1e | 以上都不中 | 「没有条目入库」+ 念 `counts` |

⚠️ **码是 1 的时候，任何一句都不许说「其余的题已经入库了」。** 1 的判据就是
`status == "failed"` 或 `approved == 0` —— 也就是说**成果必然是 0 条**。1b 最容易踩：
它同时带着「28 页成功」这种听起来有收获的数字，但成功的页也没拆出题。

**字段来源（写死，别猜）**

- `total / approved / needs_human / skipped` 一律取**顶层 `counts`**，不要取 `report.counts` ——
  任务在写出报告之前就失败时（就是 1a），`report` 是 **`null`**，读它会崩。
- `failed_pages` 只有 `report` 里有，按 `report?.failed_pages ?? []` 读。
- 1a 念的 `error` 在**顶层**；1b 念的是 `failed_pages[].error`（批次级）。**两者不是同一个字段。**
- `jobs[].error` 与 `failed_pages[].error` 通常是同一句话（后者就是前者截断的），**只念一次**，
  否则用户会以为出了两个问题。
- **页号拿不到**：`failed_pages` 里只有 `job_id`、三个计数（`done` / `failed` / `pages_total`）和 `error`，
  没有页码。所以只能说「第 N 批少了 Y 页」，
  **不许编「第 12、13 页」**（要精确到页，得让用户去审核页自己看）。

### 第 4 步（老师处理完之后）：补入库

```bash
.venv/Scripts/python.exe auto_import_cli.py --resume <task_id>
```

`resume` **不重新识别**（不重复烧 AI 调用），只重扫条目、把现在能过的入库，可重复调（已入库的不会重复）。
接口层是 `POST /api/auto/imports/{id}/resume`；任务还在跑时返回 **409**。

两次判定的关系：

- `resume` 会**重新判定并重写报告**（含 `failed_pages`），所以 resume 之后要按第 3 步的**表一
  再判一次**，不能沿用旧结论。
- 若 `resume` 之后 `status=done` 但 `jobs[].failed > 0`，说明服务还是旧版本（这句自检留着）——
  重启服务后再看，别把这个组合当成"干净跑完"。

## 5. 输出（给用户看什么）

先一句话结论，再一张表，再逐条念待人工项：

```
《第三章导数.pdf》44 页，体系「国内高中数学」，用时 31 分钟。
共 87 条 → 入库 79，需你看 6 条，跳过 2 条。任务号 83b5d9425968
（⚠️ 第 2 批 14 页里成功 12、失败 2 —— 这 2 页的题没进库，原因：识别超时）
```

| 页 | 条 | 为什么 | 怎么处理 |
|---|---|---|---|
| 12 | 4 | 这页有 14 处图形，判不出哪一处是本题的 | 打开 /batch.html?job=034f4e492489 框一下 |

字段含义（**`counts` 顶层与 `report` 里都有，取顶层**；`progress` / `jobs` 也取顶层；`needs_human` / `reasons` / `approved_question_ids` / `failed_pages` 只在 `report` 里，`report` 可能是 `null`）：

| 字段 | 含义 |
|---|---|
| `counts.total` | 识别出的条目总数 |
| `counts.approved` | 已入库（零疑点，直接成为题库里的题） |
| `counts.needs_human` | 要人看一眼才能入库 |
| `counts.skipped` | 跳过（模型这页确实没读出题 / 空白页）—— **条目级，内容没丢**；页级的内容缺失看 `failed_pages` |
| `reasons` | 按原因计数，如 `{"figure_ambiguous":4,"no_content":2}`。**只统计 `human`/`repair` 两种判定，`skip` 不计入** —— 想回答「为什么这些条要人看」时念它，不用逐条数 `needs_human` |
| `progress.pages_done` / `total_pages` | 已识别页数 / 总页数。`pages_done < total_pages` 就是有页没成 |
| `jobs[].failed` | 该批**失败页数**（页数口径，不是条目数）—— 判码 3 时与 `failed_pages` **取或**（它是原始计数，不依赖服务端有没有写报告） |
| `needs_human[].page_no` / `.seq` | 第几页、该页第几条 |
| `needs_human[].message` | 中文原因（原样念给用户，不要翻译成技术词） |
| `needs_human[].open` | 人能点开的审核页（拼上 `$SHIKE_BASE` 就是完整链接） |
| `needs_human[].fix_with` / `.candidates` | agent 能调的下一步（重裁配图） |
| `approved_question_ids` | 入库的题目 id，出卷/挂作业可直接引用；出错也能精确回滚 |
| `failed_pages[]` | 有页失败的批次，**五个字段**：`job_id`、`done`（该批成功页数）、`failed`（该批失败页数）、`pages_total`（该批总页数）、`error`（该批错误，可能为空串）。**没有页号** |
| `pages_lost` | **一共缺了多少页**（= Σ(`pages_total` − `done`)，页级，int）。`next` 和 CLI 摘要都读它。**不要自己按 `failed` 求和** —— 整批没跑成那批的 `failed` 是 0，求和会把页数报小。**`0` 是合法值**：判「有没有这个键」要用 `in` / `is None`，别写 `if report.get("pages_lost")`。老任务的老报告里可能没有这个键 |

**报「缺了几页」只有一条路：读 `pages_lost`**

- `0` 是**合法值**（真的没缺页），不等于「报告里没有这个字段」。判「有没有这个键」要看它在不在，
  别看它真不真 —— 用 `x or 兜底` 的写法会把「真的 0 页」当成缺键。
- 码 3 但拿不到这个值（`report` 是 `null`，或旧任务的老报告里没这个键）→ **就别报页数**，
  说「有页没识别成，具体页数看 `jobs[].failed`」。**绝不要拿 0 兜底** ——
  一句「缺 0 页」比不说更糟，老师会以为虚惊一场。
  这种情况是**结构上正常**的，不是数据坏了：判码 3 有两条独立来源（`failed_pages` 非空
  **或** `jobs[].failed > 0`），而 `pages_lost` 只从 `failed_pages` 算出来 ——
  所以「码 3 却没有 `pages_lost`」必然出现在「报告还没写（`report` 为 `null`）」这一支。

**`failed_pages` 和 `skipped` 不是一回事，别混着说**

- `skipped` 是**条目级**：模型确实没从这页读出题、或这页本身就是空白页 → **内容没丢**，
  这一类本来就不该入库。
- `failed_pages` 是**页级**：这页根本没进结果 → **内容缺了**，多等一会儿也不会自己补上。

所以「跳过 5 条」不是「少了 5 页」，反过来也一样。对用户说错这个，
他要么白等一轮重跑，要么以为题都在库里。

**要逐批说清「哪批出了什么问题」，读 `failed_pages` 数组，不要从 `next` 那句话里拆**

`next` 是一句给人看的摘要。逐批判断用这两条：

- `error` 非空 **且** `done == 0` → 这批**整批没跑成**（一条都没出）
- `failed > 0` **且** `done > 0` → 这批**部分页挂了**（其余的跑成了）

两者可以同时出现在不同批次里，`next` 会把它们合起来念页数 —— 别自己去拆优先级，
需要细说时直接列批次。

`report.next` 与退出码**同源生成**，正常情况下直接念即可。
万一两者矛盾（例如 `next` 说"全部处理完"但退出码是 3）：**以退出码为准**，
并把不一致如实告诉用户（「报告里写的和退出码对不上，我按退出码说」）。
不要两边都信，也不要因为怕矛盾就干脆不念 —— 那句话对老师最有用。

## 6. 失败怎么报

| 现象 | 该说的话 | 下一步 |
|---|---|---|
| 端口探测全不通 | 「拾课服务没起。请双击「启动拾课.bat」，起来后我再继续。」 | 不起服务，等用户 |
| CLI 报 `HTTP 502 …：Bad Gateway` | 「本机代理把 127.0.0.1 拦了，不是服务坏了。」 | 清空 `http_proxy/https_proxy` 重跑 |
| CLI 报 `连不上服务（…）：[WinError 10061]` | 「端口上确实没有服务在跑。」 | 请用户启动 |
| `422` 且 detail 含「请指定体系」/「认不出体系」 | 把 detail 里的可选项原样念给用户 | 让用户挑，**不猜** |
| `422`「找不到文件」 | 「路径不对，帮我确认下完整路径」 | 要路径 |
| `422`「这份文档生成不出页面图」 | 「这份文件打不开页面图（可能损坏/是纯文本/受密码保护）」 | 建议另存为 PDF 再试 |
| `422`「页号超出范围：[9,10]（共 1 页）」 | 「这份只有 1 页，1-30 超范围了」 | 改 `--pages` 或去掉 |
| `422` 且 detail 含「页范围写法认不出」 | 「页范围写法不支持，得写成 `1-30,35`。」 | 改写法重跑（参数问题，不是服务故障） |
| `422`「仅支持 PDF / Word(.docx) / 图片」 | 「这个格式不支持，.doc 请在 Word 里另存为 .docx」 | 让用户转格式 |
| 退出码 `0` 且 `done=false` | 「还在跑，没失败。当前第 N/M 页。」 | 继续等或用 `--status`；**别重起任务**（重跑要烧几十次 AI 调用） |
| 退出码 `1` | 按第 3 步**表二**挑话术（1a/1b/1c/1d/1e 是五种不同的处境，别一律说"失败"） | 见对应分支 |
| 退出码 `3` | 「跑完了，但一共缺了 `pages_lost` 页 —— 这部分题没进库。」 | 让用户查 AI 配置/网络后再跑那几页，或手工补录 |
| 跑到一半 CLI 超时退出 | 「任务还在后台跑，没失败。用 `--status <id>` 或我稍后再看。」 | `--status`，**别重新起任务**（会重复烧调用） |

## 7. 边界与陷阱

**不做**：不改体系、不替用户框图（`needs_human` 里的图只能人框或 agent 调 `fix_with`）、不删题、不入库「拿不准」的题。

**已知坑（都实测过）**

1. **「有页失败」看两处，且要取或：`report.failed_pages` 非空 或 `sum(jobs[].failed) > 0`。**
   现在这两处都会带上：30 页一批里 2 页失败、28 页成功时，报告里 `failed_pages[]` 会有一条
   `{job_id, done:28, failed:2, pages_total:30, error:""}`，任务状态是 `partial`（不是 `done`，
   `auto_import.py:424` 的 `"partial" if failed else "done"`），退出码 3。
   留着 `jobs[].failed` 这条兜底是因为它是**原始计数**，不依赖服务端有没有写报告；
   万一只看到 `jobs[].failed > 0` 而 `failed_pages` 是空的，说明服务跑在旧版本（见第 12 条），
   **仍按 3 报**，并把「报告里没列出来」如实告诉用户 —— 别当成「全成」。
2. **`report.failed_pages` 没有页码**，只有 `job_id` + `done` / `failed` / `pages_total` + `error`。
   所以说「第 N 批少了 Y 页」，
   **不许说「第 12、13 页」** —— 页号得让用户去审核页自己看，编出来比不说更糟。
3. **`resume` 会重写报告**，所以 `failed_pages` 只反映最近一次判定。担心被冲掉就 resume 前抄一份；
   resume 之后按表一**重判**，不要沿用旧结论。
4. **`report.next` 与退出码同源**：不矛盾就直接念，矛盾以退出码为准并说明。
5. **`http_proxy` 会让 127.0.0.1 请求返 502**，看起来完全像服务挂了。CLI 自己没绕代理，
   所以调用前必须清空代理变量；自己写 urllib 必须 `build_opener(ProxyHandler({}))`。
6. **体系错不报错**：挂错树只在出卷时露出来。
7. **短超时 = 假失败。** 44 页约 30 分钟。CLI 超时退出时任务**仍在后台跑**，重起任务会重复烧调用。
8. **文件会被复制进 `data/uploads/`**（原文件不动，改名成 `uuid.ext`）。所以之后删掉桌面上的原文件不影响题库。
9. **`.docx` 提交时会慢几秒**：要先用本机 Word 导出 PDF 才能出页面图，这是预检，不是卡住。
10. **别用管道取退出码**（`| tail` 取到的是 tail 的码）。要 `>log 2>&1; echo $?`。
11. 想清空重来不要在真库上试 —— 要试就用临时库（`SHIKE_DATA_DIR` / `SHIKE_DB_PATH` 指向空目录再起服务）。
12. **看到 `status=partial` 但退出码是 0**，说明服务还跑在旧版本（码 3 那次修复没生效）→
    重启服务再跑，别把这个组合当成「正常」。
