"""OISystem 任务栏托盘图标。

需求：进程无论在何时都要有一个图标在任务栏。
使用 PySide6 QSystemTrayIcon。
"""
from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QAction
from PySide6.QtCore import QObject, Signal

from ui.icons import render_svg, SVG_FLAG
from utils.helpers import logger


def _make_tray_icon() -> QIcon:
    """生成托盘图标：深蓝底 + 白色旗帜 SVG。"""
    pm = QPixmap(64, 64)
    pm.fill(QColor(30, 41, 59))  # 深蓝底
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    flag = render_svg(SVG_FLAG, size=40, color="#fbbf24")
    p.drawPixmap(12, 12, flag)
    p.end()
    return QIcon(pm)


class TrayController(QObject):
    """托盘控制器：管理图标、右键菜单、双击事件。"""

    double_clicked = Signal()
    show_settings = Signal()
    show_dialog = Signal()
    ask_now = Signal()          # R23：随时提问（与 Ctrl+Alt+A 同一入口）
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tray = QSystemTrayIcon(_make_tray_icon(), parent)
        self.tray.setToolTip("OISystem - 信息学奥赛学习辅助系统")
        self.tray.setVisible(True)
        self._build_menu()
        self.tray.activated.connect(self._on_activated)
        logger.info("托盘图标已创建")

    def _build_menu(self):
        menu = QMenu()
        act_show = QAction("显示侧边栏", menu)
        act_show.triggered.connect(self.double_clicked)
        menu.addAction(act_show)

        act_dialog = QAction("打开 AI 对话", menu)
        act_dialog.triggered.connect(self.show_dialog)
        menu.addAction(act_dialog)

        act_ask = QAction("随时提问（Ctrl+Alt+A）", menu)
        act_ask.triggered.connect(self.ask_now)
        menu.addAction(act_ask)

        act_settings = QAction("设置", menu)
        act_settings.triggered.connect(self.show_settings)
        menu.addAction(act_settings)

        menu.addSeparator()

        act_quit = QAction("退出 OISystem", menu)
        act_quit.triggered.connect(self.quit_requested)
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)

    def _on_activated(self, reason):
        # 双击托盘：显示侧边栏
        if reason == QSystemTrayIcon.DoubleClick:
            self.double_clicked.emit()

    def show_message(self, title: str, message: str, timeout_ms: int = 3000):
        """通过托盘弹窗显示消息（备用，主用 ui.toast）。"""
        self.tray.showMessage(title, message, QSystemTrayIcon.Information, timeout_ms)
