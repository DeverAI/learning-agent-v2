"""自招素材每日一条 — 数据表。

P1 极简版：单表 + 幂等生成 + 音频路径归档。
表继承 Base（models.database），由 init_db 自动建表。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, String, Text, DateTime, JSON, Integer
from models.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def gen_id() -> str:
    return uuid.uuid4().hex[:12]


class DailyFeed(Base):
    """自招素材每日一条（feed_date 唯一，幂等）。"""
    __tablename__ = "daily_feeds"

    id = Column(String, primary_key=True, default=gen_id)
    feed_date = Column(String, nullable=False, unique=True, index=True)  # YYYY-MM-DD
    subject = Column(String, default="")
    grade = Column(String, default="")
    title = Column(String, default="")          # 一句话主题（≤20字）
    summary = Column(Text, default="")          # 50-100字摘要
    body = Column(Text, default="")             # 完整文本（含题目/答案/知识点，300-500字）
    knowledge_tags = Column(JSON, default=list)  # 知识点标签
    audio_path = Column(String, default="")      # 本地音频文件路径
    audio_duration_sec = Column(Integer, default=0)
    audio_mime = Column(String, default="audio/mpeg")
    model_used = Column(String, default="")
    prompt_version = Column(String, default="v1")
    status = Column(String, default="ready")     # ready / failed
    error = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
