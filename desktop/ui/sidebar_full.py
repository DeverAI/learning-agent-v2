"""OISystem 全屏固定式侧边栏。

覆盖整个屏幕侧边宽度（~200px），全高，不可移动，始终展开。
按钮显示图标 + 文字标签（水平排列）。
"""
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QHBoxLayout, QPushButton, QApplication, QWidget, QLabel,
)
from PySide6.QtCore import (
    Qt, QPointF, QRect, QRectF, QTimer,
)
from PySide6.QtGui import (
    QPainter, QColor, QFont, QPainterPath,
    QPen, QLinearGradient,
)

from ui.icons import SIDEBAR_BUTTONS, render_svg, SVG_BELL, SVG_BELL_OFF, get_sidebar_buttons
from ui.themes import ThemeManager
from utils.helpers import logger
from utils.exceptions import FocusLockedError
from config.settings import ConfigManager

EDGE_LEFT = "left"
EDGE_RIGHT = "right"
PANEL_W = 200             # 面板宽度
BUTTON_H = 44             # 按钮高度
BUTTON_GAP = 4
MARGIN_TOP = 20
MARGIN_BOTTOM = 12
MARGIN_H = 12


# ---------- 按钮（图标 + 文字水平排列）----------
class SidebarButton(QPushButton):
    """全屏侧边栏按钮：图标在左，文字在右。"""

    def __init__(self, key, label, svg_str, accent, edge, parent=None):
        super().__init__(parent)
        self.key = key
        self.label_text = label
        self.svg_str = svg_str
        self.accent = accent
        self._edge = edge
        self.setFixedHeight(BUTTON_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)

    def set_edge(self, edge):
        self._edge = edge
        self.update()

    def set_accent(self, color):
        self.accent = color
        self.update()

    def set_svg(self, svg_str):
        self.svg_str = svg_str
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        accent = QColor(self.accent)
        if not accent.isValid():
            accent = QColor("#3b82f6")

        if self.isChecked():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 32))
            p.drawRoundedRect(QRectF(4, 2, rect.width() - 8, rect.height() - 4), 10, 10)
        elif self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 20))
            p.drawRoundedRect(QRectF(4, 2, rect.width() - 8, rect.height() - 4), 10, 10)

        color = self.accent if self.isChecked() else ("#f8fafc" if self.underMouse() else "#cbd5e1")

        # 图标
        pm = render_svg(self.svg_str, size=18, color=color)
        p.drawPixmap(MARGIN_H, (rect.height() - 18) // 2, pm)

        # 文字
        p.setPen(QColor(color))
        p.setFont(QFont("Microsoft YaHei", 10))
        text_rect = QRectF(MARGIN_H + 26, 0, rect.width() - MARGIN_H - 30, rect.height())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.label_text)

        # 选中指示条（左侧竖线）
        if self.isChecked():
            p.setPen(Qt.NoPen)
            p.setBrush(accent)
            p.drawRoundedRect(QRectF(1, rect.height() // 2 - 10, 3, 20), 1.5, 1.5)

        p.end()


# ---------- 主侧边栏 ----------
class Sidebar(QFrame):

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        cfg = ConfigManager().settings
        self.edge = cfg.sidebar_edge if cfg.sidebar_edge in (EDGE_LEFT, EDGE_RIGHT) else EDGE_RIGHT

        self._buttons = {}
        self._expanded = True       # 始终展开
        self._open_views = {}
        self._mute_observer = None
        self._layout = None

        self._accent = ThemeManager().current_theme.get("accent", "#3b82f6")

        self._setup_ui()
        self._position_at_edge()

        try:
            from core.mute_mode import add_state_observer, is_mute_mode
            self._mute_observer = self._on_mute_mode_changed
            add_state_observer(self._mute_observer)
            mute_btn = self._buttons.get("mute")
            if mute_btn is not None:
                mute_btn.setChecked(is_mute_mode())
        except Exception:
            pass

        self._update_focus_button_svg()
        try:
            from ui.focus_view import _get_global_engine
            eng = _get_global_engine()
            eng.focus_started.connect(self._on_focus_state_changed)
            eng.focus_ended.connect(self._on_focus_state_changed)
        except Exception:
            pass

    def _setup_ui(self):
        self.setObjectName("SidebarFull")

        self._container = QWidget(self)
        self._layout = QVBoxLayout(self._container)
        self._layout.setSpacing(BUTTON_GAP)
        self._layout.setContentsMargins(MARGIN_H, MARGIN_TOP, MARGIN_H, MARGIN_BOTTOM)

        for key, label, svg in get_sidebar_buttons():
            btn = SidebarButton(key, label, svg, self._accent, self.edge, self._container)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self._on_button(k))
            self._layout.addWidget(btn)
            self._buttons[key] = btn

        self._layout.addStretch(1)

    def _screen_geo(self):
        # round48：RDP 断开/无显示器时 primaryScreen() 可能返回 None
        screen = QApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1920, 1080)
        geo = screen.availableGeometry()
        return geo if geo.isValid() else QRect(0, 0, 1920, 1080)

    def _position_at_edge(self):
        geo = self._screen_geo()
        if self.edge == EDGE_RIGHT:
            x = geo.right() - PANEL_W + 1
        else:
            x = geo.left()
        self.setGeometry(x, geo.top(), PANEL_W, geo.height())
        self._container.setGeometry(0, 0, PANEL_W, geo.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._container is not None:
            self._container.setGeometry(0, 0, self.width(), self.height())

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 4 or h <= 4:
            p.end()
            return

        # 背景渐变
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(18, 24, 40, 248))
        g.setColorAt(0.5, QColor(12, 18, 34, 250))
        g.setColorAt(1.0, QColor(18, 24, 40, 248))
        p.fillRect(0, 0, w, h, g)

        # 内侧亮线（靠近屏幕中心的边）
        accent = QColor(self._accent)
        p.setPen(QPen(QColor(255, 255, 255, 20), 1))
        if self.edge == EDGE_RIGHT:
            p.drawLine(QPointF(0.5, 0), QPointF(0.5, h))
        else:
            p.drawLine(QPointF(w - 0.5, 0), QPointF(w - 0.5, h))

        # 屏幕侧辉光
        glow_w = 30
        if self.edge == EDGE_RIGHT:
            eg = QLinearGradient(w - glow_w, 0, w, 0)
        else:
            eg = QLinearGradient(glow_w, 0, 0, 0)
        eg.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        eg.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 18))
        p.fillRect(QRectF(0, 0, w, h), eg)

        p.end()

    # ---------- 展开（兼容接口）----------
    def _expand(self):
        """全屏式始终展开；托盘双击等唤醒动作置顶显示，而不是空 pass。"""
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except RuntimeError:
            # 窗口已销毁，忽略唤醒请求
            pass

    def _collapse(self):
        """全屏式不收起，此方法为接口兼容。"""
        pass

    # ---------- 按钮动作 ----------
    def _on_button(self, key):
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        self.update()
        try:
            from core.mute_mode import is_mute_mode
            if is_mute_mode():
                mute_btn = self._buttons.get("mute")
                if mute_btn is not None:
                    mute_btn.setChecked(True)
        except Exception:
            pass
        logger.info(f"Sidebar button: {key}")
        if key == "exit":
            self._exit_clicked()
        elif key == "mute":
            self._mute_clicked()
        elif key == "focus":
            self._open_focus()
        elif key == "dialog":
            self._open_dialog()
        elif key == "graph":
            self._open_graph()
        elif key == "settings":
            self._open_settings()
        elif key == "log":
            self._open_log()
        elif key == "zzoi":
            self._open_zzoi()
        elif key == "lecture":
            # F9 修复：三种非默认侧边栏样式此前都没有 lecture 分支，而按钮仍由
            # get_sidebar_buttons() 渲染出来 → 点「讲课」静默无响应。补上与 sidebar_v2 一致的分发。
            self._open_lecture()

    def _toggle_view(self, key, view_cls):
        btn = self._buttons.get(key)
        if btn is not None:
            btn.setEnabled(False)
        existing = self._open_views.get(key)
        if existing is not None:
            try:
                existing.close()
                existing.deleteLater()
            except Exception:
                pass
            self._open_views.pop(key, None)
            if btn is not None:
                btn.setChecked(False)
                btn.setEnabled(True)
            return
        try:
            view = view_cls(self)
            def _on_view_closed():
                if self._open_views.get(key) is view:
                    self._open_views.pop(key, None)
                if btn is not None:
                    btn.setChecked(False)
                    btn.setEnabled(True)
            orig_close = view.closeEvent
            def _patched_close(event):
                _on_view_closed()
                if orig_close:
                    orig_close(event)
            view.closeEvent = _patched_close
            view.show()
            self._open_views[key] = view
            if btn is not None:
                btn.setEnabled(True)
        except Exception as e:
            logger.warning(f"{view_cls.__name__} 未就绪: {e}")
            if btn is not None:
                btn.setChecked(False)
                btn.setEnabled(True)

    def _open_focus(self):
        from ui.focus_view import FocusView
        self._toggle_view("focus", FocusView)

    def _open_dialog(self):
        from ui.dialog_view import DialogView
        self._toggle_view("dialog", DialogView)

    def _open_settings(self):
        from ui.settings_view import SettingsView
        self._toggle_view("settings", SettingsView)

    def _open_log(self):
        from ui.log_view import LogView
        self._toggle_view("log", LogView)

    def _open_zzoi(self):
        from ui.zzoi_view import ZzoiView
        self._toggle_view("zzoi", ZzoiView)

    def _open_lecture(self):
        # F9：与 sidebar_v2._open_lecture 保持一致
        from ui.lecture_view import LectureView
        self._toggle_view("lecture", LectureView)

    def _open_graph(self):
        from ui.graph_editor import GraphEditor
        self._toggle_view("graph", GraphEditor)

    def _exit_clicked(self):
        btn = self._buttons.get("exit")
        if btn is not None:
            btn.setEnabled(False)
        try:
            from core.exit_flow import request_exit
            request_exit()
        except FocusLockedError:
            # 专注/ZZOI 锁定拦截：request_exit 已弹提示，不得再 quit 绕过锁定。
            logger.info("退出被专注模式锁定拦截")
        except Exception as e:
            logger.warning(f"Exit flow 未就绪: {e}")
            try:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(None, "退出失败", f"退出流程异常: {e}")
            except Exception:
                pass
        finally:
            if btn is not None:
                btn.setEnabled(True)

    def _mute_clicked(self):
        btn = self._buttons.get("mute")
        if btn is not None:
            btn.setEnabled(False)
        try:
            from core.mute_mode import toggle_mute_mode
            toggle_mute_mode()
        except Exception as e:
            logger.warning(f"静音模式未就绪: {e}")
        finally:
            if btn is not None:
                def _reenable():
                    try:
                        btn.setEnabled(True)
                    except RuntimeError:
                        pass
                QTimer.singleShot(300, _reenable)

    def _on_mute_mode_changed(self, enabled):
        mute_btn = self._buttons.get("mute")
        if mute_btn is not None:
            mute_btn.setChecked(enabled)

    def _on_focus_state_changed(self, *args):
        self._update_focus_button_svg()

    def _update_focus_button_svg(self):
        try:
            from ui.focus_view import _get_global_engine
            eng = _get_global_engine()
            btn = self._buttons.get("focus")
            if btn is None:
                return
            btn.set_svg(SVG_BELL_OFF if eng.is_active else SVG_BELL)
        except Exception:
            pass

    def closeEvent(self, event):
        if self._mute_observer is not None:
            try:
                from core.mute_mode import remove_state_observer
                remove_state_observer(self._mute_observer)
            except Exception:
                pass
            self._mute_observer = None
        # round48：断开全局 FocusEngine 信号，防止销毁后被触发
        try:
            from ui.focus_view import _get_global_engine
            eng = _get_global_engine()
            if eng is not None:
                eng.focus_started.disconnect(self._on_focus_state_changed)
                eng.focus_ended.disconnect(self._on_focus_state_changed)
        except (TypeError, RuntimeError):
            pass
        except Exception:
            pass
        super().closeEvent(event)
