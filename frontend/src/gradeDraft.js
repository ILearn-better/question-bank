// 批改台的**本地草稿** —— 专治「画到一半退出，全没了」。
//
// 用户 2026-10-19 原话：「我用笔画批改作业时，不小心退出，记录就没了，得重新批改」。
//
// 先厘清到底哪些东西会丢（不查清就动手，很容易只堵住一个口子）：
//
//   ① **笔迹**  —— 每画一笔就 POST `/marks`。看着像已经存了，其实有两个缝：
//      画完立刻关页面/切页，请求还在路上就被浏览器取消；请求失败时只弹一个
//      toast，下一轮 `settle()` 一刷，界面上那一笔就消失了（老师多半没看见 toast）。
//   ② **框题草稿**（`editAll` / `editLines`）—— 纯内存，**点「保存框」之前退出必丢**。
//      分割线一页七八刀，重画一遍是实打实的几分钟。
//   ③ **判定依据的文字**（答案 / 评分细则）—— 也是内存，没点「保存依据」就退出必丢。
//
// 所以这个模块存三样东西，全部挂在一个 key 上（按 gid 分开，不同作业互不干扰）：
//
//   { v, at, page_no, marks: [...], regions: {...}, setup: {...} }
//
// ★ 三条硬约束（改之前先读）
//
//   1. **永不抛异常**。localStorage 会因隐私模式、配额满、被浏览器禁用而直接抛。
//      草稿是「锦上添花」—— 它坏了绝不能让批改页白屏（那是把小事变成大事）。
//   2. **写要防抖**。画笔一笔、拖一个框都会触发保存；不防抖就是每帧一次
//      JSON.stringify + 一次同步磁盘写，画起来会明显卡。
//   3. **丢了要比没有强，但不能悄悄丢**。配额满时先砍最老的笔迹（并留一个
//      `trimmed` 计数），让调用方有机会说一句「有 N 笔太久没传上去，已丢弃」。
//
// ⚠️ 这里**不做跨设备同步**，也不替代服务端：它是「还没送到的那些」的暂存区，
//    不是真相。真相永远是 `/homework-grades/{gid}` 的返回。
const VERSION = 1;

/** 草稿最多保留几天。超过就丢 —— 一份作业放一周没人管，多半是已经删了/批完了。 */
export const KEEP_DAYS = 7;

/** 队列里的笔迹上限。一条 ink 最多 900 个点 ≈ 4.5KB，200 条 ≈ 900KB，
 *  在常见的 5MB 配额里留足了余量（框题草稿与判定依据还要占地方）。 */
export const MAX_MARKS = 200;

const PREFIX = 'shike:grade-draft:';

let seq = 0;
const nextLid = () => {
  seq += 1;
  return `${Date.now().toString(36)}-${seq}`;
};

/** localStorage 可能**根本不存在**（node 测试、隐私模式）。统一在这里收口。 */
function store() {
  try {
    const s = globalThis.localStorage;
    if (!s || typeof s.getItem !== 'function') return null;
    return s;
  } catch (e) {
    return null;        // 访问 localStorage 本身就可能抛（部分浏览器的第三方上下文）
  }
}

export function draftKey(gid) {
  return PREFIX + String(gid);
}

function emptyDraft() {
  return { v: VERSION, at: '', page_no: 1, marks: [], regions: null, setup: null, trimmed: 0 };
}

/** 读草稿。**永不抛**；坏数据（手改过、旧版本、别的项目同名 key）一律当没有。 */
export function readDraft(gid, now = Date.now()) {
  const s = store();
  if (!s) return emptyDraft();
  let raw = null;
  try {
    raw = s.getItem(draftKey(gid));
  } catch (e) {
    return emptyDraft();
  }
  if (!raw) return emptyDraft();
  let obj = null;
  try {
    obj = JSON.parse(raw);
  } catch (e) {
    return emptyDraft();
  }
  if (!obj || typeof obj !== 'object' || Number(obj.v) !== VERSION) return emptyDraft();

  // 过期：at 坏了也当过期（宁可丢一份草稿，也不要让一份上周的框莫名出现在今天的卷面上）
  const at = Date.parse(obj.at || '');
  if (!Number.isFinite(at) || now - at > KEEP_DAYS * 86400e3) {
    removeDraft(gid);
    return emptyDraft();
  }

  const out = emptyDraft();
  out.at = obj.at || '';
  out.page_no = Math.max(1, Number(obj.page_no) || 1);
  out.trimmed = Math.max(0, Number(obj.trimmed) || 0);
  out.marks = (Array.isArray(obj.marks) ? obj.marks : [])
    .filter((m) => m && m.lid && typeof m === 'object')
    .map((m) => ({
      lid: String(m.lid),
      page_no: Math.max(1, Number(m.page_no) || 1),
      kind: String(m.kind || 'ink'),
      geo: Array.isArray(m.geo) ? m.geo : [],
      points: Array.isArray(m.points) ? m.points : [],
      text: String(m.text || ''),
      color: String(m.color || ''),
      // 笔迹粗细（0033）。老草稿没有这个字段 → 0 = 走样式表默认，行为不变
      width: Math.max(0, Number(m.width) || 0),
      sent: !!m.sent,
    }));
  if (obj.regions && typeof obj.regions === 'object') out.regions = obj.regions;
  if (obj.setup && typeof obj.setup === 'object') out.setup = obj.setup;
  return out;
}

/** 真正落盘（内部用）。返回 false = 没写成（配额满 / 被禁用）。
 *
 *  配额满时**逐次砍半重试**，不是砍一次就放弃：浏览器里占地方的不止我们一个 key，
 *  砍一半常常还不够。砍掉的条数记进 `trimmed` 并同步回内存 ——
 *  否则内存里还留着那几十条，下一次防抖写会再撞一次同样的墙。
 */
function persist(gid, data) {
  const s = store();
  if (!s) return false;
  let marks = data.marks || [];
  const full = marks.length;
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const body = {
      v: VERSION,
      at: new Date().toISOString(),
      page_no: data.page_no,
      marks,
      regions: data.regions,
      setup: data.setup,
      trimmed: data.trimmed || 0,
    };
    try {
      s.setItem(draftKey(gid), JSON.stringify(body));
      if (marks.length !== full) {
        data.marks = marks;
        data.trimmed = (data.trimmed || 0) + (full - marks.length);
      }
      return true;
    } catch (e) {
      // 配额满 / 被禁用。已经没笔迹可砍了就只能认栽（内存里的还在）。
      if (!marks.length) return false;
      const keep = Math.floor(marks.length / 2);
      marks = keep > 0 ? marks.slice(-keep) : [];
    }
  }
  return false;
}

export function removeDraft(gid) {
  const s = store();
  if (!s) return;
  try { s.removeItem(draftKey(gid)); } catch (e) { /* 被禁用了，没什么可做的 */ }
}

/**
 * 批改页用的草稿句柄。
 *
 * 一个页面一个实例，内部持有整份状态；调用方只改自己的 ref，然后调
 * `touch({ marks })` 之类的**小 patch**，不用每次把整份塞进来。
 * 写盘走 250ms 防抖（画一笔一帧不写一次）。
 */
export function createDraft(gid, { delay = 250 } = {}) {
  const current = readDraft(gid);
  let timer = 0;
  let lastError = '';

  function flush() {
    if (timer) { clearTimeout(timer); timer = 0; }
    const ok = persist(gid, current);
    if (!ok) lastError = '本机存不下草稿（浏览器存储被禁用或已满）';
    return ok;
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(flush, delay);
  }

  function touch(patch) {
    if (!patch || typeof patch !== 'object') return;
    if ('page_no' in patch) current.page_no = Math.max(1, Number(patch.page_no) || 1);
    if ('regions' in patch) current.regions = patch.regions;
    if ('setup' in patch) current.setup = patch.setup;
    if (Array.isArray(patch.marks)) {
      current.marks = patch.marks.slice();
      // 超上限：丢最老的（数组前面的是先画的），并计数，让调用方有机会提示
      if (current.marks.length > MAX_MARKS) {
        const drop = current.marks.length - MAX_MARKS;
        current.marks = current.marks.slice(drop);
        current.trimmed = (current.trimmed || 0) + drop;
      }
    }
    schedule();
  }

  /** 加一条待发送的标注，返回它的本地 id。 */
  function pushMark(payload) {
    const rec = {
      lid: nextLid(),
      page_no: Math.max(1, Number((payload || {}).page_no) || 1),
      kind: String((payload || {}).kind || 'ink'),
      geo: Array.isArray(payload && payload.geo) ? payload.geo : [],
      points: Array.isArray(payload && payload.points) ? payload.points : [],
      text: String((payload && payload.text) || ''),
      color: String((payload && payload.color) || ''),
      // ⚠️ 必须跟着存：补发走的是**批量接口**（手写字段清单），漏了就不报错地丢
      width: Math.max(0, Number((payload || {}).width) || 0),
      sent: false,
    };
    current.marks = current.marks.concat([rec]);
    touch({ marks: current.marks });
    return rec.lid;
  }

  function setSent(lid, sent = true) {
    const rec = current.marks.find((m) => m.lid === lid);
    if (!rec) return;
    rec.sent = !!sent;
    touch({ marks: current.marks });
  }

  function dropMark(lid) {
    const next = current.marks.filter((m) => m.lid !== lid);
    if (next.length === current.marks.length) return;
    touch({ marks: next });
  }

  /** 只有「服务端确实已经有这条了」才出队 —— 见 HomeworkGrade.reconcile 的说明。 */
  function dropMarks(lids) {
    const gone = new Set((lids || []).map(String));
    if (!gone.size) return;
    const next = current.marks.filter((m) => !gone.has(m.lid));
    if (next.length !== current.marks.length) touch({ marks: next });
  }

  return {
    get data() { return current; },
    get lastError() { return lastError; },
    marks: () => current.marks.slice(),
    touch,
    flush,
    pushMark,
    setSent,
    dropMark,
    dropMarks,
    clear() {
      if (timer) { clearTimeout(timer); timer = 0; }
      current.at = '';
      current.marks = [];
      current.regions = null;
      current.setup = null;
      current.trimmed = 0;
      removeDraft(gid);
    },
  };
}

/** 队列里的一笔 → 标注层能画的 mark（`id` 带 `local:` 前缀，界面上认得出「还没传上去」）。 */
export function localMark(rec) {
  return {
    id: 'local:' + rec.lid,
    local_id: rec.lid,
    page_no: rec.page_no,
    kind: rec.kind,
    geo: rec.geo,
    points: rec.points,
    text: rec.text,
    color: rec.color,
    width: rec.width || 0,
    created_by: 'teacher',
    item_id: null,
    pending: true,
  };
}

/** `id` 是不是本地待发送的标注。 */
export function isLocalMarkId(id) {
  return String(id || '').startsWith('local:');
}

/** 两条标注是不是「同一笔」（用于去重：服务端已经有了，本地的就不用再画一遍）。 */
export function sameMark(a, b) {
  if (!a || !b) return false;
  const sig = (m) => JSON.stringify([
    String(m.kind || ''),
    (m.geo || []).map((n) => Math.round(Number(n) || 0)),
    (m.points || []).map((n) => Math.round(Number(n) || 0)),
    String(m.text || ''),
  ]);
  return sig(a) === sig(b);
}
