// 考情分析的纯逻辑：**换模板时怎么合并段落**。
//
// 为什么要单独一个文件：这块唯一真正会出错的地方是「换个模板把已经写好的字弄丢」。
// 那种错误在界面上很难当场发现（得把整段滚一遍才知道那段没了），
// 但它毁掉的是老师刚敲进去的几百字。所以拆出来，用 node 直接测（同 calendar.js / tagFilter.js）。
//
// 规则一句话：**已经写下的字，一个字都不能丢**。
//   · 新模板里有的段   → 用模板给的标题与提示语，正文沿用同 key 的旧文字
//   · 旧记录里多出来的段 → 原样附在后面（标记 extra）
//                        不静默删掉：老师自己加的「家长沟通要点」凭什么因为换个模板就没了
//   · 新模板里新增的段 → 空着，把提示语显示出来

/** 把任意来源的段落数组洗成统一形状（键都在、类型都是字符串）。 */
export function normalizeSections(list) {
  return (Array.isArray(list) ? list : [])
    .filter((s) => s && typeof s === 'object')
    .map((s) => ({
      key: String(s.key || ''),
      label: String(s.label || ''),
      hint: String(s.hint || ''),
      text: String(s.text || ''),
    }));
}

/**
 * 把「已写的内容」套到「新选的模板」上。
 *
 * @param {Array} existing 现有段落 [{key,label,hint,text}]（来自正在编辑的这份分析）
 * @param {Array} template 新模板的段落 [{key,label,hint}]（来自模板库）
 * @returns {{sections:Array, carried:number, added:number, extra:number}}
 *   carried：从旧内容里带过来的**非空**段数（>0 时界面该提醒一句「原有文字都保留了」）
 *   added  ：模板新增的、还空着的段数
 *   extra  ：模板里没有、被保留下来的段数
 */
export function mergeSections(existing, template) {
  const olds = normalizeSections(existing);
  const tpl = normalizeSections(template);

  const byKey = new Map();
  for (const s of olds) {
    if (s.key && !byKey.has(s.key)) byKey.set(s.key, s);
  }

  const used = new Set();
  const sections = tpl.map((t) => {
    const old = t.key ? byKey.get(t.key) : null;
    if (old) used.add(t.key);
    return {
      key: t.key,
      label: t.label || (old ? old.label : ''),
      hint: t.hint || (old ? old.hint : ''),
      text: old ? old.text : '',
      extra: false,
    };
  });

  // 模板里没有的段：原样带上，别丢
  const extraSecs = [];
  const seen = new Set();
  for (const s of olds) {
    if (s.key && used.has(s.key)) continue;
    const dedup = s.key || `#anon${extraSecs.length}`;
    if (seen.has(dedup)) continue;
    seen.add(dedup);
    extraSecs.push({ ...s, extra: true });
  }

  return {
    sections: [...sections, ...extraSecs],
    carried: sections.filter((s) => s.text.trim()).length,
    added: sections.filter((s) => !s.text.trim()).length,
    extra: extraSecs.length,
  };
}

/** 空的分析对象（还没选模板时用它，形状与后端存的一致）。 */
export function emptyAnalysis() {
  return { template_id: null, template_name: '', sections: [] };
}

/** 这份分析写了几段、写满了哪几段 —— 列表行上显示总结要用。 */
export function analysisStats(analysis) {
  const secs = normalizeSections((analysis || {}).sections);
  return {
    total: secs.length,
    filled: secs.filter((s) => s.text.trim()).length,
    chars: secs.reduce((n, s) => n + s.text.length, 0),
  };
}

/** 只看有内容的段（详情页展开时用；空段显示出来只是噪音）。 */
export function filledSections(analysis) {
  return normalizeSections((analysis || {}).sections)
    .filter((s) => s.text.trim() || s.label);
}

/** 给「加一段」找一个没被占用的 key。 */
export function nextExtraKey(sections) {
  const used = new Set(normalizeSections(sections).map((s) => s.key));
  let i = 1;
  while (used.has(`extra_${i}`)) i += 1;
  return `extra_${i}`;
}

/** 列表行上的一句话预览：第一段有内容的前 40 字。 */
export function analysisPreview(analysis, limit = 40) {
  const sec = normalizeSections((analysis || {}).sections).find((s) => s.text.trim());
  if (!sec) return '';
  const t = sec.text.trim().replace(/\s+/g, ' ');
  return t.length > limit ? `${t.slice(0, limit)}…` : t;
}
