// 作业批改台的「分割线」几何 —— **纯函数，不碰 DOM、不 import 任何东西**（node 直接量）。
//
// 为什么要这一层（用户 2026-10-19 原话）：
//   「借鉴录题代码，在此处实现：逐题分割（当前是框选，我还要分割线分割）」
//   批量录题那边画的是**分界线**（一条横贯整页的线，线之间就是题块），
//   这里是框选（拖一个矩形）。老师要的是前者：一页七八道题，逐题拖框太慢，
//   沿着题目之间的空隙点几下就切好了。
//
// ★ 核心决定：**分割线不新增任何数据形态，它只是「生成整页宽框」的快捷方式。**
//   一条线 y=c 切出来的段，落到库里就是一个 `box = [0, y0, 1000, y1]`。
//   于是后端一个字都不用改 —— 裁切、跨页竖拼、逐题批改、沿用到另一份，
//   全部走已有的「一题一区域」那套。这就是「不重造轮子」的落点。
//
// ★ 第二个决定：**全切，第一条线以上也算一段。**
//   批量录题那边第一条线以上是卷头/页眉，被刻意丢掉。批改这边不一样：
//   学生可能从页顶就开始写第 1 题，丢掉那段就是丢掉一整题。
//   多出来的页眉段 AI 会判「读不到题」，老师删一下就行 —— 漏题的代价比多一行大得多。
//
// ⚠️ 这个文件里的坐标一律是 **0~1000 的按页归一化整数**，与 markGeom / 库里同制。
//    不要在这里出现像素、PDF 点。
import { clampUnit } from './markGeom.js';

/** 两条线之间 / 一个段的最小高度。
 *  **与后端 grade_store.REGION_MIN_SIDE 同值**：比这更薄的段裁出来只是一条糊字，
 *  送模型等于花钱买一句「读不清」，而后端会直接 422。
 *  前端先挡一层，老师就不用为一次手抖看一个红色报错。 */
export const LINE_MIN_GAP = 20;

/** 一个框被算作「整页宽」的容差（0~1000 制，8 ≈ 纸面 0.8%）。
 *  框选很难恰好贴着左右页边，所以判据不能写 `x0 === 0 && x1 === 1000`。 */
export const FULL_WIDTH_TOL = 8;

/** 框覆盖某个段多少，才认为「这块已经被框管了」。
 *  取 0.5 的理由：框稍微盖住一段的一半以上，几乎一定是在重画这一题；
 *  而只蹭到一条边（比如框画得比段高一丁点）不该把整段吞掉。 */
export const COVER_RATIO = 0.5;

const num = (v, d = 0) => {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
};

/** box = [x0,y0,x1,y1] → 规整（左上到右下、取整、夹进 0~1000）。**永不抛异常**。 */
export function normBox(box) {
  if (!Array.isArray(box) || box.length < 4) return null;
  const x0 = clampUnit(Math.min(num(box[0]), num(box[2])));
  const x1 = clampUnit(Math.max(num(box[0]), num(box[2])));
  const y0 = clampUnit(Math.min(num(box[1]), num(box[3])));
  const y1 = clampUnit(Math.max(num(box[1]), num(box[3])));
  return [Math.round(x0), Math.round(y0), Math.round(x1), Math.round(y1)];
}

/** 这个框是不是「分割线切出来的整页宽段」。 */
export function isFullWidth(box, tol = FULL_WIDTH_TOL) {
  const b = normBox(box);
  if (!b) return false;
  return b[0] <= tol && b[2] >= 1000 - tol;
}

/**
 * 把一页的线切成一串**整页宽**的段。返回 `[[0,y0,1000,y1], …]`。
 *
 * 线的语义：`lines` 是这一页上「题目之间的空隙」的 y 坐标（0~1000）。
 * 相邻两条边之间就是一道题 —— 边集合 = 0、所有线、1000。
 *
 * ⚠️ 太薄的带**直接丢掉**（两条线挨得比 `LINE_MIN_GAP` 近）。不丢的话，
 *    老师手抖多点了一下就会多出一个 3 单位高的段：后端 422、
 *    前端还要显示一行「第 6 题 · 第 2 页 · 高 0.3%」，两个人都不知道该怎么办。
 * ⚠️ 重复的线先去重（同一个 y 点两下不该切出零高度的段）。
 * ⚠️ `0` 和 `1000` 本身不作为线（它们已经是边了）。
 */
export function bandsOfPage(lines, endY = 1000) {
  // 结束线（`endY`）：这一页切到哪儿为止。**默认 1000 = 到页底**，与历史行为一致。
  // 老师把它拖到 96% 就表示「下面 4% 是页脚，别送 AI」——
  // 这是「每页尾部处理」缺的那个旋钮，不用新增任何数据形态：
  // 切出来的段仍然是整页宽的框，只是最后一段的底边不再是 1000。
  const end = Math.round(clampUnit(num(endY, 1000))) || 1000;
  const ys = Array.from(new Set(
    (lines || []).map((y) => Math.round(clampUnit(num(y))))
      .filter((y) => y > 0 && y < end),          // 结束线以下的线没有意义（切不出东西）
  )).sort((a, b) => a - b);
  const edges = [0, ...ys, end];
  const out = [];
  for (let i = 0; i + 1 < edges.length; i += 1) {
    const y0 = edges[i];
    const y1 = edges[i + 1];
    if (y1 - y0 < LINE_MIN_GAP) continue;
    out.push([0, y0, 1000, y1]);
  }
  return out;
}

/** 两个框的交集面积（0~1000 制，所以量纲是「平方单位」）。 */
export function intersectArea(a, b) {
  const A = normBox(a);
  const B = normBox(b);
  if (!A || !B) return 0;
  const w = Math.min(A[2], B[2]) - Math.max(A[0], B[0]);
  const h = Math.min(A[3], B[3]) - Math.max(A[1], B[1]);
  return w > 0 && h > 0 ? w * h : 0;
}

/** `inner` 有多大比例落在 `outer` 里（0~1）。`inner` 面积为 0 → 0。 */
export function overlapRatio(inner, outer) {
  const A = normBox(inner);
  if (!A) return 0;
  const area = (A[2] - A[0]) * (A[3] - A[1]);
  if (area <= 0) return 0;
  return intersectArea(A, outer) / area;
}

/**
 * 从已存的段**反推分割线**（重开页面后还能继续加线）。
 *
 * 为什么可以反推：整页宽的段只可能来自分割线 —— 老师手拖一个框，
 * 左右边几乎不可能同时压在 0 和 1000 上（真的压上了，当成线也完全等价）。
 *
 * 只取「上下相邻」的两分段：`[0,1000]` 那种整页一段不产线（没有可切的边界）。
 */
export function linesFromSegs(segs, tol = FULL_WIDTH_TOL) {
  const bands = (segs || [])
    .map((s) => normBox(s && s.box))
    .filter((b) => b && isFullWidth(b, tol))
    .sort((a, b) => a[1] - b[1]);
  const out = [];
  for (let i = 0; i + 1 < bands.length; i += 1) {
    const lower = bands[i][3];           // 上一段的下边
    const upper = bands[i + 1][1];       // 下一段的上边
    // 中间有明显空隙才算一条线（两段首尾相接时 mid 就是一个点，也算）
    const mid = Math.round((lower + upper) / 2);
    if (mid > 0 && mid < 1000) out.push(mid);
  }
  return Array.from(new Set(out));
}

/**
 * 某一页**重新切一遍**：线切出来的段 + 原有的框选段，按「框优先」合并。
 *
 * 「框优先」是什么意思：老师画了线（自动切出 5 段），又发现第 3 题横跨了两段，
 * 于是用框把第 3 题整个框住 —— 这时框覆盖到的那几段**不再生成**，
 * 由框自己顶上。否则第 3 题会既有一个框段、又有两个线切段，变成三道题。
 *
 * 返回该页的新段列表（`{ seq, q_no, box, src }`）。`seq` 先给 -1（待
 * `sortBlocksByGeometry` 收口），**同题分组关系在这个函数里不产生**
 * —— 线切出来的每一段天然各是一题。
 *
 * @param {Array} curSegs  这一页现有的段（含框选段与上一次切出来的线段）
 * @param {Array} lines    这一页的线
 */
export function reslicePage(curSegs, lines, endY = 1000) {
  const boxes = (curSegs || []).filter((s) => s && s.src !== 'line');
  /* ⚠️ **没有线就不产生线段**，哪怕一段都没有。
   *    `bandsOfPage([])` 按「全切」语义会返回整页那一段 —— 那在「分割线模式下一刀没画」
   *    的场景里是对的，但放进重切就会出事：老师只是拖了一个框，页面上凭空多出一个
   *    覆盖整页的段（框数 5 变 6，还给那个幽灵段占了一个题号）。
   *    这个 bug 是挂载检查抓出来的（2026-10-19）：静态断言与单测都看不出来，
   *    因为单测里喂进去的线永远非空。 */
  const hasLines = (lines || []).some((y) => Number.isFinite(Number(y)));
  const bands = hasLines
    ? bandsOfPage(lines, endY).filter((b) => !boxes.some((x) => overlapRatio(b, x.box) >= COVER_RATIO))
    : [];
  const lineSegs = bands.map((b) => ({
    id: null, seq: -1, q_no: '', box: b, src: 'line',
  }));
  // 框原样留下（保住它们的 id / seq / q_no / 跨页分组）
  return boxes.concat(lineSegs);
}

/**
 * 从已存的段**反推结束线**（`endY`）：线切出来的段最下面那条底边。
 *
 * 为什么不用额外存一份 `endY`：它本来就已经在几何里了 —— 最后一段的底边就是它。
 * 再存一份就等于同一个事实两个出处，迟早对不上（改了段没改 endY）。
 *
 * 没有整页宽的段（一页全是手拖的框，或者还没切）→ 返回 1000（到页底），
 * 也就是历史行为，老数据零变化。
 */
export function endOfPageFromSegs(segs, tol = FULL_WIDTH_TOL) {
  const bottoms = (segs || [])
    .map((s) => normBox(s && s.box))
    .filter((b) => b && isFullWidth(b, tol))
    .map((b) => b[3]);
  if (!bottoms.length) return 1000;
  const end = Math.round(clampUnit(Math.max(...bottoms)));
  return end > 0 ? end : 1000;
}

/**
 * 按**阅读顺序**给块（一道题，可含多段）重排 `seq`。
 *
 * 与 `HomeworkGrade.js` 原来的 relabelRegions 的区别：那个按 seq 值排序，
 * 只能「补空缺」；这个按**几何**排（先页、再段顶边），所以
 * 「先画了第 2 页、后画第 1 页」也能排成正确顺序 —— 分割线是逐页切的，
 * 老师完全可能从中间某页开始。
 *
 * ⚠️ **一个块的几段共用同一个 seq，重排后仍然共用** —— 跨页题的分组靠它，
 *    这里只换号、不拆组。
 *
 * @param {Array} segs  [{ page_no, seq, box, … }]
 * @returns {Array}     同样的段，seq 换成 0..n-1（返回新数组，不改入参）
 */
export function sortBlocksByGeometry(segs) {
  const by = new Map();
  for (const s of segs || []) {
    const k = Number(s && s.seq);
    if (!by.has(k)) by.set(k, []);
    by.get(k).push(s);
  }
  const blocks = Array.from(by.entries()).map(([seq, list]) => {
    const ordered = list.slice().sort((a, b) => {
      const pa = num(a.page_no, 1);
      const pb = num(b.page_no, 1);
      if (pa !== pb) return pa - pb;
      const ba = normBox(a.box) || [0, 0, 0, 0];
      const bb = normBox(b.box) || [0, 0, 0, 0];
      return ba[1] - bb[1];
    });
    return { seq, segs: ordered, page: num(ordered[0] && ordered[0].page_no, 1), top: (normBox(ordered[0] && ordered[0].box) || [0, 0])[1] };
  });
  blocks.sort((a, b) => (a.page - b.page) || (a.top - b.top) || (a.seq - b.seq));
  const remap = new Map(blocks.map((b, i) => [b.seq, i]));
  return (segs || []).map((s) => ({ ...s, seq: remap.get(Number(s.seq)) }));
}

/**
 * 删掉某一段时该动哪条线：**它上边那条**（没有就下边那条）。
 *
 * 语义 = 「这一段不该单独成题，并到相邻那段里去」。
 * 不做「删了这一段就永久不生成它」——那需要额外记一份『不切』名单，
 * 而线一改，名单里的 y 就全对不上了（批量录题那边的 skipKeys 是挂在
 * 段 key 上的，有同样的脆弱性）。合并是更小、更可预测的动作。
 */
export function lineToDropFor(box, lines) {
  const b = normBox(box);
  if (!b) return null;
  const ys = Array.from(new Set((lines || []).map((y) => Math.round(clampUnit(num(y))))))
    .sort((x, y) => x - y);
  const up = ys.filter((y) => y <= b[1] + 1);
  if (up.length) return up[up.length - 1];          // 上边那条（最靠近 y0 的）
  const down = ys.filter((y) => y >= b[3] - 1);
  return down.length ? down[0] : null;
}
