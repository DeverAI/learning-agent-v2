"""Agent 工具处理器 + 工具表定义。

从 `routers/sessions.py::_session_chat_impl`（原先 839 行单函数、20 个 `if itype ==`
硬分支）搬出来，每个分支变成一个具名 async 处理器，并在这里登记进
`agent_core.REGISTRY`。

搬运原则：**行为等价**。除以下三类机械改动外，处理体逐行保持原样：
1. `sid` / `s` / `messages` / `steps` / `idata` / `req` / `style` 改走 `c.*`；
2. `s["messages"] = messages; s["messages"].append(...); _save(sid, s)`
   三连改成一个 `c.commit(reply)`；
3. `_final_steps(sid, steps)` 改 `c.finish(...)`。

## 本次搬运同时修掉的两个真实缺陷

- `edit_paper` / `delete_paper`：实现一直在，但**不在分类器 prompt 里**，
  分类器永远吐不出这两个 type，所以这两个分支**从来不可达**（死分支）。
  现已登记进表，prompt 由表生成，因此可达。
- `edit_paper` 的注释写着「沿用确认闸」，**但闸门并不存在** ——
  一旦它变可达，就等于开放了一条「分类器误判即可直接改试卷字段」的写路径。
  已按注释声明的意图补上确认闸。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid

from fastapi import HTTPException

from config import STORAGE_DIR
from logger import get_logger, log_error
from services.agent_core import Ctx, Tool, register
from services.ai_service import agent_tool_calls_add, agent_tool_calls_get, ai_service
from services.user_profile import load_profile, save_profile

logger = get_logger()


# ==========================================================================
# 对话兜底：chat / solve
# ==========================================================================

_DIALOGUE_SYSTEM = (
    "你是学习搭子AI助手。你有多轮对话上下文，能记住之前说过的内容。\n"
    "你的能力："
    "1. 搜索题库(search) 2. 从题库找题解题并写回(solve_q) "
    "3. 修改/重写题目答案 4. 出变式题 5. 解释概念 "
    "6. 画几何图（遇到几何/函数/装置题时插入 [[DIAGRAM:详细中文描述图形]] 自动生成示意图，"
    "描述要具体：例如 [[DIAGRAM:直角三角形ABC ∠C=90° AC=3 BC=4 标注顶点]]）"
    "7. 评估答案正确性 8. 推荐组卷参数"
    "9. 查看/搜索/创建/修改笔记（list_notes/get_note/create_note/modify_note）"
    "10. 查看题目错误和审计标记（error_info）"
    "11. 找相关内容（related_content，跨题库/笔记/知识点树/历史记忆聚合）"
    "12. 查知识点关系（knowledge_lookup，前置/后继/关联）"
    "13. 回忆你的学习历史与薄弱点（recall_memory）"
    "14. 联网查资料（web_search）"
    "15. 备课、生成可编辑课稿（prepare_lesson，后台生成、立刻返回、可取消）"
    "16. 查看已备课稿（list_lessons）17. 读课稿、可切片（read_lesson）"
    "18. 改课稿的某一片（edit_lesson）"
    "19. 今日补漏清单（review_plan，学生问「该学什么」时用它）"
    "20. 智能上传后整理并备课（import_and_prep）\n"
    "【输出格式】使用Markdown：## 小标题 / **粗体** / 1.2.3.有序列表 / $...$数学公式。"
    "禁止emoji、禁止彩色文字。"
)


async def h_dialogue(c: Ctx) -> dict:
    """chat / solve：走上下文完整的强模型。"""
    system = _DIALOGUE_SYSTEM
    if c.style:
        system += f"\n排版偏好：{c.style}"
    if c.itype == "solve":
        system += "\n这是解题任务，请详细推理，用LaTeX写公式。"

    full_messages = [{"role": "system", "content": system}] + c.messages[-20:]

    # 闲聊与求解统一走配置的 deepseek_model（deepseek_chat 内部读取设置）；
    # 旧 deepseek-chat / deepseek-v4-pro 分流已失效，deepseek-chat 已被官方下线
    step_parent = "解题Agent" if c.itype == "solve" else "对话Agent"
    if c.step_callback:
        c.step_callback(step_parent, "生成回复", "running")
    reply = await ai_service.deepseek_chat(
        full_messages, scope="chat", step_callback=c.step_callback
    )
    if c.step_callback:
        c.step_callback(step_parent, "生成回复", "done")

    # Generate diagrams for [[DIAGRAM:...]] markers in reply
    if "[[DIAGRAM:" in reply:
        from services.diagram_service import diagram_service as _ds
        import re as _re
        diag_idx = 0
        base_name = f"chat_{c.sid}_{int(time.time())}"
        for m in _re.finditer(r"\[\[DIAGRAM:([^\]]+)\]\]", reply):
            desc = m.group(1)
            try:
                path = await _ds.generate_diagram(base_name, desc, diag_idx)
                if path:
                    disk_p = os.path.join(STORAGE_DIR, path.lstrip("/"))
                    w, _ = _ds._get_svg_size(disk_p)
                    sz = f' width="{w}"' if w else ""
                    svg_tag = (f'<div class="diagram"><img src="{path}" '
                               f'style="max-width:80%;height:auto"{sz} '
                               f"onerror=\"this.style.display='none'\"></div>")
                else:
                    svg_tag = ""
                reply = reply.replace(m.group(0), svg_tag, 1)
                diag_idx += 1
            except Exception as e:
                log_error("diagram_gen", f"session={c.sid} desc={desc[:60]} error={e}")
                reply = reply.replace(m.group(0), "", 1)

    c.set_reply(reply)

    # auto-title on first exchange
    if len(c.session["messages"]) == 2:
        try:
            title_raw = await ai_service.light_task_chat(
                [{"role": "user", "content": f"给这段对话起个6字内标题，直接回复标题本身，不要引号不要额外文字：{c.message}"}],
                max_tokens=32,
            )
            title = title_raw.strip().strip('"\'').strip("\u201c\u201d\u2018\u2019").strip()[:20]
            if title:
                c.session["title"] = title
        except Exception as e:
            logger.debug("Auto-title failed for session %s: %s", c.sid, e)

    c.save()
    return {
        "reply": reply,
        "steps": _final(c),
        "session": c.session,
        "tool_calls": agent_tool_calls_get(c.sid),
    }


def _final(c: Ctx) -> list:
    from services.agent_core import final_steps
    return final_steps(c.sid, c.steps)


# ==========================================================================
# 偏好与资料
# ==========================================================================

async def h_style(c: Ctx) -> dict:
    new_style = c.data.get("style_notes", c.message)
    profile = load_profile()
    profile["style_notes"] = new_style
    save_profile(profile)
    reply = f"收到！排版偏好已更新：{new_style[:100]}。之后的题目都会按这个风格展现。"
    c.commit(reply)
    return c.finish(reply, {"type": "set_style", "data": {"style_notes": new_style}})


async def h_profile(c: Ctx) -> dict:
    profile = load_profile()
    updated = []
    for k, v in c.data.items():
        if k in profile and v:
            profile[k] = v
            updated.append(k)
    if updated:
        save_profile(profile)
        reply = f"记住了！你的{', '.join(updated)}等信息已保存，后续对话会基于这些信息为你定制。"
    else:
        reply = "收到你的信息！请告诉我更多细节，比如学校、年级、学习目标等。"
    c.commit(reply)
    return c.finish(reply, {"type": "set_style", "data": {"updated": updated}})


# ==========================================================================
# 题库：新增 / 修改
# ==========================================================================

async def h_add_question(c: Ctx) -> dict:
    from models.database import async_session as _db_s
    from models.models import Question as _Q

    content = c.data.get("content", c.message)
    answer = c.data.get("answer", "")
    subject = c.data.get("subject", "")
    grade = c.data.get("grade", "")
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
        raise HTTPException(502, detail="题目生成未完整成功，未写入题库") from exc
    if not q_html or not a_html or not answer:
        raise HTTPException(502, detail="题面、标准答案或解析不完整，未写入题库")
    new_id = uuid.uuid4().hex[:12]
    folder = os.path.join(STORAGE_DIR, "questions", new_id)
    try:
        # 先建目录再写库：makedirs 失败（磁盘满/权限）时不能留下无目录的幽灵题目
        os.makedirs(folder, exist_ok=False)
    except OSError as exc:
        log_error("sessions.add_question", f"create question folder failed for {new_id}: {exc}")
        raise HTTPException(500, detail="题目目录创建失败，未写入题库") from exc
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
    reply = f"题目已保存到题库{tags_info}。可在题库页查看。"
    agent_tool_calls_add(c.sid, "新增题目",
                         {"subject": subject, "grade": grade, "content": content[:100]},
                         f"题目已保存 #{new_id[:8]}", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "jump_question", "data": {"id": new_id}}, tool_calls=True)


async def h_edit_question(c: Ctx) -> dict:
    from models.database import async_session as _db_s
    from models.models import Question as _Q
    from datetime import datetime as _dt, timezone as _tz
    from html import escape as _html_escape

    question_id = str(c.data.get("question_id", "") or "").strip()
    field = c.data.get("field", "")
    value = str(c.data.get("value", "") or "").strip()
    if not question_id:
        reply = "确认修改仍缺少完整题目 ID，未执行任何写入。"
        c.commit(reply)
        return c.finish(reply)
    async with _db_s() as _db:
        q = await _db.get(_Q, question_id)
        if not q:
            reply = f"未找到题目 #{question_id[:12]}。"
        elif q.is_resolved:
            reply = "该题已锁定，请先在题目详情取消锁定。"
        else:
            if field in ("answer", "答案"):
                q.standard_answer = value
                q.answer_html = f"<p>{_html_escape(value)}</p>"
            elif field in ("tags", "标签"):
                q.knowledge_tags = [t.strip() for t in value.split(",") if t.strip()][:50]
            elif field in ("subject", "学科"):
                q.subject = value[:64]
            elif field in ("grade", "年级"):
                q.grade = value[:64]
            elif field in ("content", "内容"):
                q.ocr_text = value
                q.question_html = f"<p>{_html_escape(value)}</p>"
            else:
                reply = f"不支持的修改字段: {field}。支持的字段: answer/tags/subject/grade/content"
                c.commit(reply)
                return c.finish(reply)
            q.updated_at = _dt.now(_tz.utc).replace(tzinfo=None)
            await _db.commit()
            prev = q.ocr_text[:30] if q.ocr_text else ""
            reply = f"题目「{prev}...」的 **{field}** 已更新。"
            agent_tool_calls_add(c.sid, "修改题目",
                                 {"question_id": question_id, "field": field, "value": value[:100]},
                                 f"已更新 {field}", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "set_style", "data": {"updated": [field]}}, tool_calls=True)


# ==========================================================================
# 试卷：组卷 / 生成 / 修改 / 删除
# ==========================================================================

async def h_paper(c: Ctx) -> dict:
    from services.config_service import config_service
    cid = await config_service.save_paper_config(c.data) if c.data else ""
    subj = c.data.get("subject", "")
    grade = c.data.get("grade", "")
    reply = f"好的！组卷参数已保存（#{cid[:6]}）。" + (f"{subj} {grade}。" if subj else "") + "点击下方卡片跳转组卷页面。"
    c.commit(reply)
    # 注：原实现里算了个 `jump` 变量但从未使用（死变量），搬运时删除；
    # action 的字段与原实现**逐字一致**，不新增键，避免前端契约变化。
    return c.finish(reply, {"type": "jump_paper", "data": c.data, "saved_config": cid})


async def h_auto_paper(c: Ctx) -> dict:
    from services.config_service import config_service
    from models.database import async_session as _db_s
    from models.models import Question as _Q
    from sqlalchemy import select as _sel

    subject = c.data.get("subject", "")
    grade = c.data.get("grade", "")
    ptype = c.data.get("type", "custom")
    topic = c.data.get("topic", "")
    cfg = {"subject": subject, "grade": grade, "paper_type": ptype, "question_count": 5}
    if topic:
        cfg["topic"] = topic
        cfg["knowledge_tags"] = [topic]
    cid = await config_service.save_paper_config(cfg)
    async with _db_s() as _db:
        r = await _db.execute(_sel(_Q.id).where(_Q.status == "done").limit(1))
        has_q = len(r.fetchall()) > 0
    if has_q:
        from services.paper_service import paper_service as _ps
        try:
            paper = await _ps.generate_paper(cfg)
            pid = paper.id
            reply = f"试卷已经生成：**{subject} {grade} {ptype}**。"
            action = {"type": "jump_paper", "data": cfg, "saved_config": cid, "paper_id": pid}
        except Exception as e:
            logger.warning("auto_paper session generation failed: %s", e)
            reply = f"参数已就绪（{subject} {grade} {ptype}）。请手动跳转组卷页。"
            action = {"type": "jump_paper", "data": cfg, "saved_config": cid}
    else:
        reply = "题库暂无可用的题目。请先用OCR导入题目或让我出新题，再生成试卷。"
        action = {"type": "jump_paper", "data": cfg, "saved_config": cid}
    c.commit(reply)
    return c.finish(reply, action)


async def h_edit_paper(c: Ctx) -> dict:
    from models.database import async_session as _db_s
    from models.models import Paper as _P

    paper_id = str(c.data.get("paper_id", "")).strip()
    field = str(c.data.get("field", "")).strip().lower()
    value = str(c.data.get("value", "")).strip()
    allowed = {"title": "标题", "subject": "学科", "grade": "年级"}
    if field not in allowed:
        reply = f"不支持的试卷修改字段: {field or '(空)'}。支持的字段: title/subject/grade"
        c.commit(reply)
        return c.finish(reply)
    async with _db_s() as _db:
        p = await _db.get(_P, paper_id)
        if not p:
            reply = f"试卷不存在: {paper_id}"
        else:
            if field == "title":
                p.title = value[:200]
            elif field == "subject":
                p.subject = value[:64]
            elif field == "grade":
                p.grade = value[:64]
            await _db.commit()
            reply = f"试卷「{p.title}」的 **{allowed[field]}** 已更新为 {value[:60]}。"
            agent_tool_calls_add(c.sid, "修改试卷",
                                 {"paper_id": paper_id, "field": field, "value": value[:100]},
                                 "已更新", "done")
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


async def h_delete_paper(c: Ctx) -> dict:
    """删除试卷。

    **确认闸改动（R11）**：原先这里读 `c.data["confirm"]` 判断是否确认。
    但 `data` 是**分类器（LLM）产出的**，实测分类器自己就会塞
    `{"paper_id": "abc123", "confirm": True}` —— 闸门由被闸的人自己开，
    等于没有闸。现改为与其它写操作同一套机制：确认语由
    `Tool.requires_confirm` 声明，分发器检查**用户原话**里有没有这句话。
    `data` 只用来传参，不再参与授权判断。
    """
    from models.database import async_session as _db_s
    from models.models import Paper as _P

    paper_id = str(c.data.get("paper_id", "")).strip()
    async with _db_s() as _db:
        p = await _db.get(_P, paper_id)
        if not p:
            reply = f"试卷不存在: {paper_id}"
        else:
            title = p.title
            # 题目不删：只解绑试卷引用（与 DELETE /api/papers/{id} 行为一致）
            from sqlalchemy import update as _upd
            from models.models import Question as _Q2
            qids = list(p.question_ids or [])
            if qids:
                await _db.execute(_upd(_Q2).where(_Q2.id.in_(qids)).values(paper_id=None))
            await _db.delete(p)
            await _db.commit()
            reply = f"试卷「{title}」已删除，{len(qids)} 道题保留在题库。"
            agent_tool_calls_add(c.sid, "删除试卷",
                                 {"paper_id": paper_id, "title": title[:60]}, "已删除", "done")
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


# ==========================================================================
# 需求记录（兜底）
# ==========================================================================

async def h_need(c: Ctx) -> dict:
    from services.diagram_service import diagram_service
    need_desc = c.data.get("need", c.message[:60])
    try:
        summary_raw = await ai_service.light_task_chat(
            [{"role": "user", "content": f"用户在对话中说：{c.message}\n请用20字以内抽象总结用户真正需要的功能或能力，只输出总结。"}],
            max_tokens=64,
        )
        need_desc = summary_raw.strip().strip('"').strip("'")[:80]
    except Exception as exc:
        logger.warning("Need summary AI call failed for session %s: %s", c.sid, exc)
    diagram_service._note_agent_need(f"会话{c.sid}: {need_desc}")
    reply = f"对不起，我暂时还没有「{need_desc}」这个功能。不过我已经记下来了，开发组会尽快处理。请先试试其他功能吧。"
    c.commit(reply)
    return c.finish(reply, {"type": "tool_need", "data": {"need": need_desc}})


# ==========================================================================
# 题库：检索 / 解题写回
# ==========================================================================

async def h_search(c: Ctx) -> dict:
    from models.database import async_session as _db_session
    from models.models import Question as _Q
    from sqlalchemy import select as _select

    keyword = c.data.get("keyword", "")
    subject = c.data.get("subject", "")
    grade = c.data.get("grade", "")
    try:
        limit = max(1, min(int(c.data.get("limit", 5)), 20))
    except (TypeError, ValueError):
        limit = 5
    async with _db_session() as _db:
        from sqlalchemy import or_, Text
        q = _select(_Q).where(_Q.status == "done")
        if subject:
            q = q.where(_Q.subject == subject)
        if grade:
            q = q.where(_Q.grade == grade)
        if keyword:
            q = q.where(or_(
                _Q.ocr_text.contains(str(keyword), autoescape=True),
                _Q.question_html.contains(str(keyword), autoescape=True),
                _Q.knowledge_tags.cast(Text).contains(str(keyword), autoescape=True),
            ))
        q = q.order_by(_Q.created_at.desc()).limit(limit)
        r = await _db.execute(q)
        found = r.scalars().all()
    if not found:
        reply = f"题库中未找到相关题目{'（' + subject + ' ' + grade + '）' if subject or grade else ''}。试试换关键词？"
    else:
        lines = [f"找到 {len(found)} 道题："]
        for i, fq in enumerate(found, 1):
            tags = ", ".join(fq.knowledge_tags or [])
            prev = (fq.ocr_text or fq.question_html or "")[:60]
            lines.append(f"{i}. [{fq.subject}][{fq.grade}] {prev}...（标签: {tags}）")
        reply = "\n".join(lines)
        if found:
            # Store last search results in session for potential follow-up
            c.session["_last_search"] = [fq.id for fq in found]
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


async def h_solve_q(c: Ctx) -> dict:
    from models.database import async_session as _db_session
    from models.models import Question as _Q
    from sqlalchemy import select as _select

    keyword = c.data.get("keyword", "") or c.data.get("subject", "")
    subject = c.data.get("subject", "")
    grade = c.data.get("grade", "")
    async with _db_session() as _db:
        from sqlalchemy import Text, or_
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
        reply = "题库中未找到匹配的题目，请用 search 先搜索或提供更具体的信息。"
    else:
        replies = []
        from services.ai_service import ai_service as _ai
        from services.user_profile import load_profile as _lp
        from services.diagram_service import diagram_service as _ds
        _prof = _lp()
        _st = _prof.get("style_notes", "") or _prof.get("notation_preferences", "")
        for fq in found:
            original_diagram_count = len(fq.diagrams or [])
            info = json.dumps({"id": fq.id, "subject": fq.subject, "grade": fq.grade,
                               "ocr_text": fq.ocr_text, "question_html": fq.question_html,
                               "knowledge_tags": fq.knowledge_tags}, ensure_ascii=False)
            if c.step_callback:
                c.step_callback("题库解题Agent", f"解答 {fq.id[:8]}", "running")
            # 逐题容错：单题 AI 失败不拖垮整个会话请求（前面题目已各自提交写库）
            try:
                ans = await _ai.deepseek_chat_question(info, c.message, _st, step_callback=c.step_callback)
            except Exception as solve_exc:
                log_error("sessions.solve_q", f"AI solve failed for {fq.id}: {solve_exc}")
                if c.step_callback:
                    c.step_callback("题库解题Agent", f"解答 {fq.id[:8]}", "error")
                replies.append(f"**题目 {fq.id[:8]}** AI 解题失败，已跳过（其余题目继续）。")
                continue
            if not (ans or "").strip():
                replies.append(f"**题目 {fq.id[:8]}** 解答模型返回空内容，未写入。")
                continue
            if c.step_callback:
                c.step_callback("题库解题Agent", f"解答 {fq.id[:8]}", "done")
            agent_tool_calls_add(c.sid, "解题",
                                 {"question_id": fq.id, "subject": fq.subject},
                                 "已解答并写入", "done")
            # Generate diagrams in answer
            if "[[DIAGRAM:" in ans:
                for m in __import__("re").finditer(r"\[\[DIAGRAM:([^\]]+)\]\]", ans):
                    desc = m.group(1)
                    try:
                        path = await _ds.generate_diagram(fq.id, desc, len(fq.diagrams or []))
                        if path:
                            disk_p = os.path.join(STORAGE_DIR, path.lstrip("/"))
                            w, _ = _ds._get_svg_size(disk_p)
                            sz = f' width="{w}"' if w else ""
                            svg_tag = (f'<div class="diagram"><img src="{path}" '
                                       f'style="max-width:80%;height:auto"{sz}></div>')
                            ans = ans.replace(m.group(0), svg_tag, 1)
                            diags = list(fq.diagrams or [])
                            diags.append({"path": path, "place": "answer"})
                            fq.diagrams = diags
                    except Exception as exc:
                        logger.warning("Session answer diagram failed for %s: %s", fq.id, exc)
                        ans = ans.replace(m.group(0), "", 1)
            # Save the answer back to the question
            async with _db_session() as _db2:
                current = await _db2.get(_Q, fq.id)
                if not current or current.is_resolved or current.status != "done":
                    replies.append(f"**题目 {fq.id[:8]}** 状态已变化，本次解答未写入。")
                    continue
                # 去重：同一会话对同一题只追加一次，整条指令重试不会堆积重复答案
                if f"<!-- AGENT_SESSION_{c.sid} -->" in (current.answer_html or ""):
                    replies.append(f"**题目 {fq.id[:8]}** 本会话已写入过解答，跳过重复写入。")
                    continue
                current.answer_html = (current.answer_html or "") + f"\n<!-- AGENT_SESSION_{c.sid} -->\n" + ans
                additions = list(fq.diagrams or [])[original_diagram_count:]
                if additions:
                    current_diagrams = list(current.diagrams or [])
                    existing_paths = {d.get("path") for d in current_diagrams if isinstance(d, dict)}
                    current_diagrams.extend(
                        d for d in additions
                        if isinstance(d, dict) and d.get("path") not in existing_paths
                    )
                    current.diagrams = current_diagrams
                await _db2.commit()
            replies.append(f"**题目 {fq.id[:8]}** 已解答并保存。\n\n" + ans)
        reply = "\n\n---\n\n".join(replies)
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


# ==========================================================================
# 图片入待处理队列
# ==========================================================================

async def h_save_image(c: Ctx) -> dict:
    raw_b64 = c.data.get("base64_image", "") or ""
    b64_img = raw_b64.split(",")[-1] if "," in raw_b64 else raw_b64
    if not b64_img:
        reply = "请提供图片数据（base64格式）。"
        c.commit(reply)
        return c.finish(reply)

    from config import QUESTIONS_DIR as _QD
    import base64 as _b64_mod, binascii as _binascii, os as _os_mod, shutil as _shutil
    from models.models import gen_id as _gen_id

    new_id = _gen_id()
    folder = _os_mod.path.join(_QD, new_id)
    try:
        img_bytes = _b64_mod.b64decode(b64_img, validate=True)
        if not img_bytes or len(img_bytes) > 10 * 1024 * 1024:
            raise ValueError("图片为空或超过 10MB")
        if img_bytes.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif img_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = ".png"
        elif len(img_bytes) >= 12 and img_bytes[:4] == b"RIFF" and img_bytes[8:12] == b"WEBP":
            ext = ".webp"
        else:
            raise ValueError("图片内容不是有效的 JPG、PNG 或 WEBP")
        _os_mod.makedirs(folder, exist_ok=False)
        img_path = _os_mod.path.join(folder, f"original{ext}")
        with open(img_path, "wb") as _if:
            _if.write(img_bytes)
            _if.flush()
            _os_mod.fsync(_if.fileno())
    except (_binascii.Error, ValueError, OSError) as _ie:
        _shutil.rmtree(folder, ignore_errors=True)
        reply = f"图片保存失败: {str(_ie)[:100]}"
        c.commit(reply)
        return c.finish(reply)

    # Create Question entry
    from models.database import async_session as _db_s3
    from models.models import Question as _Q
    subj = c.data.get("subject", "")
    grd = c.data.get("grade", "")
    from routers.ocr import _run_bg, _save_task_state, _clear_task_state
    desc = {"question_id": new_id, "type": "process_image", "split_mode": "single",
            "user_hint": "", "tags": [], "user_grade": grd, "bank": "default"}
    try:
        async with _db_s3() as _db:
            q = _Q(id=new_id, folder_path=folder, subject=subj, grade=grd,
                   raw_image_path=img_path, status="staged", source_type="agent_image")
            _db.add(q)
            _save_task_state(desc)
            await _db.commit()
    except Exception:
        _clear_task_state(new_id)
        _shutil.rmtree(folder, ignore_errors=True)
        raise
    from services.ocr_service import ocr_service
    _run_bg(ocr_service.process_image(new_id, "", [], grd), desc)
    reply = f"图片已保存到题库（#{new_id[:8]}），已进入OCR处理队列。学科: {subj or '待识别'}"
    c.commit(reply)
    return c.finish(reply)


# ==========================================================================
# 笔记
# ==========================================================================

async def h_list_notes(c: Ctx) -> dict:
    from models.database import async_session as _db_s
    from models.models import Note as _N
    from sqlalchemy import select as _sel

    subject_name = c.data.get("subject", "")
    tag_name = c.data.get("tag", "")
    async with _db_s() as _db:
        from sqlalchemy import Text
        q = _sel(_N)
        if subject_name:
            q = q.where(_N.subject == subject_name)
        if tag_name:
            q = q.where(_N.knowledge_tags.cast(Text).contains(str(tag_name), autoescape=True))
        q = q.order_by(_N.updated_at.desc()).limit(20)
        r = await _db.execute(q)
        notes = r.scalars().all()
    if not notes:
        reply = "笔记列表为空。" + (f"（学科: {subject_name}）" if subject_name else "")
    else:
        lines = [f"找到 {len(notes)} 篇笔记："]
        for i, n in enumerate(notes, 1):
            tags = ", ".join(n.knowledge_tags or [])
            prev = (n.content or "")[:50].replace("\n", " ")
            lines.append(f"{i}. [{n.subject or '未分类'}] **{n.title}** - {prev}...（标签: {tags}）")
        reply = "\n".join(lines)
        # Store last note search results
        c.session["_last_note_search"] = [n.id for n in notes]
    agent_tool_calls_add(c.sid, "搜索笔记",
                         {"subject": subject_name, "tag": tag_name},
                         f"找到 {len(notes)} 篇笔记", "done")
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


async def h_get_note(c: Ctx) -> dict:
    from models.database import async_session as _db_s
    from models.models import Note as _N
    from sqlalchemy import select as _sel

    keyword = c.data.get("keyword", "")
    note_id = c.data.get("note_id", "")
    async with _db_s() as _db:
        n = None
        if note_id:
            n = await _db.get(_N, note_id)
        elif keyword:
            # LIKE 通配符转义（与 modify_note 一致）：% _ 会改变匹配语义
            kw_escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            r = await _db.execute(_sel(_N).where(_N.title.contains(kw_escaped, escape="\\")).limit(1))
            n = r.scalars().first()
        if not n:
            # Try last search results
            last = c.session.get("_last_note_search", [])
            if last and not keyword:
                n = await _db.get(_N, last[0])
        if not n:
            reply = "未找到该笔记。试试搜索笔记列表？"
        else:
            tags = ", ".join(n.knowledge_tags or [])
            reply = (f"**{n.title}** [{n.subject or '未分类'}][{n.grade or ''}]\n"
                     f"标签: {tags}\n\n{n.content or '(无内容)'}")
            agent_tool_calls_add(c.sid, "查看笔记", {"note_id": n.id, "title": n.title},
                                 "已查看笔记", "done")
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


async def h_create_note(c: Ctx) -> dict:
    from models.database import async_session as _db_s
    from models.models import Note as _N

    title = str(c.data.get("title") or "AI创建的笔记")[:200]
    subject = str(c.data.get("subject") or "")[:64]
    raw_content = c.data.get("content", c.message)
    content = raw_content if isinstance(raw_content, str) else (
        c.message if not isinstance(raw_content, (int, float, bool)) else str(raw_content)
    )
    # 长内容不截断：超过模型整理窗口时原样保存，避免笔记后半段丢失。
    if len(content) > 20000:
        structured = content
    else:
        try:
            struct_prompt = (
                f"将以下内容整理为结构化笔记（Markdown格式，用##分层）：\n"
                f"{content}\n\n"
                f"直接输出Markdown，不要额外文字。"
            )
            gen_raw = await ai_service.deepseek_chat(
                [{"role": "user", "content": struct_prompt}], max_tokens=4096,
                scope="notes_classify"
            )
            structured = gen_raw.strip()
        except Exception as exc:
            # AI 整理失败回退原文必须留痕：静默吞错会让 is_structured 标志失真且无从排查
            logger.warning("AI note structuring failed; keeping raw content: %s", str(exc)[:200])
            structured = content
    new_id = uuid.uuid4().hex[:12]
    async with _db_s() as _db:
        n = _N(id=new_id, subject=subject, title=title, content=structured,
               knowledge_tags=[], is_structured=True, source_type="ai_generated",
               auto_generated=False)
        _db.add(n)
        await _db.commit()
    reply = f"笔记 **{title}** 已创建（#{new_id[:8]}）。可在笔记页查看和编辑。"
    agent_tool_calls_add(c.sid, "创建笔记", {"title": title, "subject": subject},
                         f"笔记已创建 #{new_id[:8]}", "done")
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


async def h_modify_note(c: Ctx) -> dict:
    from models.database import async_session as _db_s
    from models.models import Note as _N
    from sqlalchemy import select as _sel
    from datetime import datetime as _dt, timezone as _tz

    keyword = str(c.data.get("keyword", "") or "")[:200]
    field = c.data.get("field", "")
    value = str(c.data.get("value", "") or "").strip()
    if not keyword:
        reply = "请告诉我你要修改哪篇笔记？可以提供关键词或标题。"
        c.commit(reply)
        return c.finish(reply)
    async with _db_s() as _db:
        kw_escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        r = await _db.execute(_sel(_N).where(_N.title.contains(kw_escaped, escape="\\")).limit(1))
        n = r.scalars().first()
        if not n:
            reply = f"未找到标题包含「{keyword}」的笔记。试试查看笔记列表？"
        else:
            if field in ("content", "内容"):
                n.content = value[:500_000]
            elif field in ("title", "标题"):
                n.title = value[:200]
            elif field in ("subject", "学科"):
                n.subject = value[:64]
            elif field in ("tags", "标签"):
                n.knowledge_tags = [t.strip() for t in value.split(",") if t.strip()][:50]
            else:
                reply = f"不支持的修改字段: {field}。支持: content/title/subject/tags"
                c.commit(reply)
                return c.finish(reply)
            n.updated_at = _dt.now(_tz.utc).replace(tzinfo=None)
            await _db.commit()
            # M5（2026-09-09）：与正规笔记更新链对齐——content 变更后同步
            # 刷新知识图谱中该笔记的摘要片段（notes.py 更新路径同款容错）
            if field in ("content", "内容"):
                try:
                    from services.knowledge_graph import refresh_note_snippet as _rns
                    _rns(n.id, n.content or "")
                except Exception as _ge:
                    logger.warning("Agent modify_note graph refresh failed for %s: %s", n.id, _ge)
            reply = f"笔记「{n.title}」的 **{field}** 已更新。"
            agent_tool_calls_add(c.sid, "修改笔记",
                                 {"note_id": n.id, "field": field, "value": str(value)[:100]},
                                 f"已更新 {field}", "done")
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


# ==========================================================================
# 错误信息
# ==========================================================================

async def h_error_info(c: Ctx) -> dict:
    from models.database import async_session as _db_s4
    from models.models import Question as _Q2
    from sqlalchemy import select as _sel2

    qid = c.data.get("question_id", "")
    async with _db_s4() as _db:
        if qid:
            q = await _db.get(_Q2, qid)
            if not q:
                reply = f"未找到题目 #{qid[:8]}"
            else:
                flags = q.audit_flags if hasattr(q, "audit_flags") and q.audit_flags else []
                err_msg = q.error_message or "无错误记录"
                flag_str = ""
                if isinstance(flags, list) and flags:
                    flag_items = [f"{f.get('type','')}: {f.get('reason','')}" for f in flags]
                    flag_str = "\n审计标记: " + "; ".join(flag_items)
                prev = (q.ocr_text or q.question_html or "")[:80]
                reply = (f"题目 #{qid[:8]} [{q.subject}][{q.grade}]\n{prev}...\n"
                         f"状态: {q.status}\n错误: {err_msg}{flag_str}")
        else:
            r = await _db.execute(_sel2(_Q2).where(_Q2.status == "error")
                                  .order_by(_Q2.created_at.desc()).limit(10))
            errors = r.scalars().all()
            if not errors:
                # Also check for flagged-but-not-error questions
                r2 = await _db.execute(
                    _sel2(_Q2).where(_Q2.status == "done")
                    .order_by(_Q2.created_at.desc()).limit(50)
                )
                flagged = [q for q in r2.scalars().all()
                           if hasattr(q, "audit_flags") and q.audit_flags and len(q.audit_flags) > 0]
                if not flagged and not errors:
                    reply = "当前没有标记题目或错误题目。"
                else:
                    all_items = list(errors) + flagged
                    lines = [f"共 {len(all_items)} 题存在问题："]
                    for i, eq in enumerate(all_items[:15], 1):
                        prev = (eq.ocr_text or eq.question_html or "")[:50].replace("\n", " ")
                        eq_flags = eq.audit_flags if hasattr(eq, "audit_flags") and eq.audit_flags else []
                        flag_summary = ""
                        if isinstance(eq_flags, list) and eq_flags:
                            flag_summary = " [标记:" + ",".join(f.get("type", "?") for f in eq_flags) + "]"
                        lines.append(f"{i}. #{eq.id[:8]} [{eq.subject}][{eq.grade}] {prev}... - "
                                     f"{eq.error_message or eq.status}{flag_summary}")
                    reply = "\n".join(lines)
            else:
                lines = [f"共 {len(errors)} 道错误题目："]
                for i, eq in enumerate(errors, 1):
                    prev = (eq.ocr_text or eq.question_html or "")[:50].replace("\n", " ")
                    lines.append(f"{i}. #{eq.id[:8]} [{eq.subject}][{eq.grade}] {prev}... - "
                                 f"{eq.error_message or '未知错误'}")
                reply = "\n".join(lines)
    agent_tool_calls_add(c.sid, "查看错误信息", {"question_id": qid or "all"}, reply[:100], "done")
    c.commit(reply)
    return c.finish(reply, tool_calls=True)


# ==========================================================================
# 相关内容接入（2026-09 用户要求「接入全做」）
# ==========================================================================
#
# 这一节最容易写错的地方是**状态语义**。项目有「容量诚实」硬约束：
# 每个来源必须给出可区分的状态，而不是统一成一句"没找到"：
#
#   ok        取到了内容
#   no_match  来源**有**数据，但没有匹配这条主题的
#   empty     来源**本身就是空的**（例如知识点图谱还没建立任何节点）
#   error     检索过程抛错，并带上原因
#
# "来源是空的" 与 "没匹配上" 是两件完全不同的事。混成一句，
# 用户会以为是自己没记过笔记，而实际可能是图谱压根没建数据。

_STATUS_TEXT = {
    "ok": "已取到",
    "no_match": "无匹配",
    "empty": "来源为空",
    "error": "检索失败",
}


def _src(key: str, label: str, status: str, items: list | None = None, note: str = "") -> dict:
    items = items or []
    return {"key": key, "label": label, "status": status,
            "count": len(items), "note": note, "items": items}


async def _src_questions(topic: str, subject: str, grade: str, limit: int) -> dict:
    from sqlalchemy import func as _func, select as _sel
    from models.database import async_session as _db_s
    from models.models import Question as _Q
    from services.paper_service import paper_service as _ps
    try:
        async with _db_s() as _db:
            total = (await _db.execute(_sel(_func.count(_Q.id)).where(_Q.status == "done"))).scalar() or 0
        if not total:
            return _src("questions", "题库", "empty", note="题库里还没有处理完成的题目")
        params: dict = {}
        if subject:
            params["subject"] = subject
        if grade:
            params["grade"] = grade
        qids = await _ps._search_questions_for_worksheet(params, topic, limit=limit)
        if not qids:
            return _src("questions", "题库", "no_match",
                        note=f"题库里有 {total} 道题，但没有和这个主题对上的")
        rows = await _ps._load_questions(list(qids))
        items = [{
            "id": r.get("id", ""),
            "text": (r.get("ocr_text") or r.get("question_html") or "")[:80],
            "meta": f"{r.get('subject', '')} {r.get('grade', '')}".strip(),
        } for r in rows]
        return _src("questions", "题库", "ok", items)
    except Exception as exc:
        logger.warning("related_content.questions failed: %s", exc)
        return _src("questions", "题库", "error", note=f"检索失败：{str(exc)[:100]}")


async def _src_notes(topic: str, subject: str, grade: str, limit: int) -> dict:
    from sqlalchemy import func as _func, select as _sel
    from models.database import async_session as _db_s
    from models.models import Note as _N
    from services.paper_service import paper_service as _ps
    try:
        async with _db_s() as _db:
            total = (await _db.execute(_sel(_func.count(_N.id)))).scalar() or 0
        if not total:
            return _src("notes", "笔记", "empty", note="还没有任何笔记")
        params: dict = {}
        if subject:
            params["subject"] = subject
        if grade:
            params["grade"] = grade
        notes = await _ps._search_notes(params, topic, note_count=limit)
        if not notes:
            return _src("notes", "笔记", "no_match",
                        note=f"有 {total} 篇笔记，但没有和这个主题对得上的")
        items = []
        for n in notes:
            tags = ", ".join(getattr(n, "knowledge_tags", None) or [])
            items.append({
                "id": getattr(n, "id", ""),
                "text": getattr(n, "title", "") or "(无标题)",
                "meta": f"{getattr(n, 'subject', '') or '未分类'}{' / ' + tags if tags else ''}",
            })
        return _src("notes", "笔记", "ok", items)
    except Exception as exc:
        logger.warning("related_content.notes failed: %s", exc)
        return _src("notes", "笔记", "error", note=f"检索失败：{str(exc)[:100]}")


async def _src_graph(topic: str, limit: int) -> dict:
    from services.knowledge_graph import _load_graph, search_graph
    try:
        graph = await asyncio.to_thread(_load_graph)
        nodes = graph.get("nodes") or []
        if not nodes:
            return _src("graph", "知识点图谱", "empty",
                        note="知识点图谱还没有任何节点（它目前只在笔记带知识点标签时自动入图）")
        hits = await asyncio.to_thread(search_graph, topic, limit)
        if not hits:
            return _src("graph", "知识点图谱", "no_match",
                        note=f"图谱里有 {len(nodes)} 个知识点，但没有和这个主题对上的")
        items = [{"id": n.get("id", ""), "text": n.get("label", ""),
                  "meta": n.get("type", "")} for n in hits]
        return _src("graph", "知识点图谱", "ok", items)
    except Exception as exc:
        logger.warning("related_content.graph failed: %s", exc)
        return _src("graph", "知识点图谱", "error", note=f"检索失败：{str(exc)[:100]}")


async def _src_memory(topic: str) -> dict:
    from services.hippocampus_service import get_teaching_context
    try:
        ctx = await asyncio.to_thread(get_teaching_context, "")
        topics = ctx.get("topics") or {}
        if not topics:
            return _src("memory", "学习记忆", "empty", note="还没有任何学习记录")
        key = topic.strip().lower()
        matched = {k: v for k, v in topics.items()
                   if key and (key in k.lower() or k.lower() in key)} if key else {}
        if not matched:
            return _src("memory", "学习记忆", "no_match",
                        note=f"记录里有 {len(topics)} 个主题，但没有和这个主题对上的")
        items = []
        for k, v in matched.items():
            try:
                pct = round(float(v.get("mastery") or 0) * 100)
            except (TypeError, ValueError):
                pct = 0
            wp = [str(x) for x in (v.get("weak_points") or [])][:5]
            items.append({"id": k, "text": f"{k}（掌握度 {pct}%）",
                          "meta": ("薄弱点: " + ", ".join(wp)) if wp else "无记录薄弱点"})
        return _src("memory", "学习记忆", "ok", items)
    except Exception as exc:
        logger.warning("related_content.memory failed: %s", exc)
        return _src("memory", "学习记忆", "error", note=f"检索失败：{str(exc)[:100]}")


def _render_related(topic: str, sources: list[dict]) -> str:
    lines = [f"围绕「{topic}」的相关内容："]
    for s in sources:
        lines.append(f"## {s['label']}（{_STATUS_TEXT.get(s['status'], s['status'])}）")
        if s["status"] == "ok":
            for i, it in enumerate(s["items"], 1):
                meta = f"（{it['meta']}）" if it.get("meta") else ""
                lines.append(f"{i}. {it['text']}{meta}")
        else:
            lines.append(str(s.get("note") or _STATUS_TEXT.get(s["status"], s["status"])))
    lines.append("只有标「已取到」的来源是真实内容；其余状态表示这一类来源**没有给出内容**，"
                 "不等于「查过了、没有问题」。")
    return "\n".join(lines)


async def h_related_content(c: Ctx) -> dict:
    """围绕一个主题聚合四类本地内容源。"""
    topic = str(c.data.get("topic") or c.data.get("keyword") or "").strip()
    if not topic:
        topic = c.message.strip()[:60]
    subject = str(c.data.get("subject") or "").strip()
    grade = str(c.data.get("grade") or "").strip()
    try:
        limit = max(1, min(int(c.data.get("limit", 5)), 20))
    except (TypeError, ValueError):
        limit = 5

    sources = [
        await _src_questions(topic, subject, grade, limit),
        await _src_notes(topic, subject, grade, limit),
        await _src_graph(topic, limit),
        await _src_memory(topic),
    ]
    reply = _render_related(topic, sources)
    agent_tool_calls_add(
        c.sid, "找相关内容",
        {"topic": topic, "subject": subject, "grade": grade},
        "; ".join(f"{s['label']}={s['status']}({s['count']})" for s in sources), "done")
    c.commit(reply)
    return c.finish(reply, {"type": "related_content",
                            "data": {"topic": topic, "sources": sources}},
                    tool_calls=True)


# 图谱现有的关系类型（来自 knowledge_graph._RELATION_TYPES）。
# 2026-09-12 起补上了**时序**关系 `prerequisite` / `successor`：
# 在这之前 8 种关系全是非时序的，「学 X 之前要先会什么」在图里无法表达。
_REL_LABEL = {
    "related": "相关", "is_a": "属于", "part_of": "包含", "created_by": "来源",
    "contemporary": "同期", "influenced": "影响", "belongs_to": "归属",
    "compared_with": "对比", "prerequisite": "前置", "successor": "后继",
}


async def h_knowledge_lookup(c: Ctx) -> dict:
    """查知识点图谱：搜索 / 关联网络 / 单点详情。"""
    query = str(c.data.get("query") or c.data.get("topic") or c.data.get("keyword") or "").strip()
    node_id = str(c.data.get("node_id") or "").strip()
    mode = str(c.data.get("mode") or "search").strip().lower()
    try:
        depth = max(1, min(int(c.data.get("depth", 1)), 3))
    except (TypeError, ValueError):
        depth = 1
    if not query and not node_id:
        query = c.message.strip()[:60]

    from services.knowledge_graph import _load_graph, get_related_nodes, search_graph
    try:
        graph = await asyncio.to_thread(_load_graph)
    except Exception as exc:
        reply = f"知识点图谱读取失败，没有取到任何数据。原因：{str(exc)[:120]}"
        c.commit(reply)
        return c.finish(reply, {"type": "knowledge_lookup",
                                "data": {"ok": False, "error": str(exc)[:200]}})

    nodes_map = graph.get("nodes") or {}
    if not isinstance(nodes_map, dict):
        # 数据结构与 knowledge_graph 的约定是 dict（见该模块头部注释）；
        # 万一被手工编辑成别的类型，按"空"处理而不是崩掉。
        nodes_map = {}
    if not nodes_map:
        reply = ("知识点图谱里**一个节点都还没有**，所以这里没有任何东西可查。\n"
                 "它不是「没查到」—— 是图谱本身是空的。图谱目前只在笔记带知识点标签时"
                 "自动入图，需要先记笔记（或在知识图谱页手动添加）才会长出内容。")
        c.commit(reply)
        return c.finish(reply, {"type": "knowledge_lookup",
                                "data": {"ok": True, "empty": True, "query": query}})

    # 注意：`graph["nodes"]` 是 **dict**（node_id -> node），不是 list。
    # 早先这里写成 `[n for n in nodes if n.get("id") == node_id]`，
    # 遍历 dict 拿到的是 key（字符串）-> `str.get` 直接 AttributeError。
    # 图谱为空时走的是上面的提前返回，所以这个 bug **在图谱还是空的时候看不出来**。
    hits = []
    if node_id and node_id in nodes_map:
        hits = [{**nodes_map[node_id], "id": node_id}]
    if not hits and query:
        hits = await asyncio.to_thread(search_graph, query, 8)
    if not hits:
        reply = (f"图谱里有 {len(nodes_map)} 个知识点，但没有匹配「{query}」的。"
                 f"换个说法试试，或者先看看图谱里都有哪些知识点。")
        c.commit(reply)
        return c.finish(reply, {"type": "knowledge_lookup",
                                "data": {"ok": True, "query": query, "matched": []}})

    center = hits[0]
    if mode == "prereq":
        # 「学这个之前要先会什么」。走 curriculum_service，返回结构里带 study_order。
        from services import curriculum_service as _cs
        pr = await asyncio.to_thread(_cs.prerequisites_of,
                                     str(center.get("label") or ""), depth)
        if not pr.get("found"):
            lines = [f"图谱里找不确切这个知识点：{pr.get('note', '')}"]
        elif not pr.get("levels"):
            lines = [f"图谱里**没有记录**「{center.get('label')}」的前置关系，"
                     f"所以给不出「先补什么」—— 这里不凭推断编一个顺序。"]
        else:
            lines = [f"要掌握「{center.get('label')}」，建议按这个顺序先补齐（由远到近）："]
            for i, item in enumerate(pr.get("study_order") or [], 1):
                g = f"（{item.get('grade')}）" if item.get("grade") else ""
                lines.append(f"{i}. {item.get('label')}{g}")
            lines.append("")
            lines.append("顺序来自课程体系里的前置依赖关系；"
                         "同一层的知识点之间没有先后要求，可以任意顺序。")
    elif mode == "related":
        net = await asyncio.to_thread(get_related_nodes, center["id"], depth)
        edges = net.get("edges") or []
        # 同样：nodes 是 dict，取标签要按 id 查表，不能遍历
        label_of = {nid: n.get("label") for nid, n in nodes_map.items()}
        by_rel: dict[str, list] = {}
        for e in edges:
            by_rel.setdefault(e.get("relation") or "related", []).append(e)
        lines = [f"以「{center.get('label')}」为中心的关联（深度 {depth}）："]
        if not by_rel:
            lines.append("它目前没有任何关联边。")
        has_prereq = bool(by_rel.get("prerequisite"))
        for rel, es in by_rel.items():
            lines.append(f"## {_REL_LABEL.get(rel, rel)}")
            seen_other = set()
            for e in es[:8]:
                other = e.get("to") if e.get("from") == center["id"] else e.get("from")
                if other in seen_other:
                    continue
                seen_other.add(other)
                lines.append(f"- {label_of.get(other, other)}")
        # 这句话必须**看着事实说**：有前置边还在说"没有先后顺序"就是错误信息。
        if has_prereq:
            lines.append("上列「前置 / 后继」是**有方向的**，可以据此安排先后；"
                         "其余关系不带先后语义。")
        else:
            lines.append("注：这个知识点目前**没有**前置/后继关系记录，"
                         "所以从它推不出学习先后；其余关系也不带先后语义。")
    elif mode == "detail":
        lines = [f"## {center.get('label')}（{center.get('type', '')}）"]
        for k in ("core", "description", "detail"):
            v = center.get(k)
            if v:
                lines.append(f"- {k}: {str(v)[:300]}")
        src = center.get("source_notes") or []
        if src:
            lines.append(f"- 来源笔记: {', '.join(str(x) for x in src[:5])}")
    else:
        lines = [f"图谱里匹配「{query}」的知识点 {len(hits)} 个："]
        for i, n in enumerate(hits[:10], 1):
            lines.append(f"{i}. {n.get('label')}（{n.get('type', '')}）")

    reply = "\n".join(lines)
    agent_tool_calls_add(c.sid, "查知识点",
                         {"query": query, "mode": mode, "node_id": center.get("id", "")},
                         f"命中 {len(hits)} 个", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "knowledge_lookup",
                            "data": {"ok": True, "query": query, "mode": mode,
                                     "center": {"id": center.get("id", ""),
                                                "label": center.get("label", "")},
                                     "matched": [{"id": n.get("id", ""), "label": n.get("label", ""),
                                                  "type": n.get("type", "")} for n in hits[:10]]}},
                    tool_calls=True)


async def h_recall_memory(c: Ctx) -> dict:
    """读这个学生的历史学习记录（掌握度 / 薄弱点 / 时长 / 最近学习）。"""
    topic = str(c.data.get("topic") or c.data.get("keyword") or "").strip()
    from services.hippocampus_service import get_teaching_context
    try:
        ctx = await asyncio.to_thread(get_teaching_context, topic)
    except Exception as exc:
        reply = f"学习记忆读取失败，没有取到任何数据。原因：{str(exc)[:120]}"
        c.commit(reply)
        return c.finish(reply, {"type": "recall_memory",
                                "data": {"ok": False, "error": str(exc)[:200]}})

    topics = ctx.get("topics") or {}
    meta = ctx.get("meta") or {}
    if not topics:
        reply = ("学习记忆里**没有任何主题记录** —— 这表示还没有产生过学习数据"
                 "（不是「学过但没记住」）。专注模式/讲题产生学习行为后才会写入。")
        c.commit(reply)
        return c.finish(reply, {"type": "recall_memory", "data": {"ok": True, "empty": True}})

    lines = []
    if topic:
        lines.append(f"「{topic}」的学习记录：")
    else:
        lines.append(f"全部学习记录（共 {len(topics)} 个主题）：")
        lines.append(f"基础画像：理解力 {meta.get('baseline_understanding', '')}，"
                     f"记忆力 {meta.get('baseline_memory', '')}，"
                     f"专注力 {meta.get('baseline_focus', '')}，"
                     f"偏好方式 {meta.get('preferred_style', '')}")
    for t, v in list(topics.items())[:20]:
        try:
            pct = round(float(v.get("mastery") or 0) * 100)
        except (TypeError, ValueError):
            pct = 0
        wp = [str(x) for x in (v.get("weak_points") or [])][:5]
        lines.append(f"## {t}")
        lines.append(f"- 掌握度（已按遗忘曲线衰减）：{pct}%")
        lines.append(f"- 薄弱点：{'、'.join(wp) if wp else '无记录'}")
        lines.append(f"- 累计学习：{v.get('total_minutes', 0)} 分钟")
        lines.append(f"- 检查点通过率：{v.get('checkpoint_pass_rate', 0)}")
        lines.append(f"- 最近学习：{v.get('last_study') or '无记录'}")
    lines.append("说明：掌握度是**衰减后的估计值**，不是实测分数；「无记录」表示没有这条数据，"
                 "不代表该项为零。")
    reply = "\n".join(lines)
    agent_tool_calls_add(c.sid, "回忆学习记录", {"topic": topic or "all"},
                         f"{len(topics)} 个主题", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "recall_memory",
                            "data": {"ok": True, "topic": topic, "meta": meta,
                                     "topics": {k: {"mastery": v.get("mastery"),
                                                    "weak_points": v.get("weak_points") or [],
                                                    "total_minutes": v.get("total_minutes"),
                                                    "last_study": v.get("last_study")}
                                                for k, v in list(topics.items())[:20]}}},
                    tool_calls=True)


async def h_web_search(c: Ctx) -> dict:
    """联网检索外部资料。

    诚实性要求（这条最容易做假）：检索**失败**时必须说"没搜到"，绝不能拿模型
    自己的记忆冒充"网上查到的"。所以这里失败直接给失败文案，不产出任何"结果"。
    """
    query = str(c.data.get("query") or c.data.get("keyword") or "").strip()
    if not query:
        query = c.message.strip()[:200]
    if not query:
        reply = "请告诉我要联网查什么。"
        c.commit(reply)
        return c.finish(reply, {"type": "web_search", "data": {"ok": False,
                                                               "error": "空查询"}})

    prompt = (
        f"请联网搜索下面的内容，整理成适合中学生看的参考资料：\n\n{query}\n\n"
        "要求：只写你确实搜到的内容；如果搜不到就明确写「没有搜到」；"
        "不要凭记忆编造，保留关键数据与出处。"
    )
    try:
        res = await ai_service.glm_web_search(prompt, temperature=0.4, max_tokens=2048)
    except Exception as exc:
        logger.warning("web_search failed: %s", exc)
        reply = ("联网检索**没有成功**，所以我没有拿到任何搜索结果，下面不会有内容 —— "
                 "免得把模型自己的记忆冒充成网上查到的。\n"
                 f"原因：{str(exc)[:150]}")
        agent_tool_calls_add(c.sid, "联网检索", {"query": query[:100]},
                             f"失败: {str(exc)[:80]}", "error")
        c.commit(reply)
        return c.finish(reply, {"type": "web_search",
                                "data": {"ok": False, "query": query,
                                         "error": str(exc)[:200]}},
                        tool_calls=True)

    text = res["text"]
    results = res["results"]

    # 模型的套话陷阱：实测中模型会一边说「我无法直接联网搜索」，
    # 一边使用注入的检索结果作答。如果不加处理，用户会看到一段自称"没联网"
    # 的文字被我们标成"联网检索结果" —— 这是误导。
    # 处理方式不是删掉那句话（那是篡改模型输出），而是把**检索到的原文**一并给出，
    # 让"到底联没联网"由原始材料本身说话。
    _boilerplate = [m for m in ("无法直接联网", "无法联网", "不能联网", "无法访问互联网")
                    if m in text]
    lines = []
    if _boilerplate:
        lines.append(
            f"（说明：模型正文里出现了「{_boilerplate[0]}」这类套话，但本次检索确实取回了 "
            f"{len(results)} 条网页原文，已附在下面 —— 请以下面的原始片段为准。）")
        lines.append("")
    lines.append(f"联网检索结果（检索词：{query}，命中 {len(results)} 条网页）：")
    lines.append("")
    lines.append(text)
    lines.append("")
    lines.append(f"## 检索到的原始网页片段（{len(results)} 条）")
    for i, r in enumerate(results[:8], 1):
        title = r.get("title") or ""
        link = r.get("link") or ""
        head = f"{i}. {title}" if title else f"{i}."
        if link:
            head += f" {link}"
        lines.append(head)
        lines.append(f"   {r['content'][:300]}")
    reply = "\n".join(lines)

    agent_tool_calls_add(c.sid, "联网检索", {"query": query[:100]},
                         f"命中 {len(results)} 条网页，正文 {len(text)} 字", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "web_search",
                            "data": {"ok": True, "query": query, "text": text,
                                     "results": [{"title": r.get("title", ""),
                                                  "link": r.get("link", ""),
                                                  "excerpt": r["content"][:200]}
                                                 for r in results[:8]]}},
                    tool_calls=True)


# ==========================================================================
# 后台任务召回（R23）：任务完成 → 系统自动把 Agent 叫回来继续讲
# ==========================================================================
# 链路：后台任务 done → 前端任务卡片轮询到终态 → 自动发送「[系统召回]」消息
# → sessions.py 识别前缀走确定性旁路（不经意图分类器，零误路由）
# → 本处理器读任务真实产出 → 交给对话模型接着讲。
# 之前的行为是任务完成后卡片只显示「任务已完成」，Agent 不知道、
# 用户必须自己开口问——「工具执行完毕召回 Agent 继续工作」缺的就是这一环。

async def h_task_recall(c: Ctx) -> dict:
    """后台任务完成后的召回：读任务真实产出，让对话 Agent 接着讲。"""
    from services import background_agent as _ba

    task_id = str(c.data.get("task_id") or "").strip()
    if not task_id:
        reply = ("收到召回信号，但消息里没有带 task_id，无法读取任务结果。"
                 "可以直接告诉我你想继续什么话题。")
        c.commit(reply)
        return c.finish(reply)

    try:
        t = await _ba.get_task(task_id)
    except _ba.TaskNotFound:
        reply = "后台任务不存在或已被清理，无法召回继续。"
        c.commit(reply)
        return c.finish(reply)

    if t.get("status") != "done":
        reply = (f"后台任务《{t.get('title') or ''}》当前状态是「{t.get('status')}」，"
                 f"还没到汇报结果的时候；等它完成会再召回。")
        c.commit(reply)
        return c.finish(reply)

    # 按工具类型挑关键字段组装产出摘要；未知工具退化为截断 JSON。
    out = t.get("output") or {}
    tool_name = str(t.get("tool") or "")
    try:
        if tool_name == "prepare_lesson":
            parts = [f"课稿《{out.get('title') or ''}」已生成："
                     f"{out.get('total_sections', '?')} 片中本次新写 "
                     f"{out.get('filled_now', 0)} 片。"]
            if out.get("empty_sections"):
                parts.append(f"注意：还有 {out['empty_sections']} 片讲解词为空"
                             f"（生成未完成或被取消，可续跑补齐）。")
            if out.get("note"):
                parts.append(str(out["note"]))
            brief = "".join(parts)
        else:
            brief = json.dumps(out, ensure_ascii=False)[:1500] if out else "（任务无结构化产出）"
    except Exception as exc:
        logger.warning("task_recall output summary failed for %s: %s", task_id, exc)
        brief = "（产出摘要组装失败，请直接读取任务详情）"

    system = (
        "你是学习搭子 Agent。刚才一个后台任务完成了，系统自动把你召回。"
        "请基于下面的任务产出，自然地向用户汇报结果，并继续之前的话题或讲解。"
        "不要复述本条指令，不要编造产出里没有的内容。\n任务产出：\n" + brief
    )
    full_messages = [{"role": "system", "content": system}] + c.messages[-20:]
    if c.step_callback:
        c.step_callback("任务召回", "汇报结果并继续讲解", "running")
    reply = await ai_service.deepseek_chat(
        full_messages, max_tokens=8192, scope="chat", step_callback=c.step_callback)
    if c.step_callback:
        c.step_callback("任务召回", "汇报结果并继续讲解", "done")

    agent_tool_calls_add(c.sid, "任务召回", {"task_id": task_id},
                         f"任务《{t.get('title') or ''}》完成，已继续讲解", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "task_recall",
                            "data": {"task_id": task_id, "status": "done"}},
                    tool_calls=True)


# ==========================================================================
# 备课与课稿（P7）
# ==========================================================================
#
# 这一组是**双端架构的第一个真实用户**：`prepare_lesson` 在前台**立刻返回**
# （只建骨架 + 起后台任务），生成讲解词的活儿丢给后台推理端。
# 用户不用盯着转圈等，而且随时能取消；取消后课稿仍然可读、可续写。

async def h_prepare_lesson(c: Ctx) -> dict:
    from services import background_agent as _ba
    from services import lesson_service as _ls

    qids = c.data.get("question_ids") or []
    if isinstance(qids, str):
        qids = [x.strip() for x in qids.split(",") if x.strip()]
    params = {
        "topic": str(c.data.get("topic") or "").strip(),
        "subject": str(c.data.get("subject") or "").strip(),
        "grade": str(c.data.get("grade") or "").strip(),
        "question_ids": list(qids)[:50],
        "paper_id": str(c.data.get("paper_id") or "").strip(),
    }
    if not (params["topic"] or params["question_ids"] or params["paper_id"]):
        # 什么选材条件都没给：用用户这句话当主题，而不是空着让后台去猜
        params["topic"] = c.message.strip()[:60]
    try:
        section_count = max(2, min(int(c.data.get("section_count") or 6),
                                   _ls.MAX_SECTION_COUNT))
    except (TypeError, ValueError):
        section_count = _ls.DEFAULT_SECTION_COUNT

    brief = await _ls.create_lesson({**params, "origin": "auto"},
                                   title=str(c.data.get("title") or ""))
    task = await _ba.create_task(
        sid=c.sid, tool="prepare_lesson",
        title=f"备课：{brief['title']}",
        params={"lesson_id": brief["lesson_id"], "section_count": section_count})
    _ba.spawn(task["task_id"])

    reply = ("备课已开始，**在后台生成**，你可以继续聊别的。\n"
             f"- 课稿：**{brief['title']}**（#{brief['lesson_id'][:8]}）\n"
             f"- 计划分片：{section_count} 片\n"
             f"- 任务：`{task['task_id']}`\n\n"
             "它每写完一片就会存一片，所以随时中止都已写好的部分都在。"
             "想停就说一声（我可以取消它）；想继续就说接着备。")
    agent_tool_calls_add(c.sid, "备课", {"lesson_id": brief["lesson_id"]},
                         f"后台任务已起 {task['task_id']}", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "agent_task",
                            "data": {**task, "lesson_id": brief["lesson_id"]}},
                    tool_calls=True)


async def h_list_lessons(c: Ctx) -> dict:
    from services import lesson_service as _ls
    try:
        limit = max(1, min(int(c.data.get("limit", 20)), 100))
    except (TypeError, ValueError):
        limit = 20
    res = await _ls.list_lessons(limit=limit, status=str(c.data.get("status") or ""))
    if not res["lessons"]:
        reply = ("还没有任何课稿。想让我备一节课的话，直接说「帮我备一节关于X的课」即可。\n"
                 "（这是「没有课稿」，不是「查不到」——课稿是本地生成的，不会因检索失败而空。）")
    else:
        lines = [f"共 {res['total']} 份课稿："]
        for i, L in enumerate(res["lessons"], 1):
            lines.append(f"{i}. **{L['title']}** [{L['subject'] or '未分类'} {L['grade'] or ''}]"
                         f" 分片 {L['filled_sections']}/{L['total_sections']}"
                         f" 状态 {L['status']}（#{L['lesson_id'][:8]}）")
        reply = "\n".join(lines)
    c.commit(reply)
    return c.finish(reply, {"type": "lessons_list",
                            "data": {"total": res["total"], "lessons": res["lessons"]}},
                    tool_calls=True)


async def h_read_lesson(c: Ctx) -> dict:
    """读课稿（带切片）。**任何需要讲解内容的 AI 都走这一个契约。**"""
    from services import lesson_service as _ls
    lesson_id = str(c.data.get("lesson_id") or "").strip()
    idx = c.data.get("section_index")
    try:
        idx = None if idx in (None, "") else int(idx)
    except (TypeError, ValueError):
        idx = None
    try:
        offset = max(0, int(c.data.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        max_chars = max(200, min(int(c.data.get("max_chars") or 6000), 48000))
    except (TypeError, ValueError):
        max_chars = 6000

    if not lesson_id:
        raw = str(c.data.get("keyword") or c.data.get("title") or "").strip()
        res = await _ls.list_lessons(limit=20)
        hit = None
        for L in res["lessons"]:
            if raw and raw in L["title"]:
                hit = L
                break
        if hit is None and len(res["lessons"]) == 1:
            hit = res["lessons"][0]
        if hit is None:
            if not res["lessons"]:
                reply = "还没有任何课稿，无法读取。"
            else:
                titles = "、".join(L["title"] for L in res["lessons"][:5])
                reply = f"没确定要读哪一份课稿。现有的有：{titles}。告诉我标题或编号即可。"
            c.commit(reply)
            return c.finish(reply)
        lesson_id = hit["lesson_id"]

    try:
        data = await _ls.slice_lesson(lesson_id, section_index=idx,
                                      offset=offset, max_chars=max_chars)
    except _ls.LessonNotFound:
        reply = f"课稿不存在：{lesson_id}"
        c.commit(reply)
        return c.finish(reply)

    if data.get("error"):
        reply = data["error"]
        c.commit(reply)
        return c.finish(reply)

    lines = [f"**{data['title']}**（{data['total_sections']} 片，本次返回 "
             f"{data['returned']} 片，已用 {data['used_chars']}/{data['budget_chars']} 字）"]
    if data.get("empty_sections"):
        lines.append(f"注：还有 {data['empty_sections']} 片的讲解词是空的"
                     f"（生成未完成或被取消）。")
    for s in data["sections"]:
        lines.append(f"## {s['index'] + 1}. {s['heading']}")
        if s["empty"]:
            lines.append("（这一片还没有讲解词。）")
        else:
            lines.append(s["script"] + ("……（本片已截断）" if s["script_truncated"] else ""))
    if data["truncated"]:
        lines.append(f"（**还有没返回的内容**：本次只给了 {data['returned']} 片 / "
                     f"{data['total_sections']} 片。要更多请说「继续读第 N 片」。）")
    reply = "\n".join(lines)
    agent_tool_calls_add(c.sid, "读课稿", {"lesson_id": lesson_id, "offset": offset},
                         f"返回 {data['returned']} 片", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "lesson_read", "data": data}, tool_calls=True)


async def h_edit_lesson(c: Ctx) -> dict:
    """改一片课稿（「人决定怎么走」的落点：自动生成留下的东西必须能被人改）。"""
    from services import lesson_service as _ls
    lesson_id = str(c.data.get("lesson_id") or "").strip()
    idx = c.data.get("section_index")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        reply = "请告诉我要改第几片（section_index 从 0 开始）。"
        c.commit(reply)
        return c.finish(reply)
    heading = c.data.get("heading")
    script = c.data.get("script")
    if heading is None and script is None:
        reply = "请给出要修改的内容（heading 或 script）。"
        c.commit(reply)
        return c.finish(reply)
    try:
        brief = await _ls.update_section(
            lesson_id, idx,
            heading=None if heading is None else str(heading),
            script=None if script is None else str(script))
    except _ls.LessonNotFound:
        reply = f"课稿或分片不存在：{lesson_id} #{idx}"
        c.commit(reply)
        return c.finish(reply)
    reply = f"课稿「{brief['title']}」第 {idx + 1} 片已更新。"
    agent_tool_calls_add(c.sid, "改课稿",
                         {"lesson_id": lesson_id, "section_index": idx},
                         "已更新", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "lessons_list",
                            "data": {"total": 1, "lessons": [brief]}}, tool_calls=True)


async def h_import_and_prep(c: Ctx) -> dict:
    """R30：智能上传之后的一条龙 —— 汇总导入结果，并启动备课。

    学生流程：整卷页「智能上传」丢一堆文件 → 在 Agent 说「整理并备课」。
    本工具**不接收文件**（文件走 HTTP 上传），只消费已有导入结果：
    - session_id：试卷会话内的题
    - 或最近 source_type in (photo/pdf_page/text/docx) 且 capture_group_id 相同的题
    - 有 paper_id 则备课挂试卷；否则挂 topic
    """
    from models.database import async_session
    from models.models import Question, UploadSession
    from sqlalchemy import select, desc
    from services import lesson_service as _ls

    topic = str(c.data.get("topic") or "").strip()
    subject = str(c.data.get("subject") or "").strip()
    grade = str(c.data.get("grade") or "").strip()
    session_id = str(c.data.get("session_id") or "").strip()
    paper_id = str(c.data.get("paper_id") or "").strip()
    make_paper = bool(c.data.get("make_paper"))
    try:
        section_count = max(2, min(int(c.data.get("section_count") or 6), 16))
    except (TypeError, ValueError):
        section_count = 6

    qids: list[str] = []
    note_ids: list[str] = []
    sess_title = ""

    async with async_session() as db:
        if session_id:
            sess = await db.get(UploadSession, session_id)
            if sess:
                qids = [str(x) for x in (sess.question_ids or []) if x]
                sess_title = sess.title or ""
        if not qids:
            # 最近智能上传/导入的题（近 2 小时内 staged/done 的 pdf/text/photo）
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
            r = await db.execute(
                select(Question.id, Question.ocr_text, Question.subject)
                .where(Question.source_type.in_(("photo", "pdf_page", "text", "docx")))
                .where(Question.created_at >= cutoff)
                .order_by(desc(Question.created_at))
                .limit(30)
            )
            rows = r.all()
            qids = [row[0] for row in rows]
            if not subject and rows:
                subject = rows[0][2] or subject

        # 最近笔记
        from models.models import Note
        rn = await db.execute(
            select(Note.id).order_by(desc(Note.updated_at)).limit(5)
        )
        note_ids = [x[0] for x in rn.all()]

    if not qids and not note_ids:
        reply = (
            "最近没有找到导入的题目或笔记。"
            "请先到「整卷上传 → 智能上传」把图片/PDF/Word/文本丢进去，"
            "等处理完成后再对我说「整理并备课」。"
        )
        c.commit(reply)
        return c.finish(reply, {"type": "import_prep", "data": {"ok": False, "reason": "empty"}})

    # 备课
    if not topic:
        topic = sess_title or subject or "导入内容整理"
    try:
        brief = await _ls.create_lesson({
            "topic": topic, "subject": subject, "grade": grade,
            "question_ids": qids[:20], "paper_id": paper_id,
        }, title=f"导入备课 · {topic}")
        task = None
        from services import background_agent as BA
        task = await BA.create_task(
            sid=c.sid, tool="prepare_lesson",
            title=f"备课：{topic}",
            params={"lesson_id": brief["lesson_id"], "section_count": section_count},
        )
        BA.spawn(task["task_id"])
        lesson_id = brief["lesson_id"]
    except Exception as exc:
        log_error("agent.import_and_prep", str(exc)[:200])
        reply = f"已找到导入内容（题 {len(qids)} / 笔记 {len(note_ids)}），但备课启动失败：{exc}"
        c.commit(reply)
        return c.finish(reply, {"type": "import_prep",
                                "data": {"ok": False, "question_count": len(qids)}})

    lines = [
        f"已接上导入内容，开始备课「{topic}」。",
        f"- 题目：{len(qids)} 道（最近导入）",
        f"- 笔记：{len(note_ids)} 篇",
        f"- 课稿骨架：{lesson_id}（后台生成 {section_count} 片，完成后会召回）",
        "你可以在「课稿」页查看/改讲解词；也可说「读课稿」。",
    ]
    if make_paper and not paper_id:
        lines.append("如需组卷，告诉我「确认自动组卷」，或到组卷中心操作。")
    reply = "\n".join(lines)
    agent_tool_calls_add(c.sid, "导入整理并备课",
                         {"topic": topic, "question_count": len(qids)},
                         f"课稿 {lesson_id}", "done")
    c.commit(reply)
    return c.finish(reply, {
        "type": "agent_task",
        "data": {
            "task_id": task["task_id"] if task else "",
            "lesson_id": lesson_id,
            "question_ids": qids[:20],
            "note_ids": note_ids,
        },
    }, tool_calls=True)


async def h_review_plan(c: Ctx) -> dict:
    """今日补漏清单：把课程体系 + 掌握度 + 错题合成一份可执行的清单。"""
    from services import curriculum_service as _cs
    subject = str(c.data.get("subject") or "").strip()
    try:
        limit = max(1, min(int(c.data.get("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    plan = await _cs.build_review_plan(subject=subject, limit=limit,
                                       grade=str(c.data.get("grade") or "").strip())
    items = plan.get("items") or []
    if not items:
        reply = plan.get("note") or "现在给不出补漏清单。"
        c.commit(reply)
        return c.finish(reply, {"type": "review_plan", "data": plan})

    ev_cn = {"has_wrong_questions": "有错题", "has_mastery_record": "有学习记录",
             "curriculum_only": "仅有体系标签"}
    scope = "、".join(x for x in (subject, plan.get("grade") or "") if x)
    lines = [f"今天建议补这些{'（' + scope + '）' if scope else ''}，按优先级排："]
    for i, it in enumerate(items, 1):
        bits = [f"{it['subject']} {it['grade']}"]
        if it["wrong_question_count"]:
            bits.append(f"错题 {it['wrong_question_count']} 道")
        if it["mastery"] is not None:
            bits.append(f"掌握度约 {round(it['mastery'] * 100)}%（估计）")
        bits.append(ev_cn.get(it["evidence"], it["evidence"]))
        if it["band"]:
            bits.append(it["band"])
        lines.append(f"{i}. **{it['label']}**（{'，'.join(bits)}）")
    lines.append("")
    lines.append("排序依据：先看有无错题（事实），再看掌握度（**按遗忘曲线衰减后的估计值**，"
                 "不是实测分数），最后按体系里标好的年级与难度。")
    lines.append("想知道某一条「之前要先会什么」，直接问我那个知识点的前置。")
    reply = "\n".join(lines)
    agent_tool_calls_add(c.sid, "补漏清单", {"subject": subject},
                         f"共 {plan['total']} 项，返回 {len(items)} 项", "done")
    c.commit(reply)
    return c.finish(reply, {"type": "review_plan", "data": plan}, tool_calls=True)


# ==========================================================================
# 工具表
# ==========================================================================

_TOOL_SPECS = [
    Tool("chat", "生成对话回复", "闲聊、解释概念、出变式题、评估答案、推荐组卷参数",
         foreground=True),
    Tool("solve", "推理解题", "对用户直接给出的题目做详细推理求解",
         foreground=True, aliases=("solve_question",)),
    Tool("search", "检索题库", "在本地题库里按关键词/学科/年级找已有题目",
         params_hint="keyword/subject/grade", foreground=True),
    Tool("solve_q", "从题库找题并解答", "先搜题库再解题并写回题目答案（= search + solve）",
         params_hint="问题描述/keywords", foreground=True,
         requires_confirm="确认写入解答", composes=("search", "solve")),
    Tool("paper", "准备组卷参数", "只保存组卷参数并跳转组卷页，不直接出卷",
         params_hint="subject/grade/type/topic", foreground=True, cancellable=True),
    Tool("auto_paper", "自动生成试卷", "直接生成完整试卷（耗时较长）",
         params_hint="subject/grade/type/topic", foreground=False, cancellable=True,
         requires_confirm="确认自动组卷", composes=("paper",)),
    Tool("add_question", "新增题目到题库", "把一道新题写入题库",
         params_hint="subject/grade/content(题目内容)/answer(答案)",
         foreground=True, requires_confirm="确认新增题目"),
    Tool("edit_question", "修改题库题目", "修改已有题目的字段",
         params_hint="question_id(唯一题目ID)/keyword(展示用)/field(修改字段)/value(新值)",
         foreground=True, requires_confirm="确认修改题目"),
    Tool("edit_paper", "修改试卷", "修改已有试卷的标题/学科/年级（不改题目）",
         params_hint="paper_id(唯一试卷ID)/field(修改字段: title/subject/grade)/value(新值)",
         foreground=True, requires_confirm="确认修改试卷"),
    Tool("delete_paper", "删除试卷", "删除整份试卷；卷内题目保留回题库",
         params_hint="paper_id(唯一试卷ID)",
         foreground=True, requires_confirm="确认删除试卷"),
    Tool("style", "更新排版偏好", "记住用户对题目排版/记号书写方式的偏好",
         params_hint="style_notes", foreground=True, requires_confirm="确认修改偏好"),
    Tool("profile", "更新用户信息", "记住学校/年级/学习目标等个人背景",
         params_hint="学校/年级/学习目标等", foreground=True, requires_confirm="确认保存资料"),
    Tool("need", "记录功能需求", "以上类型都不匹配时才用：用户想要一个还不存在的能力",
         params_hint="need", foreground=True),
    Tool("list_notes", "列出笔记", "列出笔记列表（可按学科/标签过滤）",
         params_hint="subject(可选)/tag(可选)", foreground=True),
    Tool("get_note", "查看笔记", "查看单篇笔记的正文",
         params_hint="keyword(找笔记关键词) 或 note_id", foreground=True),
    Tool("create_note", "创建笔记", "新建一篇笔记",
         params_hint="title/subject/content", foreground=True, requires_confirm="确认创建笔记"),
    Tool("modify_note", "修改笔记", "修改已有笔记的字段",
         params_hint="keyword(找笔记)/field/value", foreground=True,
         requires_confirm="确认修改笔记"),
    Tool("save_image", "保存图片到待处理题目", "把一张图片存进题库并进入 OCR 队列",
         params_hint="base64_image(图片base64)/subject(学科)/grade(年级)",
         foreground=True, requires_confirm="确认保存图片"),
    Tool("error_info", "查看题目错误信息", "查看题目处理错误与审计标记",
         params_hint="question_id(可选,无则列出所有标记题目)", foreground=True),
    # ---- 相关内容接入（2026-09 新增）----
    Tool("related_content", "找相关内容",
         "围绕一个主题聚合本地内容：题库题目 + 笔记 + 知识点图谱 + 学习记忆（跨源汇总时用这个）",
         params_hint="topic(主题或关键词)/subject/grade/limit",
         foreground=True, aliases=("related", "find_related", "related_material")),
    Tool("knowledge_lookup", "查知识点关系",
         "查知识点图谱：搜索知识点、看关联网络、看详情，或问「学这个之前要先会什么」（mode=prereq）",
         params_hint="query(要查的知识点)/node_id(已知节点ID)/mode(search|related|detail|prereq)/depth",
         foreground=True, aliases=("knowledge", "knowledge_graph")),
    Tool("recall_memory", "回忆学习历史",
         "查这个学生的历史学习记录：各主题掌握度、薄弱点、学习时长、最近学习时间",
         params_hint="topic(可选，不填则看全部主题)",
         foreground=True, aliases=("memory", "recall", "study_history")),
    Tool("review_plan", "今日补漏清单",
         "算出今天该补哪些知识点（按错题 + 掌握度 + 课程体系顺序）。"
         "学生问「我该学什么/今天补什么」时用这个",
         params_hint="subject(可选，如 数学)/grade(可选，如 九上)/limit",
         foreground=True, aliases=("today_plan", "study_plan", "what_to_study")),
    Tool("web_search", "联网查资料",
         "联网搜索外部资料。**只在本地题库/笔记里没有、确实需要外部信息时**才用它",
         params_hint="query(要搜什么)",
         foreground=True, needs_net=True, aliases=("websearch", "search_web")),
    # ---- 备课与课稿（P7）----
    Tool("prepare_lesson", "备课",
         "备一节课、生成可编辑课稿（后台生成，会立刻返回不等结果）",
         params_hint="topic(主题)/subject/grade/question_ids/paper_id/section_count(片数)",
         foreground=True, cancellable=True, aliases=("prepare", "lesson_prep")),
    Tool("list_lessons", "列出课稿", "看看已经备了哪些课稿",
         params_hint="status(可选: draft|final)/limit", foreground=True,
         aliases=("lessons",)),
    Tool("read_lesson", "读课稿",
         "读课稿内容（支持切片：不指定 lesson_id 就按关键词找）",
         params_hint="lesson_id(可选)/keyword(可选)/section_index(可选)/offset/max_chars",
         foreground=True, aliases=("get_lesson", "read_script")),
    Tool("edit_lesson", "改课稿",
         "改课稿的某一片：改标题或改讲解词",
         params_hint="lesson_id/section_index(从0开始)/heading(可选)/script(可选)",
         foreground=True, requires_confirm="确认修改课稿",
         aliases=("update_lesson",)),
    # ---- R30：导入后一条龙（分类→题/笔记/卷→备课）----
    Tool("import_and_prep", "导入整理并备课",
         "学生已用智能上传丢过文件后：汇总本次导入（题目/笔记/试卷会话），"
         "并**接着备课**生成可编辑课稿。"
         "学生说「把刚才传的整理好并备课/出卷备课」时用这个",
         params_hint="topic(课主题)/subject/grade/paper_id(可选)/session_id(可选)/make_paper(是否组卷)/section_count",
         foreground=True, cancellable=True,
         aliases=("bulk_import", "import_prep", "整理并备课")),
    # ---- 后台任务召回（R23）----
    # 正常用户消息不应判成这个；带 [系统召回] 前缀的消息在 sessions.py
    # 已走确定性旁路，这里登记主要是兜底与保持「表驱动」一致性。
    Tool("task_recall", "任务召回",
         "系统内部用：后台任务完成后的自动召回，把任务结果交给对话Agent继续讲解。"
         "用户自己打字的消息永远不要判成这个类型",
         params_hint="task_id", foreground=True, aliases=("recall_task",)),
]

_HANDLERS = {
    "chat": h_dialogue,
    "solve": h_dialogue,
    "search": h_search,
    "solve_q": h_solve_q,
    "paper": h_paper,
    "auto_paper": h_auto_paper,
    "add_question": h_add_question,
    "edit_question": h_edit_question,
    "edit_paper": h_edit_paper,
    "delete_paper": h_delete_paper,
    "style": h_style,
    "profile": h_profile,
    "need": h_need,
    "list_notes": h_list_notes,
    "get_note": h_get_note,
    "create_note": h_create_note,
    "modify_note": h_modify_note,
    "save_image": h_save_image,
    "error_info": h_error_info,
    # ---- 相关内容接入 ----
    "related_content": h_related_content,
    "knowledge_lookup": h_knowledge_lookup,
    "recall_memory": h_recall_memory,
    "review_plan": h_review_plan,
    "web_search": h_web_search,
    # ---- 备课与课稿 ----
    "prepare_lesson": h_prepare_lesson,
    "list_lessons": h_list_lessons,
    "read_lesson": h_read_lesson,
    "edit_lesson": h_edit_lesson,
    "import_and_prep": h_import_and_prep,
    # ---- 后台任务召回（R23）----
    "task_recall": h_task_recall,
}


def register_all() -> int:
    """把工具表装进 `agent_core.REGISTRY`。重复调用是幂等的。"""
    from dataclasses import replace
    from services.agent_core import REGISTRY, register

    added = 0
    for spec in _TOOL_SPECS:
        if spec.name in REGISTRY:
            continue
        handler = _HANDLERS.get(spec.name)
        if handler is None:
            raise RuntimeError(f"工具 {spec.name} 没有处理器，拒绝注册半成品")
        register(replace(spec, handler=handler))
        added += 1
    return added
