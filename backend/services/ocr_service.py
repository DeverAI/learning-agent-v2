import os
import re
import base64
import asyncio
import weakref
from datetime import datetime, timezone

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.models import Question, ProcessingTask
from models.database import async_session
from services.ai_service import ai_service
from services.diagram_service import _is_valid_question_id
from config import STORAGE_DIR, QUESTIONS_DIR, ENABLE_STRUCTURE_GRAPH, ENABLE_COMPARISON_MODE, _atomic_write_json
from logger import get_logger, log_error


def _is_path_within(folder: str, path: str) -> bool:
    """跨盘符、空路径和非法路径都按不在题目目录内处理。"""
    if not folder or not path:
        return False
    try:
        return os.path.commonpath([os.path.abspath(folder), os.path.abspath(path)]) == os.path.abspath(folder)
    except (OSError, ValueError):
        return False


def _remap_diagram_markers(html: str, marker_map: dict) -> str:
    """按 {AI标记号: 实际diagrams位置} 重写 [[DIAGRAM:N]] 标记。

    部分图生成失败时 diagrams 列表会紧凑化（成功项前移），
    不重映射会导致后续标记渲染出前一张图的错位内容。
    映射中不存在的标记号（生成失败的图或越界幻觉）直接移除。
    """
    if not isinstance(html, str) or "[[DIAGRAM:" not in html:
        return html

    def _sub(m):
        try:
            n = int(m.group(1))
        except ValueError:
            return ""
        target = marker_map.get(n)
        return f"[[DIAGRAM:{target}]]" if target is not None else ""

    return re.sub(r"\[\[DIAGRAM:(\d+)\]\]", _sub, html)


def _replace_diagram_markers(html: str, diagrams: list) -> str:
    """Replace [[DIAGRAM:N]] markers in HTML with actual <img> tags, including explicit width to prevent compression."""
    def _safe_diagram_url(path: str) -> str | None:
        if not path or not isinstance(path, str):
            return None
        if not re.fullmatch(r"/storage/questions/[a-zA-Z0-9_-]+/(?:diagram_\d+|reference)\.svg", path):
            return None
        return path

    def _safe_diagram_path(path: str) -> str | None:
        safe_url = _safe_diagram_url(path)
        if not safe_url:
            return None
        return os.path.join(STORAGE_DIR, safe_url[len("/storage/"):])

    def _replacer(m):
        try:
            idx = int(m.group(1))
        except ValueError:
            return m.group(0)
        if idx < len(diagrams):
            d = diagrams[idx]
            path = d["path"] if isinstance(d, dict) else d
            path = _safe_diagram_url(path)
            if not path:
                return m.group(0)
            # Get explicit width from SVG file if available
            w, h = "", ""
            if isinstance(d, dict) and d.get("w"):
                w = str(d["w"])
            else:
                # Try to read viewBox from file
                try:
                    svg_disk_path = _safe_diagram_path(path)
                    if svg_disk_path and os.path.exists(svg_disk_path):
                        with open(svg_disk_path, "r", encoding="utf-8") as _f:
                            _content = _f.read(16384)
                        _m = re.search(r'viewBox\s*=\s*["\']?\s*[\d.-]+\s+[\d.-]+\s+([\d.]+)\s+([\d.]+)', _content)
                        if _m:
                            w = str(int(float(_m.group(1))))
                            h = str(int(float(_m.group(2))))
                except Exception:
                    pass
            size_attr = f' width="{w}"' if w else ""
            return f'<div class="diagram"><img src="{path}" style="max-width:80%;height:auto"{size_attr} alt="示意图"></div>'
        return m.group(0)
    return re.sub(r'\[\[DIAGRAM:(\d+)\]\]', _replacer, html)

logger = get_logger()

# R36：SQLite 单写者。HTTP retry 一次点 4 题 + 外部脚本抢写 → audit 写锁把整题打成 error。
# 限制同时跑的 process_image 数量（不是去掉写，是排队）。
_PROCESS_SEM = None

def _process_sem():
    global _PROCESS_SEM
    if _PROCESS_SEM is None:
        _PROCESS_SEM = asyncio.Semaphore(2)
    return _PROCESS_SEM


def _image_mime(path: str) -> str:
    return {
        ".png": "image/png", ".webp": "image/webp",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }.get(os.path.splitext(path or "")[1].lower(), "image/jpeg")


def _safe_float(v):
    """安全将值转为 float；非数字或 None 返回 None，保留合法 0。"""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_solution_sections(answer_html: str, score_points_html: str) -> tuple[str, str]:
    """Normalize the solver's two supported answer formats without dropping content.

    Newer prompts return ``score_points_html`` separately, while older responses may
    place it before ``<!-- SCORE_SPLIT -->`` inside ``answer_html``.  The detailed
    answer must be persisted in both cases.
    """
    answer_raw = str(answer_html or "")
    explicit_score = str(score_points_html or "")
    split_score = ""
    if "<!-- SCORE_SPLIT -->" in answer_raw:
        split_score, answer_raw = answer_raw.split("<!-- SCORE_SPLIT -->", 1)
    return answer_raw.strip(), (explicit_score.strip() or split_score.strip())


async def _lock_question(db: AsyncSession, question_id: str):
    """使用 SELECT ... FOR UPDATE 重新加载题目，确保后续写入基于最新状态。
    注意：当前后端为 SQLite，SQLAlchemy 会静默忽略 with_for_update()；
    但 SQLite WAL 模式已保证写操作串行化，仍可避免并发覆盖。"""
    result = await db.execute(select(Question).where(Question.id == question_id).with_for_update())
    return result.scalar_one_or_none()


class OCRService:

    def __init__(self):
        # WeakValueDictionary 自动释放不再被引用的锁，避免长期运行后内存无限增长
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._locks_lock = asyncio.Lock()

    async def _acquire_question_lock(self, question_id: str) -> asyncio.Lock:
        async with self._locks_lock:
            lock = self._locks.get(question_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[question_id] = lock
            return lock

    async def process_image(self, question_id: str, user_hint: str = "",
                            user_tags: list = None, user_grade: str = "",
                            multi_images: list = None,
                            skip_ocr: bool = False,
                            existing_ocr_text: str = "",
                            existing_subject: str = "",
                            existing_grade: str = "",
                            existing_tags: list = None,
                            search_only: bool = False,
                            search_only_final_status: str = "search_done"):
        if not question_id:
            logger.warning("process_image called with empty question_id")
            return
        if not _is_valid_question_id(question_id):
            logger.warning("process_image called with invalid question_id: %s", question_id)
            return
        lock = await self._acquire_question_lock(question_id)
        async with lock:
            async with _process_sem():
                return await self._process_image_locked(
                    question_id, user_hint, user_tags, user_grade,
                    multi_images, skip_ocr, existing_ocr_text,
                    existing_subject, existing_grade, existing_tags,
                    search_only, search_only_final_status,
                )

    async def _process_image_locked(self, question_id, user_hint, user_tags, user_grade,
                                    multi_images, skip_ocr, existing_ocr_text,
                                    existing_subject, existing_grade, existing_tags,
                                    search_only, search_only_final_status):
            logger.info("Acquired OCR lock for question %s", question_id)
            task_id = None
            # 记录识别过程中**降级**的图片（某张图 OCR 失败、被 except 跳过）。
            # 之前这些失败只在日志里，题目照样可能判成 done —— 用户看到的是
            # "识别完成"，实际题干或手写作答缺了一块。这里收集起来，在终态写库时落成
            # audit_flags，让"内容可能不完整"这件事对用户可见。
            ocr_failures: list[str] = []
            async with async_session() as db:
                q = await db.get(Question, question_id)
                if not q:
                    return
                if q.status in ("done", "search_done"):
                    logger.info("Question %s already reached terminal status; skip duplicate OCR task", question_id)
                    return
                if multi_images is None:
                    multi_images = list(getattr(q, "multi_images", None) or [])
                # 已锁定题目禁止自动重新处理，防止覆盖用户确认过的内容
                if getattr(q, "is_resolved", False):
                    logger.info("Question %s is resolved, skip process_image", question_id)
                    return
                if user_grade:
                    q.grade = user_grade
                if user_tags:
                    q.knowledge_tags = user_tags
                await db.commit()

            async with async_session() as db:
                task = ProcessingTask(question_id=question_id, task_type="ocr", status="processing", progress=0.0)
                db.add(task)
                await db.commit()
                await db.refresh(task)
                task_id = task.id

            try:
                await self._update_task(task_id, progress=0.1)

                if skip_ocr:
                    # Use existing OCR data instead of re-OCR'ing
                    async with async_session() as db:
                        q = await db.get(Question, question_id)
                        if q:
                            ocr_text = existing_ocr_text or q.ocr_text or ""
                            subject = existing_subject or q.subject or ""
                            final_grade = existing_grade or user_grade or q.grade or ""
                            tags = list(set((user_tags or []) + (existing_tags or q.knowledge_tags or [])))
                            region = q.region or ""
                            avg_score_raw = q.avg_score
                            source_reference_text = q.handwriting_notes or ""
                            diagram_desc = q.diagram_description or ""
                        else:
                            # 题目在锁内被并发删除：直接终止，避免 None 解引用误导错误。
                            # 必须先把任务置为终态，否则留下永久 processing 的僵尸任务。
                            logger.warning("Question %s disappeared during skip_ocr, aborting", question_id)
                            await self._update_task(task_id, progress=1.0, status="error",
                                                    error_message="题目记录不存在（处理期间被删除）")
                            return
                    # skip_ocr path needs these variables defined for later use
                    enhanced_hint = user_hint or ""
                    await self._update_task(task_id, progress=0.5)
                else:
                    enhanced_hint = user_hint or ""

                    if multi_images:
                        question_images = [img for img in multi_images if img.get("role") == "question" and os.path.exists(img.get("path", ""))]
                        aux_images = [img for img in multi_images if img.get("role") in ("analysis", "answer") and os.path.exists(img.get("path", ""))]
                        if not question_images and multi_images:
                            question_images = [img for img in multi_images if os.path.exists(img.get("path", ""))]

                        await self._update_task(task_id, progress=0.2)

                        async def _ocr_one(img: dict) -> dict:
                            with open(img["path"], "rb") as f:
                                b64 = base64.b64encode(f.read()).decode()
                            mime = _image_mime(img["path"])
                            return await ai_service.kimi_ocr(b64, user_hint, user_tags, user_grade, mime)

                        question_results = []
                        for img in question_images:
                            try:
                                question_results.append(await _ocr_one(img))
                            except Exception as _ocr_e:
                                logger.warning("OCR failed for question image %s: %s", img.get("path"), _ocr_e)
                                ocr_failures.append(
                                    f"{os.path.basename(str(img.get('path') or '?'))}"
                                    f"（题干图：{type(_ocr_e).__name__}）")

                        ocr_text = "\n\n".join(
                            r.get("ocr_text", "") for r in question_results if r.get("ocr_text")
                        )
                        subject = next((r.get("subject", "") for r in question_results if r.get("subject")), "")
                        final_grade = user_grade or next((r.get("grade", "") for r in question_results if r.get("grade")), "")
                        tags = list(set(
                            (user_tags or []) +
                            [tag for r in question_results for tag in (r.get("knowledge_tags") or [])]
                        ))
                        region = next((r.get("region", "") for r in question_results if r.get("region")), "")
                        avg_score_raw = next((r.get("avg_score") for r in question_results if r.get("avg_score") is not None), None)
                        handwritten_ans = "\n\n".join(
                            r.get("handwritten_answer", "") for r in question_results if r.get("handwritten_answer")
                        )
                        source_reference_parts = [handwritten_ans] if handwritten_ans else []
                        diagram_desc = "\n\n".join(
                            r.get("diagram_description", "") for r in question_results if r.get("diagram_description")
                        )

                        # 解析/答案角色是待核对的来源材料，不混入用户指令，避免解题模型迎合。
                        aux_reference_texts = []
                        for img in aux_images:
                            try:
                                res = await _ocr_one(img)
                                piece = res.get("ocr_text", "")
                                if piece:
                                    aux_reference_texts.append(f"[{img.get('role', 'aux')} 图片 OCR]: {piece}")
                                if res.get("handwritten_answer"):
                                    aux_reference_texts.append(f"[{img.get('role', 'aux')} 手写参考答案]: {res['handwritten_answer']}")
                                if res.get("diagram_description"):
                                    aux_reference_texts.append(f"[{img.get('role', 'aux')} 示意图描述]: {res['diagram_description']}")
                            except Exception as _ocr_e:
                                logger.warning("OCR failed for aux image %s: %s", img.get("path"), _ocr_e)
                                ocr_failures.append(
                                    f"{os.path.basename(str(img.get('path') or '?'))}"
                                    f"（{img.get('role', 'aux')} 图：{type(_ocr_e).__name__}）")
                        source_reference_parts.extend(aux_reference_texts)
                        source_reference_text = "\n\n".join(source_reference_parts)

                        await self._update_task(task_id, progress=0.5)
                    else:
                        image_path = await self._get_image(question_id)
                        with open(image_path, "rb") as f:
                            image_b64 = base64.b64encode(f.read()).decode()
                        img_mime = _image_mime(image_path)

                        await self._update_task(task_id, progress=0.2)

                        ocr_result = await ai_service.kimi_ocr(image_b64, user_hint, user_tags, user_grade, img_mime)
                        await self._update_task(task_id, progress=0.5)

                        subject = ocr_result.get("subject", "")
                        final_grade = user_grade or ocr_result.get("grade", "")
                        tags = list(set((user_tags or []) + ocr_result.get("knowledge_tags", [])))
                        ocr_text = ocr_result.get("ocr_text", "")
                        region = ocr_result.get("region", "")
                        avg_score_raw = ocr_result.get("avg_score")
                        handwritten_ans = ocr_result.get("handwritten_answer", "")
                        source_reference_text = handwritten_ans
                        diagram_desc = ocr_result.get("diagram_description", "")

                    # OCR 图形描述和来源答案走独立参数，不提升为用户指令或可靠事实。

                async with async_session() as db:
                    q = await db.get(Question, question_id)
                    if q is None:
                        # 题目在 OCR 期间被并发删除：立即终止，避免对不存在的题目
                        # 继续烧解题/自检/验证/画图的全部 API 调用。
                        logger.warning("Question %s disappeared after OCR, aborting pipeline", question_id)
                        await self._update_task(task_id, progress=1.0, status="error",
                                                error_message="题目记录不存在（处理期间被删除）")
                        return
                    if q:
                        if not (ocr_text or "").strip():
                            raise ValueError("OCR 未识别到有效文字，请确认图片清晰且包含题目或作答内容")
                        q.subject = subject
                        q.grade = final_grade
                        q.knowledge_tags = tags
                        q.ocr_text = ocr_text
                        if not skip_ocr:
                            q.diagram_description = diagram_desc
                        q.region = region
                        q.avg_score = _safe_float(avg_score_raw)
                        q.user_hint = enhanced_hint
                        if not skip_ocr:
                            q.handwriting_notes = source_reference_text
                        # 搜题模式：只做 OCR，不进入解题流程
                        if search_only:
                            q.status = search_only_final_status
                            await db.commit()
                            await self._update_task(task_id, progress=1.0, status="done", result={"search_only": True})
                            return
                        q.status = "generating_solution"
                        await db.commit()

                await self._update_task(task_id, progress=0.6)

                # OCR 视觉模型先忠实复刻原题图，再把该 SVG 作为解题事实传给推理模型。
                # 搜题/批改的 search_only 分支已在上方返回，不产生这项额外视觉开销。
                reference_svg, reference_svg_path = await self._ensure_reference_svg(
                    question_id, ocr_text, diagram_desc, multi_images
                )

                from services.user_profile import load_profile
                profile = load_profile()
                style_notes = profile.get("style_notes", "") or profile.get("notation_preferences", "")

                solution = await ai_service.deepseek_solve(
                    subject=subject, grade=final_grade, ocr_text=ocr_text,
                    knowledge_tags=tags, user_hint=enhanced_hint, region=region,
                    avg_score=_safe_float(avg_score_raw),
                    style_notes=style_notes,
                    reference_svg=reference_svg,
                    reference_svg_description=diagram_desc,
                    source_reference=source_reference_text,
                )
                if not isinstance(solution, dict):
                    logger.warning("deepseek_solve returned non-object for %s: %r", question_id, solution)
                    solution = {}
                await self._update_task(task_id, progress=0.8)

                from services.question_challenge import (
                    combine_question_challenges, normalize_question_challenge,
                )
                solver_challenge = normalize_question_challenge(
                    solution.get("question_challenge"), source="solver"
                )

                # Validate and fix standard_answer if empty
                sa = solution.get("standard_answer", "")
                if solver_challenge["level"] == "high" and not (sa or "").strip():
                    sa = "题目疑似存在矛盾或条件不足，无法唯一确定"
                    solution["standard_answer"] = sa

                # Try once to get standard_answer if empty
                max_sa_retries = 2
                for sa_attempt in range(max_sa_retries):
                    if sa and sa.strip():
                        break
                    logger.warning(
                        "standard_answer is empty for %s (attempt %d/%d), requesting AI to fill",
                        question_id, sa_attempt + 1, max_sa_retries)
                    try:
                        fix_prompt = (
                            f"你是{final_grade}{subject}教师。以下是题目和AI解答。\n"
                            f"【题目】\n{ocr_text}\n\n"
                            f"【当前解答】\n{solution.get('answer_html', '')}\n\n"
                            "请给出这道题的标准答案。即使是解答题也要给出关键答案要点。\n"
                            "只输出标准答案文本，不要多余文字。"
                        )
                        fix_msg = [{"role": "user", "content": fix_prompt}]
                        fix_sa = await ai_service.deepseek_chat(fix_msg, max_tokens=4096, scope="solve")
                        if fix_sa and fix_sa.strip():
                            sa = fix_sa.strip()
                            solution["standard_answer"] = sa
                    except Exception as sa_e:
                        logger.warning("Failed to fill standard_answer for %s: %s", question_id, sa_e)
                    if not sa or not sa.strip():
                        await asyncio.sleep(1)

                # Final fallback: extract standard_answer from answer_html if still empty
                if not sa or not sa.strip():
                    answer_html = solution.get("answer_html", "")
                    # Try "答：" section - look for patterns like "答：xxx" or "答案：xxx"
                    import re as _re2
                    for pattern in [
                        r'[【\[]?标准答案[】\]][：:]\s*(.+?)(?:<br|<p|</p|<div|</div|$|<!)',
                        r'[【\[]?答[】\]][：:]\s*(.+?)(?:<br|<p|</p|<div|</div|$|<!)',
                        r'class="[^"]*standard-answer[^"]*"[^>]*>\s*(.+?)\s*<',
                        r'<strong>\s*答案[：:]\s*</strong>\s*(.+?)(?:<br|<p|</p|<div|</div|$)',
                    ]:
                        m = _re2.search(pattern, answer_html)
                        if m:
                            sa = m.group(1).strip()
                            if sa:
                                # Strip HTML tags from extracted answer
                                sa = _re2.sub(r'<[^>]+>', '', sa).strip()[:200]
                                solution["standard_answer"] = sa
                                break

                diagram_prompts = solution.get("diagram_prompts", [])
                if not isinstance(diagram_prompts, list):
                    diagram_prompts = []
                # AI 未返回 place 时，第一张图默认放题面，其余放解答，与 prompt 中
                # "题面1张（question）+ 解答1张（answer 辅助线）" 的约定一致
                if isinstance(solution.get("diagram_places"), list):
                    generated_places = solution["diagram_places"][:len(diagram_prompts)]
                else:
                    generated_places = ["question"] + ["answer"] * max(0, len(diagram_prompts) - 1)
                    generated_places = generated_places[:len(diagram_prompts)]
                # 原题参考图占据题面唯一事实位；后续模型生成图只能作为解题辅助图。
                if reference_svg_path:
                    generated_places = ["answer"] * len(diagram_prompts)
                diagrams = []
                diagram_places = []
                # AI 标记号 → diagrams 实际位置的映射：
                # 有参考图时标记0=参考图、辅助图从1开始；无参考图时从0开始。
                marker_map = {}
                if reference_svg_path:
                    diagrams.append({"path": reference_svg_path, "place": "question", "source": "ocr_reference"})
                    diagram_places.append("question")
                    marker_map[0] = 0
                if diagram_prompts:
                    from services.diagram_service import diagram_service as ds
                    base = len(diagrams)
                    for i, dp in enumerate(diagram_prompts):
                        path = await ds.generate_diagram(question_id, dp, i)
                        if path:
                            place = generated_places[i] if i < len(generated_places) else "answer"
                            if place not in ("question", "answer"):
                                place = "answer"
                            marker_map[base + i] = len(diagrams)
                            diagrams.append({"path": path, "place": place, "source": "solution"})
                            diagram_places.append(place)

                # 按实际成功位置重映射标记：全部成功时是恒等映射（无变化），
                # 部分失败时防止 [[DIAGRAM:N]] 图文错位，同时清掉越界/失败标记
                solution["question_html"] = _remap_diagram_markers(solution.get("question_html", "") or "", marker_map)
                solution["answer_html"] = _remap_diagram_markers(solution.get("answer_html", "") or "", marker_map)
                solution["score_points_html"] = _remap_diagram_markers(solution.get("score_points_html", "") or "", marker_map)

                if reference_svg:
                    question_html = solution.get("question_html", "") or ""
                    if "[[DIAGRAM:0]]" not in question_html:
                        solution["question_html"] = question_html + '<div>[[DIAGRAM:0]]</div>'

                # Self-review: have AI review and tidy up its own output
                review_challenge = normalize_question_challenge({}, source="self_review")
                try:
                    reviewed = await ai_service.deepseek_self_review(
                        solution.get("question_html", ""),
                        solution.get("answer_html", ""),
                        solution.get("standard_answer", ""),
                        subject, final_grade, reference_svg=reference_svg,
                        ocr_text=ocr_text, source_reference=source_reference_text,
                        current_challenge=solver_challenge,
                    )
                    review_challenge = normalize_question_challenge(
                        reviewed.get("question_challenge"), source="self_review"
                    )
                    if reviewed.get("question_html"):
                        solution["question_html"] = reviewed["question_html"]
                    if reviewed.get("answer_html"):
                        reviewed_answer, reviewed_score = _split_solution_sections(
                            reviewed["answer_html"], reviewed.get("score_points_html", "")
                        )
                        if reviewed_score.strip():
                            solution["answer_html"] = reviewed_answer
                            solution["score_points_html"] = reviewed_score
                        else:
                            logger.warning("Skipped self-review answer replacement without matching score points for %s", question_id)
                    if reviewed.get("standard_answer"):
                        solution["standard_answer"] = reviewed["standard_answer"]
                except Exception as e:
                    logger.warning("Self-review skipped for %s: %s", question_id, e)

                # Second-verification: independent DeepSeek validates (max 2 rounds retry)
                # 每轮版本保留到 solution_versions 供夜间巡检查阅修正
                import json as _json_module
                solution_versions = [{
                    "round": 0,
                    "question_html": solution.get("question_html", ""),
                    "answer_html": solution.get("answer_html", ""),
                    "standard_answer": solution.get("standard_answer", ""),
                    "score_points_html": solution.get("score_points_html", ""),
                    "question_challenge": solver_challenge,
                    "timestamp": _utcnow().isoformat(),
                }]
                verify_flagged = False
                verify_challenges = []
                MAX_VERIFY_ROUNDS = 2
                for v_round in range(1, MAX_VERIFY_ROUNDS + 1):
                    try:
                        verified = await ai_service.deepseek_verify(
                            ocr_text, solution.get("question_html", ""),
                            solution.get("answer_html", ""),
                            solution.get("standard_answer", ""),
                            subject, final_grade, reference_svg=reference_svg,
                            source_reference=source_reference_text,
                        )
                        verified_challenge = normalize_question_challenge(
                            verified.get("question_challenge"), source=f"verify_{v_round}"
                        )
                        verdict = str(verified.get("verdict", "approve") or "approve").lower()
                        if verdict == "challenge" and verified_challenge["level"] == "none":
                            verified_challenge = normalize_question_challenge({
                                "level": "low",
                                "confidence": 0.6,
                                "issue_types": ["other"],
                                "reasons": [verified.get("reason", "独立验证认为题目需要人工核对")],
                            }, source=f"verify_{v_round}")
                        verify_challenges.append(verified_challenge)
                        if verdict == "replace":
                            reason = verified.get("reason", "未说明")
                            logger.warning("Verify round %d/%d: answer replaced for %s. Reason: %s",
                                           v_round, MAX_VERIFY_ROUNDS, question_id, reason)
                            replacement_answer = str(verified.get("correct_answer_html", "") or "")
                            replacement_score = str(verified.get("correct_score_points_html", "") or "")
                            if replacement_answer and not replacement_score and "<!-- SCORE_SPLIT -->" not in replacement_answer:
                                verify_flagged = True
                                logger.warning("Verify replacement missing matching score points for %s", question_id)
                                continue
                            replacement_answer, replacement_score = _split_solution_sections(
                                replacement_answer, replacement_score
                            )
                            # Apply corrections (including question_html)
                            if verified.get("correct_question_html"):
                                solution["question_html"] = verified["correct_question_html"]
                            if replacement_answer:
                                solution["answer_html"] = replacement_answer
                            if verified.get("correct_standard_answer"):
                                solution["standard_answer"] = verified["correct_standard_answer"]
                            if replacement_score:
                                solution["score_points_html"] = replacement_score
                            # Save this version
                            solution_versions.append({
                                "round": v_round,
                                "question_html": solution.get("question_html", ""),
                                "answer_html": solution.get("answer_html", ""),
                                "standard_answer": solution.get("standard_answer", ""),
                                "score_points_html": solution.get("score_points_html", ""),
                                "question_challenge": verified_challenge,
                                "timestamp": _utcnow().isoformat(),
                                "fixed_by_verify": True,
                                "reason": reason,
                            })
                            # If we replaced, verify again next round (unless this is the last round)
                            if v_round < MAX_VERIFY_ROUNDS:
                                continue  # re-verify the fixed version
                            else:
                                # 最后轮次仍被替换 → 标记为重点巡检题目
                                verify_flagged = True
                                logger.warning("Verify: max rounds exhausted for %s, flagging for audit", question_id)
                        elif verdict == "challenge":
                            logger.warning("Verify: question challenged for %s (round %d/%d)",
                                           question_id, v_round, MAX_VERIFY_ROUNDS)
                        else:
                            # verdict == "approve" → confirmed correct
                            logger.info("Verify: answer approved for %s (round %d/%d)", question_id, v_round, MAX_VERIFY_ROUNDS)
                        break  # exit retry loop on approve or final round
                    except Exception as e:
                        logger.warning("Verify round %d/%d failed for %s: %s", v_round, MAX_VERIFY_ROUNDS, question_id, str(e)[:200])
                        # Save current version even on exception for audit trail
                        solution_versions.append({
                            "round": v_round,
                            "question_html": solution.get("question_html", ""),
                            "answer_html": solution.get("answer_html", ""),
                            "standard_answer": solution.get("standard_answer", ""),
                            "score_points_html": solution.get("score_points_html", ""),
                            "timestamp": _utcnow().isoformat(),
                            "verify_exception": str(e)[:200],
                        })
                        if v_round >= MAX_VERIFY_ROUNDS:
                            verify_flagged = True
                            logger.warning("Verify: all rounds failed for %s, flagging for audit", question_id)
                        continue  # retry on exception
                combined_challenge = combine_question_challenges(
                    solver_challenge, review_challenge, *verify_challenges
                )
                from services.audit_service import set_question_challenge
                # R36：审计写库失败不得把已解出的题打成 error（真发生过：database is locked）。
                try:
                    await set_question_challenge(question_id, combined_challenge, auto=True)
                except Exception as _ace:
                    logger.warning("set_question_challenge failed for %s (non-fatal): %s",
                                   question_id, str(_ace)[:200])
                # Save version history to question folder for night audit
                try:
                    _v_path = os.path.join(QUESTIONS_DIR, question_id, "solution_versions.json")
                    os.makedirs(os.path.dirname(_v_path), exist_ok=True)
                    _atomic_write_json(_v_path, solution_versions)
                except Exception as _ve:
                    logger.debug("Failed to save solution_versions for %s: %s", question_id, _ve)
                # Flag question for night audit if verify failed
                if verify_flagged:
                    try:
                        from services.audit_service import flag_question as _flag_q
                        await _flag_q(question_id, "verify_needs_review",
                                      f"二次验证未通过（{MAX_VERIFY_ROUNDS}轮重试后仍有问题），版本历史已保存", auto=True)
                    except Exception as _fe:
                        logger.warning("Failed to flag question %s for audit: %s", question_id, _fe)

                # ===== 图一致性校验：检查题面图和解答图中几何关系是否一致 =====
                if len(diagrams) >= 2:
                    try:
                        # 收集各图的place信息和简要描述
                        q_diagrams = [d for d in diagrams if d.get("place") == "question"]
                        a_diagrams = [d for d in diagrams if d.get("place") == "answer"]
                        if q_diagrams and a_diagrams:
                            diagram_prompt_text = "\n".join(
                                f"图{i}: {dp[:200]}" for i, dp in enumerate(diagram_prompts[:8])
                            )
                            consistency_prompt = (
                                "你是几何图形审核专家。以下题目生成了多个示意图，请检查它们之间是否存在几何关系矛盾。\n"
                                f"【题目】\n{ocr_text[:500]}\n\n"
                                f"【图描述列表】\n{diagram_prompt_text}\n\n"
                                "检查：\n"
                                "1. 相同标注点（如A/B/C）的位置/大小关系在所有图中是否一致（如AB>BC应在所有图中成立）\n"
                                "2. 直角/平行等特殊关系标注是否正确\n"
                                "3. 图与题面文字描述是否吻合\n"
                                "输出JSON:\n"
                                '{"issues":[{"diagram_idx":0,"problem":"描述问题"},...], "verdict":"pass或warn"}'
                            )
                            cons_result = await ai_service.deepseek_json(
                                [{"role": "user", "content": consistency_prompt}],
                                max_tokens=2048, scope="solve"
                            )
                            issues = cons_result.get("issues", [])
                            if issues:
                                logger.warning("Diagram consistency issues for %s: %s",
                                              question_id, _json_module.dumps(issues, ensure_ascii=False)[:300])
                                # 标记问题但不阻断流程——夜间巡检会复查
                                try:
                                    from services.audit_service import flag_question as _flag_q2
                                    issue_desc = "; ".join(
                                        f"图{i.get('diagram_idx','?')}: {i.get('problem','')[:60]}"
                                        for i in issues[:5]
                                    )
                                    await _flag_q2(question_id, "diagram_inconsistent",
                                                  f"图一致性警告: {issue_desc}", auto=True)
                                except Exception as _fe:
                                    logger.debug("Failed to flag diagram consistency: %s", _fe)
                    except Exception as _de:
                        logger.debug("Diagram consistency check skipped for %s: %s", question_id, _de)

                # After self-review, if standard_answer is still empty, try to fill again
                after_review_sa = solution.get("standard_answer", "")
                if not after_review_sa or not after_review_sa.strip():
                    logger.warning("standard_answer still empty after self-review for %s, retrying", question_id)
                    try:
                        fix_prompt = (
                            f"你是{final_grade}{subject}教师。以下是题目和AI解答。\n"
                            f"【题目】\n{ocr_text}\n\n"
                            f"【当前解答】\n{solution.get('answer_html', '')}\n\n"
                            "请给出这道题的标准答案。输出JSON:\n"
                            '{"standard_answer":"标准答案"}'
                        )
                        fix_msg = [{"role": "user", "content": fix_prompt}]
                        fix_result = await ai_service.deepseek_json(fix_msg, max_tokens=4096, scope="solve")
                        if fix_result.get("standard_answer", "").strip():
                            solution["standard_answer"] = fix_result["standard_answer"]
                    except Exception as sa_e:
                        logger.warning("Failed to fill standard_answer after review for %s: %s", question_id, sa_e)

                # 审查模型可能重写题面；保存前再次保证原题参考图占位仍存在。
                if reference_svg:
                    question_html = solution.get("question_html", "") or ""
                    if "[[DIAGRAM:0]]" not in question_html:
                        solution["question_html"] = question_html + '<div>[[DIAGRAM:0]]</div>'

                # 最终保存前做完整性校验，早发现空内容/截断问题
                self._verify_solution(question_id, solution)

                async with async_session() as db:
                    q = await db.get(Question, question_id)
                    if q:
                        answer_body, score_body = _split_solution_sections(
                            solution.get("answer_html", ""),
                            solution.get("score_points_html", ""),
                        )
                        q.score_points_html = _replace_diagram_markers(score_body, diagrams)
                        q.answer_html = _replace_diagram_markers(answer_body, diagrams)
                        q.standard_answer = solution.get("standard_answer", "") or q.standard_answer or ""
                        q.question_type = solution.get("question_type", "") or "解答"
                        q.question_html = _replace_diagram_markers(solution.get("question_html", q.question_html), diagrams)
                        q.diagrams = diagrams
                        q.diagram_places = diagram_places
                        # 保存结构梳理图数据（模块开关开启时；如 AI 未输出则自动生成，失败时重试一次）
                        # P1#1: 若用户已手动编辑过结构图，重新处理时保留用户版本
                        existing_sg = q.structure_graph or {}
                        preserve_user_edit = isinstance(existing_sg, dict) and existing_sg.get("is_user_edited")
                        # 已锁定为已解决的题目不再由 AI 自动覆盖结构图
                        if ENABLE_STRUCTURE_GRAPH and not preserve_user_edit and not q.is_resolved:
                            from services.structure_graph_service import _sanitize_structure_graph
                            sg = solution.get("structure_graph")
                            # P2#3: 允许字符串形式的 structure_graph，统一交给 _sanitize_structure_graph 判断
                            if not sg:
                                for sg_attempt in range(2):
                                    try:
                                        generated = await ai_service.deepseek_generate_structure_graph(
                                            q.ocr_text or "", q.question_html or "", q.answer_html or "",
                                            q.standard_answer or "", q.subject or "", q.grade or ""
                                        )
                                        sg = generated.get("structure_graph") if isinstance(generated, dict) else None
                                        if sg is not None:
                                            break
                                    except Exception as sg_e:
                                        logger.warning("Auto generate structure_graph attempt %d failed for %s: %s",
                                                       sg_attempt + 1, question_id, sg_e)
                                        if sg_attempt == 0:
                                            await asyncio.sleep(1)
                                        sg = None
                                if sg is None:
                                    log_error("ocr_service", f"Auto generate structure_graph failed after retry for {question_id}")
                            sanitized = _sanitize_structure_graph(sg)
                            # P1#4: 写入新结构图时同步清空旧检查信息，避免评分对应旧图
                            # P2#11: 写入前使用 FOR UPDATE 重新加载题目，防止并发覆盖用户编辑/已解决标记
                            q = await _lock_question(db, question_id) or q
                            await db.refresh(q, ['structure_graph', 'is_resolved'])
                            current_sg = q.structure_graph or {}
                            if isinstance(current_sg, dict) and current_sg.get("is_user_edited"):
                                logger.info("Preserve user-edited structure_graph for %s", question_id)
                            elif q.is_resolved:
                                logger.info("Preserve structure_graph because question resolved for %s", question_id)
                            elif sanitized is not None:
                                q.structure_graph = sanitized
                                q.structure_graph_info = None
                                # P1#3: 自动生成后追加一次 AI 检查，若发现问题且 AI 给出修正版则自动修复
                                if sanitized.get("nodes"):
                                    try:
                                        check_result = await ai_service.deepseek_check_structure_graph(
                                            q.structure_graph,
                                            q.ocr_text or "", q.question_html or "", q.answer_html or "",
                                            q.standard_answer or "", q.subject or "", q.grade or ""
                                        )
                                        fixed = check_result.get("fixed_structure_graph")
                                        if (not check_result.get("valid", True)) and fixed:
                                            fixed_sanitized = _sanitize_structure_graph(fixed)
                                            if fixed_sanitized and fixed_sanitized.get("nodes"):
                                                # 再次确认用户未在 AI 检查期间并发编辑或锁定结构图
                                                await db.refresh(q, ['structure_graph', 'is_resolved'])
                                                current_sg2 = q.structure_graph or {}
                                                if isinstance(current_sg2, dict) and current_sg2.get("is_user_edited"):
                                                    logger.info("Preserve user-edited structure_graph after auto-check for %s", question_id)
                                                elif q.is_resolved:
                                                    logger.info("Preserve structure_graph after auto-check because resolved for %s", question_id)
                                                else:
                                                    q.structure_graph = fixed_sanitized
                                                    logger.info("Auto fixed structure_graph for %s", question_id)
                                                    # 原 check_result 针对修正前版本，不能沿用旧评分。
                                                    check_result = await ai_service.deepseek_check_structure_graph(
                                                        q.structure_graph,
                                                        q.ocr_text or "", q.question_html or "", q.answer_html or "",
                                                        q.standard_answer or "", q.subject or "", q.grade or ""
                                                    )
                                        # 若用户在 AI 检查期间编辑或锁定了结构图，则不要把针对旧图的评分信息写入
                                        await db.refresh(q, ['structure_graph', 'is_resolved'])
                                        current_sg3 = q.structure_graph or {}
                                        if isinstance(current_sg3, dict) and current_sg3.get("is_user_edited"):
                                            logger.info("Skip structure_graph_info after auto-check because user edited for %s", question_id)
                                        elif q.is_resolved:
                                            logger.info("Skip structure_graph_info after auto-check because resolved for %s", question_id)
                                        else:
                                            try:
                                                _score = int(check_result.get("score", 0))
                                            except (TypeError, ValueError):
                                                _score = 0
                                            q.structure_graph_info = {
                                                "valid": check_result.get("valid", True),
                                                "score": _score,
                                                "issues": check_result.get("issues", []),
                                                "suggestions": check_result.get("suggestions", []),
                                            }
                                    except Exception as check_e:
                                        logger.warning("Auto check structure_graph failed for %s: %s", question_id, check_e)
                            else:
                                # 校验失败时清空旧结构图及检查信息；先刷新确认题目未被锁定或用户编辑
                                await db.refresh(q, ['structure_graph', 'is_resolved'])
                                current_sg_invalid = q.structure_graph or {}
                                if isinstance(current_sg_invalid, dict) and current_sg_invalid.get("is_user_edited"):
                                    logger.info("Preserve user-edited structure_graph despite invalid AI output for %s", question_id)
                                elif q.is_resolved:
                                    logger.info("Preserve structure_graph because question resolved for %s", question_id)
                                else:
                                    q.structure_graph = {"nodes": [], "edges": []}
                                    q.structure_graph_info = None
                                    logger.warning("Auto generated structure_graph invalid for %s, cleared", question_id)
                        # 保存题目对比模式分区（模块开关开启且 solution 提供时；保留用户编辑/已解决标记）
                        if ENABLE_COMPARISON_MODE:
                            from services.structure_graph_service import _sanitize_comparison_regions
                            comparison_regions = solution.get("comparison_regions")
                            if comparison_regions:
                                await db.refresh(q, ['comparison_regions', 'is_resolved'])
                                current_cr = getattr(q, "comparison_regions", None) or {}
                                if isinstance(current_cr, dict) and current_cr.get("is_user_edited"):
                                    logger.info("Preserve user-edited comparison_regions for %s", question_id)
                                elif q.is_resolved:
                                    logger.info("Preserve comparison_regions because question resolved for %s", question_id)
                                else:
                                    sanitized_cr = _sanitize_comparison_regions(comparison_regions)
                                    if sanitized_cr is not None:
                                        q.comparison_regions = sanitized_cr
                                    else:
                                        logger.warning("Auto generated comparison_regions invalid for %s, skipped", question_id)
                        # Guard: question records and task records must reach the same terminal state.
                        has_question = bool((q.question_html or q.ocr_text or "").strip())
                        has_answer = len(q.answer_html or "") > 20
                        has_standard = bool(q.standard_answer and q.standard_answer.strip())
                        if not has_question:
                            q.status = "error"
                            q.error_message = "题目内容为空，请重新上传清晰图片后重试。"
                            logger.error("Question %s marked error: question content is empty", question_id)
                        elif not has_answer or not has_standard:
                            q.status = "error"
                            q.error_message = "AI生成的标准答案或详细解析不完整，可能被截断。请重试。"
                            logger.error("Question %s marked error: answer or standard answer is incomplete", question_id)
                        else:
                            q.status = "done"
                            # 识别有降级但内容仍然完整到可用 -> 判 done，但**必须留痕**，
                            # 否则用户看到的"已完成"掩盖了"有一张图没认出来"。
                            if ocr_failures:
                                flags = q.audit_flags if isinstance(q.audit_flags, list) else []
                                if not any(isinstance(f, dict) and f.get("type") == "ocr_partial_failure"
                                           for f in flags):
                                    flags.append({
                                        "type": "ocr_partial_failure",
                                        "reason": ("以下图片识别失败，题干或参考答案可能不完整："
                                                   + "；".join(ocr_failures[:5]))[:600],
                                        "auto": True,
                                        "created_at": _utcnow().isoformat(),
                                    })
                                    q.audit_flags = flags
                                logger.warning("Question %s done with %d failed image(s): %s",
                                               question_id, len(ocr_failures), ocr_failures[:5])
                        q.updated_at = _utcnow()
                        await db.commit()

                async with async_session() as db:
                    final_q = await db.get(Question, question_id)
                    final_status = final_q.status if final_q else "error"
                    final_error = final_q.error_message if final_q else "题目记录不存在"
                await self._update_task(
                    task_id,
                    progress=1.0,
                    status="done" if final_status == "done" else "error",
                    result={"diagrams_count": len(diagrams)} if final_status == "done" else None,
                    error_message=final_error or "AI 生成结果不完整",
                )

            except Exception as e:
                msg = str(e) or type(e).__name__
                logger.warning("OCR question=%s ERROR: %s", question_id, msg, exc_info=True)
                log_error("ocr_service", f"question={question_id} {msg}")
                await self._update_task(task_id, status="error", error_message=msg)
                async with async_session() as db:
                    q = await db.get(Question, question_id)
                    if q:
                        q.status = "error"
                        q.error_message = msg[:2000]
                        await db.commit()

    async def _get_image(self, question_id: str) -> str:
        async with async_session() as db:
            q = await db.get(Question, question_id)
            if q and q.raw_image_path and os.path.exists(q.raw_image_path):
                return q.raw_image_path
        raise FileNotFoundError(f"Image not found for question {question_id}")

    async def _ensure_reference_svg(self, question_id: str, ocr_text: str,
                                    diagram_description: str, multi_images: list = None) -> tuple[str, str]:
        """读取或生成原题参考 SVG，任何失败都持久化为可见状态。"""
        async with async_session() as db:
            q = await db.get(Question, question_id)
            if not q:
                return "", ""
            folder = os.path.abspath(q.folder_path or "")
            current_url = str(getattr(q, "reference_svg_path", "") or "")
            if current_url.startswith("/storage/"):
                current_disk = os.path.abspath(os.path.join(STORAGE_DIR, current_url[len("/storage/"):]))
            else:
                current_disk = os.path.abspath(current_url) if current_url else ""
            if (getattr(q, "reference_svg_status", "") == "ready" and current_disk and
                    _is_path_within(folder, current_disk) and
                    os.path.isfile(current_disk)):
                try:
                    with open(current_disk, "r", encoding="utf-8") as stream:
                        return stream.read(100_001)[:100_000], current_url
                except OSError as exc:
                    logger.warning("Failed to reuse reference SVG for %s: %s", question_id, exc)

            source_images = list(multi_images or getattr(q, "multi_images", None) or [])
            question_paths = [
                str(item.get("path", "")) for item in source_images
                if isinstance(item, dict) and item.get("role") == "question"
            ]
            if not question_paths and q.raw_image_path:
                question_paths = [q.raw_image_path]
            safe_paths = []
            for path in question_paths[:12]:
                abs_path = os.path.abspath(path)
                if _is_path_within(folder, abs_path) and os.path.isfile(abs_path):
                    safe_paths.append(abs_path)
            q.reference_svg_status = "pending"
            q.reference_svg_error = ""
            await db.commit()

        if not safe_paths:
            error = "原题图片不存在，无法生成参考 SVG"
            async with async_session() as db:
                q = await db.get(Question, question_id)
                if q:
                    q.reference_svg_status = "failed"
                    q.reference_svg_error = error
                    await db.commit()
            return "", ""

        image_payloads = []
        for path in safe_paths:
            with open(path, "rb") as stream:
                image_payloads.append({
                    "filename": os.path.basename(path),
                    "base64": base64.b64encode(stream.read()).decode(),
                    "mime_type": _image_mime(path),
                })
        try:
            raw_svg = await ai_service.glm4v_reference_svg(
                image_payloads, ocr_text=ocr_text, diagram_description=diagram_description
            )
            if not raw_svg:
                async with async_session() as db:
                    q = await db.get(Question, question_id)
                    if q:
                        q.reference_svg_path = ""
                        q.reference_svg_status = "not_required"
                        q.reference_svg_error = ""
                        await db.commit()
                return "", ""
            from services.diagram_service import diagram_service as ds
            saved = await ds.save_reference_svg(question_id, raw_svg)
            async with async_session() as db:
                q = await db.get(Question, question_id)
                if q:
                    q.reference_svg_path = saved["path"]
                    q.reference_svg_status = "ready"
                    q.reference_svg_error = ""
                    await db.commit()
            return saved["svg"], saved["path"]
        except Exception as exc:
            error = str(exc)[:1000] or type(exc).__name__
            logger.warning("Reference SVG generation failed for %s: %s", question_id, error)
            async with async_session() as db:
                q = await db.get(Question, question_id)
                if q:
                    q.reference_svg_path = ""
                    q.reference_svg_status = "failed"
                    q.reference_svg_error = error
                    await db.commit()
            return "", ""

    async def _update_task(self, task_id: str, progress: float = None,
                           status: str = None, result: dict = None,
                           error_message: str = None):
        async with async_session() as db:
            task = await db.get(ProcessingTask, task_id)
            if task:
                if progress is not None:
                    task.progress = progress
                if status is not None:
                    task.status = status
                if result is not None:
                    task.result = result
                if error_message is not None:
                    task.error_message = error_message
                task.updated_at = _utcnow()
                await db.commit()

    @staticmethod
    def _verify_solution(question_id: str, solution: dict):
        warnings = []
        q_html = solution.get("question_html", "")
        a_html = solution.get("answer_html", "")
        if not q_html:
            warnings.append("question_html为空")
        elif len(q_html) < 20:
            warnings.append(f"question_html过短({len(q_html)}字符)")
        if not a_html:
            warnings.append("answer_html为空")
        elif len(a_html) < 20:
            warnings.append(f"answer_html过短({len(a_html)}字符)")
        standard_answer = str(solution.get("standard_answer", "") or "").strip()
        if not standard_answer:
            warnings.append("standard_answer为空")
        if warnings:
            log_error("ocr_service",
                      f"question={question_id} solution可能不完整: {', '.join(warnings)}. "
                      f"q_html预览: {q_html[:100]}, a_html预览: {a_html[:100]}")
            logger.warning("Solution verification warnings for %s: %s",
                          question_id, ", ".join(warnings))
            # R36：简单题（如「解方程 2x+4=0」）的题面本身就很短，
            # 只要**答案完整**就不得整题 error——端到端真跑测实锤。
            fatal = (
                (not a_html)
                or (not standard_answer)
                or (not q_html and len(a_html) < 20)
            )
            if fatal:
                raise ValueError("AI 解题结果不完整: " + ", ".join(warnings))
            logger.warning("Non-fatal solution warnings kept for %s", question_id)


ocr_service = OCRService()
