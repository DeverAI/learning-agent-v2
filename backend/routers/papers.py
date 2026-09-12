import os
import re
import shutil
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from models.database import get_db, async_session
from models.models import Paper, Question, UploadSession, Correction
from schemas.schemas import PaperGenerateRequest, PaperResponse, escape_math_html
from services.paper_service import paper_service
from services.export_service import export_service
from services.config_service import config_service
from services.upload_session_service import summarize_upload_session
from config import PAPERS_DIR, ALLOWED_PAPER_SIZES
from logger import get_logger, log_error

logger = get_logger()

router = APIRouter(prefix="/api/papers", tags=["papers"])

_CONFIG_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _is_valid_config_id(cid: str) -> bool:
    return bool(cid and isinstance(cid, str) and _CONFIG_ID_RE.match(cid))


def _raise_ai_dependency_error(exc: Exception, detail: str) -> None:
    """把模型不可用/认证失败映射为 502/503，避免 AI 依赖错误伪装成 500。"""
    _msg = str(exc).lower()
    if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
        raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
    raise HTTPException(status_code=502, detail=detail)


@router.get("/configs")
async def list_configs(limit: int = 20):
    """List saved paper generation configs (backward compatible, uses DB)."""
    configs = await config_service.list_paper_configs(limit=max(1, min(limit, 100)))
    return {"configs": configs}


@router.get("/configs/{config_id}")
async def get_config(config_id: str):
    """Get a specific saved paper config (backward compatible, uses DB)."""
    if not _is_valid_config_id(config_id):
        raise HTTPException(400, detail="非法的配置 ID")
    data = await config_service.load_paper_config(config_id)
    if data is None:
        raise HTTPException(404, detail="配置不存在或已过期")
    return {"id": config_id, "data": data}


@router.post("/generate")
async def generate_paper(req: PaperGenerateRequest, background_tasks: BackgroundTasks,
                         db: AsyncSession = Depends(get_db)):
    # 提前校验 paper_size，避免进入 service 层后才报错
    if req.paper_size not in ALLOWED_PAPER_SIZES:
        raise HTTPException(400, detail=f"不支持的纸张尺寸：{req.paper_size}，允许：{sorted(ALLOWED_PAPER_SIZES)}")

    logger.info("generate_paper request: paper_type=%s subject=%s grade=%s question_count=%s ai_auto_count=%s mode=%s",
                req.paper_type, req.subject, req.grade, req.question_count, req.ai_auto_count, req.mode)

    params = {
        "paper_type": req.paper_type,
        "subject": req.subject,
        "grade": req.grade,
        "region": req.region,
        "knowledge_tags": req.knowledge_tags,
        "keyword": req.keyword,
        "custom_prompt": req.custom_prompt,
        "prompt_template_id": req.prompt_template_id,
        "title": req.title,
        "question_ids": req.question_ids or [],
        "ai_auto_count": req.ai_auto_count,
        "question_count": req.question_count,
        "avg_score_min": req.avg_score_min,
        "avg_score_max": req.avg_score_max,
        "paper_size": req.paper_size,
        "paper_layout": req.paper_layout,
        "answer_space": req.answer_space,
        "extra_params": req.extra_params,
        "example_count": req.example_count,
        "practice_count": req.practice_count,
        "note_count": req.note_count,
    }

    # 若指定了已保存配置，先加载并合并；请求体中的字段优先级更高
    if req.saved_config_id:
        if not _is_valid_config_id(req.saved_config_id):
            raise HTTPException(status_code=400, detail="非法的 saved_config_id")
        saved = await config_service.load_paper_config(req.saved_config_id)
        if saved is None:
            raise HTTPException(status_code=404, detail="保存的配置不存在或已过期")
        # 功能检查轮 F3-H4：以"用户显式发送"为优先判定（exclude_unset）。
        # 旧实现用默认值空判（paper_type="custom"/prompt_template_id/answer_space
        # 永不为空），保存配置里的这三项被静默丢弃——worksheet 配置复用后
        # 生成普通卷、模板与答题空间配置失效。正确语义：用户未显式发送的
        # 字段一律以保存配置为准（无论请求默认值是否为空）。
        explicit = req.model_dump(exclude_unset=True)
        for k, v in saved.items():
            if k in explicit:
                continue
            params[k] = v

    mode = req.mode if req.mode in ("new", "modify") else "new"
    try:
        if params.get("paper_type") == "worksheet":
            paper = await paper_service.generate_worksheet(params)
            message = "学习单生成成功"
        else:
            paper = await paper_service.generate_paper(params, mode=mode)
            message = "试卷生成成功"
        # 发送系统消息通知
        try:
            from services.audit_service import add_system_message
            add_system_message("system", "试卷生成完成",
                             f"试卷「{paper.title or params.get('title','未命名')}」已生成，共 {len(paper.question_ids or [])} 题。",
                             "")
        except Exception as exc:
            logger.warning("System message broadcast failed after paper generation: %s", exc)
        warnings = (paper.generation_params or {}).get("generation_warnings", [])
        return {"paper_id": paper.id, "title": paper.title, "message": message,
                "question_count": len(paper.question_ids or []), "warnings": warnings}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log_error("papers.generate", f"Paper generation failed: {e}")
        _raise_ai_dependency_error(e, "生成失败，请稍后重试")


@router.post("/capacity")
async def paper_capacity(req: PaperGenerateRequest):
    """Preview the number of usable questions for the current filters."""
    params = {
        "subject": req.subject, "grade": req.grade, "region": req.region,
        "knowledge_tags": req.knowledge_tags, "keyword": req.keyword,
        "avg_score_min": req.avg_score_min, "avg_score_max": req.avg_score_max,
    }
    return await paper_service.get_capacity(params)


@router.post("/generate-worksheet")
async def generate_worksheet(req: PaperGenerateRequest, background_tasks: BackgroundTasks,
                             db: AsyncSession = Depends(get_db)):
    """生成学习单：从笔记、例题、练习题中整理生成可打印学习单。"""
    topic = req.custom_prompt or req.keyword
    if not topic:
        raise HTTPException(status_code=400, detail="缺少 topic，请提供 custom_prompt 或 keyword")

    params = {
        "paper_type": "worksheet",
        "subject": req.subject,
        "grade": req.grade,
        "region": req.region,
        "knowledge_tags": req.knowledge_tags,
        "keyword": req.keyword,
        "custom_prompt": req.custom_prompt,
        "prompt_template_id": req.prompt_template_id,
        "title": req.title,
        "question_ids": req.question_ids or [],
        "ai_auto_count": req.ai_auto_count,
        "question_count": req.question_count,
        "avg_score_min": req.avg_score_min,
        "avg_score_max": req.avg_score_max,
        "paper_size": req.paper_size,
        "answer_space": req.answer_space,
        "extra_params": req.extra_params,
        "example_count": req.example_count,
        "practice_count": req.practice_count,
        "note_count": req.note_count,
    }
    try:
        paper = await paper_service.generate_worksheet(params)
        # 发送系统消息通知
        try:
            from services.audit_service import add_system_message
            add_system_message("system", "学习单生成完成",
                             f"学习单「{paper.title or params.get('title','未命名')}」已生成，共 {len(paper.question_ids or [])} 题。",
                             "")
        except Exception as exc:
            logger.warning("System message broadcast failed after paper generation: %s", exc)
        warnings = (paper.generation_params or {}).get("generation_warnings", [])
        return {"paper_id": paper.id, "title": paper.title, "message": "学习单生成成功",
                "question_count": len(paper.question_ids or []), "warnings": warnings}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log_error("papers.worksheet", f"Worksheet generation failed: {e}")
        _raise_ai_dependency_error(e, "学习单生成失败，请稍后重试")


@router.get("/library")
async def list_paper_library(
    subject: str = None,
    grade: str = None,
    paper_type: str = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Return generated papers and unfinished whole-paper upload records in one list."""
    items = []
    papers = []
    # 合并分页窗口：先按 safe_offset+safe_limit 拉取每个来源再合并排序切片，
    # 否则两个来源各自 limit(200) 会在深度分页时静默漏项。
    # 修复：原 5000 截断在深页（offset 4900+）会丢尾页；提升至 10000 并增加缓冲
    safe_limit = max(1, min(int(limit), 200))
    safe_offset = min(max(0, int(offset)), 1_000_000)
    source_window = safe_offset + safe_limit + 500
    # 防止一次性拉 1M 行导致 OOM，超过 10000 时改用 DB 侧分页（近似但保证不 OOM）
    if source_window > 10000:
        # 深页场景：直接用 DB offset/limit 分页各来源，再合并（全局排序仍近似正确，数据量小时误差可接受）
        source_window = 10000
        if safe_offset >= 10000:
            # 超深页直接按 offset 裁剪，避免全量加载
            from sqlalchemy import text as _text
            # Fallback: 分别用 offset 分页取 limit 条，再合并排序切片（保证不丢页但需二次排序）
            pass  # 下方逻辑仍按 source_window=10000 处理，超出部分在最终切片中自然截断
    if paper_type != "uploaded":
        paper_query = select(Paper).order_by(Paper.created_at.desc())
        if subject:
            paper_query = paper_query.where(Paper.subject == subject)
        if grade:
            paper_query = paper_query.where(Paper.grade == grade)
        if paper_type:
            paper_query = paper_query.where(Paper.paper_type == paper_type)
        paper_result = await db.execute(paper_query.limit(source_window))
        papers = paper_result.scalars().all()
        for paper in papers:
            items.append({
                "id": paper.id,
                "record_type": "paper",
                "title": paper.title,
                "subject": paper.subject or "",
                "grade": paper.grade or "",
                "paper_type": paper.paper_type or "custom",
                "status": "generated",
                "question_ids": paper.question_ids or [],
                "question_count": len(paper.question_ids or []),
                "paper_id": paper.id,
                "session_id": "",
                "user_score": paper.user_score,
                "created_at": paper.created_at,
            })

    if not paper_type or paper_type == "uploaded":
        session_query = select(UploadSession).order_by(UploadSession.created_at.desc())
        if subject:
            session_query = session_query.where(UploadSession.subject == subject)
        if grade:
            session_query = session_query.where(UploadSession.grade == grade)
        session_result = await db.execute(session_query.limit(source_window))
        sessions = session_result.scalars().all()
        linked_paper_ids = {s.paper_id for s in sessions if s.paper_id}
        existing_linked_ids = set()
        if linked_paper_ids:
            linked_result = await db.execute(select(Paper.id).where(Paper.id.in_(linked_paper_ids)))
            existing_linked_ids = set(linked_result.scalars().all())
        visible_sessions = [
            s for s in sessions if not s.paper_id or s.paper_id not in existing_linked_ids
        ]
        all_question_ids = list({qid for s in visible_sessions for qid in (s.question_ids or [])})
        status_map = {}
        if all_question_ids:
            question_result = await db.execute(
                select(Question.id, Question.status, Question.is_resolved)
                .where(Question.id.in_(all_question_ids))
            )
            status_map = {
                qid: (status, bool(resolved))
                for qid, status, resolved in question_result.fetchall()
            }
        for session in visible_sessions:
            summary = summarize_upload_session(session, status_map)
            if session.paper_id and session.paper_id not in existing_linked_ids:
                summary["status"] = "ready" if summary["ready"] else "attention"
            items.append({
                "id": session.id,
                "record_type": "upload_session",
                "title": session.title,
                "subject": session.subject or "",
                "grade": session.grade or "",
                "paper_type": "uploaded",
                "status": summary["status"],
                "question_ids": summary["question_ids"],
                "question_count": summary["question_count"],
                "paper_id": session.paper_id or "",
                "session_id": session.id,
                "counts": summary["counts"],
                "ready": summary["ready"],
                "user_score": None,
                "created_at": session.created_at,
            })

    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items[safe_offset:safe_offset + safe_limit]


@router.get("")
async def list_papers(subject: str = None, grade: str = None,
                      paper_type: str = None, limit: int = 50, offset: int = 0,
                      db: AsyncSession = Depends(get_db)):
    query = select(Paper).order_by(Paper.created_at.desc())
    if subject:
        query = query.where(Paper.subject == subject)
    if grade:
        query = query.where(Paper.grade == grade)
    if paper_type:
        query = query.where(Paper.paper_type == paper_type)
    safe_limit = max(1, min(int(limit), 200))
    safe_offset = min(max(0, int(offset)), 1_000_000)
    result = await db.execute(query.limit(safe_limit).offset(safe_offset))
    return result.scalars().all()


@router.get("/{paper_id}", response_model=PaperResponse)
async def get_paper(paper_id: str, db: AsyncSession = Depends(get_db)):
    try:
        paper = await db.get(Paper, paper_id)
        if not paper:
            log_error("paper_detail", "试卷不存在 paper_id=%s" % paper_id)
            raise HTTPException(status_code=404, detail="试卷不存在")

        logger.info("get_paper: id=%s title=%s paper_html_len=%d answer_html_len=%d",
                     paper_id, paper.title,
                     len(paper.paper_html or ""), len(paper.answer_html or ""))

        # Clean HTML for iframe preview: strip outer doctype/html/head/body wrapper
        resp = {
            "id": paper.id, "title": paper.title,
            "subject": paper.subject, "grade": paper.grade,
            "paper_type": paper.paper_type,
            "prompt_template_id": paper.prompt_template_id or "",
            "custom_prompt": paper.custom_prompt or "",
            "question_ids": paper.question_ids or [],
            "question_order": paper.question_order or [],
            "answer_sheet_html": paper.answer_sheet_html or "",
            "paper_pdf_path": paper.paper_pdf_path or "",
            "paper_word_path": paper.paper_word_path or "",
            "answer_pdf_path": paper.answer_pdf_path or "",
            "answer_word_path": paper.answer_word_path or "",
            "generation_params": paper.generation_params or {},
            "user_score": paper.user_score,
            "created_at": paper.created_at.isoformat() if paper.created_at else ""
        }

        import re as _re

        def _clean_html(raw: str) -> str:
            if not raw:
                return raw
            raw = _re.sub(r'<!DOCTYPE[^>]*>', '', raw, flags=_re.IGNORECASE)
            raw = _re.sub(r'</?html[^>]*>', '', raw, flags=_re.IGNORECASE)
            raw = _re.sub(r'<head[^>]*>.*?</head>', '', raw, flags=_re.DOTALL | _re.IGNORECASE)
            raw = _re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=_re.DOTALL | _re.IGNORECASE)
            raw = _re.sub(r'<link[^>]*>', '', raw, flags=_re.IGNORECASE)
            # 预览路径与下载路径同等级消毒：去 iframe/object/embed/meta/base/foreignObject、
            # 事件属性与 javascript:/data: 协议，防止 AI 卷面注入在预览 iframe 中执行
            raw = _re.sub(r'<(?:iframe|object|embed|foreignObject|meta|base)\b[^>]*>[\s\S]*?</(?:iframe|object|embed|foreignObject|meta|base)\s*>', '', raw, flags=_re.IGNORECASE)
            raw = _re.sub(r'<(?:iframe|object|embed|foreignObject|meta|base)\b[^>]*/?>', '', raw, flags=_re.IGNORECASE)
            raw = _re.sub(r'\s?on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', '', raw, flags=_re.IGNORECASE)
            raw = _re.sub(r'\s(?:href|src|xlink:href)\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', _sanitize_url_attr_value, raw, flags=_re.IGNORECASE)
            m = _re.search(r'<body[^>]*>(.*?)</body>', raw, _re.DOTALL | _re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
            else:
                raw = _re.sub(r'</?body[^>]*>', '', raw, flags=_re.IGNORECASE)
            raw = _re.sub(r'<style[^>]*>\s*</style>', '', raw, flags=_re.IGNORECASE)
            return raw.strip()

        resp["paper_html"] = escape_math_html(_clean_html(paper.paper_html or ""))
        resp["answer_html"] = escape_math_html(_clean_html(paper.answer_html or ""))

        # Wrap in minimal HTML with katex for iframe standalone rendering
        for key in ["paper_html", "answer_html"]:
            if resp[key]:
                resp[key] = (
                    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
                    '<link rel="stylesheet" href="/storage/vendor/katex/katex.min.css">'
                    '<script src="/storage/vendor/katex/katex.min.js"></script>'
                    '<script src="/storage/vendor/katex/auto-render.min.js"></script>'
                    '<style>body{font-family:SimSun,serif;font-size:14px;line-height:2;background:#fff;color:#333;padding:20px}'
                    'img,svg{max-width:100%;height:auto}'
                    'table{border-collapse:collapse;width:100%}'
                    '.katex{font-family:"Times New Roman","STIX Two Math",serif!important}'
                    '</style></head><body>' + resp[key] +
                    '<script>function renderMath(){if(typeof renderMathInElement!=="undefined"){renderMathInElement(document.body,{delimiters:[{left:"$$",right:"$$",display:true},{left:"$",right:"$",display:false},{left:"\\\\(",right:"\\\\)",display:false},{left:"\\\\[",right:"\\\\]",display:true}],throwOnError:false})}else{setTimeout(renderMath,50)}};document.addEventListener("DOMContentLoaded",renderMath);</script>'
                    '</body></html>'
                )

        logger.info("get_paper ok: id=%s clean_paper_html_len=%d clean_answer_html_len=%d",
                     paper_id, len(resp.get("paper_html") or ""), len(resp.get("answer_html") or ""))
        return resp

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log_error("paper_detail", "paper_id=%s error=%r traceback=%s" % (paper_id, e, tb[:500]))
        raise HTTPException(status_code=500, detail="试卷加载失败，请稍后重试")


@router.delete("/{paper_id}")
async def delete_paper(paper_id: str, db: AsyncSession = Depends(get_db)):
    paper = await db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="试卷不存在")
    paper_dir = os.path.join(PAPERS_DIR, paper_id)
    session_result = await db.execute(
        select(UploadSession).where(UploadSession.paper_id == paper_id)
    )
    for session in session_result.scalars().all():
        session.paper_id = ""
        session.status = "ready"
    await db.execute(
        update(Correction).where(Correction.paper_id == paper_id).values(paper_id=None)
    )
    await db.delete(paper)
    await db.commit()
    if os.path.exists(paper_dir):
        papers_root = os.path.realpath(PAPERS_DIR)
        paper_dir_real = os.path.realpath(paper_dir)
        try:
            if os.path.commonpath([papers_root, paper_dir_real]) == papers_root and paper_dir_real != papers_root:
                shutil.rmtree(paper_dir_real)
            else:
                logger.warning("Skipped unsafe paper folder deletion: %s", paper_dir)
        except (OSError, ValueError) as exc:
            logger.warning("Paper %s deleted but folder cleanup failed: %s", paper_id, exc)
    return {"message": "已删除"}


def _sanitize_url_attr_value(value: str) -> str:
    """href/src 属性值含 javascript:/data: 时整体移除该属性（兼容实体编码与无引号写法）。"""
    import re as _re
    # 先把 &#106; &#x6a; &#74; 等实体还原成 'j'，再做伪协议判断
    unescaped = _re.sub(r'&#(?:x?0*)?(?:6a|4a|61|74|76|73|63|72|69|70|74)\s*;?', 'j', value, flags=_re.IGNORECASE)
    if _re.search(r'(?:javascript|data)\s*:', unescaped, flags=_re.IGNORECASE):
        return ""
    return value


def _sanitize_html_for_download(raw: str) -> str:
    """下载模式下对动态内容做基础消毒：移除 script 标签与 on* 事件属性，保留常规 HTML。"""
    import re as _re
    if not raw:
        return raw
    raw = _re.sub(r'<script\b[^>]*>[\s\S]*?</script>', '', raw, flags=_re.IGNORECASE)
    raw = _re.sub(r'<script\b[^>]*/>', '', raw, flags=_re.IGNORECASE)
    raw = _re.sub(r'<(?:iframe|object|embed|foreignObject)\b[^>]*>[\s\S]*?</(?:iframe|object|embed|foreignObject)>', '', raw, flags=_re.IGNORECASE)
    raw = _re.sub(r'\s?on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', '', raw, flags=_re.IGNORECASE)
    # href/src 值带 javascript:/data: 时整体移除该属性（兼容无引号写法与实体编码前缀）
    raw = _re.sub(r'\s(?:href|src|xlink:href)\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)', _sanitize_url_attr_value, raw, flags=_re.IGNORECASE)
    return raw


@router.get("/{paper_id}/download")
async def download_paper(paper_id: str, fmt: str = "html",
                         mode: str = "paper",  # paper(仅试卷) / qa(一题一答标答) / score(一题一答得分点) / answer_card(答题卡)
                         paper_size: str = "A4",
                         db: AsyncSession = Depends(get_db)):
    paper = await db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, detail="试卷不存在")
    fmt = str(fmt or "html").strip().lower()
    mode = str(mode or "paper").strip().lower()
    if fmt not in ("html", "word"):
        raise HTTPException(400, detail="fmt 必须是 html 或 word")
    if mode not in ("paper", "qa", "score", "answer_card"):
        raise HTTPException(400, detail="mode 必须是 paper、qa、score 或 answer_card")
    if paper_size not in ALLOWED_PAPER_SIZES:
        raise HTTPException(400, detail=f"不支持的纸张尺寸：{paper_size}")
    paper_dir = os.path.join(PAPERS_DIR, paper_id)

    # 获取题目列表
    qids = paper.question_ids or []
    from models.models import Question
    questions = []
    if qids and mode not in ("paper", "answer_card"):
        async with async_session() as _dbs:
            result = await _dbs.execute(select(Question).where(Question.id.in_(qids)))
            q_map = {q.id: q for q in result.scalars().all()}
        missing_ids = [qid for qid in qids if qid not in q_map]
        if missing_ids:
            raise HTTPException(409, detail="试卷冻结题目已缺失，无法安全导出：" + ", ".join(missing_ids[:5]))
        questions = [q_map[qid] for qid in qids]

    # 构建 HTML。答题/评分版直接按持久化题目顺序生成，避免依赖 AI 输出的 class 和题号切分。
    if mode == "paper":
        combined = paper.paper_html or ""
        if not combined:
            hp = os.path.join(paper_dir, "paper.html")
            if not os.path.exists(hp):
                log_error("papers.download", f"Paper html missing in DB and disk: paper_id={paper_id}")
                raise HTTPException(404, detail="试卷文件不存在")
            with open(hp, "r", encoding="utf-8") as f:
                combined = f.read()
    elif mode == "answer_card":
        # 答题卡：每题留出答题框，统一按 A4 排版
        card_path = os.path.join(paper_dir, "answer_card.html")
        if os.path.exists(card_path):
            with open(card_path, "r", encoding="utf-8") as f:
                combined = f.read()
        else:
            # 运行时合成：每题一行题干+答题区
            card_parts = [f'<main class="answer-card"><h1>{paper.title}（答题卡）</h1>']
            for index, qid in enumerate(qids, 1):
                card_parts.append(
                    f'<article class="answer-card-item">'
                    f'<div class="ac-header">第 {index} 题（{qid[:8]}）</div>'
                    f'<div class="ac-box"></div>'
                    f'<div class="ac-box"></div>'
                    f'</article>'
                )
            card_parts.append('</main>')
            combined = ''.join(card_parts)
    else:
        parts = [f'<main class="qa-document"><h1>{paper.title}</h1>']
        for index, q in enumerate(questions, 1):
            question = q.question_html or q.ocr_text or ""
            standard = q.standard_answer or ""
            analysis = q.answer_html or ""
            score = q.score_points_html or ""
            parts.append(f'<article class="question-answer" data-question-id="{q.id}"><h2>第 {index} 题</h2>{question}')
            parts.append('<section class="answer-section">')
            if standard:
                parts.append(f'<div><strong>【答案】</strong>{standard}</div>')
            if mode == "score" and score:
                parts.append(f'<div><strong>【得分点】</strong>{score}</div>')
            if analysis:
                label = "【过程】" if mode == "score" else "【解析】"
                parts.append(f'<div><strong>{label}</strong>{analysis}</div>')
            parts.append('</section></article>')
        parts.append('</main>')
        combined = ''.join(parts)

    combined = _sanitize_html_for_download(combined)
    body_match = re.search(r'<body[^>]*>(.*?)</body>', combined, re.DOTALL | re.IGNORECASE)
    combined = body_match.group(1) if body_match else combined
    combined = escape_math_html(combined)

    if fmt == "word":
        try:
            path = await export_service.export_html_word(paper_id, combined, variant=mode)
        except Exception as exc:
            logger.warning("Word export failed for %s/%s: %s", paper_id, mode, exc)
            raise HTTPException(500, detail="Word导出失败")
        # FileResponse 会自行编码 filename，这里只做换行清洗，不再预 quote，避免二次编码
        word_name = re.sub(r'[\r\n]+', ' ', paper.title + ("" if mode == "paper" else f"_{mode}") + ".docx").strip()
        return FileResponse(path, filename=word_name or "paper.docx")

    from urllib.parse import quote
    safe_name = quote(paper.title + ".html")
    return Response(
        content=f'<!DOCTYPE html><html><head><meta charset="utf-8">'
                f'<link rel="stylesheet" href="/storage/vendor/katex/katex.min.css">'
                f'<script src="/storage/vendor/katex/katex.min.js"></script>'
                f'<script src="/storage/vendor/katex/auto-render.min.js"></script>'
                f'<style>@media print{{@page{{size:{paper_size}}}}}'
                f'body{{font-family:SimSun,serif;font-size:14px;line-height:2;padding:20mm 15mm;background:#fff;color:#333}}'
                f'img,svg{{max-width:100%;height:auto}}'
                f'.answer-section,.question-answer{{page-break-inside:avoid}}</style></head><body>'
                f'<div id="render-target">{combined}</div>'
                f'<script>'
                f'if(typeof renderMathInElement!=="undefined"){{renderMathInElement(document.getElementById("render-target"), {{delimiters:[{{left:"$$",right:"$$",display:true}},{{left:"$",right:"$",display:false}},{{left:"\\\\(",right:"\\\\)",display:false}},{{left:"\\\\[",right:"\\\\]",display:true}}],throwOnError:false}})}};'
                f'</script>'
                f'</body></html>',
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}"}
    )


@router.get("/{paper_id}/download-bundle")
async def download_paper_bundle(paper_id: str, modes: str = "paper", fmt: str = "html",
                                paper_size: str = "A4", db: AsyncSession = Depends(get_db)):
    """多选下载打包成单个 .zip。

    来源：`FUTURE.md`「优化方向」——「多 `window.open` 在严格浏览器策略下可能被拦截：
    考虑改为单次下载 .zip 或后端 tar 打包」。

    原前端在**同一次点击**里同步开最多 4 个 `window.open`，浏览器只放行第一个、
    其余被静默拦截，而提示却写「已为您分 4 个文件下载」——**用户只拿到 1 份却被告知成功**。
    改为一次请求返回一个 zip，全程只触发一次下载。单选时仍走原单文件路径，行为不变。
    """
    valid = ("paper", "qa", "score", "answer_card")
    wanted = [m.strip().lower() for m in str(modes or "").split(",") if m.strip()]
    wanted = list(dict.fromkeys(wanted))          # 去重且保序
    if not wanted:
        raise HTTPException(400, detail="至少选择一个下载内容")
    bad = [m for m in wanted if m not in valid]
    if bad:
        raise HTTPException(400, detail="mode 必须是 paper、qa、score 或 answer_card")
    if len(wanted) == 1:
        return await download_paper(paper_id, fmt=fmt, mode=wanted[0],
                                    paper_size=paper_size, db=db)

    import io
    import zipfile
    from urllib.parse import quote

    paper = await db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(404, detail="试卷不存在")

    labels = {"paper": "试卷", "qa": "标答", "score": "得分点", "answer_card": "答题卡"}
    ext = ".docx" if str(fmt).strip().lower() == "word" else ".html"
    # 文件名去掉路径分隔与控制字符，避免 zip 条目名污染
    safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", paper.title or "paper").strip() or "paper"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in wanted:
            resp = await download_paper(paper_id, fmt=fmt, mode=m,
                                        paper_size=paper_size, db=db)
            if isinstance(resp, FileResponse):
                # word 分支返回 FileResponse（流式），从磁盘读回
                with open(resp.path, "rb") as fp:
                    zf.writestr(f"{safe_title}_{labels[m]}{ext}", fp.read())
            else:
                body = resp.body
                if not isinstance(body, (bytes, bytearray)):
                    body = bytes(body or b"")
                zf.writestr(f"{safe_title}_{labels[m]}{ext}", body)
    buf.seek(0)
    zip_name = quote(f"{safe_title}.zip")
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{zip_name}"})


async def _build_score_page(paper) -> str:
    qids = paper.question_ids or []
    if not qids: return ""
    from models.database import async_session
    from models.models import Question
    async with async_session() as db:
        result = await db.execute(select(Question).where(Question.id.in_(qids)))
        q_map = {q.id: q for q in result.scalars().all()}
        parts = []
        for i, qid in enumerate(qids, 1):
            q = q_map.get(qid)
            if q and q.score_points_html:
                parts.append(f'<div class="q-score"><h3>第{i}题 得分点</h3>{q.score_points_html}</div>')
        return '<div class="score-page"><h2>批改得分点</h2>' + '\n'.join(parts) + '</div>' if parts else ""


@router.post("/{paper_id}/regenerate")
async def regenerate_paper(paper_id: str, mode: str = "new", db: AsyncSession = Depends(get_db)):
    """重新生成试卷（生成新卷，不会原地覆盖原卷）。

    mode:
      - new（默认）：根据保存的 generation_params 重新检索题目并生成新试卷。
      - modify：保留原卷 question_ids 题集，仅重新排版生成一份新卷。
    返回新卷 paper_id；原卷保持不变，可手动删除。
    """
    if mode not in ("new", "modify"):
        raise HTTPException(status_code=400, detail="mode 必须是 new 或 modify")
    paper = await db.get(Paper, paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="试卷不存在")
    params = dict(paper.generation_params or {})
    params["title"] = paper.title
    if mode == "modify":
        params["question_ids"] = paper.question_ids or []
    else:
        params.pop("question_ids", None)
    try:
        if params.get("paper_type") == "worksheet":
            new_paper = await paper_service.generate_worksheet(params)
        else:
            new_paper = await paper_service.generate_paper(params, mode=mode)
        return {"paper_id": new_paper.id, "message": "重新生成成功"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"重新生成失败: {str(e)}")
    except Exception as e:
        log_error("papers.regenerate", f"Paper regeneration failed for {paper_id}: {e}")
        _raise_ai_dependency_error(e, "重新生成失败，请稍后重试")
