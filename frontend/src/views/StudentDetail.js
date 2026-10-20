// 学生详情 —— 「学生情况记录」的核心视图。
// 设计目标：打开这个人的页面，从上到下就能看完他发生了什么，不用在多个页面之间跳。
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { studentsApi, homeworkApi, supplementsApi, prepApi, examsApi, plansApi, gradeApi, archiveApi } from '../api.js';
import AbilityRadar from '../components/AbilityRadar.js';
import FeedbackDialog from '../components/FeedbackDialog.js';
import HomeworkDialog from '../components/HomeworkDialog.js';
import SupplementDialog from '../components/SupplementDialog.js';
import PrepDialog from '../components/PrepDialog.js';
import ExamDialog from '../components/ExamDialog.js';
import PlanDialog from '../components/PlanDialog.js';
import Modal from '../components/Modal.js';
import { filledSections, analysisPreview, analysisStats } from '../examAnalysis.js';
import { fail, fmtBytes, hhmm, loadCurricula, money, ok, shortDate, STATUS_LABEL, STATUS_TAG } from '../store.js';

/** 作业状态 -> 标签颜色。未交看红：家长沟通里这是必须提的事。 */
const HW_TAG = { submitted: 'green', late: 'orange', missing: 'red' };
/** 课后补充的状态 -> 标签颜色。还没打印看橙：下次上课前要处理的就是它。 */
const SUP_TAG = { todo: 'orange', given: 'blue', returned: 'green' };

export default {
  name: 'StudentDetail',
  components: { AbilityRadar, FeedbackDialog, HomeworkDialog, SupplementDialog, PrepDialog, ExamDialog, PlanDialog, Modal },
  setup() {
    const route = useRoute();
    const router = useRouter();
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
    // 考试情况：**学生级**记录，与上课记录并列 —— 考试日期和上课日期不一定对得上，
    // 所以它不挂在任何一节课上（见 routers/exams.py 文件头）。
    const exams = ref([]);
    const examTypes = ref([]);
    const examDialog = ref(null);      // { exam: 记录或 null }
    const expandedExam = ref(0);       // 展开看详情的那条
    // 整体规划：**学生级**的文档（可新建/查看/改/删），放在「考试情况」上方。
    // 列表只拿标题 + 摘要，点开某条才去取完整正文（list 不带正文，见 routers/plans.py 设计⑤）。
    const plans = ref([]);
    const planDialog = ref(null);      // { plan: 简要对象或 null, startEdit: bool }
    // 改作业：批改完成后归档下来的副本（一份批改 = 一份带笔迹的 PDF）。
    // 列表只拿摘要，**点开某条才取详情** —— 一条记录可能挂着 20MB 的 PDF。
    const archives = ref([]);
    const archiveOpen = ref(null);     // 正在预览的那条（详情对象）
    const archiveBusy = ref('');       // 'open' / 'upload' / 'remove'，按钮置灰用
    const archiveFile = ref(null);     // 隐藏的 <input type=file>（上传存档用）

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

    async function loadExams() {
      try {
        const r = await examsApi.list(id);
        exams.value = r.items || [];
        // 常见类型 + 库里真用过的，一起喂给弹窗的 chips（录过「入学分班考」下次就能点）
        examTypes.value = [...(r.used_types || []), ...(r.type_suggestions || [])];
      } catch (e) {
        exams.value = [];
      }
    }

    async function loadArchives() {
      try {
        const r = await archiveApi.list(id);
        archives.value = r.items || [];
      } catch (e) {
        archives.value = [];
      }
    }

    /** 点开一条存档：先弹窗（文件名/大小立刻可见），详情随后补上。
     *  ⚠️ 详情取不到时**不能关弹窗** —— 那时至少还能点「下载」把文件拿出来，
     *     关掉等于让人看着列表干瞪眼。 */
    async function openArchive(a) {
      archiveOpen.value = a;
      archiveBusy.value = 'open';
      try {
        archiveOpen.value = await archiveApi.get(a.id);
      } catch (e) {
        fail(e.message);
      } finally {
        archiveBusy.value = '';
      }
    }

    function closeArchive() {
      archiveOpen.value = null;
    }

    async function removeArchive(a) {
      if (!a) return;
      if (!window.confirm(`删掉「${a.name}」这份存档？（磁盘上的文件也一起删，不可恢复）`)) return;
      archiveBusy.value = 'remove';
      try {
        await archiveApi.remove(a.id);
        ok('这份存档已删掉');
        if (archiveOpen.value && archiveOpen.value.id === a.id) archiveOpen.value = null;
        await loadArchives();
      } catch (e) {
        fail(e.message);
      } finally {
        archiveBusy.value = '';
      }
    }

    /** 「＋ 上传存档」：线下批好的卷子、别处拿到的答案，直接存进这个学生的资料。
     *  与批改页的「导出到作业存档」走**同一个接口层**（落盘、避重、校验只有一套）。 */
    function pickArchiveFile() {
      if (archiveFile.value) archiveFile.value.click();
    }

    async function onArchiveFile(ev) {
      const f = ev.target.files && ev.target.files[0];
      ev.target.value = '';            // 清空，否则选同一个文件第二次不触发 change
      if (!f) return;
      const fd = new FormData();
      fd.append('file', f);
      fd.append('student_id', String(id));
      archiveBusy.value = 'upload';
      try {
        const r = await archiveApi.upload(fd);
        ok('已存进学生资料：' + (r.rel_path || r.name));
        await loadArchives();
      } catch (e) {
        fail(e.message);
      } finally {
        archiveBusy.value = '';
      }
    }

    async function loadPlans() {
      try {
        const r = await plansApi.list(id);
        plans.value = r.items || [];
      } catch (e) {
        plans.value = [];
      }
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
        await loadExams();
        await loadPlans();
        await loadArchives();
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

    /* 哪些 kind 能直接 <img>。PDF 走 <iframe>（浏览器内置阅读器），
       Word/Excel/压缩包一律只给下载 —— 拖进 iframe 只会是白屏。
       ⚠️ 这套判断的**权威版本在后端**（archive_store.PREVIEW_KINDS），
          这里只是「怎么显示」，can_preview 仍然是后端给的那个字段。 */
    const IMG_KINDS = ['png', 'jpg', 'webp', 'gif', 'bmp'];
    const archiveIsImg = computed(
      () => IMG_KINDS.includes((archiveOpen.value && archiveOpen.value.kind) || ''));

    /** 存档按课时分组 —— 「上课记录」里每节课下面要显示**这节课的**存档。
     *
     *  口径就一个：`lesson_id`。有课时的进那节课，没有的（手传的、或课被删了
     *  变成 SET NULL 的）不进任何课，只留在「改作业」总览里。
     *  ⚠️ 为什么不问后端要「按课过滤」的接口：一个学生一年也就几百条，
     *     列表本来就一次全取（`list_for_student` 已带 lesson_id），
     *     在前端分一次组比多一次请求更快，也不用再维护两套口径。
     *  ⚠️ 不去拿 `archives` 之外的来源拼：这节课「有没有存档」必须与总览一致，
     *     两处各算一遍迟早出现「课下有、总览没有」。 */
    const archivesByLesson = computed(() => {
      const m = {};
      for (const a of archives.value) {
        if (!a.lesson_id) continue;
        (m[a.lesson_id] || (m[a.lesson_id] = [])).push(a);
      }
      return m;
    });

    /* 判定分布：只在详情里出现（列表不查 items，那是几十行 × 一堆字段）。 */
    const verdictRows = [
      { key: 'correct', label: '对', cls: 'green' },
      { key: 'partial', label: '部分对', cls: 'orange' },
      { key: 'wrong', label: '错', cls: 'red' },
      { key: 'blank', label: '空白', cls: '' },
      { key: 'ungradable', label: '判不了', cls: '' },
    ];

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

    /** 「保存并批改」：作业在弹窗里已经存好了，这里只负责建批改任务、跳到批改台。
     *  建任务失败就停在当前页报错 —— 不要把老师丢到一个空白的批改页。 */
    async function onGrade({ lessonId, fileIds }) {
      try {
        const g = await gradeApi.create({
          lesson_id: lessonId,
          file_ids: fileIds || [],
          answer_mode: 'ai',
        });
        homeworkLesson.value = null;
        router.push('/grade/' + g.id);
      } catch (e) {
        fail(e.message);
      }
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

    /** 「记一次考试」：**学生级**入口，不依赖某节课（考试日期和上课日期常常对不上）。 */
    function openExam(e) {
      examDialog.value = { exam: e || null };
    }

    async function onExamSaved() {
      examDialog.value = null;
      await loadExams();
    }

    function toggleExam(eid) {
      expandedExam.value = expandedExam.value === eid ? 0 : eid;
    }

    async function removeExam(e) {
      const what = [e.exam_type, e.exam_date].filter(Boolean).join(' · ') || '这次考试';
      if (!window.confirm(`删掉「${what}」的记录？（试题/答题卡的归档文件也会一起删）`)) return;
      try {
        await examsApi.remove(e.id);
        ok('考试记录已删掉');
        await loadExams();
      } catch (err) {
        fail(err.message);
      }
    }

    // ---- 整体规划：点开一条 = 看排版好的正文；「改」直接进编辑态 ----
    function openPlan(p) {
      planDialog.value = { plan: p || null, startEdit: !p };
    }

    function editPlan(p) {
      planDialog.value = { plan: p, startEdit: true };
    }

    async function onPlanSaved() {
      planDialog.value = null;
      await loadPlans();
    }

    async function removePlan(p) {
      const what = p.title || '这条规划';
      if (!window.confirm(`删掉「${what}」？（原件也会一起删）`)) return;
      try {
        await plansApi.remove(p.id);
        ok('规划已删掉');
        await loadPlans();
      } catch (err) {
        fail(err.message);
      }
    }

    // ---- 考试行上的展示小工具（都是纯函数，模板里直接调）----
    const examRawUrl = (fid, inline) => examsApi.rawUrl(fid, inline);
    const isImg = (name) => /\.(png|jpe?g|gif|webp|bmp)$/i.test(String(name || ''));
    const examHasScore = (e) => e.score !== null && e.score !== undefined;
    /** 得分率只在「有成绩且有满分」时才算得出来 —— 算不出就什么都不显示，
     *  拿成绩当分母凑个 100% 比空着更误导。 */
    const scoreRatePct = (e) => (e.score_rate === null || e.score_rate === undefined)
      ? '' : `${(e.score_rate * 100).toFixed(1)}%`;

    onMounted(async () => {
      await loadCurricula();
      await load();
    });

    return {
      stu, timeline, loading, feedbackLesson, pendingCount, abilityDims, hasAbility,
      openFeedback, onSaved, load, hhmm, money, shortDate, STATUS_LABEL, STATUS_TAG,
      folder, openingFolder, openFolder, fmtBytes,
      hwRadar, homeworkLesson, openHomework, onHomeworkSaved, HW_TAG, onGrade,
      supplementLesson, supPending, supsByLesson, openSupplement, onSupplementSaved,
      setSupStatus, printSup, removeSup, printing, SUP_TAG,
      prepLesson, prepsByLesson, openPrep, onPrepSaved,
      exams, examTypes, examDialog, expandedExam,
      openExam, onExamSaved, toggleExam, removeExam,
      examRawUrl, isImg, examHasScore, scoreRatePct,
      plans, planDialog, openPlan, editPlan, onPlanSaved, removePlan,
      // 改作业：批改存档（学生资料 → 改作业）
      archives, archiveOpen, archiveBusy, archiveFile, archiveIsImg, verdictRows,
      archivesByLesson,
      openArchive, closeArchive, removeArchive, pickArchiveFile, onArchiveFile,
      archiveRawUrl: archiveApi.rawUrl,
      filledSections, analysisPreview, analysisStats,
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

      <!-- 改作业：批改完成后归档下来的副本（目标流程的最后一步）。
           与上面「作业情况」是一回事的两面 —— 那边是分数与维度，这边是**卷子本身**。
           ⚠️ 列表只给摘要；点开某条才取详情（一条记录可能挂着 20MB 的 PDF）。 -->
      <div class="card">
        <h2>
          改作业
          <span v-if="archives.length" class="small muted" style="font-weight:400">
            （共 {{ archives.length }} 份批改存档）
          </span>
          <div class="spacer"></div>
          <button class="btn sm primary" :disabled="archiveBusy === 'upload'"
                  title="线下批好的卷子、别处拿到的答案，直接存进这个学生的资料"
                  @click="pickArchiveFile">
            {{ archiveBusy === 'upload' ? '上传中…' : '＋ 上传存档' }}
          </button>
        </h2>
        <input ref="archiveFile" type="file" style="display:none" @change="onArchiveFile">
        <div v-if="!archives.length" class="empty">
          还没有批改存档
          <div class="small" style="margin-top:6px">
            在批改台批完之后点「导出到作业存档」，批好的卷子就会出现在这里
          </div>
        </div>
        <div v-else>
          <div v-for="a in archives" :key="a.id" class="archive-row">
            <div class="archive-main" @click="openArchive(a)"
                 :title="a.can_preview ? '点开看这次作业的批改情况' : '这类文件不能在线看，点「下载」'">
              <div class="archive-head">
                <span class="tag blue">{{ a.archived_on }}</span>
                <b class="archive-name">{{ a.name }}</b>
                <span v-if="a.source === 'grade'" class="tag green">批改导出</span>
                <span v-else class="tag">手传</span>
              </div>
              <div class="small muted" style="margin-top:3px">
                <!-- 对应课显示**上课那天**，不是归档日期：老师今天可以补存上周的卷子，
                     两个日期能差好几天；只给日期不给主题，是因为主题常常是空的。 -->
                <template v-if="a.lesson_start_at">
                  对应课：{{ a.lesson_start_at.replace('T', ' ').slice(0, 16) }} ·
                </template>
                <template v-else-if="a.lesson_topic">作业：{{ a.lesson_topic }} · </template>
                <template v-if="a.grader">批改人：{{ a.grader }} · </template>
                {{ fmtBytes(a.size_bytes) }}
                <template v-if="!a.can_preview"> · 这类文件只能下载</template>
              </div>
            </div>
            <div class="spacer"></div>
            <button class="btn ghost sm" @click="openArchive(a)">查看</button>
            <a class="btn ghost sm" :href="archiveRawUrl(a.id)" :download="a.name">下载</a>
            <button class="btn ghost sm" style="color:var(--danger)"
                    @click="removeArchive(a)">删</button>
          </div>
        </div>
      </div>

      <!-- 整体规划：**学生级**的一份文档（正文本体存库，可打字、也可由 Word/Markdown
           文件转成）。列表只给标题 + 摘要，**点开某条才渲染正文** ——
           一条几千字的规划铺在列表里，这一页就没法往下翻了（见 routers/plans.py 设计⑤）。 -->
      <div class="card">
        <h2>
          整体规划
          <span v-if="plans.length" class="small muted" style="font-weight:400">（共 {{ plans.length }} 条）</span>
          <div class="spacer"></div>
          <button class="btn sm primary" @click="openPlan(null)">＋ 新建规划</button>
        </h2>
        <div v-if="!plans.length" class="empty">
          还没有规划
          <div class="small" style="margin-top:6px">
            新建一条，直接打字写；也可以传一份 Word / Markdown 文件，自动转成正文
          </div>
        </div>
        <div v-else>
          <div v-for="p in plans" :key="p.id" class="plan-row">
            <div class="plan-main" @click="openPlan(p)" title="点开看排版好的正文">
              <div class="plan-head">
                <b class="plan-name">{{ p.title || '（未命名规划）' }}</b>
                <span v-if="!p.has_content" class="tag orange">还没有正文</span>
                <span v-if="p.file_count" class="tag">{{ p.file_count }} 份原件</span>
                <span class="small muted">{{ (p.updated_at || '').slice(0, 10) }}</span>
              </div>
              <div v-if="p.preview" class="plan-preview">{{ p.preview }}</div>
              <div v-else class="small muted">（点开写点内容）</div>
            </div>
            <div class="spacer"></div>
            <button class="btn ghost sm" @click="openPlan(p)">查看</button>
            <button class="btn ghost sm" @click="editPlan(p)">改</button>
            <button class="btn ghost sm" style="color:var(--danger)" @click="removePlan(p)">删</button>
          </div>
        </div>
      </div>

      <!-- 考试情况：**学生级**记录，与上课记录并列（考试日期和上课日期不一定对得上，
           所以不挂在某节课上）。成绩、等级、试题/答题卡、考情分析都在这里。 -->
      <div class="card">
        <h2>
          考试情况
          <span v-if="exams.length" class="small muted" style="font-weight:400">（共 {{ exams.length }} 次）</span>
          <div class="spacer"></div>
          <button class="btn sm primary" @click="openExam(null)">＋ 记一次考试</button>
        </h2>
        <div v-if="!exams.length" class="empty">
          还没有考试记录
          <div class="small" style="margin-top:6px">
            记一次考试：日期（可以只写大概）、类型、成绩，传上试题与答题卡，再按模板写考情分析
          </div>
        </div>
        <div v-else>
          <div v-for="e in exams" :key="e.id" class="exam-row">
            <div class="exam-head">
              <span v-if="e.exam_type" class="tag blue">{{ e.exam_type }}</span>
              <span class="small muted">{{ e.exam_date || '未填日期' }}</span>
              <span v-if="examHasScore(e)" class="exam-score">
                {{ e.score }}<span v-if="e.full_score" class="muted small"> / {{ e.full_score }}</span>
              </span>
              <span v-if="e.grade" class="tag">{{ e.grade }}</span>
              <span v-if="scoreRatePct(e)" class="tag green">{{ scoreRatePct(e) }}</span>
              <span class="small muted">附件 {{ e.files.length }} 份</span>
              <div class="spacer"></div>
              <button class="btn ghost sm" @click="toggleExam(e.id)">
                {{ expandedExam === e.id ? '收起' : '展开' }}
              </button>
              <button class="btn ghost sm" @click="openExam(e)">改</button>
              <button class="btn ghost sm" style="color:var(--danger)" @click="removeExam(e)">删</button>
            </div>

            <!-- 收起时露一句分析的开头，省得每次都要点开才知道写了什么 -->
            <div v-if="expandedExam !== e.id && analysisPreview(e.analysis)"
                 class="small muted" style="margin-top:4px">
              {{ analysisPreview(e.analysis) }}
            </div>

            <div v-if="expandedExam === e.id" style="margin-top:8px">
              <div class="small muted">
                考情分析：写了 {{ analysisStats(e.analysis).filled }} / {{ analysisStats(e.analysis).total }} 段
                <template v-if="e.analysis.template_name"> · 模板「{{ e.analysis.template_name }}」</template>
              </div>
              <template v-if="filledSections(e.analysis).length">
                <div v-for="(s, i) in filledSections(e.analysis)" :key="s.key || i" class="exam-sec">
                  <b class="lb">{{ s.label || '（未命名段）' }}</b>
                  <div class="small" style="white-space:pre-wrap">{{ s.text }}</div>
                </div>
              </template>
              <div v-else class="small muted" style="margin-top:4px">还没写内容</div>

              <!-- 试题与答题卡：图片给缩略图，点开原图；其它类型点名字下载 -->
              <div v-if="e.files.length" style="margin-top:10px">
                <div v-for="f in e.files" :key="f.id" class="exam-file">
                  <a v-if="isImg(f.name)" :href="examRawUrl(f.id, true)" target="_blank" rel="noopener"
                     style="flex:none" title="点开看原图">
                    <img :src="examRawUrl(f.id, true)" alt="" class="exam-thumb">
                  </a>
                  <span v-else class="tag" style="flex:none">{{ f.kind_cn }}</span>
                  <span class="tag" style="flex:none">{{ f.role_label }}</span>
                  <div style="flex:1;min-width:0">
                    <a :href="examRawUrl(f.id)" target="_blank" rel="noopener">{{ f.stored_name || f.name }}</a>
                    <span class="small muted"> · {{ fmtBytes(f.size_bytes) }}</span>
                  </div>
                </div>
              </div>
              <div v-if="e.dir_rel" class="small muted" style="margin-top:6px">归档位置：{{ e.dir_rel }}</div>
            </div>
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

            <!-- 作业存档：这节课的卷子批完归档下来的副本，**挂在这节课下面**。
                 与上面「作业」并列、各自独立 —— 只记了交没交还没批、或者批完存档了
                 但忘了记交没交，两种都常见，谁也不能当谁的前提。
                 ⚠️ 只列 lesson_id 命中这节课的（computed archivesByLesson）；
                    手传的存档不带课时，只出现在「改作业」总览里，这里不重复列。 -->
            <div v-if="(archivesByLesson[item.lesson.id] || []).length" style="margin-top:8px">
              <div class="small muted" style="margin-bottom:4px">
                作业存档（{{ archivesByLesson[item.lesson.id].length }}）
              </div>
              <div v-for="a in archivesByLesson[item.lesson.id]" :key="a.id" class="archive-line">
                <span class="tag green" style="flex:none">批改存档</span>
                <a class="archive-name" @click="openArchive(a)"
                   :title="a.can_preview ? '点开看这次作业的批改情况' : '这类文件不能在线看，点「下载」'">{{ a.name }}</a>
                <span class="small muted">存档于 {{ a.archived_on }} · {{ fmtBytes(a.size_bytes) }}</span>
                <span v-if="a.grader" class="small muted">· 批改人 {{ a.grader }}</span>
                <span v-if="!a.can_preview" class="small muted">· 只能下载</span>
                <div class="spacer"></div>
                <button class="btn ghost sm" @click="openArchive(a)">查看</button>
                <a class="btn ghost sm" :href="archiveRawUrl(a.id)" :download="a.name">下载</a>
              </div>
            </div>

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

    <!-- 预览一份批改存档：图片 / PDF 就地看，其它类型给下载。
         ⚠️ wide 是给 PDF 用的 —— 860px 以下的阅读器里，一页卷子要横着拖才能看完。 -->
    <Modal v-if="archiveOpen" :title="archiveOpen.name || '批改存档'" wide @close="closeArchive">
      <div v-if="archiveBusy === 'open'" class="empty">加载中…</div>
      <template v-else>
        <div class="small" style="margin-bottom:8px">
          <span class="tag blue">{{ archiveOpen.archived_on }}</span>
          <span v-if="archiveOpen.grader" class="tag">批改人 {{ archiveOpen.grader }}</span>
          <span class="tag">{{ fmtBytes(archiveOpen.size_bytes) }}</span>
          <span v-if="archiveOpen.item_count" class="tag">AI 判了 {{ archiveOpen.item_count }} 题</span>
          <span v-for="v in verdictRows" :key="v.key"
                v-show="archiveOpen.verdicts && archiveOpen.verdicts[v.key]"
                class="tag" :class="v.cls">
            {{ v.label }} {{ archiveOpen.verdicts && archiveOpen.verdicts[v.key] }}
          </span>
          <div v-if="archiveOpen.note" class="muted" style="margin-top:4px">
            备注：{{ archiveOpen.note }}
          </div>
        </div>
        <img v-if="archiveIsImg" :src="archiveRawUrl(archiveOpen.id, true)" alt=""
             style="max-width:100%;display:block;margin:0 auto">
        <iframe v-else-if="archiveOpen.kind === 'pdf'"
                :src="archiveRawUrl(archiveOpen.id, true)"
                title="批改后的卷子"
                style="width:100%;height:72vh;border:1px solid var(--border);border-radius:6px"></iframe>
        <div v-else class="empty">
          这类文件（{{ (archiveOpen.kind || '文件').toUpperCase() }}）不能在页面里看
          <div style="margin-top:10px">
            <a class="btn sm primary" :href="archiveRawUrl(archiveOpen.id)"
               :download="archiveOpen.name">下载下来看</a>
          </div>
        </div>
        <div v-if="archiveOpen.exists === false" class="small" style="color:var(--danger);margin-top:8px">
          磁盘上找不到这个文件了（可能被手动删掉了）—— 这条记录可以删掉。
        </div>
      </template>
      <template #foot>
        <a class="btn sm" :href="archiveRawUrl(archiveOpen.id)" :download="archiveOpen.name">下载</a>
        <button class="btn sm ghost" style="color:var(--danger)"
                :disabled="archiveBusy === 'remove'"
                @click="removeArchive(archiveOpen)">删掉这份存档</button>
        <button class="btn sm" @click="closeArchive">关闭</button>
      </template>
    </Modal>

    <FeedbackDialog v-if="feedbackLesson" :lesson="feedbackLesson"
                    @close="feedbackLesson = null" @saved="onSaved" />
    <HomeworkDialog v-if="homeworkLesson" :lesson="homeworkLesson"
                    @close="homeworkLesson = null" @saved="onHomeworkSaved"
                    @grade="onGrade" />
    <SupplementDialog v-if="supplementLesson" :lesson="supplementLesson"
                      @close="supplementLesson = null" @saved="onSupplementSaved" />
    <PrepDialog v-if="prepLesson" :lesson="prepLesson"
                @close="prepLesson = null" @saved="onPrepSaved" />
    <ExamDialog v-if="examDialog" :exam="examDialog.exam" :student="stu"
                :folder="folder ? folder.folder : ''" :type-options="examTypes"
                @close="examDialog = null" @saved="onExamSaved" />
    <PlanDialog v-if="planDialog" :plan="planDialog.plan" :student="stu"
                :folder="folder ? folder.folder : ''" :start-edit="planDialog.startEdit"
                @close="planDialog = null" @saved="onPlanSaved" />
  </div>`,
};
