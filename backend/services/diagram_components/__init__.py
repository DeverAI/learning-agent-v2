"""
实验装置图组件拼装系统

架构：
1. component_db.py — 组件定义（SVG+ports+z-order）
2. assembler.py — 拼装引擎（端口对齐+模板+高亮标注）
3. chem_components.py — 旧版化学组件渲染器（保留兼容）
4. phys_components.py — 旧版物理组件渲染器（保留兼容）

使用方式：
    from services.diagram_components import assemble, available_components, available_templates
    svg = assemble(comp_spec)
"""

from .assembler import assemble, available_templates, available_components
from .component_db import COMPONENT_DB, get_component, resolve_type


def render_spec(spec: dict) -> tuple[str, str]:
    """旧版兼容：按spec格式渲染为SVG，返回 (svg, error)"""
    if not spec:
        return "", "empty spec"

    try:
        svg = assemble(spec)
        return svg, ""
    except Exception as e:
        return "", str(e)
