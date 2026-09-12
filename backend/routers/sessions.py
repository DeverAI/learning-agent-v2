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
# 后台任务「召回」的确定性契约：前端 agent.html 拼消息、本模块解析，
# 两边必须同格式 —— 提成常量是为了让 test_r23 能直接对同一个来源断言（而不是各写一份）。
_RECALL_PREFIX = "[系统召回]"
_RECALL_TASK_ID_RE = re.compile(r"task_id[=:\s]*([a-zA-Z0-9_-]{6,64})")
# P4 打断判定：学生在后台任务运行中发话时的确定性关键词（不走分类器，防误路由）。
# cancel 必须确定命中 —— 「取消」误路由成 chat 会让任务继续烧额度；keep/observe 只影响文案。
_INTERRUPT_CANCEL_RE = re.compile(
    r"(取消|停下|停一下|别做了|别继续|先停|不用做了|不用继续|abort|cancel|stop\s*it)",
    re.IGNORECASE,
)
_INTERRUPT_KEEP_RE = re.compile(
    r"(继续|接着(做|说|讲)|别停|keep\s*going|continue)",
    re.IGNORECASE,
)
# 锁已拆到 services/session_locks.py（fg 前台串行 / file 短临界区 / bg 后台任务）。
# 原先这里是**整会话一把锁**，`session_chat` 持锁直到 AI 回答结束，
# 把改名、删除、以及将来的后台任务更新全部堵住 -> 用户说的"卡死"。
from services.session_locks import (  # noqa: E402
    bg_lock, drop_fg_lock_if_idle, fg_busy, fg_lock, file_lock,
)


class SessionDeletedError(Exception):
    """对话回合进行中，会话文件被删除。

    用于阻止"回合结束回写把已删除的会话复活"。
    """


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
    """兼容旧名：现在返回的是**前台回合锁**（保留此名避免外部引用断裂）。"""
    return fg_lock(sid)


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


def _save(sid: str, data: dict, guard_deleted: bool = False):
    """原子写会话文件。

    `guard_deleted=True`（对话回合回写时使用）：若回合进行期间会话文件已被删除，
    则**拒绝写回**并抛 `SessionDeletedError`。

    没有这个守卫会出现什么：用户删掉一个正在回答中的会话 -> 回合结束回写 ->
    会话文件被重新创建 -> **用户以为删了，刷新一看还在**。
    这是"静默失败"的反面：不是丢数据，是"删了又回来"，更让人困惑。
    """
    if guard_deleted and not os.path.exists(_session_path(sid)):
        raise SessionDeletedError(sid)
    _atomic_write_json(_session_path(sid), data)


def _registry():
    """惰性取得工具注册表（导入即注册，重复调用幂等）。

    惰性而非模块级导入：`services.agent_tools` 会拉起 ai_service / OCR 等重模块，
    放在函数里可以让本路由模块的导入保持轻量，也避免与 routers.ocr 的循环导入。
    """
    from services import agent_tools
    agent_tools.register_all()
    from services.agent_core import REGISTRY
    return REGISTRY


def _step_labels() -> dict:
    _registry()
    from services.agent_core import step_labels
    return step_labels()


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
    label = _step_labels().get(itype, "处理请求")
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
    """保留为薄封装：实现已搬到 `services.agent_core.final_steps`。

    不直接删名，是因为本模块外仍有引用点；删名会把"实现搬家"变成"接口删除"。
    """
    from services.agent_core import final_steps
    return final_steps(sid, fallback)


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
    """删除会话。

    锁拆分（R12）后**不再等待**正在进行的对话回合 —— 等待就是用户说的卡死。
    改为：有前台回合在跑就**立刻**返回 409 说明原因。
    并发竞态由 `_save(guard_deleted=True)` 兜底，不会把会话复活。
    """
    p = _session_path(sid)
    if fg_busy(sid):
        raise HTTPException(409, "该对话正在回答中，请等本轮结束后再删除")
    async with file_lock(sid):
        if not os.path.exists(p):
            raise HTTPException(404, "会话不存在")
        os.remove(p)
    drop_fg_lock_if_idle(sid)
    return {"message": "已删除"}


@router.patch("/{sid}/title")
async def rename_session(sid: str, req: RenameRequest):
    """手动重命名对话，或由 AI 根据已有消息总结标题。

    同 `delete_session`：有前台回合在跑就立刻 409。
    不并行改的原因：对话回合结束时会整体回写会话 JSON，期间的标题改动会被**静默回滚**
    （丢失更新）。与其悄悄丢掉用户的改名，不如明确告诉他稍后再改。
    """
    if fg_busy(sid):
        raise HTTPException(409, "该对话正在回答中，请等本轮结束后再改名")
    async with file_lock(sid):
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


async def _try_interrupt_running_tasks(sid: str, message: str) -> dict | None:
    """P4 打断判定：本会话有运行中后台任务时，确定性处理 cancel/keep/observe。

    返回 None = 无打断条件（没有运行中任务，或消息不是打断语），继续走分类器。
    返回 dict = 已处理打断，该 dict 直接作为 intent（type=chat + 已写好的 reply 语义
    由后续 chat 处理器体现；这里用 task_interrupt 专用 type 走轻量回复）。

    三态：
    - **cancel**：命中取消词 → 对本会话全部 running/pending 任务设 cancel_requested，
      并如实告知「协作式取消，最多再跑一个检查点」。
    - **keep**：命中继续词 → 只提示任务仍在跑，不打扰。
    - **observe**：有运行中任务但既非取消也非继续 → 在分类前把任务列表写进 data，
      让对话 Agent 知道背景里有事在跑（不替用户做决定）。
    """
    try:
        from services import background_agent as BA
        running = await BA.list_tasks(sid=sid, status="running")
        pending = await BA.list_tasks(sid=sid, status="pending")
        active = list((running or {}).get("tasks") or []) + list((pending or {}).get("tasks") or [])
    except Exception as exc:
        logger.warning("interrupt: list tasks failed: %s", exc)
        return None
    if not active:
        return None

    if _INTERRUPT_CANCEL_RE.search(message):
        cancelled = []
        for t in active:
            tid = str(t.get("task_id") or "")
            if not tid:
                continue
            try:
                await BA.request_cancel(tid)
                cancelled.append(t.get("title") or tid)
            except Exception as exc:
                logger.warning("interrupt: cancel %s failed: %s", tid, exc)
        n = len(cancelled)
        reply = (
            f"已请求取消本会话的 {n} 个后台任务（{'、'.join(cancelled[:3])}"
            f"{'…' if n > 3 else ''}）。"
            "取消是**协作式**的：任务会在下一个检查点停下，最多再等约 25 秒兜底；"
            "已完成的分片会保留，需要时可以说「接着做」续跑。"
        )
        return {
            "type": "task_interrupt",
            "data": {"decision": "cancel", "cancelled": cancelled},
            "steps": [{"parent": "打断处理", "child": f"取消 {n} 个后台任务",
                       "status": "running", "time": ""}],
            "_interrupt_reply": reply,
        }

    # keep / observe：**不拦截**。
    # R27 修正：原先 keep 命中「继续」就短路回一句「我不打断后台任务」，
    # 于是学生在备课时问「继续讲二次函数」会得到这句废话，**真问题被吞掉**。
    # keep 的语义是「任务照跑」，不是「不要回答我」——因此一律回落分类器。
    # 只有 cancel（明确要停任务）才走确定性旁路。
    return None


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
    #
    # 分类器的 system prompt **由工具注册表生成**（agent_core.build_intent_prompt）。
    # 原先这里是手写的一整串中文，与下面的分支失同步过 —— 见 agent_tools 模块头的
    # edit_paper / delete_paper 说明。
    _registry()  # 确保工具表已装载（幂等）
    from services.agent_core import (
        Ctx, build_intent_prompt, get as _get_tool, normalize_type, run_with_timeout,
    )
    # [系统召回] 旁路（R23）：后台任务完成后前端自动发出的消息走确定性路由，
    # **不经过意图分类器**。原因：①省一次分类调用；②召回消息含 task_id 与
    # 「继续讲解」等措辞，过分类器存在被误路由到 lecture/solve 的风险；
    # ③召回必须**必然**落到 task_recall，否则「召回继续工作」会静默失效。
    if req.message.startswith(_RECALL_PREFIX):
        m = _RECALL_TASK_ID_RE.search(req.message)
        itype = "task_recall" if m else "chat"
        idata = {"task_id": m.group(1)} if m else {}
        intent = {
            "type": itype, "data": idata,
            "steps": [{"parent": "任务召回", "child": "读取任务产出",
                       "status": "running", "time": ""}],
        }
    else:
        # P4 打断判定（cancel / keep / observe）——只在「本会话确有运行中后台任务」时启用。
        # 为什么是确定性而不是交给分类器：「取消」若被误判成 chat，任务会继续烧额度；
        # 这类短指令没有歧义空间，词表比模型更可靠。
        interrupt = await _try_interrupt_running_tasks(sid, req.message)
        if interrupt is not None:
            intent = interrupt
        else:
            try:
                _intent_messages = [
                    {"role": "system", "content": build_intent_prompt()},
                    {"role": "user", "content": req.message},
                ]
                raw = await ai_service.light_task_chat(_intent_messages, max_tokens=512)
                intent = ai_service._extract_json(raw)
            except Exception as e:
                logger.warning("Session intent classification failed: %s", e)
                intent = {"type": "chat"}

    if not isinstance(intent, dict):
        logger.warning("Session intent classification returned non-object: %r, falling back to chat", intent)
        intent = {"type": "chat"}

    # P4 打断已处理完：直接回写会话，不再走分类器/工具表。
    # task_interrupt 不是注册表工具（它是**路由层**行为，不是 Agent 能力）。
    if intent.get("type") == "task_interrupt":
        reply = str(intent.get("_interrupt_reply") or "已处理后台任务打断。")
        steps = _build_steps("chat", intent.get("data") or {}, intent.get("steps"))
        messages = list(s.get("messages", []))
        messages.append({"role": "user", "content": req.message})
        messages.append({"role": "assistant", "content": reply})
        s["messages"] = messages
        _save(sid, s, guard_deleted=True)
        return {
            "reply": reply,
            "steps": _final_steps(sid, steps),
            "action": {"type": "task_interrupt", "data": intent.get("data") or {}},
        }

    raw_type = intent.get("type", "chat")
    itype = normalize_type(raw_type)
    if not itype:
        # 认不出（含非字符串）才兜底到 chat。注意与旧行为的差别：旧代码对**任何**
        # 非空字符串都照单全收，然后落到 chat/solve 兜底分支上；
        # 现在未知 type 会显式记一条 warning，而不是静默当成对话。
        logger.warning("Session intent type unrecognized: %r, falling back to chat", raw_type)
        itype = "chat"
    tool = _get_tool(itype)
    raw_idata = intent.get("data")
    idata = raw_idata if isinstance(raw_idata, dict) else {}
    ai_steps = intent.get("steps")
    steps = _build_steps(itype, idata, ai_steps)
    # 完整历史始终持久化；[-20:] 仅作为发送给模型的上下文窗口，
    # 避免把截断后的列表写回磁盘造成历史永久丢失。
    messages = list(s.get("messages", []))
    messages.append({"role": "user", "content": req.message})

    # 写操作确认闸：确认语声明在工具表的 requires_confirm 上（原先手写在本文件）
    required_phrase = tool.requires_confirm
    if required_phrase and required_phrase not in req.message:
        reply = (f"已整理本次{_step_labels().get(itype, '写入')}请求。"
                 f"为避免意图误判直接修改数据，请核对后发送“{required_phrase}”并附上完整内容。")
        s["messages"] = messages + [{"role": "assistant", "content": reply}]
        _save(sid, s)
        return {
            "reply": reply, "steps": _final_steps(sid, steps),
            "action": {"type": "pending_confirmation", "operation": itype, "data": idata},
        }

    # ====== 分发到工具处理器 ======
    # 原先这里是一段 760 行的 `if itype == "..."` 硬分支链（20 个分支）。
    # 现在工具的实现都在 services/agent_tools.py，各自登记进 agent_core.REGISTRY；
    # 本函数只负责：分类意图 -> 查表 -> 组装 Ctx -> 调用处理器。
    # 加新工具 = 在 agent_tools.py 加一条 Tool + 一个 handler，不再动本文件。
    ctx = Ctx(
        sid=sid, req=req, session=s, messages=messages, steps=steps, data=idata,
        itype=itype, style=style, profile=p, step_callback=step_callback,
        # guard_deleted：回合进行中若会话被删除，拒绝回写（不复活已删除的会话）
        save=lambda: _save(sid, s, guard_deleted=True), ai=ai_service,
    )
    try:
        return await run_with_timeout(tool, ctx)
    except asyncio.TimeoutError:
        log_error("sessions.chat", f"tool {itype} timed out after {tool.timeout}s (sid={sid})")
        raise HTTPException(status_code=504, detail=f"「{tool.label}」超时（{tool.timeout}秒），请稍后重试")


@router.post("/{sid}/chat")
async def session_chat(sid: str, req: ChatMsg):
    _session_path(sid)
    # 前台回合锁：只串行同一会话的两条前台消息。**不再**用它挡住改名/删除/后台任务。
    lock = fg_lock(sid)
    async with lock:
        try:
            return await _session_chat_impl(sid, req)
        except SessionDeletedError:
            raise HTTPException(409, "该对话已被删除，本轮结果未写入")
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

