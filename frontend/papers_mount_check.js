/**
 * 出卷页「题库结果」的真挂载检查 —— 图片题到底有没有显示出原貌图、切「题目呈现」有没有跟着变。
 *
 *   node frontend/papers_mount_check.js    # 退出码即结果（0 通过 / 跳过，非 0 失败）
 *
 * ## 为什么要挂这一页
 *
 * Papers.js 是 SPA 视图（`<script type="module">`），jsdom 挂不了整个应用 ——
 * 所以以前这一页只到「模板能被 Vue.compile 过一遍 + 静态断言接线对了」这两层，
 * 余下的全靠人眼。可这次要验的是「**没识别出文本的题，列表里真的显示出那张原貌图**」：
 * 静态断言证明不了挂载之后图上没上屏（容器在、src 也写了，但 formOf 忘了 return 呢？）。
 *
 * 于是照 `prep_mount_check.js` 那套（本文件就是它的出卷版）**只挂这一个视图**：
 * 临时目录搭骨架 → Papers.js 原样复制 → 依赖换桩 → vendor 喂进 jsdom → mount。
 *
 * ## 判据（挂载痕迹，不是长度）
 *
 *   · 有文本的题：`.q-rich` 里出现 `.katex`（公式真渲染了），且不再残留 `$…$`
 *   · 图片题：`.q-thumb` 的 src 指向那道题的 image（**以前这里只有一句「没有文本」**）
 *   · 切到「全部用图片」→ 文本容器全没了、全换成图；切回「全部用文本」→
 *     **没文本的题自动退回印图**（与后端 _choose_form 同一条规则，绝不印空白题）
 *   · 加进「已选」后右边那列也跟着显示图（q-thumb-sm）
 *
 * 找不到 jsdom 就 **SKIP 并退出 0** —— 它只是开发期加固，不该把回归弄红。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = __dirname;                 // frontend/

let JSDOM = null;

/** jsdom 是**可选依赖**：按候选顺序找，都找不到就跳过（与 prep_mount_check.js 同一套）。 */
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

const API_STUB = `
const row = (id, content, kps, image) => ({
  id, content, image: image || '', qtype: '解答题', difficulty: '中档', tags: [],
  knowledge_points: kps || [], answer: 'x=1', answer_image: '', usage_count: 0,
  render_prefer: 'auto', curriculum_id: null,
});
// 形状照真实库来：扫过的卷子里**每道题都有原貌截图**，其中少数没识别出文本。
export const papersApi = {
  search: async () => ({ items: [
    row('q1', '解方程：$x^2 - 5x + 6 = 0$。', ['一元二次方程'], '/api/crops/a.png'),
    row('q2', '', [], '/api/crops/pic.png'),      // 图片题：只有原貌图，没有文本
    row('q3', '求 $\\\\sqrt{3}\\\\sin x$ 的最小正周期。', ['三角函数'], '/api/crops/b.png'),
    row('q4', '', []),                            // 空题：没文字也没图（出卷会印成空白）
  ], total: 4 }),
  tags: async () => ({ items: [{ tag: '一元二次方程', count: 2 }] }),
  knowledgePoints: async () => ({ items: [] }),
  exportUrl: () => 'about:blank',
};
export const paperTemplatesApi = { list: async () => ({ items: [] }) };
export const curriculumApi = { nodes: async () => [] };
`;

const STORE_STUB = `
export const state = { curricula: [] };
export const fail = () => {};
export const ok = () => {};
export const loadCurricula = async () => {};
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
  console.log('出卷挑题 · 真挂载检查（jsdom 挂 Papers.js，数渲染痕迹）');

  const jsdomDir = loadJsdom();
  if (!jsdomDir) {
    console.log('  [SKIP] 没找到 jsdom（可选依赖），跳过');
    return 0;
  }
  console.log('  jsdom: ' + jsdomDir);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'papers_mount_'));
  try {
    // ---- 1. 搭骨架：Papers.js 原样复制，依赖换桩 ----
    fs.mkdirSync(path.join(tmp, 'src', 'views'), { recursive: true });
    fs.mkdirSync(path.join(tmp, 'node_modules', 'vue'), { recursive: true });
    const copy = (rel) => {
      fs.mkdirSync(path.dirname(path.join(tmp, rel)), { recursive: true });
      fs.copyFileSync(path.join(ROOT, rel), path.join(tmp, rel));
    };
    copy('src/views/Papers.js');
    copy('src/mathRender.js');
    copy('src/tagFilter.js');
    // 形态判定（文本 / 图）也走真代码，不桩掉 —— 图片题能不能显示就靠它
    copy('src/questionForm.js');
    fs.writeFileSync(path.join(tmp, 'src', 'api.js'), API_STUB);
    fs.writeFileSync(path.join(tmp, 'src', 'store.js'), STORE_STUB);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'package.json'), VUE_PKG);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'index.js'), VUE_SHIM);

    // ---- 2. jsdom + vendor ----
    const dom = new JSDOM('<!DOCTYPE html><html><body><div id="app"></div></body></html>', {
      url: 'http://127.0.0.1:8000/#/papers',
      runScripts: 'outside-only',        // 给 window.eval 用；页面内脚本仍然不执行
      pretendToBeVisual: true,
    });
    const W = dom.window;
    global.window = W;
    global.document = W.document;
    global.navigator = W.navigator;
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

    // ---- 3. 挂载真视图 ----
    const mod = await import(pathToFileURL(path.join(tmp, 'src', 'views', 'Papers.js')).href);
    const app = W.Vue.createApp(mod.default);
    const vm = app.mount('#app');
    await new Promise((r) => setTimeout(r, 500));   // 等 onMounted 里的几个请求回来

    const D = W.document;
    const body = D.querySelector('#app').innerHTML;
    checkTrue('视图真的挂起来了（不是只剩未编译的模板）',
      !body.includes('{{ ') && !body.includes('v-else-if='), '含 ' + body.length + ' 字符');

    /** 找某个卡片（按 h2 认），几张卡都是 .card，靠标题区分左右两栏。 */
    const card = (t) => Array.from(D.querySelectorAll('.card'))
      .find((c) => { const h = c.querySelector('h2'); return h && h.textContent.includes(t); });
    const resultCard = card('题库结果');
    const pickedCard = card('已选');
    checkTrue('左栏「题库结果」卡片在', !!resultCard);
    checkTrue('右栏「已选」卡片在', !!pickedCard);
    if (!resultCard || !pickedCard) return 1;

    // ---- 4. 默认（自动 = 按每题偏好）：文本渲染文本、图片题显示图 ----
    const rows = resultCard.querySelectorAll('.lesson-row');
    check('题库结果 4 行', rows.length, 4);
    check('两道有文本的题是渲染容器（.q-rich）', resultCard.querySelectorAll('.q-rich').length, 2);
    checkTrue('★ 公式真的渲染出来了（.katex ≥ 2）',
      resultCard.querySelectorAll('.q-rich .katex').length >= 2,
      '找到 ' + resultCard.querySelectorAll('.q-rich .katex').length + ' 个');
    const richTexts = Array.from(resultCard.querySelectorAll('.q-rich')).map((el) => el.textContent || '');
    checkTrue('★ 题干里不再残留 $…$ 原文（= 渲染真生效）',
      !richTexts.some((t) => /\$[^$]+\$/.test(t)), richTexts.map((t) => t.slice(0, 24)).join(' | '));

    const thumbs = resultCard.querySelectorAll('.q-thumb');
    check('★ 图片题显示了原貌图（1 张缩略图）', thumbs.length, 1);
    if (thumbs.length) {
      checkTrue('缩略图的 src 指向那道题的 image',
        String(thumbs[0].getAttribute('src') || '').includes('pic.png'),
        String(thumbs[0].getAttribute('src')));
      checkTrue('图片外面套了「点开看原图」的链接', !!thumbs[0].closest('a.q-thumb-link'));
    }
    checkTrue('图片题不再只剩「没有文本」那句占位',
      !body.includes('（图片题，没有文本）'));
    checkTrue('图旁边写清了「为什么显示成图」',
      Array.from(resultCard.querySelectorAll('.small.muted, .muted.small'))
        .some((el) => (el.textContent || '').includes('图片题')));
    // 空题：挑题时就点出来，别等卷子印出来才发现是空白
    checkTrue('★ 没文字也没图的题在列表里就标出来了（空题警告）',
      body.includes('空题：没有文字也没有图'), '');

    // ---- 5. 切「题目呈现」：整卷覆盖压过单题偏好 ----
    vm.opts.render_mode = 'image';
    await new Promise((r) => setTimeout(r, 200));
    check('切「全部用图片」后文本容器全没了',
      resultCard.querySelectorAll('.q-rich').length, 0);
    check('★ 切「全部用图片」后 3 道有图的题全换成原貌图',
      resultCard.querySelectorAll('.q-thumb').length, 3);

    vm.opts.render_mode = 'text';
    await new Promise((r) => setTimeout(r, 200));
    check('切「全部用文本」后有文本的题回到渲染容器',
      resultCard.querySelectorAll('.q-rich').length, 2);
    check('★ 没文本的题自动退回印图（绝不印空白题，与后端同一条规则）',
      resultCard.querySelectorAll('.q-thumb').length, 1);

    vm.opts.render_mode = 'auto';
    await new Promise((r) => setTimeout(r, 200));

    // ---- 6. 加进「已选」：右栏也跟着显示图 ----
    const picRow = Array.from(resultCard.querySelectorAll('.lesson-row'))
      .find((r) => r.querySelector('.q-thumb'));
    checkTrue('能在左栏找到图片题那一行', !!picRow);
    if (picRow) {
      const btn = picRow.querySelector('button');
      checkTrue('图片题那一行有「加入」按钮', !!btn);
      if (btn) {
        btn.click();
        await new Promise((r) => setTimeout(r, 200));
        check('★ 已选那列也显示这道题的图（.q-thumb-sm）',
          pickedCard.querySelectorAll('.q-thumb-sm').length, 1);
        check('已选那列的图片题没有渲染空壳（q-rich 只有 0 个）',
          pickedCard.querySelectorAll('.q-rich').length, 0);
      }
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
