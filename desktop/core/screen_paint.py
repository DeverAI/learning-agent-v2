"""屏幕绘制原语引擎（学习搭子桌面版·网课 AI 教师）。

设计：Design.md v3「新子系统 3：屏幕绘制原语引擎」。
AI 获得一组屏幕绘制指令原语，黑板/标记/全屏板书全部是指令组合：
- paint_rect(x, y, w, h, color)：用指定颜色（白/黑）涂抹屏幕矩形区域
- write_text(x, y, text, size, color)：在任意位置以任意大小/颜色打字
- open_page(bg_color) / close_page()：独立全屏白板页（深度讲解时切换）
- clear()：清除全部 AI 绘制
- set_click_through(bool)：鼠标穿透开关（穿透时学生可操作底下窗口）

实现：单一全屏透明置顶覆盖窗（WA_TranslucentBackground + WindowStaysOnTopHint），
paintEvent 按 ops 顺序全量重绘。全局热键一键隐藏/恢复。
"""
import math

from PySide6.QtCore import Qt, QRect, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QGuiApplication
from PySide6.QtWidgets import QWidget

# 绘制指令总量上限：超限丢最旧指令（防长时间课堂 ops 无限增长拖慢全量重绘）
_MAX_OPS = 800


def _num(value, default=0.0):
    """有限数值校验：NaN/Inf/非数字一律回退默认值（防脏 op 进 paintEvent 炸整屏）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    return f if math.isfinite(f) else float(default)


def _clamp01(value):
    """比例坐标钳制到 [-0.5, 1.5]：越界一点仍可渲染，离谱值归位。"""
    return max(-0.5, min(1.5, _num(value)))


def _safe_color(value, default="#ffffff"):
    """颜色字符串校验：QColor 不识别（非法名/坏 hex）时回退默认，避免 paintEvent 抛错。"""
    s = str(value or "").strip()
    return s if QColor(s).isValid() else default


class ScreenPaintOverlay(QWidget):
    """全屏透明置顶覆盖窗：AI 屏幕绘制指令的渲染层。"""

    def __init__(self, screen=None):
        super().__init__(None)
        self._screen = screen or QGuiApplication.primaryScreen()
        self._ops = []          # 绘制指令列表，paintEvent 全量重绘
        self._page_mode = False  # 独立白板页模式（不透明背景）
        self._page_bg = "#ffffff"
        self.setWindowFlags(
            Qt.Window
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool  # 不出现在任务栏
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)  # 不抢焦点
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)  # 默认鼠标穿透
        geo = self._screen.geometry()
        self.setGeometry(geo)
        self._click_through = True

    # ---------- 显示控制 ----------

    def show_overlay(self):
        geo = self._screen.geometry()
        self.setGeometry(geo)
        self.show()
        self.raise_()

    def hide_overlay(self):
        self.hide()

    def set_click_through(self, through: bool):
        """鼠标穿透开关：True=学生可操作覆盖层之下的窗口。"""
        self._click_through = bool(through)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, self._click_through)
        # Windows 原生层级穿透：非穿透时移除 WS_EX_TRANSPARENT
        try:
            import win32con
            import win32gui
            hwnd = int(self.winId())
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if self._click_through:
                ex |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED
            else:
                ex &= ~win32con.WS_EX_TRANSPARENT
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, ex)
        except Exception:
            pass  # 非 Windows 或 pywin32 缺失时 Qt 属性兜底

    # ---------- 绘制指令原语 ----------

    def _append_op(self, op: dict):
        """统一入列口：总量有界（超限丢最旧）+ 触发重绘。"""
        self._ops.append(op)
        if len(self._ops) > _MAX_OPS:
            del self._ops[:len(self._ops) - _MAX_OPS]
        self._repaint()

    def paint_rect(self, x: float, y: float, w: float, h: float, color: str = "#ffffff"):
        """用指定颜色涂抹屏幕矩形区域（相对全屏的比例坐标 0~1，便于 AI 与分辨率解耦）。"""
        self._append_op({"op": "rect", "x": _clamp01(x), "y": _clamp01(y),
                         "w": max(0.0, _clamp01(w)), "h": max(0.0, _clamp01(h)),
                         "color": _safe_color(color)})

    def write_text(self, x: float, y: float, text: str, size: int = 28,
                   color: str = "#111111", align: str = "left"):
        """在屏幕任意位置打字（比例坐标；size 为像素字号）。"""
        try:
            px_size = max(8, min(400, int(size)))
        except (TypeError, ValueError):
            px_size = 28
        self._append_op({"op": "text", "x": _clamp01(x), "y": _clamp01(y),
                         "text": str(text)[:2000],
                         "size": px_size, "color": _safe_color(color, "#111111"),
                         "align": str(align)})

    def open_page(self, bg_color: str = "#ffffff"):
        """进入独立全屏白板页（不透明背景，深度讲解模式）。"""
        self._page_mode = True
        self._page_bg = _safe_color(bg_color)
        self._repaint()

    def close_page(self):
        """退出独立白板页，恢复透明覆盖模式（保留已有 ops）。"""
        self._page_mode = False
        self._repaint()

    def clear(self):
        """清除全部 AI 绘制并退出白板页。"""
        self._ops = []
        self._page_mode = False
        self._repaint()

    def clear_region(self, x: float, y: float, w: float, h: float):
        """清除落在指定矩形区域内的绘制指令（比例坐标）。"""
        def _inside(op):
            if op["op"] == "rect":
                return (op["x"] + op["w"] > x and op["x"] < x + w
                        and op["y"] + op["h"] > y and op["y"] < y + h)
            return x <= op["x"] <= x + w and y <= op["y"] <= y + h
        self._ops = [op for op in self._ops if not _inside(op)]
        self._repaint()

    def get_occupied_regions(self) -> list:
        """提取已占用包围盒（比例坐标，供放置代理做重叠检测——round 59）。

        rect 直接取；text 估算包围盒（宽=len(text)×size/屏宽比、
        高=size×1.6/屏高比，多行按行高累加不做——当前 text op 单行）。"""
        w = max(1, self.width())
        h = max(1, self.height())
        regions = []
        for op in self._ops:
            if op["op"] == "rect":
                regions.append({"kind": "rect", "x": op["x"], "y": op["y"],
                                "w": op["w"], "h": op["h"],
                                "text": (op.get("color") or "")})
            elif op["op"] == "text":
                size = op.get("size", 28)
                text = op.get("text", "")
                est_w = min(1.0, len(text) * size / max(1, w))
                est_h = min(1.0, size * 1.6 / max(1, h))
                regions.append({"kind": "text", "x": op["x"], "y": op["y"],
                                "w": est_w, "h": est_h, "text": text[:40]})
        return regions

    def grab_b64(self, max_width: int = 960, quality: int = 70) -> str:
        """widget 自渲染 → JPEG base64（供全模态模型看图自检——round 59）。"""
        import base64
        from PySide6.QtCore import QBuffer
        pix = self.grab()
        if pix.width() > max_width:
            # 修复（2026-09-11 审计确证）：原为 `_Qt.SmoothTransformation`，但本模块只 import 了
            # `Qt`（QTimer/QColor/QFont/QPainter/QGuiApplication），**`_Qt` 从未定义**
            # （全仓 grep 仅此一处）→ 屏宽 > max_width(960) 时必抛 NameError。
            # 1920 宽的普通屏幕必然进入该分支 → grab_b64 恒定异常 → 上层静默降级为固定位置，
            # 整个「视觉选位 + 看图自检」从未真正运行过。
            pix = pix.scaledToWidth(max_width, Qt.SmoothTransformation)
        buf = QBuffer()
        buf.open(QBuffer.ReadWrite)
        pix.save(buf, "JPEG", quality)
        data = bytes(buf.data())
        buf.close()
        return base64.b64encode(data).decode("ascii")

    # ---------- 渲染 ----------

    def _repaint(self):
        if not self.isVisible():
            self.show_overlay()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            W = max(1, self.width())
            H = max(1, self.height())
            if self._page_mode:
                painter.fillRect(self.rect(), QColor(self._page_bg))
            else:
                painter.fillRect(self.rect(), QColor(0, 0, 0, 0))  # 透明
            for op in self._ops:
                if op["op"] == "rect":
                    px, py = int(op["x"] * W), int(op["y"] * H)
                    pw, ph = max(1, int(op["w"] * W)), max(1, int(op["h"] * H))
                    painter.fillRect(px, py, pw, ph, QColor(op["color"]))
                elif op["op"] == "text":
                    font = QFont("Microsoft YaHei")
                    font.setPixelSize(max(8, int(op["size"])))
                    painter.setFont(font)
                    painter.setPen(QColor(op["color"]))
                    px, py = int(op["x"] * W), int(op["y"] * H)
                    align = op.get("align", "left")
                    flags = Qt.AlignLeft
                    if align == "center":
                        flags = Qt.AlignHCenter
                    elif align == "right":
                        flags = Qt.AlignRight
                    rect = QRect(px, py, max(10, W - px), max(10, H - py))
                    painter.drawText(rect, flags | Qt.TextWordWrap, op["text"])
        finally:
            painter.end()

    # ---------- AI 指令批量执行 ----------

    def apply_ops(self, ops: list, screen_w: int = 1920, screen_h: int = 1080):
        """执行 AI 输出的绘制指令数组（坐标为设计稿像素，按当前屏幕比例缩放）。

        指令集：
        {"op":"paint_rect","x":0,"y":0,"w":400,"h":120,"color":"#ffffff"}
        {"op":"write_text","x":20,"y":20,"text":"...","size":28,"color":"#111111","align":"left"}
        {"op":"open_page","bg":"#ffffff"} / {"op":"close_page"} / {"op":"clear"}
        """
        sx = max(1, _num(screen_w, 1920))
        sy = max(1, _num(screen_h, 1080))
        for op in ops if isinstance(ops, list) else []:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op", ""))
            kind = str(op.get("op", ""))
            try:
                if kind == "paint_rect":
                    # 字段级兜底（_num）：单个脏值不整条丢弃，保证课堂板书连续性
                    self._append_op({"op": "rect",
                                     "x": _clamp01(_num(op.get("x")) / sx),
                                     "y": _clamp01(_num(op.get("y")) / sy),
                                     "w": max(0.0, _clamp01(_num(op.get("w")) / sx)),
                                     "h": max(0.0, _clamp01(_num(op.get("h")) / sy)),
                                     "color": _safe_color(op.get("color", "#ffffff"))})
                elif kind == "write_text":
                    self._append_op({"op": "text",
                                     "x": _clamp01(_num(op.get("x")) / sx),
                                     "y": _clamp01(_num(op.get("y")) / sy),
                                     "text": str(op.get("text", ""))[:2000],
                                     "size": max(8, min(400, int(_num(op.get("size"), 28)))),
                                     "color": _safe_color(op.get("color"), "#111111"),
                                     "align": str(op.get("align", "left"))})
                elif kind == "open_page":
                    self.open_page(str(op.get("bg", "#ffffff")))
                elif kind == "close_page":
                    self.close_page()
                elif kind == "clear":
                    self.clear()
            except (TypeError, ValueError):
                continue
        self._repaint()
