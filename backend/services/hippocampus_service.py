import os
import json
import math
import copy
import time
import tempfile
import threading
from datetime import datetime, timezone
from config import SETTINGS_FILE
from logger import get_logger, log_error

logger = get_logger()

HIPPOCAMPUS_DIR = os.path.join(os.path.dirname(os.path.abspath(SETTINGS_FILE)), "hippocampus")
PROFILE_FILE = os.path.join(HIPPOCAMPUS_DIR, "profile.json")
_profile_lock = threading.RLock()

# 衰减参数
BASE_DECAY_LAMBDA = 0.05  # 每 14 天约衰减 50%
DIFFICULTY_FACTORS = {"easy": 0.8, "medium": 1.0, "hard": 1.3}
FADE_THRESHOLD = 0.2      # 低于此值标记为久远记忆
FORGET_THRESHOLD = 0.05   # 低于此值且超期则彻底删除
FORGET_MAX_DAYS = 90      # 超过此天数且低于遗忘阈值则删除

DEFAULT_HIPPOCAMPUS = {
    "version": 1,
    "meta": {
        "baseline_understanding": 0.5,
        "baseline_memory": 0.5,
        "baseline_focus": 0.5,
        "preferred_style": "conceptual",
        "best_study_time": "evening",
        "last_updated": "",
    },
    "topics": {},
}


def _ensure_dir():
    os.makedirs(HIPPOCAMPUS_DIR, exist_ok=True)


def load_hippocampus() -> dict:
    """加载海马体数据，损坏时回退到默认结构"""
    try:
        if os.path.exists(PROFILE_FILE):
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                log_error("hippocampus", f"profile.json is not an object: {type(data).__name__}, resetting")
                return copy.deepcopy(DEFAULT_HIPPOCAMPUS)
            # 使用深拷贝防止嵌套引用污染，递归合并 meta
            result = copy.deepcopy(DEFAULT_HIPPOCAMPUS)
            if isinstance(data.get("meta"), dict):
                result["meta"].update(data["meta"])
            else:
                result["meta"].update(data.get("meta") or {})
            if isinstance(data.get("topics"), dict):
                result["topics"].update(data["topics"])
            # 保留其他未知字段
            for key in data:
                if key not in result:
                    result[key] = data[key]
            # 确保必要字段类型正确
            if not isinstance(result.get("meta"), dict):
                result["meta"] = copy.deepcopy(DEFAULT_HIPPOCAMPUS["meta"])
            if not isinstance(result.get("topics"), dict):
                result["topics"] = {}
            return result
    except (json.JSONDecodeError, IOError, OSError, TypeError, ValueError) as e:
        log_error("hippocampus", f"profile.json corrupted, resetting: {e}")
    return copy.deepcopy(DEFAULT_HIPPOCAMPUS)


def save_hippocampus(data: dict):
    """原子写入海马体数据"""
    _ensure_dir()
    merged = copy.deepcopy(DEFAULT_HIPPOCAMPUS)
    merged.update(data)
    temp_path = ""
    try:
        with _profile_lock:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=HIPPOCAMPUS_DIR,
                prefix=".hippocampus-", suffix=".json", delete=False
            ) as temp_file:
                temp_path = temp_file.name
                json.dump(merged, temp_file, ensure_ascii=False, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, PROFILE_FILE)
    except (IOError, OSError) as e:
        log_error("hippocampus", f"save_hippocampus failed: {e}")
        raise
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_since(iso_timestamp: str) -> float:
    """计算自 ISO 时间戳以来的天数。

    无效时间戳返回 0（视为"刚学过"）：返回极大值会让损坏/legacy 数据在
    衰减周期里 mastery 归零并被静默删除——数据不完整不等于可以遗忘。
    legacy 无时区的时间戳按 UTC 解释（与项目 naive UTC 基准一致）。
    """
    if not iso_timestamp:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0.0, delta.total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def _compute_decay(topic_data: dict, user_memory_baseline: float) -> float:
    """计算衰减后的掌握度"""
    try:
        mastery = float(topic_data.get("mastery", 0.0))
    except (TypeError, ValueError):
        mastery = 0.0
    last_study = topic_data.get("last_study", "")
    difficulty = topic_data.get("difficulty", "medium")
    days = _days_since(last_study)
    if days <= 0:
        return mastery
    diff_factor = DIFFICULTY_FACTORS.get(difficulty, 1.0)
    try:
        raw_baseline = user_memory_baseline if user_memory_baseline is not None else 0.5
        memory_baseline = max(0.1, min(1.0, float(raw_baseline)))
    except (TypeError, ValueError):
        memory_baseline = 0.5
    lam = BASE_DECAY_LAMBDA * diff_factor / memory_baseline
    decayed = mastery * math.exp(-lam * days)
    return max(0.0, min(1.0, decayed))


def get_topic_memory(topic: str, apply_decay: bool = True) -> dict:
    """获取某主题的记忆，可选择应用时间衰减"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.get("topics", {})
        if topic not in topics:
            return {}
        topic_data = dict(topics[topic])
        if apply_decay:
            user_memory = hp.get("meta", {}).get("baseline_memory", 0.5)
            topic_data["mastery"] = _compute_decay(topic_data, user_memory)
        return topic_data


def get_all_memories(apply_decay: bool = True) -> dict:
    """获取所有主题记忆"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.get("topics", {})
        result = {}
        user_memory = hp.get("meta", {}).get("baseline_memory", 0.5)
        for topic, data in topics.items():
            topic_data = dict(data)
            if apply_decay:
                topic_data["mastery"] = _compute_decay(topic_data, user_memory)
            result[topic] = topic_data
        return result


def update_mastery(topic: str, delta: float, reason: str = ""):
    """更新某主题的掌握度"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.setdefault("topics", {})
        if topic not in topics:
            topics[topic] = {
                "mastery": 0.0,
                "weak_points": [],
                "total_minutes": 0,
                "checkpoint_pass_rate": 0.0,
                "last_study": "",
                "difficulty": "medium",
                "history": [],
            }
        td = topics[topic]
        old_mastery = td.get("mastery", 0.0)
        new_mastery = max(0.0, min(1.0, old_mastery + delta))
        td["mastery"] = new_mastery
        td["last_study"] = _now_iso()
        # 安全获取 history 列表（防止 JSON 中 null 导致 AttributeError）
        history = td.get("history")
        if not isinstance(history, list):
            history = []
        history.append({
            "date": _now_iso(),
            "delta": delta,
            "reason": reason or "",
        })
        td["history"] = history
        # 保留最近 50 条历史
        if len(td["history"]) > 50:
            td["history"] = td["history"][-50:]
        hp["meta"]["last_updated"] = _now_iso()
        save_hippocampus(hp)
        return {"topic": topic, "old_mastery": old_mastery, "new_mastery": new_mastery}


def add_weak_point(topic: str, point: str):
    """添加薄弱点"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.setdefault("topics", {})
        if topic not in topics:
            topics[topic] = {
                "mastery": 0.0,
                "weak_points": [],
                "total_minutes": 0,
                "checkpoint_pass_rate": 0.0,
                # 新建主题必须初始化 last_study，否则衰减周期会把无效时间戳
                # 当作极久未学，导致刚创建的薄弱点主题当天被"彻底遗忘"
                "last_study": _now_iso(),
                "difficulty": "medium",
                "history": [],
            }
        td = topics[topic]
        weak_points = td.setdefault("weak_points", [])
        if point not in weak_points:
            weak_points.append(point)
        hp["meta"]["last_updated"] = _now_iso()
        save_hippocampus(hp)


def remove_weak_point(topic: str, point: str):
    """移除薄弱点"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.get("topics", {})
        if topic in topics:
            td = topics[topic]
            weak_points = td.get("weak_points", [])
            if point in weak_points:
                weak_points.remove(point)
            hp["meta"]["last_updated"] = _now_iso()
            save_hippocampus(hp)


def add_topic(topic: str, difficulty: str = "medium", initial_mastery: float = 0.0):
    """新建主题记忆"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.setdefault("topics", {})
        if topic not in topics:
            topics[topic] = {
                "mastery": max(0.0, min(1.0, initial_mastery)),
                "weak_points": [],
                "total_minutes": 0,
                "checkpoint_pass_rate": 0.0,
                "last_study": _now_iso(),
                "difficulty": difficulty if difficulty in DIFFICULTY_FACTORS else "medium",
                "history": [],
            }
            hp["meta"]["last_updated"] = _now_iso()
            save_hippocampus(hp)
            return True
        return False


def delete_topic(topic: str) -> bool:
    """彻底遗忘某主题"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.get("topics", {})
        if topic in topics:
            del topics[topic]
            hp["meta"]["last_updated"] = _now_iso()
            save_hippocampus(hp)
            return True
        return False


def update_study_session(topic: str, minutes: int, checkpoint_pass_rate: float):
    """更新学习会话数据"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.setdefault("topics", {})
        if topic not in topics:
            topics[topic] = {
                "mastery": 0.0,
                "weak_points": [],
                "total_minutes": 0,
                "checkpoint_pass_rate": 0.0,
                "last_study": _now_iso(),
                "difficulty": "medium",
                "history": [],
            }
        td = topics[topic]
        # 类型安全：total_minutes
        old_minutes = td.get("total_minutes")
        try:
            old_minutes = float(old_minutes) if old_minutes is not None else 0.0
        except (TypeError, ValueError):
            old_minutes = 0.0
        td["total_minutes"] = old_minutes + (minutes or 0)
        # 类型安全：checkpoint_pass_rate 滑动平均
        old_rate = td.get("checkpoint_pass_rate")
        try:
            old_rate = float(old_rate) if old_rate is not None else 0.0
        except (TypeError, ValueError):
            old_rate = 0.0
        try:
            new_rate = float(checkpoint_pass_rate) if checkpoint_pass_rate is not None else 0.0
        except (TypeError, ValueError):
            new_rate = 0.0
        td["checkpoint_pass_rate"] = round(old_rate * 0.7 + new_rate * 0.3, 3)
        td["last_study"] = _now_iso()
        hp["meta"]["last_updated"] = _now_iso()
        save_hippocampus(hp)


def run_decay_cycle() -> dict:
    """对所有主题执行一次衰减计算，清理久远和遗忘记忆"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.get("topics", {})
        user_memory = hp.get("meta", {}).get("baseline_memory", 0.5)
        faded = []
        forgotten = []
        for topic in list(topics.keys()):
            td = topics[topic]
            old_mastery = td.get("mastery", 0.0)
            new_mastery = _compute_decay(td, user_memory)
            td["mastery"] = new_mastery
            if new_mastery < FORGET_THRESHOLD and _days_since(td.get("last_study", "")) > FORGET_MAX_DAYS:
                del topics[topic]
                forgotten.append(topic)
            elif new_mastery < FADE_THRESHOLD:
                faded.append(topic)
        hp["meta"]["last_updated"] = _now_iso()
        save_hippocampus(hp)
        return {"faded": faded, "forgotten": forgotten, "remaining": list(topics.keys())}


def detect_conflict(topic: str, new_knowledge: str) -> dict:
    """简单冲突检测：返回可能与新知识冲突的旧记忆"""
    with _profile_lock:
        hp = load_hippocampus()
        topics = hp.get("topics", {})
        if topic not in topics:
            return {"has_conflict": False, "conflicts": []}
        td = topics[topic]
        # 简单关键词重叠检测
        weak_points = td.get("weak_points", [])
        conflicts = []
        for wp in weak_points:
            # 过滤空字符串，避免误判；简单关键词重叠检测
            if wp and isinstance(wp, str) and new_knowledge and wp in new_knowledge:
                conflicts.append(wp)
        return {"has_conflict": len(conflicts) > 0, "conflicts": conflicts}


def get_meta() -> dict:
    """获取用户元认知数据"""
    with _profile_lock:
        hp = load_hippocampus()
        return dict(hp.get("meta", {}))


def update_meta(updates: dict) -> dict:
    """更新用户元认知数据"""
    with _profile_lock:
        hp = load_hippocampus()
        meta = hp.setdefault("meta", dict(DEFAULT_HIPPOCAMPUS["meta"]))
        meta.update(updates)
        meta["last_updated"] = _now_iso()
        hp["meta"] = meta
        save_hippocampus(hp)
        return meta


def get_teaching_context(topic: str = "") -> dict:
    """获取教学上下文：综合用户画像 + 海马体数据，供 AI 生成讲解时使用"""
    with _profile_lock:
        hp = load_hippocampus()
        meta = hp.get("meta", {})
        user_memory = meta.get("baseline_memory", 0.5)

        ctx = {
            "meta": {
                "baseline_understanding": meta.get("baseline_understanding", 0.5),
                "baseline_memory": user_memory,
                "baseline_focus": meta.get("baseline_focus", 0.5),
                "preferred_style": meta.get("preferred_style", "conceptual"),
            },
            "topics": {},
        }

        if topic:
            topics = hp.get("topics", {})
            if topic in topics:
                td = dict(topics[topic])
                td["mastery"] = _compute_decay(td, user_memory)
                ctx["topics"][topic] = td
        else:
            # 返回所有主题
            for t, td in hp.get("topics", {}).items():
                td_copy = dict(td)
                td_copy["mastery"] = _compute_decay(td_copy, user_memory)
                ctx["topics"][t] = td_copy

        return ctx
