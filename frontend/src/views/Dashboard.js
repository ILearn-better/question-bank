// 今日工作台 —— 打开系统第一眼要看到的东西。
// 只回答四个问题：今天有哪几节课？哪些课还没写反馈？本月收了多少？有多少在读学生？
import { onMounted, ref } from 'vue';
import { dashboardApi, lessonsApi } from '../api.js';
import FeedbackDialog from '../components/FeedbackDialog.js';
import HomeworkDialog from '../components/HomeworkDialog.js';
import {
  currentPeriod, fail, hhmm, money, ok, shortDate, STATUS_LABEL, STATUS_TAG,
} from '../store.js';

export default {
  name: 'Dashboard',
  components: { FeedbackDialog, HomeworkDialog },
  setup() {
    const data = ref(null);
    const monthly = ref(null);
    const period = ref(currentPeriod());
    const loading = ref(true);
    const busy = ref(0);
    const feedbackLesson = ref(null);
    const homeworkLesson = ref(null);

    async function load() {
      loading.value = true;
      try {
        data.value = await dashboardApi.today();
        monthly.value = await lessonsApi.monthly(period.value);
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
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

    async function reloadMonthly() {
      try {
        monthly.value = await lessonsApi.monthly(period.value);
      } catch (e) {
        fail(e.message);
      }
    }

    function onFeedbackSaved() {
      feedbackLesson.value = null;
      load();
    }

    function onHomeworkSaved() {
      homeworkLesson.value = null;
      load();
    }

    onMounted(load);
    return {
      data, monthly, period, loading, busy, feedbackLesson, homeworkLesson,
      load, complete, reloadMonthly, onFeedbackSaved, onHomeworkSaved,
      hhmm, money, shortDate, STATUS_LABEL, STATUS_TAG,
    };
  },
  template: `
  <div>
    <div class="page-head">
      <h1>今日工作台</h1>
      <span class="sub" v-if="data">{{ data.date }}</span>
      <div class="spacer"></div>
      <button class="btn" @click="load">刷新</button>
      <router-link to="/schedule" class="btn primary">排一节课</router-link>
    </div>

    <div v-if="loading" class="empty">加载中…</div>

    <template v-else-if="data">
      <div class="grid cols-4">
        <div class="card stat">
          <div class="v">{{ data.lessons_today.length }}</div>
          <div class="k">今日课程</div>
        </div>
        <div class="card stat">
          <div class="v" :style="data.pending_feedback.length ? 'color:var(--warning)' : ''">
            {{ data.pending_feedback.length }}
          </div>
          <div class="k">待写反馈</div>
        </div>
        <div class="card stat">
          <div class="v">{{ money(data.month.total_amount) }}</div>
          <div class="k">本月应收（{{ data.month.total_lessons }} 节）</div>
        </div>
        <div class="card stat">
          <div class="v">{{ data.counts.students_active }}</div>
          <div class="k">在读学生 / 共 {{ data.counts.students_total }}</div>
        </div>
      </div>

      <div class="card" style="margin-top:14px">
        <h2>今日课程</h2>
        <div v-if="!data.lessons_today.length" class="empty">
          今天没有排课
          <div class="small" style="margin-top:6px">要排课去「课表」，上完课记得写反馈</div>
        </div>
        <div v-for="ls in data.lessons_today" :key="ls.id" class="lesson-row">
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
              <span v-if="ls.topic"> · {{ ls.topic }}</span>
            </div>
          </div>
          <button v-if="ls.status === 'scheduled'" class="btn sm"
                  :disabled="busy === ls.id" @click="complete(ls)">标记完成</button>
          <button class="btn sm" :class="ls.has_feedback ? '' : 'primary'"
                  @click="feedbackLesson = ls">
            {{ ls.has_feedback ? '改反馈' : '写反馈' }}
          </button>
          <!-- 作业是跟反馈并列的另一条线：有就改、没有就交（独立于反馈，互不依赖） -->
          <button class="btn ghost sm" @click="homeworkLesson = ls">
            {{ ls.has_homework ? '改作业' : '交作业' }}
          </button>
        </div>
      </div>

      <div class="card">
        <h2>
          待写反馈
          <span class="small muted" style="font-weight:400">（上完课但还没记录，越靠上越该先写）</span>
        </h2>
        <div v-if="!data.pending_feedback.length" class="empty">都写完了，很干净</div>
        <div v-for="ls in data.pending_feedback" :key="ls.id" class="lesson-row">
          <span class="time" style="width:auto">{{ shortDate(ls.start_at) }}</span>
          <div style="flex:1; min-width:0">
            <div class="who">
              <router-link :to="'/students/' + ls.student_id">{{ ls.student_name }}</router-link>
            </div>
            <div class="meta">{{ hhmm(ls.start_at) }} · {{ ls.duration_min }} 分钟</div>
          </div>
          <button class="btn sm primary" @click="feedbackLesson = ls">写反馈</button>
          <button class="btn ghost sm" @click="homeworkLesson = ls">
            {{ ls.has_homework ? '改作业' : '交作业' }}
          </button>
        </div>
      </div>

      <div class="card">
        <h2>
          本月课时费
          <span class="small muted" style="font-weight:400">（单价按上课时快照，改学生单价不影响历史）</span>
        </h2>
        <div class="row" style="max-width:280px; margin-bottom:10px">
          <input type="month" v-model="period" @change="reloadMonthly">
        </div>
        <div v-if="!monthly || !monthly.students.length" class="empty">本月还没有已完成的课时</div>
        <table v-else class="tbl">
          <thead>
            <tr><th>学生</th><th>课时数</th><th>时长</th><th style="text-align:right">应收</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in monthly.students" :key="r.student_id">
              <td><router-link :to="'/students/' + r.student_id">{{ r.student_name }}</router-link></td>
              <td>{{ r.lesson_count }}</td>
              <td>{{ (r.billable_minutes / 60).toFixed(1) }} 小时</td>
              <td style="text-align:right">{{ money(r.amount) }}</td>
            </tr>
            <tr>
              <td colspan="3" style="text-align:right; color:var(--text-2)">合计</td>
              <td style="text-align:right; font-weight:600">{{ money(monthly.total_amount) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="card" v-if="data.upcoming.length">
        <h2>未来 7 天</h2>
        <div v-for="ls in data.upcoming" :key="ls.id" class="lesson-row">
          <span class="time" style="width:auto">{{ shortDate(ls.start_at) }}</span>
          <span class="time" style="width:auto">{{ hhmm(ls.start_at) }}</span>
          <div style="flex:1">
            <router-link :to="'/students/' + ls.student_id">{{ ls.student_name }}</router-link>
            <span class="meta" v-if="ls.topic"> · {{ ls.topic }}</span>
          </div>
          <span class="tag blue">{{ STATUS_LABEL[ls.status] }}</span>
        </div>
      </div>
    </template>

    <FeedbackDialog v-if="feedbackLesson" :lesson="feedbackLesson"
                    @close="feedbackLesson = null" @saved="onFeedbackSaved" />
    <HomeworkDialog v-if="homeworkLesson" :lesson="homeworkLesson"
                    @close="homeworkLesson = null" @saved="onHomeworkSaved" />
  </div>`,
};
