// 课后补充 —— 老师上完课、改完作业，觉得他某块不行，就在这里挑几样东西，
// 存进**今天那节课**里，下次上课打印（学生版，不含答案）给他。
//
// 几条刻意的设计（细节见 docs/学生画像与推题出卷方案.md）：
//   · **系统不挑、不猜、不排序**：内容全由老师挑（用户明确否掉了推荐算法）。
//     这里只做两件事：搜索方便、记下来。
//   · **挑完就走**：除了勾选，别的都不必填（知识点与备注都可空，
//     知识点还会自动从挑中的题目带出来）。记录这件事没有回报就没人记。
//   · 一节课可以存好几批（今天发 3 题、晚上再加 2 题），所以这里每保存一次就是一批。
import { computed, ref, watch } from 'vue';
import { notesApi, papersApi, supplementsApi } from '../api.js';
import { fail, ok } from '../store.js';
import Modal from './Modal.js';

export default {
  name: 'SupplementDialog',
  components: { Modal },
  props: { lesson: { type: Object, required: true } },
  emits: ['close', 'saved'],
  setup(props, { emit }) {
    const tab = ref('question');            // question | note
    const keyword = ref('');
    const tag = ref('');
    const kp = ref('');
    const rows = ref([]);
    const total = ref(0);
    const loading = ref(false);
    const saving = ref(false);
    const tags = ref([]);
    const kps = ref([]);
    // 已挑的：key = kind:ref_id，值里带着标题（保存时送 kind + ref_id，标题由服务端再查一遍）
    const picked = ref({});
    const focus = ref('');
    const note = ref('');
    // 知识点是否被老师手动改过：没改过就跟着挑中的题自动变
    const focusTouched = ref(false);

    const pickedList = computed(() => Object.values(picked.value));
    const pickedCount = computed(() => pickedList.value.length);
    const canSave = computed(() => pickedCount.value > 0 && !saving.value);

    const keyOf = (kind, id) => kind + ':' + id;
    const isPicked = (kind, id) => !!picked.value[keyOf(kind, id)];

    function toggle(kind, id, title) {
      const k = keyOf(kind, id);
      if (picked.value[k]) {
        delete picked.value[k];                  // Vue 3 的 reactive 代理能追踪 delete
        picked.value = { ...picked.value };
      } else {
        picked.value = { ...picked.value, [k]: { kind, ref_id: String(id), title } };
      }
      if (!focusTouched.value) autoFocus();
    }

    /** 知识点自动带出：从挑中的题目里取第一个有知识点的（老师改过就不再动）。 */
    function autoFocus() {
      const fromQ = rows.value.find((r) => picked.value[keyOf('question', r.id)] && (r.knowledge_points || []).length);
      if (fromQ) focus.value = fromQ.knowledge_points[0];
    }

    function brief(q) {
      const t = (q.content || '').replace(/\s+/g, ' ').trim();
      if (!t) return q.image ? '（图片题）' : '（空题）';
      return t.length > 80 ? t.slice(0, 80) + '…' : t;
    }

    async function search() {
      loading.value = true;
      try {
        if (tab.value === 'question') {
          const r = await papersApi.search({
            keyword: keyword.value.trim() || undefined,
            tags: tag.value || undefined,
            kp: kp.value.trim() || undefined,
            limit: 50,
          });
          rows.value = r.items || [];
          total.value = r.total || rows.value.length;
        } else {
          const r = await notesApi.list({ keyword: keyword.value.trim() || undefined });
          rows.value = (r.items || []).map((n) => ({ id: n.id, title: n.title, path: n.path }));
          total.value = rows.value.length;
        }
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    // 切页签、改筛选都重查（跟出卷页一个手感：不用再点一次「搜索」）
    watch([tab, tag, kp], () => search());
    let timer = null;
    watch(keyword, () => {
      clearTimeout(timer);
      timer = setTimeout(search, 300);           // 打字防抖，别每敲一下就发请求
    });

    // 下拉候选：库里实际用过的标签与知识点（有它才能「录了什么就能按什么搜」）
    papersApi.tags().then((t) => { tags.value = (t.items || []).map((x) => x.tag); }).catch(() => {});
    papersApi.knowledgePoints().then((t) => { kps.value = (t.items || []).map((x) => x.kp); }).catch(() => {});
    search();

    async function save() {
      if (!pickedCount.value) return;
      saving.value = true;
      try {
        await supplementsApi.create(props.lesson.id, {
          items: pickedList.value.map((p) => ({ kind: p.kind, ref_id: p.ref_id })),
          focus: focus.value,
          note: note.value,
        });
        ok('已记在今天这节课里 —— 下次上课可以打印给他');
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
    }

    return {
      tab, keyword, tag, kp, rows, total, loading, saving, tags, kps,
      picked, pickedList, pickedCount, canSave, focus, note, focusTouched,
      isPicked, toggle, brief, save, search,
    };
  },
  template: `
  <Modal title="课后补充" @close="$emit('close')">
    <div class="small muted" style="margin-bottom:10px">
      给 <b>{{ lesson.student_name }}</b> 挑几样下次上课要用的材料 ——
      存进今天这节课里，之后在「上课记录」里一眼能看到给没给。
    </div>

    <div class="chips" style="margin-bottom:10px">
      <span class="chip" :class="{ on: tab === 'question' }" @click="tab = 'question'">题库挑题</span>
      <span class="chip" :class="{ on: tab === 'note' }" @click="tab = 'note'">挑笔记</span>
    </div>

    <div class="filter-row" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      <input type="text" v-model="keyword" placeholder="关键词（题干 / 答案 / 标题）" style="flex:1;min-width:160px">
      <template v-if="tab === 'question'">
        <select v-model="tag" style="width:150px">
          <option value="">全部标签</option>
          <option v-for="t in tags" :key="t" :value="t">{{ t }}</option>
        </select>
        <input type="text" v-model="kp" list="sup-kps" placeholder="知识点" style="width:150px">
        <datalist id="sup-kps"><option v-for="k in kps" :key="k" :value="k"></option></datalist>
      </template>
    </div>

    <div class="pick-list">
      <div v-if="loading" class="empty">查询中…</div>
      <template v-else-if="rows.length">
        <div v-for="r in rows" :key="r.id" class="pick-row">
          <div style="flex:1;min-width:0">
            <div>{{ tab === 'question' ? brief(r) : r.title }}</div>
            <div class="small muted" v-if="tab === 'note' && r.path">{{ r.path }}</div>
            <div class="small muted" v-else-if="tab === 'question' && (r.knowledge_points || []).length">
              {{ r.knowledge_points.join(' / ') }}
            </div>
          </div>
          <button class="btn sm" :class="{ primary: !isPicked(tab, r.id) }"
                  @click="toggle(tab, r.id, tab === 'question' ? brief(r) : r.title)">
            {{ isPicked(tab, r.id) ? '✓ 已选' : '加入' }}
          </button>
        </div>
      </template>
      <div v-else class="empty">没有匹配的{{ tab === 'question' ? '题目' : '笔记' }}</div>
      <div v-if="total > rows.length" class="small muted" style="margin-top:6px">
        共 {{ total }} 条，只显示了前 {{ rows.length }} 条 —— 加个关键词缩小范围
      </div>
    </div>

    <div v-if="pickedCount" style="margin-top:10px">
      <div class="small muted" style="margin-bottom:4px">这批要给的（{{ pickedCount }} 项）</div>
      <div class="chips">
        <span v-for="p in pickedList" :key="p.kind + p.ref_id" class="chip on"
              :title="p.title" @click="toggle(p.kind, p.ref_id, p.title)">
          {{ p.kind === 'question' ? '题' : '笔记' }}·{{ (p.title || '').slice(0, 16) }} ✕
        </span>
      </div>
    </div>

    <div class="field" style="margin-top:12px">
      <label>针对的知识点（可空，从题目自动带出）</label>
      <input type="text" v-model="focus" list="sup-kps" @input="focusTouched = true"
             placeholder="例如：因式分解">
    </div>
    <div class="field">
      <label>备注（可空）</label>
      <input type="text" v-model="note" placeholder="例如：这两题是课本 P42 的变式">
    </div>

    <template #foot>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="!canSave" @click="save">
        {{ saving ? '保存中…' : '存进今天这节课' }}
      </button>
    </template>
  </Modal>`,
};
