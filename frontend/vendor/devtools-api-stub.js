// @vue/devtools-api 空实现。
//
// 为什么需要它：vue-router 的浏览器版构建（vue-router.esm-browser.js）在顶层
// `import { setupDevtoolsPlugin } from '@vue/devtools-api'`，而这个包没有可直接
// <script type="module"> 引用的浏览器构建。它只在开发模式往 Vue Devtools 发数据，
// 对生产环境毫无作用 —— 一个空的命名导出即可安全替代，还省掉一个 CDN 依赖。
export function setupDevtoolsPlugin() {}
export default {};
