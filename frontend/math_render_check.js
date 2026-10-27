// 渲染管线自检：公式保护覆盖 **4 个页面** + 真实 marked 端到端。
//
// 为什么需要它：项目里有 4 份「Markdown → HTML」的实现，它们**没法共用**：
//   · `src/mathRender.js`  → SPA（可 import）
//   · `entry.html`         → 录题页（零构建单文件，只能复制）
//   · `batch.html`         → 批量导入页（同上）
//   · `workbench.html`     → 工作台（同上，且是 Vue 方法，返回字符串）
// 四份一漂，就会出现「录题页公式正常、批量页公式坏掉」这种只在某个页面复现的
// 怪事，而它**不报任何错** —— 只是公式被 Markdown 吃掉、KaTeX 一个都不渲染。
//
// 所以这里做三件事，全部是**行为**层面（不比源码文本，四份结构本来就不一样）：
//   ① 同一组输入（12 组）喂给四份实现，输出必须逐字相同，且公式原文必须还在
//   ② 消毒必须发生在「放回公式」之前（拿一个假 DOMPurify 把入参录下来当证据）
//   ③ 用项目自带的 vendor/marked.min.js 真跑一遍，确认表格/列表/代码块没被吃掉
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
    console.log('  [FAIL] ' + name + (detail !== undefined && detail !== '' ? '   <- ' + detail : ''));
  }
}

function read(p) {
  return fs.readFileSync(path.join(BASE, p), 'utf8');
}

function clip(s, n) {
  s = String(s === undefined || s === null ? '' : s);
  const lim = n || 130;
  return s.length > lim ? s.slice(0, lim) + '…' : s;
}

/** 只剥 HTML 注释。
 *
 *  ⚠️ 千万别顺手用「剥块注释」那条正则：`entry.html` 里有 `accept="image/*"`，
 *     那个正则会从 `image/` 后面那个星号一路吃到下一个 `星号斜杠`，
 *     把半张页面当注释删掉 —— 本项目踩过这个坑，结论是**HTML 里的 JS 只剥 HTML 注释**。
 *     行首的 `//` / `*` / 注释开头那种行按行过滤就够了（它们不会误伤字符串）。
 */
function htmlSafe(src) {
  return src.replace(/<!--[\s\S]*?-->/g, '');
}

function codeOf(src) {
  return htmlSafe(src).split('\n').filter((l) => {
    const t = l.trim();
    return !(t.startsWith('//') || t.startsWith('*') || t.startsWith('/*'));
  }).join('\n');
}

/** 从 header 那一行开始，切到**同缩进**的闭合括号那一行为止。
 *
 *  用缩进匹配而不是数花括号：这几份代码里都有 `split(/(`…`)/)` 这种
 *  带反引号的**正则字面量**，手写花括号计数器会被它带偏（数到一半以为字符串没闭）。
 *  缩进匹配是纯行扫描，不看引号，反而稳。 */
function sliceDecl(src, headerRe, closeRe) {
  const m = headerRe.exec(src);
  if (!m) return null;
  const indent = (m[0].match(/^[ \t]*/) || [''])[0];
  const lines = src.slice(m.index).split('\n');
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if ((line.match(/^[ \t]*/) || [''])[0] !== indent) continue;
    if (closeRe.test(line.trim())) return lines.slice(0, i + 1).join('\n');
  }
  return null;
}

const FN_END = /^\}\s*[,;]?$/;

// ---------------------------------------------------------------- 页面清单
const PAGES_SPEC = [
  {
    file: 'src/mathRender.js', label: 'mathRender.js（SPA）',
    renderRe: /^[ \t]*(?:export\s+)?function\s+renderRichEl\s*\(/m,
  },
  {
    file: 'entry.html', label: 'entry.html（录题页）',
    renderRe: /^[ \t]*(?:export\s+)?function\s+renderRich\s*\(/m,
  },
  {
    file: 'batch.html', label: 'batch.html（批量导入）',
    renderRe: /^[ \t]*(?:export\s+)?function\s+renderRich\s*\(/m,
  },
  {
    file: 'workbench.html', label: 'workbench.html（工作台）',
    // 工作台那份是 Vue 方法：`    renderRich(text) {`，抽取后补一个 `function ` 头
    renderRe: /^[ \t]*renderRich\s*\(\s*text\s*\)\s*\{/m,
    method: true,
  },
];

/** 把一页里「公式保护 + renderRich」那几段拼成一段可执行代码。
 *
 *  为什么按名字逐段抓、而不是切一大块：四份文件的上下文差别很大（SPA 有 export、
 *  工作台是对象方法），逐段抓再拼，缺哪段一眼就能从 `hasProtect` 看出来。 */
function buildUnit(spec) {
  const src = read(spec.file);
  const c = htmlSafe(src);
  const parts = [];

  const norm = sliceDecl(c, /^[ \t]*(?:export\s+)?function\s+normalizeMath\s*\(/m, FN_END);
  if (norm) parts.push(norm);

  const phRe = (c.match(/^[ \t]*const\s+_PH_RE\b.*$/m) || [null])[0];
  const phFn = (c.match(/^[ \t]*const\s+_ph\s*=.*$/m) || [null])[0];
  const prot = sliceDecl(c, /^[ \t]*(?:export\s+)?function\s+protectMath\s*\(/m, FN_END);
  const rest = sliceDecl(c, /^[ \t]*(?:export\s+)?function\s+restoreMath\s*\(/m, FN_END);
  const hasProtect = !!(phRe && phFn && prot && rest);
  if (hasProtect) parts.push([phRe, phFn, prot, rest].join('\n'));

  const delim = sliceDecl(c, /^[ \t]*const\s+DELIMITERS\s*=/m, /^\];$/);
  if (delim) parts.push(delim);

  let render = sliceDecl(c, spec.renderRe, FN_END);
  if (!render) return { error: '找不到 renderRich 那一段（起止锚点失效）', src, hasProtect };

  if (spec.method) {
    render = render.replace(/^([ \t]*)(?:async\s+)?renderRich\s*\(/m, '$1function renderRich(');
    // 方法后面跟着逗号（`    },` 然后是下一个方法）—— 变成函数声明后这个逗号是语法错
    render = render.replace(/(\})\s*[,;]\s*$/, '$1');
  }
  parts.push(render);

  return { code: parts.join('\n\n').replace(/\bexport\s+/g, ''), src, hasProtect };
}

const marked = require(path.join(BASE, 'vendor/marked.min.js'));

const FAKE_DOC = {
  createElement() {
    const el = { _h: '', textContent: '' };
    Object.defineProperty(el, 'innerHTML', {
      get() { return this._h; },
      set(v) { this._h = v; },
    });
    return el;
  },
};

function makePage(spec) {
  const built = buildUnit(spec);
  const page = { label: spec.label, ...built, factory: null };
  if (built.error) return page;
  try {
    // 这个 factory 就是「一份页面的渲染环境」：window / document / renderMathInElement 由调用方注入
    // eslint-disable-next-line no-new-func
    page.factory = new Function('window', 'document', 'renderMathInElement',
      built.code
      + '\nreturn typeof renderRichEl === "function" ? renderRichEl'
      + ' : (typeof renderRich === "function" ? renderRich : null);');
  } catch (e) {
    page.error = '这段代码编译不过：' + e.message;
  }
  return page;
}

/** 跑一页的 renderRich。返回 { html, sanitized }：
 *  `sanitized` 是假 DOMPurify 收到的入参 —— 还原发生在消毒之前还是之后，看它就够了。 */
function runPage(page, text) {
  if (!page.factory) return { html: '<无法渲染：' + (page.error || '抽取失败') + '>', sanitized: [] };
  const sanitized = [];
  const win = {
    marked,
    // 假的消毒器：原样返回，但把入参录下来。
    DOMPurify: { sanitize(h) { sanitized.push(String(h)); return h; } },
    renderMathInElement: null,     // 关掉 KaTeX：这一步是浏览器 DOM 活，测的是它之前的部分
  };
  const el = FAKE_DOC.createElement();
  try {
    const fn = page.factory(win, FAKE_DOC, null);
    if (typeof fn !== 'function') throw new Error('这一页里没抽到 renderRich');
    // 2 个形参 = (el, text) 写进 el.innerHTML；1 个形参 = (text) 直接返回字符串
    const ret = (fn.length >= 2) ? fn(el, text) : fn(text);
    const html = (fn.length >= 2) ? el.innerHTML : (typeof ret === 'string' ? ret : el.innerHTML);
    return { html: html || '', sanitized };
  } catch (e) {
    return { html: '<渲染抛错：' + e.message + '>', sanitized };
  }
}

const PAGES = PAGES_SPEC.map(makePage);

const byLabel = {};
PAGES.forEach((p) => { byLabel[p.label] = p; });
const render = (label, text) => runPage(byLabel[label], text);

// ---------------------------------------------------------------- ⓪ 四份都得有这东西
console.log('\n==== ⓪ 四份实现都有公式保护（缺一份，那一页的公式就是坏的） ====');
for (const p of PAGES) {
  check(p.label + ' 有 protectMath / restoreMath 这一套',
    !!p.hasProtect && !p.error && !!p.factory,
    p.error || (p.hasProtect ? '' : '没有 _PH_RE / protectMath / restoreMath —— 渲染时公式会被 marked 吃掉'));
}

// ---------------------------------------------------------------- ① 12 组输入 × 4 份
console.log('\n==== ① 同一组输入喂给四份实现：公式原文必须还在，且四份逐字一致 ====');
const SAMPLES = [
  { name: '\\\\% 百分号', text: '$5\\%$', must: ['$5\\%$'] },
  { name: '\\\\{ \\\\} 花括号', text: '$\\{1,2\\}$', must: ['$\\{1,2\\}$'] },
  { name: '乘号 *', text: '面积 $S=2*3*4$ 平方厘米', must: ['$S=2*3*4$'], mustNot: ['<em>'] },
  { name: '双星号 **', text: '幂 $a**2**$', must: ['$a**2**$'], mustNot: ['<strong>'] },
  { name: '删除线 ~~', text: '$a~~b~~c$', must: ['$a~~b~~c$'], mustNot: ['<del>'] },
  { name: 'aligned 的 & 与 \\\\', text: '$$x&=1 \\\\ y&=2$$', must: ['&amp;', '\\\\ y'] },
  { name: '小于号 <', text: '当 $a<b$ 时', must: ['$a&lt;b$'] },
  { name: '行内代码里的 $a*b$ 不动', text: '代码里 `$a*b$` 不该被当公式', must: ['<code>$a*b$</code>'] },
  { name: '代码块里的 $a*b$ 不动', text: '```\n$a*b$\n```', must: ['<pre><code>$a*b$'] },
  { name: 'display $$…$$', text: '前 $$\\frac{a}{b}$$ 后', must: ['$$\\frac{a}{b}$$'] },
  { name: '\\\\(…\\\\) 归一', text: '\\(x+1\\)', must: ['$x+1$'] },
  { name: '\\\\[…\\\\] 归一', text: '\\[x^2\\]', must: ['$$x^2$$'] },
  // 第 13 组：**单换行**。marked 的 `breaks` 选项开不开，输出差一个 <br>。
  // 这一组是给「四份逐字一致」兜底的 —— entry/batch 传了 `breaks:true`，
  // 工作台原本只写 `marked.parse(md)`（不开 breaks），同一段文字在那边不会换行。
  { name: '单换行（breaks）', text: '第一行\n第二行 $a*b$', must: ['<br>'] },
];

let sameCount = 0;
const equalityFails = [];
for (const s of SAMPLES) {
  const outs = PAGES.map((p) => ({ label: p.label, ...runPage(p, s.text) }));

  for (const o of outs) {
    const missing = (s.must || []).filter((m) => o.html.indexOf(m) < 0);
    const leaked = (s.mustNot || []).filter((m) => o.html.indexOf(m) >= 0);
    check(`${o.label}：${s.name} 公式原文没被吃掉`,
      missing.length === 0 && leaked.length === 0,
      [missing.length ? '缺 ' + JSON.stringify(missing) : '', leaked.length ? '多出 ' + leaked.join('/') : '',
        '实际=' + clip(o.html)].filter(Boolean).join('；'));
  }

  const uniq = new Set(outs.map((o) => o.html));
  const first = outs[0];
  const diff = outs.find((o) => o.html !== first.html);
  check(`${s.name}：四份实现输出逐字一致`, uniq.size === 1,
    diff ? `${diff.label} 与 ${first.label} 不同：${clip(diff.html, 90)} vs ${clip(first.html, 90)}` : '');
  if (uniq.size === 1) sameCount++;
}
check(`${SAMPLES.length} 组输入里，四份实现全部一致的（${sameCount}/${SAMPLES.length}）`,
  sameCount === SAMPLES.length);

// ---------------------------------------------------------------- ② 消毒 → 还原 的顺序
console.log('\n==== ② 顺序：消毒时必须还是占位符，还原必须是最后一步 ====');
for (const p of PAGES) {
  const r = runPage(p, '面积 $S=2*3*4$ 平方厘米，增长率 $5\\%$');
  check(`${p.label}：消毒之前公式已摘成占位符（= 还原在消毒之后）`,
    r.sanitized.length === 1 && r.sanitized[0].indexOf('\uE000') >= 0,
    r.sanitized.length ? '消毒器拿到的是：' + clip(r.sanitized[0]) : '消毒器一次都没被调用');
  check(`${p.label}：最终 HTML 里没有漏出来的占位符`,
    r.html.indexOf('\uE000') < 0, clip(r.html));
}

// ---------------------------------------------------------------- ③ 真实 marked 端到端
console.log('\n==== ③ 真实 marked 渲染：Markdown 结构一个都不该丢 ====');
const SPA = 'mathRender.js（SPA）';
const spaOut = (t) => render(SPA, t).html;

// ① 乘号：改之前会变成 $S=2<em>3</em>4$（公式被切碎，一个都不渲染）
const mul = spaOut('面积 $S=2*3*4$ 平方厘米');
check('乘号 * 不再变斜体（公式完整保留）',
  mul.indexOf('$S=2*3*4$') >= 0 && mul.indexOf('<em>') < 0, clip(mul));

// ② \%
const pct = spaOut('增长率 $5\\%$');
check('公式里的 \\% 没被吃掉（反斜杠还在）', pct.indexOf('$5\\%$') >= 0, clip(pct));

// ③ \{ \}
const brace = spaOut('集合 $\\{1,2,3\\}$');
check('公式里的 \\{ \\} 没被吃掉', brace.indexOf('$\\{1,2,3\\}$') >= 0, clip(brace));

// ④ 双星号
const dbl = spaOut('幂 $a**2**$');
check('公式里的 ** 不再变粗体', dbl.indexOf('$a**2**$') >= 0 && dbl.indexOf('<strong>') < 0, clip(dbl));

// ⑤ 删除线
const del = spaOut('$a~~b~~c$');
check('公式里的 ~~ 不再变删除线', del.indexOf('$a~~b~~c$') >= 0 && del.indexOf('<del>') < 0, clip(del));

// ⑥ aligned 的 & 与 \\
const ali = spaOut('$$\\begin{aligned} x&=1 \\\\ y&=2 \\end{aligned}$$');
check('aligned 的对齐点 & 保留成 &amp;（浏览器解码回 &）', ali.indexOf('&amp;') >= 0, clip(ali));
check('aligned 的换行 \\\\ 是两条反斜杠（不是被吃成一条）', ali.indexOf('\\\\ y') >= 0, clip(ali));

// ⑦ 公式里的小于号不能被当标签
const lt = spaOut('当 $a<b$ 时');
check('公式里的 < 被转义（不会变成 HTML 标签）', lt.indexOf('$a&lt;b$') >= 0, clip(lt));

// ⑧ 表格（有分隔行 —— 这是合法 GFM 表格的硬要求）
const tbl = spaOut('| 棱数 | 面数 |\n|---|---|\n| 6 | 5 |');
check('合法表格（带分隔行）渲染成 <table>', tbl.indexOf('<table>') >= 0, clip(tbl));

// ⑨ 列表
check('有序列表渲染成 <ol>', spaOut('1. 先求导数\n2. 令导数为 0\n3. 解得 $x=1$').indexOf('<ol>') >= 0);
check('无序列表渲染成 <ul>', spaOut('- 第一点\n- 第二点').indexOf('<ul>') >= 0);

// ⑩ 代码块
check('代码块渲染成 <pre><code>', spaOut('```\nx^2+y^2=r^2\n```').indexOf('<pre><code>') >= 0);

// ⑪ 换行
check('单换行渲染成 <br>', spaOut('第一段\n第二段').indexOf('<br>') >= 0);
check('空行渲染成两个 <p>', (spaOut('第一段\n\n第二段').match(/<p>/g) || []).length === 2);

// ⑫ 代码块里的公式不该被保护逻辑动（它是代码，不是公式）
check('代码块里的 $a*b$ 原样保留', spaOut('```\n$a*b$\n```').indexOf('$a*b$') >= 0);

// ⑬ 表格缺分隔行（模型常见错法）—— 这里只**记录行为**：结构确实会丢，
//    所以要靠 prompt 约束（vision.py 的 answer 规则），不靠渲染兜
const badTbl = spaOut('| 棱数 | 面数 |\n| 6 | 5 |');
check('缺分隔行的表格确实渲染不成表格（这是要 prompt 约束的，不是管线能救的）',
  badTbl.indexOf('<table>') < 0, clip(badTbl));

// ---------------------------------------------------------------- ④ 源码卫生
console.log('\n==== ④ 源码卫生（反面断言看代码不看注释） ====');
const spaSrc = codeOf(read('src/mathRender.js'));
const entrySrc = codeOf(read('entry.html'));
const batchSrc = codeOf(read('batch.html'));
const wbSrc = codeOf(read('workbench.html'));

check('mathRender.js 导出 protectMath / restoreMath',
  /export\s+function\s+protectMath/.test(spaSrc)
  && /export\s+function\s+restoreMath/.test(spaSrc));
check('mathRender.js 的 renderRichEl 把「公式已摘出」的那份喂给 marked',
  /marked\.parse\(\s*p\.md/.test(spaSrc));
check('mathRender.js 的还原在消毒之后（sanitize → restoreMath）',
  /restoreMath\(\s*window\.DOMPurify\s*\?\s*window\.DOMPurify\.sanitize\(raw\)/.test(spaSrc));

check('entry.html 的 renderRich 把 p.md 喂给 marked',
  /marked\.parse\(\s*p\.md/.test(entrySrc));
check('entry.html 的还原在消毒之后',
  /restoreMath\(\s*window\.DOMPurify/.test(entrySrc));
check('entry.html 里没有残留「直接用 normalizeMath 喂 marked」的旧写法',
  !/marked\.parse\(\s*normalizeMath\(/.test(entrySrc));

check('batch.html 的 renderRich 把 p.md 喂给 marked',
  /marked\.parse\(\s*p\.md/.test(batchSrc));
check('batch.html 里没有残留「直接用 normalizeMath 喂 marked」的旧写法',
  !/marked\.parse\(\s*normalizeMath\(/.test(batchSrc));
check('batch.html 的 renderRich 里调了 protectMath',
  /protectMath\(/.test(batchSrc));

check('workbench.html 的 renderRich 把 p.md 喂给 marked',
  /marked\.parse\(\s*p\.md/.test(wbSrc));
check('workbench.html 里没有残留「直接 marked.parse(md)」的旧写法',
  !/marked\.parse\(\s*md\s*\)/.test(wbSrc));
check('workbench.html 的 renderRich 里调了 protectMath',
  /protectMath\(/.test(wbSrc));

// ---------------------------------------------------------------- 汇总
console.log('\n' + '='.repeat(50));
if (fail) {
  console.log(`失败 ${fail} 项 / 通过 ${pass} 项`);
  fails.forEach((f) => console.log('  - ' + f));
  process.exit(1);
}
console.log(`全部通过（${pass} 项）`);
process.exit(0);
