// 出卷：左边筛题库选题，右边攒成一份卷子并导出。
//
// 选题有三种方式，都通向同一个 picked：
//   · 单题「加入」           —— 挑一两道
//   · 「勾选本页」+「完成」  —— 批量选题（勾选跨页保留，存的是题目对象不是 id）
//   · 「本页全部加入」       —— 整页打包
// 发现某题没答案/录入有问题时，行上的「修」会跳到录题页的补答案模式并定位到它
// （`/entry.html?answer=1&qid=…`），新标签页打开，出卷页攒的题目不会丢。
//
// 为什么筛选用新的 /api/questions/search 而不是复用 GET /questions：
// 后者是录题页正在用的、返回纯数组的旧接口；出卷要多条件 + 分页 + 「共 N 题」，
// 诉求不同，硬改那个接口会把录题页一起弄坏。
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue';
import { curriculumApi, papersApi, paperTemplatesApi } from '../api.js';
import { fail, loadCurricula, ok, state } from '../store.js';
import { ensureLibs, renderRichEl } from '../mathRender.js';
import { matchTags, tagQueryHint } from '../tagFilter.js';
import { chooseForm, imageNote, stemImage, NO_STEM_NOTE } from '../questionForm.js';

const QTYPES = ['选择题', '填空题', '解答题', '判断题', '证明题', '应用题', '图片题'];
const DIFFS = ['基础', '中档', '拔高'];
const PAGE = 20;

// 下拉里的「全部」统一用空串：qs() 会把空串过滤掉，不用再转 undefined
// 注意 tags 是数组，reset 时要重新给一份空数组（不然会共用同一个引用）
const EMPTY_FILTER = {
  keyword: '', curriculum_id: '', kp: '',
  qtype: '', difficulty: '', has_image: false, with_answer: false, sort: 'created', tags: [],
};

function todayTitle() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} 练习`;
}

export default {
  name: 'Papers',
  setup() {
    const filter = reactive({ ...EMPTY_FILTER });
    const rows = ref([]);
    const total = ref(0);
    const offset = ref(0);
    const loading = ref(false);
    const kpGroups = ref([]);         // [{label, options:[{value,label}]}] 所有体系的知识点
    const tagOptions = ref([]);       // 库里实际用过的标签
    const picked = ref([]);           // 已选题目，顺序即卷面顺序
    // 正在预览的题（弹层）。**必须在这里声明**：paintRich() 和下面的 watch
    // 都要引用它，声明放在「预览那一节」（靠后）就是 TDZ —— setup 直接抛
    // `Cannot access 'preview' before initialization`，整页空白。
    const preview = ref(null);
    const title = ref(todayTitle());
    const templates = ref([]);        // 卷种样式（四套内置 + 自己复制的）
    const templateId = ref('');       // '' = 用默认样式（跟加样式功能之前一致）
    const opts = reactive({
      show_answer: true, show_analysis: false, show_tags: true, show_meta: true,
      // 题目呈现：整卷覆盖「每道题自己存的偏好」（录题页「出卷时用」那个）。
      // auto = 各按各的走，与加这个选项之前的行为一致。
      render_mode: 'auto',
    });

    /** 拉卷种样式。失败不阻塞出卷 —— 拿不到模板就只能用默认样式，
     *  这比「整页打不开」好得多，所以这里只提示、不抛。 */
    async function loadTemplates() {
      try {
        const r = await paperTemplatesApi.list();
        templates.value = r.items || [];
      } catch (e) {
        fail(`卷种样式加载失败（仍可用默认样式出卷）：${e.message}`);
      }
    }

    /** 当前选中的模板对象，用来在下拉下面显示抬头预览。 */
    const currentTemplate = computed(
      () => templates.value.find(t => t.id === templateId.value) || null,
    );

    /** 「题目呈现」那一行的解释。三种选择各说各的话 ——
     *  老师最怕的是「我选了图片，怎么这道题是空的」，所以每条都在讲退回规则。 */
    const renderModeHint = computed(() => ({
      auto: '按每道题自己存的偏好走（录题时「出卷时用」选的那个）：有文本用文本、没文本用图。',
      text: '整卷都印识别出来的文本；哪道题没有文本，会自动退回印它的原貌图，不会印出空白题。',
      image: '整卷都印原貌图；哪道题没截过图，会自动退回印文本。',
    }[opts.render_mode]));

    /** 知识点选项：各体系的节点（按体系分组，带 cid）+ 题库里已用过的名字。
     *
     * 选项值用**名字**（不是 node id），后端会同时匹配「文本知识点」和「同名节点子树」。
     *
     * 展示规则（2026-11-12 用户点名要的）：**选了体系就只显示这个体系的知识点**。
     * 之前是不管选哪个体系都把全部体系的节点摊在一个下拉里（当时的顾虑是「只有国内
     * 高中数学有知识树，跟着体系走的话选了初中就是空下拉」），但这样选「DSE 数学」
     * 会看到一堆国内高中节点，挑起来更费劲。空体系的问题改成**给提示**而不是摊全表。
     * `cid` 就是用来做这件事的：`shownKpGroups` 按它过滤。 */
    async function loadKpOptions() {
      const groups = [];
      try {
        const used = await papersApi.knowledgePoints();
        if (used.items.length) {
          // cid 为 null = 不分体系（题库里已经用过的名字，不知道它属于哪棵树）
          groups.push({ cid: null, label: '题库里已用过的', options: used.items.map(i => ({ value: i.kp, label: i.kp })) });
        }
        for (const c of state.curricula || []) {
          const tree = await curriculumApi.nodes(c.id);
          const options = [];
          const walk = (list, depth) => (list || []).forEach((n) => {
            options.push({ value: n.name, label: '\u3000'.repeat(depth) + n.name });
            walk(n.children, depth + 1);
          });
          walk(tree, 0);
          if (options.length) groups.push({ cid: c.id, label: c.name, options });
        }
      } catch (e) {
        fail(e.message);
      }
      kpGroups.value = groups;
    }

    /** 下拉里真正列出来的组：选了体系就只剩这个体系的（「题库里已用过的」也跟着收起来 ——
     *  它可能含别的体系的同名词，摊在 DSE 卷里会让人选错；要按名字搜的话用关键词框）。 */
    const shownKpGroups = computed(() => {
      const cid = filter.curriculum_id;
      if (!cid) return kpGroups.value;
      return kpGroups.value.filter(g => g.cid !== null && String(g.cid) === String(cid));
    });

    /** 当前体系一个知识点都没有时，下拉要给话说，而不是一个空壳。 */
    const kpHintWhenEmpty = computed(() => {
      if (!filter.curriculum_id) return '';
      if (shownKpGroups.value.length) return '';
      const c = (state.curricula || []).find(x => String(x.id) === String(filter.curriculum_id));
      return `${c ? c.name : '这个体系'}还没有知识点树 —— 可以直接用上面的「关键词」搜，`
        + '或者到「设置 → 知识点体系」给它加节点。';
    });

    async function search(reset = true) {
      if (reset) offset.value = 0;
      loading.value = true;
      try {
        const r = await papersApi.search({
          keyword: filter.keyword.trim(),
          curriculum_id: filter.curriculum_id,
          kp: filter.kp,
          qtype: filter.qtype,
          difficulty: filter.difficulty,
          // 多选标签 = 任一命中（后端也按这个语义）
          tags: filter.tags.length ? filter.tags.join(',') : undefined,
          has_image: filter.has_image ? true : undefined,
          with_answer: filter.with_answer ? true : undefined,
          sort: filter.sort,
          limit: PAGE,
          offset: offset.value,
        });
        rows.value = r.items;
        total.value = r.total;
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    async function loadTags() {
      try {
        const r = await papersApi.tags();
        tagOptions.value = r.items;
      } catch (e) {
        fail(e.message);
      }
    }

    /** 标签多选：点一下切一个，点已选中的取消。 */
    function toggleTag(t) {
      const i = filter.tags.indexOf(t);
      if (i >= 0) filter.tags.splice(i, 1); else filter.tags.push(t);
    }

    /* ---------- 标签搜索（标签会越攒越多，光靠眼睛在这一排里找不现实） ----------
     *
     *  两种写法，**和常见直觉相反**（用户点名要的，别好心「修」回去）：
     *    · 空格 = 任一命中：「对称 函数」→ 含对称的 ＋ 含函数的
     *    · 竖线 = 同时包含：「对称|函数」→ 标签里两个词都有
     *  语义与理由集中在 src/tagFilter.js（那边有 node 单测兜着）。 */
    const tagQuery = ref('');
    const shownTags = computed(() => matchTags(tagOptions.value, tagQuery.value));
    const tagHint = computed(() => tagQueryHint(tagQuery.value));
    /** 搜索把已选标签挡住了几个 —— 不提示的话老师会以为「我选的那几个没了」。 */
    const hiddenPicked = computed(
      () => filter.tags.filter((t) => !shownTags.value.some((o) => o.tag === t)).length,
    );

    function resetFilter() {
      Object.assign(filter, { ...EMPTY_FILTER, tags: [] });
      tagQuery.value = '';        // 重置就全清，别留下一个「看不见的过滤器」
      search();
    }

    /* ---------- 题干渲染：老师要看的是真公式，不是 $…$ 原文 ----------
     *
     *  ⚠️ 为什么是「拿元素 ref 自己写 innerHTML」，而不是 `{{ q.content }}` 或 v-html：
     *    KaTeX 的 auto-render 会把容器里的文本节点整个换掉，Vue 之后还往那个
     *    已摘除的节点里写新内容 —— 界面会永远停在第一次渲染的那一版。
     *    （笔记页在同一个坑里躺过，详见 src/mathRender.js 的注释。）
     */
    const richEls = new Map();          // 'row:<id>' / 'pick:<id>' → 容器元素
    function setRichEl(key, el) {
      if (el) richEls.set(key, el); else richEls.delete(key);
    }
    function paintRich() {
      rows.value.forEach((q) => {
        const el = richEls.get('row:' + q.id);
        if (el) renderRichEl(el, q.content || '');
      });
      picked.value.forEach((q) => {
        const el = richEls.get('pick:' + q.id);
        if (el) renderRichEl(el, q.content || '');
      });
      // 预览弹层：题干 / 答案 / 解析三处都要渲染公式 —— 老师点开就是为了看清这题，
      // 看到一片 `$…$` 就等于没预览。
      const pv = preview.value;
      if (pv) {
        [['pv:', 'content'], ['pva:', 'answer'], ['pvp:', 'analysis']].forEach(([pre, key]) => {
          const el = richEls.get(pre + pv.id);
          if (el) renderRichEl(el, pv[key] || '');
        });
      }
    }
    let libsReady = false;
    async function renderRich() {
      if (!libsReady) {
        paintRich();                     // 库还没到：先把原文铺出来，别留一屏空白
        try { await ensureLibs(); libsReady = true; }
        catch (e) { return; }            // 加载不上就一直显示原文，不挡着出卷
      }
      await nextTick();                  // 等 v-for 把新一页的容器挂上去
      paintRich();
    }
    // ⚠️ 监视里必须带上 render_mode：切「题目呈现」时 rows / picked **一行都没变**，
    //    但行里的容器换了一批（文本 ↔ 图），不重渲染就会看着是空的。
    watch([rows, picked, preview, () => opts.render_mode], () => { renderRich(); }, { flush: 'post' });

    /* ---------- 列表里的题目按「会印成什么」显示 ----------
     *
     *  以前写死 `v-if="q.content"`：没识别出文本的题在列表里只剩一句「（图片题，没有文本）」，
     *  老师认不出是哪道题，只能盲挑。现在走 src/questionForm.js 的判定 ——
     *  与后端印卷规则逐条一致（该文本就渲染文本，该图就显示原貌图），
     *  顺带把「卷子上会是空白」的题（文字和图都没有）在挑题时就点出来。
     */
    const formOf = (q) => chooseForm(q, opts.render_mode);

    const pickedIds = computed(() => picked.value.map((q) => q.id));
    const isPicked = (q) => pickedIds.value.includes(q.id);

    function add(q) {
      if (!isPicked(q)) picked.value.push(q);
    }
    function addPage() {
      const before = picked.value.length;
      rows.value.forEach(add);
      const n = picked.value.length - before;
      ok(n ? `已加入 ${n} 道` : '本页都已在卷子里了');
    }

    /* ---------- 批量选题：勾几个 → 完成 → 一起进卷子 ----------
     *
     * 用户 2026-11-12 要的流程：批量选题 → 方框勾选 → 完成 → 所选题目进入试卷。
     * 存**题目对象**而不是 id：翻页后 `rows` 换了一批，只留 id 的话「完成」时
     * 拿不到那些题的题干/答案，卷子里就会出现空条目。
     */
    // { [id]: q }。**顺序就是勾选顺序** —— 题目的 id 是 12 位十六进制串（不是纯数字），
    // 所以 Object 的键不会被当成数组下标重排。哪天 id 改成自增整数，这里要换成 Map，
    // 否则「进卷子的顺序」会静默变成 id 升序。
    const checked = ref({});
    const checkedList = computed(() => Object.values(checked.value));
    const checkedCount = computed(() => checkedList.value.length);

    function isChecked(q) { return !!checked.value[q.id]; }

    function toggleCheck(q) {
      const m = { ...checked.value };
      if (m[q.id]) delete m[q.id]; else m[q.id] = q;
      checked.value = m;
    }
    function clearChecked() { checked.value = {}; }

    /** 本页没入卷的题一次全勾上（原来的「本页全选」升级成这个）。 */
    function checkPage() {
      const m = { ...checked.value };
      let n = 0;
      rows.value.forEach((q) => { if (!isPicked(q) && !m[q.id]) { m[q.id] = q; n += 1; } });
      checked.value = m;
      ok(n ? `勾上了本页 ${n} 道` : '本页的题都已经在卷子里了');
    }

    /** 完成：勾的题一次性进卷子。已经在卷子里的跳过（不重复加）。 */
    function finishBatch() {
      const list = checkedList.value.filter(q => !isPicked(q));
      list.forEach(add);
      checked.value = {};
      ok(list.length ? `已把 ${list.length} 道题加入卷子` : '勾的题都已经在卷子里了');
    }
    function remove(id) {
      picked.value = picked.value.filter((q) => q.id !== id);
    }
    function clearPicked() {
      picked.value = [];
    }
    function move(i, delta) {
      const j = i + delta;
      if (j < 0 || j >= picked.value.length) return;
      const arr = picked.value.slice();
      [arr[i], arr[j]] = [arr[j], arr[i]];
      picked.value = arr;
    }

    /* ---------- 预览：挑题时光看题干不够，答案和解析也得看得见 ----------
     *
     * 用户 2026-11-12：「已选的题目需要能够预览，这样才能进行筛选」。
     * 以前已选行只印题干，答案/解析要等导出教师版才看得到 —— 而「这道题没答案」
     * 正是最该在选题阶段发现的问题（本轮第 5 条就是从这儿来的）。
     */
    function showPreview(q) {
      if (!q) return;
      preview.value = q;
    }
    function closePreview() {
      preview.value = null;
    }
    /** 在预览里直接把这题从卷子里拿掉。 */
    function removeFromPreview() {
      if (!preview.value) return;
      const id = preview.value.id;
      remove(id);
      const m = { ...checked.value };
      delete m[id];
      checked.value = m;
      preview.value = null;
      ok('已从卷子里移除');
    }

    /** 「去修这题」：跳到录题平台的补答案模式并定位到这道题。
     *
     *  ⚠️ 新标签页打开 —— 出卷页攒的选题还没导出，跳走就全丢了。
     *  目标页的接线在 `entry.html` 的 `enterAnswerFromUrl()`。
     */
    function fixUrl(q) {
      return '/entry.html?answer=1&qid=' + encodeURIComponent(String(q.id));
    }

    /** 这题缺什么 —— 列表上用一个小角标点出来，省得每题都点开看。 */
    function gapOf(q) {
      const noAns = !(q.answer || '').trim() && !q.answer_image;
      const noStem = !(q.content || '').trim() && !q.image;
      if (noStem) return '没有题干';
      if (noAns) return '没有答案';
      return '';
    }

    const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE)));
    const pageNo = computed(() => Math.floor(offset.value / PAGE) + 1);
    function prev() {
      if (offset.value <= 0) return;
      offset.value -= PAGE;
      search(false);
    }
    function next() {
      if (offset.value + PAGE >= total.value) return;
      offset.value += PAGE;
      search(false);
    }

    /** 预览/导出：一律新开标签页。
     *  HTML 后端按 inline 发（直接看，Ctrl+P 存 PDF），Word/PDF 按 attachment 发（浏览器下载）。
     *
     * ⚠️ show_answer / show_analysis **必须显式传 "false"**，不能「不加这个参数」：
     * 服务端 show_answer 的默认值是 true，不传 = 出教师版。
     * 之前这里写的是 `opts.show_answer ? true : undefined`，于是「学生版」其实带着答案 ——
     * 而且学生在文件里看得到（答案是真写在文件里的，不是隐藏）。
     */
    function open(kind) {
      if (!picked.value.length) {
        fail('还没选题目');
        return;
      }
      window.open(papersApi.exportUrl({
        ids: pickedIds.value.join(','),
        format: kind,
        title: title.value,
        // 不选模板 = 不传这个参数 = 服务端用默认样式，与加模板功能之前完全一致
        template_id: templateId.value || undefined,
        show_answer: String(opts.show_answer),
        show_analysis: String(opts.show_answer && opts.show_analysis),
        show_tags: String(opts.show_tags),
        show_meta: String(opts.show_meta),
        render_mode: opts.render_mode,
      }), '_blank');
    }

    /** 切版本时顺手把「答案带解析」也归位：学生版没有答案，也就谈不上解析。 */
    function setVersion(withAnswer) {
      opts.show_answer = withAnswer;
      if (withAnswer) opts.show_analysis = true;      // 教师版默认带上解析（可再关）
    }

    /** 换体系后，原来选中的知识点可能根本不在这个体系里 —— **必须清掉**。
     *
     *  留着它的后果和标签搜索那个「看不见的过滤器」一模一样：下拉里看不到这个选项
     *  （因为已经按体系过滤掉了），但筛选条件还在生效，结果莫名其妙地少一大半。
     */
    function dropKpIfOutOfScope() {
      if (!filter.curriculum_id || !filter.kp) return;
      const names = new Set();
      shownKpGroups.value.forEach(g => g.options.forEach(o => names.add(o.value)));
      if (!names.has(filter.kp)) {
        filter.kp = '';
        ok('换体系后，原来选的知识点不在这个体系里，已清空');
      }
    }

    // 条件一改就重筛 —— 让老师每改一次都再点一下「筛选」太多余。
    watch(
      () => [filter.curriculum_id, filter.kp, filter.qtype, filter.difficulty,
             filter.has_image, filter.with_answer, filter.sort, filter.tags.join(',')],
      () => { dropKpIfOutOfScope(); search(); },
    );
    // 关键词单独防抖：不能每敲一个字就发一次请求
    let kwTimer = null;
    watch(() => filter.keyword, () => {
      clearTimeout(kwTimer);
      kwTimer = setTimeout(() => search(), 400);
    });

    onMounted(async () => {
      await loadCurricula();
      loadKpOptions();
      loadTags();
      loadTemplates();
      search();
    });

    return {
      state, filter, rows, total, loading, kpGroups, picked, pickedIds,
      title, opts, QTYPES, DIFFS, pageNo, pageCount, tagOptions,
      templates, templateId, currentTemplate, renderModeHint,
      tagQuery, shownTags, tagHint, hiddenPicked, setRichEl,
      formOf, stemImage, imageNote, NO_STEM_NOTE,
      search, resetFilter, add, addPage, remove, clearPicked, move,
      isPicked, open, prev, next, toggleTag, setVersion,
      shownKpGroups, kpHintWhenEmpty,
      checked, checkedCount, isChecked, toggleCheck, clearChecked, checkPage, finishBatch,
      preview, showPreview, closePreview, removeFromPreview, fixUrl, gapOf,
    };
  },
  template: `
  <div>
    <div class="page-head">
      <h1>出卷</h1>
      <span class="sub">从题库筛题、排好顺序，导出 HTML / Word / PDF</span>
    </div>

    <div class="grid" style="grid-template-columns: minmax(0, 1.7fr) minmax(0, 1fr)">
      <div>
        <div class="card">
          <h2>筛选</h2>
          <div class="row" style="align-items:flex-start">
            <div class="field">
              <label>关键词（题干 / 答案 / 来源）</label>
              <input type="text" v-model="filter.keyword" @keyup.enter="search()" placeholder="如：菱形、导数">
            </div>
            <div class="field">
              <label>体系</label>
              <select v-model="filter.curriculum_id">
                <option value="">全部体系</option>
                <option v-for="c in state.curricula" :key="c.id" :value="c.id">{{ c.name }}</option>
              </select>
            </div>
            <div class="field">
              <label>知识点（含子节点）{{ filter.curriculum_id ? '· 只列当前体系' : '· 选了体系就只列该体系' }}</label>
              <select v-model="filter.kp">
                <option value="">全部知识点</option>
                <optgroup v-for="g in shownKpGroups" :key="g.label" :label="g.label">
                  <option v-for="n in g.options" :key="n.value" :value="n.value">{{ n.label }}</option>
                </optgroup>
              </select>
              <p v-if="kpHintWhenEmpty" class="muted"
                 style="font-size:12px;margin:6px 0 0;line-height:1.6">{{ kpHintWhenEmpty }}</p>
            </div>
          </div>

          <div class="row" style="align-items:flex-start">
            <div class="field">
              <label>题型</label>
              <div class="chips">
                <span class="chip" :class="{ on: !filter.qtype }" @click="filter.qtype = ''">不限</span>
                <span class="chip" v-for="t in QTYPES" :key="t"
                      :class="{ on: filter.qtype === t }" @click="filter.qtype = t">{{ t }}</span>
              </div>
            </div>
            <div class="field">
              <label>难度</label>
              <div class="chips">
                <span class="chip" :class="{ on: !filter.difficulty }" @click="filter.difficulty = ''">不限</span>
                <span class="chip" v-for="d in DIFFS" :key="d"
                      :class="{ on: filter.difficulty === d }" @click="filter.difficulty = d">{{ d }}</span>
              </div>
            </div>
          </div>

          <div class="row" style="align-items:flex-start">
            <div class="field">
              <label>标签（多选 = 任一命中，括号里是题数）</label>
              <input type="text" v-model="tagQuery"
                     placeholder="搜标签：空格分隔 = 任一命中（对称 函数）；竖线分隔 = 同时包含（对称|函数）">
              <div class="chips" style="max-height:150px;overflow:auto;margin-top:6px">
                <span class="chip" :class="{ on: !filter.tags.length }" @click="filter.tags = []">不限</span>
                <span class="chip" v-for="t in shownTags" :key="t.tag"
                      :class="{ on: filter.tags.includes(t.tag) }" @click="toggleTag(t.tag)">
                  {{ t.tag }} <span class="muted">{{ t.count }}</span>
                </span>
                <span v-if="!tagOptions.length" class="muted" style="font-size:12px">
                  还没有标签 —— 到录题页给题目打上标签，这里就会出现
                </span>
                <span v-else-if="!shownTags.length" class="muted" style="font-size:12px">
                  没有匹配的标签
                </span>
              </div>
              <p v-if="tagHint" class="muted" style="font-size:12px;margin:6px 0 0">
                {{ tagHint }}<template v-if="hiddenPicked">；有 {{ hiddenPicked }} 个已选标签被搜索挡住了，见下面「已选」</template>
              </p>
              <!-- 已选单独列一行：搜索能把它藏起来，但**绝不能让人取消不掉** -->
              <div v-if="filter.tags.length" class="chips" style="margin-top:6px">
                <span class="muted" style="font-size:12px">已选：</span>
                <span class="chip on" v-for="t in filter.tags" :key="'pk' + t"
                      title="点一下取消这个标签" @click="toggleTag(t)">{{ t }} ✕</span>
              </div>
            </div>
          </div>

          <div class="row" style="align-items:flex-start">
            <div class="field">
              <label>其他</label>
              <div class="chips">
                <span class="chip" :class="{ on: filter.has_image }"
                      @click="filter.has_image = !filter.has_image">只看有图的</span>
                <span class="chip" :class="{ on: filter.with_answer }"
                      @click="filter.with_answer = !filter.with_answer">只看有答案的</span>
              </div>
            </div>
            <div class="field">
              <label>排序</label>
              <select v-model="filter.sort">
                <option value="created">最新入库优先</option>
                <option value="oldest">最早入库优先</option>
                <option value="usage">少用过的优先（避免重复出题）</option>
              </select>
            </div>
          </div>

          <div style="display:flex;gap:8px">
            <button class="btn primary" :disabled="loading" @click="search()">
              {{ loading ? '筛选中…' : '筛选' }}
            </button>
            <button class="btn" @click="resetFilter">重置</button>
          </div>
        </div>

        <div class="card">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
            <h2 style="margin:0">题库结果</h2>
            <span class="tag">共 {{ total }} 题</span>
            <div style="flex:1"></div>
            <button class="btn sm" :disabled="!rows.length" @click="checkPage">勾选本页</button>
            <button class="btn sm ghost" :disabled="!rows.length" @click="addPage">本页全部加入</button>
          </div>
          <!-- 批量选题：勾几个 → 完成 → 一起进卷子（翻页后勾选还在） -->
          <div v-if="checkedCount"
               style="display:flex;align-items:center;gap:10px;margin:0 0 10px;padding:8px 12px;
                      background:var(--primary-weak,#eef4ff);border-radius:8px">
            <span>已勾 <b>{{ checkedCount }}</b> 题</span>
            <div style="flex:1"></div>
            <button class="btn sm primary" @click="finishBatch">完成（加入卷子）</button>
            <button class="btn sm ghost" @click="clearChecked">取消勾选</button>
          </div>
          <div v-if="!rows.length" class="empty">没有符合条件的题目</div>
          <div v-for="q in rows" :key="q.id" class="lesson-row">
            <input type="checkbox" :checked="isChecked(q)" :disabled="isPicked(q)"
                   @change="toggleCheck(q)" style="margin-top:3px;flex:none;width:16px;height:16px"
                   :title="isPicked(q) ? '已经在卷子里了' : '勾上，最后一起加入'">
            <div style="flex:1;min-width:0">
              <!-- 题干走渲染管线（见 setup 里 renderRich 的注释）：老师要看的是排好版的
                   公式，不是 $…$ 原文。容器里**不能**再放 Vue 插值 —— KaTeX 会把
                   文本节点换掉。
                   展示哪种形态由 questionForm.chooseForm 定（= 后端印卷的同一条规则）：
                   没识别出文本的题在这里显示**原貌图**，不再是「（图片题，没有文本）」。 -->
              <template v-if="formOf(q) === 'text'">
                <div class="q-rich" :ref="(el) => setRichEl('row:' + q.id, el)"></div>
              </template>
              <template v-else-if="formOf(q) === 'image'">
                <a class="q-thumb-link" :href="stemImage(q)" target="_blank" rel="noopener"
                   title="点开看原图">
                  <img class="q-thumb" :src="stemImage(q)" alt="题目原貌图"
                       loading="lazy" decoding="async">
                </a>
                <div class="small muted" style="margin-top:4px">{{ imageNote(q, opts.render_mode) }}</div>
              </template>
              <div v-else class="muted small">{{ NO_STEM_NOTE }}</div>
              <div style="margin-top:5px;display:flex;gap:5px;flex-wrap:wrap">
                <span class="tag blue">{{ q.qtype }}</span>
                <span class="tag">{{ q.difficulty }}</span>
                <span v-for="t in (q.tags || [])" :key="'tag' + t" class="tag">🏷 {{ t }}</span>
                <span v-if="q.image" class="tag green">有图</span>
                <span v-if="q.answer || q.answer_image" class="tag">有答案</span>
                <span v-if="q.usage_count" class="tag orange">用过 {{ q.usage_count }} 次</span>
                <!-- 缺题干/缺答案是选题阶段最该看见的两件事：印出来才发现就晚了 -->
                <span v-if="gapOf(q)" class="tag red">{{ gapOf(q) }}</span>
              </div>
            </div>
            <button class="btn sm ghost" @click="showPreview(q)" title="看完整题干和答案解析">看</button>
            <a class="btn sm ghost" :href="fixUrl(q)" target="_blank" rel="noopener"
               :title="gapOf(q) ? '去录题页修这道题（缺' + gapOf(q) + '）' : '去录题页修这道题'">修</a>
            <button class="btn sm" :disabled="isPicked(q)" @click="add(q)">
              {{ isPicked(q) ? '已加入' : '加入' }}
            </button>
          </div>
          <div style="display:flex;align-items:center;gap:10px;margin-top:12px">
            <button class="btn sm" :disabled="pageNo <= 1" @click="prev">上一页</button>
            <span class="muted">第 {{ pageNo }} / {{ pageCount }} 页</span>
            <button class="btn sm" :disabled="pageNo >= pageCount" @click="next">下一页</button>
          </div>
        </div>
      </div>

      <div>
        <div class="card">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
            <h2 style="margin:0">已选</h2>
            <span class="tag blue">{{ picked.length }} 题</span>
            <div style="flex:1"></div>
            <button class="btn sm ghost" :disabled="!picked.length" @click="clearPicked">清空</button>
          </div>

          <div class="field">
            <label>卷名</label>
            <input type="text" v-model="title">
          </div>

          <div class="field">
            <label>卷种样式</label>
            <select v-model="templateId">
              <option value="">默认样式（不套模板）</option>
              <option v-for="t in templates" :key="t.id" :value="t.id">
                {{ t.name }}{{ t.is_builtin ? '' : '（自定义）' }}
              </option>
            </select>
            <p v-if="currentTemplate" class="muted"
               style="font-size:12px;margin:6px 0 0;line-height:1.6">
              <template v-if="currentTemplate.paper.subtitle">{{ currentTemplate.paper.subtitle }}<br></template>
              <template v-if="currentTemplate.paper.exam_note">{{ currentTemplate.paper.exam_note }}<br></template>
              <template v-if="currentTemplate.sections.length">分 {{ currentTemplate.sections.length }} 个区：{{
                currentTemplate.sections.map(s => s.title).join(' / ') }}</template>
              <template v-if="currentTemplate.sample">示例：{{ currentTemplate.sample }}</template>
            </p>
          </div>

          <div class="field">
            <label>题目呈现</label>
            <div class="chips">
              <span class="chip" :class="{ on: opts.render_mode === 'auto' }"
                    @click="opts.render_mode = 'auto'"
                    title="按每道题自己存的偏好走">自动（按每题）</span>
              <span class="chip" :class="{ on: opts.render_mode === 'text' }"
                    @click="opts.render_mode = 'text'"
                    title="整卷都印识别出来的文本">全部用文本</span>
              <span class="chip" :class="{ on: opts.render_mode === 'image' }"
                    @click="opts.render_mode = 'image'"
                    title="整卷都印原貌截图">全部用图片</span>
            </div>
            <p class="muted" style="font-size:12px;line-height:1.6;margin:6px 0 0">
              {{ renderModeHint }}
            </p>
          </div>

          <div class="field">
            <label>版本</label>
            <div class="chips">
              <span class="chip" :class="{ on: !opts.show_answer }"
                    @click="setVersion(false)"
                    title="给学生的那份：答案一个字都不会写进文件">学生版（不含答案）</span>
              <span class="chip" :class="{ on: opts.show_answer }"
                    @click="setVersion(true)"
                    title="你自己用的那份：末尾附参考答案">教师版（含答案）</span>
            </div>
          </div>

          <div class="field">
            <label>其它</label>
            <div class="chips">
              <span class="chip" :class="{ on: opts.show_analysis, off: !opts.show_answer }"
                    :style="opts.show_answer ? '' : 'opacity:.4'"
                    @click="opts.show_answer && (opts.show_analysis = !opts.show_analysis)"
                    title="教师版才有解析；学生版连答案都不写">答案带解析</span>
              <span class="chip" :class="{ on: opts.show_tags }"
                    @click="opts.show_tags = !opts.show_tags">标注题型·难度·标签</span>
              <span class="chip" :class="{ on: opts.show_meta }"
                    @click="opts.show_meta = !opts.show_meta">留姓名日期栏</span>
            </div>
          </div>

          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
            <button class="btn primary" :disabled="!picked.length" @click="open('html')"
                    :title="opts.show_answer ? '含答案' : '不含答案'">
              预览 / 打印{{ opts.show_answer ? '（教师版）' : '（学生版）' }}
            </button>
            <button class="btn" :disabled="!picked.length" @click="open('docx')">下载 Word</button>
            <button class="btn" :disabled="!picked.length" @click="open('pdf')">下载 PDF</button>
          </div>
          <p class="muted" style="font-size:12px;line-height:1.6;margin:0 0 12px">
            文件名会带版本（<code>…_学生版.pdf</code> / <code>…_教师版.pdf</code>）——
            两份同名文件是「把教师版发给学生」的头号原因。<br>
            <b>卷种样式</b>决定抬头、字号、页边距、题号形态，以及按题型/难度分区
            （高考的「一、选择题」、DSE 的「Section A(1)」）——同一批题换个样式就是另一份卷子。<br>
            「预览 / 打印」出的是 HTML：公式在浏览器里渲染好，Ctrl+P 直接存成 PDF。<br>
            Word / PDF 由服务端排版，<b>公式会显示成 $…$ 原文</b>（服务端没有 LaTeX 引擎）。
          </p>

          <div v-if="!picked.length" class="empty">还没有选题</div>
          <div v-for="(q, i) in picked" :key="q.id" class="lesson-row">
            <span class="muted" style="width:20px;flex:none">{{ i + 1 }}</span>
            <div style="flex:1;min-width:0">
              <template v-if="formOf(q) === 'text'">
                <div class="q-rich q-rich-sm" :ref="(el) => setRichEl('pick:' + q.id, el)"></div>
              </template>
              <a v-else-if="formOf(q) === 'image'" class="q-thumb-link"
                 :href="stemImage(q)" target="_blank" rel="noopener" title="点开看原图">
                <img class="q-thumb q-thumb-sm" :src="stemImage(q)" alt="题目原貌图"
                     loading="lazy" decoding="async">
              </a>
              <div v-else class="muted small">{{ NO_STEM_NOTE }}</div>
              <div v-if="gapOf(q)" class="small"
                   style="margin-top:4px;color:#b45309">{{ gapOf(q) }} —— 点「看」进去，或「修」去补</div>
            </div>
            <button class="btn sm ghost" @click="showPreview(q)" title="看完整题干和答案解析">看</button>
            <button class="btn sm ghost" :disabled="i === 0" @click="move(i, -1)">↑</button>
            <button class="btn sm ghost" :disabled="i === picked.length - 1" @click="move(i, 1)">↓</button>
            <button class="btn sm ghost" @click="remove(q.id)">✕</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 题目预览：题干 / 答案 / 解析一次看全。
         ⚠️ 三处都要走 renderRichEl 画公式（见 setup 里 paintRich），否则老师点开
            看到的是一片 $…$ —— 那等于没预览。 -->
    <div v-if="preview" class="modal-mask" @click.self="closePreview">
      <div class="modal" style="max-width:780px">
        <div class="modal-head">
          <h3>题目预览</h3>
          <span class="tag blue">{{ preview.qtype }}</span>
          <span class="tag">{{ preview.difficulty }}</span>
          <span v-if="gapOf(preview)" class="tag red">{{ gapOf(preview) }}</span>
          <div style="flex:1"></div>
          <button class="btn sm ghost" @click="closePreview">关闭</button>
        </div>
        <div class="modal-body" style="max-height:68vh;overflow:auto">
          <div class="muted" style="font-size:12px;margin-bottom:4px">题干</div>
          <div v-if="(preview.content || '').trim()" class="q-rich"
               :ref="(el) => preview && setRichEl('pv:' + preview.id, el)"></div>
          <a v-else-if="preview.image" :href="preview.image" target="_blank" rel="noopener">
            <img class="q-thumb" :src="preview.image" alt="题目原貌图" style="max-width:100%">
          </a>
          <div v-else class="muted small">{{ NO_STEM_NOTE }}</div>

          <div class="muted" style="font-size:12px;margin:14px 0 4px">答案</div>
          <div v-if="(preview.answer || '').trim()" class="q-rich"
               :ref="(el) => preview && setRichEl('pva:' + preview.id, el)"></div>
          <a v-else-if="preview.answer_image" :href="preview.answer_image" target="_blank" rel="noopener">
            <img class="q-thumb" :src="preview.answer_image" alt="答案图" style="max-width:100%">
          </a>
          <div v-else style="color:#b45309;font-size:13px">
            这道题还没有答案 —— 点右下角「去修这题」到录题页补上。
          </div>

          <template v-if="(preview.analysis || '').trim()">
            <div class="muted" style="font-size:12px;margin:14px 0 4px">解析</div>
            <div class="q-rich" :ref="(el) => preview && setRichEl('pvp:' + preview.id, el)"></div>
          </template>

          <div class="muted" style="font-size:12px;margin:14px 0 4px">其他</div>
          <div style="font-size:13px;line-height:1.9">
            <div v-if="(preview.knowledge_points || []).length">
              知识点：{{ (preview.knowledge_points || []).join('、') }}
            </div>
            <div v-if="preview.source">来源：{{ preview.source }}</div>
            <div v-if="preview.doc_filename">原文档：{{ preview.doc_filename }}</div>
            <div v-if="preview.usage_count">已出过 {{ preview.usage_count }} 次</div>
            <div v-if="(preview.tags || []).length">标签：{{ (preview.tags || []).join('、') }}</div>
          </div>
        </div>
        <div class="modal-foot">
          <a class="btn sm" :href="fixUrl(preview)" target="_blank" rel="noopener"
             title="到录题页给这道题补答案或改录入">去修这题</a>
          <button class="btn sm" @click="removeFromPreview">从卷子里移除</button>
          <button class="btn primary" @click="closePreview">关闭</button>
        </div>
      </div>
    </div>
  </div>`,
};
