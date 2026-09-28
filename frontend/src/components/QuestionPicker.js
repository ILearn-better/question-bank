import { computed, onMounted, ref } from 'vue';
import { curriculumApi, papersApi } from '../api.js';
import { fail, ok, state } from '../store.js';
import Modal from './Modal.js';

/**
 * 从题库里挑题插进笔记。
 *
 * 为什么不是「直接把所有题列出来」：老师此刻要找的是**某一道具体的题**
 * （上次作业错的那道、今天要讲的那道），所以入口是搜索 ——
 * 体系、关键字、标签、知识点四种一起用（后端 /api/questions/search 都支持）。
 *
 * 体系下拉不只是多一个筛选项：它一改，**标签与知识点的选项也跟着按该体系重算**
 * （`?curriculum_id=` 传给统计接口）。否则选项上写的是全库次数，
 * 选了体系却只剩一两条 —— 看着就像搜坏了。
 *
 * 只负责「挑」，不负责「插」：插入位置在笔记编辑器那边（光标），
 * 所以这里只 emit 一个 qid 出去。这样组件在别的页面也能直接复用。
 */
export default {
  name: 'QuestionPicker',
  components: { Modal },
  emits: ['close', 'pick'],
  props: {
    /** 插进去之后题干前会写「练习 N：」，老师能看到自己插到第几道。 */
    nextIndex: { type: Number, default: 0 },
  },
  setup(props, { emit }) {
    const keyword = ref('');
    const curriculum = ref('');  // '' = 不限体系
    const tags = ref([]);          // 库里实际用过的标签，不让用户自由输
    const kps = ref([]);
    const tag = ref('');           // '' = 不限
    const kp = ref('');
    const rows = ref([]);
    const total = ref(0);
    const loading = ref(false);
    const picked = ref(new Set()); // 这次要插的（可以一次挑好几道，一起插）
    let timer = null;

    // 体系来自共用 store（工作台启动时就拉过），弹窗里不再单独请求一次
    const curricula = computed(() => state.curricula || []);
    const curName = computed(() => {
      const c = (state.curricula || []).find((x) => x.id === curriculum.value);
      return c ? c.name : '';
    });

    async function loadOptions() {
      try {
        const cid = curriculum.value || undefined;
        const [t, k] = await Promise.all([papersApi.tags(cid), papersApi.knowledgePoints(cid)]);
        // 后端返回 {items:[{tag|kp, count}]}；两个端点形状一样，取法写清楚免得猜
        tags.value = (t && t.items) || [];
        kps.value = (k && k.items) || [];
      } catch (e) { /* 没有标签也能用关键字搜，不挡路 */ }
    }

    /** 换体系：选项与结果一起重来。
     *
     * **不清已选的标签/知识点**：老师可能先按知识点筛、再限定体系，
     * 一改就把他的选择抹掉更让人意外。真筛不出来时列表会空，
     * 下面的提示已经写了「换个关键字，或者先别选标签」。 */
    function onCurriculum() {
      loadOptions();
      search();
    }

    async function search() {
      loading.value = true;
      try {
        const r = await papersApi.search({
          curriculum_id: curriculum.value || undefined,
          keyword: keyword.value || undefined,
          tags: tag.value || undefined,
          kp: kp.value || undefined,
          limit: 50,
        });
        rows.value = r.items || [];
        total.value = r.total != null ? r.total : rows.value.length;
      } catch (e) {
        fail('搜题失败：' + e.message);
      } finally {
        loading.value = false;
      }
    }

    /** 打字就搜（防抖 250ms）：题库搜题是本地库，不值得让老师按回车。 */
    function onInput() {
      clearTimeout(timer);
      timer = setTimeout(search, 250);
    }

    function toggle(id) {
      const s = new Set(picked.value);
      if (s.has(id)) s.delete(id); else s.add(id);
      picked.value = s;
    }

    /** 插的时候按**列表顺序**，而不是点击顺序 —— 老师看着列表点，
     *  出卷的顺序就该跟看到的一致（跟导出的挑题逻辑同一个道理）。 */
    function confirmPick() {
      const ids = rows.value.filter((r) => picked.value.has(r.id)).map((r) => r.id);
      const rest = [...picked.value].filter((id) => !ids.includes(id)); // 已翻页走的
      const all = ids.concat(rest);
      if (!all.length) { fail('先挑几道题'); return; }
      emit('pick', all);
    }

    function stem(q) {
      const t = (q.content || '').replace(/\s+/g, ' ').trim();
      if (t) return t.length > 70 ? t.slice(0, 70) + '…' : t;
      return q.image ? '（图片题）' : '（无题干）';
    }

    onMounted(async () => {
      // 体系列表：store 里没有就补一次（直接打开笔记页时可能还没拉过）
      if (!state.curricula || !state.curricula.length) {
        try { state.curricula = await curriculumApi.list(); } catch (e) { /* 不影响搜索 */ }
      }
      loadOptions();
      search();
    });

    return { keyword, curriculum, curricula, curName, tags, kps, tag, kp, rows, total,
             loading, picked, onInput, search, onCurriculum, toggle, confirmPick, stem,
             close: () => emit('close') };
  },
  template: `
    <Modal title="插入题库里的题" @close="close">
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <select v-model="curriculum" @change="onCurriculum" style="width:170px">
          <option value="">体系：不限</option>
          <option v-for="c in curricula" :key="c.id" :value="c.id">{{ c.name }}</option>
        </select>
        <input type="text" v-model="keyword" @input="onInput" style="flex:1;min-width:160px"
               placeholder="关键字（题干 / 答案里都能搜）" />
        <select v-model="tag" @change="search" style="width:170px">
          <option value="">标签：不限</option>
          <option v-for="t in tags" :key="t.tag" :value="t.tag">
            {{ t.tag }}（{{ t.count }}）
          </option>
        </select>
        <select v-model="kp" @change="search" style="width:170px">
          <option value="">知识点：不限</option>
          <option v-for="k in kps" :key="k.kp" :value="k.kp">
            {{ k.kp }}（{{ k.count }}）
          </option>
        </select>
      </div>

      <p class="small muted" style="margin:10px 0">
        <template v-if="curName">范围：{{ curName }}，</template>
        找到 {{ total }} 道<template v-if="picked.size">，已挑 {{ picked.size }} 道</template>。
        插进去的是题干和答案：导「学生版」时答案不会被写进文件；
        题里的图片会连图一起存进这篇笔记 —— 之后删掉题库里那道题，讲义也不受影响。
      </p>

      <div class="pick-list">
        <div v-if="loading" class="empty">正在搜…</div>
        <template v-else-if="rows.length">
          <div v-for="r in rows" :key="r.id" class="pick-row">
            <input type="checkbox" :checked="picked.has(r.id)" @change="toggle(r.id)" />
            <div style="flex:1;min-width:0">
              <div>{{ stem(r) }}</div>
              <div class="small muted">
                {{ [r.qtype, (r.knowledge_points || []).join(' / ')].filter(Boolean).join(' · ') }}
                <span v-for="t in (r.tags || [])" :key="t">｜{{ t }}</span>
                <template v-if="r.image">｜有图</template>
                <template v-if="(r.answer || '').trim()">｜有答案</template>
              </div>
            </div>
            <span v-if="picked.has(r.id)" class="small" style="color:var(--primary)">已挑</span>
          </div>
        </template>
        <div v-else class="empty">没搜到题。换个关键字，或者先别选标签。</div>
      </div>

      <template #foot>
        <button class="btn ghost" @click="close">取消</button>
        <button class="btn primary" :disabled="!picked.size" @click="confirmPick">
          插到光标处<template v-if="picked.size">（{{ picked.size }} 道）</template>
        </button>
      </template>
    </Modal>
  `,
};
