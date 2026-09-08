import os
import re
import json
import uuid
import time
import asyncio
from datetime import datetime, timezone
from typing import Optional
from config import STORAGE_DIR, _atomic_write_json, ENABLE_FOCUS_BLACKBOARD
from logger import get_logger, log_error
from services.hippocampus_service import (
    get_teaching_context, update_mastery, update_study_session,
    add_topic, add_weak_point, remove_weak_point, get_topic_memory,
)
from services.face_analysis_service import analyze_face, merge_emotion_report
from services.ai_service import ai_service

logger = get_logger()

FOCUS_DIR = os.path.join(STORAGE_DIR, "focus_sessions")
os.makedirs(FOCUS_DIR, exist_ok=True)
_FOCUS_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_focus_locks: dict[str, asyncio.Lock] = {}

# 黑板板书常量
_BOARD_PAGE_MAX_ENTRIES = 12   # 单页条目上限，超出自动落新页
_BOARD_TEXT_MAX_LEN = 600      # 单条板书文字上限
_BOARD_OPS_MAX = 8             # 每段讲解的板书指令上限
_BOARD_FUNCTION_MAX = 2        # 每段函数图上限
_BOARD_DIAGRAM_MAX = 1         # 每段 AI 示意图上限
_BOARD_SVGS_MAX = 200          # 单会话板书 SVG 总量上限（防无限增长）
_BOARD_ASSET_RE = re.compile(r"^board_\d{1,4}\.svg$")

# 教学 Prompt 模板
TEACHING_SYSTEM_PROMPT = """你是一位经验丰富的私人教师，名字叫「学习 Agent」。你的教学风格是：
1. 每次只讲一个知识点或一小段内容，控制在 100-200 字
2. 讲解末尾必须输出「对吧」作为检查点标记
3. 根据学生的状态调整讲解策略：
   - 学生理解时：继续推进，适当加深
   - 学生困惑时：换一种方式解释，用更简单的例子
   - 学生走神时：插入互动问题或有趣的例子
   - 学生疲劳时：建议休息或总结已讲内容
4. 语言简洁、生动，避免长篇大论
5. 适当使用比喻和例子帮助理解

输出格式：纯文本讲解内容，末尾带「对吧」。不要使用 Markdown 标题。"""

BOARD_TEACHING_SYSTEM_PROMPT = """你是一位经验丰富的私人教师，名字叫「学习 Agent」。你有一块黑板可以边讲边写板书。你的教学风格是：
1. 每次只讲一个知识点或一小段内容，讲解文本控制在 100-200 字
2. 讲解末尾必须输出「对吧」作为检查点标记
3. 根据学生的状态调整讲解策略：理解时推进；困惑时换例重述；走神时互动；疲劳时休息或总结
4. 语言简洁、生动，适当使用比喻和例子

输出严格 JSON（不要任何额外文字、不要 Markdown 代码块）：
{"content": "讲解文本，末尾带「对吧」", "board": {"ops": [...]}}

board 与 ops 是可选字段（本段不需要板书时省略 board）。ops 支持的指令：
- {"op":"write","kind":"text","content":"板书文字（Markdown/LaTeX，不超过200字）"}：写一段要点或推导
- {"op":"write","kind":"function","spec":{"expr":"x**2 - 2*x + 1","x_min":-5,"x_max":5,"title":"标题"}}：画函数图。expr 是 Python 表达式（幂用 **，可用 math.sin 等），x_min/x_max 是数字
- {"op":"write","kind":"diagram","description":"示意图的文字描述"}：复杂示意图（较慢，每段最多 1 张，仅在函数图画不了时用）
- {"op":"erase","last":N}：擦掉当前页最后 N 条；{"op":"erase","entry":序号}：擦指定条目（从 0 开始）
- {"op":"clear"}：清空当前页；{"op":"newpage"}：另起一页
- {"op":"snapshot","label":"4-8字标签"}：在关键节点保存课堂回看快照（如推导完成、例题讲完）

板书使用原则：
- 每段最多 8 条指令。板书写关键步骤、结论和图，不要把讲解原文照抄上去
- 当前页空间不足时系统会自动开新页，不必担心内容丢失
- 讲函数、几何、实验流程等需要图的内容时优先画图辅助；重要结论写完可顺手 snapshot"""

CHECKPOINT_SYSTEM_PROMPT = """你是一位教育心理学专家。根据以下学生状态报告，决定下一步教学策略。

学生状态：{emotion_report}

请输出严格的 JSON 格式（不要任何额外文字）：
{{"action": "continue|rephrase|practice|rest|end", "reason": "简短原因", "difficulty_adjust": -1|0|1}}

其中：
- continue: 学生理解，继续下一段
- rephrase: 学生困惑，换一种方式重述
- practice: 学生理解但需要巩固，插入练习题
- rest: 学生疲劳，建议休息
- end: 主题讲完或学生状态不适合继续，结束本主题
- difficulty_adjust: 难度调整（-1 降低，0 不变，1 提高）"""


def _focus_path(sid: str) -> str:
    if not isinstance(sid, str) or not _FOCUS_ID_RE.fullmatch(sid):
        raise ValueError("会话 ID 无效")
    root = os.path.realpath(FOCUS_DIR)
    path = os.path.realpath(os.path.join(root, f"{sid}.json"))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("会话 ID 无效")
    return path


def _load_focus(sid: str) -> Optional[dict]:
    try:
        p = _focus_path(sid)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, IOError, OSError, ValueError) as e:
        log_error("focus", f"load_focus {sid} failed: {e}")
    return None


def _save_focus(sid: str, data: dict):
    try:
        _atomic_write_json(_focus_path(sid), data)
    except Exception as e:
        log_error("focus", f"save_focus {sid} failed: {e}")
        raise


def _get_focus_lock(sid: str) -> asyncio.Lock:
    if sid not in _focus_locks:
        _focus_locks[sid] = asyncio.Lock()
    return _focus_locks[sid]


def _cleanup_focus_lock(sid: str, lock: asyncio.Lock):
    """会话结束后清理锁，防止内存泄漏。

    仅当无持有者、无排队等待者且仍是当前映射对象时才移除，
    避免正在等待旧锁的协程被新请求新建的锁绕过。
    """
    if (len(_focus_locks) > 256 and not lock.locked()
            and not getattr(lock, "_waiters", None)
            and _focus_locks.get(sid) is lock):
        _focus_locks.pop(sid, None)


def _validate_focus_sid(sid: str) -> None:
    """在获取锁之前校验 sid：非法 ID 直接拒绝，不留下永久锁对象。"""
    _focus_path(sid)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===== 黑板板书 =====

def _board_new() -> dict:
    return {"pages": [{"created_at": _now_iso(), "entries": []}], "snapshots": []}


def board_asset_path(session_id: str, name: str) -> str:
    """校验并返回板书 SVG 资源的绝对路径。非法名/越界路径抛 ValueError。"""
    _focus_path(session_id)
    if not isinstance(name, str) or not _BOARD_ASSET_RE.fullmatch(name):
        raise ValueError("板书资源名无效")
    root = os.path.realpath(os.path.join(FOCUS_DIR, session_id))
    path = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("板书资源路径无效")
    return path


def _save_board_svg(session_id: str, svg: str) -> str:
    """原子保存板书 SVG（O_EXCL 独占创建取号，对齐 diagram_service 惯例），返回资产名 board_N.svg。"""
    folder = os.path.join(FOCUS_DIR, session_id)
    os.makedirs(folder, exist_ok=True)
    existing = [f for f in os.listdir(folder) if _BOARD_ASSET_RE.fullmatch(f)]
    n = max([int(f[6:-4]) for f in existing], default=0) + 1
    if n > _BOARD_SVGS_MAX:
        raise ValueError("板书图形数量已达上限")
    while n <= _BOARD_SVGS_MAX:
        name = f"board_{n}.svg"
        path = board_asset_path(session_id, name)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(svg)
            return name
        except FileExistsError:
            n += 1  # 并发取号冲突：独占创建失败后顺延
    raise ValueError("板书图形数量已达上限")


async def _render_board_function(spec) -> str:
    """函数图 spec → SVG（确定性渲染，AST 白名单沙箱）。失败抛 ValueError。

    护栏：expr 长度与嵌套幂/超大常量限制（防 9**9**9 类表达式拖死事件循环），
    渲染放线程池 + 5s 超时（safe_eval 为同步采样，卡死时不阻塞服务）。
    """
    from services.diagram_service import diagram_service as _ds
    if not isinstance(spec, dict):
        raise ValueError("function spec 无效")
    expr = str(spec.get("expr", "")).strip().replace("^", "**")
    if not expr:
        raise ValueError("函数表达式为空")
    if len(expr) > 200:
        raise ValueError("函数表达式过长")
    if expr.count("**") > 1:
        raise ValueError("不允许嵌套幂运算")
    if re.search(r"\d{7,}", expr):
        raise ValueError("不允许超大数值常量")
    try:
        norm = {
            "function_expr": expr,
            "x_range": [float(spec.get("x_min", -5)), float(spec.get("x_max", 5))],
        }
    except (TypeError, ValueError):
        raise ValueError("x_min/x_max 必须是数字")
    if norm["x_range"][0] >= norm["x_range"][1]:
        raise ValueError("x_min 必须小于 x_max")
    try:
        svg = await asyncio.wait_for(
            asyncio.to_thread(_ds._render_function_graph_spec, norm), timeout=5.0
        )
    except asyncio.TimeoutError:
        raise ValueError("函数图渲染超时")
    if not svg or "<svg" not in svg:
        raise ValueError("函数图渲染为空")
    return svg


async def _render_board_diagram(description: str) -> str:
    """AI 按文字描述生成单个教学 SVG（纯文本生成任务，走 v4-pro 推理）。失败抛 ValueError。"""
    from services.diagram_service import diagram_service as _ds
    desc = str(description or "").strip()
    if not desc:
        raise ValueError("示意图描述为空")
    prompt = (
        "你是数学/物理示意图绘制专家。根据描述生成一张教学用 SVG 示意图。\n"
        "硬性要求：输出单个闭合的 <svg>...</svg>，不要任何其他文字；白底黑线；"
        "只使用基本图元（line/rect/circle/ellipse/path/polygon/polyline/text 及 g 分组）；"
        "必须带 viewBox；宽高不超过 640x480；图形准确、文字清晰；"
        "禁止 script/动画/事件属性/外部引用。\n"
        f"描述：{desc[:300]}"
    )
    raw = await ai_service.deepseek_chat(
        [{"role": "user", "content": prompt}], max_tokens=4096, scope="focus"
    )
    m = re.search(r"<svg[\s\S]*</svg>", raw or "")
    if not m:
        raise ValueError("AI 未返回闭合 SVG")
    svg = _ds._sanitize_svg(m.group(0))
    if not _ds._has_drawing_content(svg):
        raise ValueError("SVG 无有效绘图内容")
    return svg


async def _apply_board_ops(session: dict, ops) -> dict:
    """执行一段讲解附带的板书指令，返回执行摘要（注入下一段 prompt）。"""
    sid = str(session.get("id", ""))
    board = session.get("board")
    if not isinstance(board, dict):
        board = _board_new()
        session["board"] = board
    pages = board.setdefault("pages", [])
    if not pages:
        pages.append({"created_at": _now_iso(), "entries": []})
    board.setdefault("snapshots", [])

    summary = {"written": 0, "svg_ok": 0, "svg_failed": 0, "erased": 0,
               "newpage": 0, "snapshot": 0, "skipped": 0}
    function_used = 0
    diagram_used = 0
    for op in (ops if isinstance(ops, list) else [])[:_BOARD_OPS_MAX]:
        if not isinstance(op, dict):
            summary["skipped"] += 1
            continue
        kind = str(op.get("op", ""))
        page = pages[-1]
        try:
            if kind == "write":
                wk = op.get("kind")
                if wk == "text":
                    content = str(op.get("content", "")).strip()[:_BOARD_TEXT_MAX_LEN]
                    if not content:
                        summary["skipped"] += 1
                        continue
                    page["entries"].append({"kind": "text", "content": content})
                    summary["written"] += 1
                elif wk == "function":
                    if function_used >= _BOARD_FUNCTION_MAX:
                        summary["skipped"] += 1
                        continue
                    try:
                        svg = await _render_board_function(op.get("spec"))
                        name = _save_board_svg(sid, svg)
                        title = str((op.get("spec") or {}).get("title", ""))[:60]
                        page["entries"].append({"kind": "svg", "asset": name, "title": title})
                        summary["svg_ok"] += 1
                        function_used += 1
                    except Exception as e:
                        page["entries"].append({"kind": "text", "content": f"[板书图形生成失败：{str(e)[:80]}]"})
                        summary["svg_failed"] += 1
                elif wk == "diagram":
                    if diagram_used >= _BOARD_DIAGRAM_MAX:
                        summary["skipped"] += 1
                        continue
                    try:
                        svg = await _render_board_diagram(str(op.get("description", "")))
                        name = _save_board_svg(sid, svg)
                        page["entries"].append({"kind": "svg", "asset": name, "title": str(op.get("description", ""))[:60]})
                        summary["svg_ok"] += 1
                        diagram_used += 1
                    except Exception as e:
                        page["entries"].append({"kind": "text", "content": f"[板书图形生成失败：{str(e)[:80]}]"})
                        summary["svg_failed"] += 1
                else:
                    summary["skipped"] += 1
            elif kind == "erase":
                entries = page["entries"]
                if "last" in op:
                    try:
                        n = min(max(int(op.get("last", 0)), 0), len(entries))
                    except (TypeError, ValueError):
                        n = 0
                    if n:
                        del entries[len(entries) - n:]
                        summary["erased"] += n
                    else:
                        summary["skipped"] += 1
                elif "entry" in op:
                    try:
                        idx = int(op.get("entry", -1))
                    except (TypeError, ValueError):
                        idx = -1
                    if 0 <= idx < len(entries):
                        entries.pop(idx)
                        summary["erased"] += 1
                    else:
                        summary["skipped"] += 1
                else:
                    summary["skipped"] += 1
            elif kind == "clear":
                summary["erased"] += len(page["entries"])
                page["entries"] = []
            elif kind == "newpage":
                pages.append({"created_at": _now_iso(), "entries": []})
                summary["newpage"] += 1
            elif kind == "snapshot":
                label = str(op.get("label", "")).strip()[:24] or f"课堂快照{len(board['snapshots']) + 1}"
                board["snapshots"].append({"page": len(pages), "label": label, "time": _now_iso(), "by": "ai"})
                summary["snapshot"] += 1
            else:
                summary["skipped"] += 1
        except Exception as e:
            logger.warning("board op %s failed: %s", kind, str(e)[:120])
            summary["skipped"] += 1
        # 页满兜底：超出单页上限的条目自动滚入新页，保证 AI 书写不丢失
        while len(pages[-1]["entries"]) > _BOARD_PAGE_MAX_ENTRIES:
            overflow = pages[-1]["entries"][_BOARD_PAGE_MAX_ENTRIES:]
            pages[-1]["entries"] = pages[-1]["entries"][:_BOARD_PAGE_MAX_ENTRIES]
            pages.append({"created_at": _now_iso(), "entries": overflow})

    return {
        "written": summary["written"], "svg_ok": summary["svg_ok"],
        "svg_failed": summary["svg_failed"], "erased": summary["erased"],
        "newpage": summary["newpage"], "snapshot": summary["snapshot"],
        "skipped": summary["skipped"],
        "page": len(pages), "page_entries": len(pages[-1]["entries"]),
        "page_max": _BOARD_PAGE_MAX_ENTRIES, "snapshots": len(board["snapshots"]),
    }


async def save_board_snapshot(session_id: str, label: str = "") -> dict:
    """学生手动保存当前页快照（回看引用）。"""
    _validate_focus_sid(session_id)
    if not ENABLE_FOCUS_BLACKBOARD:
        raise ValueError("黑板功能未启用")
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            session = _load_focus(session_id)
            if not session:
                raise ValueError("会话不存在")
            board = session.get("board")
            if not isinstance(board, dict) or not board.get("pages"):
                raise ValueError("没有板书可保存")
            clean = str(label or "").strip()[:24] or f"课堂快照{len(board.get('snapshots', [])) + 1}"
            snap = {"page": len(board["pages"]), "label": clean, "time": _now_iso(), "by": "student"}
            board.setdefault("snapshots", []).append(snap)
            session["board"] = board
            session["updated_at"] = _now_iso()
            _save_focus(session_id, session)
            return {"snapshot": snap}
    finally:
        _cleanup_focus_lock(session_id, lock)


async def start_session(mode: str, topic: str = "", question_id: str = "") -> dict:
    """启动一个新的专注模式会话"""
    sid = str(uuid.uuid4())

    # 获取教学上下文（海马体数据）
    teaching_ctx = get_teaching_context(topic)

    session = {
        "id": sid,
        "mode": mode,
        "topic": topic,
        "question_id": question_id,
        "status": "preparing",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "segments": [],
        "teaching_strategy": {
            "pace": "normal",
            "style": teaching_ctx.get("meta", {}).get("preferred_style", "conceptual"),
            "difficulty_level": 3,
        },
        "total_checkpoints": 0,
        "passed_checkpoints": 0,
    }
    if ENABLE_FOCUS_BLACKBOARD:
        session["board"] = _board_new()

    _save_focus(sid, session)

    # 生成第一段讲解
    first_segment = await _generate_teaching_segment(session, teaching_ctx)
    session["segments"].append(first_segment)
    session["status"] = "teaching"
    session["updated_at"] = _now_iso()
    _save_focus(sid, session)

    return session


async def _generate_teaching_segment(session: dict, teaching_ctx: dict) -> dict:
    """生成一段讲解内容（黑板开启时输出 JSON 并执行板书指令）"""
    topic = session.get("topic", "")
    mode = session.get("mode", "topic")
    strategy = session.get("teaching_strategy", {})
    segment_index = len(session.get("segments", []))

    # 构建 Prompt（黑板开启用 JSON 版提示词）
    use_board = bool(ENABLE_FOCUS_BLACKBOARD)
    system = BOARD_TEACHING_SYSTEM_PROMPT if use_board else TEACHING_SYSTEM_PROMPT

    # 构建用户上下文
    user_parts = []
    if topic:
        user_parts.append(f"当前学习主题：{topic}")

    # 添加海马体上下文
    meta = teaching_ctx.get("meta", {})
    if meta:
        user_parts.append(f"学生理解力基线：{meta.get('baseline_understanding', 0.5)}")
        user_parts.append(f"学生记忆力基线：{meta.get('baseline_memory', 0.5)}")
        user_parts.append(f"偏好讲解风格：{meta.get('preferred_style', 'conceptual')}")

    # 添加已有主题记忆
    topics_memory = teaching_ctx.get("topics", {})
    if topics_memory:
        for t, td in topics_memory.items():
            mastery = td.get("mastery", 0)
            weak = td.get("weak_points", [])
            user_parts.append(f"主题「{t}」掌握度 {mastery:.0%}，薄弱点：{', '.join(weak) if weak else '无'}")

    # 添加已有讲解历史
    segments = session.get("segments", [])
    if segments:
        user_parts.append("\n已讲内容摘要：")
        for seg in segments[-3:]:  # 最近 3 段
            content = seg.get("content", "")[:100]
            user_parts.append(f"- {content}...")
        # 板书上下文：当前页占用 + 上一段执行摘要，供 AI 管理空间（擦旧/翻页）
        if use_board:
            board = session.get("board") or {}
            pages = board.get("pages") or []
            if pages:
                user_parts.append(
                    f"\n当前板书：共 {len(pages)} 页，"
                    f"当前页 {len(pages[-1].get('entries', []))}/{_BOARD_PAGE_MAX_ENTRIES} 条，"
                    f"快照 {len(board.get('snapshots', []))} 个。"
                )
            last_summary = segments[-1].get("board_summary")
            if last_summary:
                user_parts.append(f"上一段板书执行摘要：{json.dumps(last_summary, ensure_ascii=False)}")

    # 添加策略
    difficulty = strategy.get("difficulty_level", 3)
    pace = strategy.get("pace", "normal")
    user_parts.append(f"\n当前难度等级：{difficulty}/5，节奏：{pace}")

    if segment_index == 0:
        user_parts.append("\n请开始第一段讲解。")
    else:
        user_parts.append("\n请继续下一段讲解。")

    user_content = "\n".join(user_parts)

    try:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        board_ops = None
        if use_board:
            data = await ai_service.deepseek_json(messages, max_tokens=3000, scope="focus")
            if not isinstance(data, dict):
                raise ValueError("讲解返回非对象")
            content = str(data.get("content", "")).strip()
            if not content:
                raise ValueError("讲解内容为空")
            board_data = data.get("board")
            if isinstance(board_data, dict):
                board_ops = board_data.get("ops")
        else:
            raw_reply = await ai_service.deepseek_chat(messages, max_tokens=1024, scope="focus")
            content = raw_reply.strip() if raw_reply else ""

        # 确保末尾有「对吧」
        if "对吧" not in content[-10:]:
            content = content + "\n\n对吧"

        # 执行板书指令（失败只影响板书，不阻断教学）
        board_summary = None
        if use_board and board_ops:
            try:
                board_summary = await _apply_board_ops(session, board_ops)
            except Exception as be:
                log_error("focus", f"apply_board_ops failed: {str(be)[:200]}")

        segment = {
            "index": segment_index,
            "content": content,
            "has_checkpoint": True,
            "checkpoint_response": None,
        }
        if board_summary is not None:
            segment["board_summary"] = board_summary
        return segment
    except Exception as e:
        log_error("focus", f"generate_teaching_segment failed: {e}")
        return {
            "index": segment_index,
            "content": "抱歉，生成讲解时遇到了问题。请稍后重试。\n\n对吧",
            "has_checkpoint": True,
            "checkpoint_response": None,
        }


async def submit_checkpoint(
    session_id: str,
    voice_text: str = "",
    emotion_report: dict = None,
    webcam_image: str = "",
    voice_features: dict = None,
    segment_index: int = None,
) -> dict:
    """提交检查点响应，获取下一段讲解。

    segment_index：学生响应的讲解段序号（前端从 currentSegment.index 传入）。
    缺省时保持旧行为（取最新段）；显式传入时做幂等校验——该段已响应过则拒绝
    （功能检查轮 F8-2：前端超时重试/连点会把新教学段误挂成检查点响应，
    total_checkpoints 虚增并污染掌握度统计）。"""
    _validate_focus_sid(session_id)
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            return await _submit_checkpoint_locked(session_id, voice_text, emotion_report,
                                                   webcam_image, voice_features,
                                                   segment_index)
    finally:
        _cleanup_focus_lock(session_id, lock)


async def _submit_checkpoint_locked(
    session_id: str,
    voice_text: str = "",
    emotion_report: dict = None,
    webcam_image: str = "",
    voice_features: dict = None,
    segment_index: int = None,
) -> dict:
    session = _load_focus(session_id)
    if not session:
        raise ValueError("会话不存在")

    if session.get("status") not in ("teaching", "checkpoint"):
        raise ValueError(f"当前状态 {session.get('status')} 不允许提交检查点")

    segments = session.get("segments", [])
    if segment_index is not None:
        if not isinstance(segment_index, int) or segment_index < 0 or segment_index >= len(segments):
            raise ValueError(f"讲解段序号无效：{segment_index}")
        target_index = segment_index
        if segments[target_index].get("checkpoint_response"):
            raise ValueError("该讲解段已提交过检查点响应，请等待下一段讲解开始")
    else:
        target_index = len(segments) - 1
    if target_index < 0:
        raise ValueError("没有可响应的讲解段")

    # 1. 表情分析（如果有图片）
    face_report = None
    if webcam_image:
        face_report = await analyze_face(webcam_image)

    # 2. 合并情感报告
    if face_report and voice_features:
        combined_report = merge_emotion_report(face_report, voice_features)
    elif face_report:
        combined_report = face_report
    elif voice_features:
        combined_report = {
            "overall_state": "engaged",
            "confidence": 0.5,
            "suggestion": "continue",
            "face_expression": "neutral",
            "voice_features": voice_features,
            "indicators": [],
        }
    else:
        combined_report = {
            "overall_state": "engaged",
            "confidence": 0.5,
            "suggestion": "continue",
            "face_expression": "neutral",
            "voice_features": {},
            "indicators": [],
        }

    # 3. 记录检查点响应
    checkpoint_response = {
        "voice_text": voice_text or "",
        "emotion_report": combined_report,
        "timestamp": _now_iso(),
    }

    session["segments"][target_index]["checkpoint_response"] = checkpoint_response
    session["total_checkpoints"] = session.get("total_checkpoints", 0) + 1

    if combined_report.get("overall_state") in ("understanding", "engaged"):
        session["passed_checkpoints"] = session.get("passed_checkpoints", 0) + 1

    # 4. 根据情感报告决定下一步
    suggestion = combined_report.get("suggestion", "continue")

    if suggestion == "pause":
        session["status"] = "paused"
        session["updated_at"] = _now_iso()
        _save_focus(session_id, session)
        return {
            "session": session,
            "action": "pause",
            "message": "建议休息一下，准备好了再继续。",
        }

    if suggestion == "simplify":
        # 降低难度，重述当前段
        strategy = session.get("teaching_strategy", {})
        strategy["difficulty_level"] = max(1, strategy.get("difficulty_level", 3) - 1)
        session["teaching_strategy"] = strategy

    # 5. 生成下一段讲解
    teaching_ctx = get_teaching_context(session.get("topic", ""))
    next_segment = await _generate_teaching_segment(session, teaching_ctx)
    session["segments"].append(next_segment)
    session["status"] = "teaching"
    session["updated_at"] = _now_iso()
    _save_focus(session_id, session)

    return {
        "session": session,
        "action": "continue",
        "segment": next_segment,
    }


async def get_session_state(session_id: str) -> dict:
    """获取会话当前状态"""
    session = _load_focus(session_id)
    if not session:
        raise ValueError("会话不存在")
    return session


async def pause_session(session_id: str) -> dict:
    """暂停会话"""
    _validate_focus_sid(session_id)
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            session = _load_focus(session_id)
            if not session:
                raise ValueError("会话不存在")
            session["status"] = "paused"
            session["updated_at"] = _now_iso()
            _save_focus(session_id, session)
            return session
    finally:
        _cleanup_focus_lock(session_id, lock)


async def resume_session(session_id: str) -> dict:
    """恢复会话"""
    _validate_focus_sid(session_id)
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            session = _load_focus(session_id)
            if not session:
                raise ValueError("会话不存在")
            session["status"] = "teaching"
            session["updated_at"] = _now_iso()
            _save_focus(session_id, session)
            return session
    finally:
        _cleanup_focus_lock(session_id, lock)


async def end_session(session_id: str) -> dict:
    """结束会话，更新海马体"""
    _validate_focus_sid(session_id)
    lock = _get_focus_lock(session_id)
    try:
        async with lock:
            return await _end_session_locked(session_id)
    finally:
        # 在锁释放后有界清理；等待中的协程持有旧锁引用不受影响，
        # 新请求会创建新锁，与旧锁的等待者互不干扰（end 为终态，影响面有限）
        _cleanup_focus_lock(session_id, lock)


async def _end_session_locked(session_id: str) -> dict:
    session = _load_focus(session_id)
    if not session:
        raise ValueError("会话不存在")

    session["status"] = "completed"
    session["updated_at"] = _now_iso()

    # 更新海马体（失败不影响会话结束）
    try:
        topic = session.get("topic", "")
        if topic:
            total_checkpoints = session.get("total_checkpoints", 0)
            passed_checkpoints = session.get("passed_checkpoints", 0)
            pass_rate = min(1.0, passed_checkpoints / max(1, total_checkpoints))

            if pass_rate >= 0.8:
                mastery_delta = 0.1
            elif pass_rate >= 0.5:
                mastery_delta = 0.05
            else:
                mastery_delta = 0.02

            update_mastery(topic, mastery_delta, reason="focus_session")
            update_study_session(topic, minutes=max(1, total_checkpoints * 2), checkpoint_pass_rate=pass_rate)
    except Exception as e:
        log_error("focus", f"hippocampus update failed: {e}")
    finally:
        _save_focus(session_id, session)

    return session


def list_sessions(limit: int = 20) -> list:
    """列出最近的专注模式会话（按修改时间倒序）"""
    sessions = []
    try:
        all_files = [f for f in os.listdir(FOCUS_DIR) if f.endswith(".json")]
        # 按文件修改时间排序（最近的在前）
        all_files.sort(key=lambda f: os.path.getmtime(os.path.join(FOCUS_DIR, f)), reverse=True)
        files = all_files[:limit]
        for fname in files:
            sid = fname[:-5]
            data = _load_focus(sid)
            if data:
                sessions.append({
                    "id": data.get("id", sid),
                    "mode": data.get("mode", ""),
                    "topic": data.get("topic", ""),
                    "status": data.get("status", ""),
                    "created_at": data.get("created_at", ""),
                    "updated_at": data.get("updated_at", ""),
                    "total_checkpoints": data.get("total_checkpoints", 0),
                    "passed_checkpoints": data.get("passed_checkpoints", 0),
                })
    except OSError as e:
        log_error("focus", f"list_sessions failed: {e}")
    return sessions
