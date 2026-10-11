// 课表 Excel 导入弹窗 —— 三步：选文件 → 预览核对 → 结果。
//
// 为什么不做成「上传即入库」：Excel 是手敲的，写错学生名、把 10 月写成 9 月都很常见。
// 导进去再一条条删，比导入前看一眼麻烦得多。所以这里把「哪行能导、哪行有问题」
// 全摊开，老师确认后才真的写库（与批量入库页同一个思路）。
//
// ⚠️ 「学生对不上」的处理是这一页最要紧的分支（2026-10-11 加）：
//    表里写了「小明」，系统里却没有 —— 直接导进去只会得到一行跳过，老师回头还得
//    自己去学生页补档案、再导一次。所以预览一出来**先问一句「要不要现在建」**：
//      · 现在创建 → 逐个打开学生档案表单（StudentFormDialog，带默认单价），
//                   建完自动重新核对，原来对不上的行当场变成「可导入」
//      · 直接建空档案 → 保持旧行为：只落个名字，资料稍后补（这条行会没单价）
//      · 先不创建 → 只是不导这几行，别的照常
import { computed, ref } from 'vue';
import { download, scheduleImportApi } from '../api.js';
import Modal from './Modal.js';
import StudentFormDialog from './StudentFormDialog.js';
import { fail, ok } from '../store.js';

export default {
  name: 'ScheduleImportDialog',
  components: { Modal, StudentFormDialog },
  emits: ['close', 'done'],
  setup(props, { emit }) {
    const step = ref('pick');          // pick | preview | done
    const fileEl = ref(null);
    const busy = ref(false);
    const fileName = ref('');
    const lastFile = ref(null);        // 留一份文件 —— 建完学生要拿它重新核对
    const result = ref(null);          // 预览结果（后端 parse + 匹配后）
    const selected = ref(new Set());
    const createStudents = ref(false);
    const report = ref(null);

    // 「有学生系统里没有，要不要建」的询问框 + 逐个填档案的队列
    const ask = ref(false);
    const queue = ref([]);             // 还没建的学生名（建一个出队一个）
    const creating = ref('');          // 当前正在填档案的那个名字（空 = 没在填）
    const createTotal = ref(0);
    const created = ref(0);            // 这一轮里已经建好几个 —— 决定关掉时要不要重新核对

    const rows = computed(() => result.value?.rows || []);
    const counts = computed(() => result.value?.counts || {});
    const importable = computed(() => rows.value.filter((r) => r.action !== 'blocked' && r.action !== 'duplicate'));
    const unknownNames = computed(() => result.value?.unknown_students || []);
    const createHint = computed(() => {
      if (!creating.value) return '';
      const total = createTotal.value || queue.value.length;
      const i = total - queue.value.length + 1;
      return `课表里有 ${total} 个学生对不上。正在填第 ${i} 个：${creating.value}`
        + `（顺手把「默认单价」填上 —— 不填的话这节课的课时费算不出来）。`;
    });

    function pick() {
      if (fileEl.value) fileEl.value.click();
    }

    /**
     * 解析 + 匹配，把结果摊到预览里。
     * keepSelection=true 时保留老师已经勾过/取消过的行（建完学生回来重新核对用），
     * 同时把新变成「可导入」的行补勾上。
     */
    async function load(f, { keepSelection = false, askIfMissing = false } = {}) {
      busy.value = true;
      try {
        const fd = new FormData();
        fd.append('file', f);
        const prev = new Set(selected.value);
        const res = await scheduleImportApi.preview(fd);
        result.value = res;
        if (!res.ok) {
          fail(res.error || '读取失败');
          return false;                   // 停在原来那一步，让他换一个文件
        }
        const sel = new Set();
        for (const r of res.rows) {
          if (r.action === 'blocked') continue;
          if (r.action === 'import') sel.add(r.row_no);                    // 能导的默认勾上
          else if (keepSelection && prev.has(r.row_no)) sel.add(r.row_no); // 重核时保留老师手工勾的
        }
        selected.value = sel;
        step.value = 'preview';
        // 名字对不上就先问一句 —— 这是老师最容易漏的一步
        if (askIfMissing && (res.counts?.no_student || 0) > 0) ask.value = true;
        return true;
      } catch (err) {
        fail(err.message);
        return false;
      } finally {
        busy.value = false;
      }
    }

    async function onFile(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';                // 同一个文件再选一次也要能触发 change
      if (!f) return;
      fileName.value = f.name;
      lastFile.value = f;
      await load(f, { askIfMissing: true });
    }

    function toggle(row) {
      if (row.action === 'blocked') return;
      const s = new Set(selected.value);
      if (s.has(row.row_no)) s.delete(row.row_no); else s.add(row.row_no);
      selected.value = s;
    }

    function toggleAll(v) {
      const s = new Set();
      if (v) for (const r of rows.value) if (r.action !== 'blocked') s.add(r.row_no);
      selected.value = s;
    }

    // 勾上「建空档案」时，把那些原本因为名字对不上而没勾的行也带进来 ——
    // 否则用户勾了开关却没反应，会以为坏了。
    function onToggleCreate(v) {
      createStudents.value = v;
      if (v) {
        const s = new Set(selected.value);
        for (const r of rows.value) if (r.action === 'no_student') s.add(r.row_no);
        selected.value = s;
      }
    }

    const allChecked = computed(() =>
      importable.value.length > 0 && importable.value.every((r) => selected.value.has(r.row_no)));

    // ---------------------------------------------------------------- 建学生
    /** 老师点了「现在创建」：逐个开学生档案表单 */
    function startCreate() {
      ask.value = false;
      queue.value = [...unknownNames.value];
      createTotal.value = queue.value.length;
      created.value = 0;
      creating.value = queue.value[0] || '';
    }

    /** 老师选了「直接建空档案」：只落名字，不填资料（旧行为，快但没单价） */
    function createBlankAll() {
      ask.value = false;
      onToggleCreate(true);
      ok('已选上 —— 这几位会以「只有名字」的空档案建出来，资料要之后到学生页补');
    }

    function onStudentSaved(saved) {
      created.value += 1;
      // 按「原来对不上的那个名字」出队（老师可能顺手改了名 —— 改了就说明表里那个名字
      // 还是不存在，重新核对时会再提示一次，不会漏）
      const rest = queue.value.filter((n) => n !== creating.value);
      queue.value = rest;
      if (rest.length) {
        creating.value = rest[0];
      } else {
        creating.value = '';
        refresh();
      }
    }

    function cancelCreate() {
      creating.value = '';
      queue.value = [];
      if (created.value) refresh();       // 已经建了几个 → 名单变了，得重新核对
    }

    /** 建完学生后拿同一份文件重新解析/匹配 —— 原来「对不上」的行会当场变成「可导入」 */
    async function refresh() {
      if (!lastFile.value) return;
      const okk = await load(lastFile.value, { keepSelection: true });
      if (okk) {
        const left = counts.value.no_student || 0;
        ok(left
          ? `已重新核对 —— 还有 ${left} 行对不上学生`
          : '已重新核对 —— 对不上的学生都建好了，这几行现在可以导入');
      }
    }

    async function commit() {
      const picked = rows.value.filter((r) => selected.value.has(r.row_no));
      if (!picked.length) {
        fail('还没有勾选任何一行');
        return;
      }
      busy.value = true;
      try {
        const body = {
          create_students: createStudents.value,
          rows: picked.map((r) => ({
            row_no: r.row_no,
            date: r.date,
            time: r.time,
            student: r.student,
            student_id: r.student_id,
            duration_min: r.duration_min,
            topic: r.topic,
            mode: r.mode,
            location: r.location,
            rate: r.rate,
            status: r.status,
          })),
        };
        report.value = await scheduleImportApi.commit(body);
        step.value = 'done';
        ok(`已导入 ${report.value.created} 节课`);
        emit('done');
      } catch (e) {
        fail(e.message);
      } finally {
        busy.value = false;
      }
    }

    async function getTemplate() {
      try {
        await download(scheduleImportApi.templateUrl(), '课表导入模板.xlsx');
      } catch (e) {
        fail(e.message);
      }
    }

    const ACTION_TEXT = {
      import: '可导入',
      duplicate: '与已排的课重复',
      no_student: '系统里没有这个学生',
      blocked: '这行有问题',
    };
    const ACTION_TAG = { import: 'green', duplicate: 'orange', no_student: 'orange', blocked: 'red' };

    // 「重复」有两种，处理方式完全不同：表内重复多半是复制上一行时漏改了日期（赶紧回 Excel 改），
    // 与库里重复则可能是有意补排。所以文案必须分开说，不能都叫「重复」。
    function dupText(r) {
      return r.duplicate_kind === 'file' ? '表内写重了' : ACTION_TEXT.duplicate;
    }

    function dupDetail(r) {
      const d = r.duplicate_of;
      if (!d) return '';
      if (d.in_file_row) return `和本表第 ${d.in_file_row} 行一模一样`;
      if (d.start_at) return `已有一节 ${String(d.start_at).replace('T', ' ')}`;
      return '';
    }

    return {
      step, fileEl, busy, fileName, result, rows, counts, selected, createStudents, report,
      ask, queue, creating, createHint, unknownNames,
      pick, onFile, toggle, toggleAll, toggleAllChecked: allChecked, onToggleCreate, commit,
      getTemplate, ACTION_TEXT, ACTION_TAG, importable, dupText, dupDetail,
      startCreate, createBlankAll, onStudentSaved, cancelCreate, refresh,
    };
  },
  template: `
  <Modal :title="step === 'pick' ? '导入课表（Excel）' : (step === 'preview' ? '核对一下要导入的课' : '导入完成')"
         wide @close="$emit('close')">

    <!-- ---------- 第一步：选文件 ---------- -->
    <template v-if="step === 'pick'">
      <div class="empty" style="padding:22px 0">
        <div style="margin-bottom:10px">选一个 .xlsx 课表文件，先预览，确认无误再入库。</div>
        <button class="btn primary" :disabled="busy" @click="pick">
          {{ busy ? '正在读取…' : '选择 Excel 文件' }}
        </button>
        <input ref="fileEl" type="file" accept=".xlsx" style="display:none" @change="onFile">
        <div style="margin-top:14px">
          <button class="btn ghost sm" @click="getTemplate">下载导入模板（含格式说明）</button>
        </div>
        <div class="small muted" style="margin-top:12px; max-width:520px; margin-left:auto; margin-right:auto; text-align:left">
          <b>最少只要三列：</b>日期、开始时间、学生。<br>
          日期写 2026-10-01 / 2026/10/1 / 10-01 都行；时间写 19:00 或 19:00-20:30；
          其余列（时长、内容、形式、地点、单价、状态）都可以留空。<br>
          表头名字要对得上，<b>列的顺序随便换</b>，自己加的「备注」列会被忽略。老式 .xls 请先另存为 .xlsx。<br>
          表里如果出现了系统里还没有的学生，导入前会问你要不要顺手建好。
        </div>
      </div>
    </template>

    <!-- ---------- 第二步：预览核对 ---------- -->
    <template v-else-if="step === 'preview' && result">
      <div class="import-summary">
        <span>共 <b>{{ counts.total }}</b> 行</span>
        <span class="tag green">可导入 {{ counts.ready }}</span>
        <span v-if="counts.no_student" class="tag orange">学生对不上 {{ counts.no_student }}</span>
        <span v-if="counts.duplicate" class="tag orange">重复 {{ counts.duplicate }}</span>
        <span v-if="counts.duplicate_in_file" class="tag red">表内写重 {{ counts.duplicate_in_file }}</span>
        <span v-if="counts.problem" class="tag red">有错 {{ counts.problem }}</span>
        <span v-if="counts.no_rate" class="tag orange">无单价 {{ counts.no_rate }}</span>
        <span class="muted small">{{ fileName }} · 工作表「{{ result.sheet }}」</span>
      </div>

      <div v-for="n in (result.notes || [])" :key="n" class="small" style="color:var(--warning); margin:4px 0">
        ⚠️ {{ n }}
      </div>

      <div v-if="counts.no_student" class="import-unknown">
        <div>
          <b>{{ counts.no_student }} 行写的学生，系统里还没有</b>：{{ unknownNames.join('、') }}
        </div>
        <div class="row" style="margin-top:8px; align-items:center; gap:14px; flex-wrap:wrap">
          <button class="btn primary sm" @click="startCreate">
            去创建这 {{ unknownNames.length }} 个学生
          </button>
          <label style="display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer">
            <input type="checkbox" :checked="createStudents" @change="onToggleCreate($event.target.checked)">
            或直接建空档案（只落个名字，资料之后补）
          </label>
        </div>
        <div class="small muted" style="margin-top:6px">
          「去创建」会一个一个打开学生档案让你填（含默认单价）；填完自动重新核对，
          这几行当场就能导入。名字是表里写错了的话，建之前先扫一眼上面这串名字。
        </div>
      </div>

      <div style="max-height:52vh; overflow:auto; border:1px solid var(--border); border-radius:6px">
        <table class="tbl">
          <thead>
            <tr>
              <th style="width:34px">
                <input type="checkbox" :checked="toggleAllChecked"
                       @change="toggleAll($event.target.checked)">
              </th>
              <th style="width:40px">行</th>
              <th style="width:96px">日期</th>
              <th style="width:56px">时间</th>
              <th>学生</th>
              <th style="width:56px">时长</th>
              <th style="width:96px">单价</th>
              <th>本次内容</th>
              <th style="width:88px">状态</th>
              <th style="width:150px">检查结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.row_no" :style="r.action === 'blocked' ? 'background:var(--danger-weak)' : ''">
              <td>
                <input type="checkbox" :checked="selected.has(r.row_no)"
                       :disabled="r.action === 'blocked'" @change="toggle(r)">
              </td>
              <td class="muted small">{{ r.row_no }}</td>
              <td class="mono small">{{ r.date || '—' }}</td>
              <td class="mono small">{{ r.time || '—' }}</td>
              <td>
                {{ r.student || '—' }}
                <span v-if="r.student_exists === false" class="tag orange" style="margin-left:4px">新学生</span>
              </td>
              <td class="small">{{ r.duration_min }}′</td>
              <td class="small" style="width:96px">
                <template v-if="r.rate_effective === null || r.rate_effective === undefined">
                  <span style="color:var(--warning)">没有单价</span>
                </template>
                <template v-else>{{ r.rate_effective }} 元</template>
              </td>
              <td class="small muted">{{ r.topic || '—' }}</td>
              <td class="small">{{ r.status === 'scheduled' ? '已排课' : r.status }}</td>
              <td class="small">
                <span class="tag" :class="ACTION_TAG[r.action]">{{ r.action === 'duplicate' ? dupText(r) : ACTION_TEXT[r.action] }}</span>
                <div v-for="e in r.errors" :key="e" style="color:var(--danger); margin-top:2px">{{ e }}</div>
                <div v-if="r.duplicate_of" class="muted" style="margin-top:2px">
                  {{ dupDetail(r) }}
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="small muted" style="margin-top:8px">
        单价留空的行，会用学生档案里的默认单价，并按这节课「快照」存下来（以后改学生单价不影响已排的课）。
        两边都没有的话，这节课的课时费会是空的 —— 上面会标「没有单价」，月底汇总不会计入。
      </div>
    </template>

    <!-- ---------- 第三步：结果 ---------- -->
    <template v-else-if="step === 'done' && report">
      <div class="empty" style="padding:18px 0">
        <div style="font-size:16px; color:var(--success)">已导入 {{ report.created }} 节课</div>
        <div v-if="report.created_students && report.created_students.length" class="small muted" style="margin-top:6px">
          新建了学生：{{ report.created_students.join('、') }}
        </div>
      </div>
      <div v-if="report.skipped" style="margin-top:8px">
        <div class="small" style="color:var(--warning)">跳过 {{ report.skipped }} 行：</div>
        <table class="tbl" style="margin-top:6px">
          <tbody>
            <tr v-for="s in report.skipped_rows" :key="s.row_no + s.reason">
              <td class="small muted" style="width:48px">第 {{ s.row_no }} 行</td>
              <td class="small">{{ s.student || '（没写学生）' }}</td>
              <td class="small muted">{{ s.reason }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="small muted" style="margin-top:10px">
        课已经排进课表了，可以直接在日历上点开改。
      </div>
    </template>

    <template #foot>
      <template v-if="step === 'preview'">
        <button class="btn" @click="step = 'pick'">换一个文件</button>
        <div class="spacer"></div>
        <button class="btn primary" :disabled="busy || !selected.size" @click="commit">
          {{ busy ? '导入中…' : '确认导入选中的 ' + selected.size + ' 节' }}
        </button>
      </template>
      <template v-else-if="step === 'done'">
        <div class="spacer"></div>
        <button class="btn primary" @click="$emit('close')">知道了</button>
      </template>
      <template v-else>
        <div class="spacer"></div>
        <button class="btn" @click="$emit('close')">取消</button>
      </template>
    </template>
  </Modal>

  <!-- ---------- 问一句：这些学生要不要现在建？ ---------- -->
  <Modal v-if="ask" title="有学生系统里还没有，要现在建吗？" @close="ask = false">
    <div>这份课表里有 <b>{{ counts.no_student }}</b> 行的学生，系统里找不到：</div>
    <div style="margin:8px 0; font-weight:500">{{ unknownNames.join('、') }}</div>
    <div class="small muted">
      <b>现在创建</b>：一个一个打开学生档案让你填（昵称、年级、默认单价…）。填完自动重新核对，
      这几行当场就能一起导入 —— 顺手填上默认单价，月底算课时费才不会漏掉这几节。<br>
      <b>直接建空档案</b>：快，但档案里只有名字、没有单价，导进去的课金额是空的。
    </div>
    <div class="small muted" style="margin-top:8px">
      也可以先不建，回 Excel 确认名字有没有写错，改好再导。
    </div>
    <template #foot>
      <button class="btn" @click="ask = false">先不创建</button>
      <div class="spacer"></div>
      <button class="btn" @click="createBlankAll">直接建空档案（{{ unknownNames.length }} 个）</button>
      <button class="btn primary" @click="startCreate">现在创建（进学生档案）</button>
    </template>
  </Modal>

  <!-- ---------- 逐个填学生档案 ---------- -->
  <StudentFormDialog v-if="creating" :preset-name="creating" :hint="createHint"
                     @close="cancelCreate" @saved="onStudentSaved" />`,
};
