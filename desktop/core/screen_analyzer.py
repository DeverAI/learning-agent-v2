"""OISystem 屏幕检测系统。

职责：
- 截图：全屏 / 活动窗口 / 自定义矩形
- 清晰度压缩：按 screen_quality 设置压缩到低清（控 API 成本）
- 视觉引擎调度：默认 GLM-4.6V，可切 KIMI（DeepSeek 不支持视觉会被拦截）
- 异步分析：用 QThread 避免阻塞主线程
- 结果结构化：返回 {activity, efficiency, code_seen, progress_delta, current_problem}
"""
import json
import re
from typing import Dict, Optional

from PySide6.QtCore import QObject, Signal, QThread, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from config.settings import ConfigManager
from utils.helpers import logger, log_event
from utils.exceptions import AICallError


# 清晰度预设：(max_width, jpeg_quality)
QUALITY_PRESETS = {
    "low":    (640,  40),
    "medium": (1024, 65),
    "high":   (1600, 85),
}


def _str_to_bool(v) -> bool:
    """安全的字符串转 bool，处理 "false"/"true"/0/1 等。"""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _extract_json_object(text: str) -> Optional[str]:
    """从文本中提取第一个完整的 JSON 对象（支持嵌套花括号）。

    使用花括号深度计数而非贪婪/非贪婪正则，确保嵌套 JSON 如
    {"activity": "code", "detail": {"key": "value"}} 能被完整提取。
    """
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == '\\' and in_string:
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_ai_json(raw: str) -> dict:
    """解析 AI 返回的 JSON，健壮处理 markdown 包裹（模块级函数，供 worker 复用）。"""
    raw = raw.strip()
    # 尝试直接解析
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试从 ```json ... ``` 或 ``` ... ``` 中提取
        m = re.search(r"```(?:json|JSON)?\s*", raw, re.DOTALL)
        if m:
            after_fence = raw[m.end():]
            # 找到围栏后的第一个 JSON 对象（支持嵌套）
            json_str = _extract_json_object(after_fence)
            if json_str:
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError:
                    return _make_empty_result("解析失败")
            else:
                return _make_empty_result("解析失败")
        else:
            # 最后尝试从整个文本提取第一个完整 JSON 对象
            json_str = _extract_json_object(raw)
            if json_str:
                try:
                    data = json.loads(json_str)
                except json.JSONDecodeError:
                    return _make_empty_result("解析失败")
            else:
                return _make_empty_result("解析失败")
    # r39 P1 修复：AI 可能返回 JSON 数组/字符串（非 dict），data.get 会抛 AttributeError
    if not isinstance(data, dict):
        return _make_empty_result("解析失败")
    try:
        data["efficiency"] = int(data.get("efficiency", 0))
    except (ValueError, TypeError):
        data["efficiency"] = 0
    data["code_seen"] = _str_to_bool(data.get("code_seen", False))
    return data


def _make_empty_result(reason: str = "") -> dict:
    return {
        "activity": reason,
        "efficiency": 0,
        "code_seen": False,
        "progress_delta": "",
        "current_problem": "",
    }


class _AnalyzeWorker(QObject):
    """分析工作线程对象。"""
    finished = Signal(int, dict)   # (request_id, result)
    failed = Signal(int, str)      # (request_id, reason)
    # r39 P1 修复：新增 done 信号，finally 中兜底触发，确保线程能退出
    done = Signal()

    def __init__(self, image_bytes: bytes, request_id: int = 0):
        super().__init__()
        self._image_bytes = image_bytes
        self._request_id = request_id

    def run(self):
        try:
            from core.ai_client import vision_chat, resolve_vision_target
            # role 路由：视觉任务统一走 resolve_vision_target（role + screen_engine + 回退），
            # 保证模型名与 provider 一致，避免把 glm-4.6v 发给 kimi 或反之。
            provider, model = resolve_vision_target()
            prompt = (
                "你是 OI 学习辅助系统的屏幕分析模块。请分析这张编程学习屏幕截图，"
                "返回严格的 JSON（不要 markdown 代码块）：\n"
                "{\n"
                '  "activity": "用户当前活动描述（如：在 IDE 写代码/看题目/调试/发呆）",\n'
                '  "efficiency": "学习效率评分 0-100（整数）",\n'
                '  "code_seen": true/false,\n'
                '  "progress_delta": "相比上次的进展描述（如：新增 10 行/无变化/调试中）",\n'
                '  "current_problem": "当前题目 ID 或描述（如识别到）"\n'
                "}"
            )
            raw = vision_chat(prompt, self._image_bytes,
                              provider=provider, model=model or None)
            result = _parse_ai_json(raw)
            self.finished.emit(self._request_id, result)
        except AICallError as e:
            self.failed.emit(self._request_id, str(e))
        except Exception as e:
            self.failed.emit(self._request_id, f"异常: {e}")
        finally:
            # r39 P1 修复：与 _ScreenshotWorker 一致，finally 兜底保证 done 信号触发
            # 避免线程因 emit 自身异常无法退出导致泄漏
            try:
                self.done.emit()
            except RuntimeError:
                pass


class ScreenAnalyzer(QObject):
    """屏幕分析器。"""

    analyze_completed = Signal(dict)
    analyze_failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.cfg = ConfigManager()
        self._last_activity = None
        self._last_code_seen = False
        self._thread = None
        self._worker = None
        self._request_id = 0  # r49 P1：请求代次，过滤旧线程迟到结果

    # ---------- 截图 ----------
    def capture(self) -> bytes:
        """按配置截屏，返回压缩后的 JPEG 字节。截图时触发闪光动画。"""
        # 触发截图闪光动画
        try:
            from ui.camera_flash import CameraFlash
            CameraFlash.trigger()
        except Exception as e:
            logger.debug(f"闪光动画触发失败: {e}")

        s = self.cfg.settings
        region = s.screen_capture_region

        try:
            if region == "fullscreen":
                pix = self._capture_fullscreen()
            elif region == "active_window":
                pix = self._capture_active_window()
            else:  # custom
                rect = s.screen_custom_rect
                pix = self._capture_rect(rect[0], rect[1], rect[2], rect[3])
        except Exception as e:
            logger.error(f"截图失败: {e}")
            try:
                from utils.helpers import append_err_record
                append_err_record("screen_analyzer", "截图失败", str(e)[:60])
            except Exception:
                pass
            return b""

        if pix is None or pix.isNull():
            logger.error("截图为空")
            return b""

        # 清晰度压缩
        max_w, quality = QUALITY_PRESETS.get(s.screen_quality, QUALITY_PRESETS["low"])
        try:
            configured_max = int(getattr(s, "screen_max_width", 0) or 0)
        except (TypeError, ValueError):
            configured_max = 0
        if configured_max > 0 and configured_max < max_w:
            max_w = configured_max

        if pix.width() > max_w:
            pix = pix.scaledToWidth(max_w, Qt.SmoothTransformation)

        # PySide6 的 save() 需要 QIODevice，用 QByteArray + QBuffer
        from PySide6.QtCore import QByteArray, QBuffer
        buf = QByteArray()
        device = QBuffer(buf)
        device.open(QBuffer.WriteOnly)
        pix.save(device, "JPEG", quality=quality)
        device.close()
        return buf.data()

    def _capture_fullscreen(self) -> QPixmap:
        # r39 P1 修复：primaryScreen() 可能返回 None（无显示器/RDP 断开）
        screen = QApplication.primaryScreen()
        if screen is None:
            return QPixmap()
        geo = screen.virtualGeometry()
        return screen.grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())

    def _capture_active_window(self) -> QPixmap:
        try:
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                rect = win32gui.GetWindowRect(hwnd)
                screen = QApplication.primaryScreen()
                if screen is None:
                    return QPixmap()
                return screen.grabWindow(0, rect[0], rect[1],
                                         rect[2] - rect[0], rect[3] - rect[1])
        except Exception as e:
            logger.debug(f"win32gui 活动窗口截图失败，fallback 到全屏: {e}")
        return self._capture_fullscreen()

    def _capture_rect(self, x, y, w, h) -> QPixmap:
        # r39 P1 修复：primaryScreen() 可能返回 None
        screen = QApplication.primaryScreen()
        if screen is None:
            return QPixmap()
        return screen.grabWindow(0, x, y, w, h)

    # ---------- 分析（异步） ----------
    def analyze(self, image_bytes: Optional[bytes] = None) -> dict:
        """对屏幕截图做视觉分析。

        若 image_bytes 为 None，则现场截图。
        为保持与 FocusEngine 回调兼容（同步返回 dict），
        同步模式下若 API 未配置会返回空结果；异步模式下结果通过信号返回。
        """
        if image_bytes is None:
            image_bytes = self.capture()
        if not image_bytes:
            empty = self._empty_result("截图失败")
            self.analyze_failed.emit("截图失败")
            return empty

        # 同步路径（兼容 FocusEngine 回调）：返回前先尝试用 worker 计算
        # 但若已有线程在跑，直接返回空避免堆积
        if self._thread is not None and self._thread.isRunning():
            logger.debug("上一次分析仍在进行，跳过")
            return self._empty_result("上次分析未完成")

        # 启动异步线程
        self._request_id += 1
        request_id = self._request_id
        self._thread = QThread()
        self._worker = _AnalyzeWorker(image_bytes, request_id=request_id)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        # r39 P1 修复：done 信号兜底，确保线程在任何情况下都能退出
        self._worker.done.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        # r38 P1 修复：线程结束后复位引用，避免下次 analyze 调用
        # isRunning() 时访问已 deleteLater 的 C++ 对象抛 RuntimeError
        # round48 P1：带代次校验，旧线程 finished 不得清掉新线程引用
        self._thread.finished.connect(
            lambda t=self._thread: self._reset_thread_refs(t)
        )
        self._thread.start()

        # 同步返回空结果（真实结果通过 analyze_completed 信号）
        # FocusEngine 的 _auto_analyze 会收到信号后再处理
        return self._empty_result("分析中（异步）")

    def _on_finished(self, request_id: int, result: dict):
        # r49 P1：请求代次过滤——旧线程迟到结果不得污染当前窗口/触发管控
        if request_id != self._request_id:
            logger.debug(f"丢弃旧截图分析结果 (request_id={request_id}, 当前={self._request_id})")
            return
        self._last_activity = result.get("activity")
        self._last_code_seen = result.get("code_seen", False)
        self.analyze_completed.emit(result)
        log_event("screen_analyze", {
            "engine": self.cfg.settings.screen_engine,
            "activity": result.get("activity"),
            "efficiency": result.get("efficiency"),
            "code_seen": result.get("code_seen"),
        })
        # 作弊检测
        try:
            from core.cheat_detector import CheatDetector
            CheatDetector().analyze_screen_result(result)
        except Exception as e:
            logger.debug(f"作弊检测处理失败: {e}")

        # r40 P0 修复：网站/窗口管控，对命中黑名单的问题页面直接关闭
        try:
            from core.site_guard import SiteGuard
            SiteGuard().enforce(result)
        except Exception as e:
            logger.debug(f"网站管控执行失败: {e}")

    def _on_failed(self, request_id: int, reason: str):
        # r49 P1：请求代次过滤
        if request_id != self._request_id:
            logger.debug(f"丢弃旧截图失败结果 (request_id={request_id}, 当前={self._request_id})")
            return
        logger.warning(f"屏幕分析失败: {reason}")
        self.analyze_failed.emit(reason)
        log_event("screen_analyze_failed", {"reason": reason})

    def _reset_thread_refs(self, finished_thread=None):
        """r38 P1 修复：线程结束后复位引用，避免下次 analyze 调用
        isRunning() 时访问已 deleteLater 的 C++ 对象抛 RuntimeError。

        round48 P1：增加代次校验——仅当 finished 的就是当前 _thread 时才清空，
        旧线程排队中的 finished 回调不能清掉之后新启动的线程引用。
        """
        if finished_thread is not None and self._thread is not finished_thread:
            return
        self._thread = None
        self._worker = None

    def _empty_result(self, reason: str) -> dict:
        return _make_empty_result(reason)
