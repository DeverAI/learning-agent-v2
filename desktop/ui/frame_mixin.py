"""纯前端：无边框圆角纯黑底窗口混入。

提供 _apply_frame(title) 方法，一键把 QWidget/QDialog 改成：
- 无边框 + 圆角 12px + 1px 细描边
- 纯黑背景 (#000000)
- 顶部 36px 可拖动区域
- 四边/四角鼠标悬浮变光标，按住拖拽可 resize（最小 320×200）
- 右上角关闭按钮
- 首次 show 自动屏幕居中
"""
from PySide6.QtCore import Qt, QPoint, QSize, QByteArray
from PySide6.QtGui import QPainter, QColor, QFont, QCursor, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPushButton, QLabel, QApplication
from ui.icons import SVG_STANDBY

BORDER = 8          # 边缘检测宽度
MIN_W, MIN_H = 320, 200


def _edge_at(pos, w, h):
    """返回边缘方向位掩码。"""
    e = 0
    if pos.x() < BORDER:
        e |= 1  # left
    elif pos.x() > w - BORDER:
        e |= 2  # right
    if pos.y() < BORDER:
        e |= 4  # top
    elif pos.y() > h - BORDER:
        e |= 8  # bottom
    return e


EDGE_CURSORS = {
    1: Qt.SizeHorCursor,       # left
    2: Qt.SizeHorCursor,       # right
    4: Qt.SizeVerCursor,       # top
    8: Qt.SizeVerCursor,       # bottom
    1 | 4: Qt.SizeFDiagCursor, # top-left
    2 | 8: Qt.SizeFDiagCursor, # bottom-right
    1 | 8: Qt.SizeBDiagCursor, # bottom-left
    2 | 4: Qt.SizeBDiagCursor, # top-right
}


class RoundedFrameMixin:
    _RADIUS = 12
    _TITLE_H = 36

    def _apply_frame(self, title: str = ""):
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)

        self._frame_close = QPushButton("", self)
        self._frame_close.setFixedSize(28, 28)
        self._frame_close.setCursor(Qt.PointingHandCursor)
        # 渲染 SVG_STANDBY 为图标（白色 standby 图标 + 红色悬停）
        _renderer = QSvgRenderer(QByteArray(SVG_STANDBY.replace("currentColor", "#ffffff").encode("utf-8")))
        _pm = QPixmap(16, 16)
        _pm.fill(Qt.transparent)
        if _renderer.isValid():
            _painter = QPainter(_pm)
            _painter.setRenderHint(QPainter.Antialiasing)
            _renderer.render(_painter)
            _painter.end()
        self._frame_close.setIcon(_pm)
        self._frame_close.setIconSize(QSize(16, 16))
        self._frame_close.setStyleSheet("""
            QPushButton {
                background: #64748b; color: #ffffff;
                border: 2px solid #94a3b8; border-radius: 14px;
            }
            QPushButton:hover { background: #dc2626; color: white; border-color: #ef4444; }
        """)
        self._frame_close.clicked.connect(self.close)

        if title:
            self._frame_title = QLabel(title, self)
            self._frame_title.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
            from ui.themes import ThemeManager
            _t = ThemeManager().current_theme
            self._frame_title.setStyleSheet(f"color: {_t.get('warning', _t.get('accent', '#fbbf24'))}; background: transparent;")
        else:
            self._frame_title = None

        self._frame_drag_from = None    # 拖拽移动起点
        self._frame_resize_edge = 0     # 拖拽 resize 方向
        self._frame_resize_from = None  # resize 起点全局坐标
        self._frame_resize_geo = None   # resize 起点窗口几何
        self._frame_centered = False
        self._min_size = QSize(MIN_W, MIN_H)

        self._orig_paintEvent = self.paintEvent
        self._orig_resizeEvent = self.resizeEvent
        self._orig_showEvent = self.showEvent
        self._orig_mousePressEvent = self.mousePressEvent
        self._orig_mouseMoveEvent = self.mouseMoveEvent
        self._orig_mouseReleaseEvent = self.mouseReleaseEvent

        self.paintEvent = self._rf_paint
        self.resizeEvent = self._rf_resize
        self.showEvent = self._rf_show
        self.mousePressEvent = self._rf_mouse_press
        self.mouseMoveEvent = self._rf_mouse_move
        self.mouseReleaseEvent = self._rf_mouse_release

    def _set_min_size(self, w, h):
        self._min_size = QSize(w, h)

    def _rf_paint(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        # 从主题读取背景色和边框色
        try:
            from ui.themes import ThemeManager
            t = ThemeManager().current_theme
            p.setBrush(QColor(t.get("bg", "#000000")))
            p.setPen(QColor(t.get("border", "#283447")))
        except Exception:
            p.setBrush(QColor(0, 0, 0))
            p.setPen(QColor(40, 50, 70))
        p.drawRoundedRect(rect, self._RADIUS, self._RADIUS)
        p.end()
        self._orig_paintEvent(event)

    def _rf_resize(self, event):
        self._frame_close.move(self.width() - 36, 6)
        if getattr(self, "_frame_title", None):
            self._frame_title.move(16, 8)
        self._orig_resizeEvent(event)

    def _rf_show(self, event):
        if not self._frame_centered:
            # r39 P1 修复：primaryScreen() 可能返回 None（无显示器/RDP 断开）
            screen = QApplication.primaryScreen()
            if screen is None:
                # round48：无屏幕时跳过居中，但必须继续转发 showEvent，
                # 否则依赖 showEvent 的窗口（FocusView 刷新定时器等）永远不启动
                self._frame_centered = True
            else:
                geo = screen.availableGeometry()
                center = geo.center()
                r = self.rect()
                self.move(center.x() - r.width() // 2, center.y() - r.height() // 2)
                self._frame_centered = True
                lay = self.layout()
                if lay:
                    m = lay.contentsMargins()
                    need_top = self._TITLE_H + 8
                    if m.top() < need_top:
                        lay.setContentsMargins(m.left(), need_top, m.right(), m.bottom())
        try:
            self._orig_showEvent(event)
        except Exception:
            pass

    def _rf_mouse_press(self, event):
        if event.button() != Qt.LeftButton:
            self._frame_drag_from = None
            self._frame_resize_edge = 0
            self._orig_mousePressEvent(event)
            return
        pos = event.position().toPoint()
        w, h = self.width(), self.height()
        edge = _edge_at(pos, w, h)
        if edge and pos.y() >= self._TITLE_H:
            # 边框 resize
            self._frame_resize_edge = edge
            self._frame_resize_from = event.globalPosition().toPoint()
            self._frame_resize_geo = self.geometry()
            self._frame_drag_from = None
            event.accept()
        elif pos.y() < self._TITLE_H:
            # 标题栏拖拽
            self._frame_drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._frame_resize_edge = 0
            event.accept()
        else:
            self._frame_drag_from = None
            self._frame_resize_edge = 0
            self._orig_mousePressEvent(event)

    def _rf_mouse_move(self, event):
        # resize 进行中
        if event.buttons() == Qt.LeftButton and self._frame_resize_edge:
            delta = event.globalPosition().toPoint() - self._frame_resize_from
            g = self._frame_resize_geo
            e = self._frame_resize_edge
            x, y, w, h = g.x(), g.y(), g.width(), g.height()
            if e & 1:   # left
                x = g.x() + delta.x()
                w = g.width() - delta.x()
            if e & 2:   # right
                w = g.width() + delta.x()
            if e & 4:   # top
                y = g.y() + delta.y()
                h = g.height() - delta.y()
            if e & 8:   # bottom
                h = g.height() + delta.y()
            if w < self._min_size.width():
                if e & 1:
                    x = g.x() + g.width() - self._min_size.width()
                w = self._min_size.width()
            elif hasattr(self, 'maximumWidth') and self.maximumWidth() < 16777215 and w > self.maximumWidth():
                w = self.maximumWidth()
            if h < self._min_size.height():
                if e & 4:
                    y = g.y() + g.height() - self._min_size.height()
                h = self._min_size.height()
            elif hasattr(self, 'maximumHeight') and self.maximumHeight() > 0 and h > self.maximumHeight():
                h = self.maximumHeight()
            self.setGeometry(x, y, w, h)
            event.accept()
            return
        # 拖拽移动进行中
        if event.buttons() == Qt.LeftButton and self._frame_drag_from is not None:
            self.move(event.globalPosition().toPoint() - self._frame_drag_from)
            event.accept()
            return
        self._frame_drag_from = None
        self._frame_resize_edge = 0
        # 空闲时更新鼠标光标
        pos = event.position().toPoint()
        edge = _edge_at(pos, self.width(), self.height())
        if edge and pos.y() >= self._TITLE_H:
            self.setCursor(EDGE_CURSORS.get(edge, Qt.ArrowCursor))
        else:
            self.setCursor(Qt.ArrowCursor)
        self._orig_mouseMoveEvent(event)

    def _rf_mouse_release(self, event):
        """释放鼠标时重置拖拽/resize状态，防止状态残留。"""
        self._frame_drag_from = None
        self._frame_resize_edge = 0
        self._orig_mouseReleaseEvent(event)
