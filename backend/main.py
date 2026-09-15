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
    """澶滈棿鑷姩绗旇宸￠€伙細鍦ㄩ厤缃殑鏃堕棿绐楀彛鍐呰嚜鍔ㄦ墽琛岀瑪璁板幓閲嶆暣鍚?+ 棰樺簱缁忓吀妯″瀷鏀堕泦 + 棰樺簱宸℃"""
    from datetime import datetime, timedelta, timezone
    from config import load_settings
    _last_patrol_date = None
    while True:
        try:
            await asyncio.sleep(60)  # 姣忓垎閽熸鏌ヤ竴娆?            s = load_settings()
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
                continue  # 浠婂ぉ宸叉墽琛岃繃
            # 鍏堝崰浣嶅綋澶╂棩鏈熷啀鎵ц锛氫换浣曚竴姝ュけ璐ラ兘涓嶄細鍦ㄧ獥鍙ｅ唴姣忓垎閽熼噸璇曪紝
            # 閬垮厤瀵圭瑪璁板弽澶嶆墽琛屽叏閲?AI 鏁寸悊/鍚堝苟锛堟渶鍧忕害240娆★級銆?            _last_patrol_date = today_str
            logger.info("Nightly note patrol: auto-organize + classic model collection started")
            # 1. 鏀堕泦棰樺簱涓殑缁忓吀妯″瀷鏍囩锛堝涓€绾夸笁绛夎銆佹墜鎷夋墜妯″瀷绛夛級
            classic_models = await _collect_classic_models()
            # 淇濆瓨鍒扮紦瀛樹緵绗旇鍒嗙被AI浣跨敤
            try:
                from services.note_service import _save_classic_models_cache
                _save_classic_models_cache(classic_models)
            except Exception as cache_error:
                logger.warning("Classic model cache save failed: %s", cache_error)
            # 2. 绗旇鑷姩鏁寸悊锛堟敞鍏ョ粡鍏告ā鍨嬩笂涓嬫枃锛?            from routers.notes import _do_auto_organize
            result = await _do_auto_organize(classic_models_context=classic_models)
            logger.info("Nightly note patrol: %s", result.get("message", "done"))
            # 鏁寸悊瀹屾垚鍚庡埛鏂扮煡璇嗘爲缂撳瓨锛岄〉闈㈡墦寮€鏃舵棤闇€鍐嶆绛夊緟 AI銆?            try:
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
            # 3. 鍙戦€佺郴缁熸秷鎭?            try:
                from services.audit_service import add_system_message
                model_hint = f"锛堝惈 {len(classic_models)} 涓粡鍏告ā鍨嬶級" if classic_models else ""
                add_system_message("system", "澶滈棿绗旇鏁寸悊瀹屾垚",
                                 f"绗旇鍘婚噸鏁村悎宸插畬鎴恵model_hint}銆?)
            except Exception as message_error:
                logger.warning("Nightly note patrol message failed: %s", message_error)
        except asyncio.CancelledError:
            logger.info("Nightly note patrol scheduler cancelled")
            break
        except Exception as e:
            logger.warning("Nightly note patrol error: %s", str(e)[:200])


async def _health_patrol_loop():
    """R39锛氭瘡 30 鍒嗛挓宸℃棰樼洰/璇曞嵎鎶ラ敊涓庢湭澶勭悊鍙嶉銆?
    - 鎵搴?status=error锛堣繎 1 灏忔椂鏂板浼樺厛锛?    - 鎵湭澶勭悊鍙嶉
    - 鏈夐棶棰樺啓 system_messages锛堥椤垫秷鎭爮锛?    - 鍙墜鍔ㄨ皟 `GET /api/health/patrol`
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
    """浠庨搴撲腑鏀堕泦缁忓吀鏁板妯″瀷/鐗╃悊妯″瀷鏍囩"""
    try:
        from models.database import async_session as _db_s
        from models.models import Question
        from sqlalchemy import select
        CLASSIC_KEYWORDS = [
            "涓€绾夸笁绛夎", "鎵嬫媺鎵嬫ā鍨?, "灏嗗啗楗┈", "鑳′笉褰?, "闃挎皬鍦?,
            "鐡滆眴鍘熺悊", "K鍨嬪浘", "绛夌Н鍙樻崲", "鎴暱琛ョ煭", "鍊嶉暱涓嚎",
            "鏃嬭浆妯″瀷", "缈绘姌妯″瀷", "涓偣妯″瀷", "瑙掑钩鍒嗙嚎妯″瀷", "寮﹀浘",
            "鍗婅妯″瀷", "涓夌嚎鍚堜竴", "鍗佸瓧妯″瀷", "璐归┈鐐?, "鎵樺嫆瀵?,
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
    # 鍚庡彴 Agent 浠诲姟瀵硅处锛氭湇鍔￠噸鍚悗璺戜换鍔＄殑鍗忕▼宸查殢杩涚▼娑堝け锛?    # 浣嗗簱閲岃繕鐣欑潃 running 鐨勮 -> 涓嶅璐︾殑璇濆墠绔案杩滄樉绀?杩涜涓?锛?    # 鐢ㄦ埛浼氫竴鐩寸瓑涓€涓案杩滀笉浼氱粨鏉熺殑浠诲姟銆?    try:
        from services import background_agent as _ba
        _n = await _ba.reconcile_on_startup()
        if _n:
            logger.warning("Reconciled %d interrupted agent tasks", _n)
    except Exception as e:
        log_error("lifespan", f"Failed to reconcile agent tasks: {e}")
    # 鏃ョ鍙兘瑙﹀彂澶栭儴 AI锛屼笉鑳介樆濉炴湇鍔″惎鍔ㄣ€?    try:
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
    # R39锛氭瘡 30 鍒嗛挓宸℃棰樼洰/璇曞嵎鎶ラ敊涓庢湭澶勭悊鍙嶉
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


app = FastAPI(title="瀛︿範鎼瓙 - 棰樼洰鏈珹gent", version="2.0.0", lifespan=lifespan)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """鎹曡幏鏈鐞嗗紓甯稿苟鍐欏叆 Err.log锛岄伩鍏嶅墠绔敹鍒?HTML/绾枃鏈?Internal Server Error銆?""
    from fastapi.responses import JSONResponse
    import traceback
    logger.exception("Unhandled exception for %s %s", request.method, request.url.path)
    trace = traceback.format_exception(type(exc), exc, exc.__traceback__)
    log_error("unhandled_exception", f"{request.method} {request.url.path}: {''.join(trace)[-4000:]}")
    return JSONResponse(status_code=500, content={"detail": "鏈嶅姟鍣ㄥ唴閮ㄩ敊璇紝璇锋煡鐪?Err.log"})

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

# 瀵嗙爜淇濇姢涓棿浠?# 榛樿瀵嗙爜鍙€氳繃鐜鍙橀噺 LEARNING_AGENT_PASSWORD 鎴?settings.json 鐨?api_password 瑕嗙洊
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

# 鍏煎鏃у紩鐢細閮ㄥ垎鑴氭湰鍙兘鐩存帴瀵煎叆 _AUTH_PASSWORD
_AUTH_PASSWORD = _DEFAULT_AUTH_PASSWORD

@app.middleware("http")
async def password_guard(request: Request, call_next):
    """绠€鍗曞瘑鐮侀獙璇侊細API 璺緞闇€鎼哄甫瀵嗙爜锛岄〉闈㈠拰闈欐€佹枃浠惰眮鍏?""
    path = request.url.path
    # 璞佸厤锛氱簿纭尮閰嶇殑璺緞銆侀潤鎬佹枃浠躲€侀潪 API 椤甸潰
    if path in _AUTH_EXACT_EXEMPT:
        return await call_next(request)
    if not path.startswith("/api/"):
        return await call_next(request)
    # 浠?Header 鎴?Query 鍙傛暟鑾峰彇瀵嗙爜
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
    # 鏈厤缃瘑鐮侊紙鐜鍙橀噺涓?settings.json 鍧囦负绌猴級= 閴存潈鏈惎鐢紝鐩存帴鏀捐銆?    # 鏃㈡湁濂戠害锛?026-09-10 淇 45 涓祴璇?401锛夛細涓棿浠舵敼涓洪€愯姹傝鍙栧悗锛?    # 娴嬭瘯妯″潡鎶?SETTINGS_FILE 閲嶅畾鍚戝埌绌轰复鏃剁洰褰曟椂 expected 鍙樼┖涓诧紝
    # 涓庢祴璇?token 鎭掍笉绛夊鑷存暣鎵?401锛涗笖鏈厤缃椂鏈氨鎷︽埅涓嶄綇绌?header锛?    # 姝ゅ垎鏀笉鏋勬垚瀹夊叏寮卞寲銆傞厤缃簡瀵嗙爜锛堢敓浜ф湇鍔″櫒锛変粛寮哄埗鎭掑畾鏃堕棿姣旇緝銆?    import hmac as _hmac
    if expected and not _hmac.compare_digest(provided, expected):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "璁块棶琚嫆缁濓紝璇锋彁渚涘瘑鐮?},
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
    """鍙叕寮€灞曠ず鎵€闇€鏂囦欢锛岀姝㈡暟鎹簱銆佹棩蹇椼€佷細璇濆拰 JSON 琚潤鎬佷笅杞姐€?""
    normalized = str(file_path or "").replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    if not parts or any(part in ("", ".", "..") or not _SAFE_STORAGE_SEGMENT.fullmatch(part) for part in parts):
        raise HTTPException(status_code=404, detail="鏂囦欢涓嶅瓨鍦?)

    extension = os.path.splitext(parts[-1])[1].lower()
    allowed = False
    if parts[0] == "vendor" and len(parts) >= 2:
        allowed = extension in _PUBLIC_VENDOR_EXTENSIONS
    elif parts[0] == "questions" and len(parts) == 3 and _is_valid_diagram_qid(parts[1]):
        if extension == ".svg":
            # 浠呭厑璁哥粡杩?SVG 娑堟瘨娴佺▼鍐欏叆鐨?reference.svg / diagram_N.svg
            # 锛堢湡瀹為鐩墠鏈夌ず鎰忓浘锛屾棤闇€鏌ュ簱锛涘鐢熶綔绛斿浘鍙湁鍏夋爡鏂囦欢锛?            allowed = bool(_SAFE_QUESTION_SVG_NAME.fullmatch(parts[-1]))
        else:
            # 瀛︾敓浣滅瓟鍥?鎼滈涓存椂璁板綍涓庨搴撳師鍥惧叡鐢?questions 鐩綍锛?            # 涓存椂鏌ヨ璁板綍锛堝惈瀛︾敓鎵嬪啓浣滅瓟锛変笉寰楅€氳繃鍏紑闈欐€佽矾寰勮鍙栥€?            from models.database import async_session as _storage_db
            from models.models import Question as _StorageQ
            async with _storage_db() as _db:
                _q = await _db.get(_StorageQ, parts[1])
                if _q is not None:
                    st = getattr(_q, "source_type", None)
                    bk = getattr(_q, "bank", "") or ""
                    # 鍚屾椂妫€鏌?source_type 涓?bank锛岄槻姝㈡棫鏁版嵁 source_type 涓虹┖浣?bank 宸叉爣璁颁负涓存椂鏌ヨ
                    if st in ("search_query", "correction_query") or bk in ("search_queries", "correction_queries"):
                        raise HTTPException(status_code=404, detail="鏂囦欢涓嶅瓨鍦?)
            allowed = extension in _PUBLIC_RASTER_IMAGE_EXTENSIONS
    elif parts[0] == "notes" and len(parts) == 3 and _is_valid_diagram_qid(parts[1]):
        allowed = extension in _PUBLIC_RASTER_IMAGE_EXTENSIONS
    elif parts[0] == "corrections" and 3 <= len(parts) <= 5:
        allowed = extension in _PUBLIC_RASTER_IMAGE_EXTENSIONS
    if not allowed:
        raise HTTPException(status_code=404, detail="鏂囦欢涓嶅瓨鍦?)

    storage_root = os.path.realpath(STORAGE_DIR)
    disk_path = os.path.realpath(os.path.join(storage_root, *parts))
    try:
        if os.path.commonpath([storage_root, disk_path]) != storage_root:
            raise HTTPException(status_code=404, detail="鏂囦欢涓嶅瓨鍦?)
    except ValueError:
        raise HTTPException(status_code=404, detail="鏂囦欢涓嶅瓨鍦?)
    if not os.path.isfile(disk_path):
        raise HTTPException(status_code=404, detail="鏂囦欢涓嶅瓨鍦?)
    resp = FileResponse(disk_path)
    # 闃叉鏋氫妇涓庣紦瀛橈細鍏紑棰樼洰鍥炬寜绉佹湁缂撳瓨锛屼复鏃?绉佹湁鏁版嵁 0 缂撳瓨
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
# 鍚庡彴浠诲姟涓庤绋匡紙P3/P7锛氬弻绔帹鐞?+ 澶囪锛?from routers.agent_tasks import router as agent_tasks_router
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
from models import feed_models  # noqa: F401  # 鑷嫑绱犳潗姣忔棩涓€鏉?鈥?娉ㄥ唽鍒?Base.metadata 瑙﹀彂寤鸿〃
app.include_router(feed.router)



@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "瀛︿範鎼瓙Agent v2"}


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """PWA manifest锛氭墜鏈烘祻瑙堝櫒鍙?娣诲姞鍒颁富灞忓箷"锛堝浘鏍?鍚嶇О/鐙珛绐楀彛褰㈡€侊級銆?""
    manifest_data = {
        "name": "瀛︿範鎼瓙",
        "short_name": "瀛︿範鎼瓙",
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


# ===================== HTML 椤甸潰璺敱 =====================

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
    # 闈欐€佽矾寰?/correctCenter 蹇呴』鍦ㄤ换浣曞姩鎬?/correct/{...} 璺緞涔嬪墠娉ㄥ唽锛岄伩鍏嶈鍔ㄦ€佹鎹曡幏銆?    @app.get("/correctCenter", response_class=HTMLResponse)
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
    """璇剧鐙珛椤碉細鎻愬墠澶囪浜х墿鐨勫垪琛?/ 闃呰 / 缂栬緫 / 绂荤嚎缂撳瓨鍏ュ彛銆?
    姝ゅ墠璇剧鍙兘缁?Agent 瀵硅瘽宸ュ叿璇诲啓 + HTTP API锛?*娌℃湁椤甸潰** 鈥斺€?    瀛︾敓澶囧畬璇炬兂鑷繁鐪嬩竴閬嶏紝鍙兘璁?Agent 蹇碉紝鎴栨墜鎵?API銆?    """
    return render_template("lessons.html", page="lessons")


@app.get("/feedback", response_class=HTMLResponse)
async def page_feedback(request: Request):
    """闂鍙嶉椤碉細瀛︾敓鎻愪氦闂锛涘贰妫€鑴氭湰瀹氭湡璇诲彇銆?""
    return render_template("feedback.html", page="feedback")


@app.get("/feedback", response_class=HTMLResponse)
async def page_feedback(request: Request):
    """闂鍙嶉椤碉紙R39锛夛細棰樼洰/璇曞嵎/鍔熻兘闂锛屽贰妫€寰幆浼氳銆?""
    return render_template("feedback.html", page="feedback")


def _apk_version_from_gradle() -> str:
    """浠?android/app/build.gradle.kts 瑙ｆ瀽 APK 鐗堟湰锛岃繑鍥?"v1.5锛坴ersionCode 6锛? 鎴栫┖涓层€?
    涓轰粈涔堣鏋勫缓鑴氭湰鑰屼笉鏄 APK 鏈韩锛氱函 Python 瑙ｆ瀽浜岃繘鍒?AndroidManifest 闇€瑕侀澶栦緷璧?    锛堥」鐩交閲忓寲浼樺厛锛夛紝鑰屾瀯寤鸿剼鏈槸**鐗堟湰鍙风殑鍞竴婧愬ご** 鈥斺€?鍑哄寘鏃跺仛鐨勫氨鏄畠銆?    璇讳笉鍒板氨杩斿洖绌轰覆锛屾ā鏉夸晶浼氱渷鐣ョ増鏈彞锛屼笉鏄剧ず杩囨湡鏁板瓧锛堝畞鍙笉璇达紝涔熶笉璇撮敊锛夈€?    """
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "android", "app", "build.gradle.kts")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        name_m = re.search(r'versionName\s*=\s*"([^"]+)"', text)
        code_m = re.search(r"versionCode\s*=\s*(\d+)", text)
        if name_m and code_m:
            return f"v{name_m.group(1)}锛坴ersionCode {code_m.group(1)}锛?
    except Exception as exc:
        logger.warning("璇诲彇 APK 鐗堟湰澶辫触: %s", exc)
    return ""


@app.get("/download", response_class=HTMLResponse)
async def page_download(request: Request):
    """涓嬭浇椤点€?
    鏂囦欢浣撶Н銆佹墦鍖呮棩鏈?*浠庣鐩樼湡瀹炶鍙?*锛岀増鏈彿**浠?android/app/build.gradle.kts 璇诲彇**锛?    涓夋牱閮戒笉鍐嶆墜鍐欍€?    璧峰洜锛?026-09-11 鍙戠幇涓嬭浇椤靛啓鐫€ "APK v1.3锛坴ersionCode 4锛?銆?    "妗岄潰绔?鎵撳寘鏃ユ湡 2026-09-10"锛岃€岀鐩樹笂鐨?APK 鍏跺疄鏄?09-05 鐨勬棫鍖?鈥斺€?    鎵嬪啓鐨勬暟瀛楀繀鐒朵細婕傜Щ锛岃€屾紓绉荤殑鍚庢灉鏄敤鎴蜂笅杞戒簡鏃у寘鍗翠互涓烘嬁鍒颁簡鏂扮増銆?    R24 琛ュ厖锛氬綋鏃跺彧鏀逛簡浣撶Н/鏃ユ湡锛?*鐗堟湰鍙蜂粛鏄墜鍐?*锛堟灉鐒跺張婕傜Щ鎴?    "v1.4锛坴ersionCode 5锛?鑰屽疄闄呭凡鏄?1.5/6锛夈€傜幇鍦ㄦ敼涓鸿В鏋愭瀯寤鸿剼鏈紝
    鐗堟湰鍙峰彧鍙兘鏉ヨ嚜"涓嬩竴娆＄湡鐨勮鍑哄寘鐨勫湴鏂?銆?    """
    downloads_dir = os.path.join(STATIC_DIR, "downloads")
    out = {}
    apk_version = _apk_version_from_gradle()
    for key, fname in (("apk", "瀛︿範鎼瓙-Android.apk"),
                       ("desktop", "瀛︿範鎼瓙-妗岄潰绔?20260912.zip")):
        path = os.path.join(downloads_dir, fname)
        info = {"exists": os.path.exists(path), "filename": fname,
                "size_text": "", "date_text": "", "version_text": ""}
        if key == "apk":
            info["version_text"] = apk_version
        if info["exists"]:
            st = os.stat(path)
            mb = st.st_size / 1024 / 1024
            # 灏忎簬 1MB 鐢?KB 鏄剧ず锛屽惁鍒?MB锛涗繚鐣欎竴浣嶅皬鏁?            info["size_text"] = (f"{st.st_size / 1024:.0f} KB" if mb < 1
                                 else f"{mb:.1f} MB")
            info["date_text"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
        out[key] = info
    return render_template("download.html", page="download", dl=out)


@app.get("/dl/{kind}")
async def download_client(kind: str):
    """瀹㈡埛绔笅杞藉嚭鍙ｏ紙R33锛夈€?
    涓轰粈涔堜笉鐢ㄨ８ /static/downloads/锛?    Windows 涓?mimetypes 甯镐笉璁よ瘑 .apk锛孲taticFiles 浼氬洖
    application/octet-stream锛屾祻瑙堝櫒鍙﹀瓨涓?**xxx.bin**锛堢敤鎴峰疄娴嬨€屽畨瑁呭寘鏄?bin銆嶏級銆?    杩欓噷寮哄埗姝ｇ‘ MIME + RFC 5987 鏂囦欢鍚嶃€?    """
    from urllib.parse import quote
    mapping = {
        "android": ("瀛︿範鎼瓙-Android.apk", "application/vnd.android.package-archive"),
        "apk": ("瀛︿範鎼瓙-Android.apk", "application/vnd.android.package-archive"),
        "desktop": ("瀛︿範鎼瓙-妗岄潰绔?20260912.zip", "application/zip"),
        "windows": ("瀛︿範鎼瓙-妗岄潰绔?20260912.zip", "application/zip"),
    }
    if kind not in mapping:
        raise HTTPException(404, "鏈煡涓嬭浇椤?)
    fname, ctype = mapping[kind]
    path = os.path.join(STATIC_DIR, "downloads", fname)
    if not os.path.isfile(path):
        raise HTTPException(404, "瀹夎鍖呭皻鏈笂浼犲埌鏈嶅姟鍣?)
    ascii_name = "LearningAgent-Android.apk" if fname.endswith(".apk") else "LearningAgent-Desktop.zip"
    resp = FileResponse(path, media_type=ctype, filename=ascii_name)
    # 涓枃鍘熷悕缁欑幇浠ｆ祻瑙堝櫒锛坒ilename*=UTF-8''...锛?    resp.headers["Content-Disposition"] = (
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

