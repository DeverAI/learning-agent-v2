import os
from typing import List, Optional
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from models.database import get_db
from models.models import Question
from services import correction_service
from services.upload_guard import read_upload_limited
from services.correction_service import _safe_correction_error
from schemas.schemas import CorrectionResponse, CorrectionListItem, CorrectionHistoryItem
from logger import get_logger, log_error
from services.capture_modes import (
    MAX_CAPTURE_IMAGES,
    MAX_CAPTURE_TOTAL_SIZE,
    normalize_capture_mode,
)

logger = get_logger()

MAX_CORRECTION_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_CORRECTION_EXTS = (".jpg", ".jpeg", ".png", ".webp")


class SingleCorrectionRequest(BaseModel):
    student_answer: str = Field(default="", max_length=50_000)


class SearchCorrectionRequest(BaseModel):
    query_id: str = Field(min_length=1, max_length=64)
    matched_question_id: str = Field(min_length=1, max_length=64)


class BatchCorrectionRequest(BaseModel):
    question_ids: list[str] = Field(min_length=1, max_length=30)

router = APIRouter(prefix="/api/correction", tags=["correction"])


def _raise_ai_dependency_error(exc: Exception, detail: str) -> None:
    """把 AI 不可用/认证失败映射为 502/503，避免批改依赖错误伪装成 500。"""
    _msg = str(exc).lower()
    if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
        raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
    raise HTTPException(status_code=502, detail=detail)


def _safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename or "image.jpg")[1] or ".jpg"
    ext = ext.lower()
    if ext not in ALLOWED_CORRECTION_EXTS:
        ext = ".jpg"
    return ext


def _has_supported_image_signature(raw: bytes) -> bool:
    return (raw.startswith(b"\xff\xd8\xff") or raw.startswith(b"\x89PNG\r\n\x1a\n")
            or (len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))


async def _validate_image(file: UploadFile) -> tuple[bytes, str]:
    """读取并校验上传图片，返回 (raw_bytes, extension)。"""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, detail="请上传图片文件（JPG/PNG/WEBP）")
    raw_bytes = await read_upload_limited(
        file, MAX_CORRECTION_IMAGE_SIZE,
        too_large_detail="图片大小超过 10MB 限制",
        empty_detail="图片不能为空",
    )
    if not _has_supported_image_signature(raw_bytes):
        raise HTTPException(400, detail="文件内容不是有效的 JPG、PNG 或 WEBP 图片")
    ext = _safe_ext(file.filename)
    original_ext = os.path.splitext(file.filename or "")[1].lower()
    if original_ext and original_ext not in ALLOWED_CORRECTION_EXTS:
        raise HTTPException(400, detail="仅支持 JPG、PNG 或 WEBP 图片")
    return raw_bytes, ext


@router.post("/upload")
async def upload_correction_image(
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    capture_mode: str = Form("single_question"),
    db: AsyncSession = Depends(get_db)
):
    """上传单题或单页作答图片；整卷模式需先选择对应试卷。"""
    try:
        mode = normalize_capture_mode(capture_mode)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    if mode == "whole_paper":
        raise HTTPException(400, detail="整卷检查需要先选择对应试卷")
    selected = ([file] if file is not None else []) + list(files or [])
    if not selected:
        raise HTTPException(400, detail="请上传至少一张图片")
    if len(selected) > MAX_CAPTURE_IMAGES:
        raise HTTPException(400, detail=f"一次最多上传 {MAX_CAPTURE_IMAGES} 张图片")
    images = []
    for item in selected:
        raw_bytes, _ = await _validate_image(item)
        images.append((raw_bytes, item.filename or "image.jpg"))
    if sum(len(raw) for raw, _ in images) > MAX_CAPTURE_TOTAL_SIZE:
        raise HTTPException(413, detail="本次图片总大小不能超过 60MB")
    try:
        return await correction_service.upload_answer_images(
            images,
            capture_mode=mode,
            source_type=("single" if mode == "single_question" else "page"),
        )
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        log_error("correction.upload", f"Upload failed: {e}")
        raise HTTPException(500, detail="上传失败，请稍后重试")


@router.get("/status/{question_id}")
async def correction_status(question_id: str):
    """查询批改图片 OCR 状态。"""
    try:
        return await correction_service.get_correction_status(question_id)
    except ValueError as e:
        raise HTTPException(404, detail=str(e))
    except Exception as e:
        log_error("correction.status", f"Status query failed: {e}")
        raise HTTPException(500, detail="查询状态失败")


@router.post("/single/{question_id}", response_model=CorrectionResponse)
async def correct_single_question(
    question_id: str,
    data: SingleCorrectionRequest = Body(default=SingleCorrectionRequest()),
):
    """对单题作答进行 AI 批改。若 OCR 尚未完成会等待。"""
    try:
        report = await correction_service.correct_single(
            question_id,
            source_type="single",
            student_answer_override=data.student_answer or None,
        )
        payload = dict(report)
        payload["id"] = payload.get("correction_id", "")
        return CorrectionResponse(**payload)
    except HTTPException:
        # 4xx 客户端错误（如 _find_matches 的 404/400/422）不得被包装成 502
        raise
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        log_error("correction.single", f"Single correction failed: {e}")
        _raise_ai_dependency_error(e, "批改失败，请稍后重试")


@router.post("/search", response_model=CorrectionResponse)
async def correct_search_result(
    data: SearchCorrectionRequest,
    db: AsyncSession = Depends(get_db)
):
    """对拍照搜题的匹配结果直接批改：用查询图的 OCR 文本作为学生答案，按指定题库题目批改。"""
    query_id = data.query_id.strip()
    matched_question_id = data.matched_question_id.strip()

    q = await db.get(Question, query_id)
    if not q:
        raise HTTPException(404, detail="查询记录不存在")
    if q.source_type not in ("search_query", "correction_query"):
        raise HTTPException(400, detail="该记录不是搜题/批改查询记录")
    if q.status not in ("search_done", "done"):
        raise HTTPException(400, detail="查询题 OCR 尚未完成，请稍后再试")

    student_answer = (q.ocr_text or "").strip()
    if not student_answer:
        raise HTTPException(400, detail="未识别到学生作答内容")

    try:
        report = await correction_service.correct_single_target(
            db=db,
            answer_question_id=query_id,
            target_question_id=matched_question_id,
            student_answer=student_answer,
            paper_id=None,
            source_type="search",
        )
        await db.commit()
        payload = dict(report)
        payload["id"] = payload.get("correction_id", "")
        return CorrectionResponse(**payload)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as e:
        await db.rollback()
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        await db.rollback()
        log_error("correction.search", f"Search correction failed: {e}")
        _raise_ai_dependency_error(e, "批改失败，请稍后重试")


@router.post("/batch")
async def correct_page_batch(
    data: BatchCorrectionRequest,
    db: AsyncSession = Depends(get_db),
):
    """批量检查单页拆出的题目；单题失败不阻断其余题目。"""
    raw_ids = data.question_ids
    question_ids = []
    for raw_id in raw_ids:
        qid = str(raw_id or "").strip()
        if qid and qid not in question_ids:
            question_ids.append(qid)
    if not question_ids:
        raise HTTPException(400, detail="没有可批改的题目")

    query_result = await db.execute(select(Question).where(Question.id.in_(question_ids)))
    query_map = {item.id: item for item in query_result.scalars().all()}
    if len(query_map) != len(question_ids):
        raise HTTPException(400, detail="部分单页题目不存在")
    groups = {
        (getattr(query_map[qid], "capture_group_id", "") or query_map[qid].id)
        for qid in question_ids
    }
    if len(groups) != 1 or any(
        query_map[qid].source_type != "correction_query"
        or getattr(query_map[qid], "capture_mode", "single_question") != "single_page"
        for qid in question_ids
    ):
        raise HTTPException(400, detail="question_ids 必须来自同一次单页检查")

    results = []
    for index, question_id in enumerate(question_ids, 1):
        try:
            report = await correction_service.correct_single(
                question_id, source_type="page"
            )
            report["number"] = index
            results.append(report)
        except Exception as exc:
            logger.warning("Page correction item %s failed: %s", question_id, exc)
            results.append({"question_id": question_id, "number": index,
                            "error": _safe_correction_error(exc), "score": 0, "max_score": 0})
    completed = [item for item in results if not item.get("error")]
    failed_count = len(results) - len(completed)
    return {
        "capture_mode": "single_page",
        "question_count": len(question_ids),
        "completed_count": len(completed),
        "failed_count": failed_count,
        "complete": failed_count == 0,
        "score_status": "complete" if failed_count == 0 else "partial_needs_review",
        "total_score": sum(float(item.get("score") or 0) for item in completed),
        "max_score": (sum(float(item.get("max_score") or 0) for item in completed)
                      if failed_count == 0 else None),
        "results": results,
    }


@router.get("/history", response_model=list[CorrectionHistoryItem])
async def correction_history(
    type: str = Query("all", pattern="^(all|single|page|paper)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    """批改历史列表，含匹配题目元信息，支持按类型筛选。

    type=all/single/paper；返回 CorrectionHistoryItem（含 subject/grade/question_preview）。
    路由顺序：必须位于 GET /{correction_id} 之前，否则 /history 会被动态路径捕获。
    """
    try:
        st = None if type == "all" else type
        items = await correction_service.list_corrections(
            limit=limit, offset=offset, source_type=st, with_meta=True
        )
        return [CorrectionHistoryItem(**x) for x in items]
    except Exception as e:
        log_error("correction.history", f"History list failed: {e}")
        raise HTTPException(500, detail="获取批改历史失败")


@router.get("/", response_model=list[CorrectionListItem])
async def list_corrections(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    """列出最近批改记录。"""
    try:
        items = await correction_service.list_corrections(limit=limit, offset=offset)
        return [CorrectionListItem(**x) for x in items]
    except Exception as e:
        log_error("correction.list", f"List corrections failed: {e}")
        raise HTTPException(500, detail="获取批改列表失败")


@router.post("/paper/{paper_id}")
async def correct_paper(
    paper_id: str,
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
):
    """上传整份试卷的一张或多页作答图片，按卷内题目逐题批改并汇总。"""
    selected = ([file] if file is not None else []) + list(files or [])
    if not selected:
        raise HTTPException(400, detail="请上传至少一张图片")
    if len(selected) > MAX_CAPTURE_IMAGES:
        raise HTTPException(400, detail=f"一次最多上传 {MAX_CAPTURE_IMAGES} 张图片")
    images = []
    for item in selected:
        raw_bytes, _ = await _validate_image(item)
        images.append((raw_bytes, item.filename or "answer.jpg"))
    if sum(len(raw) for raw, _ in images) > MAX_CAPTURE_TOTAL_SIZE:
        raise HTTPException(413, detail="本次图片总大小不能超过 60MB")
    try:
        return await correction_service.correct_paper(paper_id, images)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    except Exception as e:
        log_error("correction.paper", f"Paper correction failed: {e}")
        _raise_ai_dependency_error(e, "试卷批改失败，请稍后重试")


@router.get("/{correction_id}", response_model=CorrectionResponse)
async def get_correction(correction_id: str):
    """获取单条批改记录详情。"""
    try:
        item = await correction_service.get_correction(correction_id)
        if not item:
            raise HTTPException(404, detail="批改记录不存在")
        return CorrectionResponse(**item)
    except HTTPException:
        raise
    except Exception as e:
        log_error("correction.get", f"Get correction failed: {e}")
        raise HTTPException(500, detail="获取批改记录失败")
