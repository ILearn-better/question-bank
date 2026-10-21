// 批改台的「返回」该回哪儿 —— 唯一出处。
//
// ## 为什么要有这个文件
//
// 批改台有两份实现：SPA 里的 `views/HomeworkGrade.js`（旧版）与独立页
// `workbench.html`（新版，零构建）。两边原来各写各的返回地址，而新版那份写的是
// `/entry.html#/grade/<gid>` —— `entry.html` 是**录题平台**（试卷分割录入），
// 它根本不认 hash 路由，于是老师点「返回」就被甩进了录题页。
// 用户 2026-10-21 点名：「返回目标由进入来源决定，而不是统一跳到录题平台」。
//
// ## 口径：URL 参数 `from`，一律显式带
//
//   from=student   学生信息 →「改作业」            → 该学生的学生信息界面
//   from=workbench 旧版批改台点「新版工作台」        → 该学生的学生信息界面
//   from=console   主控制台侧栏直接进批改工作台      → 今日工作台
//   from 缺省       老书签 / 别人甩过来的 ?gid= 链接 → 能认出学生就回学生页，否则今日工作台
//
// ## 为什么不用 document.referrer
//
//   ① 从 SPA 的 hash 路由跳过来时，referrer 里 hash 之后那段并不可靠；
//   ② 「把直链甩给别人」是这页存在的理由（workbench 特意没进 router），
//      对方打开时根本没有来源。
//
// ## 硬约束
//
//   · **不在这里出现任何「具体某个页面」的判断逻辑**（比如「如果是录题页就…」）。
//     来源只有 `from` 这一个输入，未知取值一律当缺省 —— 这样新增入口只要传对
//     `from`，返回目标自动就对，不用回来改这里。
//   · `workbench.html` 里有一份**逐字拷贝**（零构建页不能 import ES 模块）。
//     改这里必须同步那一段，`back_target_check.js` 会逐条比对两份的答案。
//
// 地址口径：SPA 是 hash 路由且由后端 `StaticFiles(html=True)` 挂在 `/`，
// 所以「今日工作台」= `/#/`、「学生信息」= `/#/students/<id>`、「旧版批改台」= `/#/grade/<id>`。

/** 已知来源。值会写进 URL，别改字符串（老链接/书签会失效）。 */
export const FROM = Object.freeze({
  STUDENT: 'student',
  WORKBENCH: 'workbench',
  CONSOLE: 'console',
});

/** 主控制台首页（今日工作台）。 */
export const HOME_URL = '/#/';
/** 学生列表（认不出具体学生时的兜底，好过把人丢回首页）。 */
export const STUDENTS_URL = '/#/students';

const KNOWN = [FROM.STUDENT, FROM.WORKBENCH, FROM.CONSOLE];

/** 正整数 id，别的（空 / NaN / 0 / -1 / '3.5'）一律当没有。 */
function toId(v) {
  const n = Number(v);
  return Number.isInteger(n) && n > 0 ? n : null;
}

/** 某位学生的学生信息界面。没给 id → 学生列表。 */
export function studentUrl(sid) {
  const id = toId(sid);
  return id ? STUDENTS_URL + '/' + id : STUDENTS_URL;
}

/** 旧版批改台（SPA 的 /grade/:id）。⚠️ 不是 entry.html —— 那是录题平台。 */
export function legacyGradeUrl(gid) {
  const id = toId(gid);
  return id ? '/#/grade/' + id : '';
}

/**
 * 从 `location.search`（或任意 query 串）里读出入口来源。
 * 未知的 `from` 取值 → 空串（走缺省），不报错：参数是手写 URL 时经常拼错。
 */
export function parseSource(search) {
  let q;
  try {
    q = new URLSearchParams(search || '');
  } catch (e) {
    q = new URLSearchParams('');
  }
  const raw = (q.get('from') || '').trim().toLowerCase();
  return {
    from: KNOWN.indexOf(raw) >= 0 ? raw : '',
    sid: toId(q.get('sid')),
    gid: toId(q.get('gid')),
  };
}

/**
 * 决定「返回」按钮的地址与文案。
 *
 * @param {object} o
 *   `from`      来源标识（`FROM.*` 之一，空串 = 缺省）
 *   `sid`       URL 上带的 student_id（最可信：那是入口那一刻的上下文）
 *   `studentId` 从当前这份批改记录里查到的 student_id（sid 缺失时的退路）
 *   `gid`       当前打开的批改 id（只用于缺省分支判断「有没有打开具体一份」）
 * @returns {{href:string, label:string, why:string}} `why` 只给测试与排查看
 */
export function backTarget(o) {
  const opt = o || {};
  const from = opt.from ? String(opt.from) : '';
  const sid = toId(opt.sid);
  const known = sid || toId(opt.studentId);

  // ① 主控制台进来的：回工作台就够了（需求原话「不要求回到旧版批改台」）
  if (from === FROM.CONSOLE) {
    return { href: HOME_URL, label: '← 回到工作台', why: 'console' };
  }

  // ② 学生信息 / 旧版批改台点「新版工作台」进来的：都回**那个学生**的界面，
  //    并把学生上下文带回去（sid 优先，其次从这份记录里读）
  if (from === FROM.STUDENT || from === FROM.WORKBENCH) {
    return { href: studentUrl(known), label: '← 返回学生', why: from };
  }

  // ③ 缺省（老书签 / 甩过来的直链 / from 拼错）：批改记录总属于某个学生，
  //    认得出就回学生页 —— 比丢回首页更像「从哪儿来回哪儿去」
  if (known) {
    return { href: studentUrl(known), label: '← 返回学生', why: 'inferred' };
  }
  return { href: HOME_URL, label: '← 回到工作台', why: 'home' };
}

/** 把「当前看的这份 gid + 来源」拼进 query，供页面内改地址栏用（刷新/甩链接都不丢来源）。 */
export function keepQuery(src, gid) {
  const s = src || {};
  const q = new URLSearchParams();
  const id = toId(gid);
  if (id) q.set('gid', String(id));
  if (s.from) q.set('from', s.from);
  const sid = toId(s.sid);
  if (sid) q.set('sid', String(sid));
  const qs = q.toString();
  return qs ? '?' + qs : '';
}
