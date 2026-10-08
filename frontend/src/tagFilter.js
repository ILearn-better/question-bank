/**
 * 标签搜索的纯逻辑 —— 单独成文件是为了能脱离浏览器单测（node 直接 import）。
 *
 * ## 两种写法（用户点名要的，**和常见直觉相反，别好心「修」回去**）
 *
 *   · 空格分隔 = **任一命中**（OR）
 *     「对称 函数」→ 所有含「对称」的标签，加上所有含「函数」的标签
 *   · 竖线分隔 = **同时包含**（AND）
 *     「对称|函数」→ 只留标签里同时出现「对称」和「函数」的那几个
 *
 * 为什么竖线反而是「并且」：老师想说的是「我要既是轴对称、又是函数题的标签」，
 * 竖线在他那儿是「并且」的写法，空格则是「随便哪个都行」的罗列。
 * 正因为反直觉，**输入框旁边必须把这两种写法写出来**（见 Papers.js 的 placeholder）。
 *
 * 两种符号都出现时：**以竖线为准**（AND 优先 —— 它是更强的约束，
 * 且「A|B C」到底算 (A且B) 还是 A 或 B 或 C 没人能预期，索性不做混用）。
 * 标签名本身**不会**含空格或竖线（录入时就限制了），所以不必担心误切。
 */

/** 解析查询串 → { mode: 'or' | 'and', kws: string[] }。 */
export function parseTagQuery(q) {
  const raw = String(q == null ? '' : q).trim();
  if (!raw) return { mode: 'or', kws: [] };
  if (raw.indexOf('|') >= 0) {
    return { mode: 'and', kws: raw.split('|').map((s) => s.trim()).filter(Boolean) };
  }
  return { mode: 'or', kws: raw.split(/\s+/).filter(Boolean) };
}

/** 过滤标签列表。
 *  元素可以是字符串，也可以是 `{ tag, count }` 这种对象（出卷页就是后者）。
 *  匹配**大小写不敏感**（英文标签用得上；中文不受影响）。 */
export function matchTags(list, q) {
  const items = list || [];
  const { mode, kws } = parseTagQuery(q);
  if (!kws.length) return items.slice();
  const needles = kws.map((k) => k.toLowerCase());
  const textOf = (t) => String(t && typeof t === 'object' ? t.tag : t).toLowerCase();
  return items.filter((t) => {
    const s = textOf(t);
    return mode === 'and'
      ? needles.every((k) => s.includes(k))
      : needles.some((k) => s.includes(k));
  });
}

/** 给 UI 的一句话回显（两种模式各说各的话），让老师确认自己没写反。 */
export function tagQueryHint(q) {
  const { mode, kws } = parseTagQuery(q);
  if (!kws.length) return '';
  return mode === 'and'
    ? `只留同时含「${kws.join('」「')}」的标签`
    : `含「${kws.join('」或「')}」的标签都列出来`;
}
