// 记一次考试 —— 学生级记录（**不挂课**），挂在学生详情页的「考试情况」里。
//
// 需求原话（用户 2026-10-11）：日期（不一定具体）/ 考试类型 / 成绩 / 等级 /
// 试题与答题卡（上传文件或图片，能删）/ 考情分析（至少两套模板可选）。
//
// 几条刻意的设计（改动前请先读）：
//   · **日期是自由文本**。「9 月中旬那次月考」也是有效输入 ——
//     用日期控件逼人精确到日，他多半会直接放弃填这一栏。旁边给「今天 / 本月」两个
//     快捷按钮，想精确的人一点就有。
//   · **成绩与满分分开**：只写「85」看不出是 85/100 还是 85/150。
//     填了满分就显示得分率，不填也不拦着（有的卷子只报等级）。
//   · **附件先落暂存区**（后端 exams_cache），保存这次考试时才搬进
//     `students/<学生>/exams/<日期>_<类型>/`。所以填表过程中随便改日期/类型，
//     文件都不会被牵动；上传完没保存也能逐份删（后端是真删，不是只从列表里拿掉）。
//     ⚠️ 传了没保存就关窗会留下暂存文件，后端按 48 小时自动清 ——
//     这里**刻意不在关窗时删**：误删老师刚传的东西，比留两天垃圾糟得多。
//   · **考情分析按模板分段写**：换模板时已经写下的字一个字都不丢
//     （合并规则在 src/examAnalysis.js，那里有 node 单测）。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { examsApi } from '../api.js';
import { fail, fmtBytes, ok, warn } from '../store.js';
import { needLocalCopyMessage, pickPastedAsset } from '../pasteAsset.js';
import {
  mergeSections, nextExtraKey, normalizeSections,
} from '../examAnalysis.js';
import Modal from './Modal.js';

/** 常见考试类型。只是**起点**，类型用 input 自己写（「入学分班考」也得能填）。 */
const DEFAULT_TYPES = ['周考', '月考', '期中考', '期末考', '模拟考', '联考', '升学考'];
const ROLE_OPTIONS = [
  { value: 'paper', label: '试题' },
  { value: 'sheet', label: '答题卡' },
  { value: 'other', label: '其他' },
];

const isImage = (name) => /\.(png|jpe?g|gif|webp|bmp)$/i.test(String(name || ''));

/** 数字输入 -> 数字或 null。**空就是 null，不归一成 0** ——
 *  用 0 顶替会把得分率和以后的趋势一起带偏。 */
function toNum(v) {
  const t = String(v ?? '').trim();
  if (!t) return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

export default {
  name: 'ExamDialog',
  components: { Modal },
  props: {
    /** 要改的那条记录；新建时传 null。 */
    exam: { type: Object, default: null },
    student: { type: Object, required: true },
    /** 学生归档文件夹名（u1_李芹旭），只用于把「文件会归到哪」显示清楚。 */
    folder: { type: String, default: '' },
    /** 库里已经用过的考试类型，拼在常见类型后面一起给 chips。 */
    typeOptions: { type: Array, default: () => [] },
  },
  emits: ['close', 'saved'],
  setup(props, { emit }) {
    const loading = ref(true);
    const saving = ref(false);
    const uploading = ref(false);
    const templates = ref([]);
    const fileEl = ref(null);
    // 选中文件时的归类：三个按钮各设一次，再打开文件对话框
    let pendingRole = 'other';

    const examDate = ref('');
    const examType = ref('');
    const score = ref('');
    const fullScore = ref('');
    const grade = ref('');
    const templateId = ref(null);
    const sections = ref([]);
    const files = ref([]);

    const preview = ref(null);

    const typeOptions = computed(() => {
      const out = [];
      for (const t of [...DEFAULT_TYPES, ...(props.typeOptions || [])]) {
        if (t && !out.includes(t)) out.push(t);
      }
      return out;
    });

    const scoreRate = computed(() => {
      const s = toNum(score.value);
      const f = toNum(fullScore.value);
      if (s === null || !f) return null;
      return Number(((s / f) * 100).toFixed(1));
    });

    const filledCount = computed(
      () => sections.value.filter((s) => String(s.text || '').trim()).length);

    /** 文件会归到哪儿。已有记录显示真实目录；还没保存的按当前日期/类型**预计**。 */
    const archiveHint = computed(() => {
      if (props.exam && props.exam.dir_rel) return props.exam.dir_rel;
      const d = (examDate.value || '').trim() || '未标日期';
      const t = (examType.value || '').trim() || '未标类型';
      const who = props.folder || '学生目录';
      return `uploads/students/${who}/exams/${d}_${t}/（保存后建立）`;
    });

    function rowFromArchived(f) {
      return {
        ...f,
        cached: false,
        thumb: isImage(f.name) ? examsApi.rawUrl(f.id, true) : '',
        link: examsApi.rawUrl(f.id),
      };
    }

    async function load() {
      loading.value = true;
      try {
        const tps = await examsApi.templates();
        templates.value = tps.items || [];
        if (props.exam) {
          const e = props.exam;
          examDate.value = e.exam_date || '';
          examType.value = e.exam_type || '';
          score.value = e.score === null || e.score === undefined ? '' : String(e.score);
          fullScore.value = e.full_score === null || e.full_score === undefined ? '' : String(e.full_score);
          grade.value = e.grade || '';
          const an = e.analysis || {};
          templateId.value = an.template_id || null;
          sections.value = normalizeSections(an.sections);
          files.value = (e.files || []).map(rowFromArchived);
        } else {
          // 新建：先套上第一套内置模板。对着白纸发呆比对着骨架填慢得多。
          applyTemplate(templates.value[0] || null);
        }
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    /** 套用一套模板。**已写的字一个字都不丢**（合并规则见 examAnalysis.mergeSections）。 */
    function applyTemplate(tpl) {
      const r = mergeSections(sections.value, tpl ? tpl.sections : []);
      sections.value = r.sections;
      templateId.value = tpl ? tpl.id : null;
      if (tpl && (r.carried || r.extra)) {
        const n = r.carried + r.extra;
        ok(`已套用「${tpl.name}」：原来写好的 ${n} 段都保留了`);
      }
    }

    function onTemplateChange(v) {
      const id = Number(v) || 0;
      applyTemplate(templates.value.find((t) => t.id === id) || null);
    }

    function fillToday() {
      const d = new Date();
      const p = (n) => String(n).padStart(2, '0');
      examDate.value = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    }

    function fillThisMonth() {
      const d = new Date();
      examDate.value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
    }

    function addSection() {
      sections.value = [...sections.value, {
        key: nextExtraKey(sections.value), label: '补充', hint: '', text: '', extra: true,
      }];
    }

    function removeSection(i) {
      const s = sections.value[i];
      if (String(s.text || '').trim()
          && !window.confirm(`「${s.label || '这一段'}」里已经写了字，确认删掉？`)) return;
      sections.value = sections.value.filter((_, j) => j !== i);
    }

    async function saveAsTemplate() {
      const name = window.prompt('把这套段落存成模板，起个名字：', '我的考情分析');
      if (!name || !name.trim()) return;
      try {
        await examsApi.createTemplate({
          name: name.trim(),
          sections: sections.value.map((s) => ({ key: s.key, label: s.label, hint: s.hint })),
        });
        templates.value = (await examsApi.templates()).items || [];
        ok('模板已保存，下次记考试就能直接选它');
      } catch (e) {
        fail(e.message);
      }
    }

    // ---------------- 附件 ----------------
    function pick(role) {
      pendingRole = role;
      fileEl.value && fileEl.value.click();
    }

    function onFilePicked(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';              // 清掉才能反复选同一个文件
      uploadFile(f, pendingRole);
    }

    /** 选文件 / 选图片 / 粘截图走**同一条路**，所以暂存、归档、删除行为天然一致。 */
    async function uploadFile(f, role) {
      if (!f) return;
      uploading.value = true;
      try {
        const fd = new FormData();
        fd.append('file', f, f.name || 'paste.png');
        const r = await examsApi.upload(fd, role || 'other');
        const isImg = !!(f.type && f.type.startsWith('image/'));
        const thumb = isImg ? URL.createObjectURL(f) : '';
        files.value = [...files.value, {
          ...r, role: role || 'other', cached: true, thumb, link: thumb,
        }];
        ok(`已选「${r.name}」；保存这次考试后会归档`);
      } catch (e) {
        fail(e.message);
      } finally {
        uploading.value = false;
      }
    }

    /** 把 data: URL（网页内嵌图）变回文件再上传 —— 内容已经在本地，不联网。 */
    async function uploadDataUrl(dataUrl) {
      try {
        const blob = await (await fetch(dataUrl)).blob();
        await uploadFile(new File([blob], 'paste.png', { type: blob.type || 'image/png' }), 'other');
      } catch (e) {
        fail('这张图读不出来：' + e.message);
      }
    }

    function onPaste(e) {
      const found = pickPastedAsset(e);
      if (!found) return;                // 普通文字：不插手
      e.preventDefault();
      if (found.kind === 'file') return uploadFile(found.file, 'other');
      if (found.kind === 'data') return uploadDataUrl(found.dataUrl);
      if (found.kind === 'url') return warn(needLocalCopyMessage(found.url));
      warn('剪贴板里没有可以直接用的图片。试试截图后再贴，或点上面的按钮选文件。');
    }

    /** 改归类。暂存的只改本地（保存时一起提交）；已归档的当场落库。 */
    async function setRole(row, role) {
      const old = row.role;
      row.role = role;
      if (row.cached) return;
      try {
        await examsApi.setFileRole(row.id, role);
      } catch (e) {
        row.role = old;
        fail(e.message);
      }
    }

    async function removeRow(row) {
      const label = row.stored_name || row.name;
      const tail = row.cached ? '（这份还没保存，会从暂存区删掉）' : '（磁盘上的原件也一起删）';
      if (!window.confirm(`删掉「${label}」？${tail}`)) return;
      try {
        if (row.cached) await examsApi.removeCache(row.cache_key);
        else await examsApi.removeFile(row.id);
        if (row.thumb && row.thumb.startsWith('blob:')) URL.revokeObjectURL(row.thumb);
        files.value = files.value.filter((x) => x !== row);
      } catch (e) {
        fail(e.message);
      }
    }

    function openPreview(row) {
      if (!row.thumb && !isImage(row.name)) return;
      preview.value = {
        name: row.stored_name || row.name,
        url: row.link || row.thumb,
        download: row.cached ? '' : examsApi.rawUrl(row.id),
      };
    }

    // ---------------- 保存 / 删除 ----------------
    async function save() {
      saving.value = true;
      try {
        const cur = templates.value.find((t) => t.id === templateId.value);
        const analysis = {
          template_id: templateId.value,
          // 模板名一起快照：模板日后改名/删掉，这条记录还显示得出来
          template_name: cur ? cur.name
            : ((props.exam && props.exam.analysis && props.exam.analysis.template_name) || ''),
          sections: sections.value.map((s) => ({
            key: s.key, label: s.label, hint: s.hint, text: s.text,
          })),
        };
        const payload = {
          exam_date: (examDate.value || '').trim(),
          exam_type: (examType.value || '').trim(),
          score: toNum(score.value),
          full_score: toNum(fullScore.value),
          grade: (grade.value || '').trim(),
          analysis,
          // 只提交**还在暂存区**的那些；已归档的不用重提
          files: files.value.filter((f) => f.cached)
            .map((f) => ({ cache_key: f.cache_key, role: f.role, name: f.name })),
        };
        if (props.exam) await examsApi.update(props.exam.id, payload);
        else await examsApi.create({ student_id: props.student.id, ...payload });
        ok(props.exam ? '考试记录已更新' : '考试记录已保存');
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
    }

    async function removeExam() {
      if (!window.confirm('删掉这次考试记录？（试题/答题卡的归档文件也会一起删）')) return;
      saving.value = true;
      try {
        await examsApi.remove(props.exam.id);
        ok('这次考试记录已删掉');
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
    }

    onMounted(() => {
      load();
      // 贴图挂在 window 上：焦点在哪个输入框都能贴。卸载时必须摘掉。
      window.addEventListener('paste', onPaste);
    });
    onBeforeUnmount(() => {
      window.removeEventListener('paste', onPaste);
      // 只回收本地 blob URL。⚠️ **不删暂存文件** —— 关窗（尤其误触）就把老师
      // 刚传的卷子删掉，比留两天垃圾糟得多；过期的暂存由后端 48 小时后清。
      files.value.forEach((f) => {
        if (f.thumb && f.thumb.startsWith('blob:')) URL.revokeObjectURL(f.thumb);
      });
    });

    return {
      loading, saving, uploading, templates, fileEl, preview,
      examDate, examType, score, fullScore, grade, templateId, sections, files,
      typeOptions, scoreRate, filledCount, archiveHint, ROLE_OPTIONS,
      applyTemplate, onTemplateChange, fillToday, fillThisMonth,
      addSection, removeSection, saveAsTemplate,
      pick, onFilePicked, setRole, removeRow, openPreview,
      save, removeExam, fmtBytes,
    };
  },
  template: `
  <Modal wide :title="(exam ? '改考试记录' : '记一次考试') + ' · ' + student.name" @close="$emit('close')">
    <div v-if="loading" class="empty">加载中…</div>
    <template v-else>
      <!-- ① 基本信息：日期（可只写大概）、类型、成绩 / 满分 / 等级 -->
      <div class="field">
        <label>考试日期 <span class="muted small">（可以只写大概：「2026-09」「9 月中旬」都行）</span></label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input v-model="examDate" placeholder="例如 2026-09-15 / 2026-09 / 9 月中旬"
                 style="max-width:280px">
          <button class="btn sm ghost" @click="fillToday">今天</button>
          <button class="btn sm ghost" @click="fillThisMonth">本月</button>
        </div>
      </div>

      <div class="field">
        <label>考试类型</label>
        <div class="chips">
          <span v-for="t in typeOptions" :key="t" class="chip"
                :class="{ on: examType === t }"
                @click="examType = (examType === t ? '' : t)">{{ t }}</span>
        </div>
        <input v-model="examType" placeholder="也可以自己写：入学分班考 / 单元测 …"
               style="margin-top:6px;max-width:300px">
      </div>

      <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">
        <div class="field" style="margin-bottom:0">
          <label>成绩</label>
          <input v-model="score" type="number" step="0.5" style="width:110px" placeholder="85">
        </div>
        <div class="field" style="margin-bottom:0">
          <label>满分 <span class="muted small">（填了才算出得分率）</span></label>
          <input v-model="fullScore" type="number" step="0.5" style="width:120px" placeholder="100">
        </div>
        <div class="field" style="margin-bottom:0">
          <label>等级 <span class="muted small">（A / 良好 …）</span></label>
          <input v-model="grade" style="width:150px" placeholder="可留空">
        </div>
        <div v-if="scoreRate !== null" style="padding-bottom:7px">
          <span class="tag blue">得分率 {{ scoreRate }}%</span>
        </div>
      </div>

      <!-- ② 试题与答题卡：先落暂存区，保存这次考试时才归档 -->
      <div class="field" style="margin-top:14px">
        <label>
          试题与答题卡
          <span class="muted small">（PDF / 照片 / 截图都行；直接 Ctrl+V 也能贴）</span>
        </label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn sm" :disabled="uploading" @click="pick('paper')">＋ 传试题</button>
          <button class="btn sm" :disabled="uploading" @click="pick('sheet')">＋ 传答题卡</button>
          <button class="btn sm ghost" :disabled="uploading" @click="pick('other')">＋ 其他</button>
          <input ref="fileEl" type="file" style="display:none" @change="onFilePicked">
          <span v-if="uploading" class="small muted">上传中…</span>
          <span v-else class="small muted">共 {{ files.length }} 份</span>
        </div>

        <div v-for="f in files" :key="f.cache_key || f.id" class="exam-file">
          <a v-if="f.thumb" :href="f.link || '#'" target="_blank" rel="noopener"
             style="flex:none" title="点一下放大看" @click.prevent="openPreview(f)">
            <img :src="f.thumb" alt="" class="exam-thumb">
          </a>
          <span v-else class="tag" style="flex:none">{{ f.kind_cn || f.kind }}</span>
          <div style="flex:1;min-width:0">
            <a v-if="f.link" :href="f.link" target="_blank" rel="noopener">{{ f.stored_name || f.name }}</a>
            <span v-else style="word-break:break-all">{{ f.stored_name || f.name }}</span>
            <span class="small muted">
              <template v-if="f.name && f.stored_name && f.name !== f.stored_name"> · 原 {{ f.name }}</template>
              · {{ fmtBytes(f.size_bytes) }}
            </span>
            <span v-if="f.cached" class="tag orange" style="margin-left:4px">未保存</span>
          </div>
          <select :value="f.role" style="flex:none;width:104px"
                  @change="setRole(f, $event.target.value)">
            <option v-for="o in ROLE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <button class="btn ghost sm" style="flex:none;color:var(--danger)"
                  @click="removeRow(f)">删除</button>
        </div>
        <div class="small muted" style="margin-top:8px">归档位置：{{ archiveHint }}</div>
      </div>

      <!-- ③ 考情分析：选模板 → 分段写。已写的字换模板不会丢 -->
      <div class="field" style="margin-top:14px">
        <label>考情分析 <span class="muted small">（换模板时已经写好的文字都会保留）</span></label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select :value="templateId || 0" style="max-width:280px"
                  @change="onTemplateChange($event.target.value)">
            <option :value="0">（不用模板，自己加段）</option>
            <option v-for="t in templates" :key="t.id" :value="t.id">
              {{ t.name }}<template v-if="t.is_builtin"> · 内置</template>
            </option>
          </select>
          <button class="btn sm ghost" @click="addSection">＋ 加一段</button>
          <button class="btn sm ghost" :disabled="!sections.length" @click="saveAsTemplate">存成模板</button>
          <span class="small muted">已写 {{ filledCount }} / {{ sections.length }} 段</span>
        </div>

        <div v-for="(s, i) in sections" :key="s.key || i" class="exam-sec">
          <div style="display:flex;align-items:center;gap:6px">
            <input v-if="s.extra" v-model="s.label" style="max-width:190px" placeholder="段落标题">
            <b v-else class="lb">{{ s.label }}</b>
            <span v-if="s.extra" class="tag" style="font-size:11px">自定义</span>
            <div class="spacer"></div>
            <button class="btn ghost sm" style="color:var(--danger)" @click="removeSection(i)">删这段</button>
          </div>
          <div v-if="s.hint" class="small muted">{{ s.hint }}</div>
          <textarea rows="3" v-model="s.text" :placeholder="s.hint || ''"></textarea>
        </div>
        <div v-if="!sections.length" class="small muted">
          还没分段 —— 上面挑一套模板，或者点「＋ 加一段」自己写。
        </div>
      </div>
    </template>

    <template #foot>
      <button v-if="exam" class="btn ghost" style="color:var(--danger)"
              :disabled="saving" @click="removeExam">删除这次记录</button>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="saving || loading" @click="save">
        {{ saving ? '保存中…' : '保存' }}
      </button>
    </template>
  </Modal>

  <!-- 图片预览：在弹窗里看大图，不用先下载 -->
  <Modal v-if="preview" :title="preview.name" @close="preview = null">
    <div class="hw-preview"><img :src="preview.url" :alt="preview.name"></div>
    <template #foot>
      <a v-if="preview.download" class="btn ghost" :href="preview.download" download>下载原件</a>
      <button class="btn" @click="preview = null">关闭</button>
    </template>
  </Modal>`,
};
