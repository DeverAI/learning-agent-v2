import math
import os
import re
import asyncio
import difflib
import shutil
from typing import List, Optional
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, Text, func
from pydantic import BaseModel, Field
from models.database import get_db
from models.models import Question, ProcessingTask, gen_id
from services.ocr_service import ocr_service
from services.upload_guard import read_upload_limited
from services.ai_service import ai_service
from services.capture_modes import (
    MAX_CAPTURE_IMAGES,
    MAX_CAPTURE_TOTAL_SIZE,
    normalize_capture_mode,
)
from config import QUESTIONS_DIR, load_settings
from logger import get_logger, log_error

MAX_SEARCH_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_SEARCH_EXTS = (".jpg", ".jpeg", ".png", ".webp")
BANK_NAME_RE = re.compile(r"^[\w\-\u4e00-\u9fa5]{1,32}$")
MATCH_THRESHOLDS = {"text": 0.60, "ai": 0.70, "hybrid": 0.75}
# 相似题检索的规模策略（FUTURE.md「优化方向」登记项）：
# 逐条 _text_similarity 走 difflib.SequenceMatcher，是**同步**计算且复杂度不低；
# 题库规模小的时候全量精排最保召回，破千后必须先用学科/年级做 SQL 前置过滤，
# 并给候选集加硬上限，否则一次搜题会把事件循环占住、拖慢所有接口。
_SIMILARITY_BANK_THRESHOLD = 1000     # 超过此规模才启用前置过滤
_SIMILARITY_CANDIDATE_LIMIT = 1200    # 单次比对候选数硬上限


class AddToBankRequest(BaseModel):
    question_id: str
    bank: str = "default"
    user_hint: str = ""
    tags: list[str] = Field(default_factory=list, max_length=50)
    grade: str = ""


class SearchMatchRequest(BaseModel):
    question_id: str = Field(min_length=1, max_length=64)
    mode: Optional[str] = Field(default=None, pattern=r"^(text|ai|hybrid)$")


def _has_supported_image_signature(raw: bytes) -> bool:
    return (raw.startswith(b"\xff\xd8\xff") or raw.startswith(b"\x89PNG\r\n\x1a\n")
            or (len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))

router = APIRouter(prefix="/api/search", tags=["search"])
logger = get_logger()
def _text_similarity(a: str, b: str) -> float:
    """基于 difflib 的文本相似度，返回 0-1 之间的值。"""
    if not a or not b:
        return 0.0
    a = re.sub(r"\s+", "", str(a))[:800]
    b = re.sub(r"\s+", "", str(b))[:800]
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _preview(html: str, ocr: str, length: int = 120) -> str:
    """生成题目预览文本。"""
    s = ocr or html or ""
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").strip()
    return s[:length] + ("..." if len(s) > length else "")


async def _get_search_mode() -> str:
    """读取系统设置中的搜题模式，默认 text。"""
    try:
        s = load_settings()
        mode = s.get("search_mode", "text")
        if mode in ("text", "ai", "hybrid"):
            return mode
    except Exception:
        pass
    return "text"


async def _read_search_image(file: UploadFile) -> tuple[bytes, str, str]:
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, detail="请上传图片文件（JPG/PNG/WEBP）")
    # 分块读 + 累计上限（FUTURE.md 优化方向）：原为「先全量 read 后验大小」，
    # 判定只是事后拒绝、内存峰值已经发生。与 ocr.py / correction.py 共用同一实现。
    raw = await read_upload_limited(
        file, MAX_SEARCH_IMAGE_SIZE,
        too_large_detail="单张图片不能超过 10MB",
        empty_detail="上传图片为空",
    )
    if not _has_supported_image_signature(raw):
        raise HTTPException(400, detail="文件内容不是有效的 JPG、PNG 或 WEBP 图片")
    ext = (os.path.splitext(file.filename or "image.jpg")[1] or ".jpg").lower()
    if ext not in ALLOWED_SEARCH_EXTS:
        raise HTTPException(400, detail="仅支持 JPG、PNG 或 WEBP 图片")
    return raw, ext, file.filename or "image.jpg"


async def _split_search_query(question_id: str, capture_mode: str | None = None) -> list[str]:
    """Split a multi-question OCR result into independently searchable query records."""
    from models.database import async_session

    async with async_session() as db:
        q = await db.get(Question, question_id)
        if not q:
            return []
        mode = normalize_capture_mode(capture_mode or getattr(q, "capture_mode", "single_question"))
        full_text = (q.ocr_text or "").strip()
        source_path = q.raw_image_path or ""
        subject, grade, tags = q.subject or "", q.grade or "", q.knowledge_tags or []
        source_type = q.source_type or "search_query"
        source_bank = q.bank or ("correction_queries" if source_type == "correction_query" else "search_queries")
        group_id = q.capture_group_id or q.id

    if mode == "single_question":
        async with async_session() as db:
            root = await db.get(Question, question_id)
            if not root:
                return []
            root.status = "search_done"
            task_result = await db.execute(
                select(ProcessingTask).where(ProcessingTask.question_id == question_id)
                .order_by(ProcessingTask.created_at.desc()).limit(1)
            )
            task = task_result.scalar_one_or_none()
            if task:
                task.result = {**(task.result or {}), "capture_mode": mode,
                               "split_question_ids": [question_id], "split_count": 1}
            await db.commit()
        return [question_id]

    items = []
    if len(full_text) >= 20:
        if len(full_text) > 24000:
            raise ValueError("整页/整卷 OCR 超过自动拆题容量，请减少单次页数，避免后半部分被截断")
        scope = "一页或同一页的多张补充照片" if mode == "single_page" else "按上传顺序排列的整份试卷多页照片"
        prompt = (
            f"以下 OCR 来自{scope}。请按原始顺序拆成可独立搜索或批改的完整题目。"
            "大题背景应复制到依赖它的小题中，不要把同一道题的多个小问错误拆开，"
            "也不要补写图片中不存在的题目。输出 JSON："
            '{"questions":[{"text":"可独立处理的完整题干与作答"}]}。若只有一道题也返回一个元素。\n'
            f"OCR 文本：\n{full_text}"
        )
        try:
            result = await ai_service.deepseek_json(
                [{"role": "user", "content": prompt}], max_tokens=16384, scope="solve"
            )
            raw_items = result.get("questions", []) if isinstance(result, dict) else []
            max_items = 30 if mode == "single_page" else 80
            if len(raw_items) > max_items:
                raise ValueError(
                    f"识别到 {len(raw_items)} 道题，超过当前模式单次最多 {max_items} 道的安全上限；"
                    "请减少本次页数后重新上传，避免静默漏题"
                )
            for item in raw_items:
                text = item.get("text", "") if isinstance(item, dict) else str(item)
                text = text.strip()
                if text:
                    items.append(text)
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("Search question splitting failed for %s: %s", question_id, exc)
    split_detected = len(items) >= 2
    if not split_detected:
        items = [full_text]

    ids = [] if split_detected else [question_id]
    child_folders: list[str] = []
    async with async_session() as db:
        try:
            root = await db.get(Question, question_id)
            if not root:
                return []
            # 多题时根记录保留完整 OCR 作为可追溯容器；独立子题另建记录。
            if not split_detected:
                root.ocr_text = items[0]
            root.status = "search_done"
            root.capture_mode = mode
            root.capture_group_id = group_id
            root.capture_index = 0
            child_items = items if split_detected else []
            for index, text in enumerate(child_items):
                child_id = gen_id()
                child_folder = os.path.join(QUESTIONS_DIR, child_id)
                os.makedirs(child_folder, exist_ok=False)
                child_folders.append(child_folder)
                child_path = ""
                if source_path and os.path.isfile(source_path):
                    try:
                        # 校验 source_path 必须位于合法题目目录内，防止旧库篡改路径
                        src_real = os.path.realpath(source_path)
                        qdir_real = os.path.realpath(QUESTIONS_DIR)
                        if os.path.commonpath([qdir_real, src_real]) == qdir_real:
                            ext = os.path.splitext(source_path)[1].lower() or ".jpg"
                            child_path = os.path.join(child_folder, f"original{ext}")
                            shutil.copy2(source_path, child_path)
                        else:
                            logger.warning("Skipped unsafe source copy for %s: %s", child_id, source_path)
                    except (OSError, ValueError) as _e:
                        logger.warning("Source path validation failed for %s: %s", child_id, _e)
                db.add(Question(
                    id=child_id, folder_path=child_folder, raw_image_path=child_path,
                    status="search_done", source_type=source_type, bank=source_bank,
                    ocr_text=text, subject=subject, grade=grade, knowledge_tags=tags,
                    capture_mode=mode, capture_group_id=group_id, capture_index=index,
                    multi_images=list(root.multi_images or []), image_roles=list(root.image_roles or []),
                ))
                ids.append(child_id)
            task_result = await db.execute(
                select(ProcessingTask).where(ProcessingTask.question_id == question_id)
                .order_by(ProcessingTask.created_at.desc()).limit(1)
            )
            task = task_result.scalar_one_or_none()
            if task:
                task.result = {**(task.result or {}), "search_only": True,
                               "capture_mode": mode, "split_question_ids": ids,
                               "split_count": len(ids)}
            await db.commit()
        except Exception:
            await db.rollback()
            for folder in child_folders:
                shutil.rmtree(folder, ignore_errors=True)
            raise
    return ids


async def _ai_rank_similarity(query_ocr: str, candidates: List[dict]) -> List[tuple]:
    """调用 AI 对候选题与查询题进行相似度评分。返回 [(idx, score), ...]。"""
    if not candidates:
        return []
    # 构建对比 prompt
    pairs = []
    for i, c in enumerate(candidates):
        text = c.get("ocr_text") or c.get("question_html") or ""
        text = re.sub(r"<[^>]+>", "", text).replace("&nbsp;", " ").strip()[:300]
        pairs.append(f"[{i}] {text}")
    query_text = re.sub(r"<[^>]+>", "", query_ocr).replace("&nbsp;", " ").strip()[:300]
    prompt = (
        "你是题目相似度评估专家。请判断以下候选题目与查询题目的相似程度。\n\n"
        f"【查询题目】\n{query_text}\n\n"
        "【候选题目】\n" + "\n\n".join(pairs) + "\n\n"
        "请输出 JSON 对象，scores 数组中的每个元素包含 index（候选序号）和 score（0-1 的相似度分数，1 表示完全相同）：\n"
        '{"scores":[{"index":0,"score":0.95},{"index":1,"score":0.42}]}\n'
        "只输出 JSON，不要其他解释。"
    )
    try:
        result = await ai_service.deepseek_json(
            [{"role": "user", "content": prompt}],
            max_tokens=1024, scope="solve"
        )
        scores = []
        raw_scores = result.get("scores", []) if isinstance(result, dict) else result
        if isinstance(raw_scores, list):
            for item in raw_scores:
                if isinstance(item, dict):
                    idx = item.get("index")
                    score = item.get("score", 0)
                    try:
                        idx = int(idx)
                        score = float(score)
                        # NaN 与任何值比较恒为 False，min/max 钳制对 NaN 失效反而得满分
                        # （FreqErr [NaN 校验绕过]）；非有限分数直接跳过该候选
                        if not math.isfinite(score):
                            continue
                        if 0 <= idx < len(candidates):
                            scores.append((idx, max(0.0, min(1.0, score))))
                    except (TypeError, ValueError):
                        continue
        return scores
    except Exception as e:
        logger.warning("AI rank similarity failed: %s", e)
        return []


async def _find_matches(db: AsyncSession, query_id: str, mode: str = None, top_k: int = 3) -> dict:
    """为查询题寻找最相似的题库题目。"""
    mode = mode or await _get_search_mode()
    q = await db.get(Question, query_id)
    if not q:
        raise HTTPException(404, detail="查询题目不存在")
    if q.source_type not in ("search_query", "correction_query"):
        raise HTTPException(400, detail="该题目不是搜题/批改查询记录")
    if q.status != "search_done":
        raise HTTPException(400, detail="查询题尚未完成 OCR，请稍后再试")

    query_ocr = q.ocr_text or ""
    if not query_ocr.strip():
        raise HTTPException(422, detail="OCR 未识别到可用于搜题的文字")
    query_subject = q.subject or ""
    query_grade = q.grade or ""
    query_tags = set(q.knowledge_tags or [])

    # 候选：已完成的正式题目，排除其他搜题临时记录
    base_stmt = (
        select(Question.id, Question.ocr_text, Question.question_html,
               Question.subject, Question.grade, Question.knowledge_tags,
               Question.raw_image_path)
        .where(Question.status == "done")
        .where(or_(
            Question.is_resolved.is_(True),
            Question.audit_flags.is_(None),
            ~Question.audit_flags.cast(Text).ilike("%question_challenge_high%"),
        ))
        .where(or_(Question.source_type.is_(None),
                   ~Question.source_type.in_(["search_query", "correction_query"])))
        .order_by(Question.created_at.desc())
    )

    bank_size = int((await db.execute(
        select(func.count()).select_from(Question).where(Question.status == "done")
    )).scalar() or 0)

    stmt = base_stmt
    narrowed = False
    if bank_size > _SIMILARITY_BANK_THRESHOLD:
        if query_subject:
            stmt = stmt.where(Question.subject == query_subject)
            narrowed = True
        if query_grade:
            stmt = stmt.where(Question.grade == query_grade)
            narrowed = True
    stmt = stmt.limit(_SIMILARITY_CANDIDATE_LIMIT)

    rows = (await db.execute(stmt)).fetchall()
    if not rows and narrowed:
        # 前置过滤过严（学科/年级标注缺失或标错）→ 回退到不限科级的最近 N 条。
        # 宁可慢一次，也不能给用户「一道相似的都找不到」。
        rows = (await db.execute(
            base_stmt.limit(_SIMILARITY_CANDIDATE_LIMIT)
        )).fetchall()

    candidates = []
    for row in rows:
        if row.id == query_id:
            continue
        cand_ocr = row.ocr_text or ""
        if not cand_ocr:
            continue
        # 文本相似度作为基础分
        text_score = _text_similarity(query_ocr, cand_ocr)
        # 标签加分
        tag_bonus = 0.0
        cand_tags = set(row.knowledge_tags or [])
        if query_tags and cand_tags:
            tag_bonus = min(0.15, len(query_tags & cand_tags) * 0.05)
        # 学科/年级加分
        meta_bonus = 0.0
        if query_subject and row.subject == query_subject:
            meta_bonus += 0.05
        if query_grade and row.grade == query_grade:
            meta_bonus += 0.05
        base_score = min(1.0, text_score + tag_bonus + meta_bonus)
        candidates.append({
            "row": row,
            "text_score": text_score,
            "base_score": base_score,
        })

    if mode == "text":
        candidates.sort(key=lambda x: x["base_score"], reverse=True)
    elif mode == "ai":
        candidates.sort(key=lambda x: x["base_score"], reverse=True)
        top_candidates = candidates[:20]
        ai_scores = await _ai_rank_similarity(query_ocr, [{
            "ocr_text": c["row"].ocr_text,
            "question_html": c["row"].question_html,
        } for c in top_candidates])
        score_map = {idx: score for idx, score in ai_scores}
        for i, c in enumerate(top_candidates):
            c["ai_score"] = score_map.get(i, c["base_score"])
            c["base_score"] = c["ai_score"]
        candidates = top_candidates
        candidates.sort(key=lambda x: x["base_score"], reverse=True)
    elif mode == "hybrid":
        # 先用文本粗排取 Top 20，再用 AI 精排
        candidates.sort(key=lambda x: x["base_score"], reverse=True)
        top_candidates = candidates[:20]
        ai_scores = await _ai_rank_similarity(query_ocr, [{
            "ocr_text": c["row"].ocr_text,
            "question_html": c["row"].question_html,
        } for c in top_candidates])
        score_map = {idx: score for idx, score in ai_scores}
        for i, c in enumerate(top_candidates):
            ai_score = score_map.get(i, c["base_score"])
            # 混合：AI 分数为主，文本分数兜底
            c["base_score"] = ai_score * 0.7 + c["text_score"] * 0.3
        candidates = top_candidates
        candidates.sort(key=lambda x: x["base_score"], reverse=True)
    else:
        candidates.sort(key=lambda x: x["base_score"], reverse=True)

    matches = []
    for c in candidates[:top_k]:
        row = c["row"]
        raw_name = os.path.basename(row.raw_image_path) if row.raw_image_path else "original.jpg"
        thumb_url = f"/storage/questions/{row.id}/{raw_name}"
        matches.append({
            "question_id": row.id,
            "score": round(c["base_score"], 3),
            "subject": row.subject or "",
            "grade": row.grade or "",
            "tags": row.knowledge_tags or [],
            "preview": _preview(row.question_html, row.ocr_text),
            "raw_image_path": thumb_url,
        })

    threshold = MATCH_THRESHOLDS.get(mode, MATCH_THRESHOLDS["text"])
    return {
        "query_id": query_id,
        "mode": mode,
        "matches": matches,
        "confidence_threshold": threshold,
        "confident": bool(matches and matches[0]["score"] >= threshold),
    }


@router.post("/upload")
async def upload_search_image(
    file: Optional[UploadFile] = File(None),
    files: Optional[List[UploadFile]] = File(None),
    capture_mode: str = Form("single_question"),
    db: AsyncSession = Depends(get_db)
):
    """上传一组搜题图片，按单题、单页或整卷模式识别。"""
    try:
        mode = normalize_capture_mode(capture_mode)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    selected = ([file] if file is not None else []) + list(files or [])
    if not selected:
        raise HTTPException(400, detail="请上传至少一张图片")
    if len(selected) > MAX_CAPTURE_IMAGES:
        raise HTTPException(400, detail=f"一次最多上传 {MAX_CAPTURE_IMAGES} 张图片")
    uploads = [await _read_search_image(item) for item in selected]
    if sum(len(item[0]) for item in uploads) > MAX_CAPTURE_TOTAL_SIZE:
        raise HTTPException(413, detail="本次图片总大小不能超过 60MB")

    qid = gen_id()
    folder = os.path.join(QUESTIONS_DIR, qid)
    os.makedirs(folder, exist_ok=False)

    desc = {"question_id": qid, "type": "search_ocr", "capture_mode": mode}
    from routers.ocr import _run_bg, _save_task_state, _clear_task_state
    try:
        multi_images = []
        image_roles = []
        for index, (raw, ext, filename) in enumerate(uploads):
            path = os.path.join(folder, f"original_{index}{ext}")
            with open(path, "wb") as w:
                w.write(raw)
                w.flush()
                os.fsync(w.fileno())
            multi_images.append({"path": path, "filename": filename, "role": "question"})
            image_roles.append({"path": path, "role": "question", "order": index})
        primary_path = multi_images[0]["path"]

        db.add(Question(
            id=qid, folder_path=folder, raw_image_path=primary_path,
            status="search_staged", source_type="search_query", bank="search_queries",
            multi_images=multi_images, image_roles=image_roles, capture_mode=mode,
            capture_group_id=qid, capture_index=0,
        ))
        # 先落盘恢复描述，再提交可见的运行态，关闭“提交后进程退出”的丢任务窗口。
        _save_task_state(desc)
        await db.commit()
    except Exception:
        await db.rollback()
        _clear_task_state(qid)
        shutil.rmtree(folder, ignore_errors=True)
        raise
    _run_bg(_run_search_ocr(qid, mode), desc)

    return {"question_id": qid, "message": "已上传，正在识别", "status": "search_staged",
            "capture_mode": mode, "image_count": len(uploads)}


@router.get("/status/{question_id}")
async def search_status(
    question_id: str,
    db: AsyncSession = Depends(get_db)
):
    """查询搜题 OCR 状态。"""
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, detail="查询题目不存在")
    # 同时查询最近任务状态
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
        "capture_mode": getattr(q, "capture_mode", "single_question") or "single_question",
        "capture_group_id": getattr(q, "capture_group_id", "") or q.id,
        "capture_index": getattr(q, "capture_index", 0) or 0,
        "task": {
            "status": task.status,
            "progress": task.progress,
            "error_message": task.error_message or "",
            "result": task.result or {},
        } if task else None,
        "split_question_ids": ((task.result or {}).get("split_question_ids", [question_id])
                               if task else [question_id]),
    }


@router.post("/match")
async def search_match(
    data: SearchMatchRequest,
    db: AsyncSession = Depends(get_db)
):
    """对已 OCR 完成的查询题进行相似匹配，返回 Top 3。"""
    query_id = data.question_id.strip()
    mode = data.mode
    result = await _find_matches(db, query_id, mode=mode, top_k=3)
    return result


@router.post("/add-to-bank")
async def add_search_to_bank(
    data: AddToBankRequest,
    db: AsyncSession = Depends(get_db)
):
    """将未匹配的搜题查询转为正式题目并加入处理队列。"""
    query_id = data.question_id.strip()
    bank = (data.bank or "default").strip()
    user_hint = data.user_hint.strip()
    user_tags = [tag.strip() for tag in data.tags if tag.strip()]
    user_grade = data.grade.strip()
    if not bank:
        bank = "default"
    if not BANK_NAME_RE.match(bank):
        raise HTTPException(400, detail="题库名称只允许 1-32 位中文、字母、数字、下划线或中划线")

    # 读取题目数据用于校验与恢复参数（SQLite 忽略 FOR UPDATE，仅作读取）
    from sqlalchemy import select, update
    r = await db.execute(select(Question).where(Question.id == query_id))
    q = r.scalar_one_or_none()
    if not q:
        raise HTTPException(404, detail="查询题目不存在")
    if q.source_type != "search_query":
        raise HTTPException(400, detail="该题目不是搜题查询记录")
    if q.status != "search_done":
        raise HTTPException(400, detail="OCR 尚未完成，请稍后再试")

    # 保留已有 OCR 结果，避免重复识别
    existing_ocr_text = q.ocr_text or ""
    existing_subject = q.subject or ""
    existing_grade = q.grade or user_grade or ""
    existing_tags = list(q.knowledge_tags or [])
    if user_tags:
        existing_tags = list({t.strip() for t in (existing_tags + user_tags) if t.strip()})

    # 原子抢占：仅当仍处 search_done 时置 pending（FreqErr [retry 非原子 claim]），
    # rowcount != 1 说明已被并发请求抢先转入，避免双任务并发处理同一题
    claim_values = {"status": "pending", "source_type": "search_unmatched", "bank": bank}
    if user_grade:
        claim_values["grade"] = user_grade
    if user_tags:
        claim_values["knowledge_tags"] = existing_tags
    if user_hint:
        claim_values["user_hint"] = user_hint
    claimed = await db.execute(
        update(Question)
        .where(Question.id == query_id, Question.status == "search_done")
        .values(**claim_values)
    )
    if claimed.rowcount != 1:
        await db.rollback()
        raise HTTPException(status_code=409, detail="该题目已被其他请求转入题库，请刷新后查看")
    # 启动完整处理流程，复用已有 OCR 结果，并持久化恢复参数。
    # 注意：原子 UPDATE 后 q 的 ORM 属性已过期，desc 一律使用本地变量
    desc = {
        "question_id": query_id, "type": "process_existing_ocr",
        "user_hint": user_hint, "tags": existing_tags,
        "user_grade": user_grade or (existing_grade or ""), "bank": bank,
        "existing_ocr_text": existing_ocr_text,
        "existing_subject": existing_subject, "existing_grade": existing_grade,
        "existing_tags": existing_tags,
    }
    from routers.ocr import _run_bg, _save_task_state, _clear_task_state
    try:
        _save_task_state(desc)
        await db.commit()
    except Exception:
        await db.rollback()
        _clear_task_state(query_id)
        raise
    _run_bg(
        ocr_service.process_image(
            query_id, user_hint=desc["user_hint"], user_tags=desc["tags"],
            user_grade=desc["user_grade"], skip_ocr=True,
            existing_ocr_text=existing_ocr_text, existing_subject=existing_subject,
            existing_grade=existing_grade, existing_tags=existing_tags, search_only=False,
        ),
        desc,
    )

    return {"question_id": query_id, "message": "已加入题库并启动完整处理", "status": "pending"}


async def _run_search_ocr(question_id: str, capture_mode: str):
    """可由上传请求或启动恢复流程复用的搜题 OCR 任务。"""
    from models.database import async_session
    async with async_session() as db:
        q = await db.get(Question, question_id)
        if not q:
            raise ValueError("搜题记录不存在")
        multi_images = list(q.multi_images or [])
    await ocr_service.process_image(
        question_id, multi_images=multi_images, search_only=True,
        search_only_final_status=(
            "search_done" if capture_mode == "single_question" else "search_splitting"
        ),
    )
    await _split_search_query(question_id, capture_mode)
