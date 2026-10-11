// 学生列表。核心是「一眼看到谁的反馈欠着」——待写反馈数直接列出来。
//
// 新增 / 编辑用的是共用的 StudentFormDialog（课表导入那边补学生档案也走同一份），
// 这样学生字段只有一处定义 —— 见 components/StudentFormDialog.js 的文件头。
import { computed, onMounted, ref } from 'vue';
import { studentsApi } from '../api.js';
import StudentFormDialog from '../components/StudentFormDialog.js';
import { fail, loadCurricula, loadStudents, money, ok, state } from '../store.js';

export default {
  name: 'Students',
  components: { StudentFormDialog },
  setup() {
    const rows = ref([]);
    const loading = ref(true);
    const filter = ref('active');
    const keyword = ref('');
    const editing = ref(null);        // { mode: 'create' | 'edit', student } | null

    async function load() {
      loading.value = true;
      try {
        rows.value = await studentsApi.list();
        await loadStudents(true);
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    const visible = computed(() => {
      const kw = keyword.value.trim().toLowerCase();
      return rows.value.filter((s) => {
        if (filter.value !== 'all' && s.status !== filter.value) return false;
        if (!kw) return true;
        return `${s.name} ${s.nickname || ''} ${s.grade || ''} ${s.school || ''}`.toLowerCase().includes(kw);
      });
    });

    function openCreate() {
      editing.value = { mode: 'create', student: null };
    }

    function openEdit(s) {
      editing.value = { mode: 'edit', student: s };
    }

    async function onSaved() {
      editing.value = null;
      await load();
    }

    async function remove(s) {
      if (!confirm(`确认删除「${s.name}」？该学生的课时与反馈记录会一并删除，且不可恢复。`)) return;
      try {
        await studentsApi.remove(s.id);
        ok('已删除');
        await load();
      } catch (e) {
        fail(e.message);
      }
    }

    onMounted(async () => {
      await loadCurricula();
      await load();
    });

    return {
      rows, visible, loading, filter, keyword, editing, state,
      openCreate, openEdit, onSaved, remove, money,
    };
  },
  template: `
  <div>
    <div class="page-head">
      <h1>学生</h1>
      <span class="sub">共 {{ rows.length }} 人</span>
      <div class="spacer"></div>
      <button class="btn primary" @click="openCreate">＋ 添加学生</button>
    </div>

    <div class="card">
      <div class="row" style="align-items:center; margin-bottom:12px">
        <div class="chips" style="flex:none">
          <span class="chip" :class="{on: filter==='active'}" @click="filter='active'">在读</span>
          <span class="chip" :class="{on: filter==='paused'}" @click="filter='paused'">暂停</span>
          <span class="chip" :class="{on: filter==='ended'}" @click="filter='ended'">已结课</span>
          <span class="chip" :class="{on: filter==='all'}" @click="filter='all'">全部</span>
        </div>
        <div style="max-width:240px">
          <input type="text" v-model="keyword" placeholder="搜索姓名 / 年级 / 学校">
        </div>
      </div>

      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="!visible.length" class="empty">
        还没有学生
        <div class="small" style="margin-top:6px">先加一个学生，才能给他排课和写反馈</div>
      </div>
      <table v-else class="tbl">
        <thead>
          <tr>
            <th>姓名</th><th>年级 / 体系</th><th>单价</th>
            <th>已上</th><th>最近上课</th><th>待写反馈</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in visible" :key="s.id">
            <td>
              <router-link :to="'/students/' + s.id" style="font-weight:500">{{ s.name }}</router-link>
              <div class="small muted" v-if="s.nickname">{{ s.nickname }}</div>
            </td>
            <td>
              <div>{{ s.grade || '—' }}</div>
              <div class="small muted">{{ (s.curriculum_names || []).join(' / ') || '未设体系' }}</div>
            </td>
            <td>
              <span v-if="s.hourly_rate">{{ money(s.hourly_rate) }}<span class="small muted">/{{ s.rate_unit === 'session' ? '次' : '小时' }}</span></span>
              <span v-else class="muted">未设</span>
            </td>
            <td>
              {{ s.stats.lesson_done }} 节
              <div class="small muted">{{ (s.stats.minutes_total / 60).toFixed(1) }} 小时</div>
            </td>
            <td class="small">{{ s.stats.last_lesson_at ? s.stats.last_lesson_at.slice(0, 10) : '—' }}</td>
            <td>
              <span v-if="s.stats.feedback_pending" class="tag orange">{{ s.stats.feedback_pending }} 节</span>
              <span v-else class="tag green">已清</span>
            </td>
            <td style="text-align:right; white-space:nowrap">
              <button class="btn ghost sm" @click="openEdit(s)">编辑</button>
              <button class="btn ghost sm" style="color:var(--danger)" @click="remove(s)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <StudentFormDialog v-if="editing" :student="editing.student"
                       @close="editing = null" @saved="onSaved" />
  </div>`,
};
