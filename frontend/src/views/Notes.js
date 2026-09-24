// 笔记：Markdown 正文 + 板书笔画 + 配图。
//
// 三栏：左边（笔记列表 + 本笔记目录）／中间（编辑·预览·分栏）／右边（公式速查，可收起）。
//
// 几个刻意的决定，写下来免得以后自己都忘了为什么：
//   · 目录是从**渲染后的 DOM** 里扫 <h1..h6> 得到的，不是用正则去解析 Markdown。
//     正则会被代码块里的 "#" 骗到，而且一旦和渲染器对不上就永远不同步。
//   · 笔画存矢量（坐标归一化到 0~1），不存位图：橡皮、换色、调粗细、撤销
//     本质都是「按新状态重画一遍」，位图做不到，而且缩放会糊。
//   · 预览走 innerHTML，所以必须过 DOMPurify —— marked 自己不做消毒。
//   · 保存是防抖 PATCH 且**只提交改动过的字段**，这样切笔记时不会把另一头覆盖掉。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { notesApi } from '../api.js';
import { fail, ok, warn } from '../store.js';

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

// 按需加载的外部库 —— 全部走本地 vendor，不碰外网。
//   · 不放 index.html：<script src> 不带 defer 会阻塞 HTML 解析，
//     而这几个库只有本页用得上，放全局等于让每个页面首屏都多等一次。
//   · 不引 CDN：实测这台机器冷启动拉 unpkg 要 29s，还经常直接超时
//     （WinError 10060），笔记页会长时间停在"正在加载编辑器…"。
//     文件在 frontend/vendor/ 下，随仓库一起走，离线也能用。
// 版本（升级时按这个换文件）：
//   marked 12.0.2 · dompurify 3.1.6 · katex 0.16.9
// katex.min.css 里的 font url 是相对路径 fonts/xxx.woff2，
// 因此字体必须放在 /vendor/katex/fonts/ 下（已就位，20 个 woff2）。
const LIBS = {
  marked: '/vendor/marked.min.js',
  purify: '/vendor/purify.min.js',
  katex: '/vendor/katex/katex.min.js',
  autoRender: '/vendor/katex/auto-render.min.js',
  katexCss: '/vendor/katex/katex.min.css',
};

let libsPromise = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    s.onload = () => resolve(src);
    s.onerror = () => reject(new Error('加载失败 ' + src));
    document.head.appendChild(s);
  });
}

/** 只加载一次（结果缓存在模块作用域，切页面来回也不重复拉）。 */
function ensureLibs() {
  if (libsPromise) return libsPromise;
  libsPromise = (async () => {
    if (!document.querySelector('link[data-katex]')) {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = LIBS.katexCss;
      l.dataset.katex = '1';
      document.head.appendChild(l);
    }
    await Promise.all([
      loadScript(LIBS.marked),
      loadScript(LIBS.purify),
      loadScript(LIBS.katex),
    ]);
    // ⚠️ auto-render **必须等 katex**：它一加载就把 window.katex 抓进闭包，
    //    并行加载时它先到就抓了个 undefined，之后一渲染公式就报
    //    "Cannot read properties of undefined (reading 'ParseError')"。
    //    （踩过：四个脚本并行时公式静默不渲染，预览里一直是 $…$ 原文。）
    await loadScript(LIBS.autoRender);
  })();
  return libsPromise;
}

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
  setup() {
    const route = useRoute();
    const router = useRouter();

    const notes = ref([]);
    const keyword = ref('');
    const cur = ref(null);                 // 当前打开的笔记（含 content / ink）
    const mode = ref('split');             // split | edit | preview
    const showFormula = ref(true);
    const saveState = ref('idle');         // idle | dirty | saving | saved | error

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
    const markdownReady = computed(() => libState.value === 'ready');

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
      searchTimer = setTimeout(loadList, 300);
    }

    async function createNote() {
      await flushSave();
      try {
        const r = await notesApi.create({ title: '未命名笔记', content: SAMPLE });
        await loadList();
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
        ok(r.images_removed ? `已删除（顺带清掉 ${r.images_removed} 张配图）` : '已删除');
      } catch (e) {
        fail(e.message);
      }
    }

    async function togglePin(n) {
      try {
        await notesApi.update(n.id, { pinned: !n.pinned });
        await loadList();
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
        await nextTick();
        render();
        if (route.params.id !== id) router.replace('/notes/' + id);
      } catch (e) {
        fail(e.message);
        cur.value = null;
      }
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
        // 就地更新列表那一条，不用整表重拉
        const i = notes.value.findIndex(n => n.id === brief.id);
        if (i >= 0) notes.value[i] = brief;
      } catch (e) {
        saveState.value = 'error';
        fail('保存失败：' + e.message);
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
      const md = cur.value.content || '';

      if (!window.marked) {                 // CDN 没加载上：退成纯文本，别白屏
        el.textContent = md;
        outline.value = [];
        return;
      }
      const raw = window.marked.parse(md, { breaks: true, gfm: true });
      el.innerHTML = window.DOMPurify ? window.DOMPurify.sanitize(raw) : raw;

      // 公式在消毒**之后**渲染：KaTeX 自己生成的 DOM 是可信的，也没必要再过一遍消毒
      if (window.renderMathInElement) {
        try {
          window.renderMathInElement(el, {
            delimiters: [
              { left: '$$', right: '$$', display: true },
              { left: '$', right: '$', display: false },
            ],
            throwOnError: false,
          });
        } catch (e) { /* 公式写错不该让整篇笔记渲染不出来 */ }
      }

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

    function pickImage() {
      if (fileEl.value) fileEl.value.click();
    }
    async function onImageFile(e) {
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
    function onResize() {
      sizeCanvas();
      redraw();
    }

    onMounted(async () => {
      // 渲染依赖这几个库，所以先加载、加载完再渲染一次：在这之前会退成纯文本，
      // 不会先闪一屏“没渲染的样子”然后才变。
      libState.value = 'loading';
      ensureLibs()
        .then(() => { libState.value = 'ready'; if (cur.value) render(); })
        .catch(() => { libState.value = 'error'; });

      await loadList();
      if (route.params.id) await open(route.params.id);
      window.addEventListener('resize', onResize);
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
    });

    watch(() => route.params.id, (id) => {
      if (id && (!cur.value || cur.value.id !== id)) open(id);
    });

    return {
      notes, keyword, cur, hasNote, mode, showFormula, saveState, markdownReady, libState,
      outline, activeHeading, editorEl, previewEl, previewWrapEl, canvasEl, fileEl,
      inkOn, inkTool, penColor, penWidth, colors: PEN_COLORS, snippets: SNIPPETS,
      loadList, onSearch, createNote, removeNote, togglePin, open,
      onEdit, onTitleInput, render, scrollToHeading, onPreviewScroll,
      insertSnippet, pickImage, onImageFile, onPaste,
      toggleInk, inkDown, inkMove, inkUp, undoInk, clearInk,
      saveNow: flushSave,
    };
  },
  template: `
  <div>
    <div class="page-head">
      <h1>笔记</h1>
      <span class="sub">Markdown 正文 · 公式实时渲染 · 插图 · 板书笔画</span>
    </div>

    <div class="notes-grid" :class="{ 'with-formula': showFormula && hasNote }">
      <!-- ============ 左：列表 + 目录 ============ -->
      <div class="notes-side">
        <div class="card">
          <div style="display:flex;gap:8px;margin-bottom:10px">
            <button class="btn primary sm" @click="createNote">新建笔记</button>
            <input type="text" v-model="keyword" placeholder="搜标题 / 正文" @input="onSearch">
          </div>
          <div class="note-list">
            <div v-for="n in notes" :key="n.id" class="note-item"
                 :class="{ on: cur && n.id === cur.id }" @click="open(n.id)">
              <div class="note-title">
                <span v-if="n.pinned" class="pin">📌 </span>{{ n.title }}
              </div>
              <div class="note-excerpt">{{ n.excerpt || '（空笔记）' }}</div>
              <div class="note-meta">
                <span>{{ (n.updated_at || '').replace('T', ' ').slice(5, 16) }}</span>
                <span style="flex:1"></span>
                <a @click.stop="togglePin(n)">{{ n.pinned ? '取消置顶' : '置顶' }}</a>
                <a @click.stop="removeNote(n)">删除</a>
              </div>
            </div>
            <div v-if="!notes.length" class="empty">还没有笔记，点「新建笔记」开始</div>
          </div>
        </div>

        <div class="card">
          <h2>目录</h2>
          <div v-if="!hasNote" class="muted" style="font-size:12px">打开一篇笔记后显示</div>
          <div v-else-if="!outline.length" class="muted" style="font-size:12px">
            正文里写 <code># 标题</code>，这里会自动列出来
          </div>
          <div v-for="h in outline" :key="h.id" class="toc-item"
               :class="{ on: h.id === activeHeading }"
               :style="{ paddingLeft: (8 + (h.level - 1) * 11) + 'px' }"
               @click="scrollToHeading(h.id)">{{ h.text }}</div>
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
            <button class="btn sm" :class="{ primary: inkOn }" @click="toggleInk">
              {{ inkOn ? '关闭画笔' : '画笔' }}
            </button>
            <button class="btn sm" :class="{ primary: showFormula }" @click="showFormula = !showFormula">
              公式速查
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
          <div v-else-if="libState === 'error'" class="muted" style="font-size:12px;margin-bottom:6px">
            ⚠️ Markdown / 公式库没加载上（多半是网络问题）—— 正文照样能编辑保存，
            但只按纯文本显示，公式也不会渲染。刷新重试。
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

      <!-- ============ 右：公式速查 ============ -->
      <div v-if="showFormula && hasNote" class="card notes-formula">
        <h2>公式速查</h2>
        <div class="muted" style="font-size:11.5px;margin-bottom:10px">
          点一下就插到光标处，光标停在空位上。行内用 <code>$…$</code>，独立一行用 <code>$$…$$</code>。
        </div>
        <div v-for="g in snippets" :key="g.title" class="fx-group">
          <div class="fx-title">{{ g.title }}</div>
          <div class="fx-items">
            <span v-for="(s, i) in g.items" :key="i" class="fx-item" @click="insertSnippet(s)">{{ s.label }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>`,
};
