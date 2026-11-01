// 用 node 直接量「标注几何」（markGeom.js 是纯函数，不碰 DOM、不 import 任何东西）。
//
// 为什么值得单独测：画笔画得准不准，全靠这一层换算。屏幕上一个偏 2mm 的偏差，
// 到了 A4 导出就是半厘米 —— 而这类偏差**界面看不出来**，只有量数值才知道。
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

(async () => {
  const m = await import(pathToFileURL(path.join(__dirname, 'src', 'markGeom.js')).href);

  const rect = { left: 100, top: 50, width: 200, height: 400 };
  const p = m.pagePoint(200, 250, rect);
  check('屏幕中心 → (500,500)', p[0] === 500 && p[1] === 500, p);
  check('左上角 → (0,0)', JSON.stringify(m.pagePoint(100, 50, rect)) === '[0,0]',
        m.pagePoint(100, 50, rect));
  check('右下角 → (1000,1000)', JSON.stringify(m.pagePoint(300, 450, rect)) === '[1000,1000]',
        m.pagePoint(300, 450, rect));

  const s = m.pagePoint(-500, 9999, rect);
  check('点出界 → 夹回 0 / 1000（不能画到纸外）', s[0] === 0 && s[1] === 1000, s);

  const t = m.pagePoint(200, 150, rect);
  check('x / y 各按宽高换算（不是同一个比例，否则竖版页面会偏）',
        t[0] === 500 && t[1] === 250, t);

  check('图还没渲染出来（宽高 0）→ null',
        m.pagePoint(10, 10, { left: 0, top: 0, width: 0, height: 0 }) === null);

  check('点在线段上 → 距离 0', m.segDist(50, 50, 0, 0, 100, 100) === 0);
  check('点到水平线段的距离 = 纵差',
        Math.abs(m.segDist(50, 80, 0, 0, 100, 0) - 80) < 1e-6);
  check('端点外的点按端点算（不会顺着延长线误判）',
        Math.abs(m.segDist(-30, 0, 0, 0, 100, 0) - 30) < 1e-6);

  const bb = m.bboxOf([[10, 20], [300, 40], [120, 500]]);
  check('包围盒取到最小 / 最大',
        JSON.stringify(bb) === JSON.stringify([10, 20, 300, 500]), bb);
  check('空点列的包围盒是全 0（不抛异常）',
        JSON.stringify(m.bboxOf([])) === '[0,0,0,0]', m.bboxOf([]));

  const fp = m.flatPoints([[10.4, 20.6], [-5, 2000]]);
  check('点列压平成 [x,y,…]，取整并夹紧',
        JSON.stringify(fp) === JSON.stringify([10, 21, 0, 1000]), fp);
  check('空点列 → 空数组', m.flatPoints([]).length === 0);

  // ---- 拖动平移（文字框 / √× / 框都用这一份）----
  const sg = m.shiftGeo([100, 200, 300, 400], 50, -100);
  check('包围盒整体平移：x 加 dx、y 加 dy（不是两个都加 dx）',
        JSON.stringify(sg) === JSON.stringify([150, 100, 350, 300]), sg);
  check('零位移 = 原样返回（拖回原地不该改数据）',
        JSON.stringify(m.shiftGeo([10, 20, 30, 40], 0, 0)) === '[10,20,30,40]');
  /* ⚠️ 2026-11-01 改口径：这里原来断言 `[900,900,990,990] + (100,100)` →
     `[1000,1000,1000,1000]`，也就是「框被压成一个点」。那**不是**期望行为，
     是把缺陷写成了断言：真浏览器实测，文本框拖到右边会从 92px 宽压成 0，
     `sized` 判据失效 → 文字退回零面积点样式往上一跳，用户报的「莫名跳回原位」就是它。
     新口径：**夹平移量、整组刚性平移**（形状永远不变形，顶多停在页边拖不动）。 */
  check('拖到页边：整组停住，**宽高不变**（不再被压成一个点）',
        JSON.stringify(m.shiftGeo([900, 900, 990, 990], 100, 100)) === '[910,910,1000,1000]',
        m.shiftGeo([900, 900, 990, 990], 100, 100));
  check('已贴右边再往右拖 → 原样不动（框宽 90 保住）',
        JSON.stringify(m.shiftGeo([910, 900, 1000, 990], 500, 0)) === '[910,900,1000,990]',
        m.shiftGeo([910, 900, 1000, 990], 500, 0));
  check('已贴左上再往左上拖 → 原样不动（框宽高都保住）',
        JSON.stringify(m.shiftGeo([0, 0, 90, 90], -500, -500)) === '[0,0,90,90]',
        m.shiftGeo([0, 0, 90, 90], -500, -500));
  check('部分出界：只截平移量（宽 100 不变，贴到 1000）',
        JSON.stringify(m.shiftGeo([800, 600, 900, 700], 200, 0)) === '[900,600,1000,700]',
        m.shiftGeo([800, 600, 900, 700], 200, 0));
  check('★ 任何方向的平移都不改变宽高（刚性平移这一条要恒成立）',
        [[-999, -999], [-7, 3], [0, 0], [7, -3], [999, 999]].every(([dx, dy]) => {
          const g = m.shiftGeo([200, 300, 420, 380], dx, dy);
          return g[2] - g[0] === 220 && g[3] - g[1] === 80;
        }),
        JSON.stringify(m.shiftGeo([200, 300, 420, 380], 999, 999)));
  check('扁平点列同样按奇偶分 x/y（手绘线也能整体挪）',
        JSON.stringify(m.shiftGeo([0, 0, 10, 10], 5, 7)) === '[5,7,15,17]',
        m.shiftGeo([0, 0, 10, 10], 5, 7));
  check('空 / 脏数据 → 空数组（不抛异常）',
        m.shiftGeo([], 1, 1).length === 0
        && m.shiftGeo(null, 1, 1).length === 0
        && m.shiftGeo([10, NaN], 1, 1).length === 0);

  console.log('');
  console.log(`共 ${total} 项，失败 ${fails.length} 项`);
  if (fails.length) process.exit(1);
  console.log('全部通过 ✅');
})();
