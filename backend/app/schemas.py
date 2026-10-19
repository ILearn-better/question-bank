# -*- coding: utf-8 -*-
"""请求 / 响应模型。输入一律用 Pydantic 校验，输出为便于前端消费的字典结构。"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ============================================================ 文档 / 题目
class CropIn(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class CropStripIn(BaseModel):
    """把多个区域竖着拼成一张图（跨页题目的原貌图）。"""

    regions: List[CropIn]
    gap: int = 0


class QuestionIn(BaseModel):
    document_id: str
    doc_filename: str = ""
    start_block: int = -1
    end_block: int = -1
    content: str = ""
    qtype: str = "解答题"
    difficulty: str = "中档"
    knowledge_points: List[str] = Field(default_factory=list)
    answer: str = ""
    answer_image: str = ""
    analysis: str = ""
    image: str = ""
    # 新增（多体系）
    curriculum_id: Optional[int] = None
    node_id: Optional[int] = None
    source: Optional[str] = None
    year: Optional[int] = None
    stem_format: str = "text"
    tags: List[str] = Field(default_factory=list)
    # 每题分值。可空 —— 不填就不标，绝不会凭空显示一个假分数。
    # ge=0 挡掉负数：负分只可能来自手滑，落到出卷里就是印错的试卷。
    score: Optional[float] = Field(default=None, ge=0, le=200)
    # 出卷时优先印哪种形态（见 models.Question.render_prefer）。
    # 用 Literal 挡掉拼错的值 —— 拼错的字符串不会报错，只会让这一题静默用回默认，
    # 排查时完全看不出问题在哪。
    render_prefer: Literal["auto", "text", "image"] = "auto"

    @field_validator("content", "answer", "answer_image", "analysis", "image", "doc_filename", mode="before")
    @classmethod
    def _none_to_empty(cls, v):  # noqa: ANN001
        return "" if v is None else v


class QuestionPatch(BaseModel):
    """部分更新：给已录的题补答案 / 改标签 / 补知识点。

    全部 Optional 且默认 None，路由层用 model_dump(exclude_unset=True) 取值，
    所以「没传」和「传了 null」是两回事：
      · 没传某个字段      -> 保持原样（不会把已有的答案图误抹）
      · 显式传 null/空串  -> 清空该字段
    """

    content: Optional[str] = None
    qtype: Optional[str] = None
    difficulty: Optional[str] = None
    answer: Optional[str] = None
    answer_image: Optional[str] = None
    analysis: Optional[str] = None
    image: Optional[str] = None
    tags: Optional[List[str]] = None
    knowledge_points: Optional[List[str]] = None
    curriculum_id: Optional[int] = None
    node_id: Optional[int] = None
    source: Optional[str] = None
    year: Optional[int] = None
    score: Optional[float] = Field(default=None, ge=0, le=200)
    render_prefer: Optional[Literal["auto", "text", "image"]] = None
    # 题干里那幅「如图」的图。传空串 = 撤掉（服务端按引用计数回收那张图）。
    figure_image: Optional[str] = None


# ============================================================ 笔记
class NoteIn(BaseModel):
    title: str = "未命名笔记"
    content: str = ""
    ink: List[dict] = Field(default_factory=list)
    pinned: bool = False
    # 放哪个目录。不传就落到「未归档」——新建时永远不会因为没选目录而丢掉归属。
    folder_id: Optional[int] = None


class NotePatch(BaseModel):
    """部分更新（笔记的自动保存走这个）。语义同 QuestionPatch：
    「没传」= 保持原样，「显式传空」= 清空。"""

    title: Optional[str] = None
    content: Optional[str] = None
    ink: Optional[List[dict]] = None
    pinned: Optional[bool] = None
    folder_id: Optional[int] = None
    # 拖动时用：放到该目录里的第 position 位（0-based，按界面显示顺序）。
    # **不是数据列**，是「放在哪」的指令 —— 服务端据此重排同层，重排规则见 note_folders.py。
    position: Optional[int] = None


class QuestionBlockIn(BaseModel):
    """「往笔记里插一道题」的请求。index 只影响题干开头的「练习 N：」。"""
    qid: str
    index: Optional[int] = None


class NoteFolderIn(BaseModel):
    """新建目录。**不传 parent_id = 建一个一级分组**（笔记自己的顶层，跟体系无关）。"""

    parent_id: Optional[int] = None
    name: str = ""


class NoteFolderPatch(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None
    position: Optional[int] = None


# ============================================================ 课后补充
class SupplementItemIn(BaseModel):
    """一项：一道题 / 一篇笔记。标题快照由服务端去查（前端不必先拉一遍）。"""

    kind: str                       # question | note
    ref_id: str


class SupplementIn(BaseModel):
    items: List[SupplementItemIn] = Field(default_factory=list)
    focus: str = ""                 # 针对的知识点（可空）
    note: str = ""                  # 备注（可空）


class SupplementPatch(BaseModel):
    """部分更新。「没传」= 保持原样（跟笔记/题库那两处同一个语义）。"""

    status: Optional[str] = None
    focus: Optional[str] = None
    note: Optional[str] = None


# ============================================================ 课前备课
class PrepItemIn(BaseModel):
    """一项材料：一道题 / 一篇笔记。标题快照由服务端去查（前端不必先拉一遍）。"""

    kind: str                       # question | note
    ref_id: str


class PrepIn(BaseModel):
    """保存课前备课（PUT，一节一条）。

    四个字段 + note_id 都「没传 = 保持原样」；note_id 传空串 = 清除与笔记的关联。
    `items` 特殊：None（没传）= 不动，传了（含空数组）= 整组替换 —— 与作业评分同一语义，
    否则「把挑的材料全撤掉」会变成「不动」，旧材料静默留在库里。
    """

    goal: Optional[str] = None
    key_points: Optional[str] = None
    flow: Optional[str] = None
    materials: Optional[str] = None
    note_id: Optional[str] = None
    items: Optional[List[PrepItemIn]] = None


# ============================================================ 体系 / 知识点
class CurriculumIn(BaseModel):
    code: str
    name: str
    region: Optional[str] = None
    stage: Optional[str] = None
    subject: str = "math"
    color: Optional[str] = None
    sort_order: int = 0


class NodeIn(BaseModel):
    name: str
    parent_id: Optional[int] = None
    level: Optional[int] = None
    code: Optional[str] = None
    sort_order: int = 0


class TreeImportIn(BaseModel):
    """从 JSON 树批量导入（保留原有单体系树的可迁移性）。"""

    tree: dict


# ============================================================ 学生
class StudentIn(BaseModel):
    name: str
    nickname: Optional[str] = None
    grade: Optional[str] = None
    school: Optional[str] = None
    contact: Optional[str] = None
    parent_contact: Optional[str] = None
    hourly_rate: Optional[float] = None
    rate_unit: str = "hour"
    status: str = "active"
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    remark: Optional[str] = None
    curriculum_ids: List[int] = Field(default_factory=list)
    primary_curriculum_id: Optional[int] = None


class StudentPatch(BaseModel):
    name: Optional[str] = None
    nickname: Optional[str] = None
    grade: Optional[str] = None
    school: Optional[str] = None
    contact: Optional[str] = None
    parent_contact: Optional[str] = None
    hourly_rate: Optional[float] = None
    rate_unit: Optional[str] = None
    status: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    remark: Optional[str] = None
    curriculum_ids: Optional[List[int]] = None
    primary_curriculum_id: Optional[int] = None


# ============================================================ 课时
class LessonIn(BaseModel):
    student_id: int
    curriculum_id: Optional[int] = None
    start_at: str                                   # '2026-09-20T19:00'
    duration_min: int = 60
    status: str = "scheduled"
    mode: Optional[str] = None
    location: Optional[str] = None
    rate: Optional[float] = None                    # 不传则取学生当前单价快照
    billable: int = 1
    topic: Optional[str] = None
    node_ids: List[int] = Field(default_factory=list)


class LessonPatch(BaseModel):
    curriculum_id: Optional[int] = None
    start_at: Optional[str] = None
    duration_min: Optional[int] = None
    status: Optional[str] = None
    mode: Optional[str] = None
    location: Optional[str] = None
    rate: Optional[float] = None
    billable: Optional[int] = None
    topic: Optional[str] = None
    node_ids: Optional[List[int]] = None


# ============================================================ 反馈
class AbilityScoreIn(BaseModel):
    dim_id: int
    score: int = Field(ge=1, le=5)


class FeedbackIn(BaseModel):
    """课后反馈。刻意全部可选 —— 允许只写一句话就存，记录成本是生命线。"""

    course_content: Optional[str] = None
    performance: Optional[str] = None
    problems: Optional[str] = None
    homework: Optional[str] = None
    next_plan: Optional[str] = None
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    share_to_parent: int = 0
    ability_scores: List[AbilityScoreIn] = Field(default_factory=list)
    # 配图 URL 数组（走现成的 POST /api/uploads/image 上传，服务端按魔数校验格式）
    images: List[str] = Field(default_factory=list)
    # 整篇正文：AI 按模板整理成文的成品。
    # 注意语义与五段不同：五段是「老师写的原料」，doc 是「可以直接发给家长的成品」。
    # 传 None 表示不动它（所以想清空必须显式传空串）—— 少了这个区分，
    # 任何没带 doc 字段的调用都会把用户辛苦整理出的整篇正文抹掉。
    doc: Optional[str] = None


class TemplateIn(BaseModel):
    """反馈模板。phrases / seeds 都是「字段名 -> 内容」的字典（见 models.FeedbackTemplate）。"""

    name: str
    phrases: Dict[str, List[str]] = Field(default_factory=dict)
    seeds: Dict[str, str] = Field(default_factory=dict)
    sort_order: int = 0


class DocTemplateIn(BaseModel):
    """润色模板：一整个文档的格式与文风参考（整段进提示词）。

    和 TemplateIn 分开，因为两者说的不是一件事：
    那边的 phrases 是「录反馈时一点即插的短语」，这边的 content 是「整篇该长什么样」。
    """

    name: str
    content: str = ""
    sort_order: int = 0


class AiSettingIn(BaseModel):
    """AI 润色配置。api_key 留空表示「不改」—— 否则前端每次保存都会把脱敏后的值写回去。

    要真的删掉密钥得显式传 clear_key=true：光靠传空串区分不了
    「我不想动它」和「我要清掉它」，结果就是用户没有办法把密钥从库里抹掉。
    """

    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[int] = Field(default=None, ge=5, le=300)
    clear_key: bool = False


class ImageRemoveIn(BaseModel):
    """删掉一张配图。url 是上传接口返回的那个本项 URL。"""

    lesson_id: int
    url: str


class PolishIn(BaseModel):
    """整篇润色请求。

    以前是按字段润色（返回 JSON 回填几个输入框）；现在整体润色：
    把五段拼成一篇原始记录 → AI 照模板整理成一篇文档 → 整篇返回。

    fields   五段原文（常规情况）
    draft    也可以直接给一篇整篇草稿，给了就优先用它
    template_id / template_content
             仿照的模板。content 优先于 id —— 老师在弹窗里改了模板内容就该按改过的来
    style    额外要求（语气、详略）
    """

    fields: Dict[str, str] = Field(default_factory=dict)
    draft: Optional[str] = None
    template_id: Optional[int] = None
    template_content: Optional[str] = None
    style: Optional[str] = None
    # 上课文件（讲义/课件）当参考资料。
    # use_files=True 表示用这个课时下所有「抽出了文字」的文件；
    # file_ids 给了就只用这几份（界面上可以让老师勾掉不相关的那份）。
    use_files: bool = True
    file_ids: Optional[List[int]] = None


class AiTestIn(BaseModel):
    """测试连接。允许带上表单里**还没保存**的值 —— 否则刚填完点「测试连接」会
    被告知「还没填全」，用户只能先去保存，很别扭。api_key 留空 = 沿用已保存的那把。"""

    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[int] = Field(default=None, ge=5, le=300)


class RecognizeIn(BaseModel):
    """公式 / 题干识别请求。

    image 收 base64（`data:image/png;base64,...` 或裸 base64 都行），**不收 URL**。
    理由：要识别的图可能还没落盘 —— 老师刚框选完、题还没保存；即便已落盘，
    让后端去反查「这个 URL 对应磁盘上哪个文件」也只是多一处会错的地方。
    前端多走一步 fetch → FileReader，换来后端完全不关心图片从哪来。
    """

    image: str
    mode: str = "question"          # question（题干）/ formula（纯公式）
    hint: Optional[str] = None      # 老师额外给的一句话提示，可空


class DimIn(BaseModel):
    name: str
    curriculum_id: Optional[int] = None
    sort_order: int = 0


# ============================================================ 作业
class HomeworkScoreIn(BaseModel):
    dim_id: int
    score: int = Field(ge=1, le=5)


class HomeworkIn(BaseModel):
    """作业记录（一节课一条）。

    status   submitted（已交）/ late（迟交）/ missing（未交）
             「没布置 / 不用记」= 直接删掉这条记录，而不是存一个空状态。
    scores   与 FeedbackIn.ability_scores 同样的语义：**带了这个字段就整组替换**
             （传空列表 = 清除全部打分），不传才原样保留。
             未交（missing）时允许只记状态、不打分。
    """

    status: str = "submitted"
    note: Optional[str] = None
    scores: List[HomeworkScoreIn] = Field(default_factory=list)


# ============================================================ 出卷样式
class PaperTemplateIn(BaseModel):
    """卷种样式模板。三个字段的形状见 services/paper_style.py。

    都不给默认值而是 Optional：**没传 = 不改这一块**。
    这是有意的 —— 只改名字时不该把排版一起重置回默认值。
    （新建时三个都不传，等于建一套全默认的模板，也是有效的用法。）

    sections 是列表、paper/style 是字典，类型分开写：混成一个 dict 的话，
    前端传错形状只会在渲染时才炸，而那时候已经过了一层 HTTP。
    """

    name: str = ""
    paper: Optional[Dict[str, Any]] = None
    style: Optional[Dict[str, Any]] = None
    sections: Optional[List[Dict[str, Any]]] = None
    sample: str = ""
    sort_order: int = 0


class PaperTemplateCopyIn(BaseModel):
    """由现有模板复制一份（含一份全默认的空模板：source_id 不传即可）。"""

    source_id: Optional[int] = None
    name: str = ""


# ============================================================ 批量入库
class BatchRegionIn(BaseModel):
    """一个题块在页面上的范围（PDF 点坐标，左上角为原点）。"""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float


class BatchBlockIn(BaseModel):
    """一个题块。`regions` 一条 = 不跨页；多条 = 跨页题，服务端竖拼成一张原貌图。

    为什么块里是**列表**而不是单个矩形：一道题跨页在真卷里很常见（尤其解答题），
    而题目只有一个 image 字段。拼成一张就不用为此加字段，尺寸换算也统一在
    `adapters/pdf.py` 的 crop_regions 里做（它已经为「录题页跨页选段」写好并实测过）。
    """

    regions: List[BatchRegionIn]


class BatchCropIn(BaseModel):
    blocks: List[BatchBlockIn]
    gap: int = 0


class BatchItemIn(BaseModel):
    """提交上来的一个题块。image 由 batch-crop 先落盘，这里只带 URL。"""

    image: str
    page_no: Optional[int] = None
    # 主矩形（PDF 点坐标），只作定位线索留档；跨页块这里存第一段。
    region: Optional[List[float]] = None
    document_id: Optional[str] = None
    doc_filename: Optional[str] = None


class BatchJobIn(BaseModel):
    curriculum_id: Optional[int] = None      # 整批体系。必填（服务端会校验）
    document_id: Optional[str] = None
    doc_filename: Optional[str] = None
    items: List[BatchItemIn] = Field(default_factory=list)
    # start=False 只建任务不发起识别。给测试用，也留给「先建好、回头再跑」的场景。
    start: bool = True


class AiPageJobIn(BaseModel):
    """AI 整页识别任务（2026-10-08 实验流程）。

    与 BatchJobIn 唯一的差别是「要识别什么从哪来」：
      块模式（BatchJobIn）：items 是**提交前就裁好**的题块图
      整页模式：pages 是**页号**，页图在识别时由服务端从文档渲染出来

    所以这里没有 items，只有 pages —— 而且**没有条目的概念**：
    一页出几道题是模型说了算，条目在识别时才创建。
    """

    curriculum_id: Optional[int] = None
    document_id: Optional[str] = None
    doc_filename: Optional[str] = None
    pages: List[int] = Field(default_factory=list)
    # start=False 只建不跑（测试与「先建好回头再跑」用），与块模式一致。
    start: bool = True


class BatchItemPatch(BaseModel):
    """审核时改条目。**只改传上来的字段** —— 没传的保持原样。"""

    content: Optional[str] = None
    qtype: Optional[str] = None
    difficulty: Optional[str] = None
    knowledge_point: Optional[str] = None
    node_id: Optional[int] = None
    tags: Optional[List[str]] = None
    # 「这题要不要配图」。AI 给初值，老师可改 —— 改的就是这个字段。
    needs_figure: Optional[bool] = None
    figure_note: Optional[str] = None
    # 配图 URL。传空串 = 撤掉配图（服务端按引用计数回收那张图）。
    figure_image: Optional[str] = None


class FigureCropIn(BaseModel):
    """给某一条待审条目框一幅配图（题干里那句「如图」的那张图）。

    跟 BatchCropIn 一样是「块 = 矩形列表」，因为图也可能跨页 ——
    真卷里图象画在下一页顶端并不少见。跨页时服务端竖拼成一张。
    """

    regions: List[BatchRegionIn]
    gap: int = 0


# ============================================================ 考试情况记录
class ExamFileIn(BaseModel):
    """一份**已经传进暂存区**的附件（cache_key 由 POST /api/exams/upload 给）。

    保存考试时服务端才把它从暂存区搬进正式归档目录并登记。
    为什么要分两步：老师在填表时随时可能改日期/类型，而归档目录名是从这两样来的 ——
    先落盘再登记的话，每改一次日期就得连人带文件搬一次家。
    """

    cache_key: str
    role: Literal["paper", "sheet", "other"] = "other"
    # 上传时的原名。暂存文件名已被安全洗字，带怪字符的名字会差一两个字符 ——
    # 界面把它回传上来，库里 `name` 就还是老师当初选的那份文件的名字。
    name: str = ""


class ExamAnalysisIn(BaseModel):
    """考情分析：选了哪套模板 + 各段写下的文字。

    template_name 与 sections 里的 label 都**快照存下来** ——
    模板日后被改名或删掉，这条记录也得显示得出来（与备课材料 title 快照同理）。
    """

    template_id: Optional[int] = None
    template_name: str = ""
    sections: List[Dict[str, Any]] = Field(default_factory=list)


class ExamIn(BaseModel):
    """新建一次考试记录。

    exam_date 是**自由文本**（'2026-09-15' / '2026-09' / '9 月中旬' 都合法），
    date_key 由服务端解析，前端不用管。
    score / full_score / grade 三者都可留空 —— 有的卷子只报等级，有的老师还没查到分。
    ⚠️ 空值一律存 NULL，**绝不归一成 0**：用 0 顶替会把得分率和趋势一起带偏。
    """

    student_id: int
    exam_date: str = ""
    exam_type: str = ""
    score: Optional[float] = None
    full_score: Optional[float] = None
    grade: str = ""
    analysis: Optional[ExamAnalysisIn] = None
    files: List[ExamFileIn] = Field(default_factory=list)


class ExamPatch(BaseModel):
    """改一次考试记录。

    **没传的字段 = 不改**（与 PaperTemplateIn 同一套语义）。这条很重要：
    只想改个成绩的调用，不该把考情分析整篇抹掉。
    （想清空就显式传 null / 空串 —— 「传了」和「没传」是两件事。）
    """

    exam_date: Optional[str] = None
    exam_type: Optional[str] = None
    score: Optional[float] = None
    full_score: Optional[float] = None
    grade: Optional[str] = None
    analysis: Optional[ExamAnalysisIn] = None
    files: Optional[List[ExamFileIn]] = None


class ExamCacheRemoveIn(BaseModel):
    """删掉暂存区里一份**还没保存**的附件（相当于把「已选」撤回）。"""

    cache_key: str


class ExamFileRoleIn(BaseModel):
    """改一份附件的归类（试题 / 答题卡 / 其他）—— 传错了不用删了重传。"""

    role: Literal["paper", "sheet", "other"]


class ExamAnalysisTemplateIn(BaseModel):
    """考情分析模板。sections 形状见 services / models.ExamAnalysisTemplate。

    sections 给 Optional 而不是默认空列表：**没传 = 不改这几段**，
    这样「只改个模板名」不会把段落结构一起清空。
    """

    name: str = ""
    sections: Optional[List[Dict[str, Any]]] = None
    sort_order: int = 0


# ============================================================ 整体规划（学生级）
class PlanFileIn(BaseModel):
    """一份**已经传进暂存区**的原件（cache_key 由 POST /api/plans/upload 给）。

    保存这条规划时服务端才把它从暂存区搬进正式归档目录并登记。
    为什么要分两步：归档目录名是从**规划标题**来的，而标题随时会改 ——
    先落盘再登记的话，每改一次标题就得连人带文件搬一次家。
    """

    cache_key: str
    # 上传时的原名。暂存文件名已被安全洗字，带怪字符的名字会差一两个字符 ——
    # 界面把它回传上来，库里 `name` 就还是老师当初选的那份文件的名字。
    name: str = ""
    # 这份文件有没有被转成正文（上传时后端已经转过一次，结果以它为准）。
    converted: bool = False
    # 没转成功的原因（界面要照实说，不能让老师以为转换悄悄成功了）。
    note: str = ""


class PlanIn(BaseModel):
    """新建一条规划。title 与 content 至少要有一个有内容 ——
    两样都空的「规划」在列表里就是一个没有标题的空条，点开也是空白。"""

    student_id: int
    title: str = ""
    content: str = ""
    files: List[PlanFileIn] = Field(default_factory=list)


class PlanPatch(BaseModel):
    """改一条规划。**没传的字段 = 不改**（与 ExamPatch / PaperTemplateIn 同一套语义）：
    只想改标题的调用，不该把整篇正文抹掉。
    （想清空就显式传空串 —— 「传了」和「没传」是两件事。）

    ⚠️ files 是**增量**（只搬这次新传的），不是整份清单：
    这样「改个错别字」不会把已归档的原件重新搬一遍、也不会误删。
    要删某一份走 DELETE /api/plan-files/{fid}。
    """

    title: Optional[str] = None
    content: Optional[str] = None
    files: Optional[List[PlanFileIn]] = None


class PlanCacheRemoveIn(BaseModel):
    """删掉暂存区里一份**还没保存**的原件（相当于把「已选」撤回）。"""

    cache_key: str


# ============================================================ 作业批改（0027）
class GradeIn(BaseModel):
    """建一次批改任务。

    提交件走 `file_ids`（`lesson_files.id`）—— 学生交的照片/PDF 已经在「交作业」那一步
    上传归档过了，这里只引用，不重传一份一样的。

    答案的三种来源就对应 `answer_mode`：
      · `ai`     —— 不用老师给，模型自己解题（会标 reference_unverified，老师能展开看它的解法）
      · `key`    —— 老师给答案：`answer_text` 粘文字，或 `answer_file_ids` 给照片/Word
      · `rubric` —— 老师给分步评分细则：`rubric_text`（每步多少分、扣分条件）
    """

    lesson_id: int
    curriculum_id: Optional[int] = None
    file_ids: List[int] = Field(default_factory=list)
    answer_mode: Literal["ai", "key", "rubric"] = "key"
    answer_text: str = ""
    rubric_text: str = ""
    answer_file_ids: List[int] = Field(default_factory=list)


class GradeSetupPatch(BaseModel):
    """开批**之前**改「判定依据」（A1：答案由老师提供，不是模型自己编）。

    为什么单独开一个接口而不是重建任务：页图、提交件、归档目录都已就位，
    重建等于把刚选好的东西丢掉。但也**只允许在没批出结果之前改** ——
    已经批完再换依据，等于拿旧判定冒充新标准，那必须走「重新批改」（会清空结果）。

    字段是 Optional 且 `None = 不改`（同其它 Patch 的语义）；传空串 = 清空。
    """

    answer_mode: Optional[Literal["ai", "key", "rubric"]] = None
    answer_text: Optional[str] = None
    rubric_text: Optional[str] = None


class GradeItemPatch(BaseModel):
    """老师改一道题的结果。**没传的字段 = 不改**（同 ExamPatch / PlanPatch 的语义）。

    ⚠️ 改 `student_answer`（老师发现识别错了，把 3 改成 8）会**清掉这一题的判定、
    把 transcription_version +1** —— 旧判定引用的原文已经不存在了，留着就是
    「引用了一句不存在的话」。服务层负责这件事，前端只要传新文本。
    """

    verdict: Optional[Literal["correct", "partial", "wrong", "blank", "ungradable"]] = None
    score: Optional[float] = None
    qtype: Optional[str] = None
    kp_name: Optional[str] = None
    error_reason: Optional[str] = None
    student_answer: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[Literal["pending", "approved", "rejected"]] = None


class GradeMarkIn(BaseModel):
    """一条标注。坐标是**按页归一化的 0~1000**，前端 viewBox 直接吃。

    `ink` 是老师手写的笔迹（红笔圈、划线、写「重做」）：点列走 `points`，
    `geo` 只放包围盒（橡皮命中、列表统计要用），别指望它精确。
    """

    page_no: int = 1
    kind: Literal["box", "ellipse", "check", "cross", "arrow", "text", "ink"] = "box"
    geo: List[float] = Field(default_factory=list)      # [x0, y0, x1, y1]
    points: List[float] = Field(default_factory=list)   # 手绘折线 [x,y,x,y,…]
    text: str = ""
    color: str = ""
    item_id: Optional[str] = None


class GradeMarkPatch(BaseModel):
    """拖 / 改 / 改颜色。同样「没传 = 不改」。"""

    geo: Optional[List[float]] = None
    points: Optional[List[float]] = None
    text: Optional[str] = None
    color: Optional[str] = None
    kind: Optional[Literal["box", "ellipse", "check", "cross", "arrow", "text", "ink"]] = None


class GradeRegionIn(BaseModel):
    """一题一区域的**一个框**。坐标按页归一化 0~1000（与标注同制，不是像素）。

    `q_no` 空着也行：批改时会回落到模型自己认的题号。填了就以它为准 ——
    这样学生把「第 3 题」写成「第 8 题」时归属仍然是第 3 题。
    """

    q_no: str = ""
    box: List[float] = Field(default_factory=list)      # [x0, y0, x1, y1]


class GradeRegionsPut(BaseModel):
    """**整页替换**老师画的框。

    为什么是整页替换而不是逐条增删：画框这件事在界面上是一次性画完再保存的，
    逐条同步会带来「删了又加、id 对不上」的一堆中间态；整页替换的结果永远等于
    老师屏幕上看到的东西，不会出现「界面上 5 个框、库里有 7 条」。
    """

    page_no: int = 1
    regions: List[GradeRegionIn] = Field(default_factory=list)


class GradeRegionsCopy(BaseModel):
    """从**同一课时的另一份批改**把框拷过来（一套卷只画一次）。

    ⚠️ 拷过来的是坐标本身。不同学生的照片会有平移/旋转，框会偏 ——
    所以界面要提示「宁大勿小」，且拷完建议扫一眼。
    """

    from_grade_id: int


class GradeToBankIn(BaseModel):
    """把一道错题挂到题库已有题上（B1 已定：**不自动建题**）。

    `question_id` 必填 —— 找不到对应题时前端引导老师去「录入新题」，
    走的是题库既有的新增接口，不在这里偷偷造一道。
    """

    question_id: str
    tags: List[str] = Field(default_factory=list)


class GradeConfirmIn(BaseModel):
    """老师确认整份批改。确认之后，统计口径（掌握度 / 家长稿）才会读它。"""

    note: str = ""
