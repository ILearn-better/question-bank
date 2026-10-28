/**
 * 作业批改页 · 题目导航栏 · 纯函数测试（activeSeqAt）。
 *
 *   node frontend/grade_nav_check.js            # 正常跑用例
 *   node frontend/grade_nav_check.js --sabotage # 反向验证（3 处改坏必须红）
 *
 * 测的是 `frontend/src/gradeNav.js` 导出的 `activeSeqAt(blocks, pageNo, linePx, imgH)`：
 * 「顶部线算法」—— 页图滚动时，页顶那根判题线落在哪个题块的顶边之上，就高亮哪一题的
 * `block.seq`。这是导航栏「卷到哪题亮哪题」的唯一数学来源，写错了不报错、只是高亮错位。
 *
 * 契约（team-lead 2026-10-27，写死在这里、不照实现反推）：
 *   - 空 blocks → null
 *   - box 坐标是**页归一化 0~1000**（HomeworkGrade.js:2001「坐标按页归一化（0~1000）」），
 *     不是像素；判题线 linePx 是像素、imgH 是图片像素高，命中条件 = imgH * y0 / 1000 <= linePx
 *   - 单块 linePx == 该块顶边像素（边界）→ 命中（等于也算）
 *   - linePx 在第一个块顶边之上 → null
 *   - 多块 linePx 落在第 2、3 块之间 → 第 2 块 seq（「最后一个顶边<=linePx 的块」）
 *   - 跨页块：pageNo 决定用哪一段的 y0（page_no 归一化，字符串 '1' 也算）
 *   - 不属于当前页的块被忽略（只剩它 → null）
 *   - box 缺失 / box[1]===0 → y0 按 0 处理，不抛异常
 *   - imgH 缩放被真的用上：y0=100、imgH=500 的顶边是 50px（不是 100），漏缩放会错
 *
 * 反向验证（sabotage）：三处改坏必须各自变红、还原全绿——
 *   S1  把「等于也算命中」改成「等于不算」            → 用例 ② 红
 *   S2  把「取最后一个满足」改成「取第一个就锁定」    → 用例 ④ 红
 *   S3  删掉「忽略非当前页」的过滤                    → 用例 ⑥ 红
 *   S4  把「取 y0 最大（不靠顺序）」改回「数组最后一个」→ 用例 ⑩ 红
 *
 * 依赖：`src/gradeNav.js`（fixer 落地）。文件没在时 **SKIP 并退出 0**，
 *        绝不冒充红、也绝不冒充绿 —— 没验就是没验。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = __dirname;                       // frontend/
const MOD = path.join(ROOT, 'src', 'gradeNav.js');

let total = 0;
const fails = [];
function check(label, got, want) {
  total += 1;
  const okk = JSON.stringify(got) === JSON.stringify(want);
  console.log('  [' + (okk ? 'OK ' : 'FAIL') + '] ' + label + ': ' + JSON.stringify(got)
    + (okk ? '' : '  期望 ' + JSON.stringify(want)));
  if (!okk) fails.push(label);
}
function checkTrue(label, cond, detail) {
  total += 1;
  console.log('  [' + (cond ? 'OK ' : 'FAIL') + '] ' + label
    + (detail === undefined ? '' : ': ' + detail));
  if (!cond) fails.push(label);
}

/** 题块工厂：seg 缺省按单页单段。box=[x0,y0,x1,y1]，y0 是归一化 0~1000。 */
const seg = (pageNo, box) => ({ page_no: pageNo, box });
const blk = (seq, pages, segs) => ({ seq, pages, segs });

/**
 * 乱序输入：数组顺序 = groupSegs 的 seq 升序（HomeworkGrade.js:287-288 按 seq 排），
 * 但老师可能先框页中那道（seq 0、y0 靠下），再回头框页顶那道（seq 1、y0 靠上）
 * —— 于是 seq 顺序与纵向顺序相反。正确判据必须与数组顺序无关，按顶边 y0 最大取。
 */
const outOfOrder = [
  blk(0, [1], [seg(1, [0, 600, 1000, 900])]),   // 下面那道（y0=600）
  blk(1, [1], [seg(1, [0, 100, 1000, 400])]),   // 上面那道（y0=100）
];

/* ============================== 用例 ============================== */

function runCases(fn) {
  const before = fails.length;

  // 1. 空 blocks
  check('① 空 blocks → null', fn([], 1, 0, 500), null);

  // 2 + 3. 单块：y0=100（归一化），imgH=500 → 顶边像素 = 500*100/1000 = 50
  const single = blk(1, [1], [seg(1, [0, 100, 1000, 400])]);
  check('② 单块 linePx == 顶边（50==50）→ 命中该 seq', fn([single], 1, 50, 500), 1);
  check('③ linePx 在顶边之上（49 < 50）→ null', fn([single], 1, 49, 500), null);

  // 4. 多块：y0 = 50/200/350 → 顶边像素 = 25/100/175
  const three = [
    blk(1, [1], [seg(1, [0, 50, 1000, 150])]),
    blk(2, [1], [seg(1, [0, 200, 1000, 300])]),
    blk(3, [1], [seg(1, [0, 350, 1000, 450])]),
  ];
  check('④ 多块 linePx=125（在 100 与 175 之间）→ 第 2 块 seq', fn(three, 1, 125, 500), 2);
  check('④′ linePx=100 恰好在第 2 块顶边 → 第 2 块（边界）', fn(three, 1, 100, 500), 2);
  check('④″ linePx=25 在第 1 块顶边 → 第 1 块', fn(three, 1, 25, 500), 1);
  check('④‴ linePx=24（< 25）→ null', fn(three, 1, 24, 500), null);

  // 5. 跨页块：pageNo 决定用哪一段。page1 段 y0=100（顶边 50），page2 段 y0=0（顶边 0）
  const cross = blk(7, [1, 2], [
    seg(1, [0, 100, 1000, 400]),
    seg(2, [0, 0, 1000, 500]),
  ]);
  check('⑤ pageNo=1 用 page_no=1 段（顶边 50）：linePx=150 → seq 7',
    fn([cross], 1, 150, 500), 7);
  check('⑤′ pageNo=1 且 linePx=49（< 50）→ null', fn([cross], 1, 49, 500), null);
  check('⑤″ pageNo=2 用 page_no=2 段（顶边 0）：linePx=50 → seq 7',
    fn([cross], 2, 50, 500), 7);
  check('⑤‴ pageNo=2 边界 linePx=0 → seq 7', fn([cross], 2, 0, 500), 7);

  // 6. 不属于当前页的块被忽略
  const otherPage = blk(1, [2], [seg(2, [0, 0, 1000, 500])]);
  check('⑥ 块在第 2 页、当前第 1 页 → 忽略（只剩它 → null）',
    fn([otherPage], 1, 300, 500), null);
  check('⑥′ 翻到第 2 页才命中', fn([otherPage], 2, 300, 500), 1);

  // 7. box 缺失 / box[1]===0 的容错（顶边按 0 处理）
  const zeroY = blk(1, [1], [seg(1, [0, 0, 1000, 300])]);
  check('⑦ box[1]===0 → y0 按 0 处理（linePx=5 命中）', fn([zeroY], 1, 5, 500), 1);
  const noBox = blk(1, [1], [{ page_no: 1 }]);
  let threw = null;
  try { threw = fn([noBox], 1, 5, 500); } catch (e) { threw = 'THREW:' + e.message; }
  check('⑦′ box 整个缺失 → 不抛异常、y0 按 0（linePx=5 命中）', threw, 1);

  // 8. page_no 字符串归一化
  const strPg = blk(1, [1], [seg('1', [0, 0, 1000, 300])]);
  check('⑧ seg.page_no 是字符串 "1" → 按数字归一（linePx=5 命中）', fn([strPg], 1, 5, 500), 1);
  check('⑧′ 字符串 "1" 不匹配第 2 页 → null', fn([strPg], 2, 5, 500), null);

  // 9. imgH 缩放真的被用上：顶边 50px，linePx 落在 50 与裸 y0(100) 之间，漏缩放就错
  check('⑨ 缩放判别：y0=100、imgH=500 → 顶边 50px，linePx=60 命中', fn([single], 1, 60, 500), 1);
  check('⑨′ 同一块 linePx=40（< 50）→ null', fn([single], 1, 40, 500), null);

  // 10. 乱序输入：数组顺序（seq 升序）≠ 纵向顺序。linePx=700、imgH=1000 → 顶边=y0，
  //     两块都满足 py<=700，正确判据取 y0 最大（600，页中那道），与谁在数组末尾无关。
  check('⑩ 乱序输入（seq 升序、纵向相反）→ 命中纵向在下面的 seq 0',
    fn(outOfOrder, 1, 700, 1000), 0);
  check('⑩′ 两块在数组里对调 → 仍是 seq 0（结果与数组顺序无关）',
    fn([outOfOrder[1], outOfOrder[0]], 1, 700, 1000), 0);

  // 11. pageNo 传字符串 / 0 / undefined：实现里 Number() 归一 + 越界不许抛
  check('⑪ pageNo 传字符串 "2"（块 pages:[2]）→ 按数字归一命中',
    fn([otherPage], '2', 300, 500), 1);
  let threw0 = null;
  try { threw0 = fn([single], 0, 50, 500); } catch (e) { threw0 = 'THREW:' + e.message; }
  check('⑪′ pageNo 传 0 → 不抛异常', threw0, null);
  let threwU = null;
  try { threwU = fn([single], undefined, 50, 500); } catch (e) { threwU = 'THREW:' + e.message; }
  check('⑪″ pageNo 传 undefined → 不抛异常', threwU, null);

  return fails.length - before;
}

/* ============================== 反向验证 ============================== */

/**
 * 把 gradeNav.js 复制到临时目录、改坏、import 后再跑用例，看该红的红不红。
 * 全程不动真 src/gradeNav.js —— 改的是临时副本。
 */
async function sabotage() {
  if (!fs.existsSync(MOD)) {
    console.log('[SKIP] src/gradeNav.js 还没落地，反向验证一并跳过');
    return;
  }
  const src = fs.readFileSync(MOD, 'utf8');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'grade_nav_sab_'));
  const writeAndImport = async (name, mutated) => {
    const p = path.join(tmp, name);
    fs.writeFileSync(p, mutated);
    const m = await import(pathToFileURL(p).href);
    return m.activeSeqAt;
  };

  const single = blk(1, [1], [seg(1, [0, 100, 1000, 400])]);
  const three = [
    blk(1, [1], [seg(1, [0, 50, 1000, 150])]),
    blk(2, [1], [seg(1, [0, 200, 1000, 300])]),
    blk(3, [1], [seg(1, [0, 350, 1000, 450])]),
  ];
  const otherPage = blk(1, [2], [seg(2, [0, 0, 1000, 500])]);

  try {
    // S1：等于边界不再命中 —— 把「py <= linePx 才算候选」改成「py < linePx 才算」。
    //     新写法是 `if (py > linePx) continue;`（改 `>` 为 `>=`）；旧写法是 `<= linePx`（改 `<`）。
    const CMP_NEW = 'if (imgH * y0 / 1000 > linePx) continue;';
    const CMP_OLD = 'imgH * y0 / 1000 <= linePx';
    let s1 = null;
    if (src.includes(CMP_NEW)) {
      s1 = src.replace(CMP_NEW, 'if (imgH * y0 / 1000 >= linePx) continue;');
    } else if (src.includes(CMP_OLD)) {
      s1 = src.replace(CMP_OLD, 'imgH * y0 / 1000 < linePx');
    }
    if (s1 !== null) {
      const fn = await writeAndImport('s1.js', s1);
      const got = fn([single], 1, 50, 500);
      checkTrue('S1 边界改成「等于不算」后，用例 ②（linePx==顶边）必须变红',
        got !== 1, '得到 ' + JSON.stringify(got) + '（期望不再是 1）');
    } else {
      checkTrue('S1 匹配到边界比较', false, '未匹配到比较式 —— 需按 fixer 实现调整');
    }

    // S2：取「最后一个满足」改成「取第一个就锁定」→ 用例 ④（第 2、3 块之间）会错成第 1 块
    const ASSIGN = 'active = b.seq';
    if (src.includes(ASSIGN)) {
      const fn = await writeAndImport('s2.js', src.replace(ASSIGN, 'if (active === null) active = b.seq'));
      const got = fn(three, 1, 125, 500);
      checkTrue('S2 把「最后一个满足」改成「第一个就锁定」后，用例 ④ 必须变红',
        got !== 2, '得到 ' + JSON.stringify(got) + '（期望不再是 2）');
    } else {
      checkTrue('S2 匹配到 `active = b.seq` 赋值', false, '未匹配到赋值式 —— 需按 fixer 实现调整');
    }

    // S3：删掉「忽略非当前页」的过滤 → 用例 ⑥（块在别的页）会命中而不是 null
    const FILTER = 'if (!(b.pages || []).includes(Number(pageNo))) continue;';
    const SEG = 'const seg = (b.segs || []).find((s) => Number(s.page_no || 1) === Number(pageNo));';
    if (src.includes(FILTER) && src.includes(SEG)) {
      let m = src.replace(FILTER, '/* 页过滤已移除 */');
      m = m.replace(SEG, 'const seg = (b.segs || [])[0] || b;');
      m = m.replace('if (!seg) continue;', '/* 段过滤已移除 */');
      const fn = await writeAndImport('s3.js', m);
      const got = fn([otherPage], 1, 300, 500);
      checkTrue('S3 删掉「忽略非当前页」后，用例 ⑥ 必须变红',
        got !== null, '得到 ' + JSON.stringify(got) + '（期望不再是 null）');
    } else {
      checkTrue('S3 匹配到「当前页过滤」', false, '未匹配到过滤表达式 —— 需按 fixer 实现调整');
    }

    // S4：乱序输入必须按「y0 最大」取，不能靠数组顺序。
    //     新写法是候选里 `if (... y0 > bestY) { bestY = y0; active = b.seq; }`；
    //     改回旧写法（无条件覆盖）后，原顺序用例 ⑩ 必须变红，且 A/B 结果随顺序变。
    let m4 = null;
    if (/&&\s*y0\s*>\s*bestY/.test(src)) {
      m4 = src.replace(/&&\s*y0\s*>\s*bestY/, '');
    } else if (/if\s*\(\s*y0\s*>\s*bestY\s*\)/.test(src)) {
      m4 = src.replace(/if\s*\(\s*y0\s*>\s*bestY\s*\)/, 'if (true)');
    }
    if (m4 !== null) {
      const fn = await writeAndImport('s4.js', m4);
      const outA = fn(outOfOrder, 1, 700, 1000);
      const outB = fn([outOfOrder[1], outOfOrder[0]], 1, 700, 1000);
      checkTrue('S4 改回「数组最后一个」后，乱序用例 ⑩ 必须变红',
        outA !== 0, '得到 ' + JSON.stringify(outA) + '（期望不再是 0）');
      checkTrue('S4 旧写法下结果随数组顺序变（⑩ 与 ⑩′ 不再同时为 0）',
        outA !== outB, 'A=' + JSON.stringify(outA) + ' B=' + JSON.stringify(outB));
    } else {
      console.log('  [SKIP] S4：gradeNav.js 还没有 bestY 守卫（fixer 未落地「取 y0 最大」），'
        + '反向验证待落地后补');
    }
  } finally {
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) { /* 删不掉无妨 */ }
  }
}

/* ============================== main ============================== */

async function main() {
  console.log('作业批改页 · 题目导航栏 · 纯函数测试（activeSeqAt）');

  if (!fs.existsSync(MOD)) {
    console.log('[SKIP] src/gradeNav.js 还没落地（fixer 未完成）—— activeSeqAt 一条都没跑，'
      + '不算通过、也不算失败。等 fixer 落地后再跑。');
    return 0;
  }

  const mod = await import(pathToFileURL(MOD).href);
  const fn = mod.activeSeqAt;
  if (typeof fn !== 'function') {
    console.log('  [FAIL] gradeNav.js 没有导出 activeSeqAt 函数');
    return 1;
  }

  const red = runCases(fn);

  if (process.argv.includes('--sabotage')) {
    await sabotage();
  }

  console.log('\n共 ' + total + ' 项，失败 ' + fails.length + ' 项');
  if (fails.length) {
    fails.forEach((f) => console.log('  - ' + f));
    return 1;
  }
  console.log('全部通过');
  return 0;
}

main().then((code) => process.exit(code)).catch((e) => {
  console.error('grade_nav_check 崩了：', e);
  process.exit(1);
});
