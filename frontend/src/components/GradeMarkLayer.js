// 作业原件上的标注层 —— 一张 SVG 叠在页图上，坐标 0~1000 直接吃，零换算。
//
// 几条刻意的设计（动之前先读）：
//
// ① **坐标不换算**。库里存的是「按页归一化 0~1000 的整数」（同 batch_items.figure_box），
//    viewBox="0 0 1000 1000" + preserveAspectRatio="none" 让 0~1000 线性铺满整张图 ——
//    窗口大小、图片缩放都不影响，也不需要知道页图真实像素尺寸。
//    ⚠️ 不要存 PDF 点坐标，也不要在这里按 naturalWidth 折算：两套坐标混在一列里迟早画到纸外。
//
// ② **一条标注一个 <g>**。点选、高亮、单删都是白拿的；
//    canvas 得自己管 dpr、重绘、命中测试（Notes.js 里那一大堆就是为此）。
//
// ③ **文字不用 <text> 画**。preserveAspectRatio="none" 在竖版页面上 x/y 缩放比例不同，
//    <text> 会被横向压扁（A4 页面下几乎读不出来）。文字改用 HTML 小标签按百分比定位：
//    位置还是 0~1000 那套，字号永远是正常字号。图形仍然走 SVG。
//
// ④ **老师批改三种手段都由这里产出**（2026-10-17 加的）：
//    画笔 ink（点列存 `points`，`geo` 只放包围盒）、橡皮（命中就 emit 要删的 id，父组件去真删）、
//    文字框 text（点一下给坐标，内容由父组件弹输入框收）。
//    另有 √ / × 快放：点一下页面直接放一个 check/cross —— 判对错是最高频的动作，
//    不该逼老师先画框再选形状。
//
// ⑤ **两种模式绝不能混**：view 模式标注可点（跳题）；ink/erase/text/check/cross 模式下
//    标注 `pointer-events:none`，否则笔一落就被某条标注吃掉，画不出线。
//    这个切换靠根节点上的 `mode-xxx` class，不靠 JS 逐个改。
//
// ⑥ **拖动只在 view 模式、且只在「框类」标注上生效**（2026-10-18 用户要求「文本框能自由拖动」）。
//    勾/叉/框/箭头/文字框都是 `geo` 四个数，平移就是四个数同加，一个 shiftGeo 全搞定；
//    **笔迹 ink 不参与**（点列几百个点，一来一回的 PATCH 太大，而且老师本来就不需要挪笔迹）。
//    拖动中的位移只存在本地的 `offsets` 里（跟着手走），松手才 PATCH 一次 ——
//    每动一帧发一个请求，SQLite 单写者会直接被拖垮。
//
// ⑦ **分割线模式（line）于 2026-10-19 加入**，与 region（框选）是同一件事的两种画法：
//    线切出来的是**整页宽**的段（`box = [0,y0,1000,y1]`），框选出来的是任意矩形段。
//    两者落到库里长一个样，所以后端一个字都没改 —— 这是「不重造轮子」的关键。
//    交互刻意分成两条路（抄的是批量录题的结论）：
//      · **落点离已有线 18 个单位以内 → 拖那条线**（老师画完总要微调）
//      · **否则 → 加一条新线**
//    删除**只走右侧那个 ×**，不靠「点在线附近就删」—— 那个设计在批量录题那边
//    被实际踩过：想加线却把刚画好的那条删了，非常难用。
//    ⚠️ line 模式下**段（region rect）不接收指针事件**：段是整页宽的，
//       收事件等于「页面任何地方都点不了新线」。段的删除走右栏面板。
import { computed, ref, watch } from 'vue';

import { UNIT, bboxOf, clampUnit as clamp, flatPoints, pagePoint, segDist, shiftGeo } from '../markGeom.js';

const COLORS = {
  box: '#dc2626',
  ellipse: '#dc2626',
  check: '#16a34a',
  cross: '#dc2626',
  arrow: '#2563eb',
  text: '#2563eb',
  ink: '#dc2626',
};

/** [x,y,x,y] 与 JSON 字符串两种形态都收（库里 json 列，接口给的是数组）。 */
function numList(v) {
  if (Array.isArray(v)) return v.map(Number).filter((n) => Number.isFinite(n));
  if (typeof v === 'string' && v.trim()) {
    try { return numList(JSON.parse(v)); } catch (e) { return []; }
  }
  return [];
}

/** 手绘点可能是 [x,y,x,y,...]，也可能是 [[x,y],...] —— 两种都认。 */
function pointList(v) {
  if (Array.isArray(v) && v.length && Array.isArray(v[0])) {
    return v.map((p) => [Number(p[0]), Number(p[1])])
      .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
  }
  const flat = numList(v);
  const out = [];
  for (let i = 0; i + 1 < flat.length; i += 2) out.push([flat[i], flat[i + 1]]);
  return out;
}

const pct = (n) => `${(clamp(n) / 10).toFixed(2)}%`;const r1 = (n) => Math.round(n * 10) / 10;

/** 一条标注 → 模板里要用的几何量。所有形状都由这几个数推出来，不在这里改数据。
 *
 *  `off` = 拖动中的实时位移 `{dx, dy}`（0~1000）。**只作用在原始 geo/points 上**，
 *  下面所有派生量（包围盒、勾叉折线、箭头、命中区、文字标签位置）自动跟着走，
 *  不需要逐个形状算一遍 —— 少一处漏算就少一个「拖了但只动了一半」的鬼 bug。 */
function buildShape(mark, off) {
  const dx = off ? off.dx : 0;
  const dy = off ? off.dy : 0;
  const moved = !!(dx || dy);
  const rawG = numList(mark.geo);
  const rawP = numList(mark.points);
  const g = moved && rawG.length ? shiftGeo(rawG, dx, dy) : rawG;
  const pts = pointList(moved && rawP.length ? shiftGeo(rawP, dx, dy) : mark.points);
  let ax = g.length >= 4 ? g[0] : 0;
  let ay = g.length >= 4 ? g[1] : 0;
  let bx = g.length >= 4 ? g[2] : 0;
  let by = g.length >= 4 ? g[3] : 0;
  if (g.length < 4 && pts.length) {
    // 只有手绘线、没有框：拿折线的包围盒当锚点（点选区域、文字标签都靠它）
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    ax = Math.min(...xs); bx = Math.max(...xs);
    ay = Math.min(...ys); by = Math.max(...ys);
  }

  const x = clamp(Math.min(ax, bx));
  const y = clamp(Math.min(ay, by));
  const w = clamp(Math.abs(bx - ax));
  const h = clamp(Math.abs(by - ay));
  const cx = clamp(x + w / 2);
  const cy = clamp(y + h / 2);

  const kind = mark.kind || 'box';
  const color = mark.color || COLORS[kind] || '#dc2626';

  // 勾/叉/文字框常常只给一个小框（甚至是 0 尺寸）。给个最小可视尺寸，
  // 否则一个 3px 的勾既看不见也点不着。
  const bw = Math.max(w, 30);
  const bh = Math.max(h, 30);
  const b0x = clamp(cx - bw / 2);
  const b0y = clamp(cy - bh / 2);

  // 箭头：方向取 geo 的两个角（不取规范化后的左上/右下），否则箭尖永远朝右下
  const ang = Math.atan2(by - ay, bx - ax) || 0;
  const HEAD = 26;
  const SPREAD = 0.45;
  const tip = [clamp(bx), clamp(by)];
  const h1 = [tip[0] - HEAD * Math.cos(ang - SPREAD), tip[1] - HEAD * Math.sin(ang - SPREAD)];
  const h2 = [tip[0] - HEAD * Math.cos(ang + SPREAD), tip[1] - HEAD * Math.sin(ang + SPREAD)];

  const hitW = Math.max(bw + 16, 40);
  const hitH = Math.max(bh + 16, 40);

  return {
    key: 'mk-' + (mark.id === undefined ? `${mark.page_no}-${cx}-${cy}` : mark.id),
    mark,
    itemId: mark.item_id === undefined ? null : mark.item_id,
    kind,
    color,
    // 0 = 这一笔没记粗细（0033 之前的老记录）→ 模板里不绑 style，走 CSS 默认
    width: Number(mark.width) || 0,
    text: mark.text || '',
    x, y, w, h, cx, cy,
    rect: { x: r1(x), y: r1(y), w: r1(w), h: r1(h) },
    line: { x1: clamp(ax), y1: clamp(ay), x2: clamp(bx), y2: clamp(by) },
    ellipse: { cx: r1(cx), cy: r1(cy), rx: r1(Math.max(bw / 2, 4)), ry: r1(Math.max(bh / 2, 4)) },
    // 勾：从左侧 30% 处下笔，转折在 45%，收在 85% 高处
    check: [
      [b0x + bw * 0.22, b0y + bh * 0.55],
      [b0x + bw * 0.43, b0y + bh * 0.80],
      [b0x + bw * 0.85, b0y + bh * 0.18],
    ].map((p) => `${r1(p[0])},${r1(p[1])}`).join(' '),
    cross: [
      [b0x + bw * 0.12, b0y + bh * 0.12, b0x + bw * 0.88, b0y + bh * 0.88],
      [b0x + bw * 0.88, b0y + bh * 0.12, b0x + bw * 0.12, b0y + bh * 0.88],
    ],
    head: `${r1(h1[0])},${r1(h1[1])} ${r1(tip[0])},${r1(tip[1])} ${r1(h2[0])},${r1(h2[1])}`,
    poly: pts.map((p) => `${r1(clamp(p[0]))},${r1(clamp(p[1]))}`).join(' '),
    dot: pts.length === 1 ? { cx: r1(clamp(pts[0][0])), cy: r1(clamp(pts[0][1])) } : null,
    hit: {
      x: r1(clamp(cx - hitW / 2)),
      y: r1(clamp(cy - hitH / 2)),
      w: r1(hitW),
      h: r1(hitH),
    },
    // 文本框的宽高也走这里：w/h 是 geo 矩形的尺寸（0~1000）。
    // 老的文字标注存的是零面积点（geo = [x,y,x,y]）→ w/h 都是 0 → 模板里不给宽高，
    // 退回 CSS 按内容自适应，老数据照旧能看，不会突然变成 0 宽的一条。
    label: { left: pct(x), top: pct(y), w: w > 0 ? pct(w) : '', h: h > 0 ? pct(h) : '',
             // 有宽高 = 新口径（矩形，top 就是左上角）→ 模板里去掉 translate(0,-100%)；
             // 零面积点 = 老口径（一个点、文字往上浮）→ 保留 translate，老标注观感不变。
             sized: w > 0 && h > 0 },
  };
}

/** 点到线段的距离由 ../markGeom.js 提供（唯一出处，可 node 单测）。 */

const ERASE_PAD = 18;      // 包围盒外扩多少还算点中
const ERASE_LINE = 22;     // 手绘线本身多粗算擦到（要大于笔画视觉宽度）
/* ⚠️ 抽稀阈值**不在这里**：它是屏幕像素（MIN_STEP_PX，见「采样」那段）。
   以前这里放过一个 MIN_STEP = 2.2，那是 0~1000 归一化单位 —— 页图越窄越丢点、
   x/y 比例不同还各向异性。留着只会让下一个人误以为是同一件事，已删。 */
/* 一笔最多存多少点。只防「涂满整页把 JSON 撑爆」这一件事。
   ⚠️ 到上限后必须**替换最后一个点**，不能像以前那样直接 return ——
      直接丢会让笔尖停在半路不动，正是用户报的「写着一半笔尖消失」。
      4000 点约 30KB，正常一笔到不了。 */
const MAX_POINTS = 4000;

/** 能拖的只有「四个数就说完」的框类标注。笔迹 ink 不在里面 —— 见文件头 ⑥。 */
const MOVE_KINDS = ['box', 'ellipse', 'check', 'cross', 'arrow', 'text'];
const DRAG_MIN = 4;        // 屏幕像素：手一抖几个像素不该被当成拖动（与月历那边同一个经验）

/** 「框题」的最小边长（0~1000 制，20 ≈ 纸面 2%）。
 *  **与后端 grade_store.REGION_MIN_SIDE 同值** —— 前端先挡一层，
 *  省得老师为一次手抖发一个请求再吃一个 422。改一处必须改两处。 */
const REGION_MIN = 20;

/** 分割线的「抓住」半径（0~1000 制，18 ≈ 纸面 1.8%）。
 *  比这更远就是「在空白处点一下 = 加一条新线」；近一点就是「拖这条线」。 */
const LINE_HIT = 18;

export default {
  name: 'GradeMarkLayer',
  props: {
    /** 只传**当前页**的标注 —— 过滤在父组件做，这里不管页码，免得两处各算一次。 */
    marks: { type: Array, default: () => [] },
    /** 当前选中的题目 id（item_id）。同题的标注一起高亮，老师能对上「这条框属于哪道题」。 */
    selectedItemId: { type: [String, Number], default: null },
    /** view | ink | erase | text | check | cross | region | line */
    mode: { type: String, default: 'view' },
    /** 画笔颜色（老师批改常用红，其次蓝）。 */
    color: { type: String, default: '#dc2626' },
    /** 笔迹粗细（屏幕像素；模板里配 `vector-effect: non-scaling-stroke`，
     *  所以页图放大缩小时粗细不变，跟手写时的观感一致）。
     *  ⚠️ `0` = 没指定 → **不输出** stroke-width 属性，走样式表默认值。
     *     绝不能输出 `stroke-width: 0` —— SVG 里那是「整条笔迹消失」。 */
    penWidth: { type: Number, default: 0 },
    /** 这一页已经画好的「一题一区域」框（{id, seq, label, box}）。**只传当前页**。 */
    regions: { type: Array, default: () => [] },
    /** 这一页的分割线 y 坐标（0~1000）。**只在 line 模式下显示与响应**。 */
    lines: { type: Array, default: () => [] },
    /** 这一页由线切出来的段（`[0,y0,1000,y1]`），只用于画淡色带 + 序号。 */
    bands: { type: Array, default: () => [] },
    /** 末段以下那一截（`[0,endY,1000,1000]`）：页脚带。**不进任何块、不送 AI**。
     *  没设结束线时是 null（末段一直切到页底，与历史行为一致）。 */
    tail: { type: Array, default: null },
  },
  emits: ['pick', 'create', 'erase', 'text-at', 'stamp', 'move', 'region', 'region-remove',
    'add-line', 'move-line', 'remove-line'],
  setup(props, { emit, expose }) {
    const svgEl = ref(null);
    const drawing = ref(false);
    const draft = ref([]);        // 正在画的那一笔（[[x,y],…]）
    const erasing = ref([]);      // 这次按住橡皮已经划掉的 id（松手才真删）
    /* 「框题」模式正在拖的那一个框（region 模式专用）。
     * 与 draft 分开：draft 是笔迹点列，regionDraft 是两个角点 —— 混在一起，
     * 判断「松手时该发 create 还是 region」就得靠 mode 二次分叉，很容易漏。 */
    const regionDraft = ref(null);   // { x0, y0, x1, y1 }
    /* 分割线模式的两个瞬态（line 模式专用）：
     *   lineDrag = 正在拖的那条线 {from, cur}（cur 跟着手走，松手才发出去）
     *   pending  = 还没决定「是点击还是拖动」的落点 y
     * 为什么不复用 regionDraft：线只有一个数（y），框有两个角点 ——
     * 硬塞进一个对象里，模板与 onUp 都要靠 mode 二次判断，是「漏一个分支」的温床。 */
    const lineDrag = ref(null);
    const pending = ref(null);

    /* ---- 拖动移动（只在 view 模式）----
     * offsets: { [markId]: { base, dx, dy } }
     *   base = 下手那一刻的 geo 快照。服务端把新 geo 回来后 base 就对不上了 →
     *   自动丢掉本地偏移，界面不闪、也不会「偏移算两次」跳到两倍远。 */
    const offsets = ref({});
    const dragState = ref(null);
    let suppressPickUntil = 0;    // 拖完松手会紧跟一个 click，别把它当成「点标注跳题」

    const geoKey = (g) => JSON.stringify(numList(g).map((n) => Math.round(n)));

    // 数据一换（settle / 切页 / 橡皮删掉），过期偏移必须退场。
    // ⚠️ 别只按 id 清：同一条标注可能只是被后端改了 geo（拖成功），此时也该清；
    //    所以比的是 base 快照，不是 id 在不在。
    watch(() => props.marks, () => {
      const cur = offsets.value;
      const keys = Object.keys(cur);
      if (!keys.length) return;
      const next = {};
      let kept = 0;
      for (const k of keys) {
        const m = (props.marks || []).find((x) => String(x.id) === k);
        if (m && geoKey(m.geo) === cur[k].base) { next[k] = cur[k]; kept += 1; }
      }
      if (kept !== keys.length) offsets.value = next;
    });

    const same = (a, b) => a !== null && a !== undefined && b !== null && b !== undefined
      && String(a) === String(b);

    /* 每帧只算一次。以前模板里调两遍（SVG 列表 + 文字标签），hitTest 里还要再调一遍，
       页上标注一多就是每帧 O(3 × 全页点数) 的字符串重建 —— 这是书写时「抖」的主因之一。
       computed 的依赖：props.marks（父组件整体换数组）+ offsets + resizing，任一变了才重算。 */
    const allShapes = computed(() => (props.marks || []).map((m) => {
      const ov = resizing.value[String(m.id)];
      // 调大小只换 geo（四个数），不叠加平移 —— 与拖动同一套渲染，但状态分开存
      return buildShape(ov ? Object.assign({}, m, { geo: ov }) : m, offsets.value[String(m.id)]);
    }));
    /** 兼容原来的调用姿势：传进来就是 props.marks 时直接用缓存，别的列表才现算。 */
    const shapesFor = (list) => (list === undefined || list === props.marks
      ? allShapes.value
      : (list || []).map((m) => buildShape(m, offsets.value[String(m.id)])));
    const isPicked = (s) => same(s.itemId, props.selectedItemId);
    const isLinked = (s) => s.itemId !== null && s.itemId !== undefined;
    const isGone = (s) => erasing.value.includes(String(s.mark.id));
    /* 哪些模式下已有的标注可以按住拖走。
     * `view` 是原来的口径；**text 也放开**（2026-10-20 用户：「我还需要文本框可以
     * 移动」）—— 文字模式下点空白处是新建，点在**已有文本框**上就是挪它，
     * 这是最朴素的直觉；不放开的话老师得先切回「只看」再拖回来，两次多余的点击。
     * ⚠️ 画笔/橡皮必须保持纯画/纯擦：那两个模式下按住标注是「落笔」，不是「抓东西」，
     *    混在一起会变成「想写一笔，结果把上一笔拖跑了」。 */
    const MOVE_MODES = ['view', 'text'];
    const canMove = (s) => MOVE_MODES.indexOf(props.mode) >= 0
      && MOVE_KINDS.includes(s.kind)
      && s.mark.id !== null && s.mark.id !== undefined;
    const isDragging = (s) => !!dragState.value && dragState.value.id === String(s.mark.id);

    function pick(s) {
      if (props.mode !== 'view') return;      // 画笔模式下点标注不该跳题
      if (!s) return;
      emit('pick', s.mark);
    }

    /** 单击跳题。刚拖完的那一下 click 要吞掉，否则「拖一下框」顺手把右栏跳到那一题。 */
    function onPickClick(s) {
      if (Date.now() < suppressPickUntil) return;
      pick(s);
    }

    /* ---------------- 拖动移动（文本框 / √× / 框 / 圆圈 / 箭头） ---------------- */
    function startDrag(e, s) {
      if (!canMove(s)) return;                       // 画笔模式下是画，不是拖
      if (e.pointerType === 'mouse' && e.button !== 0) return;   // 只认左键
      e.preventDefault();                            // 别让浏览器顺手选中文字
      e.stopPropagation();                           // 也别掉进 svg 的画/擦逻辑
      dragState.value = {
        id: String(s.mark.id), mark: s.mark,
        sx: e.clientX, sy: e.clientY, moved: false,
      };
      const el = e.currentTarget;
      if (el && el.setPointerCapture) {
        try { el.setPointerCapture(e.pointerId); } catch (err) { /* 老浏览器 */ }
      }
    }

    function dragTo(e) {
      const st = dragState.value;
      if (!st) return;
      const el = svgEl.value;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const px = e.clientX - st.sx;
      const py = e.clientY - st.sy;
      // 没过阈值就先不动：老师本来只想点一下，手抖两个像素不该把框挪走
      if (!st.moved && Math.hypot(px, py) < DRAG_MIN) return;
      st.moved = true;
      const prev = offsets.value[st.id];
      const next = Object.assign({}, offsets.value);
      next[st.id] = {
        base: prev ? prev.base : geoKey(st.mark.geo),
        dx: px / rect.width * UNIT,      // x 用宽、y 用高**分别**换算（同 pagePoint 口径）
        dy: py / rect.height * UNIT,
      };
      offsets.value = next;
    }

    function endDrag(e) {
      const st = dragState.value;
      if (!st) return;
      dragState.value = null;
      const el = e && e.currentTarget;
      if (el && el.releasePointerCapture && e.pointerId !== undefined) {
        try { el.releasePointerCapture(e.pointerId); } catch (err) { /* 同上 */ }
      }

      if (!st.moved) {
        // 没动 = 单击。pick 在这里发（pointerdown 里 preventDefault 过，click 未必来）；
        // 万一 click 也来了，就是同一个标注再 pick 一次，幂等无害 —— 不用时间戳去挡，
        // 挡了反而会留 400ms 的「点别的标注没反应」盲区。
        pick(st.mark);
        return;
      }
      suppressPickUntil = Date.now() + 400;   // 真拖过：紧跟的 click 不该把右栏跳走

      const off = offsets.value[st.id];
      if (!off) return;
      const geo = shiftGeo(numList(st.mark.geo), off.dx, off.dy);
      if (geo.length < 4) return;      // 没有可用 geo 的标注（理论上进不来）
      emit('move', { mark: st.mark, geo });   // 一次松手只发一个 PATCH
    }

    function cancelDrag() {
      const st = dragState.value;
      if (!st) return;
      dragState.value = null;
      const next = Object.assign({}, offsets.value);
      delete next[st.id];              // 被打断（手势被系统收走）就退回原位
      offsets.value = next;
    }

    /** 父组件提交失败时把界面退回去 —— 不能停在「看着拖走了、库里没变」。 */
    function clearOffsets() {
      offsets.value = {};
      resizing.value = {};      // 调大小的临时尺寸一起退，否则会停在「看着变大了、库里没变」
    }

    /* ---------------- 调文本框大小（2026-10-27：文本框要能自由调宽高） ----------------
       与「拖动位置」是两套状态：拖动是平移（offsets 存 dx/dy），调大小是改 geo 的右下角。
       两者松手都 emit('move')，父组件复用同一条 PATCH（geo 是同一个字段）——
       所以后端、接口、表结构零改动，`homework_grade_marks.geo` 本来就是 4 个 0~1000 整数。
       ⚠️ 宽高存在 geo 里，**不能借 width 字段**：那个字段是笔迹粗细（grade_store 里
          clamp 0~200），混用会让笔迹粗细跟着一起坏。 */
    const resizing = ref({});     // { [id]: [x1,y1,x2,y2] } 拖动中的实时 geo
    let resizeState = null;       // { id, mark, x1, y1 }

    /** 只有「已入库的文本框」能调大小；画笔/橡皮模式下不给（那两个模式按下去是落笔）。 */
    const canResize = (s) => s.kind === 'text'
      && s.mark.id !== null && s.mark.id !== undefined
      && !isGone(s)
      && MOVE_MODES.indexOf(props.mode) >= 0;

    function startResize(e, s) {
      if (!canResize(s)) return;
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      const g = numList(s.mark.geo);
      const x1 = g.length >= 4 ? Math.min(g[0], g[2]) : 0;
      const y1 = g.length >= 4 ? Math.min(g[1], g[3]) : 0;
      resizeState = { id: String(s.mark.id), mark: s.mark, x1, y1 };
      const el = e.currentTarget;
      if (el && el.setPointerCapture) {
        try { el.setPointerCapture(e.pointerId); } catch (err) { /* 老浏览器 */ }
      }
    }

    function resizeTo(e) {
      const st = resizeState;
      if (!st) return;
      const pt = toPt(e);
      if (!pt) return;
      // 右下角不许越过左上角；最小 40x30 单位（约页宽 4%、页高 3%），再小就读不了了
      const x2 = clamp(Math.max(pt[0], st.x1 + 40));
      const y2 = clamp(Math.max(pt[1], st.y1 + 30));
      const next = Object.assign({}, resizing.value);
      next[st.id] = [Math.round(st.x1), Math.round(st.y1), Math.round(x2), Math.round(y2)];
      resizing.value = next;
    }

    function endResize(e) {
      const st = resizeState;
      if (!st) return;
      resizeState = null;
      const el = e && e.currentTarget;
      if (el && el.releasePointerCapture && e.pointerId !== undefined) {
        try { el.releasePointerCapture(e.pointerId); } catch (err) { /* 同上 */ }
      }
      const geo = resizing.value[st.id];
      if (!geo) return;
      suppressPickUntil = Date.now() + 400;   // 拖完那一下的 click 不该把右栏跳走
      emit('move', { mark: st.mark, geo });   // 一次松手只发一个 PATCH
    }

    function cancelResize() {
      if (!resizeState) return;
      const id = resizeState.id;
      resizeState = null;
      const next = Object.assign({}, resizing.value);
      delete next[id];                 // 被打断就退回原尺寸
      resizing.value = next;
    }

    /** 屏幕坐标 → 0~1000 的页坐标。换算本身在 ../markGeom.js（那边有 node 单测）。 */
    function toPt(e) {
      const el = svgEl.value;
      if (!el) return null;
      return pagePoint(e.clientX, e.clientY, el.getBoundingClientRect());
    }

    /** 找点中的那条标注。倒着找（后画的盖在上面），手绘线按线段距离算，别用包围盒糊弄。 */
    function hitTest(px, py) {
      const list = shapesFor(props.marks).filter((s) => !isGone(s));
      for (let i = list.length - 1; i >= 0; i--) {
        const s = list[i];
        const pts = pointList(s.mark.points);
        if ((s.kind === 'ink' || pts.length > 1) && pts.length > 1) {
          for (let j = 0; j + 1 < pts.length; j += 1) {
            if (segDist(px, py, pts[j][0], pts[j][1], pts[j + 1][0], pts[j + 1][1]) <= ERASE_LINE) {
              return s.mark;
            }
          }
        }
        const h = s.hit;
        if (px >= h.x - ERASE_PAD && px <= h.x + h.w + ERASE_PAD
            && py >= h.y - ERASE_PAD && py <= h.y + h.h + ERASE_PAD) {
          return s.mark;
        }
      }
      return null;
    }

    function markErased(m) {
      const id = String(m.id);
      if (erasing.value.includes(id)) return;    // 同一条别重复发（拖动会一直命中）
      erasing.value = erasing.value.concat([id]);
    }

    /* ---------------- 采样：合帧 + 取中间点（第 2 条「笔尖不消失、不闪烁」） ----------------
       三件事，缺一件手感就变差：
       ① requestAnimationFrame 合帧 —— 120Hz 笔 / 高回报率鼠标一帧能来 4~8 次 pointermove，
          每次都改响应式 draft 就等于每次都整层重渲染。攒到下一帧一次写完。
       ② getCoalescedEvents —— 浏览器会把一帧内的多个点合并成一个事件，不主动取中间点，
          快写时笔迹就被切成折线弦（看着「不跟手」）。
       ③ 抽稀按**屏幕像素**（MIN_STEP_PX），不再用 0~1000 的归一化单位 ——
          后者在 A4 页上 x/y 比例不同 → 各向异性，页图渲染得越大丢点越狠。 */
    const MIN_STEP_PX = 1.6;         // 屏幕像素；比这更近的点不记
    const raf = typeof requestAnimationFrame === 'function'
      ? requestAnimationFrame : (fn) => setTimeout(fn, 16);
    const caf = typeof cancelAnimationFrame === 'function'
      ? cancelAnimationFrame : (id) => clearTimeout(id);
    let rafId = 0;
    let pendingPts = [];
    let inkLastPx = null;

    /** 把攒着的点一次写进 draft。到上限后**替换最后一个点**，保证笔尖继续跟手。 */
    function flushDraft() {
      rafId = 0;
      if (!pendingPts.length) return;
      const add = pendingPts;
      pendingPts = [];
      let cur = draft.value;
      for (let i = 0; i < add.length; i += 1) {
        cur = cur.length >= MAX_POINTS ? cur.slice(0, -1).concat([add[i]]) : cur.concat([add[i]]);
      }
      draft.value = cur;
    }

    /** 收笔 / 被打断时清干净：没刷出去的点一并丢掉（被打断的那一笔本来就不该提交）。 */
    function resetInk() {
      if (rafId) { caf(rafId); rafId = 0; }
      pendingPts = [];
      inkLastPx = null;
    }

    function onDown(e) {
      if (props.mode === 'view') return;
      const pt = toPt(e);
      if (!pt) return;
      e.preventDefault();
      const [x, y] = pt;

      if (props.mode === 'ink') {
        drawing.value = true;
        resetInk();                            // 兜底：万一上一笔被异常收尾，残留点不许漏进这一笔
        draft.value = [[x, y]];
        inkLastPx = [e.clientX, e.clientY];   // 抽稀的像素基准（client 差值就是屏幕像素差）
        try { svgEl.value.setPointerCapture(e.pointerId); } catch (err) { /* 老浏览器 */ }
        return;
      }
      if (props.mode === 'erase') {
        drawing.value = true;
        try { svgEl.value.setPointerCapture(e.pointerId); } catch (err) { /* 同上 */ }
        const m = hitTest(x, y);
        if (m) markErased(m);
        return;
      }
      if (props.mode === 'text') {
        emit('text-at', { x, y });    // 事件名 kebab-case：模板里 @text-at 与它严格对应
        return;
      }
      if (props.mode === 'check' || props.mode === 'cross') {
        // 快放一个 √ / × ：中心落在点击处，边长 44 单位（约页宽 4.4%，够醒目又不占地）
        const half = 22;
        emit('stamp', {
          kind: props.mode,
          geo: [x - half, y - half, x + half, y + half],
          color: props.mode === 'check' ? '#16a34a' : '#dc2626',
        });
        return;
      }
      if (props.mode === 'region') {
        // 拖出一个框。**必须 setPointerCapture**：老师常在页图边缘起手，
        // 不捕获的话指针一移出 svg 就收不到 pointermove，框会停在半路。
        drawing.value = true;
        regionDraft.value = { x0: x, y0: y, x1: x, y1: y };
        try { svgEl.value.setPointerCapture(e.pointerId); } catch (err) { /* 老浏览器 */ }
        return;
      }
      if (props.mode === 'line') {
        drawing.value = true;
        try { svgEl.value.setPointerCapture(e.pointerId); } catch (err) { /* 同上 */ }
        const near = nearestLine(y);
        if (near !== null) {
          // 落在已有线附近 → 拖它（老师画完总要微调一两个点）
          lineDrag.value = { from: near, cur: near };
          pending.value = null;
        } else {
          // 空白处落手 → 先记下，松手才决定要不要加（这样按住往下蹭也不会加两条）
          pending.value = y;
          lineDrag.value = null;
        }
      }
    }

    /** 离 y 最近的那条线（在 LINE_HIT 以内）。没有就 null。 */
    function nearestLine(y) {
      let hit = null;
      let best = LINE_HIT;
      (props.lines || []).forEach((ly) => {
        const v = Number(ly);
        if (!Number.isFinite(v)) return;
        const d = Math.abs(v - y);
        if (d <= best) { best = d; hit = v; }
      });
      return hit;
    }

    function onMove(e) {
      if (!drawing.value) return;

      if (props.mode === 'ink') {
        // ① 取出被浏览器合并掉的中间点（老浏览器没这个 API 就退化成单点）
        const raw = typeof e.getCoalescedEvents === 'function' ? e.getCoalescedEvents() : null;
        const list = raw && raw.length ? raw : [e];
        const el = svgEl.value;
        if (!el) return;
        // rect 一次算完整批共用 —— 原来每个点各算一次 getBoundingClientRect
        const rect = el.getBoundingClientRect();
        if (!rect.width || !rect.height) return;

        const fresh = [];
        for (let i = 0; i < list.length; i += 1) {
          const ev = list[i];
          // ② 抽稀按屏幕像素判（各向同性的几何距离），不再用归一化单位
          if (inkLastPx && Math.hypot(ev.clientX - inkLastPx[0], ev.clientY - inkLastPx[1]) < MIN_STEP_PX) continue;
          inkLastPx = [ev.clientX, ev.clientY];
          fresh.push([
            clamp((ev.clientX - rect.left) / rect.width * UNIT),
            clamp((ev.clientY - rect.top) / rect.height * UNIT),
          ]);
        }
        if (!fresh.length) return;
        pendingPts = pendingPts.concat(fresh);
        // ③ 合帧：一帧里来 8 次 move 也只改一次响应式 draft
        if (!rafId) rafId = raf(flushDraft);
        return;
      }

      const pt = toPt(e);
      if (!pt) return;
      const [x, y] = pt;

      if (props.mode === 'erase') {
        const m = hitTest(x, y);
        if (m) markErased(m);
        return;
      }
      if (props.mode === 'region' && regionDraft.value) {
        regionDraft.value = { ...regionDraft.value, x1: x, y1: y };
        return;
      }
      if (props.mode === 'line') {
        // 拖线：cur 跟着手走（只改本地，松手才 emit）。按在空白处时也允许
        // 上下蹭一点再松手 —— 那条线就放在蹭完的位置，比「点歪了删掉重画」省事。
        if (lineDrag.value) lineDrag.value = { ...lineDrag.value, cur: y };
        else if (pending.value !== null) pending.value = y;
      }
    }

    /** 松手时的框 → 规整成 [x0,y0,x1,y1]（左上到右下）。太小就不算数。 */
    function draftBox() {
      const d = regionDraft.value;
      if (!d) return null;
      const x0 = Math.min(d.x0, d.x1), x1 = Math.max(d.x0, d.x1);
      const y0 = Math.min(d.y0, d.y1), y1 = Math.max(d.y0, d.y1);
      if (x1 - x0 < REGION_MIN || y1 - y0 < REGION_MIN) return null;
      return [x0, y0, x1, y1].map((v) => Math.round(clamp(v)));
    }

    function onUp() {
      if (!drawing.value) return;
      drawing.value = false;

      // 合帧是有延迟的：松手那一刻，最后一帧攒下的点还躺在 pendingPts 里没进 draft。
      // **必须同步刷完再提交** —— 直接清掉的话，每一笔的尾巴都缺一小截。
      if (props.mode === 'ink') flushDraft();

      if (props.mode === 'region') {
        const box = draftBox();
        regionDraft.value = null;
        // 太小 = 手抖点了一下，不当作画框（与后端 REGION_MIN_SIDE 同口径，
        // 前端先挡一层，省得每次都吃一个 422）
        if (box) emit('region', box);
        return;
      }

      if (props.mode === 'line') {
        const drag = lineDrag.value;
        const pend = pending.value;
        lineDrag.value = null;
        pending.value = null;
        if (drag) {
          const to = Math.round(clamp(drag.cur));
          // 没挪动 = 只是点了一下这条线。**什么都不做**（删除走右侧 ×）——
          // 批量录题那边试过「点在附近就删」，实际用起来是「想加线把已有的删了」。
          if (Math.abs(to - drag.from) >= 1) emit('move-line', { from: drag.from, to });
          return;
        }
        if (pend !== null) emit('add-line', Math.round(clamp(pend)));
        return;
      }

      if (props.mode === 'ink' && draft.value.length >= 2) {
        const pts = draft.value;
        emit('create', {
          kind: 'ink',
          points: flatPoints(pts),        // [x,y,x,y,…]，与库里 _norm_geo 同一形态
          geo: bboxOf(pts),               // 包围盒只用于命中/统计，形状靠 points
          color: props.color,
          // 粗细与颜色同一类东西（「这一笔长什么样」），跟着笔迹一起存，
          // 换设备/改样式都不会让历史笔迹的观感变样
          width: props.penWidth || 0,
        });
      }
      draft.value = [];
      // 采样状态与 draft 一起清。**inkLastPx 一定要清**：留着的话，下一笔的第一点
      // 若落在上一笔收笔处附近（老师连着写两个字很常见）会被判成「太近」直接丢掉，
      // 表现就是「新起笔前 2 个像素不显形」。resetInk 只碰这几个本地变量，别的模式调也安全。
      resetInk();

      if (props.mode === 'erase' && erasing.value.length) {
        const ids = erasing.value.slice();
        erasing.value = [];
        emit('erase', ids);      // 一次松手只发一个请求（父组件串行删）
      }
    }

    /** 指针被系统收走（掌托误触、系统手势、切窗口、来电）：**什么都不提交**。
     *
     *  以前模板里 @pointercancel 直接复用 onUp —— 等于把「被打断」当成「写完了」：
     *  正在写的那一笔会被当成完整的一笔存进去，正在擦的那几条会被真的删掉。
     *  用户报的「笔尖消失」有一部分就是它：掌托一碰，半条笔迹被提前收走。
     *  被打断的正确语义是「这一笔没写完，丢掉」—— 与拖动那边的 cancelDrag 同一条原则。 */
    function onCancel() {
      if (!drawing.value) return;
      drawing.value = false;
      // 顺序要紧：先掐掉待执行的 rAF，再清 draft。
      // 反过来的话 rAF 在下一帧照样跑，把 pendingPts 又写回 draft —— 界面上会冒出
      // 一条「已经被判定为丢弃」的残留笔迹（下一笔才开始写它才消失）。
      resetInk();
      draft.value = [];
      erasing.value = [];
      regionDraft.value = null;
      lineDrag.value = null;
      pending.value = null;
    }

    expose({ clearOffsets });   // 父组件 PATCH 失败时调它，界面退回原位

    /** 分割线：当前显示用的列表（拖动中的那条用 cur 替代 from，才跟手）。 */
    const lineList = () => (props.lines || [])
      .map((v) => Number(v))
      .filter((v) => Number.isFinite(v))
      .map((v) => {
        const cur = lineDrag.value && lineDrag.value.from === v ? lineDrag.value.cur : v;
        return { y: Math.round(clamp(cur)), from: v, lx: '0%', ly: pct(cur) };
      })
      .sort((a, b) => a.y - b.y);

    /** 段落带 + 序号：让老师一眼数出「这一页切成了几题」。**纯展示，不收事件**。 */
    const bandList = () => (props.bands || []).map((b) => {
      const v = numList(b);
      if (v.length < 4) return null;
      const y0 = clamp(Math.min(v[1], v[3]));
      const y1 = clamp(Math.max(v[1], v[3]));
      return { y: r1(y0), h: r1(y1 - y0), ly: pct(y0) };
    }).filter(Boolean);

    /** 拖线时右上角那个读数（「精度得让人看得见」，与批量录题同一个理由）。 */
    const lineTip = () => {
      const d = lineDrag.value;
      if (!d) return null;
      return { y: Math.round(clamp(d.cur)), ly: pct(d.cur) };
    };

    return {
      svgEl, drawing, draft, erasing, regionDraft, lineDrag, pending,
      shapesFor, isPicked, isLinked, isGone, canMove, isDragging, pick, onPickClick,
      onDown, onMove, onUp, onCancel, startDrag, dragTo, endDrag, cancelDrag,
      canResize, startResize, resizeTo, endResize, cancelResize,
      lineList, bandList, lineTip,
      draftPoly: (pts) => pts.map((p) => `${r1(p[0])},${r1(p[1])}`).join(' '),
      /** 框 → 画图要用的四个数 + 标签位置。**永不返回 null**（模板里直接取属性）。 */
      regionRect: (box) => {
        const b = numList(box);
        if (b.length < 4) return { x: 0, y: 0, w: 0, h: 0, lx: '0%', ly: '0%' };
        const x = Math.min(b[0], b[2]);
        const y = Math.min(b[1], b[3]);
        return {
          x: r1(x), y: r1(y),
          w: r1(Math.abs(b[2] - b[0])), h: r1(Math.abs(b[3] - b[1])),
          lx: pct(x), ly: pct(y),
        };
      },
      draftRect: (d) => ({
        x: r1(Math.min(d.x0, d.x1)), y: r1(Math.min(d.y0, d.y1)),
        w: r1(Math.abs(d.x1 - d.x0)), h: r1(Math.abs(d.y1 - d.y0)),
      }),
    };
  },
  template: `
  <div class="gmark-layer" :class="'mode-' + mode">
    <!-- ⚠️ 0~1000 就是库里的坐标，preserveAspectRatio="none" 让它线性铺满页图。
         根节点默认 pointer-events:none，只有「标注」在 view 模式下可点（空白处穿透到图片）；
         画笔/橡皮/文字模式下由 CSS 反向打开整层的事件，同时让标注失去指针。 -->
    <svg class="gmark-svg" ref="svgEl" viewBox="0 0 1000 1000" preserveAspectRatio="none"
         @pointerdown="onDown" @pointermove="onMove" @pointerup="onUp" @pointercancel="onCancel">
      <!-- 一题一区域：**只在「框题」与「分割线」模式下显示**。平时也画出来的话
           整页都是虚线框，老师就看不 clean 自己批到哪儿了。
           line 模式下这些框只作参考（不收事件，见 app.css 的 .mode-line 规则）。 -->
      <g v-if="mode === 'region' || mode === 'line'" class="gmark-regions">
        <!-- ⚠️ key 里**必须带下标兜底**。刚画出来还没保存的框 id 是 null（父组件只给了
             { id: null, q_no, box }），只写 'rg-' + r.id 的话连画三个 →
             三个 'rg-null' 撞在一起。Vue 的 keyed diff 靠 key 找回节点，撞了就**删中间那个会删错格子**，
             而界面上看着仍然「有这么多框」，谁也发现不了。
             ⚠️ 这段注释在模板字面量里，**别写反引号**（会把模板提前截断，整文件语法错）。 -->
        <g v-for="(r, i) in regions" :key="'rg-' + (r.id == null ? 'new' + i : r.id)" class="gmark-region"
           @pointerdown.stop @click.stop="$emit('region-remove', r)">
          <rect :x="regionRect(r.box).x" :y="regionRect(r.box).y"
                :width="regionRect(r.box).w" :height="regionRect(r.box).h" class="rg-rect" />
        </g>
        <rect v-if="regionDraft" :x="draftRect(regionDraft).x" :y="draftRect(regionDraft).y"
              :width="draftRect(regionDraft).w" :height="draftRect(regionDraft).h" class="rg-draft" />
      </g>
      <!-- 分割线：段带（淡）+ 线本身。**整组不收指针事件**（段是整页宽的，
           收事件就等于「页面任何地方都点不了新线」）。线的拖动由 svg 根节点统一处理。 -->
      <g v-if="mode === 'line'" class="gl-layer">
        <!-- 末段以下（页脚带）：**明确画出来**，表示这一截不进任何块、不送 AI。
             没有它的话老师只会看到「最后一段到 96%」，说不清下面那 4% 去哪了。 -->
        <rect v-if="tail" class="gl-tail" x="0" :y="tail[1]" width="1000"
              :height="Math.max(1, tail[3] - tail[1])" />
        <rect v-for="(b, i) in bandList()" :key="'glb-' + i" class="gl-band"
              x="0" :y="b.y" width="1000" :height="b.h" />
        <line v-for="(l, i) in lineList()" :key="'gll-' + i" class="gl-line"
              x1="0" :y1="l.y" x2="1000" :y2="l.y" />
      </g>
      <g v-for="s in shapesFor(marks).filter(x => !isGone(x))" :key="s.key" class="gmark"
         :class="{ sel: isPicked(s), linked: isLinked(s), rough: s.mark.created_by !== 'teacher',
                   mv: canMove(s), drag: isDragging(s) }"
         @pointerdown="startDrag($event, s)" @pointermove="dragTo"
         @pointerup="endDrag" @pointercancel="cancelDrag"
         @click.stop="onPickClick(s)">
        <!-- 透明命中区：细线/小勾也得点得着 -->
        <rect class="gmark-hit" :x="s.hit.x" :y="s.hit.y" :width="s.hit.w" :height="s.hit.h" />

        <rect v-if="s.kind === 'box'" class="shape" :x="s.rect.x" :y="s.rect.y"
              :width="s.rect.w" :height="s.rect.h" :stroke="s.color" fill="none" />

        <ellipse v-else-if="s.kind === 'ellipse'" class="shape"
                 :cx="s.ellipse.cx" :cy="s.ellipse.cy"
                 :rx="s.ellipse.rx" :ry="s.ellipse.ry" :stroke="s.color" fill="none" />

        <polyline v-else-if="s.kind === 'check'" class="shape" :points="s.check"
                  :stroke="s.color" fill="none" />

        <template v-else-if="s.kind === 'cross'">
          <line v-for="(c, i) in s.cross" :key="'x-' + s.key + '-' + i" class="shape"
                :x1="c[0]" :y1="c[1]" :x2="c[2]" :y2="c[3]" :stroke="s.color" />
        </template>

        <template v-else-if="s.kind === 'arrow'">
          <line class="shape" :x1="s.line.x1" :y1="s.line.y1"
                :x2="s.line.x2" :y2="s.line.y2" :stroke="s.color" />
          <polygon class="shape" :points="s.head" :fill="s.color" :stroke="s.color" />
        </template>

        <!-- 手绘折线（老师红笔 / AI 划的线）：和框可以同时存在。
             ink 是老师真笔迹，画粗一点、圆头圆角，看着才像笔。 -->
        <!-- ⚠️ 粗细走 **inline style** 而不是 stroke-width 属性：
             SVG 的呈现属性优先级**低于**样式表，样式表里那条 stroke-width: 2.2
             会把它盖掉（写属性等于没写，而且不报错）。inline style 才盖得住。
             ⚠️⚠️ 这个文件整体是一个模板字面量：注释里**不许出现反引号**，
                 出现一个就把模板从那儿截断，整个组件加载失败（marksReady=false）。
                 2026-10-21 在这条注释上踩过一次。 -->
        <polyline v-if="s.poly" class="shape" :class="{ ink: s.kind === 'ink' }"
                  :points="s.poly" :stroke="s.color"
                  :style="s.width > 0 ? { strokeWidth: s.width + 'px' } : null"
                  fill="none" stroke-linecap="round" stroke-linejoin="round" />
        <circle v-if="s.dot" class="shape" :cx="s.dot.cx" :cy="s.dot.cy" r="7"
                :stroke="s.color" fill="none" />
      </g>

      <!-- 正在画的这一笔：不进库、不落后端，松手才提交（拖到一半网断了不至于留半条） -->
      <!-- ⚠️ 判据是 >= 1 而不是 > 1：落笔的第一个点就要画出来（一个点靠 linecap:round 显示成圆点）。
           写成 > 1 的话，按下到第一次移动之间屏幕上是空的 —— 这就是「笔尖消失」最直接的一条。 -->
      <polyline v-if="draft.length >= 1" class="gmark-draft" :points="draftPoly(draft)"
                :stroke="color" :style="penWidth > 0 ? { strokeWidth: penWidth + 'px' } : null"
                fill="none" stroke-linecap="round" stroke-linejoin="round" />
    </svg>

    <!-- 文字标注用 HTML 定位：SVG 的 <text> 在非等比 viewBox 里会被压扁。
         「选择」模式下可以直接按住拖走（父组件松手时 PATCH geo）。 -->
    <span v-for="s in shapesFor(marks).filter(x => x.text)" :key="'tx-' + s.key"
          class="gmark-label"
          :class="{ sel: isPicked(s), linked: isLinked(s), drag: isDragging(s), sized: s.label.sized }"
          :style="{ left: s.label.left, top: s.label.top, borderColor: s.color, color: s.color,
                    width: s.label.w, height: s.label.h }"
          @pointerdown="startDrag($event, s)" @pointermove="dragTo"
          @pointerup="endDrag" @pointercancel="cancelDrag"
          @click.stop="onPickClick(s)">{{ s.text
          }}<i v-if="canResize(s)" class="gmark-resize" title="拖动调整文本框大小"
             @pointerdown.stop="startResize($event, s)"
             @pointermove.stop="resizeTo"
             @pointerup.stop="endResize"
             @pointercancel.stop="cancelResize"></i></span>

    <!-- 分割线：段序号 + 线上的 ×。
         ⚠️ 点线本身**不删**（只拖），删除只走这个 × —— 见文件头 ⑦。 -->
    <template v-if="mode === 'line'">
      <span v-for="(b, i) in bandList()" :key="'glbn-' + i" class="gl-band-no"
            :style="{ top: b.ly }">{{ i + 1 }}</span>
      <span v-for="(l, i) in lineList()" :key="'gllx-' + i" class="gl-line-x"
            :style="{ top: l.ly }" title="删掉这条分割线"
            @click.stop="$emit('remove-line', l.from)">×</span>
      <span v-if="lineTip()" class="gl-tip" :style="{ top: lineTip().ly }">
        {{ lineTip().y }}
      </span>
    </template>

    <!-- 框的编号：写「3 ×」，点一下就删（删框走列表也行，这里图省事）。
         key 同样要有下标兜底 —— 理由见上面 gmark-regions 那段。 -->
    <span v-for="(r, i) in (mode === 'region' ? regions : [])"
          :key="'rgl-' + (r.id == null ? 'new' + i : r.id)"
          class="gmark-rlabel" :style="{ left: regionRect(r.box).lx, top: regionRect(r.box).ly }"
          @click.stop="$emit('region-remove', r)" :title="'点一下删掉这个框'">{{ r.label }} ×</span>
  </div>`,
};
