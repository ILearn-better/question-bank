// 文本框的 LaTeX 渲染 —— `src/gradeLabelMath.js` 是纯函数（不碰 DOM），用 node 直接量。
//
// 为什么值得单独测：它产出的 HTML 直接进 `v-html`。这地方有两个「测试反而更容易骗人」的坑：
//   ① 断言「渲染出来有 <span class=katex>」—— 桩永远成功，证明不了公式内容对不对；
//   ② 断言「没有 <script」—— 只是没测到注入，不等于转义对了。
// 所以这里的判据是**逐字符比对转义结果**，外加「哪些片段喂给了 KaTeX」的记账。
const path = require('path');
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

/** 记账用的假 katex：把每次调用都记下来，输出可预测的标记。 */
function fakeKatex() {
  const calls = [];
  return {
    calls,
    renderToString(tex, opts) {
      calls.push({ tex, display: !!(opts && opts.displayMode) });
      return '<span class="katex">K:' + tex + '</span>';
    },
  };
}

(async () => {
  const m = await import(pathToFileURL(path.join(__dirname, 'src', 'gradeLabelMath.js')).href);

  console.log('\n— 1. splitMath：定界符识别 —');
  {
    const seg = m.splitMath('前 $a+b$ 后');
    check('行内 $…$ 切出三段（文字 / 公式 / 文字）',
      seg.length === 3 && seg[0].t === 'text' && seg[1].t === 'math' && seg[2].t === 'text', seg);
    check('公式体是 a+b，display=false', seg[1].tex === 'a+b' && seg[1].display === false, seg[1]);

    const d = m.splitMath('$$x=1$$');
    check('$$…$$ → display=true', d.length === 1 && d[0].display === true && d[0].tex === 'x=1', d);

    const p = m.splitMath('\\(y\\) 与 \\[z\\]');
    check('\\(…\\) 认作行内、\\[…\\] 认作独立公式',
      p[0].t === 'math' && p[0].display === false && p[0].tex === 'y'
      && p[2].t === 'math' && p[2].display === true && p[2].tex === 'z', p);

    /* ⚠️ 这条是当年真踩过的顺序坑：$$ 必须先于 $ 匹配，
       否则 `$$x$$` 被拆成「空公式 + x + 空公式」，公式直接消失。 */
    check('★ $$ 先于 $ 匹配（不会把 $$x$$ 拆成两个空公式）',
      m.splitMath('$$x$$').length === 1, m.splitMath('$$x$$'));

    check('没配对的单个 $ 当普通文字（写「花了 $5」不该整段消失）',
      m.splitMath('花了 $5').length === 1 && m.splitMath('花了 $5')[0].t === 'text',
      m.splitMath('花了 $5'));

    check('行内 $ 不跨行（$a⏎b$ 当文字，避免吞掉一大段）',
      m.splitMath('$a\nb$').every((s) => s.t === 'text'), m.splitMath('$a\nb$'));

    check('零宽输入不死循环也不抛', (() => {
      const a = m.splitMath('$$');
      const b = m.splitMath('$');
      const c = m.splitMath('');
      return a.length > 0 && b.length > 0 && c.length === 0;
    })(), [m.splitMath('$$'), m.splitMath('$')]);
  }

  console.log('\n— 2. 转义：每一个用户字符都必须过 escapeHtml —');
  {
    const k = fakeKatex();
    const html = m.renderLabelHtml('<img src=x onerror=alert(1)>', k);
    check('★ 纯文本里的标签被转义（不会变成真 <img>）',
      !/<img/i.test(html) && html.indexOf('&lt;img') === 0, html);
    check('★ 引号外的尖括号一个不留', (html.match(/</g) || []).length === 0, html);

    const amp = m.renderLabelHtml('a & b', k);
    check('& 转成 &amp;（否则浏览器会当实体开头）', amp === 'a &amp; b', amp);

    const gt = m.renderLabelHtml('x > y', k);
    check('> 转成 &gt;', gt === 'x &gt; y', gt);

    check('escapeHtml 三者都管，且 null/undefined 不炸',
      m.escapeHtml('<&>') === '&lt;&amp;&gt;'
      && m.escapeHtml(null) === '' && m.escapeHtml(undefined) === '');
  }

  console.log('\n— 3. 渲染：喂给 KaTeX 的内容与 display 标记 —');
  {
    const k = fakeKatex();
    m.renderLabelHtml('解 $\\frac{1}{2}$ 就好', k);
    check('行内公式喂给 katex 的 tex 是原样（不经过任何 Markdown）',
      k.calls.length === 1 && k.calls[0].tex === '\\frac{1}{2}', k.calls);
    check('行内公式 displayMode=false', k.calls[0].display === false, k.calls[0]);

    const k2 = fakeKatex();
    m.renderLabelHtml('$$x^2$$', k2);
    check('独立公式 displayMode=true', k2.calls[0] && k2.calls[0].display === true, k2.calls);

    const k3 = fakeKatex();
    m.renderLabelHtml('无公式的一句话', k3);
    check('没有公式时**不调用** katex（少一次无谓渲染）', k3.calls.length === 0, k3.calls);

    /* ⚠️ 反面：不能拿 markdown 渲染批注。`a_b` / `1*2*3` 过 marked 会变成斜体，
       批注不是文档，别给它排版的权力 —— 这条钉住「公式之外的内容一个字都不加工」。 */
    const plain = m.renderLabelHtml('a_b 与 1*2*3', fakeKatex());
    check('★ 非公式部分不做 Markdown（a_b / * 原样保留）',
      plain === 'a_b 与 1*2*3', plain);
  }

  console.log('\n— 4. 退化：没有库 / 库抛异常 —');
  {
    const raw = m.renderLabelHtml('前 $x^2$ 后', null);
    check('没有 katex → 公式**原文**保留（定界符一起给，让人看得出没渲染）',
      raw.indexOf('$x^2$') >= 0, raw);
    check('没有 katex → 文字部分照样转义',
      m.renderLabelHtml('<b>$x$</b>', null).indexOf('&lt;b&gt;') === 0,
      m.renderLabelHtml('<b>$x$</b>', null));

    const boom = { renderToString() { throw new Error('ParseError'); } };
    let threw = false;
    let out = '';
    try { out = m.renderLabelHtml('$\\bad$', boom); } catch (e) { threw = true; }
    check('★ 公式渲染抛异常时不冒泡（一条写坏的公式不该让整个文本框白掉）', !threw, threw);
    check('抛异常时退回原文', out.indexOf('$\\bad$') >= 0, out);
  }

  console.log('\n— 5. 保真：换行与顺序 —');
  {
    const k = fakeKatex();
    const out = m.renderLabelHtml('第一行\n第二行', k);
    check('批注里的换行原样保留（外层 white-space:pre-wrap 负责显示）',
      out === '第一行\n第二行', out);

    const k2 = fakeKatex();
    const mix = m.renderLabelHtml('A $b$ C $$d$$ E', k2);
    const order = [];
    let i = 0;
    while (i < mix.length) {
      if (mix.startsWith('<span', i)) { order.push('M'); i = mix.indexOf('</span>', i) + 7; }
      else { order.push('T'); i += 1; }
    }
    check('文字与公式的先后顺序没有被打乱',
      order.join('').replace(/T+/g, 'T') === 'TMTMT', order.join('').replace(/T+/g, 'T'));
    check('公式段落包了 .gm-math（CSS 靠它把 white-space 改回 normal）',
      (mix.match(/class="gm-math"/g) || []).length === 2, mix);
  }

  console.log('');
  console.log(`共 ${total} 项，失败 ${fails.length} 项`);
  if (fails.length) process.exit(1);
  console.log('全部通过 ✅');
})();
