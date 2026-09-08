"""标签统一服务：维护知识点标签别名映射并标准化标签。"""

import os
import json
import tempfile
import threading
from logger import get_logger
from config import STORAGE_DIR

logger = get_logger()

# 映射文件必须从 config.STORAGE_DIR 派生（测试隔离的唯一来源），
# 不允许用 __file__ 把数据写进源码树。
def _tag_file() -> str:
    return os.path.join(STORAGE_DIR, "tag_unification.json")


# 进程内缓存：按文件 mtime 失效，避免批量归一标签时反复读磁盘
_cache: dict = {"mtime": None, "data": {}}
_update_lock = threading.Lock()


def _load_unification_map() -> dict:
    try:
        path = _tag_file()
        if os.path.exists(path):
            mtime = os.path.getmtime(path)
            if _cache["mtime"] == mtime:
                return _cache["data"]
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _cache["mtime"] = mtime
                _cache["data"] = data
                return data
    except Exception as e:
        logger.warning("Failed to load tag unification map: %s", e)
    return {}


def load_unification_map() -> dict:
    """读取标签统一映射，格式 {canonical: [aliases...]}。失败返回空 dict（兼容旧调用）。"""
    return _load_unification_map()


def save_unification_map(mapping: dict):
    """原子写入标签统一映射（先写临时文件，再 os.replace）。失败时重新抛出，绝不静默吞掉。"""
    path = _tag_file()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=".tag_unification_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _cache["mtime"] = None
        _cache["data"] = {}
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _build_reverse_index(mapping: dict) -> dict:
    """建立 alias_lower -> canonical 的反向索引。"""
    reverse = {}
    for canonical, aliases in mapping.items():
        canonical = str(canonical).strip()
        if canonical:
            reverse[canonical.lower()] = canonical
        if isinstance(aliases, list):
            for alias in aliases:
                alias = str(alias).strip()
                if alias:
                    reverse[alias.lower()] = canonical
    return reverse


def canonicalize_tag(tag: str) -> str:
    """根据映射把标签转为标准标签；大小写不敏感匹配别名；若找不到标准映射返回原标签。"""
    if not isinstance(tag, str):
        return tag
    mapping = _load_unification_map()
    if not mapping:
        return tag
    reverse = _build_reverse_index(mapping)
    return reverse.get(tag.strip().lower(), tag)


def canonicalize_tags(tags) -> list:
    """对列表中每个标签调用 canonicalize_tag，并去重（保留顺序）。

    容忍 AI 返回字符串（按逗号拆分）或 None（返回空列表），
    避免字符串被逐字符拆分成损坏标签。
    """
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    if not isinstance(tags, (list, tuple)):
        return []
    seen = set()
    result = []
    for tag in tags:
        canonical = canonicalize_tag(tag)
        if not canonical:
            continue
        if canonical not in seen:
            seen.add(canonical)
            result.append(canonical)
    return result


def update_unification_map(groups: list[list[str]]):
    """合并新的归并组到现有映射；标准标签取组中第一个。"""
    if not groups:
        return

    # 读-改-写全程加锁，防止并发更新互相覆盖丢失别名
    with _update_lock:
        mapping = load_unification_map()

        # 规范化现有映射
        normalized = {}
        for canonical, aliases in mapping.items():
            canonical = str(canonical).strip()
            if not canonical:
                continue
            normalized.setdefault(canonical, set())
            if isinstance(aliases, list):
                for alias in aliases:
                    alias = str(alias).strip()
                    if alias and alias != canonical:
                        normalized[canonical].add(alias)

        # 合并新的归并组
        for group in groups:
            if not isinstance(group, list) or not group:
                continue
            canonical = str(group[0]).strip()
            if not canonical:
                continue

            new_aliases = set()
            for item in group[1:]:
                item = str(item).strip()
                if item and item != canonical:
                    new_aliases.add(item)

            # 若新别名本身是已有的标准标签，将其别名合并过来并移除旧标准
            merged = set()
            for alias in list(new_aliases):
                if alias in normalized:
                    merged.update(normalized.pop(alias))
            new_aliases.update(merged)
            new_aliases.discard(canonical)

            normalized.setdefault(canonical, set())
            normalized[canonical].update(new_aliases)
            normalized[canonical].discard(canonical)

        # 转回稳定列表格式
        final_mapping = {}
        for canonical in sorted(normalized.keys()):
            aliases = sorted(normalized[canonical])
            if aliases:
                final_mapping[canonical] = aliases

        save_unification_map(final_mapping)
