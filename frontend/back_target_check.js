/**
 * 批改台的「返回」该回哪儿 —— 纯逻辑单测 + 两份实现的一致性比对。
 *
 *   node frontend/back_target_check.js     # 退出码即结果
 *
 * ## 为什么单独测这个
 *
 * 用户 2026-10-21 报的原话：「从「学生信息 → 改作业」进入批改作业平台时，返回应回到
 * 该学生的学生信息界面」「从「主控制台」进入时，返回只需回到工作台」
 * 「当前错误地跳到了录题平台，需要定位并修正这处跳转」。
 *
 * 那个 bug 的形状是**一个写死的地址**：workbench 顶栏 `backUrl` 恒等于
 * `/entry.html#/<...>`，而 `entry.html` 是**录题平台**（试卷分割录入）且不认 hash 路由。
 * 所以：
 *   ① 这里的核心断言不是「某个 from 映射对不对」，而是**任何来源都不许再出现 entry.html**；
 *   ② 判据要看**代码**不看注释（原来那句解释这个坑的注释里就写着 entry.html，
 *      直接 grep 文件会把自己弄红 —— 项目里踩过这个假绿，见 MEMORY 的「反面断言」条）。
 *
 * ## 为什么要比对两份实现
 *
 * `src/backTarget.js` 是唯一出处，但 `workbench.html` 是零构建的普通脚本页，
 * 写不了静态 import，只好逐字拷贝一份。**拷贝就会漂**：只改一边，界面上出现
 * 「从学生页进来却回工作台」这种只有真点一次才发现的错。所以这里拿同一张输入表
 * 同时喂两份，逐条比答案（href / 文案 / 原因）。
 */
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = __dirname;
let total = 0;
const fails = [];

function check(name, ok, detail) {
  total += 1;
  if (ok) {
    console.log('  [OK ] ' + name);
  } else {
    console.log('  [FAIL] ' + name + (detail === undefined ? '' : '   <- ' + JSON.stringify(detail)));
    fails.push(name);
  }
}

const J = (v) => JSON.stringify(v);

/** 去掉注释再断言 —— 解释这个坑的注释里必然提到 entry.html。 */
function codeOnly(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

/** 从 workbench.html 里抠出镜像块并求值（不依赖 jsdom / 浏览器）。 */
function loadMirror() {
  const file = path.join(ROOT, 'workbench.html');
  const src = fs.readFileSync(file, 'utf8');
  const m = /backTarget-mirror:begin[^\n]*\n([\s\S]*?)\n[^\n]*backTarget-mirror:end/.exec(src);
  if (!m) return { err: 'workbench.html 里没找到 backTarget-mirror 标记块', src };
  const body = m[1];
  let api;
  try {
    /* new Function 而不是 eval：块里的 const/function 不会漏到本文件作用域。 */
    api = new Function(body + '\nreturn { parseSource: bParseSource, backTarget: bBackTarget,'
      + ' keepQuery: bKeepQuery, studentUrl: bStudentUrl, legacyGradeUrl: bLegacyGradeUrl };')();
  } catch (e) {
    return { err: '镜像块求值失败：' + e.message, src };
  }
  return { api, src, body };
}

(async () => {
  const M = await import(pathToFileURL(path.join(ROOT, 'src', 'backTarget.js')).href);
  const mirror = loadMirror();

  console.log('\n① 唯一出处 src/backTarget.js —— 三种入口的返回目标');
  check('镜像块能从 workbench.html 里抠出来并求值', !mirror.err, mirror.err);
  if (mirror.err) {
    console.log('\n共 ' + total + ' 项，失败 ' + fails.length + ' 项');
    fails.forEach((f) => console.log('  - ' + f));
    return 1;
  }

  /* ---------- 需求里的三条，一条一条钉住 ---------- */
  const t1 = M.backTarget({ from: M.FROM.STUDENT, sid: 3 });
  check('★ 学生信息 → 改作业：回**这个学生**的学生信息界面',
    t1.href === '/#/students/3', t1.href);
  check('  文案明确说是回学生（不是含糊的「返回」）', t1.label === '← 返回学生', t1.label);

  const t2 = M.backTarget({ from: M.FROM.WORKBENCH, studentId: 5 });
  check('★ 旧版批改台点「新版工作台」：同样回学生信息界面（sid 缺就按记录里的 student_id）',
    t2.href === '/#/students/5', t2.href);

  const t3 = M.backTarget({ from: M.FROM.CONSOLE, sid: 3, studentId: 5 });
  check('★ 主控制台进来：回工作台，且**来源优先**（不让 sid 抢走）',
    t3.href === '/#/' && t3.why === 'console', J(t3));
  check('  文案说是回工作台', t3.label === '← 回到工作台', t3.label);

  /* ---------- 缺省与脏参数 ---------- */
  check('缺省（老书签 / 甩过来的 ?gid=）：认得出学生就回学生页',
    M.backTarget({ gid: 7, studentId: 9 }).href === '/#/students/9',
    J(M.backTarget({ gid: 7, studentId: 9 })));
  check('缺省且认不出学生 → 回工作台（不能丢进录题页）',
    M.backTarget({ gid: 7 }).href === '/#/', J(M.backTarget({ gid: 7 })));
  /* 未知取值不是「错误」而是「没说是谁」—— 于是走缺省：认得出学生就回学生页，不猜来源。
     （`parseSource` 那一层会把拼错的值归一成空串，两条路殊途同归。） */
  const dirtyFrom = M.backTarget({ from: 'Students', sid: 3 });
  check('from 拼错（大小写/未知值）当缺省处理，不报错、也不丢学生',
    dirtyFrom.href === '/#/students/3' && dirtyFrom.why === 'inferred', J(dirtyFrom));
  /* 认不出具体学生时，从学生页进来的应该回**学生列表**，不该被丢回工作台
     （「从哪儿来回哪儿去」：老师刚才就是在学生那块儿）。 */
  check('★ 学生信息进来但认不出是哪个学生 → 回学生列表（不是工作台）',
    M.backTarget({ from: M.FROM.STUDENT }).href === '/#/students',
    M.backTarget({ from: M.FROM.STUDENT }).href);
  check('sid 是脏值（0 / -2 / abc / 3.5）→ 当没有具体学生，回学生列表',
    ['0', '-2', 'abc', '3.5', '', null].every((bad) => M.backTarget({ from: M.FROM.STUDENT, sid: bad }).href === '/#/students'),
    ['0', '-2', 'abc', '3.5'].map((b) => M.backTarget({ from: M.FROM.STUDENT, sid: b }).href));
  check('sid 是字符串 "3"（路由参数永远是字符串）也能认',
    M.backTarget({ from: M.FROM.STUDENT, sid: '3' }).href === '/#/students/3');

  /* ---------- 核心回归：任何来源都不许再出现 entry.html ---------- */
  const allCases = [];
  ['', M.FROM.STUDENT, M.FROM.WORKBENCH, M.FROM.CONSOLE, 'bogus'].forEach((from) => {
    [null, 3].forEach((sid) => {
      [null, 5].forEach((studentId) => {
        [null, 7].forEach((gid) => allCases.push({ from, sid, studentId, gid }));
      });
    });
  });
  const hrefs = allCases.map((c) => M.backTarget(c).href);
  check('★★★ 20 种来源组合里**没有一个**回 entry.html（用户报的就是这个）',
    hrefs.every((h) => h.indexOf('entry.html') === -1), hrefs.filter((h) => /entry\.html/.test(h)));

  const moduleSrc = fs.readFileSync(path.join(ROOT, 'src', 'backTarget.js'), 'utf8');
  check('★★★ 唯一出处的**代码**里没有 entry.html（注释里提到不算）',
    codeOnly(moduleSrc).indexOf('entry.html') === -1, codeOnly(moduleSrc).slice(0, 120));
  check('★★★ workbench 镜像块的**代码**里也没有 entry.html',
    codeOnly(mirror.body).indexOf('entry.html') === -1);

  /* ---------- parseSource ---------- */
  const ps = M.parseSource('?gid=7&from=student&sid=3');
  check('parseSource：三样都认', J(ps) === J({ from: 'student', sid: 3, gid: 7 }), J(ps));
  check('parseSource：没参数 → 全空（不抛）', J(M.parseSource('')) === J({ from: '', sid: null, gid: null }),
    J(M.parseSource('')));
  check('parseSource：from 大小写 / 空格都归一', M.parseSource('?from=%20STUDENT%20').from === 'student',
    J(M.parseSource('?from=%20STUDENT%20')));
  check('parseSource：不认识的 from → 空串（走缺省，不猜）',
    M.parseSource('?from=whatever').from === '', J(M.parseSource('?from=whatever')));

  /* ---------- keepQuery：改地址栏时别把来源冲掉 ---------- */
  check('keepQuery：带 gid + 来源 + 学生',
    M.keepQuery({ from: 'student', sid: 3 }, 7) === '?gid=7&from=student&sid=3',
    M.keepQuery({ from: 'student', sid: 3 }, 7));
  check('keepQuery：没有来源时只写 gid（不写空 from=）',
    M.keepQuery({ from: '', sid: null }, 7) === '?gid=7', M.keepQuery({ from: '', sid: null }, 7));
  check('keepQuery：gid 为空时不写 gid=NaN',
    M.keepQuery({ from: 'console' }, null) === '?from=console', M.keepQuery({ from: 'console' }, null));

  /* ---------- 旧版批改台的地址：是 SPA 路由，不是录题页 ---------- */
  check('★ 旧版批改台地址 = /#/grade/<gid>（SPA 里那条真路由）',
    M.legacyGradeUrl(7) === '/#/grade/7', M.legacyGradeUrl(7));
  check('没有 gid → 空串（按钮不显示，不给人一个死链）', M.legacyGradeUrl(null) === '',
    M.legacyGradeUrl(null));
  check('学生地址：没 id 回列表，不拼出 /students/null', M.studentUrl(null) === '/#/students',
    M.studentUrl(null));

  /* ---------- 两份实现逐条比对（拷贝最容易漂的地方） ---------- */
  console.log('\n② workbench.html 里的镜像块 —— 与唯一出处的答案必须逐条相同');
  const mb = mirror.api;
  const diffs = [];
  allCases.forEach((c) => {
    const a = M.backTarget(c);
    const b = mb.backTarget(c);
    if (J(a) !== J(b)) diffs.push({ c, module: a, mirror: b });
  });
  check('★★★ 20 种来源组合：两份给出的 href / 文案 / 原因完全一致',
    diffs.length === 0, diffs.slice(0, 2));

  const qDiffs = [{ from: 'student', sid: 3 }, { from: '', sid: null }, { from: 'console', sid: null }]
    .filter((s) => M.keepQuery(s, 7) !== mb.keepQuery(s, 7));
  check('★★ keepQuery 两份一致（否则刷新一下来源就丢了）', qDiffs.length === 0, qDiffs);

  const pDiffs = ['', '?gid=7&from=student&sid=3', '?from=%20STUDENT%20', '?from=whatever']
    .filter((s) => J(M.parseSource(s)) !== J(mb.parseSource(s)));
  check('★★ parseSource 两份一致', pDiffs.length === 0, pDiffs);
  check('★ legacyGradeUrl 两份一致', M.legacyGradeUrl(7) === mb.legacyGradeUrl(7));
  check('★ studentUrl 两份一致', M.studentUrl('3') === mb.studentUrl('3'));

  /* ---------- 来源常量：值会写进 URL，改了老链接就失效 ---------- */
  check('来源常量的字面值不许改（写进 URL，老书签/甩出去的直链还认它）',
    M.FROM.STUDENT === 'student' && M.FROM.WORKBENCH === 'workbench' && M.FROM.CONSOLE === 'console',
    J(M.FROM));

  /* ---------- 三个入口真的把来源带下去了吗（接线，不是逻辑） ----------
   *
   * 「逻辑对」和「接上了」是两件事：backTarget 算得再准，入口不传 from 也白搭。
   * 这几条看的是**代码**（去注释后），因为 StudentDetail / Dashboard 没有挂载夹具；
   * 挂载层的结果断言在 workbench_check ⑭ 与 grade_mount_check 里。 */
  const src = (rel) => codeOnly(fs.readFileSync(path.join(ROOT, rel), 'utf8'));
  check('★ 学生信息「保存并批改」带 from=student 与本学生 id',
    /query:\s*\{\s*from:\s*FROM\.STUDENT,\s*sid:\s*id\s*\}/.test(src('src/views/StudentDetail.js')),
    '');
  check('★ 今日工作台「保存并批改」带 from=console',
    /query:\s*\{\s*from:\s*FROM\.CONSOLE\s*\}/.test(src('src/views/Dashboard.js')), '');
  check('★ 侧栏「批改工作台」链接带 from=console',
    /href="\/workbench\.html\?from=console"/.test(src('src/App.js')), '');
  check('★★ 旧版批改台的「新版工作台」把 from / sid 拼进地址',
    /qs\.set\('from', entrySource\)/.test(src('src/views/HomeworkGrade.js'))
    && /qs\.set\('sid', String\(d\.student_id\)\)/.test(src('src/views/HomeworkGrade.js')), '');
  check('★★★ 来源值都来自 FROM 常量，没有裸字符串（改口径只改一处）',
    !/from:\s*'(student|console|workbench)'/.test(src('src/views/StudentDetail.js'))
    && !/from:\s*'(student|console|workbench)'/.test(src('src/views/Dashboard.js')), '');

  console.log('\n共 ' + total + ' 项，失败 ' + fails.length + ' 项');
  if (fails.length) {
    fails.forEach((f) => console.log('  - ' + f));
    return 1;
  }
  console.log('全部通过 ✅');
  return 0;
})().then((code) => process.exit(code)).catch((e) => {
  console.error('检查崩了：', e);
  process.exit(1);
});
