"""OISystem AI 元监督模块。

职责：
- 每隔 N 轮抽样对话，调用另一个 AI 判断"是否违反规则"
- 违规则向对话窗口注入纠偏系统消息
- 规则清单：禁代码输出/禁闲聊/引导思考/检测新算法想法记日志/检测代码输出禁用
"""
import json
import re
from typing import List, Dict
from PySide6.QtCore import QObject, Signal

from config.settings import ConfigManager
from utils.helpers import logger, log_event
from utils.exceptions import AICallError


SUPERVISOR_PROMPT = """你是 OI 学习辅助系统的元监督 AI，负责检查对话 AI 是否遵守以下规则：

1. 禁止提供任何代码块（```...``` 形式）
2. 禁止与用户闲聊，用户闲聊时应自动提醒
3. 用户直接问题目时应引导思考而非给答案
4. 检测到用户试图自己寻找新算法时，应肯定想法但提醒不要遗落进度，并记入日志
5. 检测到用户代码有输出行为时应直接禁用
6. 数论、组合数学等纯数学讨论（手推公式、逻辑分析、无代码块）不视为违规，不要把数论题一律当成必须写代码解决的问题

请分析以下最近 N 轮对话，判断对话 AI 是否违反规则。
返回严格 JSON：
{
  "violated": true/false,
  "violations": ["规则1", "规则3"],
  "correction": "给对话 AI 的纠偏指令（若 violated=false 则为空字符串）"
}
"""


SUPERVISOR_PROMPT_STUDY = """你是 OISystem 学习文化课模式的元监督 AI，负责检查对话 AI 是否遵守以下规则：

1. 默认不输出任何代码块（仅在需要伪代码解释思路时可用文字描述）
2. 禁止与用户闲聊，用户闲聊时应自动提醒
3. 用户直接问题目 / 概念时应引导思考而非给答案
4. 检测到用户走神（B 站娱乐 / 抖音 / 游戏 / 微博 / 八卦等）时，对话 AI 是否主动提醒
5. 检测到用户在网课平台（腾讯课堂 / 慕课 / 学堂在线 / 学习强国 / ClassIn 等）时，是否引导专心听讲
6. 学习错误的指出应具体到知识点 / 概念 / 公式
7. 输出风格应鼓励为主，避免打击信心

请分析以下最近 N 轮对话，判断对话 AI 是否违反规则。
返回严格 JSON：
{
  "violated": true/false,
  "violations": ["规则1", "规则3"],
  "correction": "给对话 AI 的纠偏指令（若 violated=false 则为空字符串）"
}
"""


def _build_supervisor_prompt(mode: str = "oi") -> str:
    return SUPERVISOR_PROMPT_STUDY if mode == "study" else SUPERVISOR_PROMPT


class AISupervisor(QObject):
    """元监督 AI。"""

    correction_needed = Signal(str)  # 纠偏指令

    def __init__(self):
        super().__init__()
        self.cfg = ConfigManager()
        self._round_count = 0

    def should_check(self) -> bool:
        """是否到了检查时机。"""
        self._round_count += 1
        # r39 P1 修复：interval 可能为 0/None，避免模零异常
        # round48：字符串等脏配置统一 int() 归一化，避免 max(1, "abc") TypeError
        try:
            interval = int(getattr(self.cfg.settings, "ai_supervisor_interval_rounds", 5) or 5)
        except (TypeError, ValueError):
            interval = 5
        interval = max(1, min(100, interval))
        return self._round_count % interval == 0

    def reset_round_count(self):
        """重置元监督计数器（P1 修复：对外暴露方法，避免外部直接改 _round_count 私有字段）。"""
        self._round_count = 0

    def check(self, recent_messages: List[Dict]) -> dict:
        """检查最近对话是否违规。

        recent_messages: 最近 N 轮 messages（含 user/assistant）
        返回 {violated, violations, correction}
        """
        from core.ai_client import chat, resolve_flash_target
        _fp, _fm = resolve_flash_target()

        # 构造监督 prompt
        dialog_text = "\n".join(
            f"[{m.get('role','?')}] {m.get('content','')[:300]}"
            for m in recent_messages[-10:]
        )
        prompt = _build_supervisor_prompt(self.cfg.settings.focus_mode) + f"\n\n最近对话：\n{dialog_text}"

        try:
            raw = chat(
                [{"role": "user", "content": prompt}],
                provider=_fp,
                model=_fm,
                temperature=0.1,
                max_tokens=512,
            )
        except AICallError as e:
            logger.warning(f"元监督 AI 调用失败: {e}")
            return {"violated": False, "violations": [], "correction": ""}

        result = self._parse(raw)
        if result.get("violated"):
            correction = result.get("correction", "")
            logger.warning(f"元监督发现违规: {result.get('violations')}")
            log_event("ai_supervisor_violation", {
                "violations": result.get("violations"),
                "correction": correction,
            })
            if correction:
                self.correction_needed.emit(correction)
        return result

    def _parse(self, raw: str) -> dict:
        raw = raw.strip()
        # 去 markdown
        m = re.search(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
        else:
            m2 = re.search(r"\{.*\}", raw, re.DOTALL)
            if m2:
                raw = m2.group(0)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"元监督返回非 JSON: {raw[:200]}")
            return {"violated": False, "violations": [], "correction": ""}
        # r39 P1 修复：json.loads 可能返回 list/str（AI 返回 JSON 数组或字符串），
        # 后续 result.get("violated") 会抛 AttributeError
        if not isinstance(data, dict):
            logger.warning(f"元监督返回非 dict: {type(data).__name__}")
            return {"violated": False, "violations": [], "correction": ""}
        return data
