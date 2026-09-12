"""屏幕绘制原语引擎演示：自动序列执行各原语并自动退出（无人工交互，供验证）。

运行：python demo_paint.py
序列：透明层涂抹矩形+写字 -> open_page 全屏白板 -> 白板上写字 -> close_page -> clear -> 退出。
"""
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.screen_paint import ScreenPaintOverlay


def main():
    app = QApplication(sys.argv)
    overlay = ScreenPaintOverlay()
    overlay.show_overlay()

    steps = []

    # 第 1 步：透明覆盖层——涂抹屏幕左上区域 + 打字（模拟"盖住老师板书重写"）
    def step1():
        overlay.apply_ops([
            {"op": "paint_rect", "x": 40, "y": 40, "w": 560, "h": 140, "color": "#ffffff"},
            {"op": "write_text", "x": 60, "y": 60, "text": "AI 教师标注：这一步跳过了配方推导",
             "size": 24, "color": "#c62828"},
        ], screen_w=1920, screen_h=1080)
        print("step1 ok: paint_rect + write_text")

    # 第 2 步：独立全屏白板页
    def step2():
        overlay.apply_ops([
            {"op": "open_page", "bg": "#ffffff"},
            {"op": "write_text", "x": 100, "y": 80, "text": "全屏白板页：完整推导配方过程", "size": 32},
            {"op": "write_text", "x": 100, "y": 160, "text": "x² + bx = (x + b/2)² - (b/2)²", "size": 28},
        ], screen_w=1920, screen_h=1080)
        print("step2 ok: open_page + board text")

    # 第 3 步：清除全部（恢复原始屏幕）
    def step3():
        overlay.apply_ops([{"op": "clear"}])
        print("step3 ok: clear")

    steps = [(600, step1), (2200, step2), (4200, step3), (5200, app.quit)]
    for ms, fn in steps:
        QTimer.singleShot(ms, fn)

    app.exec()
    print("demo finished clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
