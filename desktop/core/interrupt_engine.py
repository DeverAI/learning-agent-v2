"""打断决策引擎（网课 AI 教师主动补位的中枢）。

设计：Design.md「新子系统 2：打断决策引擎」「5. 打断决策引擎（round55）」、
Techniques.md「5. 打断闸门（可单测的纯函数）」。

职责边界：本引擎**只决策、只发信号**，不出声、不绘制。
`interrupt_triggered` 由上层（UI/朗读/屏幕绘制）订阅后联动，保持引擎无 UI 依赖、可单测。

模型分工：判断"老师讲错了/跳步了/讲不清"属于**推理任务**，走对话主力模型
（`resolve_dialog_target()` → deepseek-v4-pro），不走 flash 轻任务通道。

fail-safe 原则：AI 调用失败、JSON 解析失败、字段缺失、置信度异常——一律**不打断**。
误打断毁课堂体验的代价远高于漏打断。
"""
import time
from datetime import datetime

from PySide6.QtCore import QObject, Signal

from config.settings import ConfigManager, ENABLE_CLASSROOM_AUDIO, INTERRUPT_LEVELS
from core import ai_client
from core.screen_analyzer import _extract_json_object
from core import mute_mode
from utils.helpers import logger, append_err_record, now_cst

import json

# 决策允许的原因类型（AI 输出白名单外的值一律视为 none）
REASON_TYPES = ("error", "omission", "unclear", "none")
# 最新一条老师转写允许的陈旧秒数：超过则暂停决策（M5，5 分钟≈课间休息上限）
STALE_CONTEXT_SEC = 300
# 各分级开关接受的 reason_type
LEVEL_ACCEPTS = {
    "off": (),
    "on_error": ("error", "omission"),
    "on_unclear": ("error", "omission", "unclear"),
}

DECISION_SYSTEM_PROMPT = """你是一位旁听网课的资深教研员。你的任务是判断"AI 教师是否需要立刻出声打断、给学生补讲"。

判断标准：
- error：老师讲错了（概念错误、公式错误、结论错误、例题解答错误）
- omission：老师跳过了关键步骤/前提，导致后续内容接不上
- unclear：老师讲得含糊、不明不白、术语堆砌、学生很可能听不懂
- none：讲解正常，不需要打断

严格输出 JSON（不要任何额外文字、不要 Markdown 围栏）：
{"interrupt": true/false, "confidence": 0.0-1.0, "reason_type": "error|omission|unclear|none", "reason": "一句话依据（引用课堂原文片段）", "teach_point": "若打断，要补讲什么（30字内，具体到知识点）"}

约束：
- 只依据给出的课堂文字流判断，不要臆测未出现的内容
- 拿不准时 interrupt=false；confidence 是你对"确实需要打断"的把握
- 转写可能有错别字，不要因个别错字判定老师讲错
- reason 必须引用具体依据，空泛理由视为 none"""


class InterruptEngine(QObject):
    """打断决策引擎：课堂上下文 → AI 结构化决策 → 五级闸门 → 信号。"""

    interrupt_triggered = Signal(dict)   # 通过全部闸门，上层应出声/绘制
    decision_made = Signal(dict)         # 每次决策结果（含被闸门拦下的，便于 UI 显示）
    gate_rejected = Signal(str, dict)    # (拒绝原因, 决策内容)

    def __init__(self, monitor=None, parent=None):
        super().__init__(parent)
        self._monitor = monitor              # ClassroomMonitor（可为 None，纯决策测试用）
        self._last_interrupt_ts = 0.0
        self._last_decision_ts = 0.0
        self._muted_override = False         # 引擎级静音（UI 开关），与全局静音模式取或
        self._screen_context = ""            # 可选：最新截屏分析结论
        self._topic = ""                     # 可选：学生当前学习主题
        self._counts = {"decisions": 0, "ai_interrupt": 0, "triggered": 0,
                        "failed": 0, "student_requests": 0}
        self._last_result = {}

    # ---------- 外部状态注入 ----------

    def set_monitor(self, monitor):
        self._monitor = monitor

    def set_screen_context(self, text: str):
        """注入最新截屏分析结论（视觉通道）。空串表示无。"""
        self._screen_context = str(text or "").strip()[:1000]

    def set_topic(self, topic: str):
        self._topic = str(topic or "").strip()[:200]

    def set_muted(self, muted: bool):
        """引擎级静音（UI 开关/热键）。与 OISystem 全局静音模式取或。"""
        self._muted_override = bool(muted)

    @property
    def muted(self) -> bool:
        if self._muted_override:
            return True
        try:
            return bool(mute_mode.is_mute_mode())
        except Exception:
            return False

    # ---------- 闸门（纯函数，可单测） ----------

    def gates(self, decision: dict, now: float = None) -> tuple:
        """五级闸门，顺序固定：静音 → level=off → 冷却 → 置信度 → 级别过滤。

        返回 (是否放行, 拒绝原因)。任一不过即不放行。
        """
        now = time.time() if now is None else float(now)
        s = ConfigManager().settings

        if self.muted:
            return False, "muted"
        level = str(getattr(s, "interrupt_level", "on_error") or "on_error").strip().lower()
        if level not in INTERRUPT_LEVELS:
            level = "on_error"
        if level == "off":
            return False, "level_off"

        cooldown = max(0, int(getattr(s, "interrupt_cooldown_sec", 180) or 0))
        if self._last_interrupt_ts and (now - self._last_interrupt_ts) < cooldown:
            return False, "cooldown"

        if not isinstance(decision, dict) or not decision.get("interrupt"):
            return False, "ai_no_interrupt"
        try:
            conf = float(decision.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf != conf or conf < 0.0 or conf > 1.0:      # NaN/越界一律视为不可信
            conf = 0.0
        conf_min = float(getattr(s, "interrupt_confidence_min", 0.6) or 0.0)
        if conf < conf_min:
            return False, "low_confidence"

        rtype = str(decision.get("reason_type", "none") or "none").strip().lower()
        if rtype not in REASON_TYPES:
            rtype = "none"
        if rtype not in LEVEL_ACCEPTS.get(level, ()):
            return False, f"level_filter:{level}"
        return True, ""

    def should_consider(self, now: float = None) -> tuple:
        """是否到了该做一次决策的时机（轮询间隔 + 最少老师段数）。

        返回 (是否该决策, 不决策的原因)。学生主动请求不走此判定。
        """
        now = time.time() if now is None else float(now)
        s = ConfigManager().settings
        if not ENABLE_CLASSROOM_AUDIO:
            return False, "module_disabled"
        if not getattr(s, "classroom_audio_enabled", True):
            return False, "audio_disabled"
        if self.muted:
            return False, "muted"
        if str(getattr(s, "interrupt_level", "on_error")).strip().lower() == "off":
            return False, "level_off"
        interval = max(5, int(getattr(s, "interrupt_decision_interval_sec", 45) or 45))
        if self._last_decision_ts and (now - self._last_decision_ts) < interval:
            return False, "interval"
        need = max(1, int(getattr(s, "interrupt_min_teacher_segments", 3) or 3))
        have = 0
        if self._monitor is not None:
            try:
                have = int(self._monitor.teacher_segment_count())
            except Exception:
                have = 0
        if have < need:
            return False, f"need_segments:{have}/{need}"
        # 新鲜度校验（M5）：通道死亡或课程结束后，别拿几十分钟前的陈旧
        # 转写反复调 AI——冷却一过就可能对旧内容误触发打断
        if self._monitor is not None:
            try:
                latest = self._monitor.recent(1, speaker="teacher")
                if latest:
                    ts = datetime.fromisoformat(str(latest[0].get("ts", "")))
                    age = (now_cst() - ts).total_seconds()
                    if age > STALE_CONTEXT_SEC:
                        return False, f"stale_context:{int(age)}s"
            except Exception:
                pass    # 新鲜度无法确认时不阻断决策（只防明确的陈旧）
        return True, ""

    # ---------- 决策 ----------

    def build_prompt(self, context: str = None) -> list:
        """构造决策 messages（上下文可注入，便于单测）。"""
        s = ConfigManager().settings
        window = max(1, int(getattr(s, "classroom_asr_window", 12) or 12))
        if context is None:
            context = ""
            if self._monitor is not None:
                try:
                    context = self._monitor.context_block(window)
                except Exception as e:
                    logger.warning(f"取课堂上下文失败: {str(e)[:150]}")
        parts = []
        if self._topic:
            parts.append(f"学生当前学习主题：{self._topic}")
        if self._screen_context:
            parts.append(f"当前屏幕/课件分析：{self._screen_context}")
        parts.append("课堂文字流（最近若干条，[老师]=系统音频转写，[学生]=麦克风转写）：")
        parts.append(context.strip() or "（暂无转写内容）")
        parts.append("\n请判断是否需要立刻打断补讲，严格输出 JSON。")
        return [
            {"role": "system", "content": DECISION_SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ]

    def parse_decision(self, raw: str) -> dict:
        """解析 AI 决策文本为规范化 dict。解析失败返回 interrupt=False（fail-safe）。"""
        text = str(raw or "").strip()
        if not text:
            return {"interrupt": False, "confidence": 0.0, "reason_type": "none",
                    "reason": "", "teach_point": "", "parse_error": "empty"}
        payload = None
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            snippet = _extract_json_object(text)
            if snippet:
                try:
                    payload = json.loads(snippet)
                except (json.JSONDecodeError, ValueError):
                    payload = None
        if not isinstance(payload, dict):
            return {"interrupt": False, "confidence": 0.0, "reason_type": "none",
                    "reason": "", "teach_point": "", "parse_error": "not_object"}

        raw_conf = payload.get("confidence", 0.0)
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf != conf:                      # NaN
            conf = 0.0
        conf = max(0.0, min(1.0, conf))

        rtype = str(payload.get("reason_type", "none") or "none").strip().lower()
        if rtype not in REASON_TYPES:
            rtype = "none"
        raw_flag = payload.get("interrupt", False)
        if isinstance(raw_flag, str):
            # 模型偶发输出 "false"/"no" 字符串，bool("false")=True 会违反
            # fail-safe 红线（H2）：显式归一，白名单外的一切值都按 False
            interrupt = raw_flag.strip().lower() in ("true", "1", "yes")
        else:
            interrupt = bool(raw_flag)
        if not interrupt:
            rtype = "none"
        return {
            "interrupt": interrupt,
            "confidence": conf,
            "reason_type": rtype,
            "reason": str(payload.get("reason", "") or "").strip()[:300],
            "teach_point": str(payload.get("teach_point", "") or "").strip()[:200],
        }

    def evaluate(self, context: str = None, force: bool = False) -> dict:
        """执行一次决策。force=True 跳过 should_consider 时机判定（仍过五级闸门）。

        返回 {"triggered": bool, "decision": dict, "gate": str, "skip": str}
        """
        result = {"triggered": False, "decision": {}, "gate": "", "skip": ""}
        if not force:
            ok, why = self.should_consider()
            if not ok:
                result["skip"] = why
                self._last_result = result
                return result

        self._last_decision_ts = time.time()
        self._counts["decisions"] += 1
        try:
            provider, model = ai_client.resolve_dialog_target()
            messages = self.build_prompt(context)
            raw = ai_client.chat(messages, provider=provider, model=model,
                                 temperature=0.2, max_tokens=512)
            decision = self.parse_decision(raw)
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:180]}"
            self._counts["failed"] += 1
            logger.error(f"打断决策 AI 调用失败（fail-safe：不打断）: {msg}")
            try:
                append_err_record("core/interrupt_engine.py", "打断决策调用失败", msg)
            except Exception:
                pass
            result["decision"] = {"interrupt": False, "confidence": 0.0,
                                  "reason_type": "none", "reason": "", "teach_point": "",
                                  "error": msg}
            result["gate"] = "ai_error"
            self._last_result = result
            try:
                self.decision_made.emit(dict(result))
            except RuntimeError:
                pass
            return result

        if decision.get("interrupt"):
            self._counts["ai_interrupt"] += 1
        passed, why = self.gates(decision)
        result["decision"] = decision
        result["gate"] = "" if passed else why
        result["triggered"] = bool(passed)
        if passed:
            self._fire(decision, source="ai")
        else:
            try:
                self.gate_rejected.emit(why, dict(decision))
            except RuntimeError:
                pass
        self._last_result = result
        try:
            self.decision_made.emit(dict(result))
        except RuntimeError:
            pass
        return result

    def request_teaching(self, point: str) -> dict:
        """学生主动要求教学某点：**绕过 AI 决策与级别/置信度闸门**，
        仅受静音与冷却约束（用户明确点名的请求不该被"AI 觉得没必要"否决）。
        """
        clean = str(point or "").strip()[:200]
        if not clean:
            return {"triggered": False, "gate": "empty_point"}
        self._counts["student_requests"] += 1
        decision = {"interrupt": True, "confidence": 1.0, "reason_type": "student_request",
                    "reason": "学生主动要求讲解", "teach_point": clean}
        now = time.time()
        if self.muted:
            self._emit_reject("muted", decision)
            return {"triggered": False, "gate": "muted", "decision": decision}
        cooldown = max(0, int(getattr(ConfigManager().settings, "interrupt_cooldown_sec", 180) or 0))
        if self._last_interrupt_ts and (now - self._last_interrupt_ts) < cooldown:
            self._emit_reject("cooldown", decision)
            return {"triggered": False, "gate": "cooldown", "decision": decision}
        self._fire(decision, source="student")
        return {"triggered": True, "gate": "", "decision": decision}

    # ---------- 内部 ----------

    def _emit_reject(self, why: str, decision: dict):
        try:
            self.gate_rejected.emit(why, dict(decision))
        except RuntimeError:
            pass

    def _fire(self, decision: dict, source: str = "ai"):
        self._last_interrupt_ts = time.time()
        self._counts["triggered"] += 1
        payload = {
            "source": source,
            "teach_point": decision.get("teach_point", ""),
            "reason": decision.get("reason", ""),
            "reason_type": decision.get("reason_type", "none"),
            "confidence": decision.get("confidence", 0.0),
            "speak": bool(getattr(ConfigManager().settings, "interrupt_speak", True)),
            "ts": self._last_interrupt_ts,
        }
        logger.info(f"打断触发({source}): {payload['reason_type']} "
                    f"conf={payload['confidence']:.2f} point={payload['teach_point'][:40]}")
        try:
            self.interrupt_triggered.emit(payload)
        except RuntimeError:
            pass

    def reset_cooldown(self):
        """清零冷却（供"立即再讲一次"类操作或测试使用）。"""
        self._last_interrupt_ts = 0.0

    def stats(self) -> dict:
        return {
            "counts": dict(self._counts),
            "muted": self.muted,
            "level": str(getattr(ConfigManager().settings, "interrupt_level", "on_error")),
            "cooldown_remaining": round(max(
                0.0, self._last_interrupt_ts + max(
                    0, int(getattr(ConfigManager().settings, "interrupt_cooldown_sec", 180) or 0))
                - time.time()), 1) if self._last_interrupt_ts else 0.0,
            "last_result": dict(self._last_result),
        }
