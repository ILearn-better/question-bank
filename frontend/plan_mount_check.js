/**
 * 「整体规划」弹窗的真挂载检查 —— 点开一条规划，正文到底有没有被渲染成排版好的样子。
 *
 *   node frontend/plan_mount_check.js     # 退出码即结果（0 通过 / 跳过，非 0 失败）
 *
 * ## 为什么不是「再加几条静态断言」
 *
 * 这个需求的核心是一句「点开某个规划要能够**查看渲染好的文件**」——
 * 静态断言只能证明「源码里写了 setRichEl」，证明不了挂载之后容器真的被填上 KaTeX、
 * 图片真的指向正文里的那张。本项目最贵的 bug 类恰恰是「接口全绿、页面白屏 /
 * 正文还是 `$…$` 原文」，两个都只有真挂一遍才看得见。
 *
 * ## 怎么把一个 SPA 组件挂起来
 *
 * `frontend/index.html` 走 `<script type="module">`，jsdom **不执行 ESM**，整页挂不了
 * （零构建页的整套检查见 render_check.js）。这里只挂**这一个组件**：
 *
 *   1. 临时目录搭最小模块骨架，`PlanDialog.js` **原样复制**（一行不改 —— 验的必须是真代码）；
 *   2. 依赖（api / store / Modal / vue）换成**桩**，形状照真实接口响应写
 *      （`/api/plans/{id}` 的返回就照 routers/plans.py 的 `_out()` 抄）；
 *   3. vendor（vue / marked / purify / katex / auto-render）**手动喂进 jsdom 的 window**
 *      —— jsdom 30 已删掉 `ResourceLoader`，指望它自己加载脚本是拉不到的；
 *   4. `createApp().mount()`，等一拍，然后数渲染痕迹。
 *
 * ## 判据（全是「挂载痕迹」，不是长度）
 *
 *   · 点开先给**看**的那一面：`.plan-rich` 里出现 `.katex`、标题在、`$…$` 原文不残留
 *   · 正文里的图片指向那张图（Word 转出来的配图不能变成破图）
 *   · 点「改」切到编辑态：出现等宽 textarea，且正文源码**带着**（不能是空的）
 *   · 文件输入框**没有 accept**（设了的话老师想传 .lnk / 截图会发现「选不了」）
 *   · 看的那一面有「删除规划」，「改」那一面有「返回查看」（两态都得能回去）
 *
 * 找不到 jsdom 就 **SKIP 并退出 0**：它只是开发期加固，不该把回归弄红。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = __dirname;                 // frontend/

let JSDOM = null;

/** jsdom 是**可选依赖**：按候选顺序找，都找不到就跳过（与 render_check.js 同一套）。 */
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

/** 正文里放：标题、加粗、列表、公式、一张配图 —— 五样都在，才看得出「渲染好了」。 */
const PLAN_MD = [
  '## 一、总体目标',
  '',
  '把 **计算准确率** 提到 95% 以上，解方程 $x^2 - 5x + 6 = 0$ 要能一眼看出因式分解。',
  '',
  '- 每周 3 次专项训练',
  '- 每次 20 题，限时 25 分钟',
  '',
  '![配图](/api/notes/files/nb_0123456789ab.png)',
].join('\n');

const API_STUB = `
export const plansApi = {
  list: async () => ({ items: [] }),
  get: async () => ({
    id: 7, student_id: 1, title: '初三全年规划',
    content: ${JSON.stringify(PLAN_MD)},
    chars: 120, lines: 6, preview: '一、总体目标 …',
    file_count: 1, has_content: true,
    created_at: '2026-10-16T09:00:00', updated_at: '2026-10-16T09:30:00',
    dir_rel: 'students/u1_张同学/plans/初三全年规划',
    files: [{
      id: 3, plan_id: 7, name: '初三规划.docx', stored_name: '初三规划.docx',
      kind: 'word', kind_cn: 'Word', size_bytes: 20480,
      converted: true, note: '已转成正文（Word，120 字）',
      url: '/api/plan-files/3/raw', created_at: '2026-10-16T09:00:00',
    }],
  }),
  create: async () => ({ id: 8 }),
  update: async () => ({ id: 7 }),
  remove: async () => ({ ok: true }),
  upload: async () => ({ cache_key: 'aa/bb.docx', name: 'bb.docx', markdown: '', converted: false, note: '' }),
  removeCache: async () => ({ deleted: true }),
  removeFile: async () => ({ ok: true }),
  rawUrl: (id) => '/api/plan-files/' + id + '/raw',
};
`;

const STORE_STUB = `
export const fail = () => {};
export const ok = () => {};
export const warn = () => {};
export const fmtBytes = (n) => n + ' B';
`;

/** Modal 只当个能透出 slot 的壳 —— 这里验的是正文，不是弹窗本身。 */
const MODAL_STUB = `
export default {
  name: 'Modal',
  props: { title: { type: String, default: '' }, wide: { type: Boolean, default: false } },
  template: '<div class="modal-stub"><div class="modal-head">{{ title }}</div>'
    + '<div class="modal-body"><slot></slot></div>'
    + '<div class="modal-foot"><slot name="foot"></slot></div></div>',
};
`;

/** 组件里写的是 \`from 'vue'\`；这里让它落到一个转发到 jsdom window 上那份 Vue 的 shim。 */
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
  console.log((cond ? '  [OK ] ' : '  [FAIL] ') + label + (detail ? ': ' + detail : ''));
  if (!cond) fails.push(label);
}

/* ============================== main ============================== */

async function main() {
  console.log('整体规划「看」的那一面 · 真挂载检查（jsdom 挂 Vue 组件，数渲染痕迹）');

  const jsdomDir = loadJsdom();
  if (!jsdomDir) {
    console.log('  [SKIP] 没找到 jsdom（可选依赖），跳过');
    return 0;
  }
  console.log('  jsdom: ' + jsdomDir);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'plan_mount_'));
  try {
    // ---- 1. 搭骨架：PlanDialog.js 原样复制，依赖换桩 ----
    const comps = path.join(tmp, 'src', 'components');
    fs.mkdirSync(comps, { recursive: true });
    fs.mkdirSync(path.join(tmp, 'node_modules', 'vue'), { recursive: true });
    fs.copyFileSync(path.join(ROOT, 'src', 'components', 'PlanDialog.js'),
      path.join(comps, 'PlanDialog.js'));
    fs.copyFileSync(path.join(ROOT, 'src', 'mathRender.js'), path.join(tmp, 'src', 'mathRender.js'));
    fs.writeFileSync(path.join(tmp, 'src', 'api.js'), API_STUB);
    fs.writeFileSync(path.join(tmp, 'src', 'store.js'), STORE_STUB);
    fs.writeFileSync(path.join(comps, 'Modal.js'), MODAL_STUB);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'package.json'), VUE_PKG);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'index.js'), VUE_SHIM);

    // ---- 2. jsdom + vendor ----
    const dom = new JSDOM('<!DOCTYPE html><html><body><div id="app"></div></body></html>', {
      url: 'http://127.0.0.1:8000/#/students/1',
      runScripts: 'outside-only',        // 给 window.eval 用；页面内脚本仍然不执行
      pretendToBeVisual: true,
    });
    const W = dom.window;
    global.window = W;
    global.document = W.document;
    global.navigator = W.navigator;
    // jsdom 的 confirm 是「未实现」会抛异常的；这里给个真值，免得碰到就崩
    W.confirm = () => true;
    for (const f of VENDOR) {
      const p = path.join(ROOT, f);
      checkTrue('vendor 存在：' + f, fs.existsSync(p));
      if (!fs.existsSync(p)) return 1;
      W.eval(fs.readFileSync(p, 'utf8'));
    }
    global.__VUE__ = W.Vue;
    checkTrue('Vue / marked / katex 都进了 window',
      !!(W.Vue && W.marked && W.katex && W.renderMathInElement),
      ['Vue', 'marked', 'katex', 'renderMathInElement'].filter((k) => !W[k]).join(', ') || '齐全');

    // ---- 3. 挂载真组件（给一条已有规划 → 应该先进「看」的那一面） ----
    const mod = await import(pathToFileURL(path.join(comps, 'PlanDialog.js')).href);
    const app = W.Vue.createApp(mod.default, {
      plan: { id: 7, title: '初三全年规划' },
      student: { id: 1, name: '张同学' },
      folder: 'u1_张同学',
    });
    app.mount('#app');
    await new Promise((r) => setTimeout(r, 500));   // 等 async setup（load → paintRich）回来

    const D = W.document;
    const body = D.querySelector('#app').innerHTML;
    checkTrue('组件真的挂起来了（不是只剩未编译的模板）',
      !body.includes('{{ ') && !body.includes('v-else-if='), '含 ' + body.length + ' 字符');

    // ---- 4. ★ 点开先给「看」：正文必须是渲染好的 ----
    const rich = D.querySelector('.plan-rich');
    checkTrue('有渲染容器 .plan-rich（正文那一份）', !!rich);
    checkTrue('标题显示出来了', (D.querySelector('.plan-title') || {}).textContent === '初三全年规划',
      (D.querySelector('.plan-title') || {}).textContent);

    const katex = D.querySelectorAll('.plan-rich .katex');
    checkTrue('★ 公式真的渲染出来了（.katex 节点数 ≥ 1）', katex.length >= 1,
      '找到 ' + katex.length + ' 个');
    const rt = rich ? rich.textContent || '' : '';
    checkTrue('★ 正文里不再残留 $…$ 原文（= 渲染真生效）',
      !/\$[^$]+\$/.test(rt), rt.slice(0, 60));
    checkTrue('Markdown 的结构真的出来了（h2 / 列表 / 加粗）',
      !!(D.querySelector('.plan-rich h2') && D.querySelector('.plan-rich li')
         && D.querySelector('.plan-rich strong')),
      ['h2', 'li', 'strong'].map((s) => !!D.querySelector('.plan-rich ' + s)).join('/'));

    // 正文里的配图（Word 转出来的那张）必须指到真地址，不能是空 src
    const img = D.querySelector('.plan-rich img');
    checkTrue('★ 正文里的配图指向那张图（不是空 src）',
      !!img && String(img.getAttribute('src')).includes('/api/notes/files/'),
      img ? String(img.getAttribute('src')) : '没有 img');
    checkTrue('正文里没有走样成字面量的图片语法（`![配图](…)` 不该原样露出来）',
      !rt.includes('![配图]'));

    // 原件列表（转换会失真，原件是对照的那一份）
    const fileRows = D.querySelectorAll('.modal-body .exam-file');
    checkTrue('原件列出来了（含类型标签 Word）',
      fileRows.length === 1 && fileRows[0].textContent.includes('Word'),
      fileRows.length ? fileRows[0].textContent.trim().slice(0, 60) : '一行都没有');

    // ---- 5. 两态都得回得去 ----
    const footBtns = Array.from(D.querySelectorAll('.modal-foot button'));
    const del = footBtns.find((b) => b.textContent.includes('删除规划'));
    const edit = footBtns.find((b) => b.textContent.includes('改'));
    checkTrue('看的那一面有「删除规划」', !!del);
    checkTrue('看的那一面有「改」', !!edit);

    // ---- 6. 点「改」→ 编辑态：正文源码带着、输入框不设 accept ----
    if (edit) {
      edit.click();
      await new Promise((r) => setTimeout(r, 120));
      const ta = D.querySelector('textarea.plan-editor');
      checkTrue('切到编辑态后出现等宽 textarea（.plan-editor 也要有配套 CSS）', !!ta);
      if (ta) {
        checkTrue('textarea 里带着完整正文源码（不是空的）',
          ta.value.includes('总体目标') && ta.value.includes('$x^2'), String(ta.value).slice(0, 40));
      }
      const fInput = D.querySelector('input[type=file]');
      checkTrue('有文件输入框（传文本文件转正文那条路）', !!fInput);
      checkTrue('★ 文件输入框**没有** accept（设了的话 .lnk / 截图就选不到了）',
        !!fInput && !fInput.hasAttribute('accept'),
        fInput ? String(fInput.getAttribute('accept')) : '没有输入框');
      const back = Array.from(D.querySelectorAll('.modal-foot button'))
        .find((b) => b.textContent.includes('返回查看'));
      checkTrue('编辑态有「返回查看」（改完能回到看的那一面）', !!back);
    }
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
