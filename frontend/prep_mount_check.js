/**
 * 备课「题库挑题」的真挂载检查 —— 题干到底有没有被渲染成真公式、悬停容器在不在。
 *
 *   node frontend/prep_mount_check.js     # 退出码即结果（0 通过 / 跳过，非 0 失败）
 *
 * ## 为什么不是「再加几条静态断言」
 *
 * 静态断言只能证明「源码里写了 setRichEl」，证明不了**挂载之后容器真的被填上 KaTeX**。
 * 本项目最贵的 bug 类恰恰是「接口测试全绿，页面白屏 / 公式还是 $…$ 原文」——两个都只有
 * 真挂一遍才看得见。
 *
 * ## 怎么把一个 SPA 组件挂起来
 *
 * `frontend/index.html` 走 `<script type="module">`，jsdom **不执行 ESM**，整页挂不了
 * （那是 render_check.js 的活，它管的是零构建页）。这里换个路子，**只挂这一个组件**：
 *
 *   1. 临时目录里搭一个最小模块骨架，把 `PrepDialog.js` **原样复制**过去
 *      （一行都不改 —— 验的必须是真代码，不能是抄一份来验）；
 *   2. 它的依赖（api / store / Modal / vue）换成**桩**，形状照真实接口响应写；
 *   3. vendor（vue / marked / purify / katex / auto-render）**手动喂进 jsdom 的 window**
 *      —— jsdom 30 已经删掉 `ResourceLoader`，指望它自己加载脚本是拉不到的；
 *   4. `createApp().mount()`，等一拍，然后数 `.katex`。
 *
 * ## 判据（全是「挂载痕迹」，不是长度）
 *
 *   · 每道有文本的题，`.q-rich` 里必须出现 `.katex`（= 公式真渲染了）
 *   · `.q-rich` 的 textContent 里**不能再有 `$…$` 原文**（渲染成功的反向证据）
 *   · 悬停展开的折叠容器 `.q-clamp` 每行一个（CSS 的展开规则由静态断言守）
 *   · 没有文本的题显示**原貌图**（`.q-thumb` 的 src 指到那张图），不是空壳
 *   · 点「加入」后 chips 里是 **brief 的纯文本**（不能把渲染后的 HTML 塞进 title）
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

const API_STUB = `
const row = (id, content, kps, image) => ({
  id, content, image: image || '', qtype: '解答题', difficulty: '中档', tags: [],
  knowledge_points: kps || [], answer: 'x=1',
});
export const papersApi = {
  search: async () => ({ items: [
    row('q1', '解方程：$x^2 - 5x + 6 = 0$。', ['一元二次方程']),
    row('q2', '已知 $\\\\frac{a}{b} = \\\\frac{2}{3}$，求 $a + b$ 的值。', ['分式']),
    // 图片题：没有文本、只有原貌截图 —— 列表里必须显示这张图
    row('q3', '', [], '/api/crops/pic.png'),
  ], total: 3 }),
  tags: async () => ({ items: [{ tag: '一元二次方程', count: 2 }] }),
  knowledgePoints: async () => ({ items: [{ kp: '一元二次方程', count: 2 }] }),
};
export const notesApi = { list: async () => ({ items: [] }) };
export const lessonFilesApi = { list: async () => ({ items: [], rel_dir: '' }) };
export const prepApi = { get: async () => ({ prep: null }) };
`;

const STORE_STUB = `
export const fail = () => {};
export const ok = () => {};
export const fmtBytes = (n) => String(n);
`;

/** Modal 只当个能透出 slot 的壳 —— 这里验的是题干，不是弹窗本身。 */
const MODAL_STUB = `
export default {
  name: 'Modal',
  props: { title: { type: String, default: '' } },
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
  console.log('备课挑题 · 真挂载检查（jsdom 挂 Vue 组件，数渲染痕迹）');

  const jsdomDir = loadJsdom();
  if (!jsdomDir) {
    console.log('  [SKIP] 没找到 jsdom（可选依赖），跳过'); 
    return 0;
  }
  console.log('  jsdom: ' + jsdomDir);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'prep_mount_'));
  try {
    // ---- 1. 搭骨架：PrepDialog.js 原样复制，依赖换桩 ----
    const comps = path.join(tmp, 'src', 'components');
    fs.mkdirSync(comps, { recursive: true });
    fs.mkdirSync(path.join(tmp, 'node_modules', 'vue'), { recursive: true });
    fs.copyFileSync(path.join(ROOT, 'src', 'components', 'PrepDialog.js'),
      path.join(comps, 'PrepDialog.js'));
    fs.copyFileSync(path.join(ROOT, 'src', 'mathRender.js'), path.join(tmp, 'src', 'mathRender.js'));
    // 形态判定（文本 / 图）也走真代码，不桩掉 —— 图片题能不能显示就靠它
    fs.copyFileSync(path.join(ROOT, 'src', 'questionForm.js'), path.join(tmp, 'src', 'questionForm.js'));
    fs.writeFileSync(path.join(tmp, 'src', 'api.js'), API_STUB);
    fs.writeFileSync(path.join(tmp, 'src', 'store.js'), STORE_STUB);
    fs.writeFileSync(path.join(comps, 'Modal.js'), MODAL_STUB);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'package.json'), VUE_PKG);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'index.js'), VUE_SHIM);

    // ---- 2. jsdom + vendor ----
    const dom = new JSDOM('<!DOCTYPE html><html><body><div id="app"></div></body></html>', {
      url: 'http://127.0.0.1:8000/#/students',
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

    // ---- 3. 挂载真组件 ----
    const mod = await import(pathToFileURL(path.join(comps, 'PrepDialog.js')).href);
    const app = W.Vue.createApp(mod.default, { lesson: { id: 1, student_name: '张同学' } });
    app.mount('#app');
    await new Promise((r) => setTimeout(r, 500));   // 等 async setup（search/load）回来

    const D = W.document;
    const body = D.querySelector('#app').innerHTML;
    checkTrue('组件真的挂起来了（不是只剩未编译的模板）',
      !body.includes('{{ ') && !body.includes('v-else-if='), '含 ' + body.length + ' 字符');

    // ---- 4. 题库挑题的渲染痕迹 ----
    const rows = D.querySelectorAll('.pick-list .pick-row');
    check('挑题列表有 3 行（两道有文本 + 一道图片题）', rows.length, 3);

    const clamp = D.querySelectorAll('.pick-row .q-clamp');
    check('每行都有可折叠容器（悬停展开的钩子）', clamp.length, 3);

    const rich = D.querySelectorAll('.pick-row .q-rich');
    check('有文本的题才有渲染容器（图片题不渲染空壳）', rich.length, 2);

    const katex = D.querySelectorAll('.pick-row .q-rich .katex');
    checkTrue('★ 公式真的渲染出来了（.katex 节点数 ≥ 2）', katex.length >= 2, '找到 ' + katex.length + ' 个');

    const texts = Array.from(rich).map((el) => el.textContent || '');
    checkTrue('★ 题干里不再残留 $…$ 原文（= 渲染真生效）',
      !texts.some((t) => /\$[^$]+\$/.test(t)),
      texts.map((t) => t.slice(0, 26)).join(' | '));
    checkTrue('渲染出的题干确实有内容（不是空 div）',
      texts.every((t) => t.trim().length > 0), texts.map((t) => t.length).join('/'));

    // ★ 图片题：列表里必须真的出现那张原貌图（以前这里只有一句「（图片题，没有文本）」）
    const thumbs = D.querySelectorAll('.pick-row .q-thumb');
    check('★ 图片题显示了原貌图（1 张缩略图）', thumbs.length, 1);
    if (thumbs.length) {
      checkTrue('缩略图的 src 指向那道题的 image',
        String(thumbs[0].getAttribute('src') || '').includes('pic.png'),
        String(thumbs[0].getAttribute('src')));
      checkTrue('图片外面套了「点开看原图」的链接',
        !!thumbs[0].closest('a.q-thumb-link'));
    }
    checkTrue('图片题不再只剩「没有文本」那句占位',
      !D.querySelector('#app').innerHTML.includes('（图片题，没有文本）'));
    const notes = Array.from(D.querySelectorAll('.pick-row .small.muted')).map((el) => el.textContent);
    checkTrue('图旁边写清了「为什么显示成图」',
      notes.some((t) => t.includes('图片题')), notes.join(' | '));

    // ---- 5. 勾选后 chips 是纯文本 title（别把渲染后的 HTML 塞进去） ----
    // ⚠️ 两个页签本身就是 `.chip.on`，取值时必须剔掉 —— 否则数出来永远多一个。
    const pickedChips = () => Array.from(D.querySelectorAll('.chips .chip.on'))
      .filter((c) => c.textContent.includes('✕'));
    const btns = D.querySelectorAll('.pick-row .btn');
    checkTrue('每行都有「加入」按钮', btns.length === 3);
    if (btns.length) {
      btns[0].click();
      await new Promise((r) => setTimeout(r, 60));
      const chips = pickedChips();
      check('点一下出现 1 个已选 chip', chips.length, 1);
      if (chips.length) {
        const tail = chips[0].textContent;
        checkTrue('chip 显示的是纯文本题干（brief），不带 HTML 标签',
          tail.includes('解方程') && !tail.includes('<'), tail.trim());
      }
      btns[0].click();                       // 再点一下取消，别影响后面的断言
      await new Promise((r) => setTimeout(r, 60));
      check('再点一下能取消（chip 就没了）', pickedChips().length, 0);
    }

    // ---- 6. 换页签不会串行（题库 → 笔记，容器得跟着消失） ----
    const chips2 = D.querySelectorAll('.chips .chip');
    const noteTab = Array.from(chips2).find((c) => c.textContent.includes('挑笔记'));
    checkTrue('有两个页签（题库挑题 / 挑笔记）', !!noteTab);
    if (noteTab) {
      noteTab.click();
      await new Promise((r) => setTimeout(r, 120));
      check('切到笔记页签后渲染容器消失（不会拿笔记行去渲染题干）',
        D.querySelectorAll('.pick-row .q-rich').length, 0);
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
