// 标注页的几何计算 —— **纯函数，不碰 DOM、不 import 任何东西**（这样才能用 node 直接量）。
//
// 唯一出处：画笔、橡皮、提交、导出都从这里取坐标口径。
// ⚠️ 别在组件里再写一套 `clientX / rect.width * 1000` —— 两套算法会慢慢漂开，
//    表现是「屏幕上画得准、导出的 PDF 差半厘米」，而且没人看得出是哪一步错的。
export const UNIT = 1000;

export function clampUnit(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return 0;
  return Math.min(UNIT, Math.max(0, v));
}

/**
 * 屏幕坐标 → 页面的 0~1000 坐标。
 * `rect` 是 SVG 元素的 getBoundingClientRect()（图还没渲染出来时宽高为 0 → 返回 null）。
 *
 * ⚠️ x 用宽、y 用高**分别换算**：页图在容器里是非等比铺满的（preserveAspectRatio="none"），
 *    用一个比例去乘 y，竖版页面上会整体偏移。
 */
export function pagePoint(clientX, clientY, rect) {
  if (!rect || !rect.width || !rect.height) return null;
  const x = (Number(clientX) - rect.left) / rect.width * UNIT;
  const y = (Number(clientY) - rect.top) / rect.height * UNIT;
  return [clampUnit(x), clampUnit(y)];
}

/** 点到线段的距离（0~1000 坐标系里算，橡皮靠它判断「擦到没有」）。 */
export function segDist(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1;
  const dy = y2 - y1;
  const len2 = dx * dx + dy * dy;
  if (!len2) return Math.hypot(px - x1, py - y1);
  let t = ((px - x1) * dx + (py - y1) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

/**
 * 整体平移一组坐标（拖动标注用）：偶数位是 x、奇数位是 y，两种情况都成立 ——
 * `[x0,y0,x1,y1]` 的包围盒 和 `[x,y,x,y,…]` 的扁平点列是同一个规律，不用分两条路。
 *
 * ⚠️ 平移**必须**走这里，不要在组件里 `geo.map(v => v + dx)` 图省事 ——
 *    那样 y 也会加上 dx（x/y 是交替的），拖一下框就斜着跑。
 *
 * ⚠️⚠️ 夹的是**平移量**，不是夹每个坐标 —— 这一点 2026-11-01 用真浏览器量出来后改的。
 *    原实现逐个坐标夹到 0~1000，于是「把文本框拖到右边」（x1+x2 都超 1000）时：
 *      [829,666,1000,719] 再往右拖 400 → [1000,666,1000,719]
 *    宽度从 92 直接归零，框被**压扁**、`sized` 判据失效 → 文字退回零面积点样式往上一跳。
 *    用户的描述是「莫名跳回原位」，但真因是形状被压没了。
 *    现在先按整组的 min/max 算出允许的平移范围，整组**刚性平移** —— 形状永远不变形，
 *    顶多停在页边拖不动（这才是拖动该有的手感）。
 */
export function shiftGeo(geo, dx, dy) {
  const list = Array.isArray(geo) ? geo : [];
  if (!list.length) return [];
  const nums = list.map(Number);
  if (nums.some((v) => !Number.isFinite(v))) return [];

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let i = 0; i < nums.length; i += 1) {
    if (i % 2) {
      if (nums[i] < minY) minY = nums[i];
      if (nums[i] > maxY) maxY = nums[i];
    } else {
      if (nums[i] < minX) minX = nums[i];
      if (nums[i] > maxX) maxX = nums[i];
    }
  }
  // 整组本来就比页面宽（理论上进不来）→ 不给平移，免得算出 上限 < 下限
  const sx = UNIT - maxX < -minX ? 0 : Math.min(Math.max(Number(dx) || 0, -minX), UNIT - maxX);
  const sy = UNIT - maxY < -minY ? 0 : Math.min(Math.max(Number(dy) || 0, -minY), UNIT - maxY);

  return nums.map((v, i) => Math.round(clampUnit(v + (i % 2 ? sy : sx))));
}

/** 包围盒（取整）。笔迹的 geo 只用于命中/统计，真正的形状靠 points。 */
export function bboxOf(points) {
  if (!points || !points.length) return [0, 0, 0, 0];
  const xs = points.map((p) => clampUnit(p[0]));
  const ys = points.map((p) => clampUnit(p[1]));
  return [
    Math.round(Math.min(...xs)), Math.round(Math.min(...ys)),
    Math.round(Math.max(...xs)), Math.round(Math.max(...ys)),
  ];
}

/** [[x,y],…] → [x,y,x,y,…]（取整并夹紧）。库里存的就是这个扁平形态。 */
export function flatPoints(points) {
  const out = [];
  for (const p of points || []) {
    out.push(Math.round(clampUnit(p[0])), Math.round(clampUnit(p[1])));
  }
  return out;
}
