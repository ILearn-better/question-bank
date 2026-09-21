// 课后反馈录入 —— 决定这个系统生死的一个界面。
//
// 设计原则（开发文档 §6.5）：目标是「上完一节课 3 分钟内记完」。
// 具体手段：
//   · 从课表点进来就是这门课，不用先选学生再选时间
//   · 四段式各自带快捷短语，一点即插，省掉打字
//   · 能力评分「允许只打部分」，没打的下次仍按历史值算，绝不强制填满
//   · 所有字段都可留空 —— 只写一句话也能存。卡住一次，这个工具就会被弃用。
import { onMounted, reactive, ref } from 'vue';
import { abilityApi, feedbackApi, studentsApi } from '../api.js';
import { fail, hhmm, ok, shortDate, warn } from '../store.js';
import Modal from './Modal.js';

// 快捷短语：一点即插。写得越具体，家长越觉得「老师真的在看我的孩子」。
const PHRASES = {
  performance: ['状态不错，配合度高', '前半段注意力较集中', '主动提问，思路跟得紧', '略疲倦，节奏放慢后好转'],
  problems: ['计算跳步导致失分', '审题不够仔细，条件看漏', '步骤书写不规范', '知识点迁移能力偏弱'],
  homework: ['课后练习 P32 第 1-8 题', '错题重做一遍', '本周完成一套限时训练', '暂无，先巩固课上内容'],
  next_plan: ['下节课讲函数单调性', '先复习错题再进入新内容', '下次带模考卷来讲解', '继续完成本章剩余题型'],
};

export default {
  name: 'FeedbackDialog',
  components: { Modal },
  props: {
    lesson: { type: Object, required: true },
  },
  emits: ['close', 'saved'],
  setup(props, { emit }) {
    const dims = ref([]);
    const saving = ref(false);
    const form = reactive({
      performance: '',
      problems: '',
      homework: '',
      next_plan: '',
      rating: null,
      share_to_parent: 0,
      scores: {},          // { dim_id: 1..5 }
    });
    const existing = ref(false);
    const lastScores = ref({});   // dim_id -> 该维度上一次的分数（打分的参照锚点）

    onMounted(async () => {
      try {
        dims.value = await abilityApi.dims();
        const fb = await feedbackApi.get(props.lesson.id);
        if (fb) {
          existing.value = true;
          form.performance = fb.performance || '';
          form.problems = fb.problems || '';
          form.homework = fb.homework || '';
          form.next_plan = fb.next_plan || '';
          form.rating = fb.rating;
          form.share_to_parent = fb.share_to_parent ? 1 : 0;
          for (const s of fb.ability_scores || []) form.scores[s.dim_id] = s.score;
        }
        // 取该学生各维度的历史分数作为参照 —— 有锚点，打分标准才稳定，
        // 否则这周给 3 星、下周给 4 星可能只是手感不同，雷达图的「变化」就成了噪声。
        //
        // 注意：如果正在修改的是已有反馈，「该维度的最新分」就是本节自己打的，
        // 拿它当锚点会显示成「上次 5」而其实是老师刚点的 5 —— 校准就失真了。
        // 所以修改场景要往前取一位（previous）。
        const editingThisLesson = existing.value;
        const stu = await studentsApi.get(props.lesson.student_id);
        const map = {};
        for (const d of (stu.ability && stu.ability.dims) || []) {
          const anchor = editingThisLesson ? d.previous : d.latest;
          if (anchor !== null && anchor !== undefined) map[d.dim_id] = anchor;
        }
        lastScores.value = map;
      } catch (e) {
        fail(e.message);
      }
    });

    function insert(field, text) {
      const cur = form[field] || '';
      form[field] = cur ? cur.replace(/\s*$/, '') + '\n' + text : text;
    }

    function setScore(dimId, value) {
      // 再点一次同一个分数 = 取消打分（允许只打部分维度）
      form.scores[dimId] = form.scores[dimId] === value ? undefined : value;
    }

    async function save() {
      saving.value = true;
      try {
        const payload = {
          performance: form.performance || null,
          problems: form.problems || null,
          homework: form.homework || null,
          next_plan: form.next_plan || null,
          rating: form.rating || null,
          share_to_parent: form.share_to_parent ? 1 : 0,
          ability_scores: Object.entries(form.scores)
            .filter(([, v]) => v)
            .map(([dimId, score]) => ({ dim_id: Number(dimId), score })),
        };
        await feedbackApi.save(props.lesson.id, payload);
        ok('反馈已保存');
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
    }

    const dimPreview = (dimId) => lastScores.value[dimId] ?? null;

    return { dims, form, saving, existing, lastScores, PHRASES, insert, setScore, save, hhmm, shortDate, warn, dimPreview };
  },
  template: `
  <Modal :title="'课后反馈 · ' + lesson.student_name" @close="$emit('close')">
    <div class="small muted" style="margin:-6px 0 14px">
      {{ shortDate(lesson.start_at) }} {{ hhmm(lesson.start_at) }}
      <span v-if="lesson.topic"> · {{ lesson.topic }}</span>
      <span v-if="existing" class="tag green" style="margin-left:6px">已有反馈，正在修改</span>
    </div>

    <div v-for="f in [
        { key:'performance', label:'课堂表现', ph:'今天课上怎么样？' },
        { key:'problems',    label:'存在问题', ph:'哪里卡住了？' },
        { key:'homework',    label:'作业布置', ph:'布置了什么？' },
        { key:'next_plan',   label:'下次安排', ph:'下节课讲什么？' }
      ]" :key="f.key" class="field">
      <label>{{ f.label }} <span class="muted small">（可留空）</span></label>
      <textarea :rows="2" v-model="form[f.key]" :placeholder="f.ph"></textarea>
      <div class="chips" style="margin-top:5px">
        <span v-for="p in PHRASES[f.key]" :key="p" class="chip" @click="insert(f.key, p)">{{ p }}</span>
      </div>
    </div>

    <div class="field">
      <label>能力评分 <span class="muted small">（可只打几项，未打的沿用历史值；再点一次可取消）</span></label>
      <div v-for="d in dims" :key="d.id" class="rate-row">
        <span class="dim">{{ d.name }}</span>
        <div class="dots">
          <button v-for="v in 5" :key="v" type="button"
                  class="dot" :class="{ on: form.scores[d.id] === v }"
                  @click="setScore(d.id, v)">{{ v }}</button>
        </div>
        <span class="small muted" v-if="dimPreview(d.id)">上次 {{ dimPreview(d.id) }}</span>
      </div>
    </div>

    <div class="field">
      <label>本次整体状态</label>
      <div class="dots">
        <button v-for="v in 5" :key="v" type="button"
                class="dot" :class="{ on: form.rating === v }"
                @click="form.rating = form.rating === v ? null : v">{{ v }}</button>
      </div>
    </div>

    <label class="small" style="display:flex; align-items:center; gap:6px">
      <input type="checkbox" v-model="form.share_to_parent" :true-value="1" :false-value="0">
      本节课内容可发给家长
    </label>

    <template #foot>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="saving" @click="save">
        {{ saving ? '保存中…' : '保存反馈' }}
      </button>
    </template>
  </Modal>`,
};
