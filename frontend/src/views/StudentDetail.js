// 学生详情 —— 「学生情况记录」的核心视图。
// 设计目标：打开这个人的页面，从上到下就能看完他发生了什么，不用在多个页面之间跳。
import { computed, onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { studentsApi, homeworkApi, supplementsApi, prepApi } from '../api.js';
import AbilityRadar from '../components/AbilityRadar.js';
import FeedbackDialog from '../components/FeedbackDialog.js';
import HomeworkDialog from '../components/HomeworkDialog.js';
import SupplementDialog from '../components/SupplementDialog.js';
import PrepDialog from '../components/PrepDialog.js';
import { fail, fmtBytes, hhmm, loadCurricula, money, ok, shortDate, STATUS_LABEL, STATUS_TAG } from '../store.js';

/** 作业状态 -> 标签颜色。未交看红：家长沟通里这是必须提的事。 */
const HW_TAG = { submitted: 'green', late: 'orange', missing: 'red' };
/** 课后补充的状态 -> 标签颜色。还没打印看橙：下次上课前要处理的就是它。 */
const SUP_TAG = { todo: 'orange', given: 'blue', returned: 'green' };

export default {
  name: 'StudentDetail',
  components: { AbilityRadar, FeedbackDialog, HomeworkDialog, SupplementDialog, PrepDialog },
  setup() {
    const route = useRoute();
    const id = Number(route.params.id);
    const stu = ref(null);
    const timeline = ref([]);
    const loading = ref(true);
    const feedbackLesson = ref(null);
    // 归档文件夹：老师经常要直接翻（找讲义、看孩子这段时间留下了什么）
    const folder = ref(null);
    const openingFolder = ref(false);
    // 作业：雷达图 + 每节课的作业记录（跟反馈并列，互不依赖）
    const hwRadar = ref(null);
    const homeworkLesson = ref(null);
    // 课后补充：我准备下次给他的材料（挂在课上，与作业方向相反 —— 那边是收，这边是发）
    const supplementLesson = ref(null);
    const supPending = ref({ count: 0, items: [] });
    // lesson_id -> 那个批次数组（时间轴上每条课直接取）
    const supsByLesson = ref({});
    const printing = ref(0);
    // 课前备课：这节课老师自己的准备（结构化四段 + 挑材料 + 关联笔记）
    const prepLesson = ref(null);
    const prepsByLesson = ref({});

    async function loadSupplements() {
      const map = {};
      await Promise.all(timeline.value.map(async (it) => {
        try {
          const r = await supplementsApi.list(it.lesson.id);
          map[it.lesson.id] = r.items || [];
        } catch (e) { map[it.lesson.id] = []; }
      }));
      supsByLesson.value = map;
      try {
        supPending.value = await supplementsApi.pending(id);
      } catch (e) {
        supPending.value = { count: 0, items: [] };
      }
    }

    async function loadPreps() {
      const map = {};
      await Promise.all(timeline.value.map(async (it) => {
        try {
          const r = await prepApi.get(it.lesson.id);
          map[it.lesson.id] = r.prep || null;
        } catch (e) { map[it.lesson.id] = null; }
      }));
      prepsByLesson.value = map;
    }

    async function load() {
      loading.value = true;
      try {
        stu.value = await studentsApi.get(id);
        timeline.value = await studentsApi.timeline(id);
        folder.value = await studentsApi.folder(id);
        hwRadar.value = await homeworkApi.radar(id);
        await loadSupplements();
        await loadPreps();
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    /** 「课后补充」：挑几样下次给他的材料，存进这节课。 */
    function openSupplement(item) {
      supplementLesson.value = {
        id: item.lesson.id,
        student_id: id,
        student_name: stu.value.name,
        start_at: item.lesson.start_at,
        topic: item.lesson.topic,
      };
    }

    async function onSupplementSaved() {
      supplementLesson.value = null;
      // 提示条由弹窗自己给（那句更具体：说明这是给下次上课准备的），这里不再重复弹一条
      await loadSupplements();
    }

    async function setSupStatus(batch, status) {
      try {
        await supplementsApi.update(batch.id, { status });
        await loadSupplements();
      } catch (e) {
        fail(e.message);
      }
    }

    async function printSup(batch) {
      printing.value = batch.id;
      try {
        const when = (batch.created_at || '').slice(0, 10);
        await supplementsApi.print(batch.id, `${stu.value.name}_${when}_课后补充_学生版.pdf`);
        ok('已出学生版（不含答案），并记成「已给」');
        await loadSupplements();
      } catch (e) {
        fail(e.message);
      } finally {
        printing.value = 0;
      }
    }

    async function removeSup(batch) {
      if (!window.confirm('删掉这批课后补充？（题库里的题、笔记都不会动）')) return;
      try {
        await supplementsApi.remove(batch.id);
        await loadSupplements();
      } catch (e) {
        fail(e.message);
      }
    }

    /** 在资源管理器里打开这个学生的文件夹。
     *  服务端就在本机，所以是真打开 —— 浏览器沙箱里做不到这种事。 */
    async function openFolder() {
      openingFolder.value = true;
      try {
        await studentsApi.openFolder(id);
        // 顺手把统计刷新一下：老师刚往里拖过东西的话，数字要跟上
        folder.value = await studentsApi.folder(id);
      } catch (e) {
        fail(e.message);
      } finally {
        openingFolder.value = false;
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

    /** 「交作业」：跟反馈并列的入口，同一节课、同一个位置。 */
    function openHomework(item) {
      homeworkLesson.value = {
        id: item.lesson.id,
        student_id: id,
        student_name: stu.value.name,
        start_at: item.lesson.start_at,
        topic: item.lesson.topic,
      };
    }

    async function onHomeworkSaved() {
      homeworkLesson.value = null;
      await load();
    }

    /** 「课前备课」：与反馈/作业/课后补充并列的入口，同一节课、同一个位置。 */
    function openPrep(item) {
      prepLesson.value = {
        id: item.lesson.id,
        student_id: id,
        student_name: stu.value.name,
        start_at: item.lesson.start_at,
        topic: item.lesson.topic,
      };
    }

    async function onPrepSaved() {
      prepLesson.value = null;
      await loadPreps();
    }

    onMounted(async () => {
      await loadCurricula();
      await load();
    });

    return {
      stu, timeline, loading, feedbackLesson, pendingCount, abilityDims, hasAbility,
      openFeedback, onSaved, load, hhmm, money, shortDate, STATUS_LABEL, STATUS_TAG,
      folder, openingFolder, openFolder, fmtBytes,
      hwRadar, homeworkLesson, openHomework, onHomeworkSaved, HW_TAG,
      supplementLesson, supPending, supsByLesson, openSupplement, onSupplementSaved,
      setSupStatus, printSup, removeSup, printing, SUP_TAG,
      prepLesson, prepsByLesson, openPrep, onPrepSaved,
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
              <tr>
                <th>资料文件夹</th>
                <td>
                  <div class="mono small" style="word-break:break-all">{{ folder ? folder.rel_dir : '—' }}</div>
                  <div class="small muted" style="margin-top:2px">
                    <template v-if="folder && folder.file_count">
                      共 {{ folder.file_count }} 个文件 · {{ fmtBytes(folder.total_bytes) }}
                    </template>
                    <template v-else>还没有资料（传讲义或贴配图就会建起来）</template>
                  </div>
                  <button class="btn sm" style="margin-top:6px"
                          :disabled="openingFolder" @click="openFolder">
                    {{ openingFolder ? '正在打开…' : '打开文件夹' }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 作业：与上面那张课堂雷达是**两张图、两套维度**（用户明确要求分开）——
           硬画进同一张只会让人误读，也说不清变化来自哪一边。 -->
      <div class="card">
        <h2>
          作业情况
          <span v-if="hwRadar && hwRadar.total" class="small muted" style="font-weight:400">
            （共 {{ hwRadar.total }} 次：已交 {{ hwRadar.status_counts.submitted }} ·
             迟交 {{ hwRadar.status_counts.late }} · 未交 {{ hwRadar.status_counts.missing }}）
          </span>
        </h2>
        <div v-if="!hwRadar || !hwRadar.total" class="empty">
          还没有作业记录
          <div class="small" style="margin-top:6px">在上课记录里点「交作业」，记一次作业交没交、顺手打几个分</div>
        </div>
        <div v-else>
          <div class="grid cols-2">
            <div>
              <AbilityRadar v-if="hwRadar.has_data" :dims="hwRadar.dims" :size="300" />
              <div v-else class="empty">
                还没有作业评分
                <div class="small" style="margin-top:6px">
                  只记了交没交也算记录；想看到雷达图，在作业上点几个维度即可
                </div>
              </div>
            </div>
            <table v-if="hwRadar.has_data" class="tbl">
              <thead><tr><th>维度</th><th>本次</th><th>上次</th><th>变化</th><th>次数</th></tr></thead>
              <tbody>
                <tr v-for="d in hwRadar.dims" :key="d.dim_id">
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
        </div>
      </div>

      <div class="card" v-if="supPending.count"
           style="border-color:var(--warning); background:var(--panel-2)">
        <b>有 {{ supPending.count }} 批课后补充还没给</b>
        <div class="small muted" style="margin-top:4px">
          挑好了但还没打印 —— 下面上课记录里带「还没打印」标记的就是。
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
              <button class="btn ghost sm" @click="openPrep(item)"
                      title="上课前的准备：写四段备课 + 挑这节课要用的材料">
                {{ prepsByLesson[item.lesson.id] ? '改备课' : '备课' }}
              </button>
              <button class="btn ghost sm" @click="openFeedback(item)">
                {{ item.feedback ? '改反馈' : '写反馈' }}
              </button>
              <button class="btn ghost sm" @click="openHomework(item)">
                {{ item.homework ? '改作业' : '交作业' }}
              </button>
              <button class="btn ghost sm" @click="openSupplement(item)"
                      title="挑几样下次上课要给他的材料（题库的题 / 笔记）">课后补充</button>
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

            <!-- 作业：跟反馈并列，**各自独立**（可能只有作业没反馈，反之亦然） -->
            <div v-if="item.homework" style="margin-top:8px">
              <span class="tag" :class="HW_TAG[item.homework.status] || ''">作业{{ item.homework.status_label }}</span>
              <span v-for="a in item.homework.scores" :key="a.dim_id" class="tag blue" style="margin-left:4px">
                {{ a.name }} {{ a.score }}
              </span>
              <div v-if="item.homework.note" class="small" style="margin-top:4px">批改备注：{{ item.homework.note }}</div>
            </div>
            <div v-else class="small muted" style="margin-top:6px">这次作业还没记</div>

            <!-- 课后补充：与作业方向相反（那边是收，这边是发），所以分两块 ---->
            <div v-for="b in (supsByLesson[item.lesson.id] || [])" :key="b.id" class="sup-row">
              <span class="tag" :class="SUP_TAG[b.status] || ''">课后补充·{{ b.status_label }}</span>
              <span class="small muted">{{ (b.created_at || '').slice(5, 16).replace('T', ' ') }}</span>
              <span v-if="b.focus" class="tag blue">{{ b.focus }}</span>
              <span class="small">
                <span v-for="(it, i) in b.items" :key="it.id">
                  <span v-if="i">、</span>{{ it.kind === 'question' ? '题' : '笔记' }}·{{ it.title }}<span v-if="!it.exists" class="muted">（内容已删）</span>
                </span>
              </span>
              <div class="spacer"></div>
              <button v-if="b.questions.length" class="btn sm" :disabled="printing === b.id"
                      @click="printSup(b)" title="出学生版 PDF（不含答案），顺手记成已给">
                {{ printing === b.id ? '生成中…' : '打印学生版' }}
              </button>
              <button v-if="b.status !== 'given'" class="btn sm ghost" @click="setSupStatus(b, 'given')">标为已给</button>
              <button v-if="b.status === 'given'" class="btn sm ghost" @click="setSupStatus(b, 'returned')">已交回</button>
              <button class="btn sm ghost" @click="removeSup(b)">删</button>
              <div v-if="b.note" class="small muted" style="width:100%">备注：{{ b.note }}</div>
            </div>

            <!-- 课前备课：与反馈/作业/课后补充并列的第四样 -->
            <div v-if="prepsByLesson[item.lesson.id]" style="margin-top:8px">
              <span class="tag green">已备课</span>
              <span v-if="prepsByLesson[item.lesson.id].note_title" class="tag blue" style="margin-left:4px">
                笔记·{{ prepsByLesson[item.lesson.id].note_title }}
              </span>
              <div v-if="prepsByLesson[item.lesson.id].goal" class="small" style="margin-top:4px">
                教学目标：{{ prepsByLesson[item.lesson.id].goal }}
              </div>
              <div v-if="(prepsByLesson[item.lesson.id].items || []).length" class="small muted" style="margin-top:4px">
                备了 {{ prepsByLesson[item.lesson.id].items.length }} 样材料
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <FeedbackDialog v-if="feedbackLesson" :lesson="feedbackLesson"
                    @close="feedbackLesson = null" @saved="onSaved" />
    <HomeworkDialog v-if="homeworkLesson" :lesson="homeworkLesson"
                    @close="homeworkLesson = null" @saved="onHomeworkSaved" />
    <SupplementDialog v-if="supplementLesson" :lesson="supplementLesson"
                      @close="supplementLesson = null" @saved="onSupplementSaved" />
    <PrepDialog v-if="prepLesson" :lesson="prepLesson"
                @close="prepLesson = null" @saved="onPrepSaved" />
  </div>`,
};
