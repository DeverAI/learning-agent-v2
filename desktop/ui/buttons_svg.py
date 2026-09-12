"""按钮 SVG 图标渲染工具。将 icons.py 中的 SVG 变量挂到按钮上。"""
from PySide6.QtWidgets import QPushButton
from PySide6.QtCore import QSize, QByteArray, Qt
from PySide6.QtGui import QPixmap, QPainter, QCursor
from PySide6.QtSvg import QSvgRenderer


def svg_pixmap(svg_text: str, size: int = 18, color: str = None) -> QPixmap:
    """渲染 SVG 字符串为 QPixmap。"""
    if color is None:
        from ui.themes import get_icon_color
        color = get_icon_color()
    colored = svg_text.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(colored.encode("utf-8")))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    if renderer.isValid():
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()
    return pm


def icon_button(svg_text: str, tooltip: str = "", size: int = 32) -> QPushButton:
    """创建一个带 SVG 图标的方形按钮。"""
    btn = QPushButton("")
    btn.setIcon(svg_pixmap(svg_text))
    btn.setIconSize(QSize(18, 18))
    btn.setToolTip(tooltip)
    btn.setFixedSize(size, size)
    btn.setCursor(QCursor(Qt.PointingHandCursor))
    btn.setStyleSheet("""
        QPushButton {
            background: #1e293b; color: #cbd5e1; border: 1px solid #334155;
            border-radius: 6px; padding: 3px;
        }
        QPushButton:hover { background: #263449; border-color: #475569; }
        QPushButton:pressed { background: #111c2e; }
    """)
    return btn
