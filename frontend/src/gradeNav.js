// 题目导航的「现在该高亮哪道题」判据 —— 唯一出处，HomeworkGrade.js 与测试都从这 import。
//
// 用**顶部线**算法，不是中间线：滚动容器的判题线（可视区顶 + 吸顶工具栏高）扫过哪道题
// 的顶边，就高亮哪道题。中间线会在两道题交界处来回跳，顶部线稳定得多。
//
// 注意：这里只算「当前页」的题。跨页题两段在不同页，各自以它在本页那一段的顶边参与比较。

/** 判定「现在该高亮哪道题」：顶部线算法。blocks = regionBlocks 形态。
 *
 *  ⚠️ **不能靠输入顺序**。`groupSegs` 是按 `seq` 排的，不是按纵向位置排的 ——
 *     老师先在页中段框一个、再回头框页顶那个，seq 顺序与 y 顺序正好相反。
 *     所以这里改成「在所有满足条件的候选里取顶边最靠下的那一道」，
 *     结果只由几何决定，与 blocks 的排列顺序无关。
 */
export function activeSeqAt(blocks, pageNo, linePx, imgH) {
  let active = null;
  let bestY = -Infinity;
  for (const b of blocks || []) {
    if (!(b.pages || []).includes(Number(pageNo))) continue;   // 只算当前页的题
    const seg = (b.segs || []).find((s) => Number(s.page_no || 1) === Number(pageNo));
    if (!seg) continue;
    const y0 = Number((seg.box && seg.box[1]) || 0);
    if (imgH * y0 / 1000 > linePx) continue;                  // 判题线还没扫到它的顶边
    if (y0 > bestY) { bestY = y0; active = b.seq; }           // 取顶边最靠下的，不靠顺序
  }
  return active;
}
