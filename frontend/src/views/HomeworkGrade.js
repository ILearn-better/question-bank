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
//
// ⑤ **老师画的东西一律先落本机，再想办法送到服务端**（2026-10-19 加，用户原话：
//    「我用笔画批改作业时，不小心退出，记录就没了，得重新批改」）。
//    顺序不能反：先 `localDraft.pushMark` 再 POST。反过来的话，画完立刻关页面
//    那一下，请求被浏览器取消，这笔就真的没有了 —— 而界面上它是"画过"的。
//    详见 `src/gradeDraft.js` 头部：它存的不只是笔迹，还有框题草稿与判定依据文字。
//
// ⑥ **分割线只是「生成整页宽框」的快捷方式**，不是新的数据类型（`src/gradeLines.js`）。
//    线切出来的段 `box = [0,y0,1000,y1]`，与框选出来的段在库里长一个样 ——
//    所以裁切 / 跨页竖拼 / 逐题批改 / 沿用上一份，全都走已有那套，后端零改动。
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { gradeApi, lessonFilesApi } from '../api.js';
import { ensureLibs, renderRichEl } from '../mathRender.js';
import { fail, ok, warn } from '../store.js';
import GradeMarkLayer from '../components/GradeMarkLayer.js';
import { createDraft, isLocalMarkId, localMark, sameMark } from '../gradeDraft.js';
import {
  LINE_MIN_GAP, endOfPageFromSegs, isFullWidth, lineToDropFor, linesFromSegs,
  reslicePage, sortBlocksByGeometry,
} from '../gradeLines.js';
import { clampUnit } from '../markGeom.js';

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
    const tool = ref('view');          // view | ink | erase | text | check | cross | region | line
    const inkColor = ref('#dc2626');
    const textDraft = ref(null);       // {x, y, text}：点完位置弹出来的输入框
    const exporting = ref('');         // 正在导出的格式（按钮置灰）
    const layerEl = ref(null);         // 标注层组件：拖动提交失败时要调它回滚本地位移

    /* 本机草稿：笔迹队列 + 框题草稿 + 判定依据文字（见 src/gradeDraft.js）。
     * `boot` 是**页面刚打开时**读到的那一份，后面所有判断都以它为准 ——
     * 不要在别处再 readDraft 一次，两份快照一定会漂。 */
    const localDraft = createDraft(gid);
    const boot = localDraft.data;
    const pendingMarks = ref(localDraft.marks());   // 本机队列（含已发送未对账的）
    const syncPending = () => { pendingMarks.value = localDraft.marks(); };
    /** 还没送上去的笔数 —— 只数 `sent === false` 的（sent 的其实已经在库里）。 */
    const pendingUnsent = computed(
      () => pendingMarks.value.filter((m) => !m.sent).length,
    );
    let regionsRestored = false;      // 框题草稿只在第一次 load 之后恢复一次
    let flushedOnce = false;          // 补发也只在第一次 load 之后跑一次

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

    /* 这一页要画的标注 = 服务端的 + **本机队列里还没对上的**。
     *
     * 为什么要有后一半：画完立刻关页面/断网时，那一笔只在队列里。
     * 只画服务端的话，老师回来看到的就是「刚才画的那道红线不见了」——
     * 明明数据还在本机，却表现得像丢了，比真丢了更让人不信任。
     *
     * 去重靠 `sameMark`（kind + 取整后的坐标 + 文字）：服务端已经有完全相同的一条，
     * 就不再重复画 —— 否则「补发成功到下一次 load 之间」会看到同一笔画了两遍。
     */
    const pageMarks = computed(() => {
      const server = marks.value.filter((m) => Number(m.page_no || 1) === pageNo.value);
      const local = pendingMarks.value
        .filter((m) => Number(m.page_no || 1) === pageNo.value)
        .filter((m) => !server.some((s) => sameMark(s, m)))
        .map(localMark);
      return server.concat(local);
    });
    /** 老师自己放的（笔迹 / 文字 / √×）。撤销、清空只动这些 —— AI 猜的框一个字不碰。
     *  ⚠️ 含本机待发的那些：老师刚画完就该能撤销，不能因为「还没传上去」而撤不了。 */
    const teacherMarks = computed(() => pageMarks.value.filter((m) => m.created_by === 'teacher'));
    /** 只算服务端的那些 —— 撤销「上一笔」时要按 id 大小排，本机队列的 id 不是数字。 */
    const serverTeacherMarks = computed(
      () => marks.value.filter((m) => Number(m.page_no || 1) === pageNo.value
        && m.created_by === 'teacher' && !isLocalMarkId(m.id)),
    );

    /* ---------------- 一题一区域：框选 + 分割线 ----------------
     * 有段（框或线切出来的块）的页按块逐题批，没段的页整页一次判 —— 两条口径并存，
     * 所以这里必须分清「服务器上已经存了的」和「正在拖但还没保存的草稿」。
     *
     * ★ 框选与分割线是**同一个东西的两种画法**（2026-10-19 用户要求「我还要分割线分割」）：
     *   线切出来的段 = 整页宽的框 `[0,y0,1000,y1]`，落到库里与手拖的框一模一样。
     *   于是裁切 / 跨页竖拼 / 逐题批改 / 沿用上一份 全都走已有那套，**后端零改动**。
     *
     * ⚠️ 进「框题」/「分割线」模式才把草稿从服务器同步一份：平时不该有两份状态，
     *    否则轮询一刷新就把老师刚拖的框冲掉（或者反过来，草稿盖掉别人的改动）。
     *    但**本机草稿里有没保存的改动时绝不能盖** —— 那正是「退出就没了」要防的事。 */
    const regions = computed(() => (detail.value && detail.value.regions) || []);
    const pageRegions = computed(
      () => regions.value.filter((r) => Number(r.page_no || 1) === pageNo.value),
    );
    /* ⚠️ 草稿是**整份**的（按页分桶），不是「当前页一份」。
     *    跨页题的几段天然落在不同页上：第 1 页画上半、翻页画下半，然后合成一道题。
     *    如果草稿只存当前页，翻一下页上半段就没了 —— 跨页根本画不出来。
     *    翻页**不提示未保存**（那是框题的常态操作），只有离开框题模式才提示。 */
    const editAll = ref({});            // { 页号: [段…] }，每段 { id, seq, q_no, box, src, page_no, skipped }
    /** 分割线：{ 页号: [y, …] }。**不是落库的实体** —— 它只决定「怎么切」，
     *  切出来的段才是。保存之后线能从整页宽的段反推回来（见 syncRegions）。 */
    const editLines = ref({});
    /** 每页的**结束线**：`{ 页号: y }`，默认 1000（切到页底）。
     *
     *  这是「每页尾部处理不到位」那个旋钮：最后一题的作答区通常一直写到底，
     *  但页脚、页码、条形码也在那儿。把它拖到 96% 就表示「下面 4% 不是题」。
     *  ⚠️ 它**不是落库的实体**：切出来的最后一段底边就是它，重开页面从段反推
     *     （`endOfPageFromSegs`）—— 同一个事实只有一个出处。 */
    const editEnds = ref({});
    const editRegions = computed(() => editAll.value[pageNo.value] || []);
    const pageLines = computed(() => editLines.value[pageNo.value] || []);
    const pageEnd = computed(() => Number(editEnds.value[pageNo.value] || 1000));
    const lineCount = computed(
      () => Object.values(editLines.value || {}).reduce((n, ys) => n + (ys || []).length, 0),
    );
    const regionsDirty = ref(false);
    /** 框选与分割线共用一套面板 —— 数据上它们本来就是同一件事。 */
    const editingRegions = computed(() => tool.value === 'region' || tool.value === 'line');

    /** 把一串段按「题」（`seq`）聚合成块。界面上一行是一道题，不是一个框。 */
    function groupSegs(list) {
      const by = new Map();
      (list || []).forEach((s) => {
        const k = Number(s.seq || 0);
        if (!by.has(k)) by.set(k, []);
        by.get(k).push(s);
      });
      return Array.from(by.entries())
        .sort((a, b) => a[0] - b[0])
        .map(([seq, segs]) => {
          const ordered = segs.slice().sort(
            (a, b) => Number(a.page_no || 1) - Number(b.page_no || 1)
              || Number((a.box || [0, 0])[1] || 0) - Number((b.box || [0, 0])[1] || 0),
          );
          const q = (ordered.find((s) => String(s.q_no || '').trim()) || {}).q_no || '';
          return {
            seq, q_no: String(q).trim(), label: String(q).trim() || String(seq + 1),
            segs: ordered, pages: ordered.map((s) => Number(s.page_no || 1)),
            // 一块被跳过 = 它的每一段都标了跳过（后端 put_regions 会归一成这个状态）。
            skipped: ordered.every((s) => !!s.skipped),
          };
        });
    }

    /** 草稿摊平成段（每段补上它属于哪一页）。 */
    function allDraftSegs() {
      const out = [];
      Object.entries(editAll.value || {}).forEach(([p, list]) => {
        (list || []).forEach((s) => out.push({ ...s, page_no: Number(p) }));
      });
      return out;
    }

    /** 面板要显示的「题」：编辑模式下是草稿，平时是服务器存的。 */
    const regionBlocks = computed(
      () => groupSegs(editingRegions.value ? allDraftSegs() : regions.value),
    );
    /** 草稿里的**框**（段）总数 —— 与「题数」不是一个数，跨页题是 1 题 2 框。 */
    const draftSegCount = computed(() => allDraftSegs().length);

    /** 交给标注层画的框：每段一个框，label 用**它所属那道题**的标签（跨页两段同号）。 */
    const layerRegions = computed(() => {
      if (!editingRegions.value) return pageRegions.value;
      const labels = new Map(regionBlocks.value.map((b) => [Number(b.seq), b.label]));
      return editRegions.value.map((s) => ({
        ...s, label: labels.get(Number(s.seq)) || '',
      }));
    });

    /** 交给标注层画的分割线。编辑时用草稿里的线；平时从整页宽的段反推。 */
    const layerLines = computed(() => (
      editingRegions.value ? pageLines.value : linesFromSegs(pageRegions.value)
    ));

    /** 这一页由线切出来的段（只用于画淡色带 + 序号，让老师数得清切成了几题）。 */
    const pageBands = computed(() => {
      if (!editingRegions.value) return [];
      return editRegions.value
        .filter((s) => s.src === 'line')
        .map((s) => s.box)
        .sort((a, b) => Number(a[1]) - Number(b[1]));
    });

    function syncRegions() {
      const by = {};
      const forLines = {};
      const ends = {};
      const byPage = {};
      regions.value.forEach((r) => {
        const p = Number(r.page_no || 1);
        const box = (r.box || []).slice();
        // 整页宽的段只可能来自分割线（手拖一个框几乎不可能同时压在 0 和 1000 上）。
        // 标出来之后，重开页面还能继续加线 / 拖线，而不是变成一堆"整页的框"。
        const src = isFullWidth(box) ? 'line' : 'box';
        (by[p] = by[p] || []).push({
          id: r.id, seq: Number(r.seq || 0), q_no: r.q_no || '',
          box, page_no: p, src, skipped: !!r.skipped,
        });
        (forLines[p] = forLines[p] || []).push({ box });
        (byPage[p] = byPage[p] || []).push({ box });
      });
      const lines = {};
      Object.entries(forLines).forEach(([p, list]) => {
        const ys = linesFromSegs(list);
        if (ys.length) lines[p] = ys;
      });
      // 结束线从段反推：最后一段的底边就是它（没有线段就是页底 1000）。
      Object.entries(byPage).forEach(([p, list]) => {
        const e = endOfPageFromSegs(list);
        if (e < 1000) ends[p] = e;
      });
      editAll.value = by;
      editLines.value = lines;
      editEnds.value = ends;
      regionsDirty.value = false;
    }

    /** 草稿落本机（内部防抖）。框题一页十几刀，丢一次就是十几分钟 —— 必须随画随存。 */
    function touchRegionsDraft() {
      localDraft.touch({
        regions: {
          dirty: !!regionsDirty.value, lines: editLines.value,
          ends: editEnds.value, segs: editAll.value,
        },
      });
    }

    /** 按**阅读顺序**收口 seq（先页、再段顶边）。跨页题的分组不会被拆开 ——
     *  `sortBlocksByGeometry` 只换号，同 seq 的几段一起换。 */
    function sortAll() {
      const flat = allDraftSegs();
      if (!flat.length) { editAll.value = {}; return; }
      const sorted = sortBlocksByGeometry(flat);
      const by = {};
      sorted.forEach((s) => {
        const p = Number(s.page_no || 1);
        (by[p] = by[p] || []).push(s);
      });
      editAll.value = by;
    }

    /** 某一页**重新切一遍**：线切出来的带 + 已有的框段（被框盖住的带不再生成）。
     *
     *  ⚠️ 新切出来的段必须给**唯一的 seq**。全填同一个值（比如 -1）的话，
     *     它们会被 groupSegs 聚成「同一题」—— 一页切八刀，最后变成一道题。
     *     这是分割线最容易错的一步：界面上框数是 9，题数是 1。
     */
    function resliceOne(p) {
      const old = editAll.value[p] || [];
      const raw = reslicePage(old, editLines.value[p] || [], editEnds.value[p] || 1000);
      let next = 0;
      allDraftSegs().forEach((s) => { next = Math.max(next, Number(s.seq) + 1); });
      /* ★ **分组继承**：新带的**上边界**与某个旧线段相同，就把那个旧段的
       *   `seq / q_no / skipped` 一起搬过来。
       *
       *   为什么必须有这一步：跨页题靠 `seq` 分组，而这里会把这一页的线段
       *   **全部重建**。老师「把跨页题并好 → 顺手把末段底边拖到 96%」是很自然的
       *   收尾顺序 —— 不继承的话，第 1 页那半段拿到一个新 seq，跨页题当场被拆成
       *   两道（一道只有上半页、一道只有下半页）。界面上只表现为「题数多了一题」，
       *   很难看出是哪一步拆的。
       *
       *   为什么按「上边界相同」而不是面积重叠比：一题的**起点**才是它的身份。
       *   拖结束线只动末段的底边（y0 不变）→ 该继承；加一刀则下半段有一个新的
       *   y0 → 该拿新 seq（「切歪了重切」就是在表达这个意思）。面积比两个方向
       *   都会错：`new ⊂ old` 会把加一刀的两半都判成继承，`old ⊂ new` 则会把
       *   「末段被截掉一大半」判成不继承 —— 跨页题照样被拆掉。
       *   `taken` 保证一个旧段只被认领一次（两半继承同一个 seq 就并成一题了）。 */
      const top = (b) => Math.round(Number((b || [])[1] || 0));
      const taken = new Set();
      const out = raw.map((s) => {
        if (s.src !== 'line') return s;      // 框段原样：id / seq / q_no / 跨页分组全保住
        const host = old.find((o) => o.src === 'line' && !taken.has(o)
          && top(o.box) === top(s.box));
        if (host) {
          taken.add(host);
          return {
            ...s, seq: Number(host.seq || 0), q_no: host.q_no || '',
            skipped: !!host.skipped, page_no: p,
          };
        }
        const seg = { ...s, seq: next, page_no: p };
        next += 1;
        return seg;
      });
      editAll.value = { ...editAll.value, [p]: out };
      sortAll();
      regionsDirty.value = true;
      touchRegionsDraft();
    }

    /* ---------------- 分割线的三个动作（标注层只报坐标，判断全在这一层） ---------------- */

    /** 点一下加一条线。`y` 已经是 0~1000 的页坐标（换算在标注层做，这里不重算）。 */
    function addLine(y) {
      const p = pageNo.value;
      const v = Math.round(Number(y));
      if (!Number.isFinite(v)) return;
      const cur = (editLines.value[p] || []).slice();
      // 太近就不加：切出来的段会比后端 REGION_MIN_SIDE 还薄，送上去是 422，
      // 界面上还会多一行「第 N 题 · 高 0.3%」—— 两个人都不知道该怎么办。
      if (cur.some((l) => Math.abs(l - v) < LINE_MIN_GAP)) {
        warn('离已有的线太近了 —— 那样切出来的题太薄，模型读不出内容');
        return;
      }
      cur.push(v);
      cur.sort((a, b) => a - b);
      editLines.value = { ...editLines.value, [p]: cur };
      resliceOne(p);
    }

    /** 拖线微调。标注层只在真挪动过时才报（点一下不动手什么都不做）。 */
    function moveLine(payload) {
      const p = pageNo.value;
      const from = Math.round(Number(payload && payload.from));
      const to = Math.round(Number(payload && payload.to));
      if (!Number.isFinite(from) || !Number.isFinite(to)) return;
      const cur = (editLines.value[p] || [])
        .map((l) => (Math.abs(l - from) < 0.5 ? to : l))
        .sort((a, b) => a - b);
      editLines.value = { ...editLines.value, [p]: cur };
      resliceOne(p);
    }

    function removeLine(y) {
      const p = pageNo.value;
      const v = Number(y);
      const cur = (editLines.value[p] || []).filter((l) => Math.abs(l - v) > 0.5);
      editLines.value = { ...editLines.value, [p]: cur };
      resliceOne(p);
    }

    /* ---------------- 每页尾部：结束线（末段切到哪为止） ----------------
     * 用户原话：「每页尾部处理也不到位」。真实情况是：最后一题的作答区一直写到
     * 页底，而页底还有页脚 / 页码 / 条形码 —— 末段必然把那些一起送进模型。
     * 这里给一个明确的旋钮：末段底边拖到 96%，下面 4% 就不进任何块。
     * ⚠️ 只影响**线切出来的段**；手拖的框本来就由老师自己定边，不去动它。 */
    function setPageEnd(y) {
      const p = pageNo.value;
      const v = Math.round(clampUnit(Number(y)));
      if (!Number.isFinite(v) || v <= 0) return;
      // 结束线必须落在最后一条线下方，否则会切出一个 0 高度的段（后端直接 422）
      const lastLine = (editLines.value[p] || []).slice().sort((a, b) => b - a)[0] || 0;
      if (v - lastLine < LINE_MIN_GAP) {
        warn('结束线太靠上了 —— 至少要离最后一条分割线 ' + LINE_MIN_GAP + ' 的距离');
        return;
      }
      editEnds.value = { ...editEnds.value, [p]: v };
      resliceOne(p);
    }

    function resetPageEnd() {
      const p = pageNo.value;
      const next = { ...editEnds.value };
      delete next[p];
      editEnds.value = next;
      resliceOne(p);
    }

    /** 末段下方那一截（页脚带）的位置，给标注层画一条提示 —— 没有就是 null。 */
    const pageTail = computed(() => {
      if (!editingRegions.value) return null;
      const e = Number(editEnds.value[pageNo.value] || 1000);
      return e < 1000 ? [0, e, 1000, 1000] : null;
    });

    /* ---------------- 不送 AI：按块 / 按页（0030） ---------------- */

    /** 一块（可能多段）设不设「不判」。**只改草稿**，点「保存分割」才落库。
     *  ⚠️ 按 `seq` 改**整块的所有段**：跨页题只跳上半段等于只批半道题。 */
    function toggleBlockSkip(b) {
      const seq = Number(b && b.seq);
      const want = !(b && b.skipped);
      const by = {};
      Object.entries(editAll.value).forEach(([k, list]) => {
        by[k] = (list || []).map((s) => (Number(s.seq) === seq ? { ...s, skipped: want } : s));
      });
      editAll.value = by;
      regionsDirty.value = true;
      touchRegionsDraft();
    }

    /** 整页不看：**立即落库**（它是一条独立接口，不跟着「保存分割」走）。
     *  理由：老师说「这页不看」的那一刻就该生效，混在还没保存的框草稿里很容易漏。 */
    const skippedPages = computed(
      () => ((detail.value && detail.value.skipped_pages) || []).map(Number),
    );
    const pageSkipped = computed(() => skippedPages.value.includes(pageNo.value));
    async function togglePageSkip() {
      const next = pageSkipped.value
        ? skippedPages.value.filter((n) => n !== pageNo.value)
        : skippedPages.value.concat([pageNo.value]);
      busy.value = 'pskip';
      try {
        const d = await gradeApi.setSkippedPages(gid, next);
        if (detail.value) detail.value = { ...detail.value, skipped_pages: d.skipped_pages || [] };
        ok(pageSkipped.value ? '这一页会重新送去批改' : '这一页不再送 AI 了（框都还留着）');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    /** 这一块切出来的样子（现裁、不落盘）。**保存前就能看** —— 这就是
     *  「分割截图完后看不到图」的正解：切歪了当场发现，而不是等模型判完。
     *  用 `id` 做 key 让浏览器缓存同一块的图；坐标一变 URL 就变，自然失效。 */
    function blockPreview(b) {
      const segs = (b && b.segs) || [];
      if (!segs.length) return '';
      const spec = segs
        .map((s) => `${Number(s.page_no || 1)}:${(s.box || []).join(',')}`)
        .join(';');
      return gradeApi.regionImageUrl(gid, spec);
    }
    /** 大图：点缩略图新开一个页签看原尺寸（不引弹窗库）。 */
    function openPreview(b) {
      const url = blockPreview(b);
      if (url) window.open(url, '_blank', 'noopener');
    }

    /** 画完一个框（标注层发上来的是 [x0,y0,x1,y1]）。只在本地，点「保存框」才落库。 */
    function addRegionBox(box) {
      const p = pageNo.value;
      const list = (editAll.value[p] || []).slice();
      let seq = 0;
      allDraftSegs().forEach((s) => { seq = Math.max(seq, Number(s.seq) + 1); });
      list.push({ id: null, seq, q_no: '', box, page_no: p, src: 'box' });
      editAll.value = { ...editAll.value, [p]: list };
      // 再切一次：被这个框盖住的线切段要退场，否则同一道题会出现两次
      // （老师「先画线切好，再单独框住其中一题」就是靠这一步收口的）。
      resliceOne(p);
    }

    /** 点图上的框/段 → 从草稿里去掉它（保存时才真的删）。
     *
     *  ⚠️ 线段不能"直接删段"：重切时它还会回来。删线段 = **删掉它上边那条线**
     *     （语义是「这一刀不需要，并到相邻那段去」），见 gradeLines.lineToDropFor。
     *  ⚠️ 匹配用 `box + seq` 而不是对象引用：标注层拿到的 `layerRegions` 是
     *     `map` 出来的**新对象**，`x !== r` 永远成立 —— 那样点了删不掉，
     *     而界面上看着"点了没反应"（2026-10-19 顺手修掉的老 bug）。
     */
    function dropRegion(r) {
      if (!r) return;
      const p = pageNo.value;
      if (r.src === 'line' || isFullWidth(r.box)) {
        const y = lineToDropFor(r.box, editLines.value[p] || []);
        if (y !== null) { removeLine(y); return; }
      }
      const sig = JSON.stringify(r.box || []);
      editAll.value = {
        ...editAll.value,
        [p]: editRegions.value.filter((x) => !(JSON.stringify(x.box || []) === sig
          && Number(x.seq) === Number(r.seq))),
      };
      regionsDirty.value = true;
      sortAll();
      touchRegionsDraft();
    }

    /** 删掉**整道题**（跨页题的两段一起删）—— 面板上的「删」按钮。
     *
     *  ⚠️ 线段删了会「复活」：它在 `editLines` 里还有对应的那一刀，重切时又出来。
     *     所以线段要先把它上边那条线删掉（与 dropRegion 同一条规则）。 */
    function dropBlock(b) {
      const segs = (b && b.segs) || [];
      if (segs.some((s) => s.src === 'line')) {
        segs.filter((s) => s.src === 'line').forEach((s) => {
          const p = Number(s.page_no || 1);
          const y = lineToDropFor(s.box, editLines.value[p] || []);
          if (y !== null) {
            editLines.value = {
              ...editLines.value,
              [p]: (editLines.value[p] || []).filter((l) => Math.abs(l - y) > 0.5),
            };
          }
        });
      }
      const next = {};
      Object.entries(editAll.value).forEach(([p, list]) => {
        next[p] = (list || []).filter((s) => Number(s.seq) !== Number(b.seq));
      });
      editAll.value = next;
      regionsDirty.value = true;
      sortAll();
      touchRegionsDraft();
    }

    /** 把这道题并入**上一题**（跨页题的收口动作，与批量录题的「并入上一块」同一个意思）。
     *
     *  这是「跨页分割的题目合并」的落点：第 2 页的第一段（接着上一页那道题的）
     *  并入第 1 页的最后一段 → 两段同一个 `seq` → 后端竖拼成一张图再判。
     *  ⚠️ 只认**列表顺序**，不要求两段在页面上相邻 —— 正是跨页时的常态。 */
    function mergeRegion(b) {
      const blocks = regionBlocks.value;
      const i = blocks.findIndex((x) => Number(x.seq) === Number(b.seq));
      if (i <= 0) {
        warn('这就是第一题，上面没有可以并的');
        return;
      }
      const prev = Number(blocks[i - 1].seq);
      const next = {};
      Object.entries(editAll.value).forEach(([p, list]) => {
        next[p] = (list || []).map(
          (s) => (Number(s.seq) === Number(b.seq) ? { ...s, seq: prev } : s),
        );
      });
      editAll.value = next;
      regionsDirty.value = true;
      sortAll();
      touchRegionsDraft();
    }

    /** 题号收口（旧名 relabelRegions）：老师删掉中间一题后，后面的要顺上来。
     *  现在直接交给 `sortAll` —— 它按**几何**排，连「先画第 2 页后画第 1 页」
     *  也能排对（分割线是逐页切的，老师完全可能从中间某页开始）。 */
    function relabelRegions() {
      sortAll();
    }

    /** 改题号 = 改**整道题**（跨页题两段一起改，否则会变成两道题）。 */
    function setRegionQNo(b, v) {
      const next = {};
      Object.entries(editAll.value).forEach(([p, list]) => {
        next[p] = (list || []).map(
          (s) => (Number(s.seq) === Number(b.seq) ? { ...s, q_no: v } : s),
        );
      });
      editAll.value = next;
      regionsDirty.value = true;
      touchRegionsDraft();
    }

    /** 清空**整份**草稿（跨页框存在之后，「本页」这个词已经不够用了）。
     *  ⚠️ 线也要一起清 —— 只清段的话，下一次任何 operation 触发重切，
     *     整页的段又会长回来（老师会以为「清空没生效」）。 */
    function clearRegions() {
      if (!allDraftSegs().length && !lineCount.value) return;
      editAll.value = {};
      editLines.value = {};
      regionsDirty.value = true;
      touchRegionsDraft();
    }

    async function saveRegions() {
      busy.value = 'regions';
      try {
        const segs = allDraftSegs();
        // `grouped: true` 是**显式声明**「我按 seq 分好组了」。不传的话后端按
        // 「一框一题」处理 —— 那是安全的默认值，但跨页题会被拆成两题。
        const res = await gradeApi.putRegions(gid, {
          grouped: true,
          regions: segs.map((s) => ({
            page_no: Number(s.page_no),
            seq: Number(s.seq || 0),
            q_no: String(s.q_no || '').trim(),
            box: s.box,
            // 「这块不送 AI」。后端会把同一题几段归一成一致的值。
            skipped: !!s.skipped,
          })),
        });
        detail.value.regions = res.regions;
        syncRegions();
        // 存进库了 = 本机草稿的使命结束。不清的话，下次打开还会弹「恢复了没保存的框」。
        localDraft.touch({ regions: null });
        const blocks = groupSegs(res.regions || []);
        const cross = blocks.filter((b) => b.pages.length > 1).length;
        const skipped = blocks.filter((b) => b.skipped).length;
        ok(segs.length
          ? `已保存 ${blocks.length} 题（${segs.length} 个段${cross ? `，其中 ${cross} 题跨页` : ''}`
            + `${skipped ? `，${skipped} 题不判` : ''}）`
          : '已清空全部框 —— 整份退回整页批');
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
        const blocks = groupSegs(res.regions || []);
        ok(`已沿用另一份的 ${blocks.length} 题（${(res.regions || []).length} 个框）`
          + ' —— 扫一眼，框偏了就重画');
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
    /** 答案/细则**引用的文件**（lesson_files.id）。
     *  为什么要有这条：老师手上常常只有一张答案照片，没有可粘贴的文字。 */
    const setupAnswerFiles = ref([]);
    /** 服务器上「真的会被送进模型」的图片（按 role 分）。只给文字的话，
     *  老师传了照片却看到答案框空着，会以为没生效。 */
    const setupImages = computed(
      () => (detail.value && detail.value.answer_images) || { answer: [], rubric: [] },
    );
    const answerImgCount = computed(
      () => (setupImages.value.answer || []).length
        + (setupImages.value.rubric || []).length,
    );
    const setupEditable = computed(() => !!detail.value
      && ['queued', 'failed'].includes(detail.value.status) && !detail.value.done);

    function initSetup() {
      if (!detail.value) return;
      setupMode.value = detail.value.answer_mode || 'ai';
      setupAnswer.value = detail.value.answer_text || '';
      setupRubric.value = detail.value.rubric_text || '';
      setupAnswerFiles.value = (detail.value.answer_file_ids || []).map(Number);
      setupTouched.value = false;
    }

    /** 把没保存的判定依据从本机草稿恢复回来。
     *
     *  为什么必须有这一步：老师粘贴了半页评分细则、又去翻了一下卷面，
     *  回来发现输入框是空的 —— 那种「白填一遍」的挫败感比丢笔迹还强。
     *  ⚠️ 已经批出结果时不恢复（`setupEditable` 为假）：那时候改了也存不进去，
     *     不如让他看到服务端真实用的那一份。 */
    function restoreDraftSetup() {
      const s = boot.setup;
      if (!s || setupTouched.value) return;
      if (!setupEditable.value) { localDraft.touch({ setup: null }); return; }
      if (!s.answer_mode && !s.answer_text && !s.rubric_text
          && !(s.files || []).length) return;
      setupMode.value = s.answer_mode || setupMode.value;
      setupAnswer.value = String(s.answer_text || '');
      setupRubric.value = String(s.rubric_text || '');
      setupAnswerFiles.value = (s.files || []).map(Number);
      // 标成「改过」：不保存就退出时，草稿还得在。否则下一轮 load 又把它冲掉。
      setupTouched.value = true;
      warn('恢复了上次没保存的判定依据 —— 看一眼对不对，再点「保存依据」');
    }

    function touchSetup() {
      setupTouched.value = true;
      localDraft.touch({
        setup: {
          answer_mode: setupMode.value,
          answer_text: setupAnswer.value,
          rubric_text: setupRubric.value,
          files: setupAnswerFiles.value.slice(),
        },
      });
    }

    /** 保存判定依据。返回 false = 别继续（比如老师填了细则但保存失败，不该照样开批）。 */
    async function saveSetup() {
      if (!detail.value || !setupEditable.value || !setupTouched.value) return true;
      setupBusy.value = true;
      try {
        detail.value = await gradeApi.patchSetup(gid, {
          answer_mode: setupMode.value,
          answer_text: setupAnswer.value,
          rubric_text: setupRubric.value,
          answer_file_ids: setupAnswerFiles.value,
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

    /** 传一张答案 / 细则**照片**。图会真的随卷面一起送进模型（不是抽文字）。
     *
     *  为什么走 `role=homework`：这是**这次作业**的文件（答案页 / 细则页扫描件），
     *  与提交件同一份清单，老师回头在「交作业」里也找得到，不会变成一份没人认领的孤儿。
     *  ⚠️ `input` 上**不要**设 `accept` —— 本机经验：accept 会把老师想选的文件藏起来
     *     （.lnk 那种），比选错更烦。格式不对后端会当场说清楚。 */
    async function uploadAnswerFile(file) {
      if (!file) return;
      if (!setupEditable.value) {
        warn('这份已经开始批了 —— 换依据要重建任务');
        return;
      }
      // 剪贴板里的图常常**没有文件名**（截图工具只给一个 Blob）。
      // 这里补一个名字：后端按扩展名判"这是不是图"，没有名字会被当成未知格式退回。
      let f = file;
      if (!f.name) {
        const ext = String(f.type || '').includes('jpeg') ? 'jpg'
          : String(f.type || '').includes('webp') ? 'webp' : 'png';
        try {
          f = new File([f], `paste-${Date.now()}.${ext}`, { type: f.type || 'image/png' });
        } catch (e) {
          f = file;        // 老浏览器没有 File 构造器：原样传，让后端说话
        }
      }
      setupBusy.value = true;
      try {
        const fd = new FormData();
        fd.append('file', f);
        const up = await lessonFilesApi.upload(detail.value.lesson_id, fd, 'homework');
        setupAnswerFiles.value = setupAnswerFiles.value.concat([Number(up.id)]);
        detail.value = await gradeApi.patchSetup(gid, {
          answer_mode: setupMode.value,
          answer_text: setupAnswer.value,
          rubric_text: setupRubric.value,
          answer_file_ids: setupAnswerFiles.value,
        });
        initSetup();
        localDraft.touch({ setup: null });     // 已经存进库了，本机草稿不必再留着
        ok('答案照片传上去了 —— 批改时会把这张图一起给模型看');
      } catch (err) {
        fail(err.message);
      } finally {
        setupBusy.value = false;
      }
    }

    /** 从 `<input type="file">` 选文件。 */
    async function uploadAnswerImage(e) {
      const file = (e.target && e.target.files && e.target.files[0]) || null;
      if (e.target) e.target.value = '';      // 清掉 value：同一个文件再选一次也能触发
      await uploadAnswerFile(file);
    }

    /** 从剪贴板 / 拖进来的东西里挑出**第一张图**。
     *
     *  用户 2026-10-19 的要求：「上传图片也可是缓存中的图片」——
     *  最常走的就是这条路：截图（或从别的窗口复制一张图）→ Ctrl+V。
     *  以前非得先存成文件再选，多两步，还要记得存哪儿了。
     *
     *  三种来源的差别（都遇到过）：
     *    ① `items` 里的 `kind === 'file'` —— 截图工具、从图片编辑器复制，最常见
     *    ② `files` —— 少数浏览器把粘贴的图只放这里，不放进 items
     *    ③ 从网页复制的 `<img>` —— `items` 里只有一串 HTML/URL，**拿不到二进制**。
     *       那种情况这里返回 null，页面提示「先存成文件」—— 不硬猜 URL（猜了也是 CORS 404）。
     */
    function imageFromClipboard(e) {
      const cd = e && (e.clipboardData || e.dataTransfer || globalThis.clipboardData);
      if (!cd) return null;
      const items = cd.items || [];
      for (let i = 0; i < items.length; i += 1) {
        const it = items[i];
        if (it && it.kind === 'file' && String(it.type || '').startsWith('image/')) {
          const f = it.getAsFile ? it.getAsFile() : null;
          if (f) return f;
        }
      }
      const files = cd.files || [];
      for (let i = 0; i < files.length; i += 1) {
        if (String(files[i].type || '').startsWith('image/')) return files[i];
      }
      return null;
    }

    /** 全局粘贴：**只在判定依据能改、且真的贴了一张图时**接管。
     *
     *  ⚠️ 贴的是文字时**绝不 preventDefault** —— 老师往答案框里贴一段题目文本，
     *     那是这个页面最常见的操作，抢过来就是灾难。
     *  ⚠️ 只在 `setupMode` 不是 `ai` 时接（AI 自己解的时候没有"给答案"这个概念）。
     */
    function onPaste(e) {
      if (setupMode.value === 'ai' || !setupEditable.value) return;
      const file = imageFromClipboard(e);
      if (!file) return;
      e.preventDefault();
      uploadAnswerFile(file);
    }

    /** 拖一张图到「或者直接传照片」那块。 */
    function onAnswerDrop(e) {
      if (!setupEditable.value) {
        warn('这份已经开始批了 —— 换依据要重建任务');
        return;
      }
      if (setupMode.value === 'ai') {
        warn('先把上面切到「用我给的答案」或「用我的评分细则」，图才有去处');
        return;
      }
      const dt = e && e.dataTransfer;
      const files = (dt && dt.files) || [];
      const img = Array.from(files).find((f) => String(f.type || '').startsWith('image/'));
      if (img) { uploadAnswerFile(img); return; }
      warn('拖进来的不是图片 —— 图片文件（png / jpg）才收');
    }

    async function clearAnswerImages() {
      if (!setupEditable.value) {
        warn('这份已经开始批了 —— 换依据要重建任务');
        return;
      }
      setupBusy.value = true;
      try {
        detail.value = await gradeApi.patchSetup(gid, {
          answer_mode: setupMode.value, answer_file_ids: [],
        });
        initSetup();
        ok('已去掉答案照片 —— 只按粘贴的文字判');
      } catch (err) {
        fail(err.message);
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

        /* ---- 本机草稿的恢复（只在**第一次**非 silent 加载之后做一次） ----
         * 三件事分开做，因为它们的"该不该恢复"完全不同：
         *   · 判定依据：已经批出结果就不恢复（改了也存不进去）
         *   · 框题/分割线：有草稿就恢复，并标成"未保存"让老师知道要按保存
         *   · 页号：恢复到上次看的那一页 —— 一页七八题，回来还得找回原页很烦 */
        if (!silent && !regionsRestored) {
          regionsRestored = true;
          restoreDraftSetup();
          const r = boot.regions;
          if (r && (Object.keys(r.segs || {}).length || Object.keys(r.lines || {}).length)) {
            editAll.value = r.segs || {};
            editLines.value = r.lines || {};
            editEnds.value = r.ends || {};
            regionsDirty.value = true;
            warn('恢复了上次没保存的框/分割线 —— 看一眼，对了就点「保存分割」');
          }
          if (boot.page_no > 1 && boot.page_no <= pageCount.value) pageNo.value = boot.page_no;
        }
        // 每次拿到服务端数据都对一次账：队列里「其实已经存进去了」的清掉
        reconcile();
        /* ⚠️ **先关 loading，再画**。题目区在模板里是 `v-else`（`v-if="loading"` 的反面），
         * loading 还是 true 的时候那几个 `.g-rich` 容器**根本不在 DOM 里**，`setRich` 没被调过
         * → `paintKey` 全部命中 `if (!el) return` → 题干 / 学生作答 / 参考答案**一片空白**。
         * 2026-10-19 用 jsdom 真挂一遍才发现：容器有 3 个、内容 0 个。
         * 为什么平时没露馅：轮询与「跑完自动刷新」都走 load(true)（silent，不动 loading），
         * 容器早就在了 —— **只有第一次打开/刷新这一页**才白。 */
        if (!silent) loading.value = false;
        await paintAll();

        /* 补发上次没传上去的笔迹。放在**这里**（load 成功之后）而不是 onMounted：
         * 补发前要先知道服务端已经有哪些，否则会把同一笔画两遍。
         * `flushedOnce` 在调用**之前**置位 —— flushPending 内部还会 load(true) 一次，
         * 不置位就是无限递归。 */
        if (!flushedOnce && !silent) {
          flushedOnce = true;
          await flushPending();
        }
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
      // 离开「框题/分割线」时没保存的东西不能悄悄丢 —— 老师画了五个框最怕这个。
      // ⚠️ 只提示、**不替老师清草稿**：本机草稿仍然在，下次点回来还能接着弄。
      if (editingRegions.value && !['region', 'line'].includes(next)
          && regionsDirty.value
          && !window.confirm('这些框/分割线还没保存，切换就收起。要放弃吗？\n（不急：它们已经存在本机，点回来还在）')) {
        return;
      }
      tool.value = next;
      if (tool.value !== 'text') textDraft.value = null;
      // ⚠️ 有未保存草稿时**绝不**从服务器同步 —— 那会把老师刚画的一把盖掉，
      //    正是「退出就没了」的同一个 bug 换了个触发点。
      if (editingRegions.value && !regionsDirty.value) syncRegions();
    }

    /* ---------------- 笔迹：先存本机，再送服务端 ----------------
     * 顺序反过来就是这次要修的 bug（用户原话：「不小心退出，记录就没了」）：
     * 先 POST 的话，画完立刻关页面那一下请求会被浏览器取消 —— 界面上"画过"，
     * 库里没有，回来一看全没了。
     *
     * 现在的时序：
     *   ① `pushMark` 进本机队列（localStorage，防抖写）→ 这一笔从此不会丢
     *   ② 乐观显示：`pageMarks` 会把它和服务器的一起画出来
     *   ③ POST；成功标 `sent`，`settle()` 拿到服务端数据，**对上了才出队**
     *   ④ 失败：留在队列里，界面上照常显示，下次打开自动补发
     */
    async function addMark(payload) {
      const body = Object.assign({ page_no: pageNo.value }, payload);
      const lid = localDraft.pushMark(body);
      syncPending();
      try {
        await gradeApi.addMark(gid, body);
        localDraft.setSent(lid, true);
        syncPending();
        await settle();
        // settle 成功才出队 —— 否则「POST 成功但详情没拉回来」会有一瞬间的空白
        localDraft.dropMarks([lid]);
        syncPending();
      } catch (e) {
        fail(`${e.message} —— 这一笔已经存在本机，下次打开会自动补发`);
      }
    }

    /** 把队列里服务端还没有的补发上去。返回补了几笔。
     *
     *  ⚠️ **只发服务端没有的**：`sameMark` 比的是形状+坐标，不是 id ——
     *     因为是另一台设备/清过缓存之后，id 早就对不上了。
     *  ⚠️ 一次批量发（`addMarks`），不是一笔一个请求：刚打开页面那几秒里
     *     网络最不稳，三十笔连发很容易断在半路。 */
    async function flushPending(manual = false) {
      const queue = localDraft.marks();
      if (!queue.length) {
        if (manual) ok('没有要补发的笔迹');
        return 0;
      }
      const missing = queue.filter((m) => !marks.value.some((s) => sameMark(s, m)));
      if (!missing.length) {
        // 全都在库里了（上次其实发成功了，只是没来得及对账）
        localDraft.dropMarks(queue.map((m) => m.lid));
        syncPending();
        if (manual) ok('没有要补发的笔迹');
        return 0;
      }
      try {
        await gradeApi.addMarks(gid, missing.map((m) => ({
          page_no: m.page_no,
          kind: m.kind,
          geo: m.geo,
          points: m.points,
          text: m.text,
          color: m.color,
        })));
        localDraft.dropMarks(missing.map((m) => m.lid));
        syncPending();
        await load(true);
        ok(`补发了 ${missing.length} 笔上次没传上去的批注`);
        return missing.length;
      } catch (e) {
        fail(`补发失败：${e.message}（还在本机，稍后自动重试）`);
        return 0;
      }
    }

    /** 对账：队列里「服务端已经有了」的清掉。
     *
     *  为什么不能发完就删：POST 成功与详情刷新之间有个空窗，
     *  这段时间界面上靠队列里的那条撑着（否则笔迹会闪一下再回来）。 */
    function reconcile() {
      const queue = localDraft.marks();
      if (!queue.length) return;
      const gone = queue
        .filter((m) => marks.value.some((s) => sameMark(s, m)))
        .map((m) => m.lid);
      if (gone.length) {
        localDraft.dropMarks(gone);
        syncPending();
      }
    }

    /** 松手时一次交上来一批 id。**串行删**：SQLite 单写者，并发删会撞锁。
     *  ⚠️ `local:` 开头的是**本机还没传上去的**，从队列里删就行 —— 发给服务端只会 404。 */
    async function eraseMarks(ids) {
      if (!ids || !ids.length) return;
      const locals = ids.filter((x) => isLocalMarkId(x)).map((x) => String(x).slice(6));
      const remotes = ids.filter((x) => !isLocalMarkId(x));
      if (locals.length) {
        localDraft.dropMarks(locals);
        syncPending();
      }
      if (!remotes.length) return;
      try {
        for (const mid of remotes) await gradeApi.removeMark(mid);
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

    /** 撤销：先退**本机还没传上去的**最后一笔，再退服务端的。
     *
     *  为什么先退本机的：它们是"最后画的"（队列是有序追加），而且删本地的不发请求 ——
     *  老师连着画三笔、发现第三笔画歪了，点一下就该立刻消失。
     *  ⚠️ 服务端那批要按 **id 大小**找最后一笔；本机队列的 id 是字符串，混在一起排会乱。 */
    async function undoLast() {
      const locals = pendingMarks.value
        .filter((m) => Number(m.page_no || 1) === pageNo.value && !m.sent);
      if (locals.length) {
        localDraft.dropMark(locals[locals.length - 1].lid);
        syncPending();
        return;
      }
      const mine = serverTeacherMarks.value;
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

    /** 导出带批改痕迹的副本。原件不动 —— 后端画在别的文件上。
     *
     *  ⚠️ 导出读的是**服务端**的标注，本机队列里的还没传上去的那些画不进去。
     *     所以先补发一次再导 —— 老师看到"我刚画的没出现在 PDF 里"只会以为功能坏了。 */
    async function exportFile(fmt) {
      if (!detail.value || !detail.value.total) {
        warn('还没有页图 —— 先点「开始批改」，页图渲染出来才能导出');
        return;
      }
      if (detail.value && pendingUnsent.value) {
        exporting.value = fmt;
        try { await flushPending(); } finally { exporting.value = ''; }
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
      // ⚠️ 框题模式下翻页**不提示未保存**：草稿是整份按页分桶存的（editAll），
      //    翻页只是换一桶看。跨页题本来就是「第 1 页画一半、翻到第 2 页画另一半」，
      //    每翻一次都弹一次确认，等于把这条操作路径堵死。
      pageNo.value = target;
      pageBroken.value = false;
      localDraft.touch({ page_no: target });    // 重开回到上次那一页（一页七八题，找回来很烦）
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

    /* ---------------- 模型回的两个「界面上要用」的信号（0030） ----------------
     * `block_kind` / `tail_note` 是区域模式才有的字段。**只在区域模式下发**，
     * 老记录（整页批的）里是空串 —— 所以这里必须能优雅地什么都不显示。
     *
     * ⚠️ 它们**只当提示，绝不自动跳过**：模型说「这块像卷头」也可能是它读错了，
     *    自动跳过就等于让模型决定「什么不判」，而老师根本不知道它跳了。
     *    所以这里只给一行提示 + 一个「去设成不判」的跳转，决定权还在老师手上。 */
    const BLOCK_KIND_LABEL = {
      header: '卷头 / 页眉', section: '段落标题', footer: '页脚 / 页码',
      blank: '空白', other: '其它（不像题目本体）',
    };
    /** 这块「不像一道题」时返回中文名，否则返回空串（question 和空都算正常题）。 */
    const oddKind = (item) => {
      const k = String((item && item.block_kind) || '').trim();
      return k && k !== 'question' ? (BLOCK_KIND_LABEL[k] || BLOCK_KIND_LABEL.other) : '';
    };
    /** 有框才谈得上「设成不判」—— 整页批的记录里没有块可点。 */
    const hasBlocks = computed(() => regions.value.length > 0);
    /** 从题卡跳到那一页的分割模式，让老师自己点「这块不判」。
     *  不替老师做决定，也不按 seq 猜是哪一块（item.seq 与 region.seq 不同源）。
     *
     *  ⚠️ `setTool` 是**切换**语义（再点一次退出）—— 已经在这个模式里就不能再调，
     *     否则「去设成不判」会把人踢回选择模式，正好反着来（挂载检查抓到的）。 */
    function gotoSkipBlock(item) {
      if (!item) return;
      if (Number(item.page_no) !== pageNo.value) goPage(Number(item.page_no));
      if (editingRegions.value) return;
      setTool(regions.value.some((r) => isFullWidth(r.box)) ? 'line' : 'region');
    }

    onMounted(async () => {
      // 全局粘贴只服务于「判定依据传图」：贴文字时 onPaste 会立刻返回，
      // 不抢 textarea 的正常粘贴（见 onPaste 的说明）。
      window.addEventListener('paste', onPaste);
      await load();
      startPolling();
    });
    onBeforeUnmount(() => {
      // 页面被路由切走时把草稿冲掉 —— 防抖窗口里那两百毫秒不能成为"丢一笔"的口子
      localDraft.flush();
      window.removeEventListener('paste', onPaste);
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
      // 模型回的两个信号（只当提示，不自动跳过）
      oddKind, hasBlocks, gotoSkipBlock,
      setupMode, setupAnswer, setupRubric, setupTouched, setupBusy, setupEditable,
      setupAnswerFiles, setupImages, answerImgCount, uploadAnswerImage, clearAnswerImages,
      touchSetup, saveSetup, onAnswerDrop, onPaste,
      // 手写批改
      tool, inkColor, textDraft, exporting, INK_COLORS, teacherMarks, layerEl,
      setTool, addMark, eraseMarks, onTextAt, saveText, moveMark,
      undoLast, clearPageMarks, exportFile,
      // 本机草稿：待发笔迹 / 补发
      pendingMarks, pendingUnsent, flushPending,
      // 一题一区域（框选 + 分割线）：有段按段批，没段仍整页批
      pageRegions, editRegions, editLines, pageLines, pageBands, lineCount,
      layerRegions, layerLines, regionsDirty, editingRegions,
      regionBlocks, draftSegCount,
      addRegionBox, dropRegion, dropBlock, mergeRegion, setRegionQNo,
      addLine, moveLine, removeLine,
      // 不送 AI（按块 / 按页）+ 每页尾部（结束线）+ 块的缩略图
      toggleBlockSkip, skippedPages, pageSkipped, togglePageSkip,
      pageEnd, pageTail, setPageEnd, resetPageEnd, blockPreview, openPreview,
      saveRegions, copyRegions, syncRegions, clearRegions,
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
        <!-- 本机草稿里还有没送上去的笔迹：**必须说出来**，否则老师会以为「刚才那笔没存上」
             而重画一遍（重画的结果是两条重叠的批注）。 -->
        <div v-if="pendingUnsent" class="grade-draft-tip">
          有 {{ pendingUnsent }} 笔批注还没传上去（已存在本机，不会丢）
          <button class="btn ghost sm" :disabled="busy === 'flush'" @click="flushPending(true)">
            {{ busy === 'flush' ? '补发中…' : '立即补发' }}
          </button>
        </div>
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
        <div v-if="setupMode !== 'ai'" class="field" style="margin-top:8px">
          <label class="small">或者直接传照片
            <span class="muted">（答案页 / 手写细则；<b>Ctrl+V 直接粘贴</b>截图，或把图拖到下面这块）</span></label>
          <!-- 拖拽区：包住整个文件行。dragover 上的 .prevent 是必须的 —— 不拦的话
               浏览器会用「打开这个文件」接管，老师拖进来的图会把整页导航走。 -->
          <div class="grade-ansfile grade-drop" :class="{ off: !setupEditable }"
               @dragover.prevent @drop.prevent="onAnswerDrop">
            <input type="file" :disabled="!setupEditable || setupBusy"
                   @change="uploadAnswerImage">
            <span v-if="answerImgCount" class="small" style="color:var(--success, #16a34a)">
              已附 {{ answerImgCount }} 张
            </span>
            <button v-if="answerImgCount" class="btn ghost sm" :disabled="setupBusy"
                    @click="clearAnswerImages">去掉</button>
            <span v-else class="small muted">或拖一张图进来 · 也可以 Ctrl+V</span>
          </div>
          <div v-if="!setupEditable" class="small muted">已开始批了，依据不能再换。</div>
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
            <!-- 整页不看（0030）：封页、空白作答页、草稿页。**立即生效**，
                 不跟着「保存分割」走 —— 老师说这页不看的那一刻就该不看。 -->
            <button class="btn ghost sm" :class="{ on: pageSkipped }" :disabled="busy === 'pskip'"
                    :title="pageSkipped ? '这一页现在不送 AI，点一下恢复' : '这一页整页不送 AI（框都还留着）'"
                    @click="togglePageSkip">
              {{ pageSkipped ? '本页不判 · 恢复' : '本页不判' }}
            </button>
            <span v-if="pageSkipped" class="tag orange">不送 AI</span>
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
            <button class="g-tool" :class="{ on: tool === 'line' }"
                    title="画分割线逐题切开：在题目之间的空隙点一下就是一刀，比逐题拖框快得多"
                    @click="setTool('line')">逐题分割</button>
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
              : tool === 'line' ? '在题目之间的空隙点一下 = 放一条分割线；按住线可以上下拖微调。相邻两条线之间就是一道题。'
              : tool === 'region' ? '按住拖出一个框，把一道题（题干 + 作答）整个框进去。框宁大勿小 —— 切掉笔画就再也读不回来了。'
              : '在要判的地方点一下，就放一个 ' + (tool === 'check' ? '√' : '×') + '。' }}
          </div>

          <!-- ============ 分割 / 框题面板：两种画法共用一块 ============ -->
          <!-- 数据上它们是同一件事（线切出来就是整页宽的框），所以面板也共用：
               一行 = 一道题，跨页题显示成「跨第 1 + 2 页 · 2 段」。 -->
          <div v-if="editingRegions" class="grade-regions">
            <div class="gr-head">
              <b>{{ tool === 'line' ? '逐题分割' : '框题' }}</b>
              <span class="small muted">
                {{ regionBlocks.length }} 题 · {{ draftSegCount }} 段<template v-if="lineCount"> · {{ lineCount }} 条分割线</template>
              </span>
              <span v-if="regionsDirty" class="small" style="color:var(--warning)">未保存</span>
              <div class="spacer"></div>
              <button class="btn ghost sm" :disabled="busy === 'copy'" @click="copyRegions">
                {{ busy === 'copy' ? '沿用中…' : '沿用上一份' }}
              </button>
              <button class="btn ghost sm" :disabled="!draftSegCount && !lineCount" @click="clearRegions">全清</button>
              <button class="btn sm" :disabled="busy === 'regions'" @click="saveRegions">
                {{ busy === 'regions' ? '保存中…' : '保存分割' }}
              </button>
            </div>
            <div v-if="!regionBlocks.length" class="small muted" style="padding:6px 0">
              <template v-if="tool === 'line'">
                还没分割线 —— 在卷面上<b>逐题点一下</b>，线之间就是一道题。
                第一条线以上也算一段（学生可能从页顶就开始写），多出来的页眉段点「删」就行。
              </template>
              <template v-else>
                还没画框 —— 在卷面上拖出一个框，把一道题（题干 + 作答）整个框进去。
              </template>
              有段的页<b>一段一次</b>判（切得越准，读得越清），没段的页仍然整页判一次。
            </div>
            <div v-else class="gr-list">
              <!-- 一行 = 一道题。**必须带缩略图**：以前这里只有「第 1 页 / 跨第 1+2 页」两句话，
                   切歪了只能等模型判完才知道哪块不对 —— 这就是用户说的「分割截图完后看不到图」。
                   图走 /region-image 现裁（不落盘），**保存前就能看**，改一下坐标 URL 就变。 -->
              <div v-for="b in regionBlocks" :key="'b-' + b.seq" class="gr-row"
                   :class="{ off: b.skipped }">
                <div class="gr-row-top">
                  <span class="gr-no">{{ b.seq + 1 }}</span>
                  <label class="small">题号</label>
                  <input class="gr-q" :value="b.q_no" placeholder="留空就按顺序"
                         @input="setRegionQNo(b, $event.target.value)">
                  <span v-if="b.pages.length > 1" class="gr-span" :title="'两段会竖拼成一张图再判'">
                    跨第 {{ b.pages.join(' + ') }} 页 · {{ b.pages.length }} 段
                  </span>
                  <span v-else class="small muted">第 {{ b.pages[0] }} 页</span>
                  <div class="spacer"></div>
                  <button class="btn ghost sm" :class="{ on: b.skipped }" @click="toggleBlockSkip(b)"
                          :title="b.skipped ? '恢复：这块会重新送去批改' : '这块不送 AI（卷头、结构说明段、重复页上的段）——框还留着'">
                    {{ b.skipped ? '不判 · 恢复' : '这块不判' }}
                  </button>
                  <button v-if="b.seq > 0" class="btn ghost sm" @click="mergeRegion(b)"
                          title="跨页题：并到上一题，两段竖拼成一张图再判">并入上一题</button>
                  <button class="btn ghost sm" @click="dropBlock(b)">删</button>
                </div>
                <div class="gr-thumb">
                  <img :src="blockPreview(b)" loading="lazy" alt="这块切出来的样子"
                       @click="openPreview(b)" title="点一下看大图">
                  <span v-if="b.skipped" class="gr-off-tag">不送 AI</span>
                </div>
              </div>
            </div>
            <!-- 每页尾部：末段切到哪为止。默认到页底（历史行为），
                 拖到 96% 就把页脚 / 页码那一截排除在块之外。 -->
            <div v-if="tool === 'line'" class="gr-tail">
              <span class="small">末段底边</span>
              <input type="range" min="50" max="100" step="1" :value="pageEnd / 10"
                     @input="setPageEnd($event.target.value * 10)">
              <span class="small" :class="{ muted: pageEnd >= 1000 }">{{ pageEnd / 10 }}%</span>
              <span v-if="pageEnd < 1000" class="small muted">
                下面 {{ (1000 - pageEnd) / 10 }}% 不进任何块、不送 AI
              </span>
              <span v-else class="small muted">一直切到页底</span>
              <div class="spacer"></div>
              <button class="btn ghost sm" @click="setPageEnd(960)"
                      title="页脚 / 页码通常在底部 4% 内">截掉页脚（96%）</button>
              <button v-if="pageEnd < 1000" class="btn ghost sm" @click="resetPageEnd">到页底</button>
            </div>
            <div class="small muted" style="margin-top:6px">
              题号由段绑定 —— 学生把「第 3 题」写成「第 8 题」也不会判错归属。
              <b>跨页题</b>：在第 1 页切上半段、翻到第 2 页切下半段，再点下面那行的
              「并入上一题」合成一道（两段会竖拼成一张图）。
              <b>弄完一定要点「保存分割」</b>，然后重新批改。
              没保存的也不会丢 —— 已经存在本机，退出再回来还在。
            </div>
          </div>

          <div class="grade-page">
            <img class="grade-page-img" :src="pageUrl" loading="lazy" alt="作业原图" draggable="false"
                 @load="pageBroken = false" @error="pageBroken = true">
            <!-- ⚠️ 成对写，别写成自闭合：零构建的模板里自闭合会把后面的节点吞掉 -->
            <GradeMarkLayer ref="layerEl" :marks="pageMarks" :selected-item-id="selectedItemId"
                            :mode="tool" :color="inkColor"
                            :regions="layerRegions" :lines="layerLines" :bands="pageBands"
                            :tail="pageTail"
                            @pick="onPick" @create="addMark" @erase="eraseMarks"
                            @text-at="onTextAt" @stamp="addMark" @move="moveMark"
                            @region="addRegionBox" @region-remove="dropRegion"
                            @add-line="addLine" @move-line="moveLine"
                            @remove-line="removeLine"></GradeMarkLayer>
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

              <!-- 模型说「这块不像一道题」。**只提示、不自动跳过** —— 它也可能读错，
                   让模型决定"什么不判"而老师不知道，是最糟的一种自动化。 -->
              <div v-if="oddKind(item)" class="g-odd">
                <span class="tag orange">{{ oddKind(item) }}</span>
                <span class="small">
                  模型认为这一块不是题目本体，判出来多半是噪音
                  <template v-if="hasBlocks">—— 可以在框题里点「这块不判」，框会留着</template>
                </span>
                <button v-if="hasBlocks" class="btn ghost sm" @click.stop="gotoSkipBlock(item)">
                  去设成不判
                </button>
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

              <!-- 模型报的「底部有问题」。这是「每页尾部处理不到位」的另一半：
                   老师自己看不出末段被切掉了一角，模型说出来才知道要回去调结束线。
                   三种情况（页脚占了 / 被页切掉 / 有邻题残字）都用同一句话提示。 -->
              <div class="g-sec" v-if="item.tail_note">
                <div class="g-lb">这一块的底部</div>
                <div class="g-tail-note">{{ item.tail_note }}</div>
                <div class="small muted" style="margin-top:4px">
                  多半是页脚 / 页码占了地方，或者题目被页边界切断了 ——
                  到分割模式把这一页的「末段底边」往上收一点，翻页看看下面还有没有内容。
                </div>
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
