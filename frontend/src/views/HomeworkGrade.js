// 作业批改台 —— 「AI 判对错，老师核对」的那一屏（第 8 个视图，独立路由 /grade/:id）。
//
// 为什么是视图而不是弹窗：Modal 宽版上限 860px，放不下「左页图 + 右逐题卡」；
// 而且批改要能刷新恢复、能深链（见 docs/AI批改作业-实现计划.md §6）。
//
// 几条硬约束（踩过的坑，改之前先读）：
//
// ① **自定义组件不能写成自闭合 `<GradeMarkLayer />`**。这个项目零构建、没有 .vue 文件，
//    这样写会被当成开标签把后面的兄弟节点一起吞掉，整页白屏。必须成对写。
//
// ② **渲染容器是空 div + 函数 ref**，内容由 JS 写 innerHTML（renderRichEl）。
//    KaTeX 的 auto-render 会把容器里的文本节点整个换掉 —— 容器里只要有 {{ }} 插值，
//    预览就永远停在第一版。key 都带题目 id 前缀，免得两道题 id 撞上、函数 ref 挂错元素。
//
// ③ **AI 只判对错，不给分**。这条是 docs/学生作业上传与评分方案.md §6 的边界：
//    5 维主观分只有老师能打；这里判的是「这道题对不对」，有标准答案可核对。
//    所以界面上一句人话常驻：「AI 只判对错，不给分；核对过才算数」。
//
// ④ **轮询只在跑的时候开**。每 2 秒取一次详情，status 离开 queued/running 就停；
//    停下来那一刻提示一次，不每轮都弹。轮询失败不弹错误（下一轮还会来）。
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { gradeApi } from '../api.js';
import { ensureLibs, renderRichEl } from '../mathRender.js';
import { fail, ok, warn } from '../store.js';
import GradeMarkLayer from '../components/GradeMarkLayer.js';

const POLL_MS = 2000;
/** 轮询要继续的两个状态。⚠️ **只用于「停不停轮询」**，别拿它判断按钮禁不禁用（见 `running`）。
 *  queued 必须留在里面：刚点「开始批改」的那一瞬间，服务端还没来得及把状态写成 running。 */
const RUNNING = ['queued', 'running'];

const G_STATUS_LABEL = {
  queued: '未开始',
  running: '批改中',
  reviewing: '待核对',
  done: '已确认',
  partial: '部分失败',
  failed: '失败',
};
const G_STATUS_TAG = {
  queued: '',
  running: 'blue',
  reviewing: 'orange',
  done: 'green',
  partial: 'red',
  failed: 'red',
};

/** 判定。顺序就是老师在卷面上核对的顺序：对 → 部分对 → 错 → 空白 → 判不了。 */
const VERDICTS = [
  { value: 'correct', label: '对', cls: 'ok' },
  { value: 'partial', label: '部分对', cls: 'part' },
  { value: 'wrong', label: '错', cls: 'bad' },
  { value: 'blank', label: '空白', cls: 'blank' },
  { value: 'ungradable', label: '判不了', cls: 'na' },
];
const VERDICT_LABEL = VERDICTS.reduce((m, v) => { m[v.value] = v.label; return m; }, {});
const CONF_LABEL = { high: '高', medium: '中', low: '低' };

/** 画笔颜色。红笔是老师批改的默认色；蓝绿黑各有各的习惯，都给上。 */
const INK_COLORS = [
  { v: '#dc2626', n: '红' },
  { v: '#2563eb', n: '蓝' },
  { v: '#16a34a', n: '绿' },
  { v: '#111827', n: '黑' },
];
const FLAG_LABEL = {
  transcription_conflict: '识别有冲突',
  insufficient_evidence: '证据不足',
  figure_guess: '图题待确认',
  low_confidence: '识别把握低',
  region_empty: '框里没读到题',
  region_multi: '框画大了（读到多道题）',
};

const q = (s) => String(s === undefined || s === null ? '' : s).trim();

export default {
  name: 'HomeworkGrade',
  components: { GradeMarkLayer },
  setup() {
    const route = useRoute();
    const gid = route.params.id;

    const detail = ref(null);
    const loading = ref(true);
    const loadError = ref('');
    const busy = ref('');                 // 正在提交的那一个操作（按钮置灰用，不锁整页）
    const pageNo = ref(1);
    const pageBroken = ref(false);
    const selectedItemId = ref(null);
    const polling = ref(false);

    /* 手写批改：老师直接在卷面上画。三种手段都是「往 marks 里加一条」，
     * 唯一区别是 mode —— 组件只管采集，落库/删除都在这一层（失败要能提示）。 */
    const tool = ref('view');          // view | ink | erase | text | check | cross | region
    const inkColor = ref('#dc2626');
    const textDraft = ref(null);       // {x, y, text}：点完位置弹出来的输入框
    const exporting = ref('');         // 正在导出的格式（按钮置灰）
    const layerEl = ref(null);         // 标注层组件：拖动提交失败时要调它回滚本地位移

    // 「识别有误」展开的那一题 + 草稿
    const editId = ref('');
    const draft = ref('');

    // 「存到题库」展开的那一题 + 表单
    const bankOpenId = ref('');
    const bankQuestionId = ref('');
    const bankTags = ref('');

    /* ---------------- 渲染容器（唯一出处 src/mathRender.js） ---------------- */
    const richEls = new Map();            // 'stem-<id>' / 'ans-<id>' / 'key-<id>' -> 容器
    const painted = new Map();            // key -> 上次画进去的文本，没变就不重画
    function setRich(el, key) {
      if (el) richEls.set(key, el);
      else richEls.delete(key);
    }
    function paintKey(key, text) {
      const el = richEls.get(key);
      if (!el) return;
      const t = text || '';
      // 轮询每 2 秒来一次；每次重画 KaTeX 会闪一下，所以只在内容真的变了才重画
      if (painted.get(key) === t) return;
      renderRichEl(el, t);
      painted.set(key, t);
    }
    async function paintAll() {
      try { await ensureLibs(); } catch (e) { /* 库挂了 renderRichEl 自己退回原文，别让整页白掉 */ }
      await nextTick();                   // 等容器真的挂上去
      for (const it of items.value) {
        paintKey('stem-' + it.id, it.stem);
        paintKey('ans-' + it.id, it.student_answer);
        paintKey('key-' + it.id, it.correct_answer);
      }
    }

    /* ---------------- 数据 ---------------- */
    const items = computed(() => (detail.value && detail.value.items) || []);
    const marks = computed(() => (detail.value && detail.value.marks) || []);
    const pageMarks = computed(() => marks.value.filter((m) => Number(m.page_no || 1) === pageNo.value));
    /** 老师自己放的（笔迹 / 文字 / √×）。撤销、清空只动这些 —— AI 猜的框一个字不碰。 */
    const teacherMarks = computed(() => pageMarks.value.filter((m) => m.created_by === 'teacher'));

    /* ---------------- 一题一区域（框题） ----------------
     * 有框的页按框逐题批，没框的页整页一次判 —— 两条口径并存，所以这里必须
     * 分清「服务器上已经存了的框」和「正在拖但还没保存的草稿」。
     *
     * ⚠️ 进「框题」模式才把草稿从服务器同步一份：平时不该有两份状态，
     *    否则轮询一刷新就把老师刚拖的框冲掉（或者反过来，草稿盖掉别人的改动）。 */
    const regions = computed(() => (detail.value && detail.value.regions) || []);
    const pageRegions = computed(
      () => regions.value.filter((r) => Number(r.page_no || 1) === pageNo.value),
    );
    const editRegions = ref([]);        // 当前页的本地草稿（只在框题模式有意义）
    const regionsDirty = ref(false);
    const editingRegions = computed(() => tool.value === 'region');

    function syncRegions() {
      editRegions.value = pageRegions.value.map((r) => ({ ...r }));
      regionsDirty.value = false;
    }

    /** 画完一个框（标注层发上来的是 [x0,y0,x1,y1]）。只在本地，点「保存框」才落库。 */
    function addRegionBox(box) {
      const n = editRegions.value.length + 1;
      editRegions.value = editRegions.value.concat([
        { id: null, q_no: '', box, label: String(n) },
      ]);
      regionsDirty.value = true;
    }

    /** 点图上的框 / 列表里的 × → 只从草稿里去掉（保存时才真的删）。 */
    function dropRegion(r) {
      editRegions.value = editRegions.value.filter((x) => x !== r);
      regionsDirty.value = true;
      relabelRegions();
    }

    /** 序号跟着当前位置走：老师删掉中间一个，后面的框号要顺上来，否则 1、3、4 看着像丢了一个。 */
    function relabelRegions() {
      editRegions.value = editRegions.value.map((x, i) => ({
        ...x, label: String(x.q_no || '').trim() || String(i + 1),
      }));
    }

    function setRegionQNo(r, v) {
      r.q_no = v;
      regionsDirty.value = true;
      relabelRegions();
    }

    /** 清空本页草稿（**只是草稿**，点保存才真的从库里删）。 */
    function clearRegions() {
      if (!editRegions.value.length) return;
      editRegions.value = [];
      regionsDirty.value = true;
    }

    async function saveRegions() {
      busy.value = 'regions';
      try {
        const res = await gradeApi.putRegions(gid, {
          page_no: pageNo.value,
          regions: editRegions.value.map((r) => ({
            q_no: String(r.q_no || '').trim(), box: r.box,
          })),
        });
        detail.value.regions = res.regions;
        syncRegions();
        ok(editRegions.value.length
          ? `已保存 ${editRegions.value.length} 个框`
          : '已清空这一页的框（这一页退回整页批）');
        if (items.value.length) {
          warn('框改了要重新批改一次才生效 —— 点右边「重新批改」');
        }
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    /** 沿用上一份的框：一套卷只画一次。**先取同课时的列表再拷**，
     *  不猜 id —— 猜错了会把别人的卷面当成模板。 */
    async function copyRegions() {
      busy.value = 'copy';
      try {
        const rows = await gradeApi.list(detail.value.lesson_id);
        const others = (rows || []).filter((r) => String(r.id) !== String(gid));
        if (!others.length) {
          warn('这节课只有这一份，没有可沿用的框 —— 先画一次');
          return;
        }
        const res = await gradeApi.copyRegions(gid, others[0].id);
        detail.value.regions = res.regions;
        syncRegions();
        ok(`已沿用另一份的 ${res.regions.length} 个框 —— 扫一眼，框偏了就重画`);
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    /** 页数：优先数提交件的 pages（total 就是页数，不是题数）；没有就按题上的最大页码兜底。 */
    const pageCount = computed(() => {
      const filed = ((detail.value && detail.value.files) || []).filter((f) => Number(f.pages) > 0);
      const subs = filed.filter((f) => !f.role || f.role === 'submission');
      const use = subs.length ? subs : filed;
      const fromFiles = use.reduce((n, f) => n + Number(f.pages || 0), 0);
      const fromItems = items.value.reduce((n, it) => Math.max(n, Number(it.page_no) || 0), 0);
      return Math.max(fromFiles, fromItems, 1);
    });

    /* 「真的在跑」≠「服务端还没给结果」。
     * queued 有两个来源：①新建之后还没人开批 ②刚点开批、线程还来不及写 running。
     * 以前把 queued 也算「在跑」→ 新建完按钮就是灰的、页面上还写着「排队中」，
     * 老师点不动、只能干等，看起来就像「跑了半天没出来」（2026-10-17 实际踩到）。
     *
     * ⚠️ 这里刻意**不用 polling 当判据**：从「保存并批改」跳进来时状态就是 queued，
     *    一挂载就轮询的话 polling=true → 按钮又变灰，等于把同一个 bug 换个地方复发。
     *    所以只认两件事：服务端说 running，或者**这次会话里老师真的点过开始批改**
     *    （runAsked），而服务端还来不及写 running。 */
    const runAsked = ref(false);
    const running = computed(() => {
      if (!detail.value) return false;
      if (detail.value.status === 'running') return true;
      return runAsked.value && detail.value.status === 'queued';
    });
    const statusLabel = computed(() => (detail.value
      ? (G_STATUS_LABEL[detail.value.status] || detail.value.status) : ''));
    const statusTag = computed(() => (detail.value ? (G_STATUS_TAG[detail.value.status] || '') : ''));
    const runLabel = computed(() => {
      if (running.value) return '批改中…';
      if (!detail.value) return '开始批改';
      return Number(detail.value.done || 0) > 0 ? '重新批改' : '开始批改';
    });
    const pageUrl = computed(() => gradeApi.pageUrl(gid, pageNo.value));
    const tallyText = computed(() => {
      const c = { correct: 0, partial: 0, wrong: 0, blank: 0, ungradable: 0 };
      for (const it of items.value) {
        const v = it.verdict || 'ungradable';
        c[v] = (c[v] || 0) + 1;
      }
      return `对 ${c.correct} · 部分对 ${c.partial} · 错 ${c.wrong} · 空白 ${c.blank} · 判不了 ${c.ungradable}`;
    });

    /* ---------------- 轮询 ---------------- */
    let timer = 0;
    /* ---------------- 判定依据（A1：答案由老师提供，不是模型自己编） ----------------
     * 入口在「交作业」那一步 —— 老师点「保存并批改」时手上还没有填答案的地方，
     * 是跳到这一页、看过页图之后才决定「这次按我的答案判」。所以这里必须能补。
     * ⚠️ 只有还没批出结果时能改：批过再换依据，那批判定就是按旧标准批出来的。 */
    const setupMode = ref('ai');
    const setupAnswer = ref('');
    const setupRubric = ref('');
    const setupTouched = ref(false);      // 用户动过没动过 —— 没动过就跟着服务端走
    const setupBusy = ref(false);
    const setupEditable = computed(() => !!detail.value
      && ['queued', 'failed'].includes(detail.value.status) && !detail.value.done);

    function initSetup() {
      if (!detail.value) return;
      setupMode.value = detail.value.answer_mode || 'ai';
      setupAnswer.value = detail.value.answer_text || '';
      setupRubric.value = detail.value.rubric_text || '';
      setupTouched.value = false;
    }
    function touchSetup() { setupTouched.value = true; }

    /** 保存判定依据。返回 false = 别继续（比如老师填了细则但保存失败，不该照样开批）。 */
    async function saveSetup() {
      if (!detail.value || !setupEditable.value || !setupTouched.value) return true;
      setupBusy.value = true;
      try {
        detail.value = await gradeApi.patchSetup(gid, {
          answer_mode: setupMode.value,
          answer_text: setupAnswer.value,
          rubric_text: setupRubric.value,
        });
        initSetup();
        ok('判定依据改好了');
        return true;
      } catch (e) {
        fail(e.message);
        return false;
      } finally {
        setupBusy.value = false;
      }
    }

    function stopPolling() {
      if (timer) { clearInterval(timer); timer = 0; }
      polling.value = false;
    }
    function startPolling() {
      stopPolling();
      // 只跟 running 走：新建完的 queued 不轮询（没人点过开始，轮询也没东西可看）
      if (!running.value) return;
      polling.value = true;
      timer = setInterval(pollOnce, POLL_MS);
    }
    async function pollOnce() {
      await load(true);
      if (!detail.value || !RUNNING.includes(detail.value.status)) {
        runAsked.value = false;          // 跑完/失败了，「点过」这个标记使命结束
        stopPolling();
        // 只在停下来的那一刻说一句（每轮都弹等于没弹）
        if (detail.value && detail.value.status === 'reviewing') ok('批完了，逐题核对一下');
        else if (detail.value && ['partial', 'failed'].includes(detail.value.status)) {
          warn(detail.value.error || '有一部分没批出来，可以单题重批');
        }
      }
    }

    async function load(silent = false) {
      if (!silent) loading.value = true;
      try {
        detail.value = await gradeApi.get(gid);
        loadError.value = '';
        // 页数可能比当前页少（重批换了文件、或上一次记的页码越界）
        if (pageNo.value > pageCount.value) pageNo.value = pageCount.value;
        if (pageNo.value < 1) pageNo.value = 1;
        if (!setupTouched.value) initSetup();     // 没动过才跟服务端走，别把老师刚敲的字冲掉
        /* ⚠️ **先关 loading，再画**。题目区在模板里是 `v-else`（`v-if="loading"` 的反面），
         * loading 还是 true 的时候那几个 `.g-rich` 容器**根本不在 DOM 里**，`setRich` 没被调过
         * → `paintKey` 全部命中 `if (!el) return` → 题干 / 学生作答 / 参考答案**一片空白**。
         * 2026-10-19 用 jsdom 真挂一遍才发现：容器有 3 个、内容 0 个。
         * 为什么平时没露馅：轮询与「跑完自动刷新」都走 load(true)（silent，不动 loading），
         * 容器早就在了 —— **只有第一次打开/刷新这一页**才白。 */
        if (!silent) loading.value = false;
        await paintAll();
      } catch (e) {
        if (!silent) { loadError.value = e.message; fail(e.message); }
      } finally {
        if (!silent) loading.value = false;      // 兜底：上面那行之前就抛了也要放出来
      }
    }

    /** 改完一条之后的对账：不自己在本地拼状态，直接跟后端再取一次。
     *  批改任务是一台会动的机器（重跑、重批都会改 done/total），本地猜一定会猜错。 */
    async function settle() {
      await load(true);
      startPolling();
    }

    /* ---------------- 操作 ---------------- */
    /** ⚠️ 全页**唯一**会真正开批的地方。
     *  「保存并批改」只建任务 + 跳转到这里，**不批** —— 老师要先看过页图、选好判定依据，
     *  再自己点这一下。（2026-10-17 用户明确要求：「点保存并批改只做跳转，我点开始批改才开始」） */
    async function run() {
      // 老师可能刚在「判定依据」里改了答案、没点保存就点了开始批改 —— 别让他白改
      if (!(await saveSetup())) return;
      busy.value = 'run';
      runAsked.value = true;             // 服务端写 running 之前，先让按钮进入「批改中」
      try {
        const d = await gradeApi.run(gid);
        if (d && d.id) { detail.value = d; await paintAll(); } else { await load(true); }
        startPolling();
        if (!running.value) runAsked.value = false;   // 已经批完了，不用留着这个标记
        ok(running.value ? '开始批改了，跑完会自动刷新' : '这一份已经批完');
      } catch (e) {
        runAsked.value = false;          // 没开起来，按钮要恢复可点
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    /* ---------------- 手写批改（画 / 擦 / 文字框 / 快放 √×） ----------------
     * 全部落到同一张 homework_grade_marks 表，kind 区分形状。
     * ⚠️ 坐标 0~1000、按页归一化，组件算好再交上来 —— 这里**不做任何坐标换算**，
     *    两边各算一次，迟早会出现「屏幕上是准的、导出的 PDF 跑位」。 */

    function setTool(t) {
      const next = tool.value === t ? 'view' : t;   // 再点一次退出，不用另找「选择」按钮
      // 离开「框题」时没保存的框不能悄悄丢 —— 老师画了五个框最怕这个
      if (tool.value === 'region' && next !== 'region' && regionsDirty.value
          && !window.confirm('这一页的框还没保存，切换就丢了。要放弃这些框吗？')) {
        return;
      }
      tool.value = next;
      if (tool.value !== 'text') textDraft.value = null;
      if (tool.value === 'region') syncRegions();
    }

    async function addMark(payload) {
      try {
        await gradeApi.addMark(gid, Object.assign({ page_no: pageNo.value }, payload));
        await settle();
      } catch (e) {
        fail(e.message);
      }
    }

    /** 松手时一次交上来一批 id。**串行删**：SQLite 单写者，并发删会撞锁。 */
    async function eraseMarks(ids) {
      if (!ids || !ids.length) return;
      try {
        for (const mid of ids) await gradeApi.removeMark(mid);
        await settle();
      } catch (e) {
        fail(e.message);
      }
    }

    /** 拖动标注改位置（文字框 / √× / 框 / 圆圈 / 箭头）。
     *  后端只认 geo 四个数，所以这里什么都不用算 —— 子组件已经把平移算好了，
     *  再算一遍就是「屏幕上是准的、导出时跑位」的老毛病。
     *  ⚠️ 失败要让子组件把本地位移退回去，不然界面停在拖到的位置、库里还是原位。 */
    async function moveMark(payload) {
      const mark = payload && payload.mark;
      const geo = payload && payload.geo;
      if (!mark || !mark.id || !geo || geo.length < 4) return;
      busy.value = 'mv-' + mark.id;
      try {
        await gradeApi.patchMark(mark.id, { geo });
        await settle();               // 重取详情：偏移由子组件按 base 快照自动退场
      } catch (e) {
        if (layerEl.value && layerEl.value.clearOffsets) layerEl.value.clearOffsets();
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    function onTextAt({ x, y }) {
      textDraft.value = { x, y, text: '' };
    }

    async function saveText() {
      const d = textDraft.value;
      if (!d) return;
      const text = (d.text || '').trim();
      textDraft.value = null;
      if (!text) return;                             // 空着就当没点
      await addMark({ kind: 'text', geo: [d.x, d.y, d.x, d.y], text, color: inkColor.value });
    }

    /** 撤销：删掉本页最后一条自己放的。笔迹一笔一条，所以撤销 = 退掉一笔。 */
    async function undoLast() {
      const mine = teacherMarks.value;
      if (!mine.length) return;
      const last = mine.reduce((a, b) => (Number(a.id) > Number(b.id) ? a : b));
      await eraseMarks([last.id]);
    }

    async function clearPageMarks() {
      const mine = teacherMarks.value;
      if (!mine.length) return;
      if (!window.confirm(`把这一页你画的 ${mine.length} 处批注全删掉？（AI 的框不动）`)) return;
      await eraseMarks(mine.map((m) => m.id));
    }

    /** 导出带批改痕迹的副本。原件不动 —— 后端画在别的文件上。 */
    function exportFile(fmt) {
      if (!detail.value || !detail.value.total) {
        warn('还没有页图 —— 先点「开始批改」，页图渲染出来才能导出');
        return;
      }
      exporting.value = fmt;
      const a = document.createElement('a');
      a.href = gradeApi.exportUrl(gid, fmt, pageNo.value);
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => { exporting.value = ''; }, 1200);
    }

    async function setVerdict(item, v) {
      if (!v || item.verdict === v) return;
      const before = item.verdict;
      item.verdict = v;                   // 先落到界面上：老师点下去的判定不该等一个来回
      busy.value = 'v-' + item.id;
      try {
        await gradeApi.patchItem(item.id, { verdict: v });
        await settle();
      } catch (e) {
        item.verdict = before;
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function toggleReviewed(item, checked) {
      busy.value = 'rv-' + item.id;
      try {
        await gradeApi.patchItem(item.id, { status: checked ? 'approved' : 'pending' });
        item.status = checked ? 'approved' : 'pending';
        await settle();                   // 核对进度决定「确认」能不能点，跟后端对一次
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    function openFix(item) {
      editId.value = item.id;
      draft.value = item.student_answer || '';
    }
    function closeFix() {
      editId.value = '';
      draft.value = '';
    }
    /** 改识别错的字。后端会把这一题的判定作废重跑（旧的 evidence 引用已经不存在了），
     *  所以保存后要重新开始轮询 —— 这一题会从 pending 回到 running。 */
    async function saveAnswer(item) {
      const text = draft.value;
      if (text === (item.student_answer || '')) { closeFix(); return; }
      busy.value = 'ans-' + item.id;
      try {
        await gradeApi.patchItem(item.id, { student_answer: text });
        closeFix();
        await settle();
        ok('已保存，这一题会重新判');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function rerunItem(item) {
      busy.value = 'rr-' + item.id;
      try {
        await gradeApi.rerunItem(item.id);
        await settle();
        ok('已提交，只重批这一题');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    function openBank(item) {
      bankOpenId.value = item.id;
      bankQuestionId.value = item.question_id || '';
      bankTags.value = '';
    }
    function closeBank() {
      bankOpenId.value = '';
      bankQuestionId.value = '';
      bankTags.value = '';
    }
    /** 错题回存题库：**不自动建题**（B1），题目 id 由老师指定，标签是追加的。 */
    async function saveToBank(item) {
      const qid = q(bankQuestionId.value);
      if (!qid) { warn('填一下题库里的题目 id（不知道就先去题库找）'); return; }
      const tags = bankTags.value.split(/[,，、\s]+/).map((t) => t.trim()).filter(Boolean);
      busy.value = 'bank-' + item.id;
      try {
        await gradeApi.toBank(item.id, { question_id: qid, tags });
        closeBank();
        await settle();
        ok('已存到题库');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function confirm() {
      if (!detail.value || detail.value.status !== 'reviewing') return;
      const note = window.prompt('确认这份批改？（确认后才进统计口径）\n可以写一句备注，留空也行：', '');
      if (note === null) return;          // 取消就是没确认
      busy.value = 'confirm';
      try {
        const d = await gradeApi.confirm(gid, note);
        if (d && d.id) { detail.value = d; await paintAll(); } else { await load(true); }
        ok('已确认，这份批改算数了');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    /* ---------------- 页与题之间的联动 ---------------- */
    const cardEls = new Map();
    function setCard(el, id) {
      if (el) cardEls.set(String(id), el);
      else cardEls.delete(String(id));
    }
    function goPage(n) {
      const target = Math.min(Math.max(1, Number(n) || 1), pageCount.value);
      if (target === pageNo.value) return;
      if (tool.value === 'region' && regionsDirty.value
          && !window.confirm('这一页的框还没保存，翻页就丢了。要放弃吗？')) {
        return;
      }
      pageNo.value = target;
      pageBroken.value = false;
      if (tool.value === 'region') syncRegions();
    }
    const prevPage = () => goPage(pageNo.value - 1);
    const nextPage = () => goPage(pageNo.value + 1);

    /** 点图上的标注 → 选中那一题 → 右侧滚到它的卡片。
     *  不挂题的整页批注（比如页脚一句「书写要工整」）点了不跳，它本来就不属于某道题。 */
    async function onPick(mark) {
      const id = mark && mark.item_id;
      if (id === undefined || id === null) return;
      selectedItemId.value = id;
      await nextTick();
      const el = cardEls.get(String(id));
      if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    /* ---------------- 小工具（模板里直接调） ---------------- */
    const verdictLabel = (v) => VERDICT_LABEL[v] || '判不了';
    const confLabel = (c) => CONF_LABEL[c] || '';
    const flagLabel = (f) => FLAG_LABEL[f] || '需留意';
    /** ungradable 必须说清为什么 —— 只给一个灰点，老师会当成「AI 没干活」。 */
    const naReason = (item) => item.error_reason || '识别不清 / 图题 / 没作答';

    onMounted(async () => {
      await load();
      startPolling();
    });
    onBeforeUnmount(() => {
      stopPolling();
      richEls.clear();
      painted.clear();
      cardEls.clear();
    });

    return {
      detail, loading, loadError, busy, pageNo, pageBroken, selectedItemId, polling,
      editId, draft, bankOpenId, bankQuestionId, bankTags,
      items, marks, pageMarks, pageCount, pageUrl, running, statusLabel, statusTag,
      runLabel, tallyText, VERDICTS,
      load, run, setVerdict, toggleReviewed, openFix, closeFix, saveAnswer,
      rerunItem, openBank, closeBank, saveToBank, confirm,
      setRich, setCard, goPage, prevPage, nextPage, onPick,
      verdictLabel, confLabel, flagLabel, naReason,
      setupMode, setupAnswer, setupRubric, setupTouched, setupBusy, setupEditable,
      touchSetup, saveSetup,
      // 手写批改
      tool, inkColor, textDraft, exporting, INK_COLORS, teacherMarks, layerEl,
      setTool, addMark, eraseMarks, onTextAt, saveText, moveMark,
      undoLast, clearPageMarks, exportFile,
      // 一题一区域（框题）：有框按框批，没框仍整页批
      pageRegions, editRegions, regionsDirty, editingRegions,
      addRegionBox, dropRegion, setRegionQNo, saveRegions, copyRegions, syncRegions,
      clearRegions,
    };
  },
  template: `
  <div>
    <div class="page-head">
      <h1>作业批改</h1>
      <span class="sub" v-if="detail">课时 #{{ detail.lesson_id }}</span>
      <div class="spacer"></div>
      <router-link v-if="detail" :to="'/students/' + detail.student_id" class="btn">返回学生</router-link>
      <router-link v-else to="/students" class="btn">返回学生</router-link>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="loadError" class="empty">
      这份批改打不开：{{ loadError }}
      <div class="small" style="margin-top:6px">可能已经删掉了，回学生页重新点「保存并批改」</div>
    </div>

    <template v-else-if="detail">
      <!-- ==================== 顶栏：整份进度 + 状态 + 两个大按钮 ==================== -->
      <div class="card grade-top">
        <div class="grade-top-row">
          <span class="grade-status" :class="'s-' + detail.status">{{ statusLabel }}</span>
          <span class="small muted">进度 {{ detail.done }} / {{ detail.total }} 页</span>
          <span v-if="detail.failed" class="small" style="color:var(--danger)">
            失败 {{ detail.failed }} 页
          </span>
          <span v-if="polling" class="small muted">· 自动刷新中</span>
          <div class="spacer"></div>
          <button class="btn sm" :disabled="running || busy === 'run'" @click="run">{{ runLabel }}</button>
          <!-- 只有「待核对」才能确认：没批完就确认，等于确认了一份半成品 -->
          <button class="btn sm primary" :disabled="detail.status !== 'reviewing' || busy === 'confirm'"
                  :title="detail.status === 'reviewing' ? '确认后这份批改才会进统计' : '批完（待核对）才能确认'"
                  @click="confirm">
            {{ busy === 'confirm' ? '确认中…' : '确认' }}
          </button>
          <!-- 导出的是**副本**：老师画的笔迹烧在新文件上，学生原件一个像素都不动 -->
          <button class="btn sm" :disabled="!!exporting || !detail.total"
                  :title="detail.total ? '导出整份（带批改痕迹）' : '页图还没渲染，先点开始批改'"
                  @click="exportFile('pdf')">
            {{ exporting === 'pdf' ? '导出中…' : '导出 PDF' }}
          </button>
          <button class="btn sm ghost" :disabled="!!exporting || !detail.total"
                  title="只导出当前这一页（PNG）" @click="exportFile('png')">
            {{ exporting === 'png' ? '导出中…' : '本页图' }}
          </button>
        </div>
        <div class="grade-tip">AI 只判对错，不给分；核对过才算数。</div>
        <!-- 新建完任务是「未开始」：**不会自动批**，不点按钮永远不会跑 → 必须说出来，
             否则页面写着「未开始」也能被当成「在排队」（2026-10-17 实际踩到）。
             ⚠️ 要排除 running：刚点完开始批改的那一瞬间服务端也还是 queued（还没写 running），
             少了 !running 这句，正在跑的时候会显示「还没开始批改」。 -->
        <div v-if="!running && detail.status === 'queued' && !detail.done" class="grade-hint">
          还没开始批改 —— 点右边「开始批改」。
        </div>
        <div v-else-if="running" class="grade-hint">
          一页通常 1~2 分钟（题目多、字迹潦草会更久）。可以先去干别的，这一页跑完会自动刷新。
        </div>
        <div v-if="detail.error" class="small" style="color:var(--danger); margin-top:4px">{{ detail.error }}</div>
      </div>

      <!-- ==================== 判定依据：AI 自解 / 老师的答案 / 老师的评分细则 ==================== -->
      <div class="card grade-setup">
        <div class="grade-setup-head">
          <b>判定依据</b>
          <span class="small muted" v-if="!setupEditable">已经开始批了 —— 要换依据请点「重新批改」（会重跑一遍）</span>
          <span class="small muted" v-else-if="!setupTouched">现在用的就是这份依据</span>
          <div class="spacer"></div>
          <button v-if="setupEditable" class="btn sm" :disabled="setupBusy || !setupTouched"
                  @click="saveSetup">{{ setupBusy ? '保存中…' : '保存依据' }}</button>
        </div>
        <div class="grade-setup-modes">
          <label :class="{ on: setupMode === 'ai' }">
            <input type="radio" value="ai" v-model="setupMode" :disabled="!setupEditable" @change="touchSetup">
            AI 自己解
          </label>
          <label :class="{ on: setupMode === 'key' }">
            <input type="radio" value="key" v-model="setupMode" :disabled="!setupEditable" @change="touchSetup">
            用我给的答案
          </label>
          <label :class="{ on: setupMode === 'rubric' }">
            <input type="radio" value="rubric" v-model="setupMode" :disabled="!setupEditable" @change="touchSetup">
            用我的评分细则
          </label>
        </div>
        <div v-if="setupMode === 'key'" class="field" style="margin-top:8px">
          <label class="small">答案 <span class="muted">（按题号写，一题一行）</span></label>
          <textarea rows="4" v-model="setupAnswer" :disabled="!setupEditable" @input="touchSetup"
                    placeholder="3. x=2 或 x=-2&#10;4. 证明：因为 AB=AC，所以 ∠B=∠C…"></textarea>
        </div>
        <div v-if="setupMode === 'rubric'" class="field" style="margin-top:8px">
          <label class="small">评分细则 <span class="muted">（每步几分、什么情况扣分）</span></label>
          <textarea rows="5" v-model="setupRubric" :disabled="!setupEditable" @input="touchSetup"
                    placeholder="第 3 题（10 分）：设未知数 2 分；列方程 3 分；解方程 3 分；检验作答 2 分。只写答案不写过程最多给 4 分。"></textarea>
        </div>
        <div class="small muted" v-if="setupMode === 'ai'">
          AI 会自己先解一遍再对照。它解错时整题会判错 —— 核对时留意一下。
        </div>
        <div class="small muted" v-else>
          一期只判对错、不给分；细则里写的分值暂时只用来判断"哪一步算数"。
        </div>
      </div>

      <div class="grade-wrap">
        <!-- ==================== 左：页图 + 标注层 ==================== -->
        <div class="card grade-left">
          <div class="grade-pagebar">
            <button class="btn ghost sm" :disabled="pageNo <= 1" @click="prevPage">← 上一页</button>
            <span class="small">第 {{ pageNo }} / {{ pageCount }} 页</span>
            <button class="btn ghost sm" :disabled="pageNo >= pageCount" @click="nextPage">下一页 →</button>
            <div class="spacer"></div>
            <span class="small muted" v-if="pageMarks.length">这一页 {{ pageMarks.length }} 处标注</span>
            <span class="small muted" v-else>这一页没有标注</span>
          </div>

          <!-- 手写批改工具条。笔迹 / 文字 / √× 都落进 homework_grade_marks，
               和 AI 给的框同源 —— 所以「擦」可以连 AI 的框一起擦，撤销只退老师自己的。 -->
          <div class="grade-tools">
            <button class="g-tool" :class="{ on: tool === 'view' }" @click="tool = 'view'">选择</button>
            <button class="g-tool" :class="{ on: tool === 'ink' }" @click="setTool('ink')">画笔</button>
            <button class="g-tool" :class="{ on: tool === 'erase' }" @click="setTool('erase')">橡皮</button>
            <button class="g-tool" :class="{ on: tool === 'text' }" @click="setTool('text')">文字框</button>
            <span class="g-sep"></span>
            <button class="g-tool g-mark" :class="{ on: tool === 'check' }" title="点一下卷面就放一个 √"
                    @click="setTool('check')">√</button>
            <button class="g-tool g-mark" :class="{ on: tool === 'cross' }" title="点一下卷面就放一个 ×"
                    @click="setTool('cross')">×</button>
            <span class="g-sep"></span>
            <button class="g-tool" :class="{ on: tool === 'region' }" title="框出每道题的作答范围：有框就一题一裁一判，比整页判准得多"
                    @click="setTool('region')">框题</button>
            <button v-for="c in INK_COLORS" :key="c.v" class="g-color" :class="{ on: inkColor === c.v }"
                    :style="{ background: c.v }" :title="c.n" @click="inkColor = c.v"></button>
            <div class="spacer"></div>
            <button class="btn ghost sm" :disabled="!teacherMarks.length" @click="undoLast">撤销一笔</button>
            <button class="btn ghost sm" :disabled="!teacherMarks.length" @click="clearPageMarks">清本页</button>
          </div>
          <div v-if="tool !== 'view'" class="grade-toolhint small muted">
            {{ tool === 'ink' ? '按住左键在卷面上写 / 画，松手就存。'
              : tool === 'erase' ? '按住左键划过要擦的笔迹（AI 的框也能擦）。'
              : tool === 'text' ? '在卷面上点一下，弹框里写批注，Ctrl+Enter 放上去。'
              : tool === 'region' ? '按住拖出一个框，把一道题（题干 + 作答）整个框进去。框宁大勿小 —— 切掉笔画就再也读不回来了。'
              : '在要判的地方点一下，就放一个 ' + (tool === 'check' ? '√' : '×') + '。' }}
          </div>

          <!-- ============ 框题面板：只在「框题」模式下出现 ============ -->
          <div v-if="tool === 'region'" class="grade-regions">
            <div class="gr-head">
              <b>第 {{ pageNo }} 页的框</b>
              <span class="small muted">{{ editRegions.length }} 个</span>
              <span v-if="regionsDirty" class="small" style="color:var(--warning)">未保存</span>
              <div class="spacer"></div>
              <button class="btn ghost sm" :disabled="busy === 'copy'" @click="copyRegions">
                {{ busy === 'copy' ? '沿用中…' : '沿用上一份' }}
              </button>
              <button class="btn ghost sm" :disabled="!editRegions.length" @click="clearRegions">全清</button>
              <button class="btn sm" :disabled="busy === 'regions'" @click="saveRegions">
                {{ busy === 'regions' ? '保存中…' : '保存框' }}
              </button>
            </div>
            <div v-if="!editRegions.length" class="small muted" style="padding:6px 0">
              这一页还没画框 —— 拖一个框出来。有框的页<b>一框一次</b>判，
              没框的页仍然整页判一次。切到「框题」再看这页就会带上框。
            </div>
            <div v-else class="gr-list">
              <div v-for="(r, i) in editRegions" :key="'r-' + i" class="gr-row">
                <span class="gr-no">{{ i + 1 }}</span>
                <label class="small">题号</label>
                <input class="gr-q" :value="r.q_no" placeholder="留空就按顺序"
                       @input="setRegionQNo(r, $event.target.value)">
                <span class="small muted">框内批出来的题都记到这个题号上</span>
                <div class="spacer"></div>
                <button class="btn ghost sm" @click="dropRegion(r)">删</button>
              </div>
            </div>
            <div class="small muted" style="margin-top:6px">
              题号由框绑定 —— 学生把「第 3 题」写成「第 8 题」也不会判错归属。
              <b>画完一定要点「保存框」</b>，然后重新批改。
            </div>
          </div>

          <div class="grade-page">
            <img class="grade-page-img" :src="pageUrl" loading="lazy" alt="作业原图" draggable="false"
                 @load="pageBroken = false" @error="pageBroken = true">
            <!-- ⚠️ 成对写，别写成自闭合：零构建的模板里自闭合会把后面的节点吞掉 -->
            <GradeMarkLayer ref="layerEl" :marks="pageMarks" :selected-item-id="selectedItemId"
                            :mode="tool" :color="inkColor"
                            :regions="tool === 'region' ? editRegions : pageRegions"
                            @pick="onPick" @create="addMark" @erase="eraseMarks"
                            @text-at="onTextAt" @stamp="addMark" @move="moveMark"
                            @region="addRegionBox" @region-remove="dropRegion"></GradeMarkLayer>
            <div v-if="pageBroken" class="grade-page-miss small">
              这一页的图还没生成出来（点「开始批改」之后才有），或者这一页渲染失败了
            </div>
          </div>
          <div class="small muted" style="margin-top:8px">
            「选择」模式下：点标注跳到对应题目；<b>按住文本框 / √ × / 方框可以拖动挪位置</b>（笔迹不动）。
            坐标按页归一化（0~1000），换窗口大小也不会跑位。
          </div>
        </div>

        <!-- ==================== 右：逐题卡片 ==================== -->
        <div class="card grade-right">
          <div class="grade-side-head">
            <b>逐题</b>
            <span class="small muted">共 {{ items.length }} 题</span>
            <div class="spacer"></div>
            <span class="small muted">{{ tallyText }}</span>
          </div>

          <div v-if="!items.length" class="empty">
            还没有题目结果
            <div class="small" style="margin-top:6px">点上面的「开始批改」，跑完这里会一题一题列出来</div>
          </div>

          <div v-else class="grade-items">
            <div v-for="item in items" :key="'it-' + item.id" class="g-item"
                 :class="{ sel: selectedItemId === item.id }"
                 :ref="el => setCard(el, item.id)"
                 @click="selectedItemId = item.id">

              <div class="g-head">
                <b class="g-no">{{ item.q_no || ('第 ' + item.seq + ' 题') }}</b>
                <span v-if="item.qtype" class="tag">{{ item.qtype }}</span>
                <span class="g-verdict" :class="'v-' + (item.verdict || 'ungradable')">
                  {{ verdictLabel(item.verdict) }}
                </span>
                <span v-for="f in (item.flags || [])" :key="'f-' + item.id + '-' + f"
                      class="tag orange">{{ flagLabel(f) }}</span>
                <div class="spacer"></div>
                <button class="btn ghost sm" @click.stop="goPage(item.page_no)">第 {{ item.page_no }} 页</button>
              </div>

              <!-- 题干：空容器 + 函数 ref，内容由 renderRichEl 写 innerHTML（不能有插值） -->
              <div class="g-sec">
                <div class="g-lb">题干</div>
                <div class="g-rich" :key="'stem-' + item.id"
                     :ref="el => setRich(el, 'stem-' + item.id)"></div>
              </div>

              <div class="g-sec">
                <div class="g-lb">
                  学生作答
                  <span v-if="item.confidence" class="muted small">识别把握 {{ confLabel(item.confidence) }}</span>
                </div>

                <template v-if="editId === item.id">
                  <textarea v-model="draft" rows="3" @click.stop
                            placeholder="学生到底写了什么，就照着打上去（看不清的字可以留个问号）"></textarea>
                  <div class="small" style="margin-top:4px; color:var(--warning)">
                    改字会重新判这一题 —— 后端会把这一题的判定作废，再跑一遍
                  </div>
                  <div style="margin-top:6px; display:flex; gap:8px">
                    <button class="btn sm primary" :disabled="busy === 'ans-' + item.id"
                            @click.stop="saveAnswer(item)">
                      {{ busy === 'ans-' + item.id ? '保存中…' : '保存并重判' }}
                    </button>
                    <button class="btn sm ghost" @click.stop="closeFix">取消</button>
                  </div>
                </template>
                <template v-else>
                  <div class="g-rich" :key="'ans-' + item.id"
                       :ref="el => setRich(el, 'ans-' + item.id)"></div>
                  <button class="btn ghost sm" style="margin-top:2px" @click.stop="openFix(item)">识别有误</button>
                </template>

                <!-- 判不了必须给原因：只放一个灰点，老师会以为 AI 什么都没做 -->
                <div v-if="item.verdict === 'ungradable'" class="g-ungradable">
                  判不了的原因：{{ naReason(item) }}
                </div>
              </div>

              <div v-if="item.correct_answer" class="g-sec">
                <div class="g-lb">参考答案</div>
                <div class="g-rich g-rich-sm" :key="'key-' + item.id"
                     :ref="el => setRich(el, 'key-' + item.id)"></div>
              </div>

              <div class="g-sec">
                <div class="g-lb">错因 / 依据</div>
                <div v-if="item.error_reason && item.verdict !== 'ungradable'" class="g-line">
                  <b>错因</b>{{ item.error_reason }}
                </div>
                <div v-if="item.basis" class="g-basis">{{ item.basis }}</div>
                <div v-if="!item.basis && !item.error_reason" class="small muted">AI 还没给出依据</div>
              </div>

              <!-- 图上读到的东西。**必须显示**：图形证据现在可以作判定依据了，
                   老师得能核对「它说的那条直角符号」到底在不在图上，否则等于盲信。 -->
              <div class="g-sec" v-if="item.figure_note">
                <div class="g-lb">
                  图上读到的
                  <span class="muted small">（（确定）能作数 /（看不清）只作疑）</span>
                </div>
                <div class="g-figure">{{ item.figure_note }}</div>
              </div>

              <div class="g-meta" v-if="item.kp_name || (item.tags || []).length">
                <span v-if="item.kp_name" class="tag blue">{{ item.kp_name }}</span>
                <span v-for="t in (item.tags || [])" :key="'tg-' + item.id + '-' + t" class="tag">{{ t }}</span>
              </div>

              <div class="g-verdicts">
                <span class="small muted">判定</span>
                <button v-for="v in VERDICTS" :key="'v-' + item.id + '-' + v.value" type="button"
                        class="g-vbtn" :class="[v.cls, { on: item.verdict === v.value }]"
                        :disabled="busy === 'v-' + item.id"
                        @click.stop="setVerdict(item, v.value)">{{ v.label }}</button>
              </div>

              <div class="g-foot">
                <button class="btn ghost sm" :disabled="busy === 'rr-' + item.id"
                        @click.stop="rerunItem(item)">
                  {{ busy === 'rr-' + item.id ? '提交中…' : '只重批这一题' }}
                </button>
                <button class="btn ghost sm" @click.stop="openBank(item)">存到题库</button>
                <div class="spacer"></div>
                <label class="g-check" @click.stop>
                  <input type="checkbox" :checked="item.status === 'approved'"
                         @change="toggleReviewed(item, $event.target.checked)"> 已核对
                </label>
              </div>

              <!-- 存到题库：不自动建题，题目 id 由老师指定（B1 拍板） -->
              <div v-if="bankOpenId === item.id" class="g-bank" @click.stop>
                <div class="small muted">
                  填题库里的题目 id（没有就先去题库建一道）；标签用逗号分隔，会追加到这道题上
                </div>
                <div class="row" style="margin-top:6px">
                  <input type="text" v-model="bankQuestionId" placeholder="题目 id">
                  <input type="text" v-model="bankTags" placeholder="标签，例如：一元二次方程, 计算错误">
                </div>
                <div style="margin-top:6px; display:flex; gap:8px">
                  <button class="btn sm primary" :disabled="busy === 'bank-' + item.id"
                          @click.stop="saveToBank(item)">
                    {{ busy === 'bank-' + item.id ? '保存中…' : '存' }}
                  </button>
                  <button class="btn sm ghost" @click.stop="closeBank">取消</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 文字框：点完位置弹出来。零构建不引第三方弹窗，自己写一层遮罩就够 -->
    <div v-if="textDraft" class="g-textmask" @click.self="textDraft = null">
      <div class="g-textbox">
        <div class="g-textbox-head">在卷面上写一句批注</div>
        <textarea v-model="textDraft.text" rows="3" autofocus
                  placeholder="例如：第 3 步两边同除 x 时漏了 x=0"
                  @keydown.ctrl.enter="saveText"></textarea>
        <div class="g-textbox-foot">
          <span class="small muted">Ctrl+Enter 放上去</span>
          <div class="spacer"></div>
          <button class="btn ghost sm" @click="textDraft = null">取消</button>
          <button class="btn primary sm" @click="saveText">放上去</button>
        </div>
      </div>
    </div>
  </div>`,
};
