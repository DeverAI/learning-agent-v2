"""装置位置校准系统

核心机制：
1. 每次手动调整保存后，记录该组合的"样本"（各组件最终坐标）
2. 样本按组合指纹（排序后的组件类型列表）分组
3. 当某组合样本数 > 5 时，对每个组件去掉 x/y 极值后取平均
4. 自动解锁并应用校准后的位置
5. 记录的样本总数持续积累，使校准越来越精确

数据文件：data/gallery/calibration.json
"""

import json
import os
import time
import threading
from typing import Optional

from logger import get_logger
from config import _atomic_write_json, GALLERY_DIR

logger = get_logger()

CALIBRATION_DIR = GALLERY_DIR
os.makedirs(CALIBRATION_DIR, exist_ok=True)
CALIBRATION_FILE = os.path.join(CALIBRATION_DIR, "calibration.json")

_calibration_lock = threading.Lock()


def _calibration_fingerprint(components: list[dict]) -> str:
    """生成组合指纹：按组件类型排序后拼接。缺失 type 的样本不进入指纹。"""
    types = sorted(
        str(comp.get("type", ""))
        for comp in components
        if isinstance(comp, dict) and comp.get("type")
    )
    if not types:
        raise ValueError("校准样本必须至少包含一个带 type 的组件")
    return "+".join(types)


def _load_calibration_data() -> dict:
    """加载校准数据"""
    if os.path.exists(CALIBRATION_FILE):
        try:
            with open(CALIBRATION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load calibration data: %s", e)
    return {}


def _save_calibration_data(data: dict):
    """保存校准数据（原子写入）"""
    try:
        _atomic_write_json(CALIBRATION_FILE, data)
    except Exception as e:
        logger.error("Failed to save calibration data: %s", e)
        raise


def _trimmed_mean(values: list[float]) -> float:
    """去掉一个最大值和一个最小值后取平均"""
    if len(values) <= 2:
        return sum(values) / len(values) if values else 0.0
    sorted_vals = sorted(values)
    trimmed = sorted_vals[1:-1]  # 去掉首尾极值
    return sum(trimmed) / len(trimmed)


def record_adjustment(components: list[dict]) -> dict:
    """记录一次手动调整的样本

    Args:
        components: 调整后的完整组件列表（含type/x/y/w/h/locked等字段）

    Returns:
        包含校准信息的字典：
        {"fingerprint": "...", "sample_count": N, "calibrated": False/True,
         "calibrated_positions": [...]}  # 仅当校准后才返回
    """
    if not isinstance(components, list) or any(
        not isinstance(comp, dict) or not comp.get("type") for comp in components
    ):
        raise ValueError("每个校准组件都必须包含字符串 type 字段")
    with _calibration_lock:
        fp = _calibration_fingerprint(components)
        data = _load_calibration_data()

        if fp not in data:
            data[fp] = {
                "fingerprint": fp,
                "component_types": sorted(c["type"] for c in components if c.get("type")),
                "samples": [],
                "calibrated_position": None,
            }

        entry = data[fp]

        # 记录本次样本（只记录非locked组件的最终位置）
        sample = {
            "timestamp": int(time.time()),
            "positions": [],
        }
        for comp in components:
            if not comp.get("locked", False):
                sample["positions"].append({
                    "type": comp["type"],
                    "x": comp.get("x", 0),
                    "y": comp.get("y", 0),
                })
        entry["samples"].append(sample)
        # 滑动窗口：只保留最近 200 条样本，防止 calibration.json 无限膨胀
        if len(entry["samples"]) > 200:
            entry["samples"] = entry["samples"][-200:]
        # 自上次校准以来的新增样本数：滑动窗口封顶后 sample_count 恒为常数，
        # 若按绝对计数比较会永远不再触发重新校准
        entry["_samples_since_calibrate"] = entry.get("_samples_since_calibrate", 0) + 1

        result = {
            "fingerprint": fp,
            "sample_count": len(entry["samples"]),
            "calibrated": False,
        }

        # 检查是否 >= 5 个样本，且自上次校准后新增满 5 条重新校准
        threshold = 5
        should_calibrate = False
        if len(entry["samples"]) >= threshold:
            if entry.get("calibrated_position") is None:
                should_calibrate = True
            else:
                if entry.get("_samples_since_calibrate", 0) >= 5:
                    should_calibrate = True

        if should_calibrate:
            calibrated_positions = _compute_calibrated_position(entry)
            if calibrated_positions:
                entry["calibrated_position"] = calibrated_positions
                entry["calibrated_at"] = int(time.time())
                entry["_last_calibrated_count"] = len(entry["samples"])
                entry["_samples_since_calibrate"] = 0
                result["calibrated"] = True
                result["calibrated_positions"] = calibrated_positions
                logger.info("Calibration applied for fingerprint '%s' (%d samples)",
                            fp, len(entry["samples"]))

        _save_calibration_data(data)
        return result


def _compute_calibrated_position(entry: dict) -> Optional[list[dict]]:
    """对一组样本计算校准后的位置（去掉极值取平均）"""
    samples = entry.get("samples", [])
    if len(samples) < 3:
        return None

    # 按组件类型分组收集所有x/y值
    type_x: dict[str, list[float]] = {}
    type_y: dict[str, list[float]] = {}
    for sample in samples:
        for pos in sample.get("positions", []):
            t = pos["type"]
            if t not in type_x:
                type_x[t] = []
                type_y[t] = []
            type_x[t].append(pos["x"])
            type_y[t].append(pos["y"])

    # 对所有组件类型计算去掉极值的平均位置
    result = []
    for t in type_x:
        if len(type_x[t]) >= 3:
            avg_x = _trimmed_mean(type_x[t])
            avg_y = _trimmed_mean(type_y[t])
        else:
            avg_x = sum(type_x[t]) / len(type_x[t])
            avg_y = sum(type_y[t]) / len(type_y[t])
        result.append({
            "type": t,
            "x": round(avg_x, 1),
            "y": round(avg_y, 1),
        })

    return result


def get_calibration(components: list[dict]) -> dict:
    """查询某组合是否有校准数据

    Returns:
        {"has_calibration": bool, "sample_count": N, "calibrated_positions": [...]}
    """
    if not components or not isinstance(components, list) or any(
        not isinstance(comp, dict) or not comp.get("type") for comp in components
    ):
        return {"has_calibration": False, "sample_count": 0, "calibrated_positions": None}
    with _calibration_lock:
        fp = _calibration_fingerprint(components)
        data = _load_calibration_data()
        entry = data.get(fp)
        if not entry:
            return {"has_calibration": False, "sample_count": 0, "calibrated_positions": None}

        return {
            "has_calibration": entry.get("calibrated_position") is not None,
            "sample_count": len(entry.get("samples", [])),
            "calibrated_positions": entry.get("calibrated_position"),
        }


def get_calibration_summary() -> dict:
    """获取所有组合的校准摘要"""
    with _calibration_lock:
        data = _load_calibration_data()
        summary = []
        for fp, entry in data.items():
            summary.append({
                "fingerprint": fp,
                "component_types": entry.get("component_types", []),
                "sample_count": len(entry.get("samples", [])),
                "has_calibration": entry.get("calibrated_position") is not None,
                "calibrated_at": entry.get("calibrated_at"),
            })
        return {"combinations": summary, "total": len(summary)}
