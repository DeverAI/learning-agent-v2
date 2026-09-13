from fastapi import APIRouter, HTTPException
from config import load_settings, save_settings, _default_settings, CORS_ORIGIN_RE
import json
import math
from pydantic import BaseModel, Field, RootModel, field_validator, model_validator
from typing import Any, Optional, List
from urllib.parse import urlparse
from logger import get_logger

router = APIRouter(prefix="/api/settings", tags=["settings"])
logger = get_logger()

_ALLOWED_CUSTOM_SCOPES = {
    "notes_ocr", "notes_classify", "solve", "paper", "diagram", "chat", "question_challenge"
}
_TIME_RE = __import__("re").compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _valid_http_url(value: str, *, allow_empty: bool = False) -> str:
    value = str(value or "").strip()
    if allow_empty and not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("API 地址必须是有效的 http(s) URL，且不能包含用户名或密码")
    return value.rstrip("/")


class CustomApiConfig(BaseModel):
    key: str = ""
    url: str
    model: str
    scope: str = "chat"
    mode: str = "replace"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _valid_http_url(value)

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128:
            raise ValueError("自定义模型名不能为空且不能超过 128 字符")
        return value

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in ("replace", "fallback"):
            raise ValueError("自定义 API 模式必须是 replace 或 fallback")
        return value

    @model_validator(mode="after")
    def validate_scope(self):
        if self.mode == "replace" and self.scope not in _ALLOWED_CUSTOM_SCOPES:
            raise ValueError("自定义 API 适用范围无效")
        if self.mode == "fallback":
            self.scope = ""
        return self


class SettingsUpdate(BaseModel):
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_max_tokens: int = Field(131072, ge=256, le=131072)
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.moonshot.cn/v1"
    kimi_model: str = "kimi-k2.6"
    kimi_max_tokens: int = Field(4096, ge=256, le=131072)
    zhipuai_api_key: str = ""
    zhipuai_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipuai_model: str = "glm-4v-flash"
    zhipuai_max_tokens: int = Field(4096, ge=256, le=131072)
    xiaomi_token_plan_api_key: str = ""
    xiaomi_token_plan_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    xiaomi_tts_voice: str = "mimo_default"
    focus_voice_engine: str = "auto"
    custom_openai_key: str = ""
    custom_openai_url: str = ""
    custom_openai_model: str = ""
    custom_openai_scopes: List[str] = Field(default_factory=list)
    custom_apis: List[CustomApiConfig] = Field(default_factory=list, max_length=20)
    ai_timeout: int = Field(900, ge=10, le=3600)
    quote_topic: str = ""
    dark_mode_auto: Optional[bool] = None
    dark_mode_start: str = "18:00"
    dark_mode_end: str = "06:00"
    night_patrol_enabled: Optional[bool] = None
    night_patrol_start: str = "01:00"
    night_patrol_end: str = "05:00"
    accent_color: str = ""
    accent_custom_light: str = ""
    accent_custom_dark: str = ""
    search_mode: str = "text"
    auto_add_new_to_bank: Optional[bool] = None

    @field_validator("deepseek_base_url", "kimi_base_url", "zhipuai_base_url", "xiaomi_token_plan_base_url")
    @classmethod
    def validate_provider_url(cls, value: str) -> str:
        return _valid_http_url(value)

    @field_validator("xiaomi_tts_voice")
    @classmethod
    def validate_tts_voice(cls, value: str) -> str:
        value = str(value or "").strip()
        if len(value) > 32:
            raise ValueError("音色 ID 不能超过 32 字符")
        return value

    @field_validator("focus_voice_engine")
    @classmethod
    def validate_focus_voice_engine(cls, value: str) -> str:
        if value not in ("auto", "browser", "xiaomi"):
            raise ValueError("配音引擎必须是 auto、browser 或 xiaomi")
        if value == "xiaomi":
            from config import ENABLE_XIAOMI_TTS as _xm
            if not _xm:
                raise ValueError("小米配音功能未启用，请选择其他引擎")
        return value

    @field_validator("custom_openai_url")
    @classmethod
    def validate_legacy_custom_url(cls, value: str) -> str:
        return _valid_http_url(value, allow_empty=True)

    @field_validator("dark_mode_start", "dark_mode_end", "night_patrol_start", "night_patrol_end")
    @classmethod
    def validate_time(cls, value: str) -> str:
        value = value.strip()
        if not _TIME_RE.fullmatch(value):
            raise ValueError("时间必须使用 HH:MM 格式")
        return value

    @field_validator("custom_openai_scopes")
    @classmethod
    def validate_scopes(cls, value: List[str]) -> List[str]:
        return list(dict.fromkeys(scope for scope in value if scope in _ALLOWED_CUSTOM_SCOPES))

    @model_validator(mode="after")
    def validate_custom_apis(self):
        fallback_count = sum(api.mode == "fallback" for api in self.custom_apis)
        if fallback_count > 1:
            raise ValueError("最多只能配置一个回退 API")
        return self


class SettingsImportRequest(RootModel[dict[str, Any]]):
    @model_validator(mode="after")
    def validate_size(self):
        if len(self.root) > 200:
            raise ValueError("导入配置字段过多")
        if len(json.dumps(self.root, ensure_ascii=False, separators=(",", ":"))) > 500_000:
            raise ValueError("导入配置不能超过 500KB")
        return self


def _looks_masked(v: str) -> bool:
    """True if the value looks like a masked/placeholder, not a real key"""
    if not v or not v.strip():
        return True
    if v.strip() == "••••••••":
        return True
    if "••" in v:
        return True
    return False


@router.get("")
async def get_settings():
    s = load_settings()
    has_ds = bool(s.get("deepseek_api_key", ""))
    has_km = bool(s.get("kimi_api_key", ""))
    has_zp = bool(s.get("zhipuai_api_key", ""))
    has_xm = bool(str(s.get("xiaomi_token_plan_api_key", "") or "").strip())
    has_custom = bool(s.get("custom_openai_key", ""))
    from config import ENABLE_XIAOMI_TTS as _xm_enabled
    from routers.audio import tts_unavailable_reason
    _reason = tts_unavailable_reason()
    return {
        "deepseek_api_key": mask_key(s.get("deepseek_api_key", "")) if has_ds else "",
        "kimi_api_key": mask_key(s.get("kimi_api_key", "")) if has_km else "",
        "zhipuai_api_key": mask_key(s.get("zhipuai_api_key", "")) if has_zp else "",
        "xiaomi_token_plan_api_key": mask_key(s.get("xiaomi_token_plan_api_key", "")) if has_xm else "",
        "deepseek_base_url": s.get("deepseek_base_url", "https://api.deepseek.com"),
        "deepseek_model": s.get("deepseek_model", "deepseek-v4-pro"),
        "deepseek_max_tokens": s.get("deepseek_max_tokens", 131072),
        "kimi_base_url": s.get("kimi_base_url", "https://api.moonshot.cn/v1"),
        "kimi_model": s.get("kimi_model", "kimi-k2.6"),
        "kimi_max_tokens": s.get("kimi_max_tokens", 4096),
        "xiaomi_token_plan_base_url": s.get(
            "xiaomi_token_plan_base_url", "https://token-plan-cn.xiaomimimo.com/v1"),
        "xiaomi_vision_model": s.get("xiaomi_vision_model", "mimo-v2.5"),
        "xiaomi_tts_voice": s.get("xiaomi_tts_voice", "mimo_default"),
        "focus_voice_engine": s.get("focus_voice_engine", "auto"),
        "tts_available": bool(_xm_enabled and has_xm),
        "tts_unavailable_reason": _reason,
        "custom_openai_key": mask_key(s.get("custom_openai_key", "")) if has_custom else "",
        "custom_openai_url": s.get("custom_openai_url", ""),
        "custom_openai_model": s.get("custom_openai_model", ""),
        "custom_openai_scopes": s.get("custom_openai_scopes", []),
        "custom_apis": _mask_custom_apis(s.get("custom_apis", [])),
        "ai_timeout": s.get("ai_timeout", 900),
        "has_deepseek": has_ds,
        "has_kimi": has_km,
        "has_zhipuai": has_zp,
        "has_xiaomi": has_xm,
        "has_custom_openai": has_custom,
        "zhipuai_base_url": s.get("zhipuai_base_url", "https://open.bigmodel.cn/api/paas/v4"),
        "zhipuai_model": s.get("zhipuai_model", "glm-4v-flash"),
        "zhipuai_max_tokens": s.get("zhipuai_max_tokens", 4096),
        "quote_topic": s.get("quote_topic", ""),
        "dark_mode_auto": s.get("dark_mode_auto", False),
        "dark_mode_start": s.get("dark_mode_start", "18:00"),
        "dark_mode_end": s.get("dark_mode_end", "06:00"),
        "night_patrol_enabled": s.get("night_patrol_enabled", True),
        "night_patrol_start": s.get("night_patrol_start", "01:00"),
        "night_patrol_end": s.get("night_patrol_end", "05:00"),
        "accent_color": s.get("accent_color", ""),
        "accent_custom_light": s.get("accent_custom_light", ""),
        "accent_custom_dark": s.get("accent_custom_dark", ""),
        "search_mode": s.get("search_mode", "text"),
        "auto_add_new_to_bank": s.get("auto_add_new_to_bank", False),
    }


@router.put("")
async def update_settings(data: SettingsUpdate):
    current = load_settings()
    explicit_fields = set(data.model_fields_set)
    changed = bool(explicit_fields)
    payload = data.model_dump()
    secret_fields = {"deepseek_api_key", "kimi_api_key", "zhipuai_api_key", "custom_openai_key", "xiaomi_token_plan_api_key"}

    for field_name in explicit_fields - secret_fields - {"custom_apis"}:
        value = payload[field_name]
        current[field_name] = value.strip() if isinstance(value, str) else value

    for field_name in explicit_fields & secret_fields:
        value = str(payload[field_name] or "")
        if not _looks_masked(value):
            current[field_name] = value.strip()

    if "custom_apis" in explicit_fields:
        old_by_key = {
            (api.get("url", ""), api.get("model", ""), api.get("mode", "replace"), api.get("scope", "")): api
            for api in current.get("custom_apis", []) if isinstance(api, dict)
        }
        new_apis = []
        for api_model in data.custom_apis:
            entry = api_model.model_dump()
            key = (entry["url"], entry["model"], entry["mode"], entry["scope"])
            if _looks_masked(entry.get("key", "")) and key in old_by_key:
                entry["key"] = old_by_key[key].get("key", "")
            new_apis.append(entry)
        current["custom_apis"] = new_apis

    if changed:
        save_settings(current)
        from services.ai_service import ai_service
        ai_service._reload()

    return {
        "message": "已保存",
        "has_deepseek": bool(current["deepseek_api_key"]),
        "has_kimi": bool(current["kimi_api_key"]),
        "has_zhipuai": bool(current.get("zhipuai_api_key", "")),
        "has_xiaomi": bool(str(current.get("xiaomi_token_plan_api_key", "") or "").strip()),
    }


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "••••••••"
    return key[:4] + "••••••••" + key[-4:]


# 敏感配置键的统一判据。历史坑：导出侧只用 `"key" in k.lower()` 判敏感，而密码字段名为
# `api_password`（不含 key）→ 明文导出；导入侧用同一条规则，导致 `api_password` 可被
# PUT /api/settings/import 清空，而密码为空即「鉴权未启用」（见 main.py 的 expected 判定）
# → 一次导入即可关掉全站鉴权。两份规则必须单点维护，新增敏感字段只改这里。
#
# 注意不要加 "token"：本项目的 `*_max_tokens` 是 int（`mask_key` 会 len(int) 抛 TypeError），
# `xiaomi_token_plan_base_url` 是 URL 而非密钥——两者都是误伤。已实测键名清单确认。
_SENSITIVE_KEY_TOKENS = ("key", "password", "secret")


def is_sensitive_setting_key(name: str) -> bool:
    """键名是否属于敏感配置：导出需掩码、导入需拒绝写入。"""
    low = str(name or "").lower()
    return any(token in low for token in _SENSITIVE_KEY_TOKENS)


def _mask_custom_apis(apis: list) -> list:
    """Mask all API keys in custom_apis list for safe frontend response"""
    if not apis:
        return []
    masked = []
    for api in apis:
        entry = dict(api)
        if entry.get("key"):
            entry["key"] = mask_key(entry["key"])
        masked.append(entry)
    return masked


@router.get("/export")
async def export_config():
    s = load_settings()
    export = dict(s)
    for k in list(export.keys()):
        # 只掩码字符串值：判据是「键名启发式」，未来若有数值键命中同类词
        # （如 *_max_tokens），直接喂给 mask_key 会 len(int) 抛 TypeError 打断整个导出。
        if is_sensitive_setting_key(k) and isinstance(export[k], str):
            export[k] = mask_key(export[k])
    export["custom_apis"] = _mask_custom_apis(s.get("custom_apis", []))
    from services.user_profile import load_profile
    export["profile"] = load_profile()
    return export


_URL_SETTING_KEYS = {
    "deepseek_base_url", "kimi_base_url", "zhipuai_base_url",
    "xiaomi_token_plan_base_url",
    "custom_openai_url",
}


@router.put("/import")
async def import_config(request: SettingsImportRequest):
    data = request.root
    s = load_settings()
    previous_settings = dict(s)
    pending_profile = None
    previous_profile = None
    # 仅允许已知的配置键，且拒绝写入任何含敏感 key 的字段
    allowed_keys = set(_default_settings.keys())
    allowed_keys.add("profile")
    for k, v in data.items():
        if k not in allowed_keys:
            continue
        if is_sensitive_setting_key(k):
            # 与上一行注释「拒绝写入任何含敏感 key 的字段」保持一致。旧规则是
            # `"key" not in k`，漏掉 `api_password`：导入空值会把密码清空，而密码为空即
            # 「鉴权未启用」（main.py 中 expected 为空直接放行）→ 一次导入关掉全站鉴权。
            continue
        if k == "profile":
            if not isinstance(v, dict):
                raise HTTPException(400, detail="profile 必须是对象")
            from routers.profile import ProfileUpdate
            from services.user_profile import load_profile, save_profile
            try:
                validated_profile = ProfileUpdate.model_validate(v)
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, detail="profile 格式无效") from exc
            profile = load_profile()
            previous_profile = dict(profile)
            profile.update(validated_profile.model_dump())
            history = v.get("paper_history")
            if history is not None:
                if not isinstance(history, list) or len(history) > 100:
                    raise HTTPException(400, detail="paper_history 必须是至多 100 项的列表")
                clean_history = []
                for item in history:
                    if not isinstance(item, dict):
                        raise HTTPException(400, detail="paper_history 项格式无效")
                    try:
                        paper_id = str(item.get("paper_id", "")).strip()
                        title = str(item.get("title", "")).strip()
                        score = float(item.get("score"))
                        total = float(item.get("total", 100))
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise HTTPException(400, detail="paper_history 分数格式无效") from exc
                    # NaN/Inf 的比较恒为 False，必须先做有限性校验，否则会绕过范围检查写入画像
                    if not (math.isfinite(score) and math.isfinite(total)):
                        raise HTTPException(400, detail="paper_history 分数必须是有限数值")
                    if not paper_id or len(paper_id) > 64 or len(title) > 200 or total <= 0 or score < 0 or score > total:
                        raise HTTPException(400, detail="paper_history 项超出允许范围")
                    clean_history.append({"paper_id": paper_id, "title": title, "score": score, "total": total})
                profile["paper_history"] = clean_history
                percentages = [item["score"] / item["total"] * 100 for item in clean_history]
                profile["avg_score"] = round(sum(percentages) / len(percentages), 1) if percentages else None
            pending_profile = profile
        elif k == "custom_apis":
            clean_apis = []
            old_by_key = {
                (str(api.get("url", "")).rstrip("/"), str(api.get("model", "")), api.get("mode", "replace"), api.get("scope", "")): api
                for api in s.get("custom_apis", []) if isinstance(api, dict)
            }
            if isinstance(v, list):
                for api in v:
                    if not isinstance(api, dict):
                        continue
                    entry = {nk: api.get(nk, "") for nk in ("key", "url", "model", "scope", "mode")}
                    entry["mode"] = entry.get("mode") or "replace"
                    lookup = (
                        str(entry.get("url", "")).rstrip("/"), str(entry.get("model", "")),
                        entry["mode"], entry.get("scope", "")
                    )
                    if _looks_masked(str(entry.get("key", ""))) and lookup in old_by_key:
                        entry["key"] = old_by_key[lookup].get("key", "")
                    try:
                        validated = CustomApiConfig.model_validate(entry)
                    except (TypeError, ValueError):
                        continue
                    clean_apis.append(validated.model_dump())
            if sum(api.get("mode") == "fallback" for api in clean_apis) > 1:
                raise HTTPException(status_code=400, detail="最多只能导入一个回退 API")
            s["custom_apis"] = clean_apis
        elif k == "cors_allowed_origins":
            # 仅允许字符串列表，拒绝 *、userinfo、path 等非法 origin
            if isinstance(v, list):
                s["cors_allowed_origins"] = [
                    origin
                    for origin in (
                        str(o).strip()
                        for o in v
                        if isinstance(o, str)
                    )
                    if CORS_ORIGIN_RE.match(origin)
                ]
        elif k in _URL_SETTING_KEYS:
            try:
                s[k] = _valid_http_url(str(v), allow_empty=(k == "custom_openai_url"))
            except ValueError:
                continue
        elif k in {"deepseek_max_tokens", "kimi_max_tokens", "zhipuai_max_tokens"}:
            try:
                value = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, detail=f"{k} 必须是整数")
            if not 256 <= value <= 131072:
                raise HTTPException(400, detail=f"{k} 超出 256-131072 范围")
            s[k] = value
        elif k == "ai_timeout":
            try:
                value = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, detail="ai_timeout 必须是整数")
            if not 10 <= value <= 3600:
                raise HTTPException(400, detail="ai_timeout 超出 10-3600 秒范围")
            s[k] = value
        elif k in {"dark_mode_start", "dark_mode_end", "night_patrol_start", "night_patrol_end"}:
            value = str(v).strip()
            if not _TIME_RE.fullmatch(value):
                raise HTTPException(400, detail=f"{k} 必须使用 HH:MM 格式")
            s[k] = value
        elif k == "focus_voice_engine":
            if v not in ("auto", "browser", "xiaomi"):
                raise HTTPException(400, detail="focus_voice_engine 必须是 auto、browser 或 xiaomi")
            if v == "xiaomi":
                from config import ENABLE_XIAOMI_TTS as _xm
                if not _xm:
                    raise HTTPException(400, detail="小米配音功能未启用，请选择其他引擎")
            s[k] = v
        elif k == "xiaomi_tts_voice":
            value = str(v or "").strip()
            if len(value) > 32:
                raise HTTPException(400, detail="xiaomi_tts_voice 不能超过 32 字符")
            s[k] = value
        elif k == "search_mode":
            if v not in ("text", "ai", "hybrid"):
                raise HTTPException(400, detail="search_mode 无效")
            s[k] = v
        elif k == "auto_bank_threshold":
            try:
                value = int(v)
            except (TypeError, ValueError):
                raise HTTPException(400, detail="auto_bank_threshold 必须是整数")
            if not 1 <= value <= 1000:
                raise HTTPException(400, detail="auto_bank_threshold 超出 1-1000 范围")
            s[k] = value
        elif k in {"dark_mode_auto", "night_patrol_enabled", "auto_add_new_to_bank"}:
            if not isinstance(v, bool):
                raise HTTPException(400, detail=f"{k} 必须是布尔值")
            s[k] = v
        elif k == "custom_openai_scopes":
            if not isinstance(v, list):
                raise HTTPException(400, detail="custom_openai_scopes 必须是字符串列表")
            s[k] = list(dict.fromkeys(scope for scope in v if isinstance(scope, str) and scope in _ALLOWED_CUSTOM_SCOPES))
        elif "••••" not in str(v):
            # 键名敏感判据已在循环开头由 is_sensitive_setting_key 统一拦截，
            # 这里只保留"掩码值不回写"这一条（防止把 •••• 当真实值存进去）。
            # 字符串键统一截断，防止导入超长字段撑爆配置
            if isinstance(v, str):
                v = v[:5000]
            s[k] = v
    try:
        save_settings(s)
        if pending_profile is not None:
            from services.user_profile import save_profile
            save_profile(pending_profile)
    except Exception as exc:
        try:
            save_settings(previous_settings)
            if previous_profile is not None:
                from services.user_profile import save_profile
                save_profile(previous_profile)
        except Exception as rollback_exc:
            logger.error("Configuration import rollback failed: %s", rollback_exc)
        raise HTTPException(500, detail="配置导入写入失败，已尝试恢复原配置") from exc
    # 导入配置后热重载 AI 服务，使新 base_url/model 立即生效
    from services.ai_service import ai_service
    ai_service._reload()
    return {"message": "配置已导入"}


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_password: str = Field(default="", max_length=128)
    confirm_new_password: str = Field(default="", max_length=128)


@router.get("/password/status")
async def password_status():
    """只返回「是否已设置密码」，不回传密码本身。"""
    from main import _get_auth_password
    return {"has_password": bool(_get_auth_password())}


@router.post("/password")
async def change_password(data: PasswordChangeRequest):
    """设置/修改/关闭访问密码。

    安全约束（与 main.password_guard 对齐）：
    - 已设密码时必须先验当前密码；未设时 current 可空。
    - new 为空 = 关闭鉴权（settings.api_password 清空），允许但需验当前密码。
    - 环境变量 LEARNING_AGENT_PASSWORD 优先于 settings.json：若 env 有值，
      改 settings 里的密码**不会生效**，必须如实告知用户（容量诚实）。
    """
    import hmac as _hmac
    import os as _os
    from main import _get_auth_password

    current = str(data.current_password or "")
    new = str(data.new_password or "")
    confirm = str(data.confirm_new_password or "")
    expected = _get_auth_password()

    if expected and not _hmac.compare_digest(current, expected):
        raise HTTPException(400, detail="当前密码不正确")
    if new != confirm:
        raise HTTPException(400, detail="两次输入的新密码不一致")
    if new and len(new.strip()) < 4:
        raise HTTPException(400, detail="新密码至少 4 位；留空表示关闭访问密码")

    env_pwd = ""
    try:
        env_pwd = (_os.environ.get("LEARNING_AGENT_PASSWORD") or "").strip()
    except Exception:
        pass
    if env_pwd:
        raise HTTPException(
            400,
            detail="服务器通过环境变量 LEARNING_AGENT_PASSWORD 设置了访问密码，"
                   "请先去掉环境变量再在页面改密",
        )

    s = load_settings()
    previous = dict(s)
    s["api_password"] = new.strip()
    try:
        save_settings(s)
    except Exception as exc:
        try:
            save_settings(previous)
        except Exception:
            pass
        raise HTTPException(500, detail="密码写入失败，已尝试恢复") from exc

    disabled = not new.strip()
    return {
        "message": "已关闭访问密码（全站 API 不再校验）" if disabled else "密码已更新",
        "has_password": not disabled,
        "token": "" if disabled else new.strip(),
    }
