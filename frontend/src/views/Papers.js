// 出卷：左边筛题库选题，右边攒成一份卷子并导出。
//
// 为什么筛选用新的 /api/questions/search 而不是复用 GET /questions：
// 后者是录题页正在用的、返回纯数组的旧接口；出卷要多条件 + 分页 + 「共 N 题」，
// 诉求不同，硬改那个接口会把录题页一起弄坏。
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { curriculumApi, papersApi } from '../api.js';
import { fail, loadCurricula, ok, state } from '../store.js';

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
    const title = ref(todayTitle());
    const opts = reactive({
      show_answer: true, show_analysis: false, show_tags: true, show_meta: true,
    });

    /** 知识点选项：**所有**体系的节点（按体系分组）+ 题库里已用过的名字。
     *
     * 为什么不再跟着「体系」走：现在只有国内高中数学有知识树（119 个节点），
     * 初中等体系是空的 —— 跟着走的话选了初中就是一个空下拉，根本没法筛。
     * 选项值用**名字**（不是 node id），后端会同时匹配「文本知识点」和「同名节点子树」。 */
    async function loadKpOptions() {
      const groups = [];
      try {
        const used = await papersApi.knowledgePoints();
        if (used.items.length) {
          groups.push({ label: '题库里已用过的', options: used.items.map(i => ({ value: i.kp, label: i.kp })) });
        }
        for (const c of state.curricula || []) {
          const tree = await curriculumApi.nodes(c.id);
          const options = [];
          const walk = (list, depth) => (list || []).forEach((n) => {
            options.push({ value: n.name, label: '\u3000'.repeat(depth) + n.name });
            walk(n.children, depth + 1);
          });
          walk(tree, 0);
          if (options.length) groups.push({ label: c.name, options });
        }
      } catch (e) {
        fail(e.message);
      }
      kpGroups.value = groups;
    }

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

    function resetFilter() {
      Object.assign(filter, { ...EMPTY_FILTER, tags: [] });
      search();
    }

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
     *  HTML 后端按 inline 发（直接看，Ctrl+P 存 PDF），Word/PDF 按 attachment 发（浏览器下载）。 */
    function open(kind) {
      if (!picked.value.length) {
        fail('还没选题目');
        return;
      }
      window.open(papersApi.exportUrl({
        ids: pickedIds.value.join(','),
        format: kind,
        title: title.value,
        show_answer: opts.show_answer ? true : undefined,
        show_analysis: opts.show_analysis ? true : undefined,
        show_tags: opts.show_tags ? true : undefined,
        show_meta: opts.show_meta ? true : undefined,
      }), '_blank');
    }

    function brief(q) {
      const t = (q.content || '').replace(/\s+/g, ' ').trim();
      if (!t) return '（图片题）';
      return t.length > 70 ? `${t.slice(0, 70)}…` : t;
    }

    // 条件一改就重筛 —— 让老师每改一次都再点一下「筛选」太多余。
    watch(
      () => [filter.curriculum_id, filter.kp, filter.qtype, filter.difficulty,
             filter.has_image, filter.with_answer, filter.sort, filter.tags.join(',')],
      () => search(),
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
      search();
    });

    return {
      state, filter, rows, total, loading, kpGroups, picked, pickedIds,
      title, opts, QTYPES, DIFFS, pageNo, pageCount, tagOptions,
      search, resetFilter, add, addPage, remove, clearPicked, move,
      isPicked, open, brief, prev, next, toggleTag,
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
              <label>知识点（含子节点）</label>
              <select v-model="filter.kp">
                <option value="">全部知识点</option>
                <optgroup v-for="g in kpGroups" :key="g.label" :label="g.label">
                  <option v-for="n in g.options" :key="n.value" :value="n.value">{{ n.label }}</option>
                </optgroup>
              </select>
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
              <div class="chips" style="max-height:80px;overflow:auto">
                <span class="chip" :class="{ on: !filter.tags.length }" @click="filter.tags = []">不限</span>
                <span class="chip" v-for="t in tagOptions" :key="t.tag"
                      :class="{ on: filter.tags.includes(t.tag) }" @click="toggleTag(t.tag)">
                  {{ t.tag }} <span class="muted">{{ t.count }}</span>
                </span>
                <span v-if="!tagOptions.length" class="muted" style="font-size:12px">
                  还没有标签 —— 到录题页给题目打上标签，这里就会出现
                </span>
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
            <button class="btn sm" :disabled="!rows.length" @click="addPage">本页全选</button>
          </div>
          <div v-if="!rows.length" class="empty">没有符合条件的题目</div>
          <div v-for="q in rows" :key="q.id" class="lesson-row">
            <div style="flex:1;min-width:0">
              <div>{{ brief(q) }}</div>
              <div style="margin-top:5px;display:flex;gap:5px;flex-wrap:wrap">
                <span class="tag blue">{{ q.qtype }}</span>
                <span class="tag">{{ q.difficulty }}</span>
                <span v-for="t in (q.tags || [])" :key="'tag' + t" class="tag">🏷 {{ t }}</span>
                <span v-if="q.image" class="tag green">有图</span>
                <span v-if="q.answer || q.answer_image" class="tag">有答案</span>
                <span v-if="q.usage_count" class="tag orange">用过 {{ q.usage_count }} 次</span>
              </div>
            </div>
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
            <label>导出选项</label>
            <div class="chips">
              <span class="chip" :class="{ on: opts.show_answer }"
                    @click="opts.show_answer = !opts.show_answer">附参考答案</span>
              <span class="chip" :class="{ on: opts.show_analysis }"
                    @click="opts.show_analysis = !opts.show_analysis">答案带解析</span>
              <span class="chip" :class="{ on: opts.show_tags }"
                    @click="opts.show_tags = !opts.show_tags">标注题型·难度·标签</span>
              <span class="chip" :class="{ on: opts.show_meta }"
                    @click="opts.show_meta = !opts.show_meta">留姓名日期栏</span>
            </div>
          </div>

          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
            <button class="btn primary" :disabled="!picked.length" @click="open('html')">预览 / 打印</button>
            <button class="btn" :disabled="!picked.length" @click="open('docx')">下载 Word</button>
            <button class="btn" :disabled="!picked.length" @click="open('pdf')">下载 PDF</button>
          </div>
          <p class="muted" style="font-size:12px;line-height:1.6;margin:0 0 12px">
            「预览 / 打印」出的是 HTML：公式在浏览器里渲染好，Ctrl+P 直接存成 PDF。<br>
            Word / PDF 由服务端排版，<b>公式会显示成 $…$ 原文</b>（服务端没有 LaTeX 引擎）。
          </p>

          <div v-if="!picked.length" class="empty">还没有选题</div>
          <div v-for="(q, i) in picked" :key="q.id" class="lesson-row">
            <span class="muted" style="width:20px;flex:none">{{ i + 1 }}</span>
            <div style="flex:1;min-width:0">{{ brief(q) }}</div>
            <button class="btn sm ghost" :disabled="i === 0" @click="move(i, -1)">↑</button>
            <button class="btn sm ghost" :disabled="i === picked.length - 1" @click="move(i, 1)">↓</button>
            <button class="btn sm ghost" @click="remove(q.id)">✕</button>
          </div>
        </div>
      </div>
    </div>
  </div>`,
};
