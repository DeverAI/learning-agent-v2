import os
import re
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional
from config import ENABLE_FOCUS_MODE, ENABLE_FOCUS_BLACKBOARD
from logger import get_logger
from services.focus_service import (
    start_session, submit_checkpoint, get_session_state,
    pause_session, resume_session, end_session, list_sessions,
    save_board_snapshot, board_asset_path,
)
from services.hippocampus_service import (
    get_all_memories, get_topic_memory, get_meta, update_meta,
    delete_topic, run_decay_cycle,
)

logger = get_logger()
router = APIRouter(tags=["focus"])

_FOCUS_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _map_focus_error(context: str, e: Exception, user_msg: str) -> HTTPException:
    """专注模式端点统一异常出口：原文进日志，客户端只见稳定文案；
    AI 依赖失败按 502/503 区分（FreqErr [错误详情泄漏]/[AI 依赖错误伪装 500]）。"""
    from logger import log_error
    log_error(context, str(e))
    msg = str(e).lower()
    if "401" in msg or "authentication" in msg or "api key" in msg or "illegal header" in msg:
        return HTTPException(503, "AI 服务认证失败，请检查模型 API Key 配置")
    if "timeout" in msg or "timed out" in msg or "connect" in msg or "unreachable" in msg:
        return HTTPException(502, "AI 服务暂不可用，请稍后重试")
    return HTTPException(500, user_msg)


def _validate_session_id(sid: str):
    if not isinstance(sid, str) or not _FOCUS_ID_RE.fullmatch(sid):
        raise HTTPException(400, "会话 ID 无效")


# ========== 请求模型 ==========

class FocusStartRequest(BaseModel):
    mode: str = Field(default="topic", pattern="^(topic|question|mixed)$")
    topic: str = Field(default="", max_length=200)
    question_id: str = Field(default="", max_length=64)


class CheckpointRequest(BaseModel):
    voice_text: str = Field(default="", max_length=5000)
    emotion_report: Optional[dict] = Field(default=None)
    # 必须允许 None：前端 captureFace() 在"无摄像头 API / getUserMedia 抛错 / 5s 超时 /
    # 视频加载失败"时返回 null（static/js/focus.js:571/611），并直接赋给 webcam_image
    # 再 JSON 序列化发出（:639/:649）。原声明是非 Optional 的 str，收到 null 会 422 →
    # "摄像头不可用不影响提交"这一降级承诺反而变成**恒定提交失败**，学生无路可走。
    webcam_image: Optional[str] = Field(default="", max_length=3_000_000)  # Base64 ~2MB
    voice_features: Optional[dict] = Field(default=None)
    segment_index: Optional[int] = Field(default=None)  # 响应的讲解段序号（幂等防护）


class MetaUpdateRequest(BaseModel):
    baseline_understanding: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    baseline_memory: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    baseline_focus: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    preferred_style: Optional[str] = Field(default=None, max_length=50)
    best_study_time: Optional[str] = Field(default=None, max_length=50)


class BoardSnapshotRequest(BaseModel):
    label: str = Field(default="", max_length=24)


def _check_blackboard():
    """黑板模块开关检查"""
    if not ENABLE_FOCUS_BLACKBOARD:
        raise HTTPException(404, "黑板功能未启用")


# ========== 前置检查：模块开关 ==========

def _check_focus_module():
    """检查专注模式模块开关"""
    if not ENABLE_FOCUS_MODE:
        raise HTTPException(404, "专注模式未启用")


# ========== 专注模式 API ==========

@router.post("/api/focus/start")
async def api_focus_start(req: FocusStartRequest):
    """启动专注模式会话"""
    _check_focus_module()
    if req.mode == "topic" and not req.topic.strip():
        raise HTTPException(400, "主题模式需要提供 topic")
    if req.mode == "question" and not req.question_id.strip():
        raise HTTPException(400, "题目模式需要提供 question_id")

    try:
        session = await start_session(
            mode=req.mode,
            topic=req.topic.strip(),
            question_id=req.question_id.strip(),
        )
        return {"ok": True, "session": session}
    except Exception as e:
        raise _map_focus_error("focus_start", e, "启动会话失败，请查看 Err.log") from e


@router.post("/api/focus/{session_id}/checkpoint")
async def api_focus_checkpoint(session_id: str, req: CheckpointRequest):
    """提交检查点响应"""
    _validate_session_id(session_id)
    _check_focus_module()

    try:
        result = await submit_checkpoint(
            session_id=session_id,
            voice_text=req.voice_text,
            emotion_report=req.emotion_report,
            webcam_image=req.webcam_image or "",  # None 归一化为空串，供 analyze_face 走降级路径
            voice_features=req.voice_features,
            segment_index=req.segment_index,
        )
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _map_focus_error("focus_checkpoint", e, "提交检查点失败，请查看 Err.log") from e


@router.get("/api/focus/{session_id}/state")
async def api_focus_state(session_id: str):
    """获取会话状态"""
    _validate_session_id(session_id)
    _check_focus_module()

    try:
        state = await get_session_state(session_id)
        return {"ok": True, "session": state}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise _map_focus_error("focus_state", e, "获取状态失败，请查看 Err.log") from e


@router.post("/api/focus/{session_id}/pause")
async def api_focus_pause(session_id: str):
    """暂停会话"""
    _validate_session_id(session_id)
    _check_focus_module()

    try:
        session = await pause_session(session_id)
        return {"ok": True, "session": session}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _map_focus_error("focus_pause", e, "暂停失败，请查看 Err.log") from e


@router.post("/api/focus/{session_id}/resume")
async def api_focus_resume(session_id: str):
    """恢复会话"""
    _validate_session_id(session_id)
    _check_focus_module()

    try:
        session = await resume_session(session_id)
        return {"ok": True, "session": session}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _map_focus_error("focus_resume", e, "恢复失败，请查看 Err.log") from e


@router.post("/api/focus/{session_id}/end")
async def api_focus_end(session_id: str):
    """结束会话"""
    _validate_session_id(session_id)
    _check_focus_module()

    try:
        session = await end_session(session_id)
        return {"ok": True, "session": session}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise _map_focus_error("focus_end", e, "结束失败，请查看 Err.log") from e


@router.get("/api/focus/history")
async def api_focus_history(limit: int = 20):
    """获取历史会话列表"""
    _check_focus_module()

    limit = max(1, min(limit, 100))
    sessions = list_sessions(limit=limit)
    return {"ok": True, "sessions": sessions}


# ========== 黑板板书 API ==========

@router.post("/api/focus/{session_id}/board/snapshot")
async def api_focus_board_snapshot(session_id: str, req: BoardSnapshotRequest):
    """学生手动保存当前板书页快照（回看引用）"""
    _check_focus_module()
    _check_blackboard()
    _validate_session_id(session_id)
    try:
        result = await save_board_snapshot(session_id, req.label)
        return {"ok": True, **result}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error("focus_board_snapshot failed: %s", str(e)[:200])
        raise HTTPException(500, "保存板书快照失败")


@router.get("/api/focus/{session_id}/board/asset/{name}")
async def api_focus_board_asset(session_id: str, name: str):
    """受控读取板书 SVG（白名单文件名 + 路径边界校验）"""
    _check_focus_module()
    _check_blackboard()
    _validate_session_id(session_id)
    try:
        path = board_asset_path(session_id, name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not os.path.isfile(path):
        raise HTTPException(404, "板书资源不存在")
    # F7 修复：资产后缀白名单同时允许 `.svg` 与 `.jpg/.png/.jpeg`（"引用原题图"走后者），
    # 而原实现对所有资产统一声明 `image/svg+xml`，并叠加 `nosniff` → JPEG/PNG 字节被当作
    # SVG 处理，浏览器大概率拒绘，于是"引用原题照片"这条路径实际不可用。
    # 改为按真实后缀给出 media type；未知后缀退回通用二进制。
    _asset_media = {
        ".svg": "image/svg+xml",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
    }.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
    resp = FileResponse(path, media_type=_asset_media)
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ========== 海马体记忆 API ==========

@router.get("/api/hippocampus/memories")
async def api_hippocampus_memories(topic: str = ""):
    """获取海马体记忆"""
    _check_focus_module()

    if topic:
        memories = get_topic_memory(topic)
        return {"ok": True, "topic": topic, "memory": memories}
    else:
        memories = get_all_memories()
        return {"ok": True, "memories": memories}


@router.get("/api/hippocampus/meta")
async def api_hippocampus_meta():
    """获取用户元认知数据"""
    _check_focus_module()

    meta = get_meta()
    return {"ok": True, "meta": meta}


@router.put("/api/hippocampus/meta")
async def api_hippocampus_update_meta(req: MetaUpdateRequest):
    """更新用户元认知数据"""
    _check_focus_module()

    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "没有要更新的字段")

    meta = update_meta(updates)
    return {"ok": True, "meta": meta}


@router.delete("/api/hippocampus/topic/{topic}")
async def api_hippocampus_delete_topic(topic: str):
    """删除主题记忆（遗忘）"""
    _check_focus_module()

    if not topic or len(topic) > 200:
        raise HTTPException(400, "主题名无效")

    deleted = delete_topic(topic)
    if deleted:
        return {"ok": True, "message": f"已遗忘主题「{topic}」"}
    else:
        raise HTTPException(404, "主题不存在")


@router.post("/api/hippocampus/decay")
async def api_hippocampus_decay():
    """手动触发记忆衰减周期"""
    _check_focus_module()

    result = run_decay_cycle()
    return {"ok": True, **result}
