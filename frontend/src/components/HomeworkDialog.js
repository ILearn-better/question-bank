// 交作业 —— 记这一次课收上来的作业。
//
// 为什么单独一个弹窗、而不是塞进课后反馈里（用户定过）：
//   · 作业能**独立于反馈存在**：老师可能只记「没交」，也可能先收了作业照片、
//     隔天再写反馈、再补打分。两件事绑在一个弹窗里就互相等来等去。
//   · 入口就在「写反馈 / 改反馈」旁边，同一个位置、同一套交互，认知成本为零。
//
// 几条刻意的设计（细节见 docs/学生作业上传与评分方案.md）：
//   · **打分不以文件为前提**：没上传作业照样能打分。老师常常是当面看过纸质作业的，
//     所以上传是可选的，这里也不会做任何「还没传文件」的拦阻提示。
//   · **未交不打分**：选「未交」时打分整块收起来（后端也会清掉分数）——
//     用 0 分记会把平均值和雷达图一起带偏，看起来像退步。
//   · **不用记 = 不存**：什么都没填直接关掉就什么都不会留下；已有记录想撤掉有「撤销记录」。
//   · 作业维度与课堂维度**是两套**（用户明确要求分开），互不影响。
import { onMounted, ref } from 'vue';
import { homeworkApi, lessonFilesApi } from '../api.js';
import { fail, fmtBytes, hhmm, ok, shortDate, warn } from '../store.js';
import Modal from './Modal.js';

export default {
  name: 'HomeworkDialog',
  components: { Modal },
  props: { lesson: { type: Object, required: true } },
  emits: ['close', 'saved'],
  setup(props, { emit }) {
    const dims = ref([]);
    const loading = ref(true);
    const saving = ref(false);
    const busy = ref(false);
    const status = ref('submitted');
    const note = ref('');
    const scores = ref({});          // dim_id -> 1..5（只放打过的）
    const prev = ref({});            // dim_id -> 上次的分数，打分的参照锚点
    const existing = ref(null);      // 已有记录时才有值（决定要不要显示「撤销记录」）
    const statusOptions = ref([]);
    const files = ref([]);
    const fileEl = ref(null);
    const fileAccept = ref('');
    const uploadingFile = ref(false);
    const relDir = ref('');

    async function load() {
      loading.value = true;
      try {
        const [r, d] = await Promise.all([
          homeworkApi.get(props.lesson.id),
          homeworkApi.dims(),
        ]);
        dims.value = d;
        statusOptions.value = r.status_options || [];
        fileAccept.value = r.accept || '';
        relDir.value = r.rel_dir || '';
        files.value = r.files || [];
        prev.value = r.previous || {};
        const hw = r.homework;
        existing.value = hw;
        status.value = hw ? hw.status : 'submitted';
        note.value = hw ? hw.note : '';
        const m = {};
        for (const s of (hw && hw.scores) || []) m[s.dim_id] = s.score;
        scores.value = m;
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    onMounted(load);

    /** 再点一次同一个分数 = 取消这一项（允许只打几项，和课堂评分一致）。 */
    function setScore(dimId, v) {
      scores.value[dimId] = scores.value[dimId] === v ? undefined : v;
    }

    const dimPrev = (id) => prev.value[id] ?? null;
    /** 未交就是没作业可评 —— 打分整块收起来，别让人对着空白处打分。 */
    const scoring = () => status.value !== 'missing';

    function pickFile() { fileEl.value && fileEl.value.click(); }

    async function onFilePicked(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';
      if (!f) return;
      uploadingFile.value = true;
      try {
        const fd = new FormData();
        fd.append('file', f, f.name);
        const r = await lessonFilesApi.upload(props.lesson.id, fd, 'homework');
        files.value = [...files.value, r];
        if (r.status === 'ok') ok(`已收下「${r.name}」，读出 ${r.chars} 字`);
        else warn(`「${r.name}」收下了，但没读出文字：${r.reason}`);
      } catch (err) {
        fail(err.message);
      } finally {
        uploadingFile.value = false;
      }
    }

    async function removeFile(f) {
      if (!window.confirm(`删掉「${f.name}」？（磁盘上的原件也一起删）`)) return;
      try {
        await lessonFilesApi.remove(f.id);
        files.value = files.value.filter((x) => x.id !== f.id);
      } catch (e) {
        fail(e.message);
      }
    }

    async function save() {
      saving.value = true;
      try {
        const payload = {
          status: status.value,
          note: note.value || '',
          scores: Object.entries(scores.value)
            .filter(([, v]) => v)
            .map(([dimId, score]) => ({ dim_id: Number(dimId), score })),
        };
        const r = await homeworkApi.save(props.lesson.id, payload);
        const snap = r && r.archive_txt;
        if (snap && snap.ok === false) {
          warn(`作业记下了，但归档文件夹里的 txt 副本没写成：${snap.reason || '原因不明'}`);
        }
        ok('作业已记录');
        existing.value = r;
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
    }

    /** 「这次没布置作业 / 不用记」—— 整条撤掉。作业原件留着：撤一条评价不该连坐删学生的作业。 */
    async function clearRecord() {
      if (!window.confirm('把这次的作业记录整个撤掉？（上传的作业原件会留着）')) return;
      busy.value = true;
      try {
        await homeworkApi.remove(props.lesson.id);
        ok('这条作业记录已撤掉');
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = false;
      }
    }

    return {
      dims, loading, saving, busy, status, note, scores, existing, statusOptions,
      files, fileEl, fileAccept, uploadingFile, relDir,
      setScore, dimPrev, scoring, pickFile, onFilePicked, removeFile, save, clearRecord,
      fmtBytes, lessonFilesApi, hhmm, shortDate,
    };
  },
  template: `
  <Modal :title="'交作业 · ' + lesson.student_name" @close="$emit('close')">
    <div class="small muted" style="margin:-6px 0 14px">
      {{ shortDate(lesson.start_at) }} {{ hhmm(lesson.start_at) }}
      <span v-if="lesson.topic"> · {{ lesson.topic }}</span>
      <span v-if="existing" class="tag green" style="margin-left:6px">已有记录，正在修改</span>
      <span v-else class="tag" style="margin-left:6px">还没记过</span>
    </div>

    <div v-if="loading" class="empty">加载中…</div>
    <template v-else>
      <!-- 状态：先问「交没交」，再谈别的。未交就是没作业可评。 -->
      <div class="field">
        <label>这次作业 <span class="muted small">（没布置就直接关掉，什么都不会留下）</span></label>
        <div class="chips">
          <span v-for="o in statusOptions" :key="o.value" class="chip"
                :class="{ on: status === o.value }" @click="status = o.value">{{ o.label }}</span>
        </div>
      </div>

      <!-- 作业原件：可选。有就传，没有照样能打分（用户明确要求）。 -->
      <div class="field">
        <label>
          作业原件
          <span class="muted small">（拍照 / PDF / Word 都行，可以不传；传了会在本机转成文字）</span>
        </label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn sm" :disabled="uploadingFile" @click="pickFile">
            {{ uploadingFile ? '解析中…' : '＋ 上传作业' }}
          </button>
          <input ref="fileEl" type="file" style="display:none" :accept="fileAccept" @change="onFilePicked">
          <span v-if="files.length" class="small muted">共 {{ files.length }} 份</span>
        </div>
        <div v-for="f in files" :key="f.id" style="display:flex;gap:8px;align-items:flex-start;margin-top:8px">
          <span class="tag" :class="f.status === 'ok' ? 'green' : 'red'" style="flex:none">
            {{ f.status === 'ok' ? f.kind_cn : '读不出' }}
          </span>
          <div style="flex:1;min-width:0">
            <a v-if="f.status === 'ok'" :href="lessonFilesApi.rawUrl(f.id)" target="_blank" rel="noopener">{{ f.name }}</a>
            <span v-else style="word-break:break-all">{{ f.name }}</span>
            <span class="small muted">
              · {{ fmtBytes(f.size_bytes) }}
              <template v-if="f.status === 'ok'"> · 抽出 {{ f.chars }} 字</template>
            </span>
            <div v-if="f.status !== 'ok'" class="small" style="color:var(--danger);margin-top:2px">{{ f.reason }}</div>
          </div>
          <button class="btn ghost sm" style="flex:none;color:var(--danger)" @click="removeFile(f)">删除</button>
        </div>
        <div v-if="relDir" class="small muted" style="margin-top:8px">归档位置：{{ relDir }}（按「学生 / 上课日期」存放）</div>
      </div>

      <!-- 打分：只在交了的时候出现。维度是作业自己那套，和课堂评分互不影响。 -->
      <div v-if="scoring()" class="field">
        <label>作业评分 <span class="muted small">（可只打几项；再点一次可取消。没交作业就不用打分）</span></label>
        <div v-for="d in dims" :key="d.id" class="rate-row">
          <span class="dim">{{ d.name }}</span>
          <div class="dots">
            <button v-for="v in 5" :key="v" type="button"
                    class="dot" :class="{ on: scores[d.id] === v }"
                    @click="setScore(d.id, v)">{{ v }}</button>
          </div>
          <span class="small muted" v-if="dimPrev(d.id)">上次 {{ dimPrev(d.id) }}</span>
        </div>
        <div v-if="!dims.length" class="small muted">还没有作业维度，去「设置」里加几条。</div>
      </div>

      <div class="field">
        <label>批改备注 <span class="muted small">（可留空）</span></label>
        <textarea rows="3" v-model="note" placeholder="例如：第 3 题思路对但算错了；错题订正只改了答案没写过程"></textarea>
      </div>
    </template>

    <template #foot>
      <button v-if="existing" class="btn ghost" style="color:var(--danger)"
              :disabled="busy || saving" @click="clearRecord">撤销记录</button>
      <button class="btn" @click="$emit('close')">取消</button>
      <button class="btn primary" :disabled="saving || loading" @click="save">
        {{ saving ? '保存中…' : '保存' }}
      </button>
    </template>
  </Modal>`,
};
