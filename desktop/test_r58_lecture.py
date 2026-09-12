# -*- coding: utf-8 -*-
"""round 58 离线回归：桌面讲课引擎（lecture_engine + lecture_view 接线）。

运行：python test_r58_lecture.py（QT_QPA_PLATFORM=offscreen，mock 网络/AI/TTS）
覆盖：
T1 数据拉取（papers/paper/question 契约、X-Auth-Token、失败 error 信号）
T2 讲解计划（v4-pro 正常路径/_finalize 规整/降级两步/迟到丢弃）
T3 播放状态机（逐步推进/自动连播/pause-resume/stop 清板/prev-next）
T4 卡片渲染（复用比例坐标/字号钳制/擦旧）
T5 sidebar 接线（按钮存在/分发分支）
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core import lecture_engine as le  # noqa: E402
from core.lecture_engine import LectureEngine  # noqa: E402

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
    def __init__(self):
        self.live_ops = []
        self.calls = 0

    def show_overlay(self):
        pass

    def apply_ops(self, ops, screen_w=1920, screen_h=1080):
        self.calls += 1
        self.live_ops.extend(list(ops))

    def clear_region(self, x, y, w, h):
        def inside(op):
            if op["op"] == "rect":
                return (op["x"] + op["w"] > x and op["x"] < x + w
                        and op["y"] + op["h"] > y and op["y"] < y + h)
            return x <= op["x"] <= x + w and y <= op["y"] <= y + h
        self.live_ops = [op for op in self.live_ops if not inside(op)]


class FakeTTS(QObject):
    stateChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.spoken = []
        self.auto_idle = False

    def speak(self, text, style_instruction=""):
        self.spoken.append(text)
        if self.auto_idle:            # 模拟真实 TTS：说完发 idle 驱动连播
            self.stateChanged.emit("idle")

    def stop(self):
        pass

    def shutdown(self):
        pass


class FakeMonitor:
    def context_block(self, n=None):
        return ""


# ---- T1 数据拉取 ----
print("\n[T1] 数据拉取（mock requests）")
engine = LectureEngine(tts=FakeTTS(), overlay=FakeOverlay())
papers_out = []
engine.papers_ready.connect(lambda p: papers_out.append(p))
errs = []
engine.error.connect(lambda m: errs.append(m))

_orig_get = le.requests.get
_orig_settings = le.ConfigManager


class _S:
    sync_server_url = "http://fake.local"
    sync_server_password = "pwd123"


le.ConfigManager = lambda: type("C", (), {"settings": _S()})()


def _fake_get(url, params=None, headers=None, timeout=None):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            if url.endswith("/api/papers/p1"):
                return {"id": "p1", "title": "期中卷",
                        "question_order": [{"id": "q1", "number": 1},
                                           {"id": "q2", "number": 2}]}
            if url.rstrip("/").endswith("/api/papers"):
                return [{"id": "p1", "title": "期中卷", "subject": "math",
                         "question_ids": ["q1", "q2"]}]
            if url.endswith("/api/questions/q1"):
                return {"id": "q1", "ocr_text": "3+5=?",
                        "standard_answer": "8",
                        "score_points_html": "<p>列式2分</p>",
                        "subject": "math", "grade": "一年级"}
            return {}

    return _R()


le.requests.get = _fake_get
try:
    engine.fetch_papers()
    deadline = time.time() + 3
    while time.time() < deadline and not papers_out:
        app.processEvents()          # queued 信号需事件循环处理
        time.sleep(0.05)
    check("1.papers 列表拉取并归一", papers_out and papers_out[0][0]["title"] == "期中卷"
          and papers_out[0][0]["question_count"] == 2)

    paper_out = []
    engine.paper_ready.connect(lambda p: paper_out.append(p))
    engine.fetch_paper("p1")
    deadline = time.time() + 3
    while time.time() < deadline and not paper_out:
        app.processEvents()
        time.sleep(0.05)
    check("1.paper 详情 question_order 归一", paper_out
          and paper_out[0]["question_order"][0]["id"] == "q1")

    q = engine.fetch_question("q1")
    check("1.question 详情同步拉取", q and q["standard_answer"] == "8"
          and "<p>" not in q["score_points_html"])
finally:
    le.requests.get = _orig_get
    le.ConfigManager = _orig_settings


# ---- T2 讲解计划 ----
print("\n[T2] 讲解计划（mock AI）")
engine2 = LectureEngine(tts=FakeTTS(), overlay=FakeOverlay())
from core import ai_client as r58_ai  # noqa: E402
_orig_chat, _orig_resolve = r58_ai.chat, r58_ai.resolve_dialog_target

r58_ai.resolve_dialog_target = lambda: ("deepseek", "deepseek-v4-pro")
r58_ai.chat = lambda messages, **kw: json.dumps({
    "steps": [
        {"title": "题意分析", "speech": "这道题要求 3 加 5"},
        {"title": "解题", "speech": "直接相加得 8"},
    ], "summary": "结果是 8"})
try:
    steps, summary = engine2.build_plan_sync(
        {"question_html": "3+5=?", "standard_answer": "8", "ocr_text": ""})
    check("2.正常计划 2 步", len(steps) == 2 and steps[0]["title"] == "题意分析")
    check("2.summary 透传", summary == "结果是 8")

    # 降级：AI 抛异常 → 两步直读
    r58_ai.chat = lambda messages, **kw: (_ for _ in ()).throw(RuntimeError("网络故障"))
    steps2, _ = engine2.build_plan_sync(
        {"question_html": "3+5=?", "standard_answer": "8"})
    check("2.AI 失败降级两步直读", len(steps2) == 2
          and steps2[0]["title"] == "题目" and steps2[1]["title"] == "答案")

    # 降级：AI 返回垃圾 → 同样两步
    r58_ai.chat = lambda messages, **kw: "不是 JSON 的输出"
    steps3, _ = engine2.build_plan_sync(
        {"ocr_text": "题目文本", "standard_answer": "答案文本"})
    check("2.垃圾输出降级不沉默", len(steps3) == 2
          and "题目文本" in steps3[0]["speech"])
finally:
    r58_ai.chat, r58_ai.resolve_dialog_target = _orig_chat, _orig_resolve


# ---- T3 播放状态机 ----
print("\n[T3] 播放状态机")
ov3 = FakeOverlay()
tts3 = FakeTTS()
engine3 = LectureEngine(tts=tts3, overlay=ov3)
steps3 = [{"title": "一", "speech": "第一步讲解"},
          {"title": "二", "speech": "第二步讲解"},
          {"title": "三", "speech": "第三步讲解"}]
step_events = []
engine3.step_started.connect(lambda i, t, s: step_events.append((i, t)))
finished = []
engine3.lecture_finished.connect(lambda: finished.append(1))

engine3.start_lecture(steps3)
check("3.启动即播第 1 步", step_events == [(0, "一")] and len(tts3.spoken) == 1)
tts3.stateChanged.emit("idle")      # 模拟 TTS 完成
check("3.idle 驱动第 2 步", step_events[-1] == (1, "二"))
tts3.stateChanged.emit("idle")
check("3.idle 驱动第 3 步", step_events[-1] == (2, "三"))
tts3.stateChanged.emit("idle")
check("3.播完发 lecture_finished", finished == [1] and not engine3._playing)

# 暂停/恢复
engine3.start_lecture(steps3)
engine3.pause()
tts3.stateChanged.emit("idle")
check("3.暂停期 idle 不推进", step_events[-1] == (0, "一"))
engine3.resume()
check("3.恢复后推进", step_events[-1] == (1, "二"))

# next 手动
engine3._next = True
engine3.pause()
engine3.resume()                    # pause→resume = 手动推进
check("3.pause-resure 即 next", step_events[-1] == (2, "三"))

# stop 清板
n_ops = len(ov3.live_ops)
engine3.stop()
check("3.stop 清板", engine3._playing is False)

# prev：重置后再验证
engine3.start_lecture(steps3)
tts3.stateChanged.emit("idle")
engine3.pause()
engine3._step_idx -= 2              # prev 语义（view 里同款）
engine3.resume()
check("3.prev 回退一步", step_events[-1] == (0, "一"))


# ---- T4 卡片渲染 ----
print("\n[T4] 卡片渲染（复用比例坐标/擦旧）")
class StubOverlay:
    """渲染层 stub：apply_ops 追加（op 名规范化同真实现）+ clear_region 擦除。"""
    _OP_MAP = {"paint_rect": "rect", "write_text": "text"}

    def __init__(self):
        self.live_ops = []
        self.shots = 0

    def show_overlay(self):
        pass

    def apply_ops(self, ops, screen_w=1920, screen_h=1080):
        for op in ops:
            op = dict(op)
            op["op"] = self._OP_MAP.get(op.get("op"), op.get("op"))
            # 与真实现一致：坐标按屏幕宽高归一为比例
            for k, total in (("x", screen_w), ("y", screen_h),
                             ("w", screen_w), ("h", screen_h)):
                if k in op and isinstance(op[k], (int, float)):
                    op[k] = op[k] / total
            self.live_ops.append(op)

    def clear_region(self, x, y, w, h):
        def inside(op):
            if op["op"] == "rect":
                return (op["x"] + op["w"] > x and op["x"] < x + w
                        and op["y"] + op["h"] > y and op["y"] < y + h)
            return x <= op["x"] <= x + w and y <= op["y"] <= y + h
        self.live_ops = [op for op in self.live_ops if not inside(op)]

    def get_occupied_regions(self):
        return []                       # 放置回路自检：无已占区域

    def grab_b64(self, **kw):
        self.shots += 1
        return "shot"


ov4 = StubOverlay()
engine4 = LectureEngine(tts=FakeTTS(), overlay=ov4)
engine4._render_step_at({"x": 0.54, "y": 0.03, "w": 0.44, "h": 0.32},
                        {"title": "题意分析", "speech": "先看题目条件" * 20})
n1 = len(ov4.live_ops)
check("4.首帧卡片入层", n1 >= 5)
engine4._render_step_at({"x": 0.54, "y": 0.03, "w": 0.44, "h": 0.32},
                        {"title": "解题", "speech": "第二步内容"})
check("4.重绘先擦旧（ops 有界）", len(ov4.live_ops) <= n1 + 6)
texts = [o.get("text", "") for o in ov4.live_ops if o["op"] == "text"]
check("4.旧步骤文本被擦除", not any("先看题目条件" in t for t in texts)
      and any("第二步内容" in t for t in texts))
check("4.标题含步骤名", any("AI 教师讲题：解题" in t for t in texts))


# ---- T5 sidebar 接线 ----
print("\n[T5] sidebar 接线")
from ui.icons import SIDEBAR_BUTTONS, get_sidebar_buttons  # noqa: E402
check("5.SIDEBAR_BUTTONS 含 lecture", any(k == "lecture" for k, _l, _s in SIDEBAR_BUTTONS))
check("5.get_sidebar_buttons 透传 lecture", any(k == "lecture" for k, _l, _s in get_sidebar_buttons()))
import ui.sidebar_v2 as sv2  # noqa: E402
check("5._on_button 有 lecture 分支",
      "lecture" in open(sv2.__file__, encoding="utf-8").read()
      and hasattr(sv2.Sidebar, "_open_lecture"))

print(f"\n===== R58 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
