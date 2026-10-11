// 课前备课 —— 老师上课前做的准备，与「写反馈 / 交作业 / 课后补充」并列的第四样。
//
// 四块内容（对应需求）：
//   ① 结构化四段：教学目标 / 重点难点 / 教学流程 / 准备材料。
//   ② 挑材料：这节课要用的题 / 笔记（交互复用课后补充那套：搜索 + 勾选 + 已选 chips）。
//      题干按**渲染后**的样子显示（真公式，不是 $…$ 原文），鼠标移上去自动展开看全 ——
//      挑题时老师要能读到整道题，不能只看见一行截断的纯文本。
//   ③ 备课材料**文件**：任意格式（含电脑上做好的 .lnk 快捷方式），只归档不抽文字，
//      落在「学生 / 上课日期」文件夹里（role='prep'，与上课文件两份清单互不相见）。
//   ④ 与笔记库双向联动：
//       · 「从笔记库选取」→ 关联一篇已有笔记（note_id 指向它）；
//       · 「存成笔记」→ 把四段 + 材料拼成 Markdown 写进笔记库（先保存备课，再调后端）。
//
// 一节一条：备课一节课备一次，改了就是改这一份（不像课后补充能存好几批）。
import { computed, nextTick, ref, watch } from 'vue';
import { lessonFilesApi, notesApi, papersApi, prepApi } from '../api.js';
import { ensureLibs, renderRichEl } from '../mathRender.js';
import { fail, fmtBytes, ok } from '../store.js';
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
    // ---- 备课材料（文件 / 快捷方式；只归档，不抽文字）----
    const files = ref([]);
    const uploadingFile = ref(false);
    const upIndex = ref(0);
    const upTotal = ref(0);
    const fileNote = ref('');
    const archiveDir = ref('');
    const fileEl = ref(null);

    async function loadFiles() {
      try {
        const r = await lessonFilesApi.list(props.lesson.id, 'prep');
        files.value = r.items || [];
        archiveDir.value = r.rel_dir || '';
      } catch (e) { /* 拿不到不影响写备课 */ }
    }

    function pickFile() { fileEl.value && fileEl.value.click(); }

    /** 原件地址（点名字就能下载/查看那份文件）。 */
    const rawUrl = (id) => lessonFilesApi.rawUrl(id);

    /** 一次可以选多份，逐份上传：一份失败不该把其余的也拦下来。 */
    async function onFilePicked(e) {
      const picked = Array.from(e.target.files || []);
      e.target.value = '';            // 同一批文件连传两次也要能再次触发 change
      if (!picked.length) return;
      uploadingFile.value = true;
      fileNote.value = '';
      upTotal.value = picked.length;
      let bad = 0;
      for (let i = 0; i < picked.length; i++) {
        upIndex.value = i + 1;
        const fd = new FormData();
        fd.append('file', picked[i], picked[i].name);
        try {
          const r = await lessonFilesApi.upload(props.lesson.id, fd, 'prep');
          files.value = [...files.value, r];
        } catch (err) {
          bad += 1;
          fileNote.value = `「${picked[i].name}」没传上去：${err.message}`;
        }
      }
      uploadingFile.value = false;
      upTotal.value = 0;
      if (!bad) ok(`已归档 ${picked.length} 份备课材料`);
    }

    async function removeFile(f) {
      if (!window.confirm(`把「${f.name}」从归档里删掉？磁盘上那份文件也会一起删。`)) return;
      try {
        await lessonFilesApi.remove(f.id);
        files.value = files.value.filter((x) => x.id !== f.id);
      } catch (e) {
        fail(e.message);
      }
    }

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

    /* ---------- 题库挑题的题干：按真公式渲染（管线唯一出处 src/mathRender.js）----------
     *
     *  为什么是「拿元素 ref 自己写 innerHTML」，而不是 `{{ r.content }}` / v-html：
     *    KaTeX 的 auto-render 会把容器里的文本节点整个换掉，Vue 之后还往那个已摘除的
     *    节点里写新内容 —— 界面会永远停在第一次渲染的那一版。容器里**不能**有插值。
     *    （出卷页 / 笔记页踩过同一个坑，见 Papers.js 里同名注释。）
     */
    const richEls = new Map();          // 'q:<id>' → 题干容器元素
    function setRichEl(key, el) {
      if (el) richEls.set(key, el); else richEls.delete(key);
    }
    function paintRich() {
      rows.value.forEach((r) => {
        const el = richEls.get('q:' + r.id);
        if (el) renderRichEl(el, r.content || '');
      });
    }
    let libsReady = false;
    async function renderRich() {
      if (!libsReady) {
        paintRich();                     // 库还没到：先把原文铺出来，别留一屏空白
        try { await ensureLibs(); libsReady = true; }
        catch (e) { return; }            // 加载不上就一直显示原文，不挡着备课
      }
      await nextTick();                  // 等 v-for 把这一批容器挂上去
      paintRich();
    }
    // 只在「题库挑题」页签下渲染：笔记页签那批行里根本没有 q-rich 容器
    watch([rows, tab], () => { if (tab.value === 'question') renderRich(); }, { flush: 'post' });

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
    loadFiles();

    return {
      FIELDS, form, tab, keyword, tag, kp, rows, total, loading, tags, kps,
      picked, pickedList, pickedCount, isPicked, toggle, brief, search, setRichEl,
      noteId, noteTitle, pickingNote, noteKeyword, noteRows, noteLoading,
      searchNotes, toggleNotePick, pickNote, save, saveAsNote,
      saving, savingNote, loaded,
      files, uploadingFile, upIndex, upTotal, fileNote, archiveDir, fileEl,
      pickFile, onFilePicked, removeFile, fmtBytes, rawUrl,
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

      <p class="small muted" style="margin:0 0 6px" v-if="tab === 'question'">
        题干按渲染后的样子显示；<b>鼠标移上去自动展开</b>看全（列表里只露一截）。
      </p>

      <div class="pick-list">
        <div v-if="loading" class="empty">查询中…</div>
        <template v-else-if="rows.length">
          <!-- key 带页签前缀：题库 id 与笔记 id 可能撞号，切页签时必须重建 DOM，
               否则函数 ref 会把新行挂到旧元素上，渲染结果串行。 -->
          <div v-for="r in rows" :key="tab + ':' + r.id" class="pick-row"
               :class="{ 'pick-row-top': tab === 'question' }">
            <div style="flex:1;min-width:0">
              <template v-if="tab === 'question'">
                <!-- 题干走渲染管线（见 setup 里 renderRich 的注释）：老师要看的是排好版的
                     公式，不是 $…$ 原文。容器里**不能**再放 Vue 插值 —— KaTeX 会把文本节点换掉。 -->
                <div class="q-clamp">
                  <div v-if="r.content" class="q-rich" :ref="(el) => setRichEl('q:' + r.id, el)"></div>
                  <div v-else class="muted small">（图片题，没有文本）</div>
                </div>
                <div class="small muted" style="margin-top:5px"
                     v-if="(r.knowledge_points || []).length">
                  {{ r.knowledge_points.join(' / ') }}
                </div>
              </template>
              <template v-else>
                <div>{{ r.title }}</div>
                <div class="small muted" v-if="r.path">{{ r.path }}</div>
              </template>
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

      <!-- 备课材料：格式不限（电脑上做好的 .lnk 快捷方式也能直接传），
           只归档到「学生 / 上课日期」文件夹里，不在本机抽文字、不送给 AI。 -->
      <div class="field" style="margin-top:12px">
        <label>
          备课材料
          <span class="muted small">
            （这节课要用的文件；格式不限 —— 快捷方式 .lnk 直接传上来即可，
             落到磁盘后在资源管理器里双击就能打开它指向的原文件）
          </span>
        </label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn sm" :disabled="uploadingFile" @click="pickFile">
            {{ uploadingFile ? '上传中…' : '＋ 添加文件 / 快捷方式' }}
          </button>
          <!-- 刻意不设 accept：一旦设了白名单，文件对话框就会把 .lnk 藏起来 -->
          <input ref="fileEl" type="file" multiple style="display:none" @change="onFilePicked">
          <span v-if="uploadingFile" class="small muted">正在传第 {{ upIndex }} / {{ upTotal }} 份…</span>
          <span v-else-if="files.length" class="small muted">共 {{ files.length }} 份</span>
        </div>

        <div v-for="f in files" :key="f.id"
             style="display:flex;gap:8px;align-items:flex-start;margin-top:8px">
          <span class="tag" style="flex:none">{{ f.kind_cn }}</span>
          <div style="flex:1;min-width:0">
            <a :href="rawUrl(f.id)" :title="f.name" style="word-break:break-all">{{ f.name }}</a>
            <span class="small muted"> · {{ fmtBytes(f.size_bytes) }}</span>
            <div class="small muted">只归档，不读文字 · 存盘名：{{ f.stored_name }}</div>
          </div>
          <button class="btn ghost sm" style="flex:none;color:var(--danger)" @click="removeFile(f)">删除</button>
        </div>

        <div v-if="fileNote" class="small" style="color:var(--danger);margin-top:6px">{{ fileNote }}</div>
        <div v-if="archiveDir" class="small muted" style="margin-top:6px">
          归档位置：<span class="mono">uploads/{{ archiveDir }}</span>（按「学生 / 上课日期」存放）
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
