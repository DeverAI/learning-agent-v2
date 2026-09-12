"""自招素材每日一条 — 业务逻辑。

P1 范围：
- generate_daily(date)  幂等生成（已存在且 status=ready 则直接返回）
- _call_llm_json(...)   调 xiaomi_chat 拿 JSON 文本
- _tts_to_file(...)     调 xiaomi_tts 合成音频写本地
- 失败 status 落库，不抛异常（API 层 500）

P2 范围（待补）：归档去重（content_hash + 近 N 天内查重）。
P3 范围（待补）：提前 N 天预生成。
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from config import STORAGE_DIR
from logger import get_logger, log_error
from models.database import async_session
from models.feed_models import DailyFeed

logger = get_logger()

FEED_AUDIO_DIR = Path(STORAGE_DIR) / "feed_audio"
FEED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)

DAILY_FEED_PROMPT = """你是一位严谨的初高中自招辅导老师。今天是 {date}。
请按下面的要求生成一条**简短的自招素材**（课内 +1 档难度，介于课本与纯竞赛之间）。

要求：
1. 主题：自招/中考考点延伸或学科经典模型
2. 总长度：300-500 字（中文）
3. 结构：
   - title：≤20 字的一句话主题
   - summary：50-100 字背景引入（为什么要学这个）
   - body：完整讲解，含一个具体题目（题目本身 80-150 字）+ 答案（50-100 字，过程清晰）
   - knowledge_tags：1-3 个关键知识点（中文短词）

**严格只输出 JSON**，不要任何额外说明、不要 Markdown 包裹。
格式：
{{"title": "...", "summary": "...", "body": "...", "knowledge_tags": ["...", "..."]}}
"""


def today_str() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")


def _strip_json_fence(raw: str) -> str:
    """模型偶发在 JSON 外包 ```json ... ```，剥掉外层。"""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.search(r"\{[\s\S]*\}", raw)
    return m.group(0) if m else raw


async def _call_llm_json(prompt: str) -> dict:
    """调 xiaomi/MiMo chat 拿 JSON 文本并解析；失败抛 ValueError。"""
    from services.ai_service import ai_service  # 延迟导入避免循环

    messages = [
        {"role": "system", "content": "你是初高中自招辅导老师，只输出严格 JSON。"},
        {"role": "user", "content": prompt},
    ]
    text = await ai_service.xiaomi_chat(messages, temperature=0.4, max_tokens=1024)
    if not text:
        raise ValueError("LLM 返回为空")
    cleaned = _strip_json_fence(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 输出不是合法 JSON: {exc}; raw_head={text[:120]!r}") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM JSON 不是对象")
    for key in ("title", "summary", "body"):
        if not data.get(key):
            raise ValueError(f"LLM JSON 缺字段 {key}")
    return data


async def _tts_to_file(text: str, date: str) -> tuple[Path, int, str]:
    """调 xiaomi_tts 合成音频写到本地，返回 (path, duration_sec, mime)。"""
    from services.ai_service import ai_service  # 延迟导入

    if not text or not text.strip():
        raise ValueError("TTS 文本为空")
    audio_bytes, mime = await ai_service.xiaomi_tts(text, "")
    out_path = FEED_AUDIO_DIR / f"{date}.mp3"
    out_path.write_bytes(audio_bytes)
    # 粗估时长：中文 6-7 字/秒，留余量按 5.5 字/秒
    duration = max(1, int(len(text) / 5.5))
    return out_path, duration, mime or "audio/mpeg"


async def generate_daily(date: str, subject: str = "math", force: bool = False) -> DailyFeed:
    """幂等生成某日素材。date 形如 'YYYY-MM-DD'。

    - 库中已有且 status=ready 且不强制：直接返回
    - 库中已有但 status=failed 或 force=True：重新生成（覆盖字段）
    - 库中没有：新建
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise ValueError(f"日期格式应为 YYYY-MM-DD，收到 {date!r}")

    async with async_session() as session:
        result = await session.execute(
            select(DailyFeed).where(DailyFeed.feed_date == date)
        )
        feed = result.scalar_one_or_none()

        if feed and feed.status == "ready" and not force:
            return feed

        # 1. 生成文本
        try:
            content = await _call_llm_json(
                DAILY_FEED_PROMPT.format(date=date)
            )
        except Exception as exc:
            log_error("feed_llm", f"{date} LLM failed: {exc}")
            if feed is None:
                feed = DailyFeed(
                    feed_date=date,
                    subject=subject,
                    status="failed",
                    error=f"LLM: {str(exc)[:300]}",
                    model_used="xiaomi-mimo",
                )
                session.add(feed)
            else:
                feed.status = "failed"
                feed.error = f"LLM: {str(exc)[:300]}"
            await session.commit()
            await session.refresh(feed)
            return feed

        # 2. 写库（不提交，等 TTS 一起提交）
        if feed is None:
            feed = DailyFeed(
                feed_date=date,
                subject=subject,
                title=content.get("title", "")[:200],
                summary=content.get("summary", "")[:2000],
                body=content.get("body", "")[:8000],
                knowledge_tags=content.get("knowledge_tags", []) or [],
                model_used="xiaomi-mimo",
                prompt_version="v1",
                status="ready",
            )
            session.add(feed)
            await session.flush()  # 拿 id
        else:
            feed.subject = subject
            feed.title = content.get("title", "")[:200]
            feed.summary = content.get("summary", "")[:2000]
            feed.body = content.get("body", "")[:8000]
            feed.knowledge_tags = content.get("knowledge_tags", []) or []
            feed.model_used = "xiaomi-mimo"
            feed.prompt_version = "v1"
            feed.status = "ready"
            feed.error = ""

        # 3. TTS（失败不阻塞素材，标 error 但 status=ready）
        try:
            audio_path, duration, mime = await _tts_to_file(
                content.get("body", ""), date
            )
            feed.audio_path = str(audio_path)
            feed.audio_duration_sec = duration
            feed.audio_mime = mime
        except Exception as exc:
            log_error("feed_tts", f"{date} TTS failed: {exc}")
            feed.error = f"TTS: {str(exc)[:200]}"

        await session.commit()
        await session.refresh(feed)
        return feed
