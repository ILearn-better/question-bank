# -*- coding: utf-8 -*-
"""请求 / 响应模型。输入一律用 Pydantic 校验，输出为便于前端消费的字典结构。"""
from __future__ import annotations

from typing import Dict, List, Optional

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
    gap: int = 14


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


# ============================================================ 笔记
class NoteIn(BaseModel):
    title: str = "未命名笔记"
    content: str = ""
    ink: List[dict] = Field(default_factory=list)
    pinned: bool = False


class NotePatch(BaseModel):
    """部分更新（笔记的自动保存走这个）。语义同 QuestionPatch：
    「没传」= 保持原样，「显式传空」= 清空。"""

    title: Optional[str] = None
    content: Optional[str] = None
    ink: Optional[List[dict]] = None
    pinned: Optional[bool] = None


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
    # 注意语义与四段不同：四段是「老师写的原料」，doc 是「可以直接发给家长的成品」。
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


class PolishIn(BaseModel):
    """整篇润色请求。

    以前是按字段润色（返回 JSON 回填四个输入框）；现在整体润色：
    把四段拼成一篇原始记录 → AI 照模板整理成一篇文档 → 整篇返回。

    fields   四段原文（常规情况）
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


class DimIn(BaseModel):
    name: str
    curriculum_id: Optional[int] = None
    sort_order: int = 0
