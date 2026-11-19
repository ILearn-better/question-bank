# -*- coding: utf-8 -*-
"""题库 md —— 题目在磁盘上的那一份，与题库双向同步。

## 为什么要有这一层

老师的要求很具体：「obsidian 和拾课笔记连通……笔记能够弄个题库模板 md，对于这个模板，
录题系统能够直接读取并且自动录为题目存入题库。重点解决有图片的题目。后期题目若要
修改或者补充答案，能够直接通过修改对应 md 进行变动入库题目。」

Obsidian 只认磁盘上的 `.md`，而题库正文存在 SQLite 里。要让两边是**同一批题目**，
就得把「一道题」序列化成一种人能读、Obsidian 能编、拾课能解析的格式。
这个模块就是那份格式的唯一定义：解析（`parse`）与序列化（`render`）都只在这里，
扫描（`scan`）与导出（`export_question`）都基于它们。

## 格式：一份 md = 一道题

    ---
    shike: question          ← 唯一的识别标记。没有它，这个文件不会被当题目
    shike_id: bf5bf71dc7cb   ← 首次入库后自动写回，是「这是哪道题」的身份
    qtype: 选择题
    knowledge: [棱柱的结构特征]
    tags: [三棱柱, 面数]
    ---

    元旦，小颖想给妈妈一个惊喜……（题干，可以多段、可以 $公式$）

    ![[三棱柱礼盒.png]]

    ## 答案
    B

    ## 解析
    解：三棱柱有 $9$ 条棱……

`## 答案` / `## Answer` 与 `## 解析` / `## Solution` 是分界；只写题干也认
（当成「答案还没录」，后续在 Obsidian 里补）。

## 图片按**位置**分流 —— 这是最容易做错的一处

题库里三列含义完全不同，不能混：

  · `image`        整道题的原貌截图（OCR/识别那条路留下的，出卷时整题印图）
  · `figure_image` 题干里那幅「如图」的配图（文本形态出卷时也要印）
  · `answer_image` 答案里的图（只在教师版印）

md 里只有「图」，没有列名，所以按它出现在哪一段来判：
`## 答案` 之前 → figure_image；之后 → answer_image。
`image` 列**不由 md 提供**（它不属于用户写的正文；把它注入 md 会毁掉往返）。

⚠️ 一条硬约束：`figure_image` 目前只存**一张**。一道题贴了多张题干图时，一期只取
第一张，其余写进同步报告 `extra_images`，**不静默丢**。

## 往返稳定（round-trip）

网页改题要写回文件，扫描又要把文件读回来。这条链只要出现一次「读回来跟写进去不一样」，
就会变成每次扫描都认为文件变了 → `updated_at` 被刷成同步时间。所以：

  · `render()` 的产物必须能被 `parse()` 还原成同样的内容（测试直接对着这条验）
  · 哈希在**规范化换行之后**算（同一份内容，CRLF 存和 LF 存算同一个哈希）
  · 图片按**内容**沿用旧值而不是按文件名，否则每次往返都会换一个新文件名

## 谁可以改用户的文件

  · 首次入库：往 frontmatter 补 `shike_id`（只加一行）
  · 网页改题：整篇重写（内容本来就该跟库里一致）

其余时候只读。写的时候**保持原文件的换行风格**：Windows 上手写的 md 是 CRLF、
Obsidian 新建的是 LF，擅自统一会在 git 里制造一次整文件 diff。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..models import Question, QuestionNode
from . import crops, knowledge

# ---------------------------------------------------------------- 目录布局
# 三个名字是**目录约定**，不是配置项：Obsidian 里就是这三层，改了老师那边就找不到了。
INBOX = "收件箱"      # 丢进来的卷子/截图/插件转出来的 md，处理后变成题目 md
SHELF = "题库"        # 题目 md 的落脚处（导出时按知识点分目录）
ATTACH = "附件"       # 题目用的图片
TRASH = "_回收"       # 网页上删题时，md 移到这儿而不是删掉

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
# 扫描时整目录跳过。`.obsidian` 是 Obsidian 自己存的配置（里面有 .md 也不是题），
# `_回收` 是删掉的题（再扫回来等于删不掉）。
SKIP_DIRS = {".obsidian", ".git", ".trash", TRASH}

# 「照这个写」的模板：它本身带 `shike: question`（不带的话根本不是题目 md、没法当格式说明），
# 所以扫描必须**按文件名**把它排除掉，否则模板会被当成一道题入库 ——
# 一道内容是「（题干写在这里）」、知识点「待填知识点」的废题。
TEMPLATE_NAME = "题目模板（照这个写）.md"
TEMPLATE_PREFIX = "题目模板"


def is_template(path: Path) -> bool:
    """这份 md 是不是模板本身。

    判据取文件名前缀而不是完整路径：老师会把模板挪进子目录、也可能另存成
    `题目模板.md`，都还是模板。真正的题目不会叫「题目模板…」这个名。
    """
    return path.stem.startswith(TEMPLATE_PREFIX)


def root() -> Path:
    return config.VAULT_DIR


def inbox_dir() -> Path:
    return root() / INBOX


def shelf_dir() -> Path:
    return root() / SHELF


def attach_dir() -> Path:
    return root() / ATTACH


def ensure_dirs() -> Path:
    """建好三个目录。返回 vault 根。**每次写文件前都调**，别指望启动时建一次。"""
    for d in (root(), shelf_dir(), attach_dir(), inbox_dir()):
        d.mkdir(parents=True, exist_ok=True)
    return root()


# ---------------------------------------------------------------- 路径与安全
def rel(path: Path) -> str:
    """相对 vault 根的 posix 路径 —— 存进 `questions.md_path` 的就是这个。

    用 posix 分隔符（`/`）而不是系统默认：Windows 上 `str(Path)` 是反斜杠，
    同一份数据在另一台机器/另一个平台读出来就对不上。相对路径也同理 ——
    绝对路径不能进库，否则换个数据目录所有题的来源就全指着空处了。
    """
    return path.resolve().relative_to(root().resolve()).as_posix()


def resolve(rel_path: str) -> Path:
    """相对路径 → 绝对路径，**并确认没跑出 vault**。

    这里是唯一的安全边界。前端/文件里来的路径一律先过这一关：
    拒绝 `..`、绝对路径、盘符，以及解析后落在 vault 外面的（软链接逃逸）。
    """
    p = (rel_path or "").strip().replace("\\", "/")
    if not p:
        raise ValueError("路径是空的")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise ValueError(f"只接受相对 vault 的路径：{rel_path}")
    base = root().resolve()
    full = (base / p).resolve()
    if full != base and base not in full.parents:
        raise ValueError(f"路径跑到 vault 外面去了：{rel_path}")
    return full


# ---------------------------------------------------------------- 读写（保住用户的换行与 BOM）
def _norm(text: str) -> str:
    """换行规范化。哈希与比较一律在规范化之后做。

    为什么要规范化：Obsidian 新建的文件是 LF，Windows 上手写的可能是 CRLF。
    老师只是用另一个编辑器打开又保存了一次，内容一个字没改，不该被当成「改过了」。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _digest(text: str) -> str:
    """内容指纹（sha256 前 16 位）。它要回答的是「跟上次同步时是不是同一份」。"""
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:16]


@dataclass
class MdFile:
    text: str      # 规范化换行后的全文
    crlf: bool     # 原文件用 CRLF 吗
    bom: bool      # 原文件有 BOM 吗


def read_md(path: Path) -> MdFile:
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    # errors="replace"：老师从别处粘来的 md 可能混着坏字节。为此整篇读不进来太亏，
    # 坏字符会变成 U+FFFD 在正文里显眼地露出来 —— 比静默失败好。
    text = raw.decode("utf-8", errors="replace")
    return MdFile(_norm(text), "\r\n" in text, bom)


def write_md(path: Path, text: str, *, crlf: bool | None = None, bom: bool = False) -> None:
    """写 md。`crlf=None` 表示「跟着原文件」；新建文件默认 LF（Obsidian 的习惯）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if crlf is None:
        crlf = path.is_file() and b"\r\n" in path.read_bytes()[:8192]
    body = _norm(text)
    if crlf:
        body = body.replace("\n", "\r\n")
    data = body.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


# ---------------------------------------------------------------- frontmatter
_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
# 便宜的预筛：不解析 YAML 就能判断「这不是题目 md」。
# 扫描整个 vault 时绝大多数文件都不是题，先过这一关能省掉绝大部分 yaml 解析。
_MARK_RE = re.compile(r"^shike[ \t]*:[ \t]*question[ \t]*$", re.I | re.M)

ANS_HEAD = re.compile(r"^##[ \t]*(?:答案|Answer)[ \t]*$", re.I | re.M)
SOL_HEAD = re.compile(r"^##[ \t]*(?:解析|详解|Solution|Explanation)[ \t]*$", re.I | re.M)

# Obsidian 的嵌入 `![[图.png]]` / `![[图.png|300]]`，与标准 Markdown `![](路径)`。
# 两种都认：Obsidian 默认写第一种，但老师从别处粘进来的是第二种。
IMG_WIKI = re.compile(r"!\[\[([^\[\]\|]+?)(?:\|[^\[\]]*)?\]\]")
# 末尾那段 `{width=50%}` 是 Pandoc 给图片加的行内属性。**必须一起吃掉**：
# 吃不吃路径都一样能解析出来，但那串花括号会留在正文里，出卷时原样印到卷子上。
IMG_MD = re.compile(r"!\[([^\[\]]*)\]\(([^()]+)\)[ \t]*(?:\{[^{}\n]*\})?")
# 这两行是「这是题目 md」的判据。放常量里，别在两处各写一份。
REQUIRED_MARK = "question"


@dataclass
class Parsed:
    """一份题目 md 解析出来的东西。纯文本层面，**不碰文件系统**（方便单测）。"""
    fields: dict = field(default_factory=dict)
    stem: str = ""
    answer: str = ""
    analysis: str = ""
    stem_imgs: list[str] = field(default_factory=list)   # 原始引用，还没解析成路径
    ans_imgs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ok: bool = False                # frontmatter 读成功了吗（读失败时 fields 可能也是空的）

    @property
    def qid(self) -> str:
        return str(self.fields.get("shike_id") or "").strip()


def is_question_md(text: str) -> bool:
    """这篇 md 是题目模板吗。判据只有一条：frontmatter 里 `shike: question`。"""
    m = _FM_RE.match(text)
    return bool(m and _MARK_RE.search(m.group(1)))


def _take_images(text: str) -> tuple[str, list[str]]:
    """把正文里的图片引用摘出来，并**从文本里删掉**。

    为什么必须删：题目入库后 `content` 是出卷时要印的正文。留着 `![[x.png]]`
    就等于把这一串字符原样印到卷子上（图片本身走 `figure_image` 单独印）。
    """
    refs: list[str] = []

    def grab(m: re.Match) -> str:
        refs.append((m.group(1) if m.re is IMG_WIKI else m.group(2)).strip())
        return ""

    out = IMG_WIKI.sub(grab, text)
    out = IMG_MD.sub(grab, out)
    # 摘掉之后原地可能留下空行或行尾多余空格 —— 收一下，别把空行喂进题库
    out = re.sub(r"[ \t]+$", "", out, flags=re.M)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip(), [r for r in refs if r]


def _split_sections(body: str) -> tuple[str, str, str]:
    """按 `## 答案` / `## 解析` 切成三段。标题的顺序反了也各归各位。"""
    marks: list[tuple[int, int, str]] = []
    for m, kind in ((ANS_HEAD.search(body), "answer"), (SOL_HEAD.search(body), "analysis")):
        if m:
            marks.append((m.start(), m.end(), kind))
    if not marks:
        return body.strip(), "", ""
    marks.sort()
    stem = body[:marks[0][0]].strip()
    answer = analysis = ""
    for i, (_start, end, kind) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        seg = body[end:nxt].strip()
        if kind == "answer":
            answer = seg
        else:
            analysis = seg
    return stem, answer, analysis


def _as_list(value) -> list[str]:
    """YAML 里的 `[甲, 乙]`、块式列表、单个字符串都收成一个列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        # `knowledge: 甲, 乙` 也认 —— Obsidian 属性面板里敲逗号分隔是很自然的写法
        parts = [p.strip() for p in re.split(r"[,，]", value)]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _as_float(value) -> float | None:
    """分值。读不出来就当没写 —— **绝不落成 0**（印「（0 分）」比不印分值糟）。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse(text: str) -> Parsed:
    """解析一份题目 md。不认识的字段原样忽略（用户可能用 Dataview 加了自己的字段）。

    ⚠️ `fields` 里**只放 frontmatter 真的写了的键**。这件事很重要：
       老师只想在 Obsidian 里补个答案时，不会把 `score`、`difficulty` 抄一遍；
       如果这里给缺省值（`score: None`、`render_prefer: "auto"`），同步时就会把
       库里原有的分值、出卷偏好一起抹掉 —— 这是「改一处、坏三处」的典型。
       正文（题干/答案/解析/图）是另一回事：它们就在文件里，**写了什么就是什么**。
    """
    out = Parsed()
    m = _FM_RE.match(_norm(text))
    if not m:
        out.warnings.append("开头没有 frontmatter（缺少 `shike: question` 标记）")
        return out
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        out.warnings.append(f"frontmatter 不是合法 YAML：{exc}")
        return out
    if not isinstance(meta, dict):
        out.warnings.append("frontmatter 不是键值对")
        return out
    out.ok = True

    if "shike_id" in meta:
        out.fields["shike_id"] = str(meta.get("shike_id") or "").strip()
    for key in ("qtype", "difficulty", "source"):
        if key in meta:
            val = str(meta.get(key) or "").strip()
            if val:
                out.fields[key] = val
    if "score" in meta:
        val = _as_float(meta.get("score"))
        if val is not None:
            out.fields["score"] = val
        elif meta.get("score") not in (None, ""):
            out.warnings.append(f"score 读不成数字（{meta.get('score')!r}），这道题的分值没动")
    if "year" in meta:
        val = _as_int(meta.get("year"))
        if val is not None:
            out.fields["year"] = val
    if "knowledge" in meta:
        out.fields["knowledge"] = knowledge.clean_tags(_as_list(meta.get("knowledge")))
    if "tags" in meta:
        out.fields["tags"] = knowledge.clean_tags(_as_list(meta.get("tags")))
    if "render_prefer" in meta:
        pref = str(meta.get("render_prefer") or "").strip().lower()
        if pref in ("auto", "text", "image"):
            out.fields["render_prefer"] = pref
        elif pref:
            out.warnings.append(f"render_prefer 只能是 auto/text/image，收到 {pref!r}，这项没动")

    body = text[m.end():]
    stem_raw, ans_raw, sol_raw = _split_sections(_norm(body))
    out.stem, out.stem_imgs = _take_images(stem_raw)
    out.answer, out.ans_imgs = _take_images(ans_raw)
    # 解析里的图不当答案图：那是「讲解插图」，出卷时不印，留着会变成悬空引用
    sol_clean, sol_imgs = _take_images(sol_raw)
    out.analysis = sol_clean
    if sol_imgs:
        out.warnings.append(f"解析里放了 {len(sol_imgs)} 张图，目前不入库（{', '.join(sol_imgs)}）")

    if not out.stem:
        out.warnings.append("题干是空的")
    return out


# ---------------------------------------------------------------- 序列化
def _yaml_value(v) -> str:
    """把一个标量写成 YAML。含 `:`、`#`、引号之类的加双引号（JSON 的引号 YAML 也认）。"""
    s = str(v)
    if not s:
        return '""'
    if s != s.strip() or any(c in s for c in ":#{}[],&*!|>'\"%@`") or s[0] in "-? ":
        return json.dumps(s, ensure_ascii=False)
    return s


def _yaml_list(name: str, items: Iterable[str]) -> list[str]:
    items = [str(i) for i in items if str(i).strip()]
    if not items:
        return []
    # 块式而不是 `[甲, 乙]`：知识点里出现逗号时，行内写法会被 YAML 拆成两个
    return [f"{name}:"] + [f"  - {_yaml_value(i)}" for i in items]


def render(q: Question, *, fig_name: str | None = None, ans_name: str | None = None) -> str:
    """一道题 → md 正文。

    `fig_name` / `ans_name` 是**附件文件名**（写进 `![[…]]` 的那个），不是路径。
    传 None 表示这道题没有对应的图。
    """
    lines = ["---", "shike: question", f"shike_id: {q.id}"]
    if q.qtype:
        lines.append(f"qtype: {_yaml_value(q.qtype)}")
    if q.difficulty:
        lines.append(f"difficulty: {_yaml_value(q.difficulty)}")
    if q.score is not None:
        # 整数分值写成 `4` 而不是 `4.0` —— 老师在属性面板里看到 4.0 会想去改成 4
        lines.append(f"score: {int(q.score) if float(q.score).is_integer() else q.score}")
    if q.year is not None:
        lines.append(f"year: {q.year}")
    if q.source:
        lines.append(f"source: {_yaml_value(q.source)}")
    lines += _yaml_list("knowledge", _json_list(q.knowledge_points))
    lines += _yaml_list("tags", _json_list(q.tags))
    if (q.render_prefer or "auto") != "auto":
        lines.append(f"render_prefer: {q.render_prefer}")
    lines += ["---", ""]

    parts: list[str] = []
    body = (q.content or "").strip()
    if body:
        parts.append(body)
    if fig_name:
        parts.append(f"![[{fig_name}]]")
    text = "\n\n".join(parts)
    if (q.answer or "").strip():
        text += "\n\n## 答案\n\n" + (q.answer or "").strip()
    if ans_name:
        text += "\n\n![[%s]]" % ans_name
    if (q.analysis or "").strip():
        text += "\n\n## 解析\n\n" + (q.analysis or "").strip()
    return "\n".join(lines) + text.rstrip() + "\n"


def _json_list(raw) -> list[str]:
    """库里 tags / knowledge_points 都是 JSON 数组文本。坏数据别让整次导出失败。"""
    try:
        val = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(v) for v in val] if isinstance(val, list) else []


# ---------------------------------------------------------------- 图片
def _index_images() -> dict[str, list[Path]]:
    """vault 里所有图片：文件名 → 绝对路径列表。

    Obsidian 的 `![[名字.png]]` 是**按文件名全局找**的，所以这里也必须按文件名索引。
    同名文件给多个路径，用的时候多于一个就报冲突 —— 让它显式失败，别猜。
    """
    idx: dict[str, list[Path]] = {}
    base = root()
    if not base.is_dir():
        return idx
    for p in base.rglob("*"):
        if p.suffix.lower() not in IMG_EXT or not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(base).parts[:-1]):
            continue
        idx.setdefault(p.name.lower(), []).append(p)
    return idx


def _clean_ref(ref: str) -> str:
    """把各种转换器塞进图片引用里的壳剥掉，只留下路径本身。

    真实遇到过的形态（Pandoc / Obsidian / Word 导出 / 老师手写）：
        media/image1.png            Pandoc 把图放在旁边的 media/
        ./attachments/a.png         显式相对
        ../附件/a.png               上一级
        my%20image.png              URL 编码的空格
        <my image.png>              尖括号包住带空格的路径
        x.png#center                带锚点
        x.png "图 1"                Markdown 的标题部分
        attachments\\x.png          Windows 反斜杠
    这些都得认出来 —— 认不出来就是「图丢了」，而图丢了这道题基本就废了。
    """
    ref = html.unescape(ref or "").strip()
    # Markdown 允许 `![](x.png "标题")`。剥标题要排在剥引号**之前** ——
    # 反过来会把末尾那个引号先吃掉，于是这段标题就匹配不上了。
    ref = re.sub(r'\s+"[^"]*"\s*$', "", ref).strip()
    # 整段被 `<>` / `""` / `''` 包住（带空格路径的常见写法）
    if len(ref) > 1 and ref[0] + ref[-1] in ('""', "''", "<>"):
        ref = ref[1:-1].strip()
    # 锚点与查询串（`#center`、`?raw=1`）不是路径的一部分。
    # 切在 unquote 之前：`%23` 是字面的 `#`，先解码会被误切。
    ref = ref.split("#", 1)[0].split("?", 1)[0].strip()
    ref = unquote(ref)
    return ref.replace("\\", "/")


def _locate(ref: str, md_dir: Path, idx: dict[str, list[Path]]) -> tuple[Path | None, str]:
    """把 md 里的图片引用解析成实际文件。返回 (路径, 出错原因)。

    顺序：先按「md 所在目录」找，再按 vault 根找，最后按文件名全局找。
    两个基准都试，因为插件可能写相对 md 的路径，也可能写相对 vault 根的路径。
    """
    ref = _clean_ref(ref)
    if not ref:
        return None, "图片引用是空的"
    low = ref.lower()
    if low.startswith(("http://", "https://", "data:", "file:")):
        return None, f"外链图片没有下载（{ref[:60]}）"
    if re.match(r"^[A-Za-z]:", ref) or ref.startswith("/"):
        return None, f"图片引用必须是 vault 内的相对路径（{ref}）"
    # 先当相对路径试
    for base in (md_dir, root()):
        try:
            cand = (base / ref).resolve()
        except (OSError, ValueError):
            continue
        base_r = root().resolve()
        if (cand == base_r or base_r in cand.parents) and cand.is_file():
            return cand, ""
    # 再按文件名找（Obsidian wiki 链接，以及 Pandoc 的 media/image1.png）
    name = Path(ref).name.lower()
    hits = idx.get(name) or []
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        return None, f"vault 里有 {len(hits)} 个叫 {name} 的图片，不知道该用哪个"
    return None, f"找不到图片 {ref}"


def _same_content(url: str | None, path: Path) -> bool:
    """库里的这张图跟 vault 里那张是不是同一份内容。

    按**内容**而不是文件名：往返一趟时附件名会跟着标题变，
    只比名字的话每次都会认为「图换了」→ 复制一张新图 → 哈希变了 → 下次又同步。
    """
    if not url:
        return False
    cur = config.CROPS_DIR / Path(url).name
    if not cur.is_file() or not path.is_file():
        return False
    try:
        return cur.stat().st_size == path.stat().st_size and cur.read_bytes() == path.read_bytes()
    except OSError:
        return False


def _ingest_image(path: Path, qid: str, kind: str) -> str:
    """把 vault 里的图搬进题库的截图目录，返回 `/api/crops/xxx.png`。

    为什么要复制而不是引用 vault 的路径：题库里的图受 `services/crops.py` 的引用计数
    管辖（删题时清理孤儿），而 vault 是老师的地盘 —— 老师删掉一个 md、或者整理附件目录，
    不该让题库里的题跟着丢图。复制一份，两边各自独立。
    """
    from . import images as image_svc

    data = path.read_bytes()
    saved = image_svc.save_image(data, config.CROPS_DIR, prefix=f"md_{kind}")
    return f"/api/crops/{saved['name']}"


def _clean_attach(qid: str) -> None:
    """删掉这道题上一次导出时生成的附件。

    只删**我们自己生成的**（文件名里带完整 qid 和 `-fig` / `-ans`）：
    老师自己贴进 vault 的图（`Pasted image xxx.png`）一个都不能碰。
    标题改了会导致附件名变，不清理就会在附件目录里越堆越多。
    """
    for p in attach_dir().glob(f"*-{qid}-fig.*"):
        p.unlink(missing_ok=True)
    for p in attach_dir().glob(f"*-{qid}-ans.*"):
        p.unlink(missing_ok=True)


def _attach_name(title: str, qid: str, kind: str, src: Path) -> str:
    """附件名：可读标题 + 完整 id + 用途。唯一（id 唯一）且认得出来是谁的。"""
    stem = _safe_stem(title, qid, limit=24)
    return f"{stem}-{qid}-{kind}{src.suffix.lower() or '.png'}"


# ---------------------------------------------------------------- 文件名
_BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_stem(title: str, qid: str, limit: int = 40) -> str:
    """题干 → 能当文件名的短标题。

    不是「清理掉危险字符」那么简单：中学题第一行常常是 `（1）请观察所给几何体…`，
    直接拿来当文件名会在 Windows 上撞保留名 / 超长 / 带公式。所以：
    去公式、去危险字符、压缩空白、截断，实在没剩下就用 id。
    """
    s = re.sub(r"\$[^$]*\$", "", title or "")          # 公式进不了文件名
    s = _BAD_NAME.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        return f"题-{qid[:6]}"
    if s.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}:
        s = f"题-{s}"
    return s[:limit].strip(" .") or f"题-{qid[:6]}"


def _title_of(q: Question) -> str:
    """文件的标题：题干第一行。空题干退回「题-id」。"""
    first = (q.content or "").strip().split("\n", 1)[0]
    return _safe_stem(first, q.id)


# ---------------------------------------------------------------- 导出
def export_question(db: Session, q: Question, *, folder: str | None = None) -> Path:
    """把一道题写成 md（已存在就原地重写），返回文件路径。

    这个函数**会改用户的文件**：只在两种情况下被调用 —— 用户点了「导出到 vault」，
    或在网页上改了这道题（那时 md 本来就该跟着变，它是正文的真源）。
    """
    ensure_dirs()
    fig_name = ans_name = None
    _clean_attach(q.id)

    for url, kind in ((q.figure_image, "fig"), (q.answer_image, "ans")):
        if not url:
            continue
        src = config.CROPS_DIR / Path(url).name
        if not src.is_file():
            # 图没了就别写进 md —— 写进去会变成悬空引用，扫描回来还会报一次错
            continue
        name = _attach_name(_title_of(q), q.id, kind, src)
        shutil.copyfile(src, attach_dir() / name)
        if kind == "fig":
            fig_name = name
        else:
            ans_name = name

    text = render(q, fig_name=fig_name, ans_name=ans_name)

    if q.md_path:
        path = resolve(q.md_path)
    else:
        sub = folder or _folder_for(q)
        base = (shelf_dir() / sub) if sub else shelf_dir()
        path = base / f"{_title_of(q)}.md"
        n = 1
        while path.exists():
            n += 1
            path = base / f"{_title_of(q)}-{n}.md"
    write_md(path, text)
    q.md_path = rel(path)
    q.md_hash = _digest(text)
    db.flush()
    return path


def _folder_for(q: Question) -> str:
    """默认按第一个知识点分目录。没有知识点就平铺在 `题库/` 下。"""
    kps = _json_list(q.knowledge_points)
    if not kps:
        return ""
    return _safe_stem(kps[0], q.id, limit=20)


# ---------------------------------------------------------------- 扫描
def _iter_md() -> list[Path]:
    base = root()
    if not base.is_dir():
        return []
    out: list[Path] = []
    for p in base.rglob("*.md"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(base).parts[:-1]):
            continue
        out.append(p)
    return sorted(out)


@dataclass
class _Plan:
    """一道题的同步方案：是新建、更新，还是原样不动。"""
    path: Path
    existed: Question | None
    parsed: Parsed
    text: str = ""                  # 本轮读到的原文（算哈希用，别再读一次盘）
    figure_image: str = ""
    answer_image: str = ""
    extra_images: list[str] = field(default_factory=list)


def _settle_plan(db: Session, plan: _Plan, idx: dict[str, list[Path]], *,
                 import_images: bool = True) -> None:
    """把 md 里的图片引用落成题库的两列，并决定「沿用旧的」还是「导入新的」。

    `import_images=False`（dry-run）时只判断图在不在，**不往截图目录写文件** ——
    副作用是收不回来的，光靠 rollback 擦不掉。
    """
    md_dir = plan.path.parent
    warnings = plan.parsed.warnings

    def one(refs: list[str], cur_url: str, kind: str) -> str:
        resolved: list[Path] = []
        for r in refs:
            p, why = _locate(r, md_dir, idx)
            if p is None:
                warnings.append(why)
            else:
                resolved.append(p)
        if not resolved:
            if refs:
                # 引用写了但一个都找不到 —— 不能当「用户想删图」处理，那会把图弄丢。
                # 保留库里原来那张，并在报告里说清楚。
                warnings.append("md 里的图片一张都没找到，库里原有的配图保持不变")
                return cur_url
            # md 里确实一张图都没有 → 库里也清空。**md 是真源**，删了图就该少一张。
            return ""
        if len(resolved) > 1:
            extra = [p.name for p in resolved[1:]]
            warnings.append(f"题干配图目前只能存一张，多出来的没入库：{', '.join(extra)}")
            if kind == "fig":
                plan.extra_images.extend(extra)
        src = resolved[0]
        if _same_content(cur_url, src):
            return cur_url          # 内容没变，沿用旧 URL（否则每次往返都换文件名）
        if not import_images:
            return f"(dry-run) {src.name}"
        return _ingest_image(src, plan.parsed.qid or (plan.existed.id if plan.existed else "new"), kind)

    plan.figure_image = one(plan.parsed.stem_imgs, (plan.existed.figure_image if plan.existed else "") or "", "fig")
    plan.answer_image = one(plan.parsed.ans_imgs, (plan.existed.answer_image if plan.existed else "") or "", "ans")


def _apply(db: Session, plan: _Plan) -> str:
    """把解析结果写进库。返回题目 id。"""
    now = datetime.now().isoformat(timespec="seconds")
    f = plan.parsed.fields
    q = plan.existed
    created = q is None

    # 一个 md 文件只能对应一道题（库里是唯一索引）。先查出冲突并给人话，
    # 别等到 flush 时抛 IntegrityError —— 那个报错信息对老师等于天书。
    rel_path = rel(plan.path)
    other = db.scalar(select(Question).where(Question.md_path == rel_path,
                                             Question.id != (q.id if q else "")))
    if other is not None:
        raise ValueError(f"这个文件已经对应另一道题（{other.id}）。"
                         "是不是复制了一份 md 却没改 shike_id？")

    if created:
        q = Question(
            id=uuid.uuid4().hex[:12],
            owner_id=config.OWNER_ID,
            created_at=now,
            tags="[]",
            knowledge_points="[]",
        )
        db.add(q)

    old_figs = crops.crop_names(q.figure_image, q.answer_image)

    # 正文是**全量真源**：文件里写了什么就是什么，没写就是空。
    q.content = plan.parsed.stem
    q.answer = plan.parsed.answer
    q.analysis = plan.parsed.analysis
    q.figure_image = plan.figure_image
    q.answer_image = plan.answer_image
    # frontmatter 是**部分真源**：只覆盖文件里真的写了的键（见 parse 的说明）。
    for key in ("qtype", "difficulty", "source", "score", "year", "render_prefer"):
        if key in f:
            setattr(q, key, f[key])
    if "knowledge" in f:
        # 名字 → 知识树节点。这一步以前漏了：只把名字写进字符串，`node_id` 永远是空，
        # 于是「按知识点筛题」「掌握度统计」都看不见这些题 —— 而它们明明有知识点，
        # 只是挂不到树上，**不报错**。和批量入库（approve_item）走同一个函数。
        #
        # 体系从哪来：md 里不写体系，`resolve_kp` 的**精确同名那一步不限体系**，
        # 命中后由节点本身带出 curriculum_id。名字写错就退回原样（和以前一样，不更差）。
        kps, node_id, curr_id = knowledge.resolve_kp(db, f["knowledge"], q.curriculum_id)
        q.knowledge_points = json.dumps(kps, ensure_ascii=False)
        if node_id != q.node_id:
            # 换了节点就把旧记录拆掉，否则一道题会同时挂在两个知识点下
            db.execute(delete(QuestionNode).where(QuestionNode.question_id == q.id))
            q.node_id = node_id
            q.curriculum_id = curr_id or q.curriculum_id
            if node_id:
                db.add(QuestionNode(question_id=q.id, node_id=node_id, weight=1.0))
    if "tags" in f:
        q.tags = json.dumps(f["tags"], ensure_ascii=False)
    q.md_path = rel_path
    q.md_hash = _digest(plan.text)
    db.flush()

    # 换掉的旧图：新值已经落进事务，现在算引用计数才数得对（和 PATCH 里同一套处理）
    stale = old_figs - crops.crop_names(q.figure_image, q.answer_image)
    if stale:
        crops.purge(stale - crops.referenced_crops(db))
    return q.id


def scan(db: Session, *, dry_run: bool = False) -> dict:
    """扫一遍 vault，把变了的题目 md 同步进库。

    只处理**内容变了**的文件（比哈希）。没变的直接跳过 —— 否则每次同步都会把所有题的
    `updated_at` 刷成当前时间，「最近改过的题」这个视图就没用了。

    `dry_run=True` 时只出报告不落库，用于「同步前先看一眼会改什么」。
    """
    ensure_dirs()
    idx = _index_images()
    report: dict = {
        "created": [], "updated": [], "unchanged": 0,
        "skipped": [], "errors": [], "warnings": [], "extra_images": [],
        "missing": [],          # 库里有 md_path 但文件不在了
        "scanned": 0,
    }
    seen_paths: dict[str, str] = {}      # md_path → qid（发现重复时用来报错）
    seen_qids: dict[str, str] = {}       # qid → md_path（两个文件写同一个 id）

    for path in _iter_md():
        report["scanned"] += 1
        rel_path = rel(path)
        try:
            md = read_md(path)
        except OSError as exc:
            report["errors"].append({"path": rel_path, "error": f"读不了：{exc}"})
            continue
        if is_template(path):
            # 模板本身不入库，但**要报出来**（写进 skipped），否则老师以为它被同步了。
            report["skipped"].append({
                "path": rel_path,
                "reason": "这是「照这个写」的模板本身，跳过；复制一份、改成你的题才会入库",
            })
            continue
        if not is_question_md(md.text):
            report["skipped"].append({"path": rel_path, "reason": "不是题目 md（没有 shike: question）"})
            continue

        parsed = parse(md.text)
        if not parsed.ok:
            report["errors"].append({"path": rel_path, "error": "frontmatter 解析失败",
                                     "detail": "；".join(parsed.warnings)})
            continue

        qid = parsed.qid
        existed: Question | None = None
        if qid:
            existed = db.get(Question, qid)
            if existed is None:
                parsed.warnings.append(f"shike_id={qid} 在题库里找不到，按新题入库并写回新 id")
                qid = ""
            elif seen_qids.get(qid):
                # 同一个 id 出现在两个文件里（多半是复制了 md）。不能两个都更新 ——
                # 那会成为「谁最后扫到谁赢」的随机结果。
                report["errors"].append({
                    "path": rel_path,
                    "error": f"shike_id={qid} 已经被 {seen_qids[qid]} 用过了（是不是复制了文件？）",
                })
                continue
        if existed is None:
            # 没有可用 id，再看「这个路径上次对应哪道题」（老师手动删掉了 shike_id 的情况）
            existed = db.scalar(select(Question).where(Question.md_path == rel_path))
            if existed is not None:
                parsed.warnings.append("frontmatter 里没有 shike_id，按路径认回原题并补写")

        # 同一道题被两个文件声明（最典型：老师复制了一个 md 当模板，忘了改 id）。
        # 判据是「库里记的来源路径**文件还在**」——文件被挪走时旧路径已经不存在，
        # 那是正常的重命名，不算冲突。必须在这里拦，否则后扫到的文件会把题的归属抢走：
        # 一片静默的「谁最后扫到谁赢」比报错难查得多。
        if existed is not None and existed.md_path and existed.md_path != rel_path:
            try:
                old = resolve(existed.md_path)
            except ValueError:
                old = None
            if old is not None and old.is_file():
                report["errors"].append({
                    "path": rel_path,
                    "error": f"shike_id={existed.id} 已经对应 {existed.md_path}（那个文件还在）。"
                             "是不是复制了 md 却没改 shike_id？",
                })
                continue

        # 哈希一样、路径没变 → 这次不用管它。
        # 路径变了（Obsidian 里拖了目录 / 改了名）不走这条：来源路径得跟着更新。
        if existed is not None and existed.md_hash == _digest(md.text) and existed.md_path == rel_path:
            report["unchanged"] += 1
            # ⚠️ 没变的题也要登记 id。漏了这一步，复制出来的 md 就查不出重复 ——
            # 「未改动」不等于「不存在」。
            seen_qids[existed.id] = rel_path
            continue

        if rel_path in seen_paths:
            report["errors"].append({"path": rel_path, "error": "这个路径本轮已经处理过了"})
            continue
        seen_paths[rel_path] = qid or (existed.id if existed else "")

        plan = _Plan(path=path, existed=existed, parsed=parsed, text=md.text)
        bucket = "updated" if existed is not None else "created"
        try:
            # dry-run 只判断图在不在，不往截图目录写文件 —— 落盘的副作用 rollback 擦不掉。
            _settle_plan(db, plan, idx, import_images=not dry_run)
            if dry_run:
                report[bucket].append({
                    "path": rel_path,
                    "title": _safe_stem(plan.parsed.stem, "dryrun", 30),
                })
            else:
                new_id = _apply(db, plan)
                report[bucket].append({
                    "path": rel_path,
                    "title": _safe_stem(plan.parsed.stem, new_id, 30),
                    "id": new_id,
                })
                seen_qids[new_id] = rel_path
        except Exception as exc:                      # noqa: BLE001 —— 一条坏文件不该中断整轮
            db.rollback()
            report["errors"].append({"path": rel_path, "error": str(exc)})
            continue

        if parsed.warnings:
            report["warnings"].append({"path": rel_path, "messages": parsed.warnings})
        if plan.extra_images:
            report["extra_images"].append({"path": rel_path, "images": plan.extra_images})

    # 首次入库的题要把 shike_id 写回文件 —— 这是唯一一次「拾课主动改用户文件」，
    # 而且只加一行。不写回的话，下次改文件名就认不出它是哪道题了。
    if not dry_run:
        for item in report["created"]:
            p = resolve(item["path"])
            try:
                _stamp_id(db, p, item["id"])
            except Exception as exc:                  # noqa: BLE001
                report["warnings"].append({"path": item["path"], "messages": [f"没写回 shike_id：{exc}"]})
        db.commit()

    # 库里有来源、但文件不见了的题：**不自动删题**。老师可能只是临时移走了文件，
    # 或者把 vault 换了个位置。只报出来让他自己决定。
    for q in db.scalars(select(Question).where(Question.md_path.isnot(None))).all():
        try:
            if not resolve(q.md_path).is_file():
                report["missing"].append({"id": q.id, "path": q.md_path})
        except ValueError:
            report["missing"].append({"id": q.id, "path": q.md_path})

    db.commit()
    return report


def _stamp_id(db: Session, path: Path, qid: str) -> None:
    """往 frontmatter 补一行 `shike_id`（已经有了就不动）。"""
    md = read_md(path)
    m = _FM_RE.match(md.text)
    if not m:
        return
    head = m.group(1)
    if re.search(r"^shike_id[ \t]*:", head, re.M):
        # 已有值但和库里不一致：以文件为准会丢题，以库为准要改文件 ——
        # 这里改文件，因为 id 是身份，用户手改 id 本意就是「换个身份」。
        if re.search(r"^shike_id[ \t]*:[ \t]*%s\s*$" % re.escape(qid), head, re.M):
            return
        head = re.sub(r"^shike_id[ \t]*:.*$", f"shike_id: {qid}", head, count=1, flags=re.M)
    else:
        head = "shike_id: %s\n%s" % (qid, head)
    new_text = "---\n" + head + "\n---\n" + md.text[m.end():]
    write_md(path, new_text, crlf=md.crlf, bom=md.bom)
    q = db.get(Question, qid)
    if q is not None and q.md_path == rel(path):
        q.md_hash = _digest(new_text)


def sync_back(db: Session, q: Question) -> bool:
    """网页上改完题，把改动写回它来源的 md。返回「写了吗」。

    只有 `md_path` 有值的题才写 —— 网页录的题不主动往 vault 里塞东西
    （老师没要求过那些题要变成文件）。文件不见了也跳过，不凭空重建。
    """
    if not q.md_path:
        return False
    try:
        path = resolve(q.md_path)
    except ValueError:
        return False
    if not path.is_file():
        return False
    export_question(db, q)
    return True


# ---------------------------------------------------------------- 宽松摄取（插件转出来的 md）
#
# 老师选定的转换方式是「在 Obsidian 里用插件把 PDF 变成 md」。插件转出来的东西
# **不是**题目模板：没有 frontmatter、正文是整份卷子、图片写的是 `![](media/image1.png)`。
# 但恰恰是它手上最多。这一段只做一件事：把它变成标准题目模板，然后交给上面那套
# parse / _settle_plan / _apply / export_question —— **不新增第二条入库路径**。
#
# 判据靠位置，不靠内容嗅探：进了 `收件箱/` 的 md 才走这里。原因是「正文里有 ## 答案
# 就是题目」这种嗅探会把老师自己的笔记也吸进来，而那是不可逆的（笔记被当题入库）。

_SPLIT = "\uE002"          # 私用区字符，正文里不会出现
# 水平分割线：**前后都必须是空行**。这一条很要紧 ——
# `上一行文字\n---` 在 Markdown 里是 setext 二级标题，不是分割线。
_RULE = re.compile(r"\n[ \t]*\n[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*\n[ \t]*\n")
# 大题号：`1.` `1、` `第 1 题`。`1.5 米` 不算 —— 所以 `.` 后面不能紧跟数字。
_QNUM = re.compile(r"^[ \t]*(?:\d{1,2}[.、．](?!\d)|第[ \t]*\d{1,2}[ \t]*题)", re.M)


def count_questions(text: str) -> int:
    """数一下正文里有几处大题号。用来判断「这是一份卷子」还是「这是一道题」。"""
    return len(_QNUM.findall(_norm(text)))


def split_units(text: str) -> list[str]:
    """把一份 md 切成「一题一段」。

    ⚠️ 分割线本身**不足以判断**是分题：老师也常用 `---` 把题干和答案隔开。
       所以只有「切出两段以上、且**至少两段里出现大题号**」才真的切；
       否则整篇当一道题 —— 宁可让老师手动拆，也不要把一道题切碎成几道废题。
    ⚠️ 题号只在**段首 200 字内**找：卷子第一段常常先有一行卷名（`2024 年卷一`），
       标题之后的 `1.` 才是题号；而正文深处的 `1.` 多半是小问，不该算。
    """
    whole = _norm(text).strip()
    if not whole:
        return []
    parts = [p.strip() for p in _RULE.sub(f"\n\n{_SPLIT}\n\n", whole).split(_SPLIT)]
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return [whole]
    if sum(1 for p in parts if _QNUM.search(p[:200])) >= 2:
        return parts
    return [whole]


def _ensure_mark(text: str, *, source: str = "") -> str:
    """补上 `shike: question` 标记。已经有的话**一个字都不动**（原 frontmatter 的键全保留）。"""
    m = _FM_RE.match(text)
    if m is None:
        head = ["---", "shike: question"]
        if source:
            head.append(f"source: {_yaml_value(source)}")
        head += ["---", ""]
        return "\n".join(head) + "\n" + text.lstrip("\n")
    if _MARK_RE.search(m.group(1)):
        return text
    # 插在 frontmatter 内容的第一行：原有键一个不动
    return text[:m.start(1)] + "shike: question\n" + text[m.start(1):]


def ingest_md(db: Session, path: Path, *, dry_run: bool = False) -> dict:
    """收件箱里的一份 md → 若干道题（入库 + 在 `题库/` 下写出标准模板）。

    返回的报告要能回答老师三个问题：进来了几段、成功了哪些、没成功的是为什么。
    """
    md = read_md(path)
    report: dict = {
        "path": rel(path), "units": 0, "created": [], "skipped": [],
        "warnings": [], "needs_split": False,
    }
    units = split_units(md.text)
    if not units:
        report["skipped"].append({"unit": 0, "reason": "文件是空的"})
        return report
    if len(units) == 1:
        n = count_questions(units[0])
        if n >= 2:
            # 整篇当一道题入库等于造一道废题，不如不进来、把话说清楚
            report["needs_split"] = True
            report["skipped"].append({
                "unit": 0,
                "reason": f"这篇里像是 {n} 道题，但没找到分隔线。请在 Obsidian 里"
                          f"每道题之间空一行加一条 `---`，或拆成一题一个文件。",
            })
            return report

    idx = _index_images()
    for no, raw in enumerate(units, 1):
        text = _ensure_mark(raw, source=path.stem)
        parsed = parse(text)
        if not parsed.ok or not parsed.stem.strip():
            report["skipped"].append({
                "unit": no, "reason": "；".join(parsed.warnings) or "题干是空的",
            })
            continue
        if dry_run:
            report["created"].append({"unit": no, "id": None,
                                      "title": _safe_stem(parsed.stem, "dryrun", 30), "dry": True})
            continue
        plan = _Plan(path=path, existed=None, parsed=parsed, text=text)
        try:
            _settle_plan(db, plan, idx)
            qid = _apply(db, plan)
            q = db.get(Question, qid)
            # `_apply` 把来源记成了这份**收件箱文件**。但它的归宿是 `题库/` 下的标准模板 ——
            # 不清掉的话 `export_question` 会原地重写收件箱里那份，源文件就毁了。
            q.md_path = None
            q.md_hash = None
            db.flush()
            out = export_question(db, q)
            db.commit()
        except Exception as exc:                       # noqa: BLE001 —— 一段坏了不带走别的段
            db.rollback()
            report["skipped"].append({"unit": no, "reason": str(exc)})
            continue
        report["created"].append({"unit": no, "id": qid,
                                  "title": _title_of(q), "md": rel(out)})
        if parsed.warnings:
            report["warnings"].append({"unit": no, "messages": list(parsed.warnings)})

    report["units"] = len(units)
    return report
