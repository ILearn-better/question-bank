# -*- coding: utf-8 -*-
"""批量入库页（frontend/batch.html）的静态自检。

    cd backend && ./.venv/Scripts/python.exe test_batch_page.py

与 test_entry_page.py 分工：那个管录题页 entry.html 与出卷页 Papers.js，
这个管批量页 batch.html 与侧栏导航（App.js）。

**为什么值得有**：batch.html 是零构建的手写前端，没有打包器挡语法错误 ——
改错一个括号或一个中文标点，整页白掉，而所有接口测试还是全绿。
这里用 node --check + 标签配对补上这道闸，跑一次不到 1 秒。

不联网、不起服务、不写项目里的任何文件（临时文件落系统临时目录）。
node 不在 PATH 时只跳过语法检查，结构检查照跑。

⑤ 是 in-DOM 模板守卫：entry.html / batch.html 是**没有构建步骤**的 HTML 文件，
它们的根模板由浏览器 HTML 解析器先解析、Vue 再编译那棵 DOM。这条路上有两个坑
（2026-10-05 真的踩了，整页白屏，接口测试全绿）：

  1. `<el-alert ... />` 这种**自闭合自定义组件在 HTML 里并不自闭合** ——
     HTML 解析器对自定义元素忽略结尾的 `/`，把它当普通开标签，
     于是后面的兄弟节点全被吞成它的子节点。
  2. 上面这一吞，`v-else` / `v-else-if` 就找不到相邻的 `v-if`，
     Vue 抛 compiler-30，`mount` 直接不执行，页面永远停在空 `<div id="app">`。

（放在单文件里的字符串模板没这问题：那条路走 Vue 自己的解析器，`/>` 是认的。
  所以 `frontend/src/views/*.js` 不受影响，不用查。）

⑥ 是本轮（2026-10-05 深夜）三个使用反馈的回归防线：

  · 「点哪就在哪画线」——点击只加线，不再按距离误删；吸附不再把线拽到页顶
  · 「换文档读不到页面信息」——逐页容错 + 失败重置状态 + 全局错误兜底
  · 「一份 46 页的 PPT 型 PDF 被自动铺出 118 条线把页面拖死」——自动铺线设上限

  这些全是**跑接口发现不了**的：接口全 200，页面却卡死或白屏。
  所以除了静态断言，isQNumLine 还直接拿真实用例在 node 里跑 ——
  正则是这次最容易改错的地方（改一版就放过一批小数/行内编号）。

⑦ 是两条使用反馈（同一晚，更晚）的防线，也都是「接口全绿但用户办不成事」：

  · 「两个分割线要自动成截图才行」—— 机制其实一直在跑（点一次线就+1块），
    缺的是**肉眼凭据**：右栏每块只有「约 x% 版面」，切歪了看不出来。
    → 因而给每块算一个 /region-image 预览地址（服务端现渲染、**不落盘**）。
  · 「带图像的题目，图像没有录入」—— 题块原貌图是整道题，出卷走文本形态时整张不印，
    题干里那句「如图」的图就没了。
    → 因而 AI 先判 needs_figure，老师再勾一次（人工兜底）、手工框出那一幅。

  这两处的关键点都容易被「顺手改回去」：缩略图改回 fetch 落盘就攒垃圾、
  提醒改成自动判定就丢了人工兜底 —— 所以钉在这里。

  ③（2026-10-07 深夜追加）「框选配图的框跟鼠标选的对不齐，且框出来的内容不是那块」。
    根因是**几何基准**：`.fig-pick-page` 既是定位容器又被当成滚动容器
    （max-height:62vh），padding-box 高度 ≠ 图片高度，于是鼠标换算与百分比定位
    各自按错的高度算。修法照抄左栏 .page-box（滚动在外层 + aspect-ratio 定高）。
    ⚠️ 这类 bug 的原有断言（「用了 pg.width/pg.height」）必然全绿 ——
       所以这里断言的是**结构不变式**：滚动不许挂在定位容器上、高度必须由
       aspect-ratio 来、图片必须绝对定位填满。改 CSS 时先看这几条。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATCH = ROOT / "frontend" / "batch.html"
APPJS = ROOT / "frontend" / "src" / "App.js"
ENTRY = ROOT / "frontend" / "entry.html"
# ⑦ 要跨到后端对一眼路由名：前端拼了地址、后端却没这个接口，是最省事的漏法
R_DOCUMENTS = ROOT / "backend" / "app" / "routers" / "documents.py"
R_BATCHES = ROOT / "backend" / "app" / "routers" / "batches.py"
S_TAXONOMY = ROOT / "backend" / "app" / "taxonomy.py"

fails: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- in-DOM 守卫

# HTML 里真正的空元素：结尾的 `/` 合法且无意义
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

# 一个够用的 HTML 分词器。
# 属性段用「非引号字符 | 整段引号」交替，避免被 `accept=".pdf,.docx"` 里的 `>` 或
# `/` 带偏；裸 `/` 只在**后面不是 `>`** 时才允许（`href=/foo` 里的那个），
# 否则结尾的 `/>` 会被属性段整段吃掉，自闭合就永远检不出来。
TOKEN_RE = re.compile(
    r"(?P<comment><!--.*?-->)"
    r"|(?P<end></\s*(?P<ename>[A-Za-z][\w:.-]*)\s*>)"
    r"|(?P<start><\s*(?P<sname>[A-Za-z][\w:.-]*)"
    r"(?P<attrs>(?:[^>\"'/]|/(?!>)|\"[^\"]*\"|'[^']*')*)"
    r"(?P<slash>/?)>)",
    re.S,
)


def strip_noise(html: str) -> str:
    """去掉注释、<script>、<style> —— 只留真正的标记文本。

    必须先去 <script>：里面的 JS 字符串带着成片的 `<div ...></div>`，
    留着会被分词器当成真标签，树就建歪了。
    """
    html = re.sub(r"<!--[\s\S]*?-->", "", html)
    html = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", html, flags=re.I)
    html = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", html, flags=re.I)
    return html


def parse_tree(html: str) -> dict:
    """把标记文本建成一棵浅树。不追求容错，够用就行。

    **刻意模拟浏览器**：非空元素上的结尾 `/` 一律忽略（`<el-alert ... />`
    照样当开标签压栈），这正是 HTML 解析器的行为，也是白屏的成因。
    如果想「按 Vue 的字符串模板语义」解析（认 `/>`），就复现不出这个 bug 了。
    """
    root = {"tag": "#root", "attrs": "", "children": []}
    stack = [root]
    for m in TOKEN_RE.finditer(html):
        if m.group("comment"):
            continue
        if m.group("ename"):
            name = m.group("ename").lower()
            for i in range(len(stack) - 1, 0, -1):
                if stack[i]["tag"] == name:
                    del stack[i:]
                    break
            continue
        name = m.group("sname").lower()
        node = {"tag": name, "attrs": m.group("attrs") or "", "children": []}
        stack[-1]["children"].append(node)
        # 只有 HTML 空元素才是真自闭合；其余（含 el-xxx）会被浏览器当成开标签
        if name not in VOID_TAGS:
            stack.append(node)
    return root


def find_by_id(node: dict, wanted: str):
    if re.search(rf"""\bid\s*=\s*["']{re.escape(wanted)}["']""", node["attrs"]):
        return node
    for ch in node["children"]:
        hit = find_by_id(ch, wanted)
        if hit is not None:
            return hit
    return None


def has_directive(attrs: str, name: str) -> bool:
    """精确匹配指令名，`v-else` 不能命中 `v-else-if`。"""
    return re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", attrs, re.I) is not None


def selfclosing_custom(html: str) -> list[tuple[int, str]]:
    """返回 [(行号, 标签名)]：自定义元素被写成自闭合的每一处。"""
    out: list[tuple[int, str]] = []
    for m in TOKEN_RE.finditer(html):
        if not m.group("slash") or not m.group("sname"):
            continue
        name = m.group("sname").lower()
        if name not in VOID_TAGS and "-" in name:
            out.append((html[: m.start()].count("\n") + 1, name))
    return out


def orphan_else(root: dict) -> list[str]:
    """返回 v-else / v-else-if 没有相邻 v-if 的每一处（含父节点，便于定位）。"""
    bad: list[str] = []

    def walk(node: dict, parents: list[str]) -> None:
        kids = node["children"]
        for i, ch in enumerate(kids):
            a = ch["attrs"]
            if has_directive(a, "v-else") or has_directive(a, "v-else-if"):
                which = "v-else" if has_directive(a, "v-else") else "v-else-if"
                prev = kids[i - 1] if i else None
                ok = prev is not None and (
                    has_directive(prev["attrs"], "v-if")
                    or has_directive(prev["attrs"], "v-else-if")
                )
                if not ok:
                    where = " > ".join(parents[-3:]) or "#root"
                    bad.append(
                        f"<{ch['tag']} {which}> 无相邻 v-if"
                        f"（兄弟前一个是 {('<%s>' % prev['tag']) if prev else '无'}，父链 {where}）"
                    )
            walk(ch, parents + [ch["tag"]])

    walk(root, [])
    return bad


def guard_in_dom(name: str, path: Path) -> None:
    """一个 HTML 页面的两条守卫：无自闭合自定义组件、无孤儿 v-else。"""
    raw = path.read_text(encoding="utf-8")
    clean = strip_noise(raw)

    sc = selfclosing_custom(clean)
    check(f"{name}：自定义组件不写自闭合 `/>`", not sc,
          "" if not sc else "→ " + "；".join(f"第 {ln} 行 <{t} />" for ln, t in sc))

    tree = parse_tree(clean)
    app = find_by_id(tree, "app")
    if app is None:
        check(f"{name}：找到 #app 根节点", False)
        return
    orph = orphan_else(app)
    check(f"{name}：v-else 都有相邻 v-if", not orph,
          "" if not orph else "→ " + "；".join(orph[:4]))


def js_syntax(code: str, label: str) -> None:
    if not shutil.which("node"):
        print(f"  [skip] {label}：没找到 node，跳过语法检查")
        return
    dst = Path(tempfile.gettempdir()) / (re.sub(r"\W+", "_", label) + ".js")
    dst.write_text(code, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(dst)], capture_output=True, text=True)
    check(f"{label} JS 语法", r.returncode == 0)
    if r.returncode:
        print("        " + (r.stderr or r.stdout).strip().replace("\n", "\n        ")[:800])


# ---------------------------------------------------------------- ⑥ 行为回归
def method_body(src: str, name: str) -> str:
    """抠出一个 `name(...) { ... }` 的方法体。

    够用就好 —— 这几个方法里没有「4 空格缩进的 `},`」这种结构，
    按「行首 4 空格收尾的 `},`」找结尾是稳的。
    """
    m = re.search(rf"\n    (?:async )?{name}\([^)]*\) \{{([\s\S]*?)\n    \}},", src)
    return m.group(1) if m else ""


def top_decl(src: str, name: str) -> str:
    """抠出一个**顶层**声明（`function name(...) { ... }` 或 `const name = [...];`）。

    ⑨ 段只抽 4 空格缩进的方法体；applyItems 依赖的顶层纯函数/常量得单独取 ——
    在测试里重抄一份，测的就成了测试自己的副本，等于没测。
    """
    m = re.search(rf"\n(function {name}\([^)]*\) \{{[\s\S]*?\n\}})", src)
    if m:
        return m.group(1)
    m = re.search(rf"\n(const {name} = \[[\s\S]*?\];)", src)
    return m.group(1) if m else ""


def css_rule(src: str, selector: str) -> str:
    """抠出一段 CSS 规则体 `selector { ... }`（不含嵌套块，够用）。"""
    m = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", src)
    return m.group(1) if m else ""


# isQNumLine 的用例：(行文本, 应该是题号吗)。
# 右列每个 False 都对应一次真实的误判 —— 别随手删。
QNUM_CASES: list[tuple[str, bool]] = [
    ("1. 已知函数 f(x)=x^2+2x-3，求零点。", True),
    ("2.（12 分）已知等差数列 {a_n}", True),
    ("第 3 题 求下列各式的值", True),
    ("(1) 求函数的最小值", True),
    ("10. 帆船比赛中，运动员可借助风力计测定风速", True),
    ("1.6~3.3", False),        # 高考卷风速对照表里的数值区间（实测误判过）
    ("0.778", False),          # 纯数据行（那页 PPT 型 PDF 里全是这种）
    ("1.", False),             # 孤零零一个题号 —— 多半是页码
    ("160(1)", False),         # 行内的 (1)，不是题号
    ("Expert Systems with Applications 160(1)", False),
    ("参考文献 [12] 张某某", False),
    ("", False),
    ("   ", False),
]


def run_qnum_cases(src: str) -> None:
    """把 batch.html 里 isQNumLine 的**真实实现**抠出来，在 node 里跑用例。

    为什么不照抄一份到 Python：抄的那份永远是「对的」——
    正则改错了它照样通过，测试就成了自我安慰。这里跑的就是页面上跑的那段代码。
    """
    m = re.search(r"function isQNumLine\(text\) \{[\s\S]*?\n\}", src)
    if not m:
        check("能从 batch.html 抠出 isQNumLine", False)
        return
    node = shutil.which("node")
    if not node:
        print("  [skip] isQNumLine 用例：没找到 node")
        return
    js = (
        m.group(0) + "\n"
        + "const CASES = " + json.dumps(QNUM_CASES, ensure_ascii=False) + ";\n"
        + "const bad = [];\n"
        + "for (const [t, want] of CASES) {\n"
        + "  const got = !!isQNumLine(t);\n"
        + "  if (got !== want) bad.push([t, want, got]);\n"
        + "}\n"
        + "console.log(JSON.stringify(bad));\n"
    )
    dst = Path(tempfile.gettempdir()) / "batch_qnum_cases.js"
    dst.write_text(js, encoding="utf-8")
    r = subprocess.run([node, str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        check("isQNumLine 用例能跑起来", False, (r.stderr or r.stdout or "")[:200])
        return
    lines = (r.stdout or "").strip().splitlines()
    try:
        bad = json.loads(lines[-1]) if lines else []
    except ValueError:
        check("isQNumLine 用例输出可解析", False, (r.stdout or "")[:200])
        return
    check(f"isQNumLine 判定 {len(QNUM_CASES)} 条用例全对", not bad,
          "" if not bad else "→ " + "；".join(
              f"{t!r} 期望 {w} 实得 {g}" for t, w, g in bad[:4]))


def check_feedback_fixes(src: str) -> None:
    """本轮三个使用反馈的修法，写成静态断言 —— 防止以后不经意改回去。"""
    down = method_body(src, "onPageDown")
    snap = method_body(src, "snapY")
    load = method_body(src, "loadDoc")
    seg = method_body(src, "autoSegment")
    topdf = method_body(src, "toPdfXY")

    check("抠到 onPageDown / snapY / loadDoc / autoSegment / toPdfXY",
          all([down, snap, load, seg, topdf]))

    # —— ①画线精度：点击只加线，删除只走线上的 × ——
    check("画线：点击不再「点在附近就删」（body 里没有 nearDist）", "nearDist" not in down)
    check("画线：onPageDown 里不再删线（没有 splice）", "splice" not in down)
    check("画线：落点先取鼠标位置（let ty = p.y）", "let ty = p.y" in down)
    check("画线：吸附只在限距内生效（SNAP_LIMIT）", "SNAP_LIMIT" in down)
    check("吸附：页顶/页底不再是候选",
          "[0, pg.height]" not in snap and "pg.height]" not in snap)
    check("吸附：行数不足就不吸（lines.length < 2）", "lines.length < 2" in snap)

    # —— ③换文档不再「读不到页面信息」并把页面弄死 ——
    check("换文档：先清空 pv 与旧状态", "this.pv = { count: 0, pages: {} }" in load)
    check("换文档：逐页容错（单页失败不拖垮整份）",
          ".catch(() => ({ ok: false }))" in load)
    check("换文档：区分「打不开」与「画不了页面」",
          "打不开这份文档" in load and "画不了页面" in load)
    check("换文档：Word 未生成页面图时给指引，不硬等",
          "pv.engine === 'word'" in load)
    check("坐标换算：几何没准备好就返回 null",
          "if (!box || !pg" in topdf and "return null" in topdf)

    # —— ②页面不再一次全渲染 ——
    check("懒加载：有 IntersectionObserver + loadedPages",
          "IntersectionObserver" in src and "loadedPages" in src)
    check("懒加载：不支持的浏览器退回全量（功能优先）",
          "typeof IntersectionObserver === 'undefined'" in src)
    check("懒加载：模板里图片受 loadedPages 控制",
          'v-if="loadedPages[pno]"' in src)
    check("懒加载：盒子用 aspect-ratio 定高（图没到也不跳）", "aspectRatio" in src)

    # —— 自动铺线护栏 ——
    check("护栏：有总量上限 AUTO_MAX_TOTAL", "AUTO_MAX_TOTAL" in src)
    check("护栏：silent 自动跑时超量就跳过", "AUTO_MAX_TOTAL" in seg and "if (silent)" in seg)
    check("护栏：提交前块数上限 SUBMIT_MAX", "SUBMIT_MAX" in src)
    check("护栏：整页未分割的页会被标出来",
          "unsplitPageCount" in src and "unsplit" in src)

    # —— 全局兜底 ——
    check("兜底：装了 app.config.errorHandler", "app.config.errorHandler" in src)
    check("兜底：监听 window error 与 unhandledrejection",
          "addEventListener('error'" in src and "unhandledrejection" in src)


# ---------------------------------------------------------------- ⑦ 缩略图 / 配图
def preview_not_persisted(src: str) -> None:
    """缩略图必须**服务端现渲染 + 前端只当 <img src>**，中间不落盘、不经 JS 取回。

    为什么专门钉这条：crops/ 是**资产**目录（每张图都有数据库行引用，删除靠引用计数）。
    预览图一旦落盘就攒成没人引用的垃圾 —— 实测踩过：题库 0 行、盘上 92 张图。
    所以 previewUrl 必须是纯字符串拼装，不能是 `this.api(...)` 那一类
    「先取回来再塞进 img」的写法（那样也丢失了浏览器的缓存/懒加载/并发控制）。
    """
    prev = method_body(src, "previewUrl")
    check("预览：previewUrl 是纯拼地址（没有 this.api 调用 → 不走取回再塞的路径）",
          bool(prev) and "this.api(" not in prev)
    check("预览：地址只出现在 <img src> 那一种用法（没有别处去 POST 它）",
          src.count("region-image") == 3 and "'/region-image'" not in src,
          f"region-image 出现 {src.count('region-image')} 次（预期 3：两处注释 + 一处拼地址）")


def check_figure_and_thumb(src: str) -> None:
    prev = method_body(src, "previewUrl")
    save = method_body(src, "saveFigure")
    warn = method_body(src, "figureWarn")
    clr = method_body(src, "clearFigure")
    pick = method_body(src, "openFigurePick")
    turn = method_body(src, "figPickTurn")
    down = method_body(src, "figDown")
    style = method_body(src, "figRectStyle")
    spick = method_body(src, "saveFigurePick")
    figsty = method_body(src, "figPageStyle")

    check("抠到配图相关的 10 个方法",
          all([prev, save, warn, clr, pick, turn, down, style, spick, figsty]))

    # —— 问题2：两条线之间切成了什么，得看得见 ——
    check("缩略图：拼的是 region-image，带 zoom 与 spec", "region-image" in prev
          and "zoom=" in prev and "spec=" in prev)
    check("缩略图：多段（跨页）全带上，服务端竖拼", "join(';')" in prev)
    check("缩略图：没有页面视图就不拼地址（不发注定失败的请求）",
          "!this.pv.count" in prev and "return ''" in prev)
    check("缩略图：模板里是 <img>，且 lazy 加载 + 点击放大",
          'class="blk-thumb"' in src and 'loading="lazy"' in src
          and '@click="zoom(b.thumb || b.preview)"' in src)
    check("缩略图：提交后用正式原貌图顶掉预览（thumb 优先）",
          'b.thumb || b.preview' in src)
    check("缩略图：rebuild 里给每块算好预览地址",
          "b.preview = this.previewUrl(b.regions)" in src)
    check("缩略图：样式在，且提示可点开", ".blk-thumb {" in src and "zoom-in" in src)

    # —— 问题1：AI 初判 + 人工勾选 + 手工框选 ——
    check("配图：勾选框绑 needs_figure（这就是「人工判断」的落点）",
          'v-model="it.needs_figure"' in src)
    check("配图：勾一下立刻落库（先记「动过」再落库，见 figToggle）",
          '@change="figToggle(it)"' in src)
    check("配图：saveFigure 只写 needs_figure，不顺手把别的字段覆盖了",
          "JSON.stringify({ needs_figure: !!it.needs_figure })" in save)
    check("配图：已入库的条目不能再框图", "it.status === 'approved'" in src)
    check("配图：模型那句「这图是什么」会显示出来", "it.figure_note" in src)
    check("配图：框过就显示图，没框但勾了就给「怎么补」的指引",
          'class="fig-thumb"' in src and "还没框图" in src)
    check("配图：撤掉走 PATCH figure_image 空串（服务端按引用计数回收）",
          "figure_image: ''" in clr)
    check("配图：撤掉前先确认（图可能被真删）", "ElMessageBox.confirm" in clr)

    # 人工兜底的两种「不静默通过」
    check("人工兜底：题干命中「如图」这类词却没勾有图 → 提醒复核",
          "figure_keywords" in warn and "请确认" in warn)
    check("人工兜底：模型没判出来 → 提醒自行确认", "figure_unknown" in warn)
    check("人工兜底：判不错时不出提醒（不刷屏）",
          "if (it.needs_figure || it.status === 'approved') return ''" in warn)
    check("人工兜底：flag 有中文文案", "figure_unknown: '有没有图没判出来'" in src)
    check("配图：关键词表来自后端（不前端写死）", "options.figure_keywords" in src)

    # 框选弹窗
    check("框选：弹窗绑定 figPick.show", 'v-model="figPick.show"' in src)
    check("框选：整页图上拖拽取矩形", '@pointerdown="figDown($event)"' in src)
    check("框选：可翻页（图在别的页）", "figPickTurn(-1)" in src and "figPickTurn(1)" in src)
    check("框选：翻页丢掉未完成的草稿", "draft = null" in turn)
    check("框选：已框段数可见，多段会说明「竖着拼成一张」",
          "figPick.rects.length" in src and "拼成一张" in src)
    check("框选：能清空重框", "figPick.rects = []" in src)
    check("框选：拖拽中画出虚线草稿", 'class="fig-rect draft"' in src)
    check("框选：拖到图外也跟得住（move/up 挂在 window 上）",
          "window.addEventListener('pointermove'" in down
          and "window.addEventListener('pointerup'" in down)
    check("框选：抬手时把两个监听都摘掉（不泄漏）",
          down.count("removeEventListener") == 2)
    check("框选：坐标按页宽高换算（不是屏幕像素）",
          "pg.width" in down and "pg.height" in down)
    check("框选：两个方向都要够大才算一次框（误点不算）",
          "pg.width * 0.03" in down and "pg.height * 0.02" in down)
    check("框选：矩形用百分比定位（缩放窗口不跑偏）",
          "* 100) + '%'" in style)
    check("框选：没页面视图就给指引而不是硬框",
          "没有页面视图" in pick)
    check("框选：打开时落在该题所在页", "it.page_no" in pick)
    check("框选：保存走 figure-crop，保存中禁用按钮",
          "/figure-crop" in spick and "figPick.saving" in src)

    # —— 框选几何：定位容器必须与图片严格同尺寸（2026-10-07 深夜反馈）——
    #    「框和鼠标选的对不齐，而且框出来的内容根本不是那一块」。
    #    根因：把 max-height/overflow 加在了**定位容器自己**身上 → 它的 padding-box
    #    高度变成 62vh（可见高度），而图片是整页高度（880px 宽下 A4 约 1188px）。
    #    于是两处一起歪：① figDown 用容器 rect 换算鼠标坐标且不含 scrollTop；
    #    ② figRectStyle 的百分比 top 按容器高度解析。
    #    修法与左栏 .page-box 一致：滚动放外层、内层用 aspect-ratio 定高、图片绝对定位填满。
    #    ⚠️ 原有的「坐标按页宽高换算」断言只能证明「用了 pg.width/pg.height」，
    #       对「基准是不是图片」完全没有约束 —— 正是这种全绿但页面不能用的漏网方式。
    page_rule = css_rule(src, ".fig-pick-page")
    scroll_rule = css_rule(src, ".fig-pick-scroll")
    img_rule = css_rule(src, ".fig-pick-page img")
    fs = method_body(src, "figPageStyle")
    check("框选几何：抠到 .fig-pick-page / .fig-pick-scroll / img 三段样式 + figPageStyle",
          all([page_rule, scroll_rule, img_rule, fs]))
    check("框选几何：滚动挂在外层 wrapper 上（max-height + overflow）",
          "max-height" in scroll_rule and "overflow" in scroll_rule)
    check("★ 框选几何：定位容器自己**不**是滚动容器，也没有 border/padding",
          "max-height" not in page_rule and "overflow" not in page_rule
          and "border" not in page_rule and "padding" not in page_rule)
    check("框选几何：定位容器高度由 aspect-ratio 定死（= 图片高度）",
          "aspectRatio" in fs)
    check("框选几何：几何缺失时给 A4 兜底（返回 {} 会让容器塌成 0 高、图看不见）",
          "'595 / 842'" in fs)
    check("框选几何：图片绝对定位填满容器（与左栏 .page-box 同一套）",
          "position: absolute" in img_rule and "inset: 0" in img_rule)
    check("框选几何：模板里容器包在 .fig-pick-scroll 内、且挂 figPageStyle",
          'class="fig-pick-scroll"' in src
          and 'class="fig-pick-page" :style="figPageStyle()"' in src)
    check("框选几何：鼠标换算的基准就是那个定位容器（同一个 rect）",
          "e.currentTarget" in down and "getBoundingClientRect" in down)
    check("框选：拖到图外夹回页内（不把越界坐标发给服务端）",
          "Math.max(0, Math.min(pg.width" in down and "Math.min(pg.height" in down)

    # —— 与轮询/保存的配合（2026-10-08 改成「动过没动过」判定，详见 ⑨ 段）——
    rf = review_fields(src)
    check("轮询：needs_figure 在可编辑清单里（老师刚勾上，轮询不能打回）",
          "'needs_figure'" in rf)
    check("轮询：figure_image **不**在可编辑清单里（否则覆盖刚框好的图）",
          "figure_image" not in rf)
    check("保存：saveItem 一并带上 needs_figure",
          "needs_figure: !!it.needs_figure" in method_body(src, "saveItem"))

    # —— 前后端对一眼：名字改了要一起改 ——
    docs = R_DOCUMENTS.read_text(encoding="utf-8")
    bts = R_BATCHES.read_text(encoding="utf-8")
    tax = S_TAXONOMY.read_text(encoding="utf-8")
    check("对齐：后端确有 region-image 路由", "region-image" in docs)
    check("对齐：后端确有 figure-crop 路由", "figure-crop" in bts)
    check("对齐：批量页调的正是 /api/batches/items/{id}/figure-crop",
          "/api/batches/items/' + it.id + '/figure-crop'" in src)
    check("对齐：options 里确实吐出 figure_keywords",
          '"figure_keywords"' in bts)
    check("对齐：关键词表就在 taxonomy 里（取值唯一出处）",
          "FIGURE_KEYWORDS" in tax)


# ---------------------------------------------------------------- ⑧ 起始线语义 + 自由排序合并
def check_startline_merge(src: str) -> None:
    """2026-10-07 两轮反馈的回归防线：

    · 「清空分界线再删块后画线没反应」—— 根因是删「整页一块」时把 cutHead 抬到页底，
      之后画的线全被「起点=页底」过滤掉。现在改成 skips 标记整页跳过，画线即恢复。
    · 「跨页合并有大间隔、截到页码」—— 跨页题改用「框选」自由截取每段，服务端 gap=0 无缝拼接。
    · 第二轮（同日深夜）：「框选好的两段中间总隔着别的块，合不了」＋「要能自由调块顺序」＋
      「每页最后一个分割线不用向下选取」。→ 引入 groups **编排表**（顺序由拖拽决定、
      rebuild 绝不按位置重排）、mergePrev 只认列表上一块、末线以下不再自动成块。
    """
    band = method_body(src, "bandsOf")
    rm = method_body(src, "removeBlock")
    mg = method_body(src, "mergePrev")
    rb = method_body(src, "rebuild")
    tail = method_body(src, "tailBand")

    check("抠到 bandsOf / removeBlock / mergePrev / rebuild / tailBand",
          all([band, rm, mg, rb, tail]))

    # —— 起始线语义：第一条线以上自动跳过，不再有 cutHead 起点概念 ——
    check("起始线：bandsOf 里不再出现 cutHead（卷头不再靠手动删）", "cutHead" not in band)
    check("起始线：无分界线时整页一块、标 unsplit", "unsplit" in band and "y0: 0" in band)
    check("起始线：两线之间成块（遍历 ds）", "ds.length - 1" in band)
    # —— 末线以下**不再**自动成块（免得把页脚、页码带进图里）——
    check("末线：bandsOf 不再生成「末线→页底」那一块",
          "pg.height - ds[ds.length - 1]" not in band)
    check("末线：tailBand 只出虚线提示、不产题块",
          "return { y0: last, y1: pg.height }" in tail and "skips[pno]" in tail)

    # —— 删整页块不再把起点抬到页底（bug 修复）——
    check("删块：整页一块改为 skips 标记（不再 cutHead=页底）",
          "skips" in rm and "cutHead" not in rm)
    check("删块：整页跳过时不再把起点抬到页底", "cutHead" not in rm and "skips[" in rm)
    check("恢复本页：restorePage 清掉 skips", "restorePage" in src and "delete this.skips[pno]" in src)

    # —— 编排表：块顺序由用户决定，rebuild 不按位置重排 ——
    check("编排：rebuild 写回 this.groups（源头是编排表，blocks 是产物）",
          "this.groups = groups" in rb)
    check("编排：新段插到锚点块的后面（不覆盖已拖过的顺序）",
          "groups.splice(gi + 1, 0" in rb)
    check("编排：块带 keys（认块、删块都靠它，不再用 rGids）", "keys: g.slice()" in rb)
    check("排序：shiftBlock 支持上移/下移", "shiftBlock(i, d)" in src)
    check("排序：拖拽 dropOn 改的是 groups 顺序",
          "dropOn" in src and "this.groups.splice(from, 1)[0]" in src)
    check("排序：拖拽事件挂在卡片上（draggable + dragstart/over/drop）",
          'draggable="true"' in src
          and '@dragstart="dragStart(i, $event)"' in src
          and '@drop.prevent="dropOn(i)"' in src)

    # —— 合并：只认列表上一块（把两块拖到相邻再合），不再受相邻/来源限制 ——
    check("合并：mergePrev 拼 groups 的相邻两块",
          "this.groups[i - 1].concat(this.groups[i])" in mg)
    check("合并：不再限制来源（线块也能合，跨页框选不再被挡）",
          "cur.source !== 'rect'" not in mg)
    # 旧机制的三处**实际用法**都不该再出现（注释里提一句历史不算）
    check("合并：joinPrev 一件套已彻底移除",
          not any(t in src for t in ("joinPrev: new Set()", "joinPrev.add", "joinPrev.has")))

    # —— 「不提交」：卷面结构说明（「四、解答题；本题共 5 小题，共 77 分」）不送 AI ——
    pb = method_body(src, "blockPayload")
    sub = method_body(src, "submitAll")
    tg = method_body(src, "toggleSkip")
    check("抠到 toggleSkip / blockPayload / submitAll", all([tg, pb, sub]))
    # 标记必须挂**段 key** —— 挂 blocks 下标会被下一次 rebuild 冲掉（blocks 是产物）
    check("不提交：状态是 skipKeys（段 key 为键），不是 blocks 下标",
          "skipKeys: {}" in src and "this.skipKeys[k]" in rb)
    check("不提交：rebuild 里 blocks 带 skipped（合并的段任一被标 → 整块不提交）",
          "skipped: g.some(k => !!this.skipKeys[k])" in rb)
    check("不提交：rebuild 顺手清掉已消失段的标记（不会越攒越多）", "this.skipKeys = sk" in rb)
    check("不提交：切换时整组段一起标/一起撤", "g.forEach((k) => { if (on) sk[k] = true; else delete sk[k]; })" in tg)
    # 删块要**显式**清标记：线被删后段会以新身份重建（如退回 unsplit），
    # 段 key 若恰好相同，旧标记会让新块一出现就是灰的。
    check("不提交：删块时显式清掉该组的标记", "g.forEach((k) => { delete sk[k]; })" in rm)
    # 不提交的块**连裁图都不发** —— 发了会在 crops/ 里攒下没人引用的图
    check("不提交：blockPayload 直接不发票了的块（连带不裁图）", "if (b.skipped) return;" in pb)
    check("不提交：提交前先滤出 todo，全被排除时给提示而不是发空任务",
          "const todo = this.blocks.filter(b => !b.skipped)" in sub
          and "没有要提交的题块" in sub)
    # 发出去的数组下标 ≠ blocks 下标（滤过一层）→ 回填必须按 todo 映射
    check("不提交：裁图回填按 todo[r.index] 映射（不能当 blocks 下标用）",
          "todo[r.index]" in sub and "this.blocks[r.index]" not in sub)
    check("不提交：按钮数字与可点状态都看 submitCount",
          '识别这 {{ submitCount }} 块' in src and ':disabled="!submitCount || !curriculumId"' in src)
    check("不提交：卡片上有开关（toggleSkip）", '@click="toggleSkip(i)"' in src)

    # —— 后端 gap 默认 0（无缝拼接）——
    schemas = (ROOT / "backend" / "app" / "schemas.py").read_text(encoding="utf-8")
    pdf = (ROOT / "backend" / "app" / "adapters" / "pdf.py").read_text(encoding="utf-8")
    check("无缝拼接：schema 里 gap 默认 0（不再是 14）",
          "gap: int = 14" not in schemas and "gap: int = 0" in schemas)
    check("无缝拼接：pdf.py 的 crop/render 默认 gap=0",
          "gap: int = 0" in pdf and "gap: int = 14" not in pdf)


# ------------------------------------------------- ⑨ 待审：模型输出同步进文本框
def review_fields(src: str) -> str:
    """抠出 REVIEW_FIELDS 的取值清单（方括号里的内容）。"""
    m = re.search(r"const REVIEW_FIELDS = \[([\s\S]*?)\];", src)
    return m.group(1) if m else ""


def check_review_sync(src: str) -> None:
    """2026-10-08 反馈：「大模型的输出没办法快速同步放到文本框中」。

    **根因不在接口**（后端 `_item_dict` 每次都返回最新内容），在**前端轮询的合并**：
    旧写法把可编辑字段**一律保留本地旧值**，而提交后第一次轮询（1.5 秒）识别还没回来、
    那时本地存下的是**空串** → 之后每一轮都把这个空串保住 →
    模型输出永远进不了文本框，只有从「历史任务」重新点开该任务才看得到
    （新列表没有旧值可保留，所以那一次是好的 —— 这个「有时好有时坏」正是它难查的地方）。

    修法（两条一起）：
    · 字段按**「老师动过没动过」**取舍：没动过 → 以服务端为准（输出自动进文本框）；
      动过 → 保留本地（不吞正在敲的字）。
    · 再加一个手动「同步识别结果」按钮，兜住**轮询已经停了**的场景
      （任务跑完 / 出错都会 stopPoll）。语义刻意保守：只补空，写过的绝不覆盖。
    """
    ap = method_body(src, "applyItems")
    load = method_body(src, "loadItems")
    sync = method_body(src, "syncModel")
    mark = method_body(src, "markEdited")
    ft = method_body(src, "figToggle")
    save = method_body(src, "saveItem")

    check("抠到 applyItems / loadItems / syncModel / markEdited / figToggle / saveItem",
          all([ap, load, sync, mark, ft, save]))

    # —— 根因：绝不能「可编辑字段一律保留本地旧值」——
    check("同步：没动过的字段以服务端为准（模型输出进得来）", "if (!ed[f]) merged[f] = n[f]" in ap)
    check("同步：动过的字段保留本地（轮询不吞正在敲的字）", "merged[f] = old[f]" in ap)
    check("同步：手动同步时，动过但为空的那格才用服务端补（只补空）",
          "fillEmptyOnly && isBlank(old[f])" in ap)
    check("同步：可编辑字段集中在 REVIEW_FIELDS（不散落在各处）",
          "REVIEW_FIELDS = [" in src and "REVIEW_FIELDS.forEach" in ap)
    check("同步：figure_image 不在清单里（它只由服务端写）",
          "figure_image" not in review_fields(src))
    check("同步：loadItems 复用同一份合并（不另写一套保字段逻辑）",
          "this.applyItems(list, false)" in load)
    check("同步：手动按钮走 fillEmptyOnly=true", "this.applyItems(list, true)" in sync)
    check("同步：按钮文案与处理函数都在，且带 loading（连点不叠请求）",
          "同步识别结果" in src and '@click="syncModel"' in src and ':loading="syncing"' in src)
    check("同步：手动同步也把任务状态一起拉回来（进度/失败数不落后）",
          "await this.api('/api/batches/' + id)" in sync)
    check("同步：空值/等价判定是顶层纯函数（tags 是数组，`!==` 会恒真）",
          "function isBlank" in src and "function sameVal" in src
          and "x.length === y.length" in src)

    # —— 模板：每个可编辑控件都要记一笔「动过」 ——
    for f in ("content", "qtype", "difficulty", "knowledge_point", "tags"):
        check(f"同步：{f} 的控件挂了 markEdited", f"markEdited(it, '{f}')" in src)
    check("同步：配图勾选先记账再落库",
          "markEdited(it, 'needs_figure')" in ft and "saveFigure(it)" in ft)
    check("同步：保存成功后清账（之后重新跟随服务端）",
          "delete this.edited[it.id]" in save)
    check("同步：记账只经 markEdited（模板不直接写 edited）",
          "this.edited[it.id] = {}" in mark and "it.id][f] = true" in mark)


# ------------------------------------------------- ⑩ 题块编排：真实方法体在 node 里跑行为断言
#
# 为什么不是纯静态断言：本轮改的是**算法**（段 → 编排表 → 题块），
# 「顺序在 rebuild 后还保不保得住」「新段插到哪」「合并后是不是真的两段」
# 这些都不是 grep 能看出来的。而它又极易被下一次「顺手改成按位置重排」弄坏。
# 所以把 batch.html 里的**真实方法体**抠出来，挂到一个最小 state 上，在 node 里跑。
JS_SIGS = [
    ("segKey(pno, gy)", "segKey"),
    ("bandsOf(pno)", "bandsOf"),
    ("tailBand(pno)", "tailBand"),
    ("rebuild()", "rebuild"),
    ("mergePrev(i)", "mergePrev"),
    ("shiftBlock(i, d)", "shiftBlock"),
    ("dropOn(i)", "dropOn"),
    ("removeBlock(i)", "removeBlock"),
    ("toggleSkip(i)", "toggleSkip"),
    ("blockPayload()", "blockPayload"),
    ("markEdited(it, f)", "markEdited"),
    ("applyItems(list, fillEmptyOnly)", "applyItems"),
]

# applyItems 依赖的**顶层**声明 —— 必须从 batch.html 里真取，
# 测试里重抄一份等于测自己的副本（那就白测了）。
JS_TOP_DECLS = ["REVIEW_FIELDS", "isBlank", "sameVal"]

JS_HEAD = """'use strict';
const fails = [];
function check(name, ok) {
  console.log('  [' + (ok ? 'OK ' : 'FAIL') + '] ' + name);
  if (!ok) fails.push(name);
}
function fresh() {
  return {
    pv: { count: 2, pages: { 1: { width: 600, height: 800 }, 2: { width: 600, height: 800 } } },
    pageList: [1, 2],
    dividers: {}, rects: [], skips: {}, skipKeys: {}, groups: [], _rectSeq: 0, blocks: [],
    dragFrom: null, dragOver: null,
    items: [], edited: {},
    previewUrl() { return ''; },
"""

JS_TAIL = r"""  };
}

const ids = s => s.blocks.map(b => b.id);
const idx = (s, id) => s.blocks.findIndex(b => b.id === id);
let s;

// 1) 空文档：每页一个整页块
s = fresh(); s.rebuild();
check('空文档：两页各一个整页块（unsplit）',
      s.blocks.length === 2 && s.blocks[0].unsplit && s.blocks[1].unsplit);

// 2) 末线不再生成到页底的块
s = fresh(); s.dividers[1] = [100, 300]; s.rebuild();
check('末线不成块：两条线只出 1 块', s.blocks.filter(b => b.startPage === 1).length === 1);
check('末线不成块：块顶就是第一条线', s.blocks[0].regions[0].y0 === 100);
const tb = s.tailBand(1);
check('末线提示：tailBand 覆盖 300→页底', !!tb && tb.y0 === 300 && tb.y1 === 800);
check('末线提示：无分界线的页不提示（整页块已覆盖）', s.tailBand(2) === null);

// 3) 删「整页一块」= 整页跳过；再画线即恢复
s = fresh(); s.rebuild();
s.removeBlock(idx(s, 'L2_0'));
check('删整页块：标记整页跳过', s.skips[2] === true);
check('删整页块：该页不再产块', s.blocks.filter(b => b.startPage === 2).length === 0);
delete s.skips[2]; s.dividers[2] = [120, 400]; s.rebuild();
check('画线即恢复：页 2 重新出块', s.blocks.filter(b => b.startPage === 2).length === 1);

// 4) 自由排序：拖/移之后 rebuild 不重排；新段插到锚点块后面
s = fresh(); s.dividers[1] = [100, 300, 600]; s.dividers[2] = [100, 300, 600]; s.rebuild();
check('四块就绪', s.blocks.length === 4);
const k0 = ids(s);
s.shiftBlock(3, -1); s.rebuild();
check('上移一块：顺序变了且 rebuild 后保持',
      ids(s).join() === [k0[0], k0[1], k0[3], k0[2]].join());
s.dragFrom = 3; s.dropOn(0);
check('拖拽：dropOn 把它挪到最前', ids(s)[0] === k0[2] && s.blocks.length === 4);
s.dividers[2] = [100, 300, 600, 700]; s.rebuild();
check('新段插入：数量 +1', s.blocks.length === 5);
check('新段插入：已拖过的顺序不变（新段插在锚点块后面）',
      ids(s).slice(0, 4).join() === [k0[2], k0[0], k0[1], k0[3]].join()
      && ids(s)[4] === 'L2_6000');

// 5) 跨页自由合并 —— 复现「框好的两段中间总隔着别的块」
s = fresh();
s.rects = [{ gid: 'r1', page: 1, x0: 40, y0: 600, x1: 300, y1: 780 },
           { gid: 'r2', page: 2, x0: 40, y0: 30, x1: 300, y1: 200 }];
s.rebuild();
check('跨页：两页都没画线 → 整页块 + 框选块共 4 块', s.blocks.length === 4);
check('跨页：中间果然隔着整页块（复现用户反馈）', idx(s, 'Rr1') + 1 < idx(s, 'Rr2'));
for (let k = idx(s, 'Rr2'); k > idx(s, 'Rr1') + 1; k--) s.shiftBlock(k, -1);
check('跨页：拖到相邻后两段挨着', idx(s, 'Rr2') === idx(s, 'Rr1') + 1);
s.mergePrev(idx(s, 'Rr2'));
const merged = s.blocks.find(b => b.regions.length === 2);
check('跨页：并入上一块后合出一块两段',
      !!merged && merged.regions[0].page === 1 && merged.regions[1].page === 2);
check('跨页：合并后总数 -1', s.blocks.length === 3);

// 6) 删块：框选块删 rect，线块删它顶部那条起始线
s = fresh(); s.dividers[1] = [100, 300, 600];
s.rects = [{ gid: 'r9', page: 2, x0: 10, y0: 10, x1: 100, y1: 100 }];
s.rebuild();
const before = s.blocks.length;
s.removeBlock(idx(s, 'Rr9'));
check('删框选块：rects 少一个', s.rects.length === 0);
check('删框选块：块数 -1', s.blocks.length === before - 1);
s = fresh(); s.dividers[1] = [100, 300, 600]; s.rebuild();
s.removeBlock(idx(s, 'L1_3000'));
check('删线块：撤掉它顶部那条起始线', s.dividers[1].indexOf(300) < 0);
check('删线块：块数 -1', s.blocks.length === 2);

// 7) 「不提交」：标记挂段 key，跨 rebuild 存活；换位跟着块走；合并/删除都不出错
s = fresh(); s.dividers[1] = [100, 300, 600]; s.rebuild();
check('不提交：默认都没标', s.blocks.length === 3 && s.blocks.every(b => !b.skipped));
s.toggleSkip(1);
check('不提交：标上后该块 skipped、其它块不受影响',
      s.blocks[1].skipped === true && s.blocks.filter(b => b.skipped).length === 1);
s.rebuild();
check('不提交：rebuild 之后标记还在（挂的是段 key，不是会被重建的下标）', s.blocks[1].skipped === true);
check('不提交：提交载荷剔掉它（也不裁图）', s.blockPayload().length === 2);
s.shiftBlock(1, 1);
check('不提交：换位后标记跟着这块走', s.blocks[2].skipped === true);
s.toggleSkip(2);
check('不提交：再点一次恢复提交', s.blocks[2].skipped === false && s.blockPayload().length === 3);
s.toggleSkip(1); s.mergePrev(1);
check('不提交：被标的段并进上一块后，整块仍不提交', s.blocks[0].skipped === true);
check('不提交：合并后提交载荷仍剔掉它', s.blockPayload().length === s.blocks.length - 1);
s.toggleSkip(0);
check('不提交：取消时整组段一起恢复', s.blocks[0].skipped === false);
s.toggleSkip(0); s.removeBlock(0);
check('不提交：删块后标记被清掉（不会越攒越多）', Object.keys(s.skipKeys).length === 0);

// 8) 待审同步：模型输出要能进文本框，但老师敲过的字不能被 1.5 秒一轮的轮询吞掉
__TOP_DECLS__
const SRV = (over) => Object.assign({
  id: 'a', content: '', qtype: '其他', difficulty: '中等', knowledge_point: '',
  tags: [], needs_figure: false, figure_image: '', status: 'pending', flags: [],
}, over || {});

// 8.1 复现老 bug：开局本地是空串，模型输出后来才到
s = fresh();
s.items = [SRV()];
s.applyItems([SRV({ content: '模型给的题干', qtype: '解答题', difficulty: '较难',
                   knowledge_point: '三角函数', tags: ['含参'], needs_figure: true,
                   figure_image: '/crops/a.png', flags: ['json_repaired'] })], false);
check('同步：没动过的题干跟随服务端（模型输出进文本框）', s.items[0].content === '模型给的题干');
check('同步：题型/难度/知识点/标签一起跟随服务端',
      s.items[0].qtype === '解答题' && s.items[0].difficulty === '较难'
      && s.items[0].knowledge_point === '三角函数' && s.items[0].tags[0] === '含参');
check('同步：配图判断跟随服务端（老师没勾就听模型的）', s.items[0].needs_figure === true);
check('同步：figure_image 只认服务端（本地旧值不许把它覆盖回去）',
      s.items[0].figure_image === '/crops/a.png');
check('同步：非可编辑字段（flags）也以服务端为准', s.items[0].flags[0] === 'json_repaired');

// 8.2 老师敲过字 → 轮询不许吞
s.markEdited(s.items[0], 'content');
s.applyItems([SRV({ content: '模型又改了一版', tags: ['恒成立'] })], false);
check('同步：敲过的题干保留本地（轮询不吞正在编辑的字）', s.items[0].content === '模型给的题干');
check('同步：没敲过的标签照样跟随服务端', s.items[0].tags[0] === '恒成立');

// 8.3 手动「同步识别结果」：只补空，写过的绝不覆盖
s.markEdited(s.items[0], 'knowledge_point');
s.items[0].content = '';                       // 老师把题干清空了
s.items[0].knowledge_point = '我写的知识点';
const chg = s.applyItems([SRV({ content: '模型版题干', knowledge_point: '模型版知识点' })], true);
check('手动同步：被清空的题干由模型输出补上', s.items[0].content === '模型版题干');
check('手动同步：已经写好的知识点不被覆盖（只补空）', s.items[0].knowledge_point === '我写的知识点');
check('手动同步：返回改动处数（给提示文案用）', chg >= 1);

// 8.4 两边一致时不能报「有更新」（tags 是数组，!== 会恒真）
s = fresh();
s.items = [SRV({ content: '同一段话', tags: ['t'] })];
check('手动同步：本地与服务端一致时不报更新（数组逐项比）',
      s.applyItems([SRV({ content: '同一段话', tags: ['t'] })], true) === 0);

console.log('');
if (fails.length) { console.log('❌ 行为验证未通过：' + JSON.stringify(fails)); process.exit(1); }
console.log('✅ 行为验证全部通过');
"""


def check_ordering_behavior(src: str) -> None:
    node = shutil.which("node")
    if not node:
        print("  [SKIP] node 不在 PATH —— 跳过题块编排的行为验证")
        return
    decls = []
    for name in JS_TOP_DECLS:
        d = top_decl(src, name)
        if not d:
            check(f"行为验证：抠出顶层声明 {name}", False)
            return
        decls.append(d)
    parts = [JS_HEAD]
    for sig, name in JS_SIGS:
        body = method_body(src, name)
        if not body:
            check(f"行为验证：抠出 {name} 的真实方法体", False)
            return
        parts.append("    %s {%s\n    },\n" % (sig, body))
    # 顶层声明插在待审同步那一段之前（不能塞进 fresh() 的对象字面量里）
    parts.append(JS_TAIL.replace("__TOP_DECLS__", "\n".join(decls)))
    js = "".join(parts)

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "blk_behaviour.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run([node, str(f)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    # ⚠️ 别对整段 stdout 做 .strip()：那会把**首行**前面的两个缩进空格也吃掉，
    #    首行就匹配不上、被静默丢掉（第一次写就踩了，第一项断言凭空消失）。
    out = r.stdout or ""
    saw_fail = False
    for line in out.splitlines():
        # 行格式与 python 的 check() 一致：`  [OK ] 名称` / `  [FAIL] 名称`
        s = line.strip()
        if s.startswith("[") and "] " in s:
            cut = s.index("] ")
            ok = s[1:cut].strip() == "OK"
            saw_fail = saw_fail or not ok
            check(s[cut + 2:], ok)
    if r.returncode != 0 and not saw_fail:
        # 有输出但一条 FAIL 都没记下来 → 多半是 JS 抛异常/语法错，别当成通过
        check("题块编排行为验证（node 异常退出）", False,
              ((r.stderr or "") + out).strip()[-300:])
    if not out.strip():
        check("题块编排行为验证（node 无输出）", False, (r.stderr or "").strip()[:300])


def main() -> int:
    src = BATCH.read_text(encoding="utf-8")
    clean = strip_noise(src)          # 标签配对要在去注释/去 script 的标记文本上数

    print("==== ① 批量页 batch.html ====")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", src)
    check("有且只有一个内联 script 块", len(blocks) == 1, f"实为 {len(blocks)}")
    if blocks:
        js_syntax(blocks[-1], "batch 主逻辑")

    # 组件对象要单独过一遍语法：它在 HTML 的 <script> 里是对象字面量，
    # 上面那次检查已经覆盖了同一段代码，这里不重复。

    # 标签配对（自闭合 <tag/> 要减掉）
    for tag in ("template", "el-form-item", "el-radio-group", "el-input", "el-button",
                "el-tag", "el-select", "el-option", "el-checkbox", "el-row", "el-col",
                "el-card", "el-tabs", "el-tab-pane", "el-alert", "el-progress",
                "el-upload", "rich-text", "div"):
        opened = len(re.findall(rf"<{tag}[\s>]", clean))
        closed = len(re.findall(rf"</{tag}>", clean))
        selfc = len(re.findall(rf"<{tag}\b[^>]*/>", clean))
        check(f"<{tag}> 配对", opened == closed + selfc, f"开 {opened} 闭 {closed} 自闭合 {selfc}")

    print("\n==== ② 批量页关键能力 ====")
    need = {
        "Markdown 渲染器（marked）": "marked.min.js",
        "消毒（DOMPurify）": "purify.min.js",
        "LaTeX 定界符归一": "normalizeMath",
        "渲染函数": "renderRich",
        "渲染用子组件（v-for 里不手写 ref）": "RichText",
        "两种分割工具": "'line'",
        "框选工具": "'rect'",
        "自动铺线": "autoSegment",
        "线间自动成块": "bandsOf",
        "起始线语义（第一条线以上跳过）": "起始线",
        "整页可跳过": "skips",
        "恢复本页": "restorePage",
        "题块自由排序（拖拽/上下移）": "shiftBlock",
        "跨页合并（拖到相邻再并入上一块）": "mergePrev",
        "末线以下不出块、给虚线提示": "tail-hint",
        "不提交（跳过识别的结构说明块）": "toggleSkip",
        "一次裁多块接口": "batch-crop",
        "建任务接口": "/api/batches",
        "把裁剪结果映射回块": "crop.items",
        "轮询进度": "startPoll",
        "轮询：动过的字段才保留本地（其余跟随服务端）": "markEdited",
        "手动同步模型输出": "syncModel",
        "待审条目编辑": "saveItem",
        "通过入库": "approve",
        "驳回": "reject",
        "退回待审": "resetItem",
        "重跑失败项": "rerunFailed",
        "删任务": "deleteJob",
        "后端枚举（不前端写死）": "/api/batch/options",
        "flag 文案": "flagLabel",
        "体系选择": "curriculumId",
        "框选块的色带": "rect-rect",
    }
    for label, token in need.items():
        check(label, token in src)

    print("\n==== ③ 侧栏导航 App.js ====")
    app = APPJS.read_text(encoding="utf-8")
    js_syntax(app, "App.js")
    check("批量入库入口在侧栏", "/batch.html" in app)
    check("录题入口仍在", "/entry.html" in app)

    print("\n==== ④ 与录题页的一致性 ====")
    entry = ENTRY.read_text(encoding="utf-8")
    for label, token in {
        "同一套 vendor 依赖（Vue）": "/vendor/vue.global.prod.js",
        "同一套 vendor 依赖（Element Plus）": "/vendor/element-plus/index.full.min.js",
        "同一套公式渲染库（KaTeX）": "/vendor/katex/auto-render.min.js",
    }.items():
        check(label, (token in src) and (token in entry))

    print("\n==== ⑤ in-DOM 模板守卫（白屏防线）====")
    # 先证明探测器本身是灵的：拿一段「已知会白屏」的模板，必须被检出。
    probe_bad = (
        '<div id="app">'
        '<div v-if="a">甲</div>'
        '<el-alert v-else-if="b" title="乙" />'      # ← 自闭合，吞掉后面的兄弟
        '<div v-else>丙</div>'
        "</div>"
    )
    check("探测器能识别自闭合自定义组件",
          [t for _, t in selfclosing_custom(strip_noise(probe_bad))] == ["el-alert"])
    check("探测器能识别被吞出来的孤儿 v-else",
          len(orphan_else(parse_tree(strip_noise(probe_bad)))) == 1)
    probe_good = (
        '<div id="app">'
        '<div v-if="a">甲</div>'
        '<el-alert v-else-if="b" title="乙"></el-alert>'
        '<div v-else>丙</div>'
        "</div>"
    )
    check("探测器不误报正确写法",
          not selfclosing_custom(strip_noise(probe_good))
          and not orphan_else(parse_tree(strip_noise(probe_good))))

    guard_in_dom("batch.html", BATCH)
    guard_in_dom("entry.html", ENTRY)
    if (ROOT / "frontend" / "index.html").exists():
        guard_in_dom("index.html", ROOT / "frontend" / "index.html")

    print("\n==== ⑥ 三个使用反馈的回归防线 ====")
    run_qnum_cases(src)
    check_feedback_fixes(src)

    print("\n==== ⑦ 题块缩略图 + 题干配图 ====")
    preview_not_persisted(src)
    check_figure_and_thumb(src)

    print("\n==== ⑧ 起始线语义 + 自由排序合并 ====")
    check_startline_merge(src)

    print("\n==== ⑨ 待审：模型输出同步进文本框 ====")
    check_review_sync(src)

    print("\n==== ⑩ 题块编排 + 待审同步（真实方法体在 node 里跑） ====")
    check_ordering_behavior(src)

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
