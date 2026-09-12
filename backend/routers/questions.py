import os
import re
import json as _json
import shutil
import threading
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, Text, update
from sqlalchemy.orm import load_only
from pydantic import BaseModel, Field
from models.database import get_db
from models.models import Question, Paper, Correction, ProcessingTask, UploadSession, Note
from schemas.schemas import QuestionResponse, QuestionUpdate, QuestionListItem, escape_math_html
from config import (QUESTIONS_DIR, STORAGE_DIR, ENABLE_STRUCTURE_GRAPH,
                    ENABLE_COMPARISON_MODE, ENABLE_CALCULATOR, _atomic_write_json)
from logger import get_logger, log_error

PENDING_DIR = os.path.join(STORAGE_DIR, "pending")
os.makedirs(PENDING_DIR, exist_ok=True)
import weakref as _weakref_q
_question_chat_locks: _weakref_q.WeakValueDictionary[str, asyncio.Lock] = _weakref_q.WeakValueDictionary()
_question_chat_locks_lock = asyncio.Lock()

async def _get_chat_lock(qid: str) -> asyncio.Lock:
    async with _question_chat_locks_lock:
        lock = _question_chat_locks.get(qid)
        if lock is None:
            lock = asyncio.Lock()
            _question_chat_locks[qid] = lock
        return lock


from services.structure_graph_service import _sanitize_structure_graph, _sanitize_comparison_regions
from services.diagram_service import _is_valid_question_id
from services.ocr_service import _replace_diagram_markers

TASK_STATE_DIR = os.path.join(STORAGE_DIR, "task_states")


async def _get_question_for_update(db: AsyncSession, question_id: str):
    """使用 SELECT ... FOR UPDATE 获取题目，防止并发事务覆盖用户状态。
    注意：当前后端为 SQLite，SQLAlchemy 会静默忽略 with_for_update()；
    但 SQLite WAL 模式已保证写操作串行化，仍可避免并发覆盖。"""
    result = await db.execute(select(Question).where(Question.id == question_id).with_for_update())
    return result.scalar_one_or_none()


async def _apply_ai_structure_graph(q, new_sg, db):
    """将 AI 返回的结构图写入 question，但保留用户手动编辑版本并尊重已锁定状态。
    返回 True 表示已写入，False 表示因用户编辑/已锁定而未写入。"""
    if not ENABLE_STRUCTURE_GRAPH:
        return False
    await db.refresh(q, ['structure_graph', 'is_resolved'])
    if q.is_resolved:
        logger.info("Preserve structure_graph because question resolved for %s", q.id)
        return False
    current_sg = q.structure_graph or {}
    if isinstance(current_sg, dict) and current_sg.get("is_user_edited"):
        logger.info("Preserve user-edited structure_graph for %s", q.id)
        return False
    sanitized = _sanitize_structure_graph(new_sg)
    if sanitized:
        q.structure_graph = sanitized
        q.structure_graph_info = None
        return True
    return False


async def _apply_ai_comparison_regions(q, new_regions, db):
    """将 AI 返回的题目对比分区写入 question，但保留用户手动编辑版本并尊重已锁定状态。
    返回 True 表示已写入，False 表示因用户编辑/已解决而未写入。"""
    if not ENABLE_COMPARISON_MODE:
        return False
    await db.refresh(q, ['comparison_regions', 'is_resolved'])
    if q.is_resolved:
        logger.info("Preserve comparison_regions because question resolved for %s", q.id)
        return False
    current_cr = getattr(q, "comparison_regions", None) or {}
    if isinstance(current_cr, dict) and current_cr.get("is_user_edited"):
        logger.info("Preserve user-edited comparison_regions for %s", q.id)
        return False
    sanitized = _sanitize_comparison_regions(new_regions)
    if sanitized is not None:
        q.comparison_regions = sanitized
        return True
    return False


def _pending_path(qid: str) -> str:
    """所有 pending 读写都必须经过统一 ID 白名单和目录边界校验，
    防止 /questions/{question_id}/confirm-pending 或 cancel-pending
    携带 ..\\settings 之类路径把 pending 操作变成任意文件删除。"""
    if not isinstance(qid, str) or not _is_valid_question_id(qid):
        raise HTTPException(status_code=400, detail="非法的题目 ID")
    root = os.path.realpath(PENDING_DIR)
    path = os.path.realpath(os.path.join(root, f"{qid}.json"))
    try:
        if os.path.commonpath([root, path]) != root:
            raise HTTPException(status_code=400, detail="非法的题目 ID")
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的题目 ID")
    return path


def _save_pending(qid: str, data: dict):
    if not _is_valid_question_id(qid):
        raise ValueError("非法题目 ID")
    _atomic_write_json(_pending_path(qid), data)


def _load_pending(qid: str) -> dict:
    path = _pending_path(qid)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return data if isinstance(data, dict) else {}
        except (_json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            # pending 文件损坏时自愈：清掉坏文件并视作无待确认内容，而不是永久 500
            logger.warning("Corrupted pending file for %s removed: %s", qid, exc)
            _clear_pending(qid)
    return {}


def _clear_pending(qid: str):
    path = _pending_path(qid)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _default_diagram_places(count: int) -> list[str]:
    """AI 未返回 diagram_places 时，第一张图放题面，其余放解答。"""
    if count <= 0:
        return []
    return ["question"] + ["answer"] * (count - 1)


def _strip_unresolved_diagram_markers(html: str) -> str:
    """移除未成功生成图片后残留的 [[DIAGRAM:N]] 占位符，避免题面出现死标记。"""
    if not isinstance(html, str):
        return html
    return re.sub(r'\[\[DIAGRAM:\d+\]\]', '', html)


router = APIRouter(prefix="/api/questions", tags=["questions"])
logger = get_logger()


@router.get("/banks")
async def list_banks(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import func, distinct
    r = await db.execute(select(func.distinct(Question.bank)))
    banks = sorted([row[0] for row in r.fetchall() if row[0]])
    return {"banks": banks}


@router.get("", response_model=list[QuestionListItem])
async def list_questions(
    subject: str = Query(None, max_length=64),
    grade: str = Query(None, max_length=64),
    keyword: str = Query(None, max_length=500),
    status: str = Query("", max_length=32),
    bank: str = Query(None, max_length=64),
    region: str = Query(None, max_length=64),
    difficulty: str = Query(None, max_length=16),
    avg_score_min: float = Query(None),
    avg_score_max: float = Query(None),
    knowledge_tags: str = Query(None, max_length=2000),
    tags: str = Query(None, max_length=2000),
    limit: int = Query(50),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db)
):
    conditions = []
    if subject:
        conditions.append(Question.subject == subject)
    if grade:
        conditions.append(Question.grade == grade)
    if status:
        conditions.append(Question.status == status)
    if bank:
        conditions.append(Question.bank == bank)
    if region:
        conditions.append(Question.region == region)
    if difficulty:
        conditions.append(Question.difficulty == difficulty)
    if avg_score_min is not None:
        conditions.append(Question.avg_score >= avg_score_min)
    if avg_score_max is not None:
        conditions.append(Question.avg_score <= avg_score_max)

    # P1#2: 列表接口只加载 QuestionListItem 所需字段，排除 structure_graph / structure_graph_info 等大字段
    # 2026-07-31: 补充 user_hint / image_roles / multi_images / comparison_regions，避免序列化时懒加载失败
    list_columns = [
        Question.id, Question.folder_path, Question.subject, Question.grade,
        Question.knowledge_tags, Question.raw_image_path, Question.ocr_text,
        Question.question_html, Question.answer_html, Question.standard_answer,
        Question.question_type, Question.score_points_html, Question.diagrams,
        Question.diagram_places, Question.diagram_description, Question.status,
        Question.error_message, Question.source_type, Question.bank, Question.region,
        Question.difficulty,
        Question.avg_score, Question.audit_flags, Question.is_resolved,
        Question.handwriting_notes, Question.user_hint, Question.image_roles,
        Question.multi_images, Question.comparison_regions,
        Question.reference_svg_path, Question.reference_svg_status,
        Question.reference_svg_error,
        Question.created_at, Question.updated_at,
    ]
    # P1#6: 将知识点标签、关键词过滤下推到 SQL，避免内存二次过滤导致分页失真
    # tags 是 knowledge_tags 的别名，两者合并处理
    effective_tags = []
    if knowledge_tags:
        effective_tags.extend([t.strip() for t in knowledge_tags.split(",") if t.strip()])
    if tags:
        effective_tags.extend([t.strip() for t in tags.split(",") if t.strip()])
    if effective_tags:
        tag_conditions = []
        for t in effective_tags[:50]:
            # 带引号精确匹配，避免 "a" 误命中 "ab"
            escaped = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace('"', '\\"')
            tag_conditions.append(Question.knowledge_tags.cast(Text).like(f'%"{escaped}"%', escape="\\"))
        if tag_conditions:
            conditions.append(or_(*tag_conditions))
    if keyword:
        escaped_kw = str(keyword).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        kw = f"%{escaped_kw}%"
        conditions.append(
            or_(
                Question.ocr_text.ilike(kw, escape="\\"),
                Question.question_html.ilike(kw, escape="\\"),
                Question.knowledge_tags.cast(Text).ilike(kw, escape="\\"),
            )
        )

    # 参数边界：limit/offset 必须先夹紧，避免 SQLite 收到负值或超大整数而报错
    safe_limit = max(1, min(limit, 200))
    safe_offset = min(max(0, offset), 1_000_000)

    # 构建查询（list_columns 用于 load_only，limit/offset 使用夹紧后的安全值）
    query = (
        select(Question)
        .options(load_only(*list_columns))
        .where(and_(*conditions))
        .order_by(Question.created_at.desc())
        .limit(safe_limit)
        .offset(safe_offset)
    )
    result = await db.execute(query)
    questions = result.scalars().all()

    # 数据库已做 limit/offset，这里不再重复切片
    return questions


@router.get("/flagged/list")
async def list_flagged_questions(
    flag_type: str = Query(None, max_length=64),
    limit: int = Query(50),
    db: AsyncSession = Depends(get_db)
):
    """获取所有带有审计标记的题目（单查询避免 N+1）"""
    safe_limit = max(1, min(limit, 200))
    result = await db.execute(
        select(
            Question.id, Question.subject, Question.grade,
            Question.audit_flags, Question.ocr_text, Question.question_html
        )
        .where(Question.status == "done")
        .where(Question.is_resolved == False)
        .where(Question.audit_flags.isnot(None))
        .where(Question.audit_flags != [])  # 排除空标记行，避免占用查询配额造成深页欠载
        .limit(safe_limit * 3)
    )
    flagged = []
    for row in result.fetchall():
        qid, subject, grade, audit_flags, ocr_text, question_html = row
        flags = audit_flags if isinstance(audit_flags, list) else []
        if not flags:
            continue
        if flag_type:
            flags = [f for f in flags if isinstance(f, dict) and f.get("type") == flag_type]
            if not flags:
                continue
        flagged.append({
            "id": qid,
            "subject": subject,
            "grade": grade,
            "flags": flags,
            "preview": (ocr_text or question_html or "")[:80],
        })
        if len(flagged) >= safe_limit:
            break
    # total 语义：返回本次返回的条数，避免把截断长度冒充总数
    return {"flagged": flagged, "total": len(flagged)}


@router.get("/tags/list")
async def list_all_tags(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Question.knowledge_tags).where(Question.status == "done"))
    all_tags = set()
    for row in result.scalars().all():
        if row:
            for tag in row:
                all_tags.add(tag)
    return {"tags": sorted(all_tags)}


@router.get("/meta/options")
async def get_meta_options(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Question.subject, Question.grade).where(Question.status == "done")
    )
    subjects = set()
    grades = set()
    for subject, grade in result.fetchall():
        if subject:
            subjects.add(subject)
        if grade:
            grades.add(grade)
    return {
        "subjects": sorted(subjects),
        "grades": sorted(grades),
    }


@router.get("/{question_id}/diagrams")
async def get_diagrams(question_id: str, db: AsyncSession = Depends(get_db)):
    if not _is_valid_question_id(question_id):
        raise HTTPException(status_code=400, detail="非法的题目ID")
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    return {"diagrams": q.diagrams, "diagram_description": q.diagram_description or ""}


class QuestionFlagRequest(BaseModel):
    type: str = Field(default="other", max_length=64)
    reason: str = Field(default="用户手动标记", max_length=2_000)


@router.post("/{question_id}/flag")
async def flag_question_api(question_id: str, req: QuestionFlagRequest, db: AsyncSession = Depends(get_db)):
    """手动标记题目问题"""
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, detail="题目不存在")
    if getattr(q, "is_resolved", False):
        raise HTTPException(409, detail="题目已锁定；请先取消锁定再标记")
    from services.audit_service import flag_question
    await flag_question(question_id, req.type.strip() or "other", req.reason.strip() or "用户手动标记", auto=False)
    return {"message": "已标记"}


@router.post("/{question_id}/challenge")
async def challenge_question_api(question_id: str, db: AsyncSession = Depends(get_db)):
    """让独立解题 AI 主动质疑题干、图形和上传参考答案，不改写正文。"""
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    if q.status != "done":
        raise HTTPException(status_code=409, detail="题目尚未处理完成，暂时不能进行独立质疑")
    if q.is_resolved:
        raise HTTPException(status_code=403, detail="题目已锁定；请先取消锁定再重新质疑")

    reference_svg = ""
    reference_url = str(q.reference_svg_path or "")
    expected_prefix = f"/storage/questions/{question_id}/"
    if reference_url.startswith(expected_prefix) and reference_url.endswith(".svg"):
        disk_path = os.path.abspath(os.path.join(STORAGE_DIR, reference_url[len("/storage/"):]))
        question_folder = os.path.abspath(q.folder_path or os.path.join(STORAGE_DIR, "questions", question_id))
        try:
            if os.path.commonpath([question_folder, disk_path]) == question_folder and os.path.isfile(disk_path):
                with open(disk_path, "r", encoding="utf-8") as stream:
                    reference_svg = stream.read(100_001)[:100_000]
        except (OSError, ValueError):
            reference_svg = ""

    from services.ai_service import ai_service
    from services.audit_service import set_question_challenge
    from services.question_challenge import combine_question_challenges
    try:
        raw = await ai_service.deepseek_challenge_question(
            ocr_text=q.ocr_text or "",
            question_html=q.question_html or "",
            answer_html=q.answer_html or "",
            standard_answer=q.standard_answer or "",
            subject=q.subject or "",
            grade=q.grade or "",
            reference_svg=reference_svg,
            source_reference=q.handwriting_notes or "",
            user_context=q.user_hint or "",
        )
    except Exception as exc:
        logger.warning("Question challenge failed for %s: %s", question_id, exc)
        message = str(exc).lower()
        if "401" in message or "authentication" in message or "api key" in message or "illegal header" in message:
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型配置")
        raise HTTPException(status_code=502, detail="AI 质疑失败，请稍后重试")

    if isinstance(raw, dict):
        raw = dict(raw)
        raw["source"] = "manual_challenge"
    challenge = combine_question_challenges(raw)
    challenge = await set_question_challenge(question_id, challenge, auto=True)
    await db.refresh(q, ["audit_flags"])
    return {"challenge": challenge, "audit_flags": q.audit_flags or []}


@router.post("/{question_id}/resolve")
async def resolve_question_api(question_id: str, db: AsyncSession = Depends(get_db)):
    """标记题目为已解决（AI不再修改）"""
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    if q.status != "done":
        raise HTTPException(status_code=409, detail="题目尚未处理完成，不能锁定为已解决")
    from services.audit_service import set_resolved
    await set_resolved(question_id, True)
    # 锁定后旧待确认修改立即失效，避免解锁后 confirm-pending 应用过期内容
    _clear_pending(question_id)
    return {"message": "已标记为已解决"}


@router.post("/{question_id}/unresolve")
async def unresolve_question_api(question_id: str, db: AsyncSession = Depends(get_db)):
    """取消已解决标记"""
    if not await db.get(Question, question_id):
        raise HTTPException(status_code=404, detail="题目不存在")
    from services.audit_service import set_resolved
    await set_resolved(question_id, False)
    _clear_pending(question_id)
    return {"message": "已取消已解决标记"}





class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=50000)


_chat_file_lock = threading.RLock()


def _chat_path(question_id: str) -> str:
    if not _is_valid_question_id(question_id):
        raise HTTPException(400, "题目 ID 无效")
    root = os.path.realpath(QUESTIONS_DIR)
    path = os.path.realpath(os.path.join(root, question_id, "chat.json"))
    if os.path.commonpath([root, path]) != root:
        raise HTTPException(400, "题目 ID 无效")
    return path


def _save_chat(question_id: str, messages: list):
    path = _chat_path(question_id)
    try:
        with _chat_file_lock:
            _atomic_write_json(path, messages[-100:])
    except Exception as exc:
        log_error("question_chat", f"Failed to save chat for {question_id}: {exc}")
        raise

def _load_chat(question_id: str) -> list:
    path = _chat_path(question_id)
    if os.path.exists(path):
        try:
            with _chat_file_lock, open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return data if isinstance(data, list) else []
        except Exception as exc:
            log_error("question_chat", f"Failed to load chat for {question_id}: {exc}")
    return []

@router.get("/{question_id}/chat-history")
async def get_chat_history(question_id: str, db: AsyncSession = Depends(get_db)):
    if not await db.get(Question, question_id):
        raise HTTPException(404, "题目不存在")
    return {"messages": _load_chat(question_id)}

async def _chat_question_impl(question_id: str, req: ChatRequest, db: AsyncSession):
    q = await _get_question_for_update(db, question_id)
    if not q: raise HTTPException(404, "题目不存在")
    from services.ai_service import ai_service
    from services.user_profile import load_profile
    from services.diagram_service import diagram_service as ds
    import re
    profile = load_profile()
    style = profile.get("style_notes", "") or profile.get("notation_preferences", "")
    info = _json.dumps({"id":q.id,"subject":q.subject,"grade":q.grade,"ocr_text":q.ocr_text,"knowledge_tags":q.knowledge_tags,"region":q.region or "","avg_score":q.avg_score or 0}, ensure_ascii=False, default=str)
    msg_lower = req.message.strip()
    messages = _load_chat(question_id)
    messages.append({"role":"user","content":req.message})
    action = None
    tool_result = None

    is_agent = any(kw in msg_lower for kw in ["生成参数","组卷参数","一键组卷","跳转组卷","更新偏好","排版偏好","符号偏好","更新画像","更新配置","修改题面","修改解答","修改答案","加图","画图","重绘","重新画","怎么解","如何解","讲解","讲一下","解释"])

    if is_agent:
        system_prompt = (
            "你是学习搭子AI助手，运行在单个题目的上下文中。你有这些工具能力：\n"
            "1. modify_question: 修改题面HTML内容\n"
            "2. modify_answer: 修改解答HTML内容\n"
            "3. draw_diagram: 画示意图（在reply中插入 [[DIAGRAM:中文几何描述]]）\n"
            "4. rewrite_answer: 根据原解答重写/优化解答\n"
            "5. set_style: 设置排版偏好\n"
            "6. jump_paper: 提取组卷参数跳转组卷\n"
            "7. calculator: 精确计算（需要数值计算时可在推理中自动调用）\n\n"
            "返回JSON格式:\n"
            '{"reply":"给用户的回复","action":{"type":"操作类型(如modify_question/modify_answer/draw_diagram/rewrite_answer/set_style/jump_paper/calculator)","data":{...}}}\n\n'
            f"当前题目信息: {info}\n"
            f"用户排版偏好: {style}\n"
            f"用户消息: {req.message}\n\n"
            "注意: 如果用户要求修改题面/解答/画图，直接用对应工具类型返回。纯聊天不返回action。"
        )
        result = await ai_service.deepseek_chat(
            [{"role": "system", "content": system_prompt}],
            temperature=0.3, max_tokens=8192,
            scope="chat", enable_calc=ENABLE_CALCULATOR
        )
        try:
            parsed = ai_service._extract_json(result)
            reply = parsed.get("reply", result)
            action = parsed.get("action")
            if action:
                atype = action.get("type", "")
                adata = action.get("data", {})
                if atype == "set_style":
                    new_style = adata.get("style_notes", "")
                    if new_style and "确认修改偏好" in msg_lower:
                        from services.user_profile import save_profile
                        profile["style_notes"] = new_style
                        save_profile(profile)
                        reply += "\n\n排版偏好已更新。"
                    elif new_style:
                        tool_result = {"type": "pending_confirmation", "operation": "set_style"}
                        reply += "\n\n为防止 AI 误判修改偏好，请发送“确认修改偏好”并附上完整要求。"
                elif atype == "modify_question" and adata.get("question_html"):
                    # Save as pending with confirmation
                    pending = {
                        "type": "modify_question",
                        "previous": {
                            "question_html": q.question_html,
                            "score_points_html": q.score_points_html,
                            "answer_html": q.answer_html,
                            "standard_answer": q.standard_answer,
                        },
                        "proposed": {"question_html": adata["question_html"]},
                    }
                    _save_pending(question_id, pending)
                    tool_result = {"type": "pending", "pending_type": "modify_question"}
                    reply += "\n\nAI 提议修改题面，请在确认后应用。\n\n"
                    # Add a summary of the change for frontend display
                    reply += f'<div style="padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--subtle);font-size:13px">'
                    reply += f'<b>新题面前200字预览:</b><br>{adata["question_html"][:200]}...</div>'
                elif atype == "modify_answer" and adata.get("answer_html"):
                    raw = adata["answer_html"]
                    proposed_answer = raw
                    proposed_score = ""
                    if "<!-- SCORE_SPLIT -->" in raw:
                        parts = raw.split("<!-- SCORE_SPLIT -->", 1)
                        proposed_score = parts[0].strip()
                        proposed_answer = parts[1].strip()
                    pending = {
                        "type": "modify_answer",
                        "previous": {
                            "question_html": q.question_html,
                            "score_points_html": q.score_points_html,
                            "answer_html": q.answer_html,
                            "standard_answer": q.standard_answer,
                        },
                        "proposed": {
                            "answer_html": proposed_answer,
                            "score_points_html": proposed_score,
                            "standard_answer": adata.get("standard_answer", ""),
                        },
                    }
                    _save_pending(question_id, pending)
                    tool_result = {"type": "pending", "pending_type": "modify_answer"}
                    reply += "\n\nAI 提议修改解答，请在确认后应用。\n\n"
                    reply += f'<div style="padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--subtle);font-size:13px">'
                    reply += f'<b>新解答前200字预览:</b><br>{proposed_answer[:200]}...</div>'
                elif atype == "rewrite_answer":
                    result2 = await ai_service.deepseek_regenerate_question(
                        q.ocr_text or "", q.subject or "", q.grade or "",
                        adata.get("instruction", "请优化解答"), style,
                        answer_html=q.answer_html or "", question_html=q.question_html or "",
                        standard_answer=q.standard_answer or ""
                    )
                    answer_raw = result2.get("answer_html", "")
                    diagram_prompts = result2.get("diagram_prompts", [])
                    new_diagrams = list(q.diagrams or [])
                    marker_diagrams: list[dict] = []
                    if diagram_prompts:
                        from services.diagram_service import diagram_service as ds
                        places = result2.get("diagram_places") or _default_diagram_places(len(diagram_prompts))
                        for i, dp in enumerate(diagram_prompts):
                            # 单图失败不拖垮整次重写：记录日志后以空路径占位，
                            # 保持 marker_diagrams 与 [[DIAGRAM:i]] 下标对齐
                            try:
                                path = await ds.generate_diagram(q.id, dp, len(new_diagrams))
                            except Exception as diag_exc:
                                log_error("questions.diagram", f"agent rewrite diagram failed for {q.id}: {diag_exc}")
                                path = None
                            if path:
                                place = places[i] if i < len(places) else "question"
                                new_item = {"path": path, "place": place}
                                new_diagrams.append(new_item)
                                marker_diagrams.append(new_item)
                            else:
                                marker_diagrams.append({"path": "", "place": places[i] if i < len(places) else "question"})
                    diagrams = new_diagrams
                    proposed_answer = ""
                    proposed_score = ""
                    if "<!-- SCORE_SPLIT -->" in answer_raw:
                        parts = answer_raw.split("<!-- SCORE_SPLIT -->", 1)
                        proposed_score = _replace_diagram_markers(parts[0].strip(), marker_diagrams)
                        proposed_answer = _replace_diagram_markers(parts[1].strip(), marker_diagrams)
                    else:
                        proposed_answer = _replace_diagram_markers(answer_raw, marker_diagrams)
                    proposed_question = _replace_diagram_markers(result2.get("question_html", q.question_html), marker_diagrams)
                    pending = {
                        "type": "rewrite_answer",
                        "previous": {
                            "question_html": q.question_html,
                            "score_points_html": q.score_points_html,
                            "answer_html": q.answer_html,
                            "standard_answer": q.standard_answer,
                            "diagrams": q.diagrams or [],
                        },
                        "proposed": {
                            "question_html": proposed_question,
                            "answer_html": proposed_answer,
                            "score_points_html": proposed_score,
                            "standard_answer": result2.get("standard_answer", q.standard_answer),
                            "diagrams": diagrams,
                            "structure_graph": result2.get("structure_graph"),
                        },
                    }
                    _save_pending(question_id, pending)
                    tool_result = {"type": "pending", "pending_type": "rewrite_answer"}
                    reply += "\n\nAI 已重写解答，请在确认后应用。"
                elif atype == "jump_paper":
                    from services.config_service import config_service
                    if not adata:
                        adata = {}
                    if not adata.get("subject"):
                        adata["subject"] = q.subject
                    if not adata.get("grade"):
                        adata["grade"] = q.grade
                    if not adata.get("knowledge_tags"):
                        adata["knowledge_tags"] = q.knowledge_tags
                    cid = await config_service.save_paper_config(adata)
                    action["data"] = adata
                    action["saved_config"] = cid
                    tool_result = {"type": "jump_paper", "data": adata}
                    reply += f"\n\n组卷参数已保存（#{cid[:6]}），正在跳转组卷..."
        except Exception as e:
            logger.warning("Agent parse failed for question %s: %s", question_id, e)
            reply = result
    elif any(kw in msg_lower for kw in ["覆盖答案","保存答案","更新答案","重写答案","标准化","规范题面","重新生成题面"]):
        if getattr(q, "is_resolved", False):
            reply = "已锁定的题目不支持覆盖答案，请先取消锁定。"
            tool_result = {"type": "error", "message": reply}
        else:
            result = await ai_service.deepseek_regenerate_question(q.ocr_text or "", q.subject or "", q.grade or "", "请用标准格式生成完整解答，标准化题面", style, answer_html=q.answer_html or "", question_html=q.question_html or "", standard_answer=q.standard_answer or "")
            answer_raw = result.get("answer_html", "")
            # Generate diagrams if AI returned prompts
            diagram_prompts = result.get("diagram_prompts", [])
            new_diagrams = list(q.diagrams or [])
            marker_diagrams: list[dict] = []
            if diagram_prompts:
                from services.diagram_service import diagram_service as ds
                places = result.get("diagram_places") or _default_diagram_places(len(diagram_prompts))
                for i, dp in enumerate(diagram_prompts):
                    # 同上：单图失败占位保序，不拖垮整次对话
                    try:
                        path = await ds.generate_diagram(q.id, dp, len(new_diagrams))
                    except Exception as diag_exc:
                        log_error("questions.diagram", f"rewrite diagram failed for {q.id}: {diag_exc}")
                        path = None
                    if path:
                        place = places[i] if i < len(places) else "question"
                        new_item = {"path": path, "place": place}
                        new_diagrams.append(new_item)
                        marker_diagrams.append(new_item)
                    else:
                        marker_diagrams.append({"path": "", "place": places[i] if i < len(places) else "question"})
            diagrams = new_diagrams
            proposed_score = ""
            if "<!-- SCORE_SPLIT -->" in answer_raw:
                parts = answer_raw.split("<!-- SCORE_SPLIT -->", 1)
                proposed_score = _replace_diagram_markers(parts[0].strip(), marker_diagrams)
                proposed_answer = _replace_diagram_markers(parts[1].strip(), marker_diagrams)
            else:
                proposed_answer = _replace_diagram_markers(answer_raw, marker_diagrams)
            pending = {
                "type": "rewrite_answer",
                "previous": {"question_html": q.question_html, "answer_html": q.answer_html,
                             "score_points_html": q.score_points_html,
                             "standard_answer": q.standard_answer, "diagrams": q.diagrams or []},
                "proposed": {
                    "question_html": _replace_diagram_markers(result.get("question_html", q.question_html), marker_diagrams),
                    "answer_html": proposed_answer, "score_points_html": proposed_score,
                    "standard_answer": result.get("standard_answer", q.standard_answer),
                    "question_type": result.get("question_type", q.question_type),
                    "diagrams": diagrams, "structure_graph": result.get("structure_graph"),
                },
            }
            _save_pending(question_id, pending)
            reply = "AI 已生成标准化题面和解答，等待你确认后再覆盖原内容。"
            tool_result = {"type": "pending", "pending_type": "rewrite_answer"}
    elif any(kw in msg_lower for kw in ["出新题","类题","变式","换数据","换个数"]):
        if getattr(q, "is_resolved", False):
            reply = "已锁定的题目不支持生成新题/变式，请先取消锁定。"
            tool_result = {"type": "error", "message": reply}
            messages.append({"role":"assistant","content":reply})
            _save_chat(question_id, messages)
            return {"reply": reply, "action": action, "tool": tool_result}
        result = await ai_service.deepseek_regenerate_question(q.ocr_text or "", q.subject or "", q.grade or "", req.message, style, answer_html=q.answer_html or "", question_html=q.question_html or "", standard_answer=q.standard_answer or "")
        answer_raw = result.get("answer_html", "")
        proposed_score = ""
        if "<!-- SCORE_SPLIT -->" in answer_raw:
            parts = answer_raw.split("<!-- SCORE_SPLIT -->", 1)
            proposed_score = parts[0].strip()
            proposed_answer = parts[1].strip()
        else:
            proposed_answer = answer_raw
        pending = {
            "type": "create_variant",
            "proposed": {
                "ocr_text": result.get("question_html", ""),
                "question_html": result.get("question_html", ""),
                "answer_html": proposed_answer,
                "score_points_html": proposed_score,
                "standard_answer": result.get("standard_answer", ""),
                "question_type": result.get("question_type", q.question_type or "解答"),
                "structure_graph": result.get("structure_graph"),
            },
        }
        _save_pending(question_id, pending)
        reply = "变式题已生成，等待你确认后作为一条新题保存；原题不会被覆盖。"
        tool_result = {"type": "pending", "pending_type": "create_variant"}
    else:
        reply = await ai_service.deepseek_chat_question(info, req.message, style)

    # Generate diagrams for any [[DIAGRAM:...]] markers in the reply
    if re.search(r'\[\[DIAGRAM:[^\]]+\]\]', reply):
        for m in re.finditer(r'\[\[DIAGRAM:([^\]]+)\]\]', reply):
            desc = m.group(1)
            reservation = ""
            try:
                diagram_index, reservation = ds._reserve_diagram_index(
                    os.path.join(QUESTIONS_DIR, question_id)
                )
                path = await ds.generate_diagram(question_id, desc, diagram_index)
                if path:
                    from services.diagram_service import diagram_service as _ds2
                    disk_p = os.path.join(STORAGE_DIR, path.lstrip("/"))
                    w, _ = _ds2._get_svg_size(disk_p)
                    sz = f' width="{w}"' if w else ""
                    svg_tag = f'<div class="diagram"><img src="{path}" style="max-width:80%;height:auto"{sz}></div>'
                else:
                    svg_tag = ''
                reply = reply.replace(m.group(0), svg_tag, 1)
            except Exception:
                reply = reply.replace(m.group(0), '', 1)
            finally:
                if reservation:
                    try:
                        os.remove(reservation)
                    except FileNotFoundError:
                        pass

    # 对回复中的数学公式做 HTML 安全转义，避免 < > 破坏页面结构
    reply = escape_math_html(reply)
    messages.append({"role":"assistant","content":reply})
    _save_chat(question_id, messages)

    return {"reply": reply, "action": action, "tool": tool_result}


@router.post("/{question_id}/chat")
async def chat_question(question_id: str, req: ChatRequest, db: AsyncSession = Depends(get_db)):
    """Serialize a question's full AI/chat cycle to avoid lost history and pending changes."""
    lock = await _get_chat_lock(question_id)
    try:
        async with lock:
            try:
                return await _chat_question_impl(question_id, req, db)
            except HTTPException:
                raise
            except Exception as exc:
                log_error("questions.chat", f"AI chat failed for {question_id}: {exc}")
                _msg = str(exc).lower()
                if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
                    raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
                raise HTTPException(status_code=502, detail="AI 对话失败，请稍后重试")
    finally:
        # 有界清理：仅当锁无持有者且无排队等待者时才移除当前键；
        # 否则正在等待该锁的协程会被第三个请求新建的锁绕过，破坏同题串行化。
        if (len(_question_chat_locks) > 512 and not lock.locked()
                and not getattr(lock, "_waiters", None)
                and _question_chat_locks.get(question_id) is lock):
            _question_chat_locks.pop(question_id, None)


class RegenerateRequest(BaseModel):
    instruction: str = Field(default="", max_length=20_000)

@router.post("/{question_id}/regenerate")
async def regenerate_question(question_id: str, req: RegenerateRequest, db: AsyncSession = Depends(get_db)):
    q = await _get_question_for_update(db, question_id)
    if not q: raise HTTPException(404, "题目不存在")
    if getattr(q, "is_resolved", False):
        raise HTTPException(400, "已锁定的题目不支持重新生成，请先取消锁定")
    from services.ai_service import ai_service
    from services.user_profile import load_profile
    profile = load_profile()
    style = profile.get("style_notes", "") or profile.get("notation_preferences", "")
    instruction = req.instruction or "请用标准格式重新生成题目和解答"
    try:
        result = await ai_service.deepseek_regenerate_question(
            q.ocr_text or "", q.subject or "", q.grade or "", instruction, style,
            answer_html=q.answer_html or "", question_html=q.question_html or "",
            standard_answer=q.standard_answer or "",
        )
    except Exception as exc:
        log_error("questions.regenerate", f"AI call failed for {question_id}: {exc}")
        _msg = str(exc).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
        raise HTTPException(status_code=502, detail="AI 重新生成失败，请稍后重试")
    answer_raw = str(result.get("answer_html", "") or "").strip()
    question_raw = str(result.get("question_html", "") or "").strip()
    standard_answer = str(result.get("standard_answer", "") or "").strip()
    if len(question_raw) < 5 or len(answer_raw) < 20 or not standard_answer:
        raise HTTPException(502, detail="模型返回的题面、标准答案或详细解析不完整，未覆盖原题")
    # Generate diagrams if AI returned prompts。
    # [[DIAGRAM:N]] 只指向本次生成的 diagram_prompts 下标，不得被旧图列表偏移。
    diagram_prompts = result.get("diagram_prompts", [])
    if not isinstance(diagram_prompts, list):
        diagram_prompts = []
    marker_diagrams: list[dict] = []
    if diagram_prompts:
        from services.diagram_service import diagram_service as ds
        new_diagrams = list(q.diagrams or [])
        places = result.get("diagram_places") or _default_diagram_places(len(diagram_prompts))
        for i, dp in enumerate(diagram_prompts):
            if not isinstance(dp, str) or not dp.strip():
                continue
            try:
                path = await ds.generate_diagram(q.id, dp, len(new_diagrams))
            except Exception as exc:
                log_error("questions.regenerate_diagram", f"Diagram generation failed for {q.id}: {exc}")
                path = None
            if path:
                place = places[i] if i < len(places) else "question"
                new_item = {"path": path, "place": place}
                new_diagrams.append(new_item)
                marker_diagrams.append(new_item)
        q.diagrams = new_diagrams
    diagrams = list(q.diagrams or [])
    if "<!-- SCORE_SPLIT -->" in answer_raw:
        parts = answer_raw.split("<!-- SCORE_SPLIT -->", 1)
        q.score_points_html = _strip_unresolved_diagram_markers(
            _replace_diagram_markers(parts[0].strip(), marker_diagrams))
        q.answer_html = _strip_unresolved_diagram_markers(
            _replace_diagram_markers(parts[1].strip(), marker_diagrams))
    else:
        q.answer_html = _strip_unresolved_diagram_markers(
            _replace_diagram_markers(answer_raw, marker_diagrams))
        if result.get("score_points_html") is not None:
            q.score_points_html = _strip_unresolved_diagram_markers(
                _replace_diagram_markers(str(result.get("score_points_html") or ""), marker_diagrams))
    q.question_html = _strip_unresolved_diagram_markers(
        _replace_diagram_markers(question_raw, marker_diagrams))
    q.standard_answer = standard_answer
    q.question_type = str(result.get("question_type", q.question_type or "") or "").strip()
    q.diagram_places = [
        (item.get("place") if isinstance(item, dict) and item.get("place") in ("question", "answer") else "answer")
        for item in diagrams
    ]
    # 题面/解答已变化，旧结构图检查信息不再适用
    q.structure_graph_info = None
    # 保存结构梳理图数据（模块开关开启时统一校验清洗；保留用户手动编辑）
    if ENABLE_STRUCTURE_GRAPH:
        await _apply_ai_structure_graph(q, result.get("structure_graph"), db)
    q.status = "done"
    q.error_message = ""
    # AI 生成耗时较长；同一事务内重读只会看到旧快照（WAL），
    # 必须用独立连接读取最新提交值：期间被用户锁定的题目不允许覆盖
    from models.database import async_session as _fresh_session
    async with _fresh_session() as _chk:
        fresh_resolved = (await _chk.execute(
            select(Question.is_resolved).where(Question.id == question_id)
        )).scalar()
    if fresh_resolved:
        await db.rollback()
        raise HTTPException(409, "题目在重新生成期间已被锁定，本次结果未写入，请先取消锁定")
    await db.commit()
    from services.audit_service import set_question_challenge
    from services.question_challenge import combine_question_challenges
    challenge_raw = result.get("question_challenge") if isinstance(result, dict) else None
    challenge = combine_question_challenges(
        {**challenge_raw, "source": "regenerate"} if isinstance(challenge_raw, dict) else {}
    )
    await set_question_challenge(question_id, challenge, auto=True)
    return {
        "message":"已重新生成",
        "question_html":escape_math_html(q.question_html or ""),
        "answer_html":escape_math_html(q.answer_html or ""),
        "score_points_html":escape_math_html(q.score_points_html or ""),
        "standard_answer": escape_math_html(q.standard_answer or ""),
        "question_type": q.question_type or "",
        "question_challenge": challenge,
        "structure_graph":q.structure_graph,
        "structure_graph_info":q.structure_graph_info,
    }


@router.post("/{question_id}/confirm-pending")
async def confirm_pending(question_id: str, db: AsyncSession = Depends(get_db)):
    """Apply pending changes after user confirmation."""
    pending = _load_pending(question_id)
    if not pending or not pending.get("type"):
        raise HTTPException(404, detail="没有待确认的修改")
    q = await _get_question_for_update(db, question_id)
    if not q:
        _clear_pending(question_id)
        raise HTTPException(404, detail="题目不存在")
    if getattr(q, "is_resolved", False):
        _clear_pending(question_id)
        raise HTTPException(400, detail="已锁定的题目不支持应用修改，请先取消锁定")
    ptype = pending["type"]
    prop = pending.get("proposed", {})
    if not isinstance(prop, dict):
        raise HTTPException(422, detail="待确认数据格式无效，请取消后重新发起")
    try:
        content_changed = False
        if ptype == "modify_question":
            if prop.get("question_html"):
                q.question_html = prop["question_html"]
                content_changed = True
        elif ptype == "modify_answer":
            if "answer_html" in prop:
                q.answer_html = prop["answer_html"]
                content_changed = True
            if "score_points_html" in prop:
                q.score_points_html = prop["score_points_html"]
            if "standard_answer" in prop:
                q.standard_answer = prop["standard_answer"]
        elif ptype == "rewrite_answer":
            if prop.get("question_html"):
                q.question_html = prop["question_html"]
                content_changed = True
            if "answer_html" in prop:
                q.answer_html = prop["answer_html"]
                content_changed = True
            if "score_points_html" in prop:
                q.score_points_html = prop["score_points_html"]
            if "standard_answer" in prop:
                q.standard_answer = prop["standard_answer"]
            if "question_type" in prop:
                q.question_type = prop["question_type"]
            if "diagrams" in prop:
                q.diagrams = prop["diagrams"]
            # P0#2: 保存 structure_graph（模块开关开启时统一校验清洗；保留用户手动编辑）
            if ENABLE_STRUCTURE_GRAPH:
                await _apply_ai_structure_graph(q, prop.get("structure_graph"), db)
            # 题面/解答已变化，旧结构图检查信息不再适用
            content_changed = True
        elif ptype == "create_variant":
            required = ("question_html", "answer_html", "standard_answer")
            if any(not str(prop.get(key, "")).strip() for key in required):
                raise HTTPException(422, detail="变式题内容不完整，未保存")
            from models.models import gen_id
            new_id = gen_id()
            folder = os.path.join(QUESTIONS_DIR, new_id)
            # 目录创建纳入 try：ID 碰撞或写失败时清理，避免残留空目录
            try:
                os.makedirs(folder, exist_ok=False)
                structure_graph = _sanitize_structure_graph(prop.get("structure_graph")) if ENABLE_STRUCTURE_GRAPH else None
                variant = Question(
                    id=new_id, folder_path=folder, source_type="ai_variant", status="done",
                    subject=q.subject or "", grade=q.grade or "", region=q.region or "",
                    bank=q.bank or "default", knowledge_tags=list(q.knowledge_tags or []),
                    ocr_text=prop.get("ocr_text") or prop["question_html"],
                    question_html=prop["question_html"], answer_html=prop["answer_html"],
                    score_points_html=prop.get("score_points_html", ""),
                    standard_answer=prop["standard_answer"],
                    question_type=prop.get("question_type", "解答"),
                    structure_graph=structure_graph or {"nodes": [], "edges": []},
                )
                db.add(variant)
                await db.commit()
            except Exception:
                await db.rollback()
                shutil.rmtree(folder, ignore_errors=True)
                raise
            _clear_pending(question_id)
            return {"message": "变式题已另存，原题未修改", "new_question_id": new_id}
        else:
            raise HTTPException(400, detail="待确认操作类型无效")
        if content_changed:
            q.structure_graph_info = None
        q.status = "done"
        await db.commit()
        _clear_pending(question_id)
        return {"message": "修改已确认并保存"}
    except HTTPException:
        raise
    except Exception as e:
        log_error("questions.pending", f"Failed to apply pending change for {question_id}: {e}")
        raise HTTPException(500, detail="应用失败，请稍后重试")


@router.post("/{question_id}/cancel-pending")
async def cancel_pending(question_id: str):
    """Cancel pending changes."""
    _clear_pending(question_id)
    return {"message": "已取消修改"}


@router.get("/{question_id}/structure-graph")
async def get_structure_graph(question_id: str, dark: bool = Query(False), db: AsyncSession = Depends(get_db)):
    """获取题目的结构梳理图（分层树状布局 + SVG 渲染）"""
    from config import ENABLE_STRUCTURE_GRAPH
    if not ENABLE_STRUCTURE_GRAPH:
        raise HTTPException(status_code=404, detail="结构梳理图功能未启用")
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    sg = q.structure_graph
    if not isinstance(sg, dict) or not sg.get("nodes"):
        return {"error": "no_data", "message": "该题目暂无结构梳理图数据"}
    from services.structure_graph_service import process_structure_graph
    try:
        result = process_structure_graph(sg, dark_mode=dark)
    except Exception as e:
        log_error("structure_graph", f"process_structure_graph failed for {question_id}: {e}")
        raise HTTPException(status_code=500, detail="结构梳理图渲染失败，请稍后重试")
    if not result:
        return {"error": "no_data", "message": "结构梳理图数据无效"}
    return result


@router.post("/{question_id}/structure-graph/generate")
async def generate_structure_graph(question_id: str, db: AsyncSession = Depends(get_db)):
    """AI 自动生成结构梳理图（基于题目和解答）"""
    from config import ENABLE_STRUCTURE_GRAPH
    if not ENABLE_STRUCTURE_GRAPH:
        raise HTTPException(status_code=404, detail="结构梳理图功能未启用")
    # 先无锁读取题目信息，避免 AI 调用期间长时间持有行锁
    q_read = await db.get(Question, question_id)
    if not q_read:
        raise HTTPException(status_code=404, detail="题目不存在")
    if q_read.is_resolved:
        raise HTTPException(status_code=403, detail="已锁定的题目不允许 AI 修改结构图")
    current_text = q_read.ocr_text or ""
    current_qhtml = q_read.question_html or ""
    current_ahtml = q_read.answer_html or ""
    current_std = q_read.standard_answer or ""
    current_subject = q_read.subject or ""
    current_grade = q_read.grade or ""
    from services.ai_service import ai_service
    try:
        result = await ai_service.deepseek_generate_structure_graph(
            current_text, current_qhtml, current_ahtml,
            current_std, current_subject, current_grade
        )
    except Exception as e:
        from logger import log_error
        log_error("questions", f"generate_structure_graph AI call failed for {question_id}: {e}")
        _msg = str(e).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查 DeepSeek API Key 配置")
        raise HTTPException(status_code=502, detail="AI 生成结构梳理图失败，请稍后重试")
    sg = result.get("structure_graph")
    # 保存前校验清洗；空图（{} 或 {"nodes":[]}）视为合法清空，与 AI prompt 约定一致
    sanitized = _sanitize_structure_graph(sg)
    if sanitized is None:
        # AI 返回畸形数据属上游数据质量问题，映射 502 而非 500
        raise HTTPException(status_code=502, detail="AI 生成的结构梳理图数据格式无效")
    nodes, edges = sanitized["nodes"], sanitized["edges"]
    # AI 调用完成后加锁写入，避免覆盖用户并发编辑/已解决标记
    q = await _get_question_for_update(db, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    applied = await _apply_ai_structure_graph(q, {"nodes": nodes, "edges": edges}, db)
    if not applied:
        if q.is_resolved:
            raise HTTPException(status_code=403, detail="已锁定的题目不允许 AI 修改结构图")
        return {"message": "已保留您的手动编辑版本", "structure_graph": q.structure_graph}
    await db.commit()
    return {"message": "结构梳理图已生成", "structure_graph": q.structure_graph}


@router.post("/{question_id}/structure-graph/check")
async def check_structure_graph(question_id: str, db: AsyncSession = Depends(get_db)):
    """AI 检查结构梳理图的合理性"""
    from config import ENABLE_STRUCTURE_GRAPH
    if not ENABLE_STRUCTURE_GRAPH:
        raise HTTPException(status_code=404, detail="结构梳理图功能未启用")
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    sg = q.structure_graph
    if not sg:
        raise HTTPException(status_code=400, detail="该题目暂无结构梳理图数据")
    from services.ai_service import ai_service
    try:
        result = await ai_service.deepseek_check_structure_graph(
            sg, q.ocr_text or "", q.question_html or "", q.answer_html or "",
            q.standard_answer or "", q.subject or "", q.grade or ""
        )
    except Exception as e:
        from logger import log_error
        log_error("questions", f"check_structure_graph AI call failed for {question_id}: {e}")
        _msg = str(e).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查 DeepSeek API Key 配置")
        raise HTTPException(status_code=502, detail="AI 检查结构梳理图失败，请稍后重试")
    # AI 检查期间题目可能被锁定或编辑，落库前使用 FOR UPDATE 重新获取对象并确认状态
    q2 = await _get_question_for_update(db, question_id)
    if not q2:
        return result
    # 已锁定的题目不再允许 AI 修改数据库中的结构图，但可直接返回检查结果供用户查看
    if q2.is_resolved:
        return result
    # 如果 AI 返回修正后的结构图，自动保存；但保留用户手动编辑标记
    fixed = result.get("fixed_structure_graph")
    if fixed:
        current_sg = q2.structure_graph or {}
        is_user_edited = isinstance(current_sg, dict) and current_sg.get("is_user_edited")
        if not is_user_edited:
            sanitized = _sanitize_structure_graph(fixed)
            if sanitized and sanitized.get("nodes"):
                q2.structure_graph = sanitized
                await db.commit()
                result["saved_fixed"] = True
                # 返回给前端的 fixed_structure_graph 也应是经过校验清洗后的版本
                result["fixed_structure_graph"] = sanitized
    # AI 检查期间用户若编辑或锁定了结构图，则不要把针对旧图的评分信息写入
    # 用 q2 当前状态判断（FOR UPDATE 已确保是最新状态）
    if q2.is_resolved:
        return result
    current_sg2 = q2.structure_graph or {}
    if isinstance(current_sg2, dict) and current_sg2.get("is_user_edited"):
        logger.info("Skip structure_graph_info because user edited during check for %s", question_id)
    else:
        # 将检查结果存入 question 的临时字段，方便前端展示
        try:
            _score = int(result.get("score", 0))
        except (TypeError, ValueError):
            _score = 0
        q2.structure_graph_info = {
            "valid": result.get("valid", True),
            "score": _score,
            "issues": result.get("issues", []),
            "suggestions": result.get("suggestions", []),
        }
        await db.commit()
    return result


@router.post("/{question_id}/structure-graph/rewrite")
async def rewrite_structure_graph(question_id: str, db: AsyncSession = Depends(get_db)):
    """AI 重写结构梳理图"""
    from config import ENABLE_STRUCTURE_GRAPH
    if not ENABLE_STRUCTURE_GRAPH:
        raise HTTPException(status_code=404, detail="结构梳理图功能未启用")
    # 先无锁读取题目信息，避免 AI 调用期间长时间持有行锁
    q_read = await db.get(Question, question_id)
    if not q_read:
        raise HTTPException(status_code=404, detail="题目不存在")
    if q_read.is_resolved:
        raise HTTPException(status_code=403, detail="已锁定的题目不允许 AI 修改结构图")
    current_text = q_read.ocr_text or ""
    current_qhtml = q_read.question_html or ""
    current_ahtml = q_read.answer_html or ""
    current_std = q_read.standard_answer or ""
    current_subject = q_read.subject or ""
    current_grade = q_read.grade or ""
    from services.ai_service import ai_service
    try:
        result = await ai_service.deepseek_generate_structure_graph(
            current_text, current_qhtml, current_ahtml,
            current_std, current_subject, current_grade
        )
    except Exception as e:
        from logger import log_error
        log_error("questions", f"rewrite_structure_graph AI call failed for {question_id}: {e}")
        _msg = str(e).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查 DeepSeek API Key 配置")
        raise HTTPException(status_code=502, detail="AI 重写结构梳理图失败，请稍后重试")
    sg = result.get("structure_graph")
    # 保存前校验清洗；空图（{} 或 {"nodes":[]}）视为合法清空，与 AI prompt 约定一致
    sanitized = _sanitize_structure_graph(sg)
    if sanitized is None:
        # AI 返回畸形数据属上游数据质量问题，映射 502 而非 500
        raise HTTPException(status_code=502, detail="AI 重写的结构梳理图数据格式无效")
    nodes, edges = sanitized["nodes"], sanitized["edges"]
    # AI 调用完成后加锁写入，避免覆盖用户并发编辑/已解决标记
    q = await _get_question_for_update(db, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    applied = await _apply_ai_structure_graph(q, {"nodes": nodes, "edges": edges}, db)
    if not applied:
        if q.is_resolved:
            raise HTTPException(status_code=403, detail="已锁定的题目不允许 AI 修改结构图")
        return {"message": "已保留您的手动编辑版本", "structure_graph": q.structure_graph}
    await db.commit()
    return {"message": "结构梳理图已重写", "structure_graph": q.structure_graph}


class MarkStructureGraphRequest(BaseModel):
    structure_graph: dict


@router.post("/{question_id}/structure-graph/mark")
async def mark_structure_graph(question_id: str, req: MarkStructureGraphRequest, db: AsyncSession = Depends(get_db)):
    """用户手动标记/修改结构梳理图（保存用户编辑后的结构图）"""
    from config import ENABLE_STRUCTURE_GRAPH
    if not ENABLE_STRUCTURE_GRAPH:
        raise HTTPException(status_code=404, detail="结构梳理图功能未启用")
    q = await _get_question_for_update(db, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    if getattr(q, "is_resolved", False):
        raise HTTPException(status_code=403, detail="已锁定的题目不允许修改结构图")
    from services.structure_graph_service import _validate_structure_graph
    nodes, edges = _validate_structure_graph(req.structure_graph)
    if not nodes:
        raise HTTPException(status_code=400, detail="结构梳理图数据无效")
    sg = {"nodes": nodes, "edges": edges, "is_user_edited": True}
    q.structure_graph = sg
    q.structure_graph_info = None
    await db.commit()
    return {"message": "标记已保存", "structure_graph": sg}


@router.get("/{question_id}/comparison")
async def get_comparison(question_id: str, db: AsyncSession = Depends(get_db)):
    """获取题目对比模式数据（分区、结构图、解答、标准答案）"""
    if not ENABLE_COMPARISON_MODE:
        raise HTTPException(status_code=404, detail="题目对比模式未启用")
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    return {
        "regions": (getattr(q, "comparison_regions", None) or {}).get("regions", []),
        "structure_graph": q.structure_graph,
        "answer_html": q.answer_html,
        "standard_answer": q.standard_answer,
    }


@router.post("/{question_id}/comparison/generate")
async def generate_comparison(question_id: str, db: AsyncSession = Depends(get_db)):
    """AI 自动生成题目对比分区（基于结构梳理图和解答）"""
    if not ENABLE_COMPARISON_MODE:
        raise HTTPException(status_code=404, detail="题目对比模式未启用")
    # 先无锁读取题目信息，避免 AI 调用期间长时间持有行锁
    q_read = await db.get(Question, question_id)
    if not q_read:
        raise HTTPException(status_code=404, detail="题目不存在")
    if q_read.is_resolved:
        raise HTTPException(status_code=403, detail="已锁定的题目不允许 AI 修改对比分区")
    if not q_read.structure_graph or not q_read.structure_graph.get("nodes"):
        raise HTTPException(status_code=400, detail="该题目暂无结构梳理图数据")
    current_sg = q_read.structure_graph
    current_ahtml = q_read.answer_html or ""
    current_std = q_read.standard_answer or ""
    from services.ai_service import ai_service
    try:
        result = await ai_service.deepseek_generate_comparison_regions(
            current_sg, current_ahtml, current_std
        )
    except Exception as e:
        from logger import log_error
        log_error("questions", f"generate_comparison AI call failed for {question_id}: {e}")
        _msg = str(e).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查 DeepSeek API Key 配置")
        raise HTTPException(status_code=502, detail="AI 生成对比分区失败，请稍后重试")
    # 保存前校验清洗；空分区（{} 或 {"regions":[]}）视为合法清空
    sanitized = _sanitize_comparison_regions(result)
    if sanitized is None:
        # AI 返回畸形数据属上游数据质量问题，映射 502 而非 500
        raise HTTPException(status_code=502, detail="AI 生成的对比分区数据格式无效")
    # AI 调用完成后加锁写入，避免覆盖用户并发编辑/已解决标记
    q = await _get_question_for_update(db, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    applied = await _apply_ai_comparison_regions(q, sanitized, db)
    if not applied:
        if q.is_resolved:
            raise HTTPException(status_code=403, detail="已锁定的题目不允许 AI 修改对比分区")
        return {"message": "已保留您的手动编辑版本", "regions": (getattr(q, "comparison_regions", None) or {}).get("regions", [])}
    await db.commit()
    return {"message": "对比分区已生成", "regions": sanitized["regions"]}


# P0: 通用 /{question_id} 路由必须放在所有 /{question_id}/xxx 子路由之后，
# 否则 Starlette 会按顺序匹配，导致子路由被它吞掉。
@router.get("/{question_id}", response_model=QuestionResponse)
async def get_question(question_id: str, db: AsyncSession = Depends(get_db)):
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")
    return q


@router.put("/{question_id}", response_model=QuestionResponse)
async def update_question(
    question_id: str,
    update: QuestionUpdate,
    db: AsyncSession = Depends(get_db)
):
    q = await _get_question_for_update(db, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")

    # 锁定守卫（FreqErr [锁定守卫缺失]）：已锁定的题目不允许再改内容。
    # 同文件的 regenerate / challenge / structure_graph / comparison_regions 等约 20 处
    # 路径都有同款守卫（统一 409「题目已锁定；请先取消锁定…」），唯独本端点此前遗漏——
    # 而它是**写入面最宽**的：QuestionUpdate 含 question_html / answer_html /
    # standard_answer / score_points_html / handwriting_notes / structure_graph /
    # comparison_regions 等全部内容字段，可静默清空 structure_graph_info 并覆盖正文，
    # 绕过整套锁定与复核链路。前端只拦了 AI 驱动的修改（结构图/对比分区/质疑），
    # 手动编辑表单未拦，故守卫必须落在后端。
    if getattr(q, "is_resolved", False):
        raise HTTPException(status_code=409, detail="题目已锁定；请先取消锁定再编辑")

    update_data = update.model_dump(exclude_unset=True)
    # 题面/解答变化后，旧的结构图检查信息可能不再适用
    if "question_html" in update_data or "answer_html" in update_data:
        q.structure_graph_info = None
    for key, value in update_data.items():
        if key == "structure_graph_info":
            # 检查评分信息仅由 /check 等内部流程维护，不允许外部直接写入
            continue
        if key == "structure_graph":
            if not ENABLE_STRUCTURE_GRAPH:
                continue
            # P1#7: 只要请求包含 structure_graph 就校验；空对象视为有效（清空）
            # 复用 _sanitize_structure_graph 以保留 is_user_edited 等元数据
            if value:
                sanitized = _sanitize_structure_graph(value)
                if sanitized is None:
                    raise HTTPException(status_code=400, detail="structure_graph 数据无效")
                value = sanitized
            else:
                value = {"nodes": [], "edges": []}
            # 同时清空检查信息，避免旧评分对应新结构图
            q.structure_graph_info = None
        if key == "comparison_regions":
            if not ENABLE_COMPARISON_MODE:
                continue
            if value:
                sanitized = _sanitize_comparison_regions(value)
                if sanitized is None:
                    raise HTTPException(status_code=400, detail="comparison_regions 数据无效")
                value = sanitized
            else:
                value = {"regions": []}
        setattr(q, key, value)

    await db.commit()
    await db.refresh(q)
    return q


@router.delete("/{question_id}")
async def delete_question(question_id: str, db: AsyncSession = Depends(get_db)):
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(status_code=404, detail="题目不存在")

    folder = q.folder_path
    await db.execute(
        update(ProcessingTask).where(ProcessingTask.question_id == question_id).values(question_id=None)
    )
    await db.execute(
        update(Correction).where(Correction.question_id == question_id).values(question_id=None)
    )
    await db.execute(
        update(Correction).where(Correction.matched_question_id == question_id).values(matched_question_id="")
    )
    # 定向清理：用 JSON 列 LIKE 定位包含该题的记录，只加载命中行，
    # 不再全表加载 Paper/UploadSession/Note 到内存逐行过滤。
    # id 以 `"id"` 形式存于 JSON 数组，模式 `%"<qid>"%` 只会精确命中该 id。
    qid_pattern = f'%"{question_id}"%'
    for model in (Paper, UploadSession, Note):
        matched = (await db.execute(
            select(model).where(model.question_ids.cast(Text).like(qid_pattern, escape="\\"))
        )).scalars().all()
        for row in matched:
            ids = list(getattr(row, "question_ids", None) or [])
            if question_id not in ids:
                continue
            row.question_ids = [qid for qid in ids if qid != question_id]
            if isinstance(row, Paper):
                row.question_order = [qid for qid in (row.question_order or []) if qid != question_id]
            elif isinstance(row, UploadSession) and not row.question_ids:
                row.status = "open"
    await db.delete(q)
    await db.commit()

    if folder and os.path.exists(folder):
        questions_root = os.path.realpath(QUESTIONS_DIR)
        folder_real = os.path.realpath(folder)
        try:
            if os.path.commonpath([questions_root, folder_real]) == questions_root and folder_real != questions_root:
                shutil.rmtree(folder_real)
            else:
                logger.warning("Skipped unsafe question folder deletion: %s", folder)
        except (OSError, ValueError) as exc:
            logger.warning("Question %s deleted but folder cleanup failed: %s", question_id, exc)

    # 清理题目旁路文件（chat.json 在题目目录内已随 rmtree 删除）
    if _is_valid_question_id(question_id):
        for stray in (os.path.join(PENDING_DIR, f"{question_id}.json"),
                      os.path.join(TASK_STATE_DIR, f"{question_id}.json")):
            try:
                root = os.path.realpath(stray)
                base = os.path.realpath(STORAGE_DIR)
                if os.path.isfile(root) and os.path.commonpath([base, root]) == base:
                    os.remove(root)
            except (OSError, ValueError) as exc:
                logger.warning("Question %s deleted but stray file cleanup failed: %s", question_id, exc)
    return {"message": "已删除"}
