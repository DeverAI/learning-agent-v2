import asyncio
import os
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from config import STORAGE_DIR, load_settings, _atomic_write_json
from logger import get_logger, log_error

logger = get_logger()
router = APIRouter()


@router.get("/api/system-messages")
async def get_system_messages(limit: int = 20):
    from services.audit_service import get_system_messages
    return {"messages": get_system_messages(max(1, min(limit, 100)))}


@router.post("/api/system-messages/read")
async def mark_messages_read():
    from services.audit_service import mark_messages_read
    mark_messages_read()
    return {"message": "已标记已读"}


@router.delete("/api/system-messages/{index:path}")
async def delete_system_message(index: str):
    """手动删除指定系统消息；index为'all'时清空全部"""
    from services.audit_service import delete_system_message_at
    if index == "all":
        delete_system_message_at(clear_all=True)
        return {"message": "已清空所有系统消息"}
    try:
        idx = int(index)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="消息索引必须是整数或 all")
    if delete_system_message_at(idx):
        return {"message": "已删除"}
    raise HTTPException(status_code=404, detail="消息索引无效")


# --- Manual trigger for audit/rewrite ---
_audit_run_lock = asyncio.Lock()


@router.post("/api/audit/trigger")
async def trigger_audit():
    """手动触发全库巡检"""
    from services.audit_service import run_full_audit
    if _audit_run_lock.locked():
        raise HTTPException(status_code=409, detail="巡检正在运行中，请稍后再试")
    async with _audit_run_lock:
        try:
            count = await run_full_audit()
            return {"message": f"巡检完成，发现 {count} 项问题"}
        except Exception as e:
            log_error("manual_audit", str(e))
            raise HTTPException(status_code=500, detail="巡检执行失败")


@router.post("/api/audit/rewrite")
async def trigger_rewrite():
    """手动触发自动重写"""
    from services.audit_service import run_auto_rewrite
    if _audit_run_lock.locked():
        raise HTTPException(status_code=409, detail="巡检或重写正在运行中，请稍后再试")
    async with _audit_run_lock:
        try:
            await run_auto_rewrite()
            return {"message": "重写任务已执行"}
        except Exception as e:
            log_error("manual_rewrite", str(e))
            raise HTTPException(status_code=500, detail="重写执行失败")


_quote_cache = {"quote": "Hello World —— 学习搭子，今日启航。", "has_api": False}
_quote_lock = asyncio.Lock()

QUOTE_DIR = os.path.join(STORAGE_DIR, "quotes")
os.makedirs(QUOTE_DIR, exist_ok=True)


def _get_quote_filepath() -> str:
    today_str = date.today().strftime("%Y-%m-%d")
    return os.path.join(QUOTE_DIR, f"quote_{today_str}.json")


def _load_quote_from_file() -> dict | None:
    path = _get_quote_filepath()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 合法 JSON 但非对象（[] / "x" / 数字）时按损坏处理，
            # 避免 .get 抛 AttributeError 造成 /api/daily-quote 500（FreqErr 同型）
            if isinstance(data, dict) and data.get("quote"):
                return data
    except (json.JSONDecodeError, IOError, OSError) as exc:
        logger.warning("Failed to load daily quote cache: %s", exc)
    return None


def _save_quote_to_file(data: dict):
    path = _get_quote_filepath()
    try:
        data["date"] = date.today().strftime("%Y-%m-%d")
        _atomic_write_json(path, data)
    except (IOError, OSError) as e:
        logger.warning("Failed to save daily quote: %s", e)


async def _gen_quote() -> dict:
    """Generate a daily quote with web search, using system time."""
    s = load_settings()
    zp_key = s.get("zhipuai_api_key", "")
    if not zp_key:
        logger.warning("GLM key not configured for daily quote")
        return {"quote": "Hello World —— 学习搭子，今日启航。", "source": "", "font": "", "has_api": False}
    zp_base = s.get("zhipuai_base_url", "https://open.bigmodel.cn/api/paas/v4").rstrip('/')
    if not zp_base.endswith('/chat/completions'):
        zp_base += '/chat/completions'
    # 每日一言按北京时间取日期/星期（服务器 OS 时区不可靠，固定 UTC+8）
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    today = now.strftime("%Y-%m-%d")
    weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
    month_day = f"{now.month}月{now.day}日"
    topic = s.get("quote_topic", "").strip()
    topic_hint = ""
    if topic:
        topic_hint = f"主题偏好：{topic}。优先搜索该领域历史上的今天重大事件或人物。"
    prompt = (
        f"当前系统时间：{today} {now.strftime('%H:%M')}，{weekday}，{month_day}。\n"
        f"请联网搜索{today}这一天（历史上的今天）发生的重要事件、诞生或逝世的名人。"
        f"{topic_hint}\n"
        f"如果这天是某位名人（如孔子、爱因斯坦、苏轼、鲁迅等）的诞辰或逝世日，请选取该人物本人的一句与教育、学习、成长相关的名言。\n"
        f"如果不是名人纪念日，则选取历史上今天发生的、与学习和成长有关的重大事件，用一句话概括其精神（50字以内）。\n\n"
        f"请输出一个JSON对象（不要markdown代码块），有三个字段：\n"
        f"\"quote\": 名言/精神概括（纯文本，50字以内），\n"
        f"\"source\": 出处。人名只写名字（如'鲁迅'、'爱因斯坦'，不要加'诞辰''逝世'等后缀）；事件只写事件名（如'五四运动'）。15字以内。\n"
        f"\"font\": CSS字体名。古文/诗词→KaiTi；现代文→留空；外国文→FangSong。\n"
        f"只输出JSON，不要任何额外文字。"
    )
    payload = {
        "model": "glm-4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
        "max_tokens": 300,
        "tools": [{"type": "web_search", "web_search": {"enable": True}}]
    }
    try:
        import httpx as _h
        async with _h.AsyncClient(timeout=_h.Timeout(60)) as c:
            resp = await c.post(
                zp_base,
                json=payload,
                headers={"Authorization": f"Bearer {zp_key}", "Content-Type": "application/json"}
            )
            if resp.status_code == 200:
                body = resp.json()
                choices = body.get("choices") or []
                if not choices:
                    logger.warning("Daily quote: empty choices array")
                    return {"quote": "Hello World —— 学习搭子，今日启航。", "source": "", "font": "", "has_api": False}
                msg_obj = choices[0].get("message") or {}
                raw = (msg_obj.get("content") or "").strip()
                # Strip markdown code fences robustly
                if raw.startswith('```'):
                    raw = raw.split('\n', 1)[-1] if '\n' in raw else raw[3:]
                if raw.endswith('```'):
                    raw = raw.rsplit('\n', 1)[0] if '\n' in raw else raw[:-3]
                raw = raw.strip()
                try:
                    data = json.loads(raw)
                    q = str(data.get("quote", "")).strip('"').strip("'")[:120]
                    src = str(data.get("source", ""))[:20]
                    font = str(data.get("font", ""))[:30]
                    if q:
                        _save_quote_to_file({"quote": q, "source": src, "font": font})
                        return {"quote": q, "source": src, "font": font, "has_api": True}
                except json.JSONDecodeError:
                    q = raw.strip('"').strip("'")[:120]
                    if q:
                        _save_quote_to_file({"quote": q, "source": "", "font": ""})
                        return {"quote": q, "source": "", "font": "", "has_api": True}
    except Exception as e:
        logger.warning("GLM daily quote failed: %s", e)
    return {"quote": "Hello World —— 学习搭子，今日启航。", "source": "", "font": "", "has_api": False}


@router.get("/api/daily-quote")
async def daily_quote():
    global _quote_cache
    async with _quote_lock:
        cached = _load_quote_from_file()
        if cached and cached.get("quote"):
            _quote_cache = {
                "quote": cached["quote"], "source": cached.get("source", ""),
                "font": cached.get("font", ""), "has_api": True,
            }
            return _quote_cache

        s = load_settings()
        if s.get("zhipuai_api_key"):
            data = await _gen_quote()
            _quote_cache = {
                "quote": data["quote"], "source": data.get("source", ""),
                "font": data.get("font", ""), "has_api": bool(data.get("has_api")),
            }
        else:
            _quote_cache = {"quote": "Hello World —— 学习搭子，今日启航。", "source": "", "font": "", "has_api": False}

        # 只在真正生成到新名言时广播系统消息；
        # 无 Key 或生成失败回退占位时不广播，避免每次请求重复堆积同一条占位消息。
        if _quote_cache.get("has_api") and _quote_cache.get("quote") != "Hello World —— 学习搭子，今日启航。":
            try:
                from services.audit_service import add_system_message
                add_system_message("daily_quote", "每日一言", _quote_cache["quote"])
            except Exception as exc:
                logger.warning("Failed to broadcast daily quote: %s", exc)

    return _quote_cache
