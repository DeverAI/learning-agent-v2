"""OISystem ZZOI 状态窗口。

显示：
- 登录状态
- 当日提交数
- 作业/比赛列表
- 每日检查结果
- 手动触发检查按钮

round48：手动检查/重新登录全部改为 QThread 异步执行，网络请求不再冻结 UI。
"""
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTextEdit,
    QGroupBox
)
from PySide6.QtCore import Qt, QThread, QObject, Signal
from PySide6.QtGui import QFont

from ui.frame_mixin import RoundedFrameMixin
from config.settings import ConfigManager
from utils.helpers import logger


class _LockRecorder:
    """记录 daily_check 触发的锁定/解除请求，由主线程统一应用到真实引擎。"""

    def __init__(self):
        self.reasons = []
        self.release_requested = False

    def lock_for_zzoi(self, reason: str):
        self.reasons.append(reason)

    def force_release_if_locked(self, reason_prefix: str = "zzoi"):
        # 子AGENT终审 C-2：检测到当日提交后请求解除 ZZOI 锁定（主线程应用）
        self.release_requested = True


class _ZzoiWorker(QObject):
    """ZZOI 网络任务工作线程。"""

    finished = Signal(str, dict)  # (task, data)
    failed = Signal(str, str)     # (task, reason)
    done = Signal()

    def __init__(self, task: str):
        super().__init__()
        self._task = task  # "check" / "login"

    def run(self):
        try:
            from core.oj_tracker import ZzoiTracker
            tracker = ZzoiTracker()  # 每任务新建，避免跨线程共享 requests.Session
            if self._task == "login":
                ok = tracker.login()
                # round48：把登录成功后的真实会话 cookie 传回主线程，注入全局 tracker，
                # 避免"重新登录成功"只是临时实例的假成功，全局 session 仍是未登录态
                cookies = None
                try:
                    cookies = tracker._session.cookies.copy()
                except Exception:
                    cookies = None
                self.finished.emit("login", {
                    "ok": ok,
                    "cookies": cookies,
                    "last_check_time": tracker._last_check_time,
                })
                return
            # check：在子线程内完成登录/提交/排行榜/作业/比赛全部网络请求
            recorder = _LockRecorder()
            result = tracker.daily_check(focus_engine=recorder)
            result["_lock_reasons"] = recorder.reasons
            result["homework"] = tracker.fetch_homework_list()
            result["contest"] = tracker.fetch_contest_list()
            result["last_check_time"] = tracker._last_check_time
            self.finished.emit("check", result)
        except Exception as e:
            self.failed.emit(self._task, str(e))
        finally:
            try:
                self.done.emit()
            except RuntimeError:
                pass


class ZzoiView(QWidget, RoundedFrameMixin):
    """ZZOI 状态窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._apply_frame("ZZOI")
        self.setWindowTitle("OISystem - ZZOI")
        self.resize(460, 480)
        self._tracker = None
        self._busy = False
        self._thread = None
        self._worker = None
        self._setup_ui()
        self._bind_tracker()
        self._refresh()

    def _setup_ui(self):
        from ui.themes import ThemeManager
        self.setStyleSheet(ThemeManager().get_css())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        title = QLabel("ZZOI 状态")
        title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        title.setStyleSheet("background: transparent;")
        layout.addWidget(title)

        # 状态摘要
        status_group = QGroupBox("状态")
        sl = QVBoxLayout(status_group)
        self.status_label = QLabel("未检查")
        self.status_label.setFont(QFont("Microsoft YaHei", 10))
        sl.addWidget(self.status_label)
        layout.addWidget(status_group)

        # 详情
        detail_group = QGroupBox("详情")
        dl = QVBoxLayout(detail_group)
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setFont(QFont("Consolas", 10))
        dl.addWidget(self.detail_text)
        layout.addWidget(detail_group, 1)

        # 按钮
        btn_row = QHBoxLayout()
        self.check_btn = QPushButton("手动检查")
        self.check_btn.setObjectName("primary")
        self.check_btn.clicked.connect(self._manual_check)
        btn_row.addWidget(self.check_btn)
        self.login_btn = QPushButton("重新登录")
        self.login_btn.clicked.connect(self._login)
        btn_row.addWidget(self.login_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

    def _bind_tracker(self):
        try:
            self._tracker = _get_global_tracker()
        except Exception as e:
            logger.warning(f"绑定 ZzoiTracker 失败: {e}")

    def _refresh(self):
        if not self._tracker:
            self.status_label.setText("ZZOI 模块未就绪")
            return
        s = ConfigManager().settings
        if not s.zzoi_uid:
            self.status_label.setText("ZZOI 未配置 → 请打开「设置」→「ZZOI」Tab，填写信息后点击该 Tab 内的保存按钮（各设置 Tab 独立保存）")
            return
        self.status_label.setText(f"UID: {s.zzoi_uid} | 最后检查: {self._tracker._last_check_time or '未检查'}")

    # ---------- 异步任务调度 ----------
    def _start_task(self, task: str, busy_text: str):
        """启动一个 ZZOI 网络任务，防重复。"""
        if self._busy:
            return
        self._busy = True
        self.check_btn.setEnabled(False)
        self.login_btn.setEnabled(False)
        self.detail_text.setPlainText(busy_text)
        self._thread = QThread()
        self._worker = _ZzoiWorker(task)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._reset_thread_refs)
        self._thread.start()

    def _reset_thread_refs(self):
        self._thread = None
        self._worker = None
        self._busy = False
        try:
            self.check_btn.setEnabled(True)
            self.login_btn.setEnabled(True)
        except RuntimeError:
            pass

    def _on_worker_finished(self, task: str, data: dict):
        if task == "login":
            ok = bool(data.get("ok"))
            self.detail_text.setPlainText(
                "登录成功" if ok else "登录失败，请检查设置中的 UID/密码/sid"
            )
            if ok and self._tracker is not None:
                # 迁移真实会话 cookie，登录态才不是假的
                cookies = data.get("cookies")
                if cookies is not None:
                    try:
                        self._tracker._session.cookies = cookies
                    except Exception as e:
                        logger.warning(f"迁移 ZZOI 会话 cookie 失败: {e}")
                self._tracker._logged_in = True
            return
        # check
        try:
            self._render_check_result(data)
        except Exception as e:
            logger.warning(f"渲染 ZZOI 检查结果失败: {e}")
            self.detail_text.setPlainText(f"渲染检查结果失败: {e}")

    def _on_worker_failed(self, task: str, reason: str):
        self.detail_text.setPlainText(
            f"{'检查' if task == 'check' else '登录'}失败: {reason}"
        )

    def _render_check_result(self, result: dict):
        """主线程渲染检查结果，并把子线程记录的锁定请求应用到真实引擎。"""
        # 应用锁定：daily_check 在子线程里只记录，不直接操作主线程引擎
        for reason in (result.get("_lock_reasons") or []):
            try:
                from ui.focus_view import _get_global_engine
                _get_global_engine().lock_for_zzoi(reason)
            except Exception as e:
                logger.warning(f"应用 ZZOI 锁定失败 ({reason}): {e}")
        # 子AGENT终审 C-2：主线程应用"当日已提交 → 解除 ZZOI 锁定"
        if result.get("release_requested") or result.get("_release_requested"):
            try:
                from ui.focus_view import _get_global_engine
                _get_global_engine().force_release_if_locked()
            except Exception as e:
                logger.warning(f"应用 ZZOI 锁定解除失败: {e}")
        # 同步"最后检查时间"到全局 tracker，供状态栏展示
        if self._tracker is not None and result.get("last_check_time"):
            self._tracker._last_check_time = result["last_check_time"]

        lines = [
            f"检查时间: {result.get('last_check_time', '?')}",
            f"登录成功: {'是' if result.get('login_ok') else '否'}",
            f"抓取成功: {'是' if result.get('fetch_ok', True) else '否'}",
            f"当日有提交: {'是' if result.get('has_submission') else '否'}",
            f"排行榜末尾: {'是' if result.get('rank_tail') else '否'}",
            f"触发锁定: {'是' if result.get('locked') else '否'}",
            "",
            "作业列表:",
        ]
        for hw in (result.get("homework") or []):
            if isinstance(hw, dict):
                lines.append(f"  - {hw.get('id')}: {hw.get('title')} (截止 {hw.get('deadline')})")
        lines.append("")
        lines.append("比赛列表:")
        for c in (result.get("contest") or []):
            if isinstance(c, dict):
                lines.append(f"  - {c.get('id')}: {c.get('title')} (结束 {c.get('end_time')})")
        self.detail_text.setPlainText("\n".join(lines))
        self._refresh()

    # ---------- 按钮 ----------
    def _manual_check(self):
        """手动触发 ZZOI 检查（异步线程）。"""
        if not self._tracker:
            return
        self._start_task("check", "正在检查...")

    def _login(self):
        """重新登录 ZZOI（异步线程）。"""
        if not self._tracker:
            return
        self._start_task("login", "正在登录...")

    def closeEvent(self, event):
        # 窗口关闭时断开 worker 回调，避免销毁后被线程信号触发
        if self._worker is not None:
            try:
                self._worker.finished.disconnect(self._on_worker_finished)
                self._worker.failed.disconnect(self._on_worker_failed)
                self._worker.done.disconnect(self._on_worker_finished)
            except (TypeError, RuntimeError):
                pass
        super().closeEvent(event)


# ---------- 全局单例 ----------
_GLOBAL_TRACKER = None


def set_global_tracker(tracker):
    global _GLOBAL_TRACKER
    _GLOBAL_TRACKER = tracker


def _get_global_tracker():
    global _GLOBAL_TRACKER
    if _GLOBAL_TRACKER is None:
        from core.oj_tracker import ZzoiTracker
        _GLOBAL_TRACKER = ZzoiTracker()
    return _GLOBAL_TRACKER
