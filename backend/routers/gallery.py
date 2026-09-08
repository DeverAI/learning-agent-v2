from fastapi import APIRouter, HTTPException
from services.diagram_components import available_components, available_templates, COMPONENT_DB
import json, os, time
from logger import get_logger
from pydantic import BaseModel, Field, field_validator
from config import _atomic_write_json, GALLERY_DIR
import uuid

router = APIRouter(prefix="/api/gallery", tags=["gallery"])

os.makedirs(GALLERY_DIR, exist_ok=True)

# 用于存图的JSON文件
GALLERY_FILE = os.path.join(GALLERY_DIR, "presets.json")

# 文件读写锁（asyncio.Lock 兼容异步端点，防止并发写入竞态）
import asyncio
_gallery_lock = asyncio.Lock()


def _load_presets() -> list[dict]:
    if os.path.exists(GALLERY_FILE):
        try:
            with open(GALLERY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            get_logger().warning("Gallery presets file is not a list, treating as empty")
        except Exception as e:
            get_logger().warning("Failed to load presets: %s", e)
    return []

def _save_presets(data: list):
    try:
        _atomic_write_json(GALLERY_FILE, data[-200:])
    except Exception as e:
        get_logger().error("Failed to save presets: %s", e)
        raise HTTPException(500, "保存预设图失败")


class PresetRequest(BaseModel):
    name: str = Field("未命名", min_length=1, max_length=100)
    spec: dict = Field(default_factory=dict)
    thumbnail: str = Field("", max_length=1_500_000)

    @field_validator("spec")
    @classmethod
    def validate_spec_size(cls, value: dict) -> dict:
        if len(json.dumps(value, ensure_ascii=False)) > 200_000:
            raise ValueError("预设图规格超过 200KB")
        return value


# 装饰器已内联到各端点函数中，使用 async with _gallery_lock:

@router.get("/components")
async def list_components():
    """列出所有可用组件"""
    return {"components": available_components(), "total": len(COMPONENT_DB)}

@router.get("/components/category/{category}")
async def list_components_by_category(category: str):
    """按分类列出组件 chem / phys / math"""
    return {"components": available_components(category)}

@router.get("/templates")
async def list_templates():
    """列出所有预设模板"""
    tpls = available_templates()
    return {"templates": tpls, "total": len(tpls)}

@router.get("/presets")
async def list_presets():
    """列出用户保存的自定义图"""
    async with _gallery_lock:
        return {"presets": _load_presets()}

@router.post("/presets")
async def save_preset(data: PresetRequest):
    """保存自定义图
    body: {"name": "我的蒸馏图", "spec": {...assembly spec...}, "thumbnail": "base64?"}
    """
    async with _gallery_lock:
        presets = _load_presets()
        entry = {
            "id": f"preset_{uuid.uuid4().hex[:16]}",
            "name": data.name.strip(),
            "spec": data.spec,
            "thumbnail": data.thumbnail,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        presets.append(entry)
        _save_presets(presets)
    return {"message": "已保存", "preset": entry}

@router.delete("/presets/{preset_id}")
async def delete_preset(preset_id: str):
    async with _gallery_lock:
        presets = _load_presets()
        filtered = [p for p in presets if p.get("id") != preset_id]
        if len(filtered) == len(presets):
            raise HTTPException(404, "预设图不存在")
        _save_presets(filtered)
    return {"message": "已删除"}

@router.get("/presets/{preset_id}")
async def get_preset(preset_id: str):
    async with _gallery_lock:
        presets = _load_presets()
        for p in presets:
            if p.get("id") == preset_id:
                return {"preset": p}
    raise HTTPException(404, "预设图不存在")

@router.get("/component/{ctype}")
async def get_component_detail(ctype: str):
    """获取单个组件的详细信息"""
    from services.diagram_components import get_component
    cdef = get_component(ctype)
    if not cdef:
        raise HTTPException(404, f"组件 {ctype} 不存在")
    # 渲染示例SVG
    try:
        from services.diagram_components.assembler import assemble
        svg = assemble({"components": [{"type": ctype, "x": 50, "y": 50, "label": cdef["name"]}]})
    except Exception as e:
        get_logger().warning("Failed to render sample SVG for %s: %s", ctype, e)
        svg = ""
    return {"component": cdef, "sample_svg": svg}
