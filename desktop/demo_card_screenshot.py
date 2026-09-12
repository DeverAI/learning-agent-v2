# -*- coding: utf-8 -*-
"""黑板卡片截图测试：渲染讲题卡片（区域填充+写字布局）→ 全屏截图 → 供视觉审查。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["QT_QPA_PLATFORM"] = "offscreen"   # 离屏渲染（widget.grab 不依赖物理屏幕）

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402

app = QApplication(sys.argv)

from core.screen_paint import ScreenPaintOverlay  # noqa: E402
from core.lecture_engine import LectureEngine  # noqa: E402

overlay = ScreenPaintOverlay()
overlay.resize(1920, 1080); overlay.show()

class _TTS(QObject):
    stateChanged = Signal(str)

    def speak(self, *a, **k):
        pass

    def shutdown(self):
        pass

eng = LectureEngine(tts=_TTS(), overlay=overlay)

# 两个真实感卡片内容（长题面 + 分步讲解）
STEP1 = {
    "title": "题意分析",
    "speech": ("先看题目条件：一个直角三角形，两条直角边分别是 3 厘米和 4 厘米，"
               "要求算出斜边的长度。这里的关键词是直角三角形，"
               "看到它就要想到勾股定理，两条直角边的平方和等于斜边的平方。"),
}
STEP2 = {
    "title": "逐步解法",
    "speech": ("设斜边为 c，根据勾股定理：c 的平方等于 3 的平方加 4 的平方，"
               "也就是 9 加 16 等于 25。对 25 开平方，取正值得到 c 等于 5，"
               "所以斜边长度是 5 厘米。最后别忘了写单位和答句。"),
}

shots = []


def shoot(name):
    pix = overlay.grab()
    path = os.path.join(os.environ.get("TEMP", "."), f"la_probe\\{name}.png")
    pix.save(path)
    shots.append(path)
    print(f"saved {path}")


def phase1():
    print("[1] 渲染卡片：题意分析")
    eng._render_step(STEP1)


def phase2():
    print("[2] 重绘卡片：逐步解法（验证擦旧+新内容）")
    eng._render_step(STEP2)
    QTimer.singleShot(1200, lambda: shoot("lecture_card_step2"))


def phase3():
    print("[3] 极端内容：超长文本 + 空文本")
    eng._render_step({"title": "很长的标题超出四十个字符的情况测试截断是否正常工作以及布局是否保持稳定不溢出",
                      "speech": "超长" * 120})
    QTimer.singleShot(1200, lambda: (shoot("lecture_card_extreme"),
                                     eng._clear_board(),
                                     QTimer.singleShot(800, lambda: (shoot("lecture_card_cleared"),
                                                                     finish()))))


def finish():
    print("[4] 完成")
    app.quit()


QTimer.singleShot(800, phase1)
QTimer.singleShot(2200, phase2)
QTimer.singleShot(4200, phase3)
app.exec()

print("\nshots:", shots)
for s in shots:
    print("  exists:", os.path.exists(s), s)
