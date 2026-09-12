"""讲题服务（round 60）：服务器端讲题计划生成 + 整卷连讲（跨题上下文压缩）。

桌面端 LectureEngine 的逻辑后端化：服务器直接查库（不走 HTTP 拉取），
复用 ai_service 的 deepseek_json。跨题压缩：前一题讲完的 summary 作为
"前情提要"注入下一题 prompt（每题只累积摘要，不累积全文——长卷不超限）。
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from logger import get_logger
from models.models import Paper, Question
from services.ai_service import ai_service

logger = get_logger()

SYSTEM_PROMPT = (
    "你是一位擅长讲题的一对一 AI 教师。学生会看着屏幕听你讲解一道题。"
    "请输出 JSON（不要任何额外文字）："
    '{"steps":[{"title":"步骤名","speech":"对学生的口语讲解","placement_hint":"top_right"},'
    '"summary":"本题一句话总结（讲给学生的收尾，10~40 字）"}。'
    "要求：\n"
    "1. steps 3~6 步，教学式递进：题意分析→解题思路→逐步解法→答案核对与得分点→易错提醒（如适用）；\n"
    "2. 只讲这一道题，引用题目已知条件；\n"
    "3. 每步 speech 口语化、可直接朗读（40~120 字），不含 Markdown/LaTeX 定界符；\n"
    "4. 每步 placement_hint 九宫格：top_left/top_center/top_right/mid_left/mid_center/"
    "mid_right/bottom_left/bottom_center/bottom_right（相邻步骤换区域）；\n"
    "5. summary 是讲给学生的收尾句，不是元描述；\n"
    "6. 无法解答时如实说明。"
)
SYSTEM_PROMPT_CONT = (
    SYSTEM_PROMPT
    + "\n7. 这是连续讲题中的一题：开头可以用一句话衔接前情（「刚才我们讲了…现在看下一题」），"
      "不要重复之前已讲的题目内容。"
)

VALID_HINTS = ("top_left", "top_center", "top_right", "mid_left", "mid_center",
               "mid_right", "bottom_left", "bottom_center", "bottom_right")

MAX_QUESTION_CHARS = 4000
MAX_ANSWER_CHARS = 4000


def _clean(text: str) -> str:
    t = str(text or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\\[\(\)\[\]]", "", t)
    t = t.replace("$", "").replace("|", "，")
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def _clean_plan(data) -> tuple[list[dict], str]:
    """规整 AI 输出 → (steps, summary)；无效返回 ([], '')。"""
    if not isinstance(data, dict):
        return [], ""
    steps = []
    for i, s in enumerate(data.get("steps") or []):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title", "") or "讲解").strip()[:30]
        speech = str(s.get("speech", "") or "").strip()[:400]
        if not speech:
            continue
        hint = str(s.get("placement_hint", "") or "").strip()
        if hint not in VALID_HINTS:
            hint = ["top_right", "mid_left", "bottom_right"][i % 3]
        steps.append({"title": title, "speech": speech, "placement_hint": hint})
    summary = str(data.get("summary", "") or "").strip()[:200]
    return steps, summary


def _fallback_plan(question_text: str, standard_answer: str, hint_idx: int = 0) -> tuple[list[dict], str]:
    """降级计划：读题面 + 读标准答案（fail-safe 不沉默）。"""
    hints = ["top_right", "mid_left", "bottom_right"]
    steps = [{"title": "题目", "speech": question_text[:400],
              "placement_hint": hints[hint_idx % 3]}]
    if standard_answer:
        steps.append({"title": "答案", "speech": standard_answer[:400],
                      "placement_hint": hints[(hint_idx + 1) % 3]})
    return steps, ""


def _question_context(q: Question) -> dict:
    """题面/答案/得分点裁到模型上下文上限。

    F14 修复：截断事实必须**可追溯**。原实现在入 prompt 前静默切片、响应里也没有任何标记，
    长题（多问、含题干表格）会被砍掉尾部条件，而 AI 仍会"自信地"讲解并给出收尾句，
    调用方完全无从判断自己看到的是不是完整题面。
    """
    q_text = _clean(q.question_html or q.ocr_text) or "（无题面文本）"
    a_text = _clean(q.standard_answer or q.answer_html)
    s_text = _clean(q.score_points_html)
    truncated = {
        "question": len(q_text) > MAX_QUESTION_CHARS,
        "answer": len(a_text) > MAX_ANSWER_CHARS,
        "score_points": len(s_text) > 1200,
    }
    return {
        "question": q_text[:MAX_QUESTION_CHARS],
        "answer": a_text[:MAX_ANSWER_CHARS],
        "score_points": s_text[:1200],
        "truncated": truncated,
        "truncated_any": any(truncated.values()),
    }


async def build_question_plan(db: AsyncSession, question_id: str,
                              prev_summary: str = "") -> dict:
    """为单题生成讲解计划（含跨题前情提要）。返回 {question_id, steps, summary}。

    AI 失败/解析失败 → 降级两步直读（fail-safe 不沉默）。"""
    q = await db.get(Question, question_id)
    if not q:
        raise ValueError(f"题目不存在: {question_id}")
    ctx = _question_context(q)

    parts = [f"题目：{ctx['question']}"]
    if ctx["answer"]:
        parts.append(f"标准答案/解析：{ctx['answer']}")
    if ctx["score_points"]:
        parts.append(f"得分点：{ctx['score_points']}")
    if prev_summary:
        parts.append(f"前情提要（上一题的收尾，可自然衔接）：{prev_summary}")

    system = SYSTEM_PROMPT_CONT if prev_summary else SYSTEM_PROMPT
    steps, summary = [], ""
    degraded, degraded_reason = False, ""
    try:
        data = await ai_service.deepseek_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": "\n\n".join(parts)}],
            max_tokens=1500, scope="solve")
        steps, summary = _clean_plan(data)
        if not steps:
            raise ValueError("空计划")
    except Exception as e:
        logger.warning("lecture plan failed for %s, fallback: %s", question_id, str(e)[:150])
        steps, summary = _fallback_plan(ctx["question"], ctx["answer"],
                                        hint_idx=len(prev_summary) % 3)
        degraded, degraded_reason = True, str(e)[:120]
    if ctx.get("truncated_any"):
        logger.warning("lecture plan 题面被截断: %s %s", question_id, ctx.get("truncated"))
    # F1 修复：降级必须**可识别**。原实现降级返回与成功路径结构完全同形（都是 steps+summary）、
    # HTTP 仍 200、无任何标记 → 前端照常渲染并朗读，学生听到的"AI 讲解"可能只是题面+标准答案
    # 的原文朗读，而 UI 与日志都分辨不出。这是本仓已登记的 [失败占位伪成功] 形态。
    result = {
        "question_id": question_id, "steps": steps, "summary": summary,
        "degraded": degraded,
        "truncated": ctx.get("truncated", {}),
    }
    if degraded:
        result["degraded_reason"] = degraded_reason
    return result


async def build_paper_plans(db: AsyncSession, paper_id: str) -> dict:
    """整卷连讲计划：按 question_order 逐题生成，跨题摘要压缩传递。"""
    paper = await db.get(Paper, paper_id)
    if not paper:
        raise ValueError(f"试卷不存在: {paper_id}")
    order = paper.question_order or []
    if not order:
        raise ValueError("试卷没有题目")

    plans = []
    prev_summary = ""
    for i, item in enumerate(order):
        qid = item.get("id") if isinstance(item, dict) else item
        number = item.get("number", i + 1) if isinstance(item, dict) else i + 1
        if not qid:
            continue
        try:
            plan = await build_question_plan(db, qid, prev_summary=prev_summary)
        except ValueError as e:
            plans.append({"question_id": qid, "number": number, "error": str(e)[:120],
                          "steps": [], "summary": ""})
            continue
        plan["number"] = number
        plans.append(plan)
        # F17 修复：**不要**在本题摘要为空时沿用更早一题的摘要。
        # 原实现 `or prev_summary` 会在 plan.summary 为空时保留上上题的收尾，却仍以
        # "上一题的收尾"注入下一题 prompt，模型据此衔接会张冠李戴；而降级路径
        # `_fallback_plan` 固定返回 summary=""，正是最容易命中的情形。
        prev_summary = plan.get("summary", "") or ""

    return {"paper_id": paper_id, "title": paper.title,
            "count": len(plans), "plans": plans}


async def list_paper_summaries(db: AsyncSession, limit: int = 50) -> list[dict]:
    """试卷清单（讲题页用）。"""
    rows = (await db.execute(
        select(Paper).order_by(Paper.created_at.desc()).limit(max(1, min(200, limit)))
    )).scalars().all()
    return [{"id": p.id, "title": p.title or "未命名试卷",
             "subject": p.subject or "", "grade": p.grade or "",
             "question_count": len(p.question_ids or [])} for p in rows]


async def list_question_summaries(db: AsyncSession, limit: int = 100) -> list[dict]:
    """已完成题目清单（无试卷时直接讲题）。"""
    rows = (await db.execute(
        select(Question).where(Question.status == "done")
        .order_by(Question.created_at.desc()).limit(max(1, min(200, limit)))
    )).scalars().all()
    return [{"id": q.id, "number": i + 1,
             "title": (_clean(q.question_html or q.ocr_text)[:36] or "未命名题目"),
             "subject": q.subject or "", "grade": q.grade or ""}
            for i, q in enumerate(rows)]
