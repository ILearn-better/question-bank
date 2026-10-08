/**
 * 前端真实挂载检查 —— 用 jsdom 走**浏览器的 HTML 解析器**加载整个页面，
 * 然后看 Vue 到底有没有把 #app 渲染出来。
 *
 *   node frontend/render_check.js           # 退出码即结果（0 通过）
 *   python test_render_page.py              # 上面这条的包装 + 静态断言
 *
 * ## 为什么值得有这么一个检查
 *
 * 本项目最贵的 bug 类是「**接口测试全绿、页面白屏**」。根因是这两个页面
 * （batch.html / entry.html）都是「零构建」手写 HTML：根模板走的是**浏览器解析器**，
 * 组件写成 `<x ... />` 时结尾那个 `/` 会被**忽略** → 当成开标签 → 紧跟其后的
 * `v-else` 被吞进组件内部 → Vue compiler-30 → `mount()` 根本不执行 → 整页白屏。
 *
 * 难查之处在于：**字符串模板语义下复现不出来**（Vue 自己的解析器认 `/`），
 * 所以 `test_batch_page.py` ⑤ 段那个自带分词器只能「模拟」浏览器；
 * 这里是真的把页面喂给 jsdom 挂一遍，任何**编译期**错误都跑不掉。
 *
 * ## 两个会让检查「假通过」的坑（都踩过，别再踩）
 *
 *   1. `JSDOM.fromFile` 在**路径含空格**时基地址解析错：页面里的 `/vendor/...` 被算成
 *      `file:///Q:/vendor/...`（盘根）→ vendor 脚本一个都加载不到 → `#app` 里只剩
 *      **没渲染的原始模板**。而原始模板本身就有 1.8 万字符，长度判据照样「通过」。
 *
 *   2. 所以判据**不能只看 innerHTML 长度**（见 marks()）：
 *      必须数**挂载痕迹**（`.el-button` / `.el-card` / `.el-tag`）> 0，
 *      再确认 `#app` 里不再残留 `{{ }}` 与 `v-else-if=` 这些字面量。
 *
 * jsdom 找不到时**跳过**（打印 SKIP、退出码 0）—— 它只是个开发期的加固检查，
 * 不该因为某台机器没装 jsdom 就把回归弄红。
 */
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = __dirname;                      // frontend/
const PAGES = ['batch.html', 'entry.html'];

let JSDOM = null;
let VirtualConsole = null;

/** jsdom 是**可选依赖**：按候选顺序找，都找不到就跳过。 */
function loadJsdom() {
  const home = process.env.USERPROFILE || process.env.HOME || '';
  const cands = [
    process.env.SHIKE_JSDOM_DIR,                                     // 显式指定优先
    path.join(ROOT, '..', 'node_modules', 'jsdom'),                  // 项目本地
    ...String(process.env.NODE_PATH || '').split(path.delimiter).map(p => p && path.join(p, 'jsdom')),
    home && path.join(home, '.workbuddy', 'binaries', 'node', 'workspace', 'node_modules', 'jsdom'),
  ].filter(Boolean);
  for (const c of cands) {
    try {
      const m = require(c);
      if (m && m.JSDOM) { JSDOM = m.JSDOM; VirtualConsole = m.VirtualConsole; return c; }
    } catch (e) { /* 换下一个候选 */ }
  }
  return null;
}

/**
 * ⚠️ jsdom 30 已经**删掉了 `ResourceLoader`**（改成 undici 拦截器），所以不能再用
 *    自定义加载器把 `/vendor/...` 映射回来。改用一个更简单、也更贴近真实的办法：
 *    保持**真实的 file:// 基地址**，只在内存里把资源标签上的**绝对路径**改写成相对路径。
 *    `src="/vendor/vue.global.prod.js"` 在 file:// 下会解析到盘根 →
 *    改写成 `src="vendor/..."` 就落回 frontend/ 里了。
 *    （负向先行断言保住 `//cdn...` 这种协议相对地址不被误伤。）
 *
 *    这一步只影响「从哪儿读资源文件」，**不影响 HTML 解析器行为** —— 而后者才是检查目的。
 */
function rewriteAssets(html) {
  return html.replace(/(\s(?:src|href))="\/(?!\/)/g, '$1="');
}

/* ====================== mock：形状取自真实接口响应（不猜结构） ====================== */

const DOC_ID = 'rc1';
const JOB_ID = 'rcj1';

const LINES = {
  page: 1,
  width: 595,        // A4：归一化换算的唯一基准，也是框选与预填共用的基准
  height: 842,
  lines: [
    { text: '1. 已知函数 f(x)=x^2+1，则 f(0)=（ ）', bbox: [72, 60, 480, 78] },
    { text: 'A. 0', bbox: [72, 84, 200, 100] },
    { text: '2. 如图，正方体 ABCD-A1B1C1D1 中……', bbox: [72, 150, 480, 168] },
  ],
};

/** 待审条目 —— 五条刚好把配图那几种状态铺满：
 *  ① AI 自动框好（snapped）② AI 估的（rough）③ 没定位到 ④ 还没框 ⑤ 本来就不需要图 */
const ITEMS = [
  {
    id: 'i1', seq: 1, job_id: JOB_ID, document_id: DOC_ID, doc_filename: 'mock.pdf',
    page_no: 1, region: null, image: '', figure_box: [86, 325, 300, 475],
    content: '如图，正方体 $ABCD-A_1B_1C_1D_1$ 的棱长为 2，求异面直线所成角。',
    qtype: '解答题', difficulty: '中等', knowledge_point: '空间向量与立体几何',
    node_id: null, node_path: null, tags: ['正方体'],
    confidence: 'high', note: '', needs_figure: true,
    figure_note: '题干给了一个正方体图', figure_image: '/api/documents/rc1/region-image?x=1',
    flags: ['figure_snapped'], status: 'pending', question_id: null, error: null,
    created_at: '2026-10-08T12:00:00', reviewed_at: null,
  },
  {
    id: 'i2', seq: 2, job_id: JOB_ID, document_id: DOC_ID, doc_filename: 'mock.pdf',
    page_no: 1, region: null, image: '', figure_box: [50, 220, 400, 380],
    content: '（扫描版）如图，抛物线 $y=x^2$ 与直线相交，求阴影面积。',
    qtype: '解答题', difficulty: '中等', knowledge_point: '圆锥曲线与方程',
    node_id: null, node_path: null, tags: [],
    confidence: 'medium', note: '', needs_figure: true,
    figure_note: '有阴影区域', figure_image: '/api/documents/rc1/region-image?x=2',
    flags: ['figure_rough'], status: 'pending', question_id: null, error: null,
    created_at: '2026-10-08T12:00:01', reviewed_at: null,
  },
  {
    id: 'i3', seq: 3, job_id: JOB_ID, document_id: DOC_ID, doc_filename: 'mock.pdf',
    page_no: 1, region: null, image: '', figure_box: null,
    content: '函数 $y=\\sin x$ 的图像与 $x$ 轴围成的面积为（ ）',
    qtype: '选择题', difficulty: '基础', knowledge_point: '三角函数',
    node_id: null, node_path: null, tags: [],
    confidence: 'high', note: '', needs_figure: true,
    figure_note: '模型说有图', figure_image: '',
    flags: ['figure_box_missing'], status: 'pending', question_id: null, error: null,
    created_at: '2026-10-08T12:00:02', reviewed_at: null,
  },
  {
    id: 'i4', seq: 4, job_id: JOB_ID, document_id: DOC_ID, doc_filename: 'mock.pdf',
    page_no: 1, region: null, image: '', figure_box: null,
    content: '（还没有框的）如图，求三角形面积。',
    qtype: '解答题', difficulty: '中等', knowledge_point: '解三角形',
    node_id: null, node_path: null, tags: [],
    confidence: 'high', note: '', needs_figure: true,
    figure_note: '', figure_image: '',
    flags: [], status: 'pending', question_id: null, error: null,
    created_at: '2026-10-08T12:00:03', reviewed_at: null,
  },
  {
    id: 'i5', seq: 5, job_id: JOB_ID, document_id: DOC_ID, doc_filename: 'mock.pdf',
    page_no: 1, region: null, image: '', figure_box: null,
    content: '$(1+5\\mathrm{i})\\mathrm{i}$ 的虚部为（ ）A. $-1$ B. $0$',
    qtype: '选择题', difficulty: '基础', knowledge_point: '复数的概念与运算',
    node_id: null, node_path: null, tags: ['虚部'],
    confidence: 'high', note: '', needs_figure: false,
    figure_note: '', figure_image: '', flags: [], status: 'pending',
    question_id: null, error: null, created_at: '2026-10-08T12:00:04', reviewed_at: null,
  },
];

const JOB = {
  id: JOB_ID, status: 'done', total: 5, done: 5, failed: 0,
  curriculum_id: 1, curriculum_name: 'DSE 数学',
  document_id: DOC_ID, doc_filename: 'mock-一张图片.png',
  error: null, ai_mode: 1, created_at: '2026-10-08T12:00:00',
  finished_at: '2026-10-08T12:00:30', pending: 5, approved: 0, rejected: 0,
  finished: true,                       // finished=true → 页面不会开轮询，检查更稳
};

const ROUTES = {
  '/api/batch/options': {
    qtypes: ['选择题', '填空题', '解答题'], difficulties: ['基础', '中等', '较难'],
    confidences: ['high', 'medium', 'low'], max_tags: 4, max_tag_chars: 12,
    workers: 3, max_pages: 30, figure_keywords: ['如图', '下图', '图中'],
  },
  '/api/curricula': [{
    id: 1, code: 'dse-math', name: 'DSE 数学', region: 'HK', stage: 'exam',
    subject: 'math', color: '#534AB7', sort_order: 1, node_count: 0,
  }],
  '/api/documents': [{
    id: DOC_ID, filename: 'mock-一张图片.png', filetype: '.png', block_count: 0,
    scanned: 1, created_at: '2026-10-08T12:00:00',
    preview_ready: false, preview_pages: null,
  }],
  [`/api/documents/${DOC_ID}`]: {
    id: DOC_ID, filename: 'mock-一张图片.png', filetype: '.png', block_count: 0,
    blocks: [], scanned: 1, created_at: '2026-10-08T12:00:00',
    preview: { engine: 'pdf', ready: true, pages: 1, error: null, can_build: false },
  },
  [`/api/documents/${DOC_ID}/pages`]: { page_count: 1 },
  [`/api/documents/${DOC_ID}/pages/1/lines`]: LINES,
  '/api/batches': [JOB],
  [`/api/batches/${JOB_ID}`]: JOB,
  [`/api/batches/${JOB_ID}/items`]: ITEMS,

  // —— entry.html（逐题录入）启动时也要的几项 ——
  '/api/questions/knowledge-points': { items: [] },
  '/api/questions/tags': { items: [] },
  '/api/questions': [],
  '/api/curricula/1/nodes': [],
};

/* ================================== 检查框架 ================================== */

/** 页面里未捕获的异常**不该把检查进程直接杀掉** —— 那会丢掉后面所有断言，
 *  只剩一句 node 栈，反而看不出「是页面坏了还是检查自己写错了」。 */
const uncaught = [];
process.on('uncaughtException', (e) => uncaught.push(String((e && e.message) || e)));
process.on('unhandledRejection', (e) => uncaught.push(String((e && e.message) || e)));

let fails = 0;
function check(name, cond, extra) {
  if (cond) { console.log(`  \u2713 ${name}`); return true; }
  fails++;
  console.log(`  \u2717 ${name}${extra !== undefined ? `  \u2192 ${extra}` : ''}`);
  return false;
}

/** 挂载痕迹：这些都是 Element Plus 的组件根节点，只有 Vue 真的 mount 了才会有。 */
function marks(doc) {
  return {
    button: doc.querySelectorAll('.el-button').length,
    card: doc.querySelectorAll('.el-card').length,
    tag: doc.querySelectorAll('.el-tag').length,
  };
}

const BAD = /compiler-|SyntaxError|Cannot read|is not a function|is not defined/i;

async function load(name) {
  const file = path.join(ROOT, name);
  const logs = [];
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => logs.push('jsdomError: ' + (e.message || e)));
  vc.on('error', (...a) => logs.push('error: ' + a.join(' ')));
  vc.on('warn', (...a) => logs.push('warn: ' + a.join(' ')));
  vc.on('log', () => {});

  const dom = new JSDOM(rewriteAssets(fs.readFileSync(file, 'utf8')), {
    url: pathToFileURL(file).href,     // ⚠️ 必须自己给：fromFile 遇空格路径会解析错基地址
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    virtualConsole: vc,
    beforeParse(win) {
      const stub = (url) => {
        const p = decodeURIComponent(String(url).split('?')[0]);
        const data = Object.prototype.hasOwnProperty.call(ROUTES, p) ? ROUTES[p] : [];
        return Promise.resolve({
          ok: true, status: 200,
          json: () => Promise.resolve(data),
          text: () => Promise.resolve(JSON.stringify(data)),
        });
      };
      win.fetch = stub;
      win.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
      win.requestAnimationFrame = (cb) => setTimeout(cb, 0);
      win.cancelAnimationFrame = (id) => clearTimeout(id);
      win.scrollTo = () => {};
      win.Element.prototype.scrollTo = () => {};
    },
  });
  await new Promise((r) => setTimeout(r, 4500));   // vendor 从磁盘加载 + mount
  return { dom, logs, doc: dom.window.document, win: dom.window };
}

/* ================================== 两个页面 ================================== */

async function checkBatch() {
  console.log('\n===== batch.html（批量入库 / 待审）=====');
  const { dom, logs, doc, win } = await load('batch.html');
  const n0 = uncaught.length;

  const m = marks(doc);
  check('Vue 真的挂载了（.el-button > 0）', m.button > 0, `button=${m.button}`);
  check('渲染出卡片结构（.el-card > 0）', m.card > 0, `card=${m.card}`);
  check('渲染出标签（.el-tag > 0）', m.tag > 0, `tag=${m.tag}`);

  check('没有编译 / 运行时异常', logs.filter((l) => BAD.test(l)).length === 0,
    logs.filter((l) => BAD.test(l)).slice(0, 4).map((l) => l.slice(0, 200)).join(' | '));

  const raw = doc.querySelector('#app').innerHTML;
  check('不再是未渲染的原始模板（无 {{ }} / v-else-if 字面量）',
    !/\{\{/.test(raw) && !/v-else-if=/.test(raw));

  // —— 打开页面**不自动加载任何卷子**（用户点名要的：省掉每次打开的那一次加载）——
  // ⚠️ 这条锁的是「新行为」，同时它也是这次改动的最大风险点：
  //    卷面预览藏起来了，但**历史任务与待审必须照旧看得见**（它们不挂在当前卷子上）。
  const raw0 = doc.querySelector('#app').innerHTML;
  check('打开时没有自动打开任何文档（左栏卷面预览是空的）',
    doc.querySelectorAll('.page-wrap').length === 0,
    `page-wrap=${doc.querySelectorAll('.page-wrap').length}`);
  check('没选卷子时给出落点提示', /先在上方选择或上传一份试卷/.test(raw0));
  check('没选卷子也照样看得到「历史任务」', /历史任务/.test(raw0));

  // 进「待审」：点第一个历史任务卡片
  const card = doc.querySelector('.job-card');
  check('历史任务卡片渲染出来了', !!card);
  if (card) {
    card.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
    await new Promise((r) => setTimeout(r, 1200));
  }

  const body = doc.querySelector('#app').innerHTML;

  // —— 配图那几种状态，两档 AI 可信度 + 两种人工提示 ——
  check('① AI 自动框的图 → 「AI 自动框的图（核对一眼即可）」', /AI 自动框的图/.test(body));
  check('② AI 估的框 → 「AI 估的图框，容易歪，建议核对」', /AI 估的图框/.test(body));
  check('③ 没定位到 → 「没能在这页上定位到它」', /没能在这页上定位到它/.test(body));
  check('④ 还没框 → 「还没框图。点「框选配图」」', /还没框图/.test(body));
  check('⑤ 按钮文案分叉：已有图 → 「调整配图」', /调整配图/.test(body));
  check('⑤ 按钮文案分叉：没有图 → 「框选配图」', /框选配图/.test(body));
  check('模型判图理由（figure_note）显示出来', /模型说：/.test(body));
  check('配图缩略图渲染出来（.fig-thumb）', doc.querySelectorAll('.fig-thumb').length >= 1,
    `fig-thumb=${doc.querySelectorAll('.fig-thumb').length}`);
  check('「AI 自动框」与「AI 估的框」两个标签互斥（不会同时出现）',
    !/AI 自动框的图[\s\S]{0,80}AI 估的图框/.test(body));

  check('点开待审后仍无异常', logs.filter((l) => BAD.test(l)).length === 0,
    logs.filter((l) => BAD.test(l)).slice(0, 4).map((l) => l.slice(0, 200)).join(' | '));

  const noise = logs.filter((l) => /Could not load/.test(l));
  // ⚠️ 标题里**不要**写 "Could not load" 字面量 —— test_render_page.py 用
  //    `"Could not load" not in out` 判这一项，标题会自己把自己判失败（踩过）。
  check('vendor 脚本全部加载成功', noise.length === 0,
    noise.slice(0, 3).join(' | '));
  check('页面没有未捕获异常', uncaught.length === n0, uncaught.slice(n0, n0 + 3).join(' | '));

  dom.window.close();
}

async function checkEntry() {
  console.log('\n===== entry.html（逐题录入）=====');
  const { dom, logs, doc } = await load('entry.html');
  const n0 = uncaught.length;

  const m = marks(doc);
  check('Vue 真的挂载了（.el-button > 0）', m.button > 0, `button=${m.button}`);
  check('渲染出卡片 / 标签', m.card + m.tag > 0, `card=${m.card} tag=${m.tag}`);

  check('没有编译 / 运行时异常', logs.filter((l) => BAD.test(l)).length === 0,
    logs.filter((l) => BAD.test(l)).slice(0, 4).map((l) => l.slice(0, 200)).join(' | '));

  const raw = doc.querySelector('#app').innerHTML;
  check('不再是未渲染的原始模板', !/\{\{/.test(raw) && !/v-else-if=/.test(raw));

  // 同上：打开时不自动加载上次那卷，停在下拉框的空状态
  check('打开时没有自动打开任何文档（停在空状态）',
    /上传一份 PDF 或 Word 试卷开始使用/.test(raw),
    raw.slice(0, 200));

  // 图片当单页文档：上传要放行图片，「图片·1 页」那条分支要在
  const html = fs.readFileSync(path.join(ROOT, 'entry.html'), 'utf8');
  check('上传 accept 放行图片', /accept="[^"]*\.png/.test(html));
  check('显示「图片·1 页」的分支', /图片·1 页/.test(html));
  check('isPaged() / isImg() 方法存在', /isPaged\s*\(/.test(html) && /isImg\s*\(/.test(html));

  const noise = logs.filter((l) => /Could not load/.test(l));
  check('vendor 脚本全部加载成功', noise.length === 0, noise.slice(0, 3).join(' | '));
  check('页面没有未捕获异常', uncaught.length === n0, uncaught.slice(n0, n0 + 3).join(' | '));

  dom.window.close();
}

(async () => {
  const found = loadJsdom();
  if (!found) {
    console.log('SKIP: 没找到 jsdom（可选依赖），跳过真实挂载检查。');
    console.log('      装法：npm i jsdom   或设 SHIKE_JSDOM_DIR 指向 node_modules/jsdom');
    process.exit(0);
  }
  console.log(`jsdom: ${found}`);
  try {
    await checkBatch();
    await checkEntry();
  } catch (e) {
    fails++;
    console.log('\n检查本身出错：', e && e.stack ? e.stack : e);
  }
  console.log('\n' + (fails ? `\u274c ${fails} 项失败` : '\u2705 全部通过'));
  process.exit(fails ? 1 : 0);
})();
