import re
import json
import time
import asyncio
import math
import httpx
from config import load_settings, ENABLE_STRUCTURE_GRAPH, ENABLE_CALCULATOR
from logger import get_logger, log_error

logger = get_logger()

MAX_RETRIES = 2
RETRY_DELAY = 3.0
# 推理模型（本项目主力 deepseek-v4-pro）会把输出预算先花在 `reasoning_content` 上，
# 正文可能在预算耗尽时一个 token 都没轮到（`finish_reason=length` + `content` 为空）。
# 实测同一个备课 prompt：2048 -> 正文 0 字；4096 仍为 0；8192 才拿到 1404 字。
# 因此"空正文 + 撞上限"的重试**至少**要跳到这个值，只做 ×2 等于白烧一次调用。
REASONING_SAFE_MIN = 8192

# Agent 步骤可视化：按 session_id 存储最近步骤（内存级，重启清空）
_agent_step_stores: dict[str, list[dict]] = {}
_tool_call_stores: dict[str, list[dict]] = {}
# 有界淘汰上限：sid 只在会话聊天开始时清理，废弃 sid 的记录按插入序淘汰，
# 防止长期运行内存无限增长。
_AGENT_STORE_MAX_SESSIONS = 256


class AIEmptyResponseError(Exception):
    """模型返回空内容/空 choices：可重试，且应允许回退到备用模型。"""
    pass


def _safe_int(value, default: int, minimum: int = 1, maximum: int = 131072) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _safe_number(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是"}
    return bool(value)


def normalize_correction_report(report) -> dict:
    """Normalize untrusted grader JSON and enforce all score boundaries."""
    parsed = report if isinstance(report, dict) else {}
    normalized_points = []
    for position, point in enumerate(parsed.get("points", []) or []):
        if not isinstance(point, dict):
            continue
        try:
            index = int(point.get("index", position))
        except (TypeError, ValueError):
            index = position
        point_max = max(0.0, _safe_number(point.get("max_score", 0)))
        point_score = max(0.0, min(point_max, _safe_number(point.get("score", 0))))
        normalized_points.append({
            "index": index,
            "title": str(point.get("title", f"得分点{position + 1}")),
            "score": point_score,
            "max_score": point_max,
            "comment": str(point.get("comment", "")),
            "hit": _safe_bool(point.get("hit", point_max > 0 and point_score >= point_max)),
        })

    raw_max = max(0.0, _safe_number(parsed.get("max_score", 0)))
    raw_score = max(0.0, _safe_number(parsed.get("score", 0)))
    if normalized_points:
        max_score = sum(point["max_score"] for point in normalized_points)
        score = sum(point["score"] for point in normalized_points)
    else:
        max_score = raw_max
        score = min(raw_score, max_score)
        if max_score > 0:
            normalized_points.append({
                "index": 0, "title": "总分", "score": score, "max_score": max_score,
                "comment": str(parsed.get("feedback", "")), "hit": score >= max_score,
            })
    return {
        "score": score,
        "max_score": max_score,
        "points": normalized_points,
        "feedback": str(parsed.get("feedback", "")),
        "error_analysis": str(parsed.get("error_analysis", "")),
        "suggestions": str(parsed.get("suggestions", "")),
    }


def _complete_svg(raw: str) -> str:
    match = re.search(r"<svg\b[\s\S]*?</svg\s*>", raw or "", flags=re.IGNORECASE)
    return match.group(0).strip() if match else ""


def agent_steps_clear(sid: str):
    """清空某会话的步骤记录。"""
    _agent_step_stores.pop(sid, None)


def _bound_agent_store(store: dict) -> None:
    """超过上限时按插入序淘汰最旧的会话记录。"""
    while len(store) > _AGENT_STORE_MAX_SESSIONS:
        try:
            store.pop(next(iter(store)), None)
        except StopIteration:
            break


def agent_steps_get(sid: str) -> list[dict]:
    """获取某会话当前步骤副本。"""
    return list(_agent_step_stores.get(sid, []))


def agent_tool_calls_clear(sid: str):
    """清空某会话的工具调用记录。"""
    _tool_call_stores.pop(sid, None)


def agent_tool_calls_get(sid: str) -> list[dict]:
    """获取某会话的工具调用列表。"""
    return list(_tool_call_stores.get(sid, []))


def agent_tool_calls_add(sid: str, name: str, args: dict, result: str = "", status: str = "done"):
    """记录一次工具调用。"""
    if not sid:
        return
    store = _tool_call_stores.setdefault(sid, [])
    _bound_agent_store(_tool_call_stores)
    store.append({
        "name": name,
        "args": args if isinstance(args, dict) else {},
        "result": (result[:2000] if isinstance(result, str) else str(result)[:2000]) if result else "",
        "status": status,
        "time": time.strftime("%H:%M:%S"),
    })


def agent_steps_add(sid: str, parent: str, child: str, status: str = "running"):
    """添加或更新步骤。status 允许 running/done/error。"""
    if not sid:
        return
    store = _agent_step_stores.setdefault(sid, [])
    _bound_agent_store(_agent_step_stores)
    now = time.strftime("%H:%M:%S")
    for st in store:
        if st.get("parent") == parent and st.get("child") == child:
            st["status"] = status
            st["time"] = now
            return
    store.append({"parent": parent, "child": child, "status": status, "time": now})


def _make_step_callback(sid: str):
    """生成绑定到会话的步骤回调函数。"""
    if not sid:
        return None

    def callback(parent: str, child: str, status: str = "running"):
        agent_steps_add(sid, parent, child, status)

    return callback


def _fix_json_newlines(text: str) -> str:
    """Replace unescaped newlines inside JSON strings with \\n."""
    result = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            result.append(ch)
            continue
        if ch == '"' and not escape:
            in_string = not in_string
            result.append(ch)
            continue
        if in_string and ch in '\n\r':
            result.append('\\n')
            continue
        result.append(ch)
    return ''.join(result)


def _repair_truncated_json(text: str) -> str:
    """Attempt to repair truncated JSON by closing unclosed strings/objects/arrays."""
    if not text:
        return '{}'
    if text[0] != '{':
        return text
    inside_string = False
    escape = False
    for ch in text[1:]:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            inside_string = not inside_string
    if inside_string:
        text += '\\n"'
    depth = 0
    for ch in text:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
    while depth > 0:
        text += '}'
        depth -= 1
    return text


class AIService:

    def __init__(self):
        self._reload()

    def _reload(self):
        s = load_settings()
        self.ds_key = s.get("deepseek_api_key", "")
        ds_base = str(s.get('deepseek_base_url', 'https://api.deepseek.com')).rstrip('/')
        if ds_base.endswith('/chat/completions'):
            self.ds_url = ds_base
        elif ds_base.endswith('/v1'):
            self.ds_url = ds_base + '/chat/completions'
        else:
            self.ds_url = ds_base + '/v1/chat/completions'
        self.km_key = s.get("kimi_api_key", "")
        km_base = str(s.get('kimi_base_url', 'https://api.moonshot.cn/v1')).rstrip('/')
        self.km_url = km_base if km_base.endswith('/chat/completions') else km_base + '/chat/completions'
        self.zp_key = s.get("zhipuai_api_key", "")
        zp_base = s.get('zhipuai_base_url','https://open.bigmodel.cn/api/paas/v4').rstrip('/')
        if not zp_base.endswith('/chat/completions'):
            zp_base += '/chat/completions'
        self.zp_url = zp_base
        self.ds_model = s.get("deepseek_model", "deepseek-v4-pro")
        self.km_model = s.get("kimi_model", "kimi-k2.6")
        self.zp_model = s.get("zhipuai_model", "glm-4v-flash")
        self.ds_max_tokens = _safe_int(s.get("deepseek_max_tokens", 16384), 16384)
        self.km_max_tokens = _safe_int(s.get("kimi_max_tokens", 4096), 4096)
        self.zp_max_tokens = _safe_int(s.get("zhipuai_max_tokens", 4096), 4096)
        # 小米 MiMo（token plan CN 网关）：视觉备用链路 + TTS 配音
        xm_base = str(s.get("xiaomi_token_plan_base_url", "")).strip().rstrip("/")
        if not xm_base:
            xm_base = "https://token-plan-cn.xiaomimimo.com/v1"
        self.xm_key = str(s.get("xiaomi_token_plan_api_key", "")).strip()
        if xm_base.endswith("/chat/completions"):
            self.xm_url = xm_base
        elif xm_base.endswith("/v1"):
            self.xm_url = xm_base + "/chat/completions"
        else:
            self.xm_url = xm_base + "/v1/chat/completions"
        self.xm_model = str(s.get("xiaomi_vision_model", "mimo-v2.5")).strip() or "mimo-v2.5"
        self.xm_tts_model = str(s.get("xiaomi_tts_model", "mimo-v2.5-tts")).strip() or "mimo-v2.5-tts"
        self.xm_tts_voice = str(s.get("xiaomi_tts_voice", "mimo_default")).strip() or "mimo_default"
        # Custom APIs: build scope-to-config map
        self.custom_apis = s.get("custom_apis", [])
        self.custom_scope_map = {}
        self.fallback_config = None  # single fallback model
        for api in self.custom_apis:
            scope = api.get("scope", "")
            mode = api.get("mode", "replace")
            if not api.get("key"):
                continue
            if mode == "fallback":
                # `or ""`：url 显式为 null 时 api.get 返回 None，.strip() 会崩掉整个 _reload
                url = (api.get("url") or "").strip().rstrip('/')
                if not url:
                    # 只有 key 没有 url 的 fallback 配置无法回退：
                    # 保留它会让每次主模型失败都先打一个空 URL（InvalidURL）并写错误日志。
                    logger.warning("Fallback custom_api has key but empty url; ignored")
                    continue
                if not url.endswith('/chat/completions'):
                    url += '/chat/completions'
                self.fallback_config = {
                    "key": api["key"],
                    "url": url,
                    "model": api.get("model", ""),
                }
            elif scope:
                url = (api.get("url") or "").strip().rstrip('/')
                if not url:
                    # scope 自定义 API 无 url 同样不可用：保留会让调用点向空 URL 发请求
                    logger.warning("Scope custom_api %s has key but empty url; ignored", scope)
                    continue
                if not url.endswith('/chat/completions'):
                    url += '/chat/completions'
                self.custom_scope_map[scope] = {
                    "key": api["key"],
                    "url": url,
                    "model": api.get("model", ""),
                }

    @staticmethod
    def _extract_content(data: dict) -> str:
        """从 chat/completions 响应体安全提取 content，畸形结构返回空串。"""
        try:
            choices = data.get("choices") or []
            if not choices:
                return ""
            msg_obj = choices[0].get("message") or {}
            return str(msg_obj.get("content") or "")
        except (AttributeError, TypeError, IndexError, KeyError):
            return ""

    async def _call(self, url: str, key: str, payload: dict, timeout: int = None, _is_fallback: bool = False) -> str:
        if timeout is None:
            from config import load_settings
            _s = load_settings()
            try:
                timeout = float(_s.get("ai_timeout", 900))
            except (TypeError, ValueError):
                timeout = 900
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        self._log_error(url, payload, resp.status_code, resp.text[:500])
                        if resp.status_code in (429, 502, 503):
                            if attempt < MAX_RETRIES:
                                wait = RETRY_DELAY * attempt
                                logger.warning("AI API %d on attempt %d/%d, retrying in %.1fs...",
                                              resp.status_code, attempt, MAX_RETRIES, wait)
                                await asyncio.sleep(wait)
                                continue
                        # Try fallback model if configured (skip if already using fallback or same model)
                        if self.fallback_config and self.fallback_config["model"] != payload.get("model", ""):
                            fb = self.fallback_config
                            logger.info("Falling back to model %s for failed call (status %d)", fb["model"], resp.status_code)
                            fb_payload = dict(payload)
                            fb_payload["model"] = fb["model"]
                            fb_headers = {"Authorization": f"Bearer {fb['key']}", "Content-Type": "application/json"}
                            for fb_attempt in range(1, MAX_RETRIES + 1):
                                try:
                                    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as fb_client:
                                        fb_resp = await fb_client.post(fb["url"], json=fb_payload, headers=fb_headers)
                                        if fb_resp.status_code == 200:
                                            try:
                                                fb_content = self._extract_content(fb_resp.json())
                                            except ValueError:
                                                fb_content = ""
                                            if fb_content.strip():
                                                return fb_content
                                            logger.warning("Fallback model returned malformed/empty body on attempt %d", fb_attempt)
                                            break
                                        self._log_error(fb["url"], fb_payload, fb_resp.status_code, fb_resp.text[:300])
                                        if fb_resp.status_code in (429, 502, 503) and fb_attempt < MAX_RETRIES:
                                            await asyncio.sleep(RETRY_DELAY * fb_attempt)
                                            continue
                                        logger.warning("Fallback model failed with status %d on attempt %d", fb_resp.status_code, fb_attempt)
                                        break
                                except (httpx.TimeoutException, httpx.ConnectError) as fbe:
                                    logger.warning("Fallback model network error on attempt %d: %s", fb_attempt, fbe)
                                    if fb_attempt < MAX_RETRIES:
                                        await asyncio.sleep(RETRY_DELAY * fb_attempt)
                                        continue
                                    break
                            logger.warning("Fallback model exhausted all retries")
                        clean_body = self._sanitize_error_body(resp.text[:500])
                        raise Exception(f"AI API {resp.status_code}: {clean_body}")
                    data = resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise AIEmptyResponseError("AI returned empty choices array")
                    choice = choices[0]
                    msg_obj = choice.get("message") or {}
                    content = msg_obj.get("content") or ""
                    if not str(content).strip():
                        finish_reason = choice.get("finish_reason", "")
                        if finish_reason == "length" and attempt < MAX_RETRIES:
                            current_tokens = _safe_int(payload.get("max_tokens", 0), 0)
                            # 至少跳到 REASONING_SAFE_MIN，不能只 ×2。
                            #
                            # 本项目主力模型是**推理模型**：预算会先被 `reasoning_content`
                            # 吃掉。实测同一个备课 prompt：2048 -> 正文长度 0；
                            # 只翻倍到 4096 仍然为 0；8192 才拿到 1404 字正文。
                            # 所以"×2"在这种模型上等于多浪费一次调用。
                            new_tokens = max(REASONING_SAFE_MIN,
                                             min(max(current_tokens * 2, 1024), 131072))
                            payload = dict(payload)
                            payload["max_tokens"] = new_tokens
                            _usage = data.get("usage") or {}
                            _details = _usage.get("completion_tokens_details") or {}
                            logger.warning(
                                "AI returned empty content after token limit; "
                                "retrying with max_tokens=%d (prev=%d, completion=%s, "
                                "reasoning=%s) —— 推理模型把预算烧在 reasoning 上了",
                                new_tokens, current_tokens,
                                _usage.get("completion_tokens"),
                                _details.get("reasoning_tokens"),
                            )
                            continue
                        raise AIEmptyResponseError("AI returned empty assistant content")
                    return str(content)
            except (httpx.TimeoutException, httpx.ReadError, httpx.WriteError) as e:
                last_error = f"Network/Timeout: {type(e).__name__}: {e}"
            except httpx.ConnectError as e:
                last_error = f"ConnectError: {e}"
            except AIEmptyResponseError as e:
                # 空响应属于业务层失败：允许重试并在耗尽后回退备用模型
                last_error = str(e)
            except Exception as e:
                if "AI API" in str(e):
                    raise
                if not isinstance(e, (httpx.HTTPError, OSError, asyncio.TimeoutError)):
                    raise
                last_error = str(e)
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY * attempt
                logger.warning("AI call attempt %d/%d failed: %s, retrying in %.1fs...",
                              attempt, MAX_RETRIES, last_error, wait)
                await asyncio.sleep(wait)
        if not _is_fallback and self.fallback_config and self.fallback_config["model"] != payload.get("model", ""):
            fb = self.fallback_config
            logger.warning(
                "Primary AI exhausted network/content retries; falling back to %s: %s",
                fb["model"], last_error,
            )
            fb_payload = dict(payload)
            fb_payload["model"] = fb["model"]
            return await self._call(fb["url"], fb["key"], fb_payload, timeout, _is_fallback=True)
        raise Exception(f"AI call failed after {MAX_RETRIES} attempts: {last_error}")

    async def _call_raw(self, url: str, key: str, payload: dict, timeout: int = None,
                        _is_fallback: bool = False, max_retries: int = MAX_RETRIES) -> dict:
        """底层调用，返回完整的 message 对象（支持 function calling）。"""
        if timeout is None:
            from config import load_settings
            _s = load_settings()
            try:
                timeout = float(_s.get("ai_timeout", 900))
            except (TypeError, ValueError):
                timeout = 900
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        self._log_error(url, payload, resp.status_code, resp.text[:500])
                        if resp.status_code in (429, 502, 503) and attempt < max_retries:
                            wait = RETRY_DELAY * attempt
                            logger.warning("AI API %d on attempt %d/%d, retrying in %.1fs...",
                                          resp.status_code, attempt, max_retries, wait)
                            await asyncio.sleep(wait)
                            continue
                        clean_body = self._sanitize_error_body(resp.text[:500])
                        raise Exception(f"AI API {resp.status_code}: {clean_body}")
                    data = resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise ValueError("AI API returned empty choices array")
                    return choices[0].get("message") or {}
            except (httpx.TimeoutException, httpx.ReadError, httpx.WriteError) as e:
                last_error = f"Network/Timeout: {type(e).__name__}: {e}"
            except httpx.ConnectError as e:
                last_error = f"ConnectError: {e}"
            except Exception as e:
                if "AI API" in str(e):
                    raise
                if not isinstance(e, (httpx.HTTPError, OSError, asyncio.TimeoutError)):
                    raise
                last_error = str(e)
            if attempt < max_retries:
                wait = RETRY_DELAY * attempt
                logger.warning("AI raw call attempt %d/%d failed: %s, retrying in %.1fs...",
                              attempt, max_retries, last_error, wait)
                await asyncio.sleep(wait)
        if not _is_fallback and self.fallback_config and self.fallback_config["model"] != payload.get("model", ""):
            fb = self.fallback_config
            logger.warning(
                "Primary tool AI exhausted retries; falling back to %s: %s",
                fb["model"], last_error,
            )
            fb_payload = dict(payload)
            fb_payload["model"] = fb["model"]
            return await self._call_raw(fb["url"], fb["key"], fb_payload, timeout, _is_fallback=True)
        raise Exception(f"AI raw call failed after {max_retries} attempts: {last_error}")

    def _log_error(self, url: str, payload: dict, code: int, body: str):
        model = payload.get("model", "unknown")
        clean_body = self._sanitize_error_body(body[:200])
        log_error("ai_service", f"HTTP {code} model={model}: {clean_body}")

    @staticmethod
    def _sanitize_error_body(body: str) -> str:
        """脱敏错误响应体，避免 API Key 等敏感信息随异常返回给上层/前端。"""
        if not body:
            return body
        # 脱敏常见 API Key 格式（DeepSeek sk-...、Kimi/Moonshot、Zhipu、通用 Bearer token 等）
        body = re.sub(r"sk-[a-zA-Z0-9_\-]{20,}", "***API_KEY_REDACTED***", body)
        body = re.sub(r"\b([a-zA-Z0-9_-]{2,})-[a-zA-Z0-9_-]{32,}\b", r"\1-***API_KEY_REDACTED***", body)
        return body

    async def _chat_inner(self, messages: list, model: str, temperature: float = 0.3,
                          max_tokens: int = 16384, response_format: dict = None, scope: str = "") -> str:
        payload = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if response_format:
            payload["response_format"] = response_format
        url, key = self.ds_url, self.ds_key
        custom_used = False
        # Check if custom API should be used for this scope (new custom_apis first, then legacy)
        if scope:
            custom = self.custom_scope_map.get(scope) or self._get_custom_openai(scope)
            if custom:
                url, key, payload["model"] = custom["url"], custom["key"], custom["model"]
                custom_used = True
        try:
            return await self._call(url, key, payload)
        except Exception as exc:
            if not custom_used:
                raise
            logger.warning("Custom API scope %s failed; falling back to DeepSeek: %s", scope, exc)
            fallback_payload = dict(payload)
            fallback_payload["model"] = model
            return await self._call(self.ds_url, self.ds_key, fallback_payload)

    async def _chat_with_tools(self, messages: list, model: str, temperature: float = 0.3,
                                max_tokens: int = 16384, tools: list = None,
                                tool_provider=None, scope: str = "") -> str:
        """支持 Function Calling 的聊天封装。

        tool_provider: 可调用对象，接收 (tool_name, arguments) -> dict 结果。
        当模型返回 tool_calls 时循环调用工具并把结果追加到上下文，最多 5 轮。
        """
        if tools is None or tool_provider is None:
            return await self._chat_inner(messages, model, temperature, max_tokens, scope=scope)

        MAX_TOOL_ROUNDS = 5
        payload_base = {"model": model, "temperature": temperature, "max_tokens": max_tokens}
        if tools:
            payload_base["tools"] = tools
            payload_base["tool_choice"] = "auto"

        url, key = self.ds_url, self.ds_key
        custom_used = False
        if scope:
            custom = self.custom_scope_map.get(scope) or self._get_custom_openai(scope)
            if custom:
                url, key, model = custom["url"], custom["key"], custom["model"]
                payload_base["model"] = model
                custom_used = True

        current_messages = list(messages)
        last_nonempty_content = ""
        for round_idx in range(MAX_TOOL_ROUNDS):
            payload = dict(payload_base)
            payload["messages"] = current_messages
            try:
                msg = await self._call_raw(url, key, payload)
            except Exception as e:
                if not custom_used:
                    logger.warning("_chat_with_tools API call failed: %s", e)
                    raise
                logger.warning("Custom tool API scope %s failed; falling back to DeepSeek: %s", scope, e)
                custom_used = False
                url, key, model = self.ds_url, self.ds_key, self.ds_model
                payload_base["model"] = model
                payload["model"] = model
                msg = await self._call_raw(url, key, payload)

            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return content
            if content.strip():
                last_nonempty_content = content

            current_messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                tc_id = tc.get("id", "")
                fn = tc.get("function", {})
                fn_name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    args = {}
                try:
                    result = tool_provider(fn_name, args)
                    tool_content = json.dumps(result, ensure_ascii=False)
                except Exception as e:
                    logger.warning("Tool %s execution failed: %s", fn_name, e)
                    tool_content = json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": fn_name,
                    "content": tool_content,
                })

        # 工具轮数耗尽：优先返回最后一次非空 content；全程为空则显式报错，
        # 避免上层把空串当 AI 输出解析而触发误导性的全量重试。
        if last_nonempty_content.strip():
            logger.warning("Tool rounds exhausted; returning last non-empty content")
            return last_nonempty_content
        raise Exception(f"Tool rounds exhausted without final answer after {MAX_TOOL_ROUNDS} rounds")

    def _get_custom_openai(self, scope: str) -> dict | None:
        """检查是否应为给定scope使用自定义OpenAI API（兼容旧单API配置）"""
        s = load_settings()
        key = s.get("custom_openai_key", "").strip()
        url = s.get("custom_openai_url", "").strip()
        model = s.get("custom_openai_model", "").strip()
        scopes = s.get("custom_openai_scopes", [])
        if key and url and model and scope in scopes:
            # Ensure URL ends with /chat/completions
            if not url.endswith('/chat/completions'):
                url = url.rstrip('/') + '/chat/completions'
            return {"key": key, "url": url, "model": model}
        # Also check new custom_apis in scope_map (already built in _reload)
        return self.custom_scope_map.get(scope)

    async def _chat_json(self, messages: list, model: str = "deepseek-v4-flash",
                         temperature: float = 0.3, max_tokens: int = 16384,
                         force_json: bool = True, scope: str = "",
                         enable_calc: bool = False, calc_session: str = "") -> dict:
        if ENABLE_CALCULATOR and enable_calc:
            return await self.deepseek_json(messages, temperature, max_tokens, scope,
                                            enable_calc=True, calc_session=calc_session)
        fmt = {"type": "json_object"} if force_json else None
        content = await self._chat_inner(messages, model, temperature, max_tokens, fmt, scope=scope)
        parsed = self._extract_json(content)
        # 模型可能输出合法 JSON 数组/字符串（FreqErr [AI 合法 JSON 非对象]）：
        # 出口统一断言为 dict，调用点的 .get 才安全；异常由调用方按 502 映射
        if not isinstance(parsed, dict):
            raise ValueError(f"AI 返回的 JSON 顶层不是对象: {type(parsed).__name__}")
        return parsed

    @staticmethod
    def _extract_json(text: str) -> dict:
        if not text or not text.strip():
            raise Exception("AI returned empty response - generation truncated")
        import re
        text = text.strip()
        # Detect truncation
        if text.endswith('...') or text.endswith('…'):
            logger.warning("_extract_json: response appears truncated (ends with ellipsis), %d chars", len(text))
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # Strip markdown code blocks
        m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # Find outermost balanced { } ignoring strings
        def _find_balanced_json(t: str) -> str:
            i = t.find('{')
            if i < 0:
                return ''
            in_str = False
            esc = False
            depth = 0
            start = i
            for j in range(i, len(t)):
                ch = t[j]
                if esc:
                    esc = False
                    continue
                if ch == '\\' and in_str:
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return t[start:j+1]
            return ''
        balanced = _find_balanced_json(text)
        if balanced:
            # Try without newline fix first
            for variant in [balanced, _repair_truncated_json(balanced)]:
                fixed = _fix_json_newlines(variant)
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError:
                    pass
        # Last resort: find last complete JSON by known top-level keys
        def _try_key(key: str):
            s2 = text.rfind('{"' + key + '"')
            if s2 < 0:
                return None
            in_str = False
            esc = False
            depth = 0
            for j in range(s2, len(text)):
                ch = text[j]
                if esc:
                    esc = False; continue
                if ch == '\\' and in_str:
                    esc = True; continue
                if ch == '"':
                    in_str = not in_str; continue
                if in_str: continue
                if ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        block2 = _fix_json_newlines(text[s2:j+1])
                        try:
                            return json.loads(block2)
                        except json.JSONDecodeError:
                            pass
                        break
            return None
        for key in ("question_html", "structure_graph", "answer_html"):
            parsed = _try_key(key)
            if parsed is not None:
                return parsed
        log_error("ai_service", f"JSON parse failed after all recovery attempts. Response length={len(text)}")
        raise Exception(f"JSON parse failed. Response length={len(text)}")

    # ===================== DeepSeek: 推理解题 =====================

    async def deepseek_chat(self, messages: list, temperature: float = 0.3,
                            max_tokens: int = 32768, scope: str = "",
                            enable_calc: bool = False, calc_session: str = "",
                            step_callback=None) -> str:
        if step_callback:
            step_callback("对话Agent", "调用推理模型", "running")
        # 未显式传入 calc_session 时使用一次性会话并在结束后清理，
        # 防止 prev_result 等会话状态跨请求/跨对话串数据。
        owned_calc_session = ""
        if ENABLE_CALCULATOR and enable_calc and not calc_session:
            import uuid as _uuid
            owned_calc_session = f"chat_{_uuid.uuid4().hex}"
            calc_session = owned_calc_session
        try:
            if ENABLE_CALCULATOR and enable_calc:
                from services import calc_service
                tools = [calc_service.CALCULATOR_TOOL_SCHEMA]
                def provider(name, args):
                    if name == "calculator":
                        return calc_service.calculator_tool_call(args, calc_session)
                    return {"success": False, "error": f"未知工具 {name}"}
                result = await self._chat_with_tools(messages, self.ds_model, temperature, max_tokens,
                                                   tools=tools, tool_provider=provider, scope=scope)
            else:
                result = await self._chat_inner(messages, self.ds_model, temperature, max_tokens, scope=scope)
            if step_callback:
                step_callback("对话Agent", "调用推理模型", "done")
            return result
        except Exception as e:
            if step_callback:
                step_callback("对话Agent", "调用推理模型", "error")
            raise
        finally:
            if owned_calc_session:
                from services import calc_service as _calc
                _calc.clear_session(owned_calc_session)

    async def deepseek_json(self, messages: list, temperature: float = 0.3,
                            max_tokens: int = 16384, scope: str = "",
                            enable_calc: bool = False, calc_session: str = "") -> dict:
        fmt = {"type": "json_object"}
        owned_calc_session = ""
        try:
            if ENABLE_CALCULATOR and enable_calc:
                from services import calc_service
                if not calc_session:
                    import uuid as _uuid
                    owned_calc_session = f"chat_{_uuid.uuid4().hex}"
                    calc_session = owned_calc_session
                tools = [calc_service.CALCULATOR_TOOL_SCHEMA]
                def provider(name, args):
                    if name == "calculator":
                        return calc_service.calculator_tool_call(args, calc_session)
                    return {"success": False, "error": f"未知工具 {name}"}
                content = await self._chat_with_tools(messages, self.ds_model, temperature, max_tokens,
                                                      tools=tools, tool_provider=provider, scope=scope)
            else:
                content = await self._chat_inner(messages, self.ds_model, temperature, max_tokens, fmt, scope=scope)
            parsed = self._extract_json(content)
            # 与 _chat_json 同款 dict 断言（FreqErr [AI 合法 JSON 非对象]）
            if not isinstance(parsed, dict):
                raise ValueError(f"AI 返回的 JSON 顶层不是对象: {type(parsed).__name__}")
            return parsed
        finally:
            if owned_calc_session:
                from services import calc_service as _calc
                _calc.clear_session(owned_calc_session)

    async def deepseek_solve(self, subject: str, grade: str, ocr_text: str,
                             knowledge_tags: list, user_hint: str = "",
                             region: str = "", avg_score: float = None,
                             style_notes: str = "", step_callback=None,
                             reference_svg: str = "",
                             reference_svg_description: str = "",
                             source_reference: str = "") -> dict:
        self._reload()
        _max_tokens = self.ds_max_tokens
        if step_callback:
            step_callback("解题Agent", "理解题意", "running")
            step_callback("解题Agent", "建立解题模型", "running")
        prompt = (
            f"你是专业的{grade}{subject}教师。请解答以下题目。\n\n"
            f"【题目原文】\n{ocr_text}\n\n"
            f"【学科】{subject}  【年级】{grade}\n"
            f"【知识点】{', '.join(knowledge_tags)}\n"
        )
        if region: prompt += f"【地区】{region}\n"
        if avg_score: prompt += f"【参考平均分】{avg_score}\n"
        if user_hint: prompt += f"【用户提示】{user_hint}\n"
        if style_notes: prompt += f"【排版偏好】{style_notes}\n"
        if reference_svg:
            prompt += (
                "【OCR视觉模型复刻的原题参考SVG】\n"
                f"{reference_svg[:16000]}\n\n"
                "这份 SVG 是另一视觉模型对原图的复刻证据，权重较高但并非绝对正确。必须把它与题目文字"
                "交叉核对；若点名、数量、方向、拓扑或数据冲突，不得擅自选择一方或重画成可解题，必须在"
                " question_challenge 中报告。没有冲突时才保持其中关系，不得重新想象另一种图。\n"
                "题面必须用 [[DIAGRAM:0]] 放置这张原题参考图。diagram_prompts 只描述额外的解题辅助图，"
                "辅助图占位从 [[DIAGRAM:1]] 开始。\n"
            )
        elif reference_svg_description:
            prompt += (
                "【OCR图形描述】\n"
                f"{reference_svg_description[:4000]}\n"
                "参考SVG生成失败，不得凭空改变上述图形关系；如无法保证唯一题意，不要生成替代题面图。\n"
            )
        prompt += (
            "\n【核心规则 - 先思考再输出】\n"
            "你必须先忽略任何参考答案，独立完成题意检查和推理，再与参考内容比较。\n"
            "严禁在输出中反复说'仍然矛盾''还是不对'等犹豫内容！\n"
            "如果冲突来自你自己的推理，静默修正；如果冲突来自题干、OCR、图形或参考答案，禁止静默篡改题目，"
            "必须按下方规则输出 question_challenge。\n\n"
        )
        if ENABLE_CALCULATOR:
            prompt += (
                "【计算器工具 - 按需使用】\n"
                "你可以调用 calculator 工具完成精确计算。调用后工具会返回结果，你再继续推理。\n"
                "支持的运算: + - * / // % **、sqrt、sin/cos/tan/asin/acos/atan、log/ln、abs、factorial、gcd/lcm。\n"
                # 单位与底数必须写进提示词，否则模型按角度制写 sin(30) 会拿到弧度制的 -0.988，
                # 而这个错值会一路进到给学生的解题步骤里。
                "【注意】sin/cos/tan 用**弧度**：角度请写 sin(pi/6) 或 sin(radians(30))；反三角返回的也是弧度。\n"
                "【注意】log 与 ln 都是**自然对数**，常用对数请用 log10(x) 或 lg(x)。\n"
                "用 pi 表示 π，e 表示自然常数。结果会保留符号（如 sqrt(2)、pi）和最简分数。\n"
                "如果后一步需要引用上一步结果，在表达式中写 prev_result。\n"
                "提醒：如果一道题需要大量复杂计算且题目未明确允许，请反思是否方法选择不当；但使用内置计算器本身是允许且推荐的。\n\n"
            )
        prompt += (
            "【物理/化学题特殊规则】\n"
            "- 只能使用题目明确给出的常量；未给出时可采用该年级公认约定，但必须在 assumptions 和解答中说明\n"
            "- 严禁根据上传的参考答案、答案是否整齐或是否为整数反推 g、π、密度、摩尔质量等常量\n"
            "- 不同合理常量会导致不同答案时，至少标记 low；缺少常量导致无法唯一作答时标记 high\n"
            "- 物理/化学示意图要标注明确：力的方向、箭头、反应条件、箭头符号等\n\n"
            "【数学π规则】\n"
            "- 题目未要求近似值时保留 $\\pi$；题目明确给出近似值时才代入\n"
            "- 严禁根据参考答案的形式倒推题目原本没有提供的取值规则\n\n"
            "【HTML风格要求 - 严格遵守】\n"
            "禁止: 彩色文字/边框/背景、emoji、渐变、阴影、花体字、ASCII艺术图、code/pre标签\n"
            "禁止: 任何灰色/纯色色块填充区域（除非是题目图中的阴影部分，用于标注面积）\n"
            "只允许: 纯白背景、深灰色细文字、标准HTML标签(p/div/table/ul/ol)\n"
            "题号格式: 直接用阿拉伯数字1、2、3……不加圆圈不加背景\n"
            "数学符号/数字必须用$...$包裹: 如 $1+1=2$、边长 $3$、$\\triangle ABC$\n"
            "图表占位: 题面中用 [[DIAGRAM:0]] 标记第一张图位置，从0开始编号\n\n"
            "【题面标准化】\n"
            "question_html: 只做忠实的格式标准化，可补充连接词'已知/求/如图'，但绝不能补造原文没有的数据、"
            "条件、选项、单位、定义域或图形关系；缺失时保留原状并提出质疑\n"
            "题面中禁止出现答案/解析/评分标准！只能有题目内容！\n"
            "题面中绝对禁止出现 '图一' '备用图' '图1' 等标签覆盖/遮挡图形内容！\n\n"
            "【解答格式 - 严格用 <!-- SCORE_SPLIT --> 分割】\n"
            "answer_html 必须严格遵守: 得分点...<!-- SCORE_SPLIT -->...详细解题过程\n"
            "前半=得分点/关键步骤（每点一行），后半=详细解题步骤+推导+易错提示\n"
            "解答中展示完整解题思路和步骤，但最终答案单独放在 standard_answer 字段\n"
            "禁止输出空字符串！确保解答内容充实完整！\n\n"
        )
        if source_reference:
            prompt += (
                "【上传材料中的参考答案/手写过程（不可信证据）】\n"
                f"{source_reference[:8000]}\n\n"
                "这部分可能正确、错误、属于其他题目，或被 OCR 误读。必须先独立求解，再比较；不得为了匹配它"
                "而补条件、改常量、选择近似方式或反向证明。冲突时在 question_challenge 中标记"
                " reference_answer_conflict，并保留你的独立结论。\n\n"
            )
        prompt += (
            "【题目质疑分级】\n"
            "- none：没有实质疑点。\n"
            "- low：可能是 OCR 字符/单位/图形细节不清、存在常见但未明说的约定，或参考答案仅有轻微冲突；"
            "仍可在明确 assumptions 后给出条件化解答。\n"
            "- high：条件互相矛盾、关键条件缺失导致不唯一、无解、图文核心关系冲突、所有选项均不成立，或"
            "独立推导明确否定参考答案。此时不得伪造条件把题目修成可解，标准答案应明确写出无法唯一确定/题目有误。\n"
            "confidence 为 0~1；reasons 写可核对的具体矛盾，evidence 列出题面中的条件或独立计算结果；"
            "issue_types 只能使用 contradictory_conditions/missing_condition/non_unique/no_solution/"
            "diagram_text_conflict/reference_answer_conflict/ocr_ambiguity/option_mismatch/"
            "unit_or_domain_conflict/out_of_scope/other。\n\n"
        )
        if reference_svg:
            prompt += (
                "【示意图规则 - 已有原题参考图】\n"
                "- 原题图已经由 [[DIAGRAM:0]] 提供，严禁在 diagram_prompts 中重画、改画或替换原题图。\n"
                "- diagram_prompts 仅在解答确实需要辅助线、分类讨论图或受力分析图时填写；这些图的 place 必须为 answer。\n"
                "- 辅助图必须继承参考SVG中的点名和几何/装置关系，不需要辅助图时两个数组均留空。\n\n"
            )
        elif reference_svg_description:
            prompt += (
                "【示意图规则 - 参考图降级】\n"
                "- OCR只可靠获得了图形描述，不得用 diagram_prompts 猜测或替换原题图。\n"
                "- 仅允许生成 place=answer 的解题辅助图；无法保证关系一致时留空数组。\n\n"
            )
        else:
            prompt += (
                "【示意图规则 - 每题必做】\n"
                "涉及几何/三角/函数图/统计图/化学实验装置/物理示意图时必须在 diagram_prompts 中生成描述：\n"
                "- 所有点必须标注字母（A、B、C、O 等），辅助线用虚线\n"
                "- 直角处标注直角符号 ┐，平行线标注平行符号\n"
                "- 避免\"图一\"\"备用图\"等标签遮挡图形内容，标签放在图形外或右上角小字\n"
                "- 描述要详细: 坐标、边长、角度、标注文字都要写清楚\n"
                "- 题面1张（question）+ 解答1张（answer 辅助线），无图则留空数组\n"
                "- 物理/化学图：标注力的方向(箭头↗)、反应条件(△加热/催化剂)、化学键等\n"
                "- 化学实验装置：描述各部件位置（试管、酒精灯、铁架台、导管、集气瓶等）\n"
                "- 物理力学图：标注物体(A/B)、力符号(F/N/f)、角度(θ)、斜面、滑轮\n\n"
            )
        if ENABLE_STRUCTURE_GRAPH:
            prompt += (
                "【结构梳理图 - 解题逻辑思维导图】\n"
                "在 structure_graph 中输出解题的逻辑推理链，用有向图表示「因为...所以...」的因果关系：\n"
                "- nodes: 每个节点代表一个推理步骤/条件/结论，含 id(从0开始整数)、level(层级: 0=已知条件/题设, 1=第一步推理, 递增)、text(节点文字，支持LaTeX $...$)、color(可选，留空即可)、type(节点语义: condition=已知条件黄色/key=关键等式或重要中间结论蓝色/conclusion=最终结论深黄色，必须填写)\n"
                "- edges: 有向边 [{from, to}]，from→to 表示「from 是 to 的前提/原因」\n"
                "- 边从低 level 指向高 level，优先相邻 level；当存在直接推理关系时允许跨层边（如 level1→level4），以准确表达逻辑\n"
                "- 节点数控制在 5~15 个，精炼表达核心推理链，不要过于琐碎\n"
                "- 示例: 证明正方形 → 节点0:矩形ABCD(level0) → 节点1:∠BAD=B=90°(level1) → 节点2:四边形ABEB'内角和360°(level1) → 节点3:四个角都是90°(level2) → 节点4:矩形ABEB'(level2) → 节点5:正方形ABEB'(level3)\n"
                "- 如果题目过于简单(如纯计算填空)，structure_graph 可留空对象 {} 或 nodes 为空数组 []\n\n"
                f"输出JSON:\n"
                '{"question_html":"标准化题面HTML","standard_answer":"最终答案或无法确定说明","answer_html":"得分点...<!-- SCORE_SPLIT -->...详细解题步骤","score_points_html":"评分标准（针对解答题/计算题的给分点，每点一行）","question_type":"填空","diagram_prompts":["几何描述..."],"diagram_places":["question","answer"],"assumptions":["采用的必要假设"],"question_challenge":{"level":"none/low/high","confidence":0.0,"issue_types":[],"reasons":[],"evidence":[],"recommended_action":""},"structure_graph":{"nodes":[{"id":0,"level":0,"text":"矩形ABCD","color":"","type":"condition"}],"edges":[{"from":0,"to":1}]}}\n'
            )
        else:
            prompt += (
                f"输出JSON:\n"
                '{"question_html":"标准化题面HTML","standard_answer":"最终答案或无法确定说明","answer_html":"得分点...<!-- SCORE_SPLIT -->...详细解题步骤","score_points_html":"评分标准（针对解答题/计算题的给分点，每点一行）","question_type":"填空","diagram_prompts":["几何描述..."],"diagram_places":["question","answer"],"assumptions":["采用的必要假设"],"question_challenge":{"level":"none/low/high","confidence":0.0,"issue_types":[],"reasons":[],"evidence":[],"recommended_action":""}}\n'
            )
        prompt += (
            "【score_points_html 规则】\n"
            "- 选择题/填空题: score_points_html 留空\n"
            "- 解答题/计算题/证明题: 每行一个得分点, 如 '正确设未知数 1分' '列出方程 2分' '计算正确 2分'\n"
            "- 得分点与答案无关，是批改给分标准\n\n"
            "【严禁规则】\n"
             "1. standard_answer 必须填写独立推导的最终答案；high 级问题题填写‘题目条件不足/矛盾，无法唯一确定’，不得迎合参考答案！\n"
             "2. answer_html 禁止为空或填充无意义字符！必须包含完整解题推导！\n"
             "3. 推理中禁止输出犹豫/矛盾/错误尝试，只输出最终正确推理过程！\n"
             "4. question_type必填(填空/解答/选择/证明)\n"
             "5. 证明题不要写横线/空白线！正常段落文字留空即可，不要用'____'或'----'填充\n"
             "6. 禁止在题面中使用Δ、∠等直接Unicode字符！必须用LaTeX：$\\triangle ABC$、$\\angle A$\n"
             "7. 禁止在题面中使用×、÷等符号！用$\\times$、$\\div$\n"
             "8. 题面中绝对禁止输出 '图一' '备用图' '图1' 等标签覆盖图形内容"
        )
        # Large response: no json_object mode to avoid truncation
        import uuid
        from services import calc_service
        calc_session = str(uuid.uuid4())
        try:
            if step_callback:
                step_callback("解题Agent", "理解题意", "done")
                step_callback("解题Agent", "建立解题模型", "done")
                step_callback("解题Agent", "推理解答", "running")
            result = await self._chat_json([{"role":"user","content":prompt}], max_tokens=_max_tokens, force_json=False,
                                           scope="solve", enable_calc=ENABLE_CALCULATOR, calc_session=calc_session)
            if step_callback:
                step_callback("解题Agent", "推理解答", "done")
                step_callback("解题Agent", "验证结果", "running")
            # 基础结构校验
            if isinstance(result, dict) and result.get("standard_answer"):
                if step_callback:
                    step_callback("解题Agent", "验证结果", "done")
            else:
                if step_callback:
                    step_callback("解题Agent", "验证结果", "error")
            if not isinstance(result, dict):
                logger.warning("deepseek_solve returned non-object: %r", result)
                raise ValueError("解题模型返回了非对象 JSON，按失败重试")
            return result
        except Exception as _se:
            if step_callback:
                step_callback("解题Agent", "推理解答", "error")
                step_callback("解题Agent", "验证结果", "error")
            _se_msg = str(_se).lower()
            if "json" in _se_msg or "parse" in _se_msg or "no json" in _se_msg or "empty" in _se_msg:
                logger.warning("deepseek_solve force_json=False failed, retrying with json_object mode: %s", str(_se)[:200])
                return await self._chat_json([{"role":"user","content":prompt}], max_tokens=_max_tokens, force_json=True,
                                             scope="solve", enable_calc=ENABLE_CALCULATOR, calc_session=calc_session)
            raise
        finally:
            try:
                calc_service.clear_session(calc_session)
            except Exception:
                pass

    async def deepseek_self_review(self, question_html: str, answer_html: str,
                                   standard_answer: str, subject: str, grade: str,
                                   reference_svg: str = "", ocr_text: str = "",
                                   source_reference: str = "",
                                   current_challenge: dict = None) -> dict:
        """Comprehensive AI review covering question, answer, standard answer, diagrams, layout."""
        prompt = (
            f"你是{grade}{subject}的审题专家。请全面检查以下题目解答，逐项核对：\n"
            "先独立检查题目是否成立，不得把上传的参考答案当作正确前提，也不得为了得到它而修改题意。"
            "OCR 文本和参考 SVG 来自不同模型，二者都可能出错；冲突必须保留为质疑。\n\n"
            "【题面审查】\n"
            "1. 条件是否清晰完整，表述是否规范\n"
            "2. 题面中不允许出现答案、解析、评分标准\n"
            "3. 如有物理/化学题，检查常量(g=10m/s²/π等)取值是否明确标注\n"
            "4. 数学公式是否正确使用LaTeX $...$ 或 $$...$$ 包裹\n"
            "5. LaTeX中Δ、π等字符用 \\\\Delta、\\\\pi 而非直接Unicode字符\n"
            "6. 禁止题面中出现 '图一' '备用图' '图1' 等标签遮挡图形内容\n\n"
            "【解答审查】\n"
            "7. 解答步骤完整、逻辑清晰，得分点准确\n"
            "8. 物理题检查数据代入是否正确，常量取值是否与现实一致\n"
            "9. 证明题不要写横线(-----或____)和连续空白线，正常段落文字留空即可\n"
            "10. HTML格式正确，使用标准标签(p/div/table/ul/ol)，禁用彩/灰/渐变/阴影样式\n"
            "11. **解答是否足够清晰易懂（学生视角）**——步骤间是否有过渡说明、关键推理是否点明原因、有没有跳跃式省略\n"
            "12. **如果解答不够清晰**——补充步骤间说明、标注推理原因、拆分过长推导为多步\n\n"
            "【标准答案审查】\n"
            "13. 标准答案简洁精确，仅有最终答案不含推导\n\n"
            "【示意图审查】\n"
            "14. 题面中禁止出现 '图一' '备用图' 等标签遮挡图形内容\n"
            "15. 所有点标注字母(A/B/C/O)，直角标注符号┐，辅助线用虚线\n"
            "16. 物理/化学图标注须标准化，力的方向标注箭头，反应条件标注△/催化剂等\n"
            "17. 图形标签必须使用 $\\triangle ABC$、$\\angle A$（禁止直接使用Δ∠等Unicode字符）\n\n"
            "18. 原题未明确给出计算常量时，不得补写或反推常量；只能在 assumptions 和 question_challenge 中说明条件化假设\n"
            "19. π、重力加速度等取值必须以原题事实为准，不得根据参考答案形式倒推\n\n"
            + ((
                "【原题参考SVG】\n" + reference_svg[:12000] + "\n"
                "检查修改不得改变该SVG表达的原题几何或装置关系，并保留题面 [[DIAGRAM:0]] 占位。\n\n"
            ) if reference_svg else "") +
            ((f"【OCR 原文】\n{ocr_text[:10000]}\n\n") if ocr_text else "") +
            ((f"【不可信的上传参考答案/手写过程】\n{source_reference[:6000]}\n"
              "只能在独立推导后比较，不得用于反推题目条件或常量。\n\n") if source_reference else "") +
            (("【解题阶段已提出的质疑】\n" + json.dumps(current_challenge, ensure_ascii=False) +
              "\n除非你能给出直接反证，否则不得通过改写题目来清除该质疑。\n\n") if current_challenge else "") +
            "如有问题直接修改后输出。\n\n"
            f"【当前题面】\n{question_html}\n\n"
            f"【当前解答】\n{answer_html}\n\n"
            f"【标准答案】{standard_answer}\n\n"
            "输出JSON:\n"
            '{"question_html":"修正后题面","standard_answer":"修正后标答或无法确定说明","answer_html":"修正后详细解答","score_points_html":"与本版解答一致的得分点","question_challenge":{"level":"none/low/high","confidence":0.0,"issue_types":[],"reasons":[],"evidence":[],"recommended_action":""}}'
        )
        try:
            return await self._chat_json([{"role":"user","content":prompt}], max_tokens=32768, force_json=False, scope="solve")
        except Exception as _sre:
            _sre_msg = str(_sre).lower()
            if "json" in _sre_msg or "parse" in _sre_msg or "no json" in _sre_msg:
                logger.warning("deepseek_self_review force_json=False failed, retrying with json_object: %s", str(_sre)[:200])
                return await self._chat_json([{"role":"user","content":prompt}], max_tokens=32768, force_json=True, scope="solve")
            raise

    async def deepseek_verify(self, ocr_text: str, question_html: str,
                              answer_html: str, standard_answer: str,
                              subject: str, grade: str,
                              reference_svg: str = "",
                              source_reference: str = "") -> dict:
        """Second DeepSeek independently verifies and replaces if answer is wrong/unclear."""
        prompt = (
            f"你是{grade}{subject}的独立审题专家。请独立推理以下题目，并判断给出的解答是否正确。\n\n"
            f"【题目原文】\n{ocr_text}\n\n"
            f"【标准化题面】\n{question_html}\n\n"
            f"【给出的解答】\n{answer_html}\n\n"
            f"【给出的标准答案】{standard_answer}\n\n"
            + ((
                "【原题参考SVG】\n" + reference_svg[:12000] + "\n"
                "该 SVG 是视觉模型复刻的高权重证据，但仍可能识别错误。请与 OCR 文字交叉核对；"
                "冲突时提出质疑，不得擅自把其中任一方改成可解题。无冲突时应保留题面 [[DIAGRAM:0]]。\n\n"
            ) if reference_svg else "") +
            ((f"【上传材料中的参考答案/手写过程（不保证正确）】\n{source_reference[:6000]}\n\n")
             if source_reference else "") +
            "【判断规则】\n"
            "必须先只依据题干独立完成推理，再对比 AI 解答和上传参考内容：\n"
            "1. 如果给出的解答完全正确、步骤清晰 → 返回 verdict: 'approve'\n"
            "2. 如果解答有错误、不清晰、逻辑混乱 → 用你的正确解答替换并返回 verdict: 'replace'\n"
            "3. 如果standard_answer错误 → 修正后返回\n"
            "4. 检查题面/解答中是否有 '图一' '备用图' 等标签遮挡图形，有则移除\n"
            "5. 检查是否有Unicode字符Δ∠等未被LaTeX包裹，替换为$\\triangle$/$\\angle$\n"
            "6. 检查证明题是否有连续横线-----或____，替换为正常留白\n"
            "7. 如果题目条件矛盾、缺失而不唯一、图文冲突、无选项成立，或上传参考答案与独立推导明确冲突，"
            "返回 verdict: 'challenge'，不得补造条件让参考答案成立\n"
            "8. low 表示 OCR/约定存在小概率歧义但可带假设作答；high 表示题目大概率有问题，应暂停自动使用\n\n"
            "输出JSON:\n"
            '{"verdict":"approve/replace/challenge","correct_question_html":"修正后题面(仅replace时有意义)","correct_answer_html":"正确详细解答","correct_score_points_html":"与修正版解答一致的得分点","correct_standard_answer":"正确标准答案或无法确定说明","reason":"判断理由","question_challenge":{"level":"none/low/high","confidence":0.0,"issue_types":[],"reasons":[],"evidence":[],"recommended_action":""}}\n'
            "verdict=replace 时 correct_answer_html 与 correct_score_points_html 必须同时提供。"
        )
        try:
            return await self._chat_json([{"role":"user","content":prompt}], max_tokens=32768, force_json=False, scope="solve")
        except Exception as _ve:
            _ve_msg = str(_ve).lower()
            if "json" in _ve_msg or "parse" in _ve_msg:
                logger.error("deepseek_verify JSON parse FAILED, content may be wrong: %s", str(_ve)[:200])
                raise Exception(f"deepseek_verify failed: {str(_ve)[:150]}")
            raise

    async def deepseek_challenge_question(
        self, ocr_text: str, question_html: str, answer_html: str,
        standard_answer: str, subject: str, grade: str,
        reference_svg: str = "", source_reference: str = "",
        user_context: str = "",
    ) -> dict:
        """Run an explicit independent challenge without rewriting the question."""
        prompt = (
            f"你是{grade}{subject}的独立审题员。你的任务不是证明现有答案，而是主动寻找题目是否有问题。\n"
            "证据之间相互独立：OCR 文字、标准化题面、视觉模型 SVG、上传参考答案、现有 AI 解答都可能出错。"
            "先仅依据题干独立推理，再逐项交叉验证；禁止为了匹配答案而补条件、改常量、选近似值或篡改图形。\n\n"
            f"【OCR 原文】\n{ocr_text[:12000]}\n\n"
            f"【标准化题面】\n{question_html[:12000]}\n\n"
            + ((f"【视觉模型参考 SVG】\n{reference_svg[:12000]}\n\n") if reference_svg else "")
            + ((f"【上传参考答案/手写过程（不可信）】\n{source_reference[:8000]}\n\n") if source_reference else "")
            + ((f"【用户补充（不一定正确）】\n{user_context[:3000]}\n\n") if user_context else "")
            + f"【当前 AI 解答】\n{answer_html[:10000]}\n\n"
            f"【当前标准答案】\n{standard_answer[:3000]}\n\n"
            "分级：none=未发现实质疑点；low=OCR/约定/图形细节可能有误但可列出假设后作答；"
            "high=矛盾、缺关键条件导致不唯一、无解、图文核心冲突、选项全错或参考答案被独立推导明确否定。\n"
            "只报告能指出具体证据的疑点，不因自己暂时不会求解而质疑。输出 JSON：\n"
            '{"level":"none/low/high","confidence":0.0,"issue_types":[],"reasons":[],"evidence":[],"recommended_action":""}'
        )
        return await self.deepseek_json(
            [{"role": "user", "content": prompt}],
            max_tokens=8192,
            enable_calc=ENABLE_CALCULATOR,
            scope="question_challenge",
        )

    async def deepseek_generate_structure_graph(self, ocr_text: str, question_html: str,
                                                 answer_html: str, standard_answer: str,
                                                 subject: str, grade: str) -> dict:
        """专门生成/重写结构梳理图的 AI 调用。"""
        prompt = (
            f"你是{grade}{subject}教师。请根据以下题目和解答，生成一个结构梳理图（解题逻辑思维导图）。\n\n"
            f"【题目原文】\n{ocr_text}\n\n"
            f"【标准化题面】\n{question_html}\n\n"
            f"【解答】\n{answer_html}\n\n"
            f"【标准答案】{standard_answer}\n\n"
            "【结构梳理图要求】\n"
            "用有向图表示解题的逻辑推理链（因果关系：因为...所以...），类似数学证明思维导图：\n"
            "- nodes: [{id, level, text, color, type}]，id 从 0 开始整数，level=0 为已知条件/题设，递增表示推理深度，text 支持 LaTeX $...$ 且不超过 100 字，color 可选（默认空），type 为以下之一：\n"
            "  - condition：已知条件/题设（黄色背景）\n"
            "  - key：关键等式、关键性质或由多个条件汇聚得到的重要中间结论（蓝色背景）\n"
            "  - conclusion：最终结论（深黄色/橙色背景）\n"
            "- edges: [{from, to}]，from→to 表示「from 是 to 的前提/原因」\n"
            "- 边从低 level 指向高 level，优先相邻 level；必要时允许跨层边（如 level1→level4）表达直接推理关系\n"
            "- 节点数 5~15 个，精炼表达核心推理链\n"
            "- 简单题可留空数组 []\n\n"
            "输出JSON:\n"
            '{"structure_graph":{"nodes":[{"id":0,"level":0,"text":"","color":"","type":"condition"}],"edges":[{"from":0,"to":1}]}}'
        )
        try:
            return await self._chat_json([{"role":"user","content":prompt}], max_tokens=16384, force_json=False, scope="solve")
        except Exception as _e:
            _e_msg = str(_e).lower()
            if "json" in _e_msg or "parse" in _e_msg or "no json" in _e_msg or "empty" in _e_msg:
                logger.warning("deepseek_generate_structure_graph force_json=False failed, retrying: %s", str(_e)[:200])
                return await self._chat_json([{"role":"user","content":prompt}], max_tokens=16384, force_json=True, scope="solve")
            raise

    async def deepseek_check_structure_graph(self, structure_graph: dict, ocr_text: str,
                                              question_html: str, answer_html: str,
                                              standard_answer: str, subject: str, grade: str) -> dict:
        """检查结构梳理图是否合理、完整。"""
        prompt = (
            f"你是{grade}{subject}审题专家。请检查以下结构梳理图是否合理、完整地反映了题目的解题逻辑链。\n\n"
            f"【题目原文】\n{ocr_text}\n\n"
            f"【标准化题面】\n{question_html}\n\n"
            f"【解答】\n{answer_html}\n\n"
            f"【标准答案】{standard_answer}\n\n"
            f"【待检查的结构梳理图】\n{json.dumps(structure_graph, ensure_ascii=False)}\n\n"
            "【检查维度】\n"
            "1. 节点是否覆盖了从已知条件到最终结论的关键步骤\n"
            "2. 边的方向是否正确地表示了因果/推理关系（from 是 to 的前提）\n"
            "3. level 是否正确（0=已知条件，递增=推理深度），边是否从低 level 指向高 level（允许跨层边表达直接推理关系）\n"
            "4. 是否有遗漏的关键推理步骤\n"
            "5. 是否有错误的因果关系\n\n"
            "输出JSON:\n"
            '{"valid":true/false,"score":7,"issues":["问题1","问题2"],"suggestions":["建议1","建议2"],"fixed_structure_graph":{"nodes":[],"edges":[]}}'
            "其中 score 为 1~10 的整数（10 表示最合理）。如果 valid=false，fixed_structure_graph 中给出修正后的结构图；如果 valid=true，fixed_structure_graph 可为空。"
        )
        try:
            return await self._chat_json([{"role":"user","content":prompt}], max_tokens=16384, force_json=False, scope="solve")
        except Exception as _e:
            _e_msg = str(_e).lower()
            if "json" in _e_msg or "parse" in _e_msg or "no json" in _e_msg or "empty" in _e_msg:
                return await self._chat_json([{"role":"user","content":prompt}], max_tokens=16384, force_json=True, scope="solve")
            raise

    async def deepseek_chat_question(self, question_json: str, message: str,
                                     style_notes: str = "",
                                     enable_calc: bool = None,
                                     step_callback=None) -> str:
        if enable_calc is None:
            enable_calc = ENABLE_CALCULATOR
        if step_callback:
            step_callback("题目讨论Agent", "分析题目", "running")
        system = (
            "你是学习搭子AI助手。帮用户修改题目、生成类题、调整难度、解释概念。直接自然回答。\n"
            "【输出格式】使用Markdown：## 标题 / **粗体** / 有序列表1. 2. 3. / 数学用$...$或$$...$$包裹。\n"
            "不得使用emoji、不得使用彩色文字、不要用HTML标签（除<p>外均用Markdown）。\n"
            "如果需要多步完成，输出JSON格式: {\"reply\":\"当前回复\",\"done\":true/false}。\n"
            "done为true表示任务完成，false表示需要继续。"
        )
        if style_notes: system += f"\n排版偏好：{style_notes}"
        try:
            result = await self.deepseek_chat([
                {"role":"system","content":system},
                {"role":"user","content":f"当前题目：\n{question_json}\n\n用户：{message}"}
            ], max_tokens=32768, scope="chat", enable_calc=enable_calc, step_callback=step_callback)
            if step_callback:
                step_callback("题目讨论Agent", "分析题目", "done")
            return result
        except Exception as e:
            if step_callback:
                step_callback("题目讨论Agent", "分析题目", "error")
            raise

    async def agent_multi_turn(self, messages: list, max_rounds: int = 5, **kwargs) -> tuple[str, bool]:
        """
        AI多轮调用。AI每次输出JSON: {"reply":"回复","done":true/false}
        done=false时继续下一轮（自动追加assistant消息到上下文），done=true时结束。
        返回 (最终回复, 是否完成)
        """
        current_messages = list(messages)
        final_reply = ""
        for round_num in range(max_rounds):
            try:
                raw = await self.deepseek_chat(current_messages, **kwargs)
            except Exception as e:
                logger.warning("agent_multi_turn round %d failed: %s", round_num, str(e)[:200])
                if final_reply:
                    return final_reply, True
                return "AI 服务暂时不可用，请稍后重试", True
            try:
                parsed = self._extract_json(raw)
                reply = parsed.get("reply", raw)
                done = parsed.get("done", True)
                final_reply = reply
                if done:
                    return reply, True
                # Not done: append assistant response to context, continue loop
                current_messages.append({"role": "assistant", "content": raw})
            except Exception:
                # JSON parse failed, treat as final reply
                return raw, True
        return final_reply or "", True

    async def deepseek_regenerate_question(self, ocr_text: str, subject: str, grade: str,
                                           instruction: str, style_notes: str = "",
                                           answer_html: str = "", question_html: str = "",
                                           standard_answer: str = "") -> dict:
        prompt = (
            f"原始题目OCR：{ocr_text}\n学科：{subject}  年级：{grade}\n指令：{instruction}\n"
        )
        if question_html: prompt += f"【当前题面】\n{question_html}\n"
        if answer_html: prompt += f"【当前解答】\n{answer_html}\n"
        if standard_answer: prompt += f"【当前标准答案】\n{standard_answer}\n"
        if style_notes: prompt += f"排版偏好：{style_notes}\n"
        prompt += (
            "请根据【当前题面】和【当前解答】，按照指令改写。"
            "如果涉及几何图形，必须在 diagram_prompts 中生成详细的中文几何描述。\n\n"
            "【SVG画图规则】\n"
            "- 如果需要分类讨论，请在图中选择最简单的、学生最容易想到的版本\n"
            "- 描述清楚讨论的区间，防止画图与其他情况一致\n"
            "- 如果解答需要辅助线，请描述清楚\n"
            "- 允许使用虚线表示辅助线\n\n"
            "输出JSON:\n"
            '{"question_html":"标准化题面HTML","standard_answer":"标准答案（填空题答案、选择题选项等，多个答案用分号分隔）",'
            '"answer_html":"得分点...<!-- SCORE_SPLIT -->...详细过程",'
            '"question_type":"填空/解答/选择/证明",'
            '"diagram_prompts":["如有几何图形用中文详细描述"],"diagram_places":["question"]'
        )
        if ENABLE_STRUCTURE_GRAPH:
            prompt += (
                ',"structure_graph":{"nodes":[{"id":0,"level":0,"text":"","color":"","type":"condition"}],"edges":[{"from":0,"to":1}]}}\n\n'
                "【结构梳理图】\n"
                "在 structure_graph 中输出解题的逻辑推理链（有向图，表示因果关系）：\n"
                "- nodes: [{id, level, text, color, type}]，level=0为已知条件，递增表示推理深度，text支持LaTeX，type必填：condition=条件黄色/key=关键蓝色/conclusion=最终结论深黄色\n"
                "- edges: [{from, to}]，from→to 表示前提→结论，从低 level 指向高 level，必要时允许跨层边\n"
                "- 节点数5~15个，精炼表达核心推理链\n"
                "- 简单题可留空对象 {} 或 nodes 为空数组 []\n"
            )
        else:
            prompt += '}\n\n'
        return await self.deepseek_json([{"role":"user","content":prompt}], max_tokens=32768, scope="solve")

    # ===================== 视觉主力：小米 MiMo V2.5 全模态 =====================
    # Fact.md 定规（2026-09-06，R30 重申）：图像识别一律 MiMo 优先；
    # ZhipuAI 视觉仅作回退。调用统一走 `vision_mimo_first`，不要直调 zhipuai_vision。

    async def zhipuai_chat(self, messages: list, model: str = "glm-4v-flash",
                           temperature: float = 1, max_tokens: int = 16384) -> str:
        if not self.zp_key:
            raise Exception("ZhipuAI API key not configured")
        # glm-4v-flash 视觉模型 max_tokens 限制为 [1, 1024]
        if "glm-4v" in model.lower():
            max_tokens = max(1, min(max_tokens, 1024))
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        return await self._call(self.zp_url, self.zp_key, payload)

    async def zhipuai_vision(self, image_base64: str, prompt: str,
                             mime_type: str = "image/jpeg", parse_json: bool = True,
                             max_tokens: int = None) -> dict | str:
        """GLM视觉模型识别图片内容。parse_json=True返回解析的JSON，否则返回原始文本。

        max_tokens=None 时使用设置页的 zhipuai_max_tokens；
        glm-4v 系列模型在 zhipuai_chat 内仍有 API 层硬上限保护。
        """
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}}
        ]}]
        if max_tokens is None:
            max_tokens = self.zp_max_tokens
        for attempt in range(1, 3):
            tokens = max_tokens if attempt == 1 else min(max_tokens * 2, 131072)
            try:
                raw = await self.zhipuai_chat(messages, model=self.zp_model, temperature=1, max_tokens=tokens)
                if parse_json:
                    return self._extract_json(raw)
                return raw.strip()
            except Exception as e:
                if attempt == 1:
                    logger.warning("zhipuai_vision attempt 1 failed, retrying with more tokens: %s", str(e)[:200])
                    continue
                raise

    async def glm_correct_answer(
        self,
        question_html: str,
        standard_answer: str,
        score_points_html: str,
        answer_html: str,
        student_answer: str,
    ) -> dict:
        """使用 GLM 批改学生答案，按得分点评分。

        返回标准 JSON：
        {
          "score": float,
          "max_score": float,
          "points": [
            {"index": int, "title": str, "score": float, "max_score": float, "comment": str, "hit": bool}
          ],
          "feedback": str,
          "error_analysis": str,
          "suggestions": str
        }
        """
        prompt = (
            "你是一位严格但宽容的中学教师。请根据题库中的原题、标准答案和得分点，批改学生作答。\n\n"
            "【原题】\n" + (question_html or "") + "\n\n"
            "【标准答案】\n" + (standard_answer or "") + "\n\n"
            "【得分点】\n" + (score_points_html or "") + "\n\n"
            "【标准解答过程】\n" + (answer_html or "") + "\n\n"
            "【学生作答】\n" + (student_answer or "") + "\n\n"
            "【批改要求】\n"
            "1. 按得分点逐条判断学生作答是否命中。\n"
            "2. 学生答案与得分点语义一致即可得分，不必字面相同。\n"
            "3. 若学生做法新颖但与标准答案等价，允许给满分并说明原因。\n"
            "4. 未命中得分点但部分正确时给部分分，并说明扣分原因。\n"
            "5. 不要过度苛刻：书写潦草、语序不同、符号等价均不应成为扣分理由。\n"
            "6. 若学生完全未作答或作答与题目无关，该题得 0 分。\n\n"
            "【输出格式】只输出 JSON，不要 markdown 代码块：\n"
            "{\n"
            '  "score": 实际得分（数字）,\n'
            '  "max_score": 满分（数字，建议与得分点 max_score 之和相等）,\n'
            '  "points": [\n'
            '    {"index": 0, "title": "得分点简述", "score": 实际得分, "max_score": 满分, "comment": "命中/未命中说明", "hit": true/false}\n'
            "  ],\n"
            '  "feedback": "整体评语",\n'
            '  "error_analysis": "错因分析",\n'
            '  "suggestions": "改进建议"\n'
            "}"
        )
        try:
            raw = await self.zhipuai_chat(
                [{"role": "user", "content": prompt}],
                model=self.zp_model,
                temperature=1,
                max_tokens=self.zp_max_tokens,
            )
            parsed = self._extract_json(raw)
        except Exception as e:
            logger.warning("GLM correct answer failed, falling back to DeepSeek: %s", str(e)[:200])
            raw = await self.deepseek_json(
                [{"role": "user", "content": prompt}],
                max_tokens=16384
            )
            parsed = raw

        return normalize_correction_report(parsed)

    async def zhipuai_search(self, query: str) -> str:
        """GLM联网搜索（GLM-4-Flash支持web_search）"""
        prompt = (
            f"请联网搜索以下信息，并整理成简洁的参考资料（保留关键数据、公式、题型结构等）：\n\n"
            f"{query}\n\n"
            f"如果搜不到相关信息，请明确说明。"
        )
        messages = [{"role": "user", "content": prompt}]
        # GLM-4-Flash支持web_search能力
        try:
            payload = {"model": "glm-4-flash", "messages": messages, "temperature": 1, "max_tokens": 8192}
            return await self._call(self.zp_url, self.zp_key, payload)
        except Exception as e:
            logger.warning("zhipuai_search failed: %s, falling back to Kimi", str(e)[:200])
            return await self.kimi_search(query)

    async def glm_web_search(self, prompt: str, *, temperature: float = 0.6,
                             max_tokens: int = 2048, timeout: int = 90) -> dict:
        """调用 GLM 的**内置 web_search 工具**做联网检索。

        返回 `{"text": 模型正文, "results": [检索到的原文片段, ...]}`。

        ## 两条实测结论（2026-09-11 实测，不是推测）

        1. `web_search.enable=True` **确实会联网**。证据：同一个问题，
           不带 tools 时 `prompt_tokens=22`，带上 tools 时 `prompt_tokens=2249`
           —— 是 Zhipu 把检索到的网页正文注入了 prompt。所以
           "这接口是假的、模型只是凭记忆编" 这个怀疑**不成立**。
        2. 但**只有**再加 `search_result=True`，响应顶层才会出现 `web_search`
           字段，里面是检索到的原文片段。**没有它，调用方无法自证检索发生过**，
           而模型的措辞还会骗人 —— 实测有一次它一边说「我无法直接联网搜索信息」，
           一边在用注入的参考资料作答。只看正文根本分不出来。

        ## 因此本方法的行为约定

        - **必须**带 `search_result=True`；
        - 拿不到 `web_search` 结果就**抛异常**，而不是返回正文 ——
          宁可报"没搜成"，也不能把来源无法证明的内容当成网上查到的交给用户
          （项目「容量诚实」原则）。
        - 失败一律抛异常，不返回空串：空串会让"没搜到"与"没搜成"变成一个样子。
        """
        if not self.zp_key:
            raise RuntimeError("未配置 zhipuai_api_key，无法联网检索")
        payload = {
            "model": "glm-4-flash",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tools": [{"type": "web_search",
                       "web_search": {"enable": True, "search_result": True}}],
        }
        import httpx as _h
        async with _h.AsyncClient(timeout=_h.Timeout(timeout)) as c:
            resp = await c.post(
                self.zp_url,
                json=payload,
                headers={"Authorization": f"Bearer {self.zp_key}",
                         "Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"联网检索失败 HTTP {resp.status_code}: {resp.text[:200]}")
            body = resp.json()

        raw_results = body.get("web_search")
        if not isinstance(raw_results, list) or not raw_results:
            # 没有检索结果字段 = 无法证明检索发生过。此处**不返回正文**。
            raise RuntimeError(
                "联网检索未返回结果（响应缺少 web_search 字段），"
                "无法确认检索是否真的执行，因此不采用本次正文"
            )
        results = []
        for item in raw_results:
            if isinstance(item, dict):
                text = str(item.get("content") or item.get("text") or "").strip()
                if text:
                    entry = {"content": text}
                    if item.get("link"):
                        entry["link"] = str(item["link"])
                    if item.get("title"):
                        entry["title"] = str(item["title"])
                    results.append(entry)
            elif isinstance(item, str) and item.strip():
                results.append({"content": item.strip()})
        if not results:
            raise RuntimeError("联网检索返回的 web_search 字段里没有可用内容")

        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError("联网检索返回空 choices")
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not text:
            raise RuntimeError("联网检索返回空正文")
        return {"text": text, "results": results}

    # ===================== 小米 MiMo：视觉备用 + TTS 配音 =====================

    async def xiaomi_chat(self, messages: list, temperature: float = 0.3,
                          max_tokens: int = 8192,
                          thinking_disabled: bool = False) -> str:
        """小米 MiMo 文本/视觉对话（OpenAI 兼容，token plan 网关）。"""
        if not self.xm_key:
            raise Exception("Xiaomi API key not configured")
        payload = {"model": self.xm_model, "messages": messages,
                   "temperature": temperature, "max_tokens": _safe_int(max_tokens, 8192)}
        if thinking_disabled:
            payload["thinking"] = {"type": "disabled"}
        return await self._call(self.xm_url, self.xm_key, payload)

    async def light_task_chat(self, messages: list, max_tokens: int = 256) -> str:
        """轻量任务（意图分类/标题/摘要等）统一入口。

        模型分工（2026-09-06 用户定规）：轻量任务优先小米 MiMo V2.5（token plan 免费）；
        未配置 Key 或调用失败时回退 DeepSeek flash 并显式关闭思考模式。
        需要思考推理的任务不走此入口，直接用 deepseek_chat（v4-pro）。
        注意：MiMo V2.5 默认开启思考模式，轻任务必须显式 disabled，
        否则思考耗尽 max_tokens 导致 content 为空（实测复现）。
        """
        if self.xm_key:
            try:
                payload = {"model": self.xm_model, "messages": messages,
                           "temperature": 0.2, "max_tokens": _safe_int(max_tokens, 8192),
                           "thinking": {"type": "disabled"}}
                return await self._call(self.xm_url, self.xm_key, payload)
            except Exception as e:
                logger.warning("Light task via MiMo failed, falling back to DeepSeek flash: %s", str(e)[:200])
        payload = {
            "model": "deepseek-v4-flash", "thinking": {"type": "disabled"},
            "messages": messages, "temperature": 0, "max_tokens": max_tokens,
        }
        return await self._call(self.ds_url, self.ds_key, payload)

    async def xiaomi_vision(self, image_base64: str, prompt: str,
                            mime_type: str = "image/jpeg",
                            parse_json: bool = True) -> "dict | str":
        """MiMo 视觉理解。返回与 kimi_vision 相同的形状：解析后的 dict 或原始文本。"""
        mime_type_clean = mime_type if mime_type in ("image/png", "image/webp", "image/jpeg") else "image/jpeg"
        b64 = str(image_base64 or "")
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[-1]
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type_clean};base64,{b64}"}}
        ]}]
        content = await self.xiaomi_chat(messages, temperature=0.3, max_tokens=8192)
        if not parse_json:
            return content
        return self._extract_json(content)

    async def vision_mimo_first(self, image_base64: str, prompt: str,
                                mime_type: str = "image/jpeg",
                                parse_json: bool = True) -> "dict | str | None":
        """MiMo-first 视觉调用（Fact.md 2026-09-06 模型分工定规）。

        小米 MiMo 全模态优先（xm_key 已配置时），ZhipuAI 视觉回退；
        两者都失败返回 None，由调用方按既有降级路径处理。
        返回形状与 xiaomi_vision/zhipuai_vision 一致：
        parse_json=True 时尽量返回 dict，否则返回原始文本。
        Kimi 无视觉输入能力，不进视觉链（FreqErr 教训）。
        """
        if self.xm_key:
            try:
                result = await self.xiaomi_vision(image_base64, prompt, mime_type,
                                                  parse_json=parse_json)
                if isinstance(result, dict) or (isinstance(result, str) and result.strip()):
                    return result
                logger.warning("vision_mimo_first: xiaomi_vision returned empty result")
            except Exception as e:
                logger.warning("vision_mimo_first: xiaomi_vision failed: %s", str(e)[:200])
        try:
            return await self.zhipuai_vision(image_base64, prompt, mime_type,
                                             parse_json=parse_json)
        except Exception as e:
            logger.warning("vision_mimo_first: zhipuai_vision failed: %s", str(e)[:200])
        return None

    async def xiaomi_tts(self, text: str, voice: str = "",
                         audio_format: str = "mp3") -> tuple[bytes, str]:
        """合成语音，返回 (音频字节, MIME)。复用 _call_raw 的重试与脱敏日志。

        上游契约：assistant 消息内容即朗读文本；audio.format 可选 wav/mp3/pcm；
        响应 choices[0].message.audio.data 为 base64 音频。
        """
        if not self.xm_key:
            raise Exception("Xiaomi API key not configured")
        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("TTS text is empty")
        fmt = str(audio_format or "mp3").strip().lower()
        if fmt in ("pcm16",):
            fmt = "pcm"
        if fmt not in ("wav", "mp3", "pcm"):
            fmt = "mp3"
        v = str(voice or "").strip()
        payload = {
            "model": self.xm_tts_model,
            "messages": [{"role": "assistant", "content": clean_text}],
            "audio": {"format": fmt, "voice": v or self.xm_tts_voice},
        }
        # TTS 固定独立超时与单次尝试：语音合成短平快，不应继承 900s 解题超时，
        # 也不应重试两次让前端干等，更不能失败后把带 audio 块的 payload
        # 泄漏给 custom fallback 聊天模型再打一轮。
        message = await self._call_raw(self.xm_url, self.xm_key, payload,
                                       timeout=60, _is_fallback=True, max_retries=1)
        audio_obj = message.get("audio") if isinstance(message, dict) else None
        b64_data = str((audio_obj or {}).get("data", "") or "")
        if not b64_data.strip():
            # 回退模型不会返回 audio；避免把空 content 当成功
            log_error("xiaomi_tts", f"no audio data for model={payload['model']} (len={len(clean_text)})")
            raise Exception("TTS response contained no audio data")
        import base64 as _b64mod
        try:
            audio_bytes = _b64mod.b64decode(b64_data)
        except Exception as exc:
            log_error("xiaomi_tts", f"base64 decode failed: {exc}")
            raise Exception(f"TTS audio decode failed: {exc}")
        if not audio_bytes:
            raise Exception("TTS audio decoded to empty bytes")
        # pcm 为裸采样流，<audio> 元素无法直接播放；当前仅服务 /api/tts 的
        # HTTP 响应（默认 mp3），此映射仅作格式归一保留，勿用于前端播放场景
        mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "pcm": "audio/L16"}[fmt]
        return audio_bytes, mime

    # ===================== Kimi: 视觉理解/OCR（备用） =====================


    async def kimi_chat(self, messages: list, temperature: float = 1,
                        max_tokens: int = 16384) -> str:
        s = load_settings()
        model = s.get("kimi_model", "kimi-k2.6")
        # kimi-k2.6 当前接口只接受 temperature=1；在服务层统一约束，
        # 防止调用方沿用 DeepSeek 的 0/0.3 导致整条回退链 400。
        if str(model).lower().startswith("kimi-k2.6"):
            temperature = 1
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        return await self._call(self.km_url, self.km_key, payload)

    async def kimi_vision(self, image_base64: str, prompt: str,
                          mime_type: str = "image/jpeg",
                          parse_json: bool = True) -> "dict | str":
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}}
        ]}]
        s = load_settings()
        model = s.get("kimi_model", "kimi-k2.6")
        for attempt in range(1, 3):
            tokens = self.km_max_tokens if attempt == 1 else min(self.km_max_tokens * 2, 131072)
            try:
                payload = {"model": model, "messages": messages, "temperature": 1, "max_tokens": tokens}
                content = await self._call(self.km_url, self.km_key, payload)
                if not parse_json:
                    return content
                return self._extract_json(content)
            except Exception as e:
                # 空响应/JSON 解析失败均视为截断，加大 tokens 重试一次
                _err = str(e)
                truncated = ("No JSON" in _err or "JSON parse failed" in _err
                             or "empty response" in _err.lower())
                if attempt == 1 and truncated:
                    logger.warning(
                        "kimi_vision attempt 1 truncated (tokens=%d), retrying with %d. "
                        "Error: %s", tokens, min(self.km_max_tokens * 2, 131072), _err[:200])
                    continue
                raise

    async def kimi_ocr(self, image_base64: str, user_hint: str = "",
                       user_tags: list = None, user_grade: str = "",
                       mime_type: str = "image/jpeg") -> dict:
        prompt = (
            "仅OCR识别图片中的文字。如果图片中包含手写的解答过程或参考答案，请提取到 `handwritten_answer` 中（如果没有则留空）。\n"
            "另外，请务必提供题目示意图的详细描述到 `diagram_description` 中（包含坐标、几何关系、各点相对位置等），如果没有图则留空。\n"
            "注意：提取题面 `ocr_text` 时，不要包含手写的答案，只需提取原始题目内容。数学公式用LaTeX（内联$...$，块级$$...$$）。\n"
        )
        if user_grade: prompt += f"年级：{user_grade}\n"
        if user_tags: prompt += f"知识点：{', '.join(user_tags)}\n"
        if user_hint: prompt += f"提示：{user_hint}\n"
        prompt += (
            "直接返回JSON，不要加任何其他文字：\n"
            '{"ocr_text":"完整题目原文","subject":"学科","grade":"年级",'
            '"knowledge_tags":["标签1"],"region":"","avg_score":null,'
            '"handwritten_answer":"提取出的手写参考答案或笔记",'
            '"diagram_description":"图中图形/示意图的详细描述"}'
        )
        mime_type_clean = mime_type if mime_type in ("image/png", "image/webp", "image/jpeg") else "image/jpeg"
        last_error = None

        # ==== 主力：小米 MiMo 全模态（token plan，模型分工定规） ====
        if self.xm_key:
            try:
                result = await self.xiaomi_vision(image_base64, prompt, mime_type_clean, parse_json=True)
                if isinstance(result, dict):
                    self._verify_ocr_result(result, "")
                logger.info("OCR succeeded with Xiaomi %s", self.xm_model)
                return result
            except Exception as e:
                last_error = e
                logger.warning("Xiaomi OCR attempt failed: %s", str(e)[:200])

        # ==== 回退：GLM (ZhipuAI) 再试2次 ====
        if self.zp_key:
            for attempt in range(1, 3):
                tokens = 8192 if attempt == 1 else 65536
                try:
                    # 显式传递升级后的 tokens（glm-4v 系列在 zhipuai_chat 内仍受 API 上限钳制）
                    raw = await self.zhipuai_vision(image_base64, prompt, mime_type_clean,
                                                    parse_json=False, max_tokens=tokens)
                    if isinstance(raw, str):
                        raw = raw.strip()
                    result = self._extract_json(raw)
                    self._verify_ocr_result(result, raw)
                    logger.info("OCR succeeded with ZhipuAI glm-4v-flash attempt %d", attempt)
                    return result
                except Exception as e:
                    last_error = e
                    logger.warning("GLM OCR attempt %d failed: %s", attempt, str(e)[:200])
                    continue

        # Kimi 视觉兜底已移除（功能检查轮）：Kimi 无视觉输入能力，
        # 发送 image_url 内容块必然 400，全链失败时白烧 2 次调用拖慢报错。

        logger.error("OCR all providers exhausted: %s", str(last_error)[:300])
        raise last_error if last_error else Exception("OCR all providers exhausted")

    async def glm4v_reference_svg(self, images: list[dict], ocr_text: str = "",
                                  diagram_description: str = "") -> str:
        """让 OCR 视觉模型先忠实复刻原题图，返回紧凑 SVG 或空字符串。

        images: [{base64, mime_type, filename}]，只应传入题干角色图片。
        """
        if not images:
            return ""
        prompt = (
            "你是考试题图OCR复刻器。只观察图片中属于当前题目的几何图、坐标图、流程图、表格图、"
            "物理示意图或实验装置图。若当前题目没有需要复刻的图，只输出 NO_DIAGRAM。\n"
            "若有图，只输出一个完整紧凑SVG，不要解释、不要Markdown代码块、不要答案。要求：\n"
            "1. viewBox合理，SVG总长度尽量小于3500字符；仅用svg/g/path/line/polyline/polygon/rect/"
            "circle/ellipse/text/tspan。\n"
            "2. 仅黑白；保留所有字母、数值、单位、箭头、虚实线、直角/平行等标记。\n"
            "3. 忠实保留点的相对位置、线段长短趋势、图形拓扑、方向和装置连接，不补造条件。\n"
            "4. 多张图时只合并属于同一道题的题干图，按原阅读顺序排布。\n"
            "5. 图片若是含多道题的整页，只复刻与下方当前题目OCR相对应的图，忽略同页其他题目及其图。\n"
            f"当前题目OCR：{(ocr_text or '')[:1800]}\n"
            f"已有图形描述：{(diagram_description or '')[:1200]}\n"
        )
        content = [{"type": "text", "text": prompt}]
        for index, img in enumerate(images[:12]):
            b64 = str(img.get("base64", ""))
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[-1]
            if not b64:
                continue
            mime = str(img.get("mime_type", "image/jpeg"))
            content.append({"type": "text", "text": f"题干图 {index + 1}"})
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        if len(content) == 1:
            return ""
        messages = [{"role": "user", "content": content}]
        last_error = None
        invalid_response = False
        if self.xm_key:
            # 模型分工定规：视觉生成 MiMo 全模态优先（原 Kimi 兜底无视觉能力，必 400）
            try:
                raw = await self.xiaomi_chat(messages, temperature=0.3, max_tokens=4096)
                if "NO_DIAGRAM" in (raw or "").upper():
                    return ""
                svg = _complete_svg(raw)
                if svg:
                    return svg
                invalid_response = True
            except Exception as exc:
                last_error = exc
                logger.warning("MiMo reference SVG failed: %s", str(exc)[:200])
        if self.zp_key:
            try:
                raw = await self.zhipuai_chat(
                    messages, model=self.zp_model, temperature=1, max_tokens=1024
                )
                if "NO_DIAGRAM" in (raw or "").upper():
                    return ""
                svg = _complete_svg(raw)
                if svg:
                    return svg
                invalid_response = True
            except Exception as exc:
                last_error = exc
                logger.warning("GLM reference SVG failed: %s", str(exc)[:200])
        if last_error:
            raise last_error
        if invalid_response:
            raise ValueError("视觉模型既未返回 SVG，也未明确返回 NO_DIAGRAM")
        return ""

    @staticmethod
    def _verify_ocr_result(result: dict, raw: str):
        if not isinstance(result, dict):
            logger.warning("kimi_ocr result is not an object: %r", raw[:200])
            raise ValueError("OCR result is not an object")
        missing = []
        ocr_text = result.get("ocr_text", "")
        if not ocr_text or not ocr_text.strip():
            missing.append("ocr_text为空")
        if len(ocr_text) > 5 and not result.get("subject"):
            missing.append("subject缺失")
        if missing:
            logger.warning(
                "kimi_ocr result incomplete: %s. ocr_text preview: %.100s",
                ", ".join(missing), ocr_text)
            raise ValueError("OCR result incomplete: " + ", ".join(missing))

    async def kimi_explain_diagram(self, image_base64: str, context: str = "") -> str:
        prompt = "请详细描述这张图片中的图表/图形内容，包括坐标轴、数据点、图形结构、标注文字等。"
        if context:
            prompt = f"上下文：{context}\n\n" + prompt
        messages = [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
        ]}]
        s = load_settings()
        model = s.get("kimi_model", "kimi-k2.6")
        payload = {"model": model, "messages": messages, "temperature": 1, "max_tokens": 8192}
        return await self._call(self.km_url, self.km_key, payload)

    async def kimi_search(self, query: str) -> str:
        s = load_settings()
        model = s.get("kimi_model", "kimi-k2.6")
        prompt = (
            f"请联网搜索以下信息，并整理成简洁的参考资料（保留关键数据、公式、题型结构等）：\n\n"
            f"{query}\n\n"
            f"如果搜不到相关信息，请明确说明。"
        )
        messages = [{"role": "user", "content": prompt}]
        payload = {"model": model, "messages": messages, "temperature": 1, "max_tokens": 8192}
        return await self._call(self.km_url, self.km_key, payload)

    # ===================== 2026-07-29 新增功能：多图角色识别 =====================

    async def glm4v_classify_image_roles(self, images: list[dict]) -> list[dict]:
        """识别多张图片在同一道题中的角色。images: [{filename, base64, mime_type}]
        返回 [{filename, role, confidence}]，role 取值 question/analysis/answer/extra"""
        if not images:
            return []
        if len(images) == 1:
            return [{"filename": images[0].get("filename", ""), "role": "question", "confidence": 1.0}]
        content = [
            {"type": "text", "text": (
                "以下是一道题的若干张图片。请判断每张图片的角色：\n"
                "- question: 题干图（题目本身，必须优先识别）\n"
                "- analysis: 解析图（解题过程中的辅助图）\n"
                "- answer: 答案图（只含最终答案/结果）\n"
                "- extra: 其他无关或无法判断的图\n"
                "输出JSON: [{\"filename\":\"原始文件名\",\"role\":\"question\",\"confidence\":0.95}]\n"
                "注意：每张图必须对应一个条目，filename 必须和输入一致。"
            )}
        ]
        for img in images:
            b64 = img.get("base64", "")
            if b64.startswith("data:"):
                b64 = b64.split(",", 1)[-1]
            mime = img.get("mime_type", "image/jpeg")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            content.append({"type": "text", "text": f"filename: {img.get('filename','')}"})
        messages = [{"role": "user", "content": content}]
        raw = ""
        last_error = None
        providers = []
        # 模型分工（Fact.md 定规）：图像识别一律走小米 MiMo V2.5 全模态；
        # 回退 ZhipuAI 视觉（GLM-4V-Flash）。Kimi 无视觉输入能力，不得作为
        # 视觉回退（旧链 Kimi 兜底每次必 400 空耗一次调用）。
        # MiMo 默认开思考，结构化轻任务必须显式 disabled（实测思考耗尽 max_tokens）。
        if self.xm_key:
            providers.append(("MiMo", lambda: self.xiaomi_chat(
                messages, temperature=0.3, max_tokens=4096,
                thinking_disabled=True)))
        if self.zp_key:
            providers.append(("ZhipuAI", lambda: self.zhipuai_chat(
                messages, model=self.zp_model, temperature=1, max_tokens=4096
            )))
        for provider_name, call in providers:
            try:
                raw = await call()
                break
            except Exception as exc:
                last_error = exc
                logger.warning("%s image role classification failed: %s", provider_name, str(exc)[:200])
        try:
            if not raw:
                raise last_error or RuntimeError("No vision provider configured")
            result = self._extract_json(raw)
            roles = result.get("roles", result) if isinstance(result, dict) else result
            if not isinstance(roles, list):
                roles = []
            valid_roles = []
            for r in roles:
                if not isinstance(r, dict):
                    continue
                role = str(r.get("role", "extra")).lower()
                if role not in ("question", "analysis", "answer", "extra"):
                    role = "extra"
                valid_roles.append({
                    "filename": str(r.get("filename", "")),
                    "role": role,
                    "confidence": float(r.get("confidence", 0.5)),
                })
            # 确保每个输入图片都有角色
            by_name = {r["filename"]: r for r in valid_roles}
            final = []
            for img in images:
                fn = img.get("filename", "")
                r = by_name.get(fn, {"filename": fn, "role": "extra", "confidence": 0.0})
                final.append(r)
            return final
        except Exception as e:
            logger.warning("Image role classification exhausted providers: %s", str(e)[:200])
            # 失败时全部标记为 question，让前端手动修正
            return [{"filename": img.get("filename", ""), "role": "question", "confidence": 0.0} for img in images]

    # ===================== 2026-07-29 新增功能：题目对比模式分区 =====================

    async def deepseek_generate_comparison_regions(
            self, structure_graph: dict, answer_html: str, standard_answer: str = "") -> dict:
        """根据结构梳理图和标准解答划分对比区域（最多7个）。"""
        nodes = (structure_graph or {}).get("nodes", [])
        edges = (structure_graph or {}).get("edges", [])
        if not nodes or not answer_html:
            return {"regions": []}
        # 浅色/深色主题各7色，AI 从中选择
        color_table = [
            {"light": "#3b82f6", "dark": "#60a5fa"},   # 蓝
            {"light": "#ef4444", "dark": "#f87171"},   # 红
            {"light": "#10b981", "dark": "#34d399"},   # 绿
            {"light": "#f59e0b", "dark": "#fbbf24"},   # 黄
            {"light": "#8b5cf6", "dark": "#a78bfa"},   # 紫
            {"light": "#ec4899", "dark": "#f472b6"},   # 粉
            {"light": "#06b6d4", "dark": "#22d3ee"},   # 青
        ]
        prompt = (
            "你是一位严谨的数学/物理教师。请根据以下结构梳理图和标准证明过程，"
            "将证明过程划分为若干逻辑区域（最多7个），每个区域对应结构图上的若干节点。\n\n"
            f"【结构图节点】\n{json.dumps(nodes, ensure_ascii=False)[:4000]}\n\n"
            f"【结构图边】\n{json.dumps(edges, ensure_ascii=False)[:2000]}\n\n"
            f"【标准证明过程HTML】\n{answer_html[:6000]}\n\n"
            f"【可选标准答案】\n{standard_answer[:1000]}\n\n"
            "输出JSON:\n"
            '{"regions":['
            '{"id":"r1","node_ids":[0,1],"paragraph_range":[0,2],"color_index":0,"purpose":"证明某结论（若目的仅为推导下一步可省略purpose）"}'
            ']}\n\n'
            "说明：\n"
            "- id 唯一，如 r1, r2...\n"
            "- node_ids 为结构图节点 id 数组\n"
            "- paragraph_range 为答案 HTML 中段落索引范围 [start, end)，按 <p> 或 <div> 标签分段\n"
            "- color_index 从 0-6 选取，对应颜色表（0蓝1红2绿3黄4紫5粉6青）\n"
            "- purpose 描述该区域要证明什么；如果只是为下一步推导做铺垫可省略\n"
            "- 不要划分过多区域，最多7个；若证明步骤少可少于7个"
        )
        try:
            result = await self.deepseek_json([{"role": "user", "content": prompt}], max_tokens=4096)
            regions = result.get("regions", [])
            if not isinstance(regions, list):
                regions = []
            valid = []
            for i, r in enumerate(regions[:7]):
                if not isinstance(r, dict):
                    continue
                idx = max(0, min(6, int(r.get("color_index", i)) if str(r.get("color_index", "")).isdigit() else i))
                valid.append({
                    "id": str(r.get("id", f"r{i+1}")),
                    "node_ids": [int(n) for n in r.get("node_ids", []) if str(n).lstrip('-').isdigit()],
                    "paragraph_range": list(r.get("paragraph_range", [0, 1]))[:2],
                    "color": color_table[idx]["light"],
                    "color_dark": color_table[idx]["dark"],
                    "purpose": str(r.get("purpose", "")).strip(),
                })
            return {"regions": valid}
        except Exception as e:
            logger.warning("deepseek_generate_comparison_regions failed: %s", str(e)[:200])
            raise

    # ===================== 2026-07-29 新增功能：标签统一 =====================

    async def deepseek_unify_tags(self, tags: list[str]) -> dict:
        """让 AI 分组语义等价的标签。返回 {groups: [[标准标签, 别名1, ...], ...]}"""
        if not tags:
            return {"groups": []}
        prompt = (
            "以下是一组题目/笔记中的知识点标签。请将语义完全等价的标签归为一组，"
            "每组第一个作为标准标签（取最常见或最完整形式），其余作为别名。\n\n"
            "只归并明确同义的标签（如「直角三角形」和「Rt三角形」），不要强行合并不同概念。\n\n"
            f"标签列表：{json.dumps(tags, ensure_ascii=False)}\n\n"
            "输出JSON: {\"groups\":[[\"标准标签\",\"别名1\",\"别名2\"],...]}"
        )
        try:
            result = await self.deepseek_json([{"role": "user", "content": prompt}], max_tokens=4096)
            groups = result.get("groups", [])
            if not isinstance(groups, list):
                groups = []
            valid = []
            for g in groups:
                if isinstance(g, list) and len(g) >= 2:
                    valid.append([str(x) for x in g])
            return {"groups": valid}
        except Exception as e:
            logger.warning("deepseek_unify_tags failed: %s", str(e)[:200])
            return {"groups": []}

    async def glm_name_bank(self, tag: str) -> str:
        """根据标签语义生成题库名称。"""
        prompt = (
            f"请为以下知识点标签生成一个合适的题库名称，要求简洁、准确、不超过12个字。\n"
            f"标签：{tag}\n\n"
            "直接输出题库名称，不要加任何其他文字。"
        )
        try:
            name = await self.zhipuai_chat([
                {"role": "user", "content": prompt}
            ], model="glm-4-flash", temperature=0.7, max_tokens=128)
            name = name.strip().strip('"').replace("题库", "").strip()
            if not name:
                name = tag
            return f"{name}题库"
        except Exception as e:
            logger.warning("glm_name_bank failed for %s: %s", tag, str(e)[:200])
            return f"{tag}题库"

    # ===================== 2026-07-29 新增功能：学习单生成 =====================

    async def deepseek_generate_worksheet(
            self, subject: str, grade: str, topic: str,
            concept_notes: str, example_questions: str, practice_questions: str,
            paper_size: str = "A4") -> str:
        """生成学习单 HTML。"""
        from config import DEFAULT_PROMPT_TEMPLATES
        tmpl = DEFAULT_PROMPT_TEMPLATES.get("worksheet", DEFAULT_PROMPT_TEMPLATES["default_paper"])
        content = tmpl["content"]
        replacements = {
            "{grade}": grade or "",
            "{subject}": subject or "",
            "{user_prompt}": topic,
            "{concept_notes}": concept_notes or "（无）",
            "{example_questions}": example_questions or "（无）",
            "{practice_questions}": practice_questions or "（无）",
            "{paper_size}": paper_size,
        }
        for k, v in replacements.items():
            content = content.replace(k, v)
        system = (
            "你是专业教学资料排版专家。生成可打印的学习单HTML。"
            "学习单应包含概念讲解、典型例题、巩固练习三部分，布局宽松，适合阅读。"
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        try:
            return await self.deepseek_chat(messages, max_tokens=16384, scope="paper")
        except Exception as e:
            logger.warning("deepseek_generate_worksheet failed: %s", str(e)[:200])
            raise


ai_service = AIService()


def call_vision_model(prompt: str, image_base64: str, max_tokens: int = 1024) -> dict | str | None:
    """
    调用视觉模型分析图片。
    优先使用智谱 GLM-4V-Flash，失败时回退 Kimi。
    返回解析后的 JSON dict，或原始文本，或 None（失败时）。
    """
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 在已有事件循环中（如 FastAPI 端点内），创建新循环跑协程。
            # 不能用 with 块：退出时会 shutdown(wait=True) join 工作线程，
            # 使 60s 超时形同虚设（实际阻塞到协程自然结束，最长 ai_timeout）。
            import concurrent.futures
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(
                    asyncio.run,
                    _call_vision_model_async(prompt, image_base64, max_tokens)
                )
                return future.result(timeout=60)
            except concurrent.futures.TimeoutError:
                log_error("call_vision_model", "timed out after 60s")
                future.cancel()
                return None
            finally:
                pool.shutdown(wait=False)
        else:
            return loop.run_until_complete(
                _call_vision_model_async(prompt, image_base64, max_tokens)
            )
    except Exception as e:
        log_error("call_vision_model", f"failed: {e}")
        return None


async def _call_vision_model_async(prompt: str, image_base64: str, max_tokens: int = 1024) -> dict | str | None:
    """异步调用视觉模型。

    模型分工（Fact.md 2026-09-06 用户定规）：图像识别一律优先小米 MiMo V2.5
    全模态（token plan 免费）；ZhipuAI 视觉为回退。Kimi 无视觉输入能力，
    不进视觉链（FreqErr：全挂时白烧 400 调用，2026-09-09 移除）。"""
    try:
        if ai_service.xm_key:
            try:
                result = await ai_service.xiaomi_vision(image_base64, prompt, parse_json=False)
                if isinstance(result, str) and result.strip():
                    return result
            except Exception as e:
                logger.warning("xiaomi_vision failed: %s", str(e)[:200])

        result = await ai_service.zhipuai_vision(image_base64, prompt, parse_json=True)
        if isinstance(result, dict):
            return result
        if isinstance(result, str) and result.strip():
            return result
    except Exception as e:
        logger.warning("zhipuai_vision failed: %s", str(e)[:200])

    return None
