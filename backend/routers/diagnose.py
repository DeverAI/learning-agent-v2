import os, re, time, json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from config import load_settings, SETTINGS_FILE, STORAGE_DIR, BASE_DIR
from services.ai_service import ai_service

router = APIRouter(prefix="/api/test", tags=["diagnose"])


@router.post("/deepseek")
async def test_deepseek():
    s = load_settings()
    if not s.get("deepseek_api_key"):
        return {"ok": False, "error": "Key 未配置"}
    try:
        t0 = time.time()
        r = await ai_service._call(
            ai_service.ds_url, ai_service.ds_key,
            {"model": ai_service.ds_model, "messages": [{"role": "user", "content": "返回1+1="}],
             "temperature": 0.3, "max_tokens": 20}
        )
        elapsed = round(time.time() - t0, 2)
        return {"ok": True, "elapsed": f"{elapsed}s", "reply": r[:100]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


@router.post("/kimi")
async def test_kimi():
    s = load_settings()
    if not s.get("kimi_api_key"):
        return {"ok": False, "error": "Key 未配置"}
    try:
        t0 = time.time()
        r = await ai_service._call(
            ai_service.km_url, ai_service.km_key,
            {"model": ai_service.km_model, "messages": [{"role": "user", "content": "返回1+1="}],
             "temperature": 1, "max_tokens": 20}
        )
        elapsed = round(time.time() - t0, 2)
        return {"ok": True, "elapsed": f"{elapsed}s", "reply": r[:100]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


@router.post("/glm")
async def test_glm():
    s = load_settings()
    if not s.get("zhipuai_api_key"):
        return {"ok": False, "error": "GLM Key 未配置"}
    try:
        t0 = time.time()
        r = await ai_service.zhipuai_chat(
            [{"role": "user", "content": "返回1+1="}],
            model="glm-4-flash", temperature=1, max_tokens=20
        )
        elapsed = round(time.time() - t0, 2)
        return {"ok": True, "elapsed": f"{elapsed}s", "reply": r[:100]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:500]}


diag_router = APIRouter(prefix="/api/diag", tags=["diagnose"])


@diag_router.get("/db")
async def diag_db():
    from models.database import async_session
    from models.models import Question
    try:
        async with async_session() as db:
            from sqlalchemy import select, func
            r = await db.execute(select(func.count(Question.id)))
            total = r.scalar() or 0
            r2 = await db.execute(select(Question).where(Question.status == "error"))
            errors = r2.scalars().all()
            return {
                "status": "ok",
                "question_count": total,
                "error_count": len(errors),
                "error_questions": [{"id": q.id, "status": q.status, "error_message": q.error_message} for q in errors]
            }
    except Exception as e:
        return {"status": "fail", "error": str(e)}


@diag_router.get("/files")
async def diag_files():
    required = [
        "main.py", "config.py",
        "models/models.py", "models/database.py",
        "schemas/schemas.py",
        "services/ai_service.py", "services/ocr_service.py",
        "services/paper_service.py", "services/layout_service.py",
        "services/diagram_service.py", "services/export_service.py",
        "services/knowledge.py", "services/user_profile.py",
        "routers/__init__.py", "routers/ocr.py", "routers/questions.py",
        "routers/papers.py", "routers/prompts.py", "routers/settings.py",
        "routers/profile.py", "routers/knowledge_base.py",
        "templates/base.html", "templates/dashboard.html",
        "templates/questions.html", "templates/papers.html",
        "templates/paper_detail.html", "templates/paper_generate.html",
        "templates/batch_upload.html", "templates/prompts.html",
        "templates/settings.html", "templates/qa.html",
    ]
    results = []
    all_ok = True
    for f in required:
        full = os.path.join(BASE_DIR, f)
        if os.path.isfile(full):
            sz = os.path.getsize(full)
            results.append(f"[OK] {f} ({sz:,} bytes)")
        else:
            results.append(f"[MISSING] {f}")
            all_ok = False
    results.insert(0, f"[{'PASS' if all_ok else 'FAIL'}] 共 {len(required)} 个文件")
    return {"all_ok": all_ok, "results": results}


@diag_router.post("/ai-analyze/{question_id}")
async def ai_diagnose_question(question_id: str):
    """AI诊断单道题目的问题"""
    from models.database import async_session
    from models.models import Question
    async with async_session() as db:
        q = await db.get(Question, question_id)
        if not q:
            raise HTTPException(404, detail="题目不存在")

        prompt = f"""你是一个专业的教学诊断AI。请分析以下题目的处理结果，找出问题并给出改进建议：

题目ID: {q.id}
学科: {q.subject or '未知'}
年级: {q.grade or '未知'}
状态: {q.status}
知识点: {', '.join(q.knowledge_tags or [])}
地区: {q.region or '未知'}
均分: {q.avg_score or '未知'}
题型: {q.question_type or '未分类'}

OCR原文: {q.ocr_text[:500] if q.ocr_text else '无'}

题面HTML: {q.question_html[:300] if q.question_html else '无'}
解答HTML: {q.answer_html[:300] if q.answer_html else '无'}
标准答案: {q.standard_answer or '未设置'}
错误信息: {q.error_message or '无错误'}

请分析：
1. 题目数据是否完整？（题面、解答、标答是否都存在）
2. 如果处理失败，可能的原因是什么？
3. 题面质量如何？是否清晰、标准？
4. 解答质量如何？是否有得分点和详细过程？
5. 标准答案是否合理？
6. 给出具体的改进建议。"""

        metadata = {
            "subject": q.subject, "grade": q.grade, "status": q.status,
            "has_question_html": bool(q.question_html),
            "has_answer_html": bool(q.answer_html),
            "has_standard_answer": bool(q.standard_answer),
            "error_message": q.error_message or "",
        }

    from services.ai_service import ai_service
    try:
        analysis = await ai_service.deepseek_chat([
            {"role": "system", "content": "你是有经验的学科诊断AI，输出简洁、专业的诊断分析。"},
            {"role": "user", "content": prompt}
        ], max_tokens=4096)
    except Exception as exc:
        from logger import log_error
        log_error("diagnose.ai_analyze", f"AI diagnose failed for {question_id}: {exc}")
        _msg = str(exc).lower()
        # \b401\b 精确匹配状态码，避免 "1401ms" 之类耗时数字误判为认证失败
        if re.search(r'\b401\b|unauthorized|authentication|api key|illegal header', _msg):
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
        raise HTTPException(status_code=502, detail="AI 诊断失败，请稍后重试")

    return {"question_id": question_id, "analysis": analysis, **metadata}


@diag_router.post("/ai-analyze-all-errors")
async def ai_diagnose_all_errors():
    """批量诊断所有错误题目"""
    from models.database import async_session
    from models.models import Question
    from sqlalchemy import select
    async with async_session() as db:
        r = await db.execute(select(Question).where(Question.status == "error"))
        errors = r.scalars().all()

    results = []
    for q in errors[:10]:  # 最多分析10题
        try:
            prompt = f"题目{q.id[:8]} ({q.subject or '未知'}/{q.grade or '未知'}): 错误={(q.error_message or '')[:200]}。请一句话诊断问题原因。"
            from services.ai_service import ai_service
            analysis = await ai_service.deepseek_chat([
                {"role": "user", "content": prompt}
            ], max_tokens=1024, scope="chat")
            results.append({"id": q.id, "error": q.error_message, "diagnosis": analysis})
        except Exception as e:
            results.append({"id": q.id, "error": q.error_message, "diagnosis": f"诊断失败: {str(e)}"})

    return {"total": len(errors), "analyzed": len(results), "results": results}
