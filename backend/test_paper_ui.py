# -*- coding: utf-8 -*-
"""出卷页细节优化 —— 题干按公式渲染 + 标签多关键词搜索；外加录题/批量页
「打开不自动加载上次文档」。

为什么不起服务
------------------------------------------------
这一轮改的全是**前端**：渲染管线的唯一出处、Papers.js 怎么接线、标签搜索的
纯逻辑。所以这里只做三件事，一件都不用 uvicorn：

  ① 静态守卫 —— 谁该引用谁、唯一出处有没有守住、顺序坑有没有被改回去
  ② Vue**真编译器**校验 Papers.js 的模板（抓 compiler-30 这类错误）
  ③ node 直接跑 tagFilter.js 的纯逻辑（OR / AND 两种写法的语义）

⚠️ 为什么不能像 batch/entry 那样用 jsdom 真挂一遍：SPA 的入口是
   `<script type="module" src="/src/main.js">` + importmap，**jsdom 不执行 ES module**。
   所以 Papers.js 只能到「模板能编译 + 接线正确」这一层，最后那一眼得靠人。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend"
TMP = Path(tempfile.gettempdir()) / "shike_paper_ui_scratch"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

fails: list[str] = []
total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global total
    total += 1
    line = f"  [{'OK ' if ok else 'FAIL'}] {name}"
    if detail and not ok:
        line += f"   <- {detail}"
    print(line)
    if not ok:
        fails.append(name)


def section(title: str) -> None:
    print(f"\n==== {title} ====")


def node_bin() -> str | None:
    return shutil.which("node")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def extract_template(path: Path) -> str | None:
    """把 `template: \\`...\\`` 那段抠出来（SPA 视图都这么写）。"""
    src = read(path)
    key = "template: `"
    i = src.find(key)
    if i < 0:
        return None
    j = src.find("`", i + len(key))
    if j < 0:
        return None
    return src[i + len(key):j]


# ================================================================ ① 渲染管线唯一出处
def t_pipeline() -> None:
    section("① 渲染管线：唯一出处（mathRender.js）")
    mr = FE / "src" / "mathRender.js"
    check("frontend/src/mathRender.js 存在", mr.exists(), str(mr))
    if not mr.exists():
        return
    src = read(mr)

    for fn in ("LIBS", "ensureLibs", "resetLibs", "normalizeMath", "renderRichEl"):
        check(f"mathRender.js 导出 {fn}",
              re.search(rf"export\s+(const|function)\s+{fn}\b", src) is not None)

    check("不 import 任何东西（纯工具模块，谁都能引）",
          not re.search(r"^\s*import\s", src, re.M), "")
    check("不引外网 CDN（脚本地址全是本地 /vendor/…，注释里提 unpkg 不算）",
          not re.search(r"""["']https?://""", src), "")
    check("vendor 路径都是绝对的 /vendor/…",
          all(f"'{v}'" in src for v in (
              "/vendor/marked.min.js", "/vendor/purify.min.js",
              "/vendor/katex/katex.min.js", "/vendor/katex/auto-render.min.js")))

    # ⚠️ 这两条是踩过的坑，别被「看起来等价」的重构改回去
    check("normalizeMath 排在 marked.parse **之前**（放后面公式必挂）",
          re.search(r"marked\.parse\(\s*normalizeMath\(", src) is not None,
          "marked.parse 的入参必须是 normalizeMath 的结果")
    i_all = src.find("await Promise.all(")
    i_auto = src.find("await loadScript(LIBS.autoRender)")
    check("auto-render 在 katex 之后单独加载（并行会抓到 undefined 的 window.katex）",
          0 <= i_all < i_auto, f"{i_all} vs {i_auto}")
    check("消毒在写 innerHTML 那一步做（marked 自己不管消毒）",
          "DOMPurify" in src and "sanitize(raw)" in src, "")
    check("marked 缺失时退纯文本、不白屏", "el.textContent = t" in src, "")
    check("公式渲染包了 try（写错一条公式不该让整块内容消失）",
          "catch (e) { /* 公式写错" in src, "")


# ================================================================ ② Notes.js 收敛
def t_notes() -> None:
    section("② 笔记页已收敛到同一条管线（不再自带一份）")
    notes = read(FE / "src" / "views" / "Notes.js")

    check("Notes.js 从 ../mathRender.js 引入",
          "from '../mathRender.js'" in notes, "")
    check("Notes.js 不再自己定义 normalizeMath（唯一出处守住）",
          not re.search(r"function\s+normalizeMath", notes), "")
    check("Notes.js 不再自己定义 ensureLibs / loadScript",
          not re.search(r"function\s+ensureLibs", notes)
          and not re.search(r"function\s+loadScript", notes), "")
    check("Notes.js 不再直接调 window.marked.parse（渲染只走管线）",
          "window.marked.parse" not in notes, "")
    check("Notes.js 的 render() 调 renderRichEl", "renderRichEl(el, src)" in notes, "")
    check("重试走 resetLibs()（不然模块级缓存一旦 reject 就永远好不了）",
          "resetLibs()" in notes, "")
    check("render 里没有残留的第二遍 renderMathInElement（会让公式渲染两遍）",
          "window.renderMathInElement" not in notes, "")


# ================================================================ ③ 题干渲染接线
def t_papers_render() -> None:
    section("③ Papers.js：题库结果改渲染后的题干")
    pp = FE / "src" / "views" / "Papers.js"
    src = read(pp)

    check("Papers.js 从 ../mathRender.js 引入 ensureLibs / renderRichEl",
          "from '../mathRender.js'" in src and "ensureLibs" in src and "renderRichEl" in src, "")

    tpl = extract_template(pp) or ""
    check("题库结果用 .q-rich 容器 + setRichEl('row:'+id) 接元素",
          'class="q-rich"' in tpl and "setRichEl('row:' + q.id, el)" in tpl, "")
    check("已选列表同样渲染（同一页里两处不能一处公式一处原文）",
          "setRichEl('pick:' + q.id, el)" in tpl, "")
    check("空题干不给渲染容器（渲染空串没意义）",
          tpl.count('v-if="q.content"') >= 2, str(tpl.count('v-if="q.content"')))
    check("纯文本摘要 brief 已移除（它正是被替换掉的那个）",
          "function brief" not in src and "brief(q)" not in tpl, "")

    # 渲染容器里**不能**有 Vue 插值：KaTeX 会把那些文本节点换掉
    m = re.search(r'<div v-if="q\.content" class="q-rich[^"]*"[^>]*>(.*?)</div>', tpl, re.S)
    check("渲染容器里没有 Vue 插值（会被 KaTeX 顶掉）",
          bool(m) and "{{" not in (m.group(1) or ""), (m.group(1) or "")[:60])

    check("每次列表变化后重渲染（否则翻页/加题后新容器是空的）",
          re.search(r"watch\(\s*\[rows,\s*picked\]", src) is not None, "")
    check("重渲染等 nextTick（等 v-for 把新容器挂上）",
          "await nextTick();" in src and "paintRich();" in src, "")
    check("库没加载上时先铺原文（别留一屏空白）", "!libsReady" in src and "libsReady = true" in src, "")
    check("渲染失败不挡着出卷（catch 后直接 return）",
          re.search(r"catch \(e\) \{ return; \}\s*//\s*加载不上", src) is not None, "")


# ================================================================ ④ 标签搜索
def t_papers_tags() -> None:
    section("④ Papers.js：标签多关键词搜索")
    pp = FE / "src" / "views" / "Papers.js"
    src = read(pp)
    tpl = extract_template(pp) or ""
    tf = FE / "src" / "tagFilter.js"

    check("frontend/src/tagFilter.js 存在", tf.exists(), str(tf))
    check("Papers.js 从 ../tagFilter.js 引入 matchTags / tagQueryHint",
          "from '../tagFilter.js'" in src and "matchTags" in src and "tagQueryHint" in src, "")

    check("有标签搜索输入框", re.search(r'<input[^>]*v-model="tagQuery"', tpl) is not None, "")
    check("占位符写明了「空格 = 任一命中」",
          "空格分隔 = 任一命中" in tpl, "")
    check("占位符写明了「竖线 = 同时包含」（反直觉，必须写出来）",
          "竖线分隔 = 同时包含" in tpl, "")
    check("标签列表渲染的是过滤后的 shownTags",
          'v-for="t in shownTags"' in tpl, "")
    check("搜不到时有明确提示", "没有匹配的标签" in tpl, "")
    check("命中说明回显（让老师确认自己没写反）", "{{ tagHint }}" in tpl, "")

    check("已选标签单独列一行（被搜索藏起来也必须能取消）",
          "已选：" in tpl and 'v-for="t in filter.tags"' in tpl, "")
    check("点已选标签能取消（toggleTag）",
          re.search(r'v-for="t in filter\.tags"[^>]*@click="toggleTag\(t\)"', tpl) is not None,
          "")
    check("重置筛选时一并清掉搜索词",
          re.search(r"tagQuery\.value = '';", src) is not None, "")
    check("搜索只在客户端过滤（不再打服务端接口）",
          "tags: filter.tags.length ? filter.tags.join(',')" in src, "")


# ================================================================ ⑤ 不自动加载上次文档
def t_no_auto_doc() -> None:
    section("⑤ 录题页 / 批量页：打开不自动加载上次那份文档")
    entry = read(FE / "entry.html")
    batch = read(FE / "batch.html")

    # entry.html：?doc=<id> 仍然直达；不再有「没指定就挑第一份」
    check("entry.html 仍有 ?doc= 直达", "get('doc')" in entry, "")
    check("entry.html 去掉了 docs[0] 兜底（那一刻的加载就是用户抱怨的等待）",
          "(this.docs.length ? this.docs[0] : null)" not in entry, "")
    check("entry.html 的兜底只在 autoFirst 时才生效（删完文档接上第一份）",
          "autoFirst && this.docs.length ? this.docs[0] : null" in entry, "")
    check("entry.html 删文档后仍是 loadDocs(true)", "loadDocs(true)" in entry, "")
    check("entry.html mounted 里是裸 loadDocs()（**别**给它传 true）",
          "this.loadDocs();" in entry and "this.loadDocs(true);" not in entry.split("mounted()")[1][:200],
          "")

    check("batch.html 去掉了 docs[0] 兜底",
          "|| this.docs[0] || null" not in batch, "")
    check("batch.html 只认 ?doc=（其余情况停在「选择试卷」）",
          re.search(r"const target = want \? this\.docs\.find\(d => d\.id === want\) : null;",
                    batch) is not None, "")
    # ⚠️ 这是这次改动的最大风险：卷面预览藏了，但历史任务/待审不能跟着没
    check("batch.html 的「历史任务」没有跟着 doc 一起被藏起来",
          re.search(r'<p class="panel-title">🕘 历史任务</p>', batch) is not None
          and not re.search(r'<el-card v-if="doc"[^>]*>\s*<template #header>\s*'
                            r'<div style="display:flex;align-items:center;gap:8px">\s*'
                            r'<p class="panel-title">🕘 历史任务', batch), "")
    check("batch.html 识别按钮那一段才是真的依赖 doc",
          re.search(r'<div v-if="doc" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">\s*'
                    r'<span class="muted-hint">识别范围</span>', batch) is not None, "")
    check("没选卷子时给了落点提示", "先在上方选择或上传一份试卷" in batch, "")

    rc = read(FE / "render_check.js")
    check("render_check.js 固化了「打开时没有自动打开任何文档」",
          "打开时没有自动打开任何文档" in rc, "")
    check("render_check.js 固化了「历史任务仍看得见」",
          "没选卷子也照样看得到「历史任务」" in rc, "")


# ================================================================ ⑥ 样式
def t_css() -> None:
    section("⑥ 样式：渲染出来的题干别撑破列表")
    css = read(FE / "app.css")
    check("app.css 有 .q-rich", re.search(r"^\.q-rich\b", css, re.M) is not None, "")
    check("列表里的渲染结果限高（老师是来挑题的，不是来通读的）",
          ".lesson-row .q-rich" in css and "max-height" in css, "")
    check("块级公式压了外边距（不然一道题吃掉半屏）",
          ".q-rich .katex-display" in css, "")
    check("长公式能换行、不溢出容器", "word-break" in css and "overflow: hidden" in css, "")


# ================================================================ ⑦ Vue 真编译器
def t_template_compile() -> None:
    section("⑦ Vue 真编译器校验 Papers.js 模板")
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上")
        return
    vue = FE / "vendor" / "vue.global.prod.js"
    check("vendor/vue.global.prod.js 存在", vue.exists(), str(vue))
    if not vue.exists():
        return

    pp = FE / "src" / "views" / "Papers.js"
    tpl = extract_template(pp)
    check("Papers.js 能提取出 template（模板写成字符串常量）",
          bool(tpl) and (tpl or "").rstrip().endswith(">"), ((tpl or "")[-60:]))
    if not tpl:
        return

    # ⚠️ 与 test_schedule_import.py ⑩ 同一套坑：compiler-dom 遇到属性值里的 `&`
    #    （`a && b`）会去 decodeEntities，需要一个 document；而 stub 又可能把真错误
    #    兜住 —— 所以下面额外编译一份**故意写错**的模板，必须报失败，用来自证校验器在跑。
    js = TMP / "compile_check.js"
    js.write_text(
        "const fs=require('fs'),vm=require('vm');\n"
        f"const VUE={str(vue)!r};\n"
        "function mkDoc() {\n"
        "  const el=()=>{const o={_raw:'',_foo:'',_text:''};\n"
        "    Object.defineProperty(o,'innerHTML',{set(v){o._raw=v;const m=/foo=\"([\\s\\S]*)\"/.exec(v);"
        "o._foo=m?m[1]:'';o._text=v;},get(){return o._raw;}});\n"
        "    Object.defineProperty(o,'children',{get(){const dec=s=>String(s).replace(/&quot;/g,'\"')"
        ".replace(/&#39;/g,\"'\").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');\n"
        "      return [{getAttribute:()=>dec(o._foo)}];}});\n"
        "    Object.defineProperty(o,'textContent',{get(){return String(o._text)"
        ".replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&').replace(/&quot;/g,'\"');}});\n"
        "    return o;};\n"
        "  return {createElement:()=>el(),querySelector:()=>null,createTextNode:()=>({})};\n"
        "}\n"
        "const ctx={console,document:mkDoc(),window:{}};vm.createContext(ctx);\n"
        "vm.runInContext(fs.readFileSync(VUE,'utf8'),ctx);\n"
        "function comp(tpl){try{ctx.Vue.compile(tpl);return 'OK';}"
        "catch(e){return 'FAIL '+String(e.message).slice(0,160);}}\n"
        "console.log('SELFTEST ' + comp('<div><template v-else-if=\"b\">x</template></div>'));\n"
        "console.log('SELFTEST_GOOD ' + comp('<div><p v-if=\"a\">1</p><p v-else>2</p></div>'));\n"
        "console.log('TPL ' + comp(fs.readFileSync(process.argv[2],'utf8')));\n",
        encoding="utf-8")
    tpl_file = TMP / "papers_template.html"
    tpl_file.write_text(tpl, encoding="utf-8")

    r = subprocess.run([node, str(js), str(tpl_file)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=90)
    out = (r.stdout or "") + (r.stderr or "")
    bad = re.search(r"SELFTEST FAIL", out)
    good = re.search(r"SELFTEST_GOOD OK", out)
    check("自证：故意写错的模板必须报错（否则说明校验在空转）", bool(bad), out[-200:])
    check("自证：正常 v-if/v-else 必须能编译", bool(good), out[-200:])
    check("Papers.js 的模板编译通过", re.search(r"^TPL OK", out, re.M) is not None,
          out[-300:])


# ================================================================ ⑧ tagFilter 纯逻辑
TAG_JS = r"""
import { parseTagQuery, matchTags, tagQueryHint } from './tagfilter.mjs';
let pass = 0, fail = 0;
function check(name, ok, extra) {
  if (ok) { pass++; console.log('  [OK ] ' + name); }
  else { fail++; console.log('  [FAIL] ' + name + (extra ? '   <- ' + extra : '')); }
}
// 刻意用一组「对称」和「函数」拆得开、又有交集的标签：
//   · 含「对称」：轴对称、中心对称
//   · 含「函数」：函数、三角函数、函数图象、余弦函数
//   · 同时含两者：无 —— 所以「对称|函数」必须一个都不返回，这就是 AND 的铁证
const TAGS = ['轴对称', '中心对称', '函数', '三角函数', '函数图象', '余弦函数', '不等式'];
const hit = (q) => matchTags(TAGS, q).join(',');
const n = (q) => matchTags(TAGS, q).length;

check('空查询 → 原样返回全部', n('') === 7 && n(null) === 7 && n(undefined) === 7, String(n('')));
check('单个词 = 子串匹配', hit('对称') === '轴对称,中心对称', hit('对称'));
check('单个词命中多个', hit('函数') === '函数,三角函数,函数图象,余弦函数', hit('函数'));
check('空格分隔 = 任一命中（对称 函数）',
      hit('对称 函数') === '轴对称,中心对称,函数,三角函数,函数图象,余弦函数', hit('对称 函数'));
check('★ 竖线分隔 = 同时包含（对称|函数 → 没有标签同时含这两个词，必须为空）',
      hit('对称|函数') === '', hit('对称|函数'));
check('★ 竖线确实是 AND：函数|图象 只留同时含这两个词的',
      hit('函数|图象') === '函数图象', hit('函数|图象'));
check('竖线两侧多空格也认（对称 |函数）', hit('对称 |函数') === '', hit('对称 |函数'));
check('只有竖线 → 关键词为空 → 返回全部', n('|') === 7 && n(' | ') === 7, String(n('|')));
check('纯空白 → 返回全部', n('   ') === 7, String(n('   ')));
check('不会改动传入的数组（返回新数组）',
      (() => { const a = TAGS.slice(); matchTags(a, '函数'); return a.length === 7; })(), '');
check('大小写不敏感（英文标签用得上）',
      matchTags(['ABC', 'abc-d'], 'abc').length === 2, '');
check('对象元素（{tag,count}）按 .tag 匹配，且原样返回',
      (() => {
        const list = [{ tag: '轴对称', count: 3 }, { tag: '函数', count: 5 }];
        const r = matchTags(list, '函数');
        return r.length === 1 && r[0].count === 5;
      })(), '');
check('列表为空不会炸', matchTags([], '函数').length === 0 && matchTags(null, 'x').length === 0, '');

check("parseTagQuery('对称 函数').mode === 'or'", parseTagQuery('对称 函数').mode === 'or', '');
check("parseTagQuery('对称|函数').mode === 'and'", parseTagQuery('对称|函数').mode === 'and', '');
check('两种符号都出现时以竖线为准（AND 更强，且混用没人能预期）',
      parseTagQuery('a b|c').mode === 'and' && parseTagQuery('a b|c').kws.join('/') === 'a b/c',
      JSON.stringify(parseTagQuery('a b|c')));
check('关键词去掉了空白', JSON.stringify(parseTagQuery(' 对称 |  函数 ').kws) === '["对称","函数"]',
      JSON.stringify(parseTagQuery(' 对称 |  函数 ').kws));
check('空查询没有提示文案', tagQueryHint('') === '', tagQueryHint(''));
check('OR 的提示里有「或」', tagQueryHint('对称 函数').indexOf('或') >= 0, tagQueryHint('对称 函数'));
check('AND 的提示里是「同时含」', tagQueryHint('对称|函数').indexOf('同时含') >= 0, tagQueryHint('对称|函数'));

console.log(`__TAG_DONE__ ${pass + fail} ${fail}`);
"""


def t_tag_filter() -> None:
    section("⑧ 标签搜索纯逻辑（node 直接跑 tagFilter.js）")
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上")
        return
    src = FE / "src" / "tagFilter.js"
    check("tagFilter.js 存在", src.exists(), str(src))
    if not src.exists():
        return
    text = read(src)
    check("tagFilter.js 不碰 DOM / 不 import 任何东西（纯逻辑才好单测）",
          "document" not in text and not re.search(r"^\s*import\s", text, re.M), "")

    d = TMP / "tag"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, d / "tagfilter.mjs")
    (d / "run.mjs").write_text(TAG_JS, encoding="utf-8")
    r = subprocess.run([node, str(d / "run.mjs")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if "[OK ]" in line or "[FAIL]" in line:
            m = re.match(r"\s*\[(OK |FAIL)\]\s*(.+?)(?:\s+<- (.*))?$", line)
            if m:
                check(m.group(2).strip(), m.group(1) == "OK ", m.group(3) or "")
    tail = [l for l in out.splitlines() if l.startswith("__TAG_DONE__")]
    check("node 侧断言全部跑完（进程没中途挂掉）", len(tail) == 1, out.strip()[-200:])
    if tail:
        n, bad = tail[0].split()[1:3]
        check(f"node 侧 {n} 项里 0 失败", bad == "0", f"失败 {bad} 项")


# ================================================================ ⑨ renderRichEl 接线
MATH_JS = r"""
import { normalizeMath, renderRichEl } from './mathrender.mjs';
let pass = 0, fail = 0;
function check(name, ok, extra) {
  if (ok) { pass++; console.log('  [OK ] ' + name); }
  else { fail++; console.log('  [FAIL] ' + name + (extra !== undefined ? '   <- ' + extra : '')); }
}

/* ---------- normalizeMath：这些是「预览能渲染 / 导出还是原文」的分水岭 ---------- */
check('不认识的写法原样返回', normalizeMath('设 f(x)=x^2') === '设 f(x)=x^2', normalizeMath('设 f(x)=x^2'));
check('\\[...\\] 归一成 $$...$$', normalizeMath('\\[x^{2}\\]') === '$$x^{2}$$', normalizeMath('\\[x^{2}\\]'));
check('\\(...\\) 归一成 $...$', normalizeMath('a\\(x+1\\)b') === 'a$x+1$b', normalizeMath('a\\(x+1\\)b'));
check('多个公式都换', normalizeMath('\\(a\\) 与 \\(b\\)') === '$a$ 与 $b$', normalizeMath('\\(a\\) 与 \\(b\\)'));
check('围栏代码块里的反斜杠**不动**（那是要展示的代码本身）',
      normalizeMath('```\n\\(x\\)\n```') === '```\n\\(x\\)\n```', normalizeMath('```\n\\(x\\)\n```'));
check('行内代码里的也不动', normalizeMath('`\\(x\\)`') === '`\\(x\\)`', normalizeMath('`\\(x\\)`'));
check('null / undefined 不会炸', normalizeMath(null) === '' && normalizeMath(undefined) === '', '');

/* ---------- renderRichEl：顺序与接线（用 stub 库，精确看它把什么喂给了谁） ---------- */
let seen = null, sawHtml = null, delims = null, errFlag = null, returned = null;
globalThis.window = {
  marked: { parse: (md) => { seen = md; return '<p>' + md + '</p>'; } },
  DOMPurify: { sanitize: (s) => { sawHtml = s; return s; } },
  renderMathInElement: (node, opt) => { delims = opt.delimiters; errFlag = opt.throwOnError; },
};
const el = { innerHTML: '', textContent: '' };
returned = renderRichEl(el, '设 $a>0$，求 \\(\\frac{a}{b}\\)');

check('★ 喂给 marked 的是**归一化之后**的文本（\(...\) 已经变成 $...$）',
      seen !== null && seen.indexOf('\\(') < 0 && seen.indexOf('$\\frac{a}{b}$') >= 0, String(seen));
check('★ 写的是 innerHTML，不是 textContent',
      el.innerHTML === '<p>' + seen + '</p>' && el.textContent === '', el.innerHTML);
check('渲染结果过了 DOMPurify（marked 自己不管消毒）', sawHtml === el.innerHTML, String(sawHtml));
check('renderMathInElement 被调用，且给了 4 个定界符（含 \( \) 与 \[ \]）',
      Array.isArray(delims) && delims.length === 4, JSON.stringify(delims));
check('公式渲染 throwOnError=false（错一条公式不该让整块内容消失）',
      errFlag === false, String(errFlag));
check('返回 true（表示真的用上了 marked）', returned === true, String(returned));

/* ---------- 退化路径：库没加载上也不能白屏 ---------- */
globalThis.window = {};                       // 一个库都没有
const el2 = { innerHTML: '', textContent: '' };
const r2 = renderRichEl(el2, '$x^2$');
check('marked 缺失 → 退纯文本，且给的是**原文**', r2 === false && el2.textContent === '$x^2$',
      JSON.stringify(el2));
const el3 = { innerHTML: '', textContent: '' };
check('空文本 → 清空容器、不算渲染成功', renderRichEl(el3, '') === false && el3.textContent === '', '');
check('容器为 null 不抛异常', renderRichEl(null, 'x') === false, '');

/* ---------- 只挂 marked、没有 KaTeX 时也不该炸 ---------- */
globalThis.window = { marked: { parse: (md) => md } };
const el4 = { innerHTML: '', textContent: '' };
check('只有 marked 没有 renderMathInElement 也能渲染（公式留原文）',
      renderRichEl(el4, 'a $b$ c') === true && el4.innerHTML === 'a $b$ c', el4.innerHTML);

console.log(`__MR_DONE__ ${pass + fail} ${fail}`);
"""


def t_math_render() -> None:
    section("⑨ renderRichEl 的接线与顺序（node 跑 mathRender.js，stub 掉三个库）")
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上")
        return
    src = FE / "src" / "mathRender.js"
    if not src.exists():
        check("mathRender.js 存在（不然谈不上接线）", False, str(src))
        return
    d = TMP / "mr"
    d.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, d / "mathrender.mjs")
    (d / "run.mjs").write_text(MATH_JS, encoding="utf-8")
    r = subprocess.run([node, str(d / "run.mjs")], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if "[OK ]" in line or "[FAIL]" in line:
            m = re.match(r"\s*\[(OK |FAIL)\]\s*(.+?)(?:\s+<- (.*))?$", line)
            if m:
                check(m.group(2).strip(), m.group(1) == "OK ", m.group(3) or "")
    tail = [l for l in out.splitlines() if l.startswith("__MR_DONE__")]
    check("node 侧断言全部跑完（进程没中途挂掉）", len(tail) == 1, out.strip()[-300:])
    if tail:
        n, bad = tail[0].split()[1:3]
        check(f"node 侧 {n} 项里 0 失败", bad == "0", f"失败 {bad} 项")


# ================================================================ ⑩ 真 KaTeX 端到端
KATEX_JS = r"""
/* 用**真的** vendor 库跑一遍渲染。
   ⑨ 段把三个库 stub 掉了（只看接线顺序），这一段回答的是另一个问题：
   「老师到底能不能看到排好版的公式」—— 所以必须上真库。
   四个 vendor 脚本内联进 jsdom 执行（内联就不受 jsdom 30 删掉 ResourceLoader 的影响），
   再把 mathRender.js 去掉 `export ` 前缀当普通脚本注入（只在临时目录里做，不动仓库文件）。 */
const fs = require('fs');
const path = require('path');

const FE = process.argv[2];
const PLAIN = process.argv[4];
const { JSDOM } = require(process.argv[3]);

let pass = 0, fail = 0;
function check(name, ok, extra) {
  if (ok) { pass++; console.log('  [OK ] ' + name); }
  else { fail++; console.log('  [FAIL] ' + name + (extra !== undefined ? '   <- ' + extra : '')); }
}

const dom = new JSDOM('<!doctype html><html><head></head><body><div id="c"></div></body></html>',
  { runScripts: 'dangerously', url: 'http://localhost/' });
const win = dom.window, doc = win.document;

function inline(file) {
  const s = doc.createElement('script');
  s.textContent = fs.readFileSync(file, 'utf8');
  doc.head.appendChild(s);
}

/* 顺序照 mathRender.js 的加载顺序：auto-render 必须在 katex 之后 */
inline(path.join(FE, 'vendor', 'marked.min.js'));
inline(path.join(FE, 'vendor', 'purify.min.js'));
inline(path.join(FE, 'vendor', 'katex', 'katex.min.js'));
inline(path.join(FE, 'vendor', 'katex', 'auto-render.min.js'));

check('marked / DOMPurify / katex / renderMathInElement 都挂上了 window',
      !!win.marked && !!win.DOMPurify && !!win.katex && !!win.renderMathInElement,
      [!!win.marked, !!win.DOMPurify, !!win.katex, !!win.renderMathInElement].join(','));

inline(PLAIN);
check('renderRichEl / normalizeMath 注入成功（顶层声明会挂到 window 上）',
      typeof win.renderRichEl === 'function' && typeof win.normalizeMath === 'function',
      typeof win.renderRichEl);

/* ---------- 核心断言：公式真的被渲染成 DOM 了 ---------- */
const el = doc.createElement('div');
const ok = win.renderRichEl(el, '设 $\\frac{a}{b}>0$，且 \\(x^{2}+1\\) 恒正');
check('renderRichEl 返回 true（真的用上了 marked）', ok === true, String(ok));
const katexCount = el.querySelectorAll('.katex').length;
check('★ 两处公式都排出来了（.katex 出现 2 次）', katexCount === 2, 'katex=' + katexCount);
check('★ \\(…\\) 这种写法也认（说明归一化真的生效了，不是只认 $）',
      el.innerHTML.indexOf('\\(x') < 0, el.innerHTML.slice(0, 120));
check('DOM 里不再残留 $ 定界符（残留 = 只做了文本替换，没真渲染）',
      el.textContent.indexOf('$') < 0, el.textContent.slice(0, 120));
check('KaTeX 的 MathML 输出在（无障碍/复制可用的那一份）',
      el.querySelectorAll('.katex-mathml').length === 2,
      String(el.querySelectorAll('.katex-mathml').length));

/* ---------- Markdown 与消毒也走通了 ---------- */
const el2 = doc.createElement('div');
win.renderRichEl(el2, '**粗体**\n\n- 甲\n- 乙');
check('Markdown 生效（** 变成 <strong>、- 变成 <li>）',
      !!el2.querySelector('strong') && el2.querySelectorAll('li').length === 2,
      el2.innerHTML.slice(0, 140));

const el3 = doc.createElement('div');
win.renderRichEl(el3, '正常文字 <img src=x onerror="window.__xss=1">');
check('★ DOMPurify 干掉了 onerror（题干里的不可信内容进不了 DOM）',
      el3.innerHTML.indexOf('onerror') < 0 && !win.__xss, el3.innerHTML.slice(0, 140));

/* ---------- 写错一条公式不该让整块内容消失 ---------- */
const el4 = doc.createElement('div');
win.renderRichEl(el4, '前半句 $\\frac{1}{$ 后半句');
check('公式写错也不抛异常、文字还在（throwOnError=false 兜住了）',
      el4.textContent.indexOf('前半句') >= 0 && el4.textContent.indexOf('后半句') >= 0,
      el4.textContent.slice(0, 120));

console.log(`__KX_DONE__ ${pass + fail} ${fail}`);
"""


def jsdom_dir() -> str | None:
    """jsdom 是可选依赖：按候选顺序找，找不到就 SKIP（别把回归弄红）。"""
    home = Path.home()
    cands = [
        ROOT / "node_modules" / "jsdom",
        home / ".workbuddy" / "binaries" / "node" / "workspace" / "node_modules" / "jsdom",
    ]
    for c in cands:
        if c.is_dir():
            return str(c)
    return None


def t_real_katex() -> None:
    section("⑩ 端到端：真 marked + 真 KaTeX 渲染一遍")
    node = node_bin()
    if not node:
        print("  [SKIP] node 不在 PATH 上")
        return
    jd = jsdom_dir()
    if not jd:
        print("  [SKIP] 没找到 jsdom（可选依赖）—— 装法：npm i jsdom")
        return

    mr = FE / "src" / "mathRender.js"
    d = TMP / "katex"
    d.mkdir(parents=True, exist_ok=True)
    # 去掉 ESM 的 `export ` 前缀，当普通脚本注入（只写临时目录，不动仓库文件）
    plain = d / "mathrender.plain.js"
    plain.write_text(re.sub(r"^export\s+", "", read(mr), flags=re.M), encoding="utf-8")

    js = d / "run.cjs"
    js.write_text(KATEX_JS, encoding="utf-8")
    r = subprocess.run([node, str(js), str(FE), jd, str(plain)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if "[OK ]" in line or "[FAIL]" in line:
            m = re.match(r"\s*\[(OK |FAIL)\]\s*(.+?)(?:\s+<- (.*))?$", line)
            if m:
                check(m.group(2).strip(), m.group(1) == "OK ", m.group(3) or "")
    tail = [l for l in out.splitlines() if l.startswith("__KX_DONE__")]
    check("node 侧断言全部跑完（进程没中途挂掉）", len(tail) == 1, out.strip()[-400:])
    if tail:
        n, bad = tail[0].split()[1:3]
        check(f"node 侧 {n} 项里 0 失败", bad == "0", f"失败 {bad} 项")


# ================================================================ main
def main() -> int:
    print("出卷页细节优化 · 自包含检查（不起服务、不碰真库）")
    t_pipeline()
    t_notes()
    t_papers_render()
    t_papers_tags()
    t_no_auto_doc()
    t_css()
    t_template_compile()
    t_tag_filter()
    t_math_render()
    t_real_katex()
    print(f"\n共 {total} 项，失败 {len(fails)} 项")
    if fails:
        print("失败项：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
