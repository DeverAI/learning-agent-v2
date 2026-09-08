"""自动笔记整理归类服务

功能：
1. OCR 文字提取（GLM-4V-Flash 视觉）
2. AI 自动分类（学科、知识点标签、标题、结构化内容）
3. 自动整合（去重、合并同知识点笔记）
4. 与画图系统接入（模型示意图 spec）

所有 AI 调用统一通过 ai_service 管理，享受统一的错误处理和重试机制。
"""

import asyncio
import json
import time
import re
from collections import Counter
from typing import Optional

from logger import get_logger
from models.database import async_session

logger = get_logger()


def build_knowledge_tree(notes) -> dict:
    """把笔记整理成稳定、可直接绘制的学科 → 知识点 → 笔记树。"""
    nodes = []
    edges = []
    seen = set()

    def add_node(node_id: str, label: str, node_type: str, note_id: str = ""):
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"id": node_id, "label": label or "未命名", "type": node_type, "note_id": note_id})

    for note in notes or []:
        subject = ((getattr(note, "subject", "") or "").strip() or "未分类")
        subject_id = "subject:" + subject
        add_node(subject_id, subject, "subject")
        tags = [str(t).strip() for t in (getattr(note, "knowledge_tags", None) or []) if str(t).strip()]
        if not tags:
            tags = ["未标注"]
        for tag in tags:
            tag_id = subject_id + ":tag:" + tag
            add_node(tag_id, tag, "tag")
            edges.append({"from": subject_id, "to": tag_id})
            note_id = str(getattr(note, "id", ""))
            note_node_id = "note:" + note_id
            add_node(note_node_id, getattr(note, "title", "") or "未命名笔记", "note", note_id)
            edges.append({"from": tag_id, "to": note_node_id})

    # 去掉同一笔记多个标签导致的重复边，保留输入顺序便于前端稳定渲染。
    unique_edges = []
    edge_seen = set()
    for edge in edges:
        key = (edge["from"], edge["to"])
        if key not in edge_seen:
            edge_seen.add(key)
            unique_edges.append(edge)
    return {"nodes": nodes, "edges": unique_edges}


async def ocr_image_async(base64_image: str) -> Optional[str]:
    """笔记图片 OCR（模型分工：MiMo 全模态优先，ZhipuAI/自定义 scope 回退）"""
    from services.ai_service import ai_service
    try:
        prompt = "请详细识别这张图片中的所有文字内容，保持原有格式。如果是题目，请保留题目编号和选项。如果是笔记，请保留层次结构。"
        if ai_service.custom_scope_map.get("notes_ocr") or ai_service._get_custom_openai("notes_ocr"):
            raw = await ai_service._chat_inner(
                [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
                ]}], ai_service.ds_model, temperature=0.3, max_tokens=8192, scope="notes_ocr"
            )
        elif ai_service.xm_key:
            # 功能检查轮 M3：只配小米 key 的用户此前笔记图片必 400
            raw = await ai_service.xiaomi_vision(
                base64_image, prompt, "image/jpeg", parse_json=False
            )
        else:
            raw = await ai_service.zhipuai_vision(
                base64_image, prompt, "image/jpeg", parse_json=False
            )
        if isinstance(raw, str):
            return raw.strip()
        return str(raw) if raw else None
    except Exception as e:
        logger.error("OCR failed via ai_service: %s", e)
        return None


async def classify_and_structure_async(raw_text: str, subject_hint: str = "") -> dict:
    """AI 自动分类并结构化笔记内容（异步，通过 ai_service 统一管理）

    Returns:
        {
            "subject": "物理",
            "grade": "初中",
            "knowledge_tags": ["牛顿第一定律", "惯性"],
            "title": "牛顿第一定律与惯性",
            "content": "## 核心概念\\n...\\n## 典型例题\\n...",
            "typical_questions": [{"stem": "...", "answer": "...", "analysis": "..."}],
        }
    """
    from services.ai_service import ai_service
    MAX_INPUT = 4000
    truncated = raw_text[:MAX_INPUT]
    if len(raw_text) > MAX_INPUT:
        logger.warning("Input text truncated from %d to %d chars for classification", len(raw_text), MAX_INPUT)

    # 加载经典模型上下文
    classic_models_hint = ""
    try:
        classic_models = _load_classic_models_cache()
        if classic_models:
            classic_models_hint = f"题库中已收录的经典模型: {', '.join(classic_models[:10])}。如果笔记涉及这些模型，请在knowledge_tags中精确标注。"
    except Exception:
        pass

    system_prompt = """你是一个专业的学习笔记整理助手。请根据用户输入的文字内容，将其整理为一篇结构化的学习笔记。

输出格式（纯 JSON，不要 markdown 包裹）：
{
  "subject": "学科名称（数学/物理/化学/生物/英语/语文/历史/地理/政治）",
  "grade": "学段（小学/初中/高中/大学）",
  "knowledge_tags": ["知识点标签1", "知识点标签2", ...],
  "title": "笔记标题",
  "content": "## 核心概念\\n...\\n## 记忆技巧\\n...\\n## 易错点\\n...",
  "typical_questions": [
    {"stem": "题目正文", "answer": "答案", "analysis": "解析"}
  ]
}

规则：
- content 用 Markdown 格式，包含 ## 二级标题分层
- knowledge_tags 3-8 个，参考经典模型名称精确标注
- typical_questions 0-3 个（如果没有就是空数组）
- 如果是纯文字无题目，typical_questions 留空
- 如果输入已经是笔记，保持原有结构并优化层次
- 只输出 JSON，不要输出其他文字"""
    if classic_models_hint:
        system_prompt += "\n" + classic_models_hint

    user_msg = truncated
    if subject_hint:
        user_msg = f"（疑似学科：{subject_hint}）\n\n{truncated}"

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]
        if ai_service.custom_scope_map.get("notes_classify") or ai_service._get_custom_openai("notes_classify"):
            result_txt = await ai_service.deepseek_chat(
                messages, max_tokens=4096, scope="notes_classify"
            )
        else:
            result_txt = await ai_service.zhipuai_chat(
                messages, model="glm-4-flash", max_tokens=2048
            )
    except Exception as e:
        logger.warning("GLM classify failed via ai_service: %s", e)
        result_txt = None

    if not result_txt:
        result = _fallback_classify(raw_text, subject_hint)
    else:
        # Parse JSON response
        try:
            json_str = result_txt.strip()
            if json_str.startswith("```"):
                json_str = re.sub(r'^```\w*\n?', '', json_str)
                json_str = re.sub(r'\n?```$', '', json_str)
            result = json.loads(json_str)
            # 模型可能返回 null/数组/字符串/数字等合法 JSON 非对象，统一降级
            if not isinstance(result, dict):
                logger.warning("AI classification result is not an object: %r", result_txt[:200])
                result = _fallback_classify(raw_text, subject_hint)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse AI classification result: %s", result_txt[:200])
            result = _fallback_classify(raw_text, subject_hint)

    # 分类输入会按模型上下文上限截断，但持久化内容必须保留完整原文。
    if len(raw_text) > MAX_INPUT:
        structured = str(result.get("content", "") or "").strip()
        result["content"] = (
            structured
            + "\n\n---\n\n## 原始材料（完整保留）\n\n"
            + raw_text
        ).strip()

    return result


def _fallback_classify(raw_text: str, subject_hint: str) -> dict:
    """无 AI 时的回退分类"""
    text_lower = raw_text.lower()
    subject = subject_hint or "通用"
    # Simple heuristics
    math_keywords = ["方程", "函数", "三角", "几何", "概率", "导数", "积分", "向量", "不等式"]
    phys_keywords = ["力", "速度", "加速度", "电场", "磁场", "电路", "光", "热", "压强", "浮力"]
    chem_keywords = ["化学式", "反应", "元素", "酸", "碱", "盐", "氧化", "还原", "mol", "滴定"]
    bio_keywords = ["细胞", "基因", "DNA", "蛋白质", "酶", "光合", "呼吸", "进化", "遗传"]

    scores = {"数学": 0, "物理": 0, "化学": 0, "生物": 0}
    for kw in math_keywords:
        if kw in text_lower: scores["数学"] += 1
    for kw in phys_keywords:
        if kw in text_lower: scores["物理"] += 1
    for kw in chem_keywords:
        if kw in text_lower: scores["化学"] += 1
    for kw in bio_keywords:
        if kw in text_lower: scores["生物"] += 1

    best = max(scores, key=scores.get)
    if scores[best] > 0:
        subject = best

    title = raw_text[:30].replace("\n", " ").strip().lstrip("#").strip()
    if len(raw_text) > 30:
        title += "..."

    return {
        "subject": subject,
        "grade": "通用",
        "knowledge_tags": [],
        "title": title,
        "content": raw_text,
        "typical_questions": [],
    }


def merge_notes(new_note: dict, existing_notes: list[dict], overlap_threshold: float = 0.5) -> tuple[int, Optional[str]]:
    """判断新笔记应合并到哪篇已有笔记

    Args:
        new_note: 新笔记 {"knowledge_tags": [...], "title": "..."}
        existing_notes: 已有笔记列表
        overlap_threshold: 重叠阈值

    Returns:
        (-1, None)  → 新建笔记
        (index, merged_content) → 合并到第 index 篇
    """
    new_tags = set(new_note.get("knowledge_tags", []))
    if not new_tags:
        return -1, None

    best_idx = -1
    best_overlap = 0.0

    for i, existing in enumerate(existing_notes):
        new_subject = str(new_note.get("subject", "")).strip()
        existing_subject = str(existing.get("subject", "")).strip()
        if new_subject and existing_subject and new_subject != existing_subject:
            continue
        existing_tags = set(existing.get("knowledge_tags", []))
        if not existing_tags:
            continue
        intersection = new_tags & existing_tags
        union = new_tags | existing_tags
        overlap = len(intersection) / len(union) if union else 0

        if overlap > best_overlap:
            best_overlap = overlap
            best_idx = i

    if best_overlap >= overlap_threshold:
        # Generate merged content
        existing_content = existing_notes[best_idx].get("content", "")
        new_content = new_note.get("content", "")
        # Simple merge: append new content with separator
        merged = existing_content
        if new_content and new_content not in existing_content:
            merged += f"\n\n---\n\n## 补充内容\n\n{new_content}"
        return best_idx, merged

    return -1, None


# === 笔记引用与自动建库 ===

_REF_MARKER_RE = re.compile(r"\[\[(QUESTION|BANK):([^\]]+)\]\]", re.IGNORECASE)


def resolve_note_references(content: str, references: list[dict]) -> str:
    """将 content 中的 [[QUESTION:xxx]] / [[BANK:xxx]] 替换为 Markdown 链接。

    如果 references 中不存在对应引用，保留原标记。
    """
    if not content:
        return content or ""

    refs = {}
    for ref in (references or []):
        if not isinstance(ref, dict):
            continue
        ref_type = str(ref.get("type", "")).upper()
        key = ref.get("id") if ref_type == "QUESTION" else ref.get("name")
        if key is not None:
            refs[(ref_type, str(key).strip())] = ref

    def _replace(m: re.Match) -> str:
        ref_type = m.group(1).upper()
        key = m.group(2).strip()
        ref = refs.get((ref_type, key))
        if not ref:
            return m.group(0)
        if ref_type == "QUESTION":
            qid = str(ref.get("id", key)).strip()
            name = str(ref.get("name") or f"题目{qid}").strip()
            return f"[{name}](/api/questions/{qid})"
        if ref_type == "BANK":
            name = str(ref.get("name", key)).strip()
            return f"[{name}](/api/banks/{name})"
        return m.group(0)

    return _REF_MARKER_RE.sub(_replace, content)


async def auto_create_banks_from_notes(db_session=None, threshold: int = 5) -> dict:
    """根据笔记/题目中的高频知识点标签自动创建题库。

    统计所有 Question.knowledge_tags 与 Note.knowledge_tags 中各标签出现次数，
    对超过 threshold 且当前不存在的题库名，调用 AI 生成题库名，并将包含该标签的题目归入新题库。
    """
    from sqlalchemy import select, func, or_, Text
    from models.database import async_session
    from models.models import Note, Question
    from services.ai_service import ai_service

    own_session = False
    if db_session is None:
        db_session = async_session()
        own_session = True

    created = []
    try:
        tag_counts = Counter()
        tag_to_questions: dict[str, list] = {}

        # 统计笔记标签
        note_rows = await db_session.execute(select(Note.knowledge_tags))
        for (tags,) in note_rows.all():
            for t in set(tags or []):
                if t:
                    tag_counts[t] += 1

        # 统计题目标签，同时建立标签->题目映射用于建库
        q_rows = await db_session.execute(
            select(Question).where(
                Question.status == "done",
                or_(Question.source_type.is_(None),
                    ~Question.source_type.in_(["search_query", "correction_query"])),
                or_(Question.is_resolved.is_(True), Question.audit_flags.is_(None),
                    ~Question.audit_flags.cast(Text).ilike("%question_challenge_high%")),
            )
        )
        questions = q_rows.scalars().all()
        for q in questions:
            q_tags = set(q.knowledge_tags or [])
            for t in q_tags:
                if not t:
                    continue
                tag_counts[t] += 1
                tag_to_questions.setdefault(t, []).append(q)

        if not tag_counts:
            return {"created": []}

        # 现有题库名
        existing_banks = set()
        bank_rows = await db_session.execute(select(func.distinct(Question.bank)))
        for (b,) in bank_rows.fetchall():
            if b:
                existing_banks.add(b)

        for tag, count in tag_counts.most_common():
            if count < threshold:
                continue
            if tag in existing_banks:
                continue

            bank_name = await ai_service.glm_name_bank(tag)
            if not bank_name or bank_name in existing_banks:
                continue

            qs = tag_to_questions.get(tag, [])
            if not qs:
                # 该标签仅来自笔记（无对应题目），暂不创建空题库
                logger.info("Tag '%s' has no questions, skipping empty bank creation", tag)
                continue

            for q in qs:
                if not q.bank or q.bank == "default":
                    q.bank = bank_name
            updated_count = sum(1 for q in qs if q.bank == bank_name)
            if not updated_count:
                continue
            created.append({
                "tag": tag,
                "bank": bank_name,
                "questions_updated": updated_count,
            })
            existing_banks.add(bank_name)

        if created:
            await db_session.commit()
    except Exception as e:
        logger.exception("auto_create_banks_from_notes failed: %s", e)
        if own_session:
            await db_session.rollback()
        raise
    finally:
        if own_session:
            await db_session.close()

    return {"created": created}


# === 经典模型缓存 ===

def _load_classic_models_cache() -> list[str]:
    """从缓存文件读取已发现的经典模型标签"""
    import json, os
    from config import STORAGE_DIR
    path = os.path.join(STORAGE_DIR, "classic_models.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save_classic_models_cache(models: list[str]):
    """保存经典模型标签到缓存文件"""
    import os
    from config import STORAGE_DIR, _atomic_write_json
    path = os.path.join(STORAGE_DIR, "classic_models.json")
    try:
        _atomic_write_json(path, sorted(set(models)))
    except Exception as exc:
        logger.warning("Failed to save classic model cache: %s", exc)


def render_note_markdown(note: dict) -> str:
    """将笔记渲染为完整 Markdown 文本（供下载/预览）"""
    md = f"# {note.get('title', '无标题')}\n\n"
    md += f"**学科**: {note.get('subject', '')}  |  **学段**: {note.get('grade', '')}\n\n"

    tags = note.get("knowledge_tags", [])
    if tags:
        md += "**知识点**: " + " · ".join(tags) + "\n\n"

    md += "---\n\n"
    md += note.get("content", "") + "\n\n"

    # Typical questions
    questions = note.get("typical_questions", [])
    if questions:
        md += "---\n\n## 典型例题\n\n"
        for i, q in enumerate(questions):
            md += f"### 例题 {i+1}\n\n"
            md += f"**题目**: {q.get('stem', '')}\n\n"
            if q.get("answer"):
                md += f"**答案**: {q.get('answer', '')}\n\n"
            if q.get("analysis"):
                md += f"**解析**: {q.get('analysis', '')}\n\n"

    # Diagram reference
    if note.get("diagram_spec"):
        md += f"\n> [打开模型示意图](/editor?note_id={note.get('id', '')})\n"

    return md
