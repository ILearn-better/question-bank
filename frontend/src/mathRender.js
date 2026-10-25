/**
 * Markdown + LaTeX 的渲染管线 —— **工作台（SPA）这一侧的唯一出处**。
 *
 * 为什么值得单独成文件：出卷页要把题干「按真公式」渲染出来（老师看的是卷子，
 * 不是 `$\\frac{a}{b}$` 这种原文），而笔记页早就有一模一样的一套。
 * 各写一份的话，「normalizeMath 必须排在 marked 之前」这类坑迟早只修一边。
 *
 * ⚠️ `frontend/entry.html`（零构建单文件页）里**另有一份**同名实现，它不走 ES 模块，
 *    没法 import 这里 —— 不是漏了，是结构决定的。改这边时记得去看一眼那份。
 * ⚠️ 后端导出（`services/paper_export.py` / `notes_export.py`）也有等价的
 *    「LaTeX 就地渲染」，不然会出现「页面能渲染、导出的 Word 里还是原文」。
 *
 * 版本（升级时按这个换文件）：marked 12.0.2 · dompurify 3.1.6 · katex 0.16.9
 * katex.min.css 里的 font url 是相对路径 fonts/xxx.woff2，
 * 因此字体必须放在 /vendor/katex/fonts/ 下（已就位，20 个 woff2）。
 * 全部走本地 vendor，不碰外网：实测这台机器冷启动拉 unpkg 要 29s，还常超时。
 */
export const LIBS = {
  marked: '/vendor/marked.min.js',
  purify: '/vendor/purify.min.js',
  katex: '/vendor/katex/katex.min.js',
  autoRender: '/vendor/katex/auto-render.min.js',
  katexCss: '/vendor/katex/katex.min.css',
};

let libsPromise = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    s.onload = () => resolve(src);
    s.onerror = () => reject(new Error('加载失败 ' + src));
    document.head.appendChild(s);
  });
}

/** 只加载一次（结果缓存在模块作用域，切页面来回也不重复拉）。 */
export function ensureLibs() {
  if (libsPromise) return libsPromise;
  libsPromise = (async () => {
    if (!document.querySelector('link[data-katex]')) {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = LIBS.katexCss;
      l.dataset.katex = '1';
      document.head.appendChild(l);
    }
    await Promise.all([
      loadScript(LIBS.marked),
      loadScript(LIBS.purify),
      loadScript(LIBS.katex),
    ]);
    // ⚠️ auto-render **必须等 katex**：它一加载就把 window.katex 抓进闭包，
    //    并行加载时它先到就抓了个 undefined，之后一渲染公式就报
    //    "Cannot read properties of undefined (reading 'ParseError')"。
    //    （踩过：四个脚本并行时公式静默不渲染，预览里一直是 $…$ 原文。）
    await loadScript(LIBS.autoRender);
  })();
  return libsPromise;
}

/** 让下次 ensureLibs() 真的重新去拉。
 *
 *  libsPromise 是模块级缓存，一旦 reject 就会**一直** reject —— 不重置的话，
 *  页面里切来切去永远好不了，只能整页刷新（用户看到的就是「公式怎么都不渲染」）。
 *  所以调用方在失败后必须调一下它，把「重试」变成真的能重试。 */
export function resetLibs() {
  libsPromise = null;
}

/** 把 LaTeX 惯用的 \(…\) / \[…\] 归一成 $…$ / $$…$$。
 *
 *  为什么必须放在 marked **之前**：Markdown 里 `\(` 是「转义的左括号」，
 *  marked 会把反斜杠吃掉 —— 等轮到 KaTeX 时它已经变成 `(x)` 了，
 *  光在 auto-render 那边多配几个定界符是没用的（实测就是这个原因渲染不出来）。
 *  代码块与行内代码里的内容**不动**：那是要展示的代码本身，改写它才是错的。
 */
export function normalizeMath(md) {
  const conv = (s) => s
    .replace(/\\\[([\s\S]+?)\\\]/g, (m, tex) => `$$${tex}$$`)
    .replace(/\\\(([\s\S]+?)\\\)/g, (m, tex) => `$${tex}$`);
  const inText = (seg) => seg.split(/(`[^`\n]*`)/)
    .map((p, i) => (i % 2 ? p : conv(p))).join('');
  return String(md || '').split(/(```[\s\S]*?```)/)
    .map((s, i) => (i % 2 ? s : inText(s))).join('');
}

/* ------------------------------------------------------------------ 公式保护
 *
 * **公式内容一个字符都不该经过 Markdown。** 光把 `\(\)` 归一成 `$…$` 是不够的：
 * marked 接着会按**它自己的**语法去读公式里的字符，实测（marked 12 + gfm）：
 *
 *     $5\%$        →  $5%$            反斜杠被吃；% 在 LaTeX 是注释符 → 公式被截断
 *     $\{1,2\}$    →  ${1,2}$         \{ 命令没了，花括号语义全变
 *     $a\_1$       →  $a_1$           凑巧还对，但那是运气，不是设计
 *     $S=2*3*4$    →  $S=2<em>3</em>4$   乘号变斜体 → 文本节点被标签切碎 → **一个都不渲染**
 *     $a**2**$     →  $a<strong>2</strong>$
 *     $a~~b~~c$    →  $a<del>b</del>c$
 *     $$x&=1 \\ y&=2$$  →  &amp; 与单个 \ → aligned 的 & 对齐点和 \\ 换行全坏
 *
 * 这些都不是「模型写错了」，是渲染管线自己吃掉的 —— 老师手打同样的公式也一样坏。
 * 所以唯一可靠的做法：**先把公式摘出来换成占位符，解析完再放回去**。
 *
 * 占位符用 U+E000（Unicode 私有使用区）+ 序号：它不在正文里出现，
 * HTML/DOMPurify 也当普通字符保留。别换成控制字符 —— 消毒器会把它们剥掉，
 * 占位符一丢，公式就永远回不来了。
 */
const _PH_RE = /\uE000(\d+)\uE000/g;
const _ph = (i) => '\uE000' + i + '\uE000';

/** `$…$` / `$$…$$` / `\(…\)` / `\[…\]` → 占位符。**必须在 marked.parse 之前**。
 *
 *  @returns {{md: string, store: Array<{tex: string, display: boolean}>}}
 *           把 `md` 喂给 marked，出结果后连同 `store` 一起交给 restoreMath()。
 *  ⚠️ 代码块与行内代码整段跳过：那里的 `$` 是要展示的代码，不是公式。
 */
export function protectMath(md) {
  const store = [];
  const keep = (tex, display) => {
    store.push({ tex, display });
    return _ph(store.length - 1);
  };
  const conv = (s) => s
    .replace(/\\\[([\s\S]+?)\\\]/g, (m, tex) => keep(tex, true))
    .replace(/\\\(([\s\S]+?)\\\)/g, (m, tex) => keep(tex, false))
    // $$…$$ 必须先于 $…$：反过来会把 `$$x$$` 拆成两个空公式
    .replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => keep(tex, true))
    .replace(/\$([^$\n]+?)\$/g, (m, tex) => keep(tex, false));
  const inText = (seg) => seg.split(/(`[^`\n]*`)/)
    .map((p, i) => (i % 2 ? p : conv(p))).join('');
  const out = String(md || '').split(/(```[\s\S]*?```)/)
    .map((s, i) => (i % 2 ? s : inText(s))).join('');
  return { md: out, store };
}

/** 占位符 → 公式。**必须在 DOMPurify 之后**（否则 `$a<b$` 里的 `<` 会被当标签剥掉）。
 *
 *  还原时把公式内容做 HTML 转义（只转 `& < >`，不动引号）：浏览器会把 `&lt;`
 *  解码回 `<`，KaTeX 读的是 textContent，所以转义只保护 HTML 结构、不改公式本身。
 *  而 `&` 必须转成 `&amp;`，否则 aligned 环境里的对齐点 `&` 会被浏览器当实体开头。
 */
export function restoreMath(html, store) {
  if (!store || !store.length) return html;
  return String(html || '').replace(_PH_RE, (m, n) => {
    const it = store[Number(n)];
    if (!it) return m;
    const tex = String(it.tex)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return it.display ? `$$${tex}$$` : `$${tex}$`;
  });
}

const DELIMITERS = [
  // 除 $…$ / $$…$$ 外，也认 LaTeX 惯用的 \(…\) 与 \[…\] —— 从别处（讲义/网页/PDF
  // 复制）粘过来常常是那种写法，只认 $ 的话用户看到的就是一段带反斜杠的原文。
  { left: '$$', right: '$$', display: true },
  { left: '\\[', right: '\\]', display: true },
  { left: '$', right: '$', display: false },
  { left: '\\(', right: '\\)', display: false },
];

/** 文本 → 渲染进 el：先按 Markdown 排版，再让 KaTeX 就地渲染 $…$。
 *
 *  必须写 innerHTML，**不能**交给 Vue 插值：KaTeX 的 auto-render 会把 Vue
 *  管理的文本节点整个换掉，Vue 之后还往那个已摘除的节点里写新内容，
 *  预览会永远停在第一次渲染出公式的那一版。（同理，容器里别放 Vue 插值。）
 *
 *  marked 挂了也要能活 —— 退回纯文本，公式继续渲染，别让整块白掉。
 *
 *  @returns {boolean} 是否用上了 marked。false = 已经退化成纯文本（调用方据此
 *                     决定还要不要做「读渲染结果」的后续动作，比如建目录）。
 */
export function renderRichEl(el, text) {
  if (!el) return false;
  const t = text || '';
  if (!t) { el.textContent = ''; return false; }

  const markedOk = !!window.marked;
  if (markedOk) {
    // ① 公式先摘出来（protectMath）→ ② 交给 marked → ③ 消毒 → ④ 公式放回去。
    //    顺序不能动：公式内容过 Markdown 会被吃掉（见 protectMath 上面那张实测表），
    //    而还原必须在消毒之后，否则公式里的 `<` `&` 会被当标签处理。
    const p = protectMath(t);
    const raw = window.marked.parse(p.md, { breaks: true, gfm: true });
    el.innerHTML = restoreMath(window.DOMPurify ? window.DOMPurify.sanitize(raw) : raw, p.store);
  } else {
    el.textContent = t;      // 给**原文**：没渲染时不该连内容都被改写
  }

  // 公式在消毒**之后**渲染：KaTeX 自己生成的 DOM 是可信的，没必要再过一遍消毒
  if (window.renderMathInElement) {
    try {
      window.renderMathInElement(el, { delimiters: DELIMITERS, throwOnError: false });
    } catch (e) { /* 公式写错不该让整块内容渲染不出来 */ }
  }
  return markedOk;
}
