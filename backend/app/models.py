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
    """课后反馈（一节一条，四段式）。"""

    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, default=OWNER_ID, server_default=str(OWNER_ID))
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), nullable=False
    )
    performance: Mapped[str | None] = mapped_column(Text)   # 课堂表现
    problems: Mapped[str | None] = mapped_column(Text)      # 存在问题
    homework: Mapped[str | None] = mapped_column(Text)      # 作业布置
    next_plan: Mapped[str | None] = mapped_column(Text)     # 下次安排
    rating: Mapped[int | None] = mapped_column(Integer)     # 1-5 综合状态
    share_to_parent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, default=_now, server_default=NOW)

    lesson: Mapped["Lesson"] = relationship(back_populates="feedback")

    __table_args__ = (
        UniqueConstraint("lesson_id", name="uq_feedback_lesson"),
        Index("idx_feedbacks_student", "student_id"),
    )
