// 月历网格 —— 纯逻辑，不碰 DOM，不依赖 Vue。
//
// 为什么单独一个文件：这块最容易出错，而错了又最难看出来。
//   · 首日偏移差一格 → 日历整体错位（周日开头的表，看起来「很正常」）
//   · 月末天数算错   → 2 月 28/29、4 月 30
//   · 跨年           → 12 月往后翻变成 13 月
// 放在这里就能用 node 直接单测（见 backend/test_schedule_import.py 里的日历段），
// 不必开浏览器用肉眼去数格子。

export const WEEK_HEADS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

const pad = (n) => String(n).padStart(2, '0');

/** '2026-10' → { year: 2026, month: 10 }；不合法返回 null */
export function parsePeriod(period) {
  const m = /^(\d{4})-(\d{1,2})$/.exec(String(period ?? '').trim());
  if (!m) return null;
  const year = Number(m[1]);
  const month = Number(m[2]);
  if (!year || month < 1 || month > 12) return null;
  return { year, month };
}

/** 这个月有几天（1-based month） */
export function daysInMonth(period) {
  const p = parsePeriod(period);
  if (!p) return 0;
  // 下个月的第 0 天 == 这个月最后一天，比记「大小月表」靠谱
  return new Date(p.year, p.month, 0).getDate();
}

/**
 * 生成日历格子。返回长度一定是 7 的倍数的数组，每项：
 *   { date: 'YYYY-MM-DD' | null, day: number | null, out: boolean }
 * out=true 表示这是一个「填位」的空白格（月初之前 / 月末之后）。
 */
export function monthGrid(period) {
  const p = parsePeriod(period);
  if (!p) return [];
  const { year, month } = p;
  const lead = new Date(year, month - 1, 1).getDay();   // 0 = 周日，与 WEEK_HEADS 对齐
  const total = daysInMonth(period);

  const cells = [];
  for (let i = 0; i < lead; i += 1) cells.push({ date: null, day: null, out: true });
  for (let d = 1; d <= total; d += 1) {
    cells.push({ date: `${year}-${pad(month)}-${pad(d)}`, day: d, out: false });
  }
  while (cells.length % 7 !== 0) cells.push({ date: null, day: null, out: true });
  return cells;
}

/** 前后翻月份（delta 为 -1 / +1）。跨年由 Date 自己处理。 */
export function shiftPeriod(period, delta) {
  const p = parsePeriod(period);
  if (!p) return period;
  const d = new Date(p.year, p.month - 1 + Number(delta || 0), 1);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

/** '2026-10' → '2026 年 10 月' */
export function periodLabel(period) {
  const p = parsePeriod(period);
  return p ? `${p.year} 年 ${p.month} 月` : String(period || '');
}

/** 把课时列表按「日期」索引起来，值已按开始时间排好序。
 *  日历每格直接 byDay.get('2026-10-01') 就能拿到当天的课。 */
export function lessonsByDate(lessons) {
  const map = new Map();
  for (const ls of lessons || []) {
    const day = String(ls?.start_at || '').slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
    if (!map.has(day)) map.set(day, []);
    map.get(day).push(ls);
  }
  for (const list of map.values()) {
    list.sort((a, b) => String(a.start_at || '').localeCompare(String(b.start_at || '')));
  }
  return map;
}

/** 一个月的日期串列表 ['2026-10-01', ...]，给「导出/统计」之类用得上 */
export function monthDates(period) {
  return monthGrid(period).filter((c) => c.date).map((c) => c.date);
}
