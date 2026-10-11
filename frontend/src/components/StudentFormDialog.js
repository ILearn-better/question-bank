// 学生档案表单弹窗 —— 「添加学生」与「编辑学生」共用一份。
//
// 为什么单独抽成组件（而不是留在 Students.js 里）：
// 课表导入时遇到「表里有、系统里没有」的学生，老师点「去创建」就得**当场把这份
// 档案填完**。少了这一步，导进去的课没有学生、或者建出来的是个没单价的空壳学生，
// 月底算课时费时静默少钱（这个坑踩过）。如果导入流程里再抄一份表单，
// 学生字段一改就会两处不一致 —— 所以表单必须只有一份。
//
// 两个入口：
//   · Students.js        —— 学生页的「＋ 添加学生」/ 列表里的「编辑」
//   · ScheduleImportDialog —— 导入课表时对不上的学生，逐个补档案
import { computed, onMounted, reactive, ref } from 'vue';
import { studentsApi } from '../api.js';
import { fail, loadCurricula, loadStudents, ok, state } from '../store.js';
import Modal from './Modal.js';

const EMPTY = {
  name: '', nickname: '', grade: '', school: '', contact: '', parent_contact: '',
  hourly_rate: null, rate_unit: 'hour', status: 'active', started_at: '', ended_at: '',
  remark: '', curriculum_ids: [], primary_curriculum_id: null,
};

export default {
  name: 'StudentFormDialog',
  components: { Modal },
  props: {
    /** 传了就是编辑模式（用它的值预填），不传是新建 */
    student: { type: Object, default: null },
    /** 新建时预填的姓名 —— 课表导入带上来的「表里写了但系统里没有」的名字 */
    presetName: { type: String, default: '' },
    /** 标题右侧的小字提示（如「第 2 / 3 个」） */
    hint: { type: String, default: '' },
  },
  emits: ['close', 'saved'],
  setup(props, { emit }) {
    const form = reactive({ ...EMPTY });
    const saving = ref(false);
    const isEdit = computed(() => !!props.student);

    (function fill() {
      const s = props.student;
      if (s) {
        Object.assign(form, EMPTY, {
          name: s.name || '', nickname: s.nickname || '', grade: s.grade || '',
          school: s.school || '', contact: s.contact || '',
          parent_contact: s.parent_contact || '', hourly_rate: s.hourly_rate,
          rate_unit: s.rate_unit || 'hour', status: s.status || 'active',
          started_at: s.started_at || '', ended_at: s.ended_at || '', remark: s.remark || '',
          curriculum_ids: [...(s.curriculum_ids || [])],
          primary_curriculum_id: null,
        });
      } else {
        Object.assign(form, EMPTY, { name: props.presetName || '', curriculum_ids: [] });
      }
    })();

    // 体系列表是全局的，别的地方可能已经加载过 —— loadCurricula 内部有缓存
    onMounted(() => { loadCurricula(); loadStudents(); });

    // 重名软提示：导入课表是**按姓名**对学生的，重名会让课排到不确定的那一位身上。
    // 只提示不阻断 —— 同名同姓的两个人也确实存在，拦死了老师反而没法录。
    const dupName = computed(() => {
      const n = form.name.trim();
      if (!n) return null;
      const others = (state.students || []).filter(
        (s) => s.name === n && (!props.student || s.id !== props.student.id));
      return others.length ? others[0] : null;
    });

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
        const payload = {
          ...form,
          hourly_rate: form.hourly_rate === '' ? null : form.hourly_rate,
        };
        const saved = isEdit.value
          ? await studentsApi.update(props.student.id, payload)
          : await studentsApi.create(payload);
        ok(isEdit.value ? '已保存' : `学生「${saved.name}」已添加`);
        // 学生列表是全局共享的（课表/出卷/反馈的下拉都要用），保存后强制刷新一遍
        await loadStudents(true);
        emit('saved', saved);
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
    }

    const title = computed(() => (isEdit.value ? '编辑学生' : '添加学生'));
    return { form, saving, isEdit, title, state, dupName, toggleCurriculum, save };
  },
  template: `
  <Modal :title="title" @close="$emit('close')">
    <div v-if="hint" class="small" style="color:var(--warning); margin-bottom:8px">{{ hint }}</div>

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
    <div v-if="dupName" class="small" style="color:var(--warning); margin:-4px 0 8px">
      已经有一个叫「{{ dupName.name }}」的学生了。排课是按姓名对学生的，重名会分不清是哪一位 ——
      建议改成能区分的名字（或至少把昵称填上）。
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
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="saving" @click="save">
        {{ saving ? '保存中…' : '保存' }}
      </button>
    </template>
  </Modal>`,
};
