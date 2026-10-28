/** 批改台快捷键 —— **唯一出处**，两个页面共用：
 *    · SPA 旧版批改台  src/views/HomeworkGrade.js
 *    · 新版工作台      frontend/workbench.html（自己那份 onKeyDown 改为调这里的 installGradeKeys）
 *
 *  为什么抽出来：两边各写一套的话，「Ctrl+Z 在输入框里到底该不该拦」这种判断
 *  迟早只有一处被改到，表现就是「同一个键在一个页面上能用、在另一个页面上不能用」——
 *  正是用户这次报的第 3 条（橡皮、撤销只能一个个点）。
 *
 *  ## 四条硬规则（改之前先读）
 *  1. **输入框里不抢键**：INPUT / TEXTAREA / contentEditable 一律放行给浏览器，
 *     否则老师写批注时按 Ctrl+Z 会去撤销上一条标注，而不是撤销刚打的字。
 *     唯一例外是 Ctrl+S —— 不拦的话浏览器会弹「保存网页」。
 *  2. **查看模式不响应写操作**：canEdit() 返回 false 时，工具键、撤销、粗细一律不接。
 *  3. **不做重做**：Ctrl+Shift+Z / Ctrl+Y 明确不接。workbench 原来的注释就写着
 *     「重做还没做，别假装支持」—— 接了但没实现比不接更坏：按下去没反应，
 *     老师会以为键盘坏了。等撤销栈真能做反向操作了再接。
 *  4. **只列真接了的键**（KEY_HINTS 给界面用）：把「计划中」的键也写进提示，等于在界面上撒谎。
 */

/** 单键 → 工具。工具名与 GradeMarkLayer 的 mode 完全一致，两页不用再翻译一遍。 */
export const KEY_TOOL = {
  b: 'ink',      // 画笔（brush）
  e: 'erase',    // 橡皮（eraser）
  v: 'view',     // 选择
  t: 'text',     // 文字框
  c: 'check',    // 打勾
  x: 'cross',    // 打叉
  l: 'line',     // 逐题分割
  r: 'region',   // 框题
};

/** 快捷键提示文案，给界面显示用。**只写真正接了的键**（规则 4）。 */
export const KEY_HINTS = [
  ['B', '画笔'], ['E', '橡皮'], ['V', '选择'], ['T', '文字框'],
  ['C', '打勾'], ['X', '打叉'], ['L', '逐题分割'], ['R', '框题'],
  ['Esc', '回到选择'], ['Ctrl+Z', '撤销'], ['Ctrl+S', '保存'],
];

/** 焦点是否在能打字的地方。命中就一律不抢键。 */
export function isTypingTarget(t) {
  if (!t || !t.tagName) return false;
  const tag = String(t.tagName).toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || !!t.isContentEditable;
}

/**
 * 装上快捷键，返回卸载函数。**页面卸载时必须调**，否则切走之后它还在抢键。
 *
 * @param {object} opts
 * @param {() => boolean} [opts.canEdit]   查看模式返回 false；缺省视为可编辑
 * @param {(tool:string) => void} [opts.tool]      切工具
 * @param {() => void} [opts.undo]         撤销
 * @param {() => void} [opts.save]         保存（Ctrl+S）
 * @param {() => void} [opts.escape]       Esc：回选择 / 关弹窗
 * @param {() => void} [opts.wider]        ] 笔粗一档
 * @param {() => void} [opts.thinner]      [ 笔细一档
 * @param {EventTarget} [opts.target]      挂哪儿，默认 window
 */
export function installGradeKeys(opts) {
  const o = opts || {};
  const host = o.target || (typeof window !== 'undefined' ? window : null);
  if (!host) return () => {};
  const canEdit = typeof o.canEdit === 'function' ? o.canEdit : () => true;

  const onKey = (e) => {
    if (!e.key || e.altKey) return;
    const mod = e.ctrlKey || e.metaKey;
    const k = e.key.toLowerCase();

    // ① Ctrl/Cmd+S：**输入框里也要拦**，否则浏览器弹「保存网页」
    if (mod && !e.shiftKey && k === 's') {
      if (o.save) { e.preventDefault(); o.save(); }
      return;
    }
    // ② 输入框里其余键一律让给浏览器（含 Ctrl+Z —— 那是撤销正在打的字）
    if (isTypingTarget(e.target)) return;

    // ③ Ctrl/Cmd+Z 撤销。带 Shift 的组合是重做，明确不接（规则 3）
    if (mod && !e.shiftKey && k === 'z') {
      if (!canEdit()) return;
      e.preventDefault();
      if (o.undo) o.undo();
      return;
    }
    if (mod) return;                 // 其余 Ctrl 组合不抢

    if (e.key === 'Escape') {
      e.preventDefault();
      if (o.escape) o.escape();
      return;
    }
    // ④ 笔粗 / 笔细：方括号。只有新版工作台有粗细档；
    //    SPA 那边不传这两个回调，自然就不响应，不用各写一份判断。
    if (e.key === '[' || e.key === ']') {
      if (!canEdit()) return;
      const fn = e.key === '[' ? o.thinner : o.wider;
      if (fn) { e.preventDefault(); fn(); }
      return;
    }
    const tool = KEY_TOOL[k];
    if (!tool) return;
    if (!canEdit()) return;          // 查看模式：工具键不响应
    e.preventDefault();
    if (o.tool) o.tool(tool);
  };

  host.addEventListener('keydown', onKey);
  return () => host.removeEventListener('keydown', onKey);
}
