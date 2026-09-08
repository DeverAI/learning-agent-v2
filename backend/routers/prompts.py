from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.database import get_db
from models.models import PromptTemplate, gen_id
from schemas.schemas import PromptTemplateCreate, PromptTemplateUpdate, PromptTemplateResponse
from config import DEFAULT_PROMPT_TEMPLATES

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.get("", response_model=list[PromptTemplateResponse])
async def list_prompts(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PromptTemplate).order_by(PromptTemplate.created_at.desc())
    )
    return result.scalars().all()


@router.get("/{template_id}", response_model=PromptTemplateResponse)
async def get_prompt(template_id: str, db: AsyncSession = Depends(get_db)):
    tmpl = await db.get(PromptTemplate, template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    return tmpl


@router.post("", response_model=PromptTemplateResponse)
async def create_prompt(
    data: PromptTemplateCreate,
    db: AsyncSession = Depends(get_db)
):
    tmpl = PromptTemplate(
        id=gen_id(),
        name=data.name,
        type=data.type,
        content=data.content,
        blocks=data.blocks,
        is_default=False,
    )
    db.add(tmpl)
    await db.commit()
    await db.refresh(tmpl)
    return tmpl


@router.put("/{template_id}", response_model=PromptTemplateResponse)
async def update_prompt(
    template_id: str,
    data: PromptTemplateUpdate,
    db: AsyncSession = Depends(get_db)
):
    tmpl = await db.get(PromptTemplate, template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="模板不存在")

    # 内容确实被修改（含显式清空）才取消默认标记；
    # 空 content 覆盖到默认模板会破坏默认内容，这里拒绝清空默认模板正文
    if tmpl.is_default and data.content is not None:
        if not (data.content or "").strip():
            raise HTTPException(status_code=400, detail="默认模板的内容不能清空，可在「恢复默认」后删除或修改")
        tmpl.is_default = False

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(tmpl, key, value)

    await db.commit()
    await db.refresh(tmpl)
    return tmpl


@router.delete("/{template_id}")
async def delete_prompt(template_id: str, db: AsyncSession = Depends(get_db)):
    tmpl = await db.get(PromptTemplate, template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="模板不存在")
    if tmpl.is_default:
        raise HTTPException(status_code=400, detail="系统默认模板不可删除")
    await db.delete(tmpl)
    await db.commit()
    return {"message": "已删除"}


@router.post("/{template_id}/reset", response_model=PromptTemplateResponse)
async def reset_prompt(template_id: str, db: AsyncSession = Depends(get_db)):
    default = DEFAULT_PROMPT_TEMPLATES.get(template_id)
    if not default:
        raise HTTPException(status_code=404, detail="该模板没有默认版本")

    tmpl = await db.get(PromptTemplate, template_id)
    if tmpl:
        tmpl.content = default["content"]
        tmpl.blocks = default.get("blocks", [])
        tmpl.is_default = True
    else:
        tmpl = PromptTemplate(
            id=template_id,
            name=default["name"],
            type=default["type"],
            content=default["content"],
            blocks=default.get("blocks", []),
            is_default=True,
        )
        db.add(tmpl)

    await db.commit()
    await db.refresh(tmpl)
    return tmpl


@router.post("/init-defaults")
async def init_default_prompts(db: AsyncSession = Depends(get_db)):
    created = []
    for tid, data in DEFAULT_PROMPT_TEMPLATES.items():
        existing = await db.get(PromptTemplate, tid)
        if not existing:
            tmpl = PromptTemplate(
                id=tid,
                name=data["name"],
                type=data["type"],
                content=data["content"],
                blocks=data.get("blocks", []),
                is_default=True,
            )
            db.add(tmpl)
            created.append(tid)

    await db.commit()
    return {"message": f"已初始化 {len(created)} 个默认模板", "created": created}
