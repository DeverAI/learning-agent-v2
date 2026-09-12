"""自招素材每日一条 — HTTP API。

P1 端点：
- GET  /api/feed/today?force=0|1    今日素材（首次自动生成）
- GET  /api/feed/{date}             历史素材（YYYY-MM-DD，自动生成）
- GET  /api/feed/audio/{date}       音频流（mp3）
- POST /api/feed/regenerate         强制重生成某日（默认今天）
"""
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from logger import get_logger
from models.database import async_session
from models.feed_models import DailyFeed
from services.feed_service import generate_daily, today_str

router = APIRouter(prefix="/api/feed", tags=["feed"])
logger = get_logger()


class FeedOut(BaseModel):
    date: str
    subject: str
    title: str
    summary: str
    body: str
    knowledge_tags: list[str]
    audio_url: str
    audio_duration_sec: int
    model_used: str
    status: str


def _to_out(feed: DailyFeed) -> FeedOut:
    audio_url = f"/api/feed/audio/{feed.feed_date}" if feed.audio_path else ""
    return FeedOut(
        date=feed.feed_date,
        subject=feed.subject or "",
        title=feed.title or "",
        summary=feed.summary or "",
        body=feed.body or "",
        knowledge_tags=feed.knowledge_tags or [],
        audio_url=audio_url,
        audio_duration_sec=feed.audio_duration_sec or 0,
        model_used=feed.model_used or "",
        status=feed.status or "ready",
    )


@router.get("/today", response_model=FeedOut)
async def api_feed_today(force: int = Query(0, ge=0, le=1)):
    """今日素材（自动生成或读缓存）。force=1 强制重生成。"""
    date = today_str()
    feed = await generate_daily(date, force=bool(force))
    if feed.status == "failed":
        raise HTTPException(
            status_code=500,
            detail=f"生成失败: {feed.error or 'unknown'}",
        )
    return _to_out(feed)


@router.get("/{date}", response_model=FeedOut)
async def api_feed_by_date(date: str):
    """历史素材（YYYY-MM-DD）。不存在则自动生成。"""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")
    feed = await generate_daily(date)
    if feed.status == "failed":
        raise HTTPException(
            status_code=500,
            detail=f"生成失败: {feed.error or 'unknown'}",
        )
    return _to_out(feed)


@router.get("/audio/{date}")
async def api_feed_audio(date: str):
    """返回某日音频 mp3 文件流。"""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")
    async with async_session() as session:
        result = await session.execute(
            select(DailyFeed).where(DailyFeed.feed_date == date)
        )
        feed = result.scalar_one_or_none()
    if feed is None or not feed.audio_path:
        raise HTTPException(status_code=404, detail="素材或音频不存在")
    p = Path(feed.audio_path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="音频文件丢失")
    return FileResponse(
        p,
        media_type=feed.audio_mime or "audio/mpeg",
        filename=f"feed_{date}.mp3",
    )


class RegenerateIn(BaseModel):
    date: str | None = None  # None = 今日


@router.post("/regenerate", response_model=FeedOut)
async def api_feed_regenerate(body: RegenerateIn):
    """强制重生成某日（默认今天）。"""
    date = body.date or today_str()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")
    feed = await generate_daily(date, force=True)
    if feed.status == "failed":
        raise HTTPException(
            status_code=500,
            detail=f"生成失败: {feed.error or 'unknown'}",
        )
    return _to_out(feed)
