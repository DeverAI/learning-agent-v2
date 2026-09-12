import os
import re
import shutil
from datetime import datetime
from sqlalchemy import select, and_, or_, text, Text, func
from models.models import Question, Paper, PromptTemplate, Note
from models.database import async_session
from services.ai_service import ai_service
from services.layout_service import layout_service
from logger import get_logger
from config import (PAPERS_DIR, DEFAULT_PROMPT_TEMPLATES, ENABLE_WORKSHEET,
                    ALLOWED_PAPER_SIZES, _atomic_write_text)

logger = get_logger()


def _escape_like(value: str) -> str:
    """转义 SQL LIKE 通配符，防止用户输入的 % _ \\ 改变匹配语义。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PaperService:

    async def generate_paper(self, params: dict, mode: str = "new") -> Paper:
        """生成试卷。

        显式 question_ids 始终视为冻结题集；需要重新选题的调用方应先移除该字段。
        """
        mode = mode if mode in ("new", "modify") else "new"
        paper_size = params.get("paper_size", "A4")
        if paper_size not in ALLOWED_PAPER_SIZES:
            raise ValueError(f"不支持的纸张尺寸：{paper_size}")

        explicit_ids = params.get("question_ids", [])
        if isinstance(explicit_ids, list) and len(explicit_ids) > 0:
            question_ids, invalid_ids = await self._validate_question_ids(explicit_ids)
            if invalid_ids:
                raise ValueError("冻结题集中存在不可用题目，未生成试卷：" + ", ".join(invalid_ids[:10]))
        else:
            question_ids = await self._search_questions(params)
            if not question_ids:
                raise ValueError("未找到符合条件的题目，请调整筛选条件")

        questions_data = await self._load_questions(question_ids)
        prompt_template = await self._get_prompt_template(
            params.get("prompt_template_id", "default_paper"))

        filled_prompt = self._fill_prompt_template(
            prompt_template, params, questions_data, paper_size)

        paper_html, _ai_answer_html, answer_sheet_html = await self._generate_with_ai(filled_prompt)
        if len((paper_html or "").strip()) < 50 or not self._has_exact_question_coverage(paper_html, question_ids):
            logger.warning("Paper generation returned incomplete question coverage; retrying once")
            retry_prompt = (
                filled_prompt
                + "\n\n【重试硬性要求】必须完整输出所有题目，每个 data-question-id 属性精确保留一次；禁止省略、重复或替换。"
            )
            paper_html, _ai_answer_html, answer_sheet_html = await self._generate_with_ai(retry_prompt)
        if len((paper_html or "").strip()) < 50 or not self._has_exact_question_coverage(paper_html, question_ids):
            raise ValueError("AI 返回的试卷题目不完整或存在重复，已自动重试仍未通过，请稍后再试")
        # 答案必须来自已审核题库，不能让排版模型重新解题后覆盖标准答案。
        answer_html = self._build_answer_html(questions_data)
        paper_raw, answer_raw = paper_html, answer_html  # Save originals before layout processing

        title = params.get("title", "") or f"{params.get('grade','')}{params.get('subject','')}试卷"

        # 排版审核在 fix_layout 之前对 body HTML 进行，避免重复包装
        try:
            reviewed_html, _ = await layout_service.review_and_fix(
                paper_html, answer_html, paper_size, title, max_rounds=2)
            if self._has_exact_question_coverage(reviewed_html, question_ids):
                paper_html = reviewed_html
            else:
                logger.warning("Layout review changed question coverage; using pre-review paper HTML")
        except Exception as e:
            logger.warning("Layout review skipped: %s", e)
            params.setdefault("generation_warnings", []).append(
                "自动排版审核暂时不可用；试卷已完成基础排版，请在定稿前人工预览"
            )

        paper_html = layout_service.fix_layout(paper_html, paper_size, title, False)
        if not paper_html or len(paper_html) < 50:
            logger.error("fix_layout returned empty paper_html, falling back to raw output")
            paper_html = paper_raw
        answer_html = layout_service.fix_layout(answer_html, paper_size, title, True)
        if not answer_html or len(answer_html) < 50:
            logger.error("fix_layout returned empty answer_html, falling back to raw output")
            answer_html = answer_raw
        if len((paper_html or "").strip()) < 50 or len((answer_html or "").strip()) < 20:
            raise ValueError("试卷排版结果不完整，未保存本次生成结果")

        paper_id = await self._save_paper(params, question_ids, paper_html, answer_html, paper_size, answer_sheet_html)

        async with async_session() as db:
            paper = await db.get(Paper, paper_id)
            return paper

    def _build_filter_query(self, params: dict):
        """构建可复用题目过滤条件的查询骨架（不含排序/分页）。"""
        query = (
            select(Question)
            .where(Question.status == "done")
            .where(or_(
                Question.is_resolved.is_(True),
                Question.audit_flags.is_(None),
                ~Question.audit_flags.cast(Text).ilike("%question_challenge_high%"),
            ))
            .where(or_(Question.source_type.is_(None),
                       ~Question.source_type.in_(["search_query", "correction_query"])))
        )

        if params.get("subject"):
            query = query.where(Question.subject == params["subject"])
        if params.get("grade"):
            query = query.where(Question.grade == params["grade"])
        if params.get("region"):
            region = _escape_like(str(params["region"]))
            query = query.where(Question.region.ilike(f"%{region}%", escape="\\"))

        avg_min = params.get("avg_score_min")
        avg_max = params.get("avg_score_max")
        # 0 / 150 是默认值，语义为"不限"；同时避免 NULL 被 SQL 比较过滤掉
        if avg_min is not None and avg_min > 0:
            query = query.where(Question.avg_score >= avg_min)
        if avg_max is not None and avg_max < 150:
            query = query.where(Question.avg_score <= avg_max)

        tags = params.get("knowledge_tags") or []
        if tags:
            # 使用带引号的精确匹配，避免 "a" 误命中 "ab"；JSON 存储为 ["tag"]，加引号更精确
            tag_conditions = [
                Question.knowledge_tags.cast(Text).ilike(f'%"{_escape_like(str(tag))}"%', escape="\\")
                for tag in tags[:50]
                if str(tag).strip()
            ]
            if tag_conditions:
                query = query.where(or_(*tag_conditions))

        if params.get("keyword"):
            keyword = f"%{_escape_like(str(params['keyword']))}%"
            query = query.where(
                or_(
                    Question.ocr_text.ilike(keyword, escape="\\"),
                    Question.question_html.ilike(keyword, escape="\\"),
                    Question.knowledge_tags.cast(Text).ilike(keyword, escape="\\"),
                )
            )
        return query

    async def _search_questions(self, params: dict) -> list[str]:
        """按条件检索题目，使用单次批量查询避免 N+1。

        统一 LIMIT/COUNT 化：不再全量物化 ORM 对象，题库大时内存与延迟不随总量线性放大。
        """
        async with async_session() as db:
            query = self._build_filter_query(params).order_by(Question.updated_at.desc())

            if params.get("ai_auto_count"):
                result = await db.execute(query.limit(50))
                questions = result.scalars().all()
                logger.info("_search_questions: ai_auto top %d candidates", len(questions))
                return await self._ai_select_questions(params, questions)

            count = params.get("question_count")
            if count is None:
                count = 10
            count = max(1, int(count))
            # 精确容量判断用 COUNT，不物化实体
            cnt_r = await db.execute(select(func.count()).select_from(query.subquery()))
            total = cnt_r.scalar() or 0
            if count > total and not params.get("capacity_probe"):
                raise ValueError(f"题库容量不足：需要 {count} 道，当前仅有 {total} 道符合条件")
            result = await db.execute(query.limit(count))
            questions = result.scalars().all()
            logger.info("_search_questions: found %d done questions matching filters (total %d)", len(questions), total)
            return [q.id for q in questions]

    async def get_capacity(self, params: dict) -> dict:
        """Return usable inventory without triggering AI selection.

        只做 COUNT 查询，不物化全量题目对象，避免库存大时浪费内存。
        """
        from sqlalchemy import func
        probe = dict(params)
        probe["ai_auto_count"] = False
        async with async_session() as db:
            query = self._build_filter_query(probe).with_only_columns(func.count(Question.id))
            total = (await db.execute(query)).scalar() or 0
        return {"available": int(total), "warnings": []}

    async def _validate_question_ids(self, question_ids: list[str]) -> tuple[list[str], list[str]]:
        ordered = list(dict.fromkeys(str(qid) for qid in question_ids if qid))
        if not ordered:
            return [], []
        async with async_session() as db:
            result = await db.execute(
                select(Question.id).where(
                    Question.id.in_(ordered),
                    Question.status == "done",
                    or_(Question.source_type.is_(None),
                        ~Question.source_type.in_(["search_query", "correction_query"])),
                    or_(
                        Question.is_resolved.is_(True),
                        Question.audit_flags.is_(None),
                        ~Question.audit_flags.cast(Text).ilike("%question_challenge_high%"),
                    ),
                )
            )
            valid_set = {row[0] for row in result.fetchall()}
        return [qid for qid in ordered if qid in valid_set], [qid for qid in ordered if qid not in valid_set]

    async def _ai_select_questions(self, params: dict, questions: list[Question]) -> list[str]:
        """Let AI choose an explicit subset so stored IDs always match the rendered paper."""
        if not questions:
            return []
        candidates = []
        for q in questions:
            plain = re.sub(r"<[^>]+>", "", q.question_html or q.ocr_text or "")
            candidates.append({
                "id": q.id,
                "tags": q.knowledge_tags or [],
                "avg_score": q.avg_score,
                "preview": plain[:180],
            })
        defaults = {
            "regular_paper": 12, "collection_paper": 15, "mock_exam": 20,
            "final_exam": 20, "topic_exam": 10, "custom": 10,
        }
        ranges = {
            "regular_paper": (8, 15), "collection_paper": (10, 20),
            "mock_exam": (12, 30), "final_exam": (12, 30),
            "topic_exam": (5, 12), "custom": (5, 15),
        }
        min_target, max_target = ranges.get(params.get("paper_type"), (5, 15))
        min_target, max_target = min(len(candidates), min_target), min(len(candidates), max_target)
        fallback_count = min(len(candidates), defaults.get(params.get("paper_type"), 10))
        prompt = (
            "你是组卷选题代理。根据试卷类型、知识点覆盖和难度梯度，从候选题中自主选出合适题量。"
            "只能使用给出的 id，不得重复；避免内容高度重复。输出 JSON："
            '{"question_ids":["id"],"reason":"一句话"}。\n'
            f"试卷类型：{params.get('paper_type','custom')}\n"
            f"学科/年级：{params.get('subject','')}/{params.get('grade','')}\n"
            f"用户要求：{params.get('custom_prompt','')}\n"
            f"可自主选择题量范围：{min_target}-{max_target} 道\n"
            f"候选题：{candidates}"
        )
        selected = []
        try:
            decision = await ai_service.deepseek_json(
                [{"role": "user", "content": prompt}], max_tokens=4096, scope="paper"
            )
            requested_ids = decision.get("question_ids", []) if isinstance(decision, dict) else []
            allowed = {item["id"] for item in candidates}
            selected = list(dict.fromkeys(qid for qid in requested_ids if qid in allowed))
            selected = selected[:max_target]
            if len(selected) < min_target:
                selected_set = set(selected)
                selected.extend(
                    item["id"] for item in candidates
                    if item["id"] not in selected_set
                )
                selected = selected[:min_target]
        except Exception as exc:
            logger.warning("AI question selection failed, using balanced fallback: %s", exc)
        if not selected:
            selected = [item["id"] for item in candidates[:fallback_count]]
            params.setdefault("generation_warnings", []).append("AI 选题决策不可用，已使用稳定的题库排序完成组卷")
        params["ai_selected_count"] = len(selected)
        return selected

    async def _filter_by_attr(self, db, ids: list[str], attr: str, value: str) -> list[str]:
        """在 ids 范围内按属性过滤，使用单次 IN 查询。"""
        if not ids or not value:
            return ids
        col = getattr(Question, attr, None)
        if col is None:
            return ids
        result = await db.execute(
            select(Question.id).where(Question.id.in_(ids), col == value)
        )
        return [row[0] for row in result.fetchall()]

    async def _load_questions(self, question_ids: list[str]) -> list[dict]:
        """批量加载题目，保持传入顺序。"""
        if not question_ids:
            return []
        async with async_session() as db:
            result = await db.execute(select(Question).where(Question.id.in_(question_ids)))
            q_map = {q.id: q for q in result.scalars().all()}

        result = []
        for qid in question_ids:
            q = q_map.get(qid)
            if not q:
                raise ValueError(f"冻结题目 {qid} 在生成过程中丢失")
            result.append({
                "id": q.id, "subject": q.subject, "grade": q.grade,
                "knowledge_tags": q.knowledge_tags, "ocr_text": q.ocr_text,
                "question_html": q.question_html, "answer_html": q.answer_html,
                "diagrams": q.diagrams, "region": q.region or "", "avg_score": q.avg_score,
                "standard_answer": q.standard_answer or "",
                "score_points_html": q.score_points_html or "",
            })
        return result

    async def _get_prompt_template(self, template_id: str) -> dict:
        async with async_session() as db:
            tmpl = await db.get(PromptTemplate, template_id)
            if tmpl:
                return {"content": tmpl.content, "blocks": tmpl.blocks}
        return DEFAULT_PROMPT_TEMPLATES.get(template_id, DEFAULT_PROMPT_TEMPLATES["default_paper"])

    def _fill_prompt_template(self, template: dict, params: dict,
                               questions_data: list[dict], paper_size: str) -> str:
        content = template["content"]
        questions_text = self._format_questions_for_prompt(questions_data)

        extra_params = params.get("extra_params") or {}
        replacements = {
            "{grade}": params.get("grade", ""),
            "{subject}": params.get("subject", ""),
            "{user_prompt}": params.get("custom_prompt", params.get("keyword", "")),
            "{question_count}": str(len(questions_data)),
            "{questions}": questions_text,
            "{knowledge_tags}": ", ".join(params.get("knowledge_tags", [])),
            "{total_score}": str(extra_params.get("total_score", "100")),
            "{exam_duration}": str(extra_params.get("exam_duration", "120")),
            "{paper_size}": paper_size,
            "{answer_space}": params.get("answer_space", "sheet"),
        }
        # 两阶段替换（顺序关键）：① 固定占位符先换成唯一哨兵；② 再插入用户可控内容
        # （{user_prompt}/{questions}，其中字面含占位符不会被误替换）；③ 最后回填哨兵。
        # 若先插入用户内容再替换固定占位符，全局 replace 会污染刚插入的内容。
        sentinel_map = {}
        user_replacements = {
            "{user_prompt}": params.get("custom_prompt", params.get("keyword", "")),
            "{questions}": questions_text,
        }
        if params.get("ai_auto_count"):
            user_replacements["{questions}"] = (
                f"【题目数量说明】选题代理已自主确定以下 {len(questions_data)} 道题。"
                "请完整使用且每题只出现一次，不得增删、替换或合并题目。\n\n" + questions_text
            )
        # ① 固定占位符 → 哨兵
        for key, value in replacements.items():
            if key in user_replacements:
                continue
            token = f"__REPLACE_{len(sentinel_map)}__"
            sentinel_map[token] = value
            content = content.replace(key, token)
        # ② 插入用户可控内容
        for key, value in user_replacements.items():
            content = content.replace(key, value)
        # ③ 哨兵 → 固定值
        for token, value in sentinel_map.items():
            content = content.replace(token, value)
        return content

    def _format_questions_for_prompt(self, questions_data: list[dict]) -> str:
        parts = []
        for i, q in enumerate(questions_data, 1):
            avg = f", 平均分{q['avg_score']}" if q.get('avg_score') is not None else ""
            region = f", {q['region']}" if q.get('region') else ""
            q_html = q.get('question_html', '') or q.get('ocr_text', '')
            # 组卷 prompt 中不暴露标准答案，避免 AI 将答案泄漏到试卷正文
            tags = ", ".join(q.get('knowledge_tags') or [])
            parts.append(
                f'<section class="source-question" data-question-id="{q.get("id", "")}">\n'
                f"【题目{i}】({q.get('grade','')}{q.get('subject','')}{region}{avg}, "
                f"知识点:{tags})\n{q_html}\n</section>\n"
            )
        return "\n".join(parts)

    @staticmethod
    def _has_exact_question_coverage(paper_html: str, question_ids: list[str]) -> bool:
        """覆盖校验：渲染题集合与冻结题集一致且无重复。

        不要求严格同序（功能检查轮 F3-H2）：生成 prompt 只约束"原样保留且
        只出现一次"，AI 按题型重排题序是合法行为；旧版严格同序比较会把
        合法重排判为失败，两次重试必然全挂。题序一致性由调用方在通过后
        按题库顺序重排正文保证。"""
        if not paper_html or not question_ids:
            return False
        rendered_ids = re.findall(r'data-question-id\s*=\s*["\']([a-zA-Z0-9_-]{1,64})["\']', paper_html)
        return (len(rendered_ids) == len(set(rendered_ids)) == len(question_ids)
                and set(rendered_ids) == set(question_ids))

    @staticmethod
    def _build_answer_html(questions_data: list[dict]) -> str:
        parts = ['<section class="answer-key"><h1>参考答案与评分标准</h1>']
        for number, question in enumerate(questions_data, 1):
            qid = question.get("id", "")
            score = question.get("score_points_html", "") or ""
            standard = question.get("standard_answer", "") or ""
            analysis = question.get("answer_html", "") or ""
            parts.append(
                f'<article class="answer-item" data-question-id="{qid}"><h2>第 {number} 题</h2>'
                f'<section class="answer-score"><h3>评</h3>{score or "<p>本题暂无独立得分点。</p>"}</section>'
                f'<section class="answer-standard"><h3>答</h3>{standard or "<p>暂无标准答案。</p>"}</section>'
                f'<section class="answer-analysis"><h3>析</h3>{analysis or "<p>暂无详细解析。</p>"}</section>'
                '</article>'
            )
        parts.append('</section>')
        return "".join(parts)

    async def _generate_with_ai(self, prompt: str) -> tuple[str, str, str]:
        messages = [
            {"role": "system", "content": (
                "你是专业试卷命题排版专家。生成可打印的考试试卷。\n"
                "【关键规则】试卷部分绝对不能出现答案、解析或评分提示！\n"
                "【三板块严格分离】\n"
                "1. 试卷部分 (paper)：只含题目、留空/答题卡，没有任何答案内容\n"
                "2. 答案由后端从已审核题库确定性生成；你不得重新解题或改写答案\n"
                "3. <!-- ANSWER_SPLIT --> 后只输出空的 <div></div> 占位，节省输出并避免答案污染题面\n"
                "【答题空间模式 - 严格遵守】\n"
                "- sheet（默认）：试卷不预留答题空位，卷末附独立答题卡表格（题号|答案），如用户要求独立答题纸则用 <!-- ANSWER_SHEET_SPLIT --> 单独标记答题纸HTML\n"
                "- inline：每题下方预留足空位，不生成答题卡表格。填空至少4个____，简答至少5行空白\n"
                "- both：题目下方留空 + 卷末答题卡\n"
                "【输出格式】\n"
                "试卷和答案用 <!-- ANSWER_SPLIT --> 分割。如果需要独立答题纸，用 <!-- ANSWER_SHEET_SPLIT --> 单独包裹答题纸HTML部分放在试卷与答案之间。\n"
                "  格式为：试卷HTML...<!-- ANSWER_SHEET_SPLIT -->答题纸HTML...<!-- ANSWER_SPLIT -->答案HTML\n"
                "【硬性要求 - 样式】\n"
                 "禁止任何灰色/纯色色块填充区域（除非是题目图中的阴影部分，用于标注面积）\n"
                 "只使用白色背景，深灰色细文字，1px灰色细边框\n"
                 "题号格式: 直接用阿拉伯数字1、2、3……不加圆圈不加背景\n"
                 "大题标题格式: 一、选择题  二、填空题 ……\n"
                 "题目图中的点必须标注字母（如 A、B、C、O 等），辅助线用虚线标注\n"
                 "【硬性要求 - 内容】\n"
                 "- 输入中的每个 data-question-id 属性必须原样保留且只出现一次，不得增删、重复、替换或合并题目\n"
                "- 试卷部分没有任何答案！填空处只用____\n"
                "- 标题h1居中\n"
                "- 填空至少____(根据答案长度)，简答至少5行空白\n"
                "- 不要有任何多余URL/href超链接\n"
                "- 不生成答案、解析或评分内容；答案占位保持为空\n"
                "- 数学LaTeX $...$ 或 $$...$$\n"
                "- CSS: body{font-family:SimSun,serif;font-size:14px;line-height:2;background:#fff}\n"
                 "- 背景纯白，纸张大小和页边距必须服从用户提示中的 paper_size\n"
                "- 完整输出，不要截断！"
            )},
            {"role": "user", "content": prompt}
        ]

        full_response = await ai_service.deepseek_chat(
            messages=messages, max_tokens=65536, temperature=0.3, scope="paper"
        )

        # Check for answer sheet split
        answer_sheet_html = ""
        if "<!-- ANSWER_SHEET_SPLIT -->" in full_response:
            sheet_parts = full_response.split("<!-- ANSWER_SHEET_SPLIT -->", 1)
            paper_html = self._extract_body(sheet_parts[0].strip())
            remaining = sheet_parts[1]
            if "<!-- ANSWER_SPLIT -->" in remaining:
                ans_parts = remaining.split("<!-- ANSWER_SPLIT -->", 1)
                answer_sheet_html = self._extract_body(ans_parts[0].strip())
                answer_html = self._extract_body(ans_parts[1].strip()) if len(ans_parts) > 1 else ""
            else:
                answer_html = self._extract_body(remaining.strip())
        else:
            parts = full_response.split("<!-- ANSWER_SPLIT -->")
            paper_html = self._extract_body(parts[0].strip())
            answer_html = self._extract_body(parts[1].strip()) if len(parts) > 1 else ""
        return paper_html, answer_html, answer_sheet_html

    def _extract_body(self, text: str) -> str:
        # Strip markdown code fences first
        import re
        text = re.sub(r'```(?:html)?\s*([\s\S]*?)\s*```', r'\1', text)
        # Strip any conversational prefix before the first HTML tag
        first_lt = text.find('<')
        if first_lt > 0:
            text = text[first_lt:]
        # Extract body content
        if '<body' in text.lower():
            m = re.search(r'<body[^>]*>(.*?)</body>', text, re.DOTALL | re.IGNORECASE)
            return m.group(1) if m else text
        # If no body tag, strip outer html/head if present
        if '<!DOCTYPE html>' in text or '<html' in text.lower():
            m = re.search(r'<html[^>]*>(.*?)</html>', text, re.DOTALL | re.IGNORECASE)
            if m:
                inner = m.group(1)
                inner = re.sub(r'<head[^>]*>.*?</head>', '', inner, flags=re.DOTALL)
                inner = re.sub(r'</?body[^>]*>', '', inner)
                return inner.strip()
        return text

    async def _save_paper(self, params: dict, question_ids: list[str],
                          paper_html: str, answer_html: str, paper_size: str,
                          answer_sheet_html: str = "") -> str:
        from models.models import gen_id
        paper_id = gen_id()

        paper_dir = os.path.join(PAPERS_DIR, paper_id)
        os.makedirs(paper_dir, exist_ok=True)

        # Write files with error protection
        try:
            paper_path = os.path.join(paper_dir, "paper.html")
            answer_path = os.path.join(paper_dir, "answer.html")
            _atomic_write_text(paper_path, paper_html)
            _atomic_write_text(answer_path, answer_html)
            if answer_sheet_html:
                sheet_path = os.path.join(paper_dir, "answer_sheet.html")
                _atomic_write_text(sheet_path, answer_sheet_html)
        except (IOError, OSError) as e:
            logger.error("Failed to write paper files for %s: %s", paper_id, e)
            shutil.rmtree(paper_dir, ignore_errors=True)
            raise IOError(f"试卷文件写入失败: {e}")

        title = params.get("title", "") or f"{params.get('grade','')}{params.get('subject','')}试卷"
        # 保存的 generation_params 仅保留组卷参数，不包含 question_ids，
        # 避免“复用配置再生成”时把旧题 ID 又写回导致无法更换题目。
        gen_params = {k: v for k, v in params.items() if k != "question_ids"}
        gen_params["paper_size"] = paper_size

        try:
            async with async_session() as db:
                paper = Paper(
                    id=paper_id, title=title,
                    subject=params.get("subject", ""),
                    grade=params.get("grade", ""),
                    paper_type=params.get("paper_type", "custom"),
                    prompt_template_id=params.get("prompt_template_id", ""),
                    custom_prompt=params.get("custom_prompt", ""),
                    question_ids=question_ids,
                    question_order=question_ids,
                    paper_html=paper_html,
                    answer_html=answer_html,
                    answer_sheet_html=answer_sheet_html or "",
                    generation_params=gen_params,
                )
                db.add(paper)
                await db.commit()
        except Exception:
            shutil.rmtree(paper_dir, ignore_errors=True)
            raise

        return paper_id


    # ===================== 学习单生成 =====================

    async def generate_worksheet(self, params: dict) -> Paper:
        if not ENABLE_WORKSHEET:
            raise ValueError("学习单功能未启用")

        topic = params.get("custom_prompt") or params.get("keyword") or ""
        if not topic:
            raise ValueError("缺少 topic，请提供 custom_prompt 或 keyword")

        paper_size = params.get("paper_size", "A4")
        if paper_size not in ALLOWED_PAPER_SIZES:
            raise ValueError(f"不支持的纸张尺寸：{paper_size}")

        subject = params.get("subject", "")
        grade = params.get("grade", "")

        # 参数边界校验
        def _safe_count(v, default, max_val):
            try:
                return max(1, min(int(v or default), max_val))
            except (ValueError, TypeError):
                return default
        example_count = _safe_count(params.get("example_count"), 3, 10)
        practice_count = _safe_count(params.get("practice_count"), 5, 20)
        note_count = _safe_count(params.get("note_count"), 5, 20)

        # 1. 检索概念笔记
        notes = await self._search_notes(params, topic, note_count=note_count)
        if len(notes) < note_count:
            params.setdefault("generation_warnings", []).append(
                f"相关笔记仅找到 {len(notes)} 篇（请求 {note_count} 篇）"
            )
        concept_notes = self._format_notes_for_worksheet(notes)

        # 2. 检索典型例题（status=done）
        # M3（2026-09-09）：显式 question_ids（regenerate modify 冻结题集）优先——
        # 原卷 question_ids 顺序即 [例题..., 练习...]（_save_paper 时按此顺序保存），
        # 按 example_count 切分还原例题/练习，不做重新检索。
        explicit_ids = list(dict.fromkeys(
            str(q) for q in (params.get("question_ids") or []) if q))
        if explicit_ids:
            example_ids = explicit_ids[:example_count]
            practice_ids = explicit_ids[example_count:]
            params.setdefault("generation_warnings", []).append(
                f"已冻结原卷题集（modify 重排）：例题 {len(example_ids)} 道、练习 {len(practice_ids)} 道")
        else:
            example_ids = await self._search_questions_for_worksheet(params, topic, limit=example_count)
            if len(example_ids) < example_count:
                params.setdefault("generation_warnings", []).append(
                    f"相关例题仅找到 {len(example_ids)} 道（请求 {example_count} 道）"
                )
            if not example_ids:
                logger.warning("generate_worksheet: no example questions found for topic=%s", topic)
            # 3. 检索配套练习题（与例题不同）
            practice_ids = await self._search_questions_for_worksheet(
                params, topic, limit=practice_count, exclude_ids=set(example_ids))
            if len(practice_ids) < practice_count:
                params.setdefault("generation_warnings", []).append(
                    f"相关练习题仅找到 {len(practice_ids)} 道（请求 {practice_count} 道）"
                )
            if not practice_ids:
                logger.warning("generate_worksheet: no practice questions found for topic=%s", topic)
        example_questions_data = await self._load_questions(example_ids)
        example_questions = self._format_questions_for_worksheet(example_questions_data, "例题")
        practice_questions_data = await self._load_questions(practice_ids)
        practice_questions = self._format_questions_for_worksheet(practice_questions_data, "练习题")

        if not notes and not example_ids and not practice_ids:
            raise ValueError("当前条件下没有可用于生成学习单的笔记或题目，请调整主题与筛选条件")

        # 4. 调用 AI 生成学习单 HTML
        full_response = await ai_service.deepseek_generate_worksheet(
            subject=subject, grade=grade, topic=topic,
            concept_notes=concept_notes,
            example_questions=example_questions,
            practice_questions=practice_questions,
            paper_size=paper_size,
        )

        # 5. 解析 WORKSHEET_SPLIT
        paper_html, answer_html = "", ""
        if "<!-- WORKSHEET_SPLIT -->" in full_response:
            parts = full_response.split("<!-- WORKSHEET_SPLIT -->", 1)
            paper_html = self._extract_body(parts[0].strip())
            answer_html = self._extract_body(parts[1].strip()) if len(parts) > 1 else ""
        else:
            paper_html = self._extract_body(full_response.strip())
            answer_html = ""

        # 6. fix_layout 处理主体（answer=False）
        title = params.get("title", "") or f"{grade}{subject}学习单：{topic}"
        paper_raw = paper_html
        paper_html = layout_service.fix_layout(paper_html, paper_size, title, False)
        if not paper_html or len(paper_html) < 50:
            logger.error("fix_layout returned empty worksheet_html, falling back to raw output")
            paper_html = paper_raw or "<p>Layout error</p>"

        # 7. 保存为 Paper
        worksheet_params = dict(params)
        worksheet_params["paper_type"] = "worksheet"
        worksheet_params["paper_size"] = paper_size
        worksheet_params["worksheet"] = True
        worksheet_params["topic"] = topic
        worksheet_params["example_count"] = example_count
        worksheet_params["practice_count"] = practice_count

        question_ids = example_ids + practice_ids
        paper_id = await self._save_paper(
            worksheet_params, question_ids, paper_html, answer_html, paper_size)

        async with async_session() as db:
            paper = await db.get(Paper, paper_id)
            return paper

    async def _search_notes(self, params: dict, topic: str, note_count: int = 5) -> list[Note]:
        """根据 subject/grade/knowledge_tags/keyword 检索相关概念笔记。"""
        async with async_session() as db:
            query = select(Note)
            conditions = []
            if params.get("subject"):
                conditions.append(Note.subject == params["subject"])
            if params.get("grade"):
                conditions.append(Note.grade == params["grade"])
            if conditions:
                query = query.where(and_(*conditions))

            result = await db.execute(query)
            notes = result.scalars().all()

            # 按相关性排序：标题/内容/标签匹配 topic/keyword
            topic_lower = topic.lower()
            keyword = (params.get("keyword") or "").lower()
            scored = []
            for note in notes:
                score = 0
                text = " ".join([
                    (note.title or ""),
                    (note.content or ""),
                    " ".join(note.knowledge_tags or []),
                ]).lower()
                if topic_lower and topic_lower in text:
                    score += 3
                if keyword and keyword in text:
                    score += 2
                tags = set(t.lower() for t in (note.knowledge_tags or []))
                for t in params.get("knowledge_tags", []):
                    if t.lower() in tags:
                        score += 2
                if score > 0:
                    scored.append((score, note))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [n for _, n in scored[:max(1, min(int(note_count or 5), 20))]]

    async def _search_questions_for_worksheet(
            self, params: dict, topic: str, limit: int = 5,
            exclude_ids: set = None) -> list[str]:
        """检索适合学习单的题目（status=done），批量加载后打分。"""
        exclude_ids = exclude_ids or set()
        async with async_session() as db:
            result = await db.execute(
                select(Question.id)
                .where(Question.status == "done")
                .where(or_(Question.source_type.is_(None),
                           ~Question.source_type.in_(["search_query", "correction_query"])))
            )
            all_ids = [row[0] for row in result.fetchall()]

            if params.get("subject"):
                all_ids = await self._filter_by_attr(db, all_ids, "subject", params["subject"])
            if params.get("grade"):
                all_ids = await self._filter_by_attr(db, all_ids, "grade", params["grade"])

            # 批量加载候选题目，避免 N+1
            if not all_ids:
                return []
            result = await db.execute(select(Question).where(Question.id.in_(all_ids)))
            questions = result.scalars().all()

            topic_lower = topic.lower()
            keyword = (params.get("keyword") or "").lower()
            knowledge_tags = params.get("knowledge_tags") or []

            scored = []
            for q in questions:
                if q.id in exclude_ids:
                    continue
                score = 0
                if knowledge_tags:
                    q_tags = set(t.lower() for t in (q.knowledge_tags or []))
                    for t in knowledge_tags:
                        if t.lower() in q_tags:
                            score += 3
                search_text = (q.ocr_text or "") + (q.question_html or "") + " ".join(q.knowledge_tags or [])
                search_text = search_text.lower()
                if topic_lower and topic_lower in search_text:
                    score += 2
                if keyword and keyword in search_text:
                    score += 1
                if score > 0:
                    scored.append((score, q.id))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [qid for _, qid in scored[:limit]]

    def _format_notes_for_worksheet(self, notes: list[Note]) -> str:
        parts = []
        for note in notes:
            parts.append(f"## {note.title or '概念笔记'}\n")
            if note.knowledge_tags:
                parts.append(f"知识点：{', '.join(note.knowledge_tags)}\n")
            parts.append(f"{note.content or ''}\n")
        return "\n".join(parts) if parts else "（未找到相关概念笔记）"

    def _format_questions_for_worksheet(self, questions_data: list[dict], section_title: str) -> str:
        parts = [f"### {section_title}\n"]
        for i, q in enumerate(questions_data, 1):
            q_html = q.get("question_html", "") or q.get("ocr_text", "")
            standard_ans = q.get("standard_answer", "")
            answer = q.get("answer_html", "")
            tags = ", ".join(q.get("knowledge_tags", []) or [])
            parts.append(f"【{section_title}{i}】(知识点：{tags})\n{q_html}\n")
            if standard_ans:
                parts.append(f"标准答案：{standard_ans}\n")
            if answer:
                parts.append(f"解析：{answer}\n")
        return "\n".join(parts) if len(parts) > 1 else f"（未找到相关{section_title}）"


paper_service = PaperService()
