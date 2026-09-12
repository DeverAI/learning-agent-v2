"""液面渲染工具

统一为所有容器组件（烧杯/锥形瓶/试管/圆底烧瓶/量筒/集气瓶）提供液面渲染。
- 液面高度可调（0.0~1.0，即从底到顶的百分比）
- 支持弧形弯月面
- 颜色可配置（默认水蓝色半透明）
"""

ENABLE_LIQUID = True

# 容器类型 → 形状映射（供 assembler 自动选择渲染方式）
LIQUID_CONTAINERS: dict[str, str] = {
    "beaker": "rect",
    "test_tube": "rect",
    "conical_flask": "conical",
    "round_bottom_flask": "round",
    "flat_bottom_flask": "rect",
    "separatory_funnel": "conical",
    "graduated_cylinder": "cylinder",
    "gas_bottle": "cylinder",
    "water_bath": "rect",
    "water_tank": "rect",
    "evaporating_dish": "rect",
}

# 液面颜色配置：{ "color": CSS fill, "opacity": float }
LIQUID_PRESETS = {
    "water":        {"color": "#42a5f5", "opacity": 0.35},
    "acid":         {"color": "#66bb6a", "opacity": 0.35},
    "base":         {"color": "#ef5350", "opacity": 0.30},
    "oil":          {"color": "#ffb74d", "opacity": 0.40},
    "indicator":    {"color": "#ab47bc", "opacity": 0.25},
    "default":      {"color": "#42a5f5", "opacity": 0.35},
}


def render_liquid(params: dict, w: int, h: int, shape: str = "rect") -> str:
    """统一液面渲染入口

    Args:
        params: 组件参数（含 liquid, liquid_color）
        w: 组件宽
        h: 组件高
        shape: 容器形状 ("rect"|"conical"|"round"|"cylinder")

    Returns:
        SVG 液面片段字符串（不含外层 <g>）

    调用约定：
        容器组件在 _render_component 中调用：
        if params.get("liquid", 0) > 0:
            svg += render_liquid(params, w, h, shape)
    """
    if not ENABLE_LIQUID:
        return ""

    liquid_level = params.get("liquid", 0)
    if liquid_level <= 0:
        return ""

    liquid_level = min(1.0, max(0.01, liquid_level))

    color_key = params.get("liquid_color", "water")
    preset = LIQUID_PRESETS.get(color_key, LIQUID_PRESETS["default"])

    fill = preset["color"]
    opacity = preset["opacity"]

    # 旋转参数：容器整体旋转时，液面在容器局部坐标系内反向旋转以保持水平
    try:
        rotation = float(params.get("rotation", 0) or 0)
    except (TypeError, ValueError):
        rotation = 0.0
    if rotation != rotation:  # NaN 检查
        rotation = 0.0

    if shape == "conical":
        svg = _render_liquid_conical(liquid_level, w, h, fill, opacity)
    elif shape == "round":
        svg = _render_liquid_round(liquid_level, w, h, fill, opacity)
    elif shape == "cylinder":
        svg = _render_liquid_cylinder(liquid_level, w, h, fill, opacity)
    else:  # rect (烧杯/试管)
        svg = _render_liquid_rect(liquid_level, w, h, fill, opacity)

    if rotation and svg:
        svg = f'<g transform="rotate({-rotation},{w/2},{h/2})">{svg}</g>'
    return svg


def _render_liquid_rect(level: float, w: int, h: int,
                        fill: str, opacity: float) -> str:
    """矩形液面（烧杯、试管等）"""
    liquid_h = int(h * level)
    # margin 用于留出容器壁厚度
    margin = max(2, min(6, w // 12))
    lx = margin
    lw = w - 2 * margin
    ly = h - liquid_h

    # 弯月面：椭圆弧，宽度为液面宽的 80%，深度 3px
    meniscus_rx = int(lw * 0.4)
    meniscus_ry = 3

    svg = (
        f'<rect x="{lx}" y="{ly+meniscus_ry}" width="{lw}" height="{liquid_h-meniscus_ry}" '
        f'fill="{fill}" fill-opacity="{opacity}"/>'
    )
    # 弯月面弧
    svg += (
        f'<ellipse cx="{lx+lw//2}" cy="{ly+meniscus_ry}" '
        f'rx="{meniscus_rx}" ry="{meniscus_ry}" '
        f'fill="{fill}" fill-opacity="{opacity}"/>'
    )
    return svg


def _render_liquid_conical(level: float, w: int, h: int,
                           fill: str, opacity: float) -> str:
    """锥形液面（锥形瓶、分液漏斗下部）"""
    liquid_h = int(h * level)

    # 锥形瓶：从 (0,0) 向下 → (w, h) 最宽
    # 液面顶部 y
    ly = h - liquid_h

    # 计算该高度处的液面宽度（按位置线性插值）
    # 锥度方向：顶部(y=0)最窄 = top_w，底部(y=h)最宽 = bottom_w，与该函数上方注释
    # 「从 (0,0) 向下 → (w, h) 最宽」一致。因此宽度应正比于 ly/h。
    #
    # 2026-09-11 修正：原为 `ratio = 1.0 - (ly / h)`，把锥度**插反了** —— 液少（贴瓶底、
    # ly 大）反而算出接近瓶口的最窄宽度，快满（在细颈、ly 小）却算出接近瓶底的最宽宽度，
    # 于是液面宽度与锥形轮廓相反，视觉上锥形瓶读成了直筒。这正是 FUTURE.md
    #「锥形瓶液体计算公式验证」要查的那条。三种 level 的期望宽度已由探针
    # backups/deploy_r5_20260911/_probe_conical.py 固定。
    top_w = int(w * 0.18)   # 瓶口宽
    bottom_w = w            # 瓶底宽
    if h > 0:
        ratio = ly / h
        current_w = int(top_w + (bottom_w - top_w) * ratio)
    else:
        current_w = top_w

    cx = w // 2
    lx = cx - current_w // 2
    lw = current_w

    # 弯月面弧：宽度为液面宽的 80%
    meniscus_rx = max(2, int(lw * 0.4))
    meniscus_ry = 3

    svg = (
        f'<polygon points="{lx},{ly+meniscus_ry} {lx+lw},{ly+meniscus_ry} '
        f'{w},{h} {0},{h}" fill="{fill}" fill-opacity="{opacity}"/>'
    )
    svg += (
        f'<ellipse cx="{cx}" cy="{ly+meniscus_ry}" '
        f'rx="{meniscus_rx}" ry="{meniscus_ry}" '
        f'fill="{fill}" fill-opacity="{opacity}"/>'
    )
    return svg


def _render_liquid_round(level: float, w: int, h: int,
                         fill: str, opacity: float) -> str:
    """圆形液面（圆底烧瓶、球形漏斗）"""
    liquid_h = int(h * level)
    ly = h - liquid_h
    cx = w // 2

    # 使用 clipPath 限制液面在圆形区域内
    clip_id = f"liquid_clip_{id(level)}_{id(h)}"[:20]
    meniscus_rx = max(2, int(w * 0.35))
    meniscus_ry = 3

    svg = (
        f'<defs><clipPath id="{clip_id}">'
        f'<circle cx="{cx}" cy="{h//2}" r="{min(w,h)//2}"/>'
        f'</clipPath></defs>'
    )
    svg += (
        f'<rect x="0" y="{ly+meniscus_ry}" width="{w}" height="{liquid_h}" '
        f'fill="{fill}" fill-opacity="{opacity}" clip-path="url(#{clip_id})"/>'
    )
    svg += (
        f'<ellipse cx="{cx}" cy="{ly+meniscus_ry}" '
        f'rx="{meniscus_rx}" ry="{meniscus_ry}" '
        f'fill="{fill}" fill-opacity="{opacity}" clip-path="url(#{clip_id})"/>'
    )
    return svg


def _render_liquid_cylinder(level: float, w: int, h: int,
                            fill: str, opacity: float) -> str:
    """柱形液面（量筒、集气瓶）"""
    liquid_h = int(h * level)
    margin = max(2, min(6, w // 12))
    lx = margin
    lw = w - 2 * margin
    ly = h - liquid_h

    meniscus_rx = max(2, int(lw * 0.4))
    meniscus_ry = 3

    svg = (
        f'<rect x="{lx}" y="{ly+meniscus_ry}" width="{lw}" height="{liquid_h-meniscus_ry}" '
        f'fill="{fill}" fill-opacity="{opacity}"/>'
    )
    svg += (
        f'<ellipse cx="{lx+lw//2}" cy="{ly+meniscus_ry}" '
        f'rx="{meniscus_rx}" ry="{meniscus_ry}" '
        f'fill="{fill}" fill-opacity="{opacity}"/>'
    )
    return svg
