import re
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator


def escape_math_html(raw: str) -> str:
    """将 $...$ / $$...$$ 数学公式块内的 < > 转义为 HTML 实体，
    防止浏览器把公式中的不等号解析成 HTML 标签。
    跳过 <script> / <style> 标签内容，避免破坏页面脚本/样式。"""
    if not raw or not isinstance(raw, str):
        return raw

    # 临时替换 <script>/<style> 块，避免公式转义破坏其中的 JS/CSS
    protected: list[str] = []

    def _protect(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x00PROTECTED_{len(protected) - 1}\x00"

    raw = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", _protect, raw, flags=re.IGNORECASE)
    raw = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", _protect, raw, flags=re.IGNORECASE)

    def _escape_block(match: re.Match) -> str:
        delim = match.group(1)
        inner = match.group(2)
        # 幂等处理：先把已有实体还原，再统一转义
        inner = inner.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        inner = inner.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"{delim}{inner}{delim}"

    # 先处理块级公式，避免 $$ 被拆成两个内联 $ 处理
    raw = re.sub(r"(\$\$)(.*?)\$\$", _escape_block, raw, flags=re.DOTALL)
    # 再处理内联公式：单独成对的 $，避免 $$ 和 \$
    raw = re.sub(r"(?<![$\\])(\$)(?!\$)(.*?)(?<![$\\])\$(?!\$)", _escape_block, raw, flags=re.DOTALL)

    # 还原受保护块
    for i, block in enumerate(protected):
        raw = raw.replace(f"\x00PROTECTED_{i}\x00", block, 1)
    return raw


class QuestionCreate(BaseModel):
    subject: str = Field(default="", max_length=64)
    grade: str = Field(default="", max_length=64)
    knowledge_tags: list[str] = Field(default_factory=list, max_length=50)
    region: str = Field(default="", max_length=64)


class QuestionUpdate(BaseModel):
    subject: Optional[str] = Field(default=None, max_length=64)
    grade: Optional[str] = Field(default=None, max_length=64)
    knowledge_tags: Optional[list[str]] = Field(default=None, max_length=50)
    region: Optional[str] = Field(default=None, max_length=64)
    difficulty: Optional[str] = Field(default=None, max_length=16)
    avg_score: Optional[float] = Field(default=None, ge=0, le=150)
    question_html: Optional[str] = Field(default=None, max_length=200_000)
    answer_html: Optional[str] = Field(default=None, max_length=300_000)
    standard_answer: Optional[str] = Field(default=None, max_length=50_000)
    score_points_html: Optional[str] = Field(default=None, max_length=100_000)
    question_type: Optional[str] = Field(default=None, max_length=32)
    handwriting_notes: Optional[str] = Field(default=None, max_length=50_000)
    structure_graph: Optional[dict] = None
    comparison_regions: Optional[dict] = None

    model_config = ConfigDict(extra="forbid")


class NoteCreate(BaseModel):
    subject: str = Field(default="", max_length=64)
    grade: str = Field(default="", max_length=64)
    knowledge_tags: list[str] = Field(default_factory=list, max_length=50)
    title: str = Field(default="", max_length=200)
    content: str = Field(default="", max_length=500_000)
    question_ids: list[str] = Field(default_factory=list, max_length=200)
    references: list[dict] = Field(default_factory=list, max_length=200)
    sort_order: int = 0


class NoteResponse(BaseModel):
    id: str
    subject: str
    grade: str
    knowledge_tags: list
    title: str
    content: str
    question_ids: list
    references: list
    sort_order: int
    auto_generated: bool
    created_at: str
    updated_at: str

    @field_validator("title", "content", mode="before")
    @classmethod
    def _escape_math_in_note_strings(cls, v):
        if isinstance(v, str):
            return escape_math_html(v)
        return v


class QuestionListItem(BaseModel):
    """题目列表项，排除大字段 structure_graph / structure_graph_info"""
    id: str
    folder_path: str
    subject: str
    grade: str
    knowledge_tags: list
    raw_image_path: str
    ocr_text: str
    question_html: str
    answer_html: str
    standard_answer: str = ""
    question_type: str = ""
    score_points_html: str = ""
    diagrams: list
    diagram_places: list = Field(default_factory=list)
    diagram_description: str = ""
    status: str
    error_message: str
    source_type: str
    bank: str = "default"
    region: str
    difficulty: str = ""
    avg_score: Optional[float]
    audit_flags: list = Field(default_factory=list)
    is_resolved: bool = False
    handwriting_notes: str = ""
    user_hint: str = ""
    image_roles: Optional[list] = None
    multi_images: Optional[list] = None
    comparison_regions: Optional[dict] = None
    reference_svg_path: str = ""
    reference_svg_status: str = "pending"
    reference_svg_error: str = ""
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "folder_path", "subject", "grade", "raw_image_path", "ocr_text",
        "question_html", "answer_html", "standard_answer", "question_type",
        "score_points_html", "diagram_description", "status", "error_message",
        "source_type", "bank", "region", "handwriting_notes", "user_hint",
        "reference_svg_path", "reference_svg_status", "reference_svg_error",
        "difficulty",
        mode="before",
    )
    @classmethod
    def _escape_math_in_strings(cls, v):
        if isinstance(v, str):
            return escape_math_html(v)
        return v

    model_config = ConfigDict(from_attributes=True)


class QuestionResponse(QuestionListItem):
    structure_graph: Optional[dict] = None
    structure_graph_info: Optional[dict] = None


class QuestionSearchParams(BaseModel):
    subject: Optional[str] = None
    grade: Optional[str] = None
    knowledge_tags: Optional[list[str]] = None
    keyword: Optional[str] = None
    status: Optional[str] = "done"
    region: Optional[str] = None
    avg_score_min: Optional[float] = None
    avg_score_max: Optional[float] = None
    limit: int = 100
    offset: int = 0


class PaperCreate(BaseModel):
    title: str
    subject: str = ""
    grade: str = ""
    paper_type: str = "custom"
    question_ids: list[str] = Field(default_factory=list)
    prompt_template_id: str = ""
    custom_prompt: str = ""
    generation_params: dict = Field(default_factory=dict)


class PaperResponse(BaseModel):
    id: str
    title: str
    subject: str
    grade: str
    paper_type: str
    prompt_template_id: str
    custom_prompt: str
    question_ids: list
    question_order: list
    paper_html: str
    answer_html: str
    answer_sheet_html: str = ""
    paper_pdf_path: str
    paper_word_path: str
    answer_pdf_path: str
    answer_word_path: str
    generation_params: dict
    user_score: Optional[float]
    created_at: datetime

    @field_validator(
        "title", "subject", "grade", "paper_type", "prompt_template_id",
        "custom_prompt", "paper_html", "answer_html", "answer_sheet_html",
        "paper_pdf_path", "paper_word_path", "answer_pdf_path", "answer_word_path",
        mode="before",
    )
    @classmethod
    def _escape_math_in_paper_strings(cls, v):
        if isinstance(v, str):
            return escape_math_html(v)
        return v

    model_config = ConfigDict(from_attributes=True)


class PromptTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    type: str = Field(default="custom", max_length=32)
    content: str = Field(default="", max_length=100_000)
    blocks: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("blocks")
    @classmethod
    def _validate_blocks(cls, v):
        for item in v:
            if not isinstance(item, str) or len(item) > 200:
                raise ValueError("blocks 每项必须是 200 字符以内的字符串")
        return v


class PromptTemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    content: Optional[str] = Field(default=None, max_length=100_000)
    blocks: Optional[list[str]] = Field(default=None, max_length=100)

    @field_validator("blocks")
    @classmethod
    def _validate_blocks(cls, v):
        if v is None:
            return v
        for item in v:
            if not isinstance(item, str) or len(item) > 200:
                raise ValueError("blocks 每项必须是 200 字符以内的字符串")
        return v


class PromptTemplateResponse(BaseModel):
    id: str
    name: str
    type: str
    is_default: bool
    content: str
    blocks: list
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProcessingTaskResponse(BaseModel):
    id: str
    question_id: Optional[str]
    task_type: str
    status: str
    progress: float
    result: dict
    error_message: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PaperGenerateRequest(BaseModel):
    paper_type: str = Field(default="custom", max_length=32)
    subject: str = Field(default="", max_length=64)
    grade: str = Field(default="", max_length=64)
    region: str = Field(default="", max_length=64)
    knowledge_tags: list[str] = Field(default_factory=list, max_length=50)
    keyword: str = Field(default="", max_length=500)
    custom_prompt: str = Field(default="", max_length=20_000)
    prompt_template_id: str = Field(default="default_paper", max_length=64)
    title: str = Field(default="", max_length=200)
    question_ids: list[str] = Field(default_factory=list, max_length=50)
    ai_auto_count: bool = False
    question_count: Optional[int] = Field(default=None, ge=1, le=50)
    answer_space: str = Field(default="sheet", max_length=32)
    avg_score_min: Optional[float] = Field(default=0, ge=0, le=150)
    avg_score_max: Optional[float] = Field(default=150, ge=0, le=150)
    extra_params: Optional[dict] = None
    paper_size: str = Field(default="A4", max_length=16)
    paper_layout: str = Field(default="standard", max_length=32)
    example_count: int = Field(default=3, ge=1, le=10)
    practice_count: int = Field(default=5, ge=1, le=20)
    note_count: int = Field(default=5, ge=1, le=20)
    saved_config_id: str = Field(default="", max_length=64)  # 复用已保存的组卷参数
    mode: str = Field(default="new", pattern=r"^(new|modify)$")

    @field_validator("question_count", mode="before")
    @classmethod
    def _normalize_question_count(cls, v, info):
        if info.data.get("ai_auto_count"):
            return None
        if v is None or v == "":
            return 10
        return v

    @field_validator("avg_score_min", mode="before")
    @classmethod
    def _normalize_avg_score_min(cls, v):
        if v is None or v == "":
            return 0
        return v

    @field_validator("avg_score_max", mode="before")
    @classmethod
    def _normalize_avg_score_max(cls, v):
        if v is None or v == "":
            return 150
        return v

    @field_validator("extra_params", mode="before")
    @classmethod
    def _normalize_extra_params(cls, v):
        if v is None or v == "":
            return {}
        return v


class DownloadRequest(BaseModel):
    paper_id: str
    format: str = "pdf"
    include_answer: bool = True


class CorrectionPoint(BaseModel):
    index: int = 0
    title: str = ""
    score: float = 0.0
    max_score: float = 0.0
    comment: str = ""
    hit: bool = False


class CorrectionResponse(BaseModel):
    id: str = ""
    correction_id: str = ""
    question_id: Optional[str] = None
    paper_id: Optional[str] = None
    student_answer: str = ""
    matched_question_id: str = ""
    score: float = 0.0
    max_score: float = 0.0
    points: list[CorrectionPoint] = Field(default_factory=list)
    feedback: str = ""
    error_analysis: str = ""
    suggestions: str = ""
    raw_image_path: str = ""
    source_type: str = "single"
    subject: str = ""
    grade: str = ""
    question_preview: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CorrectionListItem(BaseModel):
    id: str
    question_id: Optional[str] = None
    paper_id: Optional[str] = None
    matched_question_id: str = ""
    score: Optional[float] = None
    max_score: Optional[float] = None
    source_type: str = "single"
    created_at: str = ""


class CorrectionHistoryItem(BaseModel):
    """批改历史列表项：在 CorrectionListItem 基础上增加匹配题目元信息，
    供历史页/批改中心最近记录区块直接展示，避免前端再发详情请求。"""
    id: str
    question_id: Optional[str] = None
    paper_id: Optional[str] = None
    matched_question_id: str = ""
    score: Optional[float] = None
    max_score: Optional[float] = None
    source_type: str = "single"  # single / page / paper
    subject: str = ""
    grade: str = ""
    question_preview: str = ""
    created_at: str = ""
