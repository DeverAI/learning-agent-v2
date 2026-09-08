"""小米 MiMo TTS 配音端点。

专注模式讲解语音（也可被其他页面复用）：前端 POST 文本，后端调用
token plan 网关的 mimo-v2.5-tts 模型合成音频并原样转发字节流。
设计约束见 Design.md 12.5 / Techniques.md 21.4：
- 音频不落盘，内存中转；
- 未启用或未配置 Key 返回 503，上游失败返回 502 并写 Err.log；
- ENABLE_XIAOMI_TTS=False 时路由不注册（双保险：请求时也校验）。
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from config import load_settings, ENABLE_XIAOMI_TTS
from logger import get_logger, log_error

router = APIRouter(tags=["audio"])
logger = get_logger()


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str = Field(default="", max_length=32)


def tts_unavailable_reason() -> str:
    """返回不可用原因；空串表示可用。设置页与前端配置读取复用。"""
    if not ENABLE_XIAOMI_TTS:
        return "小米配音功能未启用"
    key = load_settings().get("xiaomi_token_plan_api_key", "")
    if not str(key or "").strip():
        return "未配置小米 token plan API Key"
    return ""


@router.post("/api/tts")
async def api_tts(data: TTSRequest):
    reason = tts_unavailable_reason()
    if reason:
        raise HTTPException(status_code=503, detail=reason)

    text = data.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="朗读文本为空")
    # 去掉控制字符（保留换行与制表符），避免异常字节进入上游；
    # 孤立 UTF-16 代理对经 JSON 序列化最多被上游拒绝为普通错误，不致崩溃
    text = "".join(ch for ch in text if ch in ("\n", "\t") or ord(ch) >= 32)

    from services.ai_service import ai_service
    try:
        audio_bytes, mime = await ai_service.xiaomi_tts(text, data.voice.strip())
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log_error("tts", f"Xiaomi TTS failed: {exc}")
        msg = str(exc)
        if "not configured" in msg:
            raise HTTPException(status_code=503, detail="未配置小米 token plan API Key")
        if "AI API 401" in msg or "AI API 403" in msg:
            raise HTTPException(status_code=502, detail="小米配音鉴权失败，请检查 Token Plan API Key")
        raise HTTPException(status_code=502, detail="语音合成失败，请稍后重试")

    return Response(
        content=audio_bytes,
        media_type=mime,
        headers={"Cache-Control": "no-store"},
    )
