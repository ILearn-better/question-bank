/**
 * 批量入库页「配图三档分级」的静态自检。
 *
 *     node frontend/batch_fig_check.js
 *
 * 为什么要单独一个检查脚本：分档逻辑决定了**老师要看哪些图**。
 * 判错的后果不是报错，是「该看的被折叠进可放心那一档」——
 * 老师一路点「全部通过」，错的图就进题库了，而且没人发现。
 * 这类错误只有真挂载、真点一遍才看得见；先把**判据本身**摁住。
 *
 * ⚠️ 断言里的代码一律先剥注释（code_only）：解释这个坑的那行注释本身
 *    会把自己弄红，是这套检查踩过的老坑。
 * ⚠️ 只做静态与纯函数验证，**不替代**真浏览器：`/>` 不自闭合这类问题
 *    在 HTML 解析阶段就坏了，静态看源码完全正常（见 MEMORY.md 那条）。
 */
'use strict';

const fs = require('fs');
const path = require('path');

const HTML = path.join(__dirname, 'batch.html');
const src = fs.readFileSync(HTML, 'utf8');

const fails = [];
function check(name, ok, detail) {
  console.log(`  [${ok ? 'OK ' : 'FAIL'}] ${name}${detail ? '  ' + detail : ''}`);
  if (!ok) fails.push(name);
}

/** 剥掉 HTML 注释与 JS 块注释 —— 只留真正会被执行的代码。 */
function codeOnly(text) {
  return text.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '');
}

/** 取出 `名字() {` 之后那一对花括号里的内容（含嵌套）。
 *
 *  ⚠️ `sig` 必须带上**那个 `{`**：模板里 `:style="candStyle(it, c)"` 也含同样的字样，
 *     只按名字找会命中模板、然后把后面某处的花括号当成函数体（实测踩到：
 *     fnBody 返回了错的内容，`mk()` 拿不到 return，报「Cannot read properties of undefined」）。 */
function fnBody(text, sig) {
  const esc = sig.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const m = new RegExp(esc + '\\s*\\{').exec(text);
  if (!m) return null;
  const j = m.index + m[0].length - 1;          // 指向那个 {
  let depth = 0;
  for (let k = j; k < text.length; k += 1) {
    if (text[k] === '{') depth += 1;
    else if (text[k] === '}') {
      depth -= 1;
      if (depth === 0) return text.slice(j + 1, k);
    }
  }
  return null;
}

const code = codeOnly(src);

// ---------------------------------------------------------------- 判据本身
console.log('\n==== ① 分档判据（跑的是页面里真正那份代码） ====');
const tierBody = fnBody(code, 'figTierOf()');
check('页面里能找到 figTierOf', !!tierBody);
let tierOf = null;
if (tierBody) {
  try {
    // 函数体就是 `return (it) => {...};` —— 用 Function 跑一遍拿回那个箭头函数，
    // 这样就**不用在检查脚本里抄一份判据**（抄一份必然漂）。
    tierOf = new Function(tierBody)();
  } catch (e) {
    check('figTierOf 能被求值', false, String(e));
  }
}
if (typeof tierOf === 'function') {
  const cases = [
    ['吸附到几何候选 + 有图 → 可放心',
      { figure_image: '/api/crops/a.png', flags: ['figure_snapped'] }, 'ok'],
    ['整页唯一候选 + 有图 → 可放心',
      { figure_image: '/api/crops/a.png', flags: ['figure_only_candidate'] }, 'ok'],
    ['估的框 → 待核', { figure_image: '/api/crops/a.png', flags: ['figure_rough'] }, 'check'],
    ['同页争图 → 待核',
      { figure_image: '/api/crops/a.png', flags: ['figure_shared_candidate'] }, 'check'],
    ['该有图却没配到 → 缺', { figure_image: '', needs_figure: 1,
      flags: ['figure_box_missing'] }, 'missing'],
    ['被同页别题占了 → 缺（不能当没事发生）',
      { figure_image: '', needs_figure: 1, flags: ['figure_dup_candidate'] }, 'missing'],
    ['框裁失败 → 缺', { figure_image: '', needs_figure: 1,
      flags: ['figure_crop_failed'] }, 'missing'],
    ['不需要图、也没图 → 不进任何档（不打扰老师）',
      { figure_image: '', needs_figure: 0, flags: [] }, ''],
    ['有图且无可疑标记，但 needs_figure 已取消 → 仍是可放心',
      { figure_image: '/api/crops/a.png', needs_figure: 0, flags: [] }, 'ok'],
    // 兜底那档：配图定位不到，但整道题已经裁成图了 → 出卷图一定在，不用人管。
    // ⚠️ 这类条目**同时带着** figure_box_missing，判据顺序反了就会一直挂在「要处理」里。
    ['整题图兜底 → 单列一档（不算缺图，不占人的时间）',
      { figure_image: '', image: '/api/crops/w.png', needs_figure: 1,
        flags: ['figure_whole_question', 'figure_box_missing'] }, 'covered'],
    ['兜底之后再手框了正确的图 → 回到可放心',
      { figure_image: '/api/crops/a.png', image: '', needs_figure: 1, flags: [] }, 'ok'],
  ];
  for (const [name, it, want] of cases) {
    const got = tierOf(it);
    check(name, got === want, `期望 ${want || '(空)'}，实际 ${got || '(空)'}`);
  }
}

// ---------------------------------------------------------------- 接进去了没
console.log('\n==== ② 分档、筛选、确认、候选点选都已经接进页面 ====');
check('列表按 shownItems 渲染（否则筛选形同虚设）',
  /v-for="it in shownItems"/.test(code));
check('没有残留 v-for="it in items"（会绕过筛选）',
  !/v-for="it in items"/.test(code));
check('只有 filtered 空了才提示，不能一直提示',
  /onlyFigTodo && !shownItems\.length/.test(code));
check('data 里有 onlyFigTodo / figCands',
  /onlyFigTodo:\s*false/.test(code) && /figCands:\s*\{\}/.test(code));
check('三档计数在工具栏显示', /figCounts\.ok/.test(code)
  && /figCounts\.check/.test(code) && /figCounts\.missing/.test(code));
check('兜底档也单独计数（否则它会被算进缺图）', /figCounts\.covered/.test(code));
check('兜底档的说明讲清了代价（不能改题干 / 搜不到）',
  /不能改题干/.test(code));
check('「待核的都确认」按钮存在', /ackAllCheck/.test(code));
check('逐张确认走 figure-ack 接口', /'\/figure-ack'/.test(code));
check('候选点选走 figure-candidates 接口', /'\/figure-candidates'/.test(code));
check('候选点选**复用** figure-crop 落库（不另开写入路径）',
  /pickCand[\s\S]{0,600}figure-crop/.test(code));
check('缺图的文案先引导「选现成的图」', /先试「选现成的图」/.test(code));

// ---------------------------------------------------------------- 取景数学
console.log('\n==== ③ 候选缩略图的取景换算（CSS background 百分比） ====');
const candBody = fnBody(code, 'candStyle(it, c)');
check('candStyle 存在', !!candBody);
if (candBody) {
  const mk = new Function('it', 'c', `
    const self = { figCands: { x: { page_image: '/img.png' } } };
    const it_ = { id: 'x' };
    ${candBody.replace(/this\.figCands/g, 'self.figCands')}
  `);
  // 取景目标：把框放大到容器大小再平移到框的左上角
  const style = mk({ id: 'x' }, { box: [100, 200, 300, 400] });
  const size = String(style.backgroundSize || '').split(' ');
  const pos = String(style.backgroundPosition || '').split(' ');
  const num = (s) => parseFloat(s);
  check('放大倍数 = 1000 / 框宽（横）', Math.abs(num(size[0]) - 1000 / 200 * 100) < 0.01,
    size[0]);
  check('放大倍数 = 1000 / 框高（纵）', Math.abs(num(size[1]) - 1000 / 200 * 100) < 0.01,
    size[1]);
  check('横向偏移 = x0/(1000-框宽)', Math.abs(num(pos[0]) - 100 / 800 * 100) < 0.01, pos[0]);
  check('纵向偏移 = y0/(1000-框高)', Math.abs(num(pos[1]) - 200 / 800 * 100) < 0.01, pos[1]);
  const full = mk({ id: 'x' }, { box: [0, 0, 1000, 1000] });
  check('整页框不除零（满宽时偏移取 0%）',
    /^0%/.test(String(full.backgroundPosition)) && /^100%/.test(String(full.backgroundSize)),
    `${full.backgroundSize} / ${full.backgroundPosition}`);
  check('底图用页面图（不为每个候选另裁小图）',
    /page_image/.test(candBody));
}

// ---------------------------------------------------------------- 模板卫生
console.log('\n==== ④ 模板卫生（这两条会整页白屏 / 全线报错） ====');
// ⚠️ 锚点必须用**真标签**，不能用在注释里 —— codeOnly 会把注释剥掉，整段变空、
//    断言就成了 0/0 的假绿（第一版就这么假绿过）。
const figStart = code.indexOf('class="fig-box"');
const figEnd = code.indexOf('class="item-foot"');
const figBlock = (figStart >= 0 && figEnd > figStart) ? code.slice(figStart, figEnd) : '';
const selfClosed = figBlock.match(/<el-[a-z-]+[^>]*\/>/g);
check('配图区没有自闭合的 el- 组件（in-DOM 模板会当成没闭合）',
  !selfClosed, selfClosed ? selfClosed.join(' ') : '');
const opens = (figBlock.match(/<el-[a-z-]+/g) || []).length;
const closes = (figBlock.match(/<\/el-[a-z-]+>/g) || []).length;
check('配图区 el- 开闭标签数量一致', opens === closes, `${opens} / ${closes}`);

const htmlComments = (src.match(/<!--/g) || []).length;
const htmlCommentsEnd = (src.match(/-->/g) || []).length;
check('HTML 注释配平（不配平会吞掉下一块）', htmlComments === htmlCommentsEnd,
  `${htmlComments} / ${htmlCommentsEnd}`);

console.log('\n' + (fails.length ? `失败 ${fails.length} 项：${fails.join('、')}` : '全部通过'));
process.exit(fails.length ? 1 : 0);
