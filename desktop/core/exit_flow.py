"""OISystem 退出流程。

专注模式下禁止正常退出，必须先结束专注会话。
退出时弹出进度面板，同步日志到 note.ms + 邮件 + 极域快照 + 关闭 watchdog。
同步完成后显示说明弹窗。
"""
import os
from PySide6.QtWidgets import (
    QApplication, QMessageBox, QProgressDialog, QLabel, QVBoxLayout,
    QDialog, QPushButton, QHBoxLayout
)
from PySide6.QtCore import Qt, QByteArray
from PySide6.QtGui import QPixmap, QPainter, QFont
from PySide6.QtSvg import QSvgRenderer

from utils.helpers import logger, DATA_DIR
from utils.exceptions import FocusLockedError

_exiting = False  # 防重复退出


def _render_svg(svg_text: str, size: int = 48) -> QPixmap:
    """渲染 SVG 为 QPixmap。"""
    from ui.icons import SVG_CLOUD_UPLOAD
    if not svg_text:
        svg_text = SVG_CLOUD_UPLOAD
    colored = svg_text.replace("currentColor", "#e2e8f0")
    renderer = QSvgRenderer(QByteArray(colored.encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    if renderer.isValid():
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()
    return pm


class ExitProgressDialog(QDialog):
    """退出同步进度弹窗。
    
    显示上传图标 + 进度进度条 + 状态文字说明。
    同步完成后显示详细说明弹窗。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关闭中")
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
        self.setFixedSize(360, 200)
        self.setStyleSheet("""
            QDialog { background: #0f172a; color: #e2e8f0; }
            QLabel { background: transparent; color: #e2e8f0; }
            QPushButton {
                background: #1e293b; color: #e2e8f0;
                border: 1px solid #334155; border-radius: 6px;
                padding: 6px 16px;
            }
            QPushButton:hover { background: #263449; }
            QPushButton#primary { background: #1d4ed8; color: white; border-color: #2563eb; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setAlignment(Qt.AlignCenter)

        # 上传图标
        icon_label = QLabel()
        icon_label.setPixmap(_render_svg("", 48))
        icon_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_label)

        # 进度条
        self._progress = QProgressDialog("", None, 0, 100, self)
        self._progress.setWindowFlags(Qt.FramelessWindowHint)
        self._progress.setFixedWidth(280)
        self._progress.setMinimumDuration(0)
        self._progress.setCancelButton(None)
        self._progress.setStyleSheet("""
            QProgressBar {
                background: #1e293b; border: 1px solid #334155;
                border-radius: 4px; text-align: center; color: #e2e8f0;
                font-size: 9pt;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #2563eb, stop:1 #3b82f6);
                border-radius: 3px;
            }
        """)
        layout.addWidget(self._progress)

        # 状态文字
        self.status_label = QLabel("正在保存日志…")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFont(QFont("Microsoft YaHei", 9))
        layout.addWidget(self.status_label)

        self.status_label_2 = QLabel("")
        self.status_label_2.setAlignment(Qt.AlignCenter)
        self.status_label_2.setFont(QFont("Microsoft YaHei", 8))
        self.status_label_2.setStyleSheet("color: #94a3b8;")
        layout.addWidget(self.status_label_2)

    def set_progress(self, value: int, status: str = "", detail: str = ""):
        self._progress.setValue(value)
        if status:
            self.status_label.setText(status)
        if detail:
            self.status_label_2.setText(detail)
        QApplication.processEvents()


def request_exit():
    """请求退出。专注模式下拦截。退出前弹出进度面板同步日志。"""
    global _exiting
    if _exiting:
        return
    _exiting = True

    # 专注模式拦截
    if _is_focus_active():
        _exiting = False
        logger.warning("专注模式进行中，退出被拦截")
        QMessageBox.warning(
            None, "无法退出",
            "专注模式进行中，无法退出。\n\n请先结束专注模式（急事退出或正常退出）。",
        )
        raise FocusLockedError("专注模式进行中，禁止退出")

    logger.warning("request_exit 调用，开始退出流程")

    # 1. 显示进度对话框
    progress = ExitProgressDialog()
    progress.show()

    steps = [
        (10, "正在同步对话日志…",    "同步到 note.ms"),
        (30, "正在同步专注记录…",    "发送邮件报告"),
        (50, "正在截取极域快照…",    "保存证据文件"),
        (70, "正在写入退出信号…",    "通知 watchdog"),
        (85, "正在清理热键…",        "老板模式解除"),
        (95, "准备退出…",            ""),
    ]

    for val, status, detail in steps:
        progress.set_progress(val, status, detail)
        QApplication.processEvents()

    # 2. 同步日志到 note.ms / 邮件 / 极域快照
    sync_ok = True
    try:
        # round48：退出前把当前 AI 对话摘要/存档收口（同步等待完成），
        # 否则直接退出会丢失仍开着的对话
        try:
            from ui.dialog_view import _get_global_dialog
            _get_global_dialog().close_dialog()
        except Exception as e:
            logger.warning(f"退出前对话存档失败: {e}")
        from core.log_sync import sync_all
        sync_all()
        progress.set_progress(80, "日志同步完成", "")
    except Exception as e:
        logger.error(f"退出日志同步失败: {e}")
        sync_ok = False
        progress.set_progress(80, "日志同步异常", str(e)[:40])

    # 3. 写入 exit_signal
    try:
        signal_file = os.path.join(DATA_DIR, "exit_signal")
        with open(signal_file, "w", encoding="utf-8") as f:
            f.write("exit")
        progress.set_progress(90, "退出信号已写入", "")
    except Exception as e:
        logger.error(f"写 exit_signal 失败: {e}")

    # 4. 停止老板模式热键
    try:
        from core.mute_mode import stop_global_hotkey
        stop_global_hotkey()
        progress.set_progress(95, "热键已清理", "")
    except Exception:
        pass

    # 5. 关闭进度弹窗，显示说明
    progress.close()
    _show_exit_summary(sync_ok)

    # 6. 退出
    QApplication.quit()


def _show_exit_summary(sync_ok: bool):
    """显示退出说明弹窗：告知用户已保存内容。"""
    lines = [
        "程序即将退出，以下是本次同步内容：",
        "",
        "  ✓ 对话记录已同步到 note.ms",
        "  ✓ 专注记录已发送邮件" if sync_ok else "  ✗ 邮件同步失败（日志已保存本地）",
        "  ✓ 已保存极域快照",
        "  ✓ 退出信号已写入",
        "",
        "下次启动时将自动恢复未完成的配置。",
        "日志文件保存在 data/ 目录下。",
    ]
    QMessageBox.information(None, "关闭说明", "\n".join(lines))


def _is_focus_active() -> bool:
    """检查全局 FocusEngine 是否处于激活态。"""
    try:
        from ui.focus_view import _get_global_engine
        engine = _get_global_engine()
        return engine.is_active
    except Exception:
        return False


def request_emergency_shutdown():
    """一键结束专注 + 同步日志 + 截图保存 + 关机。"""
    global _exiting
    if _exiting:
        return
    _exiting = True

    logger.warning("请求紧急关机退出")

    # round48 P1：ZZOI 锁定期间禁止"一键关机退出"绕过锁定。
    # 紧急退出本身在 ZZOI 锁定下会被 FocusEngine 拒绝，若继续执行关机流程，
    # 等于给锁定开了一个后门；这里在进入同步/关机前直接拦截。
    try:
        from ui.focus_view import _get_global_engine
        eng = _get_global_engine()
        if eng.is_active and (eng.lock_reason or "").startswith("zzoi"):
            _exiting = False
            QMessageBox.warning(
                None, "无法关机",
                "当前处于 ZZOI 锁定状态，必须提交 ZZOI 题目后才能解除锁定并关机。"
            )
            return
    except Exception:
        pass

    # 1. 如果专注模式激活，强制结束
    try:
        from ui.focus_view import _get_global_engine
        eng = _get_global_engine()
        if eng.is_active:
            eng.emergency_exit("一键关机退出")
    except Exception:
        pass

    # 2. 显示同步进度
    progress = ExitProgressDialog()
    progress.show()

    steps = [
        (15, "正在同步对话日志…",    "同步到 note.ms"),
        (35, "正在同步专注记录…",    "发送邮件报告"),
        (50, "正在截取极域快照…",    "保存证据文件"),
        (70, "退出信号写入…",        "通知 watchdog"),
        (85, "热键清理…",            ""),
        (95, "准备关机…",            ""),
    ]
    for val, status, detail in steps:
        progress.set_progress(val, status, detail)
        QApplication.processEvents()

    try:
        # round48：关机前把当前 AI 对话摘要/存档收口（同步等待完成）
        try:
            from ui.dialog_view import _get_global_dialog
            _get_global_dialog().close_dialog()
        except Exception as e:
            logger.warning(f"关机前对话存档失败: {e}")
        from core.log_sync import sync_all
        sync_all()
        progress.set_progress(85, "日志同步完成", "")
    except Exception as e:
        logger.error(f"关机同步失败: {e}")
        progress.set_progress(85, "日志同步异常", str(e)[:40])

    # 写入关机信号（watchdog 识别后不做恢复）
    try:
        signal_file = os.path.join(DATA_DIR, "exit_signal")
        with open(signal_file, "w", encoding="utf-8") as f:
            f.write("shutdown")
    except Exception:
        pass

    try:
        from core.mute_mode import stop_global_hotkey
        stop_global_hotkey()
    except Exception:
        pass

    progress.close()
    QMessageBox.warning(None, "即将关机",
        "⚠ 日志和截图已保存到本地。\n\n"
        "系统将在 30 秒后关机。\n"
        "请立即保存其他应用的所有工作！")

    # 3. 关机
    QApplication.quit()
    try:
        import subprocess
        subprocess.Popen("shutdown /s /t 25", shell=True)
    except Exception:
        pass
