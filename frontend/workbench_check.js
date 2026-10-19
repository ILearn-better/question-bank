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
    id: 7, student_id: 5, lesson_id: 31, status: 'reviewing',
    answer_mode: 'ai', answer_text: '', rubric_text: '',
    total: 3, done: 3, failed: 0, score: null, full_score: null, error: '',
    created_at: '2026-10-19T10:00:00', finished_at: '2026-10-19T10:03:00',
    reviewed_at: null, skipped_pages: [],
    files: [{ id: 1, role: 'submission', name: '作业.pdf', pages: 3 }],
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
let LAST_DETAIL = null;
/** 下一回 /run 故意失败一次 —— 用来验「没开起来，按钮要能恢复」。 */
let FAIL_RUN_ONCE = false;
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
      LAST_DETAIL = DETAIL();
      return reply(LAST_DETAIL);
    }
    if (u === '/api/homework-grades/7/regions' && method === 'PUT') {
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
    /* 终止：只在「真的在跑」时受理（后端是 409）。这里靠 LAST_DETAIL.status 判。 */
    if (u === '/api/homework-grades/7/cancel' && method === 'POST') {
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

  /* 内联脚本用正则抽出来自己 eval —— 不依赖 jsdom 执行 <script>：
   * runScripts:'outside-only' 下它不会跑，而 'dangerously' 又会让所有 src 脚本
   * 真的去磁盘取（路径是 /vendor/...，取不到）。 */
  const blocks = html.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g) || [];
  if (blocks.length !== 1) {
    console.log('  [FAIL] 期望正好 1 段内联脚本，实际 ' + blocks.length);
    return 1;
  }
  const inline = blocks[0].replace(/^<script[^>]*>/, '').replace(/<\/script>$/, '');

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
      error: (m) => MSGS.push({ type: 'error', text: String(m) }),
      success: (m) => MSGS.push({ type: 'success', text: String(m) }),
      warning: (m) => MSGS.push({ type: 'warning', text: String(m) }),
      info: (m) => MSGS.push({ type: 'info', text: String(m) }),
    };
  }
  if (W.ElementPlus && W.ElementPlus.ElMessageBox) {
    W.ElementPlus.ElMessageBox.confirm = async function (msg, title, opt) {
      CONFIRMS.push({ msg: String(msg), title: String(title || ''), opt: opt || {} });
      return true;
    };
  }
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
   *   ② 要二次确认（终止会丢掉没批的页）
   *   ③ 后端是「受理」不是「立刻停」—— 所以**不能**自己把 running 置 false */
  vm.g = Object.assign({}, vm.g, { status: 'reviewing' });
  vm.runAsked = false;
  await sleep(80);
  const cancelBtns = () => Array.prototype.slice.call(D.querySelectorAll('.el-button'))
    .filter((b) => b.textContent.trim() === '终止');
  check('★ 没在跑的时候不出现「终止」按钮', cancelBtns().length, 0);

  vm.runAsked = true;
  vm.g = Object.assign({}, vm.g, { status: 'running' });
  await sleep(120);
  check('★ 批改中 → 出现「终止」按钮', cancelBtns().length, 1);

  const nConf = CONFIRMS.length;
  const cancelBefore = CALLS.filter((c) => c.url === '/api/homework-grades/7/cancel').length;
  cancelBtns()[0].click();
  await sleep(300);
  check('★ 点「终止」→ 先问一句（二次确认，不是点了就停）',
    CONFIRMS.length, nConf + 1);
  checkTrue('★ 确认框写清了后果（已批的保留 / 没批的不批）',
    /保留/.test((CONFIRMS[CONFIRMS.length - 1] || {}).msg || ''),
    (CONFIRMS[CONFIRMS.length - 1] || {}).msg);
  /* 提示里的换行必须是**真的换行**。历史教训：bash heredoc 把 \\n 吃掉一层之后，
     产品代码里留下的是字面量 \\n，提示框上会直接显示两个字符「\n」。 */
  checkTrue('★ 确认框的换行是真的换行（不是显示出 "\\n" 两个字）',
    !((CONFIRMS[CONFIRMS.length - 1] || {}).msg || '').includes('\\n'),
    JSON.stringify((CONFIRMS[CONFIRMS.length - 1] || {}).msg || ''));
  check('★ 确认后 → POST /cancel', CALLS.filter((c) =>
    c.url === '/api/homework-grades/7/cancel').length, cancelBefore + 1);
  /* ⚠️ 关键：终止**不能**自己把 running 置 false。后端只是受理，在跑的 AI 调用
     掐不断；提前改会出现「状态说停了、题还在一条条冒出来」。要靠轮询等真状态。 */
  checkTrue('★ 终止期间给一句「正在收尾」（不然老师以为又点了没反应）',
    html.includes('正在收尾'), true);
  vm.stopPoll();

  /* ★ 后端说「没在跑」→ 409，前端要把 detail 里的理由说给老师听。 */
  vm.g = Object.assign({}, vm.g, { status: 'reviewing' });
  vm.runAsked = false;
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
