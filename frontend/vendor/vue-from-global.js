/* 把**全局那份 Vue**（vendor/vue.global.prod.js）以 ES 模块的形式导出。
 *
 * 为什么需要这个文件：
 *   `src/components/GradeMarkLayer.js` 是 ES 模块，开头写了
 *       import { ref, watch } from 'vue';
 *   裸模块名 `'vue'` 在浏览器里要靠 import map 解析。而工作台（workbench.html）
 *   用的是 **global 构建**（`window.Vue`），如果 import map 把它指到
 *   `vue.esm-browser.prod.js`，页面上就会同时存在**两份 Vue 实例** ——
 *   组件用 A 实例的 ref 建、却注册进 B 实例的 app，响应式与生命周期全错，
 *   而且这种错不报红，只表现为「画了笔迹但界面不动」。
 *
 *   所以这里让 import map 指向本文件：它只是把全局那份 Vue 的 API 转发出去，
 *   保证 ESM 组件与页面主脚本用的是**同一个** Vue。
 *
 * ⚠️ 只转发真正用到的那几个，不要 `export * from` —— global 构建上有些名字
 *    不是 ESM 的具名导出，星号导出会在严格模式下直接抛错。
 */
const V = window.Vue;

if (!V) {
  // 不该发生：本文件是 module（defer），一定排在 global 脚本之后。
  // 真发生了就让 import 这一侧立刻炸，而不是等到组件里用 ref 才炸。
  throw new Error('vue-from-global: window.Vue 不存在（vue.global.prod.js 没加载到？）');
}

export const ref = V.ref;
export const computed = V.computed;
export const watch = V.watch;
export const reactive = V.reactive;
export const onMounted = V.onMounted;
export const onBeforeUnmount = V.onBeforeUnmount;
export const nextTick = V.nextTick;
export const markRaw = V.markRaw;

export default V;
