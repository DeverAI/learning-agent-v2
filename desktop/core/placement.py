"""黑板放置代理（round 59）：视觉模型选位 + 看图自检 + 重叠反馈限次回路。

设计：Design.md「黑板智能空间管理：放置代理 + 视觉自检回路（round 59）」。

分工（用户定规）：
- 写什么由推理者（deepseek-v4-pro）决定，并叮嘱大概区域（九宫格 hint）；
- 位置选择、避让、看图调整由全模态模型（MiMo 视觉）负责；
- 渲染后截图自检：与已占区域重叠则反馈"保持原样/擦除/调整"，最多 MAX_ADJUST_ROUNDS 轮；
- 超限保持现状并记 warning（诚实不假装满意）。
"""
import base64
import json
import re
import time

from utils.helpers import logger

# 重叠判定阈值：新内容与已占区域 IoU 超过该值视为重叠
OVERLAP_THRESHOLD = 0.15
# 视觉自检最大调整轮数
MAX_ADJUST_ROUNDS = 2

# 九宫格提示 → 锚点比例坐标（放置代理输出的 rect 参考起点）
HINT_ANCHORS = {
    "top_left": (0.02, 0.02), "top_center": (0.28, 0.02), "top_right": (0.54, 0.02),
    "mid_left": (0.02, 0.32), "mid_center": (0.28, 0.32), "mid_right": (0.54, 0.32),
    "bottom_left": (0.02, 0.62), "bottom_center": (0.28, 0.62), "bottom_right": (0.54, 0.62),
}


def rect_iou(a: dict, b: dict) -> float:
    """两个比例矩形（x,y,w,h ∈ [0,1]）的 IoU。"""
    ax0, ay0, ax1, ay1 = a["x"], a["y"], a["x"] + a["w"], a["y"] + a["h"]
    bx0, by0, bx1, by1 = b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a["w"]) * max(0.0, a["h"])
    area_b = max(0.0, b["w"]) * max(0.0, b["h"])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def find_overlaps(rect: dict, occupied: list, threshold: float = OVERLAP_THRESHOLD) -> list:
    """返回与 rect 重叠（IoU>threshold）的已占区域列表。"""
    return [o for o in occupied if rect_iou(rect, o) > threshold]


def _parse_rect(obj) -> dict | None:
    """宽松解析视觉模型输出的矩形（容忍缺失/越界/比例或像素）。"""
    if not isinstance(obj, dict):
        return None
    rect = obj.get("rect") if isinstance(obj.get("rect"), dict) else obj
    try:
        vals = {k: float(rect.get(k, -1)) for k in ("x", "y", "w", "h")}
    except (TypeError, ValueError):
        return None
    if any(v != v or v < 0 for v in vals.values()):    # NaN/负值
        return None
    # 像素容忍：值 >1.5 视为像素坐标，按 1920x1080 归一
    if vals["x"] > 1.5 or vals["y"] > 1.5:
        vals["x"] /= 1920.0
        vals["y"] /= 1080.0
        vals["w"] = min(vals["w"], 1920.0) / 1920.0
        vals["h"] = min(vals["h"], 1080.0) / 1080.0
    vals["x"] = max(0.0, min(0.99, vals["x"]))
    vals["y"] = max(0.0, min(0.99, vals["y"]))
    vals["w"] = max(0.02, min(1.0 - vals["x"], vals["w"]))
    vals["h"] = max(0.02, min(1.0 - vals["y"], vals["h"]))
    return vals


class PlacementAgent:
    """放置代理：全模态模型选位 + 截图自检 + 限次调整。"""

    def __init__(self, overlay, ai_service=None):
        self._overlay = overlay
        self._ai = ai_service        # 惰性 import 也可；注入便于测试

    # ---------- 全模态调用 ----------

    def _ask_placement(self, shot_b64: str, content_text: str, hint: str,
                       occupied: list, feedback: str = "") -> dict:
        """调 MiMo 全模态：看图 + 已占区域 + 新内容 → 放置决策 JSON。

        返回 {action: place|keep|erase, rect?: {x,y,w,h}, region?: {x,y,w,h}, reason}。
        """
        if self._ai is None:
            # 修复（2026-09-11 审计确证）：原为 `from services import ai_service as _ai_mod`。
            # 但桌面端运行时 sys.path 只含 `desktop/`（main.py 只插入 desktop/），而 `services`
            # 包在 `backend/` 下（`desktop/services` 目录不存在）→ 必然 `ModuleNotFoundError`，
            # 被 lecture_engine 上层的 except 兜住 → 视觉选位永远静默失效。
            # 改用**桌面端自己的 AI 层** `core.ai_client`：它是同步的 `vision_chat()`，
            # 同时消除了"调用 backend 的 async `xiaomi_vision` 却未 await"的问题
            # （未 await 会拿到协程对象，str() 里没有 `{`，每轮都返回 no-json → 永远回退）。
            from core import ai_client as _ai_mod
            self._ai = _ai_mod
        occupied_desc = "\n".join(
            f"- [{r['kind']}] x={r['x']:.2f} y={r['y']:.2f} w={r['w']:.2f} h={r['h']:.2f}"
            f" 摘要:{r.get('text', '')[:30]}" for r in occupied) or "（当前无已占内容）"
        feedback_line = f"\n上一次放置的问题（必须修正）：{feedback}" if feedback else ""
        anchor = HINT_ANCHORS.get(hint, HINT_ANCHORS["top_right"])
        prompt = (
            "你是黑板布局代理。屏幕截图里已有一些板书卡片。现在要在黑板上放置新的讲解卡片。\n"
            f"推理者叮嘱的大概区域：{hint}（参考锚点 x={anchor[0]}, y={anchor[1]}）。\n"
            f"当前已占用区域（比例坐标）：\n{occupied_desc}\n"
            f"新卡片内容摘要：{content_text[:120]}\n"
            f"{feedback_line}\n"
            "要求：卡片宽约 0.44、高约 0.32（比例）；必须避开所有已占用区域，"
            "若放不下优先放在空白象限；与已有内容刻意重叠时才允许重叠。\n"
            "只输出 JSON：{\"action\":\"place\",\"rect\":{\"x\":0.5,\"y\":0.1,\"w\":0.44,\"h\":0.32},"
            "\"reason\":\"一句话\"}。action 也可为 keep（保持原样不动）或 erase（擦除区域，附 region）。\n"
            "屏幕截图如下。"
        )
        try:
            if self._ai is None:
                from core import ai_client as _ai_mod
                self._ai = _ai_mod
            # 桌面端 AI 层是**同步**接口：vision_chat(prompt, image_bytes)
            raw = self._ai.vision_chat(prompt, base64.b64decode(shot_b64))
        except Exception as e:
            # 显式失败：不再让 ModuleNotFoundError / NameError 静默穿透到上层被吞成"成功形状"。
            logger.warning("放置代理视觉调用失败（本轮退回回退位置）：%s", str(e)[:150])
            return {"action": "place", "rect": None,
                    "reason": f"vision failed: {str(e)[:60]}"}
        m = re.search(r"\{[\s\S]*\}", str(raw or ""))
        if not m:
            return {"action": "place", "rect": None, "reason": "no json"}
        try:
            data = json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            return {"action": "place", "rect": None, "reason": "bad json"}
        data.setdefault("action", "place")
        if data["action"] == "place":
            data["rect"] = _parse_rect(data)
        return data

    # ---------- 主回路 ----------

    def place_with_selfcheck(self, content_text: str, hint: str,
                             fallback_rect: dict,
                             render_fn, max_rounds: int = MAX_ADJUST_ROUNDS) -> dict:
        """放置 + 截图自检回路。

        render_fn(rect) → 由调用方渲染卡片（rect 为最终位置）。
        返回 {"rect": 最终位置, "rounds": 实际调整轮数, "overlaps": 最终重叠数}。
        """
        decision = {"action": "place", "rect": dict(fallback_rect), "reason": "fallback"}
        rounds = 0
        overlaps = []
        while True:
            render_fn(decision.get("rect") or fallback_rect)
            time.sleep(0.15)                      # 给 paintEvent 一点时间
            # 清掉恒真条件（2026-09-11 审计确证）：原为
            #   `[o for o in ... if o not in occupied_before or True]`
            # 末尾的 `or True` 让整个过滤条件恒成立 → 等价于直接用完整列表，
            # `occupied_before` 因此是死变量。保留原语义（取全部已占区域），但写清楚。
            occupied = self._overlay.get_occupied_regions()
            # 自检：grab 截图，检测新卡片区域与已占区域的重叠
            shot = self._overlay.grab_b64()
            rect = decision.get("rect") or fallback_rect
            overlaps = find_overlaps(rect, [o for o in occupied
                                            if abs(o["x"] - rect["x"]) > 0.01
                                            or abs(o["y"] - rect["y"]) > 0.01])
            # 清掉恒真条件：原为 `if not overlaps and rounds >= 0:`，`rounds >= 0` 永远为真
            if not overlaps:
                break                              # 无重叠：满意，结束
            if rounds >= max_rounds:
                logger.warning(f"放置自检超限（{rounds} 轮仍有重叠），保持现状")
                break
            rounds += 1
            feedback = "；".join(
                f"与 [{o.get('kind','?')}] {o.get('text','')[:20]} 重叠" for o in overlaps)
            decision = self._ask_placement(shot, content_text, hint,
                                           self._overlay.get_occupied_regions(),
                                           feedback=feedback)
            if decision["action"] == "erase" and decision.get("region"):
                rg = decision["region"]
                self._overlay.clear_region(rg["x"], rg["y"], rg["w"], rg["h"])
            if decision["action"] == "keep":
                break
        return {"rect": decision.get("rect") or fallback_rect,
                "rounds": rounds, "overlaps": overlaps}
