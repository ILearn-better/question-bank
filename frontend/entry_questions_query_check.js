// 录题页拉题方式自检（R4）：有文档时要**让后端按 document_id 过滤**，不是拉全库再前端筛。
//
// 现状（entry.html 的 loadQuestions）：
//     const qs = await this.api('/api/questions');
//     this.questions = this.doc ? qs.filter(q => q.document_id === this.doc.id) : qs;
// 两个问题：
//   ① 每次都把整个题库拉回来（题库大了以后每一次切文档都传一遍全量 JSON）；
//   ② 更难查的是**错的感觉**：前端筛一遍，看起来「只有这份文档的题」，
//      于是 `document_id` 参数没传对也看不出来 —— 真正的错（后端没筛）被掩盖了。
// 后端 `GET /api/questions?document_id=` 已经支持，这里守着「用起来」。
//
// 判据是**行为**：把页面里那份 loadQuestions 抽出来，喂一个假的 this，
// 看它到底请求了哪个 URL、以及拿到返回后有没有再自己筛一遍。
//
// 跑法：node entry_questions_query_check.js   （退出码 0 = 全过）
const fs = require('fs');
const path = require('path');

const BASE = __dirname;
let pass = 0, fail = 0;
const fails = [];

function check(name, ok, detail) {
  if (ok) {
    pass++;
    console.log('  [OK ] ' + name);
  } else {
    fail++;
    fails.push(name);
    console.log('  [FAIL] ' + name + (detail !== undefined && detail !== '' ? '   <- ' + detail : ''));
  }
}

const SRC = fs.readFileSync(path.join(BASE, 'entry.html'), 'utf8');

// ⚠️ 只剥 HTML 注释：entry.html 里有 `accept="image/*"`，
//    用「剥块注释」那条正则会把半张页面吃掉（本项目踩过）。
const CODE = SRC.replace(/<!--[\s\S]*?-->/g, '');

// 无文档时「只取最近 N 条」的那个 N，**从页面自己的常量读，不在这里另起一个数**。
// 写死 200 的话，页面哪天改成 500，测试还盯着 200，两边谁都不会知道自己漂了。
// 与 entry.html 的 `const RECENT_Q = 200` 同步：改一处要改两处（这句话就说给改的人看）。
const RECENT_Q = (() => {
  const m = /^[ \t]*const\s+RECENT_Q\s*=\s*(\d+)/m.exec(CODE);
  return m ? Number(m[1]) : null;
})();

/** 从 header 行切到**同缩进**的闭合括号（loadQuestions 是 methods 里的一个方法）。 */
function sliceMethod(src, headerRe) {
  const m = headerRe.exec(src);
  if (!m) return null;
  const indent = (m[0].match(/^[ \t]*/) || [''])[0];
  const lines = src.slice(m.index).split('\n');
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i];
    if ((line.match(/^[ \t]*/) || [''])[0] !== indent) continue;
    if (/^\}\s*[,;]?$/.test(line.trim())) return lines.slice(0, i + 1).join('\n');
  }
  return null;
}

let fn = null;
let loadErr = '';
try {
  let body = sliceMethod(CODE, /^[ \t]*async\s+loadQuestions\s*\(\s*\)\s*\{/m);
  if (!body) throw new Error('entry.html 里找不到 async loadQuestions()');
  body = body.replace(/^([ \t]*)async\s+loadQuestions\s*\(/m, '$1async function loadQuestions(');
  body = body.replace(/(\})\s*[,;]\s*$/, '$1');     // 方法后面的逗号在函数声明里是语法错
  // eslint-disable-next-line no-new-func
  fn = new Function(body + '\nreturn loadQuestions;')();
} catch (e) {
  loadErr = String(e && e.message ? e.message : e);
}

/** 拿假 this 跑一遍 loadQuestions，返回它请求过的 URL 列表与最终 questions。 */
async function runLoad(ctx0) {
  const urls = [];
  const ctx = Object.assign({
    questions: [],
    doc: null,
    __rows: [],
    api: async (url) => { urls.push(String(url)); return ctx.__rows; },
  }, ctx0);
  await fn.call(ctx);
  return { urls, questions: ctx.questions };
}

const DOC = { id: 'doc-abc123' };
const ROWS = [
  { id: 'q1', document_id: 'doc-abc123', stem: '本题属于这份文档' },
  { id: 'q2', document_id: 'doc-zzz999', stem: '别的文档的题' },
];

(async () => {
  console.log('==== ① 抽到页面里那份 loadQuestions（行为测试的前提） ====');
  check('entry.html 里能抽到可执行的 loadQuestions', typeof fn === 'function', loadErr);
  if (typeof fn !== 'function') {
    console.log(`\n失败 ${fail + 1} 项 / 通过 ${pass} 项`);
    process.exit(1);
  }

  console.log('\n==== ② 有文档：请求要带 document_id，前端不许再自己筛 ====');
  let r = await runLoad({ doc: DOC, __rows: ROWS });
  const url = r.urls[0] || '';
  check('有文档时，请求 URL 里带上了 document_id（让后端筛）',
    url.indexOf('document_id=' + DOC.id) >= 0, '实际请求：' + url);
  check('有文档时只发了一个请求（不是拉全库再筛）',
    r.urls.length === 1, r.urls.join(' , '));
  check('有文档时前端不再自己 filter(document_id)（服务端已筛，前端再筛会掩盖参数没传）',
    r.questions.length === ROWS.length,
    `接口返回 ${ROWS.length} 条，页面只留下 ${r.questions.length} 条 = 还在前端筛`);

  console.log('\n==== ③ 没有文档：只取最近 N 条，且不许传空的 document_id ====');
  r = await runLoad({ doc: null, __rows: ROWS });
  const url2 = r.urls[0] || '';
  check('entry.html 里有 `const RECENT_Q = <数字>`（无文档时的取值上限）',
    RECENT_Q !== null, '找不到这个常量 —— 无文档时会退回拉全库');
  check('没有文档时不带 document_id 参数（空值会被后端当过滤条件）',
    url2.indexOf('document_id') < 0, '实际请求：' + url2);
  const qLabel = RECENT_Q === null ? 'limit=N' : 'limit=' + RECENT_Q;
  check(`没有文档时请求带 ${qLabel}（与 entry.html 的 RECENT_Q 同步，改一处要改两处）`,
    RECENT_Q !== null && new RegExp('[?&]limit=' + RECENT_Q + '(?:[&#]|$)').test(url2),
    '实际请求：' + url2);
  check('没有文档时题目照常赋给页面', r.questions.length === ROWS.length,
    `${r.questions.length} / ${ROWS.length}`);

  console.log('\n==== ④ 源码里没有残留前端过滤 ====');
  const qsBody = sliceMethod(CODE, /^[ \t]*async\s+loadQuestions\s*\(\s*\)\s*\{/m) || '';
  check('loadQuestions 里没有 .filter(... === this.doc.id) 这种前端过滤残留',
    !/\.filter\(\s*\(?[\w$]+\s*\)?\s*=>\s*[\w$]+\.document_id\s*===/.test(qsBody), qsBody.trim());

  console.log('\n' + '='.repeat(50));
  if (fail) {
    console.log(`失败 ${fail} 项 / 通过 ${pass} 项`);
    fails.forEach((f) => console.log('  - ' + f));
    process.exit(1);
  }
  console.log(`全部通过（${pass} 项）`);
  process.exit(0);
})().catch((e) => {
  console.log('  [FAIL] 检查脚本自己抛错了：' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
