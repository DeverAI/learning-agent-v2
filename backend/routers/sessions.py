import os, json, time, re, uuid, asyncio, shutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from config import STORAGE_DIR, _atomic_write_json
from logger import get_logger, log_error
from services.user_profile import save_profile, load_profile

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
logger = get_logger()

SESSIONS_DIR = os.path.join(STORAGE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
import weakref as _weakref_s
_session_chat_locks: _weakref_s.WeakValueDictionary[str, asyncio.Lock] = _weakref_s.WeakValueDictionary()
_session_chat_locks_creation_lock = asyncio.Lock()


class ChatMsg(BaseModel):
    message: str = Field(min_length=1, max_length=50000)


class SessionCreateRequest(BaseModel):
    title: str = Field(default="新对话", max_length=80)


class RenameRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=80)
    auto_summary: bool = False


def _session_path(sid: str) -> str:
    if not isinstance(sid, str) or not _SESSION_ID_RE.fullmatch(sid):
        raise HTTPException(400, "会话 ID 无效")
    root = os.path.realpath(SESSIONS_DIR)
    path = os.path.realpath(os.path.join(root, f"{sid}.json"))
    if os.path.commonpath([root, path]) != root:
        raise HTTPException(400, "会话 ID 无效")
    return path


def _get_session_lock(sid: str) -> asyncio.Lock:
    """获取会话锁；使用 WeakValueDictionary 自动释放不再被引用的锁。"""
    lock = _session_chat_locks.get(sid)
    if lock is not None:
        return lock
    # 同步创建分支在单线程事件循环中通过 setdefault 原子完成
    new_lock = asyncio.Lock()
    existing = _session_chat_locks.setdefault(sid, new_lock)
    return existing


def _load(sid: str) -> dict:
    p = _session_path(sid)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, IOError, OSError) as exc:
            logger.warning("Session file corrupted %s: %s", sid, exc)
            raise HTTPException(500, "会话文件损坏，无法读取") from exc
        if not isinstance(data, dict):
            logger.warning("Session file %s is not an object: %s", sid, type(data).__name__)
            raise HTTPException(500, "会话文件格式无效")
        return data
    raise HTTPException(404, "会话不存在")


def _save(sid: str, data: dict):
    _atomic_write_json(_session_path(sid), data)


STEP_LABELS = {
    "chat": "生成对话回复",
    "solve": "推理解题",
    "search": "检索题库",
    "solve_q": "从题库找题并解答",
    "paper": "准备组卷参数",
    "auto_paper": "自动生成试卷",
    "add_question": "新增题目到题库",
    "edit_question": "修改题库题目",
    "style": "更新排版偏好",
    "profile": "更新用户信息",
    "need": "记录功能需求",
    "list_notes": "列出笔记",
    "get_note": "查看笔记",
    "create_note": "创建笔记",
    "modify_note": "修改笔记",
    "save_image": "保存图片到待处理题目",
    "error_info": "查看题目错误信息",
}


def _build_steps(itype: str, idata: dict, ai_steps: list = None) -> list:
    """构建 Agent 步骤数组。优先使用 AI 返回的层级步骤(parent/child/status/time)，否则按意图类型生成默认步骤。"""
    if ai_steps and isinstance(ai_steps, list):
        valid = []
        for st in ai_steps:
            if isinstance(st, dict):
                # 新格式：parent/child/status/time
                if "parent" in st or "child" in st:
                    valid.append({
                        "parent": str(st.get("parent", "Agent")),
                        "child": str(st.get("child", "执行")),
                        "status": st.get("status", "done") if st.get("status") in ("running", "done", "error") else "done",
                        "time": str(st.get("time", "")).strip(),
                    })
                # 兼容旧格式：level/text/status
                elif "text" in st:
                    valid.append({
                        "parent": "执行步骤",
                        "child": str(st.get("text", "")),
                        "status": st.get("status", "done") if st.get("status") in ("running", "done", "error") else "done",
                        "time": "",
                    })
        if valid:
            return valid
    label = STEP_LABELS.get(itype, "处理请求")
    steps = [
        {"parent": "意图识别", "child": "解析消息", "status": "done", "time": ""},
        {"parent": label, "child": "执行", "status": "done", "time": ""},
        {"parent": "完成", "child": "返回结果", "status": "done", "time": ""},
    ]
    if itype in ("search", "solve_q"):
        steps.insert(2, {"parent": label, "child": "查询数据库", "status": "done", "time": ""})
    if itype in ("solve", "solve_q"):
        steps.insert(2, {"parent": label, "child": "调用推理模型", "status": "done", "time": ""})
    if itype in ("paper", "auto_paper"):
        steps.insert(2, {"parent": label, "child": "匹配组卷参数", "status": "done", "time": ""})
    return steps


def _final_steps(sid: str, fallback: list) -> list:
    """优先返回 AI 执行过程中收集到的真实步骤，没有则回退到计划步骤。"""
    from services.ai_service import agent_steps_get
    actual = agent_steps_get(sid)
    return actual if actual else fallback


def _cleanup_empty_sessions(max_age_seconds: int = 10 * 60) -> int:
    """清理空会话：默认超过 10 分钟且无消息的会话自动删除。"""
    removed = 0
    if not os.path.isdir(SESSIONS_DIR):
        return 0
    now = time.time()
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(SESSIONS_DIR, fn)
        try:
            # 跳过最近修改的文件（可能正在使用）
            mtime = os.path.getmtime(p)
            if now - mtime < max_age_seconds:
                continue
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict):
                continue
            msgs = d.get("messages", [])
            if not msgs:
                os.remove(p)
                removed += 1
                logger.info("Auto-deleted empty session: %s", fn)
        except Exception as e:
            logger.warning("Cleanup empty session failed %s: %s", fn, e)
    return removed


def _list_sessions_sync() -> list[dict]:
    """同步扫描会话文件并提取元信息（供线程池调用，避免阻塞事件循环）。"""
    items = []
    for fn in os.listdir(SESSIONS_DIR):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
                items.append({"id": d.get("id"), "title": d.get("title", "无标题"),
                              "created_at": d.get("created_at", ""),
                              "msg_count": len(d.get("messages", [])) // 2})
            except Exception as e:
                logger.warning("Failed to parse session file %s: %s", fn, e)
    return items


@router.get("")
async def list_sessions():
    await asyncio.to_thread(_cleanup_empty_sessions)
    items = await asyncio.to_thread(_list_sessions_sync)
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"sessions": items}


@router.post("")
async def create_session(data: SessionCreateRequest | None = None):
    await asyncio.to_thread(_cleanup_empty_sessions)
    sid = uuid.uuid4().hex[:16]
    title = (data.title if data else "新对话").strip() or "新对话"
    s = {"id": sid, "title": title, "created_at": time.strftime("%m-%d %H:%M"),
         "messages": []}
    _save(sid, s)
    return s


@router.get("/{sid}")
async def get_session(sid: str):
    return _load(sid)


@router.delete("/{sid}")
async def delete_session(sid: str):
    p = _session_path(sid)
    lock = _get_session_lock(sid)
    async with lock:
        if not os.path.exists(p):
            raise HTTPException(404, "会话不存在")
        os.remove(p)
        # 仅在无人排队等待时移除锁，避免等待者被新锁绕过
        if not lock.locked() and not getattr(lock, "_waiters", None) and _session_chat_locks.get(sid) is lock:
            _session_chat_locks.pop(sid, None)
    return {"message": "已删除"}


@router.patch("/{sid}/title")
async def rename_session(sid: str, req: RenameRequest):
    """手动重命名对话，或由 AI 根据已有消息总结标题。"""
    async with _get_session_lock(sid):
        s = _load(sid)
        title = None
        if req.title is not None:
            title = req.title.strip() or None
        elif req.auto_summary:
            msgs = s.get("messages", [])
            content = ""
            for m in msgs[:6]:
                if isinstance(m, dict) and m.get("content"):
                    role = m.get("role", "user")
                    text = str(m["content"])[:200]
                    content += f"{role}: {text}\n"
            if content.strip():
                try:
                    from services.ai_service import ai_service
                    raw = await ai_service.light_task_chat(
                        [{"role": "user", "content": f"请用不超过8个字概括以下对话主题，直接输出标题，不要引号：\n{content}"}],
                        max_tokens=32,
                    )
                    title = raw.strip().strip('"\'').strip('''\u201c\u201d\u2018\u2019''').strip()[:20]
                except Exception as e:
                    logger.warning("AI summary title failed: %s", e)
            if not title:
                title = "新对话"
        if title:
            s["title"] = title
            _save(sid, s)
        return {"id": sid, "title": s.get("title", "无标题")}


async def _session_chat_impl(sid: str, req: ChatMsg):
    from services.ai_service import ai_service, agent_steps_clear, agent_tool_calls_clear, agent_tool_calls_add, agent_tool_calls_get, _make_step_callback
    agent_steps_clear(sid)
    agent_tool_calls_clear(sid)
    step_callback = _make_step_callback(sid)
    s = _load(sid)
    p = load_profile()
    style = p.get("style_notes", "") or p.get("notation_preferences", "")

    # Phase 1: classify intent with cheap model
    # 轻量意图分类走统一入口：优先小米 MiMo V2.5（免费），兜底 DeepSeek flash（关思考）
    try:
        _intent_messages = [
            {"role": "system", "content": (
                "分析意图返回JSON: {\"type\":\"类型\",\"data\":{},\"steps\":[{\"parent\":\"父步骤\",\"child\":\"子步骤\",\"status\":\"running\",\"time\":\"\"}]}。\\n"
                "类型: chat(闲聊)/solve(解题)/search(搜索题库)/"
                "solve_q(从题库找题并解题)/paper(组卷)/auto_paper(直接出卷)/"
                "add_question(新增题目到题库)/edit_question(修改题目)/"
                "style(改偏好)/profile(存个人信息)/need(要没有的功能，仅当以上类型都不匹配时才用)/"
                "list_notes(查看笔记列表)/get_note(查看单个笔记)/create_note(创建笔记)/modify_note(修改笔记)/"
                "save_image(保存图片到待处理题目)/"
                "error_info(查看题目错误信息)\\n"
                "search时data含keyword/subject/grade。solve_q时data含问题描述/keywords。"
                "add_question时data含subject/grade/content(题目内容)/answer(答案)。"
                "edit_question时data含question_id(唯一题目ID)/keyword(展示用)/field(修改字段)/value(新值)。"
                "auto_paper时data含subject/grade/type/topic。"
                "list_notes时data含subject(可选)/tag(可选)。get_note时data含keyword(找笔记关键词)或note_id。"
                "create_note时data含title/subject/content。modify_note时data含keyword(找笔记)/field/value。"
                "save_image时data含base64_image(图片base64)/subject(学科)/grade(年级)。"
                "error_info时data含question_id(可选,无则列出所有标记题目)。"
                "steps为可选字段，描述Agent计划执行的层级步骤；parent为父步骤名，child为子步骤名，status为running/done/error，time可留空由后端填充。"
            )},
            {"role": "user", "content": req.message}
        ]
        raw = await ai_service.light_task_chat(_intent_messages, max_tokens=512)
        intent = ai_service._extract_json(raw)
    except Exception as e:
        logger.warning("Session intent classification failed: %s", e)
        intent = {"type": "chat"}

    if not isinstance(intent, dict):
        logger.warning("Session intent classification returned non-object: %r, falling back to chat", intent)
        intent = {"type": "chat"}
    itype = intent.get("type", "chat")
    if not isinstance(itype, str):
        logger.warning("Session intent type is not a string: %r, falling back to chat", itype)
        itype = "chat"
    raw_idata = intent.get("data") if isinstance(intent, dict) else None
    idata = raw_idata if isinstance(raw_idata, dict) else {}
    ai_steps = intent.get("steps")
    steps = _build_steps(itype, idata, ai_steps)
    # 完整历史始终持久化；[-20:] 仅作为发送给模型的上下文窗口，
    # 避免把截断后的列表写回磁盘造成历史永久丢失。
    messages = list(s.get("messages", []))
    messages.append({"role": "user", "content": req.message})

    confirmation_phrases = {
        "add_question": "确认新增题目", "edit_question": "确认修改题目",
        "create_note": "确认创建笔记", "modify_note": "确认修改笔记",
        "save_image": "确认保存图片", "solve_q": "确认写入解答",
        "auto_paper": "确认自动组卷", "style": "确认修改偏好",
        "profile": "确认保存资料",
    }
    required_phrase = confirmation_phrases.get(itype)
    if required_phrase and required_phrase not in req.message:
        reply = f"已整理本次{STEP_LABELS.get(itype, '写入')}请求。为避免意图误判直接修改数据，请核对后发送“{required_phrase}”并附上完整内容。"
        s["messages"] = messages + [{"role": "assistant", "content": reply}]
        _save(sid, s)
        return {
            "reply": reply, "steps": _final_steps(sid, steps),
            "action": {"type": "pending_confirmation", "operation": itype, "data": idata},
        }

    if itype == "paper":
        from services.config_service import config_service
        cid = await config_service.save_paper_config(idata) if idata else ""
        jump = f"/papers/generate?saved_config={cid}"
        subj = idata.get("subject",""); grade=idata.get("grade","")
        reply = f"好的！组卷参数已保存（#{cid[:6]}）。" + (f"{subj} {grade}。" if subj else "") + "点击下方卡片跳转组卷页面。"
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "action": {"type": "jump_paper", "data": idata, "saved_config": cid}}

    if itype == "style":
        new_style = idata.get("style_notes", req.message)
        profile = load_profile()
        profile["style_notes"] = new_style
        save_profile(profile)
        reply = f"收到！排版偏好已更新：{new_style[:100]}。之后的题目都会按这个风格展现。"
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "action": {"type": "set_style", "data": {"style_notes": new_style}}}

    if itype == "profile":
        profile = load_profile()
        updated = []
        for k, v in idata.items():
            if k in profile and v:
                profile[k] = v
                updated.append(k)
        if updated:
            save_profile(profile)
            reply = f"记住了！你的{', '.join(updated)}等信息已保存，后续对话会基于这些信息为你定制。"
        else:
            reply = "收到你的信息！请告诉我更多细节，比如学校、年级、学习目标等。"
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "action": {"type": "set_style", "data": {"updated": updated}}}

    # ====== 新增题目到题库 ======
    if itype == "add_question":
        from models.database import async_session as _db_s
        from models.models import Question as _Q
        content = idata.get("content", req.message)
        answer = idata.get("answer", "")
        subject = idata.get("subject", "")
        grade = idata.get("grade", "")
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
        new_id = uuid.uuid4().hex[:12]
        folder = os.path.join(STORAGE_DIR, "questions", new_id)
        try:
            # 先建目录再写库：makedirs 失败（磁盘满/权限）时不能留下无目录的幽灵题目
            os.makedirs(folder, exist_ok=False)
        except OSError as exc:
            log_error("sessions.add_question", f"create question folder failed for {new_id}: {exc}")
            raise HTTPException(500, detail="题目目录创建失败，未写入题库")
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
        agent_tool_calls_add(sid, "新增题目", {"subject": subject, "grade": grade, "content": content[:100]}, f"题目已保存 #{new_id[:8]}", "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "action": {"type": "jump_question", "data": {"id": new_id}}, "tool_calls": agent_tool_calls_get(sid)}

    # ====== 修改题库题目 ======
    if itype == "edit_question":
        from models.database import async_session as _db_s
        from models.models import Question as _Q
        from datetime import datetime as _dt, timezone as _tz
        from html import escape as _html_escape
        question_id = str(idata.get("question_id", "") or "").strip()
        field = idata.get("field", "")
        value = str(idata.get("value", "") or "").strip()
        if not question_id:
            reply = "确认修改仍缺少完整题目 ID，未执行任何写入。"
        else:
            async with _db_s() as _db:
                q = await _db.get(_Q, question_id)
                if not q:
                    reply = f"未找到题目 #{question_id[:12]}。"
                elif q.is_resolved:
                    reply = "该题已锁定，请先在题目详情取消锁定。"
                else:
                    if field == "answer" or field == "答案":
                        q.standard_answer = value
                        q.answer_html = f"<p>{_html_escape(value)}</p>"
                    elif field == "tags" or field == "标签":
                        q.knowledge_tags = [t.strip() for t in value.split(",") if t.strip()][:50]
                    elif field == "subject" or field == "学科":
                        q.subject = value[:64]
                    elif field == "grade" or field == "年级":
                        q.grade = value[:64]
                    elif field == "content" or field == "内容":
                        q.ocr_text = value
                        q.question_html = f"<p>{_html_escape(value)}</p>"
                    else:
                        reply = f"不支持的修改字段: {field}。支持的字段: answer/tags/subject/grade/content"
                        s["messages"] = messages
                        s["messages"].append({"role": "assistant", "content": reply})
                        _save(sid, s)
                        return {"reply": reply, "steps": _final_steps(sid, steps)}
                    q.updated_at = _dt.now(_tz.utc).replace(tzinfo=None)
                    await _db.commit()
                    prev = q.ocr_text[:30] if q.ocr_text else ""
                    reply = f"题目「{prev}...」的 **{field}** 已更新。"
                    agent_tool_calls_add(sid, "修改题目", {"question_id": question_id, "field": field, "value": value[:100]}, f"已更新 {field}", "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "action": {"type": "set_style", "data": {"updated": [field]}}, "tool_calls": agent_tool_calls_get(sid)}

    # ====== 试卷管理（round 60：Agent 补齐试卷库读写意图，沿用确认闸） ======
    if itype == "edit_paper":
        from models.database import async_session as _db_s
        from models.models import Paper as _P
        paper_id = str(idata.get("paper_id", "")).strip()
        field = str(idata.get("field", "")).strip().lower()
        value = str(idata.get("value", "")).strip()
        allowed = {"title": "标题", "subject": "学科", "grade": "年级"}
        if field not in allowed:
            reply = f"不支持的试卷修改字段: {field or '(空)'}。支持的字段: title/subject/grade"
            s["messages"] = messages
            s["messages"].append({"role": "assistant", "content": reply})
            _save(sid, s)
            return {"reply": reply, "steps": _final_steps(sid, steps)}
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
                agent_tool_calls_add(sid, "修改试卷", {"paper_id": paper_id, "field": field, "value": value[:100]}, "已更新", "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    if itype == "delete_paper":
        from models.database import async_session as _db_s
        from models.models import Paper as _P
        paper_id = str(idata.get("paper_id", "")).strip()
        confirm = bool(idata.get("confirm", False))
        if not confirm:
            # 与 add_question 同款确认闸：不确认不执行
            s["messages"] = messages
            reply = f"即将删除试卷 {paper_id}，其中的题目不会被删除（会回到题库）。请回复「确认删除试卷 {paper_id}」以执行。"
            s["messages"].append({"role": "assistant", "content": reply})
            _save(sid, s)
            return {"reply": reply, "steps": _final_steps(sid, steps),
                    "action": {"type": "pending_confirmation",
                               "operation": "delete_paper", "data": {"paper_id": paper_id}}}
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
                agent_tool_calls_add(sid, "删除试卷", {"paper_id": paper_id, "title": title[:60]}, "已删除", "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    # ====== 直接生成试卷 ======
    if itype == "auto_paper":
        subject = idata.get("subject", "")
        grade = idata.get("grade", "")
        ptype = idata.get("type", "custom")
        topic = idata.get("topic", "")
        from services.config_service import config_service
        from models.database import async_session as _db_s
        from models.models import Question as _Q
        from sqlalchemy import select as _sel
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
            reply = f"题库暂无可用的题目。请先用OCR导入题目或让我出新题，再生成试卷。"
            action = {"type": "jump_paper", "data": cfg, "saved_config": cid}
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "action": action}

    # ====== 需求记录 ======
    if itype == "need":
        from services.diagram_service import diagram_service
        need_desc = idata.get("need", req.message[:60])
        try:
            summary_raw = await ai_service.light_task_chat(
                [{"role": "user", "content": f"用户在对话中说：{req.message}\\n请用20字以内抽象总结用户真正需要的功能或能力，只输出总结。"}],
                max_tokens=64,
            )
            need_desc = summary_raw.strip().strip('"').strip("'")[:80]
        except Exception as exc:
            logger.warning("Need summary AI call failed for session %s: %s", sid, exc)
        diagram_service._note_agent_need(f"会话{sid}: {need_desc}")
        reply = f"对不起，我暂时还没有「{need_desc}」这个功能。不过我已经记下来了，开发组会尽快处理。请先试试其他功能吧。"
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "action": {"type": "tool_need", "data": {"need": need_desc}}}

    # ====== 题库搜索 ======
    if itype == "search":
        from models.database import async_session as _db_session
        from models.models import Question as _Q
        from sqlalchemy import select as _select
        keyword = idata.get("keyword", "")
        subject = idata.get("subject", "")
        grade = idata.get("grade", "")
        try:
            limit = max(1, min(int(idata.get("limit", 5)), 20))
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
                s["_last_search"] = [q.id for q in found]
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    # ====== 从题库解题并写回 ======
    if itype == "solve_q":
        from models.database import async_session as _db_session
        from models.models import Question as _Q
        from sqlalchemy import select as _select
        keyword = idata.get("keyword", "") or idata.get("subject", "")
        subject = idata.get("subject", "")
        grade = idata.get("grade", "")
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
                if step_callback:
                    step_callback("题库解题Agent", f"解答 {fq.id[:8]}", "running")
                # 逐题容错：单题 AI 失败不拖垮整个会话请求（前面题目已各自提交写库）
                try:
                    ans = await _ai.deepseek_chat_question(info, req.message, _st, step_callback=step_callback)
                except Exception as solve_exc:
                    log_error("sessions.solve_q", f"AI solve failed for {fq.id}: {solve_exc}")
                    if step_callback:
                        step_callback("题库解题Agent", f"解答 {fq.id[:8]}", "error")
                    replies.append(f"**题目 {fq.id[:8]}** AI 解题失败，已跳过（其余题目继续）。")
                    continue
                if not (ans or "").strip():
                    replies.append(f"**题目 {fq.id[:8]}** 解答模型返回空内容，未写入。")
                    continue
                if step_callback:
                    step_callback("题库解题Agent", f"解答 {fq.id[:8]}", "done")
                agent_tool_calls_add(sid, "解题", {"question_id": fq.id, "subject": fq.subject}, f"已解答并写入", "done")
                # Generate diagrams in answer
                if '[[DIAGRAM:' in ans:
                    for m in __import__('re').finditer(r'\[\[DIAGRAM:([^\]]+)\]\]', ans):
                        desc = m.group(1)
                        try:
                            path = await _ds.generate_diagram(fq.id, desc, len(fq.diagrams or []))
                            if path:
                                disk_p = os.path.join(STORAGE_DIR, path.lstrip("/"))
                                w, _ = _ds._get_svg_size(disk_p)
                                sz = f' width="{w}"' if w else ""
                                svg_tag = f'<div class="diagram"><img src="{path}" style="max-width:80%;height:auto"{sz}></div>'
                                ans = ans.replace(m.group(0), svg_tag, 1)
                                diags = list(fq.diagrams or [])
                                diags.append({"path": path, "place": "answer"})
                                fq.diagrams = diags
                        except Exception as exc:
                            logger.warning("Session answer diagram failed for %s: %s", fq.id, exc)
                            ans = ans.replace(m.group(0), '', 1)
                # Save the answer back to the question
                async with _db_session() as _db2:
                    current = await _db2.get(_Q, fq.id)
                    if not current or current.is_resolved or current.status != "done":
                        replies.append(f"**题目 {fq.id[:8]}** 状态已变化，本次解答未写入。")
                        continue
                    # 去重：同一会话对同一题只追加一次，整条指令重试不会堆积重复答案
                    if f"<!-- AGENT_SESSION_{sid} -->" in (current.answer_html or ""):
                        replies.append(f"**题目 {fq.id[:8]}** 本会话已写入过解答，跳过重复写入。")
                        continue
                    current.answer_html = (current.answer_html or "") + f"\n<!-- AGENT_SESSION_{sid} -->\n" + ans
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
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    # ====== 保存图片到待处理题目 ======
    if itype == "save_image":
        b64_img = idata.get("base64_image", "").split(",")[-1] if "," in (idata.get("base64_image", "") or "") else (idata.get("base64_image", "") or "")
        if not b64_img:
            reply = "请提供图片数据（base64格式）。"
        else:
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
                s["messages"] = messages
                s["messages"].append({"role": "assistant", "content": reply})
                _save(sid, s)
                return {"reply": reply, "steps": _final_steps(sid, steps)}
            # Create Question entry
            from models.database import async_session as _db_s3
            from models.models import Question as _Q
            subj = idata.get("subject", "")
            grd = idata.get("grade", "")
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
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps)}

    # ====== 查看笔记列表 ======
    if itype == "list_notes":
        from models.database import async_session as _db_s
        from models.models import Note as _N
        from sqlalchemy import select as _sel
        subject_name = idata.get("subject", "")
        tag_name = idata.get("tag", "")
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
            reply = f"笔记列表为空。" + (f"（学科: {subject_name}）" if subject_name else "")
        else:
            lines = [f"找到 {len(notes)} 篇笔记："]
            for i, n in enumerate(notes, 1):
                tags = ", ".join(n.knowledge_tags or [])
                prev = (n.content or "")[:50].replace("\n", " ")
                lines.append(f"{i}. [{n.subject or '未分类'}] **{n.title}** - {prev}...（标签: {tags}）")
            reply = "\n".join(lines)
            # Store last note search results
            s["_last_note_search"] = [n.id for n in notes]
        agent_tool_calls_add(sid, "搜索笔记", {"subject": subject_name, "tag": tag_name}, f"找到 {len(notes)} 篇笔记", "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    # ====== 查看单个笔记 ======
    if itype == "get_note":
        from models.database import async_session as _db_s
        from models.models import Note as _N
        from sqlalchemy import select as _sel
        keyword = idata.get("keyword", "")
        note_id = idata.get("note_id", "")
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
                last = s.get("_last_note_search", [])
                if last and not keyword:
                    n = await _db.get(_N, last[0])
            if not n:
                reply = "未找到该笔记。试试搜索笔记列表？"
            else:
                tags = ", ".join(n.knowledge_tags or [])
                reply = f"**{n.title}** [{n.subject or '未分类'}][{n.grade or ''}]\n标签: {tags}\n\n{n.content or '(无内容)'}"
                agent_tool_calls_add(sid, "查看笔记", {"note_id": n.id, "title": n.title}, f"已查看笔记", "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    # ====== 创建笔记 ======
    if itype == "create_note":
        from models.database import async_session as _db_s
        from models.models import Note as _N
        title = str(idata.get("title") or "AI创建的笔记")[:200]
        subject = str(idata.get("subject") or "")[:64]
        raw_content = idata.get("content", req.message)
        content = raw_content if isinstance(raw_content, str) else (
            req.message if not isinstance(raw_content, (int, float, bool)) else str(raw_content)
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
                    [{"role":"user","content":struct_prompt}], max_tokens=4096,
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
        agent_tool_calls_add(sid, "创建笔记", {"title": title, "subject": subject}, f"笔记已创建 #{new_id[:8]}", "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    # ====== 修改笔记 ======
    if itype == "modify_note":
        from models.database import async_session as _db_s
        from models.models import Note as _N
        from sqlalchemy import select as _sel
        from datetime import datetime as _dt, timezone as _tz
        keyword = str(idata.get("keyword", "") or "")[:200]
        field = idata.get("field", "")
        value = str(idata.get("value", "") or "").strip()
        if not keyword:
            reply = "请告诉我你要修改哪篇笔记？可以提供关键词或标题。"
        else:
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
                        s["messages"] = messages
                        s["messages"].append({"role": "assistant", "content": reply})
                        _save(sid, s)
                        return {"reply": reply, "steps": _final_steps(sid, steps)}
                    n.updated_at = _dt.now(_tz.utc).replace(tzinfo=None)
                    await _db.commit()
                    reply = f"笔记「{n.title}」的 **{field}** 已更新。"
                    agent_tool_calls_add(sid, "修改笔记", {"note_id": n.id, "field": field, "value": str(value)[:100]}, f"已更新 {field}", "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    # ====== 查看题目错误信息 ======
    if itype == "error_info":
        from models.database import async_session as _db_s4
        from models.models import Question as _Q2
        from sqlalchemy import select as _sel2
        qid = idata.get("question_id", "")
        async with _db_s4() as _db:
            if qid:
                q = await _db.get(_Q2, qid)
                if not q:
                    reply = f"未找到题目 #{qid[:8]}"
                else:
                    flags = q.audit_flags if hasattr(q, 'audit_flags') and q.audit_flags else []
                    err_msg = q.error_message or "无错误记录"
                    flag_str = ""
                    if isinstance(flags, list) and flags:
                        flag_items = [f"{f.get('type','')}: {f.get('reason','')}" for f in flags]
                        flag_str = "\n审计标记: " + "; ".join(flag_items)
                    prev = (q.ocr_text or q.question_html or "")[:80]
                    reply = f"题目 #{qid[:8]} [{q.subject}][{q.grade}]\n{prev}...\n状态: {q.status}\n错误: {err_msg}{flag_str}"
            else:
                r = await _db.execute(_sel2(_Q2).where(_Q2.status == "error").order_by(_Q2.created_at.desc()).limit(10))
                errors = r.scalars().all()
                if not errors:
                    # Also check for flagged-but-not-error questions
                    r2 = await _db.execute(
                        _sel2(_Q2).where(_Q2.status == "done").order_by(_Q2.created_at.desc()).limit(50)
                    )
                    flagged = [q for q in r2.scalars().all()
                               if hasattr(q, 'audit_flags') and q.audit_flags and len(q.audit_flags) > 0]
                    if not flagged and not errors:
                        reply = "当前没有标记题目或错误题目。"
                    else:
                        all_items = list(errors) + flagged
                        lines = [f"共 {len(all_items)} 题存在问题："]
                        for i, eq in enumerate(all_items[:15], 1):
                            prev = (eq.ocr_text or eq.question_html or "")[:50].replace("\n", " ")
                            eq_flags = eq.audit_flags if hasattr(eq, 'audit_flags') and eq.audit_flags else []
                            flag_summary = ""
                            if isinstance(eq_flags, list) and eq_flags:
                                flag_summary = " [标记:" + ",".join(f.get('type','?') for f in eq_flags) + "]"
                            lines.append(f"{i}. #{eq.id[:8]} [{eq.subject}][{eq.grade}] {prev}... - {eq.error_message or eq.status}{flag_summary}")
                        reply = "\n".join(lines)
                else:
                    lines = [f"共 {len(errors)} 道错误题目："]
                    for i, eq in enumerate(errors, 1):
                        prev = (eq.ocr_text or eq.question_html or "")[:50].replace("\n", " ")
                        lines.append(f"{i}. #{eq.id[:8]} [{eq.subject}][{eq.grade}] {prev}... - {eq.error_message or '未知错误'}")
                    reply = "\n".join(lines)
        agent_tool_calls_add(sid, "查看错误信息", {"question_id": qid or "all"}, reply[:100], "done")
        s["messages"] = messages
        s["messages"].append({"role": "assistant", "content": reply})
        _save(sid, s)
        return {"reply": reply, "steps": _final_steps(sid, steps), "tool_calls": agent_tool_calls_get(sid)}

    # chat/solve: use context-aware full model
    system = (
        "你是学习搭子AI助手。你有多轮对话上下文，能记住之前说过的内容。\n"
        "你的能力："
        "1. 搜索题库(search) 2. 从题库找题解题并写回(solve_q) "
        "3. 修改/重写题目答案 4. 出变式题 5. 解释概念 "
        "6. 画几何图（遇到几何/函数/装置题时插入 [[DIAGRAM:详细中文描述图形]] 自动生成示意图，描述要具体：例如 [[DIAGRAM:直角三角形ABC ∠C=90° AC=3 BC=4 标注顶点]]）"
        "7. 评估答案正确性 8. 推荐组卷参数"
        "9. 查看/搜索/创建/修改笔记（list_notes/get_note/create_note/modify_note）"
        "10. 查看题目错误和审计标记（error_info）\n"
        "【输出格式】使用Markdown：## 小标题 / **粗体** / 1.2.3.有序列表 / $...$数学公式。"
        "禁止emoji、禁止彩色文字。"
    )
    if style: system += f"\n排版偏好：{style}"

    if itype == "solve":
        system += "\n这是解题任务，请详细推理，用LaTeX写公式。"

    full_messages = [{"role": "system", "content": system}] + messages[-20:]

    # 闲聊与求解统一走配置的 deepseek_model（deepseek_chat 内部读取设置）；
    # 旧 deepseek-chat / deepseek-v4-pro 分流已失效，deepseek-chat 已被官方下线
    step_parent = "解题Agent" if itype == "solve" else "对话Agent"
    if step_callback:
        step_callback(step_parent, "生成回复", "running")
    reply = await ai_service.deepseek_chat(
        full_messages, max_tokens=16384, scope="chat", step_callback=step_callback
    )
    if step_callback:
        step_callback(step_parent, "生成回复", "done")

    # Generate diagrams for [[DIAGRAM:...]] markers in reply
    if '[[DIAGRAM:' in reply:
        from services.diagram_service import diagram_service as _ds
        import re as _re
        diag_idx = 0
        base_name = f"chat_{sid}_{int(time.time())}"
        for m in _re.finditer(r'\[\[DIAGRAM:([^\]]+)\]\]', reply):
            desc = m.group(1)
            try:
                path = await _ds.generate_diagram(base_name, desc, diag_idx)
                if path:
                    disk_p = os.path.join(STORAGE_DIR, path.lstrip("/"))
                    w, _ = _ds._get_svg_size(disk_p)
                    sz = f' width="{w}"' if w else ""
                    svg_tag = f'<div class="diagram"><img src="{path}" style="max-width:80%;height:auto"{sz} onerror="this.style.display=\'none\'"></div>'
                else:
                    svg_tag = ''
                reply = reply.replace(m.group(0), svg_tag, 1)
                diag_idx += 1
            except Exception as e:
                log_error("diagram_gen", f"session={sid} desc={desc[:60]} error={e}")
                reply = reply.replace(m.group(0), '', 1)

    s["messages"] = messages
    s["messages"].append({"role": "assistant", "content": reply})

    # auto-title on first exchange
    if len(s["messages"]) == 2:
        try:
            title_raw = await ai_service.light_task_chat(
                [{"role": "user", "content": f"给这段对话起个6字内标题，直接回复标题本身，不要引号不要额外文字：{req.message}"}],
                max_tokens=32,
            )
            title = title_raw.strip().strip('"\'').strip('''\u201c\u201d\u2018\u2019''').strip()[:20]
            if title:
                s["title"] = title
        except Exception as e:
            logger.debug("Auto-title failed for session %s: %s", sid, e)

    _save(sid, s)
    return {"reply": reply, "steps": _final_steps(sid, steps), "session": s, "tool_calls": agent_tool_calls_get(sid)}


@router.post("/{sid}/chat")
async def session_chat(sid: str, req: ChatMsg):
    _session_path(sid)
    lock = _get_session_lock(sid)
    async with lock:
        try:
            return await _session_chat_impl(sid, req)
        except HTTPException:
            raise
        except Exception as exc:
            log_error("sessions.chat", f"AI chat failed for {sid}: {exc}")
            _msg = str(exc).lower()
            if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
                raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
            raise HTTPException(status_code=502, detail="AI 对话失败，请稍后重试")


@router.get("/{sid}/steps")
async def get_session_steps(sid: str):
    """轮询接口：返回 Agent 执行过程中的实时步骤。"""
    from services.ai_service import agent_steps_get
    _load(sid)  # 确保会话存在
    return {"steps": agent_steps_get(sid)}

