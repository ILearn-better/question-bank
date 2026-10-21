/**
 * 批改工作台（workbench.html）的真挂载检查 —— 路线 B 的那一版。
 *
 *   node frontend/workbench_check.js    # 退出码即结果（0 通过 / 跳过，非 0 失败）
 *
 * ## 为什么单独建这个页面、还要单独测
 *
 * 用户 2026-10-19 原话：「直接抄批量录题处理文件方式就行」「当前页面设计太难用了」。
 * 路线 A 是在原批改页上加旋钮（缩略图 / 跳过 / 结束线），布局还是「一次只看一页」；
 * 用户要的是录题页那套：**所有页竖排滚动 + 右边块卡片逐块处理**。
 * 所以路线 B 是独立文件 —— 两条路线并存一个分支，用户对比后再删旧的。
 *
 * ## 这个页面自己踩的坑（都只有真挂载才看得见）
 *
 *   ① **in-DOM 模板里自定义组件不能自闭合**。`<el-tag />` 会让浏览器把后面的兄弟
 *      节点吞成子节点 → 那一块凭空消失，而接口测试全绿。
 *   ② **坐标是 0~1000 不是 pt**。录题页用 PDF 点（它要按 pt 裁原 PDF），批改后端
 *      收的是归一化坐标 —— 照抄 pt 会让每一块的框都缩成纸的一角。这里要数
 *      `.blk-band` 的 `top` 百分比，确认它按 0~1000 算。
 *   ③ **全切语义**：录题页「第一条线以上当卷头跳过」，批改**不能** ——
 *      学生可能从页顶就开始写第 1 题。这里断言「页 1 切 2 条线 → 3 个块
 *      且第一块从 y=0 开始」。
 *   ④ **跨页题靠 seq 分组**：反推之后仍是 1 题 2 段，且两段落不同页。
 *   ⑤ **不判是「不删框」**：点了「不判」块数不能变，只是标灰 + 不送 AI。
 *
 * ## 判据：挂载痕迹，不是「innerHTML 长度」
 *
 * 找不到 jsdom / vendor 就 **SKIP 并退出 0** —— 它只是开发期加固，不该把回归弄红。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = __dirname;                 // frontend/
let JSDOM = null;

function loadJsdom() {
  const home = process.env.USERPROFILE || process.env.HOME || '';
  const cands = [
    process.env.SHIKE_JSDOM_DIR,
    path.join(ROOT, '..', 'node_modules', 'jsdom'),
    ...String(process.env.NODE_PATH || '').split(path.delimiter).map((p) => p && path.join(p, 'jsdom')),
    home && path.join(home, '.workbuddy', 'binaries', 'node', 'workspace', 'node_modules', 'jsdom'),
  ].filter(Boolean);
  for (const c of cands) {
    try {
      const m = require(c);
      if (m && m.JSDOM) { JSDOM = m.JSDOM; return c; }
    } catch (e) { /* 换下一个候选 */ }
  }
  return null;
}

/* ====================== 假接口：形状照 store.detail / brief 逐字来 ====================== */

/** 3 页、5 个块（含 1 个跨页题、1 个「不判」、1 个框选段）。
 *  故意让第一页有 2 条线 → 3 段，用来验「第一条线以上也算一块」。 */
function DETAIL() {
  return {
    id: 7, student_id: 5, lesson_id: 31, status: DETAIL_STATUS,
    answer_mode: detailMode(), basis_modes: DETAIL_MODES.slice(),
    answer_text: '', rubric_text: '',
    total: 3, done: DETAIL_STATUS === 'queued' ? 0 : 3, failed: 0,
    score: null, full_score: null, error: '',
    created_at: '2026-10-19T10:00:00', finished_at: '2026-10-19T10:03:00',
    reviewed_at: null, skipped_pages: [],
    files: [{ id: 1, role: 'submission', name: '作业.pdf', pages: 3,
      lesson_file_id: 1, chars: 0, preview: '' }].concat(BASIS_ROWS),
    regions: [
      // 页 1：2 条线切 3 段（全切 —— 第一条线以上也是块，卷头那段老师按需点「不判」）
      { id: 301, page_no: 1, seq: 0, seg: 0, q_no: '', label: '1', box: [0, 0, 1000, 320], skipped: 0 },
      { id: 302, page_no: 1, seq: 1, seg: 0, q_no: '', label: '2', box: [0, 320, 1000, 760], skipped: 0 },
      // 跨页题：页 1 下半 + 页 2 上半，同一个 seq
      { id: 303, page_no: 1, seq: 2, seg: 0, q_no: '', label: '3', box: [0, 760, 1000, 1000], skipped: 0 },
      { id: 304, page_no: 2, seq: 2, seg: 1, q_no: '', label: '3', box: [0, 0, 1000, 400], skipped: 0 },
      // 页 2 下半：标了「不判」（结构说明段那种）
      { id: 305, page_no: 2, seq: 3, seg: 0, q_no: '', label: '4', box: [0, 400, 1000, 1000], skipped: 1 },
      // 页 3：手拖的框（不是整页宽）→ 反推时不能变成线
      { id: 306, page_no: 3, seq: 4, seg: 0, q_no: '', label: '5', box: [40, 60, 900, 500], skipped: 0 },
    ],
    /* ⚠️ item 的 id **必须是十六进制字符串** —— 真库里是 `uuid4().hex[:12]`。
       用数字 id 会漏掉一个真 bug：`paintOne` 里若把 key 正则写成 `(stem|ans)(\d+)`，
       十六进制 id 一个都匹配不上，题干/作答永远是空框 —— 而库里明明有内容
       （2026-10-20 用户报「题干、学生作答没展示出来」，真因就是这个）。 */
    items: [
      { id: 'a1b2c3d4e5f6', seq: 1, page_no: 1, q_no: '1', qtype: '解答题', stem: '解方程：$x^2-5x+6=0$。',
        student_answer: '$x=2$ 或 $x=3$', correct_answer: '$x=2$ 或 $x=3$',
        verdict: 'correct', score: null, full_score: null, basis: '因式分解后两根都对',
        confidence: 'high', kp_name: '一元二次方程', tags: [], error_reason: '',
        flags: [], figure_note: '', block_kind: 'question', tail_note: '', status: 'pending',
        /* 0031：模型给的分类标签。故意**乱序 + 带一个空值 + 带一个多余的键**，
           用来验「显示顺序固定 / 空值不渲染半个标签 / 多余键被丢掉」。 */
        labels: { exam: '公式变形', stage: '初中', kp: '一元二次方程',
                  system: '数与代数', difficulty: '中档', blank: '   ' } },
      { id: 'ff0011223344', seq: 2, page_no: 1, q_no: '2', qtype: '证明题', stem: '如图，求证 $AD\\perp BC$。',
        student_answer: '因为 $AB=AC$，所以垂直', correct_answer: '',
        verdict: 'partial', score: null, full_score: null, basis: '结论对但缺全等这一步',
        confidence: 'medium', kp_name: '', tags: [], error_reason: '',
        flags: ['low_confidence'], figure_note: '',
        block_kind: 'question',
        labels: { stage: '初中', system: '图形与几何', kp: '等腰三角形性质',
                  exam: '逻辑推理' },
        tail_note: '底部有邻题的残字，这一块可能把下一题的开头也读进来了', status: 'pending' },
      { id: '0abc9def5678', seq: 3, page_no: 2, q_no: '3', qtype: '', stem: '', student_answer: '',
        correct_answer: '', verdict: 'ungradable', score: null, full_score: null, basis: '',
        confidence: '', kp_name: '', tags: [], error_reason: '框里没读到任何作答内容',
        flags: ['region_empty'], figure_note: '', block_kind: 'header', tail_note: '',
        status: 'pending', labels: {} },
    ],
    marks: [],
  };
}

/* ★★ 从后端源码读「哪条路径允许哪些方法」，桩按它校验。
 *
 *   为什么非要这么干：这次真机报「分割没存上：Method Not Allowed」——
 *   产品写的是 `POST /regions`，后端是 `PUT`，而**桩也写 POST**。
 *   两边照同一个错误假设写，挂载检查全绿、真机 405。
 *   手写桩永远可能按自己的假设对齐，所以这里让桩去读后端真正的
 *   `@router.<method>("path")`，方法不对就照 FastAPI 的样子回 405。
 *
 *   路径里的 `{gid}` / `{pno}` 归一成 `*`，请求侧的数字段也归一成 `*`。 */
const BACKEND_ROUTES = (function readBackendRoutes() {
  const file = path.join(ROOT, '..', 'backend', 'app', 'routers', 'homework_grades.py');
  const map = {};
  let src = '';
  try { src = fs.readFileSync(file, 'utf8'); } catch (e) { return map; }
  const re = /@router\.(get|post|put|patch|delete)\(\s*"([^"]+)"/g;
  let m;
  while ((m = re.exec(src))) {
    const key = m[2].replace(/\{[^}]+\}/g, '*');
    if (!map[key]) map[key] = [];
    map[key].push(m[1].toUpperCase());
  }
  return map;
}());
/** 请求 URL → 路由表的 key（去掉 /api 前缀与查询串，数字段归一成 *）。 */
function routeKey(u) {
  return String(u).replace(/^\/api/, '').split('?')[0].replace(/\/\d+(?=\/|$)/g, '/*');
}
/** 路径对、方法错 —— 真机上就是 405。 */
const BAD_METHOD = [];

/** 极简 fetch 桩。`region-image` / `page` 是 <img src>，不走这里。 */
const CALLS = [];
/** 让某一轮的 /cancel 走「刚好跑完了」这条分支 —— 后端这时返回 200，不是 409。 */
let CANCEL_MODE = '';
let LAST_DETAIL = null;
/* ---- 新建批改那一节的桩状态 ---- */
let NEW_GRADE_BODY = null;
let UPLOADED_FILES = [];
let UPLOAD_FAILS = false;
let FAIL_REGIONS = false;
const LESSONS = () => ([
  { id: 1, student_id: 11, student_name: '张三', start_at: '2026-10-18T19:00:00',
    topic: '二次函数', duration_min: 90 },
  { id: 2, student_id: 12, student_name: '李四', start_at: '2026-10-19T19:00:00',
    topic: '', duration_min: 60 },
]);
const LSN_FILES = () => ([
  { id: 501, name: '张三_01.jpg', kind: 'image', kind_cn: '图片', pages: 1 },
  { id: 502, name: '张三_02.jpg', kind: 'image', kind_cn: '图片', pages: 1 },
].concat(UPLOADED_FILES));
/** 桩里新建标注的自增 id（900+，与 DETAIL 里手写的那些不撞）。 */
let LAST_MARK_ID = 900;
/** 下一回 /run 故意失败一次 —— 用来验「没开起来，按钮要能恢复」。 */
let FAIL_RUN_ONCE = false;
/** 下一回存档故意失败一次 —— 验「失败要说人话 + 按钮要归位」。 */
let FAIL_ARCHIVE = false;
/* ---- 判定依据那一节的桩状态 ----
 * 用**可变**的 status / answer_mode 而不是写死 'reviewing'：
 * 判定依据只在「还没批出结果」时能改（与后端 `_assert_setup_editable` 同一个判据），
 * 写死 reviewing 的话整节都在测「锁住」这一条分支。 */
let DETAIL_STATUS = 'reviewing';
/** 勾上的条件（`[]` / `['key']` / `['rubric']` / `['key','rubric']`）。
 *  桩里也只留这一份真值，`answer_mode` 由它算出来 —— 跟后端同一个编码规则，
 *  否则桩会「自己编一个库里不可能出现的状态」，测试再绿也没意义。 */
let DETAIL_MODES = [];
const detailMode = () => (DETAIL_MODES.slice().sort().join('+') || 'ai');
/** 界面上「已传的依据」= 这个数组（真后端从 detail.files 里来）。 */
let BASIS_ROWS = [];
/** 下一回上传故意失败一次 —— 验「失败留在列表里可以单条重试」。 */
let BASIS_FAIL_ONCE = false;
function makeFetch() {
  const reply = (obj, status) => ({
    ok: (status || 200) < 400,
    status: status || 200,
    json: async () => JSON.parse(JSON.stringify(obj)),
  });
  return async (url, opt) => {
    const u = String(url || '');
    const method = String((opt && opt.method) || 'GET').toUpperCase();
    let body = null;
    if (opt && typeof opt.body === 'string') {
      try { body = JSON.parse(opt.body); } catch (e) { body = opt.body; }
    }
    CALLS.push({ url: u, method, body });
    /* ★ 方法校验：路径在后端路由表里、但方法不对 → 照 FastAPI 回 405。
       真机就是这么报「分割没存上：Method Not Allowed」的，桩必须一样硬。 */
    const key = routeKey(u);
    if (BACKEND_ROUTES[key] && BACKEND_ROUTES[key].indexOf(method) < 0) {
      BAD_METHOD.push(method + ' ' + u + ' → 后端只允许 ' + BACKEND_ROUTES[key].join('/'));
      return reply({ detail: 'Method Not Allowed' }, 405);
    }
    if (u === '/api/homework-grades' && method === 'GET') {
      return reply([{ id: 7, status: 'reviewing', total: 3, done: 3, created_at: '2026-10-19T10:00:00' }]);
    }
    if (u === '/api/homework-grades/7' && method === 'GET') {
      const prev = LAST_DETAIL;
      LAST_DETAIL = DETAIL();
      /* ⚠️ 详情是可以反复取的（`settle()` 每次画完一笔就要取一次）。
       *  服务端那边标注与批改结果是**累积**的 —— 每次都返回一份全新 DETAIL() 的话，
       *  「画完一笔 → settle 之后那一笔不见了」，于是所有「存进去了」的断言
       *  都变成假的（只是验了「发过请求」）。 */
      if (prev) {
        LAST_DETAIL.marks = prev.marks;
        LAST_DETAIL.items = prev.items;
      }
      return reply(LAST_DETAIL);
    }
    /* 批改文件留存：批改台点「导出到作业存档」→ 后端出 PDF 并归档。
       返回形状照真接口（detail 对象，含 rel_path / url / can_preview）。 */
    if (u === '/api/grade-archives/generate' && method === 'POST') {
      if (FAIL_ARCHIVE) return reply({ detail: '（测试造的失败）' }, 500);
      return reply({
        id: 61, student_id: 11, grade_id: (body && body.grade_id) || 0,
        archived_on: '2026-10-20', name: '批改_张三_20261020.pdf',
        rel_path: 'students/u11_张三/2026-10-20/批改/批改_张三_20261020.pdf',
        kind: 'pdf', size_bytes: 20480, source: 'grade', grader: '', note: '',
        can_preview: true, exists: true,
        url: '/api/grade-archives/61/raw',
        inline_url: '/api/grade-archives/61/raw?inline=1',
      });
    }
    /* ---- 判定依据（0034）：改模式 / 上传 / 删 / 取正文 ---- */
    if (u === '/api/homework-grades/7' && method === 'PATCH') {
      DETAIL_MODES = (body && body.basis_modes) || [];
      const keepMarks = (LAST_DETAIL && LAST_DETAIL.marks) || [];
      const keepItems = (LAST_DETAIL && LAST_DETAIL.items) || [];
      LAST_DETAIL = DETAIL();
      LAST_DETAIL.marks = keepMarks;
      LAST_DETAIL.items = keepItems;
      return reply(LAST_DETAIL);
    }
    /* ⚠️ 上传那条 URL **带查询串**（?role=answer），不能写成 `u === '.../basis'`：
       那样匹配不上，请求会掉进兜底分支（返回一份详情），于是「上传成功」变成假的 ——
       列表里没有、也没有失败记录，最难查的一种。 */
    if (/^\/api\/homework-grades\/7\/basis(\?|$)/.test(u) && method === 'POST') {
      if (BASIS_FAIL_ONCE) {
        BASIS_FAIL_ONCE = false;
        return reply({ detail: '（测试造的失败）' }, 500);
      }
      const role = /role=rubric/.test(u) ? 'rubric' : 'answer';
      const row = {
        id: 700 + BASIS_ROWS.length, role: role, name: '依据' + (BASIS_ROWS.length + 1) + '.png',
        kind: 'image', size_bytes: 2048, pages: 0, chars: 0,
        lesson_file_id: null, preview: '', text_warn: '',
      };
      BASIS_ROWS.push(row);
      return reply({ file: row, note: '' });
    }
    if (/^\/api\/homework-grades\/7\/basis\/\d+$/.test(u) && method === 'DELETE') {
      const id = Number(u.split('/').pop());
      BASIS_ROWS = BASIS_ROWS.filter((x) => x.id !== id);
      return reply({ ok: true });
    }
    if (/^\/api\/homework-grades\/7\/basis\/\d+\/text$/.test(u) && method === 'GET') {
      return reply({ text: '题1：x=2 或 x=3\n题2：42', chars: 18, kind: 'text', name: '答案.xlsx' });
    }
    if (u === '/api/homework-grades/7/regions' && method === 'PUT') {
      if (FAIL_REGIONS) return reply({ detail: '（测试造的失败）' }, 500);
      // 真后端是「整份替换」，且会把同一题的几段归一成一致的 skipped
      const rows = (body && body.regions) || [];
      const skip = {};
      rows.forEach((r) => { skip[Number(r.seq)] = (skip[Number(r.seq)] || 0) || (r.skipped ? 1 : 0); });
      LAST_DETAIL.regions = rows.map((r, i) => ({
        id: 400 + i, page_no: Number(r.page_no), seq: Number(r.seq || 0), seg: 0,
        q_no: r.q_no, label: r.q_no || String(Number(r.seq || 0) + 1),
        box: r.box, skipped: skip[Number(r.seq)] || 0,
      }));
      return reply({ regions: LAST_DETAIL.regions });
    }
    if (u === '/api/homework-grades/7/skipped-pages' && method === 'PATCH') {
      LAST_DETAIL.skipped_pages = (body && body.pages) || [];
      return reply({ skipped_pages: LAST_DETAIL.skipped_pages });
    }
    /* ---- 新建批改：课时列表 / 作业原件清单 / 上传 / 建档 ---- */
    if (u.indexOf('/api/lessons?') === 0 && method === 'GET') {
      return reply(LESSONS());
    }
    if (/^\/api\/lessons\/\d+\/files\?role=homework$/.test(u) && method === 'GET') {
      return reply({ items: LSN_FILES(), rel_dir: 'x/y', accept: [] });
    }
    if (/^\/api\/lessons\/\d+\/files\?role=homework$/.test(u) && method === 'POST') {
      if (UPLOAD_FAILS) {
        return reply({ detail: '不支持这种格式：目前只认 PDF / Word / 图片' }, 415);
      }
      const fid = 600 + (UPLOADED_FILES.length || 0);
      UPLOADED_FILES.push({ id: fid, name: 'up-' + fid + '.png' });
      return reply({ id: fid, name: 'up-' + fid + '.png', kind: 'image', pages: 1 });
    }
    if (u === '/api/homework-grades' && method === 'POST') {
      NEW_GRADE_BODY = body;
      return reply(Object.assign({}, DETAIL(), { id: 8, status: 'queued', done: 0 }));
    }
    /* 终止：只在「真的在跑」时受理（后端是 409）。这里靠 LAST_DETAIL.status 判。
       CANCEL_MODE='stopped' 用来模拟「这一批刚好在这一瞬间跑完了」—— 后端这时
       给的是 200 + already_stopped，**不是** 409（详见 cancel 路由的注释）。 */
    if (u === '/api/homework-grades/7/cancel' && method === 'POST') {
      if (CANCEL_MODE === 'stopped') {
        return reply({ ok: true, gid: 7, canceling: false, already_stopped: true });
      }
      if (LAST_DETAIL.status !== 'running' && LAST_DETAIL.status !== 'queued') {
        return reply({ detail: '这份作业现在没在批改' }, 409);
      }
      LAST_DETAIL.status = 'reviewing';
      LAST_DETAIL.error = '已手动终止：只批了 1/3 页，已批的可以照常复核';
      return reply({ ok: true, gid: 7 });
    }
    if (u === '/api/homework-grades/7/run' && method === 'POST') {
      if (FAIL_RUN_ONCE) { FAIL_RUN_ONCE = false; return reply({ detail: 'AI 服务 502' }, 502); }
      LAST_DETAIL.status = 'queued'; LAST_DETAIL.done = 0;
      return reply(LAST_DETAIL);
    }
    /* ---- 手写批改的标注 ----
     *  ⚠️ 与真实 store.mark_dict 同形状（id / item_id / page_no / kind / geo /
     *     points / text / color / created_by），否则「画完存进去了」是假的。 */
    if (u === '/api/homework-grades/7/marks' && method === 'POST') {
      const m = Object.assign({ id: 900 + (LAST_MARK_ID += 1), item_id: null, created_by: 'teacher' },
        body || {});
      LAST_DETAIL.marks.push(m);
      return reply(m);
    }
    if (u === '/api/homework-grades/7/marks/batch' && method === 'POST') {
      const rows = ((body && body.marks) || []).map((b) => Object.assign(
        { id: 900 + (LAST_MARK_ID += 1), item_id: null, created_by: 'teacher' }, b));
      LAST_DETAIL.marks = LAST_DETAIL.marks.concat(rows);
      return reply({ marks: rows });
    }
    if (/^\/api\/grade-marks\/\d+$/.test(u) && method === 'PATCH') {
      const id = Number(u.split('/').pop());
      const m = LAST_DETAIL.marks.find((x) => x.id === id);
      if (m && body) Object.assign(m, body);
      return reply(m || {});
    }
    if (/^\/api\/grade-marks\/\d+$/.test(u) && method === 'DELETE') {
      const id = Number(u.split('/').pop());
      LAST_DETAIL.marks = LAST_DETAIL.marks.filter((x) => x.id !== id);
      return reply({ ok: true });
    }
    if (/^\/api\/grade-items\/[0-9a-zA-Z_-]+$/.test(u) && method === 'PATCH') {
      const id = Number(u.split('/').pop());
      const it = LAST_DETAIL.items.find((x) => x.id === id);
      if (it && body && body.verdict) it.verdict = body.verdict;
      return reply({ item: it || {} });
    }
    if (/^\/api\/grade-items\/[0-9a-zA-Z_-]+\/rerun$/.test(u) && method === 'POST') return reply({ ok: 1 });
    return reply({ ok: 1 });
  };
}

/* ============================== 断言 ============================== */

let total = 0;
const fails = [];
function check(label, got, want) {
  total += 1;
  const okk = JSON.stringify(got) === JSON.stringify(want);
  console.log((okk ? '  [OK ] ' : '  [FAIL] ') + label + ': ' + JSON.stringify(got)
    + (okk ? '' : '  期望 ' + JSON.stringify(want)));
  if (!okk) fails.push(label);
}
function checkTrue(label, cond, detail) {
  total += 1;
  console.log((cond ? '  [OK ] ' : '  [FAIL] ') + label + (detail === undefined ? '' : ': ' + detail));
  if (!cond) fails.push(label);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const VENDOR = [
  'vendor/vue.global.prod.js',
  'vendor/element-plus/index.full.min.js',
  'vendor/marked.min.js',
  'vendor/purify.min.js',
  'vendor/katex/katex.min.js',
  'vendor/katex/auto-render.min.js',
];

/* ====================== 从 node 侧 import 真模块 ====================== */

/** `vue` 在浏览器里靠 import map 解析；在 node 里得有个同名包，
 *  而这个包的导出**直接转发 globalThis.__VUE__** —— 也就是 jsdom window 上那份，
 *  与页面主脚本用的是同一个实例（与 vendor/vue-from-global.js 同一个道理）。 */
const VUE_PKG = JSON.stringify({
  name: 'vue', version: '0.0.0-stub', type: 'module',
  main: 'index.js', exports: { '.': './index.js' },
}, null, 2);
const VUE_SHIM = [
  "const V = globalThis.__VUE__;",
  "if (!V) throw new Error('jsdom window 上没有 Vue —— vendor 没喂进去');",
  "export const computed = (...a) => V.computed(...a);",
  "export const nextTick = (...a) => V.nextTick(...a);",
  "export const ref = (...a) => V.ref(...a);",
  "export const watch = (...a) => V.watch(...a);",
  "export const reactive = (...a) => V.reactive(...a);",
  "export const onMounted = (...a) => V.onMounted(...a);",
  "export const onBeforeUnmount = (...a) => V.onBeforeUnmount(...a);",
  "export default V;",
].join('\n');

/** 搭一个临时骨架，把 workbench.html 动态 import 的那两个模块**原样** import 进来。
 *  返回 null 表示这一步没成（页面会走「没有手写功能」的降级，其余断言照跑）。 */
async function loadLayerModules(root, W) {
  try {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'wb_layer_'));
    const copy = (rel) => {
      fs.mkdirSync(path.dirname(path.join(tmp, rel)), { recursive: true });
      fs.copyFileSync(path.join(root, rel), path.join(tmp, rel));
    };
    copy('src/components/GradeMarkLayer.js');
    copy('src/markGeom.js');       // 坐标几何：标注层靠它算包围盒与命中
    copy('src/gradeDraft.js');     // 本机草稿队列（画到一半退出不丢笔迹）
    fs.mkdirSync(path.join(tmp, 'node_modules', 'vue'), { recursive: true });
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'package.json'), VUE_PKG);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'index.js'), VUE_SHIM);

    global.__VUE__ = W.Vue;
    global.window = W;
    global.document = W.document;
    global.localStorage = W.localStorage;
    const { pathToFileURL } = require('url');
    const layer = await import(
      pathToFileURL(path.join(tmp, 'src', 'components', 'GradeMarkLayer.js')).href);
    const draft = await import(pathToFileURL(path.join(tmp, 'src', 'gradeDraft.js')).href);
    return { layer: layer.default, draft };
  } catch (e) {
    console.log('  [WARN] 标注层模块没 import 进来（' + e.message + '）—— 手写相关断言会跳过');
    return null;
  }
}

/* ============================== main ============================== */

async function main() {
  console.log('批改工作台 · 真挂载检查（jsdom 挂 workbench.html，数渲染痕迹）');

  const jsdomDir = loadJsdom();
  if (!jsdomDir) {
    console.log('  [SKIP] 没找到 jsdom（可选依赖），跳过');
    return 0;
  }
  console.log('  jsdom: ' + jsdomDir);

  const file = path.join(ROOT, 'workbench.html');
  if (!fs.existsSync(file)) {
    console.log('  [FAIL] workbench.html 不存在');
    return 1;
  }
  const html = fs.readFileSync(file, 'utf8');
  /* 主界面源码。工作台要在侧栏有入口 —— 这条必须对着**真源码**断言，
     否则「加了入口」这件事永远是口头验收（加错了一样绿）。 */
  const APP_SRC = fs.readFileSync(path.join(ROOT, 'src', 'App.js'), 'utf8');

  /* 内联脚本用正则抽出来自己 eval —— 不依赖 jsdom 执行 <script>：
   * runScripts:'outside-only' 下它不会跑，而 'dangerously' 又会让所有 src 脚本
   * 真的去磁盘取（路径是 /vendor/...，取不到）。 */
  /* ⚠️ 排除 `<script type="importmap">`：那不是要执行的 JS，是给模块解析用的表。
   *    留在 blocks 里会变成 2 段 → 下面「正好 1 段」的断言红，而且 eval 一段
   *    JSON 会直接抛错，看起来像「页面脚本写坏了」，其实只是没过滤。 */
  const blocks = (html.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g) || [])
    .filter((b) => !/\btype\s*=\s*["']?importmap/i.test(b));
  if (blocks.length !== 1) {
    console.log('  [FAIL] 期望正好 1 段内联脚本，实际 ' + blocks.length);
    return 1;
  }
  const inline = blocks[0].replace(/^<script[^>]*>/, '').replace(/<\/script>$/, '');

  /* ★ import map：标注层（ESM）里写的是 `import { ref } from 'vue'`，裸模块名
   *   在浏览器里只能靠 import map 解析。两个都要对，少一个就是「按钮都在、画不出笔迹」：
   *     ① 有 import map，且把 vue 指向 **vue-from-global.js**（不是 esm-browser ——
   *        那样页面上是两份 Vue，响应式全废，还不报红）
   *     ② 它必须出现在**第一个 module 脚本之前**（这里是页面主脚本之前）
   *     ③ vendor 里真有这个文件 */
  checkTrue('★ 有 import map，且 vue 指向 vue-from-global.js（不能出现两份 Vue）',
    /<script\s+type="importmap">[\s\S]*?"vue"\s*:\s*"\/vendor\/vue-from-global\.js"/.test(html),
    (html.match(/<script\s+type="importmap">[\s\S]{0,160}/) || [''])[0].replace(/\s+/g, ' '));
  /* ⚠️ 用正则而不是 `indexOf('<script>\nconst ...')`：那个写法把行尾一起钉死了，
   *   文件一变成 CRLF 就红 —— 而它红的理由跟「import map 的位置」毫无关系。 */
  checkTrue('★ import map 排在主脚本之前（否则裸模块名解析不了）',
    html.indexOf('type="importmap"') > 0
    && html.indexOf('type="importmap"') < html.search(/<script>\s*const \{ createApp \}/),
    html.indexOf('type="importmap"') + ' < ' + html.search(/<script>\s*const \{ createApp \}/));
  checkTrue('★ vendor/vue-from-global.js 真的存在',
    fs.existsSync(path.join(ROOT, 'vendor', 'vue-from-global.js')));

  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8000/workbench.html?gid=7',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const W = dom.window;
  W.fetch = makeFetch();
  // Element Plus 会摸这几个东西，jsdom 没有 —— 补上，否则挂载就炸在无关的地方
  W.ResizeObserver = W.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
  if (!W.matchMedia) {
    W.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {},
      addEventListener() {}, removeEventListener() {} });
  }
  W.scrollTo = () => {};

  for (const f of VENDOR) {
    const p = path.join(ROOT, f);
    checkTrue('vendor 存在：' + f, fs.existsSync(p));
    if (!fs.existsSync(p)) return 1;
    try {
      W.eval(fs.readFileSync(p, 'utf8'));
    } catch (e) {
      console.log('  [FAIL] vendor 执行失败：' + f + ' —— ' + e.message);
      return 1;
    }
  }
  checkTrue('Vue / Element Plus / marked / katex 都进了 window',
    !!(W.Vue && W.ElementPlus && W.marked && W.katex),
    ['Vue', 'ElementPlus', 'marked', 'katex'].filter((k) => !W[k]).join(', ') || '齐全');

  // 拿到根实例：hook 一下 createApp + mount（页面自己 mount，返回值没存）。
  // ⚠️ 不要用 `app._instance.proxy`：这一版 Vue 里 mount 之后 `_instance` 仍是 null，
  //    只有 `mount()` 的返回值才是根组件代理（一开始就是这么红的）。
  const origCreate = W.Vue.createApp;
  W.Vue.createApp = function (...args) {
    const a = origCreate.apply(this, args);
    const origMount = a.mount.bind(a);
    a.mount = (el) => {
      const proxy = origMount(el);
      W.__VM__ = proxy;
      W.__APP__ = a;
      return proxy;
    };
    return a;
  };
  /* ★ 确认框自动点「确定」。产品里的 confirm 是异步的，jsdom 里没人去点它 ——
   *   不断言这一层的话，「终止」「删除」这类走二次确认的动作在测试里都是死的。
   *   记下 `CONFIRMS` 好断言「真的问过老师一句」（未经确认就删东西是不能接受的）。 */
  const CONFIRMS = [];
  /* ★ 记下给用户看的那句话。产品里失败走 ElMessage.error 而**不抛异常**，
   *   所以「到底提示了什么」只能在这里看 —— 不记的话这类断言全是空的。 */
  const MSGS = [];
  if (W.ElementPlus && W.ElementPlus.ElMessage) {
    W.ElementPlus.ElMessage = {
      /* ⚠️ 两种入参都要接：`error('一句话')` 与 `error({ message, duration })`。
       *   只按字符串处理的话，传对象的那些提示在 MSGS 里是 `[object Object]`，
       *   「到底提示了什么」这类断言就变成对着一串废话匹配。 */
      error: (m) => MSGS.push({ type: 'error', text: String((m && m.message) || m) }),
      success: (m) => MSGS.push({ type: 'success', text: String((m && m.message) || m) }),
      warning: (m) => MSGS.push({ type: 'warning', text: String((m && m.message) || m) }),
      info: (m) => MSGS.push({ type: 'info', text: String((m && m.message) || m) }),
    };
  }
  if (W.ElementPlus && W.ElementPlus.ElMessageBox) {
    W.ElementPlus.ElMessageBox.confirm = async function (msg, title, opt) {
      CONFIRMS.push({ msg: String(msg), title: String(title || ''), opt: opt || {} });
      return true;
    };
  }
  /* ★ 手写标注层是**动态 import** 的 ES 模块。jsdom 的 vm 上下文不支持它，
   *   所以这里从 node 侧把两个真模块 import 进来，再从
   *   `window.__SHIKE_LAYER_MODULES__` 递给页面（页面上有这个缝，生产走真 import）。
   *
   *   ⚠️ 一定要用**真模块**：自己写个假组件的话，「笔迹画在哪儿 / 草稿有没有存住」
   *      全变成「有没有调用」，而这个页面踩过的坑恰恰是「算得对不对」。
   *   ⚠️ `global.localStorage` 必须搬过来：gradeDraft 在 node 的 globalThis 上
   *      找不到 localStorage 会走降级分支，草稿断言就永远红
   *      （grade_mount_check 2026-10-19 就是这么红的）。 */
  const layerModules = await loadLayerModules(ROOT, W);
  checkTrue('★ 标注层与草稿队列两个真模块都 import 进来了',
    !!(layerModules && layerModules.layer && layerModules.draft),
    layerModules ? Object.keys(layerModules).join(',') : 'null');
  if (layerModules) W.__SHIKE_LAYER_MODULES__ = layerModules;

  try {
    W.eval(inline);
  } catch (e) {
    console.log('  [FAIL] 页面脚本执行失败：' + e.message);
    console.log(e.stack);
    return 1;
  }
  await sleep(600);

  const D = W.document;
  const root = D.querySelector('#app');
  const vm = W.__VM__;
  if (!vm) {
    console.log('  [FAIL] 拿不到根实例（页面没挂起来？）');
    return 1;
  }
  const body = () => root.innerHTML;

  /* ============ 1. 挂起来了 ============ */
  checkTrue('视图真的挂起来了（不是只剩未编译的模板）',
    !body().includes('{{ ') && !body().includes('v-for='), '含 ' + body().length + ' 字符');
  checkTrue('页头渲染出来了', (root.querySelector('h1') || {}).textContent === '批改工作台',
    (root.querySelector('h1') || {}).textContent);
  check('顶栏的作业下拉被填上了（列表接口通了）', vm.grades.length, 1);

  /* ============ 2. 页数 / 反推 ============ */
  check('★ 从 detail.total 得到 3 页', vm.pageList, [1, 2, 3]);
  check('★ 页 1 的 2 条线反推出来了（段 → 线，不额外存一份）', vm.dividers[1], [320, 760]);
  check('页 2 只有 1 条线', vm.dividers[2], [400]);
  check('★ 页 3 那个手拖的框**没有**被当成线（它不是整页宽）', vm.dividers[3], undefined);
  check('框选段单独记一份（页 3）', vm.rects.length, 1);
  check('框选的页号对', vm.rects[0] ? vm.rects[0].page : 0, 3);
  check('★ 反推出 5 个块（3 段 + 1 跨页合并 + 1 框选 = 5 题）', vm.blocks.length, 5);
  check('页 1 切出 3 块（**第一条线以上也算** —— 学生可能从页顶就开始写）',
    vm.bandsOf(1).length, 3);
  check('页 1 第一段从 y=0 开始（卷头没被丢掉）',
    vm.bandsOf(1)[0] ? vm.bandsOf(1)[0].box : [], [0, 0, 1000, 320]);

  /* ============ 3. 跨页题 ============ */
  const cross = vm.blocks.find((b) => b.regions.length > 1);
  checkTrue('★ 跨页题还是 1 题 2 段（seq 分组没被反推拆开）', !!cross);
  check('★ 两段落在不同页上', cross ? [cross.startPage, cross.endPage] : [], [1, 2]);
  check('★ 跨页块的缩略图 spec 是两段（后端靠它竖拼成一张）',
    cross ? decodeURIComponent(String(cross.thumb).split('spec=')[1]).split(';').length : 0, 2);

  /* ============ 4. 不判 ============ */
  check('★ 库里标了不判的那一块反推回来了（页 2 下半）',
    vm.blocks.filter((b) => b.skipped).length, 1);
  const skipIdx = vm.blocks.findIndex((b) => b.skipped);
  check('不判的是第 4 块（页 2 下半）', skipIdx, 3);
  const cards = D.querySelectorAll('.blk-card');
  check('★ 右栏 5 张块卡片都上屏了（不是只算了个 numbers）', cards.length, 5);
  check('★ 标了不判的卡片有 .skipped（一眼与要送的块区分开）',
    D.querySelectorAll('.blk-card.skipped').length, 1);
  check('缩略图一张不少（切歪了当场看得见）',
    D.querySelectorAll('.blk-thumb').length, 5);
  checkTrue('缩略图走 /region-image 现裁（不落盘）',
    Array.from(D.querySelectorAll('.blk-thumb'))
      .every((el) => String(el.getAttribute('src')).includes('/region-image?spec=')),
    Array.from(D.querySelectorAll('.blk-thumb'))[0]
      ? Array.from(D.querySelectorAll('.blk-thumb'))[0].getAttribute('src') : '');
  /* ★ 缩略图的 spec 必须是**后端约定好的** `页号:x0,y0,x1,y1`，多段 `;` 连。
     `/region-image` 是 0030 之后才有的接口 —— 后端跑旧进程时它就是 404，
     那时页面上一张图都没有、却没有任何提示（2026-10-20 用户实际遇到）。
     所以：① URL 得拼对 ② 图挂了得有替身，两条都要断言。 */
  const spec0 = Array.from(D.querySelectorAll('.blk-thumb'))[0]
    ? decodeURIComponent(String(Array.from(D.querySelectorAll('.blk-thumb'))[0]
      .getAttribute('src')).split('spec=')[1]) : '';
  checkTrue('★ 单块 spec 格式 = 页号:四个坐标（后端按它裁）',
    /^\d+:-?\d+,-?\d+,-?\d+,-?\d+$/.test(spec0), spec0);
  checkTrue('★ 缩略图 <img> 上有 @error 兜底（旧进程 404 时不至于一片破图）',
    /@error="onThumbError/.test(html), true);
  vm.onThumbError(vm.blocks[0]);
  await sleep(60);
  check('★ 图挂了 → 那一块换成说明文字（不是留个破图图标）',
    D.querySelectorAll('.blk-thumb-fail').length, 1);
  check('★ 只换坏的那一张，别的照常显示',
    D.querySelectorAll('.blk-thumb').length, 4);
  vm.thumbBroken = {};
  await sleep(60);
  check('★ 重开一份 → 「图挂了」的记录清掉（不带到下一份）',
    D.querySelectorAll('.blk-thumb-fail').length, 0);

  /* ============ 5. 色带按 0~1000 算（不是 pt） ============ */
  const bands = Array.from(D.querySelectorAll('.blk-band'));
  checkTrue('★ 色带上屏了（页 1 的 3 段 + 页 2 的 2 段）', bands.length >= 5, bands.length);
  const tops = bands.map((el) => String(el.style.top));
  checkTrue('★ 色带位置按 0~1000 归一化（top 是百分比，不是 pt 像素）',
    tops.every((t) => /%$/.test(t)), tops.slice(0, 4));
  checkTrue('页 1 第二段的 top 是 32%（=320/1000）', tops.includes('32%'), tops);

  /* ============ 6. 点「不判」：只改标记，不删框 ============ */
  const beforeBlocks = vm.blocks.length;
  vm.toggleSkip(0);                       // 第 1 块 = 页 1 顶部那段
  await sleep(200);
  check('★ 点「不判」→ 这一块标上（跨页块是整块一起）',
    vm.blocks[0].skipped, true);
  check('★ 不判**不删框**（块数不变）', vm.blocks.length, beforeBlocks);
  check('现在有 2 块不判', vm.blocks.filter((b) => b.skipped).length, 2);
  check('卡片上出现第二个 .skipped', D.querySelectorAll('.blk-card.skipped').length, 2);

  /* ============ 7. 拖拽 / ↑↓ 换位 ============ */
  const firstKey = vm.blocks[0].segKeys[0];
  vm.shiftBlock(0, 1);
  await sleep(150);
  check('★ ↑↓ 换位真的换了（不是只改了个数字）',
    vm.blocks[1].segKeys[0], firstKey);
  vm.shiftBlock(1, -1);
  await sleep(150);
  check('换回来', vm.blocks[0].segKeys[0], firstKey);

  /* ============ 8. 并入上一题 ============ */
  const nBefore = vm.blocks.length;
  vm.mergePrev(1);
  await sleep(200);
  check('★ 「并入上一题」→ 题数少一个', vm.blocks.length, nBefore - 1);
  check('★ 并出来的那块是 2 段', vm.blocks[0].regions.length, 2);

  /* ============ 9. 保存分割：seq 按块重排 ============ */
  const before = CALLS.filter((c) => c.url.endsWith('/regions')).length;
  await vm.saveRegions();
  await sleep(300);
  /* ★ 必须是 PUT：后端 `@router.put("/homework-grades/{gid}/regions")`。
   *   写 POST 真机会 405 ——「分割没存上：Method Not Allowed」，2026-10-20 实际踩到。 */
  const puts = CALLS.filter((c) => c.url.endsWith('/regions') && c.method === 'PUT');
  check('保存分割 → PUT /regions', puts.length, before + 1);
  /* ⚠️ 上面那条万一红了（方法写错 → puts 为空），这里必须还能往下走 ——
   *   否则一崩就看不到后面的断言，等于「只报第一个错」。 */
  const lastPut = puts[puts.length - 1] || { body: {} };
  const sent = (lastPut.body || {}).regions || [];
  checkTrue('整份替换：显式声明 grouped（跨页题才不会被拆）',
    (lastPut.body || {}).grouped === true, (lastPut.body || {}).grouped);
  check('★ 段的条数 = 所有块的所有段（一块不多一块不少）',
    sent.length, vm.blocks.reduce((n, b) => n + b.regions.length, 0));
  checkTrue('★ 每段都带 skipped（不是在界面上标着就算完）',
    sent.every((r) => typeof r.skipped === 'boolean'), sent[0]);
  check('★ 第 1 块是 2 段 → 这 2 段共用同一个 seq（后端靠它竖拼）',
    sent.filter((r) => r.seq === 0).length, 2);
  /* ⚠️ 这里不能硬编码「2 段」。第 8 步把第 2 块并进了第 1 块，而合并后那一块
   * 的 skipped 按 `every` 算（**所有段都标了不判才算不判**）—— 刚并进来的段没标，
   * 所以整块回到「要判」。这是对的（老师合并就该重新确认这块判不判），
   * 断言要跟着用计算值，不然测的是「我以为的语义」而不是代码的语义。 */
  check('★ 上传的 skipped 段数 = 现在仍标着不判的那些块的段数',
    sent.filter((r) => r.skipped).length,
    vm.blocks.filter((b) => b.skipped).reduce((n, b) => n + b.regions.length, 0));
  check('存完就不脏了（按钮回到「已保存」）', vm.dirty, false);

  /* ============ 10. 每页尾部：结束线 ============ */
  check('默认切到页底（老数据零变化）', vm.endOf(1), 1000);
  check('默认不画页脚带', vm.tailBand(1), null);
  vm.setEndOf(1, 960);
  await sleep(200);
  check('★ 结束线记下了', vm.endOf(1), 960);
  check('★ 页 1 末段底边缩到 960（下面 4% 不进任何块）',
    vm.bandsOf(1).slice(-1)[0].box[3], 960);
  check('★ 页脚斜纹带上屏', D.querySelectorAll('.tail-hint').length, 1);
  // 太靠上要拦住：页 1 最后一条线在 760，切到 770 只剩 10 单位 → 后端 422
  vm.setEndOf(1, 770);
  await sleep(150);
  check('★ 结束线贴到最后一条线上 → 拦住，不改', vm.endOf(1), 960);
  vm.setEndOf(1, 1000);
  await sleep(150);
  check('点「到页底」→ 回到 1000', vm.endOf(1), 1000);
  check('斜纹带跟着消失', D.querySelectorAll('.tail-hint').length, 0);

  /* ============ 11. 本页不判（立即落库，不跟保存走） ============ */
  const skBefore = CALLS.filter((c) => c.url.endsWith('/skipped-pages')).length;
  await vm.togglePageSkip(3);
  await sleep(250);
  const skCalls = CALLS.filter((c) => c.url.endsWith('/skipped-pages'));
  check('★ 本页不判走独立接口（不跟着「保存分割」走）', skCalls.length, skBefore + 1);
  check('★ 传的是这一页的页号', skCalls[skCalls.length - 1].body, { pages: [3] });
  check('这一页立刻标上了', vm.pageSkipped(3), true);
  check('★ 卷面上压一层灰（别让它看起来还在送 AI）',
    D.querySelectorAll('.page-box.page-off').length, 1);
  await vm.togglePageSkip(3);
  await sleep(250);
  const sk2 = CALLS.filter((c) => c.url.endsWith('/skipped-pages'));
  check('★ 再点一次 → 整份替换成空（不是又追加一遍）', sk2[sk2.length - 1].body, { pages: [] });
  check('恢复后这页又要判了', vm.pageSkipped(3), false);

  /* ============ 12. 画一条线 ============ */
  const nLines = (vm.dividers[1] || []).length;
  vm.addDivider(1, 500);
  await sleep(200);
  check('★ 画线 → 线多了一条', (vm.dividers[1] || []).length, nLines + 1);
  check('★ 切出来的块也多了（页 1 从 3 段变 4 段）', vm.bandsOf(1).length, 4);
  check('画完就标脏了（按钮变成「保存分割」）', vm.dirty, true);
  // 离已有的线太近要拦住（否则切出 20 单位以下的段 → 后端 422）
  vm.addDivider(1, 505);
  await sleep(120);
  check('离已有线太近 → 不加', (vm.dividers[1] || []).length, nLines + 1);
  vm.removeDivider(1, (vm.dividers[1] || []).indexOf(500));
  await sleep(150);
  check('点 × 删线 → 回到 3 段', vm.bandsOf(1).length, 3);

  /* ============ 13. 批改页签：结果卡片 ============ */
  vm.tab = 'review';
  await sleep(500);
  const gcards = D.querySelectorAll('.g-card');
  check('★ 3 道题都列出来了', gcards.length, 3);
  checkTrue('★ 题干公式真的渲染了（.katex ≥ 2）',
    D.querySelectorAll('.katex').length >= 2, D.querySelectorAll('.katex').length);
  checkTrue('★ 容器里不再残留 $…$ 原文（= 渲染真生效）',
    !D.querySelector('.g-card .g-rich').textContent.includes('$x^2'),
    D.querySelector('.g-card .g-rich').textContent.slice(0, 40));

  /* ★★ 题干 / 学生作答**两个都得填**（用户 2026-10-20 报「没展示出来」）。
   *   这两块是 `setRich` 存 ref、`paintOne` 写 innerHTML 的，而 `paintOne` 靠 key
   *   回查是哪一题 —— key 里带的是 **item.id**，真库里是 12 位十六进制。
   *   那段正则一旦写成 `(\d+)`，十六进制 id 一个都匹配不上 → 两个框全空，
   *   而库里明明有内容。所以下面断言**逐卡**查「题干框」和「作答框」都有东西。 */
  const richOf = (card) => Array.prototype.slice.call(card.querySelectorAll('.g-rich'))
    .map((e) => (e.innerHTML || '').trim());
  const r0 = richOf(gcards[0]);
  checkTrue('★ 第 1 题：题干框有内容', r0.length > 0 && r0[0].length > 0,
    JSON.stringify((r0[0] || '').slice(0, 40)));
  checkTrue('★ 第 1 题：学生作答框有内容', r0.length > 1 && r0[1].length > 0,
    JSON.stringify((r0[1] || '').slice(0, 40)));
  const r1 = richOf(gcards[1]);
  checkTrue('★ 第 2 题：题干框有内容', r1.length > 0 && r1[0].length > 0,
    JSON.stringify((r1[0] || '').slice(0, 40)));
  checkTrue('★ 第 2 题：学生作答框有内容', r1.length > 1 && r1[1].length > 0,
    JSON.stringify((r1[1] || '').slice(0, 40)));
  /* 第 3 题题干本来就是空的（框里没读到题）→ 必须给「（空）」占位，
     不能留一个什么都不写的白框（老师会以为页面坏了）。 */
  checkTrue('★ 第 3 题题干为空 → 给「（空）」占位而不是白框',
    (richOf(gcards[2])[0] || '').includes('（空）'),
    JSON.stringify((richOf(gcards[2])[0] || '').slice(0, 40)));
  check('判定徽章上屏（3 种不同判定）',
    D.querySelectorAll('.g-verdict').length, 3);
  check('★ 模型说「这块不像一道题」→ 有一行提示（不自动跳过）',
    D.querySelectorAll('.g-note').length, 2);   // 1 个 block_kind + 1 个 tail_note
  checkTrue('提示里写清是哪一种（header → 卷头 / 页眉）',
    body().includes('卷头 / 页眉'), true);
  checkTrue('★ tail_note 原样显示 + 告诉老师该怎么办',
    body().includes('邻题的残字') && body().includes('末段底边'), true);
  check('判不了的那一题给了原因（不能只留一个灰点）',
    D.querySelectorAll('.g-un').length, 1);

  /* ============ 13b. 模型给的分类标签（0031） ============ */
  /* 用户原话：「ai输出中要附带对这道题的标签(例如:学段\知识点\体系\考法等)」。
   * 三个容易做错的点：
   *   ① **顺序必须固定**。JSON 的键序跨语言（Python → JSON → JS）之后不保证，
   *      直接遍历 Object.keys() 会让四个标签的先后跳来跳去。用 LABEL_ORDER 数组。
   *   ② **空值不能渲染成半个标签**（「学段·」比没有还难看）。
   *   ③ **模型多给的键要丢掉**（difficulty 不是这组的，界面上不该出现）。
   *   ④ 没有标签的题（比如框里没读到题）**整行都不显示**，不留空行。 */
  const lb1 = Array.from(gcards[0].querySelectorAll('.g-label')).map((e) => e.textContent.trim());
  check('★ 第 1 题有 4 个标签', lb1.length, 4);
  check('★ 显示顺序固定 = 学段 → 体系 → 知识点 → 考法（不跟 JSON 键序走）',
    lb1.map((t) => t.split('·')[0]), ['学段', '体系', '知识点', '考法']);
  checkTrue('★ 标签带取值（不是只有名字）', lb1[0] === '学段·初中', lb1[0]);
  checkTrue('★ 模型多给的键被丢掉（difficulty 不是这一组的）',
    lb1.every((t) => !/中档|difficulty/.test(t)), lb1);
  checkTrue('★ 空值不渲染成半个标签（没有「空·」这种）',
    lb1.every((t) => !/·\s*$/.test(t)), lb1);
  checkTrue('★ 「学段」有单独的样式（它是唯一能拿来筛的枚举）',
    !!gcards[0].querySelector('.g-label.lb-stage'), true);
  check('第 2 题也有标签', gcards[1].querySelectorAll('.g-label').length, 4);
  check('★ 没标签的题 → 整行都不显示（不留一条空行）',
    gcards[2].querySelectorAll('.g-label').length, 0);

  /* ============ 14. 改判定 ============ */
  const vbtns = D.querySelectorAll('.g-card .g-vbtn');
  checkTrue('每道题都有 4 个判定按钮', vbtns.length >= 4, vbtns.length);
  const patchBefore = CALLS.filter((c) => /\/api\/grade-items\/[0-9a-zA-Z_-]+$/.test(c.url)
    && c.method === 'PATCH').length;
  vbtns[2].click();                        // 「错」
  await sleep(300);
  const patches = CALLS.filter((c) => /\/api\/grade-items\/[0-9a-zA-Z_-]+$/.test(c.url)
    && c.method === 'PATCH');
  check('★ 点判定 → PATCH 到那一题', patches.length, patchBefore + 1);
  check('★ 发的是选中的那个判定（不是别的）',
    (patches[patches.length - 1].body || {}).verdict, 'wrong');

  /* ============ 15. 开始批改 ============ */

  /* ★ 回归：用户 2026-10-19 报的「开始批改没法按」。
   *   真因是 `running` 把 `status === 'queued'` 一律当成在跑 —— 而 queued 就是
   *   「建档了、还没人开批」的初始状态（保存并批改只建档，唯一开批处是本页）。
   *   于是按钮一进来就 disabled、标签还写「排队中」，点不动也没提示。
   *   ⚠️ 这条断言必须**先**把状态摆成 queued 且「没点过」，否则测不出这个 bug。 */
  vm.g = Object.assign({}, vm.g, { status: 'queued', done: 0, items: [] });
  await sleep(10);
  check('★ queued 但老师没点过开始 → 不算「在跑」（按钮必须可点）', vm.running, false);
  check('★ 一次都没批过 → 按钮写「开始批改」', vm.runLabel, '开始批改');
  /* 批过的记录写「重新批改」——这个判断**看 items/done，不看 status**：
     queued 的记录也可能带着上一次的结果（重批），status 说不了这件事。 */
  vm.g = Object.assign({}, vm.g, { done: 2 });
  await sleep(10);
  check('★ 已经判过题 → 按钮写「重新批改」（status 是 queued 也一样）',
    vm.runLabel, '重新批改');
  vm.g = Object.assign({}, vm.g, { done: 0 });
  const runBtn = Array.prototype.slice.call(D.querySelectorAll('.el-button'))
    .filter((b) => /开始批改|重新批改|批改中/.test(b.textContent))[0];
  checkTrue('★ 那按钮真的在页面上、而且没被禁用',
    !!runBtn && !/is-disabled/.test(runBtn.className),
    runBtn ? runBtn.className : '没找到按钮');
  checkTrue('★ 状态标签写的是「未开始」，不是「排队中」',
    body().includes('未开始'), true);

  /* ★ 画完线直接点开始批改：必须**先把分割存上**，否则批的是库里旧分区
   *   （老师会觉得「我切的没用」）。 */
  const regBefore = CALLS.filter((c) => c.url === '/api/homework-grades/7/regions'
    && c.method === 'PUT').length;
  const blocksBefore = vm.blocks.length;
  checkTrue('手上还有题块可存', blocksBefore > 0, blocksBefore);
  vm.markDirty();
  check('画过线 → dirty', vm.dirty, true);

  const runBefore = CALLS.filter((c) => c.url === '/api/homework-grades/7/run').length;
  await vm.runGrade();
  await sleep(300);
  check('★ 有没保存的分割 → 先 PUT /regions（后端是 PUT，不是 POST）',
    CALLS.filter((c) => c.url === '/api/homework-grades/7/regions'
      && c.method === 'PUT').length, regBefore + 1);
  check('★ 存完才开批（分割没丢）', vm.blocks.length, blocksBefore);
  check('★ 存成功后 dirty 放下（按钮不再写「保存分割」）', vm.dirty, false);
  check('★ 开始批改 → POST /run', CALLS.filter((c) => c.url === '/api/homework-grades/7/run')
    .length, runBefore + 1);
  check('★ 点过之后即使状态还是 queued 也算在跑（服务端来不及写 running）', vm.running, true);
  check('★ 按钮跟着变「批改中…」', vm.runLabel, '批改中…');
  checkTrue('★ 只合并进度字段，**不动**还没保存的分割（刚画的线不能被冲掉）',
    Array.isArray(vm.blocks) && vm.blocks.length > 0, vm.blocks.length);
  vm.stopPoll();                           // 别让定时器挂住进程

  /* ★ 没开起来要能恢复：/run 报错时旗必须放下，否则按钮永远是灰的、
   *   老师只能刷新页面（这也是「没法按」的另一种形态）。 */
  FAIL_RUN_ONCE = true;
  await vm.runGrade();
  await sleep(200);
  check('★ 开批失败 → 「点过」的旗放下', vm.runAsked, false);
  check('★ 失败后不再算在跑（按钮能重点）', vm.running, false);
  vm.stopPoll();

  /* ★ 服务端一直不接手（进程重启把任务丢了）：90 秒后自己收场，
   *   不能让按钮灰一辈子。这条走的是定时器，测试里不真等 45 轮 ——
   *   断言兜底分支还在（删掉它就等于把这个死胡同还回去）。 */
  checkTrue('★ 一直卡在 queued 时有兜底（放开按钮 + 提示）',
    /_queuedTicks\s*>=?\s*45/.test(html) && html.includes('没开起来'), true);

  /* ★ 换一份记录：「点过」的旗必须跟着重置，否则上一份的旗会把新的 queued
   *   记录顶成「批改中」。 */
  vm.runAsked = true;
  await vm.loadGrade(7);
  await sleep(200);
  check('★ 换记录 → 「点过开批」的旗跟着重置', vm.runAsked, false);
  vm.stopPoll();

  /* ============ 16. 请求方法与真后端对账（这次 405 的根治） ============ */
  /* ★★ 这条是全局防线：桩会按后端源码里的 `@router.<method>("path")` 校验，
   *   路径对、方法错就回 405 并记进 BAD_METHOD。产品代码里任何一个方法写错，
   *   这里都会红 —— 不再需要靠人记得「regions 是 PUT」。 */
  checkTrue('★ 全程没有一个请求打到「路径对、方法错」的路由（真机会 405）',
    BAD_METHOD.length === 0, BAD_METHOD);
  /* ★ 防空转：读不到后端源码 / 正则没匹配上时，上面那条永远绿 —— 必须先证明
   *   表真的是读出来的，而且 regions 那条读对了方法。 */
  checkTrue('★ 后端路由表真读到了（不是空表导致的假绿）',
    Object.keys(BACKEND_ROUTES).length >= 15, Object.keys(BACKEND_ROUTES).length);
  /* 这条路径同时有 GET（读现有分割）和 PUT（整份替换）——**就是没有 POST**，
     写 POST 的那个就是这次的「Method Not Allowed」。 */
  check('★ 路由表里 /regions = GET + PUT，没有 POST',
    (BACKEND_ROUTES['/homework-grades/*/regions'] || []).slice().sort(), ['GET', 'PUT']);

  /* ============ 17. 终止批改 ============ */
  /* 一页 14 题约 2.5 分钟，一份 6 页的作业十几分钟。跑起来发现不对（分割切错了、
   * 模型配置不对）却没有「停」，只能刷新页面 —— 而刷新之后线程还在跑。
   * 三个必须守住的点：
   *   ① 只在跑批时出现（其他时候摆个按钮是噪音）
   *   ② 点一下就停（**不做二次确认**：太慢时按终止，那一刻最不想读的是对话框）
   *   ③ 后端是「受理」不是「立刻停」—— 所以既不能自己把 running 置 false，
   *      也不能点完立刻把按钮放开（放开了老师会再点 → 后端 409） */
  vm.g = Object.assign({}, vm.g, { status: 'reviewing' });
  vm.runAsked = false;
  await sleep(80);
  const cancelBtns = () => Array.prototype.slice.call(D.querySelectorAll('.el-button'))
    .filter((b) => b.textContent.trim() === '终止');
  check('★ 没在跑的时候不出现「终止」按钮', cancelBtns().length, 0);

  vm.runAsked = true;
  vm.g = Object.assign({}, vm.g, { status: 'running' });
  // ⚠️ 桩判「是不是真的在跑」读的是 LAST_DETAIL，只改 vm.g 的话 /cancel 照样回 409
  LAST_DETAIL.status = 'running';
  await sleep(120);
  check('★ 批改中 → 出现「终止」按钮', cancelBtns().length, 1);

  const nConfBefore = CONFIRMS.length;
  const nMsgBefore = MSGS.length;
  const cancelBefore = CALLS.filter((c) => c.url === '/api/homework-grades/7/cancel').length;
  cancelBtns()[0].click();
  await sleep(300);
  /* ★★ 终止**刻意不做二次确认**：批改太慢时按下终止，那一刻最不想做的事就是
   *   再读一个对话框。损失可控（已批的页保留），而每多一层弹窗就多一分
   *   「点了没反应」的错觉 —— 用户说的「无法顺利进行」正是这个。 */
  check('★★ 点「终止」→ 不弹确认框，直接发请求', CONFIRMS.length, nConfBefore);
  check('★ 只发了一个 /cancel', CALLS.filter((c) =>
    c.url === '/api/homework-grades/7/cancel').length, cancelBefore + 1);
  checkTrue('★ 受理了要给一句明确反馈（不然老师以为没点到）',
    /已请求终止/.test((MSGS[MSGS.length - 1] || {}).text || ''),
    (MSGS[MSGS.length - 1] || {}).text);
  check('★ 反馈是 success 不是 error', (MSGS[MSGS.length - 1] || {}).type, 'success');

  /* ★★ 受理之后必须把按钮「锁住」。立刻放开的话老师会再点一次，
   *   而后端已经在收尾了 —— 第二次只会收到 409 甚至更糟的提示。 */
  cancelBtns()[0].click();
  await sleep(250);
  check('★★ 受理后再点同一按钮 → 不再发第二个请求（cancelAsked 挡住）',
    CALLS.filter((c) => c.url === '/api/homework-grades/7/cancel').length, cancelBefore + 1);
  checkTrue('★ 受理后按钮一直在 loading，不是点完就恢复',
    vm.canceling === true, String(vm.canceling));
  checkTrue('★ 页面写着「正在收尾」（等到真停，不是假装停了）',
    html.includes('正在收尾'), true);

  /* ★★ 只有轮询看到状态真的落地了才放开（桩已经把 status 写成 reviewing）。
   *   tick() 从 setInterval 的匿名函数里抽出来就是为了能在这儿直调 ——
   *   否则要等好几轮 2 秒，测试里根本复现不了。 */
  await vm.tick();
  await sleep(60);
  checkTrue('★★ 轮询看到 reviewing → 才放开终止按钮',
    vm.canceling === false && vm.cancelAsked === false,
    'canceling=' + vm.canceling + ' cancelAsked=' + vm.cancelAsked);
  vm.stopPoll();

  /* ★★ 批改刚好在这一瞬间跑完了 → 后端给 200 + already_stopped。那是**成功**
   *   不是失败：老师的目的是「别再跑了」，目的达到了；弹红字只会让他以为按钮坏了。 */
  vm.cancelAsked = false;
  vm.canceling = false;
  CANCEL_MODE = 'stopped';
  const nMsg2 = MSGS.length;
  await vm.cancelGrade();
  await sleep(250);
  check('★ 已经停了 → 也给反馈（没有静默失败）', MSGS.length, nMsg2 + 1);
  checkTrue('★★ 已经停了 → 不弹红色错误（type 不是 error）',
    (MSGS[MSGS.length - 1] || {}).type !== 'error',
    (MSGS[MSGS.length - 1] || {}).type + ' / ' + (MSGS[MSGS.length - 1] || {}).text);
  CANCEL_MODE = '';
  vm.stopPoll();

  /* ★ 后端说「真没在跑」→ 409，前端要把 detail 里的理由说给老师听。 */
  vm.g = Object.assign({}, vm.g, { status: 'reviewing' });
  vm.runAsked = false;
  vm.cancelAsked = false;
  await sleep(80);
  const nMsg = MSGS.length;
  await vm.cancelGrade();
  await sleep(200);
  /* 产品里失败是 `ElMessage.error(...)`,**不往外抛** —— 所以断言要看文案，
     不能 try/catch（catch 到的是空的，那种断言永远绿，等于没写）。 */
  check('★ 没在跑时点终止 → 提示了（没静默失败）', MSGS.length, nMsg + 1);
  checkTrue('★ 提示写的是「没在批改」这句人话（不是干巴巴一个 409）',
    /没在批改/.test((MSGS[MSGS.length - 1] || {}).text || ''),
    (MSGS[MSGS.length - 1] || {}).text);
  vm.stopPoll();

  /* ============ 18. 手写批改（笔 / 橡皮 / 文字 / √× / 导出） ============ */
  /* 用户 2026-10-20 原话：「文件上也要能够笔画，橡皮，插入文本框，导出批改」。
   * 这一段最要紧的不是「按钮在不在」，而是三件容易做错的事：
   *   ① **先落本机再发请求** —— 画到一半退出不能丢（用户 10-19 的原话）
   *   ② **line / rect 模式下标注层不能抢指针** —— 分割那套交互已经调好并测过
   *   ③ **清空只清老师自己画的** —— 把 AI 判的框一起删了，老师得重新批一遍 */
  const toolBtns = (txt) => Array.prototype.slice.call(D.querySelectorAll('.el-radio-button'))
    .filter((b) => b.textContent.trim() === txt);
  check('★ 工具条上有「笔」', toolBtns('✏️ 笔').length, 1);
  check('★ 工具条上有「橡皮」', toolBtns('🧽 橡皮').length, 1);
  check('★ 工具条上有「文字」', toolBtns('🔤 文字').length, 1);
  check('★ 工具条上有 √ / × 快放（判对错是最高频的动作）',
    toolBtns('✔').length + toolBtns('✘').length, 2);

  /* ★ 模式归一化：只有手写类工具才把指针交给标注层，其余一律 view。
   *   不改这条的话，切到「画横线」时标注层会整层 pointer-events:auto，
   *   把工作台的画线/拖线全吃掉 —— 分割直接画不动。 */
  vm.tool = 'ink'; await sleep(30);
  check('★ 切到「笔」→ 标注层进 ink 模式', vm.layerMode, 'ink');
  vm.tool = 'erase'; await sleep(30);
  check('切到「橡皮」→ erase 模式', vm.layerMode, 'erase');
  vm.tool = 'line'; await sleep(30);
  check('★★ 切到「画横线」→ 标注层退回 view（不能抢分割的指针）', vm.layerMode, 'view');
  vm.tool = 'rect'; await sleep(30);
  check('★★ 切到「框选」→ 标注层退回 view', vm.layerMode, 'view');

  checkTrue('★ 标注层加载完了才挂（marksReady）', vm.marksReady === true, String(vm.marksReady));
  /* ⚠️ 卷面只在「分割」页签里渲染（批改页签是逐题卡）—— 前面的小节把 tab 停在 review 了，
   *   不切回来数到的就是 0，看着像「标注层没挂上」。 */
  vm.tab = 'blocks';
  await sleep(120);
  check('★ 3 页 → 挂了 3 个标注层（每页一份，各自只认自己那页）',
    D.querySelectorAll('.gmark-layer').length, 3);
  check('★ 标注层用的是 0~1000 的 viewBox（不存像素）',
    (D.querySelector('.gmark-svg') || {}).getAttribute
      ? D.querySelector('.gmark-svg').getAttribute('viewBox') : '', '0 0 1000 1000');

  /* ---- 画一笔 ---- */
  vm.tool = 'ink'; await sleep(30);
  const nMarkPost = () => CALLS.filter((c) => c.url === '/api/homework-grades/7/marks').length;
  const m0 = nMarkPost();
  await vm.addMark(2, { kind: 'ink', geo: [100, 200, 300, 400], points: [100, 200, 300, 400],
    color: '#dc2626' });
  await sleep(120);
  check('★ 画一笔 → POST /marks', nMarkPost(), m0 + 1);
  const lastMark = CALLS.filter((c) => c.url === '/api/homework-grades/7/marks').pop();
  check('★ 带上页号（多页卷子上落错页就是画到别人卷子上）',
    lastMark ? lastMark.body.page_no : 0, 2);
  check('★ 坐标原样发出去（0~1000，不折算成像素）',
    lastMark ? lastMark.body.geo : [], [100, 200, 300, 400]);
  check('★ 存下来之后本机草稿出队了（不重复画一遍）',
    vm.localMarks.filter((m) => Number(m.page_no) === 2).length, 0);
  check('★ 服务端那边真的多了一条（不只是发过请求）',
    (vm.g.marks || []).filter((m) => Number(m.page_no) === 2).length, 1);

  /* ---- √ 快放 ---- */
  await vm.addMark(2, { kind: 'check', geo: [300, 400, 344, 444], color: '#16a34a' });
  await sleep(120);
  check('★ √ 快放也是同一条通道（kind 不同而已）',
    (vm.g.marks || []).filter((m) => m.kind === 'check').length, 1);

  /* ---- ★ 断网时笔迹不能丢 ---- */
  const FAIL_MARK_ONCE = () => { W.__failMarkOnce = true; };
  const origFetch = W.fetch;
  FAIL_MARK_ONCE();
  W.fetch = async (u, o) => {
    if (String(u) === '/api/homework-grades/7/marks' && String((o || {}).method) === 'POST'
      && W.__failMarkOnce) {
      W.__failMarkOnce = false;
      return { ok: false, status: 500, json: async () => ({ detail: '网络断了' }) };
    }
    return origFetch(u, o);
  };
  await vm.addMark(1, { kind: 'ink', geo: [10, 10, 60, 60], points: [10, 10, 60, 60] });
  await sleep(150);
  W.fetch = origFetch;
  check('★★ 传不上去时那一笔**留在本机**（不是消失了）',
    vm.localMarks.filter((m) => Number(m.page_no) === 1).length, 1);
  checkTrue('★★ 并且告诉老师「已经存在本机」（不然他以为画上了）',
    /存在本机/.test((MSGS[MSGS.length - 1] || {}).text || ''),
    (MSGS[MSGS.length - 1] || {}).text);
  /* 补发：批量一次发，不是一笔一个请求 */
  const nBatch = CALLS.filter((c) => c.url === '/api/homework-grades/7/marks/batch').length;
  const flushed = await vm.flushPending();
  await sleep(150);
  check('★★ 下次补发时把那一笔送上去', flushed, 1);
  check('★ 补发走**批量**接口（画了三十笔不会发三十次）',
    CALLS.filter((c) => c.url === '/api/homework-grades/7/marks/batch').length, nBatch + 1);
  check('★ 补发成功后本机队列清空', vm.localMarks.length, 0);

  /* ---- 橡皮：本地的与服务器的分开处理 ---- */
  const remoteIds = (vm.g.marks || []).filter((m) => Number(m.page_no) === 2).map((m) => m.id);
  checkTrue('页 2 上服务端有 2 条', remoteIds.length === 2, JSON.stringify(remoteIds));
  const nDel = () => CALLS.filter((c) => /^\/api\/grade-marks\/\d+$/.test(c.url)
    && c.method === 'DELETE').length;
  const d0 = nDel();
  /* ⚠️ `local:` 开头的还没传上去，**只能从本机队列里删** —— 发给服务端会 404，
   *   而且那条笔迹还留在界面上，老师会以为橡皮坏了。 */
  await vm.eraseMarks(['local:abc123'].concat(remoteIds.map(String)));
  await sleep(200);
  check('★★ 橡皮：只有服务端那几条发了 DELETE（local: 的不发）', nDel(), d0 + remoteIds.length);
  check('★★ 擦完之后服务端那两条没了',
    (vm.g.marks || []).filter((m) => Number(m.page_no) === 2).length, 0);

  /* ---- 文字框 ---- */
  vm.onTextAt(3, { x: 420, y: 260 });
  await sleep(60);
  checkTrue('★ 在卷面上点一下 → 弹出文字框', vm.textOpen === true, String(vm.textOpen));
  check('★ 文字框记住了点的位置（放上去就在那儿）',
    [vm.textDraft.page, vm.textDraft.x, vm.textDraft.y], [3, 420, 260]);
  vm.textDraft.text = '这一步漏了讨论 x=0';
  await vm.saveText();
  await sleep(150);
  check('★ 文字框内容存上去了（kind=text）',
    (vm.g.marks || []).filter((m) => m.kind === 'text' && m.text === '这一步漏了讨论 x=0').length, 1);
  checkTrue('★ 存完弹框关掉', vm.textOpen === false, String(vm.textOpen));
  /* 空内容不放一个空框上去 */
  vm.onTextAt(3, { x: 10, y: 10 });
  vm.textDraft.text = '   ';
  const nText = (vm.g.marks || []).length;
  await vm.saveText();
  await sleep(120);
  check('★ 评语是空的 → 不放一个空框上去', (vm.g.marks || []).length, nText);

  /* ---- 清空本页：只清老师自己画的 ---- */
  /* AI 画的框 created_by 不是 teacher —— 一起删了老师就得重新批一遍。 */
  LAST_DETAIL.marks.push({
    id: 901 + (LAST_MARK_ID += 1), page_no: 3, kind: 'box', geo: [0, 0, 1000, 1000],
    points: [], text: '', color: '#2563eb', created_by: 'ai', item_id: 'a1b2c3d4e5f6',
  });
  await vm.settle();
  await sleep(80);
  const p3 = vm.marksOf(3);
  checkTrue('页 3 上既有老师画的也有 AI 画的',
    p3.some((m) => m.created_by === 'teacher') && p3.some((m) => m.created_by === 'ai'),
    p3.map((m) => m.created_by).join(','));
  check('★ 但「清空本页」只数老师画的', vm.teacherMarksOf(3).length, 1);
  const c0 = CONFIRMS.length;
  await vm.clearPageMarks(3);
  await sleep(250);
  check('★ 清空之前先问一句（不是点了就没了）', CONFIRMS.length, c0 + 1);
  checkTrue('★ 确认框里写清了「AI 的框不动」',
    /AI 的框不动/.test((CONFIRMS[CONFIRMS.length - 1] || {}).msg || ''),
    (CONFIRMS[CONFIRMS.length - 1] || {}).msg);
  check('★★ 清空之后：老师画的没了，(vm 侧) AI 的框还在',
    vm.marksOf(3).filter((m) => m.created_by === 'teacher').length, 0);
  checkTrue('★★ AI 画的框真的还在（重新批一遍的代价太大）',
    vm.marksOf(3).some((m) => m.created_by === 'ai'),
    vm.marksOf(3).map((m) => m.created_by).join(','));

  /* ---- 拖动 ---- */
  const mv = vm.marksOf(3).find((m) => m.created_by === 'ai');
  const nPatch = () => CALLS.filter((c) => /^\/api\/grade-marks\/\d+$/.test(c.url)
    && c.method === 'PATCH').length;
  const p0 = nPatch();
  await vm.moveMark({ mark: mv, geo: [0, 10, 1000, 20] });
  await sleep(150);
  check('★ 拖动 → PATCH 一次（松手才发，不是跟着手一直发）', nPatch(), p0 + 1);
  check('★ 发的是拖到的新位置',
    (CALLS.filter((c) => /^\/api\/grade-marks\/\d+$/.test(c.url) && c.method === 'PATCH')
      .pop() || {}).body.geo, [0, 10, 1000, 20]);

  /* ---- 点标注跳到它所属的题 ---- */
  vm.tab = 'blocks';
  await sleep(30);
  vm.onPickMark({ item_id: 'a1b2c3d4e5f6' });
  await sleep(80);
  check('★ 点 AI 画的框 → 跳到它所属那道题', vm.selItem, 'a1b2c3d4e5f6');
  check('★ 并且切到「批改」页签（不然跳过去看不见）', vm.tab, 'review');
  vm.selItem = null;
  vm.onPickMark({ item_id: null });
  await sleep(60);
  check('★ 老师自己画的（没有 item_id）点了不跳', vm.selItem, null);

  /* ---- 导出 ---- */
  /* ⚠️ 导出走 `<a>` 点击下载，**不经过 fetch**：几十页的 PDF 走 fetch 会先在内存里
   *  攒一遍再组装 blob，慢机器上直接卡住。所以这里断言的是 `<a href>` 而不是请求。 */
  const clicked = [];
  const origCreateA = D.createElement.bind(D);
  D.createElement = (tag) => {
    const el = origCreateA(tag);
    if (String(tag).toLowerCase() === 'a') {
      const origClick = el.click.bind(el);
      el.click = () => { clicked.push(String(el.getAttribute('href'))); return origClick(); };
    }
    return el;
  };
  await vm.exportFile('pdf');
  await sleep(150);
  D.createElement = origCreateA;
  check('★★ 导出 → 拼出 /export?fmt=pdf（原件不动，笔迹烧在副本上）',
    clicked.length ? clicked[clicked.length - 1] : '', '/api/homework-grades/7/export?fmt=pdf');
  checkTrue('★★ 导出前先把本机队列补发一次（老师刚画的不能漏在 PDF 外）',
    /async exportFile[\s\S]{0,400}await this\.flushPending\(\)/.test(html), true);
  checkTrue('★ 导出按钮在顶栏上', /导出 PDF/.test(html), true);
  checkTrue('★ 没有页图时先拦一句（点了没反应最难受）',
    /还没有页图/.test(html), true);

  /* ---- 兜底：标注层没加载起来时，其余功能照常 ---- */
  checkTrue('★★ 标注层加载失败只是没有手写功能（页面其余部分照常挂）',
    /catch \(e\) \{\s*console\.error\('\[workbench\] 手写标注层没加载起来/.test(html), true);
  checkTrue('★ 模板里组件是成对写的（零构建里自闭合会吞掉后面的节点）',
    /<grade-mark-layer[\s\S]{0,600}>\s*<\/grade-mark-layer>/.test(html), true);

  /* ★★ 自证：**组件注册必须排在 mount 之前**。
   *
   *   运行时编译器把 `<grade-mark-layer>` 编成渲染函数顶部的一句
   *   `const _component_grade_mark_layer = _resolveComponent("grade-mark-layer")`；
   *   而它所在的 `<el-card>` 默认插槽被标了 `_: 1`（SlotFlags.STABLE）——
   *   **稳定插槽更新时不会被替换**，于是 `el-card` 一直调用第一次渲染那个闭包，
   *   而闭包是把这个 const **按值**捕获的（`blocks` / `dividers` 那些是调用时才从
   *   `_ctx` 读，所以它们会更新，唯独组件不会）。
   *
   *   先 mount 再注册 → 捕获到的永远是字符串 → 页面上出现一个空的同名标签，
   *   笔迹一条画不上，控制台一声不响（2026-10-20 排查了两个小时）。
   *   这条断言就是守住这个顺序 —— 换回去会立刻变红。 */
  checkTrue('★★★ 组件注册排在 mount 之前（晚了就永远是个空标签，且不报错）',
    html.indexOf("app.component('grade-mark-layer'") > 0
    && html.indexOf("app.component('grade-mark-layer'") < html.indexOf("app.mount('#app')"),
    html.indexOf("app.component('grade-mark-layer'") + ' < ' + html.indexOf("app.mount('#app')"));
  checkTrue('★★★ import 失败也要 mount（只是没有手写功能）',
    /\} catch \(e\) \{\s*console\.error\('\[workbench\] 手写标注层没加载起来[\s\S]{0,80}\}\s*const root = app\.mount/.test(html),
    true);

  /* ============ 19. ★★ 手写工具必须落在卷面上（用户原话）============
   *  用户 2026-10-20：「笔画、橡皮、文本框等功能点击后只能在文件上画框，
   *  我想要的是在文件上做笔记和人工批改」。
   *
   *  根因：.page-box 上挂了 @pointerdown="onPageDown"，而 onPageDown 里只有
   *  `line` 分支 return，其余工具**一路 fall through 到框选** —— 于是选了笔，
   *  画出来的还是框：标注层压根没收到过事件。
   *  单靠「看代码觉得对」是守不住的（这次就是这么漏掉的），所以要真的派发一次。 */
  vm.tool = 'view';
  const pg1 = D.querySelector('#pg1');
  checkTrue('（准备）卷面容器找得到', !!pg1, String(!!pg1));
  /* jsdom 里所有盒子都是 0 尺寸，而 ohne 尺寸的代码会直接 return —— 必须先把
     盒子撑起来，否则下面两条都会「因为没走到」而假绿。 */
  pg1.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 1131 });

  function press(el, tool) {
    vm.tool = tool;
    vm.draft = null;
    const ev = new W.Event('pointerdown');
    Object.defineProperty(ev, 'clientX', { value: 120 });
    Object.defineProperty(ev, 'clientY', { value: 260 });
    el.dispatchEvent(ev);
    return ev;
  }

  press(pg1, 'rect');
  checkTrue('（对照）选「框选」按下 → 确实开始拖框了', !!vm.draft, String(!!vm.draft));
  vm.draft = null;

  press(pg1, 'ink');
  checkTrue('★★ 选「笔」按下 → **不会**开始拖框（事件要让给标注层）',
    vm.draft === null, String(vm.draft));
  press(pg1, 'erase');
  checkTrue('★★ 选「橡皮」按下 → 不会开始拖框', vm.draft === null, String(vm.draft));
  press(pg1, 'text');
  checkTrue('★★ 选「文字」按下 → 不会开始拖框', vm.draft === null, String(vm.draft));
  press(pg1, 'check');
  checkTrue('★★ 选「√」按下 → 不会开始拖框', vm.draft === null, String(vm.draft));
  vm.tool = 'view';
  vm.draft = null;
  /* ★ 反面自证：把那道放行删掉，「笔」会立刻退化成画框 —— 这条断言的意义就在这。
     不能只写正面断言（上面几条只要 fall through 被删也会绿，但它们删了才是错）。 */
  checkTrue('★★ 源码里确实写了「手写类工具放行」（不是靠运气）',
    /if \(INK_TOOLS\.indexOf\(this\.tool\) >= 0\) return;/.test(html), true);
  checkTrue('★ 放行写在 line 分支**之前**（顺序错了 line 也照样躺枪）',
    html.indexOf("if (INK_TOOLS.indexOf(this.tool) >= 0) return;")
      < html.indexOf("if (this.tool === 'line') {"), true);
  checkTrue('★ 每个页容器都传了 layerMode（手写时才进对应模式，其余 view 不吃事件）',
    /<grade-mark-layer[\s\S]{0,200}mode="layerMode"/.test(html)
      || /:mode="layerMode"/.test(html), true);

  /* ============ 19b. ★★★ 笔迹真的落在卷面上（用户原话）============
   *  用户 2026-10-20：「批改台上画笔没有在文件上画，我没看到笔画」。
   *
   *  这一节以前只验了「按下不会画框」—— 那是「不画错的」，不是「画得出来」。
   *  所以测试全绿、用户却什么都没看到。现在真的派发一次指针事件，
   *  并且**把 DOM 里那条笔迹原样查一遍**（有 stroke 才算看得见）。
   *
   *  当时查出的真因：层的 z-index 是 auto，而 .blk-band 是 2、.divider 5、
   *  .rect-rect 6、.crop-rect 7 —— 半透明色块全压在笔迹上面，红笔迹被染掉，
   *  看起来就跟没画一样。逻辑（DOM / stroke / points）其实一直是好的。 */
  const svgEl = D.querySelectorAll('.gmark-svg')[0];
  checkTrue('（准备）卷面上有标注层的 svg', !!svgEl, String(!!svgEl));
  if (svgEl) {
    svgEl.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 1131 });
    vm.tool = 'ink';
    await sleep(80);
    check('★ 选了笔 → 层进 ink 模式', vm.layerMode, 'ink');
    checkTrue('★ 层的 class 真带上了 mode-ink（CSS 靠它开 pointer-events）',
      /mode-ink/.test(svgEl.parentElement.className), svgEl.parentElement.className);
    checkTrue('★★ svg 的 viewBox / preserveAspectRatio 没丢（丢了坐标就全跑出纸外）',
      svgEl.getAttribute('viewBox') === '0 0 1000 1000'
        && svgEl.getAttribute('preserveAspectRatio') === 'none',
      svgEl.getAttribute('viewBox') + ' / ' + svgEl.getAttribute('preserveAspectRatio'));

    const nG = D.querySelectorAll('.gmark').length;
    const pev = (type, x, y) => {
      const e = new W.Event(type, { bubbles: true });
      Object.defineProperty(e, 'clientX', { value: x });
      Object.defineProperty(e, 'clientY', { value: y });
      Object.defineProperty(e, 'pointerId', { value: 1 });
      Object.defineProperty(e, 'pointerType', { value: 'mouse' });
      return e;
    };
    svgEl.dispatchEvent(pev('pointerdown', 100, 200));
    for (let i = 1; i <= 5; i += 1) {
      svgEl.dispatchEvent(pev('pointermove', 100 + i * 15, 200 + i * 12));
    }
    svgEl.dispatchEvent(pev('pointerup', 175, 260));
    await sleep(220);

    check('★★★ 拖一笔 → DOM 里真的多了一条标注', D.querySelectorAll('.gmark').length, nG + 1);
    const inkPoly = [...D.querySelectorAll('polyline')]
      .filter((p) => /ink/.test(p.getAttribute('class') || '')).pop();
    checkTrue('★★★ 那条笔迹是 <polyline class="shape ink">', !!inkPoly,
      inkPoly ? inkPoly.getAttribute('class') : '没找到');
    checkTrue('★★★ 并且**有 stroke 颜色**（SVG 默认 stroke:none，缺色就等于隐形）',
      !!inkPoly && !!inkPoly.getAttribute('stroke'),
      inkPoly ? inkPoly.getAttribute('stroke') : '');
    checkTrue('★★★ 点的坐标是 0~1000 归一化（不是屏幕像素）',
      !!inkPoly && (() => {
        const nums = String(inkPoly.getAttribute('points') || '')
          .split(/[\s,]+/).filter(Boolean).map(Number);
        return nums.length >= 4 && nums.every((v) => v >= 0 && v <= 1000);
      })(), inkPoly ? inkPoly.getAttribute('points') : '');
    vm.tool = 'view';
    await sleep(40);
  }

  /* ---- ★★★ 层叠顺序：笔迹必须盖在所有辅助层之上 ----
   *  这条守的就是上面那个真因。半透明块带（10~16%）压在 2.2px 的红线上，
   *  在缩略图大小的预览里就等于「什么都没画」。 */
  const zOf = (sel) => {
    const re = new RegExp('\\' + sel + '\\s*\\{[^}]*z-index:\\s*(\\d+)');
    const m = re.exec(html);
    return m ? Number(m[1]) : 0;
  };
  const layerZ = zOf('.gmark-layer');
  checkTrue('★★★ 标注层显式设了 z-index（不设 = auto，被块带/分割线/框全盖住）',
    layerZ > 0, String(layerZ));
  checkTrue('★★★ 比块带高（' + layerZ + ' > ' + zOf('.blk-band') + '）',
    layerZ > zOf('.blk-band'), '');
  checkTrue('★★★ 比分割线 / 框选 / 草框都高（2 / 5 / 6 / 7）',
    layerZ > Math.max(zOf('.divider'), zOf('.rect-rect'), zOf('.crop-rect')),
    String(layerZ));

  /* ============ 19c. 文本框要能移动 ============ */
  /* 用户 2026-10-20：「我还需要文本框可以移动」。
     组件原来只在 view 模式下允许拖，而文字模式下 .gmark-label 还是
     pointer-events:none —— 于是「加了文本框想挪一下」只能先切「只看」，
     或者在文字模式下再点一下（结果又插了一个新框，越点越多）。 */
  checkTrue('★★ 文字模式下已有的文本框能拖（CSS 放开了 label 的指针）',
    /\.gmark-layer\.mode-text \.gmark-label\s*\{[^}]*pointer-events:\s*auto/.test(html), true);
  const uGML = fs.readFileSync('src/components/GradeMarkLayer.js', 'utf8');
  checkTrue('★★ 组件的可拖模式里包含 text',
    /MOVE_MODES\s*=\s*\[\s*'view'\s*,\s*'text'\s*\]/.test(uGML),
    (uGML.match(/MOVE_MODES\s*=\s*\[[^\]]*\]/) || [''])[0]);
  checkTrue('★★ 画笔/橡皮**不**在可拖模式里（那两个模式下按住是落笔，不是抓东西）',
    !/MOVE_MODES\s*=\s*\[[^\]]*'ink'/.test(uGML) && !/MOVE_MODES\s*=\s*\[[^\]]*'erase'/.test(uGML), true);

  /* ============ 20. 一键清理分割 ============ */
  /* 14 页的卷子画歪一条线，以前只能去点那一页线上的 × —— 重来的成本高到
     老师宁愿将就着批。「清空分割」就是给这个场景一个出口。 */
  vm.dividers = { 1: [300, 620], 2: [410] };
  vm.rects = [{ n: 1, page: 1, box: [0, 0, 500, 300] }];
  await sleep(30);
  check('（准备）确认清理前有两页有线', Object.keys(vm.dividers).length, 2);
  check('★ 统计条上能数出有多少处分割（线 + 框一起算）', vm.sliceCount, 4);

  const putBefore2 = CALLS.filter((c) => c.url === '/api/homework-grades/7/regions'
    && c.method === 'PUT').length;
  await vm.clearSlices(0);
  await sleep(120);
  check('★★ 一键清空 → 所有页的线都没了', Object.keys(vm.dividers).length, 0);
  check('★★ 一键清空 → 框也没了（线与框是同一件事的两种画法）', vm.rects.length, 0);
  check('★★ 清空后**立刻落库**（不是只标 dirty，关掉页面还得回来重画）',
    CALLS.filter((c) => c.url === '/api/homework-grades/7/regions'
      && c.method === 'PUT').length, putBefore2 + 1);
  checkTrue('★ 清空后给了一句「已清空」（并且说明了清了些什么）',
    /已清空/.test((MSGS[MSGS.length - 1] || {}).text || ''),
    (MSGS[MSGS.length - 1] || {}).text);

  /* ★ 只清本页：某一页画坏了没必要推倒全部。 */
  vm.dividers = { 1: [300], 2: [410], 3: [200] };
  await sleep(30);
  await vm.clearSlices(2);
  await sleep(120);
  check('★★ 只清第 2 页 → 别的页不动', Object.keys(vm.dividers).slice().sort(), ['1', '3']);

  /* ★★ 存不上必须**回滚**：界面显示清了、库里还在，下次批改用的就是旧分割 ——
     这属于「为什么它不听我的」那一类最难查的错。 */
  FAIL_REGIONS = true;
  vm.dividers = { 1: [300] };
  const backup = JSON.stringify(vm.dividers);
  await vm.clearSlices(0);
  await sleep(120);
  FAIL_REGIONS = false;
  checkTrue('★★ 保存失败 → 内存里的分割**恢复了**（界面与库不会分叉）',
    JSON.stringify(vm.dividers) === backup, JSON.stringify(vm.dividers));
  checkTrue('★ 保存失败要给红字（不能静默）',
    (MSGS[MSGS.length - 1] || {}).type === 'error', (MSGS[MSGS.length - 1] || {}).type);

  /* ★★ 全清是**不可逆**的：`saveRegions` 一成功就写库、没有撤销，误点一次
     老师得从第一条线重画整卷。所以按钮那一层要问一句。
     ⚠️ 只在该层问 —— 按页清（clearSlices(pno)）是局部操作、重画便宜，
        再弹一次只会变成「怎么老是弹窗」。 */
  vm.dividers = { 1: [300, 620], 2: [410] };
  vm.rects = [{ n: 1, page: 1, box: [0, 0, 500, 300] }];
  await sleep(30);
  const nConfirm0 = CONFIRMS.length;
  await vm.askClearSlices();
  await sleep(120);
  check('★★ 一键清空前问了一句（不可逆，误点要重画整卷）',
    CONFIRMS.length, nConfirm0 + 1);
  /* 静态钉住：按钮必须接**确认那一层**。直接 `@click="clearSlices(0)"` 的话，
     上面的运行时断言全还是绿的（它们直接调 askClearSlices），而老师误点照样一把清光。 */
  checkTrue('★★ 按钮接的是 askClearSlices（不是绕过确认直接 clearSlices）',
    /@click="askClearSlices\(\)"/.test(html)
      && !/@click="clearSlices\(0\)"/.test(html), true);
  const lastConfirm = CONFIRMS[CONFIRMS.length - 1] || {};
  checkTrue('★★ 确认框里写清了「清多少」和「笔迹留不留」',
    /3 条分割线/.test(lastConfirm.msg || '') && /1 个框/.test(lastConfirm.msg || '')
      && /笔迹会保留/.test(lastConfirm.msg || ''), lastConfirm.msg);
  check('★ 确认之后才真清', Object.keys(vm.dividers).length, 0);

  /* 点「算了」→ 一个字都不许动。确认框不是装饰。 */
  vm.dividers = { 1: [300, 620], 2: [410] };
  vm.rects = [{ n: 1, page: 1, box: [0, 0, 500, 300] }];
  await sleep(30);
  const origConfirm = W.ElementPlus.ElMessageBox.confirm;
  W.ElementPlus.ElMessageBox.confirm = () => Promise.reject(new Error('cancel'));
  try {
    await vm.askClearSlices();
    await sleep(80);
  } finally {
    W.ElementPlus.ElMessageBox.confirm = origConfirm;
  }
  check('★★ 点了「算了」→ 线一条不少', Object.keys(vm.dividers).length, 2);
  check('★★ 点了「算了」→ 框也还在', vm.rects.length, 1);
  vm.dividers = {};
  vm.rects = [];
  await sleep(20);

  /* ============ 21. 页内新建：上传作业（工作台独立出来）============ */
  /* 用户 2026-10-20：「后续将该工作台添加到主控制侧栏，并增加上传文件功能，独立出来」。
   * 以前必须「先去课时页点 AI 批改作业」才能拿到一份 —— 老师手里拿着照片时，
   * 实际想要的就一件事：把图丢进去开始批。 */
  checkTrue('★★ 顶栏有「＋ 新建批改」入口（不用先去课时页）',
    /＋ 新建批改/.test(html), true);
  checkTrue('★ 主控制侧栏有批改工作台的入口',
    /workbench\.html/.test(APP_SRC) && /批改工作台/.test(APP_SRC), true);

  NEW_GRADE_BODY = null;
  UPLOADED_FILES = [];
  await vm.openNew();
  await sleep(120);
  checkTrue('★ 打开「新建批改」→ 拉到课时列表', vm.lessons.length >= 2, String(vm.lessons.length));
  checkTrue('★ 课时列表**倒序**（最近那节课在最上面，老师找的是它）',
    vm.lessons[0].id === 2, String(vm.lessons[0].id));
  checkTrue('★ 课时标签写清了「日期 · 学生」，挑的时候不用猜',
    /19:00 · 张三/.test(vm.lessonLabel(vm.lessons[1])),
    vm.lessonLabel(vm.lessons[1]));

  await vm.pickLesson(1);
  await sleep(120);
  check('★ 选完课时 → 列出这节课的作业原件', vm.newFiles.length, 2);
  check('★ 默认**全选**（一次作业通常就是那两张图，不用再勾一次）', vm.newPick.length, 2);
  vm.toggleNewFile(501);
  check('★ 点一下取消勾选', vm.newPick.length, 1);
  vm.toggleNewFile(501);
  check('★ 再点一下勾回来', vm.newPick.length, 2);

  /* 上传。用真的 FormData —— 记忆里的教训：File/Blob/FormData 必须一起搬成
     jsdom 那一套，跨 realm 的 Blob 互不认，.name 会变 undefined。 */
  UPLOAD_FAILS = false;
  await vm.uploadNew([new W.File(['x'], 'a.png', { type: 'image/png' })]);
  await sleep(150);
  check('★★ 上传成功 → 清单里多了一个文件', vm.newFiles.length, 3);
  UPLOAD_FAILS = true;
  await vm.uploadNew([new W.File(['x'], 'b.exe', { type: 'application/octet-stream' })]);
  await sleep(150);
  UPLOAD_FAILS = false;
  check('★ 上传失败要说清楚失败了（不许静默）',
    (MSGS[MSGS.length - 1] || {}).type, 'error');
  check('★ 一次失败不影响已经传好的那些', vm.newFiles.length, 3);

  /* 建档。 */
  await vm.createGrade();
  await sleep(200);
  check('★★ 建档时**按勾选**带上 file_ids（上传的那个也自动选上了）',
    (NEW_GRADE_BODY && NEW_GRADE_BODY.file_ids || []).length, 3);
  check('★★ 建档后自动打开这份（gid 切过去了）', vm.gid, 8);
  checkTrue('★ 地址栏写进了 gid（刷新 / 甩链接给别人还是这一份）',
    /replaceState/.test(html) && /gid=/.test(html), true);
  /* ★ 默认 answer_mode='ai'：老师手上多半没有标准答案，这一步不该问他要。 */
  checkTrue('★ 建档默认只有 AI 基准（basis_modes=[]，没答案也能开始批）',
    !!NEW_GRADE_BODY && Array.isArray(NEW_GRADE_BODY.basis_modes)
    && NEW_GRADE_BODY.basis_modes.length === 0,
    NEW_GRADE_BODY && JSON.stringify(NEW_GRADE_BODY.basis_modes));
  vm.newOpen = false;

  /* ============ 22. ★★★ 撤销（Ctrl+Z）============
   *  用户 2026-10-20：「可以撤销画笔笔记和文本框，要求能用快捷键撤销（ctrl+z）」。
   *
   *  口径：
   *   · 只记「改批注」的两类操作（新增 / 擦除）。拖动不单独记。
   *   · add  → 反向就是 DELETE 那一条；
   *   · erase→ 反向是把**擦之前存下来的原数据**整条 POST 回去（/marks/batch）。
   *   · 输入框里的 Ctrl+Z 必须留给浏览器（老师文字写错要能撤自己打的字）。
   */
  const nPostMark = () => CALLS.filter(
    (c) => c.url === '/api/homework-grades/7/marks' && c.method === 'POST').length;  const nDelMark = () => CALLS.filter(
    (c) => /^\/api\/grade-marks\/\d+$/.test(c.url) && c.method === 'DELETE').length;
  const nBatch2 = () => CALLS.filter(
    (c) => c.url === '/api/homework-grades/7/marks/batch' && c.method === 'POST').length;

  /* ---- ① 新增一笔 → 撤销把它删掉 ---- */
  /* ⚠️ 第 21 节「页内新建」把 gid 换成了刚建的那一份，这里必须拉回 7 ——
   *    否则 addMark 发到 /homework-grades/<新id>/marks，桩不认识那条路由，
   *    回一个 {ok:1}（没有 id），撤销就退化成「只能撤本机队列里那些」。 */
  vm.gid = 7;
  vm.undoStack = [];
  await vm.addMark(1, { kind: 'ink', geo: [10, 20, 60, 70], points: [10, 20, 60, 70], color: '#dc2626' });
  await sleep(150);
  check('★★ 画一笔 → 撤销栈 +1', vm.undoStack.length, 1);
  checkTrue('★★ 栈里记下了服务端 id（撤销靠它定位）',
    !!vm.undoStack[0] && vm.undoStack[0].id !== null && vm.undoStack[0].id !== undefined,
    JSON.stringify(vm.undoStack[0]));
  const uD0 = nDelMark();
  await vm.undoLast();
  await sleep(180);
  check('★★★ Ctrl+Z 撤「新增」→ 真的发了 DELETE', nDelMark(), uD0 + 1);
  check('★★★ 撤完栈空了（按钮跟着变灰）', vm.undoStack.length, 0);

  /* ---- ② 擦掉一笔 → 撤销把它**原样**补回去 ---- */
  vm.g.marks = [{
    id: 501, page_no: 1, kind: 'ink', geo: [11, 22, 33, 44],
    points: [11, 22, 33, 44], text: '', color: '#2563eb',
    item_id: null, created_by: 'teacher',
  }];
  await sleep(30);
  await vm.eraseMarks([501]);
  await sleep(150);
  check('★★ 擦掉一笔 → 撤销栈 +1', vm.undoStack.length, 1);
  checkTrue('★★★ 栈里存的是**原数据**（撤销时它已经不在库里了，现查是查不到的）',
    !!vm.undoStack[0] && vm.undoStack[0].type === 'erase'
      && (vm.undoStack[0].marks || []).length === 1
      && vm.undoStack[0].marks[0].kind === 'ink'
      && vm.undoStack[0].marks[0].color === '#2563eb',
    JSON.stringify(vm.undoStack[0]));
  const uB0 = nBatch2();
  await vm.undoLast();
  await sleep(180);
  check('★★★ Ctrl+Z 撤「擦除」→ 走 /marks/batch 把整条补回去', nBatch2(), uB0 + 1);
  const uLast = CALLS.filter(
    (c) => c.url === '/api/homework-grades/7/marks/batch' && c.method === 'POST').pop();
  checkTrue('★★ 补回去的就是原来那条（points / color 都对得上）',
    !!uLast && !!uLast.body && !!uLast.body.marks
      && uLast.body.marks[0].color === '#2563eb'
      && JSON.stringify(uLast.body.marks[0].points) === JSON.stringify([11, 22, 33, 44]),
    JSON.stringify(uLast && uLast.body));

  /* ---- ③ 输入框里按 Ctrl+Z：必须交给浏览器 ---- */
  const uTa = D.createElement('textarea');
  D.body.appendChild(uTa);
  vm.undoStack = [{ type: 'add', id: 999001, lid: '' }];
  const uD1 = nDelMark();
  uTa.dispatchEvent(new W.KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true }));
  await sleep(150);
  check('★★ 在 textarea 里按 Ctrl+Z → 不去删卷面标注（那是撤自己打的字）',
    nDelMark(), uD1);
  check('★ 栈也没被吃掉', vm.undoStack.length, 1);
  uTa.remove();

  /* ---- ④ 页面上按 Ctrl+Z：真的撤 ---- */
  const uD2 = nDelMark();
  D.body.dispatchEvent(new W.KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true }));
  await sleep(200);
  check('★★★ 页面上按 Ctrl+Z → 真的撤销了', nDelMark(), uD2 + 1);

  /* ---- ⑤ 空栈时不发请求 ---- */
  const uD3 = nDelMark();
  D.body.dispatchEvent(new W.KeyboardEvent('keydown', { key: 'z', ctrlKey: true, bubbles: true }));
  await sleep(120);
  check('★★ 没东西可撤时按 Ctrl+Z → 一个请求都不发（也不能报错）', nDelMark(), uD3);

  /* ---- ⑦ 文本框 / √× 必须和笔迹走同一条路 ---- */
  /* 用户说的是「撤销画笔笔记**和文本框**」。只要它们都走 addMark，
   * 就自动进撤销栈 —— 所以这里守的是「别给某一种标注另写一条 POST」：
   * 那样它画得出来、却撤不掉，而且不会有任何报错。 */
  const uSaveText = (html.match(/async saveText\(\)[\s\S]*?\n    \},/) || [''])[0];
  checkTrue('★★ 文本框走 addMark（于是自动进撤销栈，不用另写一套）',
    /this\.addMark\(/.test(uSaveText), uSaveText.slice(0, 60));
  checkTrue('★★ √ / × 也走 addMark（同一个道理）',
    /@stamp="\(p\) => addMark\(pno, p\)"/.test(html), true);

  /* ---- ⑥ 撤销失败要退栈，不能骗人 ---- */
  checkTrue('★★★ 撤销失败会把这一条放回栈（否则「撤销（1）」的数字一直骗人）',
    /catch \(err\) \{[\s\S]{0,400}?this\.undoStack\.push\(e\)/.test(html), true);
  checkTrue('★ 换一份作业时撤销栈清空（否则 Ctrl+Z 会去删上一份的标注）',
    /this\.undoStack = \[\];\s*\n\s*this\.undoBusy = false;/.test(html), true);

  /* ============ 23. ★★ 导出到作业存档（批改文件留存）============
     用户 2026-10-20：「在批改作业完成后，页面新增「导出到作业存档」按钮，
     点击后调用上传接口，将本次批改后的文件保存到对应目录下。」

     守三件事：
       · 按钮只在有记录时出现，且**有 loading**（防重复点 → 存出两份一样的）
       · ★★ 调接口**之前**必须 flushPending —— 否则后端导出读不到刚画的几笔，
         存出来的 PDF 少了那些笔迹，而屏幕上看着是完整的（本模块最大的坑）
       · 失败要说人话，且按钮要归位
  */
  const arBtn = (html.match(/<el-button[^>]*archiveGrade[^>]*>[\s\S]{0,90}?<\/el-button>/) || [''])[0];
  checkTrue('★ 顶栏有「导出到作业存档」按钮',
    /导出到作业存档/.test(arBtn), arBtn.slice(0, 70));
  checkTrue('★ 按钮只在有记录时出现（v-if="g"）',
    /v-if="g"/.test(arBtn), arBtn.slice(0, 90));
  checkTrue('★★ 按钮有 loading（防重复点 —— 点两下会存出两份同名副本）',
    /:loading="archiving"/.test(arBtn), arBtn.slice(0, 90));

  const arFn = (html.match(/async archiveGrade\(\) \{[\s\S]*?\n    \},/) || [''])[0];
  checkTrue('★★★ 调接口前先冲本机笔迹（否则刚画的几笔不在 PDF 里）',
    /await this\.flushPending\(\);/.test(arFn)
      && arFn.indexOf('await this.flushPending();') < arFn.indexOf('grade-archives/generate'),
    arFn.slice(0, 160));
  checkTrue('★ 不让前端「先下载再上传」（20MB 的 PDF 不该在浏览器里过一遍）',
    !/createObjectURL/.test(arFn), arFn.slice(0, 160));
  checkTrue('★ 失败要说人话（不是只 console）',
    /ElMessage\.error\('存档失败/.test(arFn), arFn.slice(0, 160));
  /* ⚠️ 跨度给到 800 而不是刚好够：`loadGrade` 开头是「一份作业的复位清单」，
     每加一类按记录走的状态就会往里插几行（判定依据就插了 7 行）。
     断言要守的是「这两条复位在**同一段**里」，不是「相邻两行」。 */
  checkTrue('★★ 换一份作业时重置 archiving（否则切过去按钮还在转）',
    /this\.archiving = false;[\s\S]{0,800}?this\.cancelAsked = false;/.test(html), true);

  /* ---- 真发一次 ---- */
  vm.gid = 7;
  vm.g = LAST_DETAIL;
  MSGS.length = 0;
  const nArch = () => CALLS.filter(
    (c) => /\/api\/grade-archives\/generate$/.test(c.url) && c.method === 'POST').length;
  const nArch0 = nArch();
  await vm.archiveGrade();
  await sleep(80);
  check('★★ 点一下真的发了生成请求', nArch() - nArch0, 1);
  const archCall = CALLS.filter(
    (c) => /grade-archives\/generate$/.test(c.url) && c.method === 'POST').pop() || {};
  check('★★ 请求里带的是当前这份作业的 grade_id',
    (archCall.body || {}).grade_id, 7);
  checkTrue('★★ 成功要说一句、并说出存到哪儿（否则老师不知道文件去哪了）',
    MSGS.some((m) => m.type === 'success' && /学生资料/.test(m.text)
      && /students\//.test(m.text)),
    JSON.stringify(MSGS.slice(-2)));
  checkTrue('★ 存完 archiving 归位（按钮不能一直转）', vm.archiving === false, String(vm.archiving));

  /* ---- 失败路径 ---- */
  FAIL_ARCHIVE = true;
  MSGS.length = 0;
  await vm.archiveGrade();
  await sleep(60);
  checkTrue('★ 失败要报红字（不能一声不响）',
    MSGS.some((m) => m.type === 'error' && /存档失败/.test(m.text)),
    JSON.stringify(MSGS.slice(-2)));
  checkTrue('★★ 失败后 archiving 也归位（否则按钮永远转圈，再点也没反应）',
    vm.archiving === false, String(vm.archiving));
  FAIL_ARCHIVE = false;

  /* ============ 24. 样式表必须能过 CSS 词法（2026-10-20 第二次报障的真因）=====
   *
   *  上面 19b 那三条 z-index 断言为什么没拦住？因为它们是**在源码文本里正则搜索**：
   *  `/\.gmark-layer\s*\{[^}]*z-index/` 只要求「这几个字符相邻出现」，
   *  而浏览器要的是「这行文字落在一条**有效**规则里」。两者不是一回事。
   *
   *  真因：上一条注释多留了一个注释结束符成了注释外的野文本 → 解析器把
   *  「野文本 + 后面那段注释 + `.gmark-layer`」并成**一条永远匹配不上的规则**
   *  整条吃掉 → position/inset/z-index 全部消失，层退回普通流里的方块。
   *  **源码一个字没变、控制台一声不响、旧断言照样绿**，只有真浏览器量
   *  getComputedStyle 才看得见（实测 layer: position static / z-index auto /
   *  678×678；修好后同一条量值是 absolute / 20 / 678×960）。
   *
   *  所以这里改成**词法扫描**：注释严格配平 + 关键规则必须真的独立成条。 */
  function cssIssues(css) {
    const bad = [];
    let i = 0;
    let inComment = false;
    while (i < css.length) {
      if (!inComment && css.startsWith('/*', i)) { inComment = true; i += 2; continue; }
      if (inComment && css.startsWith('*/', i)) { inComment = false; i += 2; continue; }
      if (!inComment && css.startsWith('*/', i)) {
        // 注释外的结束符 = 野文本，会把下一条规则整条吞掉
        const at = css.slice(Math.max(0, i - 70), i).replace(/\s+/g, ' ');
        bad.push('注释外多出一个结束符 @' + i + '：「…' + at + '」');
        i += 2;
        continue;
      }
      i += 1;
    }
    if (inComment) bad.push('注释到块尾都没闭合（少一个结束符）');
    return bad;
  }

  function stripCssComments(css) {
    return css.replace(/\/\*[\s\S]*?\*\//g, ' ');
  }

  /** 去注释后的**顶层选择器**清单。零构建这几个页面的 CSS 都是扁平的，够用。 */
  function topSelectors(css) {
    const body = stripCssComments(css);
    const out = [];
    let depth = 0;
    let buf = '';
    for (const ch of body) {
      if (ch === '{') {
        if (depth === 0) { out.push(buf.split(/\s+/).join(' ').trim()); buf = ''; }
        depth += 1;
      } else if (ch === '}') {
        depth -= 1;
        if (depth === 0) buf = '';
      } else if (depth === 0 && ch === ';') {
        buf = '';
      } else if (depth === 0) {
        buf += ch;
      }
    }
    return out;
  }

  const wbCss = (html.match(/<style[^>]*>([\s\S]*?)<\/style>/) || ['', ''])[1];
  const wbIssues = cssIssues(wbCss);
  checkTrue('★★★ workbench.html 样式表注释配平（多一个结束符会吃掉下一条规则）',
    wbIssues.length === 0, wbIssues.join(' | '));

  const wbSels = topSelectors(wbCss);
  checkTrue('★★★ .gmark-layer 是一条**独立规则**（被野文本并掉就成了垃圾选择器）',
    wbSels.indexOf('.gmark-layer') >= 0,
    '前 6 条选择器：' + wbSels.slice(0, 6).join(' / '));

  const layerRule = (stripCssComments(wbCss).match(/\.gmark-layer\s*\{([^}]*)\}/) || ['', ''])[1];
  checkTrue('★★★ 规则里真的有 position:absolute（static = 普通方块，笔迹被挤到左上角）',
    /position:\s*absolute/.test(layerRule), layerRule.slice(0, 90));
  checkTrue('★★★ 有 inset:0（铺满整页，而不是跟着内容走）',
    /inset:\s*0/.test(layerRule), '');
  checkTrue('★★★ 有 z-index:20（配合 19b 的数值比较才有意义）',
    /z-index:\s*20/.test(layerRule), '');

  /* 同一类毛病在别的零构建页面上一模一样地会发生 —— 一律扫一遍。 */
  ['workbench.html', 'entry.html', 'batch.html'].forEach((f) => {
    const p = path.join(ROOT, f);
    if (!fs.existsSync(p)) return;
    const src = fs.readFileSync(p, 'utf8');
    const blocks = [...src.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]);
    const issues = blocks.reduce((acc, b) => acc.concat(cssIssues(b)), []);
    checkTrue('★★ ' + f + ' 的 <style> 注释配平', issues.length === 0, issues.join(' | '));
  });
  const appCssPath = path.join(ROOT, 'app.css');
  if (fs.existsSync(appCssPath)) {
    const issues = cssIssues(fs.readFileSync(appCssPath, 'utf8'));
    checkTrue('★★ app.css 注释配平', issues.length === 0, issues.join(' | '));
  }

  /* ============ 25. 保存（按钮 / Ctrl+S / 已保存时间 / 未保存标记）============
     用户 2026-10-21：
       · 画完能点「保存」或按 Ctrl+S
       · 到底存住没有要说得清（已保存 + 时间；有没存的要标出来）
       · 失败要报错，**而且已经画的一笔都不能丢**

     这个页面本来就是**自动落库**的（每笔一个 POST，没送出去的进本机草稿队列，
     下次打开自动补发）。所以「保存」的语义不是「把笔迹写进去」，而是
     「现在就把所有还没落库的落库，并明确回答结果」—— 下面全按这个语义验。 */
  await vm.loadGrade(7);
  await sleep(120);

  const saveBtn = [...D.querySelectorAll('button')]
    .find((b) => /保存/.test(b.textContent || '') && !/存档|分割/.test(b.textContent || ''));
  checkTrue('★ 卷面区域有「保存」按钮', !!saveBtn,
    saveBtn ? saveBtn.textContent.trim() : '没找到');
  checkTrue('★ 按钮绑的是 saveNow', /@click="saveNow"/.test(html), '');
  checkTrue('★★ 按钮有 loading（连点会并发两份请求）', /:loading="saving"/.test(html), '');

  const stateTxt = () => {
    const el = D.querySelector('.save-state');
    return el ? el.textContent.replace(/\s+/g, ' ').trim() : '(没有 .save-state)';
  };
  checkTrue('★ 有保存状态条，且没动过时写着「已保存」', /已保存/.test(stateTxt()), stateTxt());

  /* ---- 未保存标记 ---- */
  const lidSave = vm.pushLocal({ page_no: 1, kind: 'ink', points: [10, 10, 30, 30], color: '#dc2626' });
  await sleep(40);
  check('★★ 本机还有没落库的笔 → pendingMarkCount', vm.pendingMarkCount, 1);
  checkTrue('★★ 状态条换成「未保存 N 笔」', /未保存 1 笔/.test(stateTxt()), stateTxt());

  /* ---- 保存失败：报错 + 一笔都不能少 ---- */
  const origFetchSave = W.fetch;
  W.fetch = async (u, o) => {
    if (String(u) === '/api/homework-grades/7/marks/batch') {
      return { ok: false, status: 500, json: async () => ({ detail: '网络断了' }) };
    }
    return origFetchSave(u, o);
  };
  MSGS.length = 0;
  await vm.saveNow();
  await sleep(100);
  W.fetch = origFetchSave;
  checkTrue('★★★ 保存失败要说「保存失败」（不能静默）',
    /保存失败/.test((MSGS[MSGS.length - 1] || {}).text || ''),
    (MSGS[MSGS.length - 1] || {}).text);
  checkTrue('★★★ 并且说明「还在本机，没丢」（否则老师会重画一遍）',
    /还在本机/.test((MSGS[MSGS.length - 1] || {}).text || ''), '');
  check('★★★ 失败后本机那条**没有被清掉**（数据不能丢）', vm.localMarks.length, 1);
  check('★★ 失败时不能写 savedAt（写了状态条就骗人）', vm.savedAt, '');
  checkTrue('★ 失败后状态条仍然标红「未保存」', /未保存 1 笔/.test(stateTxt()), stateTxt());

  /* ---- 保存成功 ---- */
  MSGS.length = 0;
  await vm.saveNow();
  await sleep(150);
  checkTrue('★★★ 成功提示「已保存」+ 时间',
    /^已保存 \d\d:\d\d:\d\d$/.test((MSGS[MSGS.length - 1] || {}).text || ''),
    (MSGS[MSGS.length - 1] || {}).text);
  checkTrue('★ savedAt 是 HH:MM:SS', /^\d\d:\d\d:\d\d$/.test(vm.savedAt), vm.savedAt);
  check('★★ 成功后本机队列清空', vm.localMarks.length, 0);
  checkTrue('★ 状态条回到「已保存 时间」', /已保存 \d\d:\d\d:\d\d/.test(stateTxt()), stateTxt());
  checkTrue('★★ 那一笔真的进了服务端（不是只发过请求）',
    (LAST_DETAIL.marks || []).some((m) => String(m.points) === '10,10,30,30'),
    JSON.stringify((LAST_DETAIL.marks || []).slice(-1)));

  /* ---- Ctrl+S ---- */
  const keys = (key, opts, target) => {
    const ev = new W.KeyboardEvent('keydown',
      Object.assign({ key, bubbles: true, cancelable: true }, opts || {}));
    (target || W.document.body).dispatchEvent(ev);
    return ev;
  };
  let saveCalls = 0;
  const realSave = vm.saveNow;
  vm.saveNow = function () { saveCalls += 1; return Promise.resolve(); };

  const evS = keys('s', { ctrlKey: true });
  await sleep(30);
  check('★★ Ctrl+S 触发保存', saveCalls, 1);
  checkTrue('★★ 并且 preventDefault（否则浏览器弹「保存网页」对话框）', evS.defaultPrevented, '');

  /* 输入框里按 Ctrl+S：浏览器自己的「保存网页」会挡住页面，必须照样拦下来。
     （与 Ctrl+Z 相反 —— 那个要留给浏览器撤销打字。） */
  const ta = D.createElement('textarea');
  D.body.appendChild(ta);
  saveCalls = 0;
  const evS2 = keys('s', { ctrlKey: true }, ta);
  await sleep(30);
  check('★★ 在输入框里按 Ctrl+S 也会保存', saveCalls, 1);
  checkTrue('★★ 输入框里同样 preventDefault', evS2.defaultPrevented, '');
  const evZ = keys('z', { ctrlKey: true }, ta);
  checkTrue('★ 输入框里的 Ctrl+Z 仍然留给浏览器（撤销打字）', !evZ.defaultPrevented, '');
  ta.remove();
  vm.saveNow = realSave;

  /* ---- 换一份作业：上一份的「已保存 11:45」不能留在这一份上 ---- */
  await vm.loadGrade(7);
  await sleep(120);
  check('★★ 换一份作业会清空 savedAt', vm.savedAt, '');

  /* ---- 笔迹粗细（迁移 0033）：跟着每一笔存，老记录 width=0 走默认 ---- */
  check('★ 粗细第一档仍是 2.2（没选过粗细的笔迹观感不变）', vm.INK_WIDTHS[0], 2.2);
  checkTrue('★ 标注层接到 pen-width', /:pen-width="inkWidth"/.test(html), '');
  const layerSrc = fs.readFileSync(path.join(ROOT, 'src', 'components', 'GradeMarkLayer.js'), 'utf8');
  checkTrue('★★ 每一笔按自己的 width 画（inline style；写属性会被样式表盖掉）',
    /strokeWidth: s\.width \+ 'px'/.test(layerSrc), '');
  checkTrue('★★ 松手提交时把 width 带上（不带就永远存成 0）',
    /width: props\.penWidth \|\| 0/.test(layerSrc), '');
  /* ---- 补发这条路也要带 width（单笔直传是另一条路，只修一条 = 半修）---- */
  vm.pushLocal({ page_no: 2, kind: 'ink', points: [40, 40, 80, 80], color: '#dc2626', width: 4.5 });
  await sleep(40);
  CALLS.length = 0;
  await vm.flushPending();
  await sleep(150);
  const batchCall = CALLS.filter((c) => c.url === '/api/homework-grades/7/marks/batch').pop();
  check('★★★ 补发（批量接口）也带 width —— 断网补上去的笔迹不能全变成最细一档',
    batchCall && batchCall.body && batchCall.body.marks[0].width, 4.5);
  checkTrue('★★ 本机草稿里也存了 width（不然刷新一次就丢）',
    /width: Math\.max\(0, Number\(\(payload \|\| \{\}\)\.width\) \|\| 0\)/.test(
      fs.readFileSync(path.join(ROOT, 'src', 'gradeDraft.js'), 'utf8')), '');

  /* ============ 26. 判定依据（模式 / 上传 / 预览 / 删除 / 开批拦截）============
     用户 2026-10-21 四条：
       ① 上传入口：点击 / 拖拽 / 粘贴；图片与常见文件；缩略图 + 列表 + 预览 + 删除；
          限制类型、数量、大小。
       ② **AI 基准 + 可叠加的条件**（2026-10-21 用户改的口径）：默认只有 AI 自主判定，
          勾「按我的答案」加一个条件、勾「按我的细则」再加一个，两个能同时勾；
          每档各带一句说明。
       ③ 后两种必须至少一份依据，没有就**拦住不让提交**；依据要真的进批改。
       ④ 沿用现有交互，补上传中的加载态、失败重试、错误提示。

     ⚠️ 这一节把桩的 status 切成 queued：依据只在「还没批出结果」时能改，
        沿用上一节的 reviewing 会让整节都在测「锁住」那一条分支。 */
  DETAIL_STATUS = 'queued';
  DETAIL_MODES = [];
  BASIS_ROWS = [];
  BASIS_FAIL_ONCE = false;
  LAST_DETAIL = null;
  await vm.loadGrade(7);
  await sleep(120);

  check('★ 默认一个条件都不勾（只剩 AI 基准）', vm.basisModes.length, 0);
  checkTrue('★ 默认档的说明文案不是空的（用户要求「各模式展示简洁说明」）',
    String(vm.basisModeDesc || '').length > 10, vm.basisModeDesc.slice(0, 24));

  /* 结构：1 个**恒选的基准** + 2 个可勾的条件。用 checkbox 而不是 radio ——
     radio 天生互斥，表达不了「加一个条件」，也对不上后端的 `key+rubric`。 */
  const boxes = [...D.querySelectorAll('.basis-modes .el-checkbox')];
  check('★★ 基准 + 两个条件 = 三个复选框（不是三选一的单选框）', boxes.length, 3);
  const modeTxt = boxes.map((r) => r.textContent.trim()).join('|');
  checkTrue('★★ 名字对得上：AI 基准 / 按我给的答案 / 按我的评分细则',
    /AI/.test(modeTxt) && /答案/.test(modeTxt) && /细则/.test(modeTxt), modeTxt);
  const baseBox = D.querySelector('.basis-modes .el-checkbox.basis-base');
  checkTrue('★★ 基准那档是**勾着的**（它恒生效）',
    !!baseBox && /is-checked/.test(baseBox.className), baseBox ? baseBox.className : '没渲染');
  checkTrue('★★ 基准那档**点不动**（关掉它就没有判定标准了）',
    /is-disabled/.test((baseBox && baseBox.className) || ''), (baseBox && baseBox.className) || '');
  checkTrue('★ 基准那档标了「基准」两个字（否则老师以为三选一）',
    !!baseBox && /基准/.test(baseBox.textContent), baseBox ? baseBox.textContent : '');
  check('★ 两个条件默认都没勾', [...D.querySelectorAll('.basis-conds .el-checkbox')]
    .filter((x) => /is-checked/.test(x.className)).length, 0);

  /* ---- ① 换模式要**立刻落库**（开批时服务端读的是库里那个值）---- */
  CALLS.length = 0;
  vm.basisModes = ['key'];
  await vm.onBasisModeChange();
  await sleep(60);
  const modePatch = CALLS.filter(
    (c) => c.url === '/api/homework-grades/7' && c.method === 'PATCH').pop();
  check('★★ 勾条件立刻 PATCH basis_modes（只改界面 = 批出来的口径和勾的不一样）',
    JSON.stringify((modePatch && modePatch.body && modePatch.body.basis_modes) || null),
    JSON.stringify(['key']));
  check('★ 落库后服务端存的是 key（不是只改内存）',
    JSON.stringify(DETAIL_MODES), JSON.stringify(['key']));
  checkTrue('★ 页签外那行口径跟着变（不用切页签也知道这次按什么判）',
    /答案/.test(vm.basisModeLabel) && /AI/.test(vm.basisModeLabel), vm.basisModeLabel);

  /* ---- ② 上传：类型 / 大小当场拦 ---- */
  const mkFile = (name, type, size) => new W.File(
    [new Uint8Array(size || 8)], name, { type: type || 'image/png' });
  checkTrue('★ 不认识的格式当场拦（不用等后端 422）',
    /收不了/.test(vm.basisReject(mkFile('答案.exe', 'application/x-msdownload'))), '');
  checkTrue('★ 老式 .xls 提示去「另存为」',
    /另存/.test(vm.basisReject(mkFile('旧.xls'))), vm.basisReject(mkFile('旧.xls')));
  checkTrue('★ 超过单份上限当场拦（文案带 MB）',
    /MB/.test(vm.basisReject(mkFile('大.png', 'image/png', 21 * 1024 * 1024))), '');
  check('★ 正常的 Excel 放行', vm.basisReject(mkFile('答案.xlsx')), '');

  /* ---- ③ 上传成功 → 列表里有它 ---- */
  CALLS.length = 0;
  MSGS.length = 0;
  await vm.uploadBasisFiles([mkFile('答案.png')]);
  await sleep(90);
  check('★★ 上传走的是批改自己的依据接口（role=answer，不进「交作业」）',
    CALLS.filter((c) => /\/homework-grades\/7\/basis\?role=answer$/.test(c.url)
      && c.method === 'POST').length, 1);
  check('★ 传完刷新了详情，列表里看得到', vm.basisAll.length, 1);
  checkTrue('★★ 列表里带缩略图（图片依据）',
    !!D.querySelector('.basis-thumb'), '没有 .basis-thumb');

  /* ---- ④ 上传中要有加载态 ---- */
  checkTrue('★★ 上传中有加载态（.basis-drop 里的 v-if="basisBusy"）',
    /v-if="basisBusy"/.test(html), '');
  const upSrc = html.slice(html.indexOf('async uploadBasisFiles'),
    html.indexOf('async removeBasis'));
  checkTrue('★★ busy 必须在发请求**之前**置位（否则转圈永远慢一拍）',
    upSrc.indexOf('this.basisBusy = true') > -1
    && upSrc.indexOf('this.basisBusy = true') < upSrc.indexOf("'/basis?role='"), '');

  /* ---- ⑤ 一个条件都没勾时传依据：没去处，要劝住 ---- */
  vm.basisModes = [];
  CALLS.length = 0;
  MSGS.length = 0;
  await vm.uploadBasisFiles([mkFile('多余.png')]);
  check('★ 只有 AI 基准时不发上传请求（自己解用不到依据）',
    CALLS.filter((c) => /\/basis/.test(c.url)).length, 0);
  checkTrue('★★ 而且要说明白为什么 + 下一步点哪里（不能点了一下什么都不发生）',
    /勾上/.test(MSGS.map((m) => m.text).join('')), MSGS.map((m) => m.text).join('|'));

  /* ---- ⑤b 没勾条件时，上传区就地给两个按钮（用户报的「想传东西却被劝住」）---- */
  /* 先把条件真的收回库里（只改 vm 不改库的话，下面「quickAddMode 会 PATCH」是假绿：
     saveBasisMode 比的是 vm 与 g.basis_modes，两边不同步就永远不觉得有变化）。 */
  await vm.saveBasisMode();
  await sleep(60);
  const dropEl = D.querySelector('.basis-drop');
  checkTrue('★★★ 没勾条件时，上传区不是等着劝人，而是给「先选一个条件」',
    !!dropEl && /先选一个条件/.test(dropEl.textContent),
    dropEl ? dropEl.textContent.slice(0, 40) : '没渲染');
  const quickBtns = [...((dropEl && dropEl.querySelectorAll('button')) || [])];
  check('★★★ 就地给出两个条件按钮（省得老师滚回去找复选框）', quickBtns.length, 2);
  checkTrue('★★ 两个按钮分别对得上「答案」「细则」',
    quickBtns.length === 2 && /答案/.test(quickBtns[0].textContent)
    && /细则/.test(quickBtns[1].textContent),
    quickBtns.map((x) => x.textContent.trim()).join('|'));

  CALLS.length = 0;
  await vm.quickAddMode('key');
  await sleep(80);
  check('★★★ 点「＋ 按我给的答案判定」→ 条件勾上了', vm.basisModes.join(), 'key');
  check('★★★ 而且立刻落库（不是只改界面）',
    JSON.stringify((CALLS.filter((c) => c.method === 'PATCH').pop() || {}).body
      && CALLS.filter((c) => c.method === 'PATCH').pop().body.basis_modes),
    JSON.stringify(['key']));
  await sleep(60);
  checkTrue('★★ 勾上之后上传区恢复正常（能挑文件了）',
    !/先选一个条件/.test(D.querySelector('.basis-drop').textContent), '');

  /* ---- ⑥ 上传失败 → 留在列表里 → 单条重试 ---- */
  vm.basisModes = ['rubric'];
  await vm.onBasisModeChange();
  await sleep(60);
  BASIS_FAIL_ONCE = true;
  CALLS.length = 0;
  await vm.uploadBasisFiles([mkFile('细则.png')]);
  await sleep(90);
  check('★★ 失败的那份进「待重试」列表（只弹个提示就没了 = 得重挑文件）',
    vm.basisFailed.length, 1);
  check('★ 失败不影响已经传好的那份', vm.basisAll.length, 1);
  checkTrue('★★ 失败要有红字（不能静默）',
    /没传上去|失败/.test(MSGS.map((m) => m.text).join('')) || /\.basis-fail/.test(html), '');
  await vm.retryBasis(0);
  await sleep(90);
  check('★★ 重试成功后「待重试」里不再留着它', vm.basisFailed.length, 0);
  check('★★ 重试真的又发了一次（不是假装成功）',
    CALLS.filter((c) => /\/basis\?role=rubric$/.test(c.url)).length, 2);
  check('★★ 「细则」模式下只数细则那份（答案那份不算依据）', vm.basisRoleCount, 1);

  /* ---- ⑦ 没有依据 → **拦住不让提交** ---- */
  BASIS_ROWS = [];
  LAST_DETAIL = null;
  await vm.loadGrade(7);
  await sleep(90);
  vm.basisModes = ['rubric'];
  await vm.onBasisModeChange();
  await sleep(60);
  CALLS.length = 0;
  MSGS.length = 0;
  vm.tab = 'blocks';
  await vm.runGrade();
  await sleep(60);
  check('★★★ 选了「按我的细则」却没有依据 → 一个 /run 都不发',
    CALLS.filter((c) => /\/run$/.test(c.url)).length, 0);
  check('★★ 而且把老师带到「判定依据」页签（提示缺什么）', vm.tab, 'basis');
  checkTrue('★★ 提示要说清缺的是细则',
    /细则/.test(MSGS.map((m) => m.text).join('')), MSGS.map((m) => m.text).join('|'));

  /* ---- ⑧ 有依据就放行，且**先存模式再开批** ---- */
  BASIS_ROWS = [{
    id: 801, role: 'rubric', name: '细则.txt', kind: 'text', size_bytes: 100,
    pages: 0, chars: 18, lesson_file_id: null, preview: '每步 2 分', text_warn: '',
  }];
  LAST_DETAIL = null;
  await vm.loadGrade(7);
  await sleep(90);
  check('★ 依据齐了，开批按钮不再是「未配置」状态', vm.basisWarn, false);
  CALLS.length = 0;
  await vm.runGrade();
  await sleep(60);
  check('★★ 有依据时正常开批', CALLS.filter((c) => /\/run$/.test(c.url)).length, 1);

  /* ---- ⑨ 预览：图片内嵌、文本取正文 ---- */
  CALLS.length = 0;
  await vm.previewBasis(vm.basisAll[0]);
  await sleep(60);
  check('★ 预览文本类依据 → 取抽出来的正文（那才是模型看到的东西）',
    CALLS.filter((c) => /\/basis\/801\/text$/.test(c.url)).length, 1);
  checkTrue('★ 预览框开着，正文回填了', vm.previewText.length > 0, vm.previewText.slice(0, 20));
  checkTrue('★ 预览原件走 inline=1（图片/PDF 就地看，不弹下载）',
    /inline=1$/.test(vm.basisRawUrl({ id: 801 }, true)), vm.basisRawUrl({ id: 801 }, true));

  /* ---- ⑩ 删除：先问一句，再真删 ---- */
  CALLS.length = 0;
  CONFIRMS.length = 0;
  await vm.removeBasis(vm.basisAll[0]);
  await sleep(90);
  check('★★ 删依据前问了老师一句（破坏性动作）', CONFIRMS.length, 1);
  /* ⚠️ 上传的依据是自落盘的，删行会连原件一起 unlink —— 提示不能说
     「文件还在磁盘上」。老师信了这句就不会另存，等真要找就找不回来了。 */
  checkTrue('★★ 删除提示说的是实话（上传件的原件也删了）',
    /找不回来/.test((CONFIRMS[0] || {}).msg || ''), (CONFIRMS[0] || {}).msg || '');
  check('★★ 删的是 DELETE .../basis/{id}',
    CALLS.filter((c) => c.method === 'DELETE'
      && /\/homework-grades\/7\/basis\/801$/.test(c.url)).length, 1);
  check('★ 删完列表里没了', vm.basisAll.length, 0);

  /* ---- ⑪ 批过之后锁住 ---- */
  DETAIL_STATUS = 'reviewing';
  LAST_DETAIL = null;
  await vm.loadGrade(7);
  await sleep(90);
  check('★ 批出结果之后依据锁住（与后端同一判据）', vm.basisEditable, false);
  CALLS.length = 0;
  MSGS.length = 0;
  vm.basisModes = ['key'];
  await vm.uploadBasisFiles([mkFile('晚了.png')]);
  check('★ 锁住时上传一个请求都不发',
    CALLS.filter((c) => /\/basis/.test(c.url)).length, 0);
  checkTrue('★ 锁住时要说清「得新建一份批改」（不然老师以为按钮坏了）',
    /新建一份批改/.test(MSGS.map((m) => m.text).join('')), MSGS.map((m) => m.text).join('|'));

  /* ---- ⑫ Ctrl+V 贴截图 ---- */
  DETAIL_STATUS = 'queued';
  DETAIL_MODES = [];
  BASIS_ROWS = [];
  LAST_DETAIL = null;
  await vm.loadGrade(7);
  await sleep(90);
  vm.basisModes = ['key'];
  await vm.onBasisModeChange();
  await sleep(60);

  const pasteEv = (target) => {
    const ev = new W.Event('paste');
    ev.clipboardData = {
      items: [{ kind: 'file', type: 'image/png',
        getAsFile: () => mkFile('clip.png', 'image/png') }],
      files: [],
    };
    if (target) Object.defineProperty(ev, 'target', { value: target });
    return ev;
  };
  CALLS.length = 0;
  vm.onBasisPaste(pasteEv({ tagName: 'DIV' }));
  await sleep(90);
  check('★★ Ctrl+V 贴截图 → 真的走上传（用户点名要「从剪贴板缓存上传」）',
    CALLS.filter((c) => /\/basis\?role=answer$/.test(c.url)).length, 1);

  CALLS.length = 0;
  vm.onBasisPaste(pasteEv({ tagName: 'TEXTAREA' }));
  await sleep(60);
  check('★★ 输入框里的粘贴不抢（老师要往里粘文字）',
    CALLS.filter((c) => /\/basis/.test(c.url)).length, 0);

  /* ---- ⑫b 两个条件同时勾（用户点名：「选了两个相当于加了两个条件」）---- */
  DETAIL_STATUS = 'queued';
  DETAIL_MODES = [];
  BASIS_ROWS = [];
  LAST_DETAIL = null;
  await vm.loadGrade(7);
  await sleep(90);

  CALLS.length = 0;
  vm.basisModes = ['key', 'rubric'];
  await vm.onBasisModeChange();
  await sleep(80);
  const bothPatch = CALLS.filter(
    (c) => c.url === '/api/homework-grades/7' && c.method === 'PATCH').pop();
  check('★★★ 两个都勾 → 一次 PATCH 就把两个条件都送上去',
    JSON.stringify((bothPatch && bothPatch.body && bothPatch.body.basis_modes) || null),
    JSON.stringify(['key', 'rubric']));
  check('★★ 服务端存的是 key+rubric（一个条件都没丢）',
    detailMode(), detailMode());
  checkTrue('★★ 口径那行把两个条件都写出来（不能只说一个）',
    /答案/.test(vm.basisModeLabel) && /细则/.test(vm.basisModeLabel), vm.basisModeLabel);
  checkTrue('★★ 说明文案要说清「对错看答案、漏步看细则」（否则老师以为判两遍）',
    /对错/.test(vm.basisModeDesc) && /细则/.test(vm.basisModeDesc), vm.basisModeDesc.slice(0, 40));
  check('★★ 两个都勾 → 两份依据都缺（`basisMissing` 要有两条）',
    vm.basisMissing.length, 2);
  check('★★ 两个都勾才需要老师指明「这份传的是答案还是细则」', vm.basisNeedsRolePick, true);
  checkTrue('★★ 界面上真出现了那个选择（不算明白就会把细则当答案传）',
    /basis-rolepick/.test(D.querySelector('.basis-tabs, .el-tabs').innerHTML
      || D.body.innerHTML), '');

  /* 缺两个 → 一次说全，别让老师改好一个再挨一次 422 */
  CALLS.length = 0;
  MSGS.length = 0;
  vm.tab = 'blocks';
  await vm.runGrade();
  await sleep(60);
  check('★★★ 两个条件都缺 → 一个 /run 都不发', CALLS.filter((c) => /\/run$/.test(c.url)).length, 0);
  const missMsg = MSGS.map((m) => m.text).join('|');
  checkTrue('★★★ 提示里**两个都说到**（答案 + 细则），不是只说第一个',
    /答案/.test(missMsg) && /细则/.test(missMsg), missMsg);

  /* 传一份「细则」→ role 跟着选择走，不会落到 answer */
  CALLS.length = 0;
  vm.basisRolePick = 'rubric';
  await vm.uploadBasisFiles([mkFile('细则.png')]);
  await sleep(120);
  check('★★ 两个都勾时，选「细则」上传就落到 role=rubric',
    CALLS.filter((c) => /\/basis\?role=rubric$/.test(c.url)).length, 1);
  check('★ 传了一份细则后，只缺答案了', vm.basisMissing.join(), 'key');

  vm.basisRolePick = 'answer';
  await vm.uploadBasisFiles([mkFile('答案.png')]);
  await sleep(120);
  CALLS.length = 0;
  MSGS.length = 0;
  vm.tab = 'blocks';
  await vm.runGrade();
  await sleep(80);
  check('★★ 两份都齐了 → 正常开批（两个条件同时生效）',
    CALLS.filter((c) => /\/run$/.test(c.url)).length, 1);

  /* ---- ⑬ 源码层的几条硬约定 ---- */
  const inputTag = (html.match(/<input ref="basisInput"[^>]*>/) || [''])[0];
  checkTrue('★ file input 不带 accept（本机经验：accept 会把想选的文件藏起来）',
    !!inputTag && !/accept=/.test(inputTag), inputTag);
  checkTrue('★ 拖拽上传接的是 onBasisDrop（第三条路）',
    /@drop\.prevent="onBasisDrop"/.test(html), '');
  checkTrue('★ 点击上传接的是 pickBasis', /@click="pickBasis"/.test(html), '');
  checkTrue('★★ 粘贴监听挂在 window 上（焦点在哪都灵）',
    /window\.addEventListener\('paste', this\.onBasisPaste\)/.test(html), '');
  checkTrue('★★ 卸载时摘掉监听（不然切页面后还会抢 Ctrl+V）',
    /removeEventListener\('paste', this\.onBasisPaste\)/.test(html), '');
  checkTrue('★★ 一个条件都没勾时不上传（传上去没有去处）',
    /if \(!this\.basisModes\.length\) \{/.test(html), '');

  const ti = layerSrc.indexOf('template:');
  check('★★ 标注层模板里反引号正好 2 个（多一个模板就被截断、组件整个加载失败）',
    layerSrc.slice(ti).split('').filter((c) => c.charCodeAt(0) === 96).length, 2);

  console.log('\n共 ' + total + ' 项，失败 ' + fails.length + ' 项');
  if (fails.length) {
    fails.forEach((f) => console.log('  - ' + f));
    return 1;
  }
  console.log('全部通过 ✅');
  return 0;
}

main().then((code) => process.exit(code)).catch((e) => {
  console.error('挂载检查崩了：', e);
  process.exit(1);
});
