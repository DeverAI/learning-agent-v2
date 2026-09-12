"""OISystem 统一 AI 客户端。

封装 KIMI / GLM / DEEPSEEK / XIAOMI 四套 API 调用，支持：
- 文本对话（chat completions）
- 视觉对话（图片 + 文本，GLM/KIMI/XIAOMI 全模态）
- 语音合成（小米 MiMo-V2.5-TTS 讲题朗读）
- 语音转写（小米 MiMo-V2.5 全模态 input_audio，课堂音频 → 文字，round55）
- 模型/密钥从 ConfigManager 读取
"""
import base64
import json
import requests
from typing import List, Dict, Optional

from config.settings import ConfigManager
from utils.helpers import logger
from utils.exceptions import AICallError


def _b64_image(image_bytes: bytes) -> str:
    return base64.b64encode(image_bytes).decode("utf-8")


def _get_provider_config(provider: str) -> dict:
    """provider: kimi/glm/deepseek/xiaomi"""
    s = ConfigManager().settings
    p = provider.lower()
    if p == "kimi":
        return {
            "base_url": s.kimi_base_url,
            "model": s.kimi_model,
            "api_key": s.kimi_api_key,
            "enabled": s.kimi_enabled,
        }
    if p == "glm":
        return {
            "base_url": s.glm_base_url,
            "model": s.glm_model,
            "api_key": s.glm_api_key,
            "enabled": s.glm_enabled,
        }
    if p == "deepseek":
        return {
            "base_url": s.deepseek_base_url,
            "model": s.deepseek_model,
            "api_key": s.deepseek_api_key,
            "enabled": s.deepseek_enabled,
        }
    if p == "xiaomi":
        return {
            "base_url": s.xiaomi_base_url,
            "model": s.xiaomi_model,
            "api_key": s.xiaomi_api_key,
            "enabled": s.xiaomi_enabled,
        }
    raise AICallError(f"未知 provider: {provider}")


def _is_xiaomi(provider: str) -> bool:
    return (provider or "").strip().lower() == "xiaomi"


# 角色字段：每个 provider 配置的 role 决定它服务哪种任务
_ROLE_FIELDS = (
    ("kimi", "kimi_role"),
    ("glm", "glm_role"),
    ("deepseek", "deepseek_role"),
    ("xiaomi", "xiaomi_role"),
)


def resolve_provider_for_role(role: str, default: str = "deepseek") -> str:
    """根据 kimi_role / glm_role / deepseek_role 解析出服务某 role 的 provider。

    role: "knowledge" / "vision" / "dialog"
    返回 provider 名（kimi/glm/deepseek）；未匹配或字段缺失时返回 default。
    """
    s = ConfigManager().settings
    role_l = (role or "").strip().lower()
    for provider, field in _ROLE_FIELDS:
        try:
            if (getattr(s, field, "") or "").strip().lower() == role_l:
                return provider
        except Exception:
            continue
    return default


def _resolve_role_target(role: str, default_provider: str, override_model_field: str):
    """返回某 role 任务的 (provider, model)。

    - provider 按 role 解析；若解析出的 provider 未启用/无 key，回退 default_provider，
      保证核心功能（闲聊判定/元监督/对话/摘要）始终可用。
    - model 优先用 override_model_field（如 ai_flash_model / ai_dialog_model，
      均为 deepseek 系模型名）；当 provider != default_provider 时改用该 provider
      自身配置的模型，避免把 deepseek 系模型名发给 kimi/glm 导致调用失败。
    """
    provider = resolve_provider_for_role(role, default_provider)
    try:
        cfg = _get_provider_config(provider)
        if not cfg["enabled"] or not cfg["api_key"]:
            provider = default_provider
    except AICallError:
        provider = default_provider

    try:
        model = (getattr(ConfigManager().settings, override_model_field, "") or "").strip()
    except Exception:
        model = ""

    if provider != default_provider:
        # 非默认 provider 时改用其自身模型名，绝不混用 deepseek 系 override 模型名；
        # 若该 provider 自身模型名为空，则整体回退到 default_provider，避免发空模型名。
        try:
            pmodel = _get_provider_config(provider).get("model", "") or ""
        except AICallError:
            pmodel = ""
        if pmodel:
            model = pmodel
        else:
            provider = default_provider
    if not model:
        try:
            model = _get_provider_config(provider).get("model", "")
        except AICallError:
            model = ""
    return provider, model


def resolve_flash_target():
    """flash/快速判定任务（闲聊判定/领域判定/摘要/上下文评分/元监督）目标。

    - provider 按 "knowledge" 角色解析（kimi_role/glm_role/deepseek_role）。
    - model 以 ai_flash_model（AITab"Flash 模型"设置项）为权威：
      ai_flash_model 是 deepseek 系模型名而角色路由到非 deepseek 时，改回 deepseek
      （模型名优先于角色，避免把 deepseek-flash 发给 kimi/glm 导致 400）；
      ai_flash_model 为空且 provider 非 deepseek 时，用 provider 自身模型。
    - provider 未启用/无 key 时回退 deepseek，保证 flash 功能始终可用。
    """
    provider = resolve_provider_for_role("knowledge", "deepseek")
    try:
        cfg = _get_provider_config(provider)
        if not cfg["enabled"] or not cfg["api_key"]:
            provider = "deepseek"
    except AICallError:
        provider = "deepseek"

    try:
        model = (ConfigManager().settings.ai_flash_model or "").strip()
    except Exception:
        model = ""

    if provider != "deepseek" and "deepseek" in model.lower():
        # ai_flash_model 是 deepseek 系模型名，角色路由到非 deepseek 时模型名优先，
        # 否则会把 deepseek-flash 发给 kimi/glm 导致模型不存在
        provider = "deepseek"

    if not model:
        try:
            model = _get_provider_config(provider).get("model", "") or ""
        except AICallError:
            model = ""
    return provider, model


def resolve_dialog_target():
    """主对话任务目标。"""
    return _resolve_role_target("dialog", "deepseek", "ai_dialog_model")


def resolve_vision_target():
    """返回视觉任务目标 (provider, model)。

    - provider 按 "vision" 角色解析；screen_engine 模型名显式含 kimi/moonshot/glm
      时覆盖 provider，保证模型名与 provider 一致。
    - provider 未启用/无 key 时回退 glm（视觉兜底）。
    - model 用 screen_engine（仅当与 provider 匹配），否则用 provider 自身模型。
    """
    provider = resolve_provider_for_role("vision", "glm")
    engine = ""
    try:
        engine = (ConfigManager().settings.screen_engine or "").strip()
    except Exception:
        engine = ""
    e_l = engine.lower()
    if "kimi" in e_l or "moonshot" in e_l:
        provider = "kimi"
    elif "glm" in e_l:
        provider = "glm"
    elif "mimo" in e_l or "xiaomi" in e_l:
        provider = "xiaomi"
    # deepseek 不支持视觉，若 role 路由解析到 deepseek 则强制回退 glm，避免视觉永久失败
    if provider == "deepseek":
        provider = "glm"

    # 检查 provider 可用性，不可用时遍历 glm/kimi/xiaomi 选择第一个可用的
    def _is_available(p):
        try:
            c = _get_provider_config(p)
            return c["enabled"] and c["api_key"]
        except AICallError:
            return False

    if not _is_available(provider):
        for fallback in ("glm", "kimi", "xiaomi"):
            if _is_available(fallback):
                provider = fallback
                break

    model = ""
    if engine:
        if (provider == "kimi" and ("kimi" in e_l or "moonshot" in e_l)) or \
           (provider == "glm" and "glm" in e_l) or \
           (provider == "xiaomi" and ("mimo" in e_l or "xiaomi" in e_l)):
            model = engine
    if not model:
        try:
            model = _get_provider_config(provider).get("model", "")
        except AICallError:
            model = ""
    return provider, model


def _extract_reply_content(provider: str, data: dict) -> str:
    """从 chat completions 响应安全提取最终回复。

    - 深度思考模型（如 MiMo）的 message 可能带 reasoning_content（思维链），
      最终答案始终在 content；content 为空视为异常（判定类调用会触发上层
      关键词兜底），避免把空串/思维链误当成回复。
    - 响应体异常信息只保留前 200 字符，防止未知响应结构把大量数据带进日志。
    """
    if not isinstance(data, dict):
        raise AICallError(f"{provider} chat 响应结构异常: {type(data).__name__}")
    try:
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        preview = json.dumps(data, ensure_ascii=False)[:200] if isinstance(data, dict) else str(data)[:200]
        raise AICallError(f"{provider} chat 响应结构异常: {preview}")
    if not str(content).strip():
        raise AICallError(f"{provider} 返回了空回复（可能被思维链消耗或内容审查拦截）")
    return str(content)


def chat(
    messages: List[Dict],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    base_url: Optional[str] = None,
) -> str:
    """文本对话。

    messages: [{"role": "system"/"user"/"assistant", "content": "..."}]
    provider: 未指定时按 "dialog" 角色解析（kimi/glm/deepseek/xiaomi_role）。
    base_url: 可选覆盖 provider 默认端点（审计修复：落实设置页的"对话基础 URL"）
    返回 assistant 回复文本；空回复抛 AICallError。

    小米 MiMo 适配：
    - max_tokens 很小（<=64，闲聊判定/领域判定等 flash 任务）时自动附带
      reasoning_effort="none" 关闭深度思考——实测 MiMo 的思维链会把几十个
      token 全部耗尽导致 content 为空；
    - 深度推理耗时更长，超时放宽到 180s。
    """
    if not provider:
        provider = resolve_provider_for_role("dialog", "deepseek")
    cfg = _get_provider_config(provider)
    if not cfg["enabled"]:
        raise AICallError(f"{provider} 未启用")
    if not cfg["api_key"]:
        raise AICallError(f"{provider} API key 未配置")

    use_model = model or cfg["model"]
    url = f"{(base_url or cfg['base_url']).rstrip('/')}/chat/completions"
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    timeout = 60
    if _is_xiaomi(provider):
        timeout = 180
        try:
            if int(max_tokens) <= 64:
                # 实测：MiMo 只认 reasoning_effort='none'（thinking/enable_thinking 参数无效）
                payload["reasoning_effort"] = "none"
        except (TypeError, ValueError):
            pass
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        # r39 P1 修复：旧版 requests 的 resp.json() 抛 json.JSONDecodeError（非 RequestException 子类）
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"{provider} chat 响应非 JSON: {e}")
            raise AICallError(f"{provider} chat 响应非 JSON: {e}")
        return _extract_reply_content(provider, data)
    except requests.RequestException as e:
        # 安全加固：异常文本可能携带响应片段，截断防止日志膨胀
        logger.error(f"{provider} chat 调用失败: {str(e)[:300]}")
        raise AICallError(f"{provider} chat 失败: {str(e)[:200]}")


def chat_stream(
    messages: List[Dict],
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    base_url: Optional[str] = None,
    on_delta=None,
) -> str:
    """流式对话（SSE）：delta 到达时回调 on_delta(累积文本)，最终返回完整文本。

    - OpenAI 兼容 `stream: true` + `data: {...}` 行协议；`data: [DONE]` 结束。
    - on_delta 收到**累积**全文（非增量），接收方直接上板/上屏无需拼接。
    - 空回复 / 非 JSON / 网络错误一律 AICallError（与 chat 同契约）。
    - round56 课堂补讲用：讲解词逐句流式上板，替代"生成中…"长时间占位。
    """
    if not provider:
        provider = resolve_provider_for_role("dialog", "deepseek")
    cfg = _get_provider_config(provider)
    if not cfg["enabled"]:
        raise AICallError(f"{provider} 未启用")
    if not cfg["api_key"]:
        raise AICallError(f"{provider} API key 未配置")

    use_model = model or cfg["model"]
    url = f"{(base_url or cfg['base_url']).rstrip('/')}/chat/completions"
    payload = {
        "model": use_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    timeout = 120
    if _is_xiaomi(provider):
        timeout = 180
        try:
            if int(max_tokens) <= 64:
                payload["reasoning_effort"] = "none"
        except (TypeError, ValueError):
            pass
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    parts = []
    try:
        # stream=True 必须显式 close：正常结束/[DONE] break/中途异常三条路径都要释放连接（审查 H2）
        resp = requests.post(url, json=payload, headers=headers,
                             timeout=timeout, stream=True)
        try:
            resp.raise_for_status()
            for raw in resp.iter_lines(decode_unicode=False):
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:"):].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                except (json.JSONDecodeError, ValueError):
                    continue     # 忽略 keep-alive/残行，不中断流
                try:
                    delta = (data.get("choices") or [{}])[0].get("delta", {})
                    piece = delta.get("content") or ""
                except (AttributeError, TypeError, IndexError):
                    piece = ""
                if piece:
                    parts.append(piece)
                    if on_delta:
                        try:
                            on_delta("".join(parts))
                        except Exception as e:
                            logger.warning(f"chat_stream on_delta 回调异常（不中断）: {str(e)[:120]}")
        finally:
            resp.close()
        full = "".join(parts).strip()
        if not full:
            raise AICallError(f"{provider} 流式返回空回复")
        return full
    except requests.RequestException as e:
        logger.error(f"{provider} chat_stream 调用失败: {str(e)[:300]}")
        raise AICallError(f"{provider} chat_stream 失败: {str(e)[:200]}")


def vision_chat(
    prompt: str,
    image_bytes: bytes,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 1024,
    base_url: Optional[str] = None,
) -> str:
    """视觉对话（图片 + 文本）。

    provider 未指定时按 "vision" 角色解析（默认 glm-4.6V），也可用 kimi 视觉模型
    或小米 mimo-v2.5 全模态模型。DeepSeek 不支持视觉，直接拒绝。
    """
    if not provider:
        provider = resolve_provider_for_role("vision", "glm")
    cfg = _get_provider_config(provider)
    if not cfg["enabled"]:
        raise AICallError(f"{provider} 未启用")
    if not cfg["api_key"]:
        raise AICallError(f"{provider} API key 未配置")
    if provider == "deepseek":
        raise AICallError("DeepSeek 不支持视觉模型，请用 GLM、KIMI 或小米全模态")

    use_model = model or cfg["model"]
    url = f"{(base_url or cfg['base_url']).rstrip('/')}/chat/completions"
    b64 = _b64_image(image_bytes)
    payload = {
        "model": use_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # MiMo 视觉/结构化输出同样受思维链影响：小 max_tokens 时关闭思考，
    # 否则 JSON 结构化结果会被思维链耗尽返回空 content
    timeout = 120
    if _is_xiaomi(provider):
        timeout = 180
        try:
            if int(max_tokens) <= 64:
                payload["reasoning_effort"] = "none"
        except (TypeError, ValueError):
            pass
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        # r39 P1 修复：同 chat，捕获旧版 requests 的 JSONDecodeError
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"{provider} vision 响应非 JSON: {e}")
            raise AICallError(f"{provider} vision 响应非 JSON: {e}")
        return _extract_reply_content(provider, data)
    except requests.RequestException as e:
        logger.error(f"{provider} vision_chat 调用失败: {str(e)[:300]}")
        raise AICallError(f"{provider} vision_chat 失败: {str(e)[:200]}")


def tts_speech(
    text: str,
    style_instruction: str = "",
    voice: Optional[str] = None,
    model: Optional[str] = None,
) -> bytes:
    """小米 MiMo-V2.5-TTS 语音合成，返回 WAV 音频字节。

    - 端点与 chat 相同（{base}/chat/completions），但消息格式特殊：
      待合成文本必须放在 assistant 消息；可选的 user 消息是自然语言风格指令
      （如"温柔耐心，像老师讲课一样"，不会被朗读）。
    - voice 为内置音色：mimo_default / 冰糖 / 茉莉 / 苏打 / 白桦 /
      Mia / Chloe / Milo / Dean。
    - 返回 choices[0].message.audio.data 的 base64 解码结果（WAV/RIFF）。
    """
    s = ConfigManager().settings
    cfg = _get_provider_config("xiaomi")
    if not cfg["enabled"]:
        raise AICallError("xiaomi 未启用")
    if not cfg["api_key"]:
        raise AICallError("xiaomi API key 未配置")

    clean_text = str(text or "").strip()
    if not clean_text:
        raise AICallError("TTS 待合成文本为空")
    if len(clean_text) > 2000:
        clean_text = clean_text[:2000]

    use_model = (model or s.xiaomi_tts_model or "mimo-v2.5-tts").strip()
    use_voice = (voice or s.xiaomi_tts_voice or "mimo_default").strip()
    messages = []
    style = str(style_instruction or "").strip()
    if style:
        messages.append({"role": "user", "content": style[:500]})
    messages.append({"role": "assistant", "content": clean_text})

    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    payload = {
        "model": use_model,
        "messages": messages,
        "audio": {"format": "wav", "voice": use_voice},
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"xiaomi tts 响应非 JSON: {e}")
            raise AICallError(f"xiaomi tts 响应非 JSON: {e}")
        if not isinstance(data, dict):
            raise AICallError(f"xiaomi tts 响应结构异常: {type(data).__name__}")
        try:
            audio_b64 = data["choices"][0]["message"]["audio"]["data"]
        except (KeyError, IndexError, TypeError, AttributeError):
            preview = json.dumps(data, ensure_ascii=False)[:200]
            raise AICallError(f"xiaomi tts 响应缺少音频数据: {preview}")
        if not audio_b64:
            raise AICallError("xiaomi tts 返回空音频")
        try:
            wav_bytes = base64.b64decode(audio_b64)
        except (ValueError, TypeError) as e:   # binascii.Error 是 ValueError 子类
            raise AICallError(f"xiaomi tts 音频数据非法 base64: {str(e)[:100]}")
        if not wav_bytes.startswith(b"RIFF"):
            logger.warning("xiaomi tts 返回的不是 WAV/RIFF 头，尝试原样播放")
        return wav_bytes
    except requests.RequestException as e:
        logger.error(f"xiaomi tts 调用失败: {str(e)[:300]}")
        raise AICallError(f"xiaomi tts 失败: {str(e)[:200]}")


# 课堂转写提示词：只要逐字文本，禁止模型加解释/标点建议/翻译
ASR_TRANSCRIBE_PROMPT = "请逐字转写这段音频里说话人的内容，只输出转写文字本身，不要任何解释、标注或额外说明。"


def transcribe_audio(
    wav_bytes: bytes,
    prompt: str = "",
    model: Optional[str] = None,
    max_tokens: int = 512,
) -> str:
    """小米 MiMo 全模态音频输入转写（课堂音频 → 文字），返回转写文本。

    实测契约（2026-09-06 探针验证通过）：
    - 端点同 chat/completions；音频走 OpenAI 风格 `input_audio` 内容块：
      `{"type":"input_audio","input_audio":{"data":<base64 wav>,"format":"wav"}}`
    - 模型用 `xiaomi_asr_model`（默认 mimo-v2.5），**不复用** `xiaomi_model`
      （对话用的 -pro 名是否接受音频输入未验证）。
    - 必须带 `reasoning_effort="none"`：MiMo 默认开思考，思维链会吃光
      max_tokens 导致 content 为空（本项目既有实测结论）。
    - 空转写（纯静音/无人声）返回空串而非抛错，由调用方按"该段无内容"处理。
    """
    if not wav_bytes:
        raise AICallError("转写音频为空")
    s = ConfigManager().settings
    cfg = _get_provider_config("xiaomi")
    if not cfg["enabled"]:
        raise AICallError("xiaomi 未启用，无法转写课堂音频")
    if not cfg["api_key"]:
        raise AICallError("xiaomi API key 未配置，无法转写课堂音频")

    use_model = (model or getattr(s, "xiaomi_asr_model", "") or "mimo-v2.5").strip()
    use_prompt = (prompt or ASR_TRANSCRIBE_PROMPT).strip()[:500]
    audio_b64 = base64.b64encode(wav_bytes).decode("utf-8")
    payload = {
        "model": use_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": use_prompt},
                {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}},
            ],
        }],
        "max_tokens": max(64, int(max_tokens or 512)),
        "reasoning_effort": "none",
    }
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"xiaomi asr 响应非 JSON: {e}")
            raise AICallError(f"xiaomi asr 响应非 JSON: {e}")
        # 不走 _extract_reply_content：它把"结构异常"与"空 content"折叠成同一种
        # AICallError。对 ASR 而言空 content = 静音段，属正常（返回空串由调用方
        # 跳过）；结构异常 = 系统性故障，必须抛出，让 worker 计入 failed 并触发
        # 连续失败暂停保护——否则坏响应会让转写流无声变空（M1）
        try:
            content = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            preview = json.dumps(data, ensure_ascii=False)[:200]
            logger.error(f"xiaomi asr 响应结构异常: {preview}")
            raise AICallError(f"xiaomi asr 响应结构异常: {preview}")
        return str(content).strip()
    except requests.RequestException as e:
        logger.error(f"xiaomi asr 调用失败: {str(e)[:300]}")
        raise AICallError(f"xiaomi asr 失败: {str(e)[:200]}")
