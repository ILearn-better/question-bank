// 笔记：Markdown 正文 + 板书笔画 + 配图。
//
// 三栏：左边（笔记列表）／中间（编辑·预览·分栏）／右边（本文目录 + 公式速查，各自可收起）。
//
// 几个刻意的决定，写下来免得以后自己都忘了为什么：
//   · 「本文目录」在**右**边（用户 2026-10-05 提的）：它是给预览导航用的，
//     挨着正文比压在左侧笔记列表那栏里顺手；两个侧栏区块各自有开关，
//     列数由类名决定（with-side / with-right），不跟「哪个模式」硬绑。
//   · 目录是从**渲染后的 DOM** 里扫 <h1..h6> 得到的，不是用正则去解析 Markdown。
//     正则会被代码块里的 "#" 骗到，而且一旦和渲染器对不上就永远不同步。
//   · 笔画存矢量（坐标归一化到 0~1），不存位图：橡皮、换色、调粗细、撤销
//     本质都是「按新状态重画一遍」，位图做不到，而且缩放会糊。
//   · 预览走 innerHTML，所以必须过 DOMPurify —— marked 自己不做消毒。
//   · 保存是防抖 PATCH 且**只提交改动过的字段**，这样切笔记时不会把另一头覆盖掉。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { notesApi, noteFoldersApi, notesBackupApi } from '../api.js';
import { fail, ok, warn } from '../store.js';
import Modal from '../components/Modal.js';
import QuestionPicker from '../components/QuestionPicker.js';

import { ensureLibs, renderRichEl, resetLibs } from '../mathRender.js';

const SAMPLE = `# 新笔记

在这里写内容，支持 **Markdown**：标题、列表、表格、代码块、引用都行。

## 公式

行内公式用单个美元号：$y = kx + b$；独立一行用两个：

$$x = \\frac{-b \\pm \\sqrt{b^2 - 4ac}}{2a}$$

> 右边「公式速查」面板里有常用写法，点一下就插到光标处，光标会停在空位上。

## 插图

工具栏「插入图片」，或者**直接 Ctrl+V 粘贴截图**。

## 板书

工具栏「画笔」打开后，可以直接在预览上画：红/黄/蓝三色、橡皮、粗细可调，支持撤销。
`;

const PEN_COLORS = [
  { name: '红', value: '#e53935' },
  { name: '黄', value: '#f9a825' },
  { name: '蓝', value: '#1e88e5' },
];

// 公式速查。${1}/${2} 是空位标记 —— 插入后光标停在第一个空位。
// block: true 的用 $$ 包起来（矩阵、分段函数这类行内排版会很难看）。
const SNIPPETS = [
  { title: '分数·根式', items: [
    { label: 'a/b', t: '\\frac{${1}}{${2}}' },
    { label: '√', t: '\\sqrt{${1}}' },
    { label: 'ⁿ√', t: '\\sqrt[${1}]{${2}}' },
    { label: '|x|', t: '\\left|${1}\\right|' },
  ] },
  { title: '上下标', items: [
    { label: 'x²', t: 'x^{${1}}' },
    { label: 'xᵢ', t: 'x_{${1}}' },
    { label: 'xᵢⁿ', t: 'x_{${1}}^{${2}}' },
    { label: 'f′', t: 'f^{\\prime}(${1})' },
  ] },
  { title: '希腊字母', items: [
    { label: 'α', t: '\\alpha' }, { label: 'β', t: '\\beta' }, { label: 'γ', t: '\\gamma' },
    { label: 'θ', t: '\\theta' }, { label: 'π', t: '\\pi' }, { label: 'λ', t: '\\lambda' },
    { label: 'μ', t: '\\mu' }, { label: 'φ', t: '\\varphi' }, { label: 'ω', t: '\\omega' },
    { label: 'Δ', t: '\\Delta' }, { label: '∑', t: '\\sum' }, { label: '∞', t: '\\infty' },
  ] },
  { title: '运算·比较', items: [
    { label: '×', t: '\\times' }, { label: '÷', t: '\\div' }, { label: '±', t: '\\pm' },
    { label: '≤', t: '\\leq' }, { label: '≥', t: '\\geq' }, { label: '≠', t: '\\neq' },
    { label: '≈', t: '\\approx' }, { label: '≡', t: '\\equiv' }, { label: '∝', t: '\\propto' },
    { label: '°', t: '^{\\circ}' }, { label: '%', t: '\\%' },
  ] },
  { title: '求和·积分·极限', items: [
    { label: '∑', t: '\\sum_{${1}}^{${2}}' },
    { label: '∏', t: '\\prod_{${1}}^{${2}}' },
    { label: '∫', t: '\\int_{${1}}^{${2}}' },
    { label: 'lim', t: '\\lim_{${1} \\to ${2}}' },
    { label: 'd/dx', t: '\\frac{\\mathrm{d}${1}}{\\mathrm{d}${2}}' },
  ] },
  { title: '几何·集合', items: [
    { label: '∠', t: '\\angle ${1}' },
    { label: '△', t: '\\triangle ${1}' },
    { label: '∥', t: '\\parallel' }, { label: '⊥', t: '\\perp' },
    { label: '≌', t: '\\cong' }, { label: '∼', t: '\\sim' },
    { label: '∈', t: '\\in' }, { label: '⊂', t: '\\subset' },
    { label: '∪', t: '\\cup' }, { label: '∩', t: '\\cap' },
    { label: '∀', t: '\\forall' }, { label: '∃', t: '\\exists' },
  ] },
  { title: '向量·箭头', items: [
    { label: '→', t: '\\to' }, { label: '⇒', t: '\\Rightarrow' },
    { label: '⇔', t: '\\Leftrightarrow' }, { label: 'AB→', t: '\\overrightarrow{${1}}' },
    { label: 'a→', t: '\\vec{${1}}' }, { label: 'ŷ', t: '\\hat{${1}}' },
  ] },
  { title: '排版块', items: [
    { label: '分段函数', t: '\\begin{cases} ${1}, & x \\ge 0 \\\\ ${2}, & x < 0 \\end{cases}', block: true },
    { label: '矩阵', t: '\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}', block: true },
    { label: '方程组', t: '\\begin{cases} ${1} \\\\ ${2} \\end{cases}', block: true },
    { label: '对齐', t: '\\begin{aligned} ${1} &= ${2} \\\\ &= ${3} \\end{aligned}', block: true },
    { label: '分式展开', t: '\\underbrace{${1}}_{${2}}' },
    { label: '取整', t: '\\left\\lfloor ${1} \\right\\rfloor' },
  ] },
];

export default {
  name: 'Notes',
  components: { Modal, QuestionPicker },
  setup() {
    const route = useRoute();
    const router = useRouter();

    const notes = ref([]);
    const keyword = ref('');
    const cur = ref(null);                 // 当前打开的笔记（含 content / ink）
    const mode = ref('split');             // split | edit | preview
    // 左侧笔记树列可收起（跟「公式速查」一样）；状态存在本机，刷新后保持。
    const SIDE_KEY = 'shike.notes.side';
    const showSide = ref(localStorage.getItem(SIDE_KEY) !== '0');
    function toggleSide() {
      showSide.value = !showSide.value;
      try { localStorage.setItem(SIDE_KEY, showSide.value ? '1' : '0'); } catch (e) { /* 忽略 */ }
    }
    const showFormula = ref(true);
    // 「本文目录」（从渲染后的 DOM 扫出来的 h1..h6）放在**右**边，默认开。
    const OUTLINE_KEY = 'shike.notes.outline';
    const showOutline = ref(localStorage.getItem(OUTLINE_KEY) !== '0');
    function toggleOutline() {
      showOutline.value = !showOutline.value;
      try { localStorage.setItem(OUTLINE_KEY, showOutline.value ? '1' : '0'); } catch (e) { /* 忽略 */ }
    }    const saveState = ref('idle');         // idle | dirty | saving | saved | error

    const outline = ref([]);               // [{id, level, text}] 从渲染后的 DOM 扫出来
    const activeHeading = ref('');

    const editorEl = ref(null);
    const previewEl = ref(null);
    const previewWrapEl = ref(null);
    const canvasEl = ref(null);
    const fileEl = ref(null);

    // ---- 画笔状态 ----
    const inkOn = ref(false);
    const inkTool = ref('pen');            // pen | eraser
    const penColor = ref(PEN_COLORS[0].value);
    const penWidth = ref(3);
    const drawing = ref(false);
    let curStroke = null;
    let pendingSave = null;
    let saveTimer = null;
    let searchTimer = null;

    const content = computed(() => (cur.value ? cur.value.content : ''));
    const hasNote = computed(() => !!cur.value);
    const libState = ref('idle');           // idle | loading | ready | error
    const libError = ref('');               // 失败时记下是哪个文件没加载上
    const markdownReady = computed(() => libState.value === 'ready');

    // ---- 导出（Word / PDF）----
    const showExport = ref(false);
    const includeInk = ref(true);
    // 导给谁看：默认教师版（含答案）。选学生版时，正文里插的题答案
    // **根本不会被写进文件** —— 不是渲染时藏起来（见后端 note_questions.render）。
    const exportWithAnswer = ref(true);
    const exporting = ref('');             // '' | 'docx' | 'pdf'：正在导出的格式
    const caps = ref(null);                // 导出能力，见后端 /api/notes/export/caps

    // ---- 全屏（专注）模式：只留 Markdown 代码与预览 ----
    const zen = ref(false);

    function toggleZen() {
      zen.value = !zen.value;
    }

    /** 进入/退出全屏后布局变了，但窗口尺寸没变 —— resize 事件不会触发，
     *  所以笔画 canvas 必须手动重新量一次，否则笔画会错位。 */
    function refreshCanvasLater() {
      nextTick(() => { sizeCanvas(); redraw(); });
    }

    function onZenKey(e) {
      if (e.key === 'Escape' && zen.value) zen.value = false;
    }

    /** 先问一次后端能力：PDF 依赖本机 Word、公式渲染依赖 node + Office 的 XSLT。
     *  拿到之后才能把「为什么这个按钮是灰的」直接写在界面上。 */
    async function loadCaps() {
      try {
        caps.value = await notesApi.exportCaps();
      } catch (e) { /* 探测失败不影响其它功能，导出时还会再报一次 */ }
    }

    async function doExport(fmt) {
      if (!cur.value || exporting.value) return;
      // 先落盘再导：防抖还没发出去的改动如果不刷，导出的会是**改之前**的版本
      await flushSave();
      exporting.value = fmt;
      const base = (cur.value.title || '笔记').replace(/[\\/:*?"<>|]+/g, '_').slice(0, 60) || '笔记';
      const version = exportWithAnswer.value ? '' : '_学生版';
      const filename = `${base}${version}.${fmt}`;
      try {
        await notesApi.downloadExport(
          cur.value.id,
          { format: fmt, ink: includeInk.value, with_answer: exportWithAnswer.value },
          filename);
        ok(`已导出 ${fmt === 'pdf' ? 'PDF' : 'Word'}：${filename}`);
        showExport.value = false;
      } catch (e) {
        if (isNoteGone(e)) {
          showExport.value = false;
          await handleNoteGone('导出');
        } else {
          fail('导出失败：' + e.message);
        }
      } finally {
        exporting.value = '';
      }
    }

    /* ================= 目录树 =================
     *
     * 形状：体系（DSE 数学 / 国内初中…）→ 目录… → 笔记。
     * 几个刻意的地方：
     *   · **树一次拿全**（/api/note-folders/tree），再在本地摊成一维行来渲染。
     *     递归模板不好写也不好拖，摊平之后拖拽只要算行号。笔记量级不大，整棵拉下来没问题。
     *   · 每个目录里**先列子目录、再列笔记**。两串顺序互不干扰，拖动只在同类之间重排，
     *     不会出现「目录的 0/1/2 和笔记的 0/1/2 交错」这种没法解释的顺序。
     *   · 笔记是**叶子**：不能往里放东西。这样拖拽规则永远只有一条。
     *   · 展开状态记在 localStorage，刷新后不回到「全收起」。
     */
    const tree = ref({ roots: [], unfiled_root_id: null, total: 0 });
    const expanded = ref({});
    const selectedFolder = ref(null);        // 「新建」落在这里
    const editing = ref(null);               // {mode:'new', parentId, value} | {mode:'rename', id, value}
    const drag = ref(null);                  // {kind:'folder'|'note', id}
    const dropAt = ref(null);                // {id, zone}
    const menu = ref(null);                  // {items, x, y}
    const EXPAND_KEY = 'shike.notes.expanded';

    /** 目录 id -> 节点（含 parent_id）。树拿全了，前端自己就能算父子关系。 */
    const folderMap = computed(() => {
      const m = {};
      const walk = (node, parentId) => {
        m[node.id] = { ...node, parent_id: parentId };
        for (const c of node.children) walk(c, node.id);
      };
      for (const r of tree.value.roots) walk(r, null);
      return m;
    });

    /** 笔记 id -> 笔记（含所在目录），拖动时要用。 */
    const noteMap = computed(() => {
      const m = {};
      const walk = (node) => {
        for (const n of node.notes) m[n.id] = { ...n, folder_id: node.id };
        for (const c of node.children) walk(c);
      };
      for (const r of tree.value.roots) walk(r);
      return m;
    });

    /** 树 -> 一维行。「先子目录、再笔记」，展开的才输出。 */
    const rows = computed(() => {
      const out = [];
      const kids = (node, depth) => {
        for (const c of node.children) {
          out.push({ kind: 'folder', node: c, depth });
          if (expanded.value[c.id]) kids(c, depth + 1);
        }
        for (const n of node.notes) out.push({ kind: 'note', node: n, depth, folderId: node.id });
      };
      for (const root of tree.value.roots) {
        out.push({ kind: 'root', node: root, depth: 0 });
        if (expanded.value[root.id]) kids(root, 1);
      }
      return out;
    });

    const searching = computed(() => !!keyword.value.trim());

    async function loadTree() {
      try {
        tree.value = await noteFoldersApi.tree();
      } catch (e) {
        fail(e.message);
      }
    }

    function persistExpanded() {
      try { localStorage.setItem(EXPAND_KEY, JSON.stringify(expanded.value)); } catch (e) { /* 无痕模式 */ }
    }

    function toggleExpand(id) {
      expanded.value = { ...expanded.value, [id]: !expanded.value[id] };
      persistExpanded();
    }

    function expandIn(id, on = true) {
      expanded.value = { ...expanded.value, [id]: on };
      persistExpanded();
    }

    /** 把某篇笔记/某个目录的**各级祖先**都展开 —— 搜到一篇笔记点开后，
     *  树上得能看见它在哪，而不是停在一堆收起的目录外面。 */
    function expandTo(folderId) {
      const next = { ...expanded.value };
      let cur = folderId;
      const seen = new Set();
      while (cur && !seen.has(cur)) {
        seen.add(cur);
        next[cur] = true;
        cur = folderMap.value[cur] ? folderMap.value[cur].parent_id : null;
      }
      expanded.value = next;
      persistExpanded();
    }

    /** 首次进来：有内容的分组默认展开（空分组收着，免得一屏全是空目录）。 */
    function initExpanded() {
      let saved = null;
      try { saved = JSON.parse(localStorage.getItem(EXPAND_KEY) || 'null'); } catch (e) { saved = null; }
      if (saved && typeof saved === 'object') {
        expanded.value = saved;
        return;
      }
      const next = {};
      for (const r of tree.value.roots) next[r.id] = r.count > 0;
      expanded.value = next;
    }

    /* ---- 新建 / 重命名（分组、目录、笔记） / 删除 ---- */
    function startNewFolder(parentId) {
      expandIn(parentId, true);
      editing.value = { mode: 'new', parentId, value: '' };
    }
    /** 新建一级分组：输入行渲染在树的最上面（新分组也建在最前面，所见即所得）。 */
    function startNewGroup() {
      editing.value = { mode: 'newGroup', value: '' };
    }
    function startRename(folder) {
      // 一级分组现在就是笔记自己的东西（跟体系解绑了），改名不会影响别的页面，
      // 所以不再需要那句确认；「未归档」是系统兜底容器，菜单里不给改名。
      editing.value = {
        mode: 'rename',
        kind: folder.is_root ? 'root' : 'folder',
        id: folder.id,
        value: folder.name,
      };
    }
    /** 改笔记标题：原地变输入框。
     *  两件事都要做，否则「点了没反应」：① 目录收着的要先把祖先展开 —— 输入框在屏幕外
     *  ② 搜索结果里也能改名（那边的输入框单独渲染了一份）。 */
    function startRenameNote(note) {
      if (note.folder_id) expandTo(note.folder_id);
      editing.value = { mode: 'rename', kind: 'note', id: note.id, value: note.title || '' };
    }
    function cancelEdit() { editing.value = null; }

    async function commitEdit() {
      const ed = editing.value;
      if (!ed) return;
      editing.value = null;
      const name = (ed.value || '').trim();
      if (!name) return;                       // 空名字就当放弃：不建「未命名目录」，也不改名
      try {
        if (ed.mode === 'new') {
          await noteFoldersApi.create({ parent_id: ed.parentId, name });
          await loadTree();
          expandIn(ed.parentId, true);
        } else if (ed.mode === 'newGroup') {
          await noteFoldersApi.create({ parent_id: null, name });   // 不传 parent_id = 建一级分组
          await loadTree();
        } else if (ed.kind === 'note') {
          // 只提交 title：正文/板书还在自动保存的队列里，这次请求不能顺手动它们
          const r = await notesApi.update(ed.id, { title: name });
          // 改的正好是打开着的那篇 → 标题框与右边列表要立刻跟着变，否则屏幕上还是旧名字
          if (cur.value && cur.value.id === ed.id) cur.value.title = r.title;
          await loadList();
          await loadTree();
        } else {
          const r = await noteFoldersApi.update(ed.id, { name });
          await loadTree();
          if (ed.kind === 'root') ok(`已改名为「${r.name}」`);
        }
      } catch (e) {
        fail(e.message);
      }
    }

    async function removeFolder(folder) {
      // 说清楚「东西去哪儿」再问 —— 删目录绝不删内容，但也不能让用户以为东西没了
      const inside = folder.count
        ? `里面的 ${folder.count} 篇笔记会${folder.is_root ? '移到「未归档」' : '移到上一级'}（不会删掉）。`
        : '这个是空的。';
      const what = folder.is_root ? '分组' : '目录';
      if (!window.confirm(`删除${what}「${folder.name}」？${inside}`)) return;
      try {
        const r = await noteFoldersApi.remove(folder.id);
        const moved = [r.moved_folders ? `${r.moved_folders} 个子目录` : '',
                       r.moved_notes ? `${r.moved_notes} 篇笔记` : ''].filter(Boolean).join('、');
        ok(moved ? `${what}已删除；${moved}移到了「${r.to}」` : `${what}已删除`);
        await loadTree();
      } catch (e) {
        fail(e.message);
      }
    }

    /* ---- 拖动 ---- */
    const isSelfOrDescendant = (folderId, candidateId) => {
      // 把目录拖进自己的后代 → 子树会从根上掉下来，界面上整段消失。服务端也会拒，
      // 这里判一次只是为了一开始就不给「可以放」的提示。
      let cur = candidateId;
      const seen = new Set();
      while (cur && !seen.has(cur)) {
        if (cur === folderId) return true;
        seen.add(cur);
        const n = folderMap.value[cur];
        cur = n ? n.parent_id : null;
      }
      return false;
    };

    function zoneOf(row, e) {
      const r = e.currentTarget.getBoundingClientRect();
      const y = e.clientY - r.top;
      // 笔记没有「里面」，所以它只有前后两段
      if (row.kind === 'note' || r.height < 14) return y < r.height / 2 ? 'before' : 'after';
      if (y < r.height * 0.28) return 'before';
      if (y > r.height * 0.72) return 'after';
      return 'in';
    }

    /** 一级分组（不含「未归档」）：拖动只在它们之间排序，未归档固定最后。 */
    const groupIds = () => tree.value.roots.filter((r) => !r.is_unfiled).map((r) => r.id);
    const isUnfiled = (id) => id === tree.value.unfiled_root_id;

    function canDrop(d, row, zone) {
      if (!d || d.id === row.node.id) return false;
      if (row.kind === 'root') {
        if (isUnfiled(row.node.id)) {
          // 「未归档」不接受前后插入，只能放进去
          return zone === 'in' && (d.kind === 'note' || !isSelfOrDescendant(d.id, row.node.id));
        }
        if (d.kind === 'root') return zone !== 'in';        // 分组之间只能排前后，不能嵌套
        return zone === 'in' && (d.kind === 'note' || !isSelfOrDescendant(d.id, row.node.id));
      }
      if (row.kind === 'note') {
        return d.kind === 'note' && zone !== 'in';       // 笔记是叶子，不能往里放
      }
      if (zone === 'in') return d.kind === 'note' || !isSelfOrDescendant(d.id, row.node.id);
      if (d.kind === 'root') return false;               // 分组不能插到目录之间
      return d.kind === 'folder';                        // 前后 = 同级排序，两侧必须同类
    }

    function onDragStart(row, e) {
      if (row.kind === 'root' && isUnfiled(row.node.id)) { e.preventDefault(); return; }  // 未归档固定最后
      drag.value = { kind: row.kind, id: row.node.id };
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(row.node.id));  // 某些浏览器没它不触发 drop
    }

    function onDragOver(row, e) {
      if (!canDrop(drag.value, row, zoneOf(row, e))) return;
      e.preventDefault();                                  // 不 preventDefault 就不会有 drop
      dropAt.value = { id: row.node.id, zone: zoneOf(row, e) };
    }

    function onDragEnd() { drag.value = null; dropAt.value = null; }

    /** 同级列表里「去掉被拖的那个」之后，目标在第几位 —— 和服务端的算法保持一致，
     *  否则同层里往下拖会差一位。 */
    function positionIn(ids, targetId, zone, dragId) {
      const rest = ids.filter((x) => x !== dragId);
      const i = rest.indexOf(targetId);
      return Math.max(0, i + (zone === 'after' ? 1 : 0));
    }

    async function onDrop(row, e) {
      e.preventDefault();
      const d = drag.value;
      const zone = dropAt.value && dropAt.value.id === row.node.id ? dropAt.value.zone : null;
      drag.value = null;
      dropAt.value = null;
      if (!d || !zone) return;
      try {
        if (zone === 'in') {
          // 9999 = 「放最后」，服务端会夹到合法范围
          if (d.kind === 'folder') await noteFoldersApi.update(d.id, { parent_id: row.node.id, position: 9999 });
          else await notesApi.update(d.id, { folder_id: row.node.id, position: 9999 });
          expandIn(row.node.id, true);
          if (cur.value && cur.value.id === d.id) { /* 打开着的笔记换了目录，正文不用重载 */ }
        } else if (d.kind === 'root') {
          // 拖动一级分组 = 自己在分组之间换位置
          await noteFoldersApi.update(d.id, {
            position: positionIn(groupIds(), row.node.id, zone, d.id),
          });
        } else if (d.kind === 'folder') {
          const parentId = folderMap.value[row.node.id].parent_id;
          const sibs = parentId ? folderMap.value[parentId].children.map((c) => c.id) : tree.value.roots.map((r) => r.id);
          await noteFoldersApi.update(d.id, {
            parent_id: parentId, position: positionIn(sibs, row.node.id, zone, d.id),
          });
        } else {
          const ids = (folderMap.value[row.folderId].notes || []).map((n) => n.id);
          await notesApi.update(d.id, {
            folder_id: row.folderId, position: positionIn(ids, row.node.id, zone, d.id),
          });
        }
        await loadTree();
      } catch (err) {
        fail(err.message);
        await loadTree();
      }
    }

    /* ---- 行的「⋯」菜单与上下移 ---- */
    function openMenu(row, e) {
      e.stopPropagation();
      const items = [];
      if (row.kind === 'folder') {
        items.push({ label: '新建子目录', run: () => startNewFolder(row.node.id) });
        items.push({ label: '重命名', run: () => startRename(row.node) });
        items.push({ label: '上移', run: () => nudge(row, -1) });
        items.push({ label: '下移', run: () => nudge(row, 1) });
        items.push({ label: '删除', danger: true, run: () => removeFolder(row.node) });
      } else if (row.kind === 'note') {
        items.push({ label: '打开', run: () => open(row.node.id) });
        items.push({ label: '重命名', run: () => startRenameNote(row.node) });
        items.push({ label: row.node.pinned ? '取消置顶' : '置顶', run: () => togglePin(row.node) });
        items.push({ label: '上移', run: () => nudge(row, -1) });
        items.push({ label: '下移', run: () => nudge(row, 1) });
        items.push({ label: '删除', danger: true, run: () => removeNote(row.node) });
      } else {
        // 一级分组：能改名 / 排序 / 删（内容会移到「未归档」）。
        // 「未归档」是系统兜底容器，只能往里放东西。
        if (!row.node.is_unfiled) {
          items.push({ label: '重命名', run: () => startRename(row.node) });
        }
        items.push({ label: '新建笔记', run: () => createNote(row.node.id) });
        items.push({ label: '新建子目录', run: () => startNewFolder(row.node.id) });
        if (!row.node.is_unfiled) {
          items.push({ label: '上移', run: () => nudge(row, -1) });
          items.push({ label: '下移', run: () => nudge(row, 1) });
          items.push({ label: '删除分组', danger: true, run: () => removeFolder(row.node) });
        }
      }
      const box = e.currentTarget.getBoundingClientRect();
      // 用 fixed 定位：树那一列是 overflow 滚动的，绝对定位的菜单会被裁掉。
      // 位置要**夹在视口里**：行靠近窗口底部时菜单会掉到屏幕外，最后两项就点不到了
      // （窗口小的时候尤其明显 —— 实测 300×500 的窄窗口里必现）。
      // 放不下就翻到行的上方。
      const W = 152;
      // 高度按项数算出来，以后加项不用回来改这个数字
      // （一项 ≈ 12.5px 字号的行高 + 上下各 5px 内边距 ≈ 28px，外面还有 4px 内边距）
      const H = 8 + items.length * 28;
      const vw = window.innerWidth, vh = window.innerHeight;
      const x = Math.max(8, Math.min(box.right - W, vw - W - 8));
      const below = box.bottom + 4;
      const y = below + H <= vh ? below : Math.max(8, Math.min(box.top - H - 4, vh - H - 8));
      menu.value = { items, x, y };
    }

    function closeMenu() { menu.value = null; }

    async function runMenu(it) {
      closeMenu();
      try { await it.run(); } catch (e) { fail(e.message); }
    }

    /** 上移/下移：拖拽的键盘/触控板替代品（拖不准时还有路）。 */
    async function nudge(row, delta) {
      try {
        if (row.kind === 'root') {
          // 一级分组之间换位置（「未归档」不参与，它固定最后，菜单里也不给这两项）
          const sibs = groupIds();
          const i = sibs.indexOf(row.node.id);
          const to = i + delta;
          if (i < 0 || to < 0 || to >= sibs.length) return;
          await noteFoldersApi.update(row.node.id, { position: to });
        } else if (row.kind === 'folder') {
          const parentId = folderMap.value[row.node.id].parent_id;
          const sibs = parentId ? folderMap.value[parentId].children.map((c) => c.id) : groupIds();
          const i = sibs.indexOf(row.node.id);
          const to = i + delta;
          if (i < 0 || to < 0 || to >= sibs.length) return;
          await noteFoldersApi.update(row.node.id, { parent_id: parentId, position: to });
        } else {
          const sibs = (folderMap.value[row.folderId].notes || []).map((n) => n.id);
          const i = sibs.indexOf(row.node.id);
          const to = i + delta;
          if (i < 0 || to < 0 || to >= sibs.length) return;
          await notesApi.update(row.node.id, { folder_id: row.folderId, position: to });
        }
        await loadTree();
      } catch (e) {
        fail(e.message);
      }
    }

    function selectFolder(id) {
      selectedFolder.value = id;
      expandIn(id, true);
    }

    /* ================= 备份 / 迁移 ================= */
    const showTransfer = ref(false);
    const transferFile = ref(null);
    const transferOver = ref(false);
    const transferReport = ref(null);
    const transferring = ref('');            // '' | 'try' | 'go' | 'export'
    const transferEl = ref(null);

    function pickTransferFile() { transferEl.value && transferEl.value.click(); }

    function onTransferPicked(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';
      transferReport.value = null;
      if (!f) return;
      // 选定就清掉上次的报告：不然旧数字会和这次的混在一起看
      if (!/\.zip$/i.test(f.name)) {
        transferFile.value = null;
        return warn('请选择「拾课笔记备份_….zip」这样的备份包。');
      }
      transferFile.value = f;
    }

    /** 导出全量笔记（备份包）。
     *
     * 名字必须跟「导出单篇笔记」那个区分开：两个都叫 doExport 时，
     * 同一个作用域里**后声明的那个会赢** —— 「导出 Word」按钮实际上会去下备份包，
     * 而且不报错、看不出异常（浏览器里实测踩到过）。
     */
    async function exportBackup() {
      const t = new Date();
      const p = (n) => String(n).padStart(2, '0');
      const stamp = `${t.getFullYear()}${p(t.getMonth() + 1)}${p(t.getDate())}_${p(t.getHours())}${p(t.getMinutes())}`;
      transferring.value = 'export';
      try {
        await notesBackupApi.export(`拾课笔记备份_${stamp}.zip`);
        ok('备份已导出');
      } catch (e) {
        fail(e.message);
      } finally {
        transferring.value = '';
      }
    }

    /** 导入。dry=true 只试算 —— 「会不会把我的东西盖掉」这种事，最好能先问一遍。 */
    async function doImport(dry) {
      const f = transferFile.value;
      if (!f) return warn('先选一个备份包（.zip）');
      transferring.value = dry ? 'try' : 'go';
      transferReport.value = null;
      try {
        const fd = new FormData();
        fd.append('file', f, f.name);
        const r = await notesBackupApi.import(fd, { overwrite: transferOver.value, dryRun: dry });
        transferReport.value = r;
        if (!dry) {
          await loadTree();
          await loadList();
          ok(`导入完成：新建 ${r.notes_created} 篇，跳过 ${r.notes_skipped} 篇`);
        }
      } catch (e) {
        fail(e.message);
      } finally {
        transferring.value = '';
      }
    }

    function openTransfer() {
      transferReport.value = null;
      transferFile.value = null;
      transferOver.value = false;
      showTransfer.value = true;
    }

    /* ================= 把现成文档导入成笔记 ================= */
    const docEl = ref(null);
    const importingDoc = ref(false);

    function pickDoc() { docEl.value && docEl.value.click(); }

    /** 导入 Word / PDF / HTML / 文本。**转出来的是 Markdown + 一份原件**：
     *  Markdown 是能读能改能喂 AI 的那部分，原件是一定没丢的那部分。
     *  失真与警告会写在笔记开头（后端写的），所以这里只提示一句、然后直接把笔记打开
     *  —— 用户第一眼就该看到结果长什么样，而不是先看一堆说明。
     */
    async function onDocPicked(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';
      if (!f) return;
      importingDoc.value = true;
      try {
        const fd = new FormData();
        fd.append('file', f, f.name);
        const r = await notesApi.importDoc(fd, selectedFolder.value || tree.value.unfiled_root_id);
        await loadTree();
        if (r.folder_id) expandTo(r.folder_id);
        await open(r.id);
        const extra = (r.warnings && r.warnings.length) ? `（有 ${r.warnings.length} 条提示，见正文开头）` : '';
        ok(`已导入「${r.title}」（${r.kind_cn}，${r.chars} 字）${extra}`);
      } catch (err) {
        fail(err.message);
      } finally {
        importingDoc.value = false;
      }
    }

    /** 点一行：笔记就打开，目录就「选中 + 展收」。
     *  选中目录是为了「新建笔记/新建目录落在哪」——没选就落「未归档」。 */
    function onRowClick(row) {
      if (row.kind === 'note') { open(row.node.id); return; }
      selectedFolder.value = row.node.id;
      toggleExpand(row.node.id);
    }

    /** 内联输入框出现时自动聚焦（用函数 ref，因为它在 v-for 里，
     *  写成字符串 ref 会变成数组）。 */
    function focusEdit(el) {
      if (el) nextTick(() => el.focus());
    }

    /* ================= 列表 ================= */
    async function loadList() {
      try {
        const r = await notesApi.list(keyword.value.trim() ? { keyword: keyword.value.trim() } : {});
        notes.value = r.items;
      } catch (e) {
        fail(e.message);
      }
    }
    function onSearch() {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(async () => {
        await loadList();
        if (!searching.value) await loadTree();     // 清空搜索条件时把树拉回最新
      }, 300);
    }

    /** 新建笔记：落在「选中的目录」，没选就落「未归档」——永远不会没归属。 */
    async function createNote(folderId) {
      await flushSave();
      const target = folderId ?? selectedFolder.value ?? tree.value.unfiled_root_id;
      try {
        const r = await notesApi.create({ title: '未命名笔记', content: SAMPLE, folder_id: target });
        await loadTree();
        if (target) expandTo(target);
        await open(r.id);
      } catch (e) {
        fail(e.message);
      }
    }

    async function removeNote(n) {
      if (!window.confirm(`删除笔记「${n.title}」？正文里插入的配图也会一并删掉。`)) return;
      try {
        const r = await notesApi.remove(n.id);
        if (cur.value && cur.value.id === n.id) {
          cur.value = null;
          router.replace('/notes');
        }
        await loadList();
        await loadTree();
        ok(r.images_removed ? `已删除（顺带清掉 ${r.images_removed} 张配图）` : '已删除');
      } catch (e) {
        fail(e.message);
      }
    }

    async function togglePin(n) {
      try {
        await notesApi.update(n.id, { pinned: !n.pinned });
        await loadList();
        await loadTree();
      } catch (e) {
        fail(e.message);
      }
    }

    /* ================= 打开 / 保存 ================= */
    async function open(id) {
      await flushSave();                   // 切走之前先把没落盘的写掉
      try {
        cur.value = await notesApi.get(id);
        outline.value = [];
        activeHeading.value = '';
        // 在树上把这篇所在的分支展开：从搜索或从网址直接打开时，
        // 不然左侧只会停在一堆收起的目录外面，看不出它在哪
        if (cur.value.folder_id) { expandTo(cur.value.folder_id); selectedFolder.value = cur.value.folder_id; }
        await nextTick();
        render();
        if (route.params.id !== id) router.replace('/notes/' + id);
      } catch (e) {
        cur.value = null;
        if (isNoteGone(e)) {
          // 网址上那篇已经不在了：把地址也换回列表。
          // 不换的话地址栏一直停在那个死 id 上，刷新还是它，会以为功能坏了（踩过）。
          fail('这篇笔记已不存在（可能被删除了），已回到列表。');
          await loadTree();
          if (route.params.id) router.replace('/notes');
        } else {
          fail(e.message);
        }
      }
    }

    /** 笔记在服务端已经不存在了（被删了，或者本地还留着一条过期的）。
     *  这时必须清掉当前对象并重拉列表 —— 否则用户会一直对着一个「幽灵笔记」操作，
     *  每按一次自动保存或导出都只收到一句 404，看着就像功能坏了。 */
    async function handleNoteGone(action) {
      fail(`这篇笔记在服务端已不存在（可能已被删除），${action}没有完成。列表已刷新，请重新打开一篇。`);
      cur.value = null;
      await loadList();
      await loadTree();
      if (route.params.id) router.replace('/notes');
    }

    function isNoteGone(e) {
      return !!e && (e.status === 404 || /不存在/.test(e.message || ''));
    }

    /** 攒着改动，防抖提交 —— 打字过程中不该每敲一下发一个请求。 */
    function scheduleSave(patch) {
      if (!cur.value || !cur.value.id) return;
      pendingSave = { ...(pendingSave || {}), ...patch };
      saveState.value = 'dirty';
      clearTimeout(saveTimer);
      saveTimer = setTimeout(flushSave, 800);
    }

    async function flushSave() {
      clearTimeout(saveTimer);
      const patch = pendingSave;
      pendingSave = null;
      if (!patch || !cur.value || !cur.value.id) return;
      saveState.value = 'saving';
      try {
        const brief = await notesApi.update(cur.value.id, patch);
        saveState.value = 'saved';
        // 正文里删掉的配图，服务端会把磁盘文件也清掉（引用计数归零才清）。
        // 删了东西必须说一声 —— 不能说「你自己把那段字删了，所以文件没了活该」。
        if (brief.images_removed) {
          ok(`已清掉 ${brief.images_removed} 张不再引用的配图`);
        }
        // 就地更新列表那一条，不用整表重拉
        const i = notes.value.findIndex(n => n.id === brief.id);
        if (i >= 0) notes.value[i] = brief;
      } catch (e) {
        saveState.value = 'error';
        if (isNoteGone(e)) await handleNoteGone('保存');
        else fail('保存失败：' + e.message);
      }
    }

    function onEdit() {
      scheduleSave({ content: cur.value.content });
      if (mode.value !== 'edit') render();
    }
    function onTitleInput() {
      scheduleSave({ title: cur.value.title });
    }

    /* ================= 渲染 ================= */
    function render() {
      const el = previewEl.value;
      if (!el || !cur.value) return;
      const src = cur.value.content || '';

      if (!window.marked) {                 // CDN 没加载上：退成纯文本，别白屏
        el.textContent = src;               // 注意这里给**原文**：没渲染时不该连内容都被改写
        outline.value = [];
        return;
      }
      renderRichEl(el, src);   // Markdown + 公式的渲染管线统一在 src/mathRender.js

      buildOutline();
      nextTick(() => { sizeCanvas(); redraw(); });
    }

    /** 目录直接读渲染结果 —— 保证「目录里有的」和「正文里有的」永远一致。 */
    function buildOutline() {
      const el = previewEl.value;
      const hs = el ? Array.from(el.querySelectorAll('h1,h2,h3,h4,h5,h6')) : [];
      outline.value = hs.map((h, i) => {
        if (!h.id) h.id = 'note-h-' + i;
        return { id: h.id, level: Number(h.tagName.slice(1)), text: h.textContent.trim() };
      });
    }

    function scrollToHeading(id) {
      const wrap = previewWrapEl.value;
      const el = previewEl.value;
      if (!wrap || !el) return;
      const h = el.querySelector('#' + (window.CSS && CSS.escape ? CSS.escape(id) : id));
      if (h) wrap.scrollTop = Math.max(0, h.offsetTop - 12);
      activeHeading.value = id;
    }

    /** 滚动时高亮当前所在的小节。 */
    function onPreviewScroll() {
      const wrap = previewWrapEl.value;
      const el = previewEl.value;
      if (!wrap || !el) return;
      const top = wrap.scrollTop + 24;
      let active = '';
      for (const h of el.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
        if (h.offsetTop <= top) active = h.id;
        else break;
      }
      activeHeading.value = active;
    }

    /* ================= 插入：公式 / 图片 ================= */
    /** 在光标处插入文本；可选给出插入后要选中的区间（用来把光标停在公式空位上）。 */
    function insertAtCursor(text, selFrom, selTo) {
      if (!cur.value) return;
      const ta = editorEl.value;
      if (mode.value === 'preview') mode.value = 'split';   // 预览模式下没有光标，先切回可编辑
      const c = cur.value.content || '';
      const start = ta && ta.selectionStart != null ? ta.selectionStart : c.length;
      const end = ta && ta.selectionEnd != null ? ta.selectionEnd : start;
      cur.value.content = c.slice(0, start) + text + c.slice(end);
      onEdit();
      nextTick(() => {
        const t = editorEl.value;
        if (!t) return;
        t.focus();
        const a = start + (selFrom == null ? text.length : selFrom);
        const b = start + (selTo == null ? text.length : selTo);
        t.setSelectionRange(a, b);
      });
    }

    function insertSnippet(s) {
      const first = /\$\{(\d)\}/.exec(s.t);
      const before = first ? s.t.slice(0, first.index).replace(/\$\{\d\}/g, '').length : s.t.length;
      const clean = s.t.replace(/\$\{\d\}/g, '');
      if (s.block) {
        insertAtCursor(`\n$$\n${clean}\n$$\n`, before + 5, before + 5);
      } else {
        insertAtCursor(`$${clean}$`, before + 1, before + 1);
      }
    }

    /* ================= 插入题库里的题 ================= */
    // 插进正文的是一个**可识别的块**（<!--q:qid--> … <!--qa--> … <!--qe-->），
    // 不是一段死文本：导出时按版本处理，「学生版」会把答案整段删掉。
    // 图片由后端连图一起存进这篇笔记的资源目录（nb_ 前缀），
    // 所以之后删掉题库里那道题，讲义里的图也不会消失。
    const showPickQ = ref(false);
    const pickingQ = ref(false);

    /** 「练习 N：」的序号 = 正文里已有的题块数 + 1，老师能看出插到第几道。 */
    const pickNextIndex = computed(() => {
      const c = (cur.value && cur.value.content) || '';
      return (c.match(/<!--q:/g) || []).length + 1;
    });

    async function insertQuestions(ids) {
      if (pickingQ.value) return;
      pickingQ.value = true;
      try {
        const blocks = [];
        let n = pickNextIndex.value;
        for (const qid of ids) {
          const r = await notesApi.questionBlock(qid, n);
          blocks.push(r.block);
          n += 1;
        }
        // 前后各留一个空行，免得贴着上一段或下一段
        insertAtCursor(`\n\n${blocks.join('\n\n')}\n\n`);
        showPickQ.value = false;
        const hi = blocks.some((b) => b.includes('<!--qa-->'));
        ok(hi ? `已插入 ${ids.length} 道题（含答案；导出学生版时会去掉）`
              : `已插入 ${ids.length} 道题`);
      } catch (e) {
        fail('插题失败：' + e.message);
      } finally {
        pickingQ.value = false;
      }
    }

    function pickImage() {
      if (fileEl.value) fileEl.value.click();
    }    async function onImageFile(e) {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';                  // 清掉，否则同一个文件选第二次不触发
      if (f) await uploadAndInsert(f);
    }
    /** 粘贴板里有图片就直接上传插入 —— 截图后 Ctrl+V 是最顺的路径。 */
    async function onPaste(e) {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const it of items) {
        if (it.type && it.type.startsWith('image/')) {
          e.preventDefault();
          const f = it.getAsFile();
          if (f) await uploadAndInsert(f);
          return;
        }
      }
    }
    async function uploadAndInsert(file) {
      const fd = new FormData();
      fd.append('file', file);
      try {
        const r = await notesApi.uploadImage(fd);
        insertAtCursor(`\n![图](${r.url})\n`);
        ok('图片已插入');
      } catch (e) {
        fail(e.message);
      }
    }

    /* ================= 画笔 ================= */
    function toggleInk() {
      inkOn.value = !inkOn.value;
      if (inkOn.value) {
        if (mode.value === 'edit') mode.value = 'split';
        nextTick(() => { sizeCanvas(); redraw(); });
      }
    }

    /** canvas 的 CSS 尺寸跟着预览内容走，实际像素按 dpr 放大（否则高分屏上发虚）。 */
    function sizeCanvas() {
      const c = canvasEl.value;
      const el = previewEl.value;
      if (!c || !el) return;
      const w = el.clientWidth;
      const h = el.scrollHeight;
      const dpr = window.devicePixelRatio || 1;
      c.style.width = w + 'px';
      c.style.height = h + 'px';
      c.width = Math.max(1, Math.round(w * dpr));
      c.height = Math.max(1, Math.round(h * dpr));
      const ctx = c.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function normPoint(e) {
      // 归一化到 0~1：这样换窗口大小、改字号，笔画都不会跑位
      const c = canvasEl.value;
      const r = c.getBoundingClientRect();
      return [
        Math.min(1, Math.max(0, (e.clientX - r.left) / Math.max(1, r.width))),
        Math.min(1, Math.max(0, (e.clientY - r.top) / Math.max(1, r.height))),
      ];
    }

    function drawStroke(ctx, s, r) {
      const pts = s.points || [];
      if (pts.length < 2) return;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.beginPath();
      ctx.moveTo(pts[0][0] * r.width, pts[0][1] * r.height);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0] * r.width, pts[i][1] * r.height);
      ctx.stroke();
    }

    function redraw() {
      const c = canvasEl.value;
      if (!c || !cur.value) return;
      const ctx = c.getContext('2d');
      const r = c.getBoundingClientRect();
      ctx.clearRect(0, 0, r.width, r.height);
      for (const s of cur.value.ink || []) drawStroke(ctx, s, r);
      if (curStroke) drawStroke(ctx, curStroke, r);
    }

    function inkDown(e) {
      if (!inkOn.value || !cur.value) return;
      const c = canvasEl.value;
      if (c.setPointerCapture) c.setPointerCapture(e.pointerId);
      drawing.value = true;
      const p = normPoint(e);
      if (inkTool.value === 'eraser') {
        eraseAt(p);
      } else {
        curStroke = { color: penColor.value, width: penWidth.value, points: [p] };
      }
    }

    function inkMove(e) {
      if (!drawing.value || !cur.value) return;
      const p = normPoint(e);
      if (inkTool.value === 'eraser') {
        eraseAt(p);
      } else if (curStroke) {
        curStroke.points.push(p);
        redraw();
      }
    }

    function inkUp(e) {
      if (!drawing.value) return;
      drawing.value = false;
      if (inkTool.value !== 'eraser' && curStroke && curStroke.points.length > 1) {
        cur.value.ink = [...(cur.value.ink || []), curStroke];
        scheduleSave({ ink: cur.value.ink });
      }
      curStroke = null;
      redraw();
      if (e && e.pointerId != null && canvasEl.value && canvasEl.value.releasePointerCapture) {
        try { canvasEl.value.releasePointerCapture(e.pointerId); } catch (err) { /* 已释放 */ }
      }
    }

    /** 橡皮是「整条擦」：命中任一采样点就把这条线删掉。
     *  做像素级擦除要么引入遮罩、要么把线切开，复杂度不值当，效果也差不多。 */
    function eraseAt(p) {
      const c = canvasEl.value;
      if (!c) return;
      const r = c.getBoundingClientRect();
      const px = p[0] * r.width, py = p[1] * r.height;
      const rad = Math.max(8, penWidth.value * 3);
      const before = (cur.value.ink || []).length;
      cur.value.ink = (cur.value.ink || []).filter(s =>
        !(s.points || []).some(([x, y]) => {
          const dx = x * r.width - px, dy = y * r.height - py;
          return dx * dx + dy * dy <= rad * rad;
        }));
      if (cur.value.ink.length !== before) {
        redraw();
        scheduleSave({ ink: cur.value.ink });
      }
    }

    function undoInk() {
      if (!cur.value || !cur.value.ink || !cur.value.ink.length) return;
      cur.value.ink = cur.value.ink.slice(0, -1);
      redraw();
      scheduleSave({ ink: cur.value.ink });
    }

    function clearInk() {
      if (!cur.value || !cur.value.ink || !cur.value.ink.length) return;
      if (!window.confirm('清空这页的全部笔画？')) return;
      cur.value.ink = [];
      redraw();
      scheduleSave({ ink: cur.value.ink });
      warn('笔画已清空');
    }

    /* ================= 生命周期 ================= */
    let ro = null;
    let zenMemo = null;          // 进全屏前的两侧栏状态（退出时恢复）

    /** 加载 Markdown / 公式库。失败必须能重试：ensureLibs() 的 Promise 是个模块级缓存，
     *  一旦 reject 就一直 reject —— 不 resetLibs() 的话，页面内切来切去永远好不了，
     *  只能整页刷新（用户看到的就是「公式怎么都不渲染」）。 */
    function initLibs() {
      libState.value = 'loading';
      libError.value = '';
      ensureLibs()
        .then(() => { libState.value = 'ready'; if (cur.value) render(); })
        .catch((e) => {
          libState.value = 'error';
          libError.value = e && e.message ? e.message : '加载失败';
          resetLibs();                   // 允许重试（下次点「重试」真的会再去拉）
        });
    }

    function onResize() {
      sizeCanvas();
      redraw();
    }

    onMounted(async () => {
      // 渲染依赖这几个库，所以先加载、加载完再渲染一次：在这之前会退成纯文本，
      // 不会先闪一屏“没渲染的样子”然后才变。
      initLibs();

      await loadTree();
      initExpanded();
      await loadList();
      if (route.params.id) await open(route.params.id);
      // 导出能力探测和主流程无关，不 await，避免拖慢首屏
      loadCaps();
      window.addEventListener('resize', onResize);
      window.addEventListener('keydown', onZenKey);
      // 点别处把「⋯」菜单收起来。挂在 document 上而不是用 @click，
      // 因为菜单本身是 fixed、在卡片外面
      document.addEventListener('click', closeMenu);
    });

    watch(zen, (v) => {
      // body 上的类是给全局 CSS 用的（要藏掉 App 里的左侧导航）
      document.body.classList.toggle('notes-zen', v);
      // 进全屏时**先收起**两侧栏（专注），但工具栏的开关仍然有效 ——
      // 想在全屏下继续用目录或公式速查，点一下就放回来；退出时恢复进全屏前的样子。
      if (v) {
        zenMemo = { side: showSide.value, formula: showFormula.value, outline: showOutline.value };
        showSide.value = false;
        showFormula.value = false;
        showOutline.value = false;
      } else if (zenMemo) {
        showSide.value = zenMemo.side;
        showFormula.value = zenMemo.formula;
        showOutline.value = zenMemo.outline;
        zenMemo = null;
      }
      refreshCanvasLater();
    });

    // 预览元素是随「是否打开笔记 / 模式切换」出现或消失的，所以观察器要跟着它走 ——
    // 只在 mounted 挂一次是不够的：那时可能一篇笔记都没打开，previewEl 还是 null，
    // 就永远挂不上了（踩过：窗口缩放、加字后 canvas 尺寸不跟着变）。
    watch(previewEl, (el) => {
      if (ro) { ro.disconnect(); ro = null; }
      if (el && window.ResizeObserver) {
        ro = new ResizeObserver(() => onResize());
        ro.observe(el);
      }
      if (el) nextTick(() => { sizeCanvas(); redraw(); });
    });

    onBeforeUnmount(() => {
      flushSave();
      if (ro) ro.disconnect();
      window.removeEventListener('resize', onResize);
      window.removeEventListener('keydown', onZenKey);
      document.removeEventListener('click', closeMenu);
      // 必须摘掉：否则离开笔记页后整个工作台会一直少一个侧边栏
      document.body.classList.remove('notes-zen');
    });

    watch(() => route.params.id, (id) => {
      if (id && (!cur.value || cur.value.id !== id)) open(id);
    });

    return {
      notes, keyword, cur, hasNote, mode, showFormula, saveState, markdownReady, libState,
      outline, activeHeading, editorEl, previewEl, previewWrapEl, canvasEl, fileEl,
      inkOn, inkTool, penColor, penWidth, colors: PEN_COLORS, snippets: SNIPPETS,
      loadList, onSearch, createNote, removeNote, togglePin, open,
      // 目录树
      tree, expanded, rows, searching, selectedFolder, editing, drag, dropAt, menu,
      noteFoldersApi, toggleExpand, selectFolder, startNewFolder, startNewGroup, startRename,
      startRenameNote, cancelEdit,
      commitEdit, removeFolder, openMenu, closeMenu, runMenu, nudge, onRowClick, focusEdit,
      // 备份 / 迁移
      showTransfer, transferFile, transferOver, transferReport, transferring, transferEl,
      openTransfer, pickTransferFile, onTransferPicked, exportBackup, doImport,
      // 文档导入
      docEl, importingDoc, pickDoc, onDocPicked,
      onDragStart, onDragOver, onDragEnd, onDrop,
      onEdit, onTitleInput, render, scrollToHeading, onPreviewScroll,
      insertSnippet, pickImage, onImageFile, onPaste,
      showPickQ, pickingQ, pickNextIndex, insertQuestions,
      toggleInk, inkDown, inkMove, inkUp, undoInk, clearInk,
      showExport, includeInk, exportWithAnswer, exporting, caps, doExport,
      zen, toggleZen, showSide, toggleSide, libError, initLibs,
      showOutline, toggleOutline,
      saveNow: flushSave,
    };
  },
  template: `
  <div>
    <div class="page-head" v-if="!zen">
      <h1>笔记</h1>
      <span class="sub">Markdown 正文 · 公式实时渲染 · 插图 · 板书笔画</span>
    </div>

    <div class="notes-grid" :class="{ zen, 'with-side': showSide,
                                    'with-right': hasNote && (showOutline || showFormula) }">
      <!-- ============ 左：笔记列表（目录树） ============ -->
      <div class="notes-side" v-if="showSide">
        <div class="card nb-card" @click="closeMenu">
          <!-- 工具栏：新建落在「选中的目录」；搜索一开就切成平铺结果 -->
          <div class="nb-tools">
            <button class="btn primary sm" @click="createNote()"
                    :title="selectedFolder ? '新建在当前选中的目录里' : '新建在「未归档」里'">新建笔记</button>
            <button class="btn sm" @click="startNewFolder(selectedFolder || tree.unfiled_root_id)"
                    title="在你选中的目录里建一个子目录">新建目录</button>
            <button class="btn sm" @click="startNewGroup"
                    title="建一个最顶层分组（DSE 数学、IB 数学…笔记自己的分类，跟体系无关）">新建分组</button>
            <input type="text" v-model="keyword" placeholder="搜标题 / 正文" @input="onSearch">
          </div>
          <!-- 少用的两件事另起一行：把现成文档变笔记、整体备份/迁移 -->
          <div class="nb-tools">
            <button class="btn sm ghost" :disabled="importingDoc" @click="pickDoc"
                    title="Word / PDF / HTML / 文本 / PPT 都能导入；会转成 Markdown，并把原件一起存进笔记">
              {{ importingDoc ? '正在转换…' : '导入文档' }}
            </button>
            <input ref="docEl" type="file" style="display:none"
                   accept=".docx,.doc,.pdf,.html,.htm,.txt,.md,.pptx,.ppt,.rtf" @change="onDocPicked">
            <button class="btn sm ghost" @click="openTransfer" title="导出成一个 zip / 从 zip 导入（换设备用）">
              备份 / 迁移
            </button>
          </div>

          <!-- 搜索：平铺结果 + 每篇的路径（在树里高亮反而难找） -->
          <div v-if="searching" class="note-list">
            <div v-for="n in notes" :key="n.id" class="note-item"
                 :class="{ on: cur && n.id === cur.id }" @click="open(n.id)">
              <div class="note-title">
                <input v-if="editing && editing.mode === 'rename' && editing.id === n.id"
                       :ref="focusEdit" class="nb-inline" v-model="editing.value"
                       @click.stop @keyup.enter="commitEdit" @keyup.esc="cancelEdit" @blur="commitEdit">
                <template v-else><span v-if="n.pinned" class="pin">📌 </span>{{ n.title }}</template>
              </div>
              <div class="nb-path">{{ n.path || '（没有目录）' }}</div>
              <div class="note-meta">
                <span>{{ (n.updated_at || '').replace('T', ' ').slice(5, 16) }}</span>
                <span style="flex:1"></span>
                <a @click.stop="openMenu({ kind: 'note', node: n }, $event)">更多</a>
              </div>
            </div>
            <div v-if="!notes.length" class="empty">没有匹配的笔记</div>
          </div>

          <!-- 树：分组 → 目录… → 笔记。拖动 = 改层级/改顺序 -->
          <div v-else class="nb-tree">
            <!-- 新建分组的输入行：放在最上面，新分组也建在最前面 -->
            <div v-if="editing && editing.mode === 'newGroup'" class="nb-row editing">
              <span class="nb-caret">▸</span>
              <input :ref="focusEdit" class="nb-inline" v-model="editing.value"
                     placeholder="分组名（如 DSE 数学），回车确定" @keyup.enter="commitEdit"
                     @keyup.esc="cancelEdit" @blur="commitEdit">
            </div>
            <div v-for="row in rows" :key="row.kind + row.node.id" class="nb-row"
                 :class="{
                   on: (row.kind === 'note' && cur && cur.id === row.node.id)
                       || (row.kind !== 'note' && selectedFolder === row.node.id),
                   root: row.kind === 'root',
                   folder: row.kind === 'folder',
                   note: row.kind === 'note',
                   dragging: drag && drag.id === row.node.id,
                   'drop-before': dropAt && dropAt.id === row.node.id && dropAt.zone === 'before',
                   'drop-after': dropAt && dropAt.id === row.node.id && dropAt.zone === 'after',
                   'drop-in': dropAt && dropAt.id === row.node.id && dropAt.zone === 'in',
                 }"
                 :style="{ paddingLeft: (6 + row.depth * 14) + 'px' }"
                 :draggable="row.kind !== 'root'"
                 @dragstart="onDragStart(row, $event)"
                 @dragover="onDragOver(row, $event)"
                 @dragend="onDragEnd"
                 @drop="onDrop(row, $event)"
                 @click="onRowClick(row)"
                 @contextmenu.prevent="openMenu(row, $event)">
              <!-- 展开三角：只有目录/体系有 -->
              <span v-if="row.kind !== 'note'" class="nb-caret"
                    @click.stop="toggleExpand(row.node.id)">{{ expanded[row.node.id] ? '▾' : '▸' }}</span>
              <span v-else class="nb-caret">·</span>

              <!-- 改名：原地变输入框 -->
              <input v-if="editing && editing.mode === 'rename' && editing.id === row.node.id"
                     :ref="focusEdit" class="nb-inline" v-model="editing.value"
                     @click.stop @keyup.enter="commitEdit" @keyup.esc="cancelEdit" @blur="commitEdit">
              <template v-else>
                <span class="nb-name" :title="row.kind === 'note' ? row.node.title : row.node.name">
                  <span v-if="row.kind === 'note' && row.node.pinned" class="pin">📌</span>{{ row.kind === 'note' ? row.node.title : row.node.name }}
                </span>
                <span v-if="row.kind !== 'note' && row.node.count" class="nb-count">{{ row.node.count }}</span>
              </template>

              <span class="nb-more" @click.stop="openMenu(row, $event)" title="更多">⋯</span>
            </div>

            <!-- 新建目录的输入行：挂在目标目录下面 -->
            <div v-if="editing && editing.mode === 'new'" class="nb-row editing">
              <input :ref="focusEdit" class="nb-inline" v-model="editing.value"
                     placeholder="目录名，回车确定" @keyup.enter="commitEdit"
                     @keyup.esc="cancelEdit" @blur="commitEdit">
            </div>
          </div>
          <div v-if="!searching && !rows.length" class="empty">还没有目录</div>

          <!-- 树的操作提示：拖拽是主要方式，但得先让人知道能拖 -->
          <div v-if="!searching" class="small muted nb-tip">
            拖动可改层级与顺序；「⋯」里有重命名 / 上下移 / 删除。删目录不会删笔记（内容会移到上一级）。
            <br>最上层是分组（DSE 数学、IB 数学这类）—— 那是笔记自己的分类，跟体系无关联，随便建与改。
          </div>
        </div>
      </div>

      <!-- ============ 中：编辑 / 预览 ============ -->
      <div class="card notes-main">
        <div v-if="!hasNote" class="empty">左边选一篇笔记，或新建一篇</div>

        <template v-else>
          <div class="notes-toolbar">
            <input type="text" v-model="cur.title" style="width:200px;font-weight:600"
                   @input="onTitleInput" @blur="saveNow">
            <div class="chips" style="margin:0">
              <span class="chip" :class="{ on: mode === 'edit' }" @click="mode = 'edit'">编辑</span>
              <span class="chip" :class="{ on: mode === 'split' }" @click="mode = 'split'">分栏</span>
              <span class="chip" :class="{ on: mode === 'preview' }" @click="mode = 'preview'">预览</span>
            </div>
            <span class="sep"></span>
            <button class="btn sm" @click="pickImage">插入图片</button>
            <input ref="fileEl" type="file" accept="image/*" style="display:none" @change="onImageFile">
            <button class="btn sm" :disabled="!hasNote || pickingQ"
                    title="从题库里找一道题插到光标处（可按关键字、标签、知识点搜）"
                    @click="showPickQ = true">插入题目</button>
            <button class="btn sm" :class="{ primary: inkOn }" @click="toggleInk">
              {{ inkOn ? '关闭画笔' : '画笔' }}
            </button>
            <button class="btn sm" :class="{ primary: showSide }" @click="toggleSide"
                    :title="showSide ? '收起左侧笔记列表' : '展开左侧笔记列表'">
              {{ showSide ? '收起侧栏' : '展开侧栏' }}
            </button>
            <button class="btn sm" :class="{ primary: showOutline }" @click="toggleOutline"
                    :title="showOutline ? '收起右侧目录' : '展开右侧目录'">
              目录
            </button>
            <button class="btn sm" :class="{ primary: showFormula }" @click="showFormula = !showFormula">
              公式速查
            </button>
            <button class="btn sm" @click="showExport = true">导出</button>
            <button class="btn sm" :class="{ primary: zen }" @click="toggleZen"
                    :title="zen ? '退出全屏（Esc）' : '全屏：只留 Markdown 代码与预览'">
              {{ zen ? '退出全屏' : '全屏' }}
            </button>
            <span style="flex:1"></span>
            <span class="muted" style="font-size:12px">
              <span v-if="saveState === 'dirty'">未保存</span>
              <span v-else-if="saveState === 'saving'">保存中…</span>
              <span v-else-if="saveState === 'saved'">已保存</span>
              <span v-else-if="saveState === 'error'" style="color:var(--danger)">保存失败</span>
            </span>
          </div>

          <div v-if="inkOn" class="notes-toolbar" style="margin-top:-4px">
            <div class="ink-tools">
              <span class="muted" style="font-size:12px">笔色</span>
              <span v-for="c in colors" :key="c.value" class="ink-swatch"
                    :class="{ on: inkTool === 'pen' && penColor === c.value }"
                    :style="{ background: c.value }" :title="c.name"
                    @click="inkTool = 'pen'; penColor = c.value"></span>
              <button class="btn sm" :class="{ primary: inkTool === 'eraser' }"
                      @click="inkTool = 'eraser'">橡皮</button>
              <span class="muted" style="font-size:12px;margin-left:6px">粗细</span>
              <input type="range" class="ink-width" min="1" max="14" v-model.number="penWidth">
              <span class="muted" style="font-size:12px">{{ penWidth }}px</span>
              <span class="sep"></span>
              <button class="btn sm ghost" @click="undoInk">撤销</button>
              <button class="btn sm ghost" @click="clearInk">清空</button>
              <span class="muted" style="font-size:12px">笔画只画在预览上，存在笔记里，换窗口大小不会跑位</span>
            </div>
          </div>

          <div v-if="libState === 'loading'" class="muted" style="font-size:12px;margin-bottom:6px">
            正在加载 Markdown / 公式库…
          </div>
          <div v-if="libState === 'error'" class="muted"
               style="font-size:12px;margin-bottom:6px;display:flex;align-items:center;gap:8px">
            <span>⚠️ Markdown / 公式库没加载上（{{ libError }}）—— 正文照样能编辑保存，
            但只按纯文本显示，公式也不会渲染。</span>
            <button class="btn sm" @click="initLibs">重试</button>
          </div>

          <div class="notes-body" :class="{ split: mode === 'split' }">
            <textarea v-if="mode !== 'preview'" ref="editorEl" class="notes-editor"
                      v-model="cur.content" @input="onEdit" @paste="onPaste"
                      placeholder="在这里写 Markdown。粘贴截图可以直接插进来。"></textarea>

            <div v-if="mode !== 'edit'" ref="previewWrapEl" class="notes-preview-wrap" @scroll="onPreviewScroll">
              <div ref="previewEl" class="notes-preview"></div>
              <canvas v-show="inkOn" ref="canvasEl" class="ink-canvas" :class="{ on: inkOn }"
                      @pointerdown="inkDown" @pointermove="inkMove"
                      @pointerup="inkUp" @pointerleave="inkUp" @pointercancel="inkUp"></canvas>
            </div>
          </div>
        </template>
      </div>

      <!-- ============ 右：本文目录 + 公式速查（各自可收起） ============
           目录从左侧挪到这里（用户 2026-10-05 提的）：它是给预览导航用的，
           挨着正文比压在左侧笔记列表那栏里顺手。两块各自有开关，
           都关掉时这一列整个消失（列数由 with-right 类决定，不留空列）。 -->
      <div v-if="hasNote && (showOutline || showFormula)" class="notes-right">
        <div v-if="showOutline" class="card">
          <h2>目录</h2>
          <div v-if="!outline.length" class="muted" style="font-size:12px">
            正文里写 <code># 标题</code>，这里会自动列出来
          </div>
          <div class="notes-toc">
            <div v-for="h in outline" :key="h.id" class="toc-item"
                 :class="{ on: h.id === activeHeading }"
                 :style="{ paddingLeft: (8 + (h.level - 1) * 11) + 'px' }"
                 @click="scrollToHeading(h.id)">{{ h.text }}</div>
          </div>
        </div>

        <div v-if="showFormula" class="card notes-formula">
          <h2>公式速查</h2>
          <div class="muted" style="font-size:11.5px;margin-bottom:10px">
            点一下就插到光标处，光标停在空位上。行内用 <code>$…$</code>，独立一行用 <code>$$…$$</code>；
            从别处粘来的 <code>\\(…\\)</code> 写法也认。
          </div>
          <div v-for="g in snippets" :key="g.title" class="fx-group">
            <div class="fx-title">{{ g.title }}</div>
            <div class="fx-items">
              <span v-for="(s, i) in g.items" :key="i" class="fx-item" @click="insertSnippet(s)">{{ s.label }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 导出 Word / PDF ============ -->
    <Modal v-if="showExport" title="导出这篇笔记" @close="showExport = false">
      <p class="muted" style="margin-top:0">
        正文里的公式会转成 <strong>Word 原生公式</strong>（在 Word 里还能双击修改），
        插图一并带上；板书作为一页手写图层附在文末。
      </p>

      <div class="row" style="align-items:center;gap:10px;margin:10px 0">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer"
               :class="{ muted: !cur || !cur.ink || !cur.ink.length }">
          <input type="checkbox" v-model="includeInk"
                 :disabled="!cur || !cur.ink || !cur.ink.length" style="width:auto">
          附上板书（{{ cur && cur.ink ? cur.ink.length : 0 }} 笔）
        </label>
      </div>

      <div class="row" style="align-items:center;gap:10px;margin:10px 0">
        <span class="muted" style="font-size:12px">
          给谁看：<template v-if="!cur || !(cur.content || '').includes('<!--q:')">
          （正文里还没插过题库的题，两版内容一样）</template>
        </span>
        <div v-if="cur && (cur.content || '').includes('<!--q:')" class="chips" style="margin:0">
          <span class="chip" :class="{ on: exportWithAnswer }" @click="exportWithAnswer = true">
            教师版（含答案）
          </span>
          <span class="chip" :class="{ on: !exportWithAnswer }" @click="exportWithAnswer = false">
            学生版（不含答案）
          </span>
        </div>
      </div>

      <div class="row" style="gap:10px;margin-top:4px">
        <button class="btn primary" :disabled="!!exporting" @click="doExport('docx')">
          {{ exporting === 'docx' ? '正在生成…' : '导出 Word（.docx）' }}
        </button>
        <button class="btn" :disabled="!!exporting || (caps && !caps.pdf)"
                :title="caps && !caps.pdf ? caps.pdf_reason : '用本机 Word 排版后导出 PDF，公式与分页最准'"
                @click="doExport('pdf')">
          {{ exporting === 'pdf' ? '正在生成…（Word 启动稍几秒）' : '导出 PDF' }}
        </button>
      </div>

      <div v-if="caps && !caps.pdf" class="muted" style="font-size:12px;margin-top:10px">
        ⚠️ 本机不能直接导 PDF：{{ caps.pdf_reason }}<br>
        可以先「导出 Word」，再用 Word 另存为 PDF。
      </div>
      <div v-else-if="caps && !caps.formula" class="muted" style="font-size:12px;margin-top:10px">
        ⚠️ 公式只能按 <code>$…$</code> 原文导出：{{ caps.formula_reason }}<br>
        正文、插图、板书不受影响。
      </div>
      <div v-else-if="exporting === 'pdf'" class="muted" style="font-size:12px;margin-top:10px">
        正在调用本机 Word 排版，首次可能要等十几秒，请不要关页面。
      </div>

      <template #foot>
        <button class="btn ghost" @click="showExport = false">关闭</button>
      </template>
    </Modal>

    <!-- ============ 从题库里挑题插进正文 ============ -->
    <QuestionPicker v-if="showPickQ" :next-index="pickNextIndex"
                    @close="showPickQ = false" @pick="insertQuestions" />

    <!-- 行的「⋯」菜单。fixed 定位：挂在最外层，不跟着树那一列被裁掉 -->
    <div v-if="menu" class="nb-menu" :style="{ left: menu.x + 'px', top: menu.y + 'px' }"
         @click.stop>
      <div v-for="(it, i) in menu.items" :key="i" :class="{ danger: it.danger }"
           @click="runMenu(it)">{{ it.label }}</div>
    </div>
    <!-- ============ 备份 / 迁移 ============ -->
    <Modal v-if="showTransfer" title="备份 / 迁移笔记" @close="showTransfer = false">
      <p class="muted" style="margin-top:0">
        导出的 zip 里有：<strong>目录结构、每篇正文、板书笔画、正文引用的配图</strong>，
        另外还有一份按目录摆好的 <code>.md</code> 副本 —— 哪天不用这个程序了，
        解压进资源管理器照样能一篇篇读出来。
      </p>
      <p class="muted" style="margin-top:0">
        换设备：在新机器上装好拾课，打开这里，导入同一个包即可。
      </p>

      <div class="row" style="gap:8px;align-items:center;margin:12px 0">
        <button class="btn primary" :disabled="!!transferring" @click="exportBackup">
          {{ transferring === 'export' ? '正在打包…' : '导出全部笔记（共 ' + tree.total + ' 篇）' }}
        </button>
        <span class="muted" style="font-size:12px">存哪儿由浏览器的下载设置决定</span>
      </div>

      <hr style="border:none;border-top:1px solid var(--border);margin:14px 0">

      <div class="field">
        <label>从备份导入</label>
        <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
          <button class="btn sm" :disabled="!!transferring" @click="pickTransferFile">选择备份包（.zip）</button>
          <input ref="transferEl" type="file" accept=".zip,application/zip" style="display:none"
                 @change="onTransferPicked">
          <span v-if="transferFile" class="small">{{ transferFile.name }}</span>
          <span v-else class="small muted">还没选文件</span>
        </div>
        <label class="small" style="display:flex;align-items:center;gap:6px;margin-top:8px;cursor:pointer">
          <input type="checkbox" v-model="transferOver" style="width:auto">
          本机已有同一篇笔记时，用备份里的<strong>覆盖</strong>
          <span class="muted">（默认不勾：跳过，保留本机那份）</span>
        </label>
        <div class="row" style="gap:8px;margin-top:10px">
          <button class="btn" :disabled="!transferFile || !!transferring" @click="doImport(true)">
            {{ transferring === 'try' ? '试算中…' : '先试算（不写入）' }}
          </button>
          <button class="btn primary" :disabled="!transferFile || !!transferring" @click="doImport(false)">
            {{ transferring === 'go' ? '导入中…' : '开始导入' }}
          </button>
        </div>
      </div>

      <div v-if="transferReport" class="small" style="margin-top:12px">
        <div v-if="transferReport.dry_run" class="tag" style="margin-bottom:6px">试算结果（什么都没写）</div>
        <div v-else class="tag green" style="margin-bottom:6px">导入完成</div>
        <div>笔记：新建 {{ transferReport.notes_created }} ·
          覆盖 {{ transferReport.notes_overwritten }} ·
          跳过（本机已有）{{ transferReport.notes_skipped }} ·
          包内共 {{ transferReport.notes_total }}</div>
        <div>目录：新建 {{ transferReport.folders_created }} · 复用 {{ transferReport.folders_reused }}</div>
        <div>配图：新增 {{ transferReport.images_added }} · 已存在 {{ transferReport.images_skipped }}</div>
        <div v-for="(w, i) in transferReport.warnings" :key="i" style="color:var(--warning,#d97706)">
          ⚠️ {{ w }}
        </div>
      </div>

      <template #foot>
        <button class="btn ghost" @click="showTransfer = false">关闭</button>
      </template>
    </Modal>
  </div>`,
};
