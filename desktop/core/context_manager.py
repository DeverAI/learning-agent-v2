"""OISystem 上下文管理器。

职责：
- 每轮调用 flash 模型对历史做分块 + 保留价值评分
- 每块打 block_id，对话消息下方渲染"引用"按钮
- 永久禁用列表持久化到 data/context_blacklist.json
- 用户点击"引用"按钮把对应分块加入调用上下文
"""
import os
import json
import re
from typing import List, Dict, Optional
from PySide6.QtCore import QObject, Signal

from config.settings import ConfigManager
from utils.helpers import DATA_DIR, load_json, save_json, logger, log_event
from utils.exceptions import AICallError


BLACKLIST_FILE = os.path.join(DATA_DIR, "context_blacklist.json")


class ContextBlock:
    """单个上下文块。"""

    def __init__(self, block_id: str, role: str, content: str,
                 summary: str = "", value_score: int = 50):
        self.block_id = block_id
        self.role = role              # user/assistant/system
        self.content = content
        self.summary = summary        # flash 模型生成的摘要
        self.value_score = value_score  # 0-100 保留价值
        self.manually_referenced = False  # 用户点击"引用"强制保留

    def to_dict(self) -> dict:
        return {
            "block_id": self.block_id,
            "role": self.role,
            "content": self.content,
            "summary": self.summary,
            "value_score": self.value_score,
            "manually_referenced": self.manually_referenced,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ContextBlock":
        b = cls(
            block_id=d.get("block_id", ""),
            role=d.get("role", "user"),
            content=d.get("content", ""),
            summary=d.get("summary", ""),
            value_score=d.get("value_score", 50),
        )
        b.manually_referenced = d.get("manually_referenced", False)
        return b


class ContextManager(QObject):
    """上下文管理器。"""

    block_referenced = Signal(str)  # block_id
    block_blacklisted = Signal(str)
    context_pruned = Signal(int)    # 被裁剪掉的块数

    def __init__(self):
        super().__init__()
        self.cfg = ConfigManager()
        self._blacklist: List[str] = self._load_blacklist()
        self._counter = 0

    # ---------- 黑名单 ----------
    def _load_blacklist(self) -> List[str]:
        data = load_json(BLACKLIST_FILE, [])
        return data if isinstance(data, list) else []

    def _save_blacklist(self):
        save_json(BLACKLIST_FILE, self._blacklist)

    def blacklist_block(self, block_id: str):
        """永久禁止主动调用某块。"""
        if block_id and block_id not in self._blacklist:
            self._blacklist.append(block_id)
            self._save_blacklist()
            self.block_blacklisted.emit(block_id)
            logger.info(f"上下文块 {block_id} 已加入黑名单")

    def is_blacklisted(self, block_id: str) -> bool:
        return block_id in self._blacklist

    # ---------- 分块 ----------
    def new_block_id(self, content: str, idx: int = 0) -> str:
        """基于内容哈希 + 索引生成稳定的 block_id，确保跨轮不变化。"""
        import hashlib
        h = hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()[:8]
        self._counter += 1
        return f"blk_{h}_{idx}"

    def build_blocks(self, messages: List[Dict]) -> List[ContextBlock]:
        """把原始消息列表转为 ContextBlock 列表。block_id 基于内容哈希稳定不变。"""
        blocks = []
        for idx, m in enumerate(messages):
            bid = self.new_block_id(m.get("content", ""), idx)
            blocks.append(ContextBlock(
                block_id=bid,
                role=m.get("role", "user"),
                content=m.get("content", ""),
            ))
        return blocks

    # ---------- flash 模型裁剪 ----------
    def prune_with_flash(self, blocks: List[ContextBlock]) -> List[ContextBlock]:
        """调用 flash 模型对每个块打分，裁剪低分块。

        规则：
        - 黑名单块直接裁剪
        - 用户手动引用的块强制保留
        - 超过 max_context_blocks 时按 value_score 降序保留 top N
        """
        # round48：脏配置（None/字符串/负数）不能把整个对话请求拖崩，统一归一化
        try:
            max_blocks = int(getattr(self.cfg.settings, "ai_dialog_max_context_blocks", 10))
        except (TypeError, ValueError):
            max_blocks = 10
        max_blocks = max(1, min(100, max_blocks))

        # 第一步：过滤黑名单
        kept = [b for b in blocks if not self.is_blacklisted(b.block_id)]
        pruned_count = len(blocks) - len(kept)

        # 第二步：标记手动引用
        for b in kept:
            if b.manually_referenced:
                b.value_score = 100

        # 第三步：调 flash 模型打分（可选，失败则用默认分）
        try:
            self._score_with_flash(kept)
        except AICallError as e:
            logger.warning(f"flash 打分失败，用默认分: {e}")

        # 第四步：按分数挑选 top N
        # 审计 P0 修复：挑选排序会破坏原始对话顺序，选完后必须按原始索引还原，
        # 否则裁剪后的消息会以"保留价值降序"发给主模型，
        # 打乱 user/assistant 交替结构导致对话上下文错乱。
        indexed = sorted(enumerate(kept), key=lambda t: t[1].value_score, reverse=True)
        if len(indexed) > max_blocks:
            pruned_count += len(indexed) - max_blocks
            indexed = indexed[:max_blocks]
        kept = [b for _, b in sorted(indexed, key=lambda t: t[0])]

        if pruned_count > 0:
            self.context_pruned.emit(pruned_count)
            log_event("context_pruned", {"count": pruned_count})

        return kept

    def _score_with_flash(self, blocks: List[ContextBlock]):
        """调 flash 模型对每块打分和摘要。"""
        from core.ai_client import chat, resolve_flash_target
        _fp, _fm = resolve_flash_target()

        # 批量打分：把所有块拼成一个 prompt
        lines = ["请对以下对话块逐一评分（0-100，反映对解题的保留价值）并给一句话摘要。",
                 "返回严格 JSON 数组：[{\"idx\": 0, \"score\": 80, \"summary\": \"...\"}]"]
        for i, b in enumerate(blocks):
            lines.append(f"[{i}] role={b.role} content={b.content[:200]}")
        prompt = "\n".join(lines)

        try:
            raw = chat(
                [{"role": "user", "content": prompt}],
                provider=_fp,
                model=_fm,
                temperature=0.1,
                max_tokens=1024,
            )
            # 去 markdown（大小写不敏感，兼容 AI 返回 ```JSON 的情况）
            raw = re.sub(r"^```(?:json|JSON)?\s*", "", raw.strip())
            raw = re.sub(r"\s*```$", "", raw.strip())
            scores = json.loads(raw)
            # r39 P1 修复：AI 可能返回 JSON 对象或字符串而非 list，
            # 遍历会得到非 dict 元素，item.get 抛 AttributeError（不在捕获元组中）
            if not isinstance(scores, list):
                logger.warning(f"flash 打分返回非 list: {type(scores).__name__}")
                return
            for item in scores:
                if not isinstance(item, dict):
                    continue
                idx = item.get("idx", -1)
                if 0 <= idx < len(blocks):
                    blocks[idx].value_score = int(item.get("score", 50))
                    blocks[idx].summary = item.get("summary", "")
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as e:
            logger.warning(f"flash 打分解析失败: {e}")

    # ---------- 引用 ----------
    def reference_block(self, block_id: str, blocks: List[ContextBlock]) -> bool:
        """用户点击引用按钮，把对应块标记为强制保留。"""
        for b in blocks:
            if b.block_id == block_id:
                b.manually_referenced = True
                self.block_referenced.emit(block_id)
                logger.info(f"用户引用了上下文块 {block_id}")
                return True
        return False

    # ---------- 导出为 messages ----------
    def to_messages(self, blocks: List[ContextBlock]) -> List[Dict]:
        """把保留的块转回 messages 格式供 AI 调用。"""
        return [{"role": b.role, "content": b.content} for b in blocks]
