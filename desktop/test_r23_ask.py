# -*- coding: utf-8 -*-
"""R23 离线回归：学生「随时提问」（打断网课并就地讲解）的生产入口。

运行：python test_r23_ask.py（QT_QPA_PLATFORM=offscreen，不打真网络）

## 为什么专门写这一组

`InterruptEngine.request_teaching` 的实现在 round55 就完成了、也有测试，
但**生产代码里一个调用点都没有** —— 只有 demo_coach.py 与测试在调它。
也就是说「随时打断网课并讲解」在功能上一直**不可达**：学生按下任何键都不会发生。
本组测试钉住新加的三段入口（全局热键 → 输入框 → ask_now）以及
「音频感知关掉也必须能用」这条独立性。

覆盖：
T1 `_gate_text` 闸门文案（含未知闸门回退）
T2 `_HotkeyBridge.do_action` 槽存在且旧槽 do_hide 未被改名（契约保持）
T3 `ask_now` 空问题被拒（不进引擎、有提示）
T4 真实链：ask_now(问题) → engine 触发 → 卡片标题上板 + 来源=student
T5 冷却闸门：连续第二次提问被 cooldown 拦下（设计如此，且必须给用户可见反馈）
T6 `AppSettings.ask_hotkey` 字段存在且默认值可用
T7 托盘有 ask_now 信号；main.py 已接线（源码断言）
T8 音频感知关闭时仍构建提问链；monitor=None 的清理有守卫
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core.classroom_coach import (  # noqa: E402
    ASK_HOTKEY_DEFAULT, ClassroomCoach, _HotkeyBridge,
)
from core.interrupt_engine import InterruptEngine  # noqa: E402
from config.settings import AppSettings  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


class FakeOverlay:
    """只记录指令，不做任何真实绘制/置顶。"""

    def __init__(self):
        self.ops = []
        self.cleared = 0

    def clear_region(self, *a, **k):
        self.cleared += 1

    def apply_ops(self, ops, screen_w=1, screen_h=1):
        self.ops.extend(list(ops or []))


class FakeTTS(QObject):
    stateChanged = Signal(str)

    def __init__(self):
        super().__init__()
        self.spoken = []

    def speak(self, text, **k):
        self.spoken.append(text)
        return True

    def stop(self):
        return None


def _texts(overlay):
    return " ".join(str(o.get("text", "")) for o in overlay.ops)


def _mk_coach():
    overlay = FakeOverlay()
    tts = FakeTTS()
    engine = InterruptEngine(monitor=None)
    coach = ClassroomCoach(None, engine, overlay, tts)
    # 讲解词生成会走真网络（v4-pro 流式）：本组只测「能不能被叫起来」，
    # 所以把生成段替换成空实现，避免离线测试打网。
    coach._start_speech_pipeline = lambda *a, **k: None
    return coach, engine, overlay


# ---- T1 闸门文案 ----
print("\n[T1] _gate_text 闸门文案")
check("1.muted 有中文说明", "静音" in ClassroomCoach._gate_text("muted"))
check("1.cooldown 有中文说明", "冷却" in ClassroomCoach._gate_text("cooldown"))
check("1.empty_point 有中文说明", "空" in ClassroomCoach._gate_text("empty_point"))
check("1.未知闸门不抛异常且带原文", "weird_gate" in ClassroomCoach._gate_text("weird_gate"))
check("1.None 闸门不抛异常", isinstance(ClassroomCoach._gate_text(None), str))


# ---- T2 bridge 槽契约 ----
print("\n[T2] _HotkeyBridge 槽")
br = _HotkeyBridge(lambda: None)
mo = br.metaObject()
check("2.do_action 槽存在（提问热键用）", mo.indexOfSlot("do_action()") >= 0)
check("2.do_hide 槽名未被改名（旧契约保持）", mo.indexOfSlot("do_hide()") >= 0)


# ---- T3 空问题 ----
print("\n[T3] 空问题不进引擎")
coach3, _eng3, ov3 = _mk_coach()
r3 = coach3.ask_now("   ")
check("3.空问题被拒", r3.get("triggered") is False and r3.get("gate") == "empty_point")
check("3.空问题不画卡片", _texts(ov3).strip() == "")


# ---- T4 真实链：提问 → 触发 → 上板 ----
print("\n[T4] ask_now 真实链（monitor=None）")
coach4, eng4, ov4 = _mk_coach()
coach4.connect()          # 订阅 interrupt_triggered（同时注册热键，daemon）
r4 = coach4.ask_now("这一步为什么变号")
check("4.提问被触发", r4.get("triggered") is True)
check("4.问题原文上了卡片标题", "这一步为什么变号" in _texts(ov4))
check("4.来源标记为 student", coach4._last_source_student is True)
check("4.卡片底部有关闭热键提示", "CTRL+ALT+H" in _texts(ov4).upper())
check("4.自动清除定时器已启动", coach4._clear_timer.isActive())


# ---- T5 冷却闸门（同一教练第二次提问） ----
print("\n[T5] 冷却闸门")
r5 = coach4.ask_now("再讲一遍")
check("5.连问被 cooldown 拦下", r5.get("triggered") is False and r5.get("gate") == "cooldown")


# ---- T6 设置字段 ----
print("\n[T6] ask_hotkey 设置项")
s = AppSettings()
check("6.字段存在", hasattr(s, "ask_hotkey"))
check("6.默认值非空且与常量一致", str(getattr(s, "ask_hotkey", "")) == ASK_HOTKEY_DEFAULT)
check("6.默认值含修饰键", "+" in ASK_HOTKEY_DEFAULT)


# ---- T7 托盘 + main 接线（源码断言） ----
print("\n[T7] 托盘与 main 接线")
root = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


tray_src = _read(os.path.join("ui", "tray.py"))
main_src = _read("main.py")
check("7.托盘有 ask_now 信号", "ask_now = Signal()" in tray_src)
check("7.托盘菜单有随时提问项", "随时提问" in tray_src)
check("7.main 接线到 _ask_now_from_ui", "tray.ask_now.connect" in main_src)


# ---- T8 与音频感知解耦 ----
print("\n[T8] 音频感知关闭时仍可提问")
check("8.存在 monitor=None 的提问链", "InterruptEngine(monitor=None)" in main_src)
check("8.关闭时有 None 守卫", 'mon = classroom.get("monitor")' in main_src)
check("8.启动提示告知热键", "Ctrl+Alt+A" in main_src)

try:
    coach3.shutdown()
    coach4.shutdown()
except Exception as exc:
    print(f"  [WARN] shutdown 异常: {exc}")

print(f"\n==== R23 提问链回归: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
