# AI 批改作业 · 实现计划（2026-10-17 调研稿，未动工）

> 本文是三份并行调研（开源方案 / 技术可行性 / 本仓接入）汇总后的落地方案。
> **先看第 1 节的边界，再看第 10 节要你拍板的几件事**，中间是实现细节。

---

## 0. 一句话结论

**GitHub 上没有能直接拿来用的项目。** 这个需求是「手写数学的换行级识别 + 按评分细则判分 + 在原件上做多种批注」三件事的组合，
开源界只有各自方向的零件，没有拼好的整机；最像整机的三个（AIExamGrader / quiz-ai / wrong-notebook）
**要么是非开源许可（禁修改禁分发）、要么根本没有 LICENSE**，法律上不能抄。

所以路线是：**自己写编排层，只调 API**；借三个设计（不抄代码），专用 OCR 只当"局部放大镜"；
**标注不烧进图片/PDF，存坐标、前端叠层**。

---

## 1. 边界：这和既有「AI 不许给作业打分」不冲突

`docs/学生作业上传与评分方案.md` §6 把「AI 不许给作业打分」写成了硬约束，理由是
「凭笔迹/涂改推断粗心是伪科学，一旦被当真就是误导家长」。那条约束**依然有效**，这次不推翻它 ——
因为它接着写了一个出口：

> 若以后要做「批改建议」，必须是**显式触发**、且界面写清「仅供参考、不自动写库」。

这次的批改**正好走这个出口**，并且两类东西必须严格分开：

| | 谁产出 | 存哪 | 是否可核对 |
|---|---|---|---|
| **5 维主观分**（完成度 / 书写 / 独立思考…） | **只有老师** | `homework_scores`（不动） | 主观，无法核对 |
| **逐题对错 / 得分 / 扣分理由** | AI 出初稿，**老师确认后生效** | 新表 `homework_grade_items` | **客观**，有标准答案或评分细则可核对 |

一句话：**AI 判"这道题对不对"不等于"给这个孩子打 3 分"。** 前者有标准答案兜底，后者才是那条约束禁止的。
实现上再加三道闸门：
1. 每次批改都是老师**显式点击**触发，不存在后台偷偷批；
2. AI 结果落库时 `status='pending'`，界面全程标「AI 初稿」；
3. 老师点「确认」之前，任何统计口径（掌握度、雷达图、家长稿）**都不读批改结果**。

---

## 2. 调研结论：开源方案能用到什么程度

> star / license / 更新时间由 GitHub API 现查（2026-10-17），非记忆。未核实项已标注。

### 2.1 候选（只留有参考价值的）

| 仓库 | license | 能做什么 | 致命短板 | 怎么用 |
|---|---|---|---|---|
| `FIELA/AIExamGrader` | ⚠️ **非开源**（禁修改版分发） | 整卷手写照片→GPT-4o 批改、rubric 文本驱动、**三步人工复核** | 不能改不能集成；**不在原图上批注**，只出报告 | **只看交互流程，一个字都别抄** |
| `yves-chevallier/quiz-ai` | ⚠️ **无 LICENSE** | YAML 题库→LaTeX 排版→扫描整卷→结构化观察→**二次调用按 rubric 打分**→带批注 PDF | 无许可不能抄；强绑 Anthropic | **只借架构**：两段式 + anchor 坐标复用 |
| `sevenweeks/vision-anchored-autograder` | MIT | Vision 出词框→LLM 判定→**圆圈/对勾叠回原图**+评语面板，支持离线重放 | 只做英文阅读题，非数学 | **借这段数据流**（本方案标注层就是它） |
| `ChantillyAn/homework-grader` | MIT | rubric YAML（criteria/weight/anchors/evidence_type）+ 偏见控制 | 是脚本不是系统，不做批注 | **借 rubric schema** |
| `opendatalab/UniMERNet` | Apache-2.0 | 公式图→LaTeX，含手写子集；CROHME2019 精确匹配 65.4% | 只认**孤立公式图**，不含检测，2024 后基本停更 | 复核界面的「重识别这个公式」按钮 |
| `breezedeus/Pix2Text` | MIT（代码） | 版面+公式检测+识别全链路 | `requirements` 硬钉 `pymupdf==1.24.1`（**AGPL**） | 值得试，但必须剥掉 PDF 分支 |
| `datalab-to/surya` | 代码 Apache-2.0 / **权重 OpenRAIL-M** | 整页 OCR，公式直接出 KaTeX | 权重限研究/个人/低营收；无手写作业验证 | 拍歪了读不清时的兜底 |

**别碰**：`PDF-Extract-Kit`（AGPL + PyMuPDF 双雷）、`OCRAutoScore`（AGPL + YOLOv8 AGPL）、
`texify`（GPL-3.0 且已归档）、`nougat`（权重 CC-BY-NC）、以及所有**无 LICENSE** 的仓库。

### 2.2 三个直接可用的结论

1. **业界当前做法是"VLM 直读整页 + prompt 打分"，不是"本地 OCR 管线"。** 对一份 3 页手写数学卷，
   OCR→公式→再判的错误会级联，VLM 直接看图反而更稳。这也省掉一整套依赖。
2. **标注别走"服务端烧进图/PDF"。** 你踩过 PyMuPDF 的 AGPL 坑，这是同一个坑的第二次。
   正确形态：库里存归一化坐标，前端叠层绘制 —— 老师拖动改批注就是改一条 JSON。
3. **不换 PDF 库。** 本仓已经把 PyMuPDF 隔离在 `backend/app/adapters/pdf.py` 一个文件里（运行时不分发），
   换成 pypdfium2 是独立重构，与本功能无关，**这期不做**，别混进来。

---

## 3. 技术路线（四条难点怎么落）

### 3.1 手写识别：分层 + 带不确定性，不追求"准"

**先分层，别一锅端。** 20 题里选择/填空通常占一半以上，这两类**不需要读过程**：
选择题只检测勾选/圈选字母（分类任务）；填空题转 LaTeX 后做等价判断（可复用现有 `test_math_normalize.py` 的思路）。
只有解答题才走完整识别链 —— 错误面和成本都砍一半。

**主识别：按题裁剪后送模型，不要整页送。**
现在的 `MAX_EDGE=1600` 是在**印刷体整页**上定的基线，那块内容冗余度高、错了能靠上下文纠。
手写解答题是低冗余：一个 `3` 看成 `8`、一个指数看不清，整题就废。
裁成单题再压到 1600，等效 300+ DPI，这才是手写该有的分辨率。

**输出结构强制带不确定性**，而不是只给一串 LaTeX：

```json
{"steps":[{"i":1,"latex":"2x+3=7","raw_text":"2x+3=7","conf":0.94,
           "uncertain_spans":[{"pos":4,"candidates":["3","8"]}]}]}
```

提示词写死三条：**逐字转录不补全**；看不清的字符输出 `⍰` 并给 2 个候选；**禁止根据上下文"修正"学生写法**。
这三条比任何"提高准确率"的努力都值钱 —— 它把错误从"隐形"变成"可检索"。

**交叉校验（决定成败的一步）**：同题跑两遍（换温度或换模型），LaTeX 归一化后 diff。
diff 非空 → 该题打 `transcription_conflict`，**不进自动判定**，直接进人工。

**专用 OCR 只做备选。** 数据不支持它做整段过程识别（UniMERNet 手写集 65~68%；pix2tex 手写 BLEU 0.012）。
用法：对判分关键的那一行（如最终答案）做二次确认。

**几何图：不解析，只描述。** 学生画的辅助线只让模型输出一句自然语言（"在 AB 上取中点 D，连 CD"）供老师扫读，
提示词明确写 **图形描述不得作为扣分依据** —— 图形理解没有可核对的口径，画错辅助线 ≠ 推理错，这条一旦进评分就是冤案来源。

### 3.2 判定：三段式，分数由代码算

**不要一次问完"解题 + 看学生答案 + 打分"**，拆成三次，各自可缓存、可校验：

| 阶段 | 输入 | 输出 | 缓存键 |
|---|---|---|---|
| `solve` | **只给题干**，不给学生答案 | `reference_steps[{id, desc, points}]` | `(question_id, answer_version, model)` |
| `transcribe` | 学生作答的裁剪图 | 3.1 那个 steps 结构 | `(file_id, page, q_no, model)` |
| `judge` | reference_steps + student_steps + rubric | 对齐结论 + 扣分 + 证据 | `(两个输入的 hash)` |

三种判定模式**共用这条链**，只换 `reference_steps` 的来源：

- **老师答案**：走现有 vision 把老师答案转录成 steps（或直接复用题库里已有的 `questions.answer` —— 见第 10 节 Q2）
- **rubric（评分细则）**：老师填/传的分步分值表，直接当 reference_steps
- **AI 自解**：模型生成，但必须标 `reference_unverified: true`，界面上让老师能一键展开看模型自己的解法 ——
  **模型解错时是整题判错，不是扣一分**，这个差别不显示出来就是灾难

判定输出：

```json
{"verdict":"correct|partial|wrong|ungradable","score":7,"max_score":10,
 "alignment":[{"ref":"S2","student":"A3","status":"partial"}],
 "deductions":[{"ref":"S2","points":-1,"reason":"消元后未回代",
   "evidence_quote":"由①②得 x=2","evidence_step":3}],
 "confidence":"high","flags":[],"needs_human":false}
```

**过程分怎么做到"可核对"**：每条 `deduction` 必须带 `evidence_quote`，并且**在代码层校验它是 OCR 原文的子串**
（去掉空白与 `$` 后比对）。对不上 → 该条扣分作废、整题转人工。
⚠️ **光在 prompt 里写"必须引用原文"是没用的**，只有字符串匹配能拦住编造。
`score = max_score − Σdeductions` **由代码算**，模型给的总分只用来做一致性告警。

**`ungradable` 必须是一等公民**（识别不清 / 图题 / 超纲 / 学生没作答）。
一旦允许它落进 `wrong`，系统就开始冤枉人了。

**防瞎给分，按性价比排序**：
1. **先解后判**（学生文本不进 `solve` 的 context）—— 防"顺着学生答案倒推"
2. **证据字符串校验** —— 防编理由
3. **三次采样**（temperature 0.7）取中位数；极差 >1 分或 verdict 翻转 → `needs_human`
4. **结构性拦截**：分数越界 / 扣分和≠总分 / 无 evidence / steps 为空 / 识别 conf 低 → 直接进人工队列，
   **不得进入"已批改"态**
5. **偏置方向选"宽"不选"严"**：对老师自用场景，宁可漏判错 —— 「对的被判错」会一次性摧毁信任。
   证据不足不给扣，标 `insufficient_evidence`

⚠️ 还有一个已证实的偏差要防：判分器容易给**写得更漂亮（LaTeX 密度高）**的答案更高分。
对策：rubric 里写明「排版密度不计分」，并用同一份答案的纯文本版做一次对照抽样。

### 3.3 原件标注：模型给句子，代码给框

| 路线 | 可靠性 | 工作量 | 结论 |
|---|---|---|---|
| 模型直接吐像素 bbox | **已被证伪**（严格框内点 ≤32%；本仓实测框偏高 ~1/8 页） | 低 | 只在有电子版原卷时用 |
| **行切分 + 模型"引用原话"** | 高（可确定性匹配） | 中 | **MVP 主线** |
| **老师点选/拖拽** | 最高 | 低（SVG 覆盖层白拿） | **必做，且是主交互** |
| 自出卷版面对齐（模板坐标 + 定位标记） | 最高（可到几像素） | 中高 | **二期，收益最大** |

路线 2 的关键不是"模型给坐标"，而是 **"模型给句子、代码给框"**：
模型返回 `evidence_quote` → 在本地 OCR 行列表里做字符串匹配 → 取该行 bbox。
有实证支撑：quote-and-retrieve 把 IoU0.5 召回从 ≤8% 提到 26~47%；而直接要坐标时，连 Qwen3-VL-30B 也只有 33% 的响应能解析出坐标。
你要的也不是 IoU0.5，只是"命中正确那一行"—— 行框细长，容错比通用目标检测宽得多。**匹配不到就降级成老师点，链路不会断。**

落地顺序：
- **MVP**：去畸变（检测纸张四角做透视校正）→ 二值化 + 水平投影切行（只切行不切词，中文字距不稳）→
  行 bbox 存表 → 模型 quote → 匹配 → 前端 SVG 在原 PNG 上画圈/下划线。**标注不重渲染，只叠层。**
- **二期**：自出卷在渲染时存每题 bbox，页角印两个定位标记，拍照后单应变换对齐回模板坐标，误差降到几像素。
  这条把「识别 → 定位」彻底解耦。
- **不做**：让模型吐像素框作为最终标注坐标。

### 3.4 成本与延迟：不是成本问题

假设（3 页 / 20 题，12 客观 + 8 解答；价格查于 2026-10-17，随时会变）：

| 模型 | 单人单次 | 30 人一次 |
|---|---|---|
| qwen-vl-max（¥1.6/¥4 每 M） | **≈¥0.24** | ¥7 |
| deepseek（现用，谷时） | ≈¥0.13 | ¥4 |
| Gemini 3 Pro（$2/$12） | ≈¥4.2 | ¥126 |

判分环节三次采样后：qwen ≈¥0.38/人，30 人 ¥11。**量级结论：API 费用可以忽略，不要为省钱牺牲采样次数。**

延迟：单次调用 8~35s。单生串行 ≈14 分钟；内部并发 4~6 降到 **4~6 分钟**；
30 人**按题池化**（240 次判分进一个 6 并发池）约 20~40 分钟，按学生串行则 2 小时+。
⚠️ 池化时注意 SQLite 单写锁：**单写线程、每生一事务**，别让学生级并发去打同一张表。

---

## 4. 数据模型（迁移 `0027_homework_grades.py`，四张新表）

**纯新建表，不碰既有表** —— 与 0025（考试）/ 0026（规划）同一策略，老库升级零风险。

| 表 | 关键字段 |
|---|---|
| `homework_grades` | id / owner_id / student_id / lesson_id / **homework_id（可空，SET NULL）** / curriculum_id / status(`queued/running/reviewing/done/partial/failed`) / answer_mode(`ai`/`key`/`rubric`) / total(页数) / done / failed / score / full_score（**都可空**）/ error / dir_rel / created_at / finished_at / reviewed_at |
| `homework_grade_files` | id / grade_id / **lesson_file_id（SET NULL，提交件直接引用现有原件，不复制）** / name / stored / role(`submission`/`answer`/`rubric`) / kind / size_bytes / pages / status |
| `homework_grade_items` | id(String hex12) / grade_id / seq / page_no / q_no(卷面题号,String) / qtype / stem / student_answer / correct_answer / verdict(`correct/partial/wrong/blank/ungradable`) / score / full_score（可空） / basis / confidence / kp_name / node_id / tags(JSON) / error_reason / flags(JSON) / raw(JSON) / status(`pending/approved/rejected`) / question_id / reviewed_at |
| `homework_grade_marks` | id / grade_id / **item_id（可空）** / page_no / kind(`box`/`ellipse`/`check`/`cross`/`arrow`/`text`) / geo(JSON, 0~1000) / points(JSON, 手绘) / text / color / created_by(`ai`/`teacher`) / flags |

### 四条关键取舍

1. **挂 `homeworks` 的外键，但不加唯一约束、且可空。**
   `homeworks` 上有 `UniqueConstraint("lesson_id")`（一课一条）。批改若也唯一，**重批一次就 IntegrityError**，
   而重批是常态。另外老师可能「先批了再补记交没交」—— `homeworks` 行不存在也得能开批改。
2. **不塞 `batch_jobs` / `batch_items`。** 那两张表的语义是「题库入库的待审中间态」，唯一出口是 `questions`；
   批改的产物是「题库错题 + 作业记录」两处，硬塞会污染语义。**只借它的轮询形状，不借它的表。**
3. **坐标存「按页归一化 0~1000 的整数」**，与 `batch_items.figure_box` 完全同制。
   前端 `viewBox="0 0 1000 1000"` 直接吃，零换算。**不要存 PDF 点坐标** —— 图片是 96dpi 折点、Word 转出的 PDF
   又是另一套页面尺寸，换算迟早出事。
4. **缺数据就是 NULL，绝不用 0**（同 0025 的教训）。识别不出 / 未判的题给 `score=NULL`，
   给 0 会把正确率、雷达图一起带偏。

---

## 5. 接口

前缀 `/api/homework-grades`，全部在 `backend/app/routers/homework_grades.py`。

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/homework-grades` | 建批改任务：`lesson_id` + `curriculum_id` + 提交件 id[] + `answer_mode` + 答案/细则来源 |
| GET | `/api/homework-grades?lesson_id=` | 某节课的批改记录（含进度） |
| GET | `/api/homework-grades/{gid}` | 详情：逐题结果 + 标注 + 原件页清单 |
| POST | `/api/homework-grades/{gid}/run` | 开始/重跑（**幂等**：已在跑则返回当前进度） |
| PATCH | `/api/grade-items/{iid}` | 老师改判定 / 得分 / 考点 / 错因（记 `reviewed_at`） |
| POST | `/api/homework-grades/{gid}/marks` | 加一条标注（AI 或老师） |
| PATCH / DELETE | `/api/grade-marks/{mid}` | 改 / 删一条标注 |
| POST | `/api/grade-items/{iid}/rerun` | 只重批这一题（老师改完错字、或说"识别有误"） |
| POST | `/api/grade-items/{iid}/to-bank` | 错题回存题库（带标签） |
| POST | `/api/homework-grades/{gid}/confirm` | 整份确认（此后才进统计口径） |
| GET | `/api/homework-grades/{gid}/report` | 导出批改结果（先 HTML，PDF 走现有导出链） |

---

## 6. 界面：新视图，不塞进现有弹窗

**放新视图 `frontend/src/views/HomeworkGrade.js`（第 8 个视图）。**
硬理由：`components/Modal.js` 的宽版上限是 860px，批改要「左页图 + 右逐题卡」，860 放不下；
而且批改要刷新可恢复、要能深链。

- 路由：`frontend/src/router.js` 的 `routes` 加 `{ path: '/grade/:id', name: 'grade', ... meta: { title: '作业批改' } }`（hash 模式，刷新不 404）
- 侧栏：**不加**。批改必须带 lesson/homework 上下文；等三期真有「待复核批改台」再加 `/grades`
- 入口：现有「作业」弹窗 `HomeworkDialog.js` 的 footer 加一个「AI 批改」按钮 → 先 `POST` 建任务（把当前 files 带进去）→ 跳 `/grade/{gid}`。两个调用方（`StudentDetail.js` / `Dashboard.js`）各加一个 `@grade` 处理
- 标注层：`frontend/src/components/GradeMarkLayer.js`，**SVG 叠在 `<img>` 上，不用 canvas**
  —— 每条标注要能点选、拖动、单删，SVG 里每个标注就是一个 DOM 节点，白拿；
  canvas 要自己管 dpr/尺寸/重绘/命中测试（`Notes.js` 里那一大堆就是为此）
- `viewBox="0 0 1000 1000" preserveAspectRatio="none"` + `position:absolute; inset:0`
- AI 给的框只可信到「哪一带」（实测偏高 ~1/8 页）→ **默认允许拖动**，并打 `mark_rough` 提示老师（同 `figure_rough` 的处理方式）

---

## 7. 分期

### MVP（第 1 期）— 能独立验证，不调 AI 也该全绿

上传复用 `lesson_files(role='homework')`（现有管线，含 `HomeworkDialog` 的选图/粘贴）→ 建 `homework_grades`
→ 逐页 `adapters/pdf.py::render_page` → 按需裁剪单题 → 模型返回逐题 JSON → 写 `homework_grade_items`
→ 复核页（左页图 + 右逐题卡：判定/得分/考点/错因可改，`markEdited` 对账）→ 单条回存题库。

**不做**：标注、批量、rubric、答案文件上传。

验收：临时库 + 直调 router 函数（同 `test_plans.py` 那套，不起服务不占端口）；
AI 层用固定 JSON 喂 `parse_grade_items`（同 `test_batch_page_ai.py` 的解析层测法）。

### 第 2 期 — 标注 + 三种依据 + 批量

`homework_grade_marks` + SVG 叠加层 + 行切分匹配；答案/细则上传（`answer_mode='key'|'rubric'`）；
多份作业 = 多条 `homework_grades` + 批量页（**复用 `_job_dict` 的轮询形状，不塞进 `batch_jobs`**）；
错题标签 + 批量回存 + 指纹去重。

验收：标注层是纯前端 + 一张表，可单独挂载验证；批量可离线造 3 条 grade 跑轮询。

### 第 3 期 — 对齐、自动、导出

自出卷版式存档（**需新增「卷子存档」**，见 Q1）→ 像素级定位；上传即批；
批改报告导出；掌握度联动（`services/mastery.py` 已有节点加权口径）。

---

## 8. 三个 agent 怎么分工（实现阶段）

先由指挥把**契约**定死（models + schemas + 接口签名 + 迁移），三方对齐后才并行，避免互相猜。

| 角色 | 负责 | 边界 |
|---|---|---|
| **指挥（我）** | 定契约 → 写迁移 → 集成 → 跑测试 → 验收 | 不写业务细节 |
| **Agent A · 后端** | `routers/homework_grades.py`、`services/grade_store.py`（CRUD / 归档 / 任务轮询 / 引用计数） | **不碰 AI 调用** |
| **Agent B · AI 核心** | `services/grade_ai.py`（裁剪 / 识别 / 三段式判定 / 证据校验 / 降级重试 / rubric 解析） | **只对外暴露纯函数**，不碰 HTTP 与 DB |
| **Agent C · 前端** | `views/HomeworkGrade.js`、`components/GradeMarkLayer.js`、`api.js` | **不碰后端** |

拆界原则：B 是纯函数层（可脱机单测），A 是 I/O 层，C 只认接口契约（用固定 JSON 桩）。

---

## 9. 风险清单（会被绊到的坑）

| # | 坑 | 出自 | 撞到批改的哪一步 |
|---|---|---|---|
| 1 | **AGPL：只有 `adapters/pdf.py` 能 import pymupdf** | 既有约定 | 页渲染 / 裁题图 / 算框交集，顺手 import 就破边界 |
| 2 | **1600 + JPEG 会把"模型能力"问题伪装成"识别错误"** | `vision.py` 基线是印刷体整页 | 手写低冗余 → 必须**单题裁剪后再压**；手写页用 PNG 不用 q85 JPEG |
| 3 | **转写版本与分数隐性耦合** | 本次调研新发现 | 老师把识别错的 `3` 改成 `8` 后，旧 `evidence_quote` 已不存在 → 必须 `transcription_version` + 改字后自动重跑该题。**这是数据模型决策，越晚改越贵** |
| 4 | **批量必然出现静默失败** | 全项目**没有一处读 `finish_reason`** | 600 次调用按 1~2% 截断率 = 6~12 题无声无息没结果，前端当"未批改"跳过 → 老师以为学生没做。必须 `finish_reason=="length"` 即重试 |
| 5 | **crops 引用计数漏一处就静默删图** | `services/crops.py` 目前只数 questions + batch_items | 回存题库裁题图 / 删批改任务时，若批改引用了 crops 图必须加一类 |
| 6 | **渲染容器里不能有 Vue 插值** | `mathRender.js` | 批改面板显示题干/学生答案/错因，必须 `renderRichEl` + 空 div + 函数 ref |
| 7 | **`batch_jobs.total` 是页数不是条目数** | `batch_page.create_page_job` 的注释 | 进度条照抄会出现「done 3/3 但列表 11 条」 |
| 8 | **SQLite 单写者 + 请求会话不能跨线程** | `batch_page._run_one_page` | 并行批改页：子线程只做 HTTP，写库回主线程，线程内 `SessionLocal()` |
| 9 | **`uq_homeworks_lesson` 一节课一条** | `models.py` | 新表**不要**给 `lesson_id` 加 UNIQUE，否则重批直接崩 |
| 10 | **上课文件白名单会 422 掉 HEIC** | `lesson_files.py` 的 `kind_of` | iPhone 拍的作业会被挡在门外 → 入口要自带白名单或前端转码 |
| 11 | **SPA 整页 jsdom 挂不了** | `papers_mount_check.js` | 给批改页写挂载自检时只能挂单视图 |
| 12 | **端口按检出目录名判定** | `test_batch_import.py` | 写测试打服务时照抄，别写死 8000 |

---

## 10. 已拍板的决定（2026-10-17）

| # | 问题 | 决定 |
|---|---|---|
| **A1** | 标准答案从哪来 | **老师上传答案 / 评分细则文件**（拍照或 Word）。复用题库 `questions.answer` 留到二期（需先加「卷子存档」） |
| **A2** | 「批量」指什么 | **一个学生一次交多页 / 多份** 算一份作业。跨学生批量放二期 |
| **A3** | 解答题给不给过程分 | **一期只判对错 + 错因，不给分**；按细则给过程分放二期 |
| **B1** | 错题回存题库 | **不自动建题**：先给题库已有题打标签、累计错次；找不到对应题才弹窗问「要新建一道吗」 |
| **B2** | 批改入口 | **放在「交作业」流程里**：作业弹窗保存后出现「批改」选项，点了才跳到批改页。**不另开独立入口** |
| **B3** | 作业来源 | **自出卷为主，但不是「题库出卷」** —— 指老师自己用 AI 生成、或到处摘选拼出来的**电子版卷子** |
| **C1** | 上传即批 | 二期，且默认**不自动** |
| **C2** | 结果导出 | 三期，先 HTML |
| **C3** | 几何辅助线 | 只描述、**不参与扣分** |
| **C4** | 5 维主观分 | 仍**只有老师打**，AI 不碰 |

### B3 带来的路线修正（重要）

原来以为「自出卷 = 题库出卷」，三期打算做「出卷时存档版面 + 页角印定位标记」。
**现在不成立**：卷子不在系统里，是从 AI 生成或各处摘选拼出来的，题库里没有"这次出了哪些题"。

但换个角度反而更好办：**老师手上有电子版原卷**（Word/PDF），打印出来给学生做，学生手写后拍照交回。
于是三期改做「**原卷当模板**」：

1. 老师上传**空白电子原卷** → 渲染成参考页，记下每题的 bbox（模块位置此时已知）
2. 学生拍照件做透视校正 + 单应变换，**对齐到参考页**
3. 每题框直接来自原卷 → 标注准到像素级

这比印定位标记更实用（不改出卷流程）。前提是"拍的是同一份打印出来的卷子"——这一点天然成立，
因为卷子本来就是老师自己打出来给学生做的。

⚠️ 一期完全不碰这条：一期用「模型给句子 → 本地行切分匹配」的通用定位，对练习册、拼的卷子一视同仁。

---

## 附：调研来源

- 开源候选：`UniMERNet` / `LaTeX-OCR(pix2tex)` / `Pix2Text` / `surya` / `texify` / `nougat` / `PDF-Extract-Kit` /
  `AIExamGrader` / `quiz-ai` / `written-exam-grader` / `vision-anchored-autograder` / `OCRAutoScore` /
  `wrong-notebook` / `homework-grader`（license 与更新时间均由 GitHub API 现查 2026-10-17）
- 模型定价：阿里云 qwen-vl-max、DeepSeek、OpenAI、Google Gemini、Anthropic 官方定价页（2026-10-17 查，波动快，排预算前复测）
- 判分可靠性：数学主观题判分 MAE 1.87 / 总分 Pearson 0.58；QEDBENCH 放水率最高 74.8%、分数与 LaTeX 密度正相关
- 坐标定位：VLM 严格框内点 ≤32%；quote-and-retrieve 把 IoU0.5 召回从 ≤8.1% 提到 25.9~46.9%

**未核实**：Pix2Text 权重 license；UniMERNet 手写子集具体分数；surya 在中文手写数学上的实测效果。

---

## 11. 一期实现进度（2026-10-17 晚，已落地未提交）

三个 agent 并行实现（我定契约），文件边界划死，没有互相踩：

| 层 | 文件 | 规模 |
|---|---|---|
| 契约（我） | `models.py` 末尾 M5 段 / `schemas.py` / 迁移 `0027_homework_grades.py` | 4 表 68 列 |
| 后端 I/O | `services/grade_store.py`（1025 行）+ `routers/homework_grades.py`（180 行，13 个接口） | 任务线程、页图缓存、判定依据、标注、确认、回存题库 |
| AI 核心 | `services/grade_ai.py`（1059 行） | 提示词、三级降级、`finish_reason`、证据校验、解析容错 |
| 前端 | `views/HomeworkGrade.js`（581） + `components/GradeMarkLayer.js`（202） + api/router/两个入口 | 左图右卡、轮询对账、判定依据面板 |
| 测试 | `test_homework_grades.py` | **148 项，全绿** |

### 落地时补的两件事（原计划漏了）

1. **判定依据面板**：前端入口原本硬编码 `answer_mode='ai'`，与 A1（答案由老师提供）冲突。
   补了 `PATCH /api/homework-grades/{gid}`（只在**还没批出结果**时可改）+
   批改页顶部的「AI 自己解 / 用我给的答案 / 用我的评分细则」面板。
   ⚠️ 已批过再改依据 → 422 并提示「重新批改」：旧判定是按旧依据算的，改了依据却留着旧判定比不改更糟。
2. **`GradeError` 参数顺序**：三个 agent 各写一半，`grade_store` 里 31 处调用都是 `(status, message)`，
   而类定义是 `(message, status)` —— 每个错误的状态码都会变成一句中文，FastAPI 拿它当 `status_code` 会炸。已按调用侧统一。

### 10-17 深夜追加：手写批改（画 / 擦 / 文字框）+ 导出

用户原话：「加入对以上传文件的笔画功能（能画\能擦\能在文件上放文本框），教师根据答案能够手动批改，批改完后能够导出文件」。

| 做什么 | 怎么落地 |
|---|---|
| 画笔 | `kind='ink'`，点列存 `points`（扁平 `[x,y,…]`），`geo` 只放包围盒。抽稀 2.2 单位/点、单笔上限 900 点 |
| 橡皮 | 按**线段距离**命中（`segDist`，不是包围盒 —— 包围盒会把旁边的东西一起擦掉）。按住划一片，**松手才发一个请求**，串行删 |
| 文字框 | `kind='text'`，点位置 → 弹输入框 → 落库；导出时画成半透明白底 + 边框（扫描件底色深也读得清） |
| √ / × 快放 | `kind='check'/'cross'`，点一下落 44 单位的小方块 —— 判对错是最高频动作，不该逼老师先画框 |
| 撤销 / 清本页 | 只动 `created_by='teacher'` 的，AI 给的框一个不碰 |
| 导出 | `GET /api/homework-grades/{gid}/export?fmt=pdf\|png&page=n`，返回**副本** |

三条硬约束（改动时别破坏）：

1. **绝不把标注烧回原件**。原件是学生真迹，烧上去就回不去了；同一份作业可能还要出第二版（这次红笔、下次蓝笔）。
   导出用 Pillow 画在**新文件**上，源件 md5 前后一致（测试有硬断言）。
2. **不碰 PyMuPDF**（AGPL）。页图由 `grade_store.page_png()` 产出，导出只做「PNG 上画」+「PNG 拼 PDF」，
   Pillow 自带 PDF 输出，不需要 reportlab。
3. **坐标换算只能有一处**：`frontend/src/markGeom.js`（纯函数、不碰 DOM），由 `frontend/mark_geom_check.js`
   用 node 直接量（13 项）。⚠️ x 用宽、y 用高**分别换算** —— 页图是非等比铺满的，用同一个比例竖版页面会整体偏。

测试：`test_homework_grades.py` **148 → 194 项**（新增 ⑧ 节：ink 点列原样存回、越界夹紧、
导出图**逐像素比差异**、红线/绿勾/白底的落点校验、原件 md5 不变、PDF 头、页码越界 422、反复导出不堆垃圾）。

### 一期刻意没做（都在二期）

- 标注的**拖拽**（画/擦/放文字/√× 已经做了；拖动位置仍留到二期）
- **跨学生批量**（A2 只做「一个学生多页」）
- **答案文件上传**（只支持粘贴文字；照片里的答案没有文字，硬走会退回「AI 自解」却仍显示「按老师答案判」）
- rubric **给分**（A3：一期只判对错）
- 报告导出、上传即批、原卷模板对齐（B3 的修正路线）

### 已知限制（写进 README 时要同步）

- 提交件只支持 **PDF / 图片**；Word/PPT 会 422 并提示先转 PDF（没有现成页面视图）
- `answer_mode` 一旦开批就锁住，要换只能整份重批
- 页图缓存落在 `uploads/grades/{gid}/`，**没有删除接口**，所以删记录时不会回收（将来加）
- `GradeConfirmIn.note` 目前没有落点（`homework_grades` 没有备注列）
