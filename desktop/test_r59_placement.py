# -*- coding: utf-8 -*-
"""round 59 离线回归：黑板放置代理 + 视觉自检回路（mock MiMo/截图）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core import placement as pl  # noqa: E402
from core.placement import rect_iou, find_overlaps, PlacementAgent  # noqa: E402
from core.screen_paint import ScreenPaintOverlay  # noqa: E402

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


# ---- T1 IoU 纯函数 ----
print("\n[T1] rect_iou")
r = {"x": 0.5, "y": 0.1, "w": 0.4, "h": 0.3}
check("1.完全重合 IoU≈1", abs(rect_iou(r, r) - 1.0) < 1e-9)
check("1.不相交 IoU=0",
      rect_iou(r, {"x": 0.0, "y": 0.1, "w": 0.3, "h": 0.3}) == 0.0)
half = rect_iou(r, {"x": 0.6, "y": 0.2, "w": 0.4, "h": 0.3})
check("1.半重叠 0<IoU<1", 0.0 < half < 1.0)
check("1.包含关系 IoU=小/大",
      abs(rect_iou({"x": 0.5, "y": 0.1, "w": 0.1, "h": 0.1}, r) - 0.01 / 0.12) < 1e-6)

# ---- T2 find_overlaps ----
print("\n[T2] find_overlaps")
occ = [{"kind": "text", "x": 0.5, "y": 0.1, "w": 0.4, "h": 0.3, "text": "已有卡片"},
       {"kind": "text", "x": 0.0, "y": 0.6, "w": 0.3, "h": 0.2, "text": "左下"}]
ov = find_overlaps(r, occ)
check("2.只检出重叠项", len(ov) == 1 and ov[0]["text"] == "已有卡片")
check("2.阈值过滤（低重叠不计）",
      find_overlaps({"x": 0.49, "y": 0.09, "w": 0.02, "h": 0.02}, occ) == [])


# ---- T3 放置回路：无重叠一次通过 ----
print("\n[T3] place_with_selfcheck")
class FakeOverlay:
    def __init__(self):
        self.regions = []
        self.shots = 0
        self.rendered = []

    def get_occupied_regions(self):
        return list(self.regions)

    def grab_b64(self, **kw):
        self.shots += 1
        return "fakejpeg"

    def clear_region(self, x, y, w, h):
        self.regions = [r for r in self.regions
                        if not (abs(r["x"] - x) < 0.05)]

    def apply_ops(self, ops, **kw):
        self.rendered.append(ops)


ov3 = FakeOverlay()
calls = {"n": 0}


class FakeAI:
    def __init__(self, script):
        self.script = script

    def xiaomi_vision(self, b64, prompt, mime="image/jpeg", parse_json=False):
        import re
        resp = self.script[len([1 for c in calls_per_agent if c is self])] if False else None
        return self.next_response()


calls_per_agent = []


class ScriptedAI:
    """按次序返回预设响应的假全模态。"""
    def __init__(self, responses):
        self.responses = list(responses)

    def xiaomi_vision(self, b64, prompt, mime="image/jpeg", parse_json=False):
        if self.responses:
            return self.responses.pop(0)
        return '{"action":"place","rect":{"x":0.54,"y":0.4,"w":0.44,"h":0.32},"reason":"默认"}'


rendered = []
agent3 = PlacementAgent(ov3, ai_service=object())
agent3._ai = object()
agent3._ask_placement = lambda *a, **k: {"action": "place",
                                         "rect": {"x": 0.6, "y": 0.5, "w": 0.4, "h": 0.3},
                                         "reason": "mock"}
res = agent3.place_with_selfcheck(
    "新内容", "top_right", {"x": 0.54, "y": 0.03, "w": 0.44, "h": 0.32},
    render_fn=lambda rect: rendered.append(rect))
check("3.无重叠一次通过（0 轮调整）", res["rounds"] == 0 and res["overlaps"] == [])
check("3.渲染执行", len(rendered) == 1)

# 有重叠 → 反馈调整 → 第 2 次放对
class OverlapThenOKOverlay(FakeOverlay):
    """第一次渲染制造重叠区域，之后干净。"""
    def __init__(self, agent_ref):
        super().__init__()
        self._agent_ref = agent_ref
        self._renders = 0

    def get_occupied_regions(self):
        if self._renders == 0 and self.shots >= 1:
            # 首次自检时报告一个与 fallback 重叠的区域
            return [{"kind": "text", "x": 0.6, "y": 0.1, "w": 0.3, "h": 0.2,
                     "text": "挡路的旧卡片"}]
        return []


ov4 = FakeOverlay()
plan_responses = [
    '{"action":"place","rect":{"x":0.55,"y":0.5,"w":0.44,"h":0.32},"reason":"移到下方"}']
agent4 = PlacementAgent(ov4, ai_service=object())
rendered4 = []
agent4._ask_placement = lambda shot, content, hint, occ, feedback="": (
    {"action": "place", "rect": {"x": 0.55, "y": 0.5, "w": 0.44, "h": 0.32},
     "reason": "adjusted"})


class SelfCheckOverlay(FakeOverlay):
    """自检脚本：第 1 次 get_occupied 报重叠（模拟旧卡片挡路），之后干净。"""
    def __init__(self):
        super().__init__()
        self.occ_queries = 0

    def grab_b64(self, **kw):
        return f"shot{self.occ_queries}"

    def get_occupied_regions(self):
        self.occ_queries += 1
        if self.occ_queries == 2:      # 第 1 次自检（render 后、grab 前的查询序号 2）
            return [{"kind": "text", "x": 0.6, "y": 0.1, "w": 0.3, "h": 0.2,
                     "text": "挡路旧卡片"}]
        return []


ov5 = SelfCheckOverlay()
agent5 = PlacementAgent(ov5, ai_service=object())
agent5._ask_placement = lambda *a, **k: {"action": "place",
                                         "rect": {"x": 0.55, "y": 0.5, "w": 0.44, "h": 0.3},
                                         "reason": "moved"}
res5 = agent5.place_with_selfcheck(
    "新卡片内容", "top_right", {"x": 0.54, "y": 0.03, "w": 0.44, "h": 0.32},
    render_fn=lambda rect: rendered4.append(rect))
check("3.重叠触发 1 轮调整", res5["rounds"] == 1)
check("3.调整后无重叠", res5["overlaps"] == [])

# 超限：永远报重叠 → 2 轮后保持现状
class AlwaysOverlapOverlay(FakeOverlay):
    def grab_b64(self, **kw):
        return "shot"

    def get_occupied_regions(self):
        return [{"kind": "text", "x": 0.6, "y": 0.1, "w": 0.3, "h": 0.2,
                 "text": "永远挡路"}]


ov6 = AlwaysOverlapOverlay()
agent6 = PlacementAgent(ov6, ai_service=object())
agent6._ask_placement = lambda *a, **k: {"action": "place",
                                         "rect": {"x": 0.7, "y": 0.1, "w": 0.2, "h": 0.1},
                                         "reason": "again"}
res6 = agent6.place_with_selfcheck(
    "内容", "top_right", {"x": 0.54, "y": 0.03, "w": 0.44, "h": 0.32},
    render_fn=lambda rect: None)
check("3.超限保持现状（rounds=max）", res6["rounds"] == pl.MAX_ADJUST_ROUNDS)

# erase 决策：代理要求擦除区域
class EraseAI:
    def xiaomi_vision(self, b64, prompt, mime="image/jpeg", parse_json=False):
        return ('{"action":"erase","region":{"x":0.6,"y":0.1,"w":0.3,"h":0.2},'
                '"reason":"旧内容已过时"}')


ov7 = SelfCheckOverlay()   # 第 1 次自检报重叠
agent7 = PlacementAgent(ov7, ai_service=object())
agent7._ask_placement = lambda *a, **k: {
    "action": "erase", "region": {"x": 0.6, "y": 0.1, "w": 0.3, "h": 0.2},
    "reason": "旧内容已过时"}
erased = []
_qcnt = {"n": 0}
_ov7 = type("O", (), {
    "get_occupied_regions": lambda self: (_set_q(_qcnt), ([{"kind": "text", "x": 0.6, "y": 0.1,
                                            "w": 0.3, "h": 0.2, "text": "旧"}]
                                          if _qcnt["n"] == 2 else []))[1],
    "grab_b64": lambda self, **k: "shot",
    "clear_region": lambda self, x, y, w, h: erased.append((x, y, w, h)),
    "apply_ops": lambda self, ops, **k: None,
})()
def _set_q(cnt):
    cnt["n"] += 1


_qcnt = {"n": 0}
agent7._overlay = _ov7    # 换上带 clear_region 记账的 stub

res7 = agent7.place_with_selfcheck(
    "内容", "top_right", {"x": 0.54, "y": 0.03, "w": 0.44, "h": 0.32},
    render_fn=lambda rect: None)
check("3.erase 决策被执行（clear_region 调用）", len(erased) >= 1)

print(f"\n===== R59 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
