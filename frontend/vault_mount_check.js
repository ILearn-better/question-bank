/**
 * 题库文件页（Vault.js）的真挂载检查 —— 页面能不能挂起来、同步报告到底有没有上屏。
 *
 *   node frontend/vault_mount_check.js    # 退出码即结果（0 通过 / 跳过，非 0 失败）
 *
 * ## 为什么必须挂
 *
 * 这一页有两个「静态看代码看不出来」的坑：
 *   ① 项目**没有 Element Plus**。写成 `<el-card>` 不报错，只会渲染成没样式的未知标签，
 *      页面看着像坏了但控制台一片安静。这一条只能靠挂载后数真标签验。
 *   ② 同步报告是从 `report` 的嵌套字段渲出来的（created/errors/warnings/extra_images）。
 *      静态断言能看出「写了 v-for」，看不出「点了同步之后它真的出现在屏幕上」。
 *
 * ## 判据（挂载痕迹，不是源码长度）
 *
 *   · 视图挂起来了（没有残留 `{{ }}` / `v-if=`）
 *   · 路径、四个状态数字从接口数据里来
 *   · 页面上**没有**未定义的 `el-` 标签（Element Plus 那条坑）
 *   · 点「同步」→ 出现「同步结果」+ 新增条数 + 错误原因
 *   · 「预览」点下去之后标题写的是「预览结果」
 *
 * 找不到 jsdom 就 **SKIP 并退出 0**（可选依赖，不该把回归弄红）。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = __dirname;

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

/* ====================== 桩：形状照真实接口响应来（不猜结构） ====================== */

const API_STUB = `
const STATUS = {
  root: 'D:/shike/backend/data/vault',
  exists: true, writable: true,
  md_total: 5, md_questions: 3, linked: 2, inbox_pending: 1, inbox_md: 2,
  dirs: { shelf: '题库', attach: '附件', inbox: '收件箱' },
};
const INBOX = {
  dir: 'D:/shike/backend/data/vault/收件箱',
  hint: '卷子放 收件箱/<体系>/（子文件夹名就是体系）；插件转出来的 md 放收件箱里任意位置都行',
  pending: ['收件箱/DSE/2024-P1.pdf'],
  pending_md: ['收件箱/2024 年卷一.md'],
  tasks: [],
  curricula: ['DSE', 'CN-SENIOR'],
  enabled: true,
};
const REPORT = {
  dry_run: false, root: STATUS.root, scanned: 4,
  created: [{ path: '题库/初一几何/三棱柱形礼盒.md', title: '三棱柱形礼盒', id: 'q1' }],
  updated: [{ path: '题库/另一道.md', title: '另一道', id: 'q2' }],
  unchanged: 2,
  skipped: [{ path: '题库/随笔.md', reason: '不是题目 md（没有 shike: question）' }],
  errors: [{ path: '题库/坏掉.md', error: 'frontmatter 解析失败', detail: '不是合法 YAML' }],
  warnings: [{ path: '题库/图题.md', messages: ['找不到图片 缺失.png'] }],
  extra_images: [{ path: '题库/两图.md', images: ['图二.png'] }],
  missing: [{ id: 'q9', path: '题库/搬走了.md' }],
};

export const vaultApi = {
  status: async () => STATUS,
  inbox: async () => INBOX,
  scan: async (dry) => ({ ...REPORT, dry_run: !!dry }),
  export: async () => ({ exported: [], failed: [], note: '没有需要导出的题' }),
  seedTemplate: async () => ({ path: '题库/题目模板（照这个写）.md', existed: false }),
  openFolder: async () => ({ ok: true }),
  inboxRun: async () => ({
    started: [], finished: [], failed: [], ignored: [], waiting: [],
    ingested: [
      { file: '收件箱/2024 年卷一.md', moved_to: '收件箱/_已处理/2024 年卷一.md',
        units: 2, needs_split: false,
        created: [{ unit: 1, id: 'n1', title: '1. 如图', md: '题库/1. 如图.md' },
                  { unit: 2, id: 'n2', title: '2. 化简', md: '题库/2. 化简.md' }],
        skipped: [], warnings: [] },
      { file: '收件箱/没分隔线.md', moved_to: '收件箱/_失败/没分隔线.md',
        units: 1, needs_split: true, created: [],
        skipped: [{ unit: 0, reason: '这篇里像是 3 道题，但没找到分隔线。' }],
        warnings: [] },
    ],
  }),
  source: async () => ({ qid: 'q1', md_path: null, exists: false, uri: null }),
};
`;

const STORE_STUB = `
export const state = {};
export const ok = (m) => { globalThis.__TOASTS__.push(['ok', m]); };
export const fail = (m) => { globalThis.__TOASTS__.push(['fail', m]); };
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
export const createApp = (...a) => V.createApp(...a);
export default V;
`;

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

const tick = (ms) => new Promise((r) => setTimeout(r, ms));

/** 按文案找按钮 —— 别用 querySelectorAll('button')[n]，加一个按钮就会点错。 */
function btn(doc, text) {
  return Array.from(doc.querySelectorAll('button'))
    .find((b) => (b.textContent || '').includes(text));
}

async function main() {
  console.log('题库文件页 · 真挂载检查（jsdom 挂 Vault.js，数渲染痕迹）');

  const jsdomDir = loadJsdom();
  if (!jsdomDir) {
    console.log('  [SKIP] 没找到 jsdom（可选依赖），跳过');
    return 0;
  }
  console.log('  jsdom: ' + jsdomDir);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'vault_mount_'));
  try {
    fs.mkdirSync(path.join(tmp, 'src', 'views'), { recursive: true });
    fs.mkdirSync(path.join(tmp, 'node_modules', 'vue'), { recursive: true });
    fs.copyFileSync(path.join(ROOT, 'src', 'views', 'Vault.js'),
      path.join(tmp, 'src', 'views', 'Vault.js'));
    fs.writeFileSync(path.join(tmp, 'src', 'api.js'), API_STUB);
    fs.writeFileSync(path.join(tmp, 'src', 'store.js'), STORE_STUB);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'package.json'), VUE_PKG);
    fs.writeFileSync(path.join(tmp, 'node_modules', 'vue', 'index.js'), VUE_SHIM);

    const vueFile = path.join(ROOT, 'vendor', 'vue.global.prod.js');
    checkTrue('vendor 里有 Vue', fs.existsSync(vueFile));
    if (!fs.existsSync(vueFile)) return 1;

    const dom = new JSDOM('<!DOCTYPE html><html><body><div id="app"></div></body></html>', {
      url: 'http://127.0.0.1:8000/#/vault',
      runScripts: 'outside-only',
      pretendToBeVisual: true,
    });
    const W = dom.window;
    global.window = W;
    global.document = W.document;
    global.navigator = W.navigator;
    global.__TOASTS__ = [];
    W.__TOASTS__ = global.__TOASTS__;
    W.eval(fs.readFileSync(vueFile, 'utf8'));
    global.__VUE__ = W.Vue;
    checkTrue('Vue 进了 window', !!W.Vue);

    const mod = await import(pathToFileURL(path.join(tmp, 'src', 'views', 'Vault.js')).href);
    const app = W.Vue.createApp(mod.default);
    const vm = app.mount('#app');
    await tick(300);                      // 等 onMounted 里两个请求回来

    const D = W.document;
    const html = D.querySelector('#app').innerHTML;

    checkTrue('视图真的挂起来了（不是未编译的模板）',
      !html.includes('{{ ') && !html.includes('v-if='), '含 ' + html.length + ' 字符');

    // ① Element Plus 那条坑：项目没引它，页面上就不该出现 el-* 标签
    const elTags = Array.from(D.querySelectorAll('*'))
      .map((e) => e.tagName.toLowerCase())
      .filter((t) => t.startsWith('el-'));
    check('页面上没有未定义的 el- 标签（项目没引 Element Plus）', elTags, []);

    // ② 路径与状态数字来自接口
    checkTrue('vault 路径上屏了', html.includes('D:/shike/backend/data/vault'));
    checkTrue('「3 篇题目 md」上屏了', html.includes('>3<'));
    checkTrue('「2 道题已关联」上屏了', html.includes('>2<'));
    checkTrue('收件箱待处理数上屏了', html.includes('>1<'));
    checkTrue('收件箱提示里写了子目录规则', html.includes('子文件夹名就是体系'));
    checkTrue('收件箱里的 md 计数上屏了', html.includes('收件箱里的 md'));
    checkTrue('等着解析的 md 清单上屏了',
      html.includes('等着解析的 md') && html.includes('2024 年卷一.md'));

    // ③ 点「同步」→ 报告出现
    checkTrue('还没点同步时没有报告', !html.includes('同步结果'));
    const syncBtn = btn(D, '同步（文件 → 题库）');
    checkTrue('找到同步按钮', !!syncBtn);
    syncBtn.click();
    await tick(300);

    const after = D.querySelector('#app').innerHTML;
    checkTrue('★ 点了同步之后出现「同步结果」', after.includes('同步结果'), '');
    checkTrue('★ 新增条数上屏（1）', after.includes('新增') && />\s*1\s*</.test(after));
    checkTrue('★ 新增的路径上屏', after.includes('题库/初一几何/三棱柱形礼盒.md'));
    checkTrue('★ 更新项上屏', after.includes('题库/另一道.md'));
    checkTrue('★ 失败原因上屏（不是只说「同步失败」）',
      after.includes('frontmatter 解析失败') && after.includes('不是合法 YAML'));
    checkTrue('提醒文案上屏', after.includes('找不到图片 缺失.png'));
    checkTrue('多图提醒上屏', after.includes('图二.png'));
    checkTrue('文件丢失提醒上屏', after.includes('题库/搬走了.md'));
    checkTrue('跳过的文件上屏', after.includes('没有 shike: question'));
    checkTrue('成功提示走了 ok（不是 fail）',
      global.__TOASTS__.some(([t]) => t === 'ok'), JSON.stringify(global.__TOASTS__));

    // ④ 点「预览」→ 标题变成「预览结果」
    const dryBtn = btn(D, '只预览会改什么');
    checkTrue('找到预览按钮', !!dryBtn);
    dryBtn.click();
    await tick(300);
    const dry = D.querySelector('#app').innerHTML;
    checkTrue('★ 预览时标题写的是「预览结果」', dry.includes('预览结果'));
    checkTrue('预览时也标了「没有改动任何东西」', dry.includes('没有改动任何东西'));

    // ⑤ 导出按钮：没东西可导时给出「没有需要导出的题」而不是静默
    const expBtn = btn(D, '把题库里的题导出成 md');
    checkTrue('找到导出按钮', !!expBtn);
    expBtn.click();
    await tick(300);
    checkTrue('导出后给出人话提示',
      global.__TOASTS__.some(([, m]) => String(m).includes('没有需要导出的题')),
      JSON.stringify(global.__TOASTS__));

    // ⑥ 收件箱「现在处理一次」：md 摄取的结果必须看得见 ——
    //    收到了几道、哪份没进来、为什么、源文件去哪了。只报「处理完成」等于没说。
    const inBtn = btn(D, '现在处理一次');
    checkTrue('找到收件箱处理按钮', !!inBtn);
    inBtn.click();
    await tick(300);
    const ing = D.querySelector('#app').innerHTML;
    checkTrue('★ md 摄取结果上屏（收到 2 道题）', ing.includes('收到 2 道题'), '');
    checkTrue('★ 没进来的那份标了「得先按题拆开」', ing.includes('得先按题拆开'));
    checkTrue('★ 拦下的原因写出来了', ing.includes('没找到分隔线'));
    checkTrue('★ 源文件去向也写出来了', ing.includes('_已处理/2024 年卷一.md'));
    checkTrue('摄取成功的提示走 ok',
      global.__TOASTS__.some(([, m]) => String(m).includes('收进 2 道题')),
      JSON.stringify(global.__TOASTS__));
    checkTrue('没进来的那份走了 fail（不是静默）',
      global.__TOASTS__.some(([t]) => t === 'fail'), JSON.stringify(global.__TOASTS__));
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }

  console.log('\n  ' + (total - fails.length) + '/' + total + ' 通过');
  if (fails.length) {
    console.log('  FAIL: ' + fails.join(' | '));
    return 1;
  }
  return 0;
}

main().then((c) => process.exit(c)).catch((e) => {
  console.error('  [FAIL] 挂载过程中抛错:', e);
  process.exit(1);
});
