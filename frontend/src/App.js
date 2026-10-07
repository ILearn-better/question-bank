// 应用外壳：左侧导航 + 内容区 + 消息提示。
import { onMounted, ref } from 'vue';
import { loadCurricula, loadDims, loadStudents, state, toasts } from './store.js';

const NAV_KEY = 'shike.navCollapsed';

export default {
  name: 'App',
  setup() {
    // 侧栏可收起：小屏幕上腾地方。状态存在本机，刷新后保持原样。
    const navCollapsed = ref(localStorage.getItem(NAV_KEY) === '1');
    function toggleNav() {
      navCollapsed.value = !navCollapsed.value;
      try { localStorage.setItem(NAV_KEY, navCollapsed.value ? '1' : '0'); } catch (e) { /* 无痕模式等，忽略 */ }
    }

    onMounted(() => {
      // 体系与能力维度是全站共用的基础数据，启动时各取一次。
      // 学生列表也要取 —— 侧栏的「共 N 位学生」读的是它，
      // 不取的话在工作台页永远显示 0，直到进过一次学生页才刷新。
      loadCurricula().catch(() => {});
      loadDims().catch(() => {});
      loadStudents().catch(() => {});
    });
    return { toasts, state, navCollapsed, toggleNav };
  },
  template: `
  <div class="layout" :class="{ 'nav-collapsed': navCollapsed }">
    <aside class="sidebar">
      <button class="nav-toggle" @click="toggleNav"
              :title="navCollapsed ? '展开侧栏' : '收起侧栏'">{{ navCollapsed ? '»' : '«' }}</button>
      <template v-if="!navCollapsed">
      <div class="brand">
        <div class="name">拾课</div>
        <div class="en">SHIKE</div>
      </div>
      <nav class="nav">
        <router-link to="/">今日工作台</router-link>
        <router-link to="/students">学生</router-link>
        <router-link to="/schedule">课表与课时费</router-link>
        <a href="/entry.html">题库录题</a>
        <a href="/batch.html">批量入库</a>
        <router-link to="/papers">出卷</router-link>
        <router-link to="/notes">笔记</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
      <div class="foot">
        数据存在本机<br>共 {{ state.students.length }} 位学生
      </div>
      </template>
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
