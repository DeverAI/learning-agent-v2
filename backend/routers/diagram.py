import json
import math
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

from logger import get_logger, log_error
from services.diagram_service import _is_valid_question_id as _is_valid_diagram_qid, diagram_service

logger = get_logger()
router = APIRouter()


def _validate_json_size(value, *, limit: int = 500_000):
    if len(json.dumps(value, ensure_ascii=False, default=str)) > limit:
        raise ValueError(f"请求结构不能超过 {limit // 1000}KB")
    return value


class DiagramGenerateRequest(BaseModel):
    question_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=20_000)
    index: int = Field(default=0, ge=0, le=9_999)
    spec_override: dict | None = None

    @field_validator("spec_override")
    @classmethod
    def validate_spec_override(cls, value):
        return _validate_json_size(value, limit=500_000) if value is not None else value


class DiagramInsertRequest(BaseModel):
    question_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=20_000)
    index: int = Field(default=0, ge=0, le=9_999)


class ComponentsRequest(BaseModel):
    components: list[dict] = Field(default_factory=list, max_length=300)

    @field_validator("components")
    @classmethod
    def validate_components(cls, value):
        return _validate_json_size(value, limit=500_000)


class DiagramSpecRequest(BaseModel):
    spec: dict = Field(default_factory=dict)

    @field_validator("spec")
    @classmethod
    def validate_spec(cls, value):
        return _validate_json_size(value, limit=500_000)


class ComponentRenderRequest(BaseModel):
    type: str = Field(min_length=1, max_length=100)
    w: float | None = Field(default=None, ge=1, le=2_000)
    h: float | None = Field(default=None, ge=1, le=2_000)
    label: str = Field(default="", max_length=200)
    liquid: Any = None
    filled: Any = None
    water_level: Any = None
    angle: Any = None
    clamp_y: Any = None
    clamp_w: Any = None
    a: Any = None
    b: Any = None
    c: Any = None
    k: Any = None
    temperature: Any = None
    measure_type: Any = None
    min: Any = None
    max: Any = None
    value: Any = None
    position: Any = None
    direction: Any = None
    has_ring: bool | int | str | None = None
    has_clamp: bool | int | str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_request_size(self):
        _validate_json_size(self.model_dump(), limit=100_000)
        return self


class SemanticMatchRequest(BaseModel):
    component_types: list[str] = Field(default_factory=list, max_length=300)

    @field_validator("component_types")
    @classmethod
    def validate_types(cls, value):
        if any(len(str(item)) > 100 for item in value):
            raise ValueError("组件类型名称过长")
        return value


class SemanticResolveRequest(BaseModel):
    scene_id: str = Field(default="", max_length=100)
    config_label: str = Field(default="", max_length=200)
    components: list[dict] = Field(default_factory=list, max_length=300)

    @field_validator("components")
    @classmethod
    def validate_components(cls, value):
        return _validate_json_size(value, limit=500_000)


class ConnectionSafetyRequest(BaseModel):
    src_type: str = Field(min_length=1, max_length=100)
    dst_type: str = Field(min_length=1, max_length=100)


async def _ensure_diagram_question(question_id: str) -> None:
    from models.database import async_session
    from models.models import Question
    async with async_session() as db:
        q = await db.get(Question, question_id)
        if not q:
            raise HTTPException(404, "题目不存在")
        # 锁定守卫（FreqErr [锁定守卫缺失]）：示意图与题目内容同类，
        # 与 questions.py 结构图端点对齐，已锁定的题目一律拒绝写入
        if getattr(q, "is_resolved", False):
            raise HTTPException(403, "已锁定的题目不允许修改示意图")


@router.post("/api/diagram/generate")
async def api_generate_diagram(data: DiagramGenerateRequest):
    """生成新图。若请求包含 spec_override（来自编辑器手动摆放），优先按该 spec 渲染保存。"""
    from services.diagram_service import diagram_service
    question_id = data.question_id.strip()
    prompt = data.prompt.strip()
    index = data.index
    spec_override = data.spec_override
    if not _is_valid_diagram_qid(question_id):
        raise HTTPException(400, f"非法的 question_id: {question_id}")
    await _ensure_diagram_question(question_id)
    try:
        path = await diagram_service.generate_diagram(question_id, prompt, index, spec_override=spec_override)
    except Exception as exc:
        log_error("diagram.generate", f"AI diagram generation failed for {question_id}: {exc}")
        _msg = str(exc).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(503, "AI 服务认证失败，请检查模型 API Key 配置")
        raise HTTPException(502, "AI 生成图失败，请稍后重试")
    if path:
        return {"path": path}
    raise HTTPException(502, "生成图失败")


@router.post("/api/diagram/insert")
async def api_insert_diagram(data: DiagramInsertRequest):
    """在已有图上插入新组件"""
    from services.diagram_service import diagram_service
    question_id = data.question_id.strip()
    prompt = data.prompt.strip()
    index = data.index
    if not _is_valid_diagram_qid(question_id):
        raise HTTPException(400, f"非法的 question_id: {question_id}")
    await _ensure_diagram_question(question_id)
    try:
        path = await diagram_service.insert_diagram(question_id, prompt, index)
    except Exception as exc:
        log_error("diagram.insert", f"AI diagram insertion failed for {question_id}: {exc}")
        _msg = str(exc).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(503, "AI 服务认证失败，请检查模型 API Key 配置")
        raise HTTPException(502, "AI 插入图失败，请稍后重试")
    if path:
        return {"path": path}
    raise HTTPException(502, "插入图失败")


@router.get("/api/diagram/check/{question_id}/{index}")
async def api_check_diagram_freshness(question_id: str, index: int):
    """检查图是否最新（时间戳校验）"""
    from services.diagram_service import diagram_service
    if not _is_valid_diagram_qid(question_id):
        raise HTTPException(400, f"非法的 question_id: {question_id}")
    if index < 0 or index > 9_999:
        raise HTTPException(400, "非法的图索引")
    fresh = await diagram_service.check_diagram_freshness(question_id, index)
    # R23：同一端点顺带返回质量自检结果（附加字段，旧前端只读 fresh 不受影响）。
    # 挂在这里而不是新开端点：调用方拿到的就是「这张图现在到底行不行」。
    issues = diagram_service.validate_diagram(question_id, index)
    return {"fresh": fresh, "quality_ok": not issues, "quality_issues": issues}


@router.get("/api/diagram/spec/{question_id}/{index}")
async def api_get_diagram_spec(question_id: str, index: int):
    """返回已保存示意图的元件 spec 与新鲜度（供编辑器重新加载编辑）。

    spec 仅来自服务端 _write_svg 落盘的 diagram_{index}.spec.json；
    路径由 question_id 白名单与 QUESTIONS_DIR 边界双重校验。
    """
    from services.diagram_service import diagram_service
    if not _is_valid_diagram_qid(question_id):
        raise HTTPException(400, f"非法的 question_id: {question_id}")
    if index < 0 or index > 9_999:
        raise HTTPException(400, "非法的图索引")
    spec, fresh = await diagram_service.get_diagram_spec(question_id, index)
    if spec is None:
        raise HTTPException(404, "该题目没有此索引的可编辑示意图")
    return {"spec": spec, "fresh": fresh}


@router.post("/api/diagram/calibrate")
async def api_calibrate(data: ComponentsRequest):
    """记录一次手动调整样本，返回校准结果"""
    from services.calibration_service import record_adjustment
    components = data.components
    if not components:
        raise HTTPException(400, "需要 components 列表")
    try:
        result = record_adjustment(components)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result


@router.post("/api/diagram/calibration-check")
async def api_calibration_check(data: ComponentsRequest):
    """查询某组合的校准状态"""
    from services.calibration_service import get_calibration
    components = data.components
    return get_calibration(components)


@router.post("/api/diagram/render-svg")
async def api_render_svg(data: DiagramSpecRequest):
    """根据spec实时渲染SVG（用于编辑器导出真实SVG）"""
    from services.diagram_components import assemble
    from services.diagram_components.assembler import normalize_spec
    try:
        spec, warnings = normalize_spec(data.spec)
        svg = diagram_service._sanitize_svg(assemble(spec))
        return {"svg": svg, "warnings": warnings, "component_count": len(spec.get("components", []))}
    except ValueError as e:
        raise HTTPException(400, f"SVG spec 无效: {str(e)}")
    except Exception as e:
        logger.exception("SVG render failed")
        raise HTTPException(500, "SVG渲染失败，请稍后重试")


@router.post("/api/gallery/render-previews")
async def api_render_previews():
    """批量渲染所有组件的真实SVG预览（不含坐标偏移，供编辑器画布直接嵌入）"""
    from services.diagram_components.assembler import _render_component, get_component
    from services.diagram_components import COMPONENT_DB
    previews = {}
    for ctype, cdef in COMPONENT_DB.items():
        comp = {
            "type": ctype,
            "x": 0, "y": 0,
            "w": cdef["default_w"],
            "h": cdef["default_h"],
            "label": "",
        }
        try:
            svg_fragment = _render_component(comp)
            if svg_fragment:
                # Wrap in proper SVG so frontend can innerHTML it
                w, h = cdef["default_w"], cdef["default_h"]
                previews[ctype] = diagram_service._sanitize_svg(
                    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
                    f'{svg_fragment}</svg>'
                )
        except Exception as e:
            logger.warning("Gallery preview render failed for component %s: %s", ctype, e)
    return {"previews": previews}


@router.post("/api/gallery/render-component")
async def api_render_single_component(request_data: ComponentRenderRequest):
    """渲染单个组件（可带实例级渲染参数，供编辑器摆法预览刷新）

    请求: {"type": "iron_stand", "w": 40, "h": 80, "has_ring": true, "clamp_y": 42, ...}
    返回: {"svg": "<svg ...>fragment</svg>"}
    """
    from services.diagram_components.assembler import _render_component, get_component
    data = request_data.model_dump(exclude_none=True)
    raw_type = data.get("type")
    ctype = raw_type.strip() if isinstance(raw_type, str) else ""
    cdef = get_component(ctype) if ctype else None
    if not cdef:
        raise HTTPException(400, f"未知组件类型: {ctype or '(空)'}")
    try:
        w = float(data.get("w") or cdef["default_w"])
        h = float(data.get("h") or cdef["default_h"])
    except (TypeError, ValueError):
        w, h = cdef["default_w"], cdef["default_h"]
    w = max(1.0, min(2000.0, w))
    h = max(1.0, min(2000.0, h))
    # 透传实例级渲染参数（白名单，与 _render_component 合并键一致）
    comp = {"type": ctype, "x": 0, "y": 0, "w": w, "h": h, "label": data.get("label", "")}
    for k in ("liquid", "filled", "water_level", "angle", "clamp_y", "clamp_w",
              "a", "b", "c", "k", "temperature", "measure_type", "min", "max",
              "value", "position", "direction"):
        if k in data:
            comp[k] = data[k]
    # 数值键统一归一：字符串/非有限值直接流入渲染函数会触发 TypeError/ZeroDivisionError → 500
    def _coerce_num(value, default=0.0) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(v):
            return default
        return v

    for k in ("temperature", "min", "max", "value", "clamp_y", "clamp_w", "a", "b", "c", "k"):
        if k in comp:
            comp[k] = _coerce_num(comp.get(k), 0)
    if "liquid" in comp:
        comp["liquid"] = max(0.0, min(1.0, _coerce_num(comp.get("liquid"), 0)))
    if "water_level" in comp:
        comp["water_level"] = max(0.0, min(1.0, _coerce_num(comp.get("water_level"), 0)))
    if "angle" in comp:
        comp["angle"] = _coerce_num(comp.get("angle"), 0) % 360
    # 布尔键显式归一（防字符串 "false" 被当作真值）
    for k in ("has_ring", "has_clamp", "filled"):
        if k in comp:
            comp[k] = comp[k] in (True, 1, "1", "true", "True")
    fragment = _render_component(comp)
    if not fragment:
        raise HTTPException(500, f"组件 {ctype} 渲染失败")
    return {"svg": diagram_service._sanitize_svg(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">{fragment}</svg>'
    )}


@router.get("/api/diagram/calibration-summary")
async def api_calibration_summary():
    """获取所有组合的校准摘要"""
    from services.calibration_service import get_calibration_summary
    return get_calibration_summary()


# ═══ 语义连接引擎 API ═══

@router.get("/api/diagram/semantic-scenes")
async def api_list_semantic_scenes():
    """列出所有可用语义场景"""
    from services.diagram_components.semantic_rules import list_all_scenes
    return {"scenes": list_all_scenes()}


@router.post("/api/diagram/semantic-match")
async def api_match_semantic_scene(data: SemanticMatchRequest):
    """根据已放置的组件集合匹配适用语义场景

    请求: {"component_types": ["beaker", "alcohol_lamp", "tripod", ...]}
    返回: 匹配的场景列表（含configs供选择）
    """
    from services.diagram_components.semantic_rules import match_scene
    types = set(data.component_types)
    if not types:
        return {"scenes": []}
    scenes = match_scene(types)
    return {"scenes": scenes}


@router.post("/api/diagram/semantic-resolve")
async def api_resolve_semantic_scene(data: SemanticResolveRequest):
    """解析特定场景配置，返回端口绑定

    请求: {"scene_id": "heating_beaker", "config_label": "烧杯加热...",
           "components": [{"type":"beaker",...}]}
    返回: {"label":"...", "port_bindings":[...], "params":{...}, "valid":true}
    """
    from services.diagram_components.semantic_rules import resolve_port_bindings
    scene_id = data.scene_id.strip()
    config_label = data.config_label.strip()
    components = data.components
    result = resolve_port_bindings(scene_id, config_label, components)
    return result


@router.get("/api/diagram/semantic-search")
async def api_search_semantic_scenes(q: str = Query(default="", max_length=200)):
    """按关键词搜索语义场景（供AI使用）"""
    from services.diagram_components.semantic_rules import get_scene_by_keyword
    if not q:
        return {"scenes": []}
    return {"scenes": get_scene_by_keyword(q)}


@router.post("/api/diagram/check-safety")
async def api_check_connection_safety(data: ConnectionSafetyRequest):
    """检查连接是否安全

    请求: {"src_type": "alcohol_lamp", "dst_type": "beaker"}
    返回: {"safe": false, "reason": "酒精灯不可直接加热烧杯，需通过三脚架+石棉网"}
    """
    from services.diagram_components.semantic_rules import check_connection_safety
    src = data.src_type.strip()
    dst = data.dst_type.strip()
    return check_connection_safety(src, dst)


# ═══ 液面系统 API ═══

@router.get("/api/diagram/liquid-containers")
async def api_liquid_containers():
    """返回支持液面的容器类型列表"""
    from services.diagram_components.liquid_render import LIQUID_CONTAINERS, LIQUID_PRESETS
    return {"containers": list(LIQUID_CONTAINERS.keys()), "presets": list(LIQUID_PRESETS.keys())}
