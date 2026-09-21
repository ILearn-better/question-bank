// 学生详情 —— 「学生情况记录」的核心视图。
// 设计目标：打开这个人的页面，从上到下就能看完他发生了什么，不用在多个页面之间跳。
import { computed, onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { studentsApi } from '../api.js';
import AbilityRadar from '../components/AbilityRadar.js';
import FeedbackDialog from '../components/FeedbackDialog.js';
import { fail, hhmm, loadCurricula, money, ok, shortDate, STATUS_LABEL, STATUS_TAG } from '../store.js';

export default {
  name: 'StudentDetail',
  components: { AbilityRadar, FeedbackDialog },
  setup() {
    const route = useRoute();
    const id = Number(route.params.id);
    const stu = ref(null);
    const timeline = ref([]);
    const loading = ref(true);
    const feedbackLesson = ref(null);

    async function load() {
      loading.value = true;
      try {
        stu.value = await studentsApi.get(id);
        timeline.value = await studentsApi.timeline(id);
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    const pendingCount = computed(() => stu.value?.stats?.feedback_pending || 0);
    const abilityDims = computed(() => (stu.value && stu.value.ability && stu.value.ability.dims) || []);
    const hasAbility = computed(() => abilityDims.value.some((d) => d.latest));

    function openFeedback(item) {
      feedbackLesson.value = {
        id: item.lesson.id,
        student_id: id,
        student_name: stu.value.name,
        start_at: item.lesson.start_at,
        topic: item.lesson.topic,
      };
    }

    async function onSaved() {
      feedbackLesson.value = null;
      ok('反馈已保存');
      await load();
    }

    onMounted(async () => {
      await loadCurricula();
      await load();
    });

    return {
      stu, timeline, loading, feedbackLesson, pendingCount, abilityDims, hasAbility,
      openFeedback, onSaved, load, hhmm, money, shortDate, STATUS_LABEL, STATUS_TAG,
    };
  },
  template: `
  <div>
    <div v-if="loading" class="empty">加载中…</div>
    <template v-else-if="stu">
      <div class="page-head">
        <h1>{{ stu.name }}</h1>
        <span class="tag" v-if="stu.status === 'active'">在读</span>
        <span class="tag orange" v-else-if="stu.status === 'paused'">暂停</span>
        <span class="tag" v-else>已结课</span>
        <span class="sub">{{ stu.grade || '' }} {{ (stu.curriculum_names || []).join(' / ') }}</span>
        <div class="spacer"></div>
        <router-link to="/students" class="btn">返回列表</router-link>
      </div>

      <div class="grid cols-4">
        <div class="card stat"><div class="v">{{ stu.stats.lesson_done }}</div><div class="k">已上课时</div></div>
        <div class="card stat"><div class="v">{{ (stu.stats.minutes_total / 60).toFixed(1) }}</div><div class="k">累计小时</div></div>
        <div class="card stat">
          <div class="v" :style="pendingCount ? 'color:var(--warning)' : ''">{{ pendingCount }}</div>
          <div class="k">待写反馈</div>
        </div>
        <div class="card stat"><div class="v">{{ money(stu.hourly_rate) }}</div><div class="k">单价 / {{ stu.rate_unit === 'session' ? '次' : '小时' }}</div></div>
      </div>

      <div class="grid cols-2" style="margin-top:14px">
        <div class="card">
          <h2>能力表现</h2>
          <div v-if="!hasAbility" class="empty">
            还没有能力评分
            <div class="small" style="margin-top:6px">在任何一节课上点「写反馈」，顺手点几个分数即可</div>
          </div>
          <AbilityRadar v-else :dims="abilityDims" :size="300" />
          <table v-if="hasAbility" class="tbl" style="margin-top:10px">
            <thead><tr><th>维度</th><th>本次</th><th>上次</th><th>变化</th><th>记录次数</th></tr></thead>
            <tbody>
              <tr v-for="d in abilityDims" :key="d.dim_id">
                <td>{{ d.name }}</td>
                <td>{{ d.latest ?? '—' }}</td>
                <td class="muted">{{ d.previous ?? '—' }}</td>
                <td>
                  <span v-if="d.latest && d.previous && d.latest > d.previous" style="color:var(--success)">↑ {{ d.latest - d.previous }}</span>
                  <span v-else-if="d.latest && d.previous && d.latest < d.previous" style="color:var(--danger)">↓ {{ d.previous - d.latest }}</span>
                  <span v-else class="muted">—</span>
                </td>
                <td class="muted">{{ d.count }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="card">
          <h2>基本信息</h2>
          <table class="tbl">
            <tbody>
              <tr><th style="width:90px">学校</th><td>{{ stu.school || '—' }}</td></tr>
              <tr><th>联系方式</th><td>{{ stu.contact || '—' }}</td></tr>
              <tr><th>家长联系</th><td>{{ stu.parent_contact || '—' }}</td></tr>
              <tr><th>开始日期</th><td>{{ stu.started_at || '—' }}</td></tr>
              <tr><th>累计课时费</th><td>{{ money(stu.stats.amount_total) }}</td></tr>
              <tr><th>备注</th><td>{{ stu.remark || '—' }}</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <h2>上课记录 <span class="small muted" style="font-weight:400">（从新到旧，共 {{ timeline.length }} 条）</span></h2>
        <div v-if="!timeline.length" class="empty">
          还没有课时记录
          <div class="small" style="margin-top:6px">去「课表」给他排一节课</div>
        </div>
        <div v-else class="timeline">
          <div v-for="item in timeline" :key="item.lesson.id" class="tl-item"
               :class="item.feedback ? 'done' : 'pending'">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
              <span class="small muted mono">{{ item.lesson.start_at.replace('T', ' ').slice(0, 16) }}</span>
              <span class="tag" :class="STATUS_TAG[item.lesson.status]">{{ STATUS_LABEL[item.lesson.status] || item.lesson.status }}</span>
              <span class="small muted">{{ item.lesson.duration_min }} 分钟</span>
              <span v-if="item.lesson.amount !== null" class="small muted">{{ money(item.lesson.amount) }}</span>
              <div class="spacer"></div>
              <button class="btn ghost sm" @click="openFeedback(item)">
                {{ item.feedback ? '改反馈' : '写反馈' }}
              </button>
            </div>

            <div v-if="item.lesson.topic" style="margin-top:4px">本次内容：{{ item.lesson.topic }}</div>

            <div v-if="item.feedback" class="card" style="margin-top:8px; padding:10px 12px; background:var(--panel-2)">
              <div v-if="item.feedback.performance"><b class="small">课堂表现</b><div>{{ item.feedback.performance }}</div></div>
              <div v-if="item.feedback.problems" style="margin-top:6px"><b class="small">存在问题</b><div>{{ item.feedback.problems }}</div></div>
              <div v-if="item.feedback.homework" style="margin-top:6px"><b class="small">作业布置</b><div>{{ item.feedback.homework }}</div></div>
              <div v-if="item.feedback.next_plan" style="margin-top:6px"><b class="small">下次安排</b><div>{{ item.feedback.next_plan }}</div></div>
              <div v-if="item.ability && item.ability.length" class="chips" style="margin-top:8px">
                <span v-for="a in item.ability" :key="a.dim_id" class="tag blue">{{ a.name }} {{ a.score }}</span>
              </div>
            </div>
            <div v-else class="small" style="margin-top:6px; color:var(--warning)">尚未写反馈</div>
          </div>
        </div>
      </div>
    </template>

    <FeedbackDialog v-if="feedbackLesson" :lesson="feedbackLesson"
                    @close="feedbackLesson = null" @saved="onSaved" />
  </div>`,
};
