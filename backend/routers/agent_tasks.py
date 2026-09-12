"""后台任务端点（双端架构 §9 接口设计）。

前端据此做"双线展示"：前台对话照常进行，后台任务以卡片形式列出、可取消、可续跑。
"""

from fastapi import APIRouter, HTTPException

from logger import get_logger
from services import background_agent as BA

router = APIRouter(prefix="/api/agent-tasks", tags=["agent-tasks"])
logger = get_logger()


def _wrap(coro_result: dict) -> dict:
    return coro_result


@router.get("")
async def list_agent_tasks(sid: str = "", status: str = "", limit: int = 50, offset: int = 0):
    return await BA.list_tasks(sid=sid, status=status, limit=limit, offset=offset)


@router.get("/{task_id}")
async def get_agent_task(task_id: str):
    try:
        return await BA.get_task(task_id)
    except BA.TaskNotFound:
        raise HTTPException(404, "任务不存在")


@router.post("/{task_id}/cancel")
async def cancel_agent_task(task_id: str):
    try:
        return await BA.request_cancel(task_id)
    except BA.TaskNotFound:
        raise HTTPException(404, "任务不存在")


@router.post("/{task_id}/resume")
async def resume_agent_task(task_id: str):
    try:
        return await BA.resume_task(task_id)
    except BA.TaskNotFound:
        raise HTTPException(404, "任务不存在")
