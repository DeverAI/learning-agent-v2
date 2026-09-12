"""题库自动巡检与重写服务

定时任务流程:
  1:00 → 开始全面巡检，标记问题
  3:00 → 自动重写标记的题目
  4:00 → 停止（可手动触发）

巡检项:
  - missing_diagram: 需几何图但题面无图
  - missing_standard_answer: 标答为空
  - wrong_answer: 答案逻辑错误
  - style_mismatch: 不符合用户排版偏好
  - missing_attr: 缺少年级/学科/知识点
  - thinking_in_answer: 解答中混有思考/犹豫内容
  - other: 其他问题

重写失败时保留标记，错误信息写入 error_message 备用。
"""

import os
import json
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from models.database import async_session
from models.models import Question, Note, _utcnow
from services.ai_service import ai_service
from services.user_profile import load_profile
from sqlalchemy import select
from logger import get_logger
from config import _atomic_write_json, STORAGE_DIR, load_settings

logger = get_logger()

# === 存储 ===
SYSTEM_MSG_PATH = os.path.join(STORAGE_DIR, "system_messages.json")
os.makedirs(STORAGE_DIR, exist_ok=True)
_system_message_lock = threading.RLock()
_msg_load_failed = False  # 加载失败置位：数据不可信期间拒绝写回，防空表覆盖历史


# ===== System Messages (仪表盘消息栏) =====

def _load_system_messages() -> list:
    global _msg_load_failed
    with _system_message_lock:
        try:
            if os.path.exists(SYSTEM_MSG_PATH):
                with open(SYSTEM_MSG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _msg_load_failed = False
                return data if isinstance(data, list) else []
            _msg_load_failed = False
            return []
        except (json.JSONDecodeError, IOError, OSError, TypeError) as exc:
            logger.warning("Failed to load system messages: %s", exc)
            _msg_load_failed = True
    return []

def _save_system_messages(msgs: list):
    global _msg_load_failed
    try:
        with _system_message_lock:
            if _msg_load_failed:
                # 上次加载失败时内存里的表不可信（可能是空表兜底）：
                # 此时写入会用空数据覆盖全部消息历史，必须拒绝
                logger.warning("Skip saving system messages: last load failed, refusing to overwrite")
                return
            _atomic_write_json(SYSTEM_MSG_PATH, msgs[-50:])
    except (IOError, OSError, PermissionError) as e:
        logger.warning("Failed to save system messages: %s", e)
        raise

def add_system_message(msg_type: str, title: str, content: str, question_id: str = ""):
    """添加一条系统消息到仪表盘。

    写入失败只记日志不上抛：消息属于尽力而为的旁路通知，
    若在此抛出 IO 异常会杀死夜间调度器循环（磁盘满/权限抖动等瞬时错误）。
    """
    try:
        with _system_message_lock:
            msgs = _load_system_messages()
            msgs.append({
                "type": msg_type,
                "title": str(title or "")[:200],
                "content": str(content or "")[:2000],
                "question_id": str(question_id or "")[:64],
                "time": _utcnow().isoformat(),
                "read": False,
            })
            _save_system_messages(msgs)
    except Exception as exc:
        logger.warning("Failed to persist system message (%s): %s", title, exc)

def get_system_messages(limit: int = 20) -> list:
    return _load_system_messages()[-limit:]

def mark_messages_read():
    with _system_message_lock:
        msgs = _load_system_messages()
        for m in msgs:
            m["read"] = True
        _save_system_messages(msgs)


def delete_system_message_at(index: int | None = None, *, clear_all: bool = False) -> bool:
    with _system_message_lock:
        msgs = _load_system_messages()
        if clear_all:
            _save_system_messages([])
            return True
        if index is None or index < 0 or index >= len(msgs):
            return False
        msgs.pop(index)
        _save_system_messages(msgs)
        return True


# ===== 审计标记 =====

FLAG_TYPES = {
    "missing_diagram": "题面缺失示意图",
    "missing_standard_answer": "标准答案缺失",
    "wrong_answer": "答案疑似错误",
    "style_mismatch": "解答不符合排版偏好",
    "missing_attr": "题目特征(年级/学科/知识点)缺失",
    "thinking_in_answer": "解答中混有思考/犹豫内容",
    "verify_needs_review": "二次验证未通过，需巡检复查",
    "diagram_inconsistent": "多图几何关系不一致",
    "question_challenge_low": "题目存在低概率疑点",
    "question_challenge_high": "题目大概率存在问题",
    # ==== 识别链路的"降级留痕"（2026-09-12 新增）====
    # 这两类不是"题目本身有问题"，而是"识别过程降级了，结果可能不完整"。
    # 之所以必须进 audit_flags：原先这些降级只写进 storage/task_states/{qid}.json，
    # 而该文件在任务**正常完成时会被 _clear_task_state 直接删掉** ——
    # 于是"整页只切出一道题""部分图片没认出来"这两件事**永远不会被用户看到**。
    "split_fallback": "整页切题失败，已按单题处理",
    "ocr_partial_failure": "部分图片识别失败，内容可能不完整",
    "other": "其他问题",
}
# 清障/重写流程必须**保留**的标记类型。
#
# 原先这里是写死的三元组白名单，新类型不加进来就会被 `run_auto_rewrite` 悄悄清掉 ——
# 表现为"标记出现过一会儿又没了"。所以新加标记类型时**必须同步这里**。
_PRESERVED_FLAG_TYPES = (
    "missing_diagram", "question_challenge_low", "question_challenge_high",
    "split_fallback", "ocr_partial_failure",
)

async def flag_question(qid: str, flag_type: str, reason: str, auto: bool = True):
    """给题目添加审计标记"""
    flag_type = str(flag_type or "other").strip()
    if flag_type not in FLAG_TYPES:
        flag_type = "other"
    reason = str(reason or "").strip()[:2000] or "未提供具体原因"
    async with async_session() as db:
        q = await db.get(Question, qid)
        if not q:
            return
        # 检查是否已解决（用户标记为已解决则跳过）
        is_resolved = q.is_resolved if hasattr(q, 'is_resolved') else False
        if is_resolved:
            return
        # 读取现有 flags（提交前重读，缩小与夜间巡检并发写覆盖的窗口）
        await db.refresh(q, ['audit_flags'])
        flags = q.audit_flags if hasattr(q, 'audit_flags') and q.audit_flags else []
        if not isinstance(flags, list):
            flags = []
        # 去重：同类型不重复添加
        for f in flags:
            if isinstance(f, dict) and f.get("type") == flag_type and f.get("auto") == auto:
                return
        flags.append({
            "type": flag_type,
            "reason": reason,
            "auto": auto,
            "created_at": _utcnow().isoformat(),
        })
        q.audit_flags = flags
        await db.commit()
        # 通知仪表盘（含题目链接，用户可点击跳转）
        add_system_message("audit_result", f"题目标记: {FLAG_TYPES.get(flag_type, flag_type)}",
                          f"{reason[:200]}", qid)


async def clear_flag(qid: str, flag_type: str = None):
    """清除审计标记"""
    async with async_session() as db:
        q = await db.get(Question, qid)
        if not q:
            return
        flags = q.audit_flags if hasattr(q, 'audit_flags') and q.audit_flags else []
        if not isinstance(flags, list):
            flags = []
        if flag_type:
            flags = [f for f in flags if not isinstance(f, dict) or f.get("type") != flag_type]
        else:
            flags = []
        q.audit_flags = flags
        await db.commit()


async def set_question_challenge(qid: str, challenge: dict, auto: bool = True) -> dict:
    """Replace the current AI challenge while preserving unrelated/manual flags."""
    from services.question_challenge import normalize_question_challenge

    normalized = normalize_question_challenge(challenge)
    async with async_session() as db:
        q = await db.get(Question, qid)
        if not q:
            return normalized
        flags = q.audit_flags if isinstance(q.audit_flags, list) else []
        previous = next((
            f for f in flags
            if isinstance(f, dict)
            and f.get("auto") == auto
            and f.get("type") in ("question_challenge_low", "question_challenge_high")
        ), None)
        flags = [
            f for f in flags
            if not (
                isinstance(f, dict)
                and f.get("auto") == auto
                and f.get("type") in ("question_challenge_low", "question_challenge_high")
            )
        ]
        if normalized["level"] != "none":
            reason = "；".join(normalized["reasons"][:3]) or "AI 检测到题目存在待核对疑点"
            flags.append({
                "type": f"question_challenge_{normalized['level']}",
                "reason": reason[:600],
                "auto": auto,
                "challenge": normalized,
                "created_at": _utcnow().isoformat(),
            })
        q.audit_flags = flags
        await db.commit()

    changed = (previous or {}).get("challenge") != normalized
    if normalized["level"] != "none" and changed:
        title = "题目大概率存在问题" if normalized["level"] == "high" else "题目存在低概率疑点"
        add_system_message(
            "audit_result", title,
            ("；".join(normalized["reasons"][:3]) or normalized["recommended_action"])[:300],
            qid,
        )
    return normalized


async def set_resolved(qid: str, resolved: bool = True):
    """设置题目为已解决（AI不再修改）"""
    async with async_session() as db:
        q = await db.get(Question, qid)
        if not q:
            return
        q.is_resolved = resolved
        await db.commit()


# ===== 巡检逻辑 =====

async def run_full_audit():
    """执行全库巡检"""
    logger.info("=== Nightly audit started ===")
    async with async_session() as db:
        r = await db.execute(select(Question).where(Question.status == "done"))
        all_questions = r.scalars().all()

    profile = load_profile()
    style = profile.get("style_notes", "") or profile.get("notation_preferences", "")

    total = len(all_questions)
    flagged_count = 0
    for q in all_questions:
        try:
            # 每个题目独立session，避免session状态混乱
            async with async_session() as session:
                q_recent = await session.get(Question, q.id)
                if q_recent:
                    if await _audit_single(q_recent, style):
                        flagged_count += 1
        except Exception as e:
            logger.warning("Audit failed for %s: %s", q.id, e)
        await asyncio.sleep(0.1)  # rate limit

    # 生成每日一言
    try:
        await _generate_daily_quote()
    except Exception as e:
        logger.warning("Daily quote generation during audit failed: %s", e)

    add_system_message("system", "夜间巡检完成",
                       f"共巡检 {total} 题，发现 {flagged_count} 项问题。")
    logger.info("=== Nightly audit completed: %d flagged ===", flagged_count)
    return flagged_count


async def _audit_single(q, style: str) -> bool:
    """单题审计（q为最新session中获取的对象）。
    返回 True 表示新增了至少一个标记。
    """
    flags = q.audit_flags if hasattr(q, 'audit_flags') and q.audit_flags else []
    if not isinstance(flags, list):
        flags = []
    is_resolved = q.is_resolved if hasattr(q, 'is_resolved') else False
    if is_resolved:
        return False  # 用户标记为已解决，跳过

    new_flags_added = 0  # 本地计数（flag_question 使用独立session）

    qhtml = q.question_html or ""
    ahtml = q.answer_html or ""
    sa = q.standard_answer or ""
    has_diagrams = q.diagrams and len(q.diagrams) > 0
    ocr = q.ocr_text or ""

    # 1. 检查标准答案缺失
    if not sa.strip() and not _has_flag(flags, "missing_standard_answer"):
        await flag_question(q.id, "missing_standard_answer", f"题目 {q.id[:8]} 的标准答案为空", auto=True)
        new_flags_added += 1

    # 2. 检查几何题缺图
    geo_keywords = ["三角", "圆", "四边形", "平行", "垂直", "正方", "长方", "坐标", "函数"]
    if any(kw in ocr or kw in qhtml for kw in geo_keywords):
        if not has_diagrams and not _has_flag(flags, "missing_diagram"):
            await flag_question(q.id, "missing_diagram", f"几何题 {q.id[:8]} 题面缺少示意图", auto=True)
            new_flags_added += 1

    # 3. 检查特征缺失
    missing_attrs = []
    if not q.subject: missing_attrs.append("学科")
    if not q.grade: missing_attrs.append("年级")
    if not q.knowledge_tags or len(q.knowledge_tags) == 0: missing_attrs.append("知识点")
    if missing_attrs and not _has_flag(flags, "missing_attr"):
        await flag_question(q.id, "missing_attr", f"题目 {q.id[:8]} 缺少: {', '.join(missing_attrs)}", auto=True)
        new_flags_added += 1

    # 4. 检查解答中混有思考
    thinking_words = ["仍然矛盾", "不对", "不应该", "我看看", "让我想", "抱歉", "对不起", "犹豫", "让我重新"]
    if any(kw in ahtml.lower() for kw in thinking_words):
        if not _has_flag(flags, "thinking_in_answer"):
            await flag_question(q.id, "thinking_in_answer", f"题目 {q.id[:8]} 解答中混有AI思考/犹豫内容", auto=True)
            new_flags_added += 1

    # 5. DeepSeek 深度检查 (仅对有完整内容的题目)
    if sa.strip() and ahtml.strip() and qhtml.strip():
        try:
            check_prompt = (
                f"请检查以下题目解答是否存在问题。只需返回JSON。\n"
                f"【题面】{qhtml[:500]}\n"
                f"【解答】{ahtml[:500]}\n"
                f"【标答】{sa[:100]}\n"
                f"【排版偏好】{style}\n\n"
                '{"has_issue":true/false,"issue_type":"wrong_answer/style_mismatch/other或不填","reason":"问题描述（限100字）"}'
            )
            r = await ai_service.deepseek_json([{"role": "user", "content": check_prompt}], max_tokens=1024)
            # 与 flag_question 相同的归一化：未知类型统一存为 other，
            # 否则查重键与实际存储键不一致会导致重复标记堆积
            issue_type = str(r.get("issue_type", "other") or "other").strip()
            if issue_type not in FLAG_TYPES:
                issue_type = "other"
            if r.get("has_issue") and not _has_flag(flags, issue_type):
                await flag_question(q.id, issue_type,
                                   f"题目 {q.id[:8]}: {r.get('reason', '无描述')}", auto=True)
                new_flags_added += 1
        except Exception as audit_exc:
            logger.warning("Deep audit check failed for %s: %s", q.id, audit_exc)

    return new_flags_added > 0


def _has_flag(flags: list, flag_type: str) -> bool:
    # flags 可能混入历史遗留的非 dict 条目，防御性跳过避免巡检中断
    return any(isinstance(f, dict) and f.get("type") == flag_type for f in (flags or []))


# ===== 自动重写（含重试与错误保留） =====

async def run_auto_rewrite():
    """3:00 自动重写所有标记的题目。
    重写成功 → 清除对应标记
    重写失败 → 保留标记，错误信息写入 error_message 字段
    """
    logger.info("=== Auto-rewrite started ===")
    async with async_session() as db:
        r = await db.execute(select(Question).where(Question.status == "done"))
        all_q = r.scalars().all()

    profile = load_profile()
    style = profile.get("style_notes", "") or profile.get("notation_preferences", "")
    rewritten = 0
    failed = 0
    skipped_resolved = 0
    skipped_challenge = 0
    skipped_no_actionable = 0

    for q in all_q:
        flags = q.audit_flags if hasattr(q, 'audit_flags') and q.audit_flags else []
        if not isinstance(flags, list) or not flags:
            continue
        is_resolved = q.is_resolved if hasattr(q, 'is_resolved') else False
        if is_resolved:
            skipped_resolved += 1
            continue
        # 高概率题目问题不能靠夜间 AI 擅自改条件来“修好”，必须等待人工核对。
        if any(f.get("type") == "question_challenge_high" for f in flags if isinstance(f, dict)):
            skipped_challenge += 1
            continue
        actionable_types = {
            "wrong_answer", "thinking_in_answer", "style_mismatch", "missing_diagram",
        }
        if not any(f.get("type") in actionable_types for f in flags if isinstance(f, dict)):
            skipped_no_actionable += 1
            continue

        try:
            success, error_msg = await _rewrite_single_with_retry(q, style, max_retries=2)
            if success:
                rewritten += 1
            else:
                failed += 1
                # 保留标记，写入错误信息（错误信息滚动截断，防止无限增长）
                async with async_session() as db2:
                    q2 = await db2.get(Question, q.id)
                    if q2:
                        prev = (q2.error_message or "")
                        q2.error_message = (prev + f"\n[auto_rewrite失败 {_utcnow().isoformat()}] {error_msg}")[-4000:]
                        await db2.commit()
        except Exception as e:
            logger.warning("Rewrite failed for %s: %s", q.id, e)
            failed += 1
            async with async_session() as db3:
                q3 = await db3.get(Question, q.id)
                if q3:
                    prev = (q3.error_message or "")
                    q3.error_message = (prev + f"\n[auto_rewrite异常 {_utcnow().isoformat()}] {str(e)}")[-4000:]
                    await db3.commit()
        await asyncio.sleep(0.3)

    add_system_message("system", "自动重写完成",
                       f"成功重写 {rewritten} 题，失败 {failed} 题（跳过已解决 {skipped_resolved} 题）。"
                       f"跳过待人工核对的质疑题 {skipped_challenge} 题；无可自动修复项 {skipped_no_actionable} 题。")
    logger.info("=== Auto-rewrite completed: %d rewritten, %d failed, %d resolved, %d challenged, %d no-actionable ===",
                rewritten, failed, skipped_resolved, skipped_challenge, skipped_no_actionable)


async def _rewrite_single_with_retry(q, style: str, max_retries: int = 2) -> tuple:
    """重写单题，含重试逻辑。
    返回 (success: bool, error_message: str)
    """
    from services.ai_service import ai_service as _ai

    flags = q.audit_flags if hasattr(q, 'audit_flags') and q.audit_flags else []
    if not isinstance(flags, list):
        flags = []

    need_solve = any(f.get("type") in ["wrong_answer", "thinking_in_answer", "style_mismatch"] for f in flags)
    need_diagram = any(f.get("type") == "missing_diagram" for f in flags)

    last_error = ""

    # === 重写解答 ===
    if need_solve:
        for attempt in range(1, max_retries + 1):
            try:
                solution = await _ai.deepseek_solve(
                    subject=q.subject or "", grade=q.grade or "", ocr_text=q.ocr_text or "",
                    knowledge_tags=q.knowledge_tags or [], style_notes=style
                )
                answer_html = solution.get("answer_html", "")
                if answer_html and answer_html.strip():
                    # 自检
                    try:
                        review = await _ai.deepseek_self_review(
                            solution.get("question_html", ""), answer_html,
                            solution.get("standard_answer", ""), q.subject or "", q.grade or ""
                        )
                        final_answer = review.get("answer_html", answer_html)
                        final_question = review.get("question_html", solution.get("question_html", ""))
                    except Exception:
                        final_answer = answer_html
                        final_question = solution.get("question_html", "")

                    async with async_session() as db:
                        q2 = await db.get(Question, q.id)
                        if q2:
                            # 写入前复检：执行间隙用户锁定或改动的题目不得被自动重写覆盖
                            if getattr(q2, "is_resolved", False) or q2.status != "done":
                                logger.info("Auto-rewrite skipped: question %s locked/changed mid-run", q.id)
                                return False, "题目在执行间隙被锁定或修改，已跳过"
                            q2.answer_html = final_answer
                            q2.standard_answer = solution.get("standard_answer", q2.standard_answer)
                            q2.question_html = final_question or q2.question_html
                            q2.question_type = solution.get("question_type", q2.question_type)
                            # 清除已修复标记；题目本身的疑点只能由独立复核或人工确认清除。
                            # 必须基于 q2 当前值过滤：用执行前快照整体回写会覆盖执行间隙新增的标记
                            current_flags = q2.audit_flags if isinstance(q2.audit_flags, list) else []
                            q2.audit_flags = [
                                f for f in current_flags
                                if isinstance(f, dict) and f.get("type") in _PRESERVED_FLAG_TYPES
                            ]
                            q2.error_message = ""  # 清除旧错误
                            await db.commit()
                    return True, ""
                else:
                    last_error = f"Attempt {attempt}: AI returned empty answer_html"
            except Exception as e:
                last_error = f"Attempt {attempt}: {str(e)}"
                if attempt < max_retries:
                    await asyncio.sleep(3 * attempt)  # 递增等待
                continue

    # === 生成示意图 ===
    if need_diagram:
        for attempt in range(1, max_retries + 1):
            try:
                from services.diagram_service import diagram_service as _ds
                diagram_desc = f"{q.ocr_text or ''}\n{q.question_html or ''}"
                path = await _ds.generate_diagram(q.id, diagram_desc[:500], 0)
                if path:
                    async with async_session() as db:
                        q2 = await db.get(Question, q.id)
                        if q2:
                            if getattr(q2, "is_resolved", False) or q2.status != "done":
                                logger.info("Auto-rewrite diagram skipped: question %s locked/changed mid-run", q.id)
                                return False, "题目在执行间隙被锁定或修改，已跳过"
                            diags = q2.diagrams or []
                            if isinstance(diags, list):
                                diags.append({"path": path, "place": "question"})
                                q2.diagrams = diags
                            # 示意图已补上：必须同步移除 missing_diagram 标记，
                            # 否则该题每晚都会再生成一张新图无限堆积
                            current_flags = q2.audit_flags if isinstance(q2.audit_flags, list) else []
                            q2.audit_flags = [
                                f for f in current_flags
                                if not (isinstance(f, dict) and f.get("type") == "missing_diagram")
                            ]
                            q2.error_message = ""
                            await db.commit()
                    return True, ""
                else:
                    last_error = f"Attempt {attempt}: diagram generation returned empty path"
            except Exception as e:
                last_error = f"Attempt {attempt}: {str(e)}"
                if attempt < max_retries:
                    await asyncio.sleep(3 * attempt)
                continue

    if not need_solve and not need_diagram:
        return True, ""  # 无需要修复的标记，视为成功

    return False, last_error


# ===== 每日一言 =====

async def _generate_daily_quote():
    """生成今日一言并存入 system_messages（幂等：一天只生成一次）"""
    try:
        # 幂等基准与调度器一致（UTC+8 日）：msg.time 存 UTC，比对时换算到 UTC+8 日期。
        # 直接拿 UTC 日期比对会在北京时间 0:00-8:00 窗口错位一天，导致一言隔天缺席。
        now_utc8 = datetime.now(timezone.utc) + timedelta(hours=8)
        today_str = now_utc8.strftime("%Y-%m-%d")
        existing = _load_system_messages()
        for msg in existing:
            if msg.get("type") == "daily_quote":
                msg_time = msg.get("time", "")
                try:
                    d = datetime.fromisoformat(msg_time)
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=timezone.utc)
                    if (d + timedelta(hours=8)).strftime("%Y-%m-%d") == today_str:
                        logger.info("Today's daily_quote already exists, skipping")
                        return
                except (ValueError, TypeError):
                    continue

        from routers.system import _gen_quote
        data = await _gen_quote()
        add_system_message("daily_quote", "每日一言", str(data.get("quote", data) if isinstance(data, dict) else data))
    except Exception as e:
        logger.warning("Failed to generate daily quote: %s", e)


# ===== 临时文件清理 =====

def _cleanup_diagram_dirs():
    """清理 storage/questions/ 下超过7天的临时 diagram 目录 (chat_*)"""
    import shutil, time
    from config import QUESTIONS_DIR
    cutoff = time.time() - 7 * 24 * 3600
    count = 0
    for name in os.listdir(QUESTIONS_DIR):
        if not name.startswith('chat_'):
            continue
        d = os.path.join(QUESTIONS_DIR, name)
        if os.path.isdir(d):
            if os.path.getmtime(d) < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                count += 1
    if count:
        logger.info("Cleaned %d expired diagram dirs from %s", count, QUESTIONS_DIR)


# ===== 标签统一巡检 =====

async def run_tag_unification_patrol():
    """夜间标签统一巡检：收集全库标签，调用 AI 归并，保存映射。"""
    from config import ENABLE_TAG_UNIFICATION
    if not ENABLE_TAG_UNIFICATION:
        return {"groups_count": 0, "tags_count": 0}

    logger.info("=== Nightly tag unification patrol started ===")
    tags = set()

    async with async_session() as db:
        # 收集题目标签
        r = await db.execute(select(Question.knowledge_tags))
        for row in r.fetchall():
            tag_list = row[0] if row[0] else []
            if isinstance(tag_list, list):
                for tag in tag_list:
                    if isinstance(tag, str) and tag.strip():
                        tags.add(tag.strip())

        # 收集笔记标签
        r = await db.execute(select(Note.knowledge_tags))
        for row in r.fetchall():
            tag_list = row[0] if row[0] else []
            if isinstance(tag_list, list):
                for tag in tag_list:
                    if isinstance(tag, str) and tag.strip():
                        tags.add(tag.strip())

    tags = sorted(tags)
    logger.info("Collected %d unique tags for unification", len(tags))

    if not tags:
        logger.info("=== Nightly tag unification patrol completed: no tags ===")
        return {"groups_count": 0, "tags_count": 0}

    try:
        result = await ai_service.deepseek_unify_tags(tags)
        groups = result.get("groups", []) if isinstance(result, dict) else []
    except Exception as e:
        logger.error("Tag unification AI call failed: %s", e)
        return {"groups_count": 0, "tags_count": len(tags)}

    try:
        from services.tag_unification_service import update_unification_map
        update_unification_map(groups)
    except Exception as e:
        logger.error("Failed to update tag unification map: %s", e)
        return {"groups_count": len(groups), "tags_count": len(tags)}

    logger.info("=== Nightly tag unification patrol completed: %d groups ===", len(groups))
    return {"groups_count": len(groups), "tags_count": len(tags)}


# ===== 定时调度 =====

_scheduler_running = False
_last_audit_date = None
_last_rewrite_date = None
_last_tag_unify_date = None
_last_quote_date = None
_last_cleanup_date = None

async def start_nightly_scheduler():
    """启动夜间定时调度 (UTC+8)
    1:00-1:59 → 全库巡检
    2:00-2:49 → 标签统一；2:50 起生成每日一言
    3:00-3:59 → 自动重写
    4:00-4:59 → 清理过期 diagram 临时目录（保留最近7天）
    其余时间静默。
    采用"时间窗口 + 每日一次"判断：轮询相位漂移或单次任务超过 60s，
    也只会推迟而不会整天错过；夜间巡逻开关关闭时不执行巡检/标签/重写。
    """
    global _scheduler_running, _last_audit_date, _last_rewrite_date, _last_tag_unify_date
    global _last_quote_date, _last_cleanup_date
    if _scheduler_running:
        return
    _scheduler_running = True
    logger.info("Nightly scheduler started (UTC+8)")
    try:
        while True:
            # 外层守护循环：内层任何未预期异常只暂停 30s 后重启，不让调度器永久退出
            try:
                while True:
                    now_utc8 = datetime.now(timezone.utc) + timedelta(hours=8)
                    hour = now_utc8.hour
                    minute = now_utc8.minute
                    today_str = now_utc8.strftime("%Y-%m-%d")
                    try:
                        patrol_enabled = bool(load_settings().get("night_patrol_enabled", True))
                    except Exception:
                        patrol_enabled = True

                    # 1-2 点窗口开始巡检（每天只执行一次；大题库可能跑超过 1 小时，
                    # 窗口放宽到 2 点前，后续任务的日期守卫保证不重复执行）
                    if patrol_enabled and hour in (1, 2) and _last_audit_date != today_str:
                        _last_audit_date = today_str
                        logger.info("Nightly audit triggered at %s", now_utc8)
                        add_system_message("system", "夜间巡检开始", "AI正在全库巡检题目质量，请稍后查看结果。")
                        try:
                            await run_full_audit()
                        except Exception as e:
                            logger.error("Nightly audit failed: %s", e)
                            add_system_message("system", "夜间巡检失败", f"巡检过程出现异常: {str(e)[:200]}")

                    # 2-3 点窗口标签统一（每天只执行一次）
                    if patrol_enabled and hour in (2, 3) and _last_tag_unify_date != today_str:
                        _last_tag_unify_date = today_str
                        logger.info("Tag unification patrol triggered at %s", now_utc8)
                        try:
                            result = await run_tag_unification_patrol()
                            add_system_message("system", "标签统一完成",
                                               f"共整理 {result.get('tags_count', 0)} 个标签，"
                                               f"发现 {result.get('groups_count', 0)} 组同义标签。")
                        except Exception as e:
                            logger.error("Tag unification patrol failed: %s", e)
                            add_system_message("system", "标签统一失败",
                                               f"标签统一过程出现异常: {str(e)[:200]}")

                    # 2:50 后生成每日一言（每天只执行一次，独立于巡检是否成功）
                    if (hour == 2 and minute >= 50 or hour == 3) and _last_quote_date != today_str:
                        _last_quote_date = today_str
                        try:
                            await _generate_daily_quote()
                        except Exception as e:
                            logger.warning("Daily quote generation failed: %s", e)

                    # 3-4 点窗口自动重写（每天只执行一次）
                    if patrol_enabled and hour in (3, 4) and _last_rewrite_date != today_str:
                        _last_rewrite_date = today_str
                        logger.info("Auto-rewrite triggered at %s", now_utc8)
                        try:
                            await run_auto_rewrite()
                        except Exception as e:
                            logger.error("Auto-rewrite failed: %s", e)
                            add_system_message("system", "自动重写失败",
                                               f"重写过程出现异常: {str(e)[:200]}")

                    # 4-5 点窗口清理过期 diagram 临时目录（每天只执行一次）
                    if hour in (4, 5) and _last_cleanup_date != today_str:
                        _last_cleanup_date = today_str
                        try:
                            _cleanup_diagram_dirs()
                        except Exception as e:
                            logger.warning("Diagram cleanup failed: %s", e)

                    await asyncio.sleep(60)  # 每分钟检查一次
            except asyncio.CancelledError:
                raise  # 取消直接上抛，交给外层收尾
            except Exception as e:
                # 未预期异常不得让调度器永久退出：记录后 30s 重启内层循环
                logger.error("Nightly scheduler error (restarting in 30s): %s", e)
                await asyncio.sleep(30)
    except asyncio.CancelledError:
        logger.info("Nightly scheduler cancelled")
    finally:
        _scheduler_running = False
