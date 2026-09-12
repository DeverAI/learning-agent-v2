"""课稿（备课产物）端点。

需求映射：
- 「产出可编辑课稿」-> `PATCH /{id}/sections/{index}`
- 「任何需要讲解相关内容的 AI 都可以查看、切片、读取」-> `GET /{id}` 的 slice 参数
  （同一个契约 `lesson_service.slice_lesson` 也供 Agent 工具使用，避免出现两份读法）
- 「人决定怎么走」-> `POST /{id}/status`（draft/final 由人确认）
- 「服务器长期存储，用户不删就不删」-> 无 TTL、无自动清理，只有显式 DELETE
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from logger import get_logger
from services import lesson_service as LS

router = APIRouter(prefix="/api/lessons", tags=["lessons"])
logger = get_logger()


class SectionPatch(BaseModel):
    heading: Optional[str] = Field(default=None, max_length=200)
    script: Optional[str] = Field(default=None, max_length=200000)
    question_ids: Optional[list[str]] = None


class StatusPatch(BaseModel):
    status: str = Field(pattern="^(draft|final)$")


class LessonCreate(BaseModel):
    topic: str = ""
    subject: str = ""
    grade: str = ""
    title: str = ""
    question_ids: list[str] = []
    paper_id: str = ""
    section_count: int = Field(default=LS.DEFAULT_SECTION_COUNT, ge=2, le=LS.MAX_SECTION_COUNT)
    # True 时立刻起后台任务生成；False 时只建空骨架
    generate: bool = False


@router.get("")
async def list_lessons(limit: int = 50, offset: int = 0, status: str = ""):
    return await LS.list_lessons(limit=limit, offset=offset, status=status)


@router.get("/{lesson_id}")
async def get_lesson(lesson_id: str, section_index: Optional[int] = None,
                     offset: int = 0, max_chars: int = 6000,
                     include_empty: bool = True):
    """读课稿（**带切片**）。

    返回体里 `truncated` / `total_sections` / `used_chars` 必须一起看 ——
    `truncated=True` 表示还有没返回的内容，不是"课稿就这么点"。
    """
    try:
        return await LS.slice_lesson(lesson_id, section_index=section_index,
                                     offset=offset, max_chars=max_chars,
                                     include_empty=include_empty)
    except LS.LessonNotFound:
        raise HTTPException(404, "课稿不存在")


@router.post("")
async def create_lesson(data: LessonCreate):
    params = {
        "topic": data.topic, "subject": data.subject, "grade": data.grade,
        "question_ids": list(data.question_ids or []), "paper_id": data.paper_id,
    }
    brief = await LS.create_lesson(params, title=data.title)
    if data.generate:
        task = await _spawn_generation(brief["lesson_id"], data.section_count)
        return {**brief, "task": task}
    return brief


@router.post("/{lesson_id}/generate")
async def generate_lesson(lesson_id: str, section_count: int = LS.DEFAULT_SECTION_COUNT):
    """对已有课稿（含被取消/失败的）起一次生成/续写后台任务。"""
    try:
        await LS.slice_lesson(lesson_id, max_chars=200)
    except LS.LessonNotFound:
        raise HTTPException(404, "课稿不存在")
    task = await _spawn_generation(lesson_id, section_count)
    return {"lesson_id": lesson_id, "task": task}


@router.patch("/{lesson_id}/sections/{index}")
async def patch_section(lesson_id: str, index: int, data: SectionPatch):
    try:
        return await LS.update_section(lesson_id, index, heading=data.heading,
                                      script=data.script, question_ids=data.question_ids)
    except LS.LessonNotFound:
        raise HTTPException(404, "课稿或分片不存在")


@router.post("/{lesson_id}/status")
async def patch_status(lesson_id: str, data: StatusPatch):
    try:
        return await LS.set_status(lesson_id, data.status)
    except LS.LessonNotFound:
        raise HTTPException(404, "课稿不存在")


@router.delete("/{lesson_id}")
async def delete_lesson(lesson_id: str):
    ok = await LS.delete_lesson(lesson_id)
    if not ok:
        raise HTTPException(404, "课稿不存在")
    return {"message": "已删除"}


async def _spawn_generation(lesson_id: str, section_count: int) -> dict:
    """起后台任务填课稿。返回任务摘要（前端据此立刻显示卡片，不必等生成完）。"""
    from services import background_agent as BA
    task = await BA.create_task(
        sid="", tool="prepare_lesson",
        title=f"备课：{(await LS.slice_lesson(lesson_id, max_chars=1))['title']}",
        params={"lesson_id": lesson_id, "section_count": section_count},
    )
    BA.spawn(task["task_id"])
    return task
