// 量「批改台交互一致性」：快捷键行为 + 四条改动的源码约束。
//
// 为什么这么量：
//   · 键位表（src/gradeKeys.js）是纯逻辑 → 直接驱动，能真验「按 E 是不是橡皮」。
//   · 其余是零构建页（没有打包步骤、没有类型检查）→ 只能对源码断言结构约束。
//     这类断言的意义是「防止下一轮改动把刚修好的东西悄悄改回去」，不是替代真浏览器。
//
// node 里没有 window，所以 installGradeKeys 用一个自造的事件总线当 target。
const path = require('path');
const fs = require('fs');
const { pathToFileURL } = require('url');

let total = 0;
const fails = [];
function check(name, ok, detail) {
  total += 1;
  if (ok) {
    console.log('  [OK ] ' + name);
  } else {
    console.log('  [FAIL] ' + name + (detail === undefined ? '' : '   <- ' + JSON.stringify(detail)));
    fails.push(name);
  }
}
const ROOT = __dirname;
const read = (p) => fs.readFileSync(path.join(ROOT, p), 'utf8');

/** 造一个假键盘事件。键名/修饰键保持浏览器口径。 */
function key(k, opt) {
  const o = opt || {};
  const e = {
    key: k,
    ctrlKey: !!o.ctrl, shiftKey: !!o.shift, altKey: !!o.alt, metaKey: !!o.meta,
    target: o.target || { tagName: 'DIV' },
    defaultPrevented: false,
    preventDefault() { e.defaultPrevented = true; },
  };
  return e;
}
const INPUT_T = { tagName: 'INPUT' };
const AREA_T = { tagName: 'TEXTAREA' };
const CE_T = { tagName: 'DIV', isContentEditable: true };

/** 最小事件总线，当 installGradeKeys 的 target 用。 */
function bus() {
  const ls = [];
  return {
    addEventListener(t, fn) { if (t === 'keydown') ls.push(fn); },
    removeEventListener(t, fn) { const i = ls.indexOf(fn); if (i >= 0) ls.splice(i, 1); },
    fire(e) { ls.slice().forEach((fn) => fn(e)); return e; },
    count() { return ls.length; },
  };
}

async function main() {
  const gk = await import(pathToFileURL(path.join(ROOT, 'src/gradeKeys.js')).href);
  const { installGradeKeys, KEY_TOOL, isTypingTarget } = gk;

  // ============================================================
  console.log('\n==== ① 工具键：按一下就到位（不是 toggle） ====');
  // ============================================================
  {
    const b = bus();
    const tools = [];
    installGradeKeys({ target: b, tool: (t) => tools.push(t) });
    b.fire(key('b'));
    b.fire(key('e'));
    b.fire(key('e'));      // 连按两次橡皮：两次都必须是 erase
    b.fire(key('v'));
    b.fire(key('t'));
    b.fire(key('c'));
    b.fire(key('x'));
    b.fire(key('l'));
    b.fire(key('r'));
    check('B/E/E/V/T/C/X/L/R → ink,erase,erase,view,text,check,cross,line,region',
      JSON.stringify(tools) === JSON.stringify(['ink', 'erase', 'erase', 'view', 'text', 'check', 'cross', 'line', 'region']),
      tools);
    check('大写也认（按住 Shift 时 e.key 是大写）', (() => {
      const b2 = bus(); const t2 = [];
      installGradeKeys({ target: b2, tool: (t) => t2.push(t) });
      b2.fire(key('E', { shift: true }));
      return t2.length === 1 && t2[0] === 'erase';
    })(), null);
    check('不认识的键不触发任何工具', (() => {
      const b2 = bus(); const t2 = [];
      installGradeKeys({ target: b2, tool: (t) => t2.push(t) });
      b2.fire(key('q')); b2.fire(key('F5'));
      return t2.length === 0;
    })(), null);
  }

  // ============================================================
  console.log('\n==== ② 输入框里不抢键（规则 1） ====');
  // ============================================================
  {
    check('isTypingTarget：INPUT / TEXTAREA / contentEditable 都算在打字',
      isTypingTarget(INPUT_T) && isTypingTarget(AREA_T) && isTypingTarget(CE_T) && !isTypingTarget({ tagName: 'DIV' }), null);
    for (const [nm, t] of [['INPUT', INPUT_T], ['TEXTAREA', AREA_T], ['contentEditable', CE_T]]) {
      const b = bus(); const hits = [];
      installGradeKeys({ target: b, tool: (x) => hits.push(x), undo: () => hits.push('undo') });
      b.fire(key('e', { target: t }));
      b.fire(key('z', { ctrl: true, target: t }));
      check('在 ' + nm + ' 里按 E / Ctrl+Z 都不抢（留给浏览器撤销打字）', hits.length === 0, hits);
    }
    const b = bus(); let saved = 0;
    installGradeKeys({ target: b, save: () => { saved += 1; } });
    b.fire(key('s', { ctrl: true, target: AREA_T }));
    check('★ 例外：Ctrl+S 在输入框里也要拦（否则浏览器弹「保存网页」）', saved === 1, saved);
  }

  // ============================================================
  console.log('\n==== ③ 撤销 / Esc / 重做 ====');
  // ============================================================
  {
    const b = bus(); let undo = 0;
    installGradeKeys({ target: b, undo: () => { undo += 1; } });
    b.fire(key('z', { ctrl: true }));
    b.fire(key('z', { meta: true }));
    check('Ctrl+Z 与 Cmd+Z 都触发撤销', undo === 2, undo);
    b.fire(key('z', { ctrl: true, shift: true }));
    b.fire(key('y', { ctrl: true }));
    check('★ 重做（Ctrl+Shift+Z / Ctrl+Y）明确不接 —— 接了没实现比不接更坏', undo === 2, undo);

    const b2 = bus(); let esc = 0;
    installGradeKeys({ target: b2, escape: () => { esc += 1; } });
    b2.fire(key('Escape'));
    check('Esc 触发 escape（回选择 / 关文字框）', esc === 1, esc);
  }

  // ============================================================
  console.log('\n==== ④ 查看模式：只禁写，不禁看（规则 2） ====');
  // ============================================================
  {
    const b = bus(); const hits = [];
    installGradeKeys({
      target: b, canEdit: () => false,
      tool: (x) => hits.push(x), undo: () => hits.push('undo'), escape: () => hits.push('esc'),
    });
    b.fire(key('b'));
    b.fire(key('e'));
    b.fire(key('z', { ctrl: true }));
    check('★ 查看模式下工具键与撤销一律不响应', hits.length === 0, hits);
    b.fire(key('Escape'));
    check('Esc 在查看模式仍然响应（它只是退出当前工具，不写数据）', hits.length === 1 && hits[0] === 'esc', hits);
    let saved = 0;
    const b2 = bus();
    installGradeKeys({ target: b2, canEdit: () => false, save: () => { saved += 1; } });
    b2.fire(key('s', { ctrl: true }));
    check('Ctrl+S 在查看模式仍响应（保存不写数据）', saved === 1, saved);
  }

  // ============================================================
  console.log('\n==== ⑤ 卸载真的要摘掉监听 ====');
  // ============================================================
  {
    const b = bus(); const hits = [];
    const off = installGradeKeys({ target: b, tool: (x) => hits.push(x) });
    check('装上之后监听数是 1', b.count() === 1, b.count());
    off();
    check('卸载之后监听被摘掉', b.count() === 0, b.count());
    b.fire(key('e'));
    check('★ 卸载后按键不再触发（切走页面还在抢键的老毛病）', hits.length === 0, hits);
  }

  // ============================================================
  console.log('\n==== ⑥ 静态：两页共用同一份键位表 ====');
  // ============================================================
  const hw = read('src/views/HomeworkGrade.js');
  const csv = read('app.css');
  const layer = read('src/components/GradeMarkLayer.js');
  const wb = read('workbench.html');
  {
    check('HomeworkGrade.js import 了 gradeKeys.js', /from '\.\.\/gradeKeys\.js'/.test(hw));
    check('onMounted 里装了快捷键', /keysOff = installGradeKeys\(/.test(hw), null);
    check('★ onBeforeUnmount 里摘掉（否则切页面后还在抢 B/E）', /if \(keysOff\) \{ keysOff\(\); keysOff = null; \}/.test(hw), null);
    check('查看模式判据唯一出处 = route.query.view', /const readOnly = computed\(\(\) => String\(route\.query\.view \|\| ''\) === '1'\)/.test(hw), null);
    check('★ 标注层在查看模式下强制 view（不能靠 tool 恰好是 view）', /:mode="readOnly \? 'view' : tool"/.test(hw), null);
    check('★ 工具栏禁用而非隐藏（隐藏会让卷面跳位）', /:class="\{ readonly: readOnly \}"/.test(hw) && /\.grade-tools\.readonly \{/.test(csv), null);
    check('KEY_TOOL 的 8 个键都在（b/e/v/t/c/x/l/r）',
      ['b', 'e', 'v', 't', 'c', 'x', 'l', 'r'].every((k) => KEY_TOOL[k]), KEY_TOOL);
    check('gradeKeys 只列真接了的键（提示文案与映射不互相骗）',
      Array.isArray(gk.KEY_HINTS) && gk.KEY_HINTS.length >= 8, gk.KEY_HINTS && gk.KEY_HINTS.length);
    check('workbench.html 里还留着它自己的 onKeyDown（这一轮未合并，下轮统一）',
      /onKeyDown\(e\) \{/.test(wb), null);
  }

  // ============================================================
  console.log('\n==== ⑦ 静态：第 1 条 工具栏固定 ====');
  // ============================================================
  {
    check('★ 模板里有一层 .grade-head 包住翻页条/工具条', /<div class="grade-head">/.test(hw), null);
    check('★ .grade-head 是 sticky top:0（这是「滚下去还能换工具」的直接判据）',
      /\.grade-head \{[^}]*position: sticky; top: 0;[^}]*\}/.test(csv), null);
    check('★ .grade-head 有背景色（不给背景，卷面会从字缝里透过去）',
      /\.grade-head \{[^}]*background: var\(--panel\)/.test(csv), null);
    // 顺序：grade-head 必须在 grade-page 之前，否则「固定」的是卷面下面的东西
    check('★ 结构顺序：.grade-head 在 .grade-page 之前',
      hw.indexOf('<div class="grade-head">') > 0
      && hw.indexOf('<div class="grade-head">') < hw.indexOf('<div class="grade-viewer">'), null);
    check('★ 查看器观感：灰底容器存在，且**没有**给它限高',
      /\.grade-viewer \{/.test(csv) && !/\.grade-viewer \{[^}]*max-height/.test(csv), null);
    check('⚠️ .grade-page 仍不允许 max-height（标注层按容器等比铺，限高会错位）',
      !/\.grade-page \{[^}]*max-height/.test(csv), null);
  }

  // ============================================================
  console.log('\n==== ⑧ 静态：第 2 条 画笔精度 / 笔尖 ====');
  // ============================================================
  {
    check('★ 草稿线也带 vector-effect（预览与成品同口径，松手线宽不跳）',
      /\.gmark-draft \{[^}]*vector-effect: non-scaling-stroke/.test(csv), null);
    check('成品笔迹也带（两边必须一致）',
      /\.gmark \.shape \{[^}]*vector-effect: non-scaling-stroke/.test(csv)
      || /\.gmark \.shape\.ink \{[^}]*/.test(csv), null);
    check('★ 落笔第一个点就渲染（draft.length >= 1）', /v-if="draft\.length >= 1"/.test(layer), null);
    check('★ 不再有 length > 900 直接丢弃（那会让笔尖停在半路）', !/length > 900/.test(layer), null);

    // —— 到上限后必须「替换最后一个点」而不是丢弃 ——
    // 落点从 onMove 挪进了 flushDraft（合帧后统一追加），所以断言跟着挪：
    // 只认「MAX_POINTS 存在」+「flushDraft 里有 slice(0,-1).concat(」这一对，
    // 不再钉 [x, y] 这种字面量（钉字面量 = 一改采样就假红）。
    const flushBody = (layer.match(/function flushDraft\(\)[\s\S]*?\n    \}/) || [''])[0];
    check('★ 到上限改成替换最后一个点（继续跟手）',
      /const MAX_POINTS = \d+/.test(layer) && /slice\(0, -1\)\.concat\(/.test(flushBody), flushBody.slice(0, 120));

    check('★ pointercancel 走 onCancel，不再当抬手',
      /@pointercancel="onCancel"/.test(layer) && /function onCancel\(\)/.test(layer), null);

    // onCancel 的判定改成「取函数体再查」，不再用固定字符窗口：
    // 窗口法一加注释就假红（本轮就被撑破过一次），而且窗口跨出函数也看不出来。
    const cancelBody = (layer.match(/function onCancel\(\)[\s\S]*?\n    \}/) || [''])[0];
    check('onCancel 会清掉草稿（被打断的那一笔不提交）',
      /draft\.value = \[\]/.test(cancelBody), cancelBody.slice(0, 120));
    check('★ onCancel 先掐 rAF 再清草稿（顺序反了下一帧会把丢掉的点写回来）',
      cancelBody.indexOf('resetInk()') > 0
      && cancelBody.indexOf('resetInk()') < cancelBody.indexOf('draft.value = []'), cancelBody.slice(0, 160));
  }

  // ============================================================
  console.log('\n==== ⑧b 静态：第 2 条 采样（合帧 / 中间点 / 像素抽稀） ====');
  // ============================================================
  {
    // ④ 合帧：一帧里来 8 次 pointermove 只改一次响应式 draft
    check('★ pointermove 走 requestAnimationFrame 合帧（不是每次都改 draft）',
      /raf\(flushDraft\)/.test(layer)
      && /const raf = typeof requestAnimationFrame === 'function'/.test(layer), null);
    check('★ 合帧有取消能力（否则「丢掉的笔迹」会自己冒回来）',
      /function resetInk\(\)[\s\S]{0,120}caf\(rafId\)/.test(layer), null);
    check('★ onUp 先 flushDraft 再 emit（松手那一下的尾巴不能丢）',
      (() => {
        const b = (layer.match(/function onUp\(\)[\s\S]*?if \(props\.mode === 'region'\)/) || [''])[0];
        return b.indexOf('flushDraft()') > 0 && /props\.mode === 'ink'\) flushDraft\(\)/.test(b);
      })(), null);

    // ⑤ 取中间点：浏览器把一帧内的多个点合并成一个事件，不主动取就是折线弦
    check('★ 用 getCoalescedEvents() 取回被合并的中间点',
      /getCoalescedEvents/.test(layer), null);
    check('★ 老浏览器没有这个 API 时退化成单点（不能直接崩）',
      /typeof e\.getCoalescedEvents === 'function'/.test(layer)
      && /raw && raw\.length \? raw : \[e\]/.test(layer), null);

    // ⑥ 抽稀按屏幕像素判：归一化单位在 A4 页上 x/y 比例不同 → 各向异性
    check('★ 抽稀用屏幕像素距离（Math.hypot 比 clientX/clientY）',
      /Math\.hypot\(ev\.clientX - inkLastPx\[0\], ev\.clientY - inkLastPx\[1\]\) < MIN_STEP_PX/.test(layer), null);
    check('★ 归一化单位那个 MIN_STEP 已删（留着 = 下一个人会当成同一件事）',
      !/const MIN_STEP = 2\.2/.test(layer) && /const MIN_STEP_PX = /.test(layer), null);

    // ⑦ 每帧只算一次形状：以前 SVG 层和文字标签各调一次 shapesFor，各重建全页点数
    check('★ shapesFor 走 computed 缓存（每帧只重建一次）',
      /const allShapes = computed\(/.test(layer)
      && /const shapesFor = \(list\) => \(list === undefined \|\| list === props\.marks/.test(layer), null);
  }

  // ============================================================
  console.log('\n==== ⑨ 静态：第 4 条 文本框可调大小 ====');
  // ============================================================
  {
    check('★ 不再 nowrap + 省略号截断（长批注必须看得全）',
      !/\.gmark-label \{[^}]*white-space: nowrap/.test(csv)
      && !/\.gmark-label \{[^}]*text-overflow: ellipsis/.test(csv), null);
    check('★ 新数据按矩形渲染：去掉上浮（.gmark-label.sized）',
      /\.gmark-label\.sized \{ transform: none; \}/.test(csv), null);
    check('★ 右下角有 resize 手柄', /class="gmark-resize"/.test(layer) && /\.gmark-label \.gmark-resize \{/.test(csv), null);
    check('★ 手柄在画笔/橡皮/框题/分割模式下关掉（想落笔不该变成调大小）',
      /\.gmark-layer\.mode-ink \.gmark-label \.gmark-resize[\s\S]{0,200}display: none/.test(csv), null);
    check('★ 尺寸写进 geo 四元组，不借 width（width 是笔迹粗细）',
      /geo: \[Math\.round\(x1\), Math\.round\(y1\), Math\.round\(x1 \+ w\), Math\.round\(y1 \+ h\)\]/.test(hw), null);
    check('resize 松手走同一条 PATCH（emit move + geo）',
      /emit\('move', \{ mark: st\.mark, geo \}\)/.test(layer), null);
    check('★ 失败回退时把尺寸一起退（不能停在「看着变大了、库里没变」）',
      /function clearOffsets\(\) \{[\s\S]{0,160}resizing\.value = \{\}/.test(layer), null);
    check('buildShape 给出宽高百分比', /label: \{ left: pct\(x\), top: pct\(y\), w: w > 0 \? pct\(w\)/.test(layer), null);
  }

  // ============================================================
  console.log('\n==== ⑩ 静态：题目导航栏（点击定位 + 当前题高亮） ====');
  // ============================================================
  const navSrc = fs.existsSync(path.join(ROOT, 'src/gradeNav.js'))
    ? read('src/gradeNav.js') : '';
  {
    // 判题线要算「可视区顶到图片顶」的距离，靠左栏这个 ref 拿到容器 rect
    check('★ 左栏容器带 ref="leftCol"（navLinePx 靠它算判题线位置）',
      /ref="leftCol"/.test(hw), null);

    // 导航三件套：逐题导航项 + 选中题的操作区 + 点击定位/高亮
    check('★ 模板有 .grade-nav-item（逐题导航项）与 .grade-nav-actions（选中题操作区）',
      /class="grade-nav-item"/.test(hw) && /class="grade-nav-actions"/.test(hw), null);
    check('★ 点击定位 navJump / 当前高亮 navActive / 选中块 navBlock 三件套都在',
      /navJump/.test(hw) && /navActive/.test(hw) && /navBlock/.test(hw), null);
    check('★ 导航项点击走 navJump（不是只显示不跳）',
      /@click="navJump\(b\)"/.test(hw), null);
    check('★ 当前题高亮 = navActive 与 seq 相等（.on 类）',
      /on: navActive === b\.seq/.test(hw), null);

    // 「这块不判」的位置：必须在选中题后的 .grade-nav-actions，不能塞进每个题项行
    const navList = (hw.match(/<div class="grade-nav-list">[\s\S]*?<div v-if="editingRegions && navBlock" class="grade-nav-actions">/) || [''])[0];
    check('★ 题项行（.grade-nav-list 内）没有「这块不判」按钮',
      navList.length > 0 && !/这块不判/.test(navList), null);
    check('★ 「这块不判」在 .grade-nav-actions（选中题后才出现）',
      /class="grade-nav-actions"[\s\S]{0,400}这块不判/.test(hw), null);

    // 判据唯一出处：src/gradeNav.js，HomeworkGrade.js 从它 import activeSeqAt
    check('★ src/gradeNav.js 存在（activeSeqAt 唯一出处）',
      fs.existsSync(path.join(ROOT, 'src/gradeNav.js')), null);
    check('★ HomeworkGrade.js import 了 activeSeqAt',
      /from '\.\.\/gradeNav\.js'/.test(hw) && /activeSeqAt/.test(hw), null);
    check('★ gradeNav.js 导出 activeSeqAt',
      /export function activeSeqAt/.test(navSrc), navSrc.slice(0, 60));

    // 只读态（?view=1）导航仍可点：导航栏按「有块 或 编辑中」显示、题项 @click 无 readOnly 门槛
    check('★ 只读态导航仍可点：.grade-nav 显示条件不含 readOnly、题项 @click="navJump(b)" 不被挡',
      /v-if="regionBlocks\.length \|\| editingRegions" class="grade-nav card"/.test(hw)
      && /class="grade-nav-item"[\s\S]{0,160}@click="navJump\(b\)"/.test(hw), null);

    // 结构顺序：吸顶工具条在卷面查看器之前（卷面从它下面滚过去）
    check('★ 结构顺序：.grade-head 在 .grade-viewer 之前',
      hw.indexOf('<div class="grade-head">') > 0
      && hw.indexOf('<div class="grade-head">') < hw.indexOf('<div class="grade-viewer">'), null);

    // 旧面板已移除：导航栏取代 .grade-regions（防止旧结构悄悄回来）
    check('★ 旧 .grade-regions 面板已移除（导航栏取代）', !/grade-regions/.test(hw), null);
  }

  // ============================================================
  console.log('\n' + '='.repeat(60));
  if (fails.length === 0) {
    console.log('✅ 全部通过（' + total + ' 项）');
  } else {
    console.log('❌ 失败 ' + fails.length + ' / ' + total + ' 项：');
    fails.forEach((f) => console.log('   · ' + f));
  }
  console.log('='.repeat(60));
  process.exitCode = fails.length ? 1 : 0;
}

main().catch((e) => {
  console.error('脚本自己炸了：', e);
  process.exitCode = 1;
});
