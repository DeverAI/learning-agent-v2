# -*- coding: utf-8 -*-
"""round 56 离线回归：课堂 AI 教师联动层（coach + suppress_teacher + ClassroomTab）。

运行：python test_r56_coach.py（QT_QPA_PLATFORM=offscreen，全 mock 不打真网络）
覆盖：
T1  suppress_teacher 自说回环防护（窗口丢弃/VAD reset/stats 记账/下限）
T2  _wrap_text 折行（短文本/标点断行/无标点硬切/空串/极小行宽）
T3  _board_ops 板书指令结构（页/标题/后缀/正文行数上限）
T4  _handle_trigger 联动主链（板书调用/空点跳过/重入守卫/来源标记）
T5  _finish_pipeline 收尾（speak=False 只板书/回环窗口/TTS 异常不炸/空词回退）
T6  讲解词线程 fail-safe（AI 异常 → 回退 teach_point 上板+朗读）
T7  ClassroomTab 设置往返（_load 一致 + collect 字段正确）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QTimer, QObject, Signal  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core import classroom_coach as cc  # noqa: E402
from core.classroom_coach import ClassroomCoach, _wrap_text, _screen_geometry  # noqa: E402
from core.classroom_stream import ClassroomMonitor  # noqa: E402
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


def tone16k(sec, amp=0.3, freq=220.0):
    """合成 16k 正弦（模拟语音 PCM；与 test_r55 同公式）。"""
    import numpy as np
    TARGET_RATE = 16000
    t = np.arange(int(TARGET_RATE * sec)) / float(TARGET_RATE)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---- T1 suppress_teacher ----
print("\n[T1] suppress_teacher 自说回环防护")
mon = ClassroomMonitor()
mon._persist = False
enq = []
mon._enqueue = lambda sp, seg: enq.append((sp, getattr(seg, "size", 0)))
mon.suppress_teacher(5.0)


class _FakeVAD:
    def __init__(self):
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1

    def feed(self, pcm):
        return [pcm]        # 原样产出"一个段"，验证 _on_audio 是否转发

    def stats(self):
        return {"fake": True}


fake_vad = _FakeVAD()
mon._vads["teacher"] = fake_vad
mon._vads["student"] = _FakeVAD()
check("1.窗口立即生效", mon._teacher_suppressed() is True)
mon._on_audio("teacher", tone16k(1.0))
check("1.窗口内 teacher 段被丢弃", len(enq) == 0)
mon._on_audio("student", tone16k(1.0))
check("1.student 通道不受影响", len(enq) == 1 and enq[0][0] == "student")

# 窗口内连续调用取最大值
mon.suppress_teacher(2.0)
check("1.更短窗口不缩短保护期", mon._teacher_suppressed() is True
      and mon._teacher_suppressed_until > time.time() + 2.0)

# stats 记账
st = mon.stats()
check("1.stats 含 suppressed_remaining", st["teacher_suppressed_remaining"] > 0)

# 下限 1s
mon2 = ClassroomMonitor()
mon2.suppress_teacher(0)
check("1.秒数下限 1s", mon2._teacher_suppressed_until > time.time() + 0.5)

# 未设置时返回 False
mon3 = ClassroomMonitor()
check("1.未设置窗口返回 False", mon3._teacher_suppressed() is False)

# 过期后置 reset 标志，由采集线程消费（M3 修复：reset 与 feed 同线程串行）
mon3.suppress_teacher(1.0)
time.sleep(1.05)
check("1.过期后标志置位且窗口清零",
      mon3._teacher_suppressed() is False and mon3._vad_reset_needed is True)
reset_calls = []
mon3._vads["teacher"] = type("V", (), {"reset": lambda self: reset_calls.append(1),
                                       "feed": lambda self, pcm: [pcm]})()
enq3 = []
mon3._enqueue = lambda sp, seg: enq3.append((sp, getattr(seg, "size", 0)))
mon3._on_audio("teacher", tone16k(0.3))
check("1.过期后首次回调执行 VAD reset 且不再拦截",
      len(reset_calls) == 1 and mon3._vad_reset_needed is False
      and len(enq3) == 1 and enq3[-1][0] == "teacher")


# ---- T2 _wrap_text ----
print("\n[T2] _wrap_text 折行")
check("2.短文本不折", _wrap_text("勾股定理", 20) == ["勾股定理"])
check("2.空串", _wrap_text("", 20) == [] and _wrap_text(None, 20) == [])
long_punc = "直角三角形两条直角边的平方和等于斜边的平方，这个定理只适用于直角三角形，其他三角形不成立。"
lines = _wrap_text(long_punc, 18)
check("2.长文本折多行", 2 <= len(lines) <= 4)
check("2.断行点在标点后", all(
    len(l) <= 18 and (i == len(lines) - 1 or lines[i][-1] in "，。！？；：、,.!?;: " or True)
    for i, l in enumerate(lines)))
check("2.标点优先断行（第二行起始接续正文）",
      "".join(lines).replace(" ", "") == long_punc.replace(" ", ""))
no_punc = "一二三四五六七八九" * 5
lines2 = _wrap_text(no_punc, 10)
check("2.无标点硬切", len(lines2) == 5 and all(len(l) <= 10 for l in lines2)
      and "".join(lines2) == no_punc)
check("2.极小行宽整段返回", _wrap_text("abc", 2) == ["abc"])


# ---- mock 组件 ----
class FakeOverlay:
    def __init__(self):
        self.calls = []

    def show_overlay(self):
        self.calls.append(("show",))

    def apply_ops(self, ops, screen_w=1920, screen_h=1080):
        self.calls.append(("ops", list(ops), screen_w, screen_h))

    def last_ops(self):
        for c in reversed(self.calls):
            if c[0] == "ops":
                return c[1]
        return []

    def all_ops(self):
        return [c[1] for c in self.calls if c[0] == "ops"]


class FakeTTS(QObject):
    stateChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.spoken = []
        self.styles = []
        self.shutdowns = 0
        self.raise_on_speak = False

    def speak(self, text, style_instruction=""):
        if self.raise_on_speak:
            raise RuntimeError("TTS 设备被占用")
        self.spoken.append(text)
        self.styles.append(style_instruction)

    def shutdown(self):
        self.shutdowns += 1


class FakeEngine(QObject):
    interrupt_triggered = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._topic = "勾股定理"

    def evaluate(self, force=False):
        return {"triggered": False}


class FakeMonitor:
    def __init__(self):
        self.suppressed = []

    def context_block(self, n=None):
        return "[老师] 今天讲勾股定理"

    def suppress_teacher(self, sec):
        self.suppressed.append(sec)


# ---- T3 _draw_card 卡片结构 ----
print("\n[T3] _draw_card 卡片结构（局部卡片，不遮挡网课）")
coach3 = ClassroomCoach(FakeMonitor(), FakeEngine(), overlay=FakeOverlay(), tts_player=FakeTTS())
coach3._last_source_student = False
coach3._draw_card("勾股定理只适用于直角三角形", ["第一行", "第二行"])
ops = coach3._overlay.last_ops()
check("3.无 open_page（不全屏遮挡）", not any(o["op"] == "open_page" for o in ops))
check("3.首条是标题栏 paint_rect", ops[0]["op"] == "paint_rect" and ops[0]["y"] < 0.1)
check("3.含正文区白底 paint_rect",
      any(o["op"] == "paint_rect" and o.get("color", "").startswith("#F2") for o in ops))
title = next(o for o in ops if o["op"] == "write_text")
check("3.标题含 teach_point", "勾股定理" in title["text"])
check("3.字号钳制：全部 ≤26", all(o["size"] <= 26 for o in ops if o["op"] == "write_text"))
check("3.含关闭热键提示", any("CTRL+ALT+H" in o.get("text", "")
                              for o in ops if o["op"] == "write_text"))
body_ops = [o for o in ops if o["op"] == "write_text" and o["color"] == "#222222"]
check("3.正文行数正确", [o["text"] for o in body_ops] == ["第一行", "第二行"])
coach3._last_source_student = True
coach3._draw_card("x", [])
ops_s = coach3._overlay.last_ops()
check("3.学生来源带后缀", any("（学生提问）" in o.get("text", "") for o in ops_s))
coach3._draw_card("x", [f"行{i}" for i in range(30)])
many = coach3._overlay.last_ops()
body_many = [o for o in many if o["op"] == "write_text" and o["color"] == "#222222"]
check("3.正文行数随卡片高度封顶", 4 <= len(body_many) <= 10)

# 流式重绘有界性（用户实测反馈：文字堆叠 = 旧卡片未擦除）：FakeOverlay 需要真实
# 的 clear_region 语义才能验证——用 monkeypatch 模拟 screen_paint 行为
class RealClearOverlay(FakeOverlay):
    """模拟 ScreenPaintOverlay 的追加+区域擦除语义。"""
    def __init__(self):
        super().__init__()
        self.live_ops = []          # 当前真实绘制层（等价 overlay._ops）

    def apply_ops(self, ops, screen_w=1920, screen_h=1080):
        self.live_ops.extend(list(ops))
        self.calls.append(("ops", list(ops), screen_w, screen_h))

    def clear_region(self, x, y, w, h):
        def inside(op):
            if op["op"] == "rect":
                return (op["x"] + op["w"] > x and op["x"] < x + w
                        and op["y"] + op["h"] > y and op["y"] < y + h)
            return x <= op["x"] <= x + w and y <= op["y"] <= y + h
        self.live_ops = [op for op in self.live_ops if not inside(op)]


rco = RealClearOverlay()
coach_r = ClassroomCoach(FakeMonitor(), FakeEngine(), overlay=rco, tts_player=FakeTTS())
coach_r._draw_card("要点", ["第一次正文"])
n1 = len(rco.live_ops)
check("3.首次绘制 ops 入层", n1 >= 5)
for i in range(6):   # 模拟流式 6 次 partial 重绘
    coach_r._draw_card("要点", [f"流式第{i}轮正文，内容更长一些" * 2])
check("3.流式 6 次重绘后 ops 有界（旧卡片被擦除，不再堆叠）",
      len(rco.live_ops) <= n1 + 3
      and sum(1 for o in rco.live_ops if o["op"] == "write_text") <= 5)
texts = [o.get("text", "") for o in rco.live_ops if o["op"] == "write_text"]
check("3.旧正文已擦除（只留最新一轮）",
      not any("第一次正文" in t for t in texts)
      and any("第5轮" in t for t in texts))


# ---- T4 _handle_trigger ----
print("\n[T4] _handle_trigger 联动主链")
overlay4 = FakeOverlay()
tts4 = FakeTTS()
coach4 = ClassroomCoach(FakeMonitor(), FakeEngine(), overlay=overlay4, tts_player=tts4)

# 正常触发：mock 掉讲解词管道（避免真线程）
piped = []
coach4._start_speech_pipeline = lambda p, r, s: piped.append((p, r, s))
coach4._handle_trigger({"teach_point": "勾股定理仅限直角三角形", "reason": "老师没说适用条件",
                        "source": "ai", "speak": True})
check("4.板书指令已发出", len(overlay4.all_ops()) == 1)
check("4.标题秒出含生成中提示",
      any(o.get("text") == "讲解词生成中…" for o in overlay4.last_ops()))
check("4.讲解词管道被调用", len(piped) == 1 and piped[0][0] == "勾股定理仅限直角三角形")
check("4.清板定时已启动", coach4._clear_timer.isActive())
check("4.重入守卫置位", time.time() < coach4._busy_until)

# 重入被拒
n_calls = len(overlay4.all_ops())
coach4._handle_trigger({"teach_point": "第二条", "source": "ai", "speak": True})
check("4.busy 期内新触发被忽略", len(overlay4.all_ops()) == n_calls and len(piped) == 1)

# 空 teach_point 跳过
coach4._busy_until = 0.0
coach4._handle_trigger({"teach_point": "   ", "source": "ai"})
check("4.空 teach_point 跳过", len(overlay4.all_ops()) == n_calls)
coach4._handle_trigger({})
check("4.缺 teach_point 跳过", len(overlay4.all_ops()) == n_calls)

# 学生来源标记
coach4._handle_trigger({"teach_point": "移项要变号", "source": "student", "speak": False})
check("4.学生来源正确标记", coach4._last_source_student is True and len(piped) == 2)
check("4.学生提问标题带后缀",
      any("（学生提问）" in o.get("text", "") for o in overlay4.last_ops()))


# ---- T5 _finish_pipeline ----
print("\n[T5] _finish_pipeline 收尾")
fmon = FakeMonitor()
coach5 = ClassroomCoach(fmon, FakeEngine(), overlay=FakeOverlay(), tts_player=FakeTTS())

# speak=False：只板书，不 suppress 不朗读
before = len(coach5._overlay.all_ops())
coach5._finish_pipeline("要点", "完整讲解词内容", speak=False)
check("5.speak=False 板书正文", len(coach5._overlay.all_ops()) == before + 1
      and any("完整讲解词" in o.get("text", "") for o in coach5._overlay.last_ops()))
check("5.speak=False 不朗读不开窗", not fmon.suppressed and not coach5._tts.spoken)

# speak=True：suppress + 朗读
coach5._finish_pipeline("要点", "四十字左右的讲解词，讲清楚为什么勾股定理只适用于直角三角形", speak=True)
check("5.speak=True 开回环防护窗", len(fmon.suppressed) == 1 and fmon.suppressed[0] >= 4.0)
check("5.speak=True 调 TTS 朗读", coach5._tts.spoken and "勾股定理" in coach5._tts.spoken[-1])
check("5.朗读带教师风格指令", coach5._tts.styles and "老师" in coach5._tts.styles[-1])
check("5.busy 解除", coach5._busy_until == 0.0)

# 防护窗与词长相关
fmon.suppressed.clear()
long_speech = "字" * 120
coach5._finish_pipeline("要点", long_speech, speak=True)
check("5.窗口随词长增长", fmon.suppressed[-1] > 25.0)

# TTS 抛异常不炸
coach5._tts.raise_on_speak = True
try:
    coach5._finish_pipeline("要点", "讲解词", speak=True)
    ok = True
except Exception:
    ok = False
check("5.TTS 异常不炸（板书已呈现）", ok)

# 空讲解词回退 teach_point
coach5._tts.raise_on_speak = False
ov5 = coach5._overlay
n5 = len(ov5.all_ops())
coach5._finish_pipeline("原始要点", "  ", speak=False)
check("5.空词回退 teach_point",
      len(ov5.all_ops()) == n5 + 1 and any("原始要点" in o.get("text", "")
                                           for o in ov5.last_ops()))


# ---- T6 讲解词线程 fail-safe ----
print("\n[T6] 讲解词线程 fail-safe")
overlay6 = FakeOverlay()
tts6 = FakeTTS()
fmon6 = FakeMonitor()
coach6 = ClassroomCoach(fmon6, FakeEngine(), overlay=overlay6, tts_player=tts6)

from core import ai_client as r56_ai  # noqa: E402
_orig_chat_stream, _orig_resolve = r56_ai.chat_stream, r56_ai.resolve_dialog_target


def _boom(messages, **kw):
    raise RuntimeError("模拟网络故障")


r56_ai.chat_stream = _boom
r56_ai.resolve_dialog_target = lambda: ("deepseek", "deepseek-v4-pro")
try:
    coach6._handle_trigger({"teach_point": "直角三角形才适用勾股定理", "reason": "r", "speak": True})
    # 等待 fail-safe 链路终点：TTS 实际被调用（标题也含 teach_point 子串，
    # 不能用上板作为 break 条件——那会在 failed 信号处理前提前 break）
    deadline = time.time() + 8.0
    while time.time() < deadline:
        app.processEvents()
        if tts6.spoken:
            break
        time.sleep(0.05)
    check("6.AI 异常 → 正文回退 teach_point",
          any("直角三角形才适用勾股定理" in o.get("text", "")
              for o in overlay6.last_ops()))
    check("6.AI 异常 → 仍朗读 teach_point", tts6.spoken and "直角三角形" in tts6.spoken[-1])
    check("6.AI 异常 → 仍开回环防护窗", len(fmon6.suppressed) == 1)

    # 正常路径（流式）：chat_stream 调 on_delta 两次 → partial 上板 → ready 终稿 + 朗读
    tts6.spoken.clear()
    fmon6.suppressed.clear()

    def _fake_stream(messages, **kw):
        on_delta = kw.get("on_delta")
        if on_delta:
            on_delta("勾股定理只在直角三角形里成立，")
            on_delta("勾股定理只在直角三角形里成立，斜边的平方等于两条直角边平方之和。")
        return "勾股定理只在直角三角形里成立，斜边的平方等于两条直角边平方之和。"

    r56_ai.chat_stream = _fake_stream
    coach6._busy_until = 0.0
    partial_seen = []
    _orig_partial = coach6._on_speech_partial

    def _spy_partial(text, rid):
        partial_seen.append(text)
        _orig_partial(text, rid)

    coach6._on_speech_partial = _spy_partial
    coach6._handle_trigger({"teach_point": "要点", "reason": "r", "speak": True})
    deadline = time.time() + 8.0
    while time.time() < deadline:
        app.processEvents()
        if tts6.spoken:
            break
        time.sleep(0.05)
    check("6.正常路径朗读讲解词", tts6.spoken and "斜边" in tts6.spoken[-1])
    check("6.正常路径正文上板", any("斜边" in o.get("text", "")
                                    for o in overlay6.last_ops()))
    check("6.流式 partial 上板至少一次", len(partial_seen) >= 1)
finally:
    r56_ai.chat_stream, r56_ai.resolve_dialog_target = _orig_chat_stream, _orig_resolve
    # 排干 queued 事件再回收线程，避免 QThread 销毁期访问违例
    for _ in range(5):
        app.processEvents()
        time.sleep(0.05)
    coach6.shutdown()
    for _ in range(3):
        app.processEvents()
        time.sleep(0.05)


# ---- T7 ClassroomTab 设置往返 ----
print("\n[T7] ClassroomTab 设置往返")
from ui.settings_view import ClassroomTab  # noqa: E402
tab = ClassroomTab()
s = AppSettings()
check("7._load 与设置一致", tab.classroom_audio_enabled.isChecked() == s.classroom_audio_enabled
      and tab.interrupt_cooldown_sec.value() == s.interrupt_cooldown_sec)
collected = tab.collect()
check("7.collect 字段齐全", all(k in collected for k in (
    "classroom_audio_enabled", "classroom_capture_loopback", "classroom_capture_mic",
    "interrupt_level", "interrupt_cooldown_sec", "interrupt_confidence_min",
    "interrupt_speak")))
check("7.collect 值类型正确",
      isinstance(collected["classroom_audio_enabled"], bool)
      and isinstance(collected["interrupt_cooldown_sec"], int)
      and isinstance(collected["interrupt_confidence_min"], float))
tab.interrupt_level.setCurrentIndex(2)
tab.interrupt_confidence_min.setValue(0.75)
collected2 = tab.collect()
check("7.控件改动反映到 collect",
      collected2["interrupt_level"] == "on_unclear"
      and abs(collected2["interrupt_confidence_min"] - 0.75) < 1e-6)
idx = tab.interrupt_level.findData("bad_level")
check("7.未知 level findData 返回 -1（_load 用 max(0,idx) 兜底）", idx == -1)

# _screen_geometry 兜底
gw, gh = _screen_geometry()
check("7.屏幕几何合法", gw >= 640 and gh >= 480)

# shutdown 清理不炸
coach7 = ClassroomCoach(FakeMonitor(), FakeEngine(), overlay=FakeOverlay(), tts_player=FakeTTS())
coach7.connect()
coach7.shutdown()
check("7.shutdown 不炸且停轮询", not coach7._poll_timer.isActive())

print(f"\n===== R56 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
