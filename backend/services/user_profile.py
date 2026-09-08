import os
import copy
import json
import tempfile
import threading
from logger import get_logger, log_error
from config import SETTINGS_FILE

logger = get_logger()

# 画像文件与 settings.json 放在同一目录，测试环境替换 SETTINGS_FILE 时
# 画像也会一起隔离，避免端点契约测试污染真实 user_profile.json。
PROFILE_FILE = os.path.join(os.path.dirname(os.path.abspath(SETTINGS_FILE)), "user_profile.json")
_profile_lock = threading.RLock()

DEFAULT_PROFILE = {
    "role": "student",
    # 学生字段
    "school_level": "",
    "grade": "",
    "province": "",
    "city": "",
    "school": "",
    # 教师字段
    "teach_grade": "",
    # 通用
    "favorite_subjects": [],
    "paper_history": [],
    "avg_score": None,
    "notes": "",
    "style_notes": "",
}


def load_profile() -> dict:
    try:
        if os.path.exists(PROFILE_FILE):
            with open(PROFILE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                log_error("profile", f"profile.json is not an object: {type(data).__name__}, resetting")
                return copy.deepcopy(DEFAULT_PROFILE)
            out = copy.deepcopy(DEFAULT_PROFILE)
            out.update(data)
            return out
    except (json.JSONDecodeError, IOError, OSError, TypeError, ValueError) as e:
        log_error("profile", f"profile.json corrupted, resetting: {e}")
    return copy.deepcopy(DEFAULT_PROFILE)


def save_profile(data: dict):
    merged = copy.deepcopy(DEFAULT_PROFILE)
    merged.update(data)
    temp_path = ""
    try:
        directory = os.path.dirname(PROFILE_FILE)
        with _profile_lock:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory, prefix=".profile-", suffix=".json", delete=False
            ) as temp_file:
                temp_path = temp_file.name
                json.dump(merged, temp_file, ensure_ascii=False, indent=2)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, PROFILE_FILE)
    except (IOError, OSError) as e:
        log_error("profile", f"save_profile failed: {e}")
        raise
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def add_paper_result(paper_id: str, title: str, score: float, total: float = 100):
    with _profile_lock:
        p = load_profile()
        p["paper_history"].insert(0, {
            "paper_id": paper_id,
            "title": title,
            "score": score,
            "total": total,
        })
        if len(p["paper_history"]) > 100:
            p["paper_history"] = p["paper_history"][:100]
        percentages = [
            float(h["score"]) / float(h.get("total") or 100) * 100
            for h in p["paper_history"]
            if h.get("score") is not None and float(h.get("total") or 0) > 0
        ]
        p["avg_score"] = round(sum(percentages) / len(percentages), 1) if percentages else None
        save_profile(p)


def get_difficulty_context() -> dict:
    """从用户画像推导难度上下文：地区 & 预估平均分"""
    p = load_profile()
    ctx = {}

    province = p.get("province", "").strip()
    city = p.get("city", "").strip()

    if province:
        loc = province
        if city:
            loc += city
        ctx["region"] = loc

    if p.get("role") == "student":
        level = p.get("school_level", "")
        grade = p.get("grade", "")
        if level and grade:
            ctx["grade"] = f"{level}{grade}"

    ctx["user_role"] = p.get("role", "student")

    if p.get("avg_score") is not None:
        ctx["avg_score"] = p["avg_score"]
    elif p.get("role") == "student":
        level_map = {"小学": 85, "初中": 75, "高中": 65}
        lvl = p.get("school_level", "")
        ctx["avg_score"] = level_map.get(lvl)

    fav = p.get("favorite_subjects", [])
    if fav:
        ctx["favorite_subjects"] = fav

    if p.get("role") == "teacher":
        tg = p.get("teach_grade", "")
        if tg:
            ctx["grade"] = tg

    return ctx
