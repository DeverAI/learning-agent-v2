"""备课：可编辑课稿（Lesson）的生成、切片读取与人工编辑。

对应需求（用户 2026-09 原话）：
- 「产出**可编辑课稿**」
- 「**任何需要讲解相关内容的 AI 都可以查看、切片、读取**」
- 「全自动生成，**人决定怎么走**」
- 「服务器长期存储，用户不删就不删」（无 TTL、无自动清理）

设计来源：`Agent双端架构设计.md` §8.3「产出：课包 Lesson Pack」。

## 两个关键设计决定（都是为了"取消/失败后东西还能用"）

1. **先建骨架，再逐片填充讲解词。**
   生成流程是"先让模型给出全部分片标题 -> 写进 `sections`（`script` 为空）
   -> 再一片一片把讲解词填进去，每填一片就落库"。
   于是任何时刻取消，课稿都是**可读的**：标题全在，已写的讲解词在，
   没写的明确为空（`script == ""`），而不是"什么都没有"或"看起来写完了但是空的"。

2. **切片读取有界且诚实。**
   `slice_lesson()` 永远返回 `total_sections` / `truncated` / `used_chars`，
   并在单个分片超预算时标 `script_truncated=True`。
   调用方据此知道"还有更多"，不会误以为读到的就是全部。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from logger import get_logger, log_error
from models.database import async_session
from models.models import Lesson, Question
from services.ai_service import ai_service

logger = get_logger()

DEFAULT_SECTION_COUNT = 6
MAX_SECTION_COUNT = 16
# 单个分片讲解词的字符上限（生成时约束模型，读取时也据此判断是否被截断）。
#
# 2026-09-11 从 6000 下调到 1500。理由是**实测数据**：生产环境上写一片的模型调用
# 超过 200 秒仍未返回，并且日志里出现过 "AI returned empty content after token limit;
# retrying with max_tokens=8192" —— 一次调用要生成的内容太长，撞上 token 上限后
# 还会**重试一次**，延迟翻倍。1500 字大约是一片 5 分钟讲解的量，够用且不易触顶。
SECTION_SCRIPT_LIMIT = 1500
# 单次生成的最大 token 数。
#
# ## 这个值必须给足，理由是实测出来的（2026-09-11 打原始 API 得到）
#
# 本项目用的 `deepseek-v4-pro` 是**推理模型**：它先产出 `reasoning_content`（思考），
# 再产出 `content`（正文）。同一个备课 prompt 的实测：
#
#   max_tokens=2048 -> reasoning 吃满 2048，content 长度 **0**，finish_reason=length
#   max_tokens=8192 -> reasoning 3336 + content 1404 = 4253，finish_reason=stop  ✓
#
# 也就是说：**写 1500 字的课稿，光"想"就要 3300+ tokens**。
# 预算给小了，正文一个 token 都轮不到，直接返回空 —— 表现为"备课一片都写不出来"。
#
# 曾经按"6000 字太长会撞上限"的直觉把它从 4096 **下调**到 2048，
# 方向完全反了：撞上限的是 reasoning，不是正文。**不能按正文长度估算这个值。**
SECTION_MAX_TOKENS = 8192
# 单次写片的硬上限（秒）。超过就放弃这一片并留空，不让整节课被一步拖死。
SECTION_TIMEOUT = 120


class LessonNotFound(Exception):
    pass


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# 选材
# --------------------------------------------------------------------------

async def pick_source_questions(params: dict, limit: int = 8) -> list[dict]:
    """按入参挑选备课要讲的题目。

    优先 `question_ids`（明确指定），其次 `paper_id`（整卷），
    最后按 `topic` 在题库里找。**找不到就返回空列表**，
    由调用方决定怎么向用户交代 —— 不在这里偷偷换成"随便几道题"。
    """
    ids = params.get("question_ids") or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",") if x.strip()]
    async with async_session() as db:
        rows: list[Question] = []
        if ids:
            r = await db.execute(select(Question).where(Question.id.in_(list(ids)[:50])))
            by_id = {q.id: q for q in r.scalars().all()}
            rows = [by_id[i] for i in ids if i in by_id]
        elif params.get("paper_id"):
            r = await db.execute(select(Question).where(
                Question.paper_id == str(params["paper_id"])))
            rows = list(r.scalars().all())
        else:
            topic = str(params.get("topic") or "").strip()
            if not topic:
                return []
            from sqlalchemy import Text, or_
            r = await db.execute(
                select(Question).where(
                    Question.status == "done",
                    or_(Question.ocr_text.contains(topic, autoescape=True),
                        Question.question_html.contains(topic, autoescape=True),
                        Question.knowledge_tags.cast(Text).contains(topic, autoescape=True)),
                ).order_by(Question.updated_at.desc()).limit(limit)
            )
            rows = list(r.scalars().all())
        out = []
        for q in rows[:limit]:
            out.append({
                "id": q.id,
                "subject": q.subject or "",
                "grade": q.grade or "",
                "text": (q.ocr_text or q.question_html or "")[:400],
                "tags": list(q.knowledge_tags or []),
            })
        return out


# --------------------------------------------------------------------------
# 创建与生成
# --------------------------------------------------------------------------

async def create_lesson(params: dict, title: str = "") -> dict:
    """建一份空课稿骨架（还没生成内容）。返回只读 dict。"""
    topics = params.get("topics") or ([params["topic"]] if params.get("topic") else [])
    if isinstance(topics, str):
        topics = [t.strip() for t in topics.split(",") if t.strip()]
    lesson = Lesson(
        title=title or (str(params.get("topic") or "").strip() or "未命名课稿"),
        subject=str(params.get("subject") or "")[:64],
        grade=str(params.get("grade") or "")[:64],
        topics=list(topics)[:20],
        source_question_ids=list(params.get("question_ids") or [])[:50],
        source_paper_id=str(params.get("paper_id") or ""),
        sections=[],
        status="draft",
        origin=str(params.get("origin") or "auto"),
    )
    async with async_session() as db:
        db.add(lesson)
        await db.commit()
        await db.refresh(lesson)
        return _lesson_brief(lesson)


async def generate_lesson(lesson_id: str, ckpt=None, *,
                          section_count: int = DEFAULT_SECTION_COUNT) -> dict:
    """生成课稿：先建标题骨架，再逐片填讲解词。

    `ckpt` 是 `background_agent.Checkpoint`；为 None 时表示同步执行（不起后台任务），
    此时不做取消检查。每填完一片就落库，因此中途取消后课稿仍然可读、可续写。
    """
    section_count = max(2, min(int(section_count or DEFAULT_SECTION_COUNT), MAX_SECTION_COUNT))

    # 先把总步数告诉检查点：选材 + 规划 = 2 步固定开销，再加每一片讲解词。
    # 不设的话 progress 会恒为 0（"跑了很久但进度一直是 0"比不显示进度更糟）。
    if ckpt is not None:
        ckpt.set_total(section_count + 2)

    async with async_session() as db:
        lesson = await db.get(Lesson, lesson_id)
        if not lesson:
            raise LessonNotFound(lesson_id)
        subject, grade = lesson.subject, lesson.grade
        topics = list(lesson.topics or [])
        title = lesson.title
        source_ids = list(lesson.source_question_ids or [])
        existing = list(lesson.sections or [])

    # ---- 1) 选材 ----
    if ckpt:
        await ckpt.tick("挑选要讲的题目")
    sources = await pick_source_questions(
        {"question_ids": source_ids} if source_ids else {"topic": (topics[0] if topics else title)},
    )

    # ---- 2) 骨架 ----
    if not existing:
        if ckpt:
            await ckpt.tick("规划课稿结构")
        outline = await _plan_outline(title, subject, grade, topics, sources, section_count)
        existing = [
            {
                "index": i,
                "heading": h,
                "script": "",
                "board_ops": [],
                "question_ids": [],
                "duration_sec": 0,
                "checkpoint": None,
            }
            for i, h in enumerate(outline)
        ]
        async with async_session() as db:
            lesson = await db.get(Lesson, lesson_id)
            if not lesson:
                raise LessonNotFound(lesson_id)
            lesson.sections = existing
            lesson.updated_at = _utcnow()
            await db.commit()

    # ---- 3) 逐片填讲解词（每片落库）----
    filled = 0
    skipped = 0
    for i, sec in enumerate(list(existing)):
        if sec.get("script"):
            skipped += 1
            continue
        if ckpt:
            await ckpt.tick(f"写第 {i + 1} 片：{str(sec.get('heading') or '')[:24]}")
        body = await _write_section(
            title, subject, grade, sec.get("heading", ""), topics, sources,
        )
        if not body:
            # 写不出来就明确留空并记一条错误，不塞占位文字冒充"写好了"
            log_error("lesson.generate", f"section {i} of lesson {lesson_id} produced empty script")
            continue
        async with async_session() as db:
            lesson = await db.get(Lesson, lesson_id)
            if not lesson:
                raise LessonNotFound(lesson_id)
            secs = list(lesson.sections or [])
            if i < len(secs):
                secs[i] = {**secs[i], "script": body,
                           "duration_sec": _estimate_duration(body)}
            lesson.sections = secs
            lesson.updated_at = _utcnow()
            await db.commit()
        filled += 1

    async with async_session() as db:
        lesson = await db.get(Lesson, lesson_id)
        if not lesson:
            raise LessonNotFound(lesson_id)
        secs = list(lesson.sections or [])
        empty = sum(1 for s in secs if not s.get("script"))
        result = {
            "lesson_id": lesson_id,
            "title": lesson.title,
            "total_sections": len(secs),
            "filled_now": filled,
            "already_filled": skipped,
            "empty_sections": empty,
            "source_question_count": len(sources),
        }
        if empty:
            result["note"] = (f"还有 {empty} 片没有讲解词（生成未完成或被取消）。"
                              f"可再次续跑补齐。")
        return result


async def _plan_outline(title: str, subject: str, grade: str, topics: list,
                        sources: list, section_count: int) -> list[str]:
    """让模型给出课稿的分片标题。失败时回退到基于题干/知识点的朴素结构。"""
    src_txt = "\n".join(f"- {s['text'][:120]}" for s in sources[:8]) or "（本次没有指定题目）"
    prompt = (
        f"你在为{grade or '中学'}{subject or ''}备一节课，课稿标题：{title}。\n"
        f"知识点：{', '.join(topics) or '未指定'}\n"
        f"参考题目：\n{src_txt}\n\n"
        f"请把这个主题拆成 {section_count} 个**有先后顺序**的讲解分片，"
        f"每片一个短标题（不超过 16 字）。\n"
        '只输出 JSON：{"sections": ["标题1", "标题2", ...]}，不要额外文字。'
    )
    try:
        raw = await ai_service.deepseek_chat(
            [{"role": "user", "content": prompt}], max_tokens=1024, scope="lesson_plan")
        obj = ai_service._extract_json(raw)
        heads = obj.get("sections") if isinstance(obj, dict) else None
        if isinstance(heads, list):
            clean = [str(h).strip()[:40] for h in heads if str(h).strip()]
            if clean:
                return clean[:section_count]
    except Exception as exc:
        logger.warning("lesson outline plan failed: %s", exc)
    # 回退：明确的朴素骨架（不是"假装模型给的"）
    topic = topics[0] if topics else title
    base = [f"{topic}：为什么要学", f"{topic}：核心结论",
            f"{topic}：怎么推导", f"{topic}：典型例题",
            f"{topic}：常见错误", f"{topic}：小结与检查"]
    return base[:section_count]


async def _write_section(title: str, subject: str, grade: str, heading: str,
                        topics: list, sources: list) -> str:
    """写一片的讲解词。失败返回空串（调用方据此留空，不伪造）。"""
    src_txt = "\n".join(f"- {s['text'][:200]}" for s in sources[:5]) or "（无指定题目）"
    prompt = (
        f"你在为{grade or '中学'}{subject or ''}写一份课稿中的**一节**讲解词。\n"
        f"课稿主题：{title}\n本节标题：{heading}\n"
        f"知识点：{', '.join(topics) or '未指定'}\n"
        f"可用例题：\n{src_txt}\n\n"
        "要求：口语化但不啰嗦，面向学生；先讲清「为什么」再讲「怎么做」；"
        "数学公式用 $...$；不要 emoji；不要输出标题本身；"
        f"控制在 {SECTION_SCRIPT_LIMIT} 字以内。直接输出讲解词正文。"
    )
    try:
        body = await asyncio.wait_for(
            ai_service.deepseek_chat(
                [{"role": "user", "content": prompt}], max_tokens=SECTION_MAX_TOKENS,
                scope="lesson_write"),
            timeout=SECTION_TIMEOUT)
        return (body or "").strip()
    except asyncio.TimeoutError:
        # 实测生产环境单片调用超过 200 秒仍未返回。这里给单次调用一个上限，
        # 超时就**明确记为失败并留空**（调用方会记日志），而不是让整个任务被一步拖死。
        # 这里用 wait_for 做硬取消是安全的：被中断的只是一次 HTTP 请求，
        # 本地没有文件/数据库写入处于中途。
        logger.warning("lesson section write timed out after %ss (%s)",
                       SECTION_TIMEOUT, heading)
        return ""
    except Exception as exc:
        logger.warning("lesson section write failed (%s): %s", heading, exc)
        return ""


def _estimate_duration(script: str) -> int:
    """按中文语速粗估讲解时长（秒）。

    这是**估计值**，取值口径写在字段名里（`duration_sec` 由 estimate 产生），
    不假装是实测时长。
    """
    chars = len(script or "")
    # 口语讲课约 4 字/秒
    return max(0, int(chars / 4))


# --------------------------------------------------------------------------
# 切片读取契约
# --------------------------------------------------------------------------

async def slice_lesson(lesson_id: str, *, section_index: Optional[int] = None,
                       offset: int = 0, max_chars: int = 6000,
                       include_empty: bool = True) -> dict:
    """有界地读取课稿（**任何需要讲解内容的 AI 都走这一个入口**）。

    返回体永远带 `total_sections` / `truncated` / `used_chars`：
    调用方能知道"是否还有没读到的部分"，而不是以为读到的是全部。
    """
    max_chars = max(200, min(int(max_chars or 6000), 48000))
    async with async_session() as db:
        lesson = await db.get(Lesson, lesson_id)
        if not lesson:
            raise LessonNotFound(lesson_id)
        secs = list(lesson.sections or [])
        brief = _lesson_brief(lesson)

    if section_index is not None:
        idx = int(section_index)
        if idx < 0 or idx >= len(secs):
            return {**brief, "error": f"分片序号越界：{idx}（共 {len(secs)} 片）",
                    "total_sections": len(secs), "truncated": False,
                    "used_chars": 0, "sections": []}
        window = [(idx, secs[idx])]
    else:
        start = max(0, int(offset or 0))
        window = list(enumerate(secs))[start:]

    used = 0
    out = []
    truncated = False
    for i, sec in window:
        script = str(sec.get("script") or "")
        if not script and not include_empty:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            truncated = True
            break
        piece = script[:remaining]
        piece_truncated = len(piece) < len(script)
        item = {
            "index": i,
            "heading": str(sec.get("heading") or ""),
            "script": piece,
            "script_chars": len(script),
            "script_truncated": piece_truncated,
            "empty": not script,
            "duration_sec": sec.get("duration_sec") or 0,
            "question_ids": list(sec.get("question_ids") or []),
        }
        used += len(piece)
        out.append(item)
        if piece_truncated:
            truncated = True
            break
    if len(out) < len(window):
        truncated = True

    return {
        **brief,
        "total_sections": len(secs),
        "offset": (window[0][0] if window else int(offset or 0)),
        "returned": len(out),
        "used_chars": used,
        "budget_chars": max_chars,
        "truncated": truncated,
        "empty_sections": sum(1 for s in secs if not s.get("script")),
        "sections": out,
    }


# --------------------------------------------------------------------------
# 人工编辑
# --------------------------------------------------------------------------

async def update_section(lesson_id: str, index: int, *, heading: Optional[str] = None,
                         script: Optional[str] = None,
                         question_ids: Optional[list] = None) -> dict:
    """改一片课稿。**只改传进来的字段**，其余原样保留（避免整篇覆盖）。"""
    async with async_session() as db:
        lesson = await db.get(Lesson, lesson_id)
        if not lesson:
            raise LessonNotFound(lesson_id)
        secs = list(lesson.sections or [])
        idx = int(index)
        if idx < 0 or idx >= len(secs):
            raise LessonNotFound(f"{lesson_id}#{index}")
        sec = dict(secs[idx])
        if heading is not None:
            sec["heading"] = str(heading)[:200]
        if script is not None:
            sec["script"] = str(script)
            sec["duration_sec"] = _estimate_duration(sec["script"])
        if question_ids is not None:
            sec["question_ids"] = list(question_ids)[:50]
        secs[idx] = sec
        lesson.sections = secs
        lesson.updated_at = _utcnow()
        await db.commit()
        return _lesson_brief(lesson)


async def set_status(lesson_id: str, status: str) -> dict:
    if status not in ("draft", "final"):
        raise ValueError(f"不支持的课稿状态: {status}")
    async with async_session() as db:
        lesson = await db.get(Lesson, lesson_id)
        if not lesson:
            raise LessonNotFound(lesson_id)
        lesson.status = status
        lesson.updated_at = _utcnow()
        await db.commit()
        return _lesson_brief(lesson)


async def list_lessons(limit: int = 50, offset: int = 0, status: str = "") -> dict:
    limit = max(1, min(int(limit or 50), 200))
    async with async_session() as db:
        q = select(Lesson).order_by(Lesson.updated_at.desc())
        if status:
            q = q.where(Lesson.status == status)
        total = (await db.execute(select(func.count(Lesson.id)))).scalar() or 0
        r = await db.execute(q.offset(max(0, int(offset or 0))).limit(limit))
        rows = r.scalars().all()
        # 这里**不**返回 sections（可能很大），只给摘要 + 完成度
        items = []
        for L in rows:
            secs = list(L.sections or [])
            done = sum(1 for s in secs if s.get("script"))
            items.append({**_lesson_brief(L),
                          "total_sections": len(secs),
                          "filled_sections": done})
        return {"total": total, "returned": len(items), "lessons": items}


async def delete_lesson(lesson_id: str) -> bool:
    async with async_session() as db:
        lesson = await db.get(Lesson, lesson_id)
        if not lesson:
            return False
        await db.delete(lesson)
        await db.commit()
        return True


def _lesson_brief(L: Lesson) -> dict:
    return {
        "lesson_id": L.id,
        "title": L.title,
        "subject": L.subject,
        "grade": L.grade,
        "topics": list(L.topics or []),
        "status": L.status,
        "origin": L.origin,
        "source_question_ids": list(L.source_question_ids or []),
        "source_paper_id": L.source_paper_id or "",
        "created_at": L.created_at.strftime("%Y-%m-%d %H:%M") if L.created_at else "",
        "updated_at": L.updated_at.strftime("%Y-%m-%d %H:%M") if L.updated_at else "",
    }
