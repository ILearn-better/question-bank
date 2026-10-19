/**
 * 批改台（HomeworkGrade）的真挂载检查 —— 框题、图上读数、逐题卡片到底上没上屏。
 *
 *   node frontend/grade_mount_check.js    # 退出码即结果（0 通过 / 跳过，非 0 失败）
 *
 * ## 为什么要挂这一页
 *
 * 批改台是整个工作台里**最复杂的一屏**（左页图 + 标注层 + 工具条 + 框题面板 + 逐题卡片），
 * 却是唯一一个「只有静态模板编译校验、没有真挂载」的视图 —— `papers` / `plan` / `prep`
 * / `render` 都有 mount check，唯独它没有。而这一页正好踩满了本项目的两个经典坑：
 *
 *   ① **零构建 in-DOM 模板里自定义组件不能自闭合**。`<GradeMarkLayer />` 会把后面的
 *      兄弟节点整个吞掉 → Vue compiler 报错 → **整页白屏，而接口测试全绿**。
 *      静态断言只能看出「写没写成对」，看不出「挂起来之后还剩几个节点」。
 *   ② **渲染容器必须空 div + 函数 ref**。`.g-rich` 里只要有插值，KaTeX 换掉文本节点后
 *      内容就永远停在第一版。这只有真挂起来数 `.katex` 才知道。
 *
 * ## 本轮新增的两件事，静态断言都证明不了
 *
 *   · **一题一区域（框题）**：`regions` 传进标注层之后，`.rg-rect` 真的画出来了没有？
 *     本地新画的草稿框（`id` 是 null）和服务器已存的框混在一个 v-for 里，
 *     key 撞了没有？—— 撞了的话「删中间那个框」会删错格子，而**界面上看不出来**。
 *   · **图上读到的（figure_note）**：这道题真的多出一块 `.g-figure` 吗？
 *     没有 figure_note 的题是不是**不会**凭空多一块空壳？（.g-sec 会撑一条分隔线）
 *
 * ## 判据：挂载痕迹，不是「innerHTML 长度」
 *
 * 找不到 jsdom 就 **SKIP 并退出 0** —— 它只是开发期加固，不该把回归弄红。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = __dirname;                 // frontend/

let JSDOM = null;

/** jsdom 是**可选依赖**：按候选顺序找，都找不到就跳过（与 papers_mount_check.js 同一套）。 */
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

/* ====================== 桩：形状取自真实接口响应（不猜结构） ====================== */

/** 收所有调用，最后拿它核对「点了按钮到底发了什么请求」。 */
const CALLS_STUB = `
export const CALLS = [];
export const push = (name, args) => { CALLS.push({ name, args }); return name; };
`;

/** item 的形状照 backend/app/services/grade_store.py::item_dict 逐字来。 */
const API_STUB = `
import { CALLS, push } from './calls.js';

/** 让某些调用**故意失败**的开关（模拟「网断了 / 页面被关掉，请求被浏览器取消」）。
 *  只在测试里改它 —— 这就是第 20 节要复现的现象。 */
export const CTL = { failMark: false };

const item = (o) => Object.assign({
  id: 0, seq: 1, page_no: 1, q_no: '', qtype: '', stem: '', student_answer: '',
  correct_answer: '', verdict: 'correct', score: null, full_score: null, basis: '',
  confidence: 'high', kp_name: '', tags: [], error_reason: '', flags: [],
  figure_note: '', status: 'pending', question_id: null,
  block_kind: '', tail_note: '',
}, o);

/** 三题：① 普通代数题 ② 几何题（有新加的 figure_note + tail_note）
 *        ③ 框里没读到题的占位（模型说这块像卷头）。 */
const DETAIL = {
  id: 7, student_id: 5, lesson_id: 31, status: 'reviewing',
  answer_mode: 'ai', answer_text: '', rubric_text: '',
  total: 3, done: 3, failed: 0, score: null, full_score: null, error: '',
  created_at: '2026-10-19T10:00:00', finished_at: '2026-10-19T10:03:00', reviewed_at: null,
  files: [{ id: 1, role: 'submission', name: '作业.pdf', pages: 2 }],
  items: [
    item({ id: 101, seq: 1, q_no: '1', qtype: '解答题', verdict: 'correct',
           block_kind: 'question',
           stem: '解方程：$x^2-5x+6=0$。', student_answer: '$x=2$ 或 $x=3$',
           correct_answer: '$x=2$ 或 $x=3$', basis: '因式分解后两根都对',
           kp_name: '一元二次方程', tags: ['因式分解'] }),
    item({ id: 102, seq: 2, q_no: '2', qtype: '证明题', verdict: 'partial',
           block_kind: 'question',
           tail_note: '底部有邻题的残字，这一块可能把下一题的开头也读进来了',
           stem: '如图，$AB=AC$，$D$ 为 $BC$ 中点，求证 $AD\\\\perp BC$。',
           student_answer: '因为 $AB=AC$，$D$ 是中点，所以 $AD$ 垂直 $BC$',
           correct_answer: '用 SSS 证 $\\\\triangle ABD\\\\cong\\\\triangle ACD$，得 $\\\\angle ADB=\\\\angle ADC=90^\\\\circ$',
           basis: '结论对但缺「全等」这一步',
           figure_note: '① 点与字母：A 在顶、B 左下、C 右下、D 在 BC 上（确定）；'
             + '② 连线：AB、AC、AD，其中 AD 画成虚线（确定）；'
             + '③ 记号：AD 与 BC 交点处有一个直角方框（确定）；④ 数值：无（确定）',
           flags: ['low_confidence'], confidence: 'medium' }),
    item({ id: 103, seq: 3, q_no: '3', qtype: '', verdict: 'ungradable',
           block_kind: 'header',
           stem: '', student_answer: '', error_reason: '框里没读到任何作答内容',
           flags: ['region_empty'], confidence: '' }),
  ],
  marks: [
    { id: 201, item_id: 101, page_no: 1, kind: 'check', geo: [120, 300, 40, 40],
      points: [], text: '', color: '#dc2626', created_by: 'ai' },
    { id: 202, item_id: 102, page_no: 1, kind: 'text', geo: [300, 520],
      points: [], text: '缺全等', color: '#dc2626', created_by: 'teacher' },
  ],
  skipped_pages: [],
  regions: [
    { id: 301, page_no: 1, seq: 0, q_no: '1', label: '1', box: [40, 60, 900, 300], skipped: 0 },
    { id: 302, page_no: 1, seq: 1, q_no: '2', label: '2', box: [40, 340, 900, 620], skipped: 0 },
  ],
};

export const lessonFilesApi = {
  upload: async (lessonId, fd, role) => {
    push('uploadFile', [lessonId, role, fd && fd.get ? (fd.get('file') || {}).name : '']);
    return { id: 555, name: '答案照片.png', kind: 'image', role };
  },
};

export const gradeApi = {
  get: async (gid) => { push('get', [gid]); return JSON.parse(JSON.stringify(DETAIL)); },
  list: async (lessonId) => {
    push('list', [lessonId]);
    return [{ id: 7, status: 'done' }, { id: 9, status: 'done' }];
  },
  putRegions: async (gid, body) => {
    push('putRegions', [gid, body]);
    // 真实后端是**整份替换**（page_no / seq 都在每一段上），这里照做。
    // ⚠️ skipped 必须回显：真后端会把它原样带回来（并且把同一题的几段归一），
    //    不回来的话前端 syncRegions() 会把「不送 AI」全部读成 false ——
    //    测试里就变成「跳过了，但一刷新又都回来了」这种假绿。
    DETAIL.regions = body.regions.map((r, i) => ({
      id: 400 + i, page_no: Number(r.page_no), seq: Number(r.seq || 0), seg: 0,
      q_no: r.q_no, label: r.q_no || String(Number(r.seq || 0) + 1), box: r.box,
      skipped: r.skipped ? 1 : 0,
    }));
    return { regions: JSON.parse(JSON.stringify(DETAIL.regions)) };
  },
  copyRegions: async (gid, fromId) => {
    push('copyRegions', [gid, fromId]);
    return { regions: JSON.parse(JSON.stringify(DETAIL.regions)) };
  },
  pageUrl: (gid, pno) => '/api/homework-grades/' + gid + '/page/' + pno,
  exportUrl: (gid, fmt, page) => '/api/homework-grades/' + gid + '/export?fmt=' + fmt + '&page=' + page,
  /** 一块切出来的样子：**现裁、不落盘**（服务端用 page_png + Pillow 裁，多段竖拼）。
   *  桩只负责把 URL 拼对 —— 真正的裁图在 backend，前端只是拿它当 <img src>。 */
  regionImageUrl: (gid, spec) => {
    push('regionImage', [gid, spec]);
    return '/api/homework-grades/' + gid + '/region-image?spec=' + encodeURIComponent(spec);
  },
  /** 整页不送 AI：**整份替换**（不是增量），所以桩也整份覆盖，好断言「点一下真的改了」。 */
  setSkippedPages: async (gid, pages) => {
    push('skippedPages', [gid, pages]);
    DETAIL.skipped_pages = (pages || []).map(Number).slice().sort((a, b) => a - b);
    return { skipped_pages: DETAIL.skipped_pages.slice() };
  },
  patchItem: async (iid, body) => { push('patchItem', [iid, body]); return { ok: 1 }; },
  rerunItem: async (iid) => { push('rerunItem', [iid]); return { ok: 1 }; },
  toBank: async (iid, body) => { push('toBank', [iid, body]); return { ok: 1 }; },
  patchSetup: async (gid, body) => { push('patchSetup', [gid, body]); return JSON.parse(JSON.stringify(DETAIL)); },
  run: async (gid) => { push('run', [gid]); return JSON.parse(JSON.stringify(DETAIL)); },
  confirm: async (gid, note) => { push('confirm', [gid, note]); return JSON.parse(JSON.stringify(DETAIL)); },
  addMark: async (gid, body) => {
    push('addMark', [gid, body]);
    if (CTL.failMark) throw new Error('网络断了');
    return { id: 999 };
  },
  addMarks: async (gid, marks) => { push('addMarks', [gid, marks]); return { marks: [] }; },
  patchMark: async (mid, body) => { push('patchMark', [mid, body]); return { ok: 1 }; },
  removeMark: async (mid) => { push('removeMark', [mid]); return { ok: 1 }; },
};
export const CALLS_REF = CALLS;
`;

const STORE_STUB = `
export const state = {};
export const ok = () => {};
export const fail = () => {};
export const warn = () => {};
`;

/** 视图里写的是 \`from 'vue-router'\`；只用到 useRoute().params.id。 */
const ROUTER_PKG = JSON.stringify({
  name: 'vue-router', version: '0.0.0-stub', type: 'module',
  main: 'index.js', exports: { '.': './index.js' },
}, null, 2);
const ROUTER_STUB = `
export const useRoute = () => ({ params: { id: '7' }, query: {}, path: '/grade/7' });
export const useRouter = () => ({ push: async () => {} });
`;

const VUE_PKG = JSON.stringify({
  name: 'vue', version: '0.0.0-stub', type: 'module',
  main: 'index.js', exports: { '.': './index.js' },
}, null, 2);

const VUE_SHIM = `
const V = globalThis.__VUE__;
if (!V) throw new Error('jsdom window 上没有 Vue —— vendor 没喂进去');
export const computed = (...a) => V.computed(...a);
export const nextTick = (...a) => V.nextTick(...a);
export const ref = (...a) => V.ref(...a);
export const watch = (...a) => V.watch(...a);
export const reactive = (...a) => V.reactive(...a);
export const onMounted = (...a) => V.onMounted(...a);
export const onBeforeUnmount = (...a) => V.onBeforeUnmount(...a);
export const createApp = (...a) => V.createApp(...a);
export default V;
`;

/** 按依赖顺序喂：auto-render 一加载就把 window.katex 抓进闭包，必须排在 katex 后面。 */
const VENDOR = [
  'vendor/vue.global.prod.js',
  'vendor/marked.min.js',
  'vendor/purify.min.js',
  'vendor/katex/katex.min.js',
  'vendor/katex/auto-render.min.js',
];

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

/* ============================== main ============================== */

async function main() {
  console.log('批改台 · 真挂载检查（jsdom 挂 HomeworkGrade.js，数渲染痕迹）');

  const jsdomDir = loadJsdom();
  if (!jsdomDir) {
    console.log('  [SKIP] 没找到 jsdom（可选依赖），跳过');
    return 0;
  }
  console.log('  jsdom: ' + jsdomDir);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'grade_mount_'));
  try {
    // ---- 1. 搭骨架：视图与组件原样复制，依赖换桩 ----
    fs.mkdirSync(path.join(tmp, 'src', 'views'), { recursive: true });
    fs.mkdirSync(path.join(tmp, 'src', 'components'), { recursive: true });
    fs.mkdirSync(path.join(tmp, 'node_modules', 'vue'), { recursive: true });
    fs.mkdirSync(path.join(tmp, 'node_modules', 'vue-router'), { recursive: true });
    const copy = (rel) => {
      fs.mkdirSync(path.dirname(path.join(tmp, rel)), { recursive: true });
      fs.copyFileSync(path.join(ROOT, rel), path.join(tmp, rel));
    };
    copy('src/views/HomeworkGrade.js');
    copy('src/components/GradeMarkLayer.js');
    // 坐标换算与渲染都走真代码 —— 框画在哪儿、公式渲不渲染，全靠这两个
    copy('src/markGeom.js');
    copy('src/mathRender.js');
    // 本机草稿与分割线几何也走真代码：它们是这轮新加的两块，
    // 桩掉就等于「只验了有没有调用，没验算得对不对」。
    copy('src/gradeDraft.js');
    copy('src/gradeLines.js');
    fs.writeFileSync(path.join(tmp, 'src', 'calls.js'), CALLS_STUB);
    fs.writeFileSync(path.join(tmp, 'src', 'api.js'), API_STUB);
    fs.writeFileSync(path.join(tmp, 'src', 'store.js'), STORE_STUB);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'package.json'), VUE_PKG);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'index.js'), VUE_SHIM);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue-router', 'package.json'), ROUTER_PKG);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue-router', 'index.js'), ROUTER_STUB);

    // ---- 2. jsdom + vendor ----
    const dom = new JSDOM('<!DOCTYPE html><html><body><div id="app"></div></body></html>', {
      url: 'http://127.0.0.1:8000/#/grade/7',
      runScripts: 'outside-only',        // 给 window.eval 用；页面内脚本仍然不执行
      pretendToBeVisual: true,
    });
    const W = dom.window;
    global.window = W;
    global.document = W.document;
    global.navigator = W.navigator;
    /* 视图里用 new FormData() 传答案照片（见 uploadAnswerFile）—— jsdom 有，全局没有。
     * ⚠️ `File` / `Blob` 也必须搬成 **jsdom 那一套**：node 22 自己有全局 File，
     *    不搬的话视图里 `new File([剪贴板文件], 'paste-….png')` 会拿到 node 的构造器，
     *    而 `FormData` 是 jsdom 的 —— 两个 realm 的 Blob 互不认，append 时被转成字符串，
     *    `fd.get('file')` 变成 "[object File]"，`.name` 直接 undefined
     *    （2026-10-19 就是这么红的：产品代码是对的，测试跨了两个 realm）。 */
    global.FormData = W.FormData;
    global.File = W.File;
    global.Blob = W.Blob;
    for (const f of VENDOR) {
      const p = path.join(ROOT, f);
      checkTrue('vendor 存在：' + f, fs.existsSync(p));
      if (!fs.existsSync(p)) return 1;
      W.eval(fs.readFileSync(p, 'utf8'));
    }
    global.__VUE__ = W.Vue;
    /* ⚠️ localStorage 必须显式搬过来。浏览器里 `globalThis.localStorage` 就是
     * `window.localStorage`，所以生产代码写 globalThis 是对的；但这里视图模块是
     * **在 node 里 import** 的，node 的 globalThis 上没有 localStorage ——
     * 不搬的话 gradeDraft 会走「环境不支持」的降级分支，草稿断言永远红
     * （2026-10-19 就是这么红的，一开始还以为是草稿没写进去）。 */
    global.localStorage = W.localStorage;
    checkTrue('Vue / marked / katex 都进了 window',
      !!(W.Vue && W.marked && W.katex && W.renderMathInElement),
      ['Vue', 'marked', 'katex', 'renderMathInElement'].filter((k) => !W[k]).join(', ') || '齐全');

    /* jsdom 不下载外链脚本（没开 resources），但 mathRender.ensureLibs() 会建 <script src>
     * 然后等 onload —— 事件永不到，paintAll 就卡在 await 上，公式一条都不会渲。
     * 所以这里把「插 head」拦下来：vendor 已经用 eval 喂进 window 了，
     * 直接把 onload 触发一下即可（等价于「脚本早就加载好了」）。 */
    const head = W.document.head;
    const origAppend = head.appendChild.bind(head);
    head.appendChild = (el) => {
      const r = origAppend(el);
      const tag = String((el && el.tagName) || '').toUpperCase();
      if (tag === 'SCRIPT' || tag === 'LINK') {
        setTimeout(() => { try { if (el.onload) el.onload(); } catch (e) { /* 桩不该炸 */ } }, 0);
      }
      return r;
    };

    // jsdom 的 confirm / prompt 是「未实现就抛」；这页有两处真的会调，先接管
    const asked = [];
    W.confirm = (msg) => { asked.push(String(msg)); return W.__confirmAnswer !== false; };
    W.prompt = (msg) => (W.__promptAnswer === undefined ? '老师备注' : W.__promptAnswer);

    // ---- 3. 挂载真视图 ----
    const mod = await import(pathToFileURL(path.join(tmp, 'src', 'views', 'HomeworkGrade.js')).href);
    const app = W.Vue.createApp(mod.default);
    // 真实应用里 router-link 是全局注册的；这里给个最小的，不然整片导航会消失
    app.component('router-link', {
      props: ['to'],
      template: '<a :href="to"><slot></slot></a>',
    });
    const vm = app.mount('#app');
    await sleep(500);

    const D = W.document;
    const root = D.querySelector('#app');
    /* ⚠️ 必须**每次重新读**，不能 `const body = root.innerHTML` 存一份：
     * 挂载后还会切模式、画框、保存，快照永远停在第一帧，断言就变成「验快照」了。 */
    const body = () => root.innerHTML;
    checkTrue('视图真的挂起来了（不是只剩未编译的模板）',
      !body().includes('{{ ') && !body().includes('v-else-if=')
      && /class="g-item/.test(body()),
      '含 ' + body().length + ' 字符');
    checkTrue('页头渲染出来了', (root.querySelector('h1') || {}).textContent === '作业批改',
      (root.querySelector('h1') || {}).textContent);

    /* ============ 4. 逐题卡片：文字、公式、判定 ============ */
    const cards = () => Array.from(D.querySelectorAll('.g-item'));
    check('逐题卡片 3 张', cards().length, 3);
    checkTrue('★ 题干公式真的渲染了（.katex ≥ 2）',
      D.querySelectorAll('.g-rich .katex').length >= 2,
      D.querySelectorAll('.g-rich .katex').length + ' 个');
    const richtexts = Array.from(D.querySelectorAll('.g-rich')).map((el) => el.textContent || '');
    checkTrue('★ 容器里不再残留 $…$ 原文（= 渲染真生效）',
      !richtexts.some((t) => /\$[^$]+\$/.test(t)),
      richtexts.map((t) => t.slice(0, 20)).join(' | '));
    check('判定标签按 对/部分对/判不了 显示',
      Array.from(D.querySelectorAll('.g-verdict')).map((e) => (e.textContent || '').trim()),
      ['对', '部分对', '判不了']);
    checkTrue('判不了必须说清为什么（不能只给一个灰点）',
      (D.querySelector('.g-ungradable') || {}).textContent
        && D.querySelector('.g-ungradable').textContent.includes('框里没读到任何作答内容'),
      (D.querySelector('.g-ungradable') || {}).textContent);

    /* ============ 5. 图上读到的（本轮新加） ============ */
    const figs = () => Array.from(D.querySelectorAll('.g-figure'));
    check('★ 只有几何那道题多出「图上读到的」（1 块，其余两题没有空壳）', figs().length, 1);
    checkTrue('★ figure_note 原样上屏，把握标注也在（（确定）能作数）',
      figs().length && figs()[0].textContent.includes('（确定）')
      && figs()[0].textContent.includes('直角方框'),
      figs().length ? figs()[0].textContent.slice(0, 60) : '无');
    checkTrue('「图上读到的」标题把三分档口径写给老师看',
      body().includes('（确定）能作数 /（看不清）只作疑'));

    /* ============ 6. 标记的显示名 ============ */
    checkTrue('★ 新标记 region_empty 有中文名（不是兜底的「需留意」）',
      body().includes('框里没读到题'), '');

    /* ============ 7. 框题：默认不显示，切过去才显示 ============ */
    const layer = () => D.querySelector('.gmark-layer');
    checkTrue('标注层挂上了', !!layer());
    check('默认「选择」模式下不画框（平时不该满页虚线框）',
      D.querySelectorAll('.gmark-regions').length, 0);
    checkTrue('工具条上有「框题」按钮', !!Array.from(D.querySelectorAll('.g-tool'))
      .find((b) => (b.textContent || '').trim() === '框题'));

    const btnRegion = Array.from(D.querySelectorAll('.g-tool'))
      .find((b) => (b.textContent || '').trim() === '框题');
    btnRegion.click();
    await sleep(150);
    checkTrue('★ 点「框题」后框题面板出现', !!D.querySelector('.grade-regions'));
    checkTrue('提示文案说了「宁大勿小」（切掉笔画读不回来）',
      body().includes('框宁大勿小') || (D.querySelector('.grade-toolhint') || {}).textContent.includes('宁大勿小'),
      (D.querySelector('.grade-toolhint') || {}).textContent);
    check('★ 服务器上已存的 2 个框画出来了', D.querySelectorAll('.rg-rect').length, 2);
    check('框的编号标签 2 个', D.querySelectorAll('.gmark-rlabel').length, 2);
    check('编号按 q_no 显示',
      Array.from(D.querySelectorAll('.gmark-rlabel')).map((e) => (e.textContent || '').trim()),
      ['1 ×', '2 ×']);
    check('面板里 2 行可改题号', D.querySelectorAll('.gr-row').length, 2);
    check('框的坐标按 0~1000 直接铺（不是像素）',
      [D.querySelectorAll('.rg-rect')[0].getAttribute('x'),
        D.querySelectorAll('.rg-rect')[0].getAttribute('width')],
      ['40', '860']);

    /* ============ 8. 本地画框：草稿 id 是 null，这里最容易撞 key ============ */
    // 三块互不相同的位置，靠 y 区分 —— 撞 key 的话「删中间」会删错格子
    vm.addRegionBox([40, 640, 900, 700]);
    vm.addRegionBox([40, 720, 900, 780]);
    vm.addRegionBox([40, 800, 900, 860]);
    await sleep(150);
    check('★ 画了 3 个草稿框 → 一共 5 个', D.querySelectorAll('.rg-rect').length, 5);
    check('草稿框的编号顺下来', 
      Array.from(D.querySelectorAll('.gmark-rlabel')).map((e) => (e.textContent || '').trim()),
      ['1 ×', '2 ×', '3 ×', '4 ×', '5 ×']);
    checkTrue('有未保存提示', body().includes('未保存'));

    // 删中间那个草稿（y=720 那块），剩下的应当是 640 / 800 / 860
    const mid = vm.editRegions.find((r) => r.box[1] === 720);
    checkTrue('能找到中间那个草稿框', !!mid);
    vm.dropRegion(mid);
    await sleep(150);
    check('删掉中间一个 → 剩 4 个', D.querySelectorAll('.rg-rect').length, 4);
    check('★ 删的是那一块（剩下的是 60/340/640/800，不是 60/340/640/720 这种删错格子）',
      Array.from(D.querySelectorAll('.rg-rect')).map((e) => Number(e.getAttribute('y'))),
      [60, 340, 640, 800]);
    check('删完编号顺上来（不留 1、2、4、5 像丢了一个）',
      Array.from(D.querySelectorAll('.gmark-rlabel')).map((e) => (e.textContent || '').trim()),
      ['1 ×', '2 ×', '3 ×', '4 ×']);

    // 改题号：框里批出来的题都记到这个号上
    vm.setRegionQNo(vm.editRegions[0], '7');
    await sleep(150);
    check('改了题号，图上标签跟着变',
      (D.querySelectorAll('.gmark-rlabel')[0].textContent || '').trim(), '7 ×');

    /* ============ 9. 保存：整页替换，发的是这一份的完整列表 ============ */
    // 按钮文案是「保存分割」（框选与分割线共用同一块面板，2026-10-19 起）。
    const saveBtn = Array.from(D.querySelectorAll('.grade-regions button'))
      .find((b) => /保存(框|分割)/.test((b.textContent || '').trim()));
    checkTrue('面板上有保存按钮（框选/分割线共用）', !!saveBtn);
    saveBtn.click();
    await sleep(300);
    const puts = (await import(pathToFileURL(path.join(tmp, 'src', 'calls.js')).href)).CALLS
      .filter((c) => c.name === 'putRegions');
    check('保存框发了 1 次请求', puts.length, 1);
    if (puts.length) {
      const body0 = puts[0].args[1];
      check('★ 整份替换：带 grouped=true（显式声明按 seq 分组）', body0.grouped, true);
      check('每段都带 page_no（跨页题全靠它）',
        body0.regions.map((r) => r.page_no), [1, 1, 1, 1]);
      check('每段都带 seq（同一题的几段共用）',
        body0.regions.map((r) => Number(r.seq)), [0, 1, 2, 3]);
      check('整份替换：传的是全部 4 个框（不是只传改动的）', body0.regions.length, 4);
      check('每个框都是 [x0,y0,x1,y1] 四个数',
        body0.regions.map((r) => (Array.isArray(r.box) ? r.box.length : -1)), [4, 4, 4, 4]);
      check('改了号的框按新题号提交', body0.regions[0].q_no, '7');
      // 原来那两个框本来就带题号 1 / 2（服务器上下来的），只有新画的两个是空串
      check('没改号的框原样提交空串（交给后端按顺序编）',
        body0.regions.slice(1).map((r) => r.q_no), ['2', '', '']);
    }
    await sleep(200);
    checkTrue('保存完「未保存」提示消失', !body().includes('未保存'));

    /* ============ 9b. 跨页题：第 2 页画一段 → 并入上一题 ============
     * 这是「参考批量录题的分割/跨页」那批改动的界面部分。要证的三件事：
     *   1. 草稿是**整份按页分桶**的 —— 翻页不丢（否则跨页根本画不出来）
     *   2. 「并入上一题」真的把两段合成一题，面板上看得见「跨第 1 + 2 页」
     *   3. 提交时同一题的几段**共用同一个 seq**，后端靠它拼成一张图 */
    check('保存后草稿是 4 段', vm.draftSegCount, 4);
    vm.pageNo = 2;
    await sleep(150);
    check('★ 翻到第 2 页，草稿还在（整份分桶，不是当前页一份）', vm.draftSegCount, 4);
    check('第 2 页本身还没框', vm.editRegions.length, 0);

    vm.addRegionBox([40, 60, 900, 380]);
    await sleep(150);
    check('第 2 页画完，总框数 +1', vm.draftSegCount, 5);
    check('现在 5 题（每段各成一题）', vm.regionBlocks.length, 5);

    const tailBlock = vm.regionBlocks[vm.regionBlocks.length - 1];
    vm.mergeRegion(tailBlock);
    await sleep(150);
    check('★ 并入上一题：题数 5 → 4', vm.regionBlocks.length, 4);
    const crossBlock = vm.regionBlocks[vm.regionBlocks.length - 1];
    check('★ 这一题变成两段', crossBlock.segs.length, 2);
    check('★ 两段落在不同页上（跨页题才成立）', crossBlock.pages, [1, 2]);
    checkTrue('面板上标出「跨第 1 + 2 页」', body().includes('跨第 1 + 2 页'));

    await vm.saveRegions();
    await sleep(250);
    const puts2 = (await import(pathToFileURL(path.join(tmp, 'src', 'calls.js')).href)).CALLS
      .filter((c) => c.name === 'putRegions');
    const b2 = puts2[puts2.length - 1].args[1];
    const crossSegs = b2.regions.filter((r) => Number(r.seq) === Number(crossBlock.seq));
    check('★ 跨页的两段共用同一个 seq（后端靠它拼成一张）', crossSegs.length, 2);
    check('★ 那两段的 page_no 是 1 和 2',
      crossSegs.map((r) => r.page_no), [1, 2]);

    vm.pageNo = 1;
    await sleep(120);

    /* ============ 10. 离开框题模式：没保存不能悄悄丢 ============ */
    // ⚠️ 顺序要注意：confirm 返回 true 的那一下**真的会切走**，所以「不放弃」这条
    //    必须先把模式切回 region 再测，否则 setTool('view') 时 tool 已经是 view、
    //    守卫直接跳过，测出来的是假的「没留在 region」。
    vm.addRegionBox([40, 880, 900, 940]);
    await sleep(120);
    asked.length = 0;
    W.__confirmAnswer = false;            // 老师选「算了，不放弃」
    vm.setTool('view');
    await sleep(120);
    check('★ 有未保存的框，离开框题模式必须问一句', asked.length, 1);
    // 文案 2026-10-19 起是「这些框/分割线还没保存…（不急：已经存在本机）」
    checkTrue('问的是「还没保存」且说清「本机还在」',
      /还没保存/.test(asked[0] || '') && /本机/.test(asked[0] || ''), asked[0]);
    check('选了「不放弃」→ 还留在框题模式', vm.tool, 'region');
    check('还留在框题模式时框还画着（4 个已存 + 刚画的 1 个）',
      D.querySelectorAll('.rg-rect').length, 5);
    W.__confirmAnswer = true;
    asked.length = 0;
    vm.setTool('view');
    await sleep(120);
    check('选了「放弃」→ 真的切走了', vm.tool, 'view');
    check('切走后框不再画（.gmark-regions 消失）', D.querySelectorAll('.gmark-regions').length, 0);

    /* ============ 11. 沿用上一份：先取同课时的列表，再拷别人的 ============ */
    asked.length = 0;
    vm.setTool('region');
    await sleep(150);
    const copyBtn = Array.from(D.querySelectorAll('.grade-regions button'))
      .find((b) => (b.textContent || '').trim() === '沿用上一份');
    checkTrue('面板上有「沿用上一份」按钮', !!copyBtn);
    copyBtn.click();
    await sleep(300);
    const calls = (await import(pathToFileURL(path.join(tmp, 'src', 'calls.js')).href)).CALLS;
    checkTrue('★ 沿用前先按 lesson_id 取列表（不猜 id —— 猜错会把别人的卷面当模板）',
      calls.some((c) => c.name === 'list' && c.args[0] === 31),
      JSON.stringify(calls.filter((c) => c.name === 'list').map((c) => c.args)));
    checkTrue('★ 拷的是**另一份**（id 7 自己排除掉），不是自己拷自己',
      calls.some((c) => c.name === 'copyRegions' && c.args[1] === 9),
      JSON.stringify(calls.filter((c) => c.name === 'copyRegions').map((c) => c.args)));

    /* ============ 12. 逐题卡片上的操作仍然通着 ============ */
    // 判定按钮：点「错」应该 PATCH 到 grade-items/<id>
    const wrongBtn = Array.from(cards()[0].querySelectorAll('.g-vbtn'))
      .find((b) => (b.textContent || '').trim() === '错');
    checkTrue('第一题有「错」判定按钮', !!wrongBtn);
    wrongBtn.click();
    await sleep(200);
    const patches = calls.filter((c) => c.name === 'patchItem');
    checkTrue('★ 点判定 → PATCH 到那一题（不是别的题）',
      patches.some((c) => c.args[0] === 101 && c.args[1].verdict === 'wrong'),
      JSON.stringify(patches.map((c) => c.args)));

    // 「只重批这一题」：必须带 regions 一起重批，否则会退回整页口径覆盖掉好的结果
    const rrBtn = Array.from(cards()[1].querySelectorAll('button'))
      .find((b) => (b.textContent || '').trim() === '只重批这一题');
    checkTrue('第二题有「只重批这一题」', !!rrBtn);
    rrBtn.click();
    await sleep(300);
    checkTrue('★ 单题重批打到那一题的接口', calls.some((c) => c.name === 'rerunItem' && c.args[0] === 102),
      JSON.stringify(calls.filter((c) => c.name === 'rerunItem').map((c) => c.args)));

    /* ============ 15. 判定依据传照片（答案 / 细则可以是一张图） ============
     * 这条链路后端已经通了（`_answer_images` 把图送进模型），界面要保证：
     *   1. 可编辑时真的有入口；
     *   2. `input` 上**没有** accept（本机经验：accept 会把老师想选的文件藏起来）；
     *   3. 上传后文件 id **立刻**挂进判定依据，而不是等老师再点一次保存。
     * 第 3 条最容易漏 —— 传完看着成功了，其实图没挂上，批的时候还在按旧依据判。 */
    vm.detail.status = 'queued';
    vm.detail.done = 0;
    vm.detail.answer_images = { answer: [], rubric: [] };
    vm.setupMode = 'key';
    await sleep(180);
    checkTrue('可编辑时出现「传照片」入口', body().includes('直接传照片'));

    const fileEl = D.querySelector('.grade-ansfile input[type="file"]');
    checkTrue('有 file input', !!fileEl);
    checkTrue('★ file input 上不设 accept（设了会把老师要选的文件藏起来）',
      !!fileEl && !fileEl.getAttribute('accept'));
    if (fileEl) {
      const f = new W.File([new W.Blob(['x'], { type: 'image/png' })], '答案照片.png',
        { type: 'image/png' });
      Object.defineProperty(fileEl, 'files', { value: [f], configurable: true });
      fileEl.dispatchEvent(new W.Event('change'));
      await sleep(400);
    }
    const calls2 = (await import(pathToFileURL(path.join(tmp, 'src', 'calls.js')).href)).CALLS;
    const ups = calls2.filter((c) => c.name === 'uploadFile');
    check('★ 答案照片走了这一课的文件接口', ups.length, 1);
    check('★ role 用 homework（与提交件同一份清单，不会变成没人认领的孤儿文件）',
      ups.length ? ups[0].args[1] : null, 'homework');
    const ps = calls2.filter((c) => c.name === 'patchSetup');
    const lastPatch = ps.length ? ps[ps.length - 1].args[1] : {};
    check('★ 上传完立刻把文件 id 挂进判定依据（不是等老师再点保存）',
      lastPatch.answer_file_ids || [], [555]);

    /* ============ 16. 逐题分割：画线切开 ============
     * 用户 2026-10-19 原话：「借鉴录题代码，在此处实现：逐题分割（当前是框选，
     * 我还要分割线分割）」。这里要证的三件事，静态断言一个都看不出来：
     *   ① 线真的上了屏（`.gl-line` 在不在）；
     *   ② 两条线切出的是 **3 段**（不是 2 段 —— 全切语义，第一条线以上也算）；
     *   ③ 切出来的段是**整页宽**的（x0=0 / x1=1000）—— 后端零改动的全部理由就在这。
     * 先清空：前面几节攒了一堆框，混在一起算不清是「线切的」还是「框选的」。 */
    vm.clearRegions();
    await sleep(150);
    check('清空后一段不剩（线也要一起清，否则重切会长回来）', vm.draftSegCount, 0);

    // 显式回到第 2 页 —— 前面几节翻过页，别赌当前页是哪个
    vm.goPage(2);
    await sleep(200);
    check('当前在第 2 页', vm.pageNo, 2);

    vm.setTool('line');
    await sleep(160);
    check('工具条上切到「逐题分割」', vm.tool, 'line');
    checkTrue('分割线面板出现（与框题共用一块）', !!D.querySelector('.grade-regions'));
    checkTrue('面板文案说了「逐题点一下」', body().includes('逐题点一下'));
    check('还没画线 → 一条线都没有', D.querySelectorAll('.gl-line').length, 0);
    check('还没画线 → 也不该凭空出现段（幽灵段 bug 的现场）', vm.draftSegCount, 0);

    vm.addLine(300);
    await sleep(120);
    vm.addLine(700);
    await sleep(150);
    check('★ 两条线 → 3 段（全切，第一条线以上也算一段）', vm.regionBlocks.length, 3);
    check('★ 线真的画上屏了', D.querySelectorAll('.gl-line').length, 2);
    check('段带也上屏了（让老师数得清切成了几题）', D.querySelectorAll('.gl-band').length, 3);
    check('★ 切出来的段是整页宽（x0=0、x1=1000）—— 后端一个字都不用改',
      vm.editRegions.filter((s) => s.src === 'line').map((s) => [s.box[0], s.box[2]]),
      [[0, 1000], [0, 1000], [0, 1000]]);
    check('段的上下边界就是两条线',
      vm.editRegions.filter((s) => s.src === 'line').map((s) => [s.box[1], s.box[3]]),
      [[0, 300], [300, 700], [700, 1000]]);

    // 离已有线太近必须拦住：切出来的段会比后端 REGION_MIN_SIDE 还薄 → 422
    const lineN = vm.pageLines.length;
    vm.addLine(305);
    await sleep(100);
    check('★ 离已有的线太近 → 不加（否则切出 5 单位高的段，后端 422）',
      vm.pageLines.length, lineN);

    // 点线上的 × 删一条
    const lineXs = D.querySelectorAll('.gl-line-x');
    check('线上有删除按钮（点击线本身不删，见标注层文件头 ⑦）', lineXs.length, 2);
    lineXs[0].click();
    await sleep(180);
    check('★ 点 × 删掉一条线 → 回到 2 段', vm.draftSegCount, 2);
    check('线的数量跟着少了', D.querySelectorAll('.gl-line').length, 1);

    /* ============ 17. 分割线也能跨页合并 ============
     * 用户的第二问：「跨页分割的题目合并」。第 1 页末尾切一段、第 2 页顶上切一段，
     * 点「并入上一题」之后必须变成 **1 题 2 段**（后端靠同一个 seq 竖拼成一张图）。
     * 此时第 2 页已有 2 段（上面那节剩的），再到第 1 页切 2 段。 */
    vm.goPage(1);
    await sleep(220);
    vm.addLine(600);
    await sleep(200);
    check('第 1 页切开 → 整份 4 段（第 2 页 2 段 + 第 1 页 2 段）', vm.draftSegCount, 4);
    const p2Blocks = vm.regionBlocks.filter((b) => b.pages[0] === 2);
    checkTrue('第 2 页有可合并的段', p2Blocks.length > 0, p2Blocks.length);
    const beforeCross = vm.regionBlocks.length;
    if (p2Blocks.length) {
      vm.mergeRegion(p2Blocks[0]);
      await sleep(250);
    }
    check('★ 合并后题数少一题', vm.regionBlocks.length, beforeCross - 1);
    const crossBlocks = vm.regionBlocks.filter((b) => b.pages.length > 1);
    check('★ 有一条真跨页题（1 题 2 段）', crossBlocks.length, 1);
    check('★ 跨页的两段落在不同页上', crossBlocks.length ? crossBlocks[0].pages : [], [1, 2]);

    /* ============ 17b. 看得见图 / 能不送 AI / 每页尾部 ============
     * 用户 2026-10-19 原话：「分割截图完后看不到图，没办法指定哪些图不传给ai，
     * 每页尾部处理也不到位」。三件事都发生在**点保存之前** ——
     * 以前右栏只有「第 1 页 / 跨第 1+2 页」两句话，切歪了得等模型判完才知道。
     * 所以这一节刻意留在「还没保存分割」的状态里断言。 */
    checkTrue('★ 每一块都有缩略图（以前这里只有一行字 —— 切歪了看不出来）',
      vm.regionBlocks.length > 0
      && D.querySelectorAll('.gr-thumb img').length === vm.regionBlocks.length,
      D.querySelectorAll('.gr-thumb img').length + ' / ' + vm.regionBlocks.length);
    const thumbSrcs = Array.from(D.querySelectorAll('.gr-thumb img'))
      .map((el) => String(el.getAttribute('src')));
    checkTrue('★ 缩略图走 /region-image 现裁（不是先把图存下来再引用）',
      thumbSrcs.length > 0 && thumbSrcs.every((u) => u.includes('/region-image?spec=')), thumbSrcs);
    checkTrue('缩略图懒加载（一页七八十块不能一起裁）',
      Array.from(D.querySelectorAll('.gr-thumb img'))
        .every((el) => el.getAttribute('loading') === 'lazy'));

    // `spec` 是前后端唯一的契约：`页号:x0,y0,x1,y1`，多段用 `;` 连（后端按它裁图 + 竖拼）
    const specOf = (b) => decodeURIComponent(String(vm.blockPreview(b)).split('spec=')[1] || '');
    const b0 = vm.regionBlocks[0];
    check('★ spec 拼对了（页号:框，跨页用 ; 连）—— 后端就按这个裁',
      specOf(b0), b0.segs.map((s) => s.page_no + ':' + s.box.join(',')).join(';'));
    const crossB = vm.regionBlocks.find((b) => b.segs.length > 1);
    checkTrue('★ 跨页块的 spec 是两段（后端靠它竖拼成一张图）',
      !!crossB && specOf(crossB).split(';').length === crossB.segs.length,
      crossB ? specOf(crossB) : '没有跨页块');

    // 点缩略图看大图：不引弹窗库，新开页签看原尺寸
    const opened = [];
    W.open = (u) => { opened.push(String(u)); return null; };
    D.querySelectorAll('.gr-thumb img')[0].click();
    await sleep(150);
    check('★ 点缩略图能看大图', opened.length, 1);
    check('看的就是这一块（与缩略图同一个 URL）',
      opened[0], String(D.querySelectorAll('.gr-thumb img')[0].getAttribute('src')));

    /* ---- 按块不送 AI ----
     * ⚠️ 只改草稿、**不删框**：老师常是先怀疑「这块判错了」，回头又想要 ——
     *    删了框就得从头再切一遍。
     * ⚠️ 跨页题必须**两段一起跳**：只跳上半段等于只批半道题（下半段照样送）。
     * ⚠️ 必须验到「上传的 body 里有它」，只在界面上标着不算。 */
    const segN = vm.draftSegCount;
    if (crossB) {
      vm.toggleBlockSkip(crossB);
      await sleep(180);
      const now = vm.regionBlocks.find((b) => b.seq === crossB.seq) || {};
      check('★ 跨页块点一下 → 整块标跳过', !!now.skipped, true);
      check('★ 两段都标了（只跳上半段等于只批半道题）',
        (now.segs || []).map((s) => !!s.skipped), [true, true]);
      check('★ 跳过**不删框**（框还在，随时能恢复）', vm.draftSegCount, segN);
      check('别的块不受影响（一次只跳这一块）',
        vm.regionBlocks.filter((b) => b.skipped).length, 1);
      check('界面上标出「不送 AI」（老师看得见哪块没送）',
        D.querySelectorAll('.gr-off-tag').length, 1);
      checkTrue('按钮变成「不判 · 恢复」（点完得看出成功了）',
        body().includes('不判 · 恢复'), body().includes('这块不判'));

      // 保存时随框一起上传 —— 否则「跳过了」只活到下一次刷新
      const nPut = calls2.filter((c) => c.name === 'putRegions').length;
      await vm.saveRegions();
      await sleep(320);
      const puts = calls2.filter((c) => c.name === 'putRegions');
      check('保存分割 → 走 putRegions', puts.length, nPut + 1);
      check('★ 跳过状态随框一起上传（不是只在界面上标着）',
        puts[puts.length - 1].args[1].regions.filter((r) => r.skipped).length,
        crossB.segs.length);
      check('★ 后端回包带着 skipped，刷新后还认（没被静默清成 false）',
        vm.regionBlocks.filter((b) => b.skipped).length, 1);

      // 撤回来：恢复之后这一块要真的回到「要判」
      const back = vm.regionBlocks.find((b) => b.seq === crossB.seq) || {};
      vm.toggleBlockSkip(back);
      await sleep(180);
      check('★ 再点一下 → 两段都恢复（这块重新送去批改）',
        (vm.regionBlocks.find((b) => b.seq === crossB.seq) || {}).segs.map((s) => !!s.skipped),
        [false, false]);
      check('恢复后「不送 AI」标签消失', D.querySelectorAll('.gr-off-tag').length, 0);
    }

    /* ---- 按页不送 AI（整页）----
     * 与按块的区别是**粒度**：封页、空白作答页、重复扫描页没必要一题题去框。
     * 它走独立接口、**立即落库**，不跟着还没保存的框草稿走 ——
     * 老师说「这页不看」的那一刻就该生效，混在草稿里很容易漏。 */
    vm.goPage(2);
    await sleep(240);
    check('第 2 页默认要判', vm.pageSkipped, false);
    const nSk = () => calls2.filter((c) => c.name === 'skippedPages').length;
    const skBefore = nSk();
    await vm.togglePageSkip();
    await sleep(220);
    check('★ 点「本页不判」→ 打独立接口（不跟着「保存分割」走）', nSk(), skBefore + 1);
    const skCalls = calls2.filter((c) => c.name === 'skippedPages');
    check('★ 传的是这一页的页号', skCalls[skCalls.length - 1].args[1], [2]);
    check('★ 接口一回来就生效（不用等保存）', vm.pageSkipped, true);
    checkTrue('按钮变成「本页不判 · 恢复」', body().includes('本页不判 · 恢复'));

    await vm.togglePageSkip();
    await sleep(220);
    const skCalls2 = calls2.filter((c) => c.name === 'skippedPages');
    check('★ 再点一次 → 整份替换成空（不是又追加一遍）',
      skCalls2[skCalls2.length - 1].args[1], []);
    check('恢复后这页又要判了', vm.pageSkipped, false);

    /* ---- 每页尾部：结束线 ----
     * 「每页尾部处理也不到位」的现场：最后一题一直写到页底，而页底还有
     * 页脚 / 页码 / 条形码 —— 末段把它们一起裁进去送模型。
     * 结束线就是「这页切到哪儿为止」这唯一一个旋钮，**不新增任何数据形态**
     * （最后一段的底边就是它）。 */
    vm.goPage(1);
    await sleep(240);
    check('默认切到页底（老行为，老数据零变化）', vm.pageEnd, 1000);
    check('默认不画页脚斜纹带', vm.pageTail, null);

    vm.setPageEnd(960);
    await sleep(240);
    check('★ 拖到 96% → 结束线记下了', vm.pageEnd, 960);
    const p1lines = vm.editRegions.filter((s) => s.src === 'line');
    check('★ 末段底边真的缩到 960（下面 4% 不进任何块、不送 AI）',
      Math.max(...p1lines.map((s) => s.box[3])), 960);
    check('★ 页脚斜纹带上屏（老师看得见哪一截被排除了）',
      D.querySelectorAll('.gl-tail').length, 1);
    check('滑块旁边写的是 96%（单位是百分比，不是 0~1000）',
      body().includes('96%'), true);
    check('说明了下面多少不进任何块', body().includes('不进任何块'), true);
    /* ★★ 这条是这轮最容易漏的：`resliceOne` 会把这一页的线段**全部重建**，
     *    跨页题靠 `seq` 分组 —— 不继承的话，老师「并好跨页题 → 顺手拖一下尾部滑块」
     *    就把题拆成两道（一道只有上半页、一道只有下半页），而界面上只表现为
     *    「题数多了一题」。 */
    check('★ 拖结束线**不会把跨页题拆开**（上半段继承原来的题号）',
      vm.regionBlocks.filter((b) => b.pages.length > 1).length, 1);

    // 太靠上必须拦住：第 1 页最后一条线在 600，切到 610 只剩 10 单位 → 后端直接 422
    vm.setPageEnd(610);
    await sleep(180);
    check('★ 结束线贴到最后一条线上 → 拦住，不改（否则切出 10 单位高的段，后端 422）',
      vm.pageEnd, 960);
    check('末段底边也没跟着变',
      Math.max(...vm.editRegions.filter((s) => s.src === 'line').map((s) => s.box[3])), 960);

    vm.resetPageEnd();
    await sleep(240);
    check('★ 点「到页底」→ 回到 1000', vm.pageEnd, 1000);
    check('斜纹带跟着消失（不留一条永远在的灰带）',
      D.querySelectorAll('.gl-tail').length, 0);


    /* ============ 18. 判定依据：Ctrl+V 粘贴一张图 ============
     * 用户的需求原文是「上传图片也可是缓存中的图片」—— 截图之后直接粘，
     * 不用先存成文件再找。剪贴板里的图常常**没有文件名**，所以顺带验一下补名。
     *
     * ⚠️ 这里**直接调 `onPaste`**，不 dispatch 真事件：jsdom 的 ClipboardEvent
     *    不允许给 `clipboardData` 赋值（只读、永远 null），造出来的事件里根本没有图 ——
     *    那样测的是「jsdom 能不能造剪贴板」，不是我们的解析逻辑。
     *    「监听确实绑上了 window」由 onMounted 里那行 addEventListener 保证（静态可查）。 */
    /* ⚠️ 这里必须**重新把状态掰回「还没开批」**，否则 `onPaste` 会在第一行就 return。
     *    原因不在产品代码：桩里的 `patchSetup` 一律返回一份 DETAIL 克隆（status 写死
     *    'reviewing'），而第 15 节的上传成功后会 `detail.value = await patchSetup(...)`
     *    —— 于是「可编辑」被这份假回包冲掉了。真后端此时返回的仍是 queued
     *    （没跑过的任务哪儿来的 reviewing）。所以这行是**桩的补丁，不是产品的补丁**。 */
    vm.detail.status = 'queued';
    vm.detail.done = 0;
    vm.setupMode = 'key';
    await sleep(120);

    const upsBefore = calls2.filter((c) => c.name === 'uploadFile').length;
    const pasteFile = new W.File([new W.Blob(['x'], { type: 'image/png' })], '',
      { type: 'image/png' });                 // 空文件名 = 剪贴板的真实样子
    let prevented = false;
    vm.onPaste({
      clipboardData: {
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => pasteFile }],
        files: [],
      },
      preventDefault: () => { prevented = true; },
    });
    await sleep(500);
    const calls3 = (await import(pathToFileURL(path.join(tmp, 'src', 'calls.js')).href)).CALLS;
    const ups3 = calls3.filter((c) => c.name === 'uploadFile');
    check('★ 粘贴的图也走了上传（不用先存成文件）', ups3.length, upsBefore + 1);
    check('★ 粘到图才 preventDefault（贴文字必须放行给输入框）', prevented, true);
    checkTrue('★ 剪贴板里的图没有文件名 → 自动补一个 png（否则后端按扩展名判不出来）',
      ups3.length > upsBefore && /\.png$/.test(String(ups3[ups3.length - 1].args[2])),
      ups3.length ? ups3[ups3.length - 1].args[2] : '');

    // 贴的是**文字**时必须原样放行 —— 这是往答案框里贴题目的最常见操作
    let preventedText = false;
    vm.onPaste({
      clipboardData: { items: [{ kind: 'string', type: 'text/plain', getAsFile: () => null }], files: [] },
      preventDefault: () => { preventedText = true; },
    });
    await sleep(120);
    check('★ 贴文字不拦（老师往答案框里贴题目是常规操作）', preventedText, false);

    /* ============ 19. 本机草稿：画过的东西真的落盘了 ============
     * 这是「不小心退出，记录就没了」这条的正解。挂了这么久都没丢过，
     * 就是因为 step 16~18 的每一次 addLine / mergeRegion 都会 touch 一次草稿。 */
    const dkey = 'shike:grade-draft:7';
    const rawDraft = W.localStorage.getItem(dkey);
    checkTrue('★ 本机草稿存在（退出再回来能捡回来）', !!rawDraft);
    if (rawDraft) {
      let d = null;
      try { d = JSON.parse(rawDraft); } catch (e) { d = null; }
      checkTrue('草稿里存了分割线（不是只存了框）',
        !!(d && d.regions && Object.keys(d.regions.lines || {}).length), d && d.regions);
      checkTrue('草稿带版本号（结构变了能识别旧数据）', !!(d && d.v === 1), d && d.v);
    }

    /* ============ 20. 「画了一笔就退出」→ 那一笔不能丢 ============
     * 用户的第一个问题原话：「我用笔画批改作业时，不小心退出，记录就没了，得重新批改」。
     * 现场是这样没的：每画一笔立刻 `POST /marks`，关页面时请求被浏览器取消；
     * 或者请求失败只弹一个 toast —— 下一轮 `settle()` 一刷，屏上那一笔就消失了。
     *
     * 正解的顺序**不能反**：先落本机 → 乐观显示 → 再 POST。这里就按这个顺序验：
     *   ① 断网时画一笔：屏上还在、本机队列里有、页面顶上看得见「还没传上去」；
     *   ② 网好了：一次**批量**补发（不是一笔一个请求），补完队列清空；
     *   ③ 库里已经有一模一样的一笔时**不重复补发**（否则每开一次页面多一笔）。 */
    const API = await import(pathToFileURL(path.join(tmp, 'src', 'api.js')).href);
    API.CTL.failMark = true;                  // 断网
    const nBefore = vm.pageMarks.length;
    const qBefore = vm.pendingMarks.length;
    await vm.addMark({ kind: 'cross', geo: [150, 620, 70, 70] });
    await sleep(250);
    check('★ 传不上去的那一笔：屏上还画着（乐观显示，不是等服务器回话）',
      vm.pageMarks.length, nBefore + 1);
    check('★ 它在本机队列里等着', vm.pendingMarks.length, qBefore + 1);
    check('★ 队列里那条带 local: 前缀（界面上认得出它还没落库）',
      vm.pageMarks.filter((m) => String(m.id || '').startsWith('local:')).length, 1);
    checkTrue('★ 页头提示「还没传上去」（老师知道不是白画了）',
      !!D.querySelector('.grade-draft-tip')
      && /1 笔/.test(D.querySelector('.grade-draft-tip').textContent || ''),
      (D.querySelector('.grade-draft-tip') || {}).textContent);

    API.CTL.failMark = false;                 // 网络回来了
    const batchBefore = calls3.filter((c) => c.name === 'addMarks').length;
    const singleBefore = calls3.filter((c) => c.name === 'addMark').length;
    const flushed = await vm.flushPending(true);
    await sleep(250);
    check('★ 网络恢复后把那一笔补上去了', flushed, 1);
    check('★ 走的是批量接口（刚打开页面那几秒最不稳，不能一笔一个请求）',
      calls3.filter((c) => c.name === 'addMarks').length, batchBefore + 1);
    check('补发没有再造一条单笔请求（否则同一笔会落两次）',
      calls3.filter((c) => c.name === 'addMark').length, singleBefore);
    check('★ 补发成功后本机队列清空', vm.pendingMarks.length, 0);
    check('队列空了 → 页头那条提示也就不再挂着（不留一条永远在的红字）',
      D.querySelectorAll('.grade-draft-tip').length, 0);
    const sentBody = calls3.filter((c) => c.name === 'addMarks').pop();
    checkTrue('★ 补发的正文带着坐标和工具类型（不是只发个空壳）',
      !!(sentBody && sentBody.args[1] && sentBody.args[1][0]
         && String(sentBody.args[1][0].kind) === 'cross'
         && (sentBody.args[1][0].geo || []).length === 4),
      sentBody && sentBody.args[1]);

    /* 库里已经有一模一样的一笔 → 不许重复补发。
     * 现场是「POST 其实成功了，但 `settle()` 还没回来页面就关了」——
     * 本机队列里留着一条，服务端也有一条。要是照发不误，每开一次页面就多一笔。 */
    API.CTL.failMark = true;
    await vm.addMark({ kind: 'check', geo: [120, 300, 40, 40] });   // 与库里 201 号同形状
    await sleep(250);
    checkTrue('又攒下一条（这次与库里那条完全一样）', vm.pendingMarks.length === 1,
      vm.pendingMarks.length);
    API.CTL.failMark = false;
    const batch2 = calls3.filter((c) => c.name === 'addMarks').length;
    const flushed2 = await vm.flushPending(true);
    await sleep(200);
    check('★ 库里已经有一模一样的一笔 → 不再补发（否则每开一次页面多一笔）', flushed2, 0);
    check('也没有发请求出去', calls3.filter((c) => c.name === 'addMarks').length, batch2);
    check('★ 那条重复的从队列里清掉了（不是永远躺在那里反复重试）',
      vm.pendingMarks.length, 0);

    /* ============ 21. 模型回的两个信号得上屏（block_kind / tail_note） ============
     * 这两个字段是本轮给模型新加的**输出要求**（见 grade_ai._REGION_BLOCK）。
     * 加了字段却不在界面上显示，等于白加 —— 模型说「这块像卷头」「底部被切了」，
     * 老师看不见就不可能据此改分割。所以这里要证的是「真的渲染出来了」。
     *
     * ⚠️ 同时要证**它不会自动改任何状态**：模型说「像卷头」只是提示，
     *    绝不替老师把这块设成不判（它也可能读错，而老师根本不知道它跳了）。 */
    const oddBoxes = D.querySelectorAll('.g-odd');
    check('★ block_kind ≠ question 的题有一行提示（其余两题没有空壳）', oddBoxes.length, 1);
    checkTrue('提示里写清是哪一种（header → 卷头 / 页眉）',
      oddBoxes.length === 1 && oddBoxes[0].textContent.includes('卷头'),
      oddBoxes.length ? oddBoxes[0].textContent.trim().slice(0, 60) : '没有');
    checkTrue('提示说的是「模型认为」+「判出来多半是噪音」（讲清依据，不是命令）',
      oddBoxes.length === 1 && oddBoxes[0].textContent.includes('模型认为')
      && oddBoxes[0].textContent.includes('噪音'),
      oddBoxes.length ? oddBoxes[0].textContent.trim().slice(0, 80) : '没有');
    check('★ 有框 → 给一个「去设成不判」的跳转（决定权在老师）',
      oddBoxes.length === 1 ? oddBoxes[0].querySelectorAll('button').length : 0, 1);

    const tailNotes = D.querySelectorAll('.g-tail-note');
    check('★ tail_note 上屏了（只有那一题有，其余没有空壳）', tailNotes.length, 1);
    checkTrue('原样显示模型的话（不加工成「疑似」这种含糊词）',
      tailNotes.length === 1 && tailNotes[0].textContent.includes('邻题的残字'),
      tailNotes.length ? tailNotes[0].textContent.trim().slice(0, 60) : '没有');
    checkTrue('旁边告诉老师该怎么办（去调末段底边 / 翻页看下面）',
      body().includes('末段底边') && body().includes('翻页'), true);

    // 点「去设成不判」：跳到那一页 + 进框题/分割模式（不替老师点跳过）
    const oddBtn = oddBoxes.length === 1 ? oddBoxes[0].querySelector('button') : null;
    const skipBefore = vm.regionBlocks.filter((b) => b.skipped).length;
    if (oddBtn) {
      oddBtn.click();
      await sleep(260);
    }
    checkTrue('★ 点「去设成不判」→ 切到框题/分割模式',
      ['region', 'line'].includes(vm.tool), vm.tool + ' / 第 ' + vm.pageNo + ' 页');
    check('★ 只跳过去，**不自动把这块设成不判**（模型说像卷头也可能读错）',
      vm.regionBlocks.filter((b) => b.skipped).length, skipBefore);
  } finally {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) { /* 删不掉留着也无妨 */ }
  }

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
