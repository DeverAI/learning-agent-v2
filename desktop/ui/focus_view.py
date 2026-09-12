"""OISystem 专注模式 UI。

包含：
- 倒计时显示
- 急事退出按钮
- 正常退出按钮（OI 模式=做题退出 v2：分配真实题目，AC 后放行；学习模式=直接结束）
"""
import time

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QFrame, QLineEdit
)
from PySide6.QtCore import Qt, QTimer, QThread, QObject, Signal, Slot, QUrl
from PySide6.QtGui import QFont, QColor, QDesktopServices

from ui.frame_mixin import RoundedFrameMixin
from ui.icons import SVG_BELL, SVG_BELL_OFF
from ui.buttons_svg import svg_pixmap
from utils.helpers import logger, format_time, now_cst
from utils.exceptions import FocusLockedError


class _ProblemWorker(QObject):
    """做题退出网络任务工作线程。

    task="pick": 从真实题目源挑一道题（比赛→作业→题库降级）
    task="check": 检查指定 pid 当日是否已有 AC 提交
    """

    finished = Signal(str, object)  # (task, data)
    failed = Signal(str, str)       # (task, reason)
    done = Signal()

    def __init__(self, task: str, pid: str = "", since_ts: int = 0):
        super().__init__()
        self._task = task
        self._pid = pid
        self._since_ts = int(since_ts or 0)

    def run(self):
        try:
            from core.oj_tracker import ZzoiTracker
            tracker = ZzoiTracker()  # 每任务独立会话，避免跨线程共享 requests.Session
            if self._task == "pick":
                problem = tracker.pick_problem(exclude_solved=True)
                self.finished.emit("pick", problem)
            elif self._task == "check":
                # 实测适配：display_pid 非空走精确过滤通道，否则走"分配后新 AC"
                result = tracker.check_solved(
                    display_pid=self._pid, since_ts=self._since_ts)
                self.finished.emit("check", result)
            else:
                self.failed.emit(self._task, f"未知任务 {self._task}")
        except Exception as e:
            self.failed.emit(self._task, str(e))
        finally:
            try:
                self.done.emit()
            except RuntimeError:
                pass


def _is_widget_alive(widget) -> bool:
    """r39 P0 修复：检查 QWidget 的 C++ 对象是否仍然存活。

    用于信号回调中防御性地检查窗口是否已在 closeEvent 中被销毁。
    信号可能在 disconnect 之前入队，事件派发时 self 已析构。
    """
    if widget is None:
        return False
    try:
        from shiboken6 import isValid
        return isValid(widget)
    except ImportError:
        # shiboken6 不可用时退化为 try/except RuntimeError 方式
        try:
            widget.isVisible()  # 触发 C++ 访问
            return True
        except RuntimeError:
            return False


class FocusView(QWidget, RoundedFrameMixin):
    """专注模式主窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_frame("专注模式")
        self.setWindowTitle("OISystem - 专注模式")
        self.resize(480, 360)
        self._engine = None
        self._bind_done = False
        self._setup_ui()
        self._bind_engine()
        # 刷新定时器（只在窗体可见时运行）
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._refresh)

    def _setup_ui(self):
        from ui.themes import ThemeManager
        self.setStyleSheet(ThemeManager().get_css())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # 铃铛状态图标 + 状态文字
        status_row = QHBoxLayout()
        status_row.setAlignment(Qt.AlignCenter)
        self.bell_icon = QLabel()
        self.bell_icon.setFixedSize(24, 24)
        self.bell_icon.setPixmap(svg_pixmap(SVG_BELL, 22, "#94a3b8"))
        status_row.addWidget(self.bell_icon)
        self.status_label = QLabel("未启动专注模式")
        self.status_label.setFont(QFont("Microsoft YaHei", 11))
        status_row.addWidget(self.status_label)
        layout.addLayout(status_row)

        # 模式选择器：OI / 学习文化课
        mode_row = QHBoxLayout()
        mode_row.setAlignment(Qt.AlignCenter)
        mode_label = QLabel("模式:")
        mode_row.addWidget(mode_label)
        from PySide6.QtWidgets import QComboBox
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("信息学奥赛 (OI)", "oi")
        self.mode_combo.addItem("学习文化课 (网课/读书)", "study")
        try:
            from config.settings import ConfigManager
            _cur_mode = ConfigManager().settings.focus_mode
        except Exception:
            _cur_mode = "oi"
        for i in range(self.mode_combo.count()):
            if self.mode_combo.itemData(i) == _cur_mode:
                self.mode_combo.setCurrentIndex(i)
                break
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)
        layout.addLayout(mode_row)

        self.countdown_label = QLabel("--:--:--")
        self.countdown_label.setFont(QFont("Consolas", 40, QFont.Bold))
        self.countdown_label.setAlignment(Qt.AlignCenter)
        self.countdown_label.setStyleSheet("background: transparent;")
        layout.addWidget(self.countdown_label)

        self.reason_label = QLabel("")
        self.reason_label.setAlignment(Qt.AlignCenter)
        self.reason_label.setObjectName("tip")
        layout.addWidget(self.reason_label)

        btn_row = QHBoxLayout()
        try:
            from config.settings import ConfigManager
            _default_dur = ConfigManager().settings.focus_duration_minutes or 30
        except Exception:
            _default_dur = 30
        self.start_btn = QPushButton(f"开始专注 ({_default_dur}分钟)")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._start_clicked)
        btn_row.addWidget(self.start_btn)

        self.normal_exit_btn = QPushButton("正常退出")
        self.normal_exit_btn.clicked.connect(self._normal_exit_clicked)
        self.normal_exit_btn.setVisible(False)
        btn_row.addWidget(self.normal_exit_btn)

        self.emergency_btn = QPushButton("急事退出 (仅退出专注)")
        self.emergency_btn.setObjectName("danger")
        self.emergency_btn.clicked.connect(self._emergency_clicked)
        self.emergency_btn.setVisible(False)
        btn_row.addWidget(self.emergency_btn)

        self.shutdown_btn = QPushButton("关机退出系统")
        self.shutdown_btn.setObjectName("danger")
        self.shutdown_btn.setStyleSheet(
            "QPushButton#danger { background: #7f0000; border: 2px solid #dc2626; color: white; }"
            "QPushButton#danger:hover { background: #b91c1c; border-color: #ef4444; }"
        )
        self.shutdown_btn.clicked.connect(self._shutdown_clicked)
        self.shutdown_btn.setVisible(False)
        btn_row.addWidget(self.shutdown_btn)
        layout.addLayout(btn_row)

        layout.addWidget(self._build_problem_panel())

        self.tip_label = QLabel("提示：急事退出=仅退专注不关机 | 做题退出=做掉一道真实题目后结束专注 | 关机退出=归档后关电脑")
        self.tip_label.setAlignment(Qt.AlignCenter)
        self.tip_label.setObjectName("tip")
        layout.addWidget(self.tip_label)

        # 根据当前模式刷新按钮文字/提示语
        self._refresh_button_text()

    def _build_problem_panel(self) -> QFrame:
        """做题退出面板：展示分配的真实题目与检测按钮（默认隐藏）。"""
        self.problem_panel = QFrame()
        self.problem_panel.setObjectName("problemPanel")
        self.problem_panel.setStyleSheet(
            "QFrame#problemPanel { border: 1px solid #334155; border-radius: 8px; background: transparent; }"
        )
        pl = QVBoxLayout(self.problem_panel)
        pl.setContentsMargins(12, 10, 12, 10)
        pl.setSpacing(6)

        self.problem_title_label = QLabel("题目：待分配")
        self.problem_title_label.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        self.problem_title_label.setWordWrap(True)
        self.problem_title_label.setStyleSheet("background: transparent;")
        pl.addWidget(self.problem_title_label)

        self.problem_meta_label = QLabel("")
        self.problem_meta_label.setObjectName("tip")
        self.problem_meta_label.setWordWrap(True)
        pl.addWidget(self.problem_meta_label)

        # 可选展示 ID（如 P918）：填写后走"该题 AC"精确检测；留空则检测分配后的新 AC
        pid_row = QHBoxLayout()
        pid_hint = QLabel("展示ID(可选):")
        pid_hint.setObjectName("tip")
        pid_row.addWidget(pid_hint)
        self.problem_pid_input = QLineEdit()
        self.problem_pid_input.setPlaceholderText("如 P918，可在作业页看到")
        self.problem_pid_input.setMinimumWidth(140)
        pid_row.addWidget(self.problem_pid_input)
        pid_row.addStretch(1)
        pl.addLayout(pid_row)

        row1 = QHBoxLayout()
        self.problem_open_btn = QPushButton("打开题目")
        self.problem_open_btn.setObjectName("primary")
        self.problem_open_btn.clicked.connect(self._open_problem_url)
        self.problem_open_btn.setEnabled(False)
        row1.addWidget(self.problem_open_btn)
        self.problem_check_btn = QPushButton("我已AC · 检测")
        self.problem_check_btn.clicked.connect(self._manual_check_problem)
        self.problem_check_btn.setEnabled(False)
        row1.addWidget(self.problem_check_btn)
        self.problem_refresh_btn = QPushButton("换一题")
        self.problem_refresh_btn.clicked.connect(self._pick_problem_again)
        self.problem_refresh_btn.setEnabled(False)
        row1.addWidget(self.problem_refresh_btn)
        self.problem_cancel_btn = QPushButton("继续专注")
        self.problem_cancel_btn.clicked.connect(self._cancel_problem_exit)
        row1.addWidget(self.problem_cancel_btn)
        row1.addStretch(1)
        pl.addLayout(row1)

        self.problem_status_label = QLabel("")
        self.problem_status_label.setObjectName("tip")
        self.problem_status_label.setWordWrap(True)
        pl.addWidget(self.problem_status_label)

        self.problem_panel.setVisible(False)

        # 异步任务状态（线程引用防 GC；_problem_job_id 供代次校验）
        self._problem_thread = None
        self._problem_worker = None
        self._problem_job_id = 0
        self._assigned_problem = None
        self._assigned_since_ts = 0  # 目标分配时刻（增量 AC 检测基准）
        # closeEvent 后置 True：已入队的迟到投递事件仍会执行槽（Qt 不撤队列），
        # 槽内据此直接丢弃，防止复活面板/定时器。
        self._problem_flow_closed = False

        # 轮询定时器：分配成功后面板可见期间每 60s 自动检测一次 AC
        self._problem_poll_timer = QTimer(self)
        self._problem_poll_timer.setInterval(60 * 1000)
        self._problem_poll_timer.timeout.connect(self._auto_check_problem)
        return self.problem_panel

    def _bind_engine(self):
        try:
            self._engine = _get_global_engine()
            # 只连接一次，避免重复连接/断开警告
            if self._bind_done:
                # r38 P1 修复：即使信号已连接，也要同步当前引擎状态。
                # 场景：用户关闭 FocusView 后重开，引擎仍在运行，但 UI 显示"未启动"。
                self._sync_state_from_engine()
                return
            self._engine.focus_started.connect(self._on_started)
            self._engine.focus_tick.connect(self._on_tick)
            self._engine.focus_ended.connect(self._reset_ui)
            self._engine.focus_emergency_exited.connect(self._on_emergency)
            self._engine.focus_stuck_ai_triggered.connect(self._on_stuck_ai)
            self._engine.focus_locked_for_zzoi.connect(self._on_zzoi_locked)
            self._engine.focus_auto_analyze_triggered.connect(self._on_auto_analyze)
            # 做题退出 v2 信号
            self._engine.focus_problem_exit_started.connect(self._on_problem_exit_started)
            self._engine.focus_problem_assigned.connect(self._on_problem_assigned)
            self._engine.focus_problem_status.connect(self._on_problem_status)
            self._engine.focus_problem_solved.connect(self._on_problem_solved)
            self._bind_done = True
            # r38 P1 修复：首次绑定后同步当前引擎状态。
            # 场景：FocusView 在专注模式运行中被关闭，重开时 __init__ → _bind_engine
            # 只连接信号不检查 is_active，导致用户看到倒计时但无退出按钮，无法退出专注。
            self._sync_state_from_engine()
        except Exception as e:
            logger.warning(f"绑定 FocusEngine 失败: {e}")

    def _sync_state_from_engine(self):
        """r38 P1 修复：从当前引擎状态同步 UI（专注进行中则显示退出按钮 + 倒计时）。

        场景：FocusView 关闭后重开，引擎仍在运行。原实现不检查 is_active，
        导致 UI 显示"未启动专注模式"但倒计时在走，退出按钮不可见。
        """
        if not self._engine or not self._engine.is_active:
            return
        try:
            total_sec = getattr(self._engine, "_total_seconds", 0)
            self._on_started(total_sec)
            self._on_tick(self._engine.remaining_seconds())
            # 做题退出 pending 态在窗口重开后恢复面板流程（否则点击退出按钮会被引擎静默忽略）
            if getattr(self._engine, "problem_exit_pending", False):
                assigned = getattr(self._engine, "assigned_problem", None)
                self._show_problem_panel()
                # 目标制：作业/比赛目标只有 key 无 pid，恢复时两者都认，
                # 否则会误判"未分配"而重新选题，覆盖引擎权威副本
                if isinstance(assigned, dict) and (assigned.get("pid") or assigned.get("key")):
                    # 已有权威题目：直接恢复，不重复选题
                    self._on_problem_assigned(assigned)
                else:
                    self.problem_status_label.setText(
                        "正在从 ZZOI 获取可用题目（比赛 → 作业 → 题库）…")
                    self._start_problem_task("pick")
            if self._engine.lock_reason:
                self.reason_label.setText(f"锁定原因: {self._engine.lock_reason or '无'}")
                # round48 P1：ZZOI 锁定期间重开窗口时，必须恢复"禁止退出"按钮态，
                # 否则 _on_started 已把关机/急事/正常退出按钮全部显示出来，可绕过锁定。
                self._apply_lock_ui((self._engine.lock_reason or "").startswith("zzoi"))
        except Exception as e:
            logger.warning(f"同步引擎状态失败: {e}")

    def showEvent(self, event):
        super().showEvent(event)
        self._ui_timer.start(500)

    def hideEvent(self, event):
        self._ui_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._ui_timer.stop()
        self._stop_problem_polling()
        self._problem_flow_closed = True  # 迟到的做题任务结果直接丢弃
        # 清理引擎绑定
        if self._bind_done and self._engine:
            try:
                self._engine.focus_started.disconnect(self._on_started)
            except Exception:
                pass
            try:
                self._engine.focus_tick.disconnect(self._on_tick)
            except Exception:
                pass
            try:
                self._engine.focus_ended.disconnect(self._reset_ui)
            except Exception:
                pass
            try:
                self._engine.focus_emergency_exited.disconnect(self._on_emergency)
            except Exception:
                pass
            try:
                self._engine.focus_stuck_ai_triggered.disconnect(self._on_stuck_ai)
            except Exception:
                pass
            try:
                self._engine.focus_locked_for_zzoi.disconnect(self._on_zzoi_locked)
            except Exception:
                pass
            try:
                self._engine.focus_auto_analyze_triggered.disconnect(self._on_auto_analyze)
            except Exception:
                pass
            for sig, slot in (
                (self._engine.focus_problem_exit_started, self._on_problem_exit_started),
                (self._engine.focus_problem_assigned, self._on_problem_assigned),
                (self._engine.focus_problem_status, self._on_problem_status),
                (self._engine.focus_problem_solved, self._on_problem_solved),
            ):
                try:
                    sig.disconnect(slot)
                except Exception:
                    pass
            self._bind_done = False
        # 断开在途做题任务回调，避免窗口销毁后被线程信号触发；
        # 并短暂等待在途线程收尾，防止退出时 "QThread: Destroyed while running"
        self._disconnect_problem_conns()
        if self._problem_thread is not None and self._problem_thread.isRunning():
            try:
                self._problem_thread.quit()
                self._problem_thread.wait(1500)
            except RuntimeError:
                pass
        super().closeEvent(event)

    def _on_mode_changed(self, idx: int):
        """模式切换时持久化到 ConfigManager，并同步所有已打开 FocusView 窗口。
        专注进行中禁止切换，避免运行中状态混乱。
        """
        try:
            if self._engine and self._engine.is_active:
                QMessageBox.warning(self, "禁止", "专注模式运行中无法切换模式，请先退出。")
                # 还原 combobox 到当前实际持久化 mode
                self._sync_mode_from_config()
                return
            mode = self.mode_combo.itemData(idx) or "oi"
            if not switch_focus_mode(mode):
                # 切换失败（理论上此时专注未运行，防御性恢复 UI）
                self._sync_mode_from_config()
        except Exception as e:
            logger.warning(f"切换模式失败: {e}")
        self._refresh_button_text()

    def _refresh_button_text(self):
        """根据当前模式刷新按钮文字。"""
        try:
            from config.settings import ConfigManager
            _dur = ConfigManager().settings.focus_duration_minutes or 30
            _mode = ConfigManager().settings.focus_mode
        except Exception:
            _dur, _mode = 30, "oi"
        self.start_btn.setText(f"开始{self._mode_label(_mode)} ({_dur}分钟)")
        if _mode == "study":
            self.normal_exit_btn.setText("正常退出")
        else:
            # pending 态保持"做题中…"，避免刷新文案覆盖进行中状态
            if not (self._engine and self._engine.problem_exit_pending):
                self.normal_exit_btn.setText("做题退出")
        self.tip_label.setText(self._tip_text(_mode))

    def _sync_mode_from_config(self):
        """从 ConfigManager 同步当前模式到 UI（供外部全局切换调用）。"""
        try:
            from config.settings import ConfigManager
            _mode = ConfigManager().settings.focus_mode
        except Exception:
            _mode = "oi"
        for i in range(self.mode_combo.count()):
            if self.mode_combo.itemData(i) == _mode:
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentIndex(i)
                self.mode_combo.blockSignals(False)
                break
        self._refresh_button_text()

    @staticmethod
    def _mode_label(mode: str) -> str:
        return "学习" if mode == "study" else "专注"

    @staticmethod
    def _tip_text(mode: str) -> str:
        if mode == "study":
            return "提示：学习模式 正常退出=直接结束 | 急事退出=仅退专注不关机 | 关机退出=归档后关电脑"
        return "提示：急事退出=仅退专注不关机 | 做题退出=做掉一道真实题目后结束专注 | 关机退出=归档后关电脑"

    def _start_clicked(self):
        if self._engine and not self._engine.is_active:
            try:
                self._engine.start()
            except Exception as e:
                logger.warning(f"启动专注模式失败: {e}")
                QMessageBox.warning(self, "启动失败", f"无法启动专注模式: {e}")

    def _normal_exit_clicked(self):
        """正常退出：OI 模式=做题退出 v2（引擎决定分支），学习模式=直接结束。"""
        self.normal_exit_btn.setEnabled(False)
        try:
            if not self._engine or not self._engine.is_active:
                return
            # r39 P1 修复：lock_reason 可能为 None
            if (self._engine.lock_reason or "").startswith("zzoi"):
                QMessageBox.warning(self, "禁止", "ZZOI 锁定中，必须提交成功才能退出。")
                return
            self._engine.request_normal_exit()
        finally:
            # pending 态下保持禁用（等待做题完成）；非 pending 恢复可点
            if not (self._engine and self._engine.problem_exit_pending):
                self.normal_exit_btn.setEnabled(True)

    # ---------- 做题退出 v2 ----------
    def _start_problem_task(self, task: str, pid: str = "", since_ts: int = 0):
        """启动做题相关网络任务（QThread 异步，防重复）。

        关键：信号必须连接到 self 的绑定方法（receiver 属于主线程，
        AutoConnection 自动排队回主线程执行）。若连接 lambda（无 receiver），
        Qt 会按直连处理，回调在 worker 线程直接跑并操作 QWidget——未定义行为。
        任务串行化（同一时刻最多一个），代次用 _problem_job_id 在槽内校验。
        """
        if self._problem_thread is not None and self._problem_thread.isRunning():
            logger.debug("做题任务已在进行中，忽略重复请求")
            return
        self._problem_job_id += 1
        self._problem_thread = QThread()
        self._problem_worker = _ProblemWorker(task, pid, since_ts)
        self._problem_worker.moveToThread(self._problem_thread)
        self._problem_worker.finished.connect(self._on_problem_worker_sig)
        self._problem_worker.failed.connect(self._on_problem_worker_fail_sig)
        self._problem_thread.started.connect(self._problem_worker.run)
        self._problem_worker.done.connect(self._problem_thread.quit)
        self._problem_worker.done.connect(self._problem_worker.deleteLater)
        self._problem_thread.finished.connect(self._problem_thread.deleteLater)
        self._problem_thread.finished.connect(
            lambda j=self._problem_job_id: self._reset_problem_thread_refs(j))
        self._problem_thread.start()

    def _problem_task_busy(self) -> bool:
        return (self._problem_thread is not None and self._problem_thread.isRunning())

    def _on_problem_worker_sig(self, sig_task: str, payload):
        """worker finished 信号入口（主线程排队执行）。"""
        if self._problem_flow_closed:
            return  # 窗口已关闭：丢弃已入队的迟到结果
        # 子AGENT审查修复：取消做题/会话已结束后，迟到的任务结果直接丢弃，
        # 防止面板隐藏期间轮询定时器被复活（后台打网络+写日志）
        if self._engine is None or not self._engine.is_active \
                or not getattr(self._engine, "problem_exit_pending", False):
            return
        self._on_problem_worker_finished(sig_task, payload)

    def _on_problem_worker_fail_sig(self, sig_task: str, reason: str):
        """worker failed 信号入口（主线程排队执行）。"""
        if self._problem_flow_closed:
            return
        if self._engine is None or not self._engine.is_active \
                or not getattr(self._engine, "problem_exit_pending", False):
            return
        self._on_problem_worker_failed(sig_task, reason)

    def _disconnect_problem_conns(self):
        """断开当前做题任务的全部信号（closeEvent 防迟到信号访问已销毁 UI）。"""
        if getattr(self, "_problem_worker", None) is None:
            return
        try:
            self._problem_worker.finished.disconnect(self._on_problem_worker_sig)
        except (TypeError, RuntimeError):
            pass
        try:
            self._problem_worker.failed.disconnect(self._on_problem_worker_fail_sig)
        except (TypeError, RuntimeError):
            pass

    def _reset_problem_thread_refs(self, job: int):
        if job != self._problem_job_id:
            return
        self._problem_thread = None
        self._problem_worker = None

    def _set_problem_buttons_enabled(self, pick: bool, check: bool):
        try:
            self.problem_refresh_btn.setEnabled(pick)
            self.problem_check_btn.setEnabled(check)
            self.problem_open_btn.setEnabled(bool(self._assigned_problem))
        except RuntimeError:
            pass

    def _show_problem_panel(self):
        """面板复位并显示（不含选题动作）。"""
        self._problem_flow_closed = False
        self._stop_problem_polling()
        self._assigned_problem = None
        self.problem_title_label.setText("题目：正在分配…")
        self.problem_meta_label.setText("")
        self.problem_status_label.setText("")
        self.problem_panel.setVisible(True)
        self.normal_exit_btn.setEnabled(False)
        self.normal_exit_btn.setText("做题中…")
        self._set_problem_buttons_enabled(pick=False, check=False)

    def _on_problem_exit_started(self):
        """进入做题退出 pending 态：显示面板并异步选题。"""
        if not _is_widget_alive(self):
            return
        self._show_problem_panel()
        self.problem_status_label.setText("正在从 ZZOI 获取可用题目（比赛 → 作业 → 题库）…")
        self._start_problem_task("pick")

    def _source_text(self, problem: dict) -> str:
        src = problem.get("source", "")
        name = {"contest": "比赛", "homework": "作业", "problemset": "题库"}.get(src, src or "ZZOI")
        contest = problem.get("contest_title") or ""
        meta = f"来源：{name}"
        if contest:
            meta += f" · {contest}"
        note = problem.get("source_note") or ""
        if note:
            meta += f"\n⚠ {note}"
        return meta

    def _on_problem_assigned(self, problem: dict):
        """真实目标（作业/比赛/题库题）已分配，展示给用户并启动自动轮询。"""
        if not _is_widget_alive(self):
            return
        if not isinstance(problem, dict) or \
                not (problem.get("pid") or problem.get("key")):
            return
        self._assigned_problem = problem
        # 记录分配时刻：增量 AC 检测的基准
        self._assigned_since_ts = int(time.time())
        title = str(problem.get("title") or problem.get("key") or "")
        count = int(problem.get("count") or 0)
        if count > 1:
            title = f"{title}（共 {count} 题，任选其一）"
        self.problem_title_label.setText(f"目标：{title}")
        meta = self._source_text(problem)
        url = str(problem.get("url") or "")
        if url:
            meta += f"\n{url}"
        self.problem_meta_label.setText(meta)
        try:
            self.problem_pid_input.clear()
            self.problem_pid_input.setEnabled(True)
        except RuntimeError:
            pass
        self.problem_status_label.setText(
            "完成后点「我已AC · 检测」；填了展示ID则按该题精确检测，"
            "留空则检测分配之后的新 AC。")
        self._set_problem_buttons_enabled(pick=True, check=True)
        self._stop_problem_polling()
        self._problem_poll_timer.start()

    def _on_problem_status(self, text: str):
        if not _is_widget_alive(self):
            return
        self.problem_status_label.setText(str(text))

    def _on_problem_solved(self, problem: dict):
        """AC 达成：提示后由 focus_ended 统一复位 UI。"""
        if not _is_widget_alive(self):
            return
        self._stop_problem_polling()
        pid = (problem or {}).get("pid", "")
        QMessageBox.information(
            self, "做题完成",
            f"{pid} 已检测到 AC，本次专注结束。\n做得好！")

    def _open_problem_url(self):
        url = (self._assigned_problem or {}).get("url", "")
        if not url:
            return
        try:
            QDesktopServices.openUrl(QUrl(url))
        except Exception as e:
            logger.warning(f"打开题目链接失败: {e}")

    def _manual_check_problem(self):
        if not self._assigned_problem:
            return
        # 子AGENT审查修复：任务串行中不重复发起，也不改变按钮态（避免假死）
        if self._problem_task_busy():
            self.problem_status_label.setText("上一次检测仍在进行，请稍候…")
            return
        display_pid = ""
        try:
            display_pid = self.problem_pid_input.text().strip()
        except RuntimeError:
            pass
        since_ts = int(getattr(self, "_assigned_since_ts", 0) or 0)
        self.problem_status_label.setText("正在检测提交记录…")
        self._set_problem_buttons_enabled(pick=False, check=False)
        self._start_problem_task("check", pid=display_pid, since_ts=since_ts)

    def _auto_check_problem(self):
        if not self.problem_panel.isVisible() or not self._assigned_problem:
            return
        # 自动轮询只在用户填了展示ID时按题精确检测；
        # 未填时不自动判"新 AC"（避免用户在别处 AC 被误放行），仅手动触发
        try:
            if not self.problem_pid_input.text().strip():
                return
        except RuntimeError:
            pass
        self._manual_check_problem()

    def _pick_problem_again(self):
        if self._problem_task_busy():
            self.problem_status_label.setText("上一个任务仍在进行，请稍候…")
            return
        self._assigned_problem = None
        self.problem_open_btn.setEnabled(False)
        self.problem_title_label.setText("题目：正在分配…")
        self.problem_meta_label.setText("")
        self.problem_status_label.setText("正在重新选题…")
        self._set_problem_buttons_enabled(pick=False, check=False)
        self._start_problem_task("pick")

    def _cancel_problem_exit(self):
        """取消做题流程回到专注状态。"""
        self._stop_problem_polling()
        self.problem_panel.setVisible(False)
        self._assigned_problem = None
        try:
            if self._engine is not None:
                # 先清引擎 pending，_refresh_button_text 才能把"做题中…"恢复为"做题退出"
                self._engine.cancel_problem_exit()
        except Exception as e:
            logger.warning(f"取消做题退出失败: {e}")
        self.normal_exit_btn.setEnabled(True)
        self._refresh_button_text()

    def _on_problem_worker_finished(self, task: str, data):
        if self._problem_flow_closed:
            return  # 窗口已关闭：丢弃已入队的迟到结果
        if not _is_widget_alive(self):
            return
        if task == "pick":
            # 引擎保存权威副本并发信号 → _on_problem_assigned 更新面板
            try:
                if self._engine is not None:
                    self._engine.assign_problem(data if isinstance(data, dict) else None)
            except Exception as e:
                logger.warning(f"回填引擎目标失败: {e}")
            if isinstance(data, dict) and (data.get("pid") or data.get("key")):
                self._on_problem_assigned(data)
            else:
                self.problem_status_label.setText(
                    "未获取到可用题目（ZZOI 未登录/网络失败/无进行中作业与比赛）。\n"
                    "可点「换一题」重试；急事退出与关机退出仍可用。")
                self._set_problem_buttons_enabled(pick=True, check=False)
            return
        # check：check_solved 返回 {solved, fetch_ok, mode, rid?, ts?}
        info = data if isinstance(data, dict) else {}
        if info.get("solved"):
            try:
                if self._engine is not None:
                    self._engine.confirm_problem_solved(self._assigned_problem or {})
            except Exception as e:
                logger.error(f"确认做题完成失败: {e}")
                self.problem_status_label.setText(f"放行退出失败: {e}")
            return
        if info.get("fetch_ok") is False:
            self.problem_status_label.setText("ZZOI 抓取失败，稍后将自动重试。")
        else:
            mode = info.get("mode") or ""
            tip = ("该题尚未检测到 AC" if mode == "pid"
                   else "分配之后还没有新的 AC")
            self.problem_status_label.setText(
                f"{format_time(now_cst())[11:16]} 检测完成：{tip}，继续加油！")
        self._set_problem_buttons_enabled(pick=True, check=True)

    def _on_problem_worker_failed(self, task: str, reason: str):
        if self._problem_flow_closed:
            return
        if not _is_widget_alive(self):
            return
        logger.warning(f"做题任务 {task} 失败: {reason}")
        if task == "pick":
            self.problem_status_label.setText(f"选题失败：{reason[:60]}\n可点「换一题」重试。")
            self._set_problem_buttons_enabled(pick=True, check=False)
        else:
            self.problem_status_label.setText(f"检测失败：{reason[:60]}\n稍后将自动重试。")
            self._set_problem_buttons_enabled(pick=True, check=True)

    def _stop_problem_polling(self):
        try:
            self._problem_poll_timer.stop()
        except RuntimeError:
            pass

    def _emergency_clicked(self):
        """急事退出（仅退出专注模式，不关机）。退出前归档日志。"""
        self.emergency_btn.setEnabled(False)
        try:
            if not self._engine or not self._engine.is_active:
                return
            from config.settings import ConfigManager
            if not ConfigManager().settings.focus_emergency_exit_allowed:
                QMessageBox.warning(self, "禁止", "急事退出已被设置禁用。")
                return
            # r39 P1 修复：lock_reason 可能为 None
            if (self._engine.lock_reason or "").startswith("zzoi"):
                QMessageBox.warning(self, "禁止", "ZZOI 锁定中，必须提交成功才能退出。")
                return
            reply = QMessageBox.question(
                self, "急事退出（不关机）",
                "确定要急事退出专注模式吗？\n\n"
                "注意：仅退出专注模式，不会关机。\n"
                "退出前会自动归档日志和截图。\n"
                "此行为会记入日志。",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                # 先归档数据
                try:
                    from core.log_sync import sync_all
                    sync_all()
                except Exception:
                    pass
                self._engine.emergency_exit("用户主动急事退出")
        finally:
            self.emergency_btn.setEnabled(True)

    def _shutdown_clicked(self):
        """关机退出系统：归档全部数据后关机。先禁用按钮防重复弹窗。"""
        self.shutdown_btn.setEnabled(False)
        try:
            reply = QMessageBox.warning(
                self, "即将关机",
                "⚠ 警告：系统将关机！\n\n"
                "操作流程：\n"
                "  1. 结束当前专注模式\n"
                "  2. 归档全部日志、对话记录、截图\n"
                "  3. 退出程序\n"
                "  4. 关闭计算机\n\n"
                "请确保已保存所有其他应用的工作！\n"
                "确定要继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.Yes:
                from core.exit_flow import request_emergency_shutdown
                self.close()
                request_emergency_shutdown()
        finally:
            self.shutdown_btn.setEnabled(True)

    def _on_started(self, total_sec):
        self.start_btn.setVisible(False)
        self.normal_exit_btn.setVisible(True)
        self.emergency_btn.setVisible(True)
        self.shutdown_btn.setVisible(True)
        self.status_label.setText("专注进行中...")
        self.bell_icon.setPixmap(svg_pixmap(SVG_BELL_OFF, 22, "#ef4444"))
        self.setWindowModality(Qt.ApplicationModal)
        # 专注进行中禁用模式切换
        if hasattr(self, "mode_combo"):
            self.mode_combo.setEnabled(False)
        if self._engine:
            self.reason_label.setText(f"锁定原因: {self._engine.lock_reason or '无'}")
            # round48 P1：ZZOI 锁定下任何退出按钮都不可用，锁定状态始终生效。
            self._apply_lock_ui((self._engine.lock_reason or "").startswith("zzoi"))

    def _apply_lock_ui(self, locked: bool):
        """ZZOI 锁定态按钮管控：锁定期间隐藏正常/急事/关机退出按钮与做题面板。"""
        if locked:
            self.normal_exit_btn.setVisible(False)
            self.emergency_btn.setVisible(False)
            self.shutdown_btn.setVisible(False)
            self._stop_problem_polling()
            self.problem_panel.setVisible(False)
            self.status_label.setText("ZZOI 锁定中 — 提交后方可退出")

    def _on_tick(self, rem):
        # P2 修复：clamp 负数，避免 Python 向下取整导致显示负时间（如 -1 → "-1:59:59"）
        if rem <= 0:
            self.countdown_label.setText("00:00:00")
            return
        h = rem // 3600
        m = (rem % 3600) // 60
        s = rem % 60
        self.countdown_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _on_emergency(self, reason):
        # r39 P0 修复：信号可能在 closeEvent disconnect 前入队，回调时 self 已销毁
        if not _is_widget_alive(self):
            return
        self._reset_ui()
        QMessageBox.information(self, "已退出", f"急事退出: {reason}\n已记入日志。")

    def _on_stuck_ai(self, pid_desc, screen_result=None):
        # P1 修复：信号可能在 closeEvent disconnect 前入队，回调时 self 已销毁
        if not _is_widget_alive(self):
            return
        self.tip_label.setText(f"检测到卡住，AI 正在分析: {pid_desc}")

    def _on_zzoi_locked(self, reason):
        msg = {
            "zzoi_no_submit": "检测到当日 ZZOI 任务零提交，已自动锁定专注模式",
            "zzoi_rank_tail": "检测到排行榜末尾，已自动锁定专注模式",
        }.get(reason, f"ZZOI 锁定: {reason}")
        # r39 P0 修复：信号可能在 closeEvent disconnect 前入队，回调时 self 已销毁。
        # 仅当窗口存活时才更新窗口内控件；弹窗用无父窗口，保证 FocusView 已关闭时用户仍能看到锁定通知。
        if _is_widget_alive(self):
            self.status_label.setText(msg)
            self.reason_label.setText(f"锁定原因: {reason}")
            # ZZOI 锁定下禁用急事/正常退出/关机退出
            self._apply_lock_ui(True)
        QMessageBox.warning(None, "专注模式已锁定",
            f"{msg}\n\n必须提交 ZZOI 题目后才能解除锁定。\n提示：提交后请稍候，系统会自动检测到并释放。",
            QMessageBox.Ok)

    def _on_auto_analyze(self, result):
        # P1 修复：信号可能在 closeEvent disconnect 前入队，回调时 self 已销毁
        if not _is_widget_alive(self):
            return
        # round48：信号回调防御非 dict 的异常结果
        if not isinstance(result, dict):
            return
        eff = result.get("efficiency", "?")
        act = result.get("activity", "?")
        # 外部 AI 聊天提醒（仅当 reminder 字段存在时）
        ext = result.get("_external_ai_reminder")
        if ext:
            self.tip_label.setText(
                f"⚠️ 检测到你正在用外部 AI（{ext.get('activity','')[:30]}），请专注本地 OISystem"
            )
            try:
                from utils.helpers import log_event
                log_event("external_ai_reminder_shown", ext)
            except Exception:
                pass
        else:
            self.tip_label.setText(f"自动分析: 活动={act}, 效率={eff}")

    def _reset_ui(self):
        self.start_btn.setVisible(True)
        self.normal_exit_btn.setVisible(True)
        self.emergency_btn.setVisible(False)
        self.shutdown_btn.setVisible(False)
        self.status_label.setText("未启动专注模式")
        self.bell_icon.setPixmap(svg_pixmap(SVG_BELL, 22, "#94a3b8"))
        self.countdown_label.setText("--:--:--")
        self.reason_label.setText("")
        # 做题退出面板复位
        self._stop_problem_polling()
        self.problem_panel.setVisible(False)
        self._assigned_problem = None
        try:
            from config.settings import ConfigManager
            _mode = ConfigManager().settings.focus_mode
        except Exception:
            _mode = "oi"
        self.tip_label.setText(self._tip_text(_mode))
        self.setWindowModality(Qt.NonModal)
        # 退出后恢复模式选择
        if hasattr(self, "mode_combo"):
            self.mode_combo.setEnabled(True)
        # 同步按钮文案（学习/OI 模式）并恢复未锁定态
        self.normal_exit_btn.setEnabled(True)
        self._refresh_button_text()
        self._apply_lock_ui(False)

    def _refresh(self):
        """UI 刷新：更新倒计时，并在信号未触发的极端情况下兜底复位。"""
        if self._engine and self._engine.is_active:
            rem = self._engine.remaining_seconds()
            self._on_tick(rem)
            if rem <= 0 and not (self._engine.lock_reason or "").startswith("zzoi"):
                # 先通知引擎自然完成，再复位 UI，避免引擎状态与 UI 不一致。
                # ZZOI 锁定为名义一年倒计时，禁止走自然完成（与 _tick 守卫一致）。
                try:
                    self._engine._normal_complete()
                except Exception:
                    pass
                self._reset_ui()


# ---------- 全局单例 ----------
_GLOBAL_ENGINE = None


def set_global_engine(engine):
    global _GLOBAL_ENGINE
    _GLOBAL_ENGINE = engine


def _get_global_engine():
    global _GLOBAL_ENGINE
    if _GLOBAL_ENGINE is None:
        from core.focus_engine import FocusEngine
        _GLOBAL_ENGINE = FocusEngine()
    return _GLOBAL_ENGINE


def switch_focus_mode(mode: str) -> bool:
    """全局切换 focus_mode，并同步所有已打开的 FocusView 窗口。

    返回是否成功切换。若当前已有专注模式运行，则不切换并返回 False。
    """
    if mode not in ("oi", "study"):
        return False
    try:
        from PySide6.QtWidgets import QApplication
        from config.settings import ConfigManager
        cfg = ConfigManager()
        # 检查是否有运行中的专注模式
        if _GLOBAL_ENGINE is not None and _GLOBAL_ENGINE.is_active:
            logger.warning("专注模式运行中，拒绝外部模式切换")
            return False
        cfg.settings.focus_mode = mode
        cfg.save()
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, FocusView):
                widget._sync_mode_from_config()
        logger.info(f"全局切换 focus_mode -> {mode}")
        return True
    except Exception as e:
        logger.warning(f"全局切换模式失败: {e}")
        return False
