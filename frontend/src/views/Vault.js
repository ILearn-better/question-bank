// 题库文件（vault）：用 Obsidian 管题，拾课负责把文件同步进库。
//
// 这一页解决的是「正文放在哪」的问题：以前题目只在 SQLite 里，老师想用
// Obsidian 的编辑体验（双链、大纲、全文搜索、随手贴图）没有任何入口。
// 现在一道题 = 磁盘上一个 .md，这边只做两件事：**导出**与**同步**。
//
// 页面上唯一不能省的是「报告」：同步是**会改数据**的动作，老师必须能看清
// 「哪几篇进来了、哪几篇没进来、为什么」。只回一句「同步完成」等于什么都没说。
//
// ⚠️ 收件箱（`收件箱/<体系>/` 丢 PDF、插件转出来的 md）**不在这一页露出**。
//    那条路的用户是 agent：扫描是后台每 20 秒自动跑，接口是
//    `GET /api/vault/inbox`、`POST /api/vault/inbox/run`（见 docs/收件箱与讲义-给agent.md）。
//    老师在界面上需要做的只有「同步 / 导出」，把收件箱的说明和按钮摆在这里
//    只会让他以为「拖进去还得点一下」。
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
    const busy = ref('');
    const report = ref(null);
    const exportResult = ref(null);
    const exportLimit = ref(200);

    const nothingHappened = computed(() => {
      const r = report.value;
      if (!r) return false;
      const lec = (r.lectures || []).reduce(
        (n, x) => n + x.created.length + x.updated.length + x.errors.length, 0);
      return !r.created.length && !r.updated.length && !r.errors.length && !lec;
    });

    async function load() {
      try {
        status.value = await vaultApi.status();
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
        // 讲义的题单独挂在 r.lectures 里，报数时要把它们并进来 —— 否则
        // 老师刚同步完一篇 5 道题的讲义，提示写着「新增 0」，会以为没生效。
        const lp = (r.lectures || []).reduce((n, x) => n + x.created.length, 0);
        const lu = (r.lectures || []).reduce((n, x) => n + x.updated.length, 0);
        const ln = (r.lectures || []).reduce((n, x) => n + x.unchanged, 0);
        const lec = lp + lu ? `（讲义里 ${lp + lu} 道）` : '';
        if (dry) {
          ok(`预览完成：会新增 ${r.created.length + lp} 道、更新 ${r.updated.length + lu} 道${lec}`);
        } else {
          ok(`同步完成：新增 ${r.created.length + lp}、更新 ${r.updated.length + lu}、未变 ${r.unchanged + ln}${lec}`);
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
        // 后端按「一批多少道」收口（输入框那个数）。老师点导出就是想都导出来，
        // 不该为了 256 道题点两下 —— 这里接着要下一批，直到后端说没有了。
        const limit = Math.max(1, Number(exportLimit.value) || 200);
        let total = 0;
        const failed = [];
        let note = '';
        for (let round = 0; round < 100; round += 1) {
          const r = await vaultApi.export([], limit);
          total += r.exported.length;
          note = r.note || '';
          if (r.failed && r.failed.length) failed.push(...r.failed);
          exportResult.value = { exported: [], failed, total };
          if (!/再点一次/.test(note)) break;
        }
        exportResult.value = { exported: [], failed, total };
        if (total) ok(`导出了 ${total} 道题到文件夹`);
        else if (failed.length) ok(`有 ${failed.length} 道没导出来，看下面`);
        else ok(note || '没有需要导出的题（每道题的文件都还在）');
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
        ok(r.existed && r.lecture_existed
          ? '两份模板都已经有了，没有覆盖：' + r.path + '、' + r.lecture_path
          : '模板已生成：' + (r.existed ? '' : r.path + ' ') + (r.lecture_existed ? '' : r.lecture_path));
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
      status, busy, report, nothingHappened, exportResult, exportLimit,
      load, scan, doExport, seedTemplate, openFolder, copyPath,
    };
  },
  template: `
  <div style="max-width:1000px">
    <div class="card">
      <h2>题库文件</h2>
      <div class="muted small" style="line-height:1.8;margin-bottom:12px">
        一道题一个 <code>.md</code> 文件，放在这个文件夹里。用 Obsidian「打开文件夹作为库」就能改题、
        补答案、贴图；改完点「同步」把改动收进题库。<b>不在</b>这个文件夹里的题（网页上录的）不受影响。<br />
        讲义（一份 md 里既写讲解又放题）放在 <code>讲义</code> 文件夹，开头写
        <code>shike: lecture</code>；同步时<b>只收</b>它 <code>## 题目</code> 下面的每一道题，知识部分一个字符都不动。
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
          <div style="font-size:22px;font-weight:600">{{ status.md_lectures }}</div>
          <div class="muted small">篇讲义</div>
        </div>
        <div v-if="status.note_linked">
          <div style="font-size:22px;font-weight:600">{{ status.note_linked }}</div>
          <div class="muted small">道题的正文在讲义里</div>
        </div>
        <div>
          <div style="font-size:22px;font-weight:600">{{ status.md_total - status.md_questions - status.md_lectures }}</div>
          <div class="muted small">其他 md（既不是题也不是讲义）</div>
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
        <span class="muted small">道（只导出磁盘上还没有文件的题；文件删了也会重新写出来）</span>
      </div>
      <div class="muted small" style="margin-top:10px;line-height:1.8">
        「预览」不改任何东西；「同步」会按文件内容更新题库，并在你<b>第一次</b>同步某篇时往它的
        frontmatter 里补一行 <code>shike_id</code>（题目身份，别手改）。讲义里则是在每道题的
        标题下面补一行 <code>shike_id</code> 注释 —— Obsidian 阅读视图看不到它。
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
        <span v-if="report.lectures && report.lectures.length">讲义 <b>{{ report.lectures.length }}</b></span>
        <span>跳过 <b>{{ report.skipped.length }}</b></span>
        <span v-if="report.errors.length">失败 <b style="color:var(--danger)">{{ report.errors.length }}</b></span>
        <span v-if="report.warnings.length">提醒 <b style="color:var(--warning)">{{ report.warnings.length }}</b></span>
        <span v-if="report.extra_images.length">多图 <b style="color:var(--warning)">{{ report.extra_images.length }}</b></span>
        <span v-if="report.missing.length">文件丢失 <b style="color:var(--warning)">{{ report.missing.length }}</b></span>
      </div>

      <!-- 讲义：一篇对应好几道题，单独一栏，别和「新增 N 道」混在一起 -->
      <div v-if="report.lectures && report.lectures.length" style="margin-bottom:12px">
        <div class="muted small" style="margin-bottom:2px">讲义（只收题目部分，知识部分不动）</div>
        <div v-for="l in report.lectures" :key="'L'+l.path" class="small" style="line-height:1.9">
          · {{ l.path }}
          <span v-if="l.created.length" style="color:var(--success)">新增 {{ l.created.length }} 道</span>
          <span v-if="l.updated.length" style="color:var(--primary)">更新 {{ l.updated.length }} 道</span>
          <span v-if="l.unchanged" class="muted">未变 {{ l.unchanged }} 道</span>
          <div v-for="(s, si) in l.skipped" :key="'Ls'+si" class="muted" style="padding-left:16px">
            第 {{ s.unit }} 段：{{ s.reason }}
          </div>
          <div v-for="(e, ei) in l.errors" :key="'Le'+ei" style="color:var(--danger);padding-left:16px">
            第 {{ e.unit }} 段：{{ e.error }}
          </div>
          <div v-for="(w, wi) in l.warnings" :key="'Lw'+wi" style="color:var(--warning);padding-left:16px">
            第 {{ w.unit }} 段：{{ w.messages.join('；') }}
          </div>
        </div>
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
