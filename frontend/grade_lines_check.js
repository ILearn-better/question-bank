// 用 node 直接量「分割线几何」（gradeLines.js 是纯函数，不碰 DOM）。
//
// 为什么值得单独测：分割线切出来的段**直接决定每一道题裁什么图**。
// 少切一刀 → 两道题挤在一张图里，模型只能判出一道；
// 多切一刀 → 一道题被腰斩，下半段变成「读不到题」。
// 这两种错在界面上都只是「题数不对」，很难看出是哪一刀画错了。
//
// 跑法：node grade_lines_check.js
const path = require('path');
const { pathToFileURL } = require('url');

let total = 0;
const fails = [];

// 参数顺序与 mark_geom_check.js 一致：(名字, 布尔, 细节)
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

(async () => {
  const L = await import(pathToFileURL(path.join(__dirname, 'src', 'gradeLines.js')).href);

  /* ---------------- 线 → 段 ---------------- */
  check('没有线 → 整页一段（全切，不是「切出 0 段」）',
        J(L.bandsOfPage([])) === J([[0, 0, 1000, 1000]]), L.bandsOfPage([]));

  check('1 条线 → 2 段，且第一段从页顶开始（批改是全切，页顶不算卷头）',
        J(L.bandsOfPage([400])) === J([[0, 0, 1000, 400], [0, 400, 1000, 1000]]),
        L.bandsOfPage([400]));

  check('3 条线 → 4 段，顺序按 y 从小到大',
        J(L.bandsOfPage([700, 200, 450])) === J([
          [0, 0, 1000, 200], [0, 200, 1000, 450],
          [0, 450, 1000, 700], [0, 700, 1000, 1000],
        ]), L.bandsOfPage([700, 200, 450]));

  check('段一律**整页宽**（x0=0, x1=1000）—— 这是后端零改动的关键',
        L.bandsOfPage([300, 600]).every((b) => b[0] === 0 && b[2] === 1000),
        L.bandsOfPage([300, 600]));

  // 太薄的带必须丢：后端 REGION_MIN_SIDE=20，留下它只会吃一个 422
  check('两条线挨太近（< 20）→ 那一段不生成',
        L.bandsOfPage([100, 108]).length === 2 && J(L.bandsOfPage([100, 108])) === J([[0, 0, 1000, 100], [0, 108, 1000, 1000]]),
        L.bandsOfPage([100, 108]));

  // 贴着页边的两条线本身没错，错的是它们与页边之间那 **5 单位**的窄条 ——
  // 窄条被丢，其余照常切。这也说明「丢薄带」是按带判的，不是按线判的。
  check('线贴着页边（y=5 / y=995）→ 只丢掉页边那两条 5 单位的窄条',
        J(L.bandsOfPage([5, 500, 995])) === J([
          [0, 5, 1000, 500], [0, 500, 1000, 995],
        ]), L.bandsOfPage([5, 500, 995]));

  check('同一个 y 点两下 → 去重，不会切出零高度段',
        J(L.bandsOfPage([300, 300, 300])) === J([[0, 0, 1000, 300], [0, 300, 1000, 1000]]),
        L.bandsOfPage([300, 300, 300]));

  check('0 / 1000 本身不当线（它们已经是页边）',
        J(L.bandsOfPage([0, 1000])) === J([[0, 0, 1000, 1000]]), L.bandsOfPage([0, 1000]));

  check('线越界 → 夹进 0~1000，不是画到纸外',
        J(L.bandsOfPage([-80, 1200])) === J([[0, 0, 1000, 1000]]), L.bandsOfPage([-80, 1200]));

  check('线是字符串 / 小数也认（接口回来常常是 JSON 数字串）',
        J(L.bandsOfPage(['400.6', 800])) === J([[0, 0, 1000, 401], [0, 401, 1000, 800], [0, 800, 1000, 1000]]),
        L.bandsOfPage(['400.6', 800]));

  check('坏值（null / 非数字）当 0 处理，不抛异常',
        J(L.bandsOfPage([null, 'abc', 500])) === J([[0, 0, 1000, 500], [0, 500, 1000, 1000]]),
        L.bandsOfPage([null, 'abc', 500]));

  /* ---------------- 覆盖率 ---------------- */
  check('整页宽判定：贴边少许也算（框选没法精确压在 0/1000 上）',
        L.isFullWidth([3, 100, 996, 400]) === true, L.isFullWidth([3, 100, 996, 400]));
  check('整页宽判定：右边只到 800 → 不是',
        L.isFullWidth([0, 100, 800, 400]) === false);
  check('整页宽判定：坏值 → false（不抛）', L.isFullWidth(null) === false);

  check('完全落在里面 → 覆盖比 1', L.overlapRatio([0, 100, 1000, 200], [0, 0, 1000, 1000]) === 1);
  check('完全不相交 → 0', L.overlapRatio([0, 100, 1000, 200], [0, 500, 1000, 600]) === 0);
  check('被切一半 → 约 0.5',
        Math.abs(L.overlapRatio([0, 100, 1000, 200], [0, 100, 1000, 150]) - 0.5) < 1e-9,
        L.overlapRatio([0, 100, 1000, 200], [0, 100, 1000, 150]));

  /* ---------------- 从段反推线（重开页面后继续加线） ---------------- */
  check('两段相邻的整页宽段 → 反推出一条线',
        J(L.linesFromSegs([
          { box: [0, 0, 1000, 300] }, { box: [0, 300, 1000, 1000] },
        ])) === J([300]), L.linesFromSegs([
          { box: [0, 0, 1000, 300] }, { box: [0, 300, 1000, 1000] },
        ]));

  // 两个整页宽段之间夹着一个框选段：线只反推**一条**，放在两段之间的中点 350。
  // 为什么不放 300 或 400：中间那一带已经被框管了，线放边界上会把框的上下沿
  // 重复切一刀；放中点则「重切时那一带照样被框覆盖掉」，结果与现在完全一致。
  check('框选段（不整页宽）不参与反推，两段整页宽段之间只反推一条中点线',
        J(L.linesFromSegs([
          { box: [0, 0, 1000, 300] }, { box: [80, 300, 900, 400] }, { box: [0, 400, 1000, 1000] },
        ])) === J([350]), L.linesFromSegs([
          { box: [0, 0, 1000, 300] }, { box: [80, 300, 900, 400] }, { box: [0, 400, 1000, 1000] },
        ]));

  check('整页只有一段（没有可切的边界）→ 没有线',
        J(L.linesFromSegs([{ box: [0, 0, 1000, 1000] }])) === J([]));

  /* ---------------- 重切：框优先 ---------------- */
  const cur = [
    // 老师上一轮框出来的第 3 题（有 id、有题号、seq 已经分好组）
    { id: 7, seq: 2, q_no: '3', box: [60, 220, 940, 460], src: 'box', page_no: 1 },
    // 上一次切出来的线段（会在重切时被整批替换）
    { id: null, seq: 0, q_no: '', box: [0, 0, 1000, 220], src: 'line', page_no: 1 },
  ];
  const next = L.reslicePage(cur, [220, 460, 700]);
  check('重切后：框选段原样保留（id / seq / q_no 一个不丢）',
        next.some((s) => s.id === 7 && s.seq === 2 && s.q_no === '3'), next);
  // 线是 [220, 460, 700] → 4 条带；中间那条 220~460 被框 [60,220,940,460] 整个盖住，
  // 不再生成 → 剩下 3 条线段 + 1 条框段。
  check('重切后：线段按新线重建（被框盖住的那条不算）',
        next.filter((s) => s.src === 'line').length === 3, next);
  check('重切后：被框覆盖的那一带不再生成线段（否则第 3 题会变成三道）',
        !next.some((s) => s.src === 'line' && s.box[1] === 220 && s.box[3] === 460), next);
  check('重切后：没被框覆盖的带照常生成',
        next.some((s) => s.src === 'line' && s.box[1] === 0 && s.box[3] === 220), next);

  // 这条是**真机挂载检查抓出来的**：没有线的时候如果按「全切」生成整页一段，
  // 老师只是拖了一个框，页面上就凭空多一个覆盖整页的幽灵段（框数 5 → 6）。
  check('没画线时重切 → 一段都不生成（不能凭空长出整页那一段）',
        L.reslicePage([], []).length === 0, L.reslicePage([], []));
  check('没画线 + 已有 2 个框 → 还是 2 段，不多不少',
        L.reslicePage([
          { id: 1, seq: 0, box: [40, 60, 900, 300], src: 'box' },
          { id: 2, seq: 1, box: [40, 340, 900, 620], src: 'box' },
        ], []).length === 2, L.reslicePage([
          { id: 1, seq: 0, box: [40, 60, 900, 300], src: 'box' },
          { id: 2, seq: 1, box: [40, 340, 900, 620], src: 'box' },
        ], []).length);

  /* ---------------- 按几何重排 seq ---------------- */
  const segs = [
    { page_no: 2, seq: 5, box: [0, 0, 1000, 300] },     // 第 2 页第一段
    { page_no: 1, seq: 3, box: [0, 600, 1000, 1000] },  // 第 1 页最后一段
    { page_no: 1, seq: 4, box: [0, 100, 1000, 600] },
  ];
  const sorted = L.sortBlocksByGeometry(segs);
  check('按几何排：第 1 页的排在前面，页内按 y0',
        J(sorted.filter((s) => s.page_no === 1).sort((a, b) => a.box[1] - b.box[1]).map((s) => s.box[1]))
        === J([100, 600]), sorted);
  check('seq 收口成 0..n-1（不留空洞）',
        J(Array.from(new Set(sorted.map((s) => s.seq))).sort((a, b) => a - b)) === J([0, 1, 2]), sorted);
  check('第 1 页 y0=100 的那段拿到 seq 0（阅读顺序第一）',
        sorted.find((s) => s.page_no === 1 && s.box[1] === 100).seq === 0, sorted);

  // 跨页题：两段同 seq，重排后仍然同 seq
  const cross = [
    { page_no: 2, seq: 0, box: [0, 0, 1000, 400] },
    { page_no: 1, seq: 0, box: [0, 700, 1000, 1000] },
    { page_no: 1, seq: 1, box: [0, 0, 1000, 700] },
  ];
  const cs = L.sortBlocksByGeometry(cross);
  check('跨页题的两段重排后**仍然同一个 seq**（分组不能被重排拆开）',
        cs.filter((s) => s.box[1] === 700 || (s.page_no === 2 && s.box[1] === 0))
          .every((s) => s.seq === cs.find((x) => x.page_no === 2).seq), cs);
  check('跨页题按「第一段所在页」定位 → 排在第 1 页那一题之后',
        cs.find((s) => s.page_no === 2).seq === 1, cs);

  /* ---------------- 删除段 → 删哪条线 ---------------- */
  check('删中间那段 → 删它上边那条线（并进上一段）',
        L.lineToDropFor([0, 300, 1000, 600], [300, 600, 800]) === 300);
  check('删第一段（上边没有线）→ 删下边那条',
        L.lineToDropFor([0, 0, 1000, 300], [300, 600]) === 300);
  check('一条线都没有（整页一段）→ 返回 null',
        L.lineToDropFor([0, 0, 1000, 1000], []) === null);

  /* ---------------- 自证：断言器不是空转 ---------------- */
  // 故意写一条必然错的期望。如果 check 是空转的（永远 OK），这里就抓不到，
  // 后面「全绿」也就没有意义 —— 这是本项目测试的老规矩。
  // 输出里那一条 FAIL 是**故意的**，这里把它从统计与日志里摘掉，免得看着像真失败。
  const before = fails.length;
  const totalBefore = total;
  const log = console.log;
  console.log = () => {};                       // 静音这一条
  check('【自证】故意写错的期望必须被判红', L.bandsOfPage([500]).length === 99);
  console.log = log;
  if (fails.length !== before + 1) {
    console.log('\n!!!! 断言器空转：错的期望也报 OK，上面所有结论都不可信');
    process.exit(1);
  }
  fails.length = before;                        // 撤掉自证那一条
  total = totalBefore;
  console.log('  [OK ] 【自证】断言器有效（故意写错的期望确实被判红）');

  console.log(`\n分割线几何：${total - fails.length}/${total} 通过`);
  if (fails.length) {
    console.log('失败：' + fails.join(' / '));
    process.exit(1);
  }
  process.exit(0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
