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

    check("抠到配图相关的 9 个方法",
          all([prev, save, warn, clr, pick, turn, down, style, spick]))

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
    check("配图：勾一下立刻落库", '@change="saveFigure(it)"' in src)
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

    # —— 与轮询/保存的配合 ——
    check("轮询：保留字段里有 needs_figure（刚勾上不被打回）",
          "needs_figure: old.needs_figure" in src)
    kept = re.search(r"return \{ \.\.\.n,[\s\S]*?\};", src)
    check("轮询：figure_image 反而不能保留（否则覆盖刚框好的图）",
          kept is not None and "figure_image" not in kept.group(0))
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
        "页眉块可删（页顶可切）": "cutHead",
        "恢复页顶": "restoreHead",
        "跨页合并": "mergeFirstOf",
        "一次裁多块接口": "batch-crop",
        "建任务接口": "/api/batches",
        "把裁剪结果映射回块": "crop.items",
        "轮询进度": "startPoll",
        "轮询不吞正在编辑的字": "保留正在编辑的字段",
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

    print("\n" + "=" * 60)
    if fails:
        print(f"❌ {len(fails)} 项未通过：{fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
