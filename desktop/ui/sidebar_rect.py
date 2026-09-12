"""OISystem 矩形吸附式侧边栏。

简洁矩形风格：收起态窄标签 + 展开态矩形面板，可拖拽成球吸附边缘。
无梯形裁剪，纯矩形区域。
"""
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QPushButton, QApplication, QWidget,
)
from PySide6.QtCore import (
    Qt, QPoint, QPointF, QRect, QRectF, QTimer, QPropertyAnimation,
    QEasingCurve, QElapsedTimer,
)
from PySide6.QtGui import (
    QPainter, QColor, QFont, QMouseEvent, QPainterPath,
    QRegion, QPen, QLinearGradient, QRadialGradient, QCursor,
)

from ui.icons import SIDEBAR_BUTTONS, render_svg, SVG_BELL, SVG_BELL_OFF, get_sidebar_buttons
from ui.themes import ThemeManager
from utils.helpers import logger
from utils.exceptions import FocusLockedError
from config.settings import ConfigManager

EDGE_LEFT = "left"
EDGE_RIGHT = "right"

ANIM_MS = 240
PANEL_W = 80              # 展开态宽度
TAB_W = 14                # 收起态宽度
BUTTON_H = 36
BUTTON_GAP = 4
MARGIN_V = 12
MIN_TAB_H = 64
MAX_TAB_H = 170
BALL_SIZE = 44


def _is_widget_alive(widget) -> bool:
    """检查 QWidget C++ 对象是否仍存活（PySide6 无 sip 模块，用 shiboken6）。"""
    if widget is None:
        return False
    try:
        from shiboken6 import isValid
        return isValid(widget)
    except ImportError:
        try:
            widget.isVisible()
            return True
        except RuntimeError:
            return False


def _panel_height():
    n = len(SIDEBAR_BUTTONS)
    return max(100, MARGIN_V * 2 + n * BUTTON_H + max(0, n - 1) * BUTTON_GAP)


def _tab_height():
    return max(MIN_TAB_H, min(MAX_TAB_H, int(_panel_height() * 0.55)))


# ---------- 按钮 ----------
class SidebarButton(QPushButton):
    """矩形侧边栏按钮，图标居中。"""

    def __init__(self, key, label, svg_str, accent, edge, parent=None):
        super().__init__(parent)
        self.key = key
        self.label_text = label
        self.svg_str = svg_str
        self.accent = accent
        self._edge = edge
        self.setFixedHeight(BUTTON_H)
        self.setMinimumWidth(24)
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
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 30))
            p.drawRoundedRect(QRectF(2, 2, rect.width() - 4, rect.height() - 4), 8, 8)
        elif self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 18))
            p.drawRoundedRect(QRectF(2, 2, rect.width() - 4, rect.height() - 4), 8, 8)

        color = self.accent if self.isChecked() else ("#f8fafc" if self.underMouse() else "#cbd5e1")

        pm = render_svg(self.svg_str, size=16, color=color)
        icon_x = (rect.width() - 16) // 2
        p.drawPixmap(icon_x, 2, pm)
        p.setPen(QColor(color))
        p.setFont(QFont("Microsoft YaHei", 7))
        p.drawText(QRectF(0, 22, rect.width(), 14), Qt.AlignCenter, self.label_text)
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

        self._dragging = False
        self._ball_mode = False
        self._ball_near_edge = None
        self._buttons = {}
        self._expanded = False
        self._animating = False
        self._layout = None
        self._anim = None
        self._open_views = {}
        self._mute_observer = None
        self._leave_timer_id = None

        self._accent = ThemeManager().current_theme.get("accent", "#3b82f6")

        # 呼吸动画
        self._breath = 0.0
        self._breath_timer = QTimer(self)
        self._breath_timer.timeout.connect(self._on_breath)
        self._breath_timer.start(50)
        self._breath_clock = QElapsedTimer()
        self._breath_clock.start()

        self._setup_ui()
        self._snap_to_edge(self.edge, expand=cfg.sidebar_expanded, animate=False)

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
        self.setObjectName("SidebarRect")

        self._container = QWidget(self)
        self._container.setGeometry(self.rect())
        self._layout = QVBoxLayout(self._container)
        self._layout.setSpacing(BUTTON_GAP)
        self._layout.setContentsMargins(8, MARGIN_V, 8, MARGIN_V)
        self._layout.addStretch(1)
        for key, label, svg in get_sidebar_buttons():
            btn = SidebarButton(key, label, svg, self._accent, self.edge, self._container)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self._on_button(k))
            self._layout.addWidget(btn, 0, Qt.AlignCenter)
            self._buttons[key] = btn
        self._layout.addStretch(1)

    # ---------- 呼吸 ----------
    def _on_breath(self):
        if not self._expanded and not self._ball_mode and not self._dragging:
            import math
            t = self._breath_clock.elapsed() / 1000.0
            self._breath = (math.sin(t * 2.2) + 1.0) / 2.0
            self.update()

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 4 or h <= 4:
            p.end()
            return

        if self._ball_mode:
            self._paint_ball(p, w, h)
            p.end()
            return

        if self._animating:
            if w < 24:
                self._paint_tab(p, w, h)
            else:
                self._paint_panel(p, w, h)
            p.end()
            return

        if not self._expanded:
            self._paint_tab(p, w, h)
            p.end()
            return
        self._paint_panel(p, w, h)
        p.end()

    def _paint_ball(self, p, w, h):
        cx, cy = w / 2.0, h / 2.0
        r = min(w, h) / 2.0 - 2
        near = self._ball_near_edge is not None
        accent = QColor(self._accent)

        glow = QColor(accent if near else QColor(100, 116, 139))
        rg = QRadialGradient(QPointF(cx, cy), r + 5)
        rg.setColorAt(0.0, QColor(glow.red(), glow.green(), glow.blue(), 0))
        rg.setColorAt(max(0.0, (r - 4) / (r + 5)), QColor(glow.red(), glow.green(), glow.blue(), 0))
        rg.setColorAt(0.85, QColor(glow.red(), glow.green(), glow.blue(), 90 if near else 50))
        rg.setColorAt(1.0, QColor(glow.red(), glow.green(), glow.blue(), 0))
        p.setPen(Qt.NoPen)
        p.setBrush(rg)
        p.drawEllipse(QPointF(cx, cy), r + 5, r + 5)

        bg = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.35), r * 1.5)
        bg.setColorAt(0.0, QColor(51, 65, 85, 230))
        bg.setColorAt(1.0, QColor(15, 23, 42, 230))
        p.setBrush(bg)
        p.setPen(QPen(QColor(glow.red(), glow.green(), glow.blue(), 180), 1.4))
        p.drawEllipse(QPointF(cx, cy), r, r)

        core_r = 4 if near else 3
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(accent if near else QColor(203, 213, 225)))
        p.drawEllipse(QPointF(cx, cy), core_r, core_r)

    def _paint_tab(self, p, w, h):
        """收起态：窄标签 + 呼吸光 + 握把纹。"""
        hovered = self.underMouse()
        accent = QColor(self._accent)

        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(30, 41, 59, 200))
        g.setColorAt(0.5, QColor(15, 23, 42, 225))
        g.setColorAt(1.0, QColor(30, 41, 59, 200))
        r = 7
        if self.edge == EDGE_RIGHT:
            path = QPainterPath()
            path.addRoundedRect(QRectF(0, 0, w + r, h), r, r)
        else:
            path = QPainterPath()
            path.addRoundedRect(QRectF(-r, 0, w + r, h), r, r)
        p.setClipPath(path)
        p.fillPath(path, g)

        alpha = int(40 + self._breath * 90) + (60 if hovered else 0)
        alpha = min(220, alpha)
        c = QColor(accent.red(), accent.green(), accent.blue(), alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        if self.edge == EDGE_RIGHT:
            p.drawRect(w - 2, 6, 2, h - 12)
        else:
            p.drawRect(0, 6, 2, h - 12)

        line_c = QColor(226, 232, 240, 150 if hovered else 100)
        p.setPen(QPen(line_c, 1.6, Qt.SolidLine, Qt.RoundCap))
        cy = h / 2.0
        lw = min(6, w - 6)
        lx = (w - lw) / 2.0
        for i in (-1, 0, 1):
            y = cy + i * 6
            p.drawLine(QPointF(lx, y), QPointF(lx + lw, y))
        p.setClipping(False)

    def _paint_panel(self, p, w, h):
        """展开态：矩形面板 + 渐变背景 + 边缘亮线。"""
        accent = QColor(self._accent)
        r = 10

        # 矩形路径（靠屏幕侧圆角，靠外边缘直边）
        path = QPainterPath()
        if self.edge == EDGE_RIGHT:
            path.addRoundedRect(QRectF(0, 0, w + r, h), r, r)
        else:
            path.addRoundedRect(QRectF(-r, 0, w + r, h), r, r)
        p.setClipPath(path)

        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(23, 32, 51, 240))
        g.setColorAt(0.5, QColor(15, 23, 42, 240))
        g.setColorAt(1.0, QColor(23, 32, 51, 240))
        p.fillPath(path, g)

        # 屏幕侧亮线
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        if self.edge == EDGE_RIGHT:
            p.drawLine(QPointF(w - 0.5, r + 1), QPointF(w - 0.5, h - r - 1))
        else:
            p.drawLine(QPointF(0.5, r + 1), QPointF(0.5, h - r - 1))

        # 选中按钮高光条（屏幕侧缝隙）
        for btn in self._buttons.values():
            if btn.isChecked() and btn.isVisible():
                cy = btn.geometry().center().y()
                p.setPen(Qt.NoPen)
                p.setBrush(accent)
                if self.edge == EDGE_RIGHT:
                    p.drawRoundedRect(QRectF(w - 5, cy - 12, 3, 24), 1.5, 1.5)
                else:
                    p.drawRoundedRect(QRectF(2, cy - 12, 3, 24), 1.5, 1.5)
                break

        p.setClipping(False)

    # ---------- 几何 ----------
    def _screen_geo(self):
        # round48：RDP 断开/无显示器时 primaryScreen() 可能返回 None
        screen = QApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1920, 1080)
        geo = screen.availableGeometry()
        return geo if geo.isValid() else QRect(0, 0, 1920, 1080)

    def _expanded_geometry(self, edge, y_center=None):
        geo = self._screen_geo()
        panel_h = min(_panel_height(), geo.height() - 20)
        if y_center is None:
            y = geo.top() + (geo.height() - panel_h) // 2
        else:
            y = max(geo.top(), min(geo.bottom() - panel_h, y_center - panel_h // 2))
        if edge == EDGE_RIGHT:
            return QRect(geo.right() - PANEL_W + 1, y, PANEL_W, panel_h)
        return QRect(geo.left(), y, PANEL_W, panel_h)

    def _collapsed_geometry(self, edge, y_center=None):
        geo = self._screen_geo()
        tab_h = _tab_height()
        if y_center is None:
            y = geo.top() + (geo.height() - tab_h) // 2
        else:
            y = max(geo.top(), min(geo.bottom() - tab_h, y_center - tab_h // 2))
        if edge == EDGE_RIGHT:
            x = geo.right() - TAB_W + 1
        else:
            x = geo.left()
        return QRect(x, y, TAB_W, tab_h)

    def _snap_to_edge(self, edge, expand=True, animate=True, y_center=None):
        was_expanded = self._expanded
        self.edge = edge
        self._expanded = expand
        target = self._expanded_geometry(edge, y_center) if expand else self._collapsed_geometry(edge, y_center)

        for btn in self._buttons.values():
            btn.set_edge(edge)
            btn.setVisible(expand)
        if self._container is not None:
            self._container.setGeometry(0, 0, target.width(), target.height())

        if animate and was_expanded != expand:
            self._anim_geometry(target, expanding=expand)
        else:
            self.setGeometry(target)

        try:
            ConfigManager().update(sidebar_edge=edge, sidebar_expanded=expand)
        except Exception as e:
            logger.warning(f"持久化侧边栏状态失败: {e}")

    def _anim_geometry(self, target, expanding=True):
        if self._anim is not None:
            try:
                self._anim.stop()
                self._anim.deleteLater()
            except Exception:
                pass
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(ANIM_MS)
        anim.setStartValue(self.geometry())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic if expanding else QEasingCurve.InCubic)
        self._animating = True
        anim.finished.connect(self._on_anim_finished)
        anim.start()
        self._anim = anim

    def _on_anim_finished(self):
        # round48：动画 finished 信号可能在 closeEvent 后入队，先判活再访问
        if not _is_widget_alive(self):
            return
        self._animating = False

    def _nearest_edge(self, pos):
        geo = self._screen_geo()
        return EDGE_LEFT if abs(pos.x() - geo.left()) < abs(pos.x() - geo.right()) else EDGE_RIGHT

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._container is not None:
            self._container.setGeometry(0, 0, self.width(), self.height())

    # ---------- 展开 / 收起 ----------
    def _expand(self):
        if self._expanded or self._ball_mode:
            return
        self._snap_to_edge(self.edge, expand=True, animate=True,
                           y_center=self.geometry().center().y())

    def _collapse(self):
        # round48：PySide6 无 sip 模块，改用 shiboken6 检查对象是否已销毁
        if not _is_widget_alive(self):
            return
        if not self._expanded or self._dragging:
            return
        self._snap_to_edge(self.edge, expand=False, animate=True,
                           y_center=self.geometry().center().y())

    # ---------- 悬停 ----------
    def enterEvent(self, event):
        if not self._dragging and not self._ball_mode:
            self._expand()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._dragging and not self._ball_mode:
            if self._leave_timer_id is not None:
                self.killTimer(self._leave_timer_id)
            self._leave_timer_id = self.startTimer(250)
        super().leaveEvent(event)

    def timerEvent(self, event):
        if event.timerId() == self._leave_timer_id:
            self.killTimer(self._leave_timer_id)
            self._leave_timer_id = None
            self._maybe_collapse()
        super().timerEvent(event)

    def _maybe_collapse(self):
        if self._dragging or self._ball_mode or self._animating or not self._expanded:
            return
        if not self.geometry().contains(QCursor.pos()):
            if self._open_views:
                return
            self._collapse()

    # ---------- 拖动成球 ----------
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._dragging = True
        self._ball_mode = True
        self._ball_near_edge = None
        for btn in self._buttons.values():
            btn.setVisible(False)
        if self._container is not None:
            self._container.setVisible(False)
        gp = event.globalPosition().toPoint()
        self.setGeometry(gp.x() - BALL_SIZE // 2, gp.y() - BALL_SIZE // 2, BALL_SIZE, BALL_SIZE)
        self._expanded = False
        self.grabMouse()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging or not self._ball_mode:
            return
        gp = event.globalPosition().toPoint()
        self.move(gp.x() - BALL_SIZE // 2, gp.y() - BALL_SIZE // 2)
        geo = self._screen_geo()
        dl = abs(gp.x() - geo.left())
        dr = abs(gp.x() - geo.right())
        near = None
        if min(dl, dr) < 90:
            near = EDGE_LEFT if dl < dr else EDGE_RIGHT
        if near != self._ball_near_edge:
            self._ball_near_edge = near
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self.releaseMouse()
        if self._ball_mode:
            self._ball_mode = False
            self._ball_near_edge = None
            self._expanded = False
            for btn in self._buttons.values():
                btn.setVisible(True)
            if self._container is not None:
                self._container.setVisible(True)
        center = self.frameGeometry().center()
        new_edge = self._nearest_edge(center)
        self._snap_to_edge(new_edge, expand=True, animate=True, y_center=center.y())
        event.accept()

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
                QTimer.singleShot(50, self._collapse)
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
        self._breath_timer.stop()
        if self._leave_timer_id is not None:
            self.killTimer(self._leave_timer_id)
            self._leave_timer_id = None
        if self._anim is not None:
            try:
                self._anim.stop()
                self._anim.deleteLater()
            except Exception:
                pass
            self._anim = None
        if self._mute_observer is not None:
            try:
                from core.mute_mode import remove_state_observer
                remove_state_observer(self._mute_observer)
            except Exception:
                pass
            self._mute_observer = None
        # round48：与 sidebar_v2 对齐，断开全局 FocusEngine 信号，防止销毁后被触发
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
