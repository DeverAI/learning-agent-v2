import logging
import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response, FileResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from models.database import init_db
from routers import ocr, questions, papers, prompts, settings, profile, knowledge_base, diagnose, sessions, banks, feed
from routers.notes import router as notes_router
from routers.gallery import router as gallery_router
from routers.configs import router as configs_router
from routers.chat import router as chat_router
from routers.diagram import router as diagram_router
from routers.system import router as system_router
from config import STORAGE_DIR, ENABLE_SEARCH, ENABLE_CORRECT, ENABLE_FOCUS_MODE, ENABLE_XIAOMI_TTS, ENABLE_OFFLINE_CACHE, CORS_ALLOWED_ORIGINS, _atomic_write_json
from logger import get_logger, log_error
from services.diagram_service import _is_valid_question_id as _is_valid_diagram_qid
import os
import re
from datetime import datetime

logger = get_logger()
_lifespan_tasks = set()


def _start_lifespan_task(coro):
    task = asyncio.create_task(coro)
    _lifespan_tasks.add(task)

    def _on_done(done_task):
        _lifespan_tasks.discard(done_task)
        if done_task.cancelled():
            return
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            log_error("lifespan_task", str(exc))

    task.add_done_callback(_on_done)
    return task

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data: https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-src 'self'"
)

env = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=select_autoescape(['html', 'xml']),
    auto_reload=True,
    cache_size=0,
)


def render_template(name: str, **ctx) -> HTMLResponse:
    tmpl = env.get_template(name)
    ctx.setdefault("enable_search", ENABLE_SEARCH)
    ctx.setdefault("enable_correct", ENABLE_CORRECT)
    ctx.setdefault("enable_xiaomi_tts", ENABLE_XIAOMI_TTS)
    from config import ENABLE_OFFLINE_CACHE
    ctx.setdefault("enable_offline", ENABLE_OFFLINE_CACHE)
    return HTMLResponse(tmpl.render(ctx))


async def _night_note_patrol_loop():
    """夜间自动笔记巡逻：在配置的时间窗口内自动执行笔记去重整合 + 题库经典模型收集 + 题库巡检"""
    from datetime import datetime, timedelta, timezone
    from config import load_settings
    _last_patrol_date = None
    while True:
        try:
            await asyncio.sleep(60)  # 每分钟检查一次
            s = load_settings()
            if not s.get("night_patrol_enabled", True):
                continue
            now = datetime.now(timezone.utc) + timedelta(hours=8)  # UTC+8
            today_str = now.strftime("%Y-%m-%d")
            patrol_start = s.get("night_patrol_start", "01:00")
            patrol_end = s.get("night_patrol_end", "05:00")
            now_time = now.strftime("%H:%M")
            # Handle overnight range (e.g., 22:00-06:00)
            in_window = False
            if patrol_start <= patrol_end:
                in_window = patrol_start <= now_time <= patrol_end
            else:
                in_window = now_time >= patrol_start or now_time <= patrol_end
            if not in_window:
                continue
            if _last_patrol_date == today_str:
                continue  # 今天已执行过
            # 先占位当天日期再执行：任何一步失败都不会在窗口内每分钟重试，
            # 避免对笔记反复执行全量 AI 整理/合并（最坏约240次）。
            _last_patrol_date = today_str
            logger.info("Nightly note patrol: auto-organize + classic model collection started")
            # 1. 收集题库中的经典模型标签（如一线三等角、手拉手模型等）
            classic_models = await _collect_classic_models()
            # 保存到缓存供笔记分类AI使用
            try:
                from services.note_service import _save_classic_models_cache
                _save_classic_models_cache(classic_models)
            except Exception as cache_error:
                logger.warning("Classic model cache save failed: %s", cache_error)
            # 2. 笔记自动整理（注入经典模型上下文）
            from routers.notes import _do_auto_organize
            result = await _do_auto_organize(classic_models_context=classic_models)
            logger.info("Nightly note patrol: %s", result.get("message", "done"))
            # 整理完成后刷新知识树缓存，页面打开时无需再次等待 AI。
            try:
                from config import STORAGE_DIR
                from models.database import async_session
                from models.models import Note
                from services.note_service import build_knowledge_tree
                from sqlalchemy import select
                async with async_session() as tree_db:
                    tree_rows = await tree_db.execute(select(Note).order_by(Note.updated_at.desc()))
                    tree = build_knowledge_tree(tree_rows.scalars().all())
                _atomic_write_json(os.path.join(STORAGE_DIR, "knowledge_tree.json"), tree)
            except Exception as tree_error:
                logger.warning("Knowledge tree cache refresh failed: %s", tree_error)
            # 3. 发送系统消息
            try:
                from services.audit_service import add_system_message
                model_hint = f"（含 {len(classic_models)} 个经典模型）" if classic_models else ""
                add_system_message("system", "夜间笔记整理完成",
                                 f"笔记去重整合已完成{model_hint}。")
            except Exception as message_error:
                logger.warning("Nightly note patrol message failed: %s", message_error)
        except asyncio.CancelledError:
            logger.info("Nightly note patrol scheduler cancelled")
            break
        except Exception as e:
            logger.warning("Nightly note patrol error: %s", str(e)[:200])


async def _health_patrol_loop():
    """R39：每 30 分钟巡检题目/试卷报错与未处理反馈。

    - 扫题库 status=error（近 1 小时新增优先）
    - 扫未处理反馈
    - 有问题写 system_messages（首页消息栏）
    - 可手动调 `GET /api/health/patrol`
    """
    from routers.system import health_patrol
    while True:
        try:
            await asyncio.sleep(1800)  # 30 min
            r = await health_patrol()
            if r.get("problems"):
                logger.info("Health patrol found %d issue(s)", len(r["problems"]))
        except asyncio.CancelledError:
            logger.info("Health patrol cancelled")
            break
        except Exception as e:
            logger.warning("Health patrol error: %s", str(e)[:200])


async def _collect_classic_models() -> list[str]:
    """从题库中收集经典数学模型/物理模型标签"""
    try:
        from models.database import async_session as _db_s
        from models.models import Question
        from sqlalchemy import select
        CLASSIC_KEYWORDS = [
            "一线三等角", "手拉手模型", "将军饮马", "胡不归", "阿氏圆",
            "瓜豆原理", "K型图", "等积变换", "截长补短", "倍长中线",
            "旋转模型", "翻折模型", "中点模型", "角平分线模型", "弦图",
            "半角模型", "三线合一", "十字模型", "费马点", "托勒密",
        ]
        found = set()
        async with _db_s() as db:
            r = await db.execute(select(Question.knowledge_tags).where(Question.status == "done"))
            rows = r.fetchall()
            for row in rows:
                tags = row[0] if row[0] else []
                if isinstance(tags, list):
                    for tag in tags:
                        tag_lower = tag.strip().lower()
                        for kw in CLASSIC_KEYWORDS:
                            if kw in tag or kw in tag_lower:
                                found.add(kw)
        return list(found)[:20]
    except Exception as e:
        logger.warning("Classic model collection failed: %s", e)
        return []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    try:
        from routers.prompts import init_default_prompts
        from models.database import async_session
        async with async_session() as db:
            await init_default_prompts(db)
    except Exception as e:
        logger.warning("Prompt init skipped: %s", e)
    try:
        from routers.ocr import resume_pending_tasks
        await resume_pending_tasks()
    except Exception as e:
        log_error("lifespan", f"Failed to resume pending tasks: {e}")
        logger.warning("Failed to resume pending tasks, continuing...", exc_info=True)
    # 后台 Agent 任务对账：服务重启后跑任务的协程已随进程消失，
    # 但库里还留着 running 的行 -> 不对账的话前端永远显示"进行中"，
    # 用户会一直等一个永远不会结束的任务。
    try:
        from services import background_agent as _ba
        _n = await _ba.reconcile_on_startup()
        if _n:
            logger.warning("Reconciled %d interrupted agent tasks", _n)
    except Exception as e:
        log_error("lifespan", f"Failed to reconcile agent tasks: {e}")
    # 日签可能触发外部 AI，不能阻塞服务启动。
    try:
        from services.audit_service import _generate_daily_quote
        _start_lifespan_task(_generate_daily_quote())
    except Exception as e:
        logger.warning("Daily quote startup generation failed: %s", e)

    # Start nightly audit scheduler
    try:
        from services.audit_service import start_nightly_scheduler
        _start_lifespan_task(start_nightly_scheduler())
        logger.info("Nightly audit scheduler started")
    except Exception as e:
        logger.warning("Failed to start nightly audit scheduler: %s", e)

    # Start nightly note patrol scheduler
    try:
        _start_lifespan_task(_night_note_patrol_loop())
        logger.info("Nightly note patrol scheduler started")
    except Exception as e:
        logger.warning("Failed to start nightly note patrol: %s", e)
    # R39：每 30 分钟巡检题目/试卷报错与未处理反馈
    try:
        _start_lifespan_task(_health_patrol_loop())
        logger.info("30-min health patrol started")
    except Exception as e:
        logger.warning("Failed to start health patrol: %s", e)
    try:
        yield
    finally:
        tasks = list(_lifespan_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        _lifespan_tasks.clear()


app = FastAPI(title="学习搭子 - 题目本Agent", version="2.0.0", lifespan=lifespan)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """捕获未处理异常并写入 Err.log，避免前端收到 HTML/纯文本 Internal Server Error。"""
    from fastapi.responses import JSONResponse
    import traceback
    logger.exception("Unhandled exception for %s %s", request.method, request.url.path)
    trace = traceback.format_exception(type(exc), exc, exc.__traceback__)
    log_error("unhandled_exception", f"{request.method} {request.url.path}: {''.join(trace)[-4000:]}")
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请查看 Err.log"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)


@app.middleware("http")
async def add_csp_header(request: Request, call_next):
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if "text/html" in ct:
        response.headers["Content-Security-Policy"] = CSP_HEADER
    return response

# 密码保护中间件
# 默认密码可通过环境变量 LEARNING_AGENT_PASSWORD 或 settings.json 的 api_password 覆盖
_DEFAULT_AUTH_PASSWORD = ""
_AUTH_EXACT_EXEMPT = {"/api/health", "/favicon.ico"}

def _get_auth_password() -> str:
    try:
        env_pwd = os.environ.get("LEARNING_AGENT_PASSWORD", "").strip()
        if env_pwd:
            return env_pwd
    except Exception:
        pass
    try:
        from config import load_settings as _load_s
        s = _load_s()
        pwd = str(s.get("api_password", "") or "").strip()
        if pwd:
            return pwd
    except Exception:
        pass
    return _DEFAULT_AUTH_PASSWORD

# 兼容旧引用：部分脚本可能直接导入 _AUTH_PASSWORD
_AUTH_PASSWORD = _DEFAULT_AUTH_PASSWORD

@app.middleware("http")
async def password_guard(request: Request, call_next):
    """简单密码验证：API 路径需携带密码，页面和静态文件豁免"""
    path = request.url.path
    # 豁免：精确匹配的路径、静态文件、非 API 页面
    if path in _AUTH_EXACT_EXEMPT:
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)
    # 从 Header 或 Query 参数获取密码
    auth_header = request.headers.get("authorization", "")
    token_header = request.headers.get("x-auth-token", "")
    query_pass = request.query_params.get("_auth", "")
    provided = ""
    if auth_header.lower().startswith("bearer "):
        provided = auth_header[7:].strip()
    elif token_header:
        provided = token_header.strip()
    elif query_pass:
        provided = query_pass.strip()
    expected = _get_auth_password()
    # 未配置密码（环境变量与 settings.json 均为空）= 鉴权未启用，直接放行。
    # 既有契约（2026-09-10 修复 45 个测试 401）：中间件改为逐请求读取后，
    # 测试模块把 SETTINGS_FILE 重定向到空临时目录时 expected 变空串，
    # 与测试 token 恒不等导致整批 401；且未配置时本就拦截不住空 header，
    # 此分支不构成安全弱化。配置了密码（生产服务器）仍强制恒定时间比较。
    import hmac as _hmac
    if expected and not _hmac.compare_digest(provided, expected):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "访问被拒绝，请提供密码"},
            headers={"WWW-Authenticate": 'Bearer realm="learning-agent"'},
        )
    return await call_next(request)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_PUBLIC_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_PUBLIC_RASTER_IMAGE_EXTENSIONS = set(_PUBLIC_IMAGE_EXTENSIONS)
_PUBLIC_VENDOR_EXTENSIONS = {".js", ".css", ".woff", ".woff2", ".ttf", ".otf"}
_SAFE_STORAGE_SEGMENT = re.compile(r"^[a-zA-Z0-9_.-]+$")
_SAFE_QUESTION_SVG_NAME = re.compile(r"^(?:reference|diagram_\d+)\.svg$")


@app.get("/storage/{file_path:path}", include_in_schema=False)
async def serve_public_storage_file(file_path: str):
    """只公开展示所需文件，禁止数据库、日志、会话和 JSON 被静态下载。"""
    normalized = str(file_path or "").replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    if not parts or any(part in ("", ".", "..") or not _SAFE_STORAGE_SEGMENT.fullmatch(part) for part in parts):
        raise HTTPException(status_code=404, detail="文件不存在")

    extension = os.path.splitext(parts[-1])[1].lower()
    allowed = False
    if parts[0] == "vendor" and len(parts) >= 2:
        allowed = extension in _PUBLIC_VENDOR_EXTENSIONS
    elif parts[0] == "questions" and len(parts) == 3 and _is_valid_diagram_qid(parts[1]):
        if extension == ".svg":
            # 仅允许经过 SVG 消毒流程写入的 reference.svg / diagram_N.svg
            # （真实题目才有示意图，无需查库；学生作答图只有光栅文件）
            allowed = bool(_SAFE_QUESTION_SVG_NAME.fullmatch(parts[-1]))
        else:
            # 学生作答图/搜题临时记录与题库原图共用 questions 目录：
            # 临时查询记录（含学生手写作答）不得通过公开静态路径读取。
            from models.database import async_session as _storage_db
            from models.models import Question as _StorageQ
            async with _storage_db() as _db:
                _q = await _db.get(_StorageQ, parts[1])
                if _q is not None:
                    st = getattr(_q, "source_type", None)
                    bk = getattr(_q, "bank", "") or ""
                    # 同时检查 source_type 与 bank，防止旧数据 source_type 为空但 bank 已标记为临时查询
                    if st in ("search_query", "correction_query") or bk in ("search_queries", "correction_queries"):
                        raise HTTPException(status_code=404, detail="文件不存在")
            allowed = extension in _PUBLIC_RASTER_IMAGE_EXTENSIONS
    elif parts[0] == "notes" and len(parts) == 3 and _is_valid_diagram_qid(parts[1]):
        allowed = extension in _PUBLIC_RASTER_IMAGE_EXTENSIONS
    elif parts[0] == "corrections" and 3 <= len(parts) <= 5:
        allowed = extension in _PUBLIC_RASTER_IMAGE_EXTENSIONS
    if not allowed:
        raise HTTPException(status_code=404, detail="文件不存在")

    storage_root = os.path.realpath(STORAGE_DIR)
    disk_path = os.path.realpath(os.path.join(storage_root, *parts))
    try:
        if os.path.commonpath([storage_root, disk_path]) != storage_root:
            raise HTTPException(status_code=404, detail="文件不存在")
    except ValueError:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not os.path.isfile(disk_path):
        raise HTTPException(status_code=404, detail="文件不存在")
    resp = FileResponse(disk_path)
    # 防止枚举与缓存：公开题目图按私有缓存，临时/私有数据 0 缓存
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

app.include_router(ocr.router)
app.include_router(questions.router)
app.include_router(papers.router)
app.include_router(prompts.router)
app.include_router(settings.router)
app.include_router(profile.router)
app.include_router(knowledge_base.router)
app.include_router(diagnose.router)
app.include_router(diagnose.diag_router)
app.include_router(sessions.router)
app.include_router(banks.router)
if ENABLE_SEARCH:
    from routers import search
    app.include_router(search.router)
if ENABLE_CORRECT:
    from routers.correction import router as correction_router
    app.include_router(correction_router)
app.include_router(notes_router)
app.include_router(gallery_router)
app.include_router(configs_router)
app.include_router(chat_router)
app.include_router(diagram_router)
app.include_router(system_router)
from routers.knowledge_graph import router as kg_router
app.include_router(kg_router)
from routers.lecture import router as lecture_router
app.include_router(lecture_router)
# 后台任务与课稿（P3/P7：双端推理 + 备课）
from routers.agent_tasks import router as agent_tasks_router
app.include_router(agent_tasks_router)
from routers.lessons import router as lessons_router
app.include_router(lessons_router)
from routers.smart_upload import router as smart_upload_router
app.include_router(smart_upload_router)
from routers.feedback import router as feedback_router
app.include_router(feedback_router)
if ENABLE_FOCUS_MODE:
    from routers.focus import router as focus_router
    app.include_router(focus_router)
if ENABLE_XIAOMI_TTS:
    from routers.audio import router as audio_router
    app.include_router(audio_router)
from models import feed_models  # noqa: F401  # 自招素材每日一条 — 注册到 Base.metadata 触发建表
app.include_router(feed.router)



@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "学习搭子Agent v2"}


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """PWA manifest：手机浏览器可"添加到主屏幕"（图标/名称/独立窗口形态）。"""
    manifest_data = {
        "name": "学习搭子",
        "short_name": "学习搭子",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#f5f6fa",
        "theme_color": "#2b5aed",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return Response(content=json.dumps(manifest_data, ensure_ascii=False), media_type="application/manifest+json")


@app.get("/favicon.ico")
async def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="6" fill="#333"/>'
           '<text x="16" y="23" text-anchor="middle" font-size="20" fill="#fff">S</text></svg>')
    return Response(content=svg, media_type="image/svg+xml")


# ===================== HTML 页面路由 =====================

@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    return render_template("dashboard.html", page="dashboard")


@app.get("/questions", response_class=HTMLResponse)
async def page_questions(request: Request):
    from config import ENABLE_STRUCTURE_GRAPH, ENABLE_COMPARISON_MODE
    return render_template("questions.html", page="questions",
                           enable_structure_graph=ENABLE_STRUCTURE_GRAPH,
                           enable_comparison_mode=ENABLE_COMPARISON_MODE)


@app.get("/papers", response_class=HTMLResponse)
async def page_papers(request: Request):
    return render_template("papers.html", page="papers")


@app.get("/papers/generate", response_class=HTMLResponse)
async def page_paper_generate(request: Request):
    return render_template("paper_generate.html", page="generate")

if ENABLE_CORRECT:
    # 静态路径 /correctCenter 必须在任何动态 /correct/{...} 路径之前注册，避免被动态段捕获。
    @app.get("/correctCenter", response_class=HTMLResponse)
    async def page_correct_center(request: Request):
        return render_template("correct_center.html", page="correct")

    @app.get("/correctcenter", response_class=HTMLResponse)
    async def page_correct_center_lower(request: Request):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/correctCenter")

    @app.get("/correct", response_class=HTMLResponse)
    async def page_correct(request: Request):
        return render_template("correct.html", page="correct")

    @app.get("/correction/history", response_class=HTMLResponse)
    async def page_correction_history(request: Request):
        return render_template("correction_history.html", page="correct")

    @app.get("/papers/{paper_id}/correct", response_class=HTMLResponse)
    async def page_paper_correct(request: Request, paper_id: str):
        return render_template("paper_correct.html", page="correct", paper_id=paper_id)


@app.get("/papers/{paper_id}", response_class=HTMLResponse)
async def page_paper_detail(request: Request, paper_id: str):
    return render_template("paper_detail.html", page="papers", paper_id=paper_id)


@app.get("/prompts", response_class=HTMLResponse)
async def page_prompts(request: Request):
    return render_template("prompts.html", page="prompts")


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return render_template("settings.html", page="settings")


@app.get("/qa", response_class=HTMLResponse)
async def page_qa(request: Request):
    return render_template("qa.html", page="qa")

@app.get("/notes", response_class=HTMLResponse)
async def page_notes(request: Request):
    return render_template("notes.html", page="notes")

@app.get("/lecture", response_class=HTMLResponse)
async def page_lecture(request: Request):
    return render_template("lecture.html", page="lecture")


@app.get("/lessons", response_class=HTMLResponse)
async def page_lessons(request: Request):
    """课稿独立页：提前备课产物的列表 / 阅读 / 编辑 / 离线缓存入口。

    此前课稿只能经 Agent 对话工具读写 + HTTP API，**没有页面** ——
    学生备完课想自己看一遍，只能让 Agent 念，或手打 API。
    """
    return render_template("lessons.html", page="lessons")


@app.get("/feedback", response_class=HTMLResponse)
async def page_feedback(request: Request):
    """问题反馈页：学生提交问题；巡检脚本定期读取。"""
    return render_template("feedback.html", page="feedback")


@app.get("/feedback", response_class=HTMLResponse)
async def page_feedback(request: Request):
    """问题反馈页（R39）：题目/试卷/功能问题，巡检循环会读。"""
    return render_template("feedback.html", page="feedback")


def _apk_version_from_gradle() -> str:
    """从 android/app/build.gradle.kts 解析 APK 版本，返回 "v1.5（versionCode 6）" 或空串。

    为什么读构建脚本而不是读 APK 本身：纯 Python 解析二进制 AndroidManifest 需要额外依赖
    （项目轻量化优先），而构建脚本是**版本号的唯一源头** —— 出包时做的就是它。
    读不到就返回空串，模板侧会省略版本句，不显示过期数字（宁可不说，也不说错）。
    """
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "android", "app", "build.gradle.kts")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        name_m = re.search(r'versionName\s*=\s*"([^"]+)"', text)
        code_m = re.search(r"versionCode\s*=\s*(\d+)", text)
        if name_m and code_m:
            return f"v{name_m.group(1)}（versionCode {code_m.group(1)}）"
    except Exception as exc:
        logger.warning("读取 APK 版本失败: %s", exc)
    return ""


@app.get("/download", response_class=HTMLResponse)
async def page_download(request: Request):
    """下载页。

    文件体积、打包日期**从磁盘真实读取**，版本号**从 android/app/build.gradle.kts 读取**，
    三样都不再手写。
    起因：2026-09-11 发现下载页写着 "APK v1.3（versionCode 4）"、
    "桌面端 打包日期 2026-09-10"，而磁盘上的 APK 其实是 09-05 的旧包 ——
    手写的数字必然会漂移，而漂移的后果是用户下载了旧包却以为拿到了新版。
    R24 补充：当时只改了体积/日期，**版本号仍是手写**（果然又漂移成
    "v1.4（versionCode 5）"而实际已是 1.5/6）。现在改为解析构建脚本，
    版本号只可能来自"下一次真的要出包的地方"。
    """
    downloads_dir = os.path.join(STATIC_DIR, "downloads")
    out = {}
    apk_version = _apk_version_from_gradle()
    for key, fname in (("apk", "学习搭子-Android.apk"),
                       ("desktop", "学习搭子-桌面端-20260912.zip")):
        path = os.path.join(downloads_dir, fname)
        info = {"exists": os.path.exists(path), "filename": fname,
                "size_text": "", "date_text": "", "version_text": ""}
        if key == "apk":
            info["version_text"] = apk_version
        if info["exists"]:
            st = os.stat(path)
            mb = st.st_size / 1024 / 1024
            # 小于 1MB 用 KB 显示，否则 MB；保留一位小数
            info["size_text"] = (f"{st.st_size / 1024:.0f} KB" if mb < 1
                                 else f"{mb:.1f} MB")
            info["date_text"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        out[key] = info
    return render_template("download.html", page="download", dl=out)


@app.get("/dl/{kind}")
async def download_client(kind: str):
    """客户端下载出口（R33）。

    为什么不用裸 /static/downloads/：
    Windows 上 mimetypes 常不认识 .apk，StaticFiles 会回
    application/octet-stream，浏览器另存为 **xxx.bin**（用户实测「安装包是 bin」）。
    这里强制正确 MIME + RFC 5987 文件名。
    """
    from urllib.parse import quote
    mapping = {
        "android": ("学习搭子-Android.apk", "application/vnd.android.package-archive"),
        "apk": ("学习搭子-Android.apk", "application/vnd.android.package-archive"),
        "desktop": ("学习搭子-桌面端-20260912.zip", "application/zip"),
        "windows": ("学习搭子-桌面端-20260912.zip", "application/zip"),
    }
    if kind not in mapping:
        raise HTTPException(404, "未知下载项")
    fname, ctype = mapping[kind]
    path = os.path.join(STATIC_DIR, "downloads", fname)
    if not os.path.isfile(path):
        raise HTTPException(404, "安装包尚未上传到服务器")
    ascii_name = "LearningAgent-Android.apk" if fname.endswith(".apk") else "LearningAgent-Desktop.zip"
    resp = FileResponse(path, media_type=ctype, filename=ascii_name)
    # 中文原名给现代浏览器（filename*=UTF-8''...）
    resp.headers["Content-Disposition"] = (
        f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname)}"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/batch-upload", response_class=HTMLResponse)
async def page_batch_upload(request: Request):
    return render_template("batch_upload.html", page="batch")


@app.get("/diagnose", response_class=HTMLResponse)
async def page_diagnose(request: Request):
    return render_template("diagnose.html", page="diagnose")


@app.get("/agent", response_class=HTMLResponse)
async def page_agent(request: Request):
    return render_template("agent.html", page="agent")

@app.get("/editor", response_class=HTMLResponse)
async def page_editor(request: Request):
    return render_template("editor.html", page="editor")

@app.get("/banks")
async def page_banks():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/questions")


if ENABLE_SEARCH:
    @app.get("/search", response_class=HTMLResponse)
    async def page_search():
        return render_template("search.html", page="search")


if ENABLE_FOCUS_MODE:
    @app.get("/focus", response_class=HTMLResponse)
    async def page_focus(request: Request):
        from config import ENABLE_FOCUS_BLACKBOARD
        return render_template("focus.html", page="focus", enable_blackboard=ENABLE_FOCUS_BLACKBOARD)
