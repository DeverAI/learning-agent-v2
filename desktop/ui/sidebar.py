"""OISystem 吸附式侧边栏 — 纯梯形面板版。

- 整个窗口本身就是圆角梯形，贴合屏幕左/右边缘
- 按钮容器水平 + 垂直居中，完全包裹在梯形内部
- 收起态缩为细梯形条，鼠标悬停即展开
- 支持拖动坍缩成球，松手吸附最近边缘
"""
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QPushButton, QApplication, QMessageBox,
)
from PySide6.QtCore import (
    Qt, QPoint, QPointF, QRect, QRectF, QTimer, QPropertyAnimation,
)
from PySide6.QtGui import (
    QPainter, QColor, QPixmap, QFont, QMouseEvent, QPainterPath,
    QPolygonF, QRegion, QPen, QBrush, QLinearGradient, QCursor,
)

from ui.icons import SIDEBAR_BUTTONS, render_svg, SVG_BELL, SVG_BELL_OFF, get_sidebar_buttons
from ui.themes import ThemeManager
from utils.helpers import logger
from utils.exceptions import FocusLockedError
from config.settings import ConfigManager

EDGE_LEFT = "left"
EDGE_RIGHT = "right"

ANIM_MS = 240

# 梯形几何参数
TAPER_RATIO = 0.75     # 内缘 / 外缘 宽度比
CORNER_RADIUS = 12     # 顶部和底部圆角半径
BUTTON_H = 40          # 单个按钮高度
BUTTON_GAP = 6         # 按钮之间间距
MARGIN_V = 14          # 面板上下内边距
MIN_TAB_H = 64         # 收起态标签最小高度
MAX_TAB_H = 160        # 收起态标签最大高度
BALL_SIZE = 44         # 拖动坍缩小球直径


def _panel_depth() -> int:
    """面板水平深度：根据按钮数量决定，至少 44px。"""
    return max(44, int(len(SIDEBAR_BUTTONS) * 9.5))


def _panel_height() -> int:
    """面板高度 = 按钮总高 + 间距 + 上下边距。"""
    n = len(SIDEBAR_BUTTONS)
    return max(120, MARGIN_V * 2 + n * BUTTON_H + max(0, n - 1) * BUTTON_GAP)


def _tab_height() -> int:
    """收起态梯形条高度：与面板高度成比例，但有上下限。"""
    return max(MIN_TAB_H, min(MAX_TAB_H, int(_panel_height() * 0.55)))


# ---------- 梯形按钮 ----------
class SidebarButton(QPushButton):
    """侧边栏按钮，居中绘制图标和文字。"""

    def __init__(self, key: str, label: str, svg_str: str, accent: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.label_text = label
        self.svg_str = svg_str
        self.accent = accent
        self.setFixedSize(34, BUTTON_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)

    def set_accent(self, color: str):
        self.accent = color
        self.update()

    def set_svg(self, svg_str: str):
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
            p.setBrush(accent)
            bar = rect.adjusted(2, 8, -rect.width() + 5, -8)
            p.drawRoundedRect(bar, 2, 2)
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 22))
            p.drawRoundedRect(rect.adjusted(4, 2, -2, -2), 8, 8)
        elif self.underMouse():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 14))
            p.drawRoundedRect(rect.adjusted(4, 2, -2, -2), 8, 8)

        if self.isChecked():
            color = self.accent
        elif self.underMouse():
            color = "#f8fafc"
        else:
            color = "#cbd5e1"

        pm = render_svg(self.svg_str, size=18, color=color)
        p.drawPixmap(8, 4, pm)
        p.setPen(QColor(color))
        f = QFont("Microsoft YaHei", 7)
        p.setFont(f)
        p.drawText(QRectF(0, 24, 34, 14), Qt.AlignCenter, self.label_text)




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
        self._drag_offset = QPoint()
        self._ball_mode = False
        self._saved_expanded = False
        self._saved_edge = EDGE_RIGHT
        self._saved_full_geo = None
        self._buttons = {}
        self._expanded = False
        self._layout = None
        self._anim = None
        self._open_views = {}
        self._mute_observer = None

        # 梯形尺寸
        self._wide_px = _panel_depth()
        self._panel_h = _panel_height()
        self._tab_w = 8
        self._tab_h = _tab_height()

        # 主题色
        self._accent = ThemeManager().current_theme.get("accent", "#3b82f6")

        self._setup_ui()
        self._snap_to_edge(self.edge, expand=cfg.sidebar_expanded, animate=False)

        # 信号绑定
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
        self.setObjectName("Sidebar")
        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(BUTTON_GAP)
        self._layout.setContentsMargins(0, MARGIN_V, 0, MARGIN_V)
        self._layout.addStretch(1)
        for key, label, svg in get_sidebar_buttons():
            btn = SidebarButton(key, label, svg, self._accent, self)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self._on_button(k))
            self._layout.addWidget(btn, 0, Qt.AlignCenter)
            self._buttons[key] = btn
        self._layout.addStretch(1)

    # ---------- 梯形遮罩 ----------
    def _inner_offset(self, width: int) -> int:
        """根据当前宽度计算梯形内缘偏移量（外缘贴屏幕边缘）。"""
        return max(2, int(width * (1 - TAPER_RATIO)))

    def _trapezoid_path(self, w: int, h: int) -> QPainterPath:
        """生成当前宽度/高度下的圆角梯形路径。"""
        offset = self._inner_offset(w)
        if w <= offset + 4:
            return QPainterPath()
        r = min(CORNER_RADIUS, h // 2 - 2)
        path = QPainterPath()
        if self.edge == EDGE_RIGHT:
            path.moveTo(offset + r, 0)
            path.lineTo(w - r, 0)
            path.arcTo(w - 2 * r, 0, 2 * r, 2 * r, 90, -90)
            path.lineTo(w, h - r)
            path.arcTo(w - 2 * r, h - 2 * r, 2 * r, 2 * r, 0, -90)
            path.lineTo(offset + r, h)
            path.arcTo(offset, h - 2 * r, 2 * r, 2 * r, 270, -90)
            path.lineTo(offset, r)
            path.arcTo(offset, 0, 2 * r, 2 * r, 180, -90)
        else:
            path.moveTo(r, 0)
            path.lineTo(w - offset - r, 0)
            path.arcTo(w - offset - 2 * r, 0, 2 * r, 2 * r, 90, -90)
            path.lineTo(w - offset, h - r)
            path.arcTo(w - offset - 2 * r, h - 2 * r, 2 * r, 2 * r, 0, -90)
            path.lineTo(r, h)
            path.arcTo(0, h - 2 * r, 2 * r, 2 * r, 270, -90)
            path.lineTo(0, r)
            path.arcTo(0, 0, 2 * r, 2 * r, 180, -90)
        path.closeSubpath()
        return path

    def _update_mask(self):
        """始终施加梯形遮罩（球态除外）。"""
        if self._ball_mode:
            self.clearMask()
            return
        w, h = self.width(), self.height()
        if w <= 4 or h <= 4:
            return
        path = self._trapezoid_path(w, h)
        if path.isEmpty():
            self.clearMask()
            return
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_mask()

    # ---------- 面板绘制 ----------
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        if self._ball_mode:
            r = min(w, h) // 2 - 2
            cx, cy = w // 2, h // 2
            p.setPen(QPen(QColor(100, 116, 139, 160), 1.5))
            p.setBrush(QColor(15, 23, 42, 220))
            p.drawEllipse(QPointF(cx, cy), r, r)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(203, 213, 225, 200))
            p.drawEllipse(QPointF(cx, cy), 5, 5)
            p.end()
            return

        if w <= 4 or h <= 4:
            p.end()
            return

        path = self._trapezoid_path(w, h)
        if path.isEmpty():
            p.end()
            return

        p.setClipPath(path)

        # 主体深色背景
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 23, 42, 240))
        p.drawPath(path)

        offset = self._inner_offset(w)

        # 内侧高光边（梯形收窄边）
        hl = QColor(255, 255, 255, 18)
        p.setPen(QPen(hl, 1.0))
        if self.edge == EDGE_RIGHT:
            p.drawLine(QPointF(offset + 0.5, CORNER_RADIUS + 1),
                       QPointF(offset + 0.5, h - CORNER_RADIUS - 1))
        else:
            p.drawLine(QPointF(w - offset - 0.5, CORNER_RADIUS + 1),
                       QPointF(w - offset - 0.5, h - CORNER_RADIUS - 1))

        # 外侧边缘微光
        edge_pen = QPen(QColor(255, 255, 255, 30), 1)
        p.setPen(edge_pen)
        if self.edge == EDGE_RIGHT:
            p.drawLine(QPointF(w - 0.5, CORNER_RADIUS + 1),
                       QPointF(w - 0.5, h - CORNER_RADIUS - 1))
        else:
            p.drawLine(QPointF(0.5, CORNER_RADIUS + 1),
                       QPointF(0.5, h - CORNER_RADIUS - 1))

        p.end()

    # ---------- 几何计算 ----------
    def _screen_geo(self) -> QRect:
        # round48：RDP 断开/无显示器时 primaryScreen() 可能返回 None
        screen = QApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1920, 1080)
        geo = screen.availableGeometry()
        return geo if geo.isValid() else QRect(0, 0, 1920, 1080)

    def _expanded_geometry(self, edge: str) -> QRect:
        geo = self._screen_geo()
        screen_h = geo.height()
        panel_h = min(self._panel_h, screen_h - 20)
        y = geo.top() + (screen_h - panel_h) // 2
        if edge == EDGE_RIGHT:
            return QRect(geo.right() - self._wide_px + 1, y, self._wide_px, panel_h)
        return QRect(geo.left(), y, self._wide_px, panel_h)

    def _collapsed_geometry(self, edge: str) -> QRect:
        geo = self._screen_geo()
        screen_h = geo.height()
        # 与展开态使用相同的 y 基准，避免展开/收起时斜向移动
        panel_h = min(self._panel_h, screen_h - 20)
        y = geo.top() + (screen_h - panel_h) // 2
        if edge == EDGE_RIGHT:
            x = geo.right() - self._tab_w + 1
        else:
            x = geo.left()
        return QRect(x, y, self._tab_w, self._tab_h)

    def _snap_to_edge(self, edge: str, expand: bool = True, animate: bool = True):
        was_expanded = self._expanded
        self.edge = edge

        self._expanded = expand

        target = self._expanded_geometry(edge) if expand else self._collapsed_geometry(edge)

        for btn in self._buttons.values():
            btn.setVisible(expand)

        if animate and was_expanded != expand:
            self._anim_geometry(target)
        else:
            self._apply_geometry(target)

        try:
            ConfigManager().update(sidebar_edge=edge, sidebar_expanded=expand)
        except Exception as e:
            logger.warning(f"持久化侧边栏状态失败: {e}")

    def _apply_geometry(self, rect: QRect):
        self.setGeometry(rect)

    def _anim_geometry(self, target: QRect):
        if self._anim is not None:
            try:
                self._anim.stop()
                self._anim.deleteLater()
            except Exception:
                pass
        cur = self.geometry()
        anim = QPropertyAnimation(self, b"geometry", self)
        anim.setDuration(ANIM_MS)
        anim.setStartValue(cur)
        anim.setEndValue(target)
        anim.start()
        self._anim = anim

    def _nearest_edge(self, pos: QPoint) -> str:
        geo = self._screen_geo()
        dist_left = abs(pos.x() - geo.left())
        dist_right = abs(pos.x() - geo.right())
        return EDGE_LEFT if dist_left < dist_right else EDGE_RIGHT

    # ---------- 展开 / 收起 ----------
    def _expand(self):
        if self._expanded or self._ball_mode:
            return
        self._snap_to_edge(self.edge, expand=True, animate=True)

    def _collapse(self):
        # round48：PySide6 无 sip 模块，改用 shiboken6 检查对象是否已销毁
        try:
            from shiboken6 import isValid
            if not isValid(self):
                return
        except ImportError:
            try:
                self.isVisible()
            except RuntimeError:
                return
        if not self._expanded or self._dragging:
            return
        self._snap_to_edge(self.edge, expand=False, animate=True)

    # ---------- 悬停事件 ----------
    def enterEvent(self, event):
        if not self._dragging and not self._ball_mode:
            self._expand()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self._dragging and not self._ball_mode:
            QTimer.singleShot(250, self._maybe_collapse)
        super().leaveEvent(event)

    def _maybe_collapse(self):
        if self._dragging or self._ball_mode:
            return
        if not self._expanded:
            return
        cursor_pos = QCursor.pos()
        if not self.geometry().contains(cursor_pos):
            if self._open_views:
                return
            self._collapse()

    # ---------- 鼠标事件（拖动坍缩为球） ----------
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton:
            return
        self._dragging = True
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        self._saved_edge = self.edge
        self._saved_expanded = self._expanded
        self._saved_full_geo = self.geometry()
        self._ball_mode = True
        self.clearMask()
        for btn in self._buttons.values():
            btn.setVisible(False)
        ball_geo = QRect(
            event.globalPosition().toPoint().x() - BALL_SIZE // 2,
            event.globalPosition().toPoint().y() - BALL_SIZE // 2,
            BALL_SIZE, BALL_SIZE
        )
        self.setGeometry(ball_geo)
        self._expanded = False
        self.grabMouse()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self._dragging:
            return
        if self._ball_mode:
            self.move(
                event.globalPosition().toPoint().x() - BALL_SIZE // 2,
                event.globalPosition().toPoint().y() - BALL_SIZE // 2
            )
        else:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if not self._dragging:
            return
        self._dragging = False
        self.releaseMouse()
        if self._ball_mode:
            self._ball_mode = False
            for btn in self._buttons.values():
                btn.setVisible(True)
        center = self.frameGeometry().center()
        new_edge = self._nearest_edge(center)
        self._snap_to_edge(new_edge, expand=True, animate=True)
        event.accept()


    # ---------- 按钮动作 ----------
    def _on_button(self, key: str):
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
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

    def _toggle_view(self, key: str, view_cls):
        """切换视图：已在内存中 → 关闭；否则新建。按钮立即禁用防连点。"""
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

    def _open_graph(self):
        from ui.graph_editor import GraphEditor
        self._toggle_view("graph", GraphEditor)

    def _exit_clicked(self):
        """退出：立即禁用按钮防连点，再请求退出流程。"""
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

    def _on_mute_mode_changed(self, enabled: bool):
        mute_btn = self._buttons.get("mute")
        if mute_btn is not None:
            mute_btn.setChecked(enabled)

    def _on_focus_state_changed(self, *args):
        self._update_focus_button_svg()

    def _update_focus_button_svg(self):
        try:
            from ui.focus_view import _get_global_engine
            eng = _get_global_engine()
            active = eng.is_active
            btn = self._buttons.get("focus")
            if btn is None:
                return
            btn.set_svg(SVG_BELL_OFF if active else SVG_BELL)
        except Exception:
            pass

    def closeEvent(self, event):
        # P0 修复：停止并清理动画，防止已销毁 widget 被动画访问
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
