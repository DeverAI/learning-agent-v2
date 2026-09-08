"""讲题 API（round 60）：试卷/题目清单 + 讲解计划（含整卷连讲）。

桌面端讲课引擎与 Web/安卓讲课页共用。鉴权沿用 password_guard（/api/*）。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import get_db
from pydantic import BaseModel, Field
from services import lecture_service

router = APIRouter(prefix="/api/lecture", tags=["lecture"])


@router.get("/papers")
async def lecture_papers(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """可讲解的试卷清单。"""
    return await lecture_service.list_paper_summaries(db, limit)


@router.get("/questions")
async def lecture_questions(limit: int = 50, db: AsyncSession = Depends(get_db)):
    """可讲解的已完成题目清单（无试卷时直接讲题）。"""
    return await lecture_service.list_question_summaries(db, limit)


@router.get("/paper/{paper_id}")
async def lecture_paper(paper_id: str, db: AsyncSession = Depends(get_db)):
    """试卷详情（题目顺序）。"""
    from models.models import Paper
    paper = await db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, detail="试卷不存在")
    order = paper.question_order or []
    return {"id": paper.id, "title": paper.title,
            "question_order": [{"id": (o.get("id") if isinstance(o, dict) else o),
                                "number": (o.get("number", i + 1) if isinstance(o, dict) else i + 1)}
                               for i, o in enumerate(order)]}


@router.post("/plan/{question_id}")
async def lecture_plan_question(question_id: str, db: AsyncSession = Depends(get_db)):
    """单题讲解计划。"""
    try:
        return await lecture_service.build_question_plan(db, question_id)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))


class PaperPlanRequest(BaseModel):
    paper_id: str = Field(min_length=1, max_length=64)


@router.post("/plan-paper")
async def lecture_plan_paper(req: PaperPlanRequest, db: AsyncSession = Depends(get_db)):
    """整卷连讲计划：逐题生成，跨题摘要压缩传递（长卷不超限）。"""
    try:
        return await lecture_service.build_paper_plans(db, req.paper_id)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))
