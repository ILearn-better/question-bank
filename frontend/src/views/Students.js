// 学生列表。核心是「一眼看到谁的反馈欠着」——待写反馈数直接列出来。
import { computed, onMounted, reactive, ref } from 'vue';
import { studentsApi } from '../api.js';
import Modal from '../components/Modal.js';
import { fail, loadCurricula, loadStudents, money, ok, state } from '../store.js';

const EMPTY = {
  name: '', nickname: '', grade: '', school: '', contact: '', parent_contact: '',
  hourly_rate: null, rate_unit: 'hour', status: 'active', started_at: '', ended_at: '',
  remark: '', curriculum_ids: [], primary_curriculum_id: null,
};

export default {
  name: 'Students',
  components: { Modal },
  setup() {
    const rows = ref([]);
    const loading = ref(true);
    const filter = ref('active');
    const keyword = ref('');
    const editing = ref(null);
    const form = reactive({ ...EMPTY });
    const saving = ref(false);

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
      Object.assign(form, EMPTY, { curriculum_ids: [] });
      editing.value = { mode: 'create' };
    }

    function openEdit(s) {
      Object.assign(form, EMPTY, {
        name: s.name, nickname: s.nickname || '', grade: s.grade || '', school: s.school || '',
        contact: s.contact || '', parent_contact: s.parent_contact || '',
        hourly_rate: s.hourly_rate, rate_unit: s.rate_unit || 'hour', status: s.status,
        started_at: s.started_at || '', ended_at: s.ended_at || '', remark: s.remark || '',
        curriculum_ids: [...(s.curriculum_ids || [])],
        primary_curriculum_id: null,
      });
      editing.value = { mode: 'edit', id: s.id };
    }

    function toggleCurriculum(id) {
      const i = form.curriculum_ids.indexOf(id);
      if (i >= 0) form.curriculum_ids.splice(i, 1);
      else form.curriculum_ids.push(id);
      if (!form.curriculum_ids.includes(form.primary_curriculum_id)) {
        form.primary_curriculum_id = form.curriculum_ids[0] ?? null;
      }
    }

    async function save() {
      if (!form.name.trim()) {
        fail('学生姓名不能为空');
        return;
      }
      saving.value = true;
      try {
        const payload = { ...form, hourly_rate: form.hourly_rate === '' ? null : form.hourly_rate };
        if (editing.value.mode === 'create') {
          await studentsApi.create(payload);
          ok('学生已添加');
        } else {
          await studentsApi.update(editing.value.id, payload);
          ok('已保存');
        }
        editing.value = null;
        await load();
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
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
      rows, visible, loading, filter, keyword, editing, form, saving, state,
      openCreate, openEdit, toggleCurriculum, save, remove, money,
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

    <Modal v-if="editing" :title="editing.mode === 'create' ? '添加学生' : '编辑学生'" @close="editing = null">
      <div class="row">
        <div class="field">
          <label>姓名 *</label>
          <input type="text" v-model="form.name" placeholder="真实姓名或代号">
        </div>
        <div class="field">
          <label>昵称 / 备注名</label>
          <input type="text" v-model="form.nickname" placeholder="选填">
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>年级</label>
          <input type="text" v-model="form.grade" placeholder="中四 / Year 11 / 高一">
        </div>
        <div class="field">
          <label>学校</label>
          <input type="text" v-model="form.school">
        </div>
      </div>

      <div class="field">
        <label>所学体系（可多选 —— 一个学生可以同时学 DSE 和国内课程）</label>
        <div class="chips">
          <span v-for="c in state.curricula" :key="c.id" class="chip"
                :class="{on: form.curriculum_ids.includes(c.id)}"
                @click="toggleCurriculum(c.id)">{{ c.name }}</span>
        </div>
      </div>

      <div class="row">
        <div class="field">
          <label>默认单价</label>
          <input type="number" v-model.number="form.hourly_rate" placeholder="如 500">
        </div>
        <div class="field">
          <label>计价方式</label>
          <select v-model="form.rate_unit">
            <option value="hour">按小时</option>
            <option value="session">按次（与时长无关）</option>
          </select>
        </div>
        <div class="field">
          <label>状态</label>
          <select v-model="form.status">
            <option value="active">在读</option>
            <option value="paused">暂停</option>
            <option value="ended">已结课</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>开始日期</label>
          <input type="date" v-model="form.started_at">
        </div>
        <div class="field">
          <label>结束日期</label>
          <input type="date" v-model="form.ended_at">
        </div>
        <div class="field">
          <label>联系方式</label>
          <input type="text" v-model="form.contact">
        </div>
      </div>
      <div class="field">
        <label>家长联系方式（导出报告时默认脱敏，不会出现在发给家长的页面上）</label>
        <input type="text" v-model="form.parent_contact">
      </div>
      <div class="field">
        <label>备注</label>
        <textarea rows="2" v-model="form.remark" placeholder="基础情况、性格、家长诉求…"></textarea>
      </div>

      <template #foot>
        <button class="btn" @click="editing = null">取消</button>
        <button class="btn primary" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>
  </div>`,
};
