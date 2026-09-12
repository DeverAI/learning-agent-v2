"""OISystem 吸附式侧边栏 V2 — 视觉与交互优化版。

相对 sidebar.py 的改动（纯展示/交互层，业务逻辑不变）：
- 收起态：加宽为 14px 渐变标签，呼吸发光，三道握把纹，始终可见便于唤醒
- 展开/收起动画：加入缓动曲线（OutCubic / InCubic），更顺滑
- 拖球：外发光环 + 投影 + 中心核；靠近屏幕边缘时球体变为强调色，
  并在目标边缘显示一条吸附预览光带（独立 overlay）
- 梯形面板：纵向微渐变背景、屏幕侧边缘辉光、内缘高光更精致
- 修复：收起态真正收窄（旧版收起/展开同宽导致热区被自身挡死）
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
from ui.themes import ThemeManager, get_icon_color
from utils.helpers import logger
from utils.exceptions import FocusLockedError
from config.settings import ConfigManager

EDGE_LEFT = "left"
EDGE_RIGHT = "right"

ANIM_MS = 260

TAPER_RATIO = 0.50
CORNER_RADIUS = 12
BUTTON_H = 40
BUTTON_GAP = 6
MARGIN_V = 14
TAB_W = 14               # 收起态标签宽度（真正收窄）
MIN_TAB_H = 72
MAX_TAB_H = 180
BALL_SIZE = 46
SNAP_DIST = 90           # 拖球距边缘多少 px 内显示吸附预览


def _panel_depth() -> int:
    # 内缘 = wide_px * TAPER_RATIO 必须 >= 52（按钮 24px + 两侧各 14px 留白）
    return max(64, int(len(SIDEBAR_BUTTONS) * 18))


def _panel_height() -> int:
    n = len(SIDEBAR_BUTTONS)
    return max(120, MARGIN_V * 2 + n * BUTTON_H + max(0, n - 1) * BUTTON_GAP)


def _tab_height() -> int:
    return max(MIN_TAB_H, min(MAX_TAB_H, int(_panel_height() * 0.55)))


# ---------- 吸附预览光带 ----------
class _SnapHint(QWidget):
    """拖球靠近边缘时，在该边缘显示的发光预览条。"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._accent = QColor("#3b82f6")

    def show_at(self, edge: str, accent: str, y_center: int = None):
        self._accent = QColor(accent)
        # r39 P1 修复：primaryScreen() 可能返回 None（无显示器/RDP 断开）
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        h = _panel_height()
        if y_center is None:
            y = geo.top() + (geo.height() - h) // 2
        else:
            y = max(geo.top(), min(geo.bottom() - h, y_center - h // 2))
        if edge == EDGE_RIGHT:
            self.setGeometry(geo.right() - 3, y, 4, h)
        else:
            self.setGeometry(geo.left(), y, 4, h)
        self.show()
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        g = QLinearGradient(0, 0, 0, h)
        c = QColor(self._accent)
        g.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 0))
        g.setColorAt(0.5, QColor(c.red(), c.green(), c.blue(), 200))
        g.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
        p.fillRect(0, 0, w, h, g)
        p.end()


# ---------- 按钮容器：仅做布局，不自己裁剪（由 Sidebar 顶层 mask 统一裁剪） ----------
class _ButtonClipWidget(QWidget):
    """按钮父容器，跟随 Sidebar 尺寸，但不自行施加 mask，避免双重裁剪。"""

    def __init__(self, edge: str, parent=None):
        super().__init__(parent)
        self._edge = edge

    def set_edge(self, edge: str):
        self._edge = edge


# ---------- 梯形按钮 ----------
class SidebarButton(QPushButton):
    """侧边栏按钮，宽度随面板内缘自适应。"""

    def __init__(self, key: str, label: str, svg_str: str, accent: str, edge: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.label_text = label
        self.svg_str = svg_str
        self.accent = accent
        self._edge = edge          # "left" or "right"
        self.setFixedHeight(BUTTON_H)
        self.setMinimumWidth(24)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)

    def set_edge(self, edge: str):
        self._edge = edge
        self.update()

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
            # 选中微光底（竖条高光由面板统一画在屏幕侧缝隙）
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 26))
            p.drawRoundedRect(QRectF(4, 2, rect.width() - 6, rect.height() - 4), 9, 9)
        elif self.underMouse():
            p.setPen(Qt.NoPen)
            # 悬停微光：用主题强调色半透明
            p.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 22))
            p.drawRoundedRect(QRectF(4, 2, rect.width() - 6, rect.height() - 4), 9, 9)

        if self.isChecked():
            color = self.accent
        elif self.underMouse():
            # 悬停时：亮色主题用深色，暗色主题用亮色
            from ui.themes import is_light_theme
            color = "#0f172a" if is_light_theme() else "#f8fafc"
        else:
            color = get_icon_color()

        pm = render_svg(self.svg_str, size=18, color=color)
        icon_x = (rect.width() - 18) // 2
        p.drawPixmap(icon_x, 4, pm)
        p.setPen(QColor(color))
        p.setFont(QFont("Microsoft YaHei", 7))
        p.drawText(QRectF(0, 24, rect.width(), 14), Qt.AlignCenter, self.label_text)
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
        self._animating = False       # 动画进行中，geometry 与 _expanded 不一致
        self._layout = None
        self._anim = None
        self._open_views = {}
        self._mute_observer = None
        self._leave_timer_id = None   # 防 leaveEvent 累积定时器

        self._wide_px = _panel_depth()
        self._panel_h = _panel_height()
        self._tab_h = _tab_height()

        self._accent = ThemeManager().current_theme.get("accent", "#3b82f6")
        
        # 监听主题变化
        self._theme_manager = ThemeManager()
        self._update_theme_colors()
        # 注册配置变化观察者
        ConfigManager().add_observer(self)

        # 收起态呼吸动画
        self._breath = 0.0
        self._breath_timer = QTimer(self)
        self._breath_timer.timeout.connect(self._on_breath)
        self._breath_timer.start(50)
        self._breath_clock = QElapsedTimer()
        self._breath_clock.start()

        # 吸附预览光带
        self._snap_hint = _SnapHint()

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
        self.setObjectName("Sidebar")

        # 按钮放在 clip container 里，由 Sidebar 顶层 mask 统一裁剪
        self._clip = _ButtonClipWidget(self.edge, self)
        self._clip.setGeometry(self.rect())
        self._layout = QVBoxLayout(self._clip)
        self._layout.setSpacing(BUTTON_GAP)
        # 内容靠屏幕侧，远离收窄边（margin 由 _update_clip_geometry 动态调整）
        self._layout.setContentsMargins(0, MARGIN_V, 0, MARGIN_V)
        self._layout.addStretch(1)
        for key, label, svg in get_sidebar_buttons():
            btn = SidebarButton(key, label, svg, self._accent, self.edge, self._clip)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self._on_button(k))
            self._layout.addWidget(btn, 0, Qt.AlignCenter)
            self._buttons[key] = btn
        self._layout.addStretch(1)

    def _update_clip_geometry(self):
        self._clip.setGeometry(self.rect())
        # 把按钮内容推向屏幕侧，远离收窄边
        w = self.width()
        offset = self._inner_offset(w) if self._expanded else 0
        if self.edge == EDGE_RIGHT:
            # 左 margin = offset（收窄边），右 margin = 小（屏幕侧）
            self._layout.setContentsMargins(offset + 6, MARGIN_V, 6, MARGIN_V)
        else:
            self._layout.setContentsMargins(6, MARGIN_V, offset + 6, MARGIN_V)

    # ---------- 呼吸动画 ----------
    def _on_breath(self):
        # 仅收起态需要呼吸重绘
        if not self._expanded and not self._ball_mode and not self._dragging:
            t = self._breath_clock.elapsed() / 1000.0
            import math
            self._breath = (math.sin(t * 2.2) + 1.0) / 2.0   # 0..1
            self.update()

    # ---------- 梯形几何 ----------
    def _inner_offset(self, width: int) -> int:
        return max(2, int(width * (1 - TAPER_RATIO)))

    def _trapezoid_path(self, w: int, h: int) -> QPainterPath:
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
        if self._ball_mode:
            self.clearMask()
            return
        w, h = self.width(), self.height()
        if w <= 4 or h <= 4:
            return
        offset = self._inner_offset(w)
        # 太窄时（收起标签）用矩形，否则纯四点梯形（不用弧线，保证 mask 可靠）
        if w < 24 or w <= offset + 6:
            self.setMask(QRegion(0, 0, w, h))
            return
        from PySide6.QtGui import QPolygon
        from PySide6.QtCore import QPoint as _QP
        if self.edge == EDGE_RIGHT:
            poly = QPolygon([_QP(offset, 0), _QP(w, 0), _QP(w, h), _QP(offset, h)])
        else:
            poly = QPolygon([_QP(0, 0), _QP(w - offset, 0), _QP(w - offset, h), _QP(0, h)])
        self.setMask(QRegion(poly))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_mask()
        self._update_clip_geometry()

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

        # 动画期间按实际宽度决定画什么，_expanded 可能还未/已切换
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

    def _paint_ball(self, p: QPainter, w: int, h: int):
        cx, cy = w / 2.0, h / 2.0
        r = min(w, h) / 2.0 - 2
        near = self._ball_near_edge is not None
        accent = QColor(self._accent)

        # 外发光（近边缘时用强调色，否则冷灰）
        glow = QColor(accent if near else QColor(100, 116, 139))
        rg = QRadialGradient(QPointF(cx, cy), r + 6)
        rg.setColorAt(0.0, QColor(glow.red(), glow.green(), glow.blue(), 0))
        rg.setColorAt(max(0.0, (r - 4) / (r + 6)),
                      QColor(glow.red(), glow.green(), glow.blue(), 0))
        rg.setColorAt(0.85, QColor(glow.red(), glow.green(), glow.blue(), 110 if near else 60))
        rg.setColorAt(1.0, QColor(glow.red(), glow.green(), glow.blue(), 0))
        p.setPen(Qt.NoPen)
        p.setBrush(rg)
        p.drawEllipse(QPointF(cx, cy), r + 6, r + 6)

        # 球体
        bg = QRadialGradient(QPointF(cx - r * 0.3, cy - r * 0.35), r * 1.6)
        bg.setColorAt(0.0, QColor(51, 65, 85, 235))
        bg.setColorAt(1.0, QColor(15, 23, 42, 235))
        p.setBrush(bg)
        p.setPen(QPen(QColor(glow.red(), glow.green(), glow.blue(), 190), 1.6))
        p.drawEllipse(QPointF(cx, cy), r, r)

        # 中心核
        core_r = 4.5 if near else 3.5
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(accent if near else QColor(203, 213, 225)))
        p.drawEllipse(QPointF(cx, cy), core_r, core_r)

    def _paint_tab(self, p: QPainter, w: int, h: int):
        """收起态：窄渐变标签 + 呼吸光 + 握把纹。"""
        hovered = self.underMouse()
        accent = QColor(self._accent)

        # 背景纵向渐变
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(30, 41, 59, 200))
        g.setColorAt(0.5, QColor(15, 23, 42, 225))
        g.setColorAt(1.0, QColor(30, 41, 59, 200))
        path = QPainterPath()
        r = 7
        if self.edge == EDGE_RIGHT:
            path.addRoundedRect(QRectF(0, 0, w + r, h), r, r)   # 右缘出界被裁，左缘圆角
        else:
            path.addRoundedRect(QRectF(-r, 0, w + r, h), r, r)
        p.setClipPath(path)
        p.fillPath(path, g)

        # 呼吸高光（屏幕侧边缘）
        alpha = int(40 + self._breath * 90) + (60 if hovered else 0)
        alpha = min(220, alpha)
        c = QColor(accent.red(), accent.green(), accent.blue(), alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        if self.edge == EDGE_RIGHT:
            p.drawRect(w - 2, 6, 2, h - 12)
        else:
            p.drawRect(0, 6, 2, h - 12)

        # 握把纹（三条短横线，居中）
        line_c = QColor(226, 232, 240, 150 if hovered else 100)
        p.setPen(QPen(line_c, 1.6, Qt.SolidLine, Qt.RoundCap))
        cy = h / 2.0
        lw = min(6, w - 6)
        lx = (w - lw) / 2.0
        for i in (-1, 0, 1):
            y = cy + i * 6
            p.drawLine(QPointF(lx, y), QPointF(lx + lw, y))
        p.setClipping(False)

    def _paint_panel(self, p: QPainter, w: int, h: int):
        path = self._trapezoid_path(w, h)
        if path.isEmpty():
            return
        p.setClipPath(path)

        # 主体纵向微渐变
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(23, 32, 51, 242))
        g.setColorAt(0.5, QColor(15, 23, 42, 242))
        g.setColorAt(1.0, QColor(23, 32, 51, 242))
        p.fillPath(path, g)

        offset = self._inner_offset(w)

        # 屏幕侧辉光（贴屏幕那一边，强调色微光）
        accent = QColor(self._accent)
        glow_w = min(18, max(8, w // 4))
        if self.edge == EDGE_RIGHT:
            eg = QLinearGradient(w - glow_w, 0, w, 0)
        else:
            eg = QLinearGradient(glow_w, 0, 0, 0)
        eg.setColorAt(0.0, QColor(accent.red(), accent.green(), accent.blue(), 0))
        eg.setColorAt(1.0, QColor(accent.red(), accent.green(), accent.blue(), 34))
        p.fillRect(QRectF(0, 0, w, h), eg)

        # 屏幕侧亮线
        p.setPen(QPen(QColor(255, 255, 255, 46), 1))
        if self.edge == EDGE_RIGHT:
            p.drawLine(QPointF(w - 0.5, CORNER_RADIUS + 1), QPointF(w - 0.5, h - CORNER_RADIUS - 1))
        else:
            p.drawLine(QPointF(0.5, CORNER_RADIUS + 1), QPointF(0.5, h - CORNER_RADIUS - 1))

        # 内缘高光（收窄边），仅在 offset 足够时绘制
        if offset > 0 and offset < w - 4:
            p.setPen(QPen(QColor(255, 255, 255, 22), 1))
            if self.edge == EDGE_RIGHT:
                p.drawLine(QPointF(offset + 0.5, CORNER_RADIUS + 1), QPointF(offset + 0.5, h - CORNER_RADIUS - 1))
            else:
                p.drawLine(QPointF(w - offset - 0.5, CORNER_RADIUS + 1), QPointF(w - offset - 0.5, h - CORNER_RADIUS - 1))

        # 选中按钮的竖条高光：画在图标与屏幕边缘之间的缝隙里
        for btn in self._buttons.values():
            if btn.isChecked() and btn.isVisible():
                cy = btn.geometry().center().y()
                p.setPen(Qt.NoPen)
                p.setBrush(accent)
                if self.edge == EDGE_RIGHT:
                    p.drawRoundedRect(QRectF(w - 6, cy - 13, 3, 26), 1.5, 1.5)
                else:
                    p.drawRoundedRect(QRectF(3, cy - 13, 3, 26), 1.5, 1.5)
                break

    # ---------- 几何计算 ----------
    def _screen_geo(self) -> QRect:
        # round48：RDP 断开/无显示器时 primaryScreen() 可能返回 None
        screen = QApplication.primaryScreen()
        if screen is None:
            return QRect(0, 0, 1920, 1080)
        geo = screen.availableGeometry()
        return geo if geo.isValid() else QRect(0, 0, 1920, 1080)

    def _expanded_geometry(self, edge: str, y_center: int = None) -> QRect:
        geo = self._screen_geo()
        panel_h = min(self._panel_h, geo.height() - 20)
        if y_center is None:
            y = geo.top() + (geo.height() - panel_h) // 2
        else:
            y = max(geo.top(), min(geo.bottom() - panel_h, y_center - panel_h // 2))
        if edge == EDGE_RIGHT:
            return QRect(geo.right() - self._wide_px + 1, y, self._wide_px, panel_h)
        return QRect(geo.left(), y, self._wide_px, panel_h)

    def _collapsed_geometry(self, edge: str, y_center: int = None) -> QRect:
        geo = self._screen_geo()
        if y_center is None:
            y = geo.top() + (geo.height() - self._tab_h) // 2
        else:
            y = max(geo.top(), min(geo.bottom() - self._tab_h, y_center - self._tab_h // 2))
        if edge == EDGE_RIGHT:
            x = geo.right() - TAB_W + 1
        else:
            x = geo.left()
        return QRect(x, y, TAB_W, self._tab_h)

    def _snap_to_edge(self, edge: str, expand: bool = True, animate: bool = True, y_center: int = None):
        was_expanded = self._expanded
        self.edge = edge
        self._expanded = expand
        target = self._expanded_geometry(edge, y_center) if expand else self._collapsed_geometry(edge, y_center)

        # 同步按钮高光方向
        for btn in self._buttons.values():
            btn.set_edge(edge)
            btn.setVisible(expand)
        if self._clip is not None:
            self._clip.set_edge(edge)
            self._clip.setVisible(expand)

        if animate and was_expanded != expand:
            self._anim_geometry(target, expanding=expand)
        else:
            self.setGeometry(target)

        try:
            ConfigManager().update(sidebar_edge=edge, sidebar_expanded=expand)
        except Exception as e:
            logger.warning(f"持久化侧边栏状态失败: {e}")

    def _anim_geometry(self, target: QRect, expanding: bool = True):
        if self._anim is not None:
            try:
                self._anim.stop()
                self._anim.deleteLater()
            except Exception:
                pass
        # P1 修复：动画设置 parent=self，确保 sidebar 销毁时动画同步销毁
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
        # P1 修复：动画 finished 信号可能在 closeEvent 后入队，先判活再访问
        try:
            from shiboken6 import isValid
            if not isValid(self):
                return
        except ImportError:
            try:
                self.isVisible()
            except RuntimeError:
                return
        self._animating = False
        # 动画结束后确保 mask/clip 与最终状态一致
        self._update_mask()
        self._update_clip_geometry()

    def _nearest_edge(self, pos: QPoint) -> str:
        geo = self._screen_geo()
        return EDGE_LEFT if abs(pos.x() - geo.left()) < abs(pos.x() - geo.right()) else EDGE_RIGHT

    # ---------- 展开 / 收起 ----------
    def _expand(self):
        if self._expanded or self._ball_mode:
            return
        # 以当前收起标签的中心为锚点展开，避免视觉跳动
        self._snap_to_edge(self.edge, expand=True, animate=True,
                           y_center=self.geometry().center().y())

    def _collapse(self):
        # r39 P1 修复：PySide6 无 sip 模块，改用 shiboken6 检查对象是否已销毁
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
        # 以当前面板中心为锚点收起
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
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton:
            return
        self._dragging = True
        self._ball_mode = True
        self._ball_near_edge = None
        self.clearMask()
        for btn in self._buttons.values():
            btn.setVisible(False)
        if self._clip is not None:
            self._clip.setVisible(False)
        gp = event.globalPosition().toPoint()
        self.setGeometry(gp.x() - BALL_SIZE // 2, gp.y() - BALL_SIZE // 2, BALL_SIZE, BALL_SIZE)
        self._expanded = False
        self.grabMouse()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent):
        if not self._dragging or not self._ball_mode:
            return
        gp = event.globalPosition().toPoint()
        self.move(gp.x() - BALL_SIZE // 2, gp.y() - BALL_SIZE // 2)
        # 靠近边缘检测
        geo = self._screen_geo()
        dl = abs(gp.x() - geo.left())
        dr = abs(gp.x() - geo.right())
        near = None
        if min(dl, dr) < SNAP_DIST:
            near = EDGE_LEFT if dl < dr else EDGE_RIGHT
        if near != self._ball_near_edge:
            self._ball_near_edge = near
            self.update()
        # 光带实时跟随鼠标位置
        if near is not None:
            self._snap_hint.show_at(near, self._accent, gp.y())
        else:
            self._snap_hint.hide()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if not self._dragging:
            return
        self._dragging = False
        self.releaseMouse()
        self._snap_hint.hide()
        if self._ball_mode:
            self._ball_mode = False
            self._ball_near_edge = None
            self._expanded = False  # 球态结束，在 _snap_to_edge 中会设为 True
            for btn in self._buttons.values():
                btn.setVisible(True)
            if self._clip is not None:
                self._clip.setVisible(True)
        center = self.frameGeometry().center()
        new_edge = self._nearest_edge(center)
        self._snap_to_edge(new_edge, expand=True, animate=True, y_center=center.y())
        event.accept()

    # ---------- 按钮动作 ----------
    def _on_button(self, key: str):
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        self.update()   # 刷新面板上的选中高光条
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
            self._open_lecture()

    def _toggle_view(self, key: str, view_cls):
        btn = self._buttons.get(key)
        if btn is not None:
            btn.setEnabled(False)
        existing = self._open_views.get(key)
        if existing is not None:
            try:
                # 调用 close() 触发各视图的 closeEvent 清理（存档、断开信号、停止定时器等）
                existing.close()
                existing.deleteLater()
            except Exception:
                pass
            self._open_views.pop(key, None)
            if btn is not None:
                btn.setChecked(False)
                btn.setEnabled(True)
            # 强制垃圾回收，确保窗口立即释放
            import gc
            gc.collect()
            return
        try:
            view = view_cls(self)
            def _on_view_closed():
                if self._open_views.get(key) is view:
                    self._open_views.pop(key, None)
                if btn is not None:
                    btn.setChecked(False)
                    btn.setEnabled(True)
                # P1 修复：使用 lambda 检查 sidebar 存活状态，避免 C++ 对象销毁后回调崩溃
                def _safe_collapse():
                    try:
                        from shiboken6 import isValid
                        if isValid(self):
                            self._collapse()
                    except ImportError:
                        try:
                            self._collapse()
                        except RuntimeError:
                            pass
                    except RuntimeError:
                        pass
                QTimer.singleShot(50, _safe_collapse)
            orig_close = view.closeEvent
            # P1 修复：防止 closeEvent 被调用多次导致重复清理
            _close_called = [False]
            def _patched_close(event):
                if _close_called[0]:
                    return
                _close_called[0] = True
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
            # 专注/ZZOI 锁定拦截：request_exit 已弹提示，绝对不能再 quit，
            # 否则锁定保护会被退出流程绕过。
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
        self._snap_hint.hide()
        self._snap_hint.deleteLater()
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
        # r39 P0 修复：断开全局引擎信号，避免 Sidebar 销毁后引擎信号触发已销毁对象的槽函数
        try:
            from ui.focus_view import _get_global_engine
            eng = _get_global_engine()
            if eng is not None:
                eng.focus_started.disconnect(self._on_focus_state_changed)
                eng.focus_ended.disconnect(self._on_focus_state_changed)
        except (TypeError, RuntimeError):
            # 信号从未连接或已断开
            pass
        except Exception:
            pass
        # 取消配置观察者注册
        ConfigManager().remove_observer(self)
        super().closeEvent(event)

    def _update_theme_colors(self):
        """更新侧边栏颜色以响应主题变化"""
        theme = self._theme_manager.current_theme
        self._accent = theme.get("accent", "#3b82f6")
        # 更新所有按钮的颜色
        for btn in self._buttons.values():
            btn.set_accent(self._accent)
        self.update()

    def on_config_changed(self):
        """配置变化回调，更新主题颜色"""
        self._update_theme_colors()
