import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, DateTime, JSON, Integer, Float, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from models.database import Base


def _utcnow() -> datetime:
    """Naive UTC now（替代已弃用的 datetime.utcnow，存储格式不变）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def gen_id():
    return uuid.uuid4().hex[:12]


class Question(Base):
    __tablename__ = "questions"

    id = Column(String, primary_key=True, default=gen_id)
    folder_path = Column(String, unique=True, nullable=False)
    subject = Column(String, default="")
    grade = Column(String, default="")
    knowledge_tags = Column(JSON, default=list)
    raw_image_path = Column(String, default="")
    ocr_text = Column(Text, default="")
    question_html = Column(Text, default="")
    answer_html = Column(Text, default="")
    standard_answer = Column(Text, default="")
    question_type = Column(String, default="")
    score_points_html = Column(Text, default="")
    diagrams = Column(JSON, default=list)
    diagram_places = Column(JSON, default=list)
    diagram_description = Column(Text, default="")
    status = Column(String, default="pending")
    error_message = Column(Text, default="")
    source_type = Column(String, default="photo")
    capture_mode = Column(String, default="single_question")
    capture_group_id = Column(String, default="")
    capture_index = Column(Integer, default=0)
    bank = Column(String, default="default")
    region = Column(String, default="")
    # 难度分层：基础 / 中档 / 难 / 自招。空串 = 未标。自招分层与组卷按此筛。
    difficulty = Column(String, default="")
    avg_score = Column(Float, nullable=True)
    user_hint = Column(Text, default="")
    audit_flags = Column(JSON, default=list)  # 审计标记列表
    is_resolved = Column(Boolean, default=False)  # 用户标记为已解决
    handwriting_notes = Column(Text, default="")  # 手写笔记（AI识别，不出现在题面）
    structure_graph = Column(JSON, default=None)  # 结构梳理图数据 {nodes, edges}
    structure_graph_info = Column(JSON, default=None)  # 检查状态/评分 {valid, score, issues, suggestions}
    image_roles = Column(JSON, default=None)        # 多图角色 [{path, role, order}]
    multi_images = Column(JSON, default=None)       # 多图完整列表（冗余回显）
    comparison_regions = Column(JSON, default=None) # 题目对比模式分区 {regions:[...]}
    reference_svg_path = Column(String, default="")  # OCR 视觉模型忠实复刻的原题参考 SVG
    reference_svg_status = Column(String, default="pending")  # pending/ready/not_required/failed
    reference_svg_error = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Paper(Base):
    __tablename__ = "papers"

    id = Column(String, primary_key=True, default=gen_id)
    title = Column(String, nullable=False)
    subject = Column(String, default="")
    grade = Column(String, default="")
    paper_type = Column(String, default="custom")
    prompt_template_id = Column(String, default="")
    custom_prompt = Column(Text, default="")
    question_ids = Column(JSON, default=list)
    question_order = Column(JSON, default=list)
    paper_html = Column(Text, default="")
    answer_html = Column(Text, default="")
    answer_sheet_html = Column(Text, default="")
    paper_pdf_path = Column(String, default="")
    paper_word_path = Column(String, default="")
    answer_pdf_path = Column(String, default="")
    answer_word_path = Column(String, default="")
    generation_params = Column(JSON, default=dict)
    user_score = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class Correction(Base):
    __tablename__ = "corrections"

    id = Column(String, primary_key=True, default=gen_id)
    question_id = Column(String, ForeignKey("questions.id"), nullable=True)
    paper_id = Column(String, ForeignKey("papers.id"), nullable=True)
    student_answer = Column(Text, default="")
    matched_question_id = Column(String, default="")
    score = Column(Float, nullable=True)
    max_score = Column(Float, nullable=True)
    points = Column(JSON, default=list)
    feedback = Column(Text, default="")
    error_analysis = Column(Text, default="")
    suggestions = Column(Text, default="")
    raw_image_path = Column(String, default="")
    source_type = Column(String, default="single")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    type = Column(String, default="custom")
    is_default = Column(Boolean, default=False)
    content = Column(Text, default="")
    blocks = Column(JSON, default=list)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class SavedConfig(Base):
    """多态持久化配置：paper（组卷参数）、home_widgets（首页组件布局）等。"""
    __tablename__ = "saved_configs"

    id = Column(String, primary_key=True, default=gen_id)
    config_type = Column(String, nullable=False, index=True)
    name = Column(String, default="")
    payload = Column(JSON, default=dict)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class ProcessingTask(Base):
    __tablename__ = "processing_tasks"

    id = Column(String, primary_key=True, default=gen_id)
    question_id = Column(String, ForeignKey("questions.id"), nullable=True)
    task_type = Column(String, default="ocr")
    status = Column(String, default="pending")
    progress = Column(Float, default=0.0)
    result = Column(JSON, default=dict)
    error_message = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class AgentTask(Base):
    """后台推理任务（双端架构：前台交互端 / 后台推理端，见 Agent双端架构设计.md §7.1）。

    与 ProcessingTask 的区别：ProcessingTask 服务于"题目处理"这一条固定管线；
    AgentTask 是通用的后台工具执行单元，tool 字段对应 agent_core.TOOLS 里的工具名。
    状态机：pending -> running -> (done | cancelled | error)，cancelled 保留 partial 可续跑。
    """
    __tablename__ = "agent_tasks"

    id = Column(String, primary_key=True, default=gen_id)
    sid = Column(String, default="", index=True)      # 所属会话
    tool = Column(String, default="")                  # 工具名，对应 agent_core.TOOLS
    title = Column(String, default="")                 # 展示名，如「提前备课：宋代经济」
    params = Column(JSON, default=dict)                # 入参快照
    status = Column(String, default="pending", index=True)  # pending/running/done/cancelled/error
    progress = Column(Float, default=0.0)              # 0-100
    steps = Column(JSON, default=list)                 # 该任务的步骤（parent/child/status/time）
    output = Column(JSON, default=dict)                # 产出引用（material_id / 文件路径等）
    partial = Column(JSON, default=dict)               # 取消/失败时的中间态，供续跑
    cancel_requested = Column(Boolean, default=False)  # 协作式取消标志位
    error = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Lesson(Base):
    """备课产物 = 可编辑课稿（`Agent双端架构设计.md` §8.3「课包 Lesson Pack」）。

    用户 2026-09 的要求原话：「产出可编辑课稿」「任何需要讲解相关内容的 AI 都可以
    查看、切片、读取」「全自动生成，人决定怎么走」。

    因此本表的设计要点：
    - **可编辑**：`sections` 是**有序切片列表**，每片能被单独改写（`lesson_service.update_section`），
      不必整篇重生成；
    - **可切片读取**：对外契约是 `lesson_service.slice_lesson(...)`，返回带
      `total_sections` / `truncated` 的**有界**片段 —— 大课稿不会一次性塞满调用方的上下文，
      也不会悄悄截断不告诉调用方（项目「容量诚实」原则）；
    - **人决定怎么走**：`status` 只有 draft/final 两态，`origin` 区分自动/人工，
      自动生成的东西永远先进 draft，等人确认。
    """
    __tablename__ = "lessons"

    id = Column(String, primary_key=True, default=gen_id)
    title = Column(String, default="")
    subject = Column(String, default="")
    grade = Column(String, default="")
    topics = Column(JSON, default=list)                # 知识点标签
    source_question_ids = Column(JSON, default=list)    # 选材：题目
    source_paper_id = Column(String, default="")        # 选材：试卷
    sections = Column(JSON, default=list)               # 有序切片（课稿本体）
    status = Column(String, default="draft")            # draft / final
    origin = Column(String, default="auto")             # auto / manual
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id = Column(String, primary_key=True, default=gen_id)
    title = Column(String, default="")
    subject = Column(String, default="")
    grade = Column(String, default="")
    question_ids = Column(JSON, default=list)
    paper_id = Column(String, default="")
    status = Column(String, default="open")
    notes = Column(Text, default="")
    upload_mode = Column(String, default="one_per_image")
    created_at = Column(DateTime, default=_utcnow)


class Note(Base):
    """AI整理的笔记/考点"""
    __tablename__ = "notes"

    id = Column(String, primary_key=True, default=gen_id)
    subject = Column(String, default="")
    grade = Column(String, default="")
    knowledge_tags = Column(JSON, default=list)
    title = Column(String, default="")
    content = Column(Text, default="")          # 笔记/考点说明（Markdown）
    question_ids = Column(JSON, default=list)     # 关联的题目ID列表
    typical_questions = Column(JSON, default=list) # 典型例题 [{stem, answer, analysis}]
    source_images = Column(JSON, default=list)    # 来源图片 [base64或路径]
    diagram_spec = Column(JSON, default=None)   # 模型示意图 spec（可选，跳编辑器）
    references = Column(JSON, default=None)     # 引用 [{type:"question"/"bank", id/name}]
    is_structured = Column(Boolean, default=False)  # AI是否已结构化整理
    source_type = Column(String, default="manual")   # image/manual/ai_generated
    sort_order = Column(Integer, default=0)     # 排序权重（频次越高越前）
    auto_generated = Column(Boolean, default=False)  # AI自动生成
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
