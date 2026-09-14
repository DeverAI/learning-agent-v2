# -*- coding: utf-8 -*-
"""问题反馈 API：学生/桌面/安卓提交问题，运维巡检读取。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import get_db
from models.models import Feedback

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class FeedbackCreate(BaseModel):
    kind: str = Field(default="bug", max_length=16)
    page: str = Field(default="", max_length=128)
    title: str = Field(default="", max_length=200)
    content: str = Field(..., min_length=1, max_length=5000)
    contact: str = Field(default="", max_length=64)
    client: str = Field(default="web", max_length=16)


class FeedbackPatch(BaseModel):
    status: str = Field(..., pattern="^(open|doing|done|rejected)$")


@router.post("")
async def create_feedback(req: FeedbackCreate, db: AsyncSession = Depends(get_db)):
    kind = req.kind if req.kind in ("bug", "idea", "question", "other") else "bug"
    client = req.client if req.client in ("web", "android", "desktop") else "web"
    f = Feedback(
        kind=kind,
        page=(req.page or "")[:128],
        title=(req.title or "").strip()[:200] or req.content[:40],
        content=req.content.strip(),
        contact=(req.contact or "")[:64],
        client=client,
        status="open",
    )
    db.add(f)
    await db.commit()
    return {"id": f.id, "message": "已收到反馈，谢谢"}


@router.get("")
async def list_feedback(status: str = "", limit: int = 50, db: AsyncSession = Depends(get_db)):
    limit = max(1, min(int(limit or 50), 200))
    q = select(Feedback).order_by(desc(Feedback.created_at)).limit(limit)
    if status:
        q = select(Feedback).where(Feedback.status == status).order_by(
            desc(Feedback.created_at)).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    total = (await db.execute(select(func.count(Feedback.id)))).scalar() or 0
    return {
        "total": total,
        "items": [
            {
                "id": f.id, "kind": f.kind, "page": f.page, "title": f.title,
                "content": f.content[:500], "client": f.client, "status": f.status,
                "created_at": f.created_at.strftime("%Y-%m-%d %H:%M") if f.created_at else "",
            }
            for f in rows
        ],
    }


@router.patch("/{fid}")
async def patch_feedback(fid: str, req: FeedbackPatch, db: AsyncSession = Depends(get_db)):
    f = await db.get(Feedback, fid)
    if not f:
        raise HTTPException(404, "反馈不存在")
    f.status = req.status
    await db.commit()
    return {"message": "已更新", "status": f.status}
