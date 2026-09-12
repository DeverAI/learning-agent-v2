"""OISystem 主入口。

职责：
1. 启动主 QApplication
2. 初始化侧边栏 UI
3. 创建任务栏托盘图标（常驻）
4. 启动 watchdog 子进程（如启用）
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject, Signal, QThread, Slot

from utils.helpers import ensure_dirs, logger, append_err_record, ERR_MD_PATH
from config.settings import ConfigManager


def _install_global_exception_hook():
    """注册全局异常钩子，未捕获异常自动写入 Err.md。"""
    old_hook = sys.excepthook

    def _hook(etype, value, tb):
        import traceback
        tb_str = "".join(traceback.format_exception(etype, value, tb))
        module = getattr(etype, "__module__", str(etype))
        append_err_record(
            module=f"sys.excepthook.{module}",
            title=str(value)[:50],
            detail=tb_str[:120].replace("\n", " "),
        )
        logger.critical(f"Unhandled exception:\n{tb_str}")
        if old_hook:
            old_hook(etype, value, tb)

    sys.excepthook = _hook

    # 同时 hook Qt 的异常
    import warnings
    warnings.filterwarnings("always")

    # 补一个 Err.md 表头（如果还没有）
    if not os.path.exists(ERR_MD_PATH) or os.path.getsize(ERR_MD_PATH) < 10:
        header = """# Err — Bug / 问题追踪记录

> 自动记录运行时 Bug 和待修复问题。

## 自动记录

| 日期 | 模块 | 问题 | 详情 | 状态 |
|------|------|------|------|:----:|
"""
        os.makedirs(os.path.dirname(ERR_MD_PATH), exist_ok=True)
        with open(ERR_MD_PATH, "w", encoding="utf-8") as f:
            f.write(header)


def start_watchdog():
    """启动独立 watchdog 子进程。返回 Popen 对象供主进程持有引用。"""
    cfg = ConfigManager().settings
    if not cfg.watchdog_enabled:
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(__file__), "watchdog.py"),
             str(os.getpid())],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        logger.info(f"Watchdog started, pid={proc.pid}")
        return proc
    except Exception as e:
        logger.error(f"Failed to start watchdog: {e}")
        return None


class _ZzoiLockRecorder:
    """自动 ZZOI 检查在子线程记录锁定/解除请求，由主线程统一应用。"""

    def __init__(self):
        self.reasons = []
        self.release_requested = False

    def lock_for_zzoi(self, reason: str):
        self.reasons.append(reason)

    def force_release_if_locked(self, reason_prefix: str = "zzoi"):
        # 子AGENT终审 C-2：检测到当日提交后请求解除 ZZOI 锁定（主线程应用）
        self.release_requested = True


class _AutoZzoiWorker(QObject):
    """30 分钟自动 ZZOI 检查工作线程（round48：不再阻塞 UI 线程）。"""

    finished = Signal(dict)
    done = Signal()

    def run(self):
        try:
            from core.oj_tracker import ZzoiTracker
            tracker = ZzoiTracker()  # 每次检查独立 session，不与手动检查串扰
            recorder = _ZzoiLockRecorder()
            result = tracker.daily_check(focus_engine=recorder)
            result["_lock_reasons"] = recorder.reasons
            result["_release_requested"] = recorder.release_requested
            result["last_check_time"] = tracker._last_check_time
            self.finished.emit(result)
        except Exception as e:
            logger.warning(f"自动 ZZOI 检查线程异常: {e}")
        finally:
            try:
                self.done.emit()
            except RuntimeError:
                pass


class _AutoZzoiBridge(QObject):
    """主线程桥：把自动 ZZOI 检查结果 marshal 回主线程再操作 FocusEngine。

    round48 P1：worker.finished 若直连普通函数，会在 worker 线程内直接执行，
    导致在非主线程创建 QTimer/操作 FocusEngine（Qt 未定义行为）。
    这里由主线程创建 bridge，worker 信号经 AutoConnection 排队到 bridge 所在线程。
    """

    def __init__(self, zzoi_tracker, focus_engine, job_refs: list, thread, worker):
        super().__init__()
        self._zzoi_tracker = zzoi_tracker
        self._focus_engine = focus_engine
        self._job_refs = job_refs
        self._thread = thread
        self._worker = worker

    @Slot(dict)
    def _apply(self, result: dict):
        try:
            if result.get("last_check_time"):
                self._zzoi_tracker._last_check_time = result["last_check_time"]
        except Exception:
            pass
        for reason in (result.get("_lock_reasons") or []):
            try:
                self._focus_engine.lock_for_zzoi(reason)
            except Exception as e:
                logger.warning(f"自动 ZZOI 锁定应用失败 ({reason}): {e}")
        # 子AGENT终审 C-2：主线程应用"当日已提交 → 解除 ZZOI 锁定"
        if result.get("_release_requested"):
            try:
                self._focus_engine.force_release_if_locked()
            except Exception as e:
                logger.warning(f"自动 ZZOI 锁定解除应用失败: {e}")

    @Slot()
    def _cleanup(self):
        try:
            entry = (self._thread, self._worker, self)
            if entry in self._job_refs:
                self._job_refs.remove(entry)
        except Exception:
            pass
        try:
            self.deleteLater()
        except Exception:
            pass


def _start_auto_zzoi_check(zzoi_tracker, focus_engine, job_refs: list):
    """启动一次异步 ZZOI 每日检查；主线程只负责应用锁定结果。

    round48 P1：thread/worker/bridge 全部持有引用防止 GC；
    结果应用经主线程 bridge 排队执行，锁定不会在 worker 线程操作 Qt。
    """
    thread = QThread()
    worker = _AutoZzoiWorker()
    worker.moveToThread(thread)
    bridge = _AutoZzoiBridge(zzoi_tracker, focus_engine, job_refs, thread, worker)
    entry = (thread, worker, bridge)

    thread.started.connect(worker.run)
    worker.finished.connect(bridge._apply)
    worker.done.connect(thread.quit)
    worker.done.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(bridge._cleanup)
    job_refs.append(entry)
    thread.start()


def main():
    try:
        # 注册全局异常钩子（自动写入 Err.md）—— 必须在任何 import 之后尽早安装
        _install_global_exception_hook()

        ensure_dirs()
        logger.info("OISystem 启动")

        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)  # 关闭窗口不退出进程（托盘常驻）

        # 初始化配置
        cfg = ConfigManager()

        # 初始化全局 FocusEngine 单例
        from core.focus_engine import FocusEngine
        from ui.focus_view import set_global_engine
        focus_engine = FocusEngine()
        set_global_engine(focus_engine)

        # 初始化屏幕分析器并注入 FocusEngine
        from core.screen_analyzer import ScreenAnalyzer
        screen_analyzer = ScreenAnalyzer()
        focus_engine.set_screen_analyze_callback(screen_analyzer.analyze)
        # 异步分析结果回流到 FocusEngine（用于进展判定和卡住检测）
        screen_analyzer.analyze_completed.connect(focus_engine._on_screen_result)
        # round48：失败信号接入弹窗，消除 analyze_failed 只有 emit 无消费者
        from ui.toast import show_toast
        screen_analyzer.analyze_failed.connect(
            lambda reason: show_toast("屏幕分析失败", reason, "warning")
        )

        # 初始化 AI 对话引擎并注入 FocusEngine 卡住分析回调
        from core.ai_dialog import AIDialog
        from ui.dialog_view import set_global_dialog
        ai_dialog = AIDialog()
        set_global_dialog(ai_dialog)
        def _on_stuck(pid_desc, screen_result=None):
            # 卡住时主动分析：发送一条附带屏幕上下文的引导消息
            ctx = screen_result if isinstance(screen_result, dict) else None
            ai_dialog.send(
                f"用户在 {pid_desc} 卡住了，请基于屏幕上下文主动引导分析思路（不要给代码）。",
                screen_context=ctx,
            )
        focus_engine.set_ai_stuck_callback(_on_stuck)

        # 初始化 ZZOI 追踪器（零提交/排行榜末尾锁定检测）
        from core.oj_tracker import ZzoiTracker
        zzoi_tracker = ZzoiTracker()
        # ZZOI 每日自动检查（每 30 分钟，异步线程执行，避免网络请求冻结 UI）
        from PySide6.QtCore import QTimer
        zzoi_jobs = []
        zzoi_timer = QTimer()
        zzoi_timer.timeout.connect(
            lambda: _start_auto_zzoi_check(zzoi_tracker, focus_engine, zzoi_jobs)
        )
        zzoi_timer.start(30 * 60 * 1000)  # 30 分钟
        # 首次延迟 60 秒启动检查
        QTimer.singleShot(
            60000,
            lambda: _start_auto_zzoi_check(zzoi_tracker, focus_engine, zzoi_jobs)
        )

        # 启动 watchdog
        watchdog_proc = start_watchdog()
        
        # 从 ui.zzoi_view 全局单例共享 zzoi_tracker
        from ui.zzoi_view import set_global_tracker
        set_global_tracker(zzoi_tracker)

        # 初始化主侧边栏 UI（通过工厂函数，根据配置选择样式）
        from ui.sidebar_factory import create_sidebar
        sidebar = create_sidebar()
        sidebar.show()

        # 初始化任务栏托盘图标（常驻）
        from ui.tray import TrayController
        tray = TrayController()
        tray.double_clicked.connect(sidebar._expand)
        tray.show_settings.connect(sidebar._open_settings)
        tray.show_dialog.connect(sidebar._open_dialog)
        tray.quit_requested.connect(sidebar._exit_clicked)
        # R23：托盘「随时提问」——热键之外的第二入口（想不起来热键时用鼠标也能问）
        tray.ask_now.connect(lambda: _ask_now_from_ui())

        # 启动静音模式全局热键
        from core.mute_mode import start_global_hotkey
        start_global_hotkey(cfg.settings.mute_hotkey)

        # round56：课堂 AI 教师（感知 + 决策 + 联动）接线
        classroom = None
        try:
            from config.settings import ENABLE_CLASSROOM_AUDIO
        except ImportError:
            ENABLE_CLASSROOM_AUDIO = False
        if ENABLE_CLASSROOM_AUDIO and cfg.settings.classroom_audio_enabled:
            try:
                from core.classroom_stream import ClassroomMonitor
                from core.interrupt_engine import InterruptEngine
                from core.classroom_coach import ClassroomCoach
                from core.screen_paint import ScreenPaintOverlay
                from core.tts_player import TTSPlayer
                monitor = ClassroomMonitor()
                res = monitor.start()
                engine = InterruptEngine(monitor=monitor)
                overlay = ScreenPaintOverlay()
                coach = ClassroomCoach(monitor, engine, overlay, TTSPlayer())
                coach.connect()
                classroom = {"monitor": monitor, "engine": engine,
                             "coach": coach, "overlay": overlay}
                # round57：成果互通（默认关，用户在设置中心显式开启）
                if cfg.settings.classroom_sync_enabled:
                    from core.classroom_sync import ClassroomSync
                    sync = ClassroomSync(monitor, coach)
                    coach.teach_logger = sync.add_teach
                    sync.start()
                    classroom["sync"] = sync
                    logger.info("课堂笔记同步已启用")
                if res.get("ok"):
                    show_toast("课堂 AI 教师已开启",
                               "发现讲错/跳步会自动补讲；Ctrl+Alt+A 随时提问；静音热键可让 AI 闭嘴")
                    logger.info(f"课堂感知启动: {res.get('started')}")
                else:
                    show_toast("课堂感知降级",
                               f"音频通道启动失败({res.get('reason')})，"
                               "仍可 Ctrl+Alt+A 随时提问",
                               "warning")
                    logger.warning(f"课堂音频通道启动失败: {res}")
            except Exception as e:
                # 联动/设备初始化失败不阻断应用启动：记 Err 降级为无课堂感知
                append_err_record("main.py", "课堂感知初始化失败",
                                  f"{type(e).__name__}: {str(e)[:300]}")
                logger.error(f"课堂感知初始化失败: {e}", exc_info=True)
        else:
            # R23：音频感知关掉时**仍要保留「随时提问」**。
            # 学生主动打断不该依赖「AI 自动打断」的开关：即使完全不听课、不要
            # 自动补讲，他也要能在任何时刻按热键问一句。这条链用 monitor=None
            # 起步（request_teaching 本就只受静音/冷却约束，与感知无关）。
            try:
                from core.interrupt_engine import InterruptEngine
                from core.classroom_coach import ClassroomCoach
                from core.screen_paint import ScreenPaintOverlay
                from core.tts_player import TTSPlayer
                engine = InterruptEngine(monitor=None)
                overlay = ScreenPaintOverlay()
                coach = ClassroomCoach(None, engine, overlay, TTSPlayer())
                coach.connect()
                classroom = {"monitor": None, "engine": engine,
                             "coach": coach, "overlay": overlay}
                logger.info("课堂感知未开启：仅注册「随时提问」链路")
            except Exception as e:
                append_err_record("main.py", "随时提问链路初始化失败",
                                  f"{type(e).__name__}: {str(e)[:300]}")
                logger.error(f"随时提问链路初始化失败: {e}", exc_info=True)

        def _ask_now_from_ui():
            """托盘/UI 触发的随时提问；链路不可用时如实告知，不静默。"""
            if not classroom:
                show_toast("提问不可用", "提问链路未初始化，请查看日志", "warning")
                return
            try:
                classroom["coach"].prompt_ask()
            except Exception as e:
                logger.warning(f"打开提问框失败: {str(e)[:150]}")
                show_toast("提问失败", f"{type(e).__name__}", "error")

        def _shutdown_classroom():
            if classroom:
                try:
                    classroom["coach"].shutdown()
                    if "sync" in classroom:
                        classroom["sync"].shutdown()
                    mon = classroom.get("monitor")
                    if mon is not None:      # R23：仅提问链时 monitor 为 None
                        mon.stop()
                except Exception as e:
                    logger.warning(f"课堂感知清理异常: {str(e)[:150]}")

        app.aboutToQuit.connect(_shutdown_classroom)

        logger.info("OISystem 主界面就绪")
        exit_code = app.exec()
        logger.info(f"OISystem 退出, code={exit_code}")
        sys.exit(exit_code)
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
