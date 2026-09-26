// 课后反馈录入 —— 决定这个系统生死的一个界面。
//
// 设计原则（开发文档 §6.5）：目标是「上完一节课 3 分钟内记完」。
// 具体手段：
//   · 从课表点进来就是这门课，不用先选学生再选时间
//   · 四段式各自带快捷短语，一点即插，省掉打字
//   · 能力评分「允许只打部分」，没打的下次仍按历史值算，绝不强制填满
//   · 所有字段都可留空 —— 只写一句话也能存。卡住一次，这个工具就会被弃用。
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { abilityApi, feedbackApi, studentsApi } from '../api.js';
import { fail, hhmm, ok, shortDate, warn } from '../store.js';
import Modal from './Modal.js';

// 四段字段的键与中文名。多处要用（拼原始记录、渲染、存模板），集中一份。
const FIELDS = [
  { key: 'performance', label: '课堂表现' },
  { key: 'problems', label: '存在问题' },
  { key: 'homework', label: '作业布置' },
  { key: 'next_plan', label: '下次安排' },
];

// 快捷短语：一点即插。写得越具体，家长越觉得「老师真的在看我的孩子」。
const PHRASES = {
  performance: ['状态不错，配合度高', '前半段注意力较集中', '主动提问，思路跟得紧', '略疲倦，节奏放慢后好转'],
  problems: ['计算跳步导致失分', '审题不够仔细，条件看漏', '步骤书写不规范', '知识点迁移能力偏弱'],
  homework: ['课后练习 P32 第 1-8 题', '错题重做一遍', '本周完成一套限时训练', '暂无，先巩固课上内容'],
  next_plan: ['下节课讲函数单调性', '先复习错题再进入新内容', '下次带模考卷来讲解', '继续完成本章剩余题型'],
};

export default {
  name: 'FeedbackDialog',
  components: { Modal },
  props: {
    lesson: { type: Object, required: true },
  },
  emits: ['close', 'saved'],
  setup(props, { emit }) {
    const dims = ref([]);
    const saving = ref(false);
    const form = reactive({
      performance: '',
      problems: '',
      homework: '',
      next_plan: '',
      rating: null,
      share_to_parent: 0,
      scores: {},          // { dim_id: 1..5 }
    });
    const existing = ref(false);
    const lastScores = ref({});   // dim_id -> 该维度上一次的分数（打分的参照锚点）

    // ---- 模板 ----
    const templates = ref([]);
    const templateId = ref('');
    // 快捷短语随模板走。没选模板时用默认那组（就是原来写死的 PHRASES）。
    const phrases = ref({ ...PHRASES });

    // ---- 配图 ----
    const images = ref([]);
    const fileEl = ref(null);
    const uploading = ref(false);

    // ---- 整篇正文 ----
    // 四段是老师随手写的**原料**，doc 是整理成文、可以直接发给家长的**成品**。
    // 两者并存：润色只写 doc，绝不动四段（老师写的东西不能被 AI 反向覆盖）。
    const doc = ref('');

    // ---- AI 润色 / 导出 ----
    const polishing = ref(false);
    const polishResult = ref(null);       // 整篇建议稿的完整响应（含 draft / usage / model）
    const polishText = ref('');           // 建议稿的**可编辑**副本，采用前老师能直接改
    const showExport = ref(false);
    const exporting = ref('');
    const exportSource = ref('auto');     // auto / doc / fields（仅当有整篇时才让选）
    // 润色前先问模板；模板另存要先起名字。
    // 用内联小弹窗而不是 window.prompt —— prompt 在部分环境（含内嵌浏览器）
    // 直接抛 “prompt() is not supported”，而且原生弹窗不可控、也不能写多行说明。
    const showPolishAsk = ref(false);
    const polishStyle = ref('');
    const showTplSave = ref(false);
    const tplName = ref('');
    // ---- 润色模板（整篇文档的格式与文风） ----
    const docTemplates = ref([]);
    const docTemplateId = ref('');
    const docTemplateContent = ref('');    // 可临场改；改完就按改过的发
    const showDocTplSave = ref(false);
    const docTplName = ref('');

    onMounted(async () => {
      try {
        dims.value = await abilityApi.dims();
        const fb = await feedbackApi.get(props.lesson.id);
        if (fb) {
          existing.value = true;
          form.performance = fb.performance || '';
          form.problems = fb.problems || '';
          form.homework = fb.homework || '';
          form.next_plan = fb.next_plan || '';
          form.rating = fb.rating;
          form.share_to_parent = fb.share_to_parent ? 1 : 0;
          doc.value = fb.doc || '';
          images.value = fb.images || [];
          for (const s of fb.ability_scores || []) form.scores[s.dim_id] = s.score;
        }
        // 模板列表。问不到不算错 —— 没有模板照样能写反馈，不能因为模板接口挂了就录不了课。
        try {
          const r = await feedbackApi.templates();
          templates.value = r.items || [];
        } catch (e) { /* 忽略 */ }
        // 润色模板单独拉（和上面那套不是一回事），失败同样不影响录反馈
        try {
          const r = await feedbackApi.docTemplates();
          docTemplates.value = r.items || [];
          // 默认选第一个（内置就是「完整课堂反馈（推荐）」）——
          // 不预选的话，第一次点润色只能得到干巴巴的四段，没人知道还有个模板能用。
          const first = docTemplates.value[0];
          if (first) {
            docTemplateId.value = first.id;
            docTemplateContent.value = first.content || '';
          }
        } catch (e) { /* 忽略 */ }
        // 取该学生各维度的历史分数作为参照 —— 有锚点，打分标准才稳定，
        // 否则这周给 3 星、下周给 4 星可能只是手感不同，雷达图的「变化」就成了噪声。
        //
        // 注意：如果正在修改的是已有反馈，「该维度的最新分」就是本节自己打的，
        // 拿它当锚点会显示成「上次 5」而其实是老师刚点的 5 —— 校准就失真了。
        // 所以修改场景要往前取一位（previous）。
        const editingThisLesson = existing.value;
        const stu = await studentsApi.get(props.lesson.student_id);
        const map = {};
        for (const d of (stu.ability && stu.ability.dims) || []) {
          const anchor = editingThisLesson ? d.previous : d.latest;
          if (anchor !== null && anchor !== undefined) map[d.dim_id] = anchor;
        }
        lastScores.value = map;
      } catch (e) {
        fail(e.message);
      }
      // 贴图：直接 Ctrl+V 粘贴截图。挂在 window 上，弹窗卸载时摘掉。
      window.addEventListener('paste', onPaste);
    });

    onBeforeUnmount(() => window.removeEventListener('paste', onPaste));

    function insert(field, text) {
      const cur = form[field] || '';
      form[field] = cur ? cur.replace(/\s*$/, '') + '\n' + text : text;
    }

    function setScore(dimId, value) {
      // 再点一次同一个分数 = 取消打分（允许只打部分维度）
      form.scores[dimId] = form.scores[dimId] === value ? undefined : value;
    }

    async function save() {
      saving.value = true;
      try {
        const payload = {
          performance: form.performance || null,
          problems: form.problems || null,
          homework: form.homework || null,
          next_plan: form.next_plan || null,
          rating: form.rating || null,
          share_to_parent: form.share_to_parent ? 1 : 0,
          images: images.value,
          // doc 始终一起提交：弹窗打开时就把服务端那份读进来了，所以这里的值
          // 就是「用户现在看到的样子」—— 包括他点了「清空」的空串。
          // （服务端对「没带 doc 字段」的请求仍然按「不改」处理，见 upsert_feedback。）
          doc: doc.value || '',
          ability_scores: Object.entries(form.scores)
            .filter(([, v]) => v)
            .map(([dimId, score]) => ({ dim_id: Number(dimId), score })),
        };
        await feedbackApi.save(props.lesson.id, payload);
        ok('反馈已保存');
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
    }

    /* ================= 模板 ================= */
    const currentTpl = () => templates.value.find(t => String(t.id) === String(templateId.value)) || null;

    /** 选模板：
     *  · 快捷短语一定跟着换（这是模板最直接的用处）
     *  · 四段文本只在**空的时候**填骨架 —— 已经写了内容的字段绝不默默覆盖，
     *    先问一句再动。反馈是老师一个字一个字写出来的，被清掉比多问一句难受得多。 */
    function applyTemplate() {
      const t = currentTpl();
      if (!t) { phrases.value = { ...PHRASES }; return; }
      phrases.value = { ...PHRASES, ...(t.phrases || {}) };
      const seeds = t.seeds || {};
      const keys = ['performance', 'problems', 'homework', 'next_plan'];
      const filled = keys.filter(k => (form[k] || '').trim());
      const seedKeys = keys.filter(k => (seeds[k] || '').trim());
      if (!seedKeys.length) return;
      if (filled.length) {
        if (!window.confirm(`「${t.name}」有预设文本，但你已经写了内容。\n要用模板文本覆盖吗？（取消 = 只换快捷短语，你的文字不动）`)) return;
        keys.forEach(k => { form[k] = seeds[k] || form[k] || ''; });
      } else {
        keys.forEach(k => { if (seeds[k]) form[k] = seeds[k]; });
      }
    }

    async function saveAsTemplate() {
      const name = (tplName.value || '').trim();
      if (!name) return warn('先给模板起个名字');
      try {
        const r = await feedbackApi.createTemplate({
          name,
          // 短语用当前工具栏里那组；四段文本存成「骨架」，
          // 相当于把这篇写好的反馈变成下次的起点。
          phrases: phrases.value,
          seeds: {
            performance: form.performance || '',
            problems: form.problems || '',
            homework: form.homework || '',
            next_plan: form.next_plan || '',
          },
        });
        templates.value.push(r);
        templateId.value = r.id;
        showTplSave.value = false;
        tplName.value = '';
        ok(`已存为模板「${r.name}」`);
      } catch (e) {
        fail(e.message);
      }
    }

    async function deleteTemplate() {
      const t = currentTpl();
      if (!t) return;
      if (t.is_builtin) return warn('内置模板不能删，可以另存一个新模板');
      if (!window.confirm(`删除模板「${t.name}」？已写好的反馈不受影响。`)) return;
      try {
        await feedbackApi.removeTemplate(t.id);
        templates.value = templates.value.filter(x => x.id !== t.id);
        templateId.value = '';
        phrases.value = { ...PHRASES };
        ok('模板已删除');
      } catch (e) {
        fail(e.message);
      }
    }

    /* ================= 配图 ================= */
    function pickImage() { fileEl.value && fileEl.value.click(); }

    async function uploadImage(file) {
      if (!file) return;
      uploading.value = true;
      try {
        const fd = new FormData();
        fd.append('file', file, file.name || 'paste.png');
        const r = await feedbackApi.uploadImage(fd);
        images.value = [...images.value, r.url];
      } catch (e) {
        fail(e.message);
      } finally {
        uploading.value = false;
      }
    }

    function onImageFile(e) {
      const f = e.target.files && e.target.files[0];
      if (f) uploadImage(f);
      e.target.value = '';
    }

    function onPaste(e) {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const it of items) {
        if (it.type && it.type.startsWith('image/')) {
          uploadImage(it.getAsFile());
          e.preventDefault();
          return;
        }
      }
    }

    function removeImage(i) { images.value = images.value.filter((_, idx) => idx !== i); }

    /* ================= AI 润色（整篇） ================= */
    // 逻辑：四段记录拼成一篇「原始记录」→ 连同模板一起发给 AI → 拿回一整篇文档。
    // 以前是按字段分别润色、逐框回填，那样得到的是四段碎语，成不了给家长看的文档。

    const currentDocTpl = () => docTemplates.value.find(t => String(t.id) === String(docTemplateId.value)) || null;

    /** 选了模板就把它的正文填进可编辑框（老师可以当场改，改完按改过的发）。 */
    function applyDocTemplate() {
      const t = currentDocTpl();
      docTemplateContent.value = t ? (t.content || '') : '';
    }

    /** 预览「到底会发出去什么」。隐私上这是必需的：老师得看得见有什么要离开本机。 */
    const willSend = computed(() => {
      const lines = ['## 课程信息'];
      lines.push(`学生：${props.lesson.student_name || ''}`);
      if (props.lesson.start_at) lines.push(`上课时间：${props.lesson.start_at.replace('T', ' ')}`);
      if (props.lesson.topic) lines.push(`本次内容：${props.lesson.topic}`);
      lines.push('', '## 老师填写的记录（分段、尚未整理）');
      for (const f of FIELDS) {
        const t = (form[f.key] || '').trim();
        if (t) lines.push(`〔${f.label}〕`, t, '');
      }
      return lines.join('\n').trim();
    });

    const fieldsPayload = () => {
      const o = {};
      for (const f of FIELDS) o[f.key] = form[f.key] || '';
      return o;
    };

    /** 点「AI 润色」只开询问窗口（模板 + 隐私提醒），真正发请求在 runPolish()。 */
    function openPolish() {
      if (!Object.values(fieldsPayload()).some(v => (v || '').trim())) {
        return warn('四个字段都是空的，先写点内容再润色');
      }
      showPolishAsk.value = true;
    }

    /** 真正调 AI。拿回来的是**建议稿**，必须老师点「采用」才写进整篇正文。 */
    async function runPolish() {
      showPolishAsk.value = false;
      polishing.value = true;
      try {
        const res = await feedbackApi.polish(props.lesson.id, {
          fields: fieldsPayload(),
          template_id: docTemplateId.value || null,
          // 只有老师改过模板正文时才把它一起发（服务端以它优先），
          // 否则发 null 让服务端按 id 取库里的那份 —— 少传一坨文本。
          template_content: (() => {
            const t = currentDocTpl();
            const cur = (docTemplateContent.value || '').trim();
            return cur && cur !== ((t && t.content) || '').trim() ? cur : null;
          })(),
          style: polishStyle.value || '',
        });
        polishResult.value = res;
        polishText.value = res.text || '';
      } catch (e) {
        fail(e.message);
      } finally {
        polishing.value = false;
      }
    }

    function adoptPolish() {
      doc.value = (polishText.value || '').trim();
      polishResult.value = null;
      if (!doc.value) return warn('内容是空的，没有可采用的');
      ok('已放入「整篇正文」（记得点保存反馈）');
    }

    // 存模板弹窗里亮一眼「到底要存什么」：模板正文有几百字，光看名字没法确认存的是哪一版。
    // ⚠️ 这个正则不能写在 template 里 —— 整个模板是个 JS 模板字符串，
    //    里面的 \n 会被 JS 先解释成真换行，Vue 编译器拿到的就是半个正则（实测直接白屏）。
    const docTplPreview = computed(() => {
      const t = (docTemplateContent.value || '').trim();
      return t ? t.slice(0, 40).replace(/\s+/g, ' ') + '…' : '';
    });

    async function saveDocTemplate() {
      const name = (docTplName.value || '').trim();
      if (!name) return warn('先给模板起个名字');
      if (!(docTemplateContent.value || '').trim()) return warn('模板内容是空的');
      try {
        const r = await feedbackApi.createDocTemplate({ name, content: docTemplateContent.value });
        docTemplates.value.push(r);
        docTemplateId.value = r.id;
        showDocTplSave.value = false;
        docTplName.value = '';
        ok(`已存为润色模板「${r.name}」`);
      } catch (e) {
        fail(e.message);
      }
    }

    async function deleteDocTemplate() {
      const t = currentDocTpl();
      if (!t) return;
      if (t.is_builtin) return warn('内置模板不能删，可以另存一个新模板');
      if (!window.confirm(`删除润色模板「${t.name}」？已有的反馈不受影响。`)) return;
      try {
        await feedbackApi.removeDocTemplate(t.id);
        docTemplates.value = docTemplates.value.filter(x => x.id !== t.id);
        docTemplateId.value = '';
        docTemplateContent.value = '';
        ok('模板已删除');
      } catch (e) {
        fail(e.message);
      }
    }

    /* ================= 整篇正文 ================= */
    async function copyDoc() {
      try {
        await navigator.clipboard.writeText(doc.value);
        ok('整篇正文已复制');
      } catch (e) {
        fail('复制失败，请手动选中复制');
      }
    }

    function clearDoc() {
      if (!window.confirm('清空整篇正文？四段快记不受影响。')) return;
      doc.value = '';
    }

    /* ================= 导出 ================= */
    function exportName(ext) {
      const stu = props.lesson.student_name || '学生';
      const when = (props.lesson.start_at || '').slice(0, 10);
      const base = `课后反馈-${stu}-${when}`.replace(/[\\/:*?"<>|]+/g, '_');
      return `${base}.${ext}`;
    }

    /** 有整篇正文就默认导整篇（那是老师确认过的成品），没有就导四段。 */
    const exportMode = computed(
      () => (doc.value.trim() ? exportSource.value : 'fields'),
    );

    async function doExport(fmt) {
      exporting.value = fmt;
      try {
        await feedbackApi.downloadExport(props.lesson.id, fmt, exportName(fmt), exportMode.value);
        ok(`已导出 ${fmt.toUpperCase()}`);
        showExport.value = false;
      } catch (e) {
        fail(e.message);
      } finally {
        exporting.value = '';
      }
    }

    const dimPreview = (dimId) => lastScores.value[dimId] ?? null;

    return { dims, form, saving, existing, lastScores, PHRASES, FIELDS, insert, setScore, save, hhmm, shortDate, warn, dimPreview,
             templates, templateId, phrases, applyTemplate, saveAsTemplate, deleteTemplate, currentTpl,
             showTplSave, tplName,
             doc, copyDoc, clearDoc,
             images, fileEl, uploading, pickImage, onImageFile, removeImage,
             polishing, polishResult, polishText, openPolish, runPolish, adoptPolish,
             showPolishAsk, polishStyle, willSend,
             docTemplates, docTemplateId, docTemplateContent, applyDocTemplate,
             currentDocTpl, showDocTplSave, docTplName, saveDocTemplate, deleteDocTemplate, docTplPreview,
             showExport, exporting, exportSource, exportMode, doExport };
  },
  template: `
  <Modal :title="'课后反馈 · ' + lesson.student_name" @close="$emit('close')">
    <div class="small muted" style="margin:-6px 0 14px">
      {{ shortDate(lesson.start_at) }} {{ hhmm(lesson.start_at) }}
      <span v-if="lesson.topic"> · {{ lesson.topic }}</span>
      <span v-if="existing" class="tag green" style="margin-left:6px">已有反馈，正在修改</span>
    </div>

    <!-- 模板：换一套快捷短语（可选骨架文本）。空字段才填，已写的要问过才覆盖。 -->
    <div class="row" style="align-items:center;gap:8px;margin-bottom:12px">
      <span class="small muted" style="flex:none">模板</span>
      <select v-model="templateId" @change="applyTemplate" style="flex:1">
        <option value="">默认（不使用模板）</option>
        <option v-for="t in templates" :key="t.id" :value="t.id">{{ t.name }}</option>
      </select>
      <button class="btn sm" style="flex:none" @click="showTplSave = true">存为模板</button>
      <button v-if="currentTpl() && !currentTpl().is_builtin" class="btn ghost sm"
              style="flex:none;color:var(--danger)" @click="deleteTemplate">删除模板</button>
    </div>

    <div v-for="f in [
        { key:'performance', label:'课堂表现', ph:'今天课上怎么样？' },
        { key:'problems',    label:'存在问题', ph:'哪里卡住了？' },
        { key:'homework',    label:'作业布置', ph:'布置了什么？' },
        { key:'next_plan',   label:'下次安排', ph:'下节课讲什么？' }
      ]" :key="f.key" class="field">
      <label>{{ f.label }} <span class="muted small">（可留空）</span></label>
      <textarea :rows="2" v-model="form[f.key]" :placeholder="f.ph"></textarea>
      <div class="chips" style="margin-top:5px">
        <span v-for="p in (phrases[f.key] || [])" :key="p" class="chip" @click="insert(f.key, p)">{{ p }}</span>
      </div>
    </div>

    <!-- 配图：可以直接 Ctrl+V 粘截图，也可以从文件选。 -->
    <div class="field">
      <label>
        附图 <span class="muted small">（板书照片 / 作业截图；直接 Ctrl+V 也能贴，导出的 txt 不含图片）</span>
      </label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <div v-for="(u, i) in images" :key="u" style="position:relative">
          <img :src="u" alt="" style="height:74px;border:1px solid var(--border);border-radius:var(--radius-sm)">
          <button class="btn ghost sm" title="移除"
                  style="position:absolute;top:-8px;right:-8px;padding:0 6px;line-height:18px"
                  @click="removeImage(i)">×</button>
        </div>
        <button class="btn sm" :disabled="uploading" @click="pickImage">
          {{ uploading ? '上传中…' : '＋ 贴图' }}
        </button>
        <input ref="fileEl" type="file" accept="image/*" style="display:none" @change="onImageFile">
      </div>
    </div>

    <!-- 整篇正文：四段是原料，这一篇是成品。导出时优先用它。 -->
    <div class="field">
      <label>
        整篇正文
        <span class="muted small">
          （AI 按模板整理的成品，可以直接发家长；导出 Word/PDF/txt 时优先用这一篇）
        </span>
      </label>
      <textarea v-model="doc" rows="10"
                placeholder="还没有整篇正文。点左下角「AI 润色」按模板生成一篇，也可以直接在这里手写。"></textarea>
      <div class="row" style="gap:8px;margin-top:6px">
        <span class="small muted">{{ (doc || '').length }} 字</span>
        <span class="spacer"></span>
        <button v-if="doc" class="btn sm ghost" @click="copyDoc">复制</button>
        <button v-if="doc" class="btn sm ghost" style="color:var(--danger)" @click="clearDoc">清空</button>
      </div>
    </div>

    <div class="field">
      <label>能力评分 <span class="muted small">（可只打几项，未打的沿用历史值；再点一次可取消）</span></label>
      <div v-for="d in dims" :key="d.id" class="rate-row">
        <span class="dim">{{ d.name }}</span>
        <div class="dots">
          <button v-for="v in 5" :key="v" type="button"
                  class="dot" :class="{ on: form.scores[d.id] === v }"
                  @click="setScore(d.id, v)">{{ v }}</button>
        </div>
        <span class="small muted" v-if="dimPreview(d.id)">上次 {{ dimPreview(d.id) }}</span>
      </div>
    </div>

    <div class="field">
      <label>本次整体状态</label>
      <div class="dots">
        <button v-for="v in 5" :key="v" type="button"
                class="dot" :class="{ on: form.rating === v }"
                @click="form.rating = form.rating === v ? null : v">{{ v }}</button>
      </div>
    </div>

    <label class="small" style="display:flex; align-items:center; gap:6px">
      <input type="checkbox" v-model="form.share_to_parent" :true-value="1" :false-value="0">
      本节课内容可发给家长
    </label>

    <template #foot>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn" :disabled="polishing" @click="openPolish">
        {{ polishing ? '润色中…' : 'AI 润色' }}
      </button>
      <button class="btn" @click="showExport = true">导出</button>
      <button class="btn primary" :disabled="saving" @click="save">
        {{ saving ? '保存中…' : '保存反馈' }}
      </button>
    </template>
  </Modal>

  <!-- 存为反馈模板（快捷短语 + 骨架文本） -->
  <Modal v-if="showTplSave" title="存为反馈模板" @close="showTplSave = false">
    <p class="muted small" style="margin-top:0">
      会把<strong>当前的快捷短语</strong>和<strong>四段文字</strong>一起存成模板。
      下次选它，短语直接可用；四段文本会在字段为空时自动填入，已经写了内容则会先问你。
    </p>
    <div class="field">
      <label>模板名称</label>
      <input type="text" v-model="tplName" placeholder="如：初三冲刺课" @keyup.enter="saveAsTemplate">
    </div>
    <template #foot>
      <button class="btn ghost" @click="showTplSave = false">取消</button>
      <button class="btn primary" @click="saveAsTemplate">保存模板</button>
    </template>
  </Modal>

  <!-- 存为润色模板（一整篇文档的格式与文风） -->
  <Modal v-if="showDocTplSave" title="存为润色模板" @close="showDocTplSave = false">
    <p class="muted small" style="margin-top:0">
      存的是「AI 润色」窗口里那个<strong>模板内容</strong>框里的东西 ——
      也就是 AI 要仿照的结构与文风。下次润色时直接选它即可，不用再粘一遍。
    </p>
    <div class="field">
      <label>模板名称</label>
      <input type="text" v-model="docTplName" placeholder="如：完整课堂反馈" @keyup.enter="saveDocTemplate">
    </div>
    <!-- 把要存的内容亮一眼：模板正文很长，光看名字没法确认存的是哪一版 -->
    <div class="small muted">
      将要保存的内容：{{ (docTemplateContent || '').length }} 字
      <span v-if="docTplPreview">· {{ docTplPreview }}</span>
      <span v-else style="color:var(--danger)">（空的，请先在润色窗口里填写模板内容）</span>
    </div>
    <template #foot>
      <button class="btn ghost" @click="showDocTplSave = false">取消</button>
      <button class="btn primary" @click="saveDocTemplate">保存模板</button>
    </template>
  </Modal>

  <!-- AI 润色：模板 + 额外要求 + 「到底会发出去什么」 -->
  <Modal v-if="showPolishAsk" title="AI 润色（整篇）" wide @close="showPolishAsk = false">
    <div class="small" style="margin-bottom:12px;color:var(--warning, #d97706)">
      ⚠️ 接下来会把下面的内容（含学生姓名）发送到你配置的 AI 服务。
      接口地址与密钥在「设置 → AI 润色」里配置；不配置就不会联网。
    </div>

    <div class="field">
      <label>仿照的模板 <span class="muted small">（决定分几个栏目、什么语气；整段会一起发给 AI）</span></label>
      <div class="row">
        <select v-model="docTemplateId" @change="applyDocTemplate">
          <option value="">不套用模板（只把四段整理成一篇）</option>
          <option v-for="t in docTemplates" :key="t.id" :value="t.id">{{ t.name }}</option>
        </select>
        <button class="btn sm" style="flex:none" @click="showDocTplSave = true">存为新模板</button>
        <button v-if="currentDocTpl() && !currentDocTpl().is_builtin"
                class="btn sm ghost" style="flex:none;color:var(--danger)"
                @click="deleteDocTemplate">删除模板</button>
      </div>
    </div>

    <div class="field">
      <label>
        模板内容
        <span class="muted small">（可以现场改，改完就按改过的发；满意了可以「存为新模板」）</span>
      </label>
      <textarea v-model="docTemplateContent" rows="7"
                placeholder="写清结构（有几个栏目、每栏写什么），再给一小段示例说明语气。"></textarea>
    </div>

    <div class="field">
      <label>额外要求 <span class="muted small">（可留空）</span></label>
      <input type="text" v-model="polishStyle" placeholder="如：保持简洁，语气对家长友好" @keyup.enter="runPolish">
    </div>

    <details>
      <summary class="small muted" style="cursor:pointer">看看具体会发出去什么</summary>
      <pre class="small" style="white-space:pre-wrap;margin:6px 0 0;background:var(--panel-2);border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px;max-height:200px;overflow:auto">{{ willSend }}</pre>
      <div class="small muted" style="margin-top:4px">（另外还会带上上面那份模板正文和额外要求。）</div>
    </details>

    <template #foot>
      <button class="btn ghost" @click="showPolishAsk = false">取消</button>
      <button class="btn primary" :disabled="polishing" @click="runPolish">
        {{ polishing ? '润色中…' : '开始润色' }}
      </button>
    </template>
  </Modal>

  <!-- 导出：先选导哪一份内容，再选格式 -->
  <Modal v-if="showExport" title="导出这条反馈" @close="showExport = false">
    <div class="field">
      <label>导出内容</label>
      <div v-if="doc.trim()" style="display:flex;gap:14px;flex-wrap:wrap">
        <label class="small" style="display:flex;align-items:center;gap:6px">
          <input type="radio" value="auto" v-model="exportSource">
          整篇正文（推荐 —— 你确认过的成品）
        </label>
        <label class="small" style="display:flex;align-items:center;gap:6px">
          <input type="radio" value="fields" v-model="exportSource">
          四段快记
        </label>
      </div>
      <div v-else class="small muted">
        还没有整篇正文，将按四段快记导出。点「AI 润色」可以生成一篇完整的。
      </div>
    </div>
    <p class="muted small" style="margin-top:0">
      三种格式面向不同场合：<strong>txt</strong> 最通用（微信直接发，按需求不带图片）；
      <strong>Word</strong> 带附图、可再编辑；<strong>PDF</strong> 版式最稳，发给家长最好看。
    </p>
    <div class="row" style="gap:10px">
      <button class="btn" :disabled="!!exporting" @click="doExport('txt')">
        {{ exporting === 'txt' ? '生成中…' : '导出 TXT' }}
      </button>
      <button class="btn" :disabled="!!exporting" @click="doExport('docx')">
        {{ exporting === 'docx' ? '生成中…' : '导出 Word（.docx）' }}
      </button>
      <button class="btn" :disabled="!!exporting" @click="doExport('pdf')">
        {{ exporting === 'pdf' ? '生成中…（Word 启动稍几秒）' : '导出 PDF' }}
      </button>
    </div>
    <div v-if="images.length" class="small muted" style="margin-top:10px">
      这篇有 {{ images.length }} 张附图：Word / PDF 会带上，TXT 不会。
    </div>
    <div class="small muted" style="margin-top:6px">
      PDF 需要本机装有 Microsoft Word（服务端用它排版）；没有的话请先导 Word 再另存为 PDF。
    </div>
    <template #foot>
      <button class="btn ghost" @click="showExport = false">关闭</button>
    </template>
  </Modal>

  <!-- AI 润色结果：左边看发了什么，右边是**可编辑**的成品 -->
  <Modal v-if="polishResult" title="AI 润色结果（整篇）" wide @close="polishResult = null">
    <p class="muted small" style="margin-top:0">
      右边就是整理好的整篇正文，<strong>可以直接改</strong>。点「采用」放进「整篇正文」框，
      再点「保存反馈」才真正存下来。
      <span v-if="polishResult.template_name">· 仿照模板：{{ polishResult.template_name }}</span>
      <span v-if="polishResult.model">· 模型：{{ polishResult.model }}</span>
      <span v-if="polishResult.usage">
        · 用量：输入 {{ polishResult.usage.prompt ?? '—' }} / 输出 {{ polishResult.usage.completion ?? '—' }} tokens
      </span>
    </p>
    <div class="grid cols-2">
      <div>
        <div class="small muted">发出去的原始记录</div>
        <pre class="small" style="white-space:pre-wrap;margin:4px 0 0;max-height:300px;overflow:auto;border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px;background:var(--panel-2)">{{ polishResult.draft }}</pre>
      </div>
      <div>
        <div class="small muted">AI 整理后（可编辑）</div>
        <textarea v-model="polishText" rows="14" style="margin-top:4px"></textarea>
      </div>
    </div>
    <template #foot>
      <button class="btn ghost" @click="polishResult = null">放弃</button>
      <button class="btn primary" :disabled="!polishText.trim()" @click="adoptPolish">采用为整篇正文</button>
    </template>
  </Modal>`,
};
