// 课后反馈录入 —— 决定这个系统生死的一个界面。
//
// 设计原则（开发文档 §6.5）：目标是「上完一节课 3 分钟内记完」。
// 具体手段：
//   · 从课表点进来就是这门课，不用先选学生再选时间
//   · 四段式各自带快捷短语，一点即插，省掉打字
//   · 能力评分「允许只打部分」，没打的下次仍按历史值算，绝不强制填满
//   · 所有字段都可留空 —— 只写一句话也能存。卡住一次，这个工具就会被弃用。
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { abilityApi, feedbackApi, studentsApi } from '../api.js';
import { fail, hhmm, ok, shortDate, warn } from '../store.js';
import Modal from './Modal.js';

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

    // ---- AI 润色 / 导出 ----
    const polishing = ref(false);
    const polishResult = ref(null);      // { fields: {...}, usage: {...}, model } —— 建议稿，需老师确认
    const showExport = ref(false);
    const exporting = ref('');
    // 润色前先问风格；模板另存要先起名字。
    // 两个都用内联小弹窗而不是 window.prompt —— prompt 在部分环境（含内嵌浏览器）
    // 直接抛 “prompt() is not supported”，而且原生弹窗不可控、也不能写多行说明。
    const showPolishAsk = ref(false);
    const polishStyle = ref('保持简洁，语气对家长友好');
    const showTplSave = ref(false);
    const tplName = ref('');

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
          images.value = fb.images || [];
          for (const s of fb.ability_scores || []) form.scores[s.dim_id] = s.score;
        }
        // 模板列表。问不到不算错 —— 没有模板照样能写反馈，不能因为模板接口挂了就录不了课。
        try {
          const r = await feedbackApi.templates();
          templates.value = r.items || [];
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

    /* ================= AI 润色 ================= */
    /** 点「AI 润色」只是打开询问窗口（风格 + 隐私提醒），真正发请求在 runPolish()。 */
    function polish() {
      const fields = { performance: form.performance, problems: form.problems,
                       homework: form.homework, next_plan: form.next_plan };
      if (!Object.values(fields).some(v => (v || '').trim())) {
        return warn('四个字段都是空的，先写点内容再润色');
      }
      showPolishAsk.value = true;
    }

    /** 真正调 AI。返回的是**建议稿**，必须老师点「采用」才写回去。 */
    async function runPolish() {
      const fields = { performance: form.performance, problems: form.problems,
                       homework: form.homework, next_plan: form.next_plan };
      showPolishAsk.value = false;
      polishing.value = true;
      try {
        polishResult.value = await feedbackApi.polish(props.lesson.id,
          { fields, style: polishStyle.value || '' });
      } catch (e) {
        fail(e.message);
      } finally {
        polishing.value = false;
      }
    }

    function adoptPolish() {
      const f = (polishResult.value && polishResult.value.fields) || {};
      for (const k of ['performance', 'problems', 'homework', 'next_plan']) {
        if ((f[k] || '').trim()) form[k] = f[k];
      }
      polishResult.value = null;
      ok('已采用润色结果（记得点保存）');
    }

    /* ================= 导出 ================= */
    function exportName(ext) {
      const stu = props.lesson.student_name || '学生';
      const when = (props.lesson.start_at || '').slice(0, 10);
      const base = `课后反馈-${stu}-${when}`.replace(/[\\/:*?"<>|]+/g, '_');
      return `${base}.${ext}`;
    }

    async function doExport(fmt) {
      exporting.value = fmt;
      try {
        await feedbackApi.downloadExport(props.lesson.id, fmt, exportName(fmt));
        ok(`已导出 ${fmt.toUpperCase()}`);
        showExport.value = false;
      } catch (e) {
        fail(e.message);
      } finally {
        exporting.value = '';
      }
    }

    const dimPreview = (dimId) => lastScores.value[dimId] ?? null;

    return { dims, form, saving, existing, lastScores, PHRASES, insert, setScore, save, hhmm, shortDate, warn, dimPreview,
             templates, templateId, phrases, applyTemplate, saveAsTemplate, deleteTemplate, currentTpl,
             showTplSave, tplName,
             images, fileEl, uploading, pickImage, onImageFile, removeImage,
             polishing, polishResult, polish, runPolish, adoptPolish, showPolishAsk, polishStyle,
             showExport, exporting, doExport };
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
      <button class="btn" :disabled="polishing" @click="polish">
        {{ polishing ? '润色中…' : 'AI 润色' }}
      </button>
      <button class="btn" @click="showExport = true">导出</button>
      <button class="btn primary" :disabled="saving" @click="save">
        {{ saving ? '保存中…' : '保存反馈' }}
      </button>
    </template>
  </Modal>

  <!-- 存为模板：起个名字 -->
  <Modal v-if="showTplSave" title="存为模板" @close="showTplSave = false">
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

  <!-- AI 润色前的询问：风格 + 隐私提醒 -->
  <Modal v-if="showPolishAsk" title="AI 润色" @close="showPolishAsk = false">
    <div class="small" style="margin-bottom:12px;color:var(--warning, #d97706)">
      ⚠️ 接下来会把四段内容（可能包含学生姓名）发送到你配置的 AI 服务。
      接口地址与密钥在「设置 → AI 润色」里配置；不配置就不会联网。
    </div>
    <div class="field">
      <label>风格要求 <span class="muted small">（可留空）</span></label>
      <input type="text" v-model="polishStyle" placeholder="如：保持简洁，语气对家长友好" @keyup.enter="runPolish">
    </div>
    <div class="small muted">
      润色结果会先给你看，确认后才替换；不点「保存反馈」就不会生效。
    </div>
    <template #foot>
      <button class="btn ghost" @click="showPolishAsk = false">取消</button>
      <button class="btn primary" :disabled="polishing" @click="runPolish">
        {{ polishing ? '润色中…' : '开始润色' }}
      </button>
    </template>
  </Modal>

  <!-- 导出：txt / Word / PDF -->
  <Modal v-if="showExport" title="导出这条反馈" @close="showExport = false">
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

  <!-- AI 润色结果：先看建议稿，点「采用」才写回去 -->
  <Modal v-if="polishResult" title="AI 润色建议" wide @close="polishResult = null">
    <p class="muted small" style="margin-top:0">
      下面是建议稿，<strong>不会自动替换</strong>你写的内容。确认没问题再点「采用」；
      采用后仍可自己改，不保存就无效。
      <span v-if="polishResult.model">（模型：{{ polishResult.model }}）</span>
      <span v-if="polishResult.usage">
        · 用量：输入 {{ polishResult.usage.prompt ?? '—' }} / 输出 {{ polishResult.usage.completion ?? '—' }} tokens
      </span>
    </p>
    <div v-for="f in [
        { key:'performance', label:'课堂表现' },
        { key:'problems',    label:'存在问题' },
        { key:'homework',    label:'作业布置' },
        { key:'next_plan',   label:'下次安排' }
      ]" :key="f.key">
      <div v-if="(polishResult.fields[f.key] || '').trim()" class="field">
        <label>{{ f.label }}</label>
        <div class="grid cols-2">
          <div>
            <div class="small muted">原文</div>
            <div class="small" style="white-space:pre-wrap;border:1px solid var(--border);border-radius:var(--radius-sm);padding:8px;background:var(--panel-2)">{{ form[f.key] || '（空）' }}</div>
          </div>
          <div>
            <div class="small muted">润色后</div>
            <div class="small" style="white-space:pre-wrap;border:1px solid var(--primary);border-radius:var(--radius-sm);padding:8px">{{ polishResult.fields[f.key] }}</div>
          </div>
        </div>
      </div>
    </div>
    <template #foot>
      <button class="btn ghost" @click="polishResult = null">放弃</button>
      <button class="btn primary" @click="adoptPolish">采用润色结果</button>
    </template>
  </Modal>`,
};
