"""组件拼装引擎

核心功能：
1. 端口对齐：根据连接关系自动计算各组件绝对坐标
2. z-order排序：处理前后遮挡关系
3. 预置模板：常见实验装置一键组装
4. 高亮/标签：单独标记和文字说明
"""

import json
import math
import time
import copy
import html
from .component_db import COMPONENT_DB, ALIASES, resolve_type, get_component

# 默认画布尺寸
DEFAULT_VIEWBOX = (0, 0, 400, 300)


def _safe_float(val, default=0.0) -> float:
    """Safe float coercion for AI-generated values (may be strings)。

    任何非有限结果（NaN/Inf/超范围字符串）都回退 default，防止流入 SVG 属性。"""
    try:
        value = float(val)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return value


def normalize_spec(raw_spec: dict) -> tuple[dict, list[str]]:
    """复制并规范化画布 spec，限制异常尺寸和过量节点。"""
    if not isinstance(raw_spec, dict):
        raise ValueError("spec 必须是对象")
    spec = copy.deepcopy(raw_spec)
    warnings: list[str] = []
    allowed_basic = {"rect", "rect_fill", "circle", "line", "text", "arrow"}
    raw_components = spec.get("components", [])
    if not isinstance(raw_components, list):
        warnings.append("components 不是列表，已忽略")
        raw_components = []
    if len(raw_components) > 300:
        warnings.append("组件超过 300 个，已截断")
        raw_components = raw_components[:300]

    components = []
    for index, raw in enumerate(raw_components):
        if not isinstance(raw, dict):
            warnings.append(f"组件 {index} 格式无效，已忽略")
            continue
        ctype = raw.get("type")
        if not isinstance(ctype, str) or not ctype.strip():
            warnings.append(f"组件 {index} 缺少 type，已忽略")
            continue
        ctype = resolve_type(ctype.strip())
        if ctype not in COMPONENT_DB and ctype not in allowed_basic:
            warnings.append(f"未知组件 {ctype}，已忽略")
            continue
        comp = dict(raw)
        comp["type"] = ctype
        cdef = get_component(ctype)
        defaults = (cdef or {})
        for key, default, low, high in (
            ("x", 0, -10000, 10000), ("y", 0, -10000, 10000),
            ("w", defaults.get("default_w", 40), 1, 2000),
            ("h", defaults.get("default_h", 40), 1, 2000),
            ("z", defaults.get("z", 10), -1000, 1000),
        ):
            value = _safe_float(comp.get(key, default), default)
            if not math.isfinite(value):
                value = float(default)
            comp[key] = max(low, min(high, value))
        rotation = _safe_float(comp.get("rotation", 0), 0)
        comp["rotation"] = rotation % 360 if math.isfinite(rotation) else 0
        if "label" in comp:
            comp["label"] = str(comp.get("label") or "")[:500]
        # 实例级数值参数归一：AI/编辑器可能传字符串或非有限值，
        # 直接流入渲染函数会触发 TypeError/ZeroDivisionError → 500
        if "liquid" in comp:
            lq = _safe_float(comp.get("liquid"), 0)
            comp["liquid"] = max(0.0, min(1.0, lq))
        if "water_level" in comp:
            wl = _safe_float(comp.get("water_level"), 0)
            comp["water_level"] = max(0.0, min(1.0, wl))
        if "angle" in comp:
            angle = _safe_float(comp.get("angle"), 0)
            comp["angle"] = angle % 360
        for num_key in ("temperature", "min", "max", "value", "clamp_y", "clamp_w",
                        "a", "b", "c", "k"):
            if num_key in comp:
                comp[num_key] = _safe_float(comp.get(num_key), 0)
        # 布尔键显式归一（防字符串 "false" 被当作真值）
        for bool_key in ("filled", "has_ring", "has_clamp"):
            if bool_key in comp:
                comp[bool_key] = comp.get(bool_key) in (True, 1, "1", "true", "True")
        components.append(comp)
    spec["components"] = components

    vb = spec.get("viewBox")
    if vb is not None:
        if not isinstance(vb, (list, tuple)) or len(vb) != 4:
            warnings.append("viewBox 无效，已使用默认值")
            spec["viewBox"] = list(DEFAULT_VIEWBOX)
        else:
            vals = [_safe_float(v, 0) for v in vb]
            if not all(math.isfinite(v) for v in vals) or vals[2] <= 0 or vals[3] <= 0:
                warnings.append("viewBox 无效，已使用默认值")
                spec["viewBox"] = list(DEFAULT_VIEWBOX)
            else:
                spec["viewBox"] = [max(-10000, min(10000, vals[0])), max(-10000, min(10000, vals[1])),
                                   min(10000, vals[2]), min(10000, vals[3])]

    lines = spec.get("connection_lines", [])
    clean_lines = []
    if isinstance(lines, list):
        for line in lines[:500]:
            if not isinstance(line, dict):
                continue
            start, end = line.get("from"), line.get("to")
            if not (isinstance(start, (list, tuple)) and len(start) == 2 and isinstance(end, (list, tuple)) and len(end) == 2):
                continue
            clean = dict(line)
            clean["from"] = [_safe_float(start[0]), _safe_float(start[1])]
            clean["to"] = [_safe_float(end[0]), _safe_float(end[1])]
            clean["width"] = max(.1, min(20, _safe_float(line.get("width", 1.5), 1.5)))
            clean_lines.append(clean)
    spec["connection_lines"] = clean_lines
    if "title" in spec:
        spec["title"] = str(spec.get("title") or "")[:500]
    return spec, warnings

# ──────────── 模板系统（预置实验装置） ────────────

TEMPLATES = {
    "distillation": {
        "name": "蒸馏装置",
        "viewBox": (0, 0, 420, 420),
        "components": [
            # (type, x, y, params)
            ("tripod", 80, 210, {"label": "三脚架"}),
            ("wire_gauze", 80, 223, {"label": "石棉网"}),
            ("alcohol_lamp", 85, 245, {"label": "酒精灯"}),
            ("round_bottom_flask", 80, 150, {"label": "蒸馏烧瓶", "liquid": 0.3}),
            ("distillation_head", 80, 125, {"label": "蒸馏头"}),
            ("condenser", 175, 100, {"label": "直形冷凝管"}),
            ("cow_receiver", 250, 288, {"label": "牛角管"}),
            ("conical_flask", 255, 318, {"label": "接收瓶"}),
            ("iron_stand", 45, 130, {"label": "铁架台", "clamp_w": 45}),
            ("thermometer", 83, 78, {"label": "温度计"}),
        ],
        "connections": [
            # 连接线（导管连接示意）
            ("alcohol_lamp", "top", "tripod", "bottom"),
            ("wire_gauze", "bottom", "tripod", "top"),
            ("round_bottom_flask", "bottom", "wire_gauze", "top"),
            ("distillation_head", "side", "condenser", "top"),
        ],
        "connection_lines": [
            # 额外的连接线条（管道/导管）
            {"from": (110, 147), "to": (197.5, 100), "color": "#999", "width": 2},
            {"from": (262, 318), "to": (279, 318), "color": "#999", "width": 2},
        ],
    },
    "filtration": {
        "name": "过滤装置",
        "viewBox": (0, 0, 350, 230),
        "components": [
            ("funnel", 120, 35, {"label": "漏斗"}),
            ("beaker", 120, 138, {"label": "烧杯", "liquid": 0.6}),
            ("iron_stand", 55, 50, {"label": "铁架台"}),
            ("glass_tube", 170, 90, {"label": "玻璃棒"}),
        ],
        "connections": [
            ("funnel", "bottom", "beaker", "top"),
        ],
        "connection_lines": [],
    },
    "gas_collection": {
        "name": "排水集气装置",
        "viewBox": (0, 0, 350, 210),
        "components": [
            ("water_tank", 120, 140, {"label": "水槽", "water_level": 0.6}),
            ("gas_bottle", 150, 105, {"label": "集气瓶", "filled": False}),
            ("delivery_tube", 50, 90, {"label": "导管"}),
            ("test_tube", 260, 100, {"label": "试管", "liquid": 0.3}),
        ],
        "connections": [
            ("delivery_tube", "right", "gas_bottle", "top"),
        ],
        "connection_lines": [
            {"from": (70, 105), "to": (150, 105), "color": "#999", "width": 1.5},
        ],
    },
    "electrolysis": {
        "name": "电解水装置",
        "viewBox": (0, 0, 300, 220),
        "components": [
            ("water_tank", 80, 130, {"label": "水槽", "water_level": 0.7}),
            ("test_tube", 55, 68, {"label": "正极(氧气)", "liquid": 0.1}),
            ("test_tube", 115, 68, {"label": "负极(氢气)", "liquid": 0.2}),
            ("glass_tube", 35, 105, {}),
            ("glass_tube", 135, 105, {}),
        ],
        "connections": [],
        "connection_lines": [
            {"from": (55, 130), "to": (55, 68), "color": "#e33", "width": 1.5},
            {"from": (115, 130), "to": (115, 68), "color": "#33e", "width": 1.5},
            {"from": (55, 130), "to": (115, 130), "color": "#999", "width": 1},
        ],
    },
    "pulley_system": {
        "name": "滑轮组",
        "viewBox": (0, 0, 300, 250),
        "components": [
            ("pulley", 130, 50, {"label": "定滑轮"}),
            ("block", 130, 170, {"label": "G"}),
        ],
        "connections": [
            ("pulley", "bottom_left", "block", "top"),
        ],
        "connection_lines": [
            {"from": (130, 100), "to": (130, 155), "color": "#333", "width": 1.5},
            {"from": (130, 90), "to": (80, 130), "color": "#333", "width": 1},
        ],
    },
    "inclined_plane": {
        "name": "斜面滑块",
        "viewBox": (0, 0, 300, 200),
        "components": [
            ("inclined_plane", 30, 120, {"angle": 30, "label": "斜面"}),
            ("block", 110, 85, {"label": "m"}),
            ("spring", 180, 30, {"label": "弹簧"}),
        ],
        "connections": [
            ("block", "right", "spring", "bottom"),
        ],
        "connection_lines": [],
    },
}


def _resolve_type_index(type_str: str) -> tuple[str, int]:
    """解析类型字符串，支持 type:index 格式（如 test_tube:1）
    Returns (type_name, index), index默认为0
    """
    if ":" in type_str:
        parts = type_str.split(":", 1)
        try:
            return resolve_type(parts[0]), int(parts[1])
        except (ValueError, TypeError):
            from logger import get_logger
            get_logger().warning("Invalid type:index format '%s', using index 0", type_str)
            return resolve_type(parts[0]), 0
    return resolve_type(type_str), 0


def _resolve_connections(components: list[dict], connections: list[tuple], custom_ports: dict | None = None) -> list[dict]:
    """根据端口连接关系，计算各组件坐标

    Args:
        components: [{"type": "beaker", "params": {}, "z": 10}]
        connections: [("typeA", "portA", "typeB", "portB")]
                    支持 type:index 格式指定多组件实例，如 ("test_tube:0", "top", "test_tube:1", "bottom")
        custom_ports: {"typeA": [{"id": "name", "dx": x, "dy": y, "dir": "up"}]}
                      AI自创端口，当组件库中不存在所需端口时使用

    Returns:
        components with x,y filled in
    """
    from logger import get_logger
    logger = get_logger()

    if not connections:
        return components

    # Build index map: type -> list of components (by occurrence order)
    type_index_map: dict[str, list[dict]] = {}
    for comp in components:
        t = comp["type"]
        if t not in type_index_map:
            type_index_map[t] = []
        type_index_map[t].append(comp)

    def _find_port(comp_type: str, port_id: str, comp_def: dict | None) -> dict | None:
        """查找端口：优先组件库内置端口，fallback到custom_ports"""
        if comp_def:
            for p in comp_def.get("ports", []):
                if p.get("id") == port_id:
                    return p
        if custom_ports and comp_type in custom_ports:
            for p in custom_ports[comp_type]:
                if not isinstance(p, dict):
                    continue
                pid = p.get("id")
                if pid == port_id:
                    # Validate and coerce types for custom_ports
                    return {
                        "id": pid,
                        "dx": _safe_float(p.get("dx", 0)),
                        "dy": _safe_float(p.get("dy", 0)),
                        "dir": p.get("dir", "up"),
                        "gap": _safe_float(p.get("gap", 0)),
                    }
        return None

    # Process connections sequentially
    for conn in connections:
        src_type_str, src_port, dst_type_str, dst_port = conn
        src_type, src_idx = _resolve_type_index(src_type_str)
        dst_type, dst_idx = _resolve_type_index(dst_type_str)

        src_list = type_index_map.get(src_type)
        dst_list = type_index_map.get(dst_type)
        if not src_list or not dst_list:
            logger.warning("Connection references unknown type: src=%s dst=%s", src_type, dst_type)
            continue
        if src_idx >= len(src_list) or dst_idx >= len(dst_list):
            logger.warning("Connection index out of range: src=%s[%d](have %d) dst=%s[%d](have %d)",
                           src_type, src_idx, len(src_list), dst_type, dst_idx, len(dst_list))
            continue

        src_comp = src_list[src_idx]
        dst_comp = dst_list[dst_idx]

        # If both locked → nothing to do
        if src_comp.get("locked", False) and dst_comp.get("locked", False):
            continue

        src_def = get_component(src_type)
        dst_def = get_component(dst_type)
        if not src_def or not dst_def:
            logger.warning("Connection references component not in DB: src=%s dst=%s", src_type, dst_type)
            continue

        # Find port definitions (built-in first, then custom_ports)
        sp = _find_port(src_type, src_port, src_def)
        dp = _find_port(dst_type, dst_port, dst_def)
        if not sp or not dp:
            missing = []
            if not sp: missing.append(f"{src_type}.{src_port}")
            if not dp: missing.append(f"{dst_type}.{dst_port}")
            logger.warning("Connection references unknown port: %s (use custom_ports to define)",
                           ", ".join(missing))
            continue

        # If destination is locked → move source to align with destination
        if dst_comp.get("locked", False):
            src_comp["x"] = dst_comp.get("x", 0) + dp["dx"] - sp["dx"]
            # y 方向叠加 gap：仅垂直 gap（火焰顶到容器底），用于在端口对齐基础上留出视觉间距
            src_comp["y"] = dst_comp.get("y", 0) + dp["dy"] - sp["dy"] + dp.get("gap", 0) - sp.get("gap", 0)
            continue

        # If source is locked → move destination to align with source
        if src_comp.get("locked", False):
            dst_comp["x"] = src_comp.get("x", 0) + sp["dx"] - dp["dx"]
            dst_comp["y"] = src_comp.get("y", 0) + sp["dy"] - dp["dy"] + dp.get("gap", 0) - sp.get("gap", 0)
            continue

        # Both unlocked → get source position and move destination
        sx = src_comp.get("x", 0)
        sy = src_comp.get("y", 0)
        dst_comp["x"] = sx + sp["dx"] - dp["dx"]
        dst_comp["y"] = sy + sp["dy"] - dp["dy"] + dp.get("gap", 0) - sp.get("gap", 0)

    return components


def resolve_ports_for_modification(
    existing: list[dict],
    new_components: list[dict],
    connections: list[tuple],
    custom_ports: dict | None = None,
) -> list[dict]:
    """在已有图上插入新组件时，进行端口对齐

    Args:
        existing: 已有组件列表（含已保存的x/y坐标）
        new_components: 新插入的组件列表（可能不含x/y）
        connections: 连接关系 [("typeA", "portA", "typeB", "portB")]
        custom_ports: AI自创端口

    Returns:
        调整后的完整组件列表
    """
    # Mark existing components as locked by default
    for comp in existing:
        if "locked" not in comp:
            comp["locked"] = True

    # Combine all components
    all_components = list(existing) + list(new_components)

    # Resolve connections — only non-locked (new) components will be moved
    if connections:
        resolved_connections = []
        for conn in connections:
            src_type, src_port, dst_type, dst_port = conn
            src_resolved, _ = _resolve_type_index(src_type)
            dst_resolved, _ = _resolve_type_index(dst_type)
            resolved_connections.append((
                src_resolved, src_port,
                dst_resolved, dst_port,
            ))
        all_components = _resolve_connections(all_components, resolved_connections, custom_ports)

    return all_components


def _apply_connection_lines(svg_parts: list, lines: list[dict]):
    """添加连接线（导管、导线等）"""
    for ln in lines:
        f = ln["from"]
        t = ln["to"]
        color = ln.get("color", "#333")
        width = ln.get("width", 1.5)
        dashed = ln.get("dashed", False)
        dash_attr = ' stroke-dasharray="4,3"' if dashed else ""
        svg_parts.append(
            f'<line x1="{f[0]}" y1="{f[1]}" x2="{t[0]}" y2="{t[1]}" '
            f'stroke="{color}" stroke-width="{width}"{dash_attr}/>'
        )


def _add_highlight(svg_parts: list, comp: dict, x: float, y: float, w: float, h: float):
    """给组件添加高亮效果（带颜色外框+阴影）"""
    highlight = comp.get("highlight", "")
    if not highlight:
        return
    color_map = {"red": "#e33", "blue": "#33e", "green": "#3a3", "yellow": "#fa0"}
    color = color_map.get(highlight, highlight)
    pad = 4
    svg_parts.append(
        f'<rect x="{x-pad}" y="{y-pad}" width="{w+pad*2}" height="{h+pad*2}" '
        f'rx="3" fill="none" stroke="{color}" stroke-width="2.5" stroke-opacity="0.7"/>'
    )


def _add_annotation(svg_parts: list, comp: dict, x: float, y: float, w: float, h: float):
    """添加标注文字"""
    note = comp.get("note", "")
    if not note:
        return
    note = html.escape(str(note))
    pos = comp.get("note_pos", "bottom")
    nx, ny = x + w / 2, y + h + 14
    if pos == "top":
        ny = y - 6
    elif pos == "left":
        nx, ny = x - 4, y + h / 2 + 4
        svg_parts.append(
            f'<text x="{nx}" y="{ny}" font-size="12" fill="#e60" '
            f'text-anchor="end" font-weight="bold">{note}</text>'
        )
        return
    svg_parts.append(
        f'<text x="{nx}" y="{ny}" font-size="12" fill="#e60" '
        f'text-anchor="middle" font-weight="bold">{note}</text>'
    )


def _render_component(comp: dict) -> str | None:
    """渲染单个组件到SVG"""
    ctype = comp["type"]
    x = comp.get("x", 0)
    y = comp.get("y", 0)

    # 基本图形（非组件库的基本形状）
    if ctype in ("rect", "rect_fill"):
        rw = comp.get("w", 20)
        rh = comp.get("h", 20)
        fill = comp.get("fill", "none")
        stroke = comp.get("stroke", "#333")
        parts = [f'<rect x="{x}" y="{y}" width="{rw}" height="{rh}" '
                 f'fill="{fill}" stroke="{stroke}" stroke-width="1.5" rx="{comp.get("rx", 0)}"/>']
        if comp.get("label"):
            parts.append(f'<text x="{x+rw/2}" y="{y+rh/2+4}" font-size="12" '
                         f'fill="#333" text-anchor="middle">{html.escape(str(comp["label"]))}</text>')
        return "\n".join(parts)

    if ctype == "circle":
        cx = comp.get("cx", x)
        cy = comp.get("cy", y)
        r = comp.get("r", 10)
        return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{comp.get("fill","none")}" stroke="{comp.get("stroke","#333")}" stroke-width="1.5"/>'

    if ctype == "line":
        x2 = comp.get("x2", x + 20)
        y2 = comp.get("y2", y + 20)
        return f'<line x1="{x}" y1="{y}" x2="{x2}" y2="{y2}" stroke="{comp.get("stroke","#333")}" stroke-width="{comp.get("width",1.5)}"/>'

    if ctype == "text":
        return f'<text x="{x}" y="{y}" font-size="{comp.get("font_size",14)}" fill="{comp.get("fill","#333")}" text-anchor="{comp.get("text_anchor","start")}">{html.escape(str(comp.get("text", "")))}</text>'

    if ctype == "arrow":
        x2 = comp.get("x2", x + 30)
        y2 = comp.get("y2", y)
        # Simple arrow: line + arrowhead
        dx, dy = x2 - x, y2 - y
        length = math.sqrt(dx*dx + dy*dy)
        if length == 0:
            return ""
        ux, uy = dx/length, dy/length
        ah = 8
        ax1 = x2 - ah * ux - ah*0.4 * uy
        ay1 = y2 - ah * uy + ah*0.4 * ux
        ax2 = x2 - ah * ux + ah*0.4 * uy
        ay2 = y2 - ah * uy - ah*0.4 * ux
        return f'<line x1="{x}" y1="{y}" x2="{x2}" y2="{y2}" stroke="{comp.get("stroke","#333")}" stroke-width="{comp.get("width",1.5)}"/><polygon points="{x2},{y2} {ax1},{ay1} {ax2},{ay2}" fill="{comp.get("stroke","#333")}"/>'

    # 组件库中的标准组件
    cdef = get_component(ctype)
    if not cdef:
        from logger import get_logger
        get_logger().warning("Unknown component type '%s' in _render_component, skipped", ctype)
        return None

    w = comp.get("w", cdef["default_w"])
    h = comp.get("h", cdef["default_h"])

    # 合并参数：组件的params + comp的params
    params = dict(comp.get("params", {}))
    for k in ("w", "h", "liquid", "liquid_color", "label", "filled", "water_level", "angle",
              "clamp_y", "clamp_w", "has_ring", "has_clamp",
              "a", "b", "c", "k", "temperature", "rotation", "steps",
              "measure_type", "min", "max", "value", "position", "direction",
              "label_x", "label_y"):
        if k in comp:
            params[k] = comp[k]
    params.setdefault("w", w)
    params.setdefault("h", h)
    # 如果没有label但组件有label字段，用组件的label
    if comp.get("label"):
        params["label"] = comp["label"]
    # 文本字段统一在渲染入口转义（组件库内部不得再二次转义），
    # 防止 label 含 & < > 时把整张图静默降级成空白画布
    for text_key in ("label", "label_x", "label_y"):
        if text_key in params and params[text_key] is not None:
            params[text_key] = html.escape(str(params[text_key]), quote=True)

    svg_inner = cdef["render"](params)

    # Keep the component as one group so editor rotation and exported SVG agree.
    rotation = _safe_float(comp.get("rotation", 0), 0) % 360
    transform = f"translate({x},{y})"
    if rotation:
        transform += f" rotate({rotation},{w/2},{h/2})"
    return f'<g transform="{transform}">{svg_inner}</g>'


def assemble(comp_spec: dict) -> str:
    """拼装入口函数

    Args:
        comp_spec: {
            "template": "distillation" (可选，预置模板),
            "viewBox": [0, 0, 400, 300] (可选),
            "components": [
                {"type": "beaker", "x": 50, "y": 80, "w": 50, "h": 42,
                 "label": "烧杯", "liquid": 0.5, "highlight": "red",
                 "note": "A", "note_pos": "top", "z": 10},
                ...
            ],
            "connections": [("酒精灯", "top", "烧瓶", "bottom")] (可选，端口对齐),
            "connection_lines": [{"from": [x,y], "to": [x,y], ...}] (可选，额外线条),
            "title": "实验装置图" (可选)
        }

    Returns:
        SVG字符串
    """
    from logger import get_logger
    logger = get_logger()
    comp_spec, normalize_warnings = normalize_spec(comp_spec)
    for warning in normalize_warnings:
        logger.warning("diagram spec normalized: %s", warning)

    # ─── 0. 空spec检查 ───
    if not comp_spec.get("template") and not comp_spec.get("components"):
        logger.warning("assemble called with empty spec (no template, no components)")
        ts = int(time.time())
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 300"'
            f' style="background:#fafafa;max-width:100%;height:auto">\n'
            f'<text x="200" y="150" text-anchor="middle" fill="#999" font-size="14">'
            f'无可用组件，无法生成示意图</text>\n'
            f'<!-- ts:{ts} -->\n'
            f'</svg>'
        )

    # ─── 1. 检查是否是模板 ───
    template_name = comp_spec.get("template", "")
    if template_name and template_name in TEMPLATES:
        tpl = TEMPLATES[template_name]
        # 构建标准格式
        components = []
        for c in tpl["components"]:
            ctype, cx, cy, params = c
            comp = {"type": ctype, "x": cx, "y": cy}
            comp.update(params)
            cdef = get_component(ctype)
            if cdef:
                comp.setdefault("w", cdef["default_w"])
                comp.setdefault("h", cdef["default_h"])
            components.append(comp)
        # 用户自定义 viewBox 优先于模板默认
        viewbox = comp_spec.get("viewBox", tpl.get("viewBox", DEFAULT_VIEWBOX))
        connection_lines = list(tpl.get("connection_lines", []))
        # 模板坐标已手调，不执行端口对齐（仅保留connection_lines做视觉连接）
        # 合并外部覆盖的connection_lines
        if comp_spec.get("connection_lines"):
            connection_lines.extend(comp_spec["connection_lines"])
        # 合并外部组件：同type的合并属性（允许覆盖位置/标签等），新type追加
        if comp_spec.get("components"):
            for oc in comp_spec["components"]:
                existing = [c for c in components if c["type"] == oc["type"]]
                if existing:
                    # 合并用户提供的字段到模板现有组件
                    for k, v in oc.items():
                        if k != "type":
                            existing[0][k] = v
                else:
                    cdef = get_component(oc["type"])
                    if cdef:
                        oc.setdefault("w", cdef["default_w"])
                        oc.setdefault("h", cdef["default_h"])
                    components.append(oc)
    else:
        # ─── 2. 自由组装模式 ───
        components = list(comp_spec.get("components", []))
        for comp in components:
            cdef = get_component(comp["type"])
            if cdef:
                comp.setdefault("w", cdef["default_w"])
                comp.setdefault("h", cdef["default_h"])
        viewbox = comp_spec.get("viewBox", DEFAULT_VIEWBOX)

        # ─── AI自创端口（当内置端口不够用时） ───
        custom_ports = comp_spec.get("custom_ports", None)

        # ─── 端口对齐 ───
        connections = comp_spec.get("connections", [])
        if connections:
            resolved_connections = []
            for conn in connections:
                src_type, src_port, dst_type, dst_port = conn
                src_resolved, _ = _resolve_type_index(src_type)
                dst_resolved, _ = _resolve_type_index(dst_type)
                resolved_connections.append((
                    src_resolved, src_port,
                    dst_resolved, dst_port,
                ))
            components = _resolve_connections(components, resolved_connections, custom_ports)

        connection_lines = list(comp_spec.get("connection_lines", []))

    # ─── 3. z-order排序（从小到大，后排先画） ───
    for comp in components:
        cdef = get_component(comp["type"])
        if cdef and "z" not in comp:
            comp["z"] = cdef["z"]
        elif "z" not in comp:
            comp["z"] = 10

    components.sort(key=lambda c: c.get("z", 10))

    # ─── 4. 计算总占区域，自动适应viewBox ───
    all_x, all_y, max_x_vals, max_y_vals = [], [], [], []
    for comp in components:
        cx = comp.get("x", 0)
        cy = comp.get("y", 0)
        cw = comp.get("w", 40)
        ch = comp.get("h", 40)
        # 组件库定义的 overflow（刻度/标注溢出）
        cdef = get_component(comp.get("type", ""))
        ov = cdef.get("overflow", {}) if cdef else {}
        ov_left = ov.get("left", 0)
        ov_top = ov.get("top", 0)
        ov_right = ov.get("right", 0)
        ov_bottom = ov.get("bottom", 0)
        all_x.append(cx - ov_left)
        all_y.append(cy - ov_top)
        max_x_vals.append(cx + cw + ov_right)
        max_y_vals.append(cy + ch + ov_bottom)

    # 将连接线的端点也纳入viewBox计算
    for ln in connection_lines:
        f = ln["from"]
        t = ln["to"]
        all_x.append(f[0])
        all_y.append(f[1])
        max_x_vals.append(t[0])
        max_y_vals.append(t[1])

    # Auto-calculate viewBox if not specified by user
    auto_vb = comp_spec.get("viewBox", None)
    if not auto_vb and not template_name:
        if all_x and all_y and max_x_vals and max_y_vals:
            # 不再把 min 钳到 0：`max(0, min(all_x) - 20)` 会在内容靠近原点时**把这 20px
            # 留白整个吃掉**（min(all_x)=5 -> max(0,-15)=0），于是左/上两侧的刻度与标注溢出
            # 正好落在 viewBox 之外被裁掉——正是「自动 viewBox 对连接线和刻度溢出的覆盖」
            # 要修的问题。SVG 允许 viewBox 原点为负，下游只读宽高，故安全。
            min_x = min(all_x) - 20
            min_y = min(all_y) - 20
            max_x = max(max_x_vals) + 20
            max_y = max(max_y_vals) + 20
            # Make minimum size（最小尺寸约束按内容起点算，保持原有"至少 200x150 视野"语义）
            max_x = max(max_x, min_x + 200)
            max_y = max(max_y, min_y + 150)
            viewbox = (min_x, min_y, max_x - min_x, max_y - min_y)
        else:
            viewbox = DEFAULT_VIEWBOX

    vb = f"{viewbox[0]} {viewbox[1]} {viewbox[2]} {viewbox[3]}"

    # ─── 5. 渲染 ───
    svg_parts = []
    title = comp_spec.get("title", "")
    if title:
        svg_parts.append(
            f'<text x="{viewbox[0] + viewbox[2]/2}" y="{viewbox[1] + 20}" '
            f'font-size="16" fill="#333" text-anchor="middle" font-weight="bold">{html.escape(str(title))}</text>'
        )

    # 先画连接线（在组件后面，除非常见效果需要）
    _apply_connection_lines(svg_parts, connection_lines)

    # 画组件
    for comp in components:
        svg_inner = _render_component(comp)
        if svg_inner:
            svg_parts.append(svg_inner)

    # 添加高亮和标注（在所有组件之上）
    for comp in components:
        ctype = comp["type"]
        cdef = get_component(ctype)
        if cdef:
            x = comp.get("x", 0)
            y = comp.get("y", 0)
            w = comp.get("w", cdef["default_w"])
            h = comp.get("h", cdef["default_h"])
            _add_highlight(svg_parts, comp, x, y, w, h)
            _add_annotation(svg_parts, comp, x, y, w, h)

    # 背景
    bg = comp_spec.get("background", "none")
    svg_content = "\n".join(svg_parts)

    # 时间戳（不修改传入的 comp_spec）
    ts = int(time.time())

    result = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}"'
        f' style="background:{bg};max-width:100%;height:auto">\n'
        f'{svg_content}\n'
        f'<!-- ts:{ts} -->\n'
        f'</svg>'
    )
    return result


def available_templates() -> list[dict]:
    """返回所有可用模板列表（含组件数据，供前端编辑器使用）"""
    return [
        {
            "id": tid,
            "name": tpl["name"],
            "viewBox": list(tpl["viewBox"]),
            "components": tpl.get("components", []),
        }
        for tid, tpl in TEMPLATES.items()
    ]


def available_components(category: str = "") -> list[dict]:
    """返回所有可用组件列表（用于AI提示词）"""
    result = []
    for cid, cdef in COMPONENT_DB.items():
        if category and cdef["category"] != category:
            continue
        ports_str = ", ".join([f"{p['id']}({p['dir']})" for p in cdef["ports"]])
        ports_data = [{"id": p["id"], "dx": p["dx"], "dy": p["dy"], "dir": p["dir"]} for p in cdef["ports"]]
        result.append({
            "type": cid,
            "name": cdef["name"],
            "category": cdef["category"],
            "ports": ports_str,
            "ports_data": ports_data,
            "default_w": cdef["default_w"],
            "default_h": cdef["default_h"],
            "z": cdef["z"],
            "locked": False,
            # 组件声明的绘制溢出（温度计/刻度尺的刻度线与示数会画到 default_w/h 之外）。
            # 后端 auto-viewBox 已经在用这个字段，但此前**没有下发给前端**，
            # 导致编辑器只能按 default_w/h 画命中区与选择框——看到的内容与能点到的框对不上，
            # 视觉上也会压到邻居（FUTURE.md「温度计刻度线超出组件宽度的视觉溢出」）。
            "overflow": cdef.get("overflow", {}),
        })
    return result
