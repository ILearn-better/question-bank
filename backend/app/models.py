# -*- coding: utf-8 -*-
"""数据模型（SQLAlchemy 2.0）。

约定（对应开发文档 §5.2）：
  - 时间统一存 TEXT，本地时间字符串，与既有数据保持同构
  - 所有「层级根实体」带 owner_id（自用固定 1）——现在加一列成本为零，
    将来做多租户时不必全库改表（详见文档 §12.1 第 1 条）
  - M0 共享内核：Curriculum / Node / Resource
  - M1 题库：Document / Question / QuestionNode
  - M2 学生：Student / StudentCurriculum / AbilityDim / AbilityScore / Feedback
  - M3 课表：Lesson（枢纽实体：student_id + node_ids + amount 串起三大模块）
"""
from datetime import datetime

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    REAL,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import OWNER_ID

NOW = text("(datetime('now','localtime'))")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Base(DeclarativeBase):
    pass


# ============================================================
# M0 共享内核
# ============================================================
class Curriculum(Base):
    """体系：多体系是一等公民，新增体系 = 插数据，不改代码。"""

    __tablename__ = "curricula"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    code: Mapped[str] = mapped_column(String, nullable=False, unique=True)   # dse-math
    name: Mapped[str] = mapped_column(String, nullable=False)                # DSE 数学
    region: Mapped[str | None] = mapped_column(String)                       # HK / UK / CN
    stage: Mapped[str | None] = mapped_column(String)                        # junior / senior / exam
    subject: Mapped[str] = mapped_column(String, nullable=False, default="math", server_default="math")
    color: Mapped[str | None] = mapped_column(String)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    nodes: Mapped[list["Node"]] = relationship(
        back_populates="curriculum", cascade="all, delete", passive_deletes=True
    )


class Node(Base):
    """知识点树节点。level：1 模块/章 2 节 3 知识点 4 考点（体系本身由 curricula 承载）。"""

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    curriculum_id: Mapped[int] = mapped_column(
        ForeignKey("curricula.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str | None] = mapped_column(String)      # 官方大纲编号，如 '2.3'
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    curriculum: Mapped["Curriculum"] = relationship(back_populates="nodes")
    children: Mapped[list["Node"]] = relationship(
        back_populates="parent", cascade="all, delete", passive_deletes=True
    )
    parent: Mapped["Node | None"] = relationship(back_populates="children", remote_side=[id])

    __table_args__ = (
        Index("idx_nodes_curr", "curriculum_id", "level"),
        Index("idx_nodes_parent", "parent_id"),
    )


class Resource(Base):
    """教材 / 资料归档。"""

    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    curriculum_id: Mapped[int | None] = mapped_column(ForeignKey("curricula.id"))
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"))
    title: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)   # textbook/exam_paper/worksheet/note/syllabus
    file_path: Mapped[str | None] = mapped_column(Text)         # 相对 DATA_DIR
    doc_id: Mapped[str | None] = mapped_column(String)          # 复用 documents 表
    file_size: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    __table_args__ = (Index("idx_res_curr_kind", "curriculum_id", "kind"),)


# ============================================================
# M1 教材 / 题库
# ============================================================
class Document(Base):
    """上传的试卷原件与解析出的内容块（沿用既有表结构）。

    preview_pdf / preview_error 是为「Word 也能在页面上画框选区」加的：
    Word 上传后由本机 Word 导出一份 PDF 作为页面视图的数据源，
    两份信息都入库，而不是靠猜文件在不在 ——
      preview_pdf   有值 = 转换成功，页面视图可用（存相对 DATA_DIR 的路径）
      preview_error 有值 = 转换失败的原因，直接显示给用户，并避免每次请求都重试
    """

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    filename: Mapped[str | None] = mapped_column(String)
    filetype: Mapped[str | None] = mapped_column(String)
    block_count: Mapped[int | None] = mapped_column(Integer)
    blocks: Mapped[str | None] = mapped_column(Text)            # JSON 数组
    created_at: Mapped[str | None] = mapped_column(Text)
    file_path: Mapped[str | None] = mapped_column(Text)         # 相对 DATA_DIR
    scanned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    preview_pdf: Mapped[str | None] = mapped_column(Text)       # Word 转出的 PDF（相对 DATA_DIR）
    preview_error: Mapped[str | None] = mapped_column(Text)     # 转换失败原因
    preview_pages: Mapped[int | None] = mapped_column(Integer)  # 转换时的页数（列表展示用）


class Question(Base):
    """题目。knowledge_points(JSON) 为兼容旧数据保留，新关系走 question_nodes。

    tags 是自由标签（JSON 数组），存字符串而不建表的原因：标签是「随手打的分类」，
    没有层级、没有权重，出卷筛选只按「任一命中」。等哪天要做重命名/合并/统计，
    再升级成表也不迟（数据迁移很简单）。
    """

    __tablename__ = "questions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    document_id: Mapped[str | None] = mapped_column(String)
    doc_filename: Mapped[str | None] = mapped_column(String)
    start_block: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")
    end_block: Mapped[int] = mapped_column(Integer, default=-1, server_default="-1")
    content: Mapped[str | None] = mapped_column(Text)
    qtype: Mapped[str | None] = mapped_column(String)
    difficulty: Mapped[str | None] = mapped_column(String)
    knowledge_points: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    answer: Mapped[str | None] = mapped_column(Text)
    analysis: Mapped[str | None] = mapped_column(Text)
    image: Mapped[str] = mapped_column(Text, default="", server_default="")
    answer_image: Mapped[str] = mapped_column(Text, default="", server_default="")
    # 题干补充配图 —— 题干里那幅「如图」的图，单独框出来的一张。
    # 与 image 不是一回事：image 是**整道题**的原貌截图（含全部文字），
    # 出卷选「图片」形态时印的是它，框出来的这幅图自然也在里面；
    # 而选「文本」形态时 image 整张都不印，题里的图就只能靠这一列补上。
    # 所以两列必须分开存，出卷时按形态取用（见 services/paper_export.py）。
    figure_image: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[str | None] = mapped_column(Text)
    # —— 新增（M1 多体系化）——
    curriculum_id: Mapped[int | None] = mapped_column(ForeignKey("curricula.id"))
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"))   # 主知识点
    source: Mapped[str | None] = mapped_column(String)                    # '2024 DSE Paper 1'
    year: Mapped[int | None] = mapped_column(Integer)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_used_at: Mapped[str | None] = mapped_column(Text)
    stem_format: Mapped[str] = mapped_column(String, default="text", server_default="text")
    tags: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    # 每题分值。可空是刻意的：老数据没有分值，出卷时按「不标分值」处理，
    # 绝不拿 0 或某个默认值去填 —— 印错分数的试卷比不印分数严重得多。
    score: Mapped[float | None] = mapped_column(REAL)
    # 出卷时这一题优先印哪种形态。录题时「原貌图」和「识别文本」常常同时在，
    # 到底印哪个是老师对**这一题**的判断（公式题印文本更清楚、图形题只能印图），
    # 替他定就是印错。所以每题存一个偏好，出卷时还能整卷统一覆盖。
    #   auto  —— 有文本用文本，没文本用图（默认；也兼容老数据）
    #   text  —— 强制用文本（没有文本时自动退回图，不印空白题）
    #   image —— 强制用图（没有图时自动退回文本）
    render_prefer: Mapped[str] = mapped_column(String, default="auto", server_default="auto")

    __table_args__ = (Index("idx_questions_curr", "curriculum_id", "node_id"),)


class QuestionNode(Base):
    """题目 ↔ 知识点 多对多。weight：主考点 1.0 / 涉及 0.5，用于掌握度加权。"""

    __tablename__ = "question_nodes"

    question_id: Mapped[str] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    weight: Mapped[float] = mapped_column(REAL, default=1.0, server_default="1.0")

    __table_args__ = (Index("idx_qn_node", "node_id"),)


class BatchJob(Base):
    """批量入库任务：一次提交的一批题块，共用一次并行识别。

    ⚠️ 为什么待审条目单独一张表，而不是给 questions 加个 status 字段：
        - questions 的语义保持纯粹（「已确认可用的题」），出卷 / 作业 / 反馈 /
          笔记插题这些下游一行都不用改；
        - 驳回的、识别失败的脏数据**永远进不了题库**；
        - 批量导入中途失败只影响这张表，不会在 questions 里留下半截题目。
      也就是说「待审」只是批量导入这条入口的中间状态，不是题目的必经状态。

    ⚠️ 为什么叫 batch 而不是 import：`import` 是 Python 关键字，
       表名/模块名带上它，写 `from app.services import import_xxx` 时处处别扭。
    """

    __tablename__ = "batch_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    # queued 刚建好 / running 识别中 / done 全部完成 / partial 有失败 / failed 一条没成
    status: Mapped[str] = mapped_column(String, default="queued", server_default="queued")
    total: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    done: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 整批指定的体系。**刻意不让模型判体系** —— 一份卷子一套体系，
    # 模型判错的代价是整批错到出卷，而老师选一次的成本是零。
    curriculum_id: Mapped[int | None] = mapped_column(ForeignKey("curricula.id"))
    document_id: Mapped[str | None] = mapped_column(String)
    doc_filename: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list["BatchItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="BatchItem.seq"
    )


class BatchItem(Base):
    """待审条目：一个题块 + 模型给出的结构化结果。"""

    __tablename__ = "batch_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("batch_jobs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    document_id: Mapped[str | None] = mapped_column(String)
    doc_filename: Mapped[str | None] = mapped_column(String)
    page_no: Mapped[int | None] = mapped_column(Integer)
    region: Mapped[str | None] = mapped_column(Text)          # JSON [x0,y0,x1,y1]，PDF 点坐标
    image: Mapped[str] = mapped_column(Text, default="", server_default="")   # 原貌图 URL

    # ---- 题干配图（「如图」那张图）----
    # 为什么要单独一套：整块原貌图里当然有图，但**出卷走文本形态时原貌图整张都用不上**，
    # 题里的图就丢了。所以让模型先判断「这题要不要图」，再由老师框选出那一幅存下来。
    #
    # needs_figure 是 AI 给的初值（0/1），**待在待审页被老师改** —— 模型判图不可靠，
    # 用户点名要求「再加上人工判断逻辑」。所以这里存的是「当前结论」，
    # AI 的原话另存在 figure_note 里，页面才能显示「模型说有图，你确认了没」。
    needs_figure: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    figure_note: Mapped[str | None] = mapped_column(Text)     # 模型对这张图的描述
    figure_image: Mapped[str] = mapped_column(Text, default="", server_default="")  # 老师框选的配图

    # ---- 模型给的那几列。列名与 questions 对齐，通过时直接搬过去 ----
    content: Mapped[str | None] = mapped_column(Text)
    qtype: Mapped[str | None] = mapped_column(String)
    difficulty: Mapped[str | None] = mapped_column(String)
    # 模型给的是**名字**（自由文本），不是 node_id：它不可能知道我们的节点 id。
    # 落库时在所选体系的知识树里做模糊匹配填 node_id，匹配不上就只留名字。
    knowledge_point: Mapped[str | None] = mapped_column(String)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id"))
    tags: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    confidence: Mapped[str | None] = mapped_column(String)     # high | medium | low
    note: Mapped[str | None] = mapped_column(Text)             # 模型自述的不确定点

    # 后端校验留下的痕迹。JSON 数组，例如 ["qtype_fallback","json_repaired"]。
    # 有它才能让审核页把「模型自己拿的主意」标出来 —— 没标的话老师看不出来
    # 这个「解答题」是模型判断的，还是枚举不合法被后端兜的。
    flags: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    raw: Mapped[str | None] = mapped_column(Text)              # 模型原始返回（截断），排查用
    status: Mapped[str] = mapped_column(String, default="pending", server_default="pending")
    question_id: Mapped[str | None] = mapped_column(String)     # 通过后指向生成的题目
    error: Mapped[str | None] = mapped_column(Text)             # 单条失败原因
    created_at: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[str | None] = mapped_column(Text)

    job: Mapped["BatchJob"] = relationship(back_populates="items")

    __table_args__ = (Index("idx_batch_items_job", "job_id", "status"),)


# ============================================================
# M2 学生管理
# ============================================================
class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    name: Mapped[str] = mapped_column(String, nullable=False)
    nickname: Mapped[str | None] = mapped_column(String)
    grade: Mapped[str | None] = mapped_column(String)          # '中三' / 'Year 11' / '高一'
    school: Mapped[str | None] = mapped_column(String)
    contact: Mapped[str | None] = mapped_column(String)
    parent_contact: Mapped[str | None] = mapped_column(String)  # 敏感，导出时脱敏
    hourly_rate: Mapped[float | None] = mapped_column(REAL)     # 默认单价，实际以 Lesson 快照为准
    rate_unit: Mapped[str] = mapped_column(String, default="hour", server_default="hour")
    status: Mapped[str] = mapped_column(String, default="active", server_default="active")
    started_at: Mapped[str | None] = mapped_column(String)
    ended_at: Mapped[str | None] = mapped_column(String)
    remark: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    curricula: Mapped[list["StudentCurriculum"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )
    lessons: Mapped[list["Lesson"]] = relationship(
        back_populates="student", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("idx_students_status", "status"),)


class StudentCurriculum(Base):
    """学生 ↔ 体系（一个学生可同时学 DSE + 国内课程）。"""

    __tablename__ = "student_curricula"

    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), primary_key=True
    )
    curriculum_id: Mapped[int] = mapped_column(
        ForeignKey("curricula.id", ondelete="CASCADE"), primary_key=True
    )
    is_primary: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    student: Mapped["Student"] = relationship(back_populates="curricula")
    curriculum: Mapped["Curriculum"] = relationship()


class AbilityDim(Base):
    """能力维度（老师自定义，家长报告雷达图的内容骨架，不依赖题库）。"""

    __tablename__ = "ability_dims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    name: Mapped[str] = mapped_column(String, nullable=False)
    curriculum_id: Mapped[int | None] = mapped_column(ForeignKey("curricula.id"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    active: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class AbilityScore(Base):
    """能力评分（1-5，老师主观打分）。"""

    __tablename__ = "ability_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    dim_id: Mapped[int] = mapped_column(ForeignKey("ability_dims.id"), nullable=False)
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"))
    period: Mapped[str | None] = mapped_column(String)      # 或按阶段 '2026-09'
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    dim: Mapped["AbilityDim"] = relationship()

    __table_args__ = (Index("idx_abscore_student", "student_id", "dim_id"),)


# ============================================================
# M3 课表 / 课时费
# ============================================================
class Lesson(Base):
    """课时记录 —— 连接三大模块的枢纽。

    student_id → M2 学生；node_ids → M1 知识点；rate/amount → 计费。
    ⚠️ rate 是「单价快照」：学生后来涨价，不改历史课时的金额。
    """

    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), nullable=False)
    curriculum_id: Mapped[int | None] = mapped_column(ForeignKey("curricula.id"))
    start_at: Mapped[str] = mapped_column(String, nullable=False)     # '2026-09-20T19:00'
    duration_min: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="scheduled", server_default="scheduled"
    )  # scheduled/done/cancelled/leave/makeup/moved
    mode: Mapped[str | None] = mapped_column(String)                  # online/offline
    location: Mapped[str | None] = mapped_column(String)
    rate: Mapped[float | None] = mapped_column(REAL)                  # 单价快照
    billable: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    amount: Mapped[float | None] = mapped_column(REAL)                # 应计金额快照
    topic: Mapped[str | None] = mapped_column(String)
    node_ids: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")  # JSON 数组
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    student: Mapped["Student"] = relationship(back_populates="lessons")
    feedback: Mapped["Feedback | None"] = relationship(
        back_populates="lesson", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        Index("idx_lessons_student_time", "student_id", "start_at"),
        Index("idx_lessons_time", "start_at"),
    )


class Feedback(Base):
    """课后反馈（一节一条，五段式）。"""

    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    course_content: Mapped[str] = mapped_column(Text, default="", server_default="")  # 课程内容（本次讲了什么）
    performance: Mapped[str | None] = mapped_column(Text)   # 课堂表现
    problems: Mapped[str | None] = mapped_column(Text)      # 存在问题
    homework: Mapped[str | None] = mapped_column(Text)      # 作业布置
    next_plan: Mapped[str | None] = mapped_column(Text)     # 下次安排
    # 整篇正文：按润色模板整理成文的成品（可直接发给家长）。
    # 上面五个字段是老师随手写的**原料**，这里是**成品** —— 两者并存，导出时二选一。
    # 之所以单独存一列而不是让 AI 把长文拆回几个字段：那些栏目标题
    # （【易错内容】【作业预计时长】…）根本塞不进「五段」这个形状里。
    doc: Mapped[str] = mapped_column(Text, default="", server_default="")
    rating: Mapped[int | None] = mapped_column(Integer)     # 1-5 综合状态
    share_to_parent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 配图：URL 的 JSON 数组。存 JSON 而不是建关联表 —— 和 notes.ink / questions.tags
    # 同一套路数：图片没有额外属性、也不参与查询，只是跟着主体一起取出来渲染。
    images: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    lesson: Mapped["Lesson"] = relationship(back_populates="feedback")

    __table_args__ = (
        UniqueConstraint("lesson_id", name="uq_feedback_lesson"),
        Index("idx_feedbacks_student", "student_id"),
    )


class HomeworkDim(Base):
    """作业维度（与课堂的 ability_dims **分开**，用户明确要求的那一条）。

    为什么两套而不是共用一套：课堂评「这节课他表现如何」，作业评「他自己独立做题的
    质量如何」—— 前者看当场反应，后者看自主完成度，评价角度不同。共用的话两张雷达图
    会重复，也说不清变化到底来自哪边。

    默认 5 条：完成度 / 正确率 / 过程与规范 / 独立完成 / 订正与复盘（在 0013 迁移里种下）。
    跟 ability_dims 一样是老师可改可停用的 —— 教不同科目换一套维度即可。
    """

    __tablename__ = "homework_dims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    name: Mapped[str] = mapped_column(String, nullable=False)
    curriculum_id: Mapped[int | None] = mapped_column(ForeignKey("curricula.id"))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    active: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class LessonSupplement(Base):
    """课后补充：老师给这个学生挑好的材料（下次上课打印给他）。

    与作业方向相反，所以是**两张表、两块并列**，不塞进 homeworks：
      · homeworks 是**收**：他交回来的 + 我对它的评价（一课一条）。
      · 这里是**发**：我准备下次给他的（一节课可以有好几批 —— 今天发 3 题，
        晚上又想加 2 题，是常态）。
    两者都挂在同一节课上，所以学生详情页那条时间轴不用另开一栏就「一目了然」。

    为什么记 status：打印发生在**下一次课**，中间隔几天。不记的话，
    「上节课我准备了点东西」和「到底给没给」就分不出来 —— 那记录就白记了。
    """

    __tablename__ = "lesson_supplements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    # 针对的知识点：复用题库那套「知识点一律用名字」（不是外键，认名字即可）
    focus: Mapped[str] = mapped_column(Text, default="", server_default="")
    note: Mapped[str] = mapped_column(Text, default="", server_default="")   # 备注，可空
    status: Mapped[str] = mapped_column(String, nullable=False, default="todo", server_default="todo")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    items: Mapped[list["LessonSupplementItem"]] = relationship(
        back_populates="supplement", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("idx_supplements_lesson", "lesson_id", "id"),)


class LessonSupplementItem(Base):
    """一批里面的一项：一道题 / 一篇笔记。

    `title` 是**标题快照**：题库里的题或笔记被删了之后，这条记录还得能自解释
    （显示「《因式分解巩固 8 题》（内容已删）」而不是一片空白）。
    这里存引用 id 就够 —— 它是「我当时发了什么」的日志，内容变了不影响日志的意义
    （区别于「笔记正文里插题」：那边必须连图片与文字一起快照，见 doc_to_note/notes 那条线）。
    """

    __tablename__ = "lesson_supplement_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    supplement_id: Mapped[int] = mapped_column(
        ForeignKey("lesson_supplements.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)     # question | note
    ref_id: Mapped[str] = mapped_column(String, nullable=False)   # questions.id 或 notes.id
    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    supplement: Mapped["LessonSupplement"] = relationship(back_populates="items")

    __table_args__ = (Index("idx_supplement_items", "supplement_id", "sort_order"),)


class Homework(Base):
    """一次作业的记录（一节课一条）。

    为什么独立于 feedbacks：老师完全可能只记「作业没交」而先不写反馈，也可能先收了
    作业照片、隔天再写反馈、再打分 —— 挂在反馈上会把两件事互相绑死。
    粒度仍是一节一条（lesson_id 唯一）：跟反馈一致，老师的心智模型就是「这次课的作业」。

    状态只有三种，而**「没布置 / 不用记」= 没有这一行**，不存一个空状态 ——
    省掉「空状态算不算分母」这种没人想得清的问题。
    missing（未交）也只记状态、**不打分**：用 0 分记会把平均值和雷达图一起带偏，
    看起来像退步，而事实可能只是那天没写。

    作业原件不在这里：复用 lesson_files（role='homework'）那套上传/抽文字/归档/清理。
    """

    __tablename__ = "homeworks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String, nullable=False, default="submitted", server_default="submitted")
    note: Mapped[str] = mapped_column(Text, default="", server_default="")   # 老师的批改备注
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)
    updated_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("lesson_id", name="uq_homeworks_lesson"),
        Index("idx_homeworks_student", "student_id", "id"),
    )


class HomeworkScore(Base):
    """作业评分（1-5，老师主观打分）。

    构造与 AbilityScore 完全对齐（student × dim × 1-5），所以雷达图、「本次 vs 上次」、
    趋势的代码可以照拄 —— 但同时它是**另一张表**，两边不可能混在一起。
    只存真正打过的分：没打分的维度就是没有行，不要存 0。
    """

    __tablename__ = "homework_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    homework_id: Mapped[int] = mapped_column(
        ForeignKey("homeworks.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    dim_id: Mapped[int] = mapped_column(ForeignKey("homework_dims.id"), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    dim: Mapped["HomeworkDim"] = relationship()

    __table_args__ = (Index("idx_hwscore_student", "student_id", "dim_id"),)


class LessonFile(Base):
    """上课文件（讲义 / 课件 / 试卷），AI 润色时当参考资料用。

    为什么存的是**抽出来的文字**而不是「把文件发给 AI」：
      接的是 OpenAI 兼容的 /chat/completions，这是**纯文本**协议 ——
      deepseek-chat、qwen-plus 这些主流模型都不接受文件本身。所以在本机把文字抽出来，
      再把文字发出去：任何文本模型都能用，而且发出去的内容在界面上看得见。
    原件也留一份（stored）：抽失败时能换个解析器重试，也算个出处。

    status/reason 要如实记下「没抽出来」和原因 —— 一个静默失败的附件比报错更糟，
    用户会以为 AI 已经读过那份材料了。
    """

    __tablename__ = "lesson_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)      # 原始文件名（给用户看的）
    stored: Mapped[str] = mapped_column(String, default="", server_default="")   # 磁盘上的名字
    # material（上课材料：讲义/课件/试卷）/ homework（学生作业原件）
    # 作业原件走同一套上传/抽文字/归档/清理，只是角色不同；默认 material，老数据语义不变。
    # **两份清单必须互相看不见**：写反馈时看到的「上课文件」只列 material；
    # 作业那里只列 homework。否则老师会在反馈的文件列表里看到学生的作业照片，
    # 而 AI 润色也不该把作业当「参考资料」读进去（见 feedbacks 里取 materials 的地方）。
    role: Mapped[str] = mapped_column(String, nullable=False, default="material", server_default="material")
    kind: Mapped[str] = mapped_column(String, default="", server_default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pages: Mapped[int | None] = mapped_column(Integer)
    chars: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    truncated: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String, default="ok", server_default="ok")
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    text: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    __table_args__ = (Index("idx_lesson_files_lesson", "lesson_id", "id"),)


# ============================================================
# M4 笔记（Markdown 正文 + 一层板书笔画）
# ============================================================
class NoteFolder(Base):
    """笔记目录树的节点（迁移 0014）。

    树形：体系根 → 目录… → 笔记。**不复用题库的 `nodes`**：那是官方大纲，
    学习路径定死；笔记目录是老师自己的整理习惯，要能随手拖。两者混在一棵树上，
    结果就是两边都不能动。

    两个容易写歪的地方：

      · `is_root` —— 每个体系一棵，根行承载「这棵树属于哪个体系」。
        有根行，整棵树就只有一种结构（笔记一律有父，归属沿 parent_id 往上走就能定）；
        不然「笔记属于哪个体系」会有两个来源，一处漏判就把笔记分错体系。

      · `curriculum_id` **只有根行有值**，非根行一律 NULL。它不是每行的冗余属性；
        冗余就得在移动目录时同步整棵子树，多一个能写歪的地方。

      · `is_unfiled` —— **只有「未归档」那一行是 1**。它是兜底容器：
        新建笔记没选目录时的落点、以及删掉某个分组后内容的去处。
        这个标记是显式加上去的（迁移 0015）：以前靠「curriculum_id 为空」认它，
        那个认法依赖「一级节点必然来自体系」这条前提 —— 前提一旦松动就会认错，
        而认错的后果很具体（把用户的分组当成兜底容器，或者反过来）。
        现在就直接读这个字段，不再做推断。
    """

    __tablename__ = "note_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    curriculum_id: Mapped[int | None] = mapped_column(ForeignKey("curricula.id", ondelete="SET NULL"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("note_folders.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String, nullable=False)
    is_root: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_unfiled: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    __table_args__ = (
        Index("idx_note_folders_parent", "parent_id", "sort_order"),
        Index("idx_note_folders_curriculum", "curriculum_id"),
    )


class Note(Base):
    """一页笔记：Markdown 正文 + 一层矢量笔画。

    为什么笔画存 JSON 而不渲染成 PNG 再存图片：
      橡皮、换颜色、调粗细、撤销，本质都是「把已画的线按新状态重画一遍」。
      存成位图就只能整张重来，也存不了「这条线是什么颜色多粗」。
      矢量数据本身很小（一页板书通常几十 KB），而且换窗口大小、缩放都不糊。
    坐标按内容框归一化到 0~1，所以窗口尺寸/字号变化时笔画不会跑位。
    """

    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    # 所在目录（笔记目录树的节点）。可空只是**安全网**：应用层一律写值，
    # 万一目录行被别的路径删掉，笔记会退回「未归档」而不是消失。
    # 外键带名字：0014 用 batch 模式重建表（SQLite 加带约束的列只能重建），
    # 而重建时匿名约束对不上号，所以库里的名字和这里保持一致。
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("note_folders.id", ondelete="SET NULL", name="fk_notes_folder"))
    # 同一目录里的手排顺序（拖动出来的）。拖动只改它，**不动 updated_at** ——
    # 那是「最后编辑」，把笔记拖个位置不该算改动。
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    title: Mapped[str] = mapped_column(String, nullable=False, default="未命名笔记", server_default="未命名笔记")
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    ink: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")     # 笔画数组（JSON）
    pinned: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)
    updated_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    __table_args__ = (
        Index("idx_notes_updated", "updated_at"),
        Index("idx_notes_folder", "folder_id"),
    )


# ============================================================
# 反馈模板与 AI 配置
# ============================================================
class FeedbackTemplate(Base):
    """课后反馈模板。

    为什么存数据库而不是写死在代码里：
      每个老师的行文习惯差很多（有的写「状态不错，配合度高」，有的写「本节掌握情况良好」），
      模板这种东西只有让用户自己改才用得上。写死等于逼所有人用同一个腔调。

    phrases：每段的快捷短语（一点即插），JSON：{"performance": [...], ...}
    seeds  ：每段的起始骨架文本，JSON：{"performance": "...", ...}，可为空
    is_builtin：内置模板，允许改文案但不允许删（删了下次种子又会建回来，反而迷惑）
    """

    __tablename__ = "feedback_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    name: Mapped[str] = mapped_column(String, nullable=False)
    phrases: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    seeds: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    is_builtin: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    __table_args__ = (Index("idx_fb_templates_owner", "owner_id", "sort_order"),)


class FeedbackDocTemplate(Base):
    """润色时「仿照的模板」—— 一整篇文档的格式与文风参考。

    和 FeedbackTemplate 分开建表，因为两者形状根本不同：
      · FeedbackTemplate：按四个字段分的快捷短语 + 骨架文本（**录反馈时**用）
      · FeedbackDocTemplate：一整篇成品文档的结构与语气（**AI 润色时**用，整段塞进提示词）

    为什么不把示例文档直接写死在提示词里：每个老师给家长的格式差得很远
    （有的要抬头带科目/任课老师，有的只写四段），只有让用户自己给模板才迁就得过来。
    content 里既放**结构说明**也放**脱敏示例**：光说「写得具体一点」没用，
    给一段能照着学的文字，模型的语气才对得上。
    is_builtin：内置模板允许改文案但不允许删（删了下一次种子又会建回来，反而迷惑）。
    """

    __tablename__ = "feedback_doc_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    name: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    is_builtin: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    __table_args__ = (Index("idx_fb_doc_templates_owner", "owner_id", "sort_order"),)


class PaperTemplate(Base):
    """出卷的「卷种样式」：一套卷面长什么样，决定导出 HTML / Word / PDF 的版式。

    为什么存数据库而不是把四套写死在渲染代码里：
      高考 / 中考 / DSE / A-Level 只是**起点**。同一场考试不同年份、不同学校
      的抬头和说明都不一样，写死等于每次改一行说明都要改代码。
      存成数据后，「复制内置模板 → 改两个字段」就是一次普通的界面操作。

    形状（四个 JSON 字段，都在 services/paper_style.py 里解析）：
      paper    抬头区 —— 副标题、考试说明、注意事项逐条、姓名栏字段
      style    排版   —— 字号、行距、页边距、题间距、答题留白、题号样式
      sections 分区   —— 按题型把题目分组（"一、选择题" / "Section A"）
      sample   示例   —— 一句「这套长什么样」的说明，仅供界面上给人看

    sections 为空数组 = **不分区**，题目按用户排的顺序平铺（自定义模板的默认形态）。
    is_builtin：内置模板允许改但不允许删 —— 删了下次启动种子又会建回来，反而迷惑。
    """

    __tablename__ = "paper_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    name: Mapped[str] = mapped_column(String, nullable=False)
    code: Mapped[str] = mapped_column(String, default="", server_default="")   # 内置标识，如 gaokao
    paper: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    style: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    sections: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    sample: Mapped[str] = mapped_column(Text, default="", server_default="")
    is_builtin: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    __table_args__ = (Index("idx_paper_templates_owner", "owner_id", "sort_order"),)


class AiSetting(Base):
    """AI 润色的接口配置（单行，owner_id 唯一）。
    走 OpenAI 兼容的 /chat/completions 协议：DeepSeek、通义、Kimi、本地 Ollama / vLLM
    都是这个格式，所以只存 base_url + model + api_key 就能对接绝大多数服务，
    不需要为每家写一个适配器。

    ⚠️ api_key 以明文存本机数据库。这是「单机自用」形态下的取舍：
       存环境变量更安全，但那样就没法在设置页里改。因此接口面上一律**脱敏返回**
       （只回 has_key 与后 4 位），绝不把完整 key 发回浏览器。
    """

    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, default=OWNER_ID, server_default=str(OWNER_ID))
    base_url: Mapped[str] = mapped_column(String, default="", server_default="")
    model: Mapped[str] = mapped_column(String, default="", server_default="")
    api_key: Mapped[str] = mapped_column(Text, default="", server_default="")
    timeout: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    updated_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)
