// 课表与课时费。按「记录入口」来设计：排课、标记完成、看本月账，都在一页完成。
//
// 两种视图（2026-10-08 加）：
//   · 月历 —— 默认。一个月铺成一屏，一眼看出「哪天有课、哪天空着」，
//             排课/调课这类「按天想事」的操作都在这里做。
//   · 列表 —— 原来的按天分组，顺着一行行往下看。适合核对某一周的细节。
// 两份视图读同一份 lessons 数据，切换不重新请求。
//
// 月历上的交互（2026-10-11 加拖动调课）：
//   单击一条课 → 编辑弹窗（改日期/时间/状态、标记完成、**删除**）
//   双击一条课 → 进入「调课模式」，再按住它拖到目标日期，放开即改日期（Esc 取消）
// 为什么调课要「双击解锁」再做拖动：块一旦常驻 draggable，单击时手抖几像素就会被
// 浏览器当成拖动，弹窗永远打不开。双击解锁后单击绝对可靠。
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { lessonsApi } from '../api.js';
import { lessonsByDate, monthGrid, shiftPeriod, WEEK_HEADS } from '../calendar.js';
import Modal from '../components/Modal.js';
import ScheduleImportDialog from '../components/ScheduleImportDialog.js';
import {
  currentPeriod, fail, hhmm, loadCurricula, loadStudents, money, ok,
  shortDate, state, STATUS_LABEL, STATUS_TAG, todayISO,
} from '../store.js';

// 单击要等这么久才弹窗：双击的第一次 click 会先到，不能让它顺手把弹窗打开
const CLICK_DELAY = 200;

function emptyLesson() {
  return {
    student_id: null,
    curriculum_id: null,
    date: todayISO(),
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
  components: { Modal, ScheduleImportDialog },
  setup() {
    const period = ref(currentPeriod());
    const lessons = ref([]);
    const monthly = ref(null);
    const loading = ref(true);
    const editing = ref(null);
    const form = reactive(emptyLesson());
    const saving = ref(false);
    const busy = ref(0);
    const view = ref('calendar');          // calendar | list
    const importing = ref(false);

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

    // ---------------- 月历 ----------------
    const grid = computed(() => monthGrid(period.value));
    const byDay = computed(() => lessonsByDate(lessons.value));

    function shift(delta) {
      period.value = shiftPeriod(period.value, delta);
    }
    function goToday() {
      period.value = currentPeriod();
    }

    // ---------------- 列表 ----------------
    const grouped = computed(() => {
      const map = new Map();
      for (const ls of lessons.value) {
        const day = ls.start_at.slice(0, 10);
        if (!map.has(day)) map.set(day, []);
        map.get(day).push(ls);
      }
      return [...map.entries()].map(([day, items]) => ({
        day,
        weekday: WEEK_HEADS[new Date(day + 'T00:00').getDay()],
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
      editing.value = { mode: 'edit', id: ls.id, lesson: ls };
    }

    // ---------------- 拖动调课（月历） ----------------
    const dragId = ref(0);                 // 正在被拖的那节课
    const dragMode = ref(0);               // 双击解锁的调课模式（= 课 id）
    const dragOver = ref('');              // 悬停到哪一天（高亮目标格）
    let clickTimer = null;

    const dragLesson = computed(() => lessons.value.find((x) => x.id === dragMode.value) || null);
    const dragText = computed(() => (dragLesson.value
      ? `${dragLesson.value.student_name} ${hhmm(dragLesson.value.start_at)}` : ''));

    function onItemClick(ls) {
      if (dragMode.value) return;          // 调课模式里单击不做别的
      clearTimeout(clickTimer);
      clickTimer = setTimeout(() => { clickTimer = null; openEdit(ls); }, CLICK_DELAY);
    }

    function onItemDbl(ls) {
      clearTimeout(clickTimer);            // 别让双击顺带弹出编辑窗
      clickTimer = null;
      dragMode.value = ls.id;
      ok(`调课模式：按住「${ls.student_name} ${hhmm(ls.start_at)}」拖到目标日期，放开即可（Esc 取消）`);
    }

    function cancelDrag() {
      dragMode.value = 0;
      dragId.value = 0;
      dragOver.value = '';
    }

    function onDragStart(ls, e) {
      if (dragMode.value !== ls.id) return;   // 没解锁就按住拖 → 不拖（避免误触）
      dragId.value = ls.id;
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', String(ls.id));   // Firefox 不设就不拖
      }
    }

    function onDragEnd() {
      dragId.value = 0;
      dragOver.value = '';
    }

    function onDragOver(day) {
      if (dragId.value && day) dragOver.value = day;
    }

    function onDragLeave(day) {
      if (dragOver.value === day) dragOver.value = '';
    }

    /** 放开：只改日期，开始时间不变。真正写库就这一处（PATCH start_at）。 */
    async function onDrop(day) {
      const ls = lessons.value.find((x) => x.id === dragId.value);
      cancelDrag();
      if (!ls || !day || ls.start_at.slice(0, 10) === day) return;   // 原地放下，什么都不做
      try {
        await lessonsApi.update(ls.id, { start_at: `${day}T${ls.start_at.slice(11, 16)}` });
        ok(`已把「${ls.student_name}」调到 ${day}`);
        await load();
      } catch (e) {
        fail(e.message);
      }
    }

    function onKey(e) {
      if (e.key === 'Escape' && dragMode.value) {
        cancelDrag();
        ok('已取消调课');
      }
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
        editing.value = null;               // 从弹窗里点的 → 顺手关掉
        await load();
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = 0;
      }
    }

    async function remove(ls) {
      if (!ls) return;
      if (!confirm(`删除 ${ls.student_name} ${String(ls.start_at).replace('T', ' ')} 的这节课？`)) return;
      try {
        await lessonsApi.remove(ls.id);
        ok('已删除');
        editing.value = null;
        cancelDrag();
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

    // Excel 导入完成后：可能有跨月的新课，直接重载当前月即可
    async function onImported() {
      await load();
    }

    watch(period, load);

    onMounted(async () => {
      await loadCurricula();
      await loadStudents();
      await load();
    });
    onMounted(() => window.addEventListener('keydown', onKey));
    onBeforeUnmount(() => window.removeEventListener('keydown', onKey));

    return {
      period, lessons, monthly, loading, grouped, editing, form, saving, busy, state,
      view, importing, grid, byDay, WEEK_HEADS,
      openCreate, openEdit, save, complete, remove, onStudentChange, shift, goToday, onImported,
      hhmm, money, shortDate, todayISO, STATUS_LABEL, STATUS_TAG,
      dragId, dragMode, dragOver, dragText, onItemClick, onItemDbl, cancelDrag,
      onDragStart, onDragEnd, onDragOver, onDragLeave, onDrop,
    };
  },
  template: `
  <div>
    <div class="page-head">
      <h1>课表</h1>

      <div class="seg">
        <button :class="{ on: view === 'calendar' }" @click="view = 'calendar'">月历</button>
        <button :class="{ on: view === 'list' }" @click="view = 'list'">列表</button>
      </div>

      <div class="cal-nav">
        <button class="btn ghost sm" title="上一个月" @click="shift(-1)">‹</button>
        <div style="width:136px"><input type="month" v-model="period"></div>
        <button class="btn ghost sm" title="下一个月" @click="shift(1)">›</button>
        <button class="btn ghost sm" @click="goToday()">本月</button>
      </div>

      <div class="spacer"></div>
      <button class="btn" @click="importing = true">导入 Excel</button>
      <button class="btn primary" @click="openCreate()">＋ 排课</button>
    </div>

    <div class="grid cols-5">
      <div class="card stat"><div class="v">{{ monthly ? monthly.total_lessons : '—' }}</div><div class="k">本月已上节数</div></div>
      <div class="card stat"><div class="v">{{ monthly ? (monthly.total_minutes / 60).toFixed(1) : '—' }}</div><div class="k">本月课时（小时）</div></div>
      <div class="card stat"><div class="v">{{ money(monthly ? monthly.total_amount : null) }}</div><div class="k">本月应收（已上）</div></div>
      <div class="card stat">
        <div class="v" :class="{ pending: monthly && monthly.pending_total_amount }">
          {{ money(monthly ? monthly.pending_total_amount : null) }}
        </div>
        <div class="k">本月待上（未上课）{{ monthly ? ' · ' + monthly.pending_total_lessons + ' 节' : '' }}</div>
      </div>
      <div class="card stat"><div class="v">{{ monthly ? monthly.students.length : '—' }}</div><div class="k">涉及学生</div></div>
    </div>

    <div class="grid cols-3" style="margin-top:14px; align-items:start">
      <div :style="view === 'calendar' ? 'grid-column: 1 / -1' : 'grid-column: span 2'">

        <!-- ============ 月历 ============ -->
        <div v-if="view === 'calendar'" class="card">
          <div v-if="dragMode" class="cal-drag-tip">
            调课中：按住「{{ dragText }}」拖到目标日期，放开即完成。
            <button class="btn ghost sm" style="margin-left:6px" @click="cancelDrag()">取消（Esc）</button>
          </div>
          <div v-if="loading" class="empty">加载中…</div>
          <template v-else>
            <div class="cal-head">
              <span v-for="w in WEEK_HEADS" :key="w">{{ w }}</span>
            </div>
            <div class="cal-grid">
              <div v-for="(c, i) in grid" :key="i" class="cal-cell"
                   :class="{ out: c.out, today: c.date === todayISO(),
                             'drag-ready': !!dragMode, 'drag-over': c.date && dragOver === c.date }"
                   @dragover.prevent="onDragOver(c.date)"
                   @dragleave="onDragLeave(c.date)"
                   @drop.prevent="onDrop(c.date)">
                <template v-if="c.date">
                  <div class="cal-day">
                    <span>{{ c.day }}</span>
                    <span v-if="byDay.get(c.date)" class="cal-count">{{ byDay.get(c.date).length }} 节</span>
                  </div>
                  <div class="cal-items">
                    <div v-for="ls in (byDay.get(c.date) || [])" :key="ls.id"
                         class="cal-item" :class="[STATUS_TAG[ls.status] || 'plain',
                                                   { dragging: dragId === ls.id, armed: dragMode === ls.id }]"
                         :title="ls.student_name + ' ' + hhmm(ls.start_at) + ' ' + ls.duration_min + '分钟' + (ls.topic ? ' · ' + ls.topic : '') + '（单击编辑，双击后可拖动调课）'"
                         :draggable="dragMode === ls.id"
                         @click="onItemClick(ls)"
                         @dblclick="onItemDbl(ls)"
                         @dragstart="onDragStart(ls, $event)"
                         @dragend="onDragEnd">
                      <span class="t">{{ hhmm(ls.start_at) }}</span>
                      <span class="n">{{ ls.student_name }}</span>
                    </div>
                  </div>
                  <button class="cal-add" title="这天加一节课" @click="openCreate(c.date)">＋</button>
                </template>
              </div>
            </div>
            <div class="small muted" style="margin-top:10px">
              <b>单击</b>某节课 → 打开弹窗改（编辑、标记完成、删除都在里面）；
              <b>双击</b>某节课 → 进入调课模式，把它拖到目标日期放开，就能改到那天（时间不变，Esc 取消）。
              鼠标移到某天格子上，右下角会出现 ＋ 可以加课。
            </div>
          </template>
        </div>

        <!-- ============ 列表 ============ -->
        <div v-else class="card">
          <h2>课时记录</h2>
          <div v-if="loading" class="empty">加载中…</div>
          <div v-else-if="!grouped.length" class="empty">
            这个月还没有课时记录
            <div class="small" style="margin-top:6px">点右上角「排课」，或用「导入 Excel」批量排</div>
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
          <div v-if="!monthly || !monthly.students.length" class="empty">本月还没有课时记录</div>
          <table v-else class="tbl">
            <thead>
              <tr>
                <th>学生</th>
                <th style="text-align:right">已上（应收）</th>
                <th style="text-align:right">待上（未上课）</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in monthly.students" :key="r.student_id">
                <td>
                  <router-link :to="'/students/' + r.student_id">{{ r.student_name }}</router-link>
                  <div class="small muted">
                    已上 {{ r.lesson_count }} 节 · {{ (r.billable_minutes / 60).toFixed(1) }} 小时
                    <template v-if="r.pending_lessons">
                      　待上 {{ r.pending_lessons }} 节 · {{ (r.pending_minutes / 60).toFixed(1) }} 小时
                    </template>
                  </div>
                </td>
                <td style="text-align:right; white-space:nowrap">{{ money(r.amount) }}</td>
                <td style="text-align:right; white-space:nowrap"
                    :class="r.pending_amount ? 'pending' : 'muted'">
                  {{ r.pending_lessons ? money(r.pending_amount) : '—' }}
                </td>
              </tr>
              <tr>
                <td style="text-align:right; color:var(--text-2)">合计</td>
                <td style="text-align:right; font-weight:600">{{ money(monthly.total_amount) }}</td>
                <td style="text-align:right; font-weight:600" class="pending">{{ money(monthly.pending_total_amount) }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="monthly && monthly.pending_no_rate" class="small" style="margin-top:8px; color:var(--warning)">
            ⚠️ 有 {{ monthly.pending_no_rate }} 节待上的课没填单价，金额没算进去 —— 补上单价后才会显示。
          </div>
          <div class="small muted" style="margin-top:10px">
            <b>已上</b>＝已完成/补课，已经到手的钱；<b>待上</b>＝本月已排好、还没上的课，
            按排课时的单价预估，上完课会自动转到「已上」。请假、取消不计费但会留痕。
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
        <button v-if="editing.mode === 'edit'" class="btn ghost" style="color:var(--danger)"
                @click="remove(editing.lesson)">删除这节课</button>
        <button v-if="editing.mode === 'edit' && editing.lesson && editing.lesson.status === 'scheduled'"
                class="btn" :disabled="busy === editing.lesson.id"
                @click="complete(editing.lesson)">标记完成</button>
        <div class="spacer"></div>
        <button class="btn" @click="editing = null">取消</button>
        <button class="btn primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>

    <ScheduleImportDialog v-if="importing" @close="importing = false" @done="onImported" />
  </div>`,
};
