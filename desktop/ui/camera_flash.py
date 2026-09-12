"""截图悬浮球闪光动画。

截图触发时，在屏幕右下角出现一个黑色圆球：
- 白框黑底，中间显示白色眼睛图标 (SVG_EYE)
- 内部由黑变灰两次（类似闪光灯效果）
- 然后缩回消失
"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QPainter, QColor, QPixmap

from ui.icons import SVG_EYE, render_svg
from utils.helpers import logger


class CameraFlash(QWidget):
    """截图闪光浮球。单例，全局只用一个实例。"""

    _instance = None

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # 尺寸参数
        self._ball_size = 48          # 球直径
        self._flash_brightness = 0.3  # 闪光灰度值 0~1, 0=黑, 1=白
        self._opacity = 0.0
        self._is_flashing = False

        # 动画
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._on_flash_tick)

        # 显示眼睛图标
        self._eye_pixmap = render_svg(SVG_EYE, 24, "#e2e8f0")

        # 位置：屏幕右下角
        self._place()
        self.setFixedSize(self._ball_size + 12, self._ball_size + 12)  # 留边

    def _place(self):
        """定位到屏幕右下角，距离边缘 20px。"""
        from PySide6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.right() - self._ball_size - 32
            y = geo.bottom() - self._ball_size - 32
            self.move(x, y)

    # ---------- 公开触发 ----------

    def flash(self):
        """触发闪光动画。外部调用入口。"""
        if self._is_flashing:
            return
        self._is_flashing = True
        self._flash_count = 0           # 已闪次数
        self._flash_brightness = 0.3
        self._opacity = 1.0
        self.setWindowOpacity(1.0)       # 修复：重置 windowOpacity，防止上次 fade_out 后 windowOpacity=0 导致不可见
        self.show()
        self.raise_()
        self.update()
        # 启动闪动计时器（每 120ms 变换一次亮度）
        self._flash_timer.start(120)

    def _on_flash_tick(self):
        """一次闪光：变亮再变暗。"""
        if self._flash_brightness < 0.6:
            # 变亮
            self._flash_brightness = 0.85
        else:
            # 变暗
            self._flash_brightness = 0.25
            self._flash_count += 1

        self.update()

        if self._flash_count >= 2:
            # 两次闪光完毕，缩回消失
            self._fade_out()
        else:
            self._flash_timer.start(120)

    def _fade_out(self):
        """缩回消失动画。"""
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(300)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.finished.connect(self._on_fade_done)
        self._fade_anim.start()

    def _on_fade_done(self):
        self.hide()
        self._opacity = 0.0
        self._is_flashing = False

    # ---------- 绘制 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 整体透明度
        painter.setOpacity(self._opacity)

        cx = self.width() // 2
        cy = self.height() // 2
        r = self._ball_size // 2

        # 外圈（白框）
        painter.setPen(QPen(QColor("#cbd5e1"), 2))  # 浅灰白边框
        # 内圈（黑色底色 + 灰度闪光）
        gray = int(20 + self._flash_brightness * 180)
        painter.setBrush(QColor(gray, gray, gray))
        painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

        # 眼睛图标
        eye_size = 24
        ex = cx - eye_size // 2
        ey = cy - eye_size // 2
        painter.drawPixmap(ex, ey, self._eye_pixmap)

        painter.end()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = CameraFlash()
        return cls._instance

    @classmethod
    def trigger(cls):
        """一键触发闪光。"""
        inst = cls.get_instance()
        inst.flash()
