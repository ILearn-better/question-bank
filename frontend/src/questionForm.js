// 「这一题该显示成文本、还是显示成图」—— 前端侧的唯一判定出处。
//
// 为什么单独一个模块：出卷挑题、备课挑题都要做同一个判断（有文本用文本、没文本用图、
// 两样都没有就是空题）。各写一遍的结果已经吃过一次 —— 挑题列表写着「（图片题，没有文本）」，
// 老师根本认不出是哪道题，只能盲挑。
//
// ⚠️ 后端 `backend/app/services/paper_export.py` 的 `_choose_form()` 是**印在卷子上**
//    的唯一出处；本模块是它在**显示**侧的镜像。两边规则必须逐条一致 ——
//    不一致的后果就是本项目最贵的那类 bug：「列表里看着有题，印出来是空白」。
//    test_paper_ui.py 第 ⑪ 节拿同一张对照表喂两边，改一边不改另一边会立刻红。
//
// 纯逻辑：不 import 任何东西、不碰 document，node 可以直接单测
//（和 tagFilter.js / calendar.js 一个路子）。

/** 有题干文字吗。老数据里 content 可能是 NULL。 */
export function hasStemText(q) {
  return !!String((q && q.content) || '').trim();
}

/** 有原貌截图吗。
 *
 *  ⚠️ 刻意**不把 figure_image 算进来** —— 题干配图是「文本形态里补印的那幅图」，
 *  不改变形态判定。后端 _choose_form 收的就是 `it.get("image")`，算进来两边就不一致了。 */
export function hasStemImage(q) {
  return !!(q && q.image);
}

/** 要显示的那张图（原貌截图）。没有就是空串 —— 别给 <img> 塞空 src（会去请求当前页）。 */
export function stemImage(q) {
  return (q && q.image) || '';
}

/**
 * 这一题按哪种形态显示 / 会印成什么。与后端 `_choose_form` 逐条对应。
 *
 * @param {object} q  题目（content / image / render_prefer）
 * @param {string} override 整卷覆盖：'auto' | 'text' | 'image'（出卷页那个「题目呈现」）
 * @returns {'text'|'image'|'none'}
 *
 * 两条规则（和后端注释里写的是同两条）：
 *   1. override 压过单题偏好 —— 「全部用图片」不该被每题各自的默认挡住；
 *   2. ★ **缺的那种自动退回另一种**。强制用图但没图、强制用文本但没文本，
 *      都落到还有的那一个，绝不印空白题。
 */
export function chooseForm(q, override) {
  const hasText = hasStemText(q);
  const hasImg = hasStemImage(q);
  // 认不出来的值（拼错的偏好、老前端传来的空）当 auto —— 与后端 `pref or "auto"` 一致
  let pref = (override === 'text' || override === 'image')
    ? override
    : ((q && q.render_prefer) || 'auto');
  if (pref !== 'text' && pref !== 'image') pref = 'auto';
  if (pref === 'text') return hasText ? 'text' : (hasImg ? 'image' : 'none');
  if (pref === 'image') return hasImg ? 'image' : (hasText ? 'text' : 'none');
  return hasText ? 'text' : (hasImg ? 'image' : 'none');
}

/** 两样都没有的题 —— 卷子上会是空白。挑题时就该拦一道，别等印出来才发现。 */
export const NO_STEM_NOTE = '空题：没有文字也没有图（出卷会印成空白）';

/** 这一题显示成图的原因。老师看一眼就懂，不用猜「我的公式哪去了」。 */
export function imageNote(q, override) {
  if (!hasStemText(q)) return '图片题：没有识别出文字，按原貌图显示';
  if (override === 'image') return '按你选的「全部用图片」显示原貌图';
  return '这题存的是「出卷用图片」偏好，按原貌图显示';
}
