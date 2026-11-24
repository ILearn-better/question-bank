/**
 * 「课表 → 学生信息面板」这条快捷路径的检查。
 *
 *   node frontend/schedule_student_link_check.js   # 退出码即结果（0 通过 / 跳过，非 0 失败）
 *
 * ## 为什么必须真挂一遍
 *
 * 这次改的全是**事件接线**：`@click="onItemClick(ls, $event)"` 里那个 `$event` 有没有传进来、
 * Ctrl 分支有没有赶在 200ms 的 setTimeout 之前 return、`router.push` 拼出来的地址对不对。
 * 这些用静态断言一条也证明不了 —— 源码里写着 `ctrlKey` 不等于按下 Ctrl 之后真的会跳，
 * 更不等于「跳过去的同时背后不会多冒一个编辑弹窗」。
 *
 * 所以这里真挂 `views/Schedule.js`（原样复制，一行不改），配**真 vue-router**
 * （`vendor/vue-router.esm-browser.js`）和真 `store.js` / `calendar.js`，
 * 只把网络层（api）和两个弹窗组件换成桩。然后派**带 ctrlKey 的 click**，
 * 看 `location.hash` 到底变没变。
 *
 * ## 判据（全是用户看得见的结果）
 *
 *   · Ctrl+单击课块 → 地址栏变成 `#/students/7?from=schedule`
 *   · 而且**没有**弹出编辑窗（200ms 后也没有，说明 Ctrl 分支真的截住了定时器）
 *   · 普通单击 → 编辑窗照旧出现（改这一刀不能把原来的用法弄坏）
 *   · 编辑窗里有「看学生面板」入口，点了也过去
 *   · 调课模式下 Ctrl+单击不跳（调课模式优先，别在拖课途中把人带走）
 *   · 列表视图 / 本月课时费的链接也带着 `from=schedule`（返回才回得到课表）
 *
 * ## B 段：学生详情的「返回」按钮
 *
 * 挂整个 StudentDetail 太重（8 个接口 + 7 个对话框），而这里要验的只有一条：
 * 从课表进来时「返回」指向课表。做法是**读源码里的真表达式真模板行**再执行它 ——
 * 比静态断言强（真的求值了），又不像整页挂载那样一改字段就假红。
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

/* ============================== 桩 ============================== */

/** 网络层。数据从 globalThis 拿，桩里不写字面量 —— 日期得跟着「本月」算，写死会过期。 */
const API_STUB = `
export const lessonsApi = {
  list: async () => (globalThis.__LESSONS__ || []),
  monthly: async () => (globalThis.__MONTHLY__ || null),
  create: async () => ({}),
  update: async () => ({}),
  complete: async () => ({}),
  remove: async () => ({}),
};
export const curriculumApi = { list: async () => (globalThis.__CURRICULA__ || []), nodes: async () => ({}) };
export const studentsApi = { list: async () => (globalThis.__STUDENTS__ || []) };
export const abilityApi = { dims: async () => [] };
export default {};
`;

/** Modal 只要透出 slot 就够了；带上 `.modal-stub` 这个类，好判断「弹窗到底开没开」。 */
const MODAL_STUB = `
export default {
  name: 'Modal',
  props: { title: { type: String, default: '' }, wide: { type: Boolean, default: false } },
  emits: ['close'],
  template: '<div class="modal-stub"><div class="modal-head">{{ title }}</div>'
    + '<div class="modal-body"><slot></slot></div>'
    + '<div class="modal-foot"><slot name="foot"></slot></div></div>',
};
`;

const IMPORT_DIALOG_STUB = `
export default { name: 'ScheduleImportDialog', emits: ['close', 'done'], template: '<div class="import-stub"></div>' };
`;

/** 组件里写的是 `from 'vue'`；让它落到 jsdom window 上那份 Vue。 */
const VUE_PKG = JSON.stringify({
  name: 'vue', version: '0.0.0-stub', type: 'module',
  main: 'index.js', exports: { '.': './index.js' },
}, null, 2);

/** vue-router 的浏览器构建要的 17 个名字，一个都不能少（少一个它 import 就失败）。 */
const VUE_SHIM = `
const V = globalThis.__VUE__;
if (!V) throw new Error('jsdom window 上没有 Vue —— vendor 没喂进去');
export const computed = (...a) => V.computed(...a);
export const nextTick = (...a) => V.nextTick(...a);
export const ref = (...a) => V.ref(...a);
export const watch = (...a) => V.watch(...a);
export const watchEffect = (...a) => V.watchEffect(...a);
export const reactive = (...a) => V.reactive(...a);
export const shallowReactive = (...a) => V.shallowReactive(...a);
export const shallowRef = (...a) => V.shallowRef(...a);
export const onMounted = (...a) => V.onMounted(...a);
export const onBeforeUnmount = (...a) => V.onBeforeUnmount(...a);
export const onUnmounted = (...a) => V.onUnmounted(...a);
export const onActivated = (...a) => V.onActivated(...a);
export const onDeactivated = (...a) => V.onDeactivated(...a);
export const inject = (...a) => V.inject(...a);
export const provide = (...a) => V.provide(...a);
export const getCurrentInstance = (...a) => V.getCurrentInstance(...a);
export const unref = (...a) => V.unref(...a);
export const defineComponent = (...a) => V.defineComponent(...a);
export const h = (...a) => V.h(...a);
export const createApp = (...a) => V.createApp(...a);
export const markRaw = (...a) => V.markRaw(...a);
export default V;
`;

const ROUTER_PKG = JSON.stringify({
  name: 'vue-router', version: '4.4.5-stub', type: 'module',
  main: 'index.js', exports: { '.': './index.js' },
}, null, 2);

const DEVTOOLS_PKG = JSON.stringify({
  name: '@vue/devtools-api', version: '6.0.0-stub', type: 'module',
  main: 'index.js', exports: { '.': './index.js' },
}, null, 2);

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

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 派一次真 click。ctrl=true 时带 ctrlKey —— Vue 的 `@click` 读到的是这个字段。 */
function click(win, el, opts) {
  const o = Object.assign({ bubbles: true, cancelable: true, view: win }, opts || {});
  el.dispatchEvent(new win.MouseEvent('click', o));
}

/* ============================== main ============================== */

async function main() {
  console.log('课表 → 学生信息面板 · 真挂载检查（真 vue-router + 派真 click）');

  const jsdomDir = loadJsdom();
  if (!jsdomDir) {
    console.log('  [SKIP] 没找到 jsdom（可选依赖），跳过');
    return 0;
  }
  console.log('  jsdom: ' + jsdomDir);

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sched_link_'));
  let dom = null;
  try {
    /* ---- 1. 骨架：真组件原样复制，只有网络层和弹窗换桩 ---- */
    const src = path.join(tmp, 'src');
    const comps = path.join(src, 'components');
    const nm = path.join(tmp, 'node_modules');
    fs.mkdirSync(comps, { recursive: true });
    for (const p of [path.join(nm, 'vue'), path.join(nm, 'vue-router'), path.join(nm, '@vue', 'devtools-api')]) {
      fs.mkdirSync(p, { recursive: true });
    }
    fs.mkdirSync(path.join(src, 'views'), { recursive: true });
    fs.copyFileSync(path.join(ROOT, 'src', 'views', 'Schedule.js'), path.join(src, 'views', 'Schedule.js'));
    fs.copyFileSync(path.join(ROOT, 'src', 'calendar.js'), path.join(src, 'calendar.js'));
    fs.copyFileSync(path.join(ROOT, 'src', 'store.js'), path.join(src, 'store.js'));
    fs.writeFileSync(path.join(src, 'api.js'), API_STUB);
    fs.writeFileSync(path.join(comps, 'Modal.js'), MODAL_STUB);
    fs.writeFileSync(path.join(comps, 'ScheduleImportDialog.js'), IMPORT_DIALOG_STUB);

    fs.writeFileSync(path.join(nm, 'vue', 'package.json'), VUE_PKG);
    fs.writeFileSync(path.join(nm, 'vue', 'index.js'), VUE_SHIM);
    // 真 vue-router（与页面用的是同一份 vendor 文件），只换掉它的 devtools 依赖
    fs.copyFileSync(path.join(ROOT, 'vendor', 'vue-router.esm-browser.js'), path.join(nm, 'vue-router', 'index.js'));
    fs.writeFileSync(path.join(nm, 'vue-router', 'package.json'), ROUTER_PKG);
    fs.writeFileSync(path.join(nm, '@vue', 'devtools-api', 'package.json'), DEVTOOLS_PKG);
    fs.writeFileSync(path.join(nm, '@vue', 'devtools-api', 'index.js'),
      'export function setupDevtoolsPlugin() {}\nexport default {};\n');

    /* ---- 2. 本月的课：日期跟着今天算，不写死 ---- */
    const now = new Date();
    const p2 = (n) => String(n).padStart(2, '0');
    const period = `${now.getFullYear()}-${p2(now.getMonth() + 1)}`;
    const day = `${period}-05`;                 // 本月 5 号，避开「今天」这种会飘的断言

    globalThis.__STUDENTS__ = [{ id: 7, name: '张同学', grade: '初二', hourly_rate: 200, curriculum_ids: [1] }];
    globalThis.__CURRICULA__ = [{ id: 1, name: '初中' }];
    globalThis.__LESSONS__ = [{
      id: 101, student_id: 7, student_name: '张同学',
      start_at: `${day}T19:00`, duration_min: 60, status: 'done',
      mode: 'offline', location: '', rate: 200, amount: 200, topic: '函数图像',
    }];
    globalThis.__MONTHLY__ = {
      total_lessons: 1, total_minutes: 60, total_amount: 200,
      pending_total_amount: 0, pending_total_lessons: 0, pending_no_rate: 0,
      students: [{
        student_id: 7, student_name: '张同学', lesson_count: 1, billable_minutes: 60,
        amount: 200, pending_lessons: 0, pending_minutes: 0, pending_amount: 0,
      }],
    };

    /* ---- 3. jsdom + vendor ---- */
    dom = new JSDOM('<!DOCTYPE html><html><body><div id="app"></div></body></html>', {
      url: 'http://127.0.0.1:8000/#/schedule',
      runScripts: 'outside-only',
      pretendToBeVisual: true,
    });
    const W = dom.window;
    global.window = W;
    global.document = W.document;
    global.navigator = W.navigator;
    // vue-router 的浏览器构建直接引用裸 `location` / `history`（默认在 window 上），
    // Node 里没有这两个全局 —— 不补的话 createWebHashHistory() 第一行就 ReferenceError
    global.location = W.location;
    global.history = W.history;
    W.confirm = () => true;
    W.eval(fs.readFileSync(path.join(ROOT, 'vendor', 'vue.global.prod.js'), 'utf8'));
    globalThis.__VUE__ = W.Vue;
    checkTrue('Vue 进了 window（jsdom 用 vendor 那份）', !!W.Vue, typeof W.Vue);

    /* ---- 4. 挂载（真 router） ---- */
    const routerMod = await import(pathToFileURL(path.join(nm, 'vue-router', 'index.js')).href);
    const router = routerMod.createRouter({
      history: routerMod.createWebHashHistory(),
      routes: [
        { path: '/', component: { template: '<div>首页</div>' } },
        { path: '/schedule', component: { template: '<div>课表</div>' } },
        { path: '/students/:id', component: { template: '<div>学生</div>' } },
      ],
    });
    const mod = await import(pathToFileURL(path.join(src, 'views', 'Schedule.js')).href);
    const app = W.Vue.createApp(mod.default);
    app.use(router);
    app.mount('#app');
    await sleep(300);                    // 等 onMounted 里那三个 await

    const D = W.document;
    const hash = () => String(W.location.hash || '');
    const items = () => Array.from(D.querySelectorAll('.cal-item'));

    checkTrue('月历里渲染出了这节课（.cal-item）', items().length === 1, items().length + ' 个');
    // 测试里这张路由表是手写的（真 router.js 会把 7 个页面全拉进来），
    // 所以得回头对一眼：万一以后改了路径，这里必须跟着改，别让检查自己骗自己。
    const routerSrc = fs.readFileSync(path.join(ROOT, 'src', 'router.js'), 'utf8');
    checkTrue('真路由表里有 /students/:id（跳的地址对得上）',
      /path:\s*'\/students\/:id'/.test(routerSrc));
    checkTrue('真路由表里有 /schedule（「返回课表」的地址存在）',
      /path:\s*'\/schedule'/.test(routerSrc));
    checkTrue('课块的 title 写了「Ctrl+单击看学生」',
      items().length === 1 && String(items()[0].getAttribute('title')).includes('Ctrl+单击看学生'),
      items().length ? String(items()[0].getAttribute('title')).slice(-30) : '没有课块');
    checkTrue('页面底部说明了 Ctrl+单击',
      String(D.body.textContent).includes('Ctrl+单击'),
      '正文里' + (String(D.body.textContent).includes('Ctrl+单击') ? '有' : '没有') + '这几个字');

    /* ---- 5a. 先单击留一个挂起的定时器，紧接着 Ctrl+单击 → 只跳转，别弹窗 ---- */
    // 这条专门守 Ctrl 分支里的 `clearTimeout`：光有「Ctrl 就 return」是不够的，
    // 手快的时候第一下已经挂了 200ms 的弹窗定时器，不清掉它就会「跳过去了、背后又一个窗」。
    click(W, items()[0]);                // 普通单击 → 定时器挂起
    await sleep(60);
    click(W, items()[0], { ctrlKey: true });
    await sleep(330);                    // 等过那个 200ms，弹窗要是该冒出来就冒出来了
    check('先单击再 Ctrl+单击：目的地对', hash(), '#/students/7?from=schedule');
    checkTrue('★ 先单击再 Ctrl+单击：不弹窗（Ctrl 分支真的 clearTimeout 了）',
      !D.querySelector('.modal-stub'), D.querySelector('.modal-stub') ? '背后多了一个弹窗' : '干净');

    /* ---- 5b. 纯粹 Ctrl+单击 → 真的到学生页 ---- */
    router.push('/schedule');
    await sleep(150);
    check('回到课表', hash(), '#/schedule');
    click(W, items()[0], { ctrlKey: true });
    await sleep(320);                    // 故意等过 CLICK_DELAY(200)，验证弹窗没冒出来
    console.log('     ctrl+click 之后 hash = ' + hash());
    checkTrue('★ Ctrl+单击课块 → 地址栏到该学生（带 from=schedule）',
      hash() === '#/students/7?from=schedule', hash());
    checkTrue('★ Ctrl+单击**没有**弹出编辑窗（Ctrl 分支真截住了那个 200ms 定时器）',
      !D.querySelector('.modal-stub'), D.querySelector('.modal-stub') ? '弹窗开了' : '没有弹窗');

    /* ---- 6. 普通单击照旧弹编辑窗（改这一刀不许弄坏原用法） ---- */
    router.push('/schedule');
    await sleep(120);
    click(W, items()[0]);                // 不按 Ctrl
    checkTrue('单击后 200ms 内还没弹窗（有防双击的延迟）', !D.querySelector('.modal-stub'));
    await sleep(260);
    const modal = D.querySelector('.modal-stub');
    checkTrue('★ 普通单击 → 编辑窗照旧打开', !!modal);
    checkTrue('编辑窗标题是「编辑课时」',
      !!modal && String(modal.querySelector('.modal-head').textContent).trim() === '编辑课时',
      modal ? String(modal.querySelector('.modal-head').textContent).trim() : '没开');

    /* ---- 7. 编辑窗里的入口也能过去 ---- */
    const panelLink = modal && Array.from(modal.querySelectorAll('a'))
      .find((a) => String(a.textContent).includes('看学生面板'));
    checkTrue('编辑窗里有「看学生面板」入口', !!panelLink,
      modal ? '弹窗开了，但里面没有这个链接' : '没开窗');
    checkTrue('这个入口是真链接（带 href 和 from=schedule，可中键开新标签）',
      !!panelLink && String(panelLink.getAttribute('href')).includes('/students/7')
      && String(panelLink.getAttribute('href')).includes('from=schedule'),
      panelLink ? String(panelLink.getAttribute('href')) : '没有链接');
    if (panelLink) {
      panelLink.dispatchEvent(new W.MouseEvent('click', { bubbles: true, cancelable: true, view: W }));
      await sleep(150);
      checkTrue('★ 点它 → 也到该学生（带 from=schedule）',
        hash() === '#/students/7?from=schedule', hash());
    }

    /* ---- 8. 调课模式里 Ctrl+单击不跳（调课优先） ---- */
    router.push('/schedule');
    await sleep(150);
    items()[0].dispatchEvent(new W.MouseEvent('dblclick', { bubbles: true, cancelable: true, view: W }));
    await sleep(120);
    checkTrue('双击进入了调课模式（.cal-drag-tip 出现）', !!D.querySelector('.cal-drag-tip'));
    click(W, items()[0], { ctrlKey: true });
    await sleep(120);
    check('调课模式下 Ctrl+单击不跳走', hash(), '#/schedule');
    // 收拾现场：Esc 退出调课模式
    W.dispatchEvent(new W.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await sleep(80);

    /* ---- 9. 列表视图 / 本月课时费的链接也带 from ---- */
    const listBtn = Array.from(D.querySelectorAll('.page-head .seg button'))
      .find((b) => String(b.textContent).includes('列表'));
    checkTrue('有「列表」视图切换按钮', !!listBtn);
    if (listBtn) {
      listBtn.click();
      await sleep(150);
      const rowLink = D.querySelector('.lesson-row a[href]');
      checkTrue('★ 列表视图里学生名是指向该学生的链接',
        !!rowLink && String(rowLink.getAttribute('href')).includes('/students/7'),
        rowLink ? String(rowLink.getAttribute('href')) : '没有链接');
      checkTrue('列表视图的链接带着 from=schedule（返回才回得到课表）',
        !!rowLink && String(rowLink.getAttribute('href')).includes('from=schedule'),
        rowLink ? String(rowLink.getAttribute('href')) : '没有链接');
    }
    const billLink = D.querySelector('table.tbl a[href]');
    checkTrue('★ 本月课时费里的学生名也指向该学生、且带 from=schedule',
      !!billLink && String(billLink.getAttribute('href')).includes('/students/7')
      && String(billLink.getAttribute('href')).includes('from=schedule'),
      billLink ? String(billLink.getAttribute('href')) : '没有链接');

    app.unmount();

    /* ============ B 段：学生详情的「返回」按钮 ============ */
    console.log('\n学生详情的「返回」指向哪儿（真表达式 + 真模板行）');
    const detailSrc = fs.readFileSync(path.join(ROOT, 'src', 'views', 'StudentDetail.js'), 'utf8');

    // ① 判断条件：直接从源码里抠出表达式再求值，不复制一份「等价物」
    const cmpM = /const fromSchedule = computed\(\(\) => ([\s\S]+?)\);/.exec(detailSrc);
    checkTrue('源码里有 fromSchedule 这个判断', !!cmpM, cmpM ? cmpM[1].trim() : '没找到');
    if (cmpM) {
      let judge;
      try {
        judge = new Function('route', 'return (' + cmpM[1] + ');');   // eslint-disable-line no-new-func
      } catch (e) {
        judge = null;
        checkTrue('表达式能被求值', false, e.message);
      }
      if (judge) {
        check('from=schedule → 认得是从课表来的', judge({ query: { from: 'schedule' } }), true);
        check('没有 from → 不是从课表来的', judge({ query: {} }), false);
        check('from=student（别的入口）→ 不认', judge({ query: { from: 'student' } }), false);
        check('from 拼错 → 不认（不报错）', judge({ query: { from: 'schedul' } }), false);
      }
    }

    // ② 模板行：从源码里取出那一行 router-link，真编译真渲染两种状态
    const lineM = /<router-link[^>]*fromSchedule[^>]*>[\s\S]*?<\/router-link>/.exec(detailSrc);
    checkTrue('模板里有按 fromSchedule 切目标的 router-link', !!lineM,
      lineM ? lineM[0].replace(/\s+/g, ' ').slice(0, 70) : '没找到');
    if (lineM) {
      const host = W.document.createElement('div');
      host.innerHTML = '<div id="back-probe"></div>';
      W.document.body.appendChild(host);
      const Probe = {
        props: ['fromSchedule'],
        components: {
          // 只测「三元选中了哪个目标」，不测 vue-router 的 to→href，所以这里用个直通的壳
          RouterLink: {
            props: ['to'],
            template: '<a class="probe-link" :href="String(to)"><slot></slot></a>',
          },
        },
        template: '<div>' + lineM[0] + '</div>',
      };
      const probe = W.Vue.createApp(Probe, { fromSchedule: false });
      probe.mount('#back-probe');
      await sleep(60);
      const a1 = W.document.querySelector('#back-probe .probe-link');
      checkTrue('★ 从学生列表来的：按钮指向 /students', !!a1 && a1.getAttribute('href') === '/students',
        a1 ? a1.getAttribute('href') : '没渲染出来');
      check('从学生列表来的：文案还是「返回列表」',
        a1 ? String(a1.textContent).trim() : '', '返回列表');
      probe.unmount();

      const probe2 = W.Vue.createApp(Probe, { fromSchedule: true });
      probe2.mount('#back-probe');
      await sleep(60);
      const a2 = W.document.querySelector('#back-probe .probe-link');
      checkTrue('★ 从课表来的：按钮指向 /schedule', !!a2 && a2.getAttribute('href') === '/schedule',
        a2 ? a2.getAttribute('href') : '没渲染出来');
      check('从课表来的：文案是「← 返回课表」',
        a2 ? String(a2.textContent).trim() : '', '← 返回课表');
      probe2.unmount();
    }
  } finally {
    try { if (dom) dom.window.close(); } catch (e) { /* 关不掉也无妨 */ }
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
