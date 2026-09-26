// 设置：体系 / 能力维度 / 系统与数据安全。
// 「系统信息」这一块不是装饰 —— 它把 Phase 0 重构的三个验收项直接摆出来：
// 迁移到哪个版本、WAL 有没有开、外键约束到底生效了没有。
import { onMounted, reactive, ref } from 'vue';
import { abilityApi, aiApi, curriculumApi, systemApi } from '../api.js';
import { fail, loadCurricula, loadDims, money, ok, todayISO } from '../store.js';

export default {
  name: 'Settings',
  setup() {
    const info = ref(null);
    const backups = ref([]);
    const fk = ref(null);
    const curricula = ref([]);
    const dims = ref([]);
    const newCurr = reactive({ code: '', name: '', region: 'CN', stage: 'senior' });
    const newDim = reactive({ name: '', sort_order: 0 });
    const busy = ref('');

    // ---- AI 润色配置 ----
    const ai = ref(null);                 // 后端返回的是脱敏形态（只有 has_key / key_hint）
    const aiForm = reactive({ base_url: '', model: '', api_key: '', timeout: 60 });
    const aiTest = ref(null);

    async function loadAi() {
      try {
        const cfg = await aiApi.settings();
        ai.value = cfg;
        aiForm.base_url = cfg.base_url || '';
        aiForm.model = cfg.model || '';
        aiForm.timeout = cfg.timeout || 60;
        aiForm.api_key = '';            // 密钥永远不回填：接口根本不返回明文
      } catch (e) {
        fail(e.message);
      }
    }

    async function saveAi() {
      busy.value = 'ai';
      try {
        // api_key 留空 = 不改（库里那份保持不变）
        ai.value = await aiApi.save({
          base_url: aiForm.base_url, model: aiForm.model, timeout: aiForm.timeout,
          api_key: aiForm.api_key || '',
        });
        aiForm.api_key = '';
        ok('AI 配置已保存');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function clearAiKey() {
      if (!window.confirm('清除已保存的 API 密钥？清掉之后 AI 润色就用不了了。')) return;
      try {
        ai.value = await aiApi.save({ clear_key: true });
        ok('密钥已清除');
      } catch (e) {
        fail(e.message);
      }
    }

    async function testAi() {
      busy.value = 'ai-test';
      aiTest.value = null;
      try {
        const r = await aiApi.test();
        aiTest.value = { ok: true, msg: `连接正常，模型回复：「${r.reply}」` };
      } catch (e) {
        aiTest.value = { ok: false, msg: e.message };
      } finally {
        busy.value = '';
      }
    }

    async function load() {
      try {
        const [i, b, c, d] = await Promise.all([
          systemApi.info(), systemApi.backups(), curriculumApi.list(), abilityApi.dims(),
        ]);
        info.value = i;
        backups.value = b;
        curricula.value = c;
        dims.value = d;
        await loadCurricula(true);
        await loadDims(true);
      } catch (e) {
        fail(e.message);
      }
    }

    async function addCurriculum() {
      if (!newCurr.code.trim() || !newCurr.name.trim()) {
        fail('体系标识和名称都要填');
        return;
      }
      try {
        await curriculumApi.create({ ...newCurr, subject: 'math', sort_order: curricula.value.length + 1 });
        Object.assign(newCurr, { code: '', name: '', region: 'CN', stage: 'senior' });
        ok('体系已添加');
        await load();
      } catch (e) {
        fail(e.message);
      }
    }

    async function addDim() {
      if (!newDim.name.trim()) {
        fail('维度名称不能为空');
        return;
      }
      try {
        await abilityApi.createDim({ name: newDim.name, sort_order: dims.value.length + 1 });
        newDim.name = '';
        ok('维度已添加');
        await load();
      } catch (e) {
        fail(e.message);
      }
    }

    async function disableDim(d) {
      if (!confirm(`停用「${d.name}」？历史评分会保留，只是不再出现在打分界面。`)) return;
      try {
        await abilityApi.disableDim(d.id);
        ok('已停用');
        await load();
      } catch (e) {
        fail(e.message);
      }
    }

    async function doBackup() {
      busy.value = 'backup';
      try {
        const r = await systemApi.backup();
        ok(r.ok ? `已备份：${r.file}` : r.message);
        await load();
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    async function runFkCheck() {
      busy.value = 'fk';
      try {
        fk.value = await systemApi.fkCheck();
        ok(fk.value.ok ? '数据库完整性正常，无孤儿数据' : '发现外键违规，请检查');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = '';
      }
    }

    function fmtSize(n) {
      if (n === null || n === undefined) return '—';
      if (n < 1024) return n + ' B';
      if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
      return (n / 1024 / 1024).toFixed(2) + ' MB';
    }

    onMounted(() => { load(); loadAi(); });

    return {
      info, backups, fk, curricula, dims, newCurr, newDim, busy,
      addCurriculum, addDim, disableDim, doBackup, runFkCheck, fmtSize, money, todayISO,
      ai, aiForm, aiTest, saveAi, clearAiKey, testAi,
    };
  },
  template: `
  <div>
    <div class="page-head">
      <h1>设置</h1>
      <div class="spacer"></div>
      <button class="btn" @click="load">刷新</button>
    </div>

    <div class="card">
      <h2>系统与数据</h2>
      <div v-if="!info" class="empty">加载中…</div>
      <template v-else>
        <div class="grid cols-4">
          <div class="stat"><div class="v" style="font-size:17px">{{ info.app }}</div><div class="k">版本 {{ info.version }}</div></div>
          <div class="stat"><div class="v mono" style="font-size:17px">{{ info.revision }}</div><div class="k">数据库结构版本（Alembic）</div></div>
          <div class="stat">
            <div class="v" style="font-size:17px" :style="info.db.journal_mode === 'wal' ? 'color:var(--success)' : 'color:var(--danger)'">
              {{ info.db.journal_mode }}
            </div>
            <div class="k">日志模式（wal = 读写不阻塞）</div>
          </div>
          <div class="stat">
            <div class="v" style="font-size:17px" :style="info.db.foreign_keys ? 'color:var(--success)' : 'color:var(--danger)'">
              {{ info.db.foreign_keys ? '已开启' : '未开启' }}
            </div>
            <div class="k">外键约束</div>
          </div>
        </div>

        <table class="tbl" style="margin-top:12px">
          <tbody>
            <tr><th style="width:110px">数据库</th><td class="mono small">{{ info.db.path }}（{{ fmtSize(info.db.size_bytes) }}）</td></tr>
            <tr>
              <th>附加文件</th>
              <td class="small">
                <span v-if="info.db.wal_files.length" class="mono">{{ info.db.wal_files.join(' , ') }}</span>
                <span v-else>无</span>
                <div class="muted">WAL 模式下会有这两个文件，属正常状态。备份请用下面的一键备份，不要手工复制 .db 文件。</div>
              </td>
            </tr>
            <tr><th>数据目录</th><td class="mono small">{{ info.dirs.data }}</td></tr>
            <tr>
              <th>数据量</th>
              <td class="small">
                体系 {{ info.counts.curricula }} · 知识点 {{ info.counts.nodes }} · 题目 {{ info.counts.questions }}
                · 学生 {{ info.counts.students }} · 课时 {{ info.counts.lessons }} · 反馈 {{ info.counts.feedbacks }}
              </td>
            </tr>
          </tbody>
        </table>

        <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap">
          <button class="btn" :disabled="busy === 'fk'" @click="runFkCheck">
            {{ busy === 'fk' ? '检查中…' : '运行数据完整性自检' }}
          </button>
          <span v-if="fk" class="tag" :class="fk.ok ? 'green' : 'red'" style="align-self:center">
            {{ fk.ok ? '完整性 OK，无外键违规' : '发现 ' + fk.violations + ' 处违规' }}
          </span>
        </div>
      </template>
    </div>

    <div class="card">
      <h2>备份</h2>
      <div class="small muted" style="margin-bottom:10px">
        使用 SQLite 官方备份接口，会把 WAL 中尚未合并的改动一并算进去 —— 因此比手工复制文件可靠。
        软件每次启动时会自动备份一次（保留最近 10 份）。
      </div>
      <button class="btn primary" :disabled="busy === 'backup'" @click="doBackup">
        {{ busy === 'backup' ? '备份中…' : '立即备份' }}
      </button>
      <table class="tbl" style="margin-top:12px" v-if="backups.length">
        <thead><tr><th>备份文件</th><th>大小</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="b in backups" :key="b.name">
            <td class="mono small">{{ b.name }}</td>
            <td class="small">{{ fmtSize(b.size_bytes) }}</td>
            <td class="small muted">{{ b.created_at.replace('T', ' ') }}</td>
          </tr>
        </tbody>
      </table>
      <div v-else class="empty">还没有备份</div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h2>体系</h2>
        <div class="small muted" style="margin-bottom:10px">
          多体系是一等公民：新增一个体系只需在这里加一行，不用改任何代码。
        </div>
        <table class="tbl">
          <thead><tr><th>名称</th><th>标识</th><th>地区</th><th>知识点</th></tr></thead>
          <tbody>
            <tr v-for="c in curricula" :key="c.id">
              <td>{{ c.name }}</td>
              <td class="mono small">{{ c.code }}</td>
              <td class="small">{{ c.region || '—' }}</td>
              <td class="small">{{ c.node_count }}</td>
            </tr>
          </tbody>
        </table>
        <div class="row" style="margin-top:12px">
          <input type="text" v-model="newCurr.code" placeholder="标识，如 ib-math">
          <input type="text" v-model="newCurr.name" placeholder="名称，如 IB 数学">
          <button class="btn" style="flex:none" @click="addCurriculum">添加</button>
        </div>
      </div>

      <div class="card">
        <h2>能力维度</h2>
        <div class="small muted" style="margin-bottom:10px">
          家长报告雷达图的骨架。全部存数据库、不写死在代码里 ——
          同行教物理化学时换一套维度即可，这是这东西能交付给别人的前提。
        </div>
        <table class="tbl">
          <thead><tr><th>#</th><th>维度</th><th></th></tr></thead>
          <tbody>
            <tr v-for="(d, i) in dims" :key="d.id">
              <td class="muted small">{{ i + 1 }}</td>
              <td>{{ d.name }}</td>
              <td style="text-align:right">
                <button class="btn ghost sm" style="color:var(--danger)" @click="disableDim(d)">停用</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div class="row" style="margin-top:12px">
          <input type="text" v-model="newDim.name" placeholder="新维度名称">
          <button class="btn" style="flex:none" @click="addDim">添加</button>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>AI 润色</h2>
      <div class="small muted" style="margin-bottom:10px">
        课后反馈里的「AI 润色」按钮用这里的配置。走的是 <strong>OpenAI 兼容</strong>接口，
        DeepSeek / 通义 / Kimi / 智谱，以及本机跑的 Ollama、vLLM 都是这个格式，
        所以只需要填地址和模型名。地址填到 <span class="mono">…/v1</span> 或直接填域名均可，会自动补齐。
      </div>
      <div class="small" style="margin-bottom:10px;color:var(--warning, #d97706)">
        ⚠️ 点「AI 润色」时，反馈正文（通常含学生姓名）会发送到你配置的服务商。
        不配置就完全不会联网，功能照常用。
      </div>

      <div class="grid cols-2">
        <div class="field">
          <label>接口地址</label>
          <input type="text" v-model="aiForm.base_url" placeholder="如 https://api.deepseek.com">
        </div>
        <div class="field">
          <label>模型名</label>
          <input type="text" v-model="aiForm.model" placeholder="如 deepseek-chat">
        </div>
        <div class="field">
          <label>
            API 密钥
            <span v-if="ai && ai.has_key" class="muted small">（已保存 {{ ai.key_hint }}；留空表示不改）</span>
          </label>
          <input type="password" v-model="aiForm.api_key" :placeholder="ai && ai.has_key ? '留空 = 不修改' : 'sk-...'">
        </div>
        <div class="field">
          <label>超时（秒）</label>
          <input type="number" v-model.number="aiForm.timeout" min="5" max="300">
        </div>
      </div>

      <div v-if="ai && ai.from_env && ai.from_env.length" class="small muted" style="margin-bottom:8px">
        环境变量已覆盖：<span class="mono">{{ ai.from_env.join(' , ') }}</span>
        （不想把密钥落库的话，就只设环境变量）
      </div>

      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <button class="btn primary" :disabled="busy === 'ai'" @click="saveAi">
          {{ busy === 'ai' ? '保存中…' : '保存配置' }}
        </button>
        <button class="btn" :disabled="busy === 'ai-test'" @click="testAi">
          {{ busy === 'ai-test' ? '测试中…' : '测试连接' }}
        </button>
        <button v-if="ai && ai.has_key" class="btn ghost" style="color:var(--danger)" @click="clearAiKey">清除密钥</button>
        <span v-if="ai && ai.configured" class="tag green" style="align-self:center">已配置</span>
        <span v-else class="tag" style="align-self:center">未配置</span>
      </div>
      <div v-if="aiTest" class="small" :style="aiTest.ok ? 'color:var(--success);margin-top:10px' : 'color:var(--danger);margin-top:10px'">
        {{ aiTest.msg }}
      </div>
    </div>

    <div class="card">
      <h2>题库录题</h2>
      <div class="small muted" style="margin-bottom:10px">
        试卷 PDF / Word 的分割录入功能保持原样，还没并进新的界面结构里（开发文档里的 Phase 5 会做）。
        现在通过一个独立页面进入，功能一点没减。
      </div>
      <a href="/entry.html" class="btn">打开录题工具 →</a>
    </div>
  </div>`,
};
