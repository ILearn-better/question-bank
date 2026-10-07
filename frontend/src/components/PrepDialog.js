// 课前备课 —— 老师上课前做的准备，与「写反馈 / 交作业 / 课后补充」并列的第四样。
//
// 三块内容（对应需求的三句话）：
//   ① 结构化四段：教学目标 / 重点难点 / 教学流程 / 准备材料。
//   ② 挑材料：这节课要用的题 / 笔记（交互复用课后补充那套：搜索 + 勾选 + 已选 chips）。
//   ③ 与笔记库双向联动：
//       · 「从笔记库选取」→ 关联一篇已有笔记（note_id 指向它）；
//       · 「存成笔记」→ 把四段 + 材料拼成 Markdown 写进笔记库（先保存备课，再调后端）。
//
// 一节一条：备课一节课备一次，改了就是改这一份（不像课后补充能存好几批）。
import { computed, ref, watch } from 'vue';
import { notesApi, papersApi, prepApi } from '../api.js';
import { fail, ok } from '../store.js';
import Modal from './Modal.js';

// 四段的键与中文名（保存时 keyPoints -> key_points 由服务端字段名对一下）。
const FIELDS = [
  { key: 'goal', label: '教学目标', ph: '这节课要让他学会什么' },
  { key: 'keyPoints', label: '重点难点', ph: '哪里是重点，哪里容易卡' },
  { key: 'flow', label: '教学流程', ph: '先讲什么、再练什么、按什么节奏' },
  { key: 'materials', label: '准备材料', ph: '讲义、教具、要提前打印的东西' },
];

export default {
  name: 'PrepDialog',
  components: { Modal },
  props: { lesson: { type: Object, required: true } },
  emits: ['close', 'saved'],
  setup(props, { emit }) {
    const form = ref({ goal: '', keyPoints: '', flow: '', materials: '' });
    // ---- 挑材料（复用课后补充的交互）----
    const tab = ref('question');            // question | note
    const keyword = ref('');
    const tag = ref('');
    const kp = ref('');
    const rows = ref([]);
    const total = ref(0);
    const loading = ref(false);
    const tags = ref([]);
    const kps = ref([]);
    const picked = ref({});                 // key = kind:ref_id
    // ---- 笔记联动 ----
    const noteId = ref('');
    const noteTitle = ref('');
    const pickingNote = ref(false);
    const noteKeyword = ref('');
    const noteRows = ref([]);
    const noteLoading = ref(false);
    // ---- 状态 ----
    const saving = ref(false);
    const savingNote = ref(false);
    const loaded = ref(false);

    const pickedList = computed(() => Object.values(picked.value));
    const pickedCount = computed(() => pickedList.value.length);

    const keyOf = (kind, id) => kind + ':' + id;
    const isPicked = (kind, id) => !!picked.value[keyOf(kind, id)];

    function brief(q) {
      const t = (q.content || '').replace(/\s+/g, ' ').trim();
      if (!t) return q.image ? '（图片题）' : '（空题）';
      return t.length > 80 ? t.slice(0, 80) + '…' : t;
    }

    function toggle(kind, id, title) {
      const k = keyOf(kind, id);
      if (picked.value[k]) {
        delete picked.value[k];
        picked.value = { ...picked.value };
      } else {
        picked.value = { ...picked.value, [k]: { kind, ref_id: String(id), title } };
      }
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

    watch([tab, tag, kp], () => search());
    let timer = null;
    watch(keyword, () => {
      clearTimeout(timer);
      timer = setTimeout(search, 300);
    });

    // ---- 笔记联动 ----
    async function searchNotes() {
      noteLoading.value = true;
      try {
        const r = await notesApi.list({ keyword: noteKeyword.value.trim() || undefined });
        noteRows.value = r.items || [];
      } catch (e) {
        fail(e.message);
      } finally {
        noteLoading.value = false;
      }
    }
    function toggleNotePick() {
      pickingNote.value = !pickingNote.value;
      if (pickingNote.value && !noteRows.value.length) searchNotes();
    }
    function pickNote(n) {
      noteId.value = n.id;
      noteTitle.value = n.title;
      pickingNote.value = false;
    }

    async function load() {
      try {
        const r = await prepApi.get(props.lesson.id);
        if (r && r.prep) {
          form.value = {
            goal: r.prep.goal || '',
            keyPoints: r.prep.key_points || '',
            flow: r.prep.flow || '',
            materials: r.prep.materials || '',
          };
          noteId.value = r.prep.note_id || '';
          noteTitle.value = r.prep.note_title || '';
          const p = {};
          for (const it of r.prep.items || []) {
            p[keyOf(it.kind, it.ref_id)] = { kind: it.kind, ref_id: it.ref_id, title: it.title };
          }
          picked.value = p;
        }
      } catch (e) {
        /* 拿不到就当新建，不影响备课 */
      } finally {
        loaded.value = true;
      }
    }

    function payload() {
      return {
        goal: form.value.goal,
        key_points: form.value.keyPoints,
        flow: form.value.flow,
        materials: form.value.materials,
        note_id: noteId.value || '',
        items: pickedList.value.map((p) => ({ kind: p.kind, ref_id: p.ref_id })),
      };
    }

    async function doSave() {
      saving.value = true;
      try {
        await prepApi.save(props.lesson.id, payload());
        return true;
      } catch (e) {
        fail(e.message);
        return false;
      } finally {
        saving.value = false;
      }
    }

    async function save() {
      if (await doSave()) {
        ok('备课已保存');
        emit('saved');
      }
    }

    async function saveAsNote() {
      savingNote.value = true;
      try {
        // 先保存备课，保证「存成笔记」同步的是最新编辑内容，而不是库里的旧值
        const saved = await doSave();
        if (!saved) return;
        const r = await prepApi.saveAsNote(props.lesson.id);
        noteId.value = r.note_id;
        noteTitle.value = r.note_title;
        ok(r.created ? `已存成新笔记：${r.note_title}` : `已同步到笔记：${r.note_title}`);
      } finally {
        savingNote.value = false;
      }
    }

    papersApi.tags().then((t) => { tags.value = (t.items || []).map((x) => x.tag); }).catch(() => {});
    papersApi.knowledgePoints().then((t) => { kps.value = (t.items || []).map((x) => x.kp); }).catch(() => {});
    search();
    load();

    return {
      FIELDS, form, tab, keyword, tag, kp, rows, total, loading, tags, kps,
      picked, pickedList, pickedCount, isPicked, toggle, brief, search,
      noteId, noteTitle, pickingNote, noteKeyword, noteRows, noteLoading,
      searchNotes, toggleNotePick, pickNote, save, saveAsNote,
      saving, savingNote, loaded,
    };
  },
  template: `
  <Modal :title="'课前备课 · ' + lesson.student_name" @close="$emit('close')">
    <div class="small muted" style="margin-bottom:10px">
      给 <b>{{ lesson.student_name }}</b> 备这节课 —— 写四段准备，挑这节课要用的材料，
      写完可以一键存成笔记。
    </div>

    <div v-if="!loaded" class="empty">加载中…</div>
    <template v-else>
      <div v-for="f in FIELDS" :key="f.key" class="field">
        <label>{{ f.label }}</label>
        <textarea v-model="form[f.key]" rows="3" :placeholder="f.ph"></textarea>
      </div>

      <div class="chips" style="margin:12px 0 10px">
        <span class="chip" :class="{ on: tab === 'question' }" @click="tab = 'question'">题库挑题</span>
        <span class="chip" :class="{ on: tab === 'note' }" @click="tab = 'note'">挑笔记</span>
      </div>

      <div class="filter-row" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
        <input type="text" v-model="keyword" placeholder="关键词（题干 / 标题）" style="flex:1;min-width:160px">
        <template v-if="tab === 'question'">
          <select v-model="tag" style="width:150px">
            <option value="">全部标签</option>
            <option v-for="t in tags" :key="t" :value="t">{{ t }}</option>
          </select>
          <input type="text" v-model="kp" list="prep-kps" placeholder="知识点" style="width:150px">
          <datalist id="prep-kps"><option v-for="k in kps" :key="k" :value="k"></option></datalist>
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
      </div>

      <div v-if="pickedCount" style="margin-top:10px">
        <div class="small muted" style="margin-bottom:4px">这节课要用的（{{ pickedCount }} 项）</div>
        <div class="chips">
          <span v-for="p in pickedList" :key="p.kind + p.ref_id" class="chip on"
                :title="p.title" @click="toggle(p.kind, p.ref_id, p.title)">
            {{ p.kind === 'question' ? '题' : '笔记' }}·{{ (p.title || '').slice(0, 16) }} ✕
          </span>
        </div>
      </div>

      <div style="margin-top:14px; padding:10px 12px; background:var(--panel-2); border-radius:8px">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
          <b class="small">关联笔记</b>
          <span v-if="noteTitle" class="tag blue">{{ noteTitle }}</span>
          <span v-else class="small muted">还没关联 —— 可以从笔记库选一篇，或写完后存成一篇</span>
          <div class="spacer"></div>
          <button class="btn sm ghost" @click="toggleNotePick">从笔记库选取</button>
          <button class="btn sm" :disabled="savingNote" @click="saveAsNote">
            {{ savingNote ? '存成笔记中…' : '存成笔记' }}
          </button>
        </div>
        <div v-if="pickingNote" style="margin-top:8px">
          <input type="text" v-model="noteKeyword" placeholder="搜笔记标题 / 内容"
                 style="width:100%;margin-bottom:6px" @input="searchNotes">
          <div class="pick-list" style="max-height:200px;overflow:auto">
            <div v-if="noteLoading" class="empty">查询中…</div>
            <template v-else-if="noteRows.length">
              <div v-for="n in noteRows" :key="n.id" class="pick-row">
                <div style="flex:1;min-width:0">
                  <div>{{ n.title }}</div>
                  <div class="small muted" v-if="n.path">{{ n.path }}</div>
                </div>
                <button class="btn sm" @click="pickNote(n)">关联</button>
              </div>
            </template>
            <div v-else class="empty">没有匹配的笔记</div>
          </div>
        </div>
      </div>
    </template>

    <template #foot>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="saving" @click="save">
        {{ saving ? '保存中…' : '保存备课' }}
      </button>
    </template>
  </Modal>`,
};
