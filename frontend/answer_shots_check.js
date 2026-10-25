// 答案多图 + 剪贴板粘贴的前端自检（静态断言，不需要起服务）。
//
// 为什么需要它：这几处接线最怕「看起来改好了、其实没接上」——
//   ① 绑定写在 mounted 里、解绑忘了写（切页面后全局监听越积越多）
//   ② 粘贴判据写反了（把老师粘的文字吞掉了，这是最糟的）
//   ③ 多图分支没走到（还是只发一张，跨页答案照旧断）
//   ④ 缩略图的删除按钮是 submit（在 form 里会触发保存）
// 这些都不报错，只在某个具体操作下表现为「怎么没反应」。
//
// ⚠️ 断言一律先剥注释（`codeOnly`）：锚点落在注释里会造成 0/0 的假绿（踩过）。
//
// 跑法：node answer_shots_check.js   （退出码 0 = 全过）
const fs = require('fs');
const path = require('path');

const BASE = __dirname;
let pass = 0, fail = 0;
const fails = [];

function check(name, ok, detail) {
  if (ok) { pass++; console.log('  [OK ] ' + name); }
  else {
    fail++; fails.push(name);
    console.log('  [FAIL] ' + name + (detail !== undefined ? '   <- ' + detail : ''));
  }
}

const html = fs.readFileSync(path.join(BASE, 'entry.html'), 'utf8');

/** JS 部分：剥掉 // 与 /* *\/ 注释（注释只做说明，不当断言依据）。 */
function codeOnly(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:\\])\/\/[^\n]*/g, '$1');
}

/** 模板部分：**只剥 HTML 注释**。
 *
 *  ⚠️ 不能套用 JS 那套：模板里有 `accept="image/*"`，那个 `/*` 会被当成
 *     块注释的开头，一路剥到后面某个 `*\/` —— 于是「文件 input 在答案阶段
 *     允许多选」这条断言读到的是一片空白，报假失败（本轮踩过）。 */
function tplOnly(src) {
  return src.replace(/<!--[\s\S]*?-->/g, '');
}

// 内联 script（零构建页只有一段）
const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const js = codeOnly(scripts.join('\n'));
const tpl = tplOnly(html.slice(0, html.indexOf('<script')));   // <style> + 模板

// ---------------------------------------------------------------- ① 状态
console.log('\n==== ① 状态：多张截图与忙碌标记 ====');
check('data 里有 answerShots 数组', /answerShots:\s*\[\]/.test(js), '');
check('data 里有 shotBusy（读图/拼图时按钮转圈）', /shotBusy:\s*false/.test(js), '');
check('answerShots 元素带 id（v-for 的 key 要稳定，否则删中间一张会错位）',
      /id:\s*Date\.now\(\)/.test(js), '');

// ---------------------------------------------------------------- ② 粘贴接线
console.log('\n==== ② 剪贴板：绑定 / 解绑 / 判据 ====');
check('mounted 里 addEventListener("paste")',
      /addEventListener\('paste',\s*this\.onPaste\)/.test(js), '');
check('beforeUnmount 里 removeEventListener（不然切页面后越积越多）',
      /beforeUnmount\s*\(\s*\)/.test(js) && /removeEventListener\('paste',\s*this\.onPaste\)/.test(js), '');
check('★ 剪贴板里带文字就**不插手**（不能吞掉老师粘的文字）',
      /if\s*\(text\)\s*return;/.test(js), '');
check('只在答案阶段接手粘贴（题干阶段别抢）',
      /onPaste\(e\)\s*\{[\s\S]{0,120}?this\.stage\s*!==\s*'answer'/.test(js), '');
check('接手时要 preventDefault（不然浏览器还会把图当文件默认处理）',
      /e\.preventDefault\(\)/.test(js), '');
check('只认 image/* 的 file 项',
      /it\.type\.indexOf\('image\/'\)\s*===\s*0/.test(js), '');

// ---------------------------------------------------------------- ③ 多图落成一张
console.log('\n==== ③ 提交：单张走原上传、多张走竖拼 ====');
check('commitShots 存在', /async\s+commitShots\s*\(/.test(js), '');
check('多张时调 /api/images/stitch', /'\/api\/images\/stitch'/.test(js), '');
check('拼图请求体带 images 数组', /images:\s*this\.answerShots\.map\(/.test(js), '');
check('单张仍走原来的 /api/uploads/image（不重新编码）',
      /'\/api\/uploads\/image'/.test(js), '');
check('拼图结果写回 form.answer_image',
      /this\.form\.answer_image\s*=\s*r\.url/.test(js), '');
check('太高被缩放时**明确说出来**（不能让老师以为图就是那样）',
      /if\s*\(r\.scaled\)/.test(js), '');
check('removeShot 删一张后重新拼接',
      /async\s+removeShot\s*\([\s\S]{0,160}?commitShots\(\)/.test(js), '');

// ---------------------------------------------------------------- ④ 识别走多图
console.log('\n==== ④ AI 识别：答案阶段多图一次送 ====');
check('aiRecognize 里有 useMulti 判断',
      /const\s+useMulti\s*=\s*mode\s*===\s*'answer'\s*&&\s*this\.answerShots\.length\s*>\s*1/.test(js), '');
check('多图分支发 images 而不是 image',
      /payload\s*=\s*\{[\s\S]{0,160}?images:\s*this\.answerShots\.map/.test(js), '');
check('单图分支仍然发 image（老路径没被删）',
      /payload\s*=\s*\{\s*image,\s*mode,\s*hint/.test(js), '');

// ---------------------------------------------------------------- ⑤ 清空联动
console.log('\n==== ⑤ 换成单张时清空多图列表（不然缩略图和实际用的图对不上） ====');
for (const fn of ['clearAnswerImage', 'useCrop', 'copyToAnswer']) {
  const i = js.indexOf(fn + '(');
  check(`${fn} 里清空 answerShots`,
        i >= 0 && /this\.answerShots\s*=\s*\[\]/.test(js.slice(i, i + 400)), '');
}

// ---------------------------------------------------------------- ⑥ 模板
console.log('\n==== ⑥ 模板：缩略图条 / 多选 / 不让删除按钮变成 submit ====');
check('模板里有 shot-strip', /class="shot-strip"/.test(tpl), '');
check('只在多张时显示缩略图条', /v-if="answerShots\.length\s*>\s*1"/.test(tpl), '');
check('缩略图 v-for 带 :key', /v-for="\(s, i\) in answerShots"\s+:key="s\.id"/.test(tpl), '');
check('删除按钮是 type="button"（在 form 里否则会触发保存）',
      /<button[^>]*class="shot-del"[^>]*type="button"|<button[^>]*type="button"[^>]*class="shot-del"/.test(tpl)
      || /<button\s+type="button"\s+class="shot-del"/.test(tpl), '');
check('文件 input 在答案阶段允许多选', /:multiple="stage === 'answer'"/.test(tpl), '');
check('界面上说清了「可以 Ctrl+V 粘贴」', /Ctrl\+V/.test(tpl), '');
check('答案图标签上写明「几张截图竖拼」', /张截图竖拼/.test(tpl), '');

// ---------------------------------------------------------------- ⑦ 卫生
console.log('\n==== ⑦ 模板卫生与语法 ====');
const selfClose = (tpl.match(/<el-[a-z-]*[^>]*\/>/g) || []).length;
check('没有自闭合的 el- 组件（in-DOM 模板里 /> 会白屏）', selfClose === 0, selfClose + ' 处');
check('shot-strip 的 CSS 存在', /\.shot-strip\s*\{/.test(tpl), '');
check('shot-del 的 CSS 存在', /\.shot-del\s*\{/.test(tpl), '');

try {
  new Function(js);            // 只做语法解析，不执行
  check('内联 script 语法能过解析', true, '');
} catch (e) {
  check('内联 script 语法能过解析', false, e.message);
}

// ---------------------------------------------------------------- 汇总
console.log('\n' + '='.repeat(50));
if (fail) {
  console.log(`失败 ${fail} 项 / 通过 ${pass} 项`);
  fails.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log(`全部通过（${pass} 项）`);
process.exit(0);
