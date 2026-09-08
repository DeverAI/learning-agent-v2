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
