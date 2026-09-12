"""screen_paint 脏输入回归测试（不弹窗，纯指令层断言）。

覆盖核验轮发现的三类健壮性缺口：非法颜色、非有限坐标、ops 无上限。
运行：python test_screen_paint.py（需 PySide6，Python312 解释器）
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core.screen_paint import ScreenPaintOverlay, _MAX_OPS  # noqa: E402

o = ScreenPaintOverlay()

# 1) 非法/危险颜色 → 回退默认（黑白），绝不进 paintEvent
o.apply_ops([
    {"op": "paint_rect", "x": 0, "y": 0, "w": 100, "h": 50, "color": "#xyz"},
    {"op": "paint_rect", "x": 0, "y": 0, "w": 100, "h": 50, "color": "not-a-color"},
    {"op": "paint_rect", "x": 0, "y": 0, "w": 100, "h": 50, "color": None},
    {"op": "write_text", "x": 0, "y": 0, "text": "t", "color": "<script>"},
], screen_w=1920, screen_h=1080)
from PySide6.QtGui import QColor  # noqa: E402
for op in o._ops:
    assert QColor(op["color"]).isValid(), f"非法颜色漏网: {op}"
rects = [op for op in o._ops if op["op"] == "rect"]
assert rects and all(op["color"] == "#ffffff" for op in rects), "rect 非法色应回退白色"
texts = [op for op in o._ops if op["op"] == "text"]
assert texts and texts[0]["color"] == "#111111", "text 非法色应回退黑色"
print("PASS 1: 非法颜色全部回退")

# 2) NaN/Inf/垃圾坐标 → 有限值兜底，钳制在 [-0.5, 1.5]
o.clear()
o.apply_ops([
    {"op": "paint_rect", "x": float("nan"), "y": float("inf"), "w": 1e18, "h": -5, "color": "#fff"},
    {"op": "write_text", "x": "-abc", "y": 9e99, "text": "脏坐标"},
    {"op": "paint_rect", "x": None, "y": [], "w": {}, "h": 0, "color": "#000"},
], screen_w=1920, screen_h=1080)
assert len(o._ops) == 3, f"3 条脏 op 应被兜底保留而非丢弃: {len(o._ops)}"
for op in o._ops:
    for k in ("x", "y", "w", "h"):
        if k in op:
            assert math.isfinite(op[k]) and -0.5 <= op[k] <= 1.5, f"坐标未钳制: {op}"
print("PASS 2: NaN/Inf/垃圾坐标全部钳制")

# 3) 字号与文本长度边界
o.clear()
o.apply_ops([
    {"op": "write_text", "x": 10, "y": 10, "text": "长" * 5000, "size": 0},
    {"op": "write_text", "x": 10, "y": 10, "text": "x", "size": 100000},
    {"op": "write_text", "x": 10, "y": 10, "text": "x", "size": "medium"},
], screen_w=1920, screen_h=1080)
sizes = [op["size"] for op in o._ops]
assert sizes == [8, 400, 28], f"字号钳制异常: {sizes}"  # 0→8；100000→400；"medium"→默认28
print("PASS 3: 字号/文本长度边界正确")

# 4) 指令总量上限
o.clear()
bulk = [{"op": "write_text", "x": 10, "y": 10, "text": f"t{i}"} for i in range(_MAX_OPS + 200)]
o.apply_ops(bulk, screen_w=1920, screen_h=1080)
assert len(o._ops) == _MAX_OPS, f"ops 应封顶 {_MAX_OPS}: {len(o._ops)}"
assert o._ops[-1]["text"] == f"t{len(bulk) - 1}", "应丢最旧留最新"
print("PASS 4: ops 总量封顶且丢旧留新")

# 5) 原语直调（内部代码路径）也要兜住
o.clear()
o.paint_rect(float("nan"), 0.5, 0.2, "#12345g")
o.write_text(0.5, 0.5, "正常文字", size=32)
o.write_text(0.5, 0.5, "坏字号", size=None)
o.open_page("#zzz")
assert o._page_bg == "#ffffff", f"open_page 非法背景未回退: {o._page_bg}"
assert o._ops[1]["size"] == 32 and o._ops[2]["size"] == 28
print("PASS 5: 原语直调脏参数全部兜底")

# 6) 结构垃圾输入不炸
o.apply_ops("not-a-list")
o.apply_ops([None, 42, "str", {"no_op_key": 1}])
o.clear()
assert o._ops == []
o.set_click_through(False)
o.set_click_through(True)
print("PASS 6: 结构垃圾输入与非穿透切换不抛异常")

print("ALL PASS: screen_paint 脏输入回归 6 组全部通过")
