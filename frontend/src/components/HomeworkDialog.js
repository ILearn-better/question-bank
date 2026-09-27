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
import { onBeforeUnmount, onMounted, ref } from 'vue';
import { homeworkApi, lessonFilesApi } from '../api.js';
import { fail, fmtBytes, hhmm, ok, shortDate, warn } from '../store.js';
import { needLocalCopyMessage, pickPastedAsset } from '../pasteAsset.js';
import Modal from './Modal.js';

// 浏览器能直接当缩略图渲染的图片格式。
// HEIC/HEIF（iPhone 默认）存得下，但 Chrome 不认，硬渲染只会得到一张碎图 →
// 那就退化成「图片」标签 + 点开看原件。
const PREVIEWABLE = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'];
const extOf = (name) => String(name || '').split('.').pop().toLowerCase();

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
    const fileEl = ref(null);       // 选文件（文档为主，也接受图片）
    const imgEl = ref(null);        // 选图片（accept="image/*"）
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

    onMounted(() => {
      load();
      // 贴图挂在 window 上：焦点在哪个输入框都能贴。卸载时必须摘掉。
      window.addEventListener('paste', onPaste);
    });
    onBeforeUnmount(() => window.removeEventListener('paste', onPaste));

    /** 再点一次同一个分数 = 取消这一项（允许只打几项，和课堂评分一致）。 */
    function setScore(dimId, v) {
      scores.value[dimId] = scores.value[dimId] === v ? undefined : v;
    }

    const dimPrev = (id) => prev.value[id] ?? null;
    /** 未交就是没作业可评 —— 打分整块收起来，别让人对着空白处打分。 */
    const scoring = () => status.value !== 'missing';

    function pickFile() { fileEl.value && fileEl.value.click(); }
    function pickImage() { imgEl.value && imgEl.value.click(); }

    /** 收下一份作业原件。**选文件 / 选图片 / 粘截图走的是同一条路**，
     *  所以三者的归档、抽文字、失败提示、删除行为天然一致。 */
    async function uploadFile(f) {
      if (!f) return;
      uploadingFile.value = true;
      try {
        const fd = new FormData();
        fd.append('file', f, f.name || 'paste.png');
        const r = await lessonFilesApi.upload(props.lesson.id, fd, 'homework');
        files.value = [...files.value, r];
        // 图片没有文字可抽**是正常的**，不要报成「读不出」吓人
        if (r.kind === 'image') ok(`已收下图片「${r.name}」`);
        else if (r.status === 'ok') ok(`已收下「${r.name}」，读出 ${r.chars} 字`);
        else warn(`「${r.name}」收下了，但没读出文字：${r.reason}`);
      } catch (err) {
        fail(err.message);
      } finally {
        uploadingFile.value = false;
      }
    }

    function onFilePicked(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';        // 清掉才能反复选同一个文件
      uploadFile(f);
    }

    /** 把 data: URL（网页内嵌图）变回文件再上传 —— 内容已经在本地了，不联网。 */
    async function uploadDataUrl(dataUrl) {
      try {
        const blob = await (await fetch(dataUrl)).blob();
        await uploadFile(new File([blob], 'paste.png', { type: blob.type || 'image/png' }));
      } catch (err) {
        fail('这张图读不出来：' + err.message);
      }
    }

    /** Ctrl+V 直接贴。截图、从微信/文件夹复制的图、网页内嵌图、
     *  甚至从文件夹复制的 PDF 都能直接收下 —— 判断用与反馈「附图」同一份
     * （pasteAsset.js），粘进什么形态太杂，不能两处各写一套。 */
    function onPaste(e) {
      const found = pickPastedAsset(e);
      if (!found) return;         // 普通文字：不插手
      e.preventDefault();
      if (found.kind === 'file') return uploadFile(found.file);
      if (found.kind === 'data') return uploadDataUrl(found.dataUrl);
      if (found.kind === 'url') return warn(needLocalCopyMessage(found.url));
      warn('剪贴板里没有可以直接用的图片。试试截图后再贴，或点上面的按钮选文件。');
    }

    /** 能不能当缩略图渲染（HEIC 等由浏览器不认的退化成标签）。 */
    const canPreview = (f) => f.kind === 'image' && PREVIEWABLE.includes(extOf(f.name));

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
      files, fileEl, imgEl, fileAccept, uploadingFile, relDir,
      setScore, dimPrev, scoring, pickFile, pickImage, onFilePicked, removeFile, save, clearRecord,
      canPreview, fmtBytes, lessonFilesApi, hhmm, shortDate,
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

      <!-- 作业原件：可选。有就传，没有照样能打分（用户明确要求）。
           **图片与文件都能选，也能直接粘截图** —— 三样东西走同一条上传路，
           所以归档、抽文字、失败提示、删除行为天然一致。 -->
      <div class="field">
        <label>
          作业原件
          <span class="muted small">（照片 / 截图 / PDF / Word 都行，可以不传；直接 Ctrl+V 也能贴）</span>
        </label>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn sm" :disabled="uploadingFile" @click="pickImage">
            {{ uploadingFile ? '处理中…' : '＋ 选图片' }}
          </button>
          <button class="btn sm" :disabled="uploadingFile" @click="pickFile">＋ 选文件</button>
          <input ref="imgEl" type="file" accept="image/*" style="display:none" @change="onFilePicked">
          <input ref="fileEl" type="file" style="display:none" :accept="fileAccept" @change="onFilePicked">
          <span v-if="files.length" class="small muted">共 {{ files.length }} 份</span>
          <span v-else class="small muted">贴截图可以直接 Ctrl+V</span>
        </div>

        <div v-for="f in files" :key="f.id" class="hw-file">
          <!-- 图片给缩略图：作业照片得看得见才敢确认收对了 -->
          <a v-if="canPreview(f)" :href="lessonFilesApi.rawUrl(f.id)" target="_blank"
             rel="noopener" style="flex:none">
            <img :src="lessonFilesApi.rawUrl(f.id)" alt="" class="hw-thumb">
          </a>
          <span v-else class="tag" :class="f.status === 'ok' ? 'green' : 'red'" style="flex:none">
            {{ f.status === 'ok' ? f.kind_cn : '读不出' }}
          </span>
          <div style="flex:1;min-width:0">
            <a v-if="f.status === 'ok'" :href="lessonFilesApi.rawUrl(f.id)" target="_blank" rel="noopener">{{ f.name }}</a>
            <span v-else style="word-break:break-all">{{ f.name }}</span>
            <span class="small muted">
              · {{ fmtBytes(f.size_bytes) }}
              <template v-if="f.kind === 'image'"> · 图片只存档，不抽文字</template>
              <template v-else-if="f.status === 'ok'"> · 抽出 {{ f.chars }} 字</template>
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
