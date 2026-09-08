import re
import base64
from typing import Optional
from services.ai_service import ai_service
from logger import get_logger, log_error

logger = get_logger()

ALLOWED_IMAGE_TYPES = {"jpeg", "png"}
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2MB


def _detect_image_type(data: bytes) -> Optional[str]:
    """纯 Python 图片类型检测（替代已废弃的 imghdr）"""
    if data[:3] == b'\xff\xd8\xff':
        return "jpeg"
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "png"
    return None
FACE_ANALYSIS_PROMPT = """你是一位教育心理学专家。请分析这张学生的面部表情照片，判断其当前学习状态。

可能的状态类别：
- focused: 专注、认真听讲
- confused: 困惑、不理解
- neutral: 中性、无明显情绪
- tired: 疲劳、困倦
- positive: 积极、理解、兴奋
- negative: 消极、沮丧、厌烦
- distracted: 走神、注意力不集中

请输出严格的 JSON 格式（不要任何额外文字）：
{"expression": "状态类别", "confidence": 0.0-1.0, "indicators": ["观察到的具体表现"], "overall_state": "understanding|confused|distracted|tired|engaged", "suggestion": "continue|simplify|pause|encourage"}"""

DEFAULT_RESULT = {
    "expression": "neutral",
    "confidence": 0.5,
    "indicators": [],
    "overall_state": "engaged",
    "suggestion": "continue",
}


def _validate_image_bytes(image_bytes: bytes) -> Optional[str]:
    """校验图片格式和大小，返回图片类型或 None"""
    if not image_bytes or len(image_bytes) == 0:
        return None
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return None
    return _detect_image_type(image_bytes)


async def analyze_face(image_base64: str) -> dict:
    """
    分析学生面部表情，返回结构化情感报告。
    image_base64: Base64 编码的图片数据（不含 data URI 前缀）
    """
    if not image_base64:
        return dict(DEFAULT_RESULT)

    try:
        # 清理 data URI 前缀
        clean_base64 = image_base64
        if "," in image_base64:
            clean_base64 = image_base64.split(",", 1)[1]

        # Base64 编码膨胀系数约 4/3，提前限制
        if len(clean_base64) > (MAX_IMAGE_BYTES * 4 // 3) + 100:
            log_error("face_analysis", "base64 input too large")
            return dict(DEFAULT_RESULT)

        # 解码并校验
        image_bytes = base64.b64decode(clean_base64)
        img_type = _validate_image_bytes(image_bytes)
        if not img_type:
            log_error("face_analysis", "invalid image format or size")
            return dict(DEFAULT_RESULT)

        # 调用视觉模型（异步）
        result = await _call_vision_async(clean_base64, FACE_ANALYSIS_PROMPT)

        if not result:
            return dict(DEFAULT_RESULT)

        # 解析结果
        if isinstance(result, dict):
            return _normalize_emotion_report(result)
        elif isinstance(result, str):
            # 尝试从文本中提取 JSON
            json_match = re.search(r'\{[^}]+\}', result)
            if json_match:
                import json
                try:
                    parsed = json.loads(json_match.group())
                    return _normalize_emotion_report(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            return dict(DEFAULT_RESULT)
        else:
            return dict(DEFAULT_RESULT)

    except Exception as e:
        log_error("face_analysis", f"analyze_face failed: {e}")
        return dict(DEFAULT_RESULT)


def _normalize_emotion_report(data: dict) -> dict:
    """归一化表情分析结果"""
    valid_expressions = {"confused", "focused", "neutral", "tired", "positive", "negative", "distracted"}
    valid_states = {"understanding", "confused", "distracted", "tired", "engaged"}
    valid_suggestions = {"continue", "simplify", "pause", "encourage"}

    expression = data.get("expression", "neutral")
    if expression not in valid_expressions:
        expression = "neutral"

    try:
        confidence = float(data.get("confidence", 0.5))
        # NaN 与任何值比较恒为 False，max/min 夹紧对 NaN 失效（FreqErr [NaN 校验绕过]）；
        # 显式范围判断让 NaN/inf 落到回退值 0.5
        confidence = confidence if 0.0 <= confidence <= 1.0 else 0.5
    except (TypeError, ValueError):
        confidence = 0.5

    indicators = data.get("indicators", [])
    if not isinstance(indicators, list):
        indicators = []
    indicators = [str(i)[:100] for i in indicators[:5]]

    overall_state = data.get("overall_state", "engaged")
    if overall_state not in valid_states:
        overall_state = "engaged"

    suggestion = data.get("suggestion", "continue")
    if suggestion not in valid_suggestions:
        suggestion = "continue"

    return {
        "expression": expression,
        "confidence": confidence,
        "indicators": indicators,
        "overall_state": overall_state,
        "suggestion": suggestion,
    }


async def _call_vision_async(image_base64: str, prompt: str) -> dict | str | None:
    """异步调用视觉模型（模型分工：MiMo 全模态优先，ZhipuAI 回退；
    Kimi 无视觉输入能力，兜底链已移除——功能检查轮）。"""
    if ai_service.xm_key:
        try:
            result = await ai_service.xiaomi_vision(image_base64, prompt, parse_json=False)
            if isinstance(result, str) and result.strip():
                return result
        except Exception as e:
            logger.warning("xiaomi_vision failed: %s", str(e)[:200])

    try:
        result = await ai_service.zhipuai_vision(image_base64, prompt, parse_json=True)
        if isinstance(result, dict):
            return result
        if isinstance(result, str) and result.strip():
            return result
    except Exception as e:
        logger.warning("zhipuai_vision failed: %s", str(e)[:200])

    return None


def merge_emotion_report(face_report: dict, voice_features: dict) -> dict:
    """
    合并表情分析和语音情感特征，生成综合情感报告。
    face_report: 表情分析结果
    voice_features: 语音特征 {"speed": "slow|normal|fast", "volume": "quiet|normal|loud", "pause_count": int}
    """
    face_state = face_report.get("overall_state", "engaged")
    face_suggestion = face_report.get("suggestion", "continue")
    face_confidence = face_report.get("confidence", 0.5)

    # 语音特征加权
    voice_speed = voice_features.get("speed", "normal") if isinstance(voice_features, dict) else "normal"
    voice_volume = voice_features.get("volume", "normal") if isinstance(voice_features, dict) else "normal"
    try:
        pause_count = int(voice_features.get("pause_count", 0)) if isinstance(voice_features, dict) else 0
    except (TypeError, ValueError):
        pause_count = 0

    # 综合判断
    overall_state = face_state
    suggestion = face_suggestion

    # 语音特征辅助判断
    if voice_speed == "slow" and pause_count > 3:
        if face_state in ("engaged", "neutral"):
            overall_state = "confused"
            suggestion = "simplify"
    elif voice_volume == "quiet" and face_state == "neutral":
        overall_state = "confused"
    elif voice_speed == "fast" and voice_volume == "loud":
        if face_state in ("engaged", "neutral"):
            overall_state = "engaged"
            suggestion = "continue"

    # 综合置信度
    voice_confidence = 0.5
    if voice_speed != "normal" or voice_volume != "normal":
        voice_confidence = 0.7
    combined_confidence = round(face_confidence * 0.6 + voice_confidence * 0.4, 2)

    return {
        "overall_state": overall_state,
        "confidence": combined_confidence,
        "suggestion": suggestion,
        "face_expression": face_report.get("expression", "neutral"),
        "voice_features": {
            "speed": voice_speed,
            "volume": voice_volume,
            "pause_count": pause_count,
        },
        "indicators": face_report.get("indicators", []),
    }
