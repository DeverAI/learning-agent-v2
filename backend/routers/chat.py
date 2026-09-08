import os
import json
import shutil
import uuid
from html import escape as html_escape

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import STORAGE_DIR
from logger import get_logger, log_error

logger = get_logger()
router = APIRouter()


class ChatMsg(BaseModel):
    message: str = Field(min_length=1, max_length=50000)
    session_id: str = Field(default="", max_length=64)


class LogPaperError(BaseModel):
    action: str = Field(max_length=64)  # "init" / "load_answer_card" / "render"
    paper_id: str = Field(max_length=64)
    error: str = Field(max_length=2000)
    detail: str = Field(default="", max_length=5000)


@router.post("/api/chat")
async def global_chat(req: ChatMsg):
    from services.ai_service import ai_service
    from services.user_profile import load_profile
    p = load_profile()
    style = p.get("style_notes", "") or p.get("notation_preferences", "")
    # Phase 1: cheap classifier
    # 轻量意图分类走统一入口：优先小米 MiMo V2.5（免费），兜底 DeepSeek flash（关思考）
    classify_prompt = (
        "分析用户意图，返回JSON。\n"
        "type: chat(闲聊)/solve(解题需要AI推理)/search(搜索题库)/"
        "solve_q(从题库找题并解题写回)/"
        "paper(组卷跳转)/auto_paper(直接生成试卷)/"
        "add_question(新增题目到题库)/edit_question(修改题库题目)/"
        "style(改偏好)/profile(存个人信息)/need(要没有的功能，仅当以上类型都不匹配时才用)/"
        "error_info(查看题目错误信息)/"
        "knowledge(知识问答/百科事实检索，需要查资料回答时用)\\n"
        "data: 相关参数。search时data含keyword/subject/grade。"
        f"add_question时data含subject/grade/content(题目内容)/answer(答案)。"
        f"edit_question时data含question_id(唯一题目ID)/keyword(展示用关键词)/field(要改的字段)/value(新值)。"
        f"auto_paper时data含subject/grade/type/topic(专题)。"
        f"error_info时data含question_id(可选,无则列所有错误题目)。"
        f"knowledge时data含query(要检索的知识点)。"
        f"用户消息: {req.message}"
    )
    try:
        raw = await ai_service.light_task_chat(
            [{"role": "user", "content": classify_prompt}], max_tokens=256
        )
        intent = ai_service._extract_json(raw)
    except Exception as e:
        logger.warning("Intent classification failed: %s, falling back to chat", e)
        intent = {"type": "chat"}

    if not isinstance(intent, dict):
        logger.warning("Intent classification returned non-object: %r, falling back to chat", intent)
        intent = {"type": "chat"}
    itype = intent.get("type", "chat")
    if not isinstance(itype, str):
        logger.warning("Intent type is not a string: %r, falling back to chat", itype)
        itype = "chat"
    raw_idata = intent.get("data") if isinstance(intent, dict) else None
    idata = raw_idata if isinstance(raw_idata, dict) else {}

    confirmation_phrases = {
        "style": "确认修改偏好", "profile": "确认保存资料",
        "auto_paper": "确认自动组卷", "solve_q": "确认写入解答",
    }
    required_phrase = confirmation_phrases.get(itype)
    if required_phrase and required_phrase not in req.message:
        return {
            "reply": f"AI 已整理该操作。为避免意图误判直接修改数据或消耗组卷资源，请发送“{required_phrase}”并附上完整要求。",
            "action": {"type": "pending_confirmation", "operation": itype, "data": idata},
        }

    # ====== 组卷 ======
    if itype == "paper":
        from services.config_service import config_service
        from models.database import async_session
        from models.models import Question
        from sqlalchemy import select
        cid = ""
        if idata:
            try:
                cid = await config_service.save_paper_config(idata)
            except Exception as e:
                logger.warning("chat save_paper_config failed: %s", e)
        async with async_session() as db:
            r = await db.execute(select(Question.id).where(Question.status == "done").limit(5))
            qids = [row[0] for row in r.fetchall()]
        hint = f"（题库中已有{len(qids)}道可用题目）" if qids else ""
        jump = f"/papers/generate?saved_config={cid}" if cid else "/papers/generate"
        return {"reply": f"组卷参数已就绪{hint}，已保存（#{cid[:6] if cid else '...'}）。点击下方卡片跳转组卷。",
                "action": {"type": "jump_paper", "data": idata, "saved_config": cid}}

    # ====== 排版偏好 ======
    if itype == "style":
        new_style = idata.get("style_notes", req.message)
        from services.user_profile import save_profile
        p["style_notes"] = new_style
        save_profile(p)
        return {"reply": f"排版偏好已更新为：{new_style[:200]}",
                "action": {"type": "set_style", "data": {"style_notes": new_style}}}

    # ====== 用户画像 ======
    if itype == "profile":
        from services.user_profile import save_profile
        updated = []
        for k, v in idata.items():
            if k in p and v:
                p[k] = v
                updated.append(k)
        if updated:
            save_profile(p)
            return {"reply": f"已记录：{', '.join(updated)}。后续对话将基于这些信息为你服务。",
                    "action": {"type": "set_style", "data": {"updated": updated}}}
        return {"reply": "收到！但请告诉我更具体的信息，比如学校、年级、排名等。"}

    # ====== 需求记录 ======
    if itype == "need":
        from services.diagram_service import diagram_service
        need_desc = idata.get("need", req.message[:60])
        diagram_service._note_agent_need(f"用户需求: {need_desc}")
        return {"reply": f"需求「{need_desc}」已记录到需求池。",
                "action": {"type": "tool_need", "data": {"need": need_desc}}}

    # ====== 查看题目错误信息 ======
    if itype == "error_info":
        from models.database import async_session as _db_s
        from models.models import Question as _Q
        from sqlalchemy import select as _sel
        qid = idata.get("question_id", "")
        async with _db_s() as _db:
            if qid:
                q = await _db.get(_Q, qid)
                if not q:
                    return {"reply": f"未找到题目 #{qid[:8]}"}
                flags = q.audit_flags if hasattr(q, 'audit_flags') and q.audit_flags else []
                err_msg = q.error_message or "无错误记录"
                flag_str = ""
                if isinstance(flags, list) and flags:
                    flag_items = [f"{f.get('type','')}: {f.get('reason','')}" for f in flags if isinstance(f, dict)]
                    flag_str = "\n审计标记: " + "; ".join(flag_items)
                prev = (q.ocr_text or q.question_html or "")[:80]
                return {"reply": f"题目 #{qid[:8]} [{q.subject}][{q.grade}]\n{prev}...\n状态: {q.status}\n错误: {err_msg}{flag_str}"}
            else:
                r = await _db.execute(_sel(_Q).where(_Q.status == "error").order_by(_Q.created_at.desc()).limit(10))
                errors = r.scalars().all()
                if not errors:
                    return {"reply": "当前没有错误题目。"}
                lines = [f"共 {len(errors)} 道错误题目："]
                for i, eq in enumerate(errors, 1):
                    prev = (eq.ocr_text or eq.question_html or "")[:50].replace("\n", " ")
                    lines.append(f"{i}. #{eq.id[:8]} [{eq.subject}][{eq.grade}] {prev}... - {eq.error_message or '未知错误'}")
                return {"reply": "\n".join(lines)}

    # ====== 知识问答/联网检索 ======
    if itype == "knowledge":
        from services.knowledge import search_local, search_and_enrich
        query = str(idata.get("query", "") or req.message).strip()[:2000]
        if not query:
            return {"reply": "请告诉我你想检索的知识内容。"}
        local = search_local(query)
        if local.get("found") and local.get("content"):
            return {"reply": local["content"],
                    "action": {"type": "knowledge_result",
                               "data": {"query": query[:200], "source": local.get("source", "")}}}
        from config import load_settings
        _ks = load_settings()
        if not _ks.get("zhipuai_api_key"):
            return {"reply": "本地知识库没有相关内容；联网检索需要先在设置页配置 GLM API Key。"}
        try:
            content = await search_and_enrich(query)
        except Exception as exc:
            log_error("chat.knowledge", f"knowledge search failed for {query[:60]}: {exc}")
            raise HTTPException(502, detail="知识检索失败，请稍后重试")
        if not content:
            return {"reply": f"没有检索到与「{query[:60]}」相关的内容，请换个说法试试。"}
        return {"reply": content,
                "action": {"type": "knowledge_result", "data": {"query": query[:200]}}}

    # ====== 新增题目到题库 ======
    if itype == "add_question":
        if "确认新增题目" not in req.message:
            return {
                "reply": "已整理新增题目请求。为避免意图误判直接污染题库，请核对内容后发送“确认新增题目”并附上题目内容。",
                "action": {"type": "pending_confirmation", "operation": "add_question", "data": idata},
            }
        from models.database import async_session as _db_s
        from models.models import Question as _Q
        content = idata.get("content", req.message)
        answer = idata.get("answer", "")
        subject = idata.get("subject", "")
        grade = idata.get("grade", "")
        # Use AI to generate formatted question HTML
        gen_prompt = (
            f"根据描述生成一道题目。\n"
            f"学科: {subject or '通用'}\n年级: {grade or '通用'}\n"
            f"题目内容: {content}\n答案: {answer or '请推导'}\n"
            "返回JSON: {{\"question_html\":\"题目HTML(含数学公式用$...$)\","
            "\"answer_html\":\"答案HTML\"}}"
        )
        try:
            raw = await ai_service.deepseek_chat(
                [{"role": "user", "content": gen_prompt}], temperature=0.7,
                max_tokens=32768, scope="solve"
            )
            gen = ai_service._extract_json(raw)
            q_html = gen.get("question_html", content)
            a_html = gen.get("answer_html", answer)
        except Exception as exc:
            logger.warning("Confirmed add_question generation failed: %s", exc)
            raise HTTPException(502, detail="题目生成未完整成功，未写入题库")
        if not q_html or not a_html or not answer:
            raise HTTPException(502, detail="题面、标准答案或解析不完整，未写入题库")
        from models.models import gen_id as _gen_qid
        new_id = _gen_qid()
        folder = os.path.join(STORAGE_DIR, "questions", new_id)
        # ID 碰撞（极小概率）时重试，避免 FileExistsError 直接 500
        for _attempt in range(3):
            try:
                os.makedirs(folder, exist_ok=False)
                break
            except FileExistsError:
                new_id = _gen_qid()
                folder = os.path.join(STORAGE_DIR, "questions", new_id)
        else:
            raise HTTPException(500, detail="题目 ID 生成冲突，请重试")
        try:
            async with _db_s() as _db:
                q = _Q(id=new_id, folder_path=folder, subject=subject, grade=grade,
                       ocr_text=content, question_html=q_html, answer_html=a_html,
                       standard_answer=answer, status="done", source_type="ai_generated")
                _db.add(q)
                await _db.commit()
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        tags_info = f"（{subject} {grade}）" if subject or grade else ""
        return {"reply": f"题目已保存到题库{tags_info}，共1题。可在题库页查看。",
                "action": {"type": "jump_question", "data": {"id": new_id, "subject": subject, "grade": grade}}}

    # ====== 修改题库题目 ======
    if itype == "edit_question":
        from models.database import async_session as _db_s
        from models.models import Question as _Q
        from sqlalchemy import select as _sel
        from datetime import datetime as _dt, timezone
        keyword = idata.get("keyword", "")
        question_id = str(idata.get("question_id", "") or "").strip()
        field = idata.get("field", "")  # answer / tags / subject / grade / content
        value = str(idata.get("value", "") or "").strip()
        if "确认修改题目" not in req.message or not question_id:
            return {
                "reply": "修改正式题目需要精确题目 ID 和二次确认。请在题目详情核对后发送“确认修改题目”，并注明完整题目 ID、字段和新值。",
                "action": {"type": "pending_confirmation", "operation": "edit_question", "data": idata},
            }
        async with _db_s() as _db:
            q = await _db.get(_Q, question_id)
            if not q:
                return {"reply": f"未找到题目 #{question_id[:12]}。"}
            if q.is_resolved:
                return {"reply": "该题已锁定，请先在题目详情取消锁定。"}
            if field == "answer" or field == "答案":
                q.standard_answer = value
                q.answer_html = f"<p>{html_escape(value)}</p>"
            elif field == "tags" or field == "标签":
                q.knowledge_tags = [t.strip() for t in value.split(",") if t.strip()][:50]
            elif field == "subject" or field == "学科":
                q.subject = value[:64]
            elif field == "grade" or field == "年级":
                q.grade = value[:64]
            elif field == "content" or field == "内容":
                q.ocr_text = value
                q.question_html = f"<p>{html_escape(value)}</p>"
            else:
                return {"reply": f"不支持的修改字段: {field}。支持的字段: answer/tags/subject/grade/content"}
            q.updated_at = _dt.now(timezone.utc).replace(tzinfo=None)
            await _db.commit()
            prev = q.ocr_text[:30] if q.ocr_text else ""
        return {"reply": f"题目「{prev}...」的 **{field}** 已更新。",
                "action": {"type": "set_style", "data": {"updated": [field]}}}

    # ====== 直接生成试卷 ======
    if itype == "auto_paper":
        subject = idata.get("subject", "")
        grade = idata.get("grade", "")
        ptype = idata.get("type", "custom")
        topic = idata.get("topic", "")
        # Save config first
        from services.config_service import config_service
        from models.database import async_session as _db_s
        from models.models import Question as _Q
        from sqlalchemy import select as _sel
        cfg = {"subject": subject, "grade": grade, "paper_type": ptype, "question_count": 5}
        if topic:
            cfg["topic"] = topic
            cfg["knowledge_tags"] = [topic]
        cid = ""
        try:
            cid = await config_service.save_paper_config(cfg)
        except Exception as e:
            logger.warning("auto_paper save config failed: %s", e)
        # Try to generate immediately
        async with _db_s() as _db:
            r = await _db.execute(_sel(_Q.id).where(_Q.status == "done").limit(1))
            has_q = len(r.fetchall()) > 0
        if has_q:
            from services.paper_service import paper_service as _ps
            try:
                paper = await _ps.generate_paper(cfg)
                pid = paper.id
                return {"reply": f"试卷已经生成：**{subject} {grade} {ptype}**。",
                        "action": {"type": "jump_paper", "data": cfg, "saved_config": cid, "paper_id": pid}}
            except Exception as e:
                logger.warning("auto_paper generation failed: %s", e)
                hint = f"参数已就绪（{subject} {grade} {ptype}）。"
                return {"reply": f"自动生成暂未成功，请手动跳转组卷页。{hint}",
                        "action": {"type": "jump_paper", "data": cfg, "saved_config": cid}}
        else:
            return {"reply": f"题库暂无可用的题目。请先用OCR导入题目或让我出新题，再生成试卷。",
                    "action": {"type": "jump_paper", "data": cfg, "saved_config": cid}}

    # ====== 题库搜索 ======
    if itype == "search":
        from models.database import async_session as _db_session
        from models.models import Question as _Q
        from sqlalchemy import select as _select, or_, Text
        keyword = idata.get("keyword", "")
        subject = idata.get("subject", "")
        grade = idata.get("grade", "")
        async with _db_session() as _db:
            q = _select(_Q).where(_Q.status == "done")
            if keyword:
                q = q.where(or_(
                    _Q.ocr_text.contains(str(keyword), autoescape=True),
                    _Q.question_html.contains(str(keyword), autoescape=True),
                    _Q.knowledge_tags.cast(Text).contains(str(keyword), autoescape=True),
                ))
            if subject:
                q = q.where(_Q.subject == subject)
            if grade:
                q = q.where(_Q.grade == grade)
            q = q.order_by(_Q.created_at.desc()).limit(5)
            r = await _db.execute(q)
            found = r.scalars().all()
        if not found:
            return {"reply": f"题库中未找到相关题目{'（' + subject + ' ' + grade + '）' if subject or grade else ''}。试试换关键词？"}
        lines = [f"找到 {len(found)} 道题："]
        for i, fq in enumerate(found, 1):
            tags = ", ".join(fq.knowledge_tags or [])
            prev = (fq.ocr_text or fq.question_html or "")[:60]
            lines.append(f"{i}. [{fq.subject}][{fq.grade}] {prev}...（标签: {tags}）")
        return {"reply": "\n".join(lines)}

    # ====== 从题库解题并写回（含自检）======
    if itype == "solve_q":
        from models.database import async_session as _db_session
        from models.models import Question as _Q
        from sqlalchemy import select as _select, or_, Text
        keyword = idata.get("keyword", "") or idata.get("subject", "")
        subject = idata.get("subject", "")
        grade = idata.get("grade", "")
        async with _db_session() as _db:
            q = _select(_Q).where(
                _Q.status == "done", _Q.is_resolved.is_(False),
                or_(_Q.source_type.is_(None), ~_Q.source_type.in_(["search_query", "correction_query"])),
                or_(_Q.audit_flags.is_(None),
                    ~_Q.audit_flags.cast(Text).contains("question_challenge_high")),
            )
            if keyword:
                q = q.where(or_(
                    _Q.ocr_text.contains(str(keyword), autoescape=True),
                    _Q.question_html.contains(str(keyword), autoescape=True),
                    _Q.knowledge_tags.cast(Text).contains(str(keyword), autoescape=True),
                ))
            if subject:
                q = q.where(_Q.subject == subject)
            if grade:
                q = q.where(_Q.grade == grade)
            q = q.order_by(_Q.created_at.desc()).limit(3)
            r = await _db.execute(q)
            found = r.scalars().all()
        if not found:
            return {"reply": "题库中未找到匹配的题目，请用 search 先搜索或提供更具体的学科/年级。"}
        from services.ai_service import ai_service as _ai
        from services.diagram_service import diagram_service as _ds
        replies = []
        for fq in found:
            info = json.dumps({"id": fq.id, "subject": fq.subject, "grade": fq.grade,
                               "ocr_text": fq.ocr_text, "question_html": fq.question_html,
                               "knowledge_tags": fq.knowledge_tags}, ensure_ascii=False)
            # solve_q: solve mode
            try:
                ans = await _ai.deepseek_solve(
                    subject=fq.subject or "", grade=fq.grade or "", ocr_text=fq.ocr_text or "",
                    knowledge_tags=fq.knowledge_tags or [], style_notes=style
                )
            except Exception as exc:
                log_error("chat.solve_q", f"AI solve call failed for {fq.id}: {exc}")
                _msg = str(exc).lower()
                if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
                    raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
                raise HTTPException(status_code=502, detail="AI 解题失败，请稍后重试")
            answer_html = ans.get("answer_html", "")
            # 自检
            try:
                review = await _ai.deepseek_self_review(
                    ans.get("question_html", ""), answer_html,
                    ans.get("standard_answer", ""), fq.subject or "", fq.grade or ""
                )
                answer_html = review.get("answer_html", answer_html)
                q_html = review.get("question_html", ans.get("question_html", ""))
            except Exception as exc:
                logger.warning("Global agent self-review failed for %s: %s", fq.id, exc)
                review = {}
                q_html = ans.get("question_html", "")
            from services.question_challenge import combine_question_challenges
            from services.audit_service import set_question_challenge
            challenge = combine_question_challenges(
                {**(ans.get("question_challenge") or {}), "source": "global_agent_solver"},
                {**(review.get("question_challenge") or {}), "source": "global_agent_review"},
            )
            await set_question_challenge(fq.id, challenge, auto=True)
            if challenge.get("level") == "high":
                replies.append(
                    f"**{fq.subject}{fq.grade} - {fq.id[:8]}** 解题时发现高概率题目疑点，已标记复核，未覆盖原解答。"
                )
                continue
            if not q_html.strip() or not answer_html.strip() or not str(ans.get("standard_answer", "")).strip():
                replies.append(f"**{fq.subject}{fq.grade} - {fq.id[:8]}** 返回内容不完整，未覆盖原解答。")
                continue
            # 处理示意图
            diagram_prompts = ans.get("diagram_prompts", [])
            new_diagrams = list(fq.diagrams or [])
            if diagram_prompts:
                places = ans.get("diagram_places") or (["question"] + ["answer"] * max(0, len(diagram_prompts) - 1))
                for i, dp in enumerate(diagram_prompts):
                    try:
                        path = await _ds.generate_diagram(fq.id, dp, len(new_diagrams))
                        if path:
                            place = places[i] if i < len(places) else "question"
                            new_diagrams.append({"path": path, "place": place})
                            svg_tag = f'<div class="diagram"><img src="{path}" style="max-width:80%;height:auto"></div>'
                            answer_html = answer_html.replace(f"[[DIAGRAM:{i}]]", svg_tag, 1)
                            q_html = q_html.replace(f"[[DIAGRAM:{i}]]", svg_tag, 1)
                    except Exception:
                        answer_html = answer_html.replace(f"[[DIAGRAM:{i}]]", "", 1)
                        q_html = q_html.replace(f"[[DIAGRAM:{i}]]", "", 1)
            proposed_score = ""
            if "<!-- SCORE_SPLIT -->" in answer_html:
                parts = answer_html.split("<!-- SCORE_SPLIT -->", 1)
                proposed_score = parts[0].strip()
                proposed_answer = parts[1].strip()
            else:
                proposed_answer = answer_html
            if not proposed_answer.strip():
                replies.append(f"**{fq.subject}{fq.grade} - {fq.id[:8]}** 返回内容不完整，未覆盖原解答。")
                continue
            async with _db_session() as _db2:
                current = await _db2.get(_Q, fq.id)
                if not current or current.is_resolved or current.status != "done":
                    replies.append(f"**{fq.subject}{fq.grade} - {fq.id[:8]}** 状态已变化，未覆盖原解答。")
                    continue
                current_diagrams = list(current.diagrams or [])
                known_paths = {
                    item.get("path") for item in current_diagrams if isinstance(item, dict) and item.get("path")
                }
                current.diagrams = current_diagrams + [
                    item for item in new_diagrams
                    if isinstance(item, dict) and item.get("path") not in known_paths
                ]
                current.question_html = q_html
                current.answer_html = proposed_answer
                current.score_points_html = proposed_score
                current.standard_answer = ans.get("standard_answer", current.standard_answer)
                current.question_type = ans.get("question_type", current.question_type)
                current.structure_graph_info = None
                await _db2.commit()
            replies.append(f"**{fq.subject}{fq.grade} - {fq.id[:8]}** 已解答并保存。\n\n" + answer_html)
        return {"reply": "\n\n---\n\n".join(replies)}

    # ====== 解题 ======
    if itype == "solve":
        try:
            reply = await ai_service.deepseek_chat([
                {"role": "system", "content": (
                    "你是学习搭子AI解题助手。能力：1.详细解题 2.出变式题 3.解释概念 4.画图。\n"
                    "遇到几何题、函数图像、物理化学装置等需要图示的内容，请在解答中插入 [[DIAGRAM:详细中文描述图形]] 自动生成示意图。\n"
                    "示意图描述要具体：例如 [[DIAGRAM:直角三角形ABC，∠C=90°，AC=3 BC=4 标注顶点]]。\n"
                    "【输出格式】使用Markdown：## 标题 / **粗体** / 有序列表1. 2. 3. / 数学用$...$包裹。不得使用emoji和彩色文字。\n"
                    f"排版偏好：{style}"
                )},
                {"role": "user", "content": req.message}
            ], max_tokens=32768, scope="solve")
        except Exception as exc:
            log_error("chat.solve", f"AI solve call failed: {exc}")
            _msg = str(exc).lower()
            if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
                raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
            raise HTTPException(status_code=502, detail="AI 解题失败，请稍后重试")
        # 自动生成示意图
        reply = await _render_diagrams_in_reply(reply)
        return {"reply": reply}

    # ====== 默认：智能对话（含多轮能力） ======
    system_prompt = (
        f"你是学习搭子AI助手。你有以下能力：\n"
        f"1. 搜索题库 2. 从题库找题解题并写回 3. 修改/重写题目答案 "
        f"4. 出变式题 5. 解释概念 6. 画几何图（遇到几何/函数/装置题时插入 [[DIAGRAM:详细中文描述图形]] 自动生成示意图。例如 [[DIAGRAM:直角三角形ABC ∠C=90° AC=3 BC=4]]）\n"
        f"7. 评估答案正确性 8. 推荐组卷参数 9. 查看错误信息(error_info)\n"
        f"【输出格式】使用Markdown格式回答：用 ## 表示小标题，用 **粗体** 强调重点，有序列表用 1. 2. 3.，数学公式用 $...$ 包裹。\n"
        f"禁止使用emoji、禁止使用彩色HTML、禁止使用代码块标记（除非展示代码）。\n"
        f"排版偏好：{style}\n"
        f"如果需要多步完成，输出JSON格式: {{\"reply\":\"当前回复\",\"done\":true/false}}"
    )
    try:
        reply = await ai_service.deepseek_chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": req.message}
        ], max_tokens=32768, scope="chat")
    except Exception as exc:
        log_error("chat.default", f"AI chat call failed: {exc}")
        _msg = str(exc).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
        raise HTTPException(status_code=502, detail="AI 对话失败，请稍后重试")
    # 自动生成示意图
    reply = await _render_diagrams_in_reply(reply)
    return {"reply": reply}


@router.post("/api/log-paper-error")
async def log_paper_error(req: LogPaperError):
    """客户端上报试卷展示错误"""
    from logger import log_error
    # 清洗换行与超长字段，防止日志伪造与膨胀
    def _flat(v: str, limit: int) -> str:
        return str(v or "").replace("\r", " ").replace("\n", " ")[:limit]
    if req.detail:
        msg = "paper_id=%s action=%s error=%s detail=%s" % (
            _flat(req.paper_id, 64), _flat(req.action, 64), _flat(req.error, 2000), _flat(req.detail, 300))
    else:
        msg = "paper_id=%s action=%s error=%s" % (
            _flat(req.paper_id, 64), _flat(req.action, 64), _flat(req.error, 2000))
    log_error("paper_frontend", msg)
    return {"message": "logged"}


class LogFrontendError(BaseModel):
    error: str = Field(max_length=5000)
    stack: str = Field(default="", max_length=10000)
    kind: str = Field(default="error", max_length=64)
    page: str = Field(default="", max_length=256)


@router.post("/api/log-frontend-error")
async def log_frontend_error(req: LogFrontendError):
    """全局前端JS错误自动上报 —— 类似自动记录需求"""
    def _flat(v: str, limit: int) -> str:
        return str(v or "").replace("\r", " ").replace("\n", " ")[:limit]
    msg = "[%s][%s] %s" % (_flat(req.kind, 64), _flat(req.page, 256), _flat(req.error, 200))
    if req.stack:
        msg += " | stack=%s" % _flat(req.stack, 300)
    log_error("frontend_js", msg)
    return {"message": "logged"}


async def _render_diagrams_in_reply(reply: str) -> str:
    """替换回复中的 [[DIAGRAM:...]] 标记为实际SVG，含显式宽度防组卷压缩"""
    if '[[DIAGRAM:' not in reply:
        return reply
    from services.diagram_service import diagram_service as _ds
    import re as _re
    diag_idx = 0
    base_name = f"chat_{uuid.uuid4().hex[:20]}"
    for m in _re.finditer(r'\[\[DIAGRAM:([^\]]+)\]\]', reply):
        desc = m.group(1)
        try:
            path = await _ds.generate_diagram(base_name, desc, diag_idx)
            diag_idx += 1
            if path:
                disk_path = os.path.join(STORAGE_DIR, path.lstrip("/"))
                w, h = _ds._get_svg_size(disk_path)
                size_attr = f' width="{w}"' if w else ""
                svg_tag = f'<div class="diagram"><img src="{path}" style="max-width:80%;height:auto"{size_attr} alt="示意图" onerror="this.style.display=\'none\'"></div>'
                reply = reply.replace(m.group(0), svg_tag, 1)
            else:
                log_error("diagram_gen", f"empty_path base={base_name} idx={diag_idx-1} desc={desc[:60]}")
                reply = reply.replace(m.group(0), '', 1)
        except Exception as e:
            log_error("diagram_gen", f"base={base_name} idx={diag_idx} desc={desc[:60]} error={e}")
            reply = reply.replace(m.group(0), '', 1)
    return reply
