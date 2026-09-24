// 把一批 LaTeX 公式转成 MathML —— 注意：这是 **JS** 文件，不是 Python 模块。
//
// 为什么用 node + KaTeX 而不是 Python 库：
//   1. 前端预览用的就是这份 KaTeX（frontend/vendor/katex/），同一个引擎出 MathML，
//      导出的公式和屏幕上看到的结构完全一致，不会出现"预览对了导出不对"；
//   2. 不引入新的 Python 依赖，也不需要联网装 latex2mathml 之类的包。
//
// 用法（由 app/adapters/notes_math.py 调用）：
//   node katex_mathml.js <katex.min.js 路径>   ← 公式数组从 stdin 读（JSON 字符串数组）
//                                             → 结果同样以 JSON 字符串数组从 stdout 输出
//   每项是 <math>…</math> 片段；转换失败给空串，调用方据此回退成纯文本。
//
// ⚠️ 必须用**脚本文件**方式启动，不能 `node -e "..."`：
//    实测这台机器上 `node -e` 会在启动阶段崩（Assertion failed: ncrypto::CSPRNG），
//    而 `node file.js` 完全正常。别为了少写一个文件改成 -e。
//
// stdin 用 JSON 而不是行分隔：公式里本来就可能有多行（\begin{cases} 之类），
// 用换行当分隔符一定会出错。

const katexPath = process.argv[2];
if (!katexPath) {
  console.error('用法: node katex_mathml.js <katex.min.js 路径>  (公式数组走 stdin)');
  process.exit(2);
}

let katex;
try {
  katex = require(katexPath);
} catch (e) {
  console.error('加载 KaTeX 失败: ' + e.message);
  process.exit(3);
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => { input += d; });
process.stdin.on('end', () => {
  let texs;
  try {
    texs = JSON.parse(input);
  } catch (e) {
    console.error('stdin 不是合法 JSON: ' + e.message);
    process.exit(4);
  }
  if (!Array.isArray(texs)) texs = [texs];

  const out = texs.map((tex) => {
    try {
      const html = katex.renderToString(String(tex), {
        output: 'mathml',      // 只要 MathML，不要那堆定位用的 span
        throwOnError: false,   // 语法错就渲染成红色原文，不要抛异常打断整篇导出
        displayMode: true,     // 独立公式：影响 \sum 上下标等排版
      });
      const m = html.match(/<math[\s\S]*<\/math>/);
      return m ? m[0] : '';
    } catch (e) {
      return '';
    }
  });
  process.stdout.write(JSON.stringify(out));
});
