// 题库文件（vault）：用 Obsidian 管题，拾课负责把文件同步进库。
//
// 这一页解决的是「正文放在哪」的问题：以前题目只在 SQLite 里，老师想用
// Obsidian 的编辑体验（双链、大纲、全文搜索、随手贴图）没有任何入口。
// 现在一道题 = 磁盘上一个 .md，这边只做两件事：**导出**与**同步**。
//
// 页面上唯一不能省的是「报告」：同步是**会改数据**的动作，老师必须能看清
// 「哪几篇进来了、哪几篇没进来、为什么」。只回一句「同步完成」等于什么都没说。
//
// ⚠️ 这个项目**没有引入 Element Plus**（index.html 的 importmap 只有 vue / vue-router），
//    所以这一页和别的页一样用原生 HTML + app.css 里的 `.card` / `.btn` / `.muted`。
//    写成 `<el-card>` 不会报错，只会渲染成一堆没样式的未知标签。
import { computed, onMounted, ref } from 'vue';
import { vaultApi } from '../api.js';
import { fail, ok } from '../store.js';

export default {
  name: 'Vault',
  setup() {
    const status = ref(null);
    const inbox = ref(null);
    const busy = ref('');
    const report = ref(null);
    const exportResult = ref(null);
    const exportLimit = ref(200);

    const nothingHappened = computed(() => {
      const r = report.value;
      return r && !r.created.length && !r.updated.length && !r.errors.length;
    });

    async function load() {
      try {
        status.value = await vaultApi.status();
        inbox.value = await vaultApi.inbox();
      } catch (e) {
        fail(e.message);
      }
    }

    /** 同步。dry=true 时只出报告，不动库也不动文件。 */
    async function scan(dry) {
      busy.value = dry ? 'preview' : 'sync';
      report.value = null;
      try {
        report.value = await vaultApi.scan(dry);
        const r = report.value;
        if (dry) {
          ok(`预览完成：会新增 ${r.created.length} 道、更新 ${r.updated.length} 道`);
        } else {
          ok(`同步完成：新增 ${r.created.length}、更新 ${r.updated.length}、未变 ${r.unchanged}`);
          await load();
        }
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function doExport() {
      busy.value = 'export';
      exportResult.value = null;
      try {
        const r = await vaultApi.export([], Number(exportLimit.value) || 200);
        exportResult.value = r;
        if (r.exported.length) ok(`导出了 ${r.exported.length} 道题到文件夹`);
        else if (r.note) ok(r.note);
        else ok('没有需要导出的题（都已经有 md 文件了）');
        await load();
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function seedTemplate() {
      busy.value = 'seed';
      try {
        const r = await vaultApi.seedTemplate();
        ok(r.existed ? '已经有模板了，没有覆盖：' + r.path : '模板已生成：' + r.path);
        await load();
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function openFolder() {
      try {
        await vaultApi.openFolder();
      } catch (e) {
        fail(e.message);
      }
    }

    /** 收件箱：正常情况下后台每 20 秒自己跑，这个按钮是「别等了，现在跑」。 */
    async function runInbox() {
      busy.value = 'inbox';
      try {
        const r = await vaultApi.inboxRun();
        const n = r.started.length;
        if (n) ok(`已开始识别 ${n} 份文件，跑完会自动写成题目 md`);
        else if (r.ignored.length) fail(r.ignored[0].reason);
        else if (r.waiting.length) ok('已经有任务在跑，等它跑完再处理下一份');
        else ok('收件箱里没有可处理的新文件');
        await load();
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function copyPath() {
      const p = status.value && status.value.root;
      if (!p) return;
      try {
        await navigator.clipboard.writeText(p);
        ok('路径已复制，粘到 Obsidian「打开文件夹作为库」里');
      } catch (e) {
        // 剪贴板在非 https / 非 localhost 下会被浏览器拦住，这时把路径显示出来让人手抄
        fail('复制不了，手动选一下上面的路径：' + p);
      }
    }

    onMounted(load);

    return {
      status, inbox, busy, report, nothingHappened, exportResult, exportLimit,
      load, scan, doExport, seedTemplate, openFolder, copyPath, runInbox,
    };
  },
  template: `
  <div style="max-width:1000px">
    <div class="card">
      <h2>题库文件</h2>
      <div class="muted small" style="line-height:1.8;margin-bottom:12px">
        一道题一个 <code>.md</code> 文件，放在下面这个文件夹里。用 Obsidian「打开文件夹作为库」就能改题、
        补答案、贴图；改完点「同步」把改动收进题库。<b>不在</b>这个文件夹里的题（网页上录的）不受影响。
      </div>

      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span class="muted small" style="flex:none">文件夹</span>
        <code style="background:var(--panel-2);padding:4px 8px;border-radius:4px;font-size:12px;word-break:break-all;flex:1;min-width:240px">{{ status ? status.root : '…' }}</code>
        <button class="btn sm" :disabled="!status" @click="copyPath">复制路径</button>
        <button class="btn sm" :disabled="!status" @click="openFolder">打开文件夹</button>
        <button class="btn sm" :disabled="!!busy" @click="seedTemplate">生成模板</button>
      </div>

      <div v-if="status && !status.writable"
           style="margin-top:10px;padding:8px 10px;border-radius:6px;background:#fef0f0;color:#a32d2d;font-size:12px">
        这个目录写不进去（只读盘 / 没有权限），同步会失败
      </div>

      <div v-if="status" style="display:flex;gap:26px;margin-top:16px;flex-wrap:wrap">
        <div>
          <div style="font-size:22px;font-weight:600">{{ status.md_questions }}</div>
          <div class="muted small">篇题目 md</div>
        </div>
        <div>
          <div style="font-size:22px;font-weight:600">{{ status.linked }}</div>
          <div class="muted small">道题已关联文件</div>
        </div>
        <div>
          <div style="font-size:22px;font-weight:600">{{ status.md_total - status.md_questions }}</div>
          <div class="muted small">其他 md（不当作题目）</div>
        </div>
        <div>
          <div style="font-size:22px;font-weight:600"
               :style="{ color: status.inbox_pending ? 'var(--warning)' : 'inherit' }">{{ status.inbox_pending }}</div>
          <div class="muted small">收件箱待处理</div>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>同步</h2>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <button class="btn primary" :disabled="!!busy" @click="scan(false)">
          {{ busy === 'sync' ? '同步中…' : '同步（文件 → 题库）' }}
        </button>
        <button class="btn" :disabled="!!busy" @click="scan(true)">
          {{ busy === 'preview' ? '预览中…' : '只预览会改什么' }}
        </button>
        <span style="width:1px;height:20px;background:var(--border);margin:0 6px"></span>
        <button class="btn" :disabled="!!busy" @click="doExport">
          {{ busy === 'export' ? '导出中…' : '把题库里的题导出成 md' }}
        </button>
        <input type="number" min="1" max="2000" v-model="exportLimit"
               style="width:90px" :disabled="!!busy" />
        <span class="muted small">道（只导出还没有 md 的题）</span>
      </div>
      <div class="muted small" style="margin-top:10px;line-height:1.8">
        「预览」不改任何东西；「同步」会按文件内容更新题库，并在你<b>第一次</b>同步某篇时往它的
        frontmatter 里补一行 <code>shike_id</code>（题目身份，别手改）。
      </div>
    </div>

    <div class="card" v-if="inbox">
      <h2>收件箱（把卷子丢进去就自动识别）</h2>
      <div class="muted small" style="line-height:1.8;margin-bottom:10px">
        {{ inbox.hint }}。识别用的是拾课已有的整页识别，跑完会自动写成题目 md 放进「题库」。
        <span v-if="!inbox.enabled" style="color:var(--warning)">（收件箱已关闭：SHIKE_VAULT_INBOX=0）</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <button class="btn" :disabled="!!busy || !inbox.enabled" @click="runInbox">
          {{ busy === 'inbox' ? '处理中…' : '现在处理一次' }}
        </button>
        <span class="muted small">可用的体系子目录：{{ inbox.curricula.join('、') || '（还没有体系）' }}</span>
      </div>

      <div v-if="inbox.pending.length" style="margin-top:12px">
        <div class="muted small" style="margin-bottom:4px">等着处理的文件</div>
        <div v-for="f in inbox.pending" :key="f" class="small" style="line-height:1.9">· {{ f }}</div>
      </div>
      <div v-else class="muted small" style="margin-top:10px">收件箱是空的。</div>

      <div v-if="inbox.tasks.length" style="margin-top:12px">
        <div class="muted small" style="margin-bottom:4px">正在处理的</div>
        <div v-for="t in inbox.tasks" :key="t.file" class="small" style="line-height:1.9">
          · {{ t.file }}
          <span class="muted">— {{ t.stage || t.status }}</span>
          <span v-if="t.error" style="color:var(--danger)">（{{ t.error }}）</span>
        </div>
      </div>
    </div>

    <div class="card" v-if="report">
      <h2>{{ report.dry_run ? '预览结果（没有改动任何东西）' : '同步结果' }}</h2>
      <div class="muted small" style="margin-bottom:10px">扫了 {{ report.scanned }} 篇 md</div>

      <div v-if="nothingHappened && !report.skipped.length"
           class="small" style="color:var(--success);margin-bottom:10px">
        没有需要处理的东西，题库和文件是一致的。
      </div>

      <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin-bottom:12px">
        <span>新增 <b style="color:var(--success)">{{ report.created.length }}</b></span>
        <span>更新 <b style="color:var(--primary)">{{ report.updated.length }}</b></span>
        <span>未变 <b>{{ report.unchanged }}</b></span>
        <span>跳过 <b>{{ report.skipped.length }}</b></span>
        <span v-if="report.errors.length">失败 <b style="color:var(--danger)">{{ report.errors.length }}</b></span>
        <span v-if="report.warnings.length">提醒 <b style="color:var(--warning)">{{ report.warnings.length }}</b></span>
        <span v-if="report.extra_images.length">多图 <b style="color:var(--warning)">{{ report.extra_images.length }}</b></span>
        <span v-if="report.missing.length">文件丢失 <b style="color:var(--warning)">{{ report.missing.length }}</b></span>
      </div>

      <div v-if="report.created.length" style="margin-bottom:10px">
        <div class="muted small" style="margin-bottom:2px">新增</div>
        <div v-for="x in report.created" :key="'c'+x.path" class="small" style="line-height:1.9">
          <span style="color:var(--success)">+</span> {{ x.path }}
        </div>
      </div>

      <div v-if="report.updated.length" style="margin-bottom:10px">
        <div class="muted small" style="margin-bottom:2px">更新</div>
        <div v-for="x in report.updated" :key="'u'+x.path" class="small" style="line-height:1.9">
          <span style="color:var(--primary)">~</span> {{ x.path }}
        </div>
      </div>

      <div v-for="(e, i) in report.errors" :key="'e'+i"
           style="margin-bottom:6px;padding:8px 10px;border-radius:6px;background:#fef0f0;color:#a32d2d;font-size:12px;line-height:1.7">
        <div style="font-weight:500">{{ e.path }}</div>
        <div>{{ e.error }}<span v-if="e.detail">（{{ e.detail }}）</span></div>
      </div>

      <div v-for="(w, i) in report.warnings" :key="'w'+i"
           class="small" style="color:var(--warning);line-height:1.8">
        ! {{ w.path }}：{{ w.messages.join('；') }}
      </div>

      <div v-for="(x, i) in report.extra_images" :key="'x'+i"
           class="small" style="color:var(--warning);line-height:1.8">
        ! {{ x.path }}：多了 {{ x.images.length }} 张题干图没入库（{{ x.images.join('、') }}）
      </div>

      <div v-if="report.missing.length" style="margin-top:10px">
        <div class="muted small" style="margin-bottom:2px">
          这些题记着来源文件，但文件已经不在了（<b>没有删题</b>，只是提醒）
        </div>
        <div v-for="m in report.missing" :key="'m'+m.id" class="small"
             style="color:var(--warning);line-height:1.8">! {{ m.path }}</div>
      </div>

      <div v-if="report.skipped.length" style="margin-top:10px">
        <div class="muted small" style="margin-bottom:2px">跳过的文件</div>
        <div v-for="s in report.skipped" :key="'s'+s.path" class="small muted" style="line-height:1.8">
          - {{ s.path }}（{{ s.reason }}）
        </div>
      </div>
    </div>

    <div class="card" v-if="exportResult && exportResult.failed.length">
      <h2>这几道导出失败了</h2>
      <div v-for="f in exportResult.failed" :key="'f'+f.id" class="small"
           style="color:var(--danger);line-height:1.8">{{ f.id }}：{{ f.error }}</div>
    </div>
  </div>`,
};
