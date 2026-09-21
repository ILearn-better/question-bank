// 轻量共享状态 + 消息提示。
// 没用 Pinia：这个应用的跨页面共享状态只有「体系列表 / 能力维度 / 学生列表」三项，
// 一个 reactive 对象就够，引入状态管理库属于为架构而架构。
import { reactive } from 'vue';
import { abilityApi, curriculumApi, studentsApi } from './api.js';

export const state = reactive({
  curricula: [],
  dims: [],
  students: [],
  loaded: { curricula: false, dims: false, students: false },
});

export async function loadCurricula(force = false) {
  if (state.loaded.curricula && !force) return state.curricula;
  state.curricula = await curriculumApi.list();
  state.loaded.curricula = true;
  return state.curricula;
}

export async function loadDims(force = false) {
  if (state.loaded.dims && !force) return state.dims;
  state.dims = await abilityApi.dims();
  state.loaded.dims = true;
  return state.dims;
}

export async function loadStudents(force = false) {
  if (state.loaded.students && !force) return state.students;
  state.students = await studentsApi.list();
  state.loaded.students = true;
  return state.students;
}

export function curriculumName(id) {
  const c = state.curricula.find((x) => x.id === id);
  return c ? c.name : null;
}

// ---------------- 消息提示 ----------------
export const toasts = reactive([]);
let toastSeq = 0;

export function notify(message, type = 'info') {
  const id = ++toastSeq;
  toasts.push({ id, message, type });
  setTimeout(() => {
    const i = toasts.findIndex((t) => t.id === id);
    if (i >= 0) toasts.splice(i, 1);
  }, type === 'err' ? 5200 : 2600);
}

export const ok = (m) => notify(m, 'ok');
export const warn = (m) => notify(m, 'info');
export const fail = (m) => notify(m, 'err');

// ---------------- 通用小工具 ----------------
export function todayISO() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function currentPeriod() {
  return todayISO().slice(0, 7);
}

/** '2026-09-20T19:00' -> '19:00' */
export function hhmm(iso) {
  return (iso || '').slice(11, 16);
}

/** '2026-09-20T19:00' -> '09-20 周日' */
export function shortDate(iso) {
  if (!iso) return '';
  const d = new Date(iso.replace(' ', 'T'));
  if (Number.isNaN(d.getTime())) return iso.slice(5, 10);
  const wk = '日一二三四五六'[d.getDay()];
  return `${iso.slice(5, 10)} 周${wk}`;
}

export const STATUS_LABEL = {
  scheduled: '已排课',
  done: '已完成',
  makeup: '补课',
  cancelled: '已取消',
  leave: '请假',
  moved: '已调课',
};

export const STATUS_TAG = {
  scheduled: 'blue',
  done: 'green',
  makeup: 'green',
  cancelled: '',
  leave: 'orange',
  moved: 'orange',
};

export function money(v) {
  if (v === null || v === undefined) return '—';
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}
