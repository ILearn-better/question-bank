// 渲染管线自检：公式保护 + 两份实现一致 + 真实 marked 端到端。
//
// 为什么需要它：`entry.html`（零构建单文件页）和 `src/mathRender.js`（SPA 模块）
// 各有一份 protectMath / restoreMath —— 这是结构决定的，没法共用。两份一漂，
// 就会出现「录题页公式正常、笔记页公式坏掉」这种只在某个页面复现的怪事，
// 而它不报任何错。所以这里做两件事：
//   ① **行为对比**（不是比源码文本）：同一组输入喂给两份实现，输出必须逐字相同
//   ② 用项目自带的 marked 真跑一遍，确认公式/表格/列表/代码块/换行都没被吃掉
//
// 跑法：node math_render_check.js   （退出码 0 = 全过）
const fs = require('fs');
const path = require('path');

const BASE = __dirname;
let pass = 0, fail = 0;
const fails = [];

function check(name, ok, detail) {
  if (ok) {
    pass++;
    console.log('  [OK ] ' + name);
  } else {
    fail++;
    fails.push(name);
    console.log('  [FAIL] ' + name + (detail !== undefined ? '   <- ' + detail : ''));
  }
}

function read(p) {
  return fs.readFileSync(path.join(BASE, p), 'utf8');
}

/** 从源码里切出「_PH_RE … restoreMath 结束」这一段，去掉 export 后执行。
 *
 *  用起止锚点而不是逐个函数抓：这段代码是自包含的（只依赖自己定义的两个常量），
 *  整段 eval 最不容易因为注释/换行差异而抓错。 */
function loadImpl(src, endMark, label) {
  const i = src.indexOf('const _PH_RE');
  const j = src.indexOf(endMark, i);
  if (i < 0 || j < 0) throw new Error(label + '：找不到 protectMath 那一段（起止锚点失效）');
  const code = src.slice(i, j).replace(/\bexport\s+/g, '');
  // eslint-disable-next-line no-new-func
  const f = new Function(code + '\nreturn { protectMath, restoreMath };');
  return f();
}

const spa = loadImpl(read('src/mathRender.js'), 'const DELIMITERS', 'mathRender.js');
const entry = loadImpl(read('entry.html'), 'function renderRich', 'entry.html');

// ---------------------------------------------------------------- ① 两份实现一致
console.log('\n==== ① entry.html 与 src/mathRender.js 的公式保护行为一致 ====');
const SAMPLES = [
  '设 $a>0$，求 $\\frac{a}{b}$',
  '面积 $S=2*3*4$ 平方厘米',
  '增长率 $5\\%$',
  '集合 $\\{1,2,3\\}$',
  '$$\n\\begin{aligned} x&=1 \\\\ y&=2 \\end{aligned}\n$$',
  'a \\(x+1\\) b 与 \\[x^2\\]',
  '代码里 `$a*b$` 不该被当公式',
  '```\n$a*b$ 在代码块里\n```',
  '前缀 $a_1$ 中间 $$b_2$$ 结尾 $c$',
  '公式不闭合的 $abc 保持原样',
  '',
  null,
];
let same = 0;
for (const s of SAMPLES) {
  const x = JSON.stringify(spa.protectMath(s));
  const y = JSON.stringify(entry.protectMath(s));
  if (x === y) same++;
  else check('两份实现一致：' + JSON.stringify(String(s).slice(0, 24)), false, x + ' vs ' + y);
}
check(`两份 protectMath 行为一致（${same}/${SAMPLES.length} 组输入逐字相同）`, same === SAMPLES.length);

// 占位符与还原也必须一致
const p0 = spa.protectMath('设 $a>0$');
check('两份 restoreMath 行为一致',
      spa.restoreMath('<p>' + p0.md + '</p>', p0.store) ===
      entry.restoreMath('<p>' + entry.protectMath('设 $a>0$').md + '</p>',
                        entry.protectMath('设 $a>0$').store),
      spa.restoreMath('<p>' + p0.md + '</p>', p0.store));

// ---------------------------------------------------------------- ② 真实 marked 端到端
console.log('\n==== ② 真实 marked 渲染：公式一个字符都不该被吃掉 ====');
const marked = require(path.join(BASE, 'vendor/marked.min.js'));

/** 完整复刻 renderRich 的管线顺序：保护 → marked → （跳过消毒）→ 还原。 */
function render(src) {
  const p = spa.protectMath(src);
  return spa.restoreMath(marked.parse(p.md, { breaks: true, gfm: true }), p.store);
}

// ① 乘号：改之前会变成 $S=2<em>3</em>4$（公式被切碎，一个都不渲染）
const mul = render('面积 $S=2*3*4$ 平方厘米');
check('乘号 * 不再变斜体（公式完整保留）',
      mul.indexOf('$S=2*3*4$') >= 0 && mul.indexOf('<em>') < 0, mul);

// ② \%
const pct = render('增长率 $5\\%$');
check('公式里的 \\% 没被吃掉（反斜杠还在）',
      pct.indexOf('$5\\%$') >= 0, pct);

// ③ \{ \}
const brace = render('集合 $\\{1,2,3\\}$');
check('公式里的 \\{ \\} 没被吃掉', brace.indexOf('$\\{1,2,3\\}$') >= 0, brace);

// ④ 双星号
const dbl = render('幂 $a**2**$');
check('公式里的 ** 不再变粗体',
      dbl.indexOf('$a**2**$') >= 0 && dbl.indexOf('<strong>') < 0, dbl);

// ⑤ 删除线
const del = render('$a~~b~~c$');
check('公式里的 ~~ 不再变删除线',
      del.indexOf('$a~~b~~c$') >= 0 && del.indexOf('<del>') < 0, del);

// ⑥ aligned 的 & 与 \\
const ali = render('$$\\begin{aligned} x&=1 \\\\ y&=2 \\end{aligned}$$');
check('aligned 的对齐点 & 保留成 &amp;（浏览器解码回 &）',
      ali.indexOf('&amp;') >= 0, ali);
check('aligned 的换行 \\\\ 是两条反斜杠（不是被吃成一条）',
      ali.indexOf('\\\\ y') >= 0, ali);

// ⑦ 公式里的小于号不能被当标签
const lt = render('当 $a<b$ 时');
check('公式里的 < 被转义（不会变成 HTML 标签）',
      lt.indexOf('$a&lt;b$') >= 0, lt);

// ⑧ 表格（有分隔行 —— 这是合法 GFM 表格的硬要求）
const tbl = render('| 棱数 | 面数 |\n|---|---|\n| 6 | 5 |');
check('合法表格（带分隔行）渲染成 <table>', tbl.indexOf('<table>') >= 0, tbl);

// ⑨ 列表：有序 / 无序，单换行也要成列表
const ol = render('1. 先求导数\n2. 令导数为 0\n3. 解得 $x=1$');
check('有序列表渲染成 <ol>', ol.indexOf('<ol>') >= 0, ol);
const ul = render('- 第一点\n- 第二点');
check('无序列表渲染成 <ul>', ul.indexOf('<ul>') >= 0, ul);

// ⑩ 代码块
const code = render('```\nx^2+y^2=r^2\n```');
check('代码块渲染成 <pre><code>', code.indexOf('<pre><code>') >= 0, code);

// ⑪ 换行：单换行 → <br>（breaks:true）；空行 → 两个段落
check('单换行渲染成 <br>', render('第一段\n第二段').indexOf('<br>') >= 0);
check('空行渲染成两个 <p>',
      (render('第一段\n\n第二段').match(/<p>/g) || []).length === 2);

// ⑫ 代码块里的公式不该被保护逻辑动（它是代码，不是公式）
const inCode = render('```\n$a*b$\n```');
check('代码块里的 $a*b$ 原样保留', inCode.indexOf('$a*b$') >= 0, inCode);

// ⑬ 表格缺分隔行（模型常见错法）—— 这里只**记录行为**：结构确实会丢，
//    所以要靠 prompt 约束（vision.py 的 answer 规则），不靠渲染兜
const badTbl = render('| 棱数 | 面数 |\n| 6 | 5 |');
check('缺分隔行的表格确实渲染不成表格（这是要 prompt 约束的，不是管线能救的）',
      badTbl.indexOf('<table>') < 0, badTbl);

// ⑭ 占位符不能漏在最终 HTML 里
const leak = render('$a$ 与 $b$ 与 $$c$$');
check('最终 HTML 里没有漏出的占位符', leak.indexOf('\uE000') < 0, leak);

// ---------------------------------------------------------------- ③ 源码卫生
console.log('\n==== ③ 源码卫生 ====');
const spaSrc = read('src/mathRender.js');
const entrySrc = read('entry.html');
check('mathRender.js 导出 protectMath / restoreMath',
      /export\s+function\s+protectMath/.test(spaSrc)
      && /export\s+function\s+restoreMath/.test(spaSrc));
check('renderRichEl 里 marked.parse 的入参是「公式已摘出」的那份（p.md）',
      /marked\.parse\(\s*p\.md/.test(spaSrc), '');
check('还原在消毒之后（order: sanitize → restoreMath）',
      /restoreMath\(\s*window\.DOMPurify\s*\?\s*window\.DOMPurify\.sanitize\(raw\)/.test(spaSrc), '');
check('entry.html 的 renderRich 也走同一条顺序',
      /marked\.parse\(\s*p\.md/.test(entrySrc)
      && /restoreMath\(\s*window\.DOMPurify/.test(entrySrc), '');
check('entry.html 里没有残留「直接用 normalizeMath 喂 marked」的旧写法',
      !/marked\.parse\(\s*normalizeMath\(/.test(entrySrc), '');

// ---------------------------------------------------------------- 汇总
console.log('\n' + '='.repeat(50));
if (fail) {
  console.log(`失败 ${fail} 项 / 通过 ${pass} 项`);
  fails.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log(`全部通过（${pass} 项）`);
process.exit(0);
