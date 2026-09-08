import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from services.config_service import (
    config_service, get_available_widgets, get_default_home_layout
)
from logger import get_logger

logger = get_logger()

router = APIRouter(prefix="/api", tags=["configs"])

_CONFIG_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _is_valid_config_id(cid: str) -> bool:
    return bool(cid and isinstance(cid, str) and _CONFIG_ID_RE.match(cid))


# ============== paper configs ==============

class PaperConfigSaveRequest(BaseModel):
    name: str = Field(default="", max_length=200)
    data: dict = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def validate_payload_size(cls, value: dict) -> dict:
        import json
        if len(json.dumps(value, ensure_ascii=False, default=str)) > 100_000:
            raise ValueError("组卷配置不能超过 100KB")
        return value


class PaperConfigListResponse(BaseModel):
    configs: list[dict]


@router.get("/paper-configs")
async def list_paper_configs(limit: int = 20, offset: int = 0):
    """列出已保存的组卷参数（数据库持久化）。"""
    configs = await config_service.list_paper_configs(
        limit=max(1, min(limit, 100)),
        offset=min(max(0, offset), 1_000_000),
    )
    return {"configs": configs}


@router.post("/paper-configs")
async def save_paper_config(req: PaperConfigSaveRequest):
    """保存组卷参数，返回配置 ID。"""
    try:
        cid = await config_service.save_paper_config(req.data or {}, req.name or "")
        return {"id": cid, "message": "配置已保存"}
    except Exception as e:
        logger.warning("save_paper_config failed: %s", e)
        raise HTTPException(status_code=500, detail="保存失败，请稍后重试")


@router.get("/paper-configs/{config_id}")
async def get_paper_config(config_id: str):
    """读取指定组卷参数。"""
    if not _is_valid_config_id(config_id):
        raise HTTPException(status_code=400, detail="非法的配置 ID")
    data = await config_service.load_paper_config(config_id)
    if data is None:
        raise HTTPException(status_code=404, detail="配置不存在或已过期")
    return {"id": config_id, "data": data}


@router.delete("/paper-configs/{config_id}")
async def delete_paper_config(config_id: str):
    """删除指定组卷参数。"""
    if not _is_valid_config_id(config_id):
        raise HTTPException(status_code=400, detail="非法的配置 ID")
    ok = await config_service.delete_paper_config(config_id)
    if not ok:
        raise HTTPException(status_code=404, detail="配置不存在")
    return {"message": "已删除"}


# ============== home widgets ==============

class HomeLayoutSaveRequest(BaseModel):
    layout: list[str] = Field(default_factory=list, max_length=50)


@router.get("/home-widgets")
async def list_home_widgets():
    """返回所有可用 widget 与当前保存的布局。"""
    widgets = get_available_widgets()
    layout = await config_service.load_home_layout()
    return {"widgets": widgets, "layout": layout}


@router.get("/home-widgets/layout")
async def get_home_layout():
    """返回当前首页 widget 布局。"""
    layout = await config_service.load_home_layout()
    return {"layout": layout}


@router.post("/home-widgets")
async def save_home_layout(req: HomeLayoutSaveRequest):
    """保存首页 widget 布局。"""
    layout = req.layout if isinstance(req.layout, list) else []
    try:
        cid = await config_service.save_home_layout(layout)
        return {"id": cid, "layout": await config_service.load_home_layout(), "message": "布局已保存"}
    except Exception as e:
        logger.warning("save_home_layout failed: %s", e)
        raise HTTPException(status_code=500, detail="保存失败，请稍后重试")
