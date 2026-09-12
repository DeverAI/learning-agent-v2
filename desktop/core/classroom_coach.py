"""课堂 AI 教师联动层（round 56）：打断信号 → 局部卡片板书 + 流式讲解词 + 朗读。

设计：Design.md「课堂 AI 教师联动层与设置接线（round 56）」、Techniques.md round 56 §1。

体验反馈修正（2026-09-07 用户实测）：
- 不再 open_page 全屏白板（遮挡网课且无关闭手段）→ 右上角局部卡片，
  不遮挡主内容，自动 30s 清除 + Ctrl+Alt+H 随时手动关闭；
- 字号不再随屏幕线性放大失控 → 双向钳制（标题 ≤26px、正文 ≤19px）；
- "讲解词生成中…"长时间占位 → chat_stream 流式，讲解词逐句实时上板。

职责边界：
- 订阅 InterruptEngine.interrupt_triggered（AI 主动打断与学生主动请求同链）
- 讲解词走对话主力模型（v4-pro 推理任务，流式）；失败 fail-safe 直接朗读 teach_point
- 朗读复用 TTSPlayer；朗读前 suppress_teacher 防自说回环
- 本层不碰麦克风/ASR/决策闸门——那是 round 55 引擎层的职责
"""
import threading
import time

from PySide6.QtCore import QObject, QTimer, QThread, Signal, Slot, QMetaObject, Qt
from PySide6.QtGui import QGuiApplication

from config.settings import ConfigManager
from core.screen_paint import ScreenPaintOverlay
from core.tts_player import TTSPlayer
from utils.helpers import logger, append_err_record

# 卡片展示秒数：讲完自动清除（常量起步，后续按反馈再加设置项）
COACH_BOARD_SEC = 30
# 正文最大字符数（卡片区域有限）
SPEECH_MAX_CHARS = 300
# 重入守卫上限（秒）：讲解词线程异常挂死时最迟此时允许新触发
BUSY_TIMEOUT_SEC = 90
# 关闭卡片的全局热键（pynput GlobalHotKeys 格式）
BOARD_HIDE_HOTKEY = "ctrl+alt+h"
# 学生「随时提问」的全局热键（R23）。
# 为什么必须有它：`InterruptEngine.request_teaching` 早就实现且有测试，但生产里
# **没有任何调用点**（只有 demo_coach.py 与测试在调）—— 也就是说
# 「随时打断网课并讲解」这句话在实现上是**不可达**的。这里补上入口。
ASK_HOTKEY_DEFAULT = "ctrl+alt+a"

# 卡片几何（屏幕比例坐标：右上角，不遮挡网课主内容）
CARD_X, CARD_Y, CARD_W = 0.54, 0.03, 0.44
CARD_HEADER_H = 0.055
CARD_BODY_H = 0.26


def _clamp_px(value: float, lo: int, hi: int) -> int:
    """字号双向钳制：随屏幕缩放但封顶封底（用户反馈#2：AI 字号失控）。"""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def _screen_geometry():
    """主屏几何（宽高像素）；无 QGuiApplication 环境时兜底 1080p。"""
    try:
        g = QGuiApplication.primaryScreen().geometry()
        return int(g.width()), int(g.height())
    except Exception:
        return 1920, 1080


def _wrap_text(text: str, chars_per_line: int) -> list:
    """按每行字数折行（write_text 原语无自动换行；优先在标点/空格断行）。"""
    clean = str(text or "").strip()
    if not clean:
        return []
    if chars_per_line <= 4:
        return [clean]
    lines = []
    while clean:
        if len(clean) <= chars_per_line:
            lines.append(clean)
            break
        head, rest = clean[:chars_per_line], clean[chars_per_line:]
        # 在行尾附近找最后一个断行点（标点优先，其次空格），不早于行中 55%
        cut = 0
        for i in range(len(head) - 1, max(0, int(chars_per_line * 0.55)) - 1, -1):
            if head[i] in "，。！？；：、,.!?;: ":
                cut = i + 1
                break
        if cut <= 0:
            cut = chars_per_line
        lines.append(head[:cut])
        clean = head[cut:] + rest
    return lines


class _HotkeyBridge(QObject):
    """把 pynput 监听线程的热键触发 marshal 回主线程（同 mute_mode 模式）。
    回调经构造注入，@Slot 方法体内调用——invokeMethod 按元对象找槽，queued 派发。"""

    def __init__(self, callback=None, parent=None):
        super().__init__(parent)
        self._cb = callback

    @Slot()
    def do_hide(self):
        try:
            if self._cb:
                self._cb()
        except Exception as e:
            logger.warning(f"热键清卡片异常: {str(e)[:120]}")

    @Slot()
    def do_action(self):
        """通用槽（R23）：同一个 bridge 类服务多个热键，各自构造注入回调。

        保留 `do_hide` 不动：已有测试与文档按该槽名断言，改名会把「加功能」
        变成「改契约」。invokeMethod 按元对象找槽，两个槽可分别调用。
        """
        try:
            if self._cb:
                self._cb()
        except Exception as e:
            logger.warning(f"热键动作异常: {str(e)[:120]}")


class _SpeechWorker(QObject):
    """讲解词生成工作线程：v4-pro 流式调用；partial/ready/failed 均带 request_id。"""

    partial = Signal(str, int)   # (累积文本, request_id)——流式节流后的中间结果
    ready = Signal(str, int)     # (完整讲解词, request_id)
    failed = Signal(str, int)    # (原因, request_id)
    done = Signal()

    def __init__(self, messages: list, request_id: int = 0, parent=None):
        super().__init__(parent)
        self._messages = messages
        self._request_id = request_id
        self._last_emit = 0.0
        self._last_len = 0
        self._lock = threading.Lock()

    def _emit_partial(self, full_text: str):
        """节流：≥0.4s 且 ≥12 字新增才发一次 partial（板书重绘频率控制）。"""
        now = time.time()
        with self._lock:
            if now - self._last_emit < 0.4 or len(full_text) - self._last_len < 12:
                return
            self._last_emit = now
            self._last_len = len(full_text)
        self.partial.emit(full_text[:SPEECH_MAX_CHARS], self._request_id)

    def run(self):
        try:
            from core import ai_client
            provider, model = ai_client.resolve_dialog_target()
            full = ai_client.chat_stream(
                self._messages, provider=provider, model=model,
                temperature=0.3, max_tokens=300,
                on_delta=self._emit_partial)
            self.ready.emit(full, self._request_id)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {str(e)[:200]}", self._request_id)
        finally:
            try:
                self.done.emit()
            except RuntimeError:
                pass


class ClassroomCoach(QObject):
    """课堂 AI 教师编排器：局部卡片板书 + 流式讲解词 + 朗读 + 回环防护。"""

    def __init__(self, monitor, engine, overlay=None, tts_player=None, parent=None):
        super().__init__(parent)
        self._monitor = monitor
        self._engine = engine
        self._overlay = overlay if overlay is not None else ScreenPaintOverlay()
        self._tts = tts_player if tts_player is not None else TTSPlayer()
        self._clear_timer = QTimer(self)          # 卡片自动清除（重入时复用）
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self._clear_board)
        self._poll_timer = QTimer(self)           # 决策轮询（evaluate 时机判定在引擎内）
        self._poll_timer.setSingleShot(False)
        self._poll_timer.timeout.connect(self._poll_evaluate)
        self._speech_thread = None
        self._speech_worker = None
        self._speech_req = 0                      # 迟到结果丢弃
        self._pending = None                      # (point, speak) 在途讲解词的上下文
        self._active_speech = ""                  # 当前朗读文本（playing 时补防护窗用）
        self._busy_until = 0.0                    # 简单重入守卫（讲解词在途）
        self._last_source_student = False
        self._hotkey_listener = None
        self._hotkey_bridge = None
        # R23：随时提问（学生主动打断）的热键与 bridge
        self._ask_listener = None
        self._ask_bridge = None
        self._eval_running = False                # 决策工作线程在途标志
        self.teach_logger = None                  # 可选回调 fn(point, speech, source)：成果互通登记

    # ---------- 接线 ----------

    def connect(self):
        """订阅打断信号 + 启动决策轮询 + 注册关闭热键 + TTS 状态跟踪。"""
        self._engine.interrupt_triggered.connect(self._on_trigger)
        self._poll_timer.start(self._poll_interval_ms())
        self._start_hide_hotkey()
        self._start_ask_hotkey()
        # TTS 状态跟踪（审查 M4）：防护窗在合成前开启，估时不含合成时延——
        # 真正开始播放时按词长补窗，播完再补收尾余量（suppress 取 max 语义）
        self._tts.stateChanged.connect(self._on_tts_state)

    def _poll_interval_ms(self) -> int:
        try:
            sec = int(getattr(ConfigManager().settings,
                              "interrupt_decision_interval_sec", 45) or 45)
        except Exception:
            sec = 45
        return max(5, min(600, sec)) * 1000

    def _poll_evaluate(self):
        """决策轮询（审查 C1 修复）：evaluate 内部是同步网络调用（v4-pro
        5~60s），绝不能在主线程跑——挪到 daemon 工作线程；engine 的
        interrupt_triggered 等信号均为绑定方法连接，从工作线程 emit 会
        queued 回主线程，信号侧零改动。上一轮未完成则跳过本轮（防堆积）。"""
        if self._eval_running:
            return
        self._eval_running = True

        def _job():
            try:
                self._engine.evaluate(force=False)
            except Exception as e:
                logger.warning(f"课堂决策线程异常（不中断）: {str(e)[:150]}")
            finally:
                self._eval_running = False

        try:
            threading.Thread(target=_job, daemon=True,
                             name="ClassroomEval").start()
        except Exception as e:
            # start 失败（线程资源耗尽等）必须复位标志，否则决策轮询永久静默死亡
            self._eval_running = False
            logger.error(f"决策工作线程启动失败: {type(e).__name__}: {str(e)[:150]}")
            try:
                append_err_record("core/classroom_coach.py", "决策线程启动失败",
                                  f"{type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass
        # 间隔设置可能被用户改小，按最新值重排
        try:
            self._poll_timer.start(self._poll_interval_ms())
        except RuntimeError:
            pass

    def _start_hide_hotkey(self):
        """Ctrl+Alt+H 关闭卡片（用户反馈#1：板书关不掉）。pynput 监听线程回调
        经 bridge marshal 回主线程再操作 QWidget。"""
        try:
            from pynput import keyboard as pynput_keyboard

            if self._hotkey_bridge is None:
                self._hotkey_bridge = _HotkeyBridge(self._clear_board)

            parts = BOARD_HIDE_HOTKEY.split("+")
            mods = "".join(f"<{p}>" for p in parts[:-1])
            pynput_str = f"{mods}+{parts[-1]}"

            def _on_hotkey():
                try:
                    QMetaObject.invokeMethod(self._hotkey_bridge, "do_hide",
                                             Qt.QueuedConnection)
                except Exception:
                    pass

            self._hotkey_listener = pynput_keyboard.GlobalHotKeys({
                pynput_str: _on_hotkey})
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
            logger.info(f"板书关闭热键已注册: {BOARD_HIDE_HOTKEY}")
        except Exception as e:
            logger.warning(f"板书关闭热键注册失败（不影响其他功能）: {str(e)[:120]}")

    # ---------- 学生「随时提问」（R23） ----------

    def _ask_hotkey_str(self) -> str:
        try:
            raw = str(getattr(ConfigManager().settings, "ask_hotkey", "") or "").strip()
        except Exception:
            raw = ""
        return raw or ASK_HOTKEY_DEFAULT

    def _start_ask_hotkey(self):
        """注册「随时提问」全局热键：学生打断网课的**唯一生产入口**。

        与 AI 自动打断解耦：即使音频感知被关掉，只要这条链在，学生仍能主动问。
        """
        try:
            from pynput import keyboard as pynput_keyboard

            hotkey = self._ask_hotkey_str()
            parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
            if not parts:
                return
            if self._ask_bridge is None:
                self._ask_bridge = _HotkeyBridge(self.prompt_ask)
            mods = "".join(f"<{p}>" for p in parts[:-1])
            pynput_str = f"{mods}+{parts[-1]}" if mods else parts[-1]

            def _on_hotkey():
                try:
                    QMetaObject.invokeMethod(self._ask_bridge, "do_action",
                                             Qt.QueuedConnection)
                except Exception:
                    pass

            self._ask_listener = pynput_keyboard.GlobalHotKeys({pynput_str: _on_hotkey})
            self._ask_listener.daemon = True
            self._ask_listener.start()
            logger.info(f"随时提问热键已注册: {hotkey}")
        except Exception as e:
            logger.warning(f"提问热键注册失败（不影响其他功能）: {str(e)[:120]}")

    def prompt_ask(self):
        """弹出提问输入框（必须在主线程；热键回调经 bridge 已 marshal 回来）。

        置顶标志是必需的：学生多半在全屏网课里按热键，普通窗口会藏到视频后面，
        表现就是「按了没反应」。
        """
        try:
            from PySide6.QtWidgets import QInputDialog
        except Exception as e:
            logger.warning(f"无法加载提问输入框: {str(e)[:120]}")
            return
        try:
            dlg = QInputDialog()
            dlg.setWindowTitle("随时提问（AI 立即讲解）")
            dlg.setLabelText("想问什么？（例如：这一步为什么变号）")
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
            dlg.resize(460, dlg.height())
            ok = dlg.exec()
            text = dlg.textValue()
        except Exception as e:
            logger.warning(f"提问输入框异常: {str(e)[:150]}")
            return
        if not ok:
            return
        self.ask_now(text)

    def ask_now(self, point: str) -> dict:
        """学生主动提问：走 engine.request_teaching（绕过决策闸门，受静音/冷却约束）。"""
        clean = str(point or "").strip()
        if not clean:
            self._toast("没听到问题内容", "再按一次热键，把问题写进去", "warning")
            return {"triggered": False, "gate": "empty_point"}
        try:
            result = self._engine.request_teaching(clean)
        except Exception as e:
            logger.error(f"提问触发失败: {type(e).__name__}: {str(e)[:200]}")
            self._toast("提问失败", f"{type(e).__name__}: {str(e)[:60]}", "error")
            return {"triggered": False, "gate": "exception"}
        if not isinstance(result, dict):
            return {"triggered": False, "gate": "bad_result"}
        if result.get("triggered"):
            self._toast("已收到问题", "正在生成讲解（右上角卡片，Ctrl+Alt+H 可关闭）", "info")
        else:
            gate = str(result.get("gate") or "")
            self._toast("暂时不能讲解", self._gate_text(gate), "warning")
            # 冷却拦截是对用户最费解的一种：他没有得到任何反馈，会以为热键坏了
            logger.info(f"学生提问被闸门拦截: gate={gate}")
        return result

    @staticmethod
    def _gate_text(gate: str) -> str:
        mapping = {
            "empty_point": "问题内容为空",
            "muted": "当前处于静音模式（先用静音热键解除）",
            "cooldown": "刚讲完一段，等冷却时间过去再问",
            "busy": "正在讲上一条，稍等几秒再问",
        }
        return mapping.get(str(gate or ""), f"被拒绝（{gate or '原因未知'}）")

    @staticmethod
    def _toast(title: str, message: str, level: str = "info"):
        """toast 是可选依赖：导入失败时只记日志，绝不能让提问链崩掉。"""
        try:
            from ui.toast import show_toast
            show_toast(title, message, level)
        except Exception as e:
            logger.warning(f"toast 不可用（{title}: {message}）: {str(e)[:100]}")

    def _on_tts_state(self, state: str):
        """TTS 状态跟踪（主线程槽）：playing 补足防护窗，idle 收尾余量。"""
        if state == "playing" and self._active_speech:
            try:
                if self._monitor is not None:
                    self._monitor.suppress_teacher(
                        max(4.0, len(self._active_speech) / 4.0 + 3.0))
            except Exception:
                pass
        elif state == "idle" and self._active_speech:
            self._active_speech = ""
            try:
                if self._monitor is not None:
                    self._monitor.suppress_teacher(2.0)   # 播放尾段余量
            except Exception:
                pass

    def shutdown(self):
        """应用退出清理：停轮询/热键、停 TTS、清卡片、回收讲解词线程。"""
        for listener in (self._hotkey_listener, self._ask_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
        self._hotkey_listener = None
        self._ask_listener = None
        try:
            self._poll_timer.stop()
        except RuntimeError:
            pass
        self._clear_timer.stop()
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None
        try:
            self._tts.shutdown()
        except Exception:
            pass
        self._clear_board()
        self._cleanup_speech_thread()

    # ---------- 联动主链 ----------

    def _on_trigger(self, payload: dict):
        try:
            self._handle_trigger(dict(payload or {}))
        except Exception as e:
            # 联动层任何异常都不能拖垮课堂流：记 Err 并降级为静默
            logger.error(f"课堂联动处理异常: {type(e).__name__}: {str(e)[:180]}")
            try:
                append_err_record("core/classroom_coach.py", "联动处理异常",
                                  f"{type(e).__name__}: {str(e)[:300]}")
            except Exception:
                pass

    def _handle_trigger(self, payload: dict):
        now = time.time()
        if now < self._busy_until:
            logger.info("上一条补讲仍在途，忽略新触发（重入守卫）")
            return
        point = str(payload.get("teach_point", "") or "").strip()[:200]
        if not point:
            logger.info("触发载荷无 teach_point，跳过联动")
            return
        self._last_source_student = (str(payload.get("source", "ai") or "ai") == "student")
        reason = str(payload.get("reason", "") or "").strip()[:300]
        speak = bool(payload.get("speak", True))

        # 1) 卡片先行：标题秒出，正文流式补
        self._draw_card_header(point)
        # 2) 自动清除定时（重入时先取消旧定时器再重启）
        self._clear_timer.stop()
        self._clear_timer.start(COACH_BOARD_SEC * 1000)
        self._busy_until = now + BUSY_TIMEOUT_SEC

        # 3) 讲解词（异步流式）→ 正文逐句上板 → 朗读
        self._start_speech_pipeline(point, reason, speak)

    # ---------- 卡片板书（右上角局部，不遮挡网课主内容） ----------

    def _draw_card(self, point: str, body_lines: list, gray_body: bool = False):
        """卡片指令：标题栏 + 正文区 + 底部热键提示。body_lines 为折行后文本。

        apply_ops 是**追加**语义（screen_paint 按序全量重绘），流式期间每
        0.4s 重绘一次卡片——必须先 clear_region 擦掉本卡片区域的旧指令，
        否则新旧文字层层叠印（用户实测反馈：文字堆在一起）。"""
        try:
            self._overlay.clear_region(
                CARD_X - 0.01, CARD_Y - 0.01,
                CARD_W + 0.02, CARD_HEADER_H + CARD_BODY_H + 0.04)
        except Exception as e:
            logger.warning(f"卡片区域擦除失败（继续叠加绘制）: {str(e)[:120]}")
        title_size = _clamp_px(_screen_geometry()[1] * 0.024, 16, 26)
        body_size = _clamp_px(_screen_geometry()[1] * 0.016, 13, 19)
        suffix = "（学生提问）" if self._last_source_student else ""
        # apply_ops 契约是**设计稿像素**坐标（内部按屏幕宽高归一）：
        # 必须传像素值，传比例坐标会被再次除以屏宽缩成一个点
        sw, sh = _screen_geometry()
        PX = lambda r, total: int(r * total)
        ops = [
            {"op": "paint_rect", "x": PX(CARD_X, sw), "y": PX(CARD_Y, sh),
             "w": PX(CARD_W, sw), "h": PX(CARD_HEADER_H, sh), "color": "#1b3a6b"},
            {"op": "write_text",
             "x": PX(CARD_X + 0.012, sw), "y": PX(CARD_Y + 0.012, sh),
             "text": f"AI 教师补讲：{point}{suffix}"[:40],
             "size": title_size, "color": "#ffffff"},
            {"op": "paint_rect", "x": PX(CARD_X, sw), "y": PX(CARD_Y + CARD_HEADER_H, sh),
             "w": PX(CARD_W, sw), "h": PX(CARD_BODY_H, sh), "color": "#F2FFFFFF"},
        ]
        y = PX(CARD_Y + CARD_HEADER_H + 0.022, sh)
        max_lines = int(CARD_BODY_H / 0.032)     # 正文区行数随高度
        for line in body_lines[:max_lines]:
            ops.append({"op": "write_text", "x": PX(CARD_X + 0.012, sw), "y": y,
                        "text": line, "size": body_size,
                        "color": "#999999" if gray_body else "#222222"})
            y += PX(0.032, sh)
        ops.append({"op": "write_text", "x": PX(CARD_X + 0.012, sw),
                    "y": PX(CARD_Y + CARD_HEADER_H + CARD_BODY_H - 0.028, sh),
                    "text": f"{BOARD_HIDE_HOTKEY.upper()} 关闭 · {COACH_BOARD_SEC}s 自动消失",
                    "size": 12, "color": "#aaaaaa"})
        self._overlay.apply_ops(ops, screen_w=sw, screen_h=sh)

    def _draw_card_header(self, point: str):
        self._draw_card(point, ["讲解词生成中…"], gray_body=True)

    def _draw_card_body(self, speech: str, point: str):
        sw, sh = _screen_geometry()
        body_size = _clamp_px(sh * 0.016, 13, 19)
        chars_per_line = max(12, int((CARD_W - 0.024) * sw / body_size))
        body_lines = _wrap_text(str(speech or "").strip()[:SPEECH_MAX_CHARS],
                                chars_per_line)
        self._draw_card(point, body_lines)

    def _clear_board(self):
        try:
            self._overlay.apply_ops([{"op": "clear"}], screen_w=1, screen_h=1)
        except Exception as e:
            logger.warning(f"清卡片失败（忽略）: {str(e)[:120]}")

    # ---------- 讲解词（流式）+ 朗读 ----------

    def _start_speech_pipeline(self, point: str, reason: str, speak: bool):
        messages = self._build_speech_prompt(point, reason)
        self._cleanup_speech_thread()
        self._speech_req += 1
        req_id = self._speech_req
        self._pending = (point, speak)
        self._speech_thread = QThread(self)
        worker = _SpeechWorker(messages, req_id)
        worker.moveToThread(self._speech_thread)
        self._speech_thread.started.connect(worker.run)
        worker.done.connect(self._speech_thread.quit)
        # 必须连绑定方法（FreqErr「信号生命周期」）：闭包无 receiver 是 direct
        # connection，槽会在 worker 子线程执行，跨线程操作 TTSPlayer/QWidget。
        # 绑定方法 → queued 回主线程执行（r56 实机 demo 复现并修复）。
        worker.partial.connect(self._on_speech_partial)
        worker.ready.connect(self._on_speech_ready)
        worker.failed.connect(self._on_speech_failed)
        self._speech_worker = worker   # 防 GC
        self._speech_thread.start()

    def _on_speech_partial(self, text: str, rid: int):
        """流式中间结果（主线程槽）：正文实时上板，不出声不防回环。"""
        if rid != self._speech_req or self._pending is None:
            return
        point, _ = self._pending
        self._draw_card_body(text, point)

    def _on_speech_ready(self, speech: str, rid: int):
        """讲解词完成（主线程槽）：终稿上板 + 防回环 + 朗读。"""
        if rid != self._speech_req or self._pending is None:
            return
        point, speak, = self._pending
        self._pending = None
        self._finish_pipeline(point, speech, speak)

    def _on_speech_failed(self, err: str, rid: int):
        """讲解词失败（主线程槽）：fail-safe 退回 teach_point 直读。"""
        if rid != self._speech_req or self._pending is None:
            return
        point, speak = self._pending
        self._pending = None
        logger.warning(f"讲解词生成失败，退回 teach_point 直读: {err[:150]}")
        self._finish_pipeline(point, point, speak)

    def _build_speech_prompt(self, point: str, reason: str) -> list:
        system = ("你是网课旁听的 AI 教师。学生此刻需要听一段简短补讲。"
                  "用口语化中文把要点讲清楚，40~80 字，"
                  "直接对着学生说话，只输出讲解词本身，不要任何前缀、标题或解释。")
        context = ""
        try:
            s = ConfigManager().settings
            window = max(1, int(getattr(s, "classroom_asr_window", 12) or 12))
            context = str(self._monitor.context_block(window) or "")[:1500]
        except Exception:
            context = ""
        parts = []
        topic = str(getattr(self._engine, "_topic", "") or "")
        if topic:
            parts.append(f"学习主题：{topic}")
        if context:
            parts.append(f"课堂上下文（[老师]=老师讲解，[学生]=学生提问）：\n{context}")
        parts.append(f"要补讲的要点：{point}")
        if reason:
            parts.append(f"触发原因：{reason}")
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    def _finish_pipeline(self, point: str, speech: str, speak: bool):
        """讲解词完成（或回退）后的收尾：终稿上板 + 防回环 + 朗读 + 成果登记。"""
        clean = str(speech or point or "").strip()[:SPEECH_MAX_CHARS]
        if not clean:
            clean = point
        self._draw_card_body(clean, point)
        # 成果互通（round57）：登记本次补讲，供定时同步到学习 Agent
        try:
            if self.teach_logger is not None:
                self.teach_logger(point, clean,
                                  "student" if self._last_source_student else "ai")
        except Exception as e:
            logger.warning(f"补讲登记失败（不影响教学）: {str(e)[:120]}")
        if not speak:
            logger.info("interrupt_speak=False，仅板书不朗读")
            self._busy_until = 0.0
            return
        # 回环防护：朗读会被 loopback 抓回 teacher 通道，先开丢弃窗口
        try:
            if self._monitor is not None:
                est = max(4.0, len(clean) / 4.0 + 2.0)
                self._monitor.suppress_teacher(est)
        except Exception as e:
            logger.warning(f"suppress_teacher 失败（继续朗读）: {str(e)[:120]}")
        try:
            self._tts.speak(clean, style_instruction="像老师讲课一样，口语自然，重点清晰")
            self._active_speech = clean
        except Exception as e:
            logger.error(f"TTS 朗读失败（板书已呈现）: {str(e)[:180]}")
        finally:
            self._busy_until = 0.0

    def _cleanup_speech_thread(self):
        t = self._speech_thread
        if t is not None:
            try:
                t.quit()
                t.wait(1500)
            except RuntimeError:
                pass
            self._speech_thread = None
            self._speech_worker = None
            self._pending = None
