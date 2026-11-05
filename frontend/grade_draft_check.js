// 用 node 直接量「本地草稿」（它的存在意义就是让数据**不丢**，所以必须量）。
//
// node 没有 localStorage，这里塞一个内存版进去 —— 与浏览器同 API 的那几个方法。
// 配额满的场景也一起量：真的到那一步时，降级路径要是通的。
const path = require('path');
const { pathToFileURL } = require('url');

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

/** 内存版 localStorage。`quota` 给定时，超过就抛（模拟浏览器配额满）。 */
function fakeStorage({ quota = 0 } = {}) {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => {
      const s = String(v);
      if (quota && s.length > quota) {
        const e = new Error('QuotaExceededError');
        e.name = 'QuotaExceededError';
        throw e;
      }
      m.set(k, s);
    },
    removeItem: (k) => { m.delete(k); },
    _size: () => Array.from(m.values()).reduce((n, s) => n + s.length, 0),
    _keys: () => Array.from(m.keys()),
  };
}

(async () => {
  const D = await import(pathToFileURL(path.join(__dirname, 'src', 'gradeDraft.js')).href);

  const sleep = (ms) => new Promise((r) => { setTimeout(r, ms); });
  const DAY = 86400e3;

  /* ---------------- 读写往返 ---------------- */
  globalThis.localStorage = fakeStorage();
  const d1 = D.createDraft(11, { delay: 0 });
  const lid = d1.pushMark({
    page_no: 2, kind: 'ink', geo: [10, 20, 30, 40], points: [10, 20, 30, 40], color: '#dc2626',
  });
  check('pushMark 返回一个本地 id', typeof lid === 'string' && lid.length > 4, lid);
  check('pushMark 立刻进内存队列（不等落盘）', d1.marks().length === 1, d1.marks());
  d1.flush();

  const back = D.readDraft(11);
  check('flush 之后读得回来', back.marks.length === 1, back.marks);
  check('笔迹的坐标一个不差（这是「不丢」的最小证据）',
        J(back.marks[0].points) === J([10, 20, 30, 40]), back.marks[0]);
  check('页号也存了（补发时要发回原来那一页）', back.marks[0].page_no === 2, back.marks[0]);
  check('新笔迹默认 sent=false（表示「还没送到服务端」）', back.marks[0].sent === false);

  check('不同作业互不干扰（key 按 gid 分）',
        D.readDraft(99).marks.length === 0, D.readDraft(99).marks);

  /* ---------------- 发送状态 ---------------- */
  d1.setSent(lid, true);
  check('setSent 能把一条标成已发', d1.marks()[0].sent === true, d1.marks());
  d1.dropMarks([lid]);
  check('dropMarks 才真正出队（sent 只是标记，不删）', d1.marks().length === 0);

  const lid2 = d1.pushMark({ page_no: 1, kind: 'check', geo: [1, 2, 3, 4] });
  d1.dropMark(lid2);
  check('dropMark 删单条', d1.marks().length === 0);

  /* ---------------- setGeo：还没传上去的那条被挪/缩过 ----------------
     ⚠️ 只在**服务端还没有副本**时用（POST 失败、或打开时补发之前）。
        正常路径是父组件记 pendingGeo、拿到真 id 再补 PATCH —— 所以这条
        必须真的改到 rec 上、并且立刻落盘，否则下次补发还是老位置。 */
  const lid3 = d1.pushMark({ page_no: 1, kind: 'text', geo: [100, 100, 100, 100], text: 'x' });
  d1.setGeo(lid3, [100, 100, 420, 260]);
  check('★ setGeo 改得动队列里那条的坐标',
        J(d1.marks()[0].geo) === J([100, 100, 420, 260]), d1.marks()[0].geo);
  d1.flush();
  check('★ setGeo 的结果落盘了（下次补发才带得上）',
        J(D.readDraft(11).marks[0].geo) === J([100, 100, 420, 260]),
        D.readDraft(11).marks[0].geo);
  check('setGeo 存的是拷贝，不是把外面那个数组挂进去',
        (() => {
          const g = [0, 0, 50, 50];
          d1.setGeo(lid3, g);
          g[0] = 999;
          return d1.marks()[0].geo[0] === 0;
        })(), d1.marks()[0].geo);
  d1.setGeo(lid3, [1, 2]);                       // 不足四个数 → 忽略（宁可不动，不要写半个框）
  check('setGeo 拒绝不完整的 geo（不会写半个框）',
        J(d1.marks()[0].geo) === J([0, 0, 50, 50]), d1.marks()[0].geo);
  d1.setGeo('不存在的 lid', [1, 2, 3, 4]);
  check('setGeo 找不到那条就安静返回（不抛）', d1.marks().length === 1);
  d1.dropMark(lid3);

  /* ---------------- 框题草稿 / 判定依据 ---------------- */
  d1.touch({
    page_no: 3,
    regions: {
      dirty: true,
      lines: { 1: [300, 600], 2: [420] },
      segs: { 1: [{ seq: 0, q_no: '1', box: [0, 0, 1000, 300], src: 'line' }] },
    },
    setup: { answer_mode: 'key', answer_text: '1. x=2', rubric_text: '', files: [] },
  });
  d1.flush();
  const back2 = D.readDraft(11);
  check('框题草稿（线 + 段）存得住', J(back2.regions.lines) === J({ 1: [300, 600], 2: [420] }),
        back2.regions);
  check('判定依据的文字存得住（没点保存就退出也不丢）',
        back2.setup.answer_text === '1. x=2' && back2.setup.answer_mode === 'key', back2.setup);
  check('当前页也存了（重开回到上次那一页）', back2.page_no === 3, back2.page_no);

  /* ---------------- 坏数据 / 过期 ---------------- */
  globalThis.localStorage = fakeStorage();
  const L = globalThis.localStorage;
  L.setItem(D.draftKey(5), '这不是 JSON');
  check('不是 JSON → 当没有草稿（不抛）', D.readDraft(5).marks.length === 0);

  L.setItem(D.draftKey(6), JSON.stringify({ v: 999, marks: [{ lid: 'x' }] }));
  check('版本对不上 → 当没有（免得用一份老结构去画新界面）', D.readDraft(6).marks.length === 0);

  const now = Date.parse('2026-10-19T12:00:00Z');
  L.setItem(D.draftKey(7), JSON.stringify({
    v: 1, at: new Date(now - (D.KEEP_DAYS + 1) * DAY).toISOString(), marks: [{ lid: 'a' }],
  }));
  check('超过保留期 → 丢，且顺手把 key 清掉',
        D.readDraft(7, now).marks.length === 0 && L.getItem(D.draftKey(7)) === null);

  L.setItem(D.draftKey(8), JSON.stringify({
    v: 1, at: new Date(now - DAY).toISOString(), marks: [{ lid: 'a', kind: 'ink' }],
  }));
  check('保留期内 → 读得到', D.readDraft(8, now).marks.length === 1);

  L.setItem(D.draftKey(9), JSON.stringify({ v: 1, at: '不是时间', marks: [{ lid: 'a' }] }));
  check('at 坏掉 → 当过期（宁可丢，也不要让上周的框出现在今天的卷面上）',
        D.readDraft(9, now).marks.length === 0);

  /* ---------------- 没有 localStorage ---------------- */
  delete globalThis.localStorage;
  check('环境根本没有 localStorage（隐私模式）→ 读得到空草稿，不抛',
        D.readDraft(1).marks.length === 0);
  {
    const dd = D.createDraft(1, { delay: 0 });
    dd.pushMark({ page_no: 1, kind: 'ink', geo: [1, 2, 3, 4] });
    check('写盘失败但内存仍然可用（老师这一笔还在界面上）', dd.marks().length === 1);
    check('写盘失败会被记下来（界面可以提示「本机存不下」）',
          dd.flush() === false && String(dd.lastError).includes('存不下'), dd.lastError);
  }

  /* ---------------- 配额满：降级而不是崩 ---------------- */
  globalThis.localStorage = fakeStorage({ quota: 6000 });
  {
    const dq = D.createDraft(2, { delay: 0 });
    for (let i = 0; i < 40; i += 1) {
      // 每笔 ~60 个点，40 笔稳稳超过 6000 字符
      const pts = [];
      for (let j = 0; j < 60; j += 1) pts.push(100 + j, 200 + j);
      dq.pushMark({ page_no: 1, kind: 'ink', geo: [100, 200, 300, 400], points: pts });
    }
    const okWrite = dq.flush();
    check('配额满时写盘降级（砍一半再试），不是直接炸', okWrite === true, okWrite);
    const saved = D.readDraft(2);
    check('降级后仍然留下了笔迹（不是全丢）', saved.marks.length > 0, saved.marks.length);
    check('降级后条数不超过上限', saved.marks.length <= D.MAX_MARKS, saved.marks.length);
  }

  /* ---------------- 超上限截断 ---------------- */
  globalThis.localStorage = fakeStorage();
  {
    const dm = D.createDraft(3, { delay: 0 });
    for (let i = 0; i < D.MAX_MARKS + 7; i += 1) {
      dm.pushMark({ page_no: 1, kind: 'ink', geo: [i, 0, i + 1, 1] });
    }
    check('笔迹超上限 → 只留最新的 MAX_MARKS 条（丢的是最老的）',
          dm.marks().length === D.MAX_MARKS, dm.marks().length);
    check('丢了多少条要记下来（不能让老师以为没丢）', dm.data.trimmed === 7, dm.data.trimmed);
  }

  /* ---------------- 去重用的同一笔判定 ---------------- */
  const a = { kind: 'box', geo: [10, 20, 30, 40], points: [], text: '' };
  check('同一笔（坐标完全相同）→ sameMark 为真',
        D.sameMark(a, { kind: 'box', geo: [10, 20, 30, 40], points: [], text: '' }) === true);
  check('稍微不同的几何 → 不是同一笔',
        D.sameMark(a, { kind: 'box', geo: [10, 20, 30, 41], points: [], text: '' }) === false);
  check('小数与整数等价（服务端会取整，前端不该因此判成两笔）',
        D.sameMark({ kind: 'box', geo: [10.4, 20, 30, 40], points: [] },
                    { kind: 'box', geo: [10, 20, 30, 40], points: [] }) === true);

  /* ---------------- 本地 id 与 localMark ---------------- */
  globalThis.localStorage = fakeStorage();
  check('isLocalMarkId 认 local: 前缀',
        D.isLocalMarkId('local:abc') === true && D.isLocalMarkId(12) === false);
  {
    const dl = D.createDraft(4, { delay: 0 });
    const l = dl.pushMark({ page_no: 3, kind: 'cross', geo: [5, 6, 7, 8] });
    const m = D.localMark(dl.marks()[0]);
    check('localMark 造出来的 mark 带 local: id 与 pending 标记',
          m.id === 'local:' + l && m.pending === true && m.page_no === 3, m);
    check('localMark 标成 teacher 画的（导出与统计口径不能把它算成 AI 的）',
          m.created_by === 'teacher', m.created_by);
  }

  /* ---------------- clear ---------------- */
  globalThis.localStorage = fakeStorage();
  {
    const dc = D.createDraft(12, { delay: 0 });
    dc.pushMark({ page_no: 1, kind: 'ink', geo: [1, 2, 3, 4] });
    dc.flush();
    dc.clear();
    check('clear 之后 key 真的没了（「确认并清空」不能留残渣）',
          globalThis.localStorage.getItem(D.draftKey(12)) === null
          && D.readDraft(12).marks.length === 0);
  }

  /* ---------------- 插入的图片（0039） ----------------
     三条都要量，各自对应一种「静默丢图」：
       ① 队列里存得住（pushMark / readDraft 是**手写字段清单**，漏一行就丢）
       ② localMark 给界面的那条也带图（不然「还没传上去」那几秒只是一块「图丢了」占位）
       ③ sameMark 把 image 算进签名（两张**不同**的图放在同一位置时，坐标/文字完全一样） */
  globalThis.localStorage = fakeStorage();
  {
    const db = D.createDraft(14, { delay: 5 });
    const lid = db.pushMark({
      page_no: 1, kind: 'image', image: '/api/mark-images/gm_abc.png', geo: [10, 20, 210, 260],
    });
    const rec = db.marks().find((m) => m.lid === lid);
    check('① 队列记下了图片 URL', !!rec && rec.image === '/api/mark-images/gm_abc.png',
      rec && rec.image);
    db.flush();
    const back = D.readDraft(14).marks.find((m) => m.lid === lid);
    check('① 落盘再读回来仍然有图（漏字段 = 补发时变成「图丢了」占位）',
      !!back && back.image === '/api/mark-images/gm_abc.png', back && back.image);
    const lm = D.localMark(back);
    check('② 给界面画的那条也带图', lm.image === '/api/mark-images/gm_abc.png', lm.image);
    const other = {
      kind: 'image', image: '/api/mark-images/gm_zzz.png', geo: [10, 20, 210, 260], text: '',
    };
    check('③ 同位置的两张**不同**图不是同一笔（否则 reconcile 会把后插的那张出队丢掉）',
      D.sameMark(lm, other) === false, D.sameMark(lm, other));
    check('③ 同一条自己跟自己仍然是同一笔（去重还得能去）',
      D.sameMark(lm, Object.assign({}, lm)) === true, null);
  }

  /* ---------------- 防抖 ---------------- */
  globalThis.localStorage = fakeStorage();
  {
    const db = D.createDraft(13, { delay: 30 });
    db.pushMark({ page_no: 1, kind: 'ink', geo: [1, 2, 3, 4] });
    check('防抖期内还没落盘（连画几十笔不该触发几十次磁盘写）',
          globalThis.localStorage.getItem(D.draftKey(13)) === null);
    await sleep(90);
    check('防抖结束后自动落盘（老师忘了点保存也不会丢）',
          globalThis.localStorage.getItem(D.draftKey(13)) !== null);
  }

  console.log(`\n本地草稿：${total - fails.length}/${total} 通过`);
  if (fails.length) {
    console.log('失败：' + fails.join(' / '));
    process.exit(1);
  }
  process.exit(0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
