import os
import re
import asyncio
import shutil
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.models import Question, Correction, Paper, ProcessingTask, gen_id
from models.database import async_session
from services.ocr_service import ocr_service
from services.ai_service import ai_service, normalize_correction_report
from routers.search import _find_matches
from routers.search import _run_search_ocr
from config import CORRECTIONS_DIR, QUESTIONS_DIR
from services.capture_modes import (
    MAX_CAPTURE_IMAGES,
    MAX_CAPTURE_TOTAL_SIZE,
    normalize_capture_mode,
)
from services.diagram_service import _is_valid_question_id
from services.question_challenge import has_unresolved_high_challenge
from logger import get_logger, log_error


def _safe_correction_error(e: Exception) -> str:
    """逐题批改失败返回稳定文案：AI 鉴权/依赖故障区分语义，原文只进日志
    （FreqErr [错误详情泄漏] —— 500/批量路径不得回传 str(exc)）。"""
    msg = str(e).lower()
    if "401" in msg or "authentication" in msg or "api key" in msg or "illegal header" in msg:
        return "AI 服务认证失败，请检查模型 API Key 配置"
    if "timeout" in msg or "timed out" in msg or "connect" in msg or "unreachable" in msg:
        return "AI 服务暂不可用，请稍后重试"
    return "本题批改失败，请稍后重试或人工批改"

logger = get_logger()
MAX_CORRECTION_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_CORRECTION_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename or "image.jpg")[1] or ".jpg"
    ext = ext.lower()
    if ext not in ALLOWED_CORRECTION_EXTS:
        ext = ".jpg"
    return ext


def _atomic_write_bytes(path: str, raw: bytes) -> None:
    temp_path = path + ".tmp"
    try:
        with open(temp_path, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                logger.warning("Failed to remove temporary correction file: %s", temp_path)


async def _wait_for_ocr(question_id: str, timeout: float = 60.0, interval: float = 0.5) -> Optional[Question]:
    """轮询等待 OCR 完成或失败。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with async_session() as db:
            q = await db.get(Question, question_id)
            if q and q.status in ("search_done", "done", "error"):
                return q
        await asyncio.sleep(interval)
    return None


async def upload_answer_image(raw_bytes: bytes, filename: str, source_type: str = "single") -> dict:
    """上传学生作答图片，创建临时记录并启动 OCR。

    返回 {"correction_id": ..., "question_id": ..., "message": ...}
    """
    return await upload_answer_images([(raw_bytes, filename)], capture_mode="single_question",
                                      source_type=source_type)


async def upload_answer_images(
    images: list[tuple[bytes, str]],
    capture_mode: str = "single_question",
    source_type: str = "single",
) -> dict:
    """保存一组作答照片，并按拍照模式启动 OCR/拆题。"""
    mode = normalize_capture_mode(capture_mode)
    if mode == "whole_paper":
        raise ValueError("整卷检查需要先选择对应试卷")
    if not images:
        raise ValueError("请上传至少一张图片")
    if len(images) > MAX_CAPTURE_IMAGES:
        raise ValueError(f"一次最多上传 {MAX_CAPTURE_IMAGES} 张图片")
    if any(not raw for raw, _ in images):
        raise ValueError("图片不能为空")
    if any(len(raw) > MAX_CORRECTION_IMAGE_SIZE for raw, _ in images):
        raise ValueError("单张图片不能超过 10MB")
    if sum(len(raw) for raw, _ in images) > MAX_CAPTURE_TOTAL_SIZE:
        raise ValueError("本次图片总大小不能超过 60MB")

    qid = gen_id()
    folder = os.path.join(QUESTIONS_DIR, qid)
    os.makedirs(folder, exist_ok=False)

    desc = {"question_id": qid, "type": "search_ocr", "capture_mode": mode}
    from routers.ocr import _run_bg, _save_task_state, _clear_task_state
    try:
        multi_images = []
        image_roles = []
        for index, (raw_bytes, filename) in enumerate(images):
            ext = _safe_ext(filename)
            path = os.path.join(folder, f"original_{index}{ext}")
            with open(path, "wb") as w:
                w.write(raw_bytes)
                w.flush()
                os.fsync(w.fileno())
            multi_images.append({"path": path, "filename": filename, "role": "question"})
            image_roles.append({"path": path, "role": "question", "order": index})
        primary_path = multi_images[0]["path"]

        async with async_session() as db:
            db.add(Question(
                id=qid,
                folder_path=folder,
                raw_image_path=primary_path,
                status="search_staged",
                source_type="correction_query",
                bank="correction_queries",
                multi_images=multi_images,
                image_roles=image_roles,
                capture_mode=mode,
                capture_group_id=qid,
                capture_index=0,
            ))
            _save_task_state(desc)
            await db.commit()
    except Exception:
        _clear_task_state(qid)
        shutil.rmtree(folder, ignore_errors=True)
        raise

    _run_bg(_run_search_ocr(qid, mode), desc)

    return {
        "correction_id": qid,
        "question_id": qid,
        "message": "已上传，正在识别作答内容",
        "status": "search_staged",
        "source_type": source_type,
        "capture_mode": mode,
        "image_count": len(images),
    }


async def get_correction_status(question_id: str) -> dict:
    """查询批改 OCR 状态。"""
    async with async_session() as db:
        q = await db.get(Question, question_id)
        if not q:
            raise ValueError("查询记录不存在")
        task_result = await db.execute(
            select(ProcessingTask).where(ProcessingTask.question_id == question_id)
            .order_by(ProcessingTask.created_at.desc()).limit(1)
        )
        task = task_result.scalar_one_or_none()
        return {
            "question_id": question_id,
            "status": q.status,
            "ocr_text": q.ocr_text or "",
            "subject": q.subject or "",
            "grade": q.grade or "",
            "tags": q.knowledge_tags or [],
            "error_message": q.error_message or "",
            "capture_mode": getattr(q, "capture_mode", "single_question") or "single_question",
            "capture_group_id": getattr(q, "capture_group_id", "") or q.id,
            "capture_index": getattr(q, "capture_index", 0) or 0,
            "split_question_ids": ((task.result or {}).get("split_question_ids", [question_id])
                                   if task else [question_id]),
        }


async def _fresh_question(db: AsyncSession, question_id: str) -> Optional[Question]:
    """在当前会话中重新读取题目，避免缓存导致状态陈旧。"""
    r = await db.execute(select(Question).where(Question.id == question_id))
    return r.scalar_one_or_none()


async def _save_correction(
    db: AsyncSession,
    question_id: str,
    paper_id: Optional[str],
    student_answer: str,
    matched_id: str,
    report: dict,
    raw_image_path: str,
    source_type: str,
) -> str:
    """保存一条批改记录，返回 correction_id。"""
    report = normalize_correction_report(report)
    correction_id = gen_id()
    corr = Correction(
        id=correction_id,
        question_id=question_id,
        paper_id=paper_id,
        student_answer=student_answer,
        matched_question_id=matched_id,
        score=report["score"],
        max_score=report["max_score"],
        points=report.get("points", []) or [],
        feedback=report.get("feedback", ""),
        error_analysis=report.get("error_analysis", ""),
        suggestions=report.get("suggestions", ""),
        raw_image_path=raw_image_path or "",
        source_type=source_type,
    )
    db.add(corr)
    return correction_id


async def correct_single(
    question_id: str,
    db: Optional[AsyncSession] = None,
    source_type: str = "single",
    paper_id: Optional[str] = None,
    student_answer_override: Optional[str] = None,
) -> dict:
    """对单个学生作答进行批改。

    流程：OCR 文本 -> 题库匹配 -> AI 按得分点评分 -> 保存 Correction。
    若 student_answer_override 提供，则跳过 OCR 等待，直接用作学生答案。
    """
    if not _is_valid_question_id(question_id):
        raise ValueError(f"非法题目ID: {question_id}")

    managed_db = db is None
    if managed_db:
        db = async_session()

    try:
        q = await _fresh_question(db, question_id)
        if not q:
            raise ValueError("作答记录不存在")
        if q.source_type not in ("search_query", "correction_query"):
            raise ValueError("该记录不是拍照作答或搜题查询记录")

        if student_answer_override is not None:
            student_answer = student_answer_override.strip()
        else:
            if q.status not in ("search_done", "done"):
                q_waited = await _wait_for_ocr(question_id)
                if not q_waited:
                    raise ValueError("OCR 识别超时，请稍后再试")
                # 回到当前会话重新读取，确保拿到最新状态
                q = await _fresh_question(db, question_id)
                if not q or q.status == "error":
                    raise ValueError(f"OCR 识别失败: {q.error_message if q else '未知错误'}")
            student_answer = (q.ocr_text or "").strip()

        if not student_answer:
            raise ValueError("未识别到学生作答内容")

        # 强制使用 hybrid 模式匹配题库
        match_result = await _find_matches(db, question_id, mode="hybrid", top_k=3)
        matches = match_result.get("matches", [])
        if not matches:
            raise ValueError("未匹配到题库中的题目，无法批改")

        top = matches[0]
        threshold = float(match_result.get("confidence_threshold", 0.75))
        if not match_result.get("confident", (top.get("score") or 0) >= threshold):
            raise ValueError(
                f"匹配度不足（{((top.get('score') or 0) * 100):.1f}%），请先在搜题结果中确认正确题目"
            )

        matched_id = top.get("question_id")
        matched = await db.get(Question, matched_id)
        if not matched:
            raise ValueError("匹配到的题库题目不存在")
        if has_unresolved_high_challenge(matched.audit_flags, matched.is_resolved):
            raise ValueError("匹配题目被标记为大概率存在问题，人工确认前不能用于批改")

        correction_report = await ai_service.glm_correct_answer(
            question_html=matched.question_html or matched.ocr_text or "",
            standard_answer=matched.standard_answer or "",
            score_points_html=matched.score_points_html or "",
            answer_html=matched.answer_html or "",
            student_answer=student_answer,
        )
        correction_report = normalize_correction_report(correction_report)

        correction_id = await _save_correction(
            db, question_id, paper_id, student_answer, matched_id,
            correction_report, q.raw_image_path or "", source_type,
        )

        if managed_db:
            await db.commit()

        return {
            "correction_id": correction_id,
            "question_id": question_id,
            "paper_id": paper_id,
            "matched_question_id": matched_id,
            "student_answer": student_answer,
            "score": correction_report.get("score", 0),
            "max_score": correction_report.get("max_score", 0),
            "points": correction_report.get("points", []),
            "feedback": correction_report.get("feedback", ""),
            "error_analysis": correction_report.get("error_analysis", ""),
            "suggestions": correction_report.get("suggestions", ""),
            "question_preview": top.get("preview", ""),
            "subject": matched.subject or "",
            "grade": matched.grade or "",
        }
    except Exception:
        if managed_db:
            await db.rollback()
        raise
    finally:
        if managed_db:
            await db.close()


async def correct_single_target(
    db: AsyncSession,
    answer_question_id: str,
    target_question_id: str,
    student_answer: str,
    paper_id: Optional[str],
    source_type: str,
) -> dict:
    """按指定题库题目批改学生作答（用于试卷批改，避免匹配偏差）。"""
    target = await db.get(Question, target_question_id)
    if not target:
        raise ValueError("目标题目不存在")
    if target.status != "done" or target.source_type in ("search_query", "correction_query"):
        raise ValueError("目标题目尚未完成处理或不是正式题库题目")
    if has_unresolved_high_challenge(target.audit_flags, target.is_resolved):
        raise ValueError("目标题目被标记为大概率存在问题，人工确认前不能用于批改")
    student_answer = (student_answer or "").strip()
    unanswered = not student_answer
    answer_for_model = student_answer or "（未作答）"

    answer_q = await db.get(Question, answer_question_id)
    raw_image_path = answer_q.raw_image_path if answer_q else ""

    correction_report = await ai_service.glm_correct_answer(
        question_html=target.question_html or target.ocr_text or "",
        standard_answer=target.standard_answer or "",
        score_points_html=target.score_points_html or "",
        answer_html=target.answer_html or "",
        student_answer=answer_for_model,
    )
    correction_report = normalize_correction_report(correction_report)
    if unanswered:
        correction_report["score"] = 0.0
        correction_report["feedback"] = correction_report.get("feedback") or "本题未作答"

    correction_id = await _save_correction(
        db, answer_question_id, paper_id, student_answer, target_question_id,
        correction_report, raw_image_path, source_type,
    )

    return {
        "correction_id": correction_id,
        "question_id": answer_question_id,
        "paper_id": paper_id,
        "matched_question_id": target_question_id,
        "student_answer": student_answer,
        "score": correction_report.get("score", 0),
        "max_score": correction_report.get("max_score", 0),
        "points": correction_report.get("points", []),
        "feedback": correction_report.get("feedback", ""),
        "error_analysis": correction_report.get("error_analysis", ""),
        "suggestions": correction_report.get("suggestions", ""),
        "question_preview": (target.ocr_text or target.question_html or "")[:120],
        "subject": target.subject or "",
        "grade": target.grade or "",
    }


async def correct_paper(
    paper_id: str,
    raw_bytes: bytes | list[tuple[bytes, str]],
    filename: str = "answer.jpg",
) -> dict:
    """对整份试卷作答进行批改。

    将整份作答 OCR 后按题目数量大致分段，再逐题调用指定题目的批改。
    """
    if not _is_valid_question_id(paper_id):
        raise ValueError(f"非法试卷ID: {paper_id}")

    images = raw_bytes if isinstance(raw_bytes, list) else [(raw_bytes, filename)]
    if not images:
        raise ValueError("请上传至少一张图片")
    if len(images) > MAX_CAPTURE_IMAGES:
        raise ValueError(f"一次最多上传 {MAX_CAPTURE_IMAGES} 张图片")
    if any(not raw for raw, _ in images):
        raise ValueError("图片不能为空")
    if any(len(raw) > MAX_CORRECTION_IMAGE_SIZE for raw, _ in images):
        raise ValueError("单张图片不能超过 10MB")
    if sum(len(raw) for raw, _ in images) > MAX_CAPTURE_TOTAL_SIZE:
        raise ValueError("本次图片总大小不能超过 60MB")

    async with async_session() as db:
        paper = await db.get(Paper, paper_id)
        if not paper:
            raise ValueError("试卷不存在")

        question_order = paper.question_order or []
        if not question_order:
            # 兼容旧数据：使用 question_ids
            question_order = [{"id": qid, "number": i + 1} for i, qid in enumerate(paper.question_ids or [])]
        if not question_order:
            raise ValueError("试卷中没有题目")

        qid = gen_id()

        # 按本次批改记录保存多页作答，避免下一次上传覆盖旧图片。
        folder = os.path.join(CORRECTIONS_DIR, f"paper_{paper_id}", qid)
        q_folder = os.path.join(QUESTIONS_DIR, qid)
        try:
            os.makedirs(folder, exist_ok=False)
            # 上传为临时题目用于 OCR
            os.makedirs(q_folder, exist_ok=False)
            image_paths = []
            multi_images = []
            image_roles = []
            for index, (raw, image_name) in enumerate(images):
                ext = _safe_ext(image_name)
                image_path = os.path.join(folder, f"answer_{index}{ext}")
                _atomic_write_bytes(image_path, raw)
                q_path = os.path.join(q_folder, f"original_{index}{ext}")
                _atomic_write_bytes(q_path, raw)
                image_paths.append(image_path)
                multi_images.append({"path": q_path, "filename": image_name, "role": "question"})
                image_roles.append({"path": q_path, "role": "question", "order": index})
            q_path = multi_images[0]["path"]

            db.add(Question(
                id=qid,
                folder_path=q_folder,
                raw_image_path=q_path,
                status="search_staged",
                source_type="correction_query",
                bank="correction_queries",
                multi_images=multi_images,
                image_roles=image_roles,
                capture_mode="whole_paper",
                capture_group_id=qid,
                capture_index=0,
            ))
            await db.commit()
        except Exception:
            await db.rollback()
            shutil.rmtree(q_folder, ignore_errors=True)
            shutil.rmtree(folder, ignore_errors=True)
            raise

    # 整卷端点本来就会等待 OCR，直接 await 可避免后台任务失去引用或轮询竞态。
    async def _cleanup_failed_query():
        """失败路径清理：临时 Question 行与双份图片目录不得残留（FreqErr [文件数据库双写]）"""
        try:
            async with async_session() as cdb:
                temp_q = await cdb.get(Question, qid)
                if temp_q:
                    await cdb.delete(temp_q)
                    await cdb.commit()
        except Exception as ce:
            logger.warning("Temp correction query cleanup failed for %s: %s", qid, ce)
        shutil.rmtree(q_folder, ignore_errors=True)
        shutil.rmtree(folder, ignore_errors=True)

    try:
        await ocr_service.process_image(qid, multi_images=multi_images, search_only=True)
    except Exception as e:
        log_error("correction.paper_ocr", f"Paper OCR failed for {paper_id}: {e}")
        logger.warning("Paper OCR failed for %s: %s", paper_id, e, exc_info=True)
        await _cleanup_failed_query()
        raise ValueError("试卷作答识别超时或失败")
    q_waited = await _wait_for_ocr(qid, timeout=5.0)
    if not q_waited or q_waited.status != "search_done":
        await _cleanup_failed_query()
        raise ValueError("试卷作答识别超时或失败")
    full_text = (q_waited.ocr_text or "").strip()
    if not full_text:
        await _cleanup_failed_query()
        raise ValueError("未识别到试卷作答内容")

    # Prefer semantic answer segmentation. Equal-length slicing can attach an answer
    # to the wrong question and is therefore deliberately not used as a fallback.
    ordered = []
    target_ids = []
    for idx, item in enumerate(question_order):
        target_id = item.get("id") if isinstance(item, dict) else item
        number = item.get("number", idx + 1) if isinstance(item, dict) else idx + 1
        target_ids.append(target_id)
        ordered.append({"id": target_id, "number": number})

    async with async_session() as db:
        target_result = await db.execute(select(Question).where(Question.id.in_(target_ids)))
        target_map = {item.id: item for item in target_result.scalars().all()}
    for item in ordered:
        target = target_map.get(item["id"])
        item["question"] = re.sub(
            r"<[^>]+>", "", (target.question_html or target.ocr_text or "") if target else ""
        )[:300]
    missing_targets = [item["id"] for item in ordered if item["id"] not in target_map]
    if missing_targets:
        await _cleanup_failed_query()   # 功能检查轮 F4-H3：失败路径清理临时记录
        raise ValueError(f"试卷冻结题目已缺失，无法安全批改: {', '.join(missing_targets[:5])}")
    if len(full_text) > 24000:
        await _cleanup_failed_query()
        raise ValueError("整卷 OCR 内容超过自动分割容量，请分为单页批改，避免卷尾答案被截断")

    segment_map = {}
    try:
        split_prompt = (
            "把整份学生作答 OCR 按给定试卷题目准确分配。题号缺失时结合题干判断；"
            "不得编造作答。输出 JSON："
            '{"answers":[{"question_id":"id","student_answer":"原文作答"}]}。\n'
            f"试卷题目：{ordered}\n学生作答 OCR：\n{full_text}"
        )
        split_result = await ai_service.deepseek_json(
            [{"role": "user", "content": split_prompt}], max_tokens=16384, scope="solve"
        )
        valid_ids = set(target_ids)
        for item in split_result.get("answers", []) if isinstance(split_result, dict) else []:
            target_id = item.get("question_id") if isinstance(item, dict) else None
            answer = str(item.get("student_answer", "")).strip() if isinstance(item, dict) else ""
            if target_id in valid_ids and answer:
                segment_map[target_id] = answer
    except Exception as exc:
        logger.warning("AI paper answer splitting failed for %s: %s", paper_id, exc)

    if len(target_ids) == 1 and not segment_map:
        segment_map[target_ids[0]] = full_text
    if not segment_map:
        markers = list(re.finditer(r"(?m)(?:^|\n)\s*(\d{1,3})\s*[\.．、\)]\s*", full_text))
        number_to_id = {str(item["number"]): item["id"] for item in ordered}
        for idx, marker in enumerate(markers):
            target_id = number_to_id.get(marker.group(1))
            if target_id:
                end = markers[idx + 1].start() if idx + 1 < len(markers) else len(full_text)
                answer = full_text[marker.end():end].strip()
                if answer:
                    segment_map[target_id] = answer
    if not segment_map:
        await _cleanup_failed_query()
        raise ValueError("无法可靠分割整卷作答，请保留清晰题号，或改用单题批改")

    results = []
    total_score = 0.0
    total_max = 0.0
    failed_count = 0
    unmapped_count = 0
    async with async_session() as db:
        for idx, item in enumerate(question_order):
            qid_in_paper = item.get("id") if isinstance(item, dict) else item
            number = item.get("number", idx + 1) if isinstance(item, dict) else idx + 1
            if qid_in_paper not in segment_map:
                # AI 分段/题号回退未映射到本题（功能检查轮 F4-H1）：不虚构 0 分
                # 成绩——缺题记 unmapped 交人工复核，且不覆盖正式成绩
                unmapped_count += 1
                results.append({
                    "paper_question_id": qid_in_paper,
                    "number": number,
                    "unmapped": True,
                    "score": None,
                    "max_score": None,
                })
                continue
            segment = segment_map[qid_in_paper]
            try:
                report = await correct_single_target(
                    db=db,
                    answer_question_id=qid,
                    target_question_id=qid_in_paper,
                    student_answer=segment,
                    paper_id=paper_id,
                    source_type="paper",
                )
                report["paper_question_id"] = qid_in_paper
                report["number"] = number
                if not segment:
                    report["student_answer"] = ""
                    report["unanswered"] = True
                results.append(report)
                total_score += report.get("score", 0) or 0
                total_max += report.get("max_score", 0) or 0
            except Exception as e:
                logger.warning("批改试卷第 %s 题失败: %s", number, e)
                failed_count += 1
                results.append({
                    "paper_question_id": qid_in_paper,
                    "number": number,
                    "error": _safe_correction_error(e),
                    "score": 0,
                    "max_score": 0,
                })
        await db.commit()

    # 只有全部题目都成功批改才覆盖正式成绩；unmapped/失败均视为不完整，
    # 保留为人工复核材料，避免漏题被记 0 分污染正式成绩。
    if failed_count == 0 and unmapped_count == 0:
        async with async_session() as db2:
            paper = await db2.get(Paper, paper_id)
            if paper:
                paper.user_score = total_score
                await db2.commit()

    relative_images = [
        "/storage/" + os.path.relpath(path, os.path.dirname(CORRECTIONS_DIR)).replace(os.sep, "/")
        for path in image_paths
    ]

    return {
        "paper_id": paper_id,
        "total_score": total_score,
        "max_score": total_max if (failed_count == 0 and unmapped_count == 0) else None,
        "complete": failed_count == 0 and unmapped_count == 0,
        "score_status": ("complete" if failed_count == 0 and unmapped_count == 0
                         else "partial_needs_review"),
        "failed_count": failed_count,
        "unmapped_count": unmapped_count,
        "image_path": relative_images[0],
        "image_paths": relative_images,
        "image_count": len(image_paths),
        "capture_mode": "whole_paper",
        "results": results,
    }


async def list_corrections(
    limit: int = 50,
    offset: int = 0,
    source_type: Optional[str] = None,
    with_meta: bool = False,
) -> list[dict]:
    """列出最近的批改记录。

    - source_type: 可选 'single' / 'paper' 过滤；None 表示全部。
    - with_meta: 为 True 时附加 subject/grade/question_preview（用于历史页/批改中心最近记录），
      通过一次 IN 查询批量加载匹配题目，避免 N+1。
    """
    async with async_session() as db:
        stmt = select(Correction).order_by(Correction.created_at.desc())
        if source_type in ("single", "page", "paper"):
            stmt = stmt.where(Correction.source_type == source_type)
        stmt = stmt.limit(limit).offset(offset)
        r = await db.execute(stmt)
        items = r.scalars().all()

        base = [
            {
                "id": c.id,
                "question_id": c.question_id,
                "paper_id": c.paper_id,
                "matched_question_id": c.matched_question_id or "",
                "score": c.score,
                "max_score": c.max_score,
                "source_type": c.source_type or "single",
                "created_at": c.created_at.isoformat() if c.created_at else "",
            }
            for c in items
        ]

        if not with_meta or not items:
            return base

        matched_ids = [c.matched_question_id for c in items if c.matched_question_id]
        meta_map: dict[str, dict] = {}
        if matched_ids:
            qr = await db.execute(select(Question).where(Question.id.in_(matched_ids)))
            for q in qr.scalars().all():
                meta_map[q.id] = {
                    "subject": q.subject or "",
                    "grade": q.grade or "",
                    "question_preview": (q.ocr_text or q.question_html or "")[:120],
                }
        for item in base:
            m = meta_map.get(item["matched_question_id"], {})
            item["subject"] = m.get("subject", "")
            item["grade"] = m.get("grade", "")
            item["question_preview"] = m.get("question_preview", "")
        return base


async def get_correction(correction_id: str) -> Optional[dict]:
    """获取单条批改记录详情。"""
    async with async_session() as db:
        c = await db.get(Correction, correction_id)
        if not c:
            return None
        subject = ""
        grade = ""
        question_preview = ""
        if c.matched_question_id:
            matched = await db.get(Question, c.matched_question_id)
            if matched:
                subject = matched.subject or ""
                grade = matched.grade or ""
                question_preview = (matched.ocr_text or matched.question_html or "")[:120]
        return {
            "id": c.id,
            "correction_id": c.id,
            "question_id": c.question_id,
            "paper_id": c.paper_id,
            "student_answer": c.student_answer or "",
            "matched_question_id": c.matched_question_id or "",
            "score": c.score,
            "max_score": c.max_score,
            "points": c.points or [],
            "feedback": c.feedback or "",
            "error_analysis": c.error_analysis or "",
            "suggestions": c.suggestions or "",
            "raw_image_path": c.raw_image_path or "",
            "source_type": c.source_type or "single",
            "subject": subject,
            "grade": grade,
            "question_preview": question_preview,
            "created_at": c.created_at.isoformat() if c.created_at else "",
            "updated_at": c.updated_at.isoformat() if c.updated_at else "",
        }
