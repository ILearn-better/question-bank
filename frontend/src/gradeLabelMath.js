/**
 * 文本框里的 **LaTeX 渲染** —— 纯函数，不碰 DOM、不 import 任何东西（node 能直接量）。
 *
 * 为什么单独成文件而不是塞进 `mathRender.js`：
 *   - `mathRender.js` 是「Markdown + 公式」的整块管线，要 marked + DOMPurify，走的是
 *     `renderRichEl(el, text)` 那种**写进真实 DOM** 的姿势，异步加载库、还得等容器挂上；
 *   - 文本框是**每帧都可能重算的内联 HTML 串**（@v-html），只需要「纯文本 + 公式」，
 *     不需要 Markdown。走 markdown 的话，批注里随手写的 `a_b` 会变成斜体、
 *     `1*2*3` 会变成 `<em>` —— 批注不是文档，别给它排版的权力。
 *   两条管线共用**同一套定界符**（$$ / \[ / $ / \(，且 $$ 先于 $），只是要不要过 marked 的区别。
 *
 * ⚠️ 输出要直接交给 `v-html`，所以**每一个**来自用户的字符都必须过 `escapeHtml`；
 *     只有 KaTeX 自己生成的标签可以原样拼进去。
 */

// 与 mathRender.js 的 protectMath 同序：`$$` 必须先于 `$`，否则 `$$x$$` 会被拆成两个空公式。
// 四个分支分别对应 ($$..$$) (\[..\]) ($..$) (\(..\))
const MATH_RE = /\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\$([^$\n]+?)\$|\\\(([\s\S]+?)\\\)/g;

export function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/**
 * 把一段批注切成「文字 / 公式」交替的片段。
 * 没配对的定界符（比如单个 `$`）当普通文字 —— 老师写「花了 $5」不该整段消失。
 *
 * @returns {Array<{t:'text', v:string} | {t:'math', tex:string, display:boolean}>}
 */
export function splitMath(text) {
  const s = String(text == null ? '' : text);
  const out = [];
  let last = 0;
  MATH_RE.lastIndex = 0;
  let m;
  while ((m = MATH_RE.exec(s)) !== null) {
    if (m[0] === '') { MATH_RE.lastIndex += 1; continue; }   // 防零宽匹配死循环
    if (m.index > last) out.push({ t: 'text', v: s.slice(last, m.index) });
    const display = m[1] !== undefined || m[2] !== undefined;
    const tex = m[1] !== undefined ? m[1]
      : m[2] !== undefined ? m[2]
        : m[3] !== undefined ? m[3] : m[4];
    out.push({ t: 'math', tex, display });
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push({ t: 'text', v: s.slice(last) });
  return out;
}

/**
 * 批注文本 → 能直接喂 `v-html` 的 HTML。
 *
 * @param {string} text  老师的原文（可含 LaTeX）
 * @param {object|null} katex  `window.katex`；**没加载到就退回原文**（只是不渲染，不吞内容）
 * @returns {string}
 */
export function renderLabelHtml(text, katex) {
  const t = String(text == null ? '' : text);
  if (!t) return '';
  const parts = splitMath(t);
  let out = '';
  for (const p of parts) {
    if (p.t === 'text') { out += escapeHtml(p.v); continue; }
    // 公式整体包一层：外层 `.gmark-label` 是 white-space:pre-wrap（保住批注里的换行），
    // 但 KaTeX 的排版不能被 pre-wrap 影响，所以这层单独改回 normal。
    if (katex && typeof katex.renderToString === 'function') {
      try {
        out += '<span class="gm-math">' + katex.renderToString(p.tex, {
          displayMode: !!p.display, throwOnError: false, strict: false,
        }) + '</span>';
        continue;
      } catch (e) { /* 落到下面的原文分支 */ }
    }
    // 没库 / 渲染失败：给**原文**，连定界符一起 —— 老师一眼能看出「这里没渲染」，
    // 比悄悄把 $ 吃掉、看起来像正常文字要诚实。
    out += '<span class="gm-math-raw">'
      + escapeHtml((p.display ? '$$' : '$') + p.tex + (p.display ? '$$' : '$'))
      + '</span>';
  }
  return out;
}
