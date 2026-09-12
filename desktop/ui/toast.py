"""OISystem 右下角弹窗系统。

所有通知从屏幕右下角弹出，自动消失。
支持 info/warning/error 三种级别。
"""
from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout, QFrame, QApplication
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QRect, QRectF, QPoint
from PySide6.QtGui import QColor, QPainter, QFont

from utils.helpers import logger


TOAST_WIDTH = 380
TOAST_MARGIN = 20
TOAST_DURATION_MS = 3500

COLORS = {
    "info":    {"bg": QColor(0, 0, 0, 245),  "accent": QColor(56, 189, 248)},
    "warning": {"bg": QColor(0, 0, 0, 245),  "accent": QColor(251, 191, 36)},
    "error":   {"bg": QColor(0, 0, 0, 245),  "accent": QColor(248, 113, 113)},
    "success": {"bg": QColor(0, 0, 0, 245),  "accent": QColor(74, 222, 128)},
}


class Toast(QFrame):
    """单个弹窗。"""

    def __init__(self, title: str, message: str, level: str = "info", parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # P1 修复：关闭时自动销毁 C++ 对象，防止 toast 累积导致内存泄漏
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        c = COLORS.get(level, COLORS["info"])
        self._bg = c["bg"]
        self._accent = c["accent"]

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 8, 14, 8)

        title_lbl = QLabel(title)
        tf = QFont("Microsoft YaHei", 10, QFont.Bold)
        title_lbl.setFont(tf)
        title_lbl.setStyleSheet(f"color: #f8fafc; background: transparent;")
        title_lbl.setWordWrap(True)
        layout.addWidget(title_lbl)

        msg_lbl = QLabel(message)
        mf = QFont("Microsoft YaHei", 9)
        msg_lbl.setFont(mf)
        msg_lbl.setStyleSheet(f"color: #cbd5e1; background: transparent;")
        msg_lbl.setWordWrap(True)
        layout.addWidget(msg_lbl)

        self.setFixedWidth(TOAST_WIDTH)
        self.setMinimumHeight(64)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(0, 0, -1, -1)
        # 主体：纯黑圆角卡片 + 更明显描边
        p.setPen(QColor(255, 255, 255, 35))
        p.setBrush(self._bg)
        p.drawRoundedRect(rect, 12, 12)
        # 左侧级别色条
        p.setPen(Qt.NoPen)
        p.setBrush(self._accent)
        bar = QRectF(rect.left() + 8, rect.top() + 12, 4, rect.height() - 24)
        p.drawRoundedRect(bar, 2, 2)

    def appear(self, x: int, y: int):
        """从屏幕外滑入。"""
        self.move(x, y + 40)
        self.show()
        # P1 修复：动画设置 parent=self，确保 toast 销毁时动画同步销毁
        anim = QPropertyAnimation(self, b"pos", self)
        anim.setDuration(250)
        anim.setStartValue(QPoint(x, y + 40))
        anim.setEndValue(QPoint(x, y))
        anim.start()
        self._anim = anim
        QTimer.singleShot(TOAST_DURATION_MS, self._disappear)

    def _disappear(self):
        # P1 修复：toast 可能已被关闭（WA_DeleteOnClose），先检查存活
        try:
            if not self.isVisible():
                return
        except RuntimeError:
            return
        # P1 修复：动画设置 parent=self，确保 toast 销毁时动画同步销毁
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(300)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.finished.connect(self.close)
        anim.start()
        self._anim = anim


def show_toast(title: str, message: str, level: str = "info"):
    """在屏幕右下角显示一个弹窗。"""
    try:
        # 静音模式：不创建弹窗，直接返回（避免静音期间积压的弹窗在解除静音后于 (0,0) 永驻）
        try:
            from core.mute_mode import is_mute_mode
            if is_mute_mode():
                return None
        except Exception:
            pass
        # r39 P1 修复：primaryScreen() 可能返回 None
        primary = QApplication.primaryScreen()
        if primary is None:
            return
        screen = primary.availableGeometry()
        toast = Toast(title, message, level)
        toast.adjustSize()
        x = screen.right() - toast.width() - TOAST_MARGIN
        y = screen.bottom() - toast.height() - TOAST_MARGIN
        # 注册到静音模式，静音模式开启时立即隐藏（模块名为 mute_mode，非 boss_mode）
        try:
            from core.mute_mode import register_toast
            register_toast(toast)
        except Exception:
            pass
        toast.appear(x, y)
        logger.info(f"[toast:{level}] {title} - {message}")
        return toast
    except Exception as e:
        logger.error(f"Toast 显示失败: {e}")
        return None
