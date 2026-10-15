// 整体规划 —— 一条规划的「看」与「改」，**同一个弹窗的两种模式**。
//
// 需求原话（用户 2026-10-16）：
// 「学生信息界面添加一下"整体规划",放在"考试情况"上方,要能够新建规划,删除规划,
//   查看规划,修改规划。我会将文本或者文本文件放到规划,我点开某个规划要能够查看渲染好的
//   文件,常见的 markdown、word 这些。(每个规划点开才能详细查看)」
//
// 几条刻意的设计（改动前请先读）：
//
// ① **默认是「看」，不是「改」**。需求写着「每个规划点开才能详细查看」——
//    点开一条规划，他要看的就是**排版好的正文**（真公式、真标题、真表格），
//    而不是一屏 Markdown 源码或者一个等着填的表单。要改再点「改」。
//    新建时没有正文可看，所以新建直接进编辑态。
//
// ② **正文是 Markdown（打字的 / 由文件转来的都是它）**。传一个 Word 或 Markdown
//    文件时，后端在上传那一刻就把它转成 Markdown 返回（见 routers/plans.py 的设计①），
//    这里只决定「灌进哪儿」：正文空着就直接填，已经有字就**问一句**是追加还是只存原件。
//    不猜——猜错了要么丢掉刚转出来的内容、要么把老师写好的规划冲掉。
//
// ③ **渲染容器是空 div + 函数 ref，内容由 JS 写 innerHTML**。
//    KaTeX 的 auto-render 会把容器里的文本节点整个换掉，所以容器里**不能**有 Vue 插值
//    （这是本项目踩过多次的坑，出卷页 / 备课弹窗都有同名注释）。
//    渲染管线仍是 src/mathRender.js 那一套唯一出处。
//
// ④ **原件与正文是两样东西**：正文是「能读的那部分」，原件是「一定没丢的那部分」
//    （转换一定失真：Word 里的公式只留文字、版面还原不了）。所以文件既进正文、
//    也留在附件里可下载；转不出正文的（图片、压缩包、扫描件）**照常归档**，
//    并把原因写在行上，绝不悄悄丢。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { plansApi } from '../api.js';
import { ensureLibs, renderRichEl } from '../mathRender.js';
import { fail, fmtBytes, ok, warn } from '../store.js';
import Modal from './Modal.js';

/** 追加时用的分界线。写成引用块是为了**渲染出来看得见** ——
 *  过两天再打开，得能一眼看出「这一段是从哪个文件导进来的」，也能直接删掉它。 */
const sepFor = (name) => `\n\n> —— 以下内容来自「${name}」 ——\n\n`;

export default {
  name: 'PlanDialog',
  components: { Modal },
  props: {
    /** 要打开的那条规划（列表里的简要对象）；新建时传 null。 */
    plan: { type: Object, default: null },
    student: { type: Object, required: true },
    /** 学生归档文件夹名（u1_李芹旭），只用于把「原件会归到哪」显示清楚。 */
    folder: { type: String, default: '' },
    /** 传 true 直接进编辑态（列表上那个「改」按钮用）；
     *  不传就默认「看」—— 需求写着「每个规划点开才能详细查看」。 */
    startEdit: { type: Boolean, default: false },
  },
  emits: ['close', 'saved'],
  setup(props, { emit }) {
    const loading = ref(true);
    const saving = ref(false);
    const uploading = ref(false);
    const deleting = ref(false);
    const fileEl = ref(null);

    // 'view' 看排版好的正文 / 'edit' 改标题与正文
    // 新建、或点的是列表上的「改」→ 直接进编辑态；否则点开先给人看排版好的那份
    const mode = ref(props.plan && !props.startEdit ? 'view' : 'edit');
    const detail = ref(null);                 // 完整详情（含正文、原件清单）
    const title = ref(props.plan ? props.plan.title || '' : '');
    const content = ref('');
    const files = ref([]);                    // 已归档的 + 本次刚传的（cached: true）

    const toastNote = ref('');                // 上次上传的转换说明，贴在按钮下面

    const archiveHint = computed(() => {
      if (detail.value && detail.value.dir_rel) return detail.value.dir_rel;
      const t = (title.value || props.plan?.title || '').trim() || '未命名规划';
      return `uploads/students/${props.folder || '学生目录'}/plans/${t}/（保存后建立）`;
    });

    const isEdit = computed(() => mode.value === 'edit');
    const hasBody = computed(() => !!String(content.value || '').trim());
    const charCount = computed(() => String(content.value || '').length);

    /* ---------- 渲染正文（唯一出处 src/mathRender.js） ---------- */
    const richEl = ref(null);
    let libsReady = false;
    function paint() {
      if (richEl.value) renderRichEl(richEl.value, content.value || '');
    }
    async function paintRich() {
      if (!libsReady) {
        paint();                            // 库还没到：先把原文铺出来，别留一屏空白
        try { await ensureLibs(); libsReady = true; }
        catch (e) { return; }                // 加载不上就一直显示原文（Markdown 源码也读得懂）
      }
      await nextTick();                      // 等容器真的挂上去
      paint();
    }
    // 看模式、正文变化后都要重绘：切模式时容器是**新挂的**，旧内容不会自己跟过来
    watch([mode, content, detail], () => { if (mode.value === 'view') paintRich(); },
      { flush: 'post' });

    function rowFromArchived(f) {
      return { ...f, cached: false, link: plansApi.rawUrl(f.id) };
    }

    async function load() {
      loading.value = true;
      try {
        if (props.plan) {
          const d = await plansApi.get(props.plan.id);
          detail.value = d;
          title.value = d.title || '';
          content.value = d.content || '';
          files.value = (d.files || []).map(rowFromArchived);
        }
      } catch (e) {
        fail(e.message);
      } finally {
        loading.value = false;
      }
    }

    function toView() {
      mode.value = 'view';
      toastNote.value = '';
    }

    function toEdit() {
      mode.value = 'edit';
      toastNote.value = '';
    }

    // ---------------- 原件 ----------------
    function pickFile() {
      fileEl.value && fileEl.value.click();
    }

    function onFilePicked(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';                   // 清掉才能反复选同一个文件
      if (f) uploadFile(f);
    }

    /** 传一份文件：原件进暂存区，正文**由后端转好的 Markdown 决定**。
     *
     *  ⚠️ 转换失败（或本来就没有正文的格式）**不是错误** —— 原件照常收下，
     *  行上写清为什么没转出正文。把它报成失败会把「传了张图」变成一件吓人的事。
     */
    async function uploadFile(f) {
      uploading.value = true;
      toastNote.value = '';
      try {
        const fd = new FormData();
        fd.append('file', f, f.name || 'file');
        const r = await plansApi.upload(fd);
        const md = String(r.markdown || '');
        files.value = [...files.value, {
          ...r, cached: true, link: '', id: 0,
        }];

        if (md.trim()) {
          // 已经有字 → 问一句怎么放。**不猜**：猜错了要么丢掉刚转出来的内容，
          // 要么把老师写好的规划冲掉。（确定 = 追加到末尾，取消 = 只要原件）
          if (hasBody.value) {
            const append = window.confirm(
              `「${r.name}」已转成正文（${r.chars} 字）。\n\n`
              + '确定 = 追加到正文末尾\n取消 = 只把原件存进这条规划');
            if (append) content.value = content.value.replace(/\s+$/, '') + sepFor(r.name) + md;
            toastNote.value = append ? `${r.note}；已追加到正文末尾`
              : `${r.note}；没有写进正文（原件已收下，随时可以再插进来）`;
            if (append && mode.value !== 'edit') mode.value = 'edit';
            if (append) ok('已追加到正文末尾，可以自己改');
          } else {
            content.value = md;
            toastNote.value = `${r.note}；正文是空的，已直接填进去`;
            if (mode.value !== 'edit') mode.value = 'edit';
            ok(`已把「${r.name}」转成正文（${r.chars} 字），可以直接改`);
          }
        } else {
          warn(r.note || `「${r.name}」没有转出正文，已作为原件存好`);
          toastNote.value = r.note || '';
        }
      } catch (e) {
        fail(e.message);
      } finally {
        uploading.value = false;
      }
    }

    async function removeRow(row) {
      const label = row.stored_name || row.name;
      const tail = row.cached ? '（这份还没保存，会从暂存区删掉）' : '（磁盘上的原件也一起删）';
      if (!window.confirm(`删掉「${label}」？${tail}\n\n⚠️ 正文里由它转出来的那部分**不会**跟着删。`)) return;
      try {
        if (row.cached) await plansApi.removeCache(row.cache_key);
        else await plansApi.removeFile(row.id);
        files.value = files.value.filter((x) => x !== row);
      } catch (e) {
        fail(e.message);
      }
    }

    // ---------------- 保存 / 删除 ----------------
    async function save() {
      const t = (title.value || '').trim();
      if (!t && !hasBody.value) {
        fail('规划总得有点内容：写个标题，或者写点正文');
        return;
      }
      saving.value = true;
      try {
        const payload = {
          title: t,
          content: content.value || '',
          // 只提交**还在暂存区**的那些；已归档的不用重提（files 是增量）
          files: files.value.filter((f) => f.cached).map((f) => ({
            cache_key: f.cache_key, name: f.name,
            converted: !!f.converted, note: f.note || '',
          })),
        };
        if (props.plan) {
          await plansApi.update(props.plan.id, payload);
          ok('规划已保存');
        } else {
          await plansApi.create({ student_id: props.student.id, ...payload });
          ok('规划已新建');
        }
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        saving.value = false;
      }
    }

    async function removePlan() {
      const what = (title.value || props.plan?.title || '这条规划').trim();
      if (!window.confirm(`删掉「${what}」？（原件也会一起删）\n\n⚠️ 删掉就没了，确认一下？`)) return;
      deleting.value = true;
      try {
        await plansApi.remove(props.plan.id);
        ok('规划已删掉');
        emit('saved');
      } catch (e) {
        fail(e.message);
      } finally {
        deleting.value = false;
      }
    }

    onMounted(() => {
      load().then(() => { if (mode.value === 'view') paintRich(); });
    });

    // 关窗时**不删暂存文件**：关窗（尤其误触）就把老师刚传的文件删掉，
    // 比留两天垃圾糟得多；过期的暂存由后端 48 小时后清。
    onBeforeUnmount(() => { richEl.value = null; });

    return {
      loading, saving, uploading, deleting, fileEl, mode, detail, title, content, files,
      isEdit, hasBody, charCount, toastNote, archiveHint, richEl,
      toView, toEdit, pickFile, onFilePicked, removeRow, save, removePlan, fmtBytes,
    };
  },
  template: `
  <Modal wide :title="(plan ? '整体规划' : '新建规划') + ' · ' + student.name" @close="$emit('close')">
    <div v-if="loading" class="empty">加载中…</div>
    <template v-else>

      <!-- ==================== 看：排版好的正文 ==================== -->
      <template v-if="mode === 'view'">
        <div class="plan-title">{{ title || '（未命名规划）' }}</div>
        <div class="small muted" style="margin-bottom:10px">
          共 {{ charCount }} 字
          <template v-if="files.length"> · 原件 {{ files.length }} 份</template>
          <span v-if="detail && detail.updated_at"> · 最后修改 {{ (detail.updated_at || '').slice(0, 16).replace('T', ' ') }}</span>
        </div>

        <!-- ⚠️ 空容器 + 函数 ref：正文由 JS 写 innerHTML（KaTeX 会换掉容器里的文本节点，
             所以这里**不能**有 Vue 插值）。渲染管线见 src/mathRender.js。 -->
        <div v-if="hasBody" class="plan-rich" ref="richEl"></div>
        <div v-else class="empty">
          这条规划还没有正文
          <div class="small" style="margin-top:6px">点下面的「改」，直接打字，或者传一份 Word / Markdown 让它自己转</div>
        </div>

        <div v-if="files.length" style="margin-top:14px">
          <div class="small muted" style="margin-bottom:4px">原件（转换会失真，这里是可以对照的那一份）</div>
          <div v-for="f in files" :key="f.id || f.cache_key" class="exam-file">
            <span class="tag" style="flex:none">{{ f.kind_cn }}</span>
            <div style="flex:1;min-width:0">
              <a :href="f.link" target="_blank" rel="noopener">{{ f.stored_name || f.name }}</a>
              <span class="small muted"> · {{ fmtBytes(f.size_bytes) }}</span>
              <div v-if="f.note" class="small muted">{{ f.note }}</div>
            </div>
          </div>
        </div>
      </template>

      <!-- ==================== 改：标题 + 正文 + 原件 ==================== -->
      <template v-else>
        <div class="field">
          <label>规划名称</label>
          <input v-model="title" placeholder="例如 初三全年规划 / 暑期冲刺安排">
        </div>

        <div class="field">
          <label>
            正文
            <span class="muted small">（支持 Markdown：用 # 写标题、** 加粗、$公式$、表格、列表）</span>
          </label>
          <textarea v-model="content" rows="12" class="plan-editor"
                    placeholder="在这里直接写规划。也可以传一份 Word / Markdown / 文本文件，让它自动转成正文。"></textarea>
          <div class="small muted" style="margin-top:4px">
            {{ charCount }} 字 · 保存后「看」的那一面会把它排版好（公式、标题、表格都渲染）
          </div>
        </div>

        <div class="field">
          <label>文本文件 / 原件 <span class="muted small">（Word、Markdown、PDF、PPT、文本都行；其它格式也能传，只归档不转正文）</span></label>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <button class="btn sm" :disabled="uploading" @click="pickFile">
              {{ uploading ? '正在解析…' : '选择文件' }}
            </button>
            <span class="small muted">会转成正文（正文空着直接填，已有内容会问你追加还是只存原件）</span>
          </div>
          <!-- ⚠️ **不设 accept**：设了文件对话框会把别的类型藏起来，
               老师想传个 .lnk 或者截图会发现「选不了」。格式本来就都能传。 -->
          <input ref="fileEl" type="file" style="display:none" @change="onFilePicked">
          <div v-if="toastNote" class="small" style="margin-top:6px;color:var(--success)">{{ toastNote }}</div>

          <div v-for="f in files" :key="f.id || f.cache_key" class="exam-file" style="margin-top:8px">
            <span class="tag" style="flex:none">{{ f.kind_cn }}</span>
            <span class="tag" :class="f.converted ? 'green' : 'orange'" style="flex:none">
              {{ f.converted ? '已转正文' : '只存原件' }}
            </span>
            <div style="flex:1;min-width:0">
              <a v-if="f.link" :href="f.link" target="_blank" rel="noopener">{{ f.stored_name || f.name }}</a>
              <span v-else>{{ f.name }}</span>
              <span class="small muted"> · {{ fmtBytes(f.size_bytes) }}</span>
              <span v-if="f.cached" class="small muted"> · 还没保存</span>
              <div v-if="f.note" class="small muted">{{ f.note }}</div>
            </div>
            <button class="btn ghost sm" style="color:var(--danger)" @click="removeRow(f)">删</button>
          </div>
        </div>

        <div class="small muted">原件会归到：{{ archiveHint }}</div>
        <div class="small muted">
          正文也会在这里存一份 <code>规划.md</code> —— 在文件里改了<strong>不会</strong>同步回这里
        </div>
      </template>
    </template>

    <template #foot>
      <template v-if="mode === 'view'">
        <button class="btn ghost" style="color:var(--danger)" :disabled="deleting" @click="removePlan">
          {{ deleting ? '删除中…' : '删除规划' }}
        </button>
        <div class="spacer"></div>
        <button class="btn" @click="$emit('close')">关闭</button>
        <button class="btn primary" @click="toEdit">改</button>
      </template>
      <template v-else>
        <button v-if="plan" class="btn ghost" @click="toView">返回查看</button>
        <div class="spacer"></div>
        <button class="btn" @click="$emit('close')">取消</button>
        <button class="btn primary" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </template>
  </Modal>`,
};
