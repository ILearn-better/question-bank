// 课表与课时费。按「记录入口」来设计：排课、标记完成、看本月账，都在一页完成。
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { lessonsApi } from '../api.js';
import Modal from '../components/Modal.js';
import {
  currentPeriod, fail, hhmm, loadCurricula, loadStudents, money, ok,
  shortDate, state, STATUS_LABEL, STATUS_TAG,
} from '../store.js';

const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

function emptyLesson() {
  const today = new Date().toISOString().slice(0, 10);
  return {
    student_id: null,
    curriculum_id: null,
    date: today,
    time: '19:00',
    duration_min: 60,
    status: 'scheduled',
    mode: 'offline',
    location: '',
    rate: null,
    topic: '',
  };
}

export default {
  name: 'Schedule',
  components: { Modal },
  setup() {
    const period = ref(currentPeriod());
    const lessons = ref([]);
    const monthly = ref(null);
    const loading = ref(true);
    const editing = ref(null);
    const form = reactive(emptyLesson());
    const saving = ref(false);
    const busy = ref(0);

    async function load() {
      loading.value = true;
      try {
        const start = `${period.value}-01`;
        const end = `${period.value}-31`;
        lessons.value = await lessonsApi.list({ start, end });
        monthly.value = await lessonsApi.monthly(period.value);
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    // 按日期分组，同一天的课排在一起
    const grouped = computed(() => {
      const map = new Map();
      for (const ls of lessons.value) {
        const day = ls.start_at.slice(0, 10);
        if (!map.has(day)) map.set(day, []);
        map.get(day).push(ls);
      }
      return [...map.entries()].map(([day, items]) => ({
        day,
        weekday: WEEK[new Date(day + 'T00:00').getDay()],
        items,
        minutes: items.filter((x) => x.status === 'done' || x.status === 'makeup')
          .reduce((s, x) => s + (x.duration_min || 0), 0),
      }));
    });

    function openCreate(day) {
      Object.assign(form, emptyLesson());
      if (day) form.date = day;
      editing.value = { mode: 'create' };
    }

    function openEdit(ls) {
      Object.assign(form, emptyLesson(), {
        student_id: ls.student_id,
        curriculum_id: ls.curriculum_id,
        date: ls.start_at.slice(0, 10),
        time: ls.start_at.slice(11, 16),
        duration_min: ls.duration_min,
        status: ls.status,
        mode: ls.mode || 'offline',
        location: ls.location || '',
        rate: ls.rate,
        topic: ls.topic || '',
      });
      editing.value = { mode: 'edit', id: ls.id };
    }

    async function save() {
      if (!form.student_id) {
        fail('请选择学生');
        return;
      }
      saving.value = true;
      try {
        const payload = {
          student_id: form.student_id,
          curriculum_id: form.curriculum_id || null,
          start_at: `${form.date}T${form.time}`,
          duration_min: Number(form.duration_min) || 60,
          status: form.status,
          mode: form.mode || null,
          location: form.location || null,
          rate: form.rate === '' || form.rate === null ? null : Number(form.rate),
          topic: form.topic || null,
        };
        if (editing.value.mode === 'create') {
          await lessonsApi.create(payload);
          ok('已排课');
        } else {
          await lessonsApi.update(editing.value.id, payload);
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

    async function complete(ls) {
      busy.value = ls.id;
      try {
        await lessonsApi.complete(ls.id);
        ok('已标记完成');
        await load();
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = 0;
      }
    }

    async function remove(ls) {
      if (!confirm(`删除 ${ls.student_name} ${ls.start_at.replace('T', ' ')} 的这节课？`)) return;
      try {
        await lessonsApi.remove(ls.id);
        ok('已删除');
        await load();
      } catch (e) {
        fail(e.message);
      }
    }

    // 选了学生后，若没手填单价，就用学生默认单价（后端也会兜底）
    function onStudentChange() {
      const s = state.students.find((x) => x.id === form.student_id);
      form.rate = s ? s.hourly_rate : null;
      if (s && s.curriculum_ids && s.curriculum_ids.length && !form.curriculum_id) {
        form.curriculum_id = s.curriculum_ids[0];
      }
    }

    watch(period, load);

    onMounted(async () => {
      await loadCurricula();
      await loadStudents();
      await load();
    });

    return {
      period, lessons, monthly, loading, grouped, editing, form, saving, busy, state,
      openCreate, openEdit, save, complete, remove, onStudentChange,
      hhmm, money, shortDate, STATUS_LABEL, STATUS_TAG,
    };
  },
  template: `
  <div>
    <div class="page-head">
      <h1>课表</h1>
      <div style="max-width:170px"><input type="month" v-model="period"></div>
      <div class="spacer"></div>
      <button class="btn primary" @click="openCreate()">＋ 排课</button>
    </div>

    <div class="grid cols-4">
      <div class="card stat"><div class="v">{{ monthly ? monthly.total_lessons : '—' }}</div><div class="k">本月已上节数</div></div>
      <div class="card stat"><div class="v">{{ monthly ? (monthly.total_minutes / 60).toFixed(1) : '—' }}</div><div class="k">本月课时（小时）</div></div>
      <div class="card stat"><div class="v">{{ money(monthly ? monthly.total_amount : null) }}</div><div class="k">本月应收</div></div>
      <div class="card stat"><div class="v">{{ monthly ? monthly.students.length : '—' }}</div><div class="k">涉及学生</div></div>
    </div>

    <div class="grid cols-3" style="margin-top:14px; align-items:start">
      <div style="grid-column: span 2">
        <div class="card">
          <h2>课时记录</h2>
          <div v-if="loading" class="empty">加载中…</div>
          <div v-else-if="!grouped.length" class="empty">
            这个月还没有课时记录
            <div class="small" style="margin-top:6px">点右上角「排课」开始</div>
          </div>
          <template v-else>
            <div v-for="g in grouped" :key="g.day" style="margin-bottom:14px">
              <div class="small muted" style="margin-bottom:6px">
                {{ g.day }} {{ g.weekday }}
                <span v-if="g.minutes"> · 已上 {{ (g.minutes / 60).toFixed(1) }} 小时</span>
                <button class="btn ghost sm" style="margin-left:6px" @click="openCreate(g.day)">+ 加课</button>
              </div>
              <div v-for="ls in g.items" :key="ls.id" class="lesson-row">
                <span class="time">{{ hhmm(ls.start_at) }}</span>
                <div style="flex:1; min-width:0">
                  <div class="who">
                    <router-link :to="'/students/' + ls.student_id">{{ ls.student_name }}</router-link>
                    <span class="tag" :class="STATUS_TAG[ls.status]" style="margin-left:6px">
                      {{ STATUS_LABEL[ls.status] || ls.status }}
                    </span>
                  </div>
                  <div class="meta">
                    {{ ls.duration_min }} 分钟
                    <span v-if="ls.rate"> · 单价 {{ money(ls.rate) }}</span>
                    <span v-if="ls.amount !== null"> · 计 {{ money(ls.amount) }}</span>
                    <span v-if="ls.topic"> · {{ ls.topic }}</span>
                  </div>
                </div>
                <button v-if="ls.status === 'scheduled'" class="btn sm" :disabled="busy === ls.id"
                        @click="complete(ls)">完成</button>
                <button class="btn ghost sm" @click="openEdit(ls)">编辑</button>
                <button class="btn ghost sm" style="color:var(--danger)" @click="remove(ls)">删除</button>
              </div>
            </div>
          </template>
        </div>
      </div>

      <div>
        <div class="card">
          <h2>本月课时费</h2>
          <div v-if="!monthly || !monthly.students.length" class="empty">暂无已完成的课时</div>
          <table v-else class="tbl">
            <tbody>
              <tr v-for="r in monthly.students" :key="r.student_id">
                <td>
                  <router-link :to="'/students/' + r.student_id">{{ r.student_name }}</router-link>
                  <div class="small muted">{{ r.lesson_count }} 节 · {{ (r.billable_minutes / 60).toFixed(1) }} 小时</div>
                </td>
                <td style="text-align:right; white-space:nowrap">{{ money(r.amount) }}</td>
              </tr>
              <tr>
                <td style="text-align:right; color:var(--text-2)">合计</td>
                <td style="text-align:right; font-weight:600">{{ money(monthly.total_amount) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="small muted" style="margin-top:10px">
            只统计「已完成」和「补课」；请假、取消不计费但会留痕。
          </div>
        </div>
      </div>
    </div>

    <Modal v-if="editing" :title="editing.mode === 'create' ? '排课' : '编辑课时'" @close="editing = null">
      <div class="row">
        <div class="field">
          <label>学生 *</label>
          <select v-model.number="form.student_id" @change="onStudentChange">
            <option :value="null">请选择</option>
            <option v-for="s in state.students" :key="s.id" :value="s.id">
              {{ s.name }}<template v-if="s.grade"> · {{ s.grade }}</template>
            </option>
          </select>
        </div>
        <div class="field">
          <label>体系</label>
          <select v-model.number="form.curriculum_id">
            <option :value="null">不指定</option>
            <option v-for="c in state.curricula" :key="c.id" :value="c.id">{{ c.name }}</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>日期</label>
          <input type="date" v-model="form.date">
        </div>
        <div class="field">
          <label>开始时间</label>
          <input type="text" v-model="form.time" placeholder="19:00">
        </div>
        <div class="field">
          <label>时长（分钟）</label>
          <input type="number" v-model.number="form.duration_min" step="15">
        </div>
      </div>
      <div class="row">
        <div class="field">
          <label>状态</label>
          <select v-model="form.status">
            <option value="scheduled">已排课</option>
            <option value="done">已完成</option>
            <option value="makeup">补课</option>
            <option value="leave">请假</option>
            <option value="cancelled">已取消</option>
            <option value="moved">已调课</option>
          </select>
        </div>
        <div class="field">
          <label>上课形式</label>
          <select v-model="form.mode">
            <option value="offline">线下</option>
            <option value="online">线上</option>
          </select>
        </div>
        <div class="field">
          <label>本节课单价（留空用学生默认）</label>
          <input type="number" v-model.number="form.rate" placeholder="按学生默认">
        </div>
      </div>
      <div class="field">
        <label>本次内容</label>
        <input type="text" v-model="form.topic" placeholder="如：三角函数图像变换">
      </div>
      <div class="field">
        <label>地点</label>
        <input type="text" v-model="form.location">
      </div>
      <div class="small muted">
        单价会按这节课「快照」保存 —— 以后调学生的单价，不会改动这节课的金额。
      </div>

      <template #foot>
        <button class="btn" @click="editing = null">取消</button>
        <button class="btn primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>
  </div>`,
};
