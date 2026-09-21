// 应用外壳：左侧导航 + 内容区 + 消息提示。
import { onMounted } from 'vue';
import { loadCurricula, loadDims, loadStudents, state, toasts } from './store.js';

export default {
  name: 'App',
  setup() {
    onMounted(() => {
      // 体系与能力维度是全站共用的基础数据，启动时各取一次。
      // 学生列表也要取 —— 侧栏的「共 N 位学生」读的是它，
      // 不取的话在工作台页永远显示 0，直到进过一次学生页才刷新。
      loadCurricula().catch(() => {});
      loadDims().catch(() => {});
      loadStudents().catch(() => {});
    });
    return { toasts, state };
  },
  template: `
  <div class="layout">
    <aside class="sidebar">
      <div class="brand">
        <div class="name">拾课</div>
        <div class="en">SHIKE</div>
      </div>
      <nav class="nav">
        <router-link to="/">今日工作台</router-link>
        <router-link to="/students">学生</router-link>
        <router-link to="/schedule">课表与课时费</router-link>
        <a href="/entry.html">题库录题</a>
        <router-link to="/papers">出卷</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
      <div class="foot">
        数据存在本机<br>共 {{ state.students.length }} 位学生
      </div>
    </aside>

    <main class="content">
      <router-view />
    </main>

    <div class="toasts">
      <div v-for="t in toasts" :key="t.id" class="toast"
           :class="t.type === 'err' ? 'err' : (t.type === 'ok' ? 'ok' : '')">
        {{ t.message }}
      </div>
    </div>
  </div>`,
};
