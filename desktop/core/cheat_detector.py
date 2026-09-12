"""OISystem 作弊检测模块。

职责：
1. 追踪每个题目的代码写入量（行数、字节数、时间戳）
2. 检测异常写入速度（代码量长但时间极短）
3. 从截图分析代码来源（只在 IDE/编辑器中的代码才计入）
4. 页面切换不会丢失进度（按题目聚合）
5. 事件记录与日志

设计：
- 内存持有 `{problem_id: CodeStats}` 字典
- 每次截图分析后，AI 返回的 `code_seen` 和 `current_problem` 更新对应题目的统计
- 当检测到极速输入时，自动标记并记录日志
"""
import json
import os
import time
import dataclasses
from dataclasses import dataclass
from typing import Dict, Optional

from config.settings import ConfigManager
from utils.helpers import logger, log_event, now_cst, format_time, DATA_DIR


# 可疑写入速度阈值：超过 200 bytes/秒 视为极速
SPEED_THRESHOLD_BYTES_PER_SEC = 200

# 代码来源白名单关键词（从 activity 中匹配）
CODE_SOURCE_KEYWORDS = [
    "ide", "vscode", "vs code", "visual studio", "code editor", "editor",
    "编写代码", "写代码", "编程", "代码编辑", "编辑代码",
    "pycharm", "intellij", "clion", "webstorm", "vscode",
    "sublime", "notepad++", "vim", "neovim", "emacs",
]

# 外部 AI 聊天工具（Web 端 + 桌面端），命中后只发"提醒"不强制禁用
EXTERNAL_AI_KEYWORDS = [
    # Web 端
    "chatgpt", "openai", "chat.openai", "gpt-4", "gpt-3.5", "gpt4", "gpt3",
    "claude", "anthropic", "claude.ai",
    "gemini", "bard", "bard.google",
    # P1 修复：只匹配"Copilot 的聊天/对话界面"才视为外部 AI。
    # IDE 内用 Copilot 写代码是合法的（用户在 IDE 调试），不应误判。
    "copilot chat", "copilot.microsoft", "bing.com/chat", "copilot.com",
    "perplexity",
    "poe", "poe.com",
    "文心一言", "yiyan.baidu",
    "通义千问", "tongyi.aliyun",
    "kimi", "kimi.moonshot", "kimi.ai", "moonshot",
    "智谱", "chatglm", "zhipuai",
    "讯飞星火", "spark.iflytek",
    "豆包", "doubao",
    # 桌面端
    "cursor", "cursor.sh",  # Cursor IDE
]


def _is_external_ai(activity: str) -> bool:
    """判断 activity 是否指向外部 AI 聊天工具（Web/桌面）。"""
    if not activity or not isinstance(activity, str):
        return False
    al = activity.lower()
    return any(kw in al for kw in EXTERNAL_AI_KEYWORDS)


@dataclass
class ProblemCodeStats:
    """单题目的代码统计。"""
    problem_id: str                # 题目 ID
    total_bytes: int = 0           # 累计代码字节数
    total_lines: int = 0           # 累计代码行数
    total_chars: int = 0           # 累计字符数（不含空白）
    first_seen: str = ""           # 首次看到该题目
    last_seen: str = ""            # 最近一次看到
    update_count: int = 0          # 更新次数
    # 速度检测
    last_bytes: int = 0
    last_update_time: float = 0.0
    flagged: bool = False          # 是否已被标记为作弊


class CheatDetector:
    """作弊检测器（单例，全局唯一）。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.cfg = ConfigManager()
        self._problems: Dict[str, ProblemCodeStats] = {}  # problem_id -> stats
        self._last_activity = ""                           # 上次 activity
        self._code_seen_counter = 0
        self._external_ai_streak = 0    # 连续识别到外部 AI 的次数（节流提醒）
        self._last_external_ai_at = 0.0  # 上次提醒时间戳
        self._load()

    # ---------- 外部 AI 检测 ----------
    def detect_external_ai(self, activity: str) -> Optional[dict]:
        """检测 activity 是否指向外部 AI 聊天工具。

        返回 None 表示未检测到；否则返回 {kind: "external_ai", activity, ...}。
        节流：连续 3 次以上才提醒，且两次提醒至少间隔 5 分钟。
        """
        if not activity or not _is_external_ai(activity):
            # 重置 streak
            self._external_ai_streak = 0
            return None

        self._external_ai_streak += 1
        now = time.time()
        # round48：冷却分钟数为 None/str 时归一化，不能因脏配置让外部 AI 提醒整体静默失效
        try:
            cfg_min = int(getattr(self.cfg.settings, "external_ai_remind_cooldown_min", 5) or 5)
        except (TypeError, ValueError):
            cfg_min = 5
        cfg_min = max(1, min(1440, cfg_min))
        cooldown_sec = cfg_min * 60
        if self._external_ai_streak < 3:
            return None
        if now - self._last_external_ai_at < cooldown_sec:
            return None

        self._last_external_ai_at = now
        result = {
            "kind": "external_ai",
            "activity": activity,
            "streak": self._external_ai_streak,
        }
        logger.info(f"检测到外部 AI 聊天: {activity} (streak={self._external_ai_streak})")
        log_event("external_ai_detected", {
            "activity": activity,
            "streak": self._external_ai_streak,
        })
        # 重置 streak，避免反复触发
        self._external_ai_streak = 0
        return result

    # ---------- 持久化 ----------
    @property
    def _storage_path(self) -> str:
        return os.path.join(DATA_DIR, "cheat_tracker.json")

    def _load(self):
        """从磁盘恢复代码追踪数据。"""
        try:
            if os.path.exists(self._storage_path):
                with open(self._storage_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for pid, data in raw.get("problems", {}).items():
                    self._problems[pid] = ProblemCodeStats(**data)
                logger.info(f"作弊检测加载 {len(self._problems)} 个题目的追踪数据")
        except Exception as e:
            logger.warning(f"加载作弊检测数据失败: {e}")

    def _save(self):
        """持久化代码追踪数据（页面切换不丢失）。"""
        try:
            data = {
                "problems": {pid: dataclasses.asdict(stats) for pid, stats in self._problems.items()},
                "last_updated": format_time(now_cst()),
            }
            with open(self._storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存作弊检测数据失败: {e}")

    # ---------- 核心检测 ----------

    def analyze_screen_result(self, result: dict):
        """每次截图分析完成后调用，更新代码追踪并检测作弊。"""
        # round48：非 dict 结果直接忽略，避免 .get 抛 AttributeError 使整个检测静默失效
        if not isinstance(result, dict):
            logger.debug(f"作弊检测收到非 dict 结果: {type(result).__name__}")
            return
        activity = result.get("activity", "")
        if not isinstance(activity, str):
            activity = "" if activity is None else str(activity)
        code_seen = result.get("code_seen", False)
        problem = result.get("current_problem", "")
        progress = result.get("progress_delta", "")
        # round48：AI 可能返回数字等非字符串进展描述，统一转 str 供估算器使用
        if not isinstance(progress, str):
            progress = "" if progress is None else str(progress)
        if not isinstance(problem, str):
            problem = "" if problem is None else str(problem)

        # 0. 外部 AI 聊天检测（连续 3 次以上 + 5 分钟冷却才提醒）
        try:
            ext = self.detect_external_ai(activity)
            if ext:
                # 不抛错，调用方按需展示提醒（屏幕分析模块或 dialog_view）
                result["_external_ai_reminder"] = ext
        except Exception as e:
            logger.debug(f"外部 AI 检测失败: {e}")

        # 1. 代码来源判断：仅在 IDE/编辑器中的代码才计入
        if code_seen and self._is_valid_code_source(activity):
            self._code_seen_counter += 1
            # 提取代码量特征（由 AI 在 progress_delta 中描述）
            bytes_estimate = self._estimate_bytes(progress)
            lines_estimate = self._estimate_lines(progress)
            chars_estimate = self._estimate_chars(progress)

            # 2. 如果检测到题目 ID，按题目聚合
            if problem:
                self._update_problem_stats(
                    problem, bytes_estimate, lines_estimate,
                    chars_estimate, activity, progress
                )
            else:
                # 未识别到具体题目，也尝试记一个通用条目
                self._update_problem_stats(
                    "_unknown", bytes_estimate, lines_estimate,
                    chars_estimate, activity, progress
                )
        else:
            # 没有代码 → reset 计数器，但不丢失题目数据
            self._code_seen_counter = max(0, self._code_seen_counter - 1)

        self._last_activity = activity

    def _is_valid_code_source(self, activity: str) -> bool:
        """判断 activity 描述是否指向合法的代码编写场景。"""
        if not activity:
            return False
        al = activity.lower()
        # 排除非代码来源
        exclude_keywords = ["图片浏览器", "图片查看", "photo", "image viewer",
                           "截图", "screenshot", "浏览器", "浏览器页面",
                           "极域", "电子教室", "桌面", "desktop"]
        for kw in exclude_keywords:
            if kw in al:
                return False
        # 正匹配：IDE/编辑器关键词
        for kw in CODE_SOURCE_KEYWORDS:
            if kw in al:
                return True
        # 如果 activity 包含"写"、"打码"等也视为有效
        if any(kw in al for kw in ["写代码", "写程序", "写题", "做题", "提交",
                                    "coding", "programming", "typing code"]):
            return True
        return False

    def _estimate_bytes(self, progress: str) -> int:
        """从 progress_delta 中估算新增代码字节数。"""
        if not progress:
            return 0
        import re
        m = re.search(r'(\d+)\s*(?:bytes?|字节|b)', progress, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # 尝试匹配行数
        m = re.search(r'(\d+)\s*(?:line|行)', progress, re.IGNORECASE)
        if m:
            return int(m.group(1)) * 30  # 每行按 30 bytes 估算
        # 默认：若有"新增"、"添加"等词，给一个保守值
        if any(kw in progress for kw in ["新增", "添加", "新写", "new", "add"]):
            return 50
        return 0

    def _estimate_lines(self, progress: str) -> int:
        """从 progress_delta 中估算新增代码行数。"""
        if not progress:
            return 0
        import re
        m = re.search(r'(\d+)\s*(?:line|行)', progress, re.IGNORECASE)
        if m:
            return int(m.group(1))
        # 没有明确行数就从字节数反推
        bytes_val = self._estimate_bytes(progress)
        if bytes_val > 0:
            return max(1, bytes_val // 30)
        return 0

    def _estimate_chars(self, progress: str) -> int:
        """从 progress_delta 中估算新增有效字符数。"""
        bytes_val = self._estimate_bytes(progress)
        return max(0, bytes_val - bytes_val // 4)  # 约 75% 为有效字符

    def _update_problem_stats(self, problem_id: str, bytes_added: int,
                               lines_added: int, chars_added: int,
                               activity: str, progress: str):
        """更新题目统计数据并检测异常。"""
        now_str = format_time(now_cst())
        now_ts = time.time()

        if problem_id not in self._problems:
            self._problems[problem_id] = ProblemCodeStats(
                problem_id=problem_id,
                first_seen=now_str,
                last_seen=now_str,
            )

        stats = self._problems[problem_id]
        stats.last_seen = now_str
        stats.update_count += 1

        if bytes_added > 0:
            # 速度检测
            time_delta = now_ts - stats.last_update_time if stats.last_update_time > 0 else 999
            if time_delta > 0 and time_delta < 60:  # 只检测 1 分钟内的更新
                speed = bytes_added / time_delta
                if speed > SPEED_THRESHOLD_BYTES_PER_SEC and not stats.flagged:
                    stats.flagged = True
                    self._report_cheat_suspicion(problem_id, bytes_added, time_delta, speed,
                                                  activity, progress)

            stats.total_bytes += bytes_added
            stats.total_lines += lines_added
            stats.total_chars += chars_added
            stats.last_bytes = bytes_added
            stats.last_update_time = now_ts

            logger.debug(
                f"[作弊检测] {problem_id}: +{bytes_added}B/{lines_added}行 "
                f"(累计 {stats.total_bytes}B/{stats.total_lines}行)"
            )

        self._save()

    def _report_cheat_suspicion(self, problem_id: str, bytes_added: int,
                                 time_delta: float, speed: float,
                                 activity: str, progress: str):
        """记录作弊怀疑事件。"""
        msg = (
            f"[作弊检测] ⚠️ 检测到异常写入速度!\n"
            f"  题目: {problem_id}\n"
            f"  新增: {bytes_added} bytes\n"
            f"  耗时: {time_delta:.1f} 秒\n"
            f"  速度: {speed:.1f} bytes/秒 (阈值: {SPEED_THRESHOLD_BYTES_PER_SEC})\n"
            f"  活动: {activity}\n"
            f"  进展: {progress}\n"
            f"  累计: {self._problems[problem_id].total_bytes}B / "
            f"{self._problems[problem_id].total_lines}行"
        )
        logger.warning(msg)
        log_event("cheat_detected", {
            "problem_id": problem_id,
            "bytes_added": bytes_added,
            "time_delta_sec": round(time_delta, 1),
            "speed_bytes_per_sec": round(speed, 1),
            "threshold": SPEED_THRESHOLD_BYTES_PER_SEC,
            "activity": activity,
            "progress": progress,
            "total_bytes": self._problems[problem_id].total_bytes,
            "total_lines": self._problems[problem_id].total_lines,
            "source": "code_input_speed",
        })

    # ---------- 查询 ----------

    def get_problem_stats(self, problem_id: str = None) -> dict:
        """获取题目统计。不传参则返回全部。"""
        if problem_id:
            stats = self._problems.get(problem_id)
            if stats:
                return dataclasses.asdict(stats)
            return {}
        return {pid: dataclasses.asdict(s) for pid, s in self._problems.items()}

    def get_flagged_problems(self) -> list:
        """获取所有被标记作弊的题目。"""
        return [
            {"problem_id": pid, "stats": dataclasses.asdict(s)}
            for pid, s in self._problems.items() if s.flagged
        ]

    def get_summary(self) -> dict:
        """获取汇总信息。"""
        return {
            "total_problems": len(self._problems),
            "total_flagged": sum(1 for s in self._problems.values() if s.flagged),
            "total_bytes": sum(s.total_bytes for s in self._problems.values()),
            "total_lines": sum(s.total_lines for s in self._problems.values()),
        }
