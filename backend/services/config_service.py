from datetime import datetime, timezone

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)
from sqlalchemy import select, desc
from models.database import async_session
from models.models import SavedConfig
from config import ENABLE_SEARCH, ENABLE_CORRECT, ENABLE_FOCUS_MODE
from logger import get_logger

logger = get_logger()

# 首页 widget 注册表
HOME_WIDGET_REGISTRY = {
    "upload_question": {"title": "上传题目", "url": "/batch-upload", "default_enabled": True, "requires": []},
    "smart_upload": {"title": "智能上传", "url": "/batch-upload#smart", "default_enabled": True, "requires": []},
    "search": {"title": "拍照搜题", "url": "/search", "default_enabled": False, "requires": ["ENABLE_SEARCH"]},
    "correct_single": {"title": "单题批改", "url": "/correct", "default_enabled": False, "requires": ["ENABLE_CORRECT"]},
    "correct_paper": {"title": "试卷批改", "url": "/papers", "default_enabled": False, "requires": ["ENABLE_CORRECT"]},
    "correct_center": {"title": "批改中心", "url": "/correctCenter", "default_enabled": False, "requires": ["ENABLE_CORRECT"]},
    "editor": {"title": "实验图编辑", "url": "/editor", "default_enabled": False, "requires": []},
    "notes": {"title": "笔记整理", "url": "/notes", "default_enabled": True, "requires": []},
    "knowledge": {"title": "题库管理", "url": "/questions", "default_enabled": True, "requires": []},
    "papers": {"title": "试卷列表", "url": "/papers", "default_enabled": True, "requires": []},
    "paper_generate": {"title": "组卷中心", "url": "/papers/generate", "default_enabled": True, "requires": []},
    "lessons": {"title": "课稿备课", "url": "/lessons", "default_enabled": True, "requires": []},
    "focus_mode": {"title": "专注模式", "url": "/focus", "default_enabled": True, "requires": ["ENABLE_FOCUS_MODE"]},
    "dashboard": {"title": "首页", "url": "/", "default_enabled": False, "requires": []},
}

# 组卷参数白名单（只允许这些键进入 payload，防止污染）
PAPER_CONFIG_KEYS = {
    "paper_type", "subject", "grade", "region", "knowledge_tags", "keyword",
    "custom_prompt", "prompt_template_id", "title", "ai_auto_count", "question_count",
    "avg_score_min", "avg_score_max", "paper_size", "answer_space",
    "extra_params", "example_count", "practice_count", "note_count",
    "worksheet_topic",
}


def _paper_payload_whitelist(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    cleaned = {}
    for k, v in payload.items():
        if k in PAPER_CONFIG_KEYS:
            cleaned[k] = v
    # 兼容旧配置中的 type / paper_type 字段
    if "paper_type" not in cleaned and "type" in payload:
        t = payload["type"]
        if isinstance(t, str):
            cleaned["paper_type"] = t
    return cleaned


def _widget_enabled(widget_id: str) -> bool:
    info = HOME_WIDGET_REGISTRY.get(widget_id)
    if not info:
        return False
    for req in info.get("requires", []):
        if req == "ENABLE_SEARCH" and not ENABLE_SEARCH:
            return False
        if req == "ENABLE_CORRECT" and not ENABLE_CORRECT:
            return False
        if req == "ENABLE_FOCUS_MODE" and not ENABLE_FOCUS_MODE:
            return False
    return True


def get_available_widgets() -> list[dict]:
    """返回当前开关下可用的首页 widget 列表。"""
    return [
        {"id": wid, "title": info["title"], "url": info["url"], "default_enabled": info["default_enabled"]}
        for wid, info in HOME_WIDGET_REGISTRY.items()
        if _widget_enabled(wid)
    ]


def get_default_home_layout() -> list[str]:
    """默认首页 widget 布局。"""
    return [
        wid for wid, info in HOME_WIDGET_REGISTRY.items()
        if info["default_enabled"] and _widget_enabled(wid)
    ]


def normalize_home_layout(layout) -> list[str]:
    """过滤未知/禁用/重复项并保持用户顺序；显式空数组仍保持为空。"""
    if not isinstance(layout, list):
        return []
    valid = []
    seen = set()
    for raw in layout:
        wid = str(raw or "").strip()
        if not wid or wid in seen:
            continue
        if wid in HOME_WIDGET_REGISTRY and _widget_enabled(wid):
            seen.add(wid)
            valid.append(wid)
    return valid


class ConfigService:

    # ============== paper configs ==============

    async def save_paper_config(self, payload: dict, name: str = "") -> str:
        """保存组卷参数，返回短配置 ID。"""
        cleaned = _paper_payload_whitelist(payload)
        name = name or self._paper_config_name(cleaned)
        async with async_session() as db:
            cfg = SavedConfig(config_type="paper", name=name, payload=cleaned)
            db.add(cfg)
            await db.commit()
            await db.refresh(cfg)
            return cfg.id

    def _paper_config_name(self, payload: dict) -> str:
        title = payload.get("title", "")
        if title:
            return str(title)[:60]
        parts = [payload.get("grade", ""), payload.get("subject", ""), payload.get("paper_type", "custom")]
        return "".join(parts) or "组卷参数"

    async def load_paper_config(self, config_id: str) -> dict | None:
        async with async_session() as db:
            cfg = await db.get(SavedConfig, config_id)
            if cfg and cfg.config_type == "paper":
                return dict(cfg.payload or {})
        return None

    async def get_paper_config(self, config_id: str) -> SavedConfig | None:
        async with async_session() as db:
            cfg = await db.get(SavedConfig, config_id)
            if cfg and cfg.config_type == "paper":
                return cfg
        return None

    async def list_paper_configs(self, limit: int = 20, offset: int = 0) -> list[dict]:
        async with async_session() as db:
            result = await db.execute(
                select(SavedConfig)
                .where(SavedConfig.config_type == "paper")
                .order_by(desc(SavedConfig.created_at))
                .limit(limit)
                .offset(offset)
            )
            configs = result.scalars().all()
        out = []
        for c in configs:
            payload = c.payload or {}
            title = c.name or payload.get("title") or f"{payload.get('grade', '')}{payload.get('subject', '')}试卷"
            out.append({
                "id": c.id,
                "name": c.name,
                "title": title,
                "data": payload,
                "time": c.created_at.isoformat() if c.created_at else "",
            })
        return out

    async def delete_paper_config(self, config_id: str) -> bool:
        async with async_session() as db:
            cfg = await db.get(SavedConfig, config_id)
            if cfg and cfg.config_type == "paper":
                await db.delete(cfg)
                await db.commit()
                return True
        return False

    # ============== home widgets ==============

    async def load_home_layout(self) -> list[str]:
        async with async_session() as db:
            result = await db.execute(
                select(SavedConfig)
                .where(SavedConfig.config_type == "home_widgets")
                .order_by(desc(SavedConfig.updated_at))
                .limit(1)
            )
            cfg = result.scalars().first()
        if not cfg:
            return get_default_home_layout()
        payload = cfg.payload or {}
        if not isinstance(payload, dict) or "layout" not in payload or not isinstance(payload.get("layout"), list):
            return get_default_home_layout()
        return normalize_home_layout(payload["layout"])

    async def save_home_layout(self, layout: list[str]) -> str:
        valid = normalize_home_layout(layout)
        async with async_session() as db:
            result = await db.execute(
                select(SavedConfig)
                .where(SavedConfig.config_type == "home_widgets")
                .order_by(desc(SavedConfig.updated_at))
                .limit(1)
            )
            cfg = result.scalars().first()
            if cfg:
                cfg.payload = {"layout": valid}
                cfg.updated_at = _utcnow()
            else:
                cfg = SavedConfig(config_type="home_widgets", name="home_layout", payload={"layout": valid})
                db.add(cfg)
            await db.commit()
            await db.refresh(cfg)
            return cfg.id


config_service = ConfigService()
