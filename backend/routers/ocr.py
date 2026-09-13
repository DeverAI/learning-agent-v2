import os
import asyncio
import base64
import json
import shutil
from typing import List, Literal
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Body
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from models.database import get_db
from models.database import async_session
from models.models import Question, ProcessingTask, gen_id
from services.ocr_service import ocr_service
from services.upload_guard import read_upload_limited
from services.ai_service import ai_service
from services.tag_unification_service import canonicalize_tags
from services.upload_session_service import summarize_upload_session
from config import QUESTIONS_DIR, STORAGE_DIR, load_settings, _atomic_write_json
from logger import get_logger, log_error

router = APIRouter(prefix="/api/ocr", tags=["ocr"])
logger = get_logger()

TASK_STATE_DIR = os.path.join(STORAGE_DIR, "task_states")
os.makedirs(TASK_STATE_DIR, exist_ok=True)

_background_tasks = set()

# 整卷上传会话串行化：同一 session 的并发上传会基于过期 question_ids 快照
# 互相覆盖（丢题/混模式），按 session_id 加锁；使用 WeakValueDictionary 自动释放
import weakref as _weakref_u
_session_upload_locks: _weakref_u.WeakValueDictionary[str, asyncio.Lock] = _weakref_u.WeakValueDictionary()


def _get_session_upload_lock(session_id: str) -> asyncio.Lock:
    lock = _session_upload_locks.get(session_id)
    if lock is not None:
        return lock
    new_lock = asyncio.Lock()
    return _session_upload_locks.setdefault(session_id, new_lock)

MAX_IMAGE_SIZE = 10 * 1024 * 1024
# 拍照上限统一出处：capture_modes 是搜题/批改/整卷多图共用的唯一契约，
# 这里以别名引用，避免两份常量漂移。
from services.capture_modes import MAX_CAPTURE_IMAGES as MAX_MULTI_IMAGES
from services.capture_modes import MAX_CAPTURE_TOTAL_SIZE as MAX_MULTI_TOTAL_SIZE
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
UPLOAD_MODES = {"one_per_image", "one_question_multi_image", "auto_split"}
VALID_IMAGE_ROLES = {"question", "analysis", "answer", "extra"}


class ImageRoleItem(BaseModel):
    role: Literal["question", "analysis", "answer", "extra"] = "extra"


class ImageRolesRequest(BaseModel):
    roles: list[ImageRoleItem] = Field(min_length=1, max_length=MAX_MULTI_IMAGES)


def _normalize_upload_mode(upload_mode: str = "", split_mode: str = "single") -> str:
    """把新旧上传参数归一成唯一模式，避免多图一题与自动分题同时生效。"""
    raw = str(upload_mode or "").strip().lower()
    aliases = {
        "single": "one_per_image",
        "multi": "one_question_multi_image",
    }
    raw = aliases.get(raw, raw)
    if not raw:
        raw = "auto_split" if str(split_mode or "").strip().lower() == "auto" else "one_per_image"
    if raw not in UPLOAD_MODES:
        raise HTTPException(400, detail="上传模式无效")
    return raw


def _split_mode_for_upload(upload_mode: str) -> str:
    return "auto" if upload_mode == "auto_split" else "single"


def _normalize_classified_roles(saved: list[dict], roles) -> list[dict]:
    """Return exactly one safe role for every saved image.

    Vision providers occasionally omit, reorder, or return malformed role entries.
    Matching by filename first and falling back to upload order prevents orphaned
    images and keeps the original question image deterministic.
    """
    raw_roles = roles if isinstance(roles, list) else []
    named = {
        str(item.get("filename", "")): item
        for item in raw_roles
        if isinstance(item, dict) and item.get("filename")
    }
    normalized = []
    for index, image in enumerate(saved):
        info = named.get(str(image.get("filename", "")))
        if not isinstance(info, dict) and index < len(raw_roles) and isinstance(raw_roles[index], dict):
            info = raw_roles[index]
        default_role = "question" if index == 0 else "extra"
        role = str((info or {}).get("role", default_role)).strip().lower()
        if role not in VALID_IMAGE_ROLES:
            role = default_role
        normalized.append({"role": role})
    if normalized and not any(item["role"] == "question" for item in normalized):
        normalized[0]["role"] = "question"
    return normalized


def _has_supported_image_signature(raw: bytes) -> bool:
    return (raw.startswith(b"\xff\xd8\xff") or raw.startswith(b"\x89PNG\r\n\x1a\n")
            or (len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))


def _mime_for_ext(ext: str) -> str:
    return {".png": "image/png", ".webp": "image/webp"}.get(ext.lower(), "image/jpeg")


async def _read_image_upload(file: UploadFile) -> tuple[bytes, str]:
    content_type = (file.content_type or "").lower()
    ext = os.path.splitext(file.filename or "image.jpg")[1].lower() or ".jpg"
    if not content_type.startswith("image/") or ext not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(400, detail="仅支持 JPG、PNG 或 WEBP 图片")
    raw = await read_upload_limited(
        file, MAX_IMAGE_SIZE,
        too_large_detail="单张图片不能超过 10MB",
        empty_detail="上传图片为空",
    )
    if not _has_supported_image_signature(raw):
        raise HTTPException(400, detail="文件内容不是有效的 JPG、PNG 或 WEBP 图片")
    return raw, ext


def _run_bg(coro, task_desc: dict = None):
    qid = task_desc.get("question_id", "?") if task_desc else "?"
    if task_desc:
        try:
            _save_task_state(task_desc)
        except Exception as exc:
            coro.close()
            error_text = str(exc) or type(exc).__name__
            async def _mark_queue_failure():
                try:
                    from models.database import async_session as sm
                    async with sm() as db:
                        q = await db.get(Question, qid)
                        if q and q.status not in ("done", "search_done"):
                            q.status = "error"
                            q.error_message = f"任务状态持久化失败: {error_text}"[:2000]
                            await db.commit()
                except Exception as inner_exc:
                    log_error("ocr.queue", f"Failed to mark queue persistence error for {qid}: {inner_exc}")
            # 必须持有任务引用，否则任务可能在执行中被垃圾回收
            _mark_task = asyncio.create_task(_mark_queue_failure())
            _background_tasks.add(_mark_task)
            _mark_task.add_done_callback(_background_tasks.discard)
            raise

    async def _wrapper():
        try:
            await coro
            if task_desc:
                _clear_task_state(qid)
        except Exception as e:
            msg = str(e) or type(e).__name__
            log_error("ocr.background", f"Task {qid} failed: {msg}")
            logger.warning("Background task %s failed: %s", qid, msg, exc_info=True)
            if task_desc:
                _clear_task_state(qid)
            try:
                from models.database import async_session as sm
                async with sm() as db:
                    q = await db.get(Question, qid)
                    if q and q.status not in ("done", "error"):
                        q.status = "error"
                        q.error_message = msg[:2000]
                        await db.commit()
            except Exception as inner_e:
                log_error("ocr.background", f"Failed to update error status for {qid}: {inner_e}")
    t = asyncio.create_task(_wrapper())
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)


def _save_task_state(desc: dict):
    qid = desc.get("question_id", "unknown")
    if not isinstance(qid, str) or not __import__("re").fullmatch(r"[a-zA-Z0-9_-]{1,64}", qid):
        raise ValueError("Invalid task question_id")
    path = os.path.join(TASK_STATE_DIR, f"{qid}.json")
    try:
        _atomic_write_json(path, desc)
    except (IOError, OSError) as e:
        log_error("ocr.queue", f"Failed to save task state for {qid}: {e}")
        raise


def _clear_task_state(qid: str):
    # 与 _save_task_state 相同的 ID 白名单，防止未来调用点误传用户输入造成越界删除
    if not isinstance(qid, str) or not __import__("re").fullmatch(r"[a-zA-Z0-9_-]{1,64}", qid):
        logger.warning("Refusing to clear task state for invalid qid: %r", qid)
        return
    path = os.path.join(TASK_STATE_DIR, f"{qid}.json")
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError as exc:
        logger.warning("Failed to clear task state for %s: %s", qid, exc)


async def resume_pending_tasks():
    if not os.path.exists(TASK_STATE_DIR):
        return
    from models.database import async_session as sm
    descriptors = []
    for fname in os.listdir(TASK_STATE_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(TASK_STATE_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                desc = json.load(f)
            qid = desc.get("question_id", "") if isinstance(desc, dict) else ""
            if not isinstance(qid, str) or not __import__("re").fullmatch(r"[a-zA-Z0-9_-]{1,64}", qid):
                raise ValueError("任务描述中的题目 ID 无效")
            descriptors.append(desc)
        except Exception as exc:
            log_error("ocr.resume", f"Invalid task state {fname}: {exc}")
            try:
                os.remove(path)
            except OSError as cleanup_exc:
                logger.warning("Failed to remove invalid task state %s: %s", path, cleanup_exc)

    # 只在应用启动恢复阶段终止旧运行态；不要把它放进通用数据库迁移，
    # 否则另一个诊断进程调用 init_db 会误伤正在运行的任务。
    async with sm() as db:
        await db.execute(
            update(ProcessingTask).where(
                ProcessingTask.status.in_(["pending", "processing"])
            ).values(status="error", error_message="服务器重启，原任务已终止")
        )
        await db.execute(
            update(Question).where(
                Question.status.in_(["pending", "processing", "generating_solution", "search_staged"])
            ).values(status="error", error_message="服务器重启，等待自动恢复或手动重试")
        )
        await db.commit()

    resumed = 0
    for desc in descriptors:
        qid = desc["question_id"]
        try:
            async with sm() as db:
                q = await db.get(Question, qid)
                if not q or q.is_resolved or q.status in ("done", "search_done"):
                    _clear_task_state(qid)
                    continue
                multi_images = list(getattr(q, "multi_images", None) or []) if q else []
            split_mode = desc.get("split_mode", "")
            user_hint = desc.get("user_hint", "")
            tags = desc.get("tags", [])
            user_grade = desc.get("user_grade", "")
            bank = desc.get("bank", "default")
            session_id = desc.get("session_id", "")
            task_type = desc.get("type", "")
            if task_type == "search_ocr":
                from routers.search import _run_search_ocr
                _run_bg(_run_search_ocr(qid, desc.get("capture_mode", "single_question")), desc)
            elif task_type == "process_existing_ocr":
                _run_bg(ocr_service.process_image(
                    qid, desc.get("user_hint", ""), desc.get("tags", []),
                    desc.get("user_grade", ""), skip_ocr=True,
                    existing_ocr_text=desc.get("existing_ocr_text", ""),
                    existing_subject=desc.get("existing_subject", ""),
                    existing_grade=desc.get("existing_grade", ""),
                    existing_tags=desc.get("existing_tags", []), search_only=False,
                ), desc)
            elif split_mode == "auto" and desc.get("split_complete"):
                _run_bg(_resume_split_processing(desc), desc)
            elif split_mode == "auto":
                _run_bg(_split_and_process(qid, user_hint, tags, user_grade, bank, session_id), desc)
            else:
                _run_bg(ocr_service.process_image(qid, user_hint, tags, user_grade, multi_images=multi_images), desc)
            resumed += 1
        except Exception as exc:
            log_error("ocr.resume", f"Failed to resume task {qid}: {exc}")
            _clear_task_state(qid)
    if resumed:
        logger.info("Resumed %d pending tasks", resumed)


async def _resume_split_processing(desc: dict):
    from models.database import async_session as sm
    ids = [str(qid) for qid in desc.get("split_question_ids", []) if qid]
    for qid in ids:
        async with sm() as db:
            q = await db.get(Question, qid)
            if not q or q.is_resolved or q.status == "done":
                continue
            existing = {
                "ocr_text": q.ocr_text or "", "subject": q.subject or "",
                "grade": q.grade or "", "tags": q.knowledge_tags or [],
            }
        await ocr_service.process_image(
            qid, desc.get("user_hint", ""), desc.get("tags", []), desc.get("user_grade", ""),
            skip_ocr=True, existing_ocr_text=existing["ocr_text"],
            existing_subject=existing["subject"], existing_grade=existing["grade"],
            existing_tags=existing["tags"],
        )


def _check_keys():
    s = load_settings()
    ds = s.get("deepseek_api_key", "")
    zp = s.get("zhipuai_api_key", "")
    km = s.get("kimi_api_key", "")
    ai_service._reload()
    has_solver = bool(ds or ai_service.custom_scope_map.get("solve"))
    has_vision = bool(zp or km)
    missing = []
    if not has_solver:
        missing.append("解题模型（DeepSeek 或 solve 自定义 API）")
    if not has_vision:
        missing.append("视觉模型（智谱GLM 或 Kimi）")
    if missing:
        raise HTTPException(400, f"缺少可用能力: {', '.join(missing)}。请在「API 设置」页面配置。")


@router.post("/upload")
async def upload_image(file: UploadFile = File(...),
                        bank: str = Form("default"),
                        db: AsyncSession = Depends(get_db)):
    """上传单张图片，返回 question_id"""
    raw, ext = await _read_image_upload(file)
    qid = gen_id()
    folder = os.path.join(QUESTIONS_DIR, qid)
    os.makedirs(folder, exist_ok=False)

    path = os.path.join(folder, f"original{ext}")
    try:
        with open(path, "wb") as w:
            w.write(raw)
            w.flush()
            os.fsync(w.fileno())

        db.add(Question(id=qid, folder_path=folder, raw_image_path=path,
                        status="staged", source_type="photo", bank=bank,
                        capture_mode="single_question"))
        await db.commit()
    except Exception:
        await db.rollback()
        shutil.rmtree(folder, ignore_errors=True)
        raise

    return {
        "question_id": qid,
        "image_url": f"/storage/questions/{qid}/{os.path.basename(path)}",
        "message": "已暂存",
    }


@router.post("/upload-multi")
async def upload_multi(
    files: List[UploadFile] = File(...),
    bank: str = Form("default"),
    user_hint: str = Form(""),
    user_tags: str = Form(""),
    user_grade: str = Form(""),
    split_mode: str = Form("single"),
    db: AsyncSession = Depends(get_db)
):
    """上传多张图片作为同一道题，识别每张图的角色并暂存"""
    if not files:
        raise HTTPException(400, detail="请上传至少一张图片")
    if len(files) > MAX_MULTI_IMAGES:
        raise HTTPException(400, detail=f"一次最多上传 {MAX_MULTI_IMAGES} 张图片")
    uploads = []
    total_size = 0
    for file in files:
        raw, ext = await _read_image_upload(file)
        total_size += len(raw)
        # 边读边累计，超限立即中断，避免最坏情况全量驻留内存后才拒绝
        if total_size > MAX_MULTI_TOTAL_SIZE:
            raise HTTPException(413, detail="本次图片总大小不能超过 60MB")
        uploads.append((file, raw, ext))

    qid = gen_id()
    folder = os.path.join(QUESTIONS_DIR, qid)
    os.makedirs(folder, exist_ok=False)

    saved = []
    try:
        for i, (file, raw, ext) in enumerate(uploads):
            dst = os.path.join(folder, f"original_{i}{ext}")
            with open(dst, "wb") as w:
                w.write(raw)
                w.flush()
                os.fsync(w.fileno())
            saved.append({"filename": file.filename or f"original_{i}.jpg", "path": dst})
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    # 识别每张图的角色（直接复用内存中的字节，避免二次读盘）
    images_for_classify = []
    for (file, raw, ext), item in zip(uploads, saved):
        images_for_classify.append({
            "filename": item["filename"],
            "base64": base64.b64encode(raw).decode(),
            "mime_type": _mime_for_ext(ext),
        })

    try:
        roles = await ai_service.glm4v_classify_image_roles(images_for_classify)
    except Exception as exc:
        logger.warning("Image role classification failed, using upload order: %s", exc)
        roles = [{"role": "question" if i == 0 else "extra"} for i in range(len(saved))]
    roles = _normalize_classified_roles(saved, roles)

    multi_images = []
    image_roles = []
    for idx, (item, role_info) in enumerate(zip(saved, roles)):
        role = "question"
        if isinstance(role_info, dict):
            role = role_info.get("role", "question")
        multi_images.append({
            "path": item["path"],
            "filename": item["filename"],
            "role": role,
        })
        image_roles.append({
            "path": item["path"],
            "role": role,
            "order": idx,
        })

    # 兼容旧流程：将第一张 question 图（或第一张图）复制为 original.jpg
    question_items = [img for img in multi_images if img.get("role") == "question"]
    primary_path = question_items[0]["path"] if question_items else multi_images[0]["path"]
    primary_ext = os.path.splitext(primary_path)[1].lower() or ".jpg"
    original_path = os.path.join(folder, f"original{primary_ext}")
    try:
        shutil.copy(primary_path, original_path)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise

    tags = [t.strip() for t in user_tags.split(",") if t.strip()] if user_tags else []

    db.add(Question(
        id=qid,
        folder_path=folder,
        raw_image_path=original_path,
        status="staged",
        source_type="photo",
        bank=bank,
        user_hint=user_hint,
        grade=user_grade,
        knowledge_tags=tags,
        image_roles=image_roles,
        multi_images=multi_images,
        capture_mode="single_question",
    ))
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        shutil.rmtree(folder, ignore_errors=True)
        raise

    image_urls = [
        f"/storage/questions/{qid}/{os.path.basename(item['path'])}"
        for item in multi_images
    ]
    return {
        "question_id": qid,
        "image_roles": image_roles,
        "image_urls": image_urls,
        "message": "已暂存",
    }


@router.put("/{question_id}/image-roles")
async def update_image_roles(
    question_id: str,
    data: ImageRolesRequest,
    db: AsyncSession = Depends(get_db)
):
    """更新已暂存多图题目的图片角色，并重新同步主图 original.jpg"""
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, detail="题目不存在")
    if q.status != "staged":
        raise HTTPException(409, detail="题目已开始处理，不能再修改图片角色")

    new_roles = [item.model_dump() for item in data.roles]

    # 按现有 multi_images 顺序校验，角色只接受合法值
    multi_images = getattr(q, "multi_images", None) or []
    updated_multi = []
    updated_roles = []
    for i, img in enumerate(multi_images):
        role_item = new_roles[i] if i < len(new_roles) else {}
        role = str(role_item.get("role", img.get("role", "extra"))).lower()
        if role not in VALID_IMAGE_ROLES:
            role = "extra"
        updated_img = dict(img)
        updated_img["role"] = role
        updated_multi.append(updated_img)
        updated_roles.append({
            "path": updated_img.get("path", ""),
            "role": role,
            "order": i,
        })

    # 重新选择主图：role=question 的第一张，否则第一张
    question_items = [img for img in updated_multi if img.get("role") == "question"]
    primary = question_items[0] if question_items else (updated_multi[0] if updated_multi else None)
    if primary:
        src = primary.get("path", "")
        folder_real = os.path.realpath(q.folder_path or "")
        src_real = os.path.realpath(str(src)) if src else ""
        try:
            src_is_inside = bool(
                src_real and folder_real
                and os.path.commonpath([folder_real, src_real]) == folder_real
                and os.path.isfile(src_real)
            )
        except ValueError:
            src_is_inside = False
        if src_is_inside:
            ext = os.path.splitext(src_real)[1].lower() or ".jpg"
            original_path = os.path.join(folder_real, f"original{ext}")
            shutil.copy(src_real, original_path)
            q.raw_image_path = original_path
        else:
            logger.warning("Skipped unsafe primary image copy for %s: %s", question_id, src)

    q.multi_images = updated_multi
    q.image_roles = updated_roles
    await db.commit()
    return {"message": "已更新", "image_roles": updated_roles, "multi_images": updated_multi}


@router.delete("/{question_id}/image/{image_index}")
async def delete_staged_image(
    question_id: str,
    image_index: int,
    db: AsyncSession = Depends(get_db),
):
    """删除暂存多图中的一张，避免前端删除后留下孤儿文件。"""
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, detail="题目不存在")
    if q.status != "staged":
        raise HTTPException(409, detail="题目已开始处理，不能删除单张图片")

    images = list(getattr(q, "multi_images", None) or [])
    roles = list(getattr(q, "image_roles", None) or [])
    if image_index < 0 or image_index >= len(images):
        raise HTTPException(400, detail="图片索引无效")

    item = images.pop(image_index)
    if image_index < len(roles):
        roles.pop(image_index)
    folder = os.path.abspath(q.folder_path or "")
    path = os.path.abspath(str(item.get("path", ""))) if isinstance(item, dict) else ""

    if not images:
        await db.delete(q)
        await db.commit()
        if folder and os.path.isdir(folder):
            questions_root = os.path.realpath(QUESTIONS_DIR)
            folder_real = os.path.realpath(folder)
            try:
                if (os.path.commonpath([questions_root, folder_real]) == questions_root
                        and folder_real != questions_root):
                    shutil.rmtree(folder_real, ignore_errors=True)
                else:
                    logger.warning("Skipped unsafe staged question folder deletion: %s", folder)
            except (OSError, ValueError) as exc:
                logger.warning("Staged question deleted but folder cleanup failed for %s: %s", folder, exc)
        return {
            "message": "已删除题目",
            "deleted_question": True,
            "image_roles": [],
            "multi_images": [],
            "image_urls": [],
        }

    q.multi_images = images
    q.image_roles = [
        {**(roles[i] if i < len(roles) and isinstance(roles[i], dict) else {}),
         "path": img.get("path", ""), "role": img.get("role", "extra"), "order": i}
        for i, img in enumerate(images) if isinstance(img, dict)
    ]
    question_items = [img for img in images if isinstance(img, dict) and img.get("role") == "question"]
    primary = question_items[0] if question_items else images[0]
    q.raw_image_path = primary.get("path") if isinstance(primary, dict) else q.raw_image_path
    await db.commit()
    if folder and path:
        try:
            if os.path.commonpath([folder, path]) == folder and os.path.isfile(path):
                os.remove(path)
        except (OSError, ValueError) as exc:
            logger.warning("Question metadata updated but staged image cleanup failed for %s: %s", path, exc)
    image_urls = [
        f"/storage/questions/{question_id}/{os.path.basename(img['path'])}"
        for img in q.multi_images
        if isinstance(img, dict) and img.get("path")
    ]
    return {"message": "已删除图片", "deleted_question": False,
            "image_roles": q.image_roles, "multi_images": q.multi_images,
            "image_urls": image_urls}


@router.post("/batch-process")
async def batch_process(
    question_ids: str = Form(""),
    user_hint: str = Form(""),
    user_tags: str = Form(""),
    user_grade: str = Form(""),
    bank: str = Form("default"),
    split_mode: str = Form("single"),
    upload_mode: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    _check_keys()
    canonical_mode = _normalize_upload_mode(upload_mode, split_mode)
    split_mode = _split_mode_for_upload(canonical_mode)
    ids = list(dict.fromkeys(x.strip() for x in question_ids.split(",") if x.strip()))
    if not ids:
        raise HTTPException(400, detail="请选择要处理的题目")
    if canonical_mode == "one_question_multi_image" and len(ids) != 1:
        raise HTTPException(400, detail="多图一题模式一次只能处理一个已合并题目")

    tags = [t.strip() for t in user_tags.split(",") if t.strip()] if user_tags else []
    tags = canonicalize_tags(tags)

    accepted = []
    rejected = []
    jobs = {}
    for qid in ids:
        q = await db.get(Question, qid)
        if not q:
            rejected.append({"question_id": qid, "reason": "题目不存在"})
            continue
        if q.is_resolved or q.status not in ("staged", "error"):
            rejected.append({"question_id": qid, "reason": "题目已锁定或当前状态不允许普通处理"})
            continue
        claim = await db.execute(
            update(Question).where(
                Question.id == qid, Question.status.in_(["staged", "error"]),
                Question.is_resolved.is_(False),
            ).values(
                grade=user_grade or q.grade, knowledge_tags=tags or q.knowledge_tags,
                bank=bank or q.bank, status="pending", error_message="",
            )
        )
        if claim.rowcount == 1:
            accepted.append(qid)
            target_bank = bank or q.bank or "default"
            jobs[qid] = {
                "multi_images": list(getattr(q, "multi_images", None) or []),
                "desc": {"question_id": qid,
                         "type": "split_process" if split_mode == "auto" else "process_image",
                         "split_mode": split_mode, "user_hint": user_hint, "tags": tags,
                         "user_grade": user_grade, "bank": target_bank},
            }
        else:
            rejected.append({"question_id": qid, "reason": "题目已被其他请求启动"})
    if not accepted:
        await db.rollback()
        raise HTTPException(409, detail="所选题目均不存在、已锁定或正在处理")
    try:
        for job in jobs.values():
            _save_task_state(job["desc"])
        await db.commit()
    except Exception:
        await db.rollback()
        for qid in accepted:
            _clear_task_state(qid)
        raise

    for qid in accepted:
        desc = jobs[qid]["desc"]
        if split_mode == "auto":
            _run_bg(_split_and_process(qid, user_hint, tags, user_grade, desc["bank"]), desc)
        else:
            _run_bg(ocr_service.process_image(
                qid, user_hint, tags, user_grade, multi_images=jobs[qid]["multi_images"]
            ), desc)
    return {"message": f"已启动 {len(accepted)} 道题的处理", "count": len(accepted), "rejected": rejected}


@router.post("/{question_id}/process")
async def process_single(
    question_id: str,
    user_hint: str = Form(""),
    user_tags: str = Form(""),
    user_grade: str = Form(""),
    split_mode: str = Form("single"),
    upload_mode: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    _check_keys()
    canonical_mode = _normalize_upload_mode(upload_mode, split_mode)
    split_mode = _split_mode_for_upload(canonical_mode)
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, detail="题目不存在")
    if getattr(q, "is_resolved", False):
        raise HTTPException(400, detail="已锁定的题目不支持重新处理")
    if q.status not in ("staged", "error"):
        raise HTTPException(409, detail=f"当前状态 {q.status} 不允许普通处理；已完成题目请使用重新生成或重试")

    tags = canonicalize_tags([t.strip() for t in user_tags.split(",") if t.strip()] if user_tags else [])
    claim = await db.execute(
        update(Question).where(
            Question.id == question_id, Question.status.in_(["staged", "error"]),
            Question.is_resolved.is_(False),
        ).values(
            grade=user_grade or q.grade, knowledge_tags=tags or q.knowledge_tags,
            status="pending", error_message="",
        )
    )
    if claim.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, detail="题目已被其他请求启动")
    bank = q.bank or "default"
    desc = {"question_id": question_id, "type": "split_process" if split_mode == "auto" else "process_image",
            "split_mode": split_mode, "user_hint": user_hint, "tags": tags,
            "user_grade": user_grade, "bank": bank}
    try:
        _save_task_state(desc)
        await db.commit()
    except Exception:
        await db.rollback()
        _clear_task_state(question_id)
        raise
    if split_mode == "auto":
        _run_bg(_split_and_process(question_id, user_hint, tags, user_grade, bank), desc)
    else:
        _run_bg(ocr_service.process_image(question_id, user_hint, tags, user_grade, multi_images=getattr(q, "multi_images", None)), desc)
    return {"message": "已加入处理队列", "question_id": question_id}


def _mark_split_fallback(question_id: str, reason: str):
    """拆题失败降级单题时，在任务状态文件留下可见标记（排查/续跑窗口可见）。

    任务正常完成后状态文件会被 _clear_task_state 清除；常驻的用户可见
    告警需要 DB 字段或任务面板支持（done.md 登记，待产品决策）。
    """
    try:
        state_path = os.path.join(TASK_STATE_DIR, f"{question_id}.json")
        data = {}
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError, OSError):
                data = {}
        data["split_fallback"] = True
        data["split_fallback_reason"] = (reason or "")[:200]
        _atomic_write_json(state_path, data)
    except Exception as exc:
        logger.warning("Failed to mark split fallback for %s: %s", question_id, exc)


async def _split_and_process(question_id: str, user_hint: str,
                             user_tags: list, user_grade: str, bank: str,
                             session_id: str = ""):
    from models.database import async_session as sm
    # 幂等恢复：若上次切题已提交子题但状态文件没来得及更新（提交与写状态之间崩溃），
    # 直接按已有 split_question_ids 续跑，绝不重新切题，避免重复建题产生孤儿。
    state_path = os.path.join(TASK_STATE_DIR, f"{question_id}.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                prev = json.load(f)
            if isinstance(prev, dict) and prev.get("split_complete") and prev.get("split_question_ids"):
                await _resume_split_processing(prev)
                return
        except (json.JSONDecodeError, IOError, OSError):
            pass
    q_multi_images = None
    try:
        async with sm() as db:
            q = await db.get(Question, question_id)
            if not q or not q.raw_image_path:
                return
            with open(q.raw_image_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode()
            # 保留多图信息，回退单题处理时仍需完整图片集
            q_multi_images = getattr(q, "multi_images", None)

        # 拆题判定（MiMo-first，Fact.md 模型分工定规）；失败共尝试 2 次（初试+1 重试）
        result = None
        for _attempt in (1, 2):
            try:
                result = await ai_service.vision_mimo_first(img_b64,
                    '判断图片中是否含多道独立题目。注意：不要将大板块标题（如\u201c一、追寻碳的足迹\u201d）当成题目。'
                    "对于化学等学科，需要判断每道小题是否需要大标题作为背景支撑，需要的话将背景文本合并到小题中。"
                    "不要计算答案。还要判断整页的学科、年级和知识点。输出JSON: "
                    '{"has_multiple":bool,"subject":"学科","grade":"年级","knowledge_tags":["知识点"],'
                    '"questions":[{"index":1,"text":"题目内容（含所需背景上下文）","needs_context":bool}]}')
                if result is not None:
                    break
                logger.warning("Split detection attempt %d returned empty for %s", _attempt, question_id)
            except Exception as e:
                if _attempt == 2:
                    raise
                logger.warning("Split detection attempt %d failed for %s: %s", _attempt, question_id, str(e)[:200])
        if result is None:
            # 重试后仍无结果：与调用失败同路处理（降级单题 + 留痕），不做静默分支
            raise RuntimeError("split detection returned empty after 2 attempts")
        items = result.get("questions", []) if isinstance(result, dict) and result.get("has_multiple") else []
        items = [item for item in items if isinstance(item, dict) and str(item.get("text", "")).strip()]

        if len(items) > 50:
            raise ValueError(f"自动切题结果异常：单页返回 {len(items)} 道题，超过 50 道安全上限")

        if len(items) < 2:
            await ocr_service.process_image(question_id, user_hint, user_tags, user_grade,
                                            multi_images=q_multi_images)
            return
    except Exception as e:
        log_error("ocr.split", f"Split detection failed for {question_id}: {e}")
        logger.warning("Split detection failed for %s, processing as one question: %s", question_id, e, exc_info=True)
        _mark_split_fallback(question_id, str(e)[:200])
        # 同时写进 DB 的 audit_flags —— 状态文件那份在任务正常完成时会被
        # _clear_task_state 删掉，只有 DB 这份能活到用户看见。
        # 用户现象：上传一整页 5 道题，结果只出 1 道（题干是整页），此前没有任何提示。
        try:
            from services.audit_service import flag_question
            await flag_question(question_id, "split_fallback",
                                f"整页切题未成功（{str(e)[:120]}），已按单题处理。"
                                f"这一条可能是整页多题，建议核对后手动拆题。", auto=True)
        except Exception as flag_exc:
            # 留痕失败不能拖垮识别本身，但必须记下来（否则又变回静默）
            log_error("ocr.split", f"Failed to write split_fallback flag for {question_id}: {flag_exc}")
        await ocr_service.process_image(question_id, user_hint, user_tags, user_grade,
                                        multi_images=q_multi_images)
        return

    sub_items = []
    try:
        async with sm() as db:
            q = await db.get(Question, question_id)
            if not q:
                return
            first_text = str(items[0].get("text", "")).strip()
            split_subject = str(result.get("subject", "") or q.subject or "").strip()
            split_grade = user_grade or str(result.get("grade", "") or q.grade or "").strip()
            split_tags = canonicalize_tags(list(user_tags or []) + list(result.get("knowledge_tags", []) or []))
            q.ocr_text = first_text
            q.question_html = ""
            q.status = "pending"
            q.subject = split_subject
            q.grade = split_grade
            q.knowledge_tags = split_tags
            src_img = q.raw_image_path if q else ""
            ext = (os.path.splitext(src_img)[1] or ".jpg") if src_img else ".jpg"

            for item in items[1:]:
                sid = gen_id()
                sdir = os.path.join(QUESTIONS_DIR, sid)
                os.makedirs(sdir, exist_ok=True)
                dst = os.path.join(sdir, f"original{ext}")
                if src_img and os.path.exists(src_img):
                    try:
                        src_real = os.path.realpath(src_img)
                        qdir_real = os.path.realpath(QUESTIONS_DIR)
                        if os.path.commonpath([qdir_real, src_real]) == qdir_real:
                            shutil.copy(src_img, dst)
                        else:
                            logger.warning("Skipped unsafe split source copy for %s: %s", sid, src_img)
                            dst = ""
                    except (OSError, ValueError) as _e:
                        logger.warning("Split source validation failed for %s: %s", sid, _e)
                        dst = ""
                txt = item.get("text", "")
                db.add(Question(id=sid, folder_path=sdir, raw_image_path=dst,
                                status="pending", source_type="photo",
                                subject=split_subject, grade=split_grade, knowledge_tags=split_tags,
                                ocr_text=txt, bank=bank))
                sub_items.append((sid, txt))
            if session_id and sub_items:
                from models.models import UploadSession
                session = await db.get(UploadSession, session_id)
                if session:
                    session.question_ids = list(dict.fromkeys(
                        list(session.question_ids or []) + [sid for sid, _ in sub_items]
                    ))
            # 先落盘"将要提交的子题清单"再提交：若提交失败，状态描述可被清除后重切；
            # 若提交成功但随后进程崩溃，恢复入口能凭 split_complete 续跑既有子题，绝不重复建题。
            split_desc = {
                "question_id": question_id, "type": "split_process", "split_mode": "auto",
                "split_complete": True, "split_question_ids": [question_id] + [sid for sid, _ in sub_items],
                "user_hint": user_hint, "tags": user_tags, "user_grade": user_grade,
                "bank": bank, "session_id": session_id,
            }
            _save_task_state(split_desc)
            try:
                await db.commit()
            except Exception:
                _clear_task_state(question_id)
                # 提交失败：清理本轮已落盘的子题目录，避免孤儿目录堆积
                for sid, _ in sub_items:
                    sdir = os.path.join(QUESTIONS_DIR, sid)
                    shutil.rmtree(sdir, ignore_errors=True)
                raise

        await _resume_split_processing(split_desc)
    except Exception as e:
        log_error("ocr.split", f"Split processing failed for {question_id}: {e}")
        logger.warning("Split processing failed for %s: %s", question_id, e, exc_info=True)
        raise


@router.get("/status/{task_id}")
async def get_status(task_id: str, db: AsyncSession = Depends(get_db)):
    t = await db.get(ProcessingTask, task_id)
    if not t:
        raise HTTPException(404, detail="不存在")
    return t


@router.get("/queue")
async def get_queue(db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(ProcessingTask).where(
        ProcessingTask.status.in_(["pending", "processing"]))
        .order_by(ProcessingTask.created_at.desc()).limit(50))
    return r.scalars().all()


@router.post("/{question_id}/retry")
async def retry_question(
    question_id: str,
    db: AsyncSession = Depends(get_db)
):
    """重试失败的题目 - 跳过OCR，直接用已有OCR文本调用DeepSeek解题"""
    _check_keys()
    q = await db.get(Question, question_id)
    if not q:
        raise HTTPException(404, detail="题目不存在")
    if getattr(q, "is_resolved", False):
        raise HTTPException(400, detail="已锁定的题目不支持重试")

    # 原子抢占：与 process_single/batch_process 一致，用 UPDATE+rowcount 防止并发重复启动
    claim = await db.execute(
        update(Question)
        .where(Question.id == question_id,
               Question.status.in_(["error", "staged", "done"]),
               Question.is_resolved.is_(False))
        .values(status="pending", error_message="")
    )
    if claim.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, detail="题目已被其他请求启动或状态已变化")

    # If we already have ocr_text, skip OCR and use existing data
    if q.ocr_text and q.ocr_text.strip():
        desc = {
            "question_id": question_id, "type": "process_existing_ocr", "split_mode": "single",
            "user_hint": q.user_hint or "", "tags": q.knowledge_tags or [],
            "user_grade": q.grade or "", "bank": q.bank or "default",
            "existing_ocr_text": q.ocr_text or "", "existing_subject": q.subject or "",
            "existing_grade": q.grade or "", "existing_tags": q.knowledge_tags or [],
        }
        task_coro = ocr_service.process_image(
            question_id,
            q.user_hint or "",
            q.knowledge_tags or [],
            q.grade or "",
            skip_ocr=True,
            existing_ocr_text=q.ocr_text or "",
            existing_subject=q.subject or "",
            existing_grade=q.grade or "",
            existing_tags=q.knowledge_tags or [],
            multi_images=getattr(q, "multi_images", None),
        )
    else:
        desc = {
            "question_id": question_id, "type": "process_image", "split_mode": "single",
            "user_hint": q.user_hint or "", "tags": q.knowledge_tags or [],
            "user_grade": q.grade or "", "bank": q.bank or "default",
        }
        task_coro = ocr_service.process_image(
            question_id, q.user_hint or "", q.knowledge_tags or [], q.grade or "",
            multi_images=getattr(q, "multi_images", None),
        )
    try:
        _save_task_state(desc)
        await db.commit()
    except Exception:
        task_coro.close()
        await db.rollback()
        _clear_task_state(question_id)
        raise
    _run_bg(task_coro, desc)
    return {"message": "已重新加入处理队列", "question_id": question_id}


# ========== 整卷上传 ==========

@router.post("/session/create")
async def create_session(
    title: str = Form(""),
    subject: str = Form(""),
    grade: str = Form(""),
    notes: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    from models.models import UploadSession
    sid = gen_id()
    db.add(UploadSession(id=sid, title=title or "未命名试卷", subject=subject,
                          grade=grade, notes=notes, status="open"))
    await db.commit()
    return {"session_id": sid, "message": f"已创建上传会话: {title or '未命名试卷'}"}


@router.post("/session/import-pdf")
async def session_import_pdf(
    file: UploadFile = File(...),
    title: str = Form(""),
    subject: str = Form(""),
    grade: str = Form(""),
    process: bool = Form(default=False),
    db: AsyncSession = Depends(get_db)
):
    """电子版真题/试卷 PDF 导入整卷会话。

    诚实边界（用户已确认材料形态之②）：
    - **文字版 PDF**：pypdf 抽出每页文本 → 每页一道 staged 题（ocr_text 已填）；
      process=true 时按 skip_ocr 走解题/示意图，不再二次 OCR。
    - **扫描版 PDF**：抽不出文字 → **明确失败**，指引改走拍照/智能上传，
      不生成空壳题目假装成功。
    """
    import io as _io
    filename = (file.filename or "").lower()
    if not filename.endswith(".pdf"):
        raise HTTPException(400, "请上传 PDF 文件")
    raw = await file.read()
    if not raw or len(raw) > 50 * 1024 * 1024:
        raise HTTPException(413, "PDF 为空或超过 50MB")

    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(503, "PDF 解析组件未安装，请安装 requirements 后重试")

    try:
        reader = PdfReader(_io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(400, f"PDF 无法解析：{exc}") from exc
    if len(reader.pages) == 0:
        raise HTTPException(400, "PDF 没有页面")
    if len(reader.pages) > 60:
        raise HTTPException(400, f"一次最多导入 60 页（当前 {len(reader.pages)} 页），请分卷上传")

    # R34：多源摄入（pypdf+PUA 重映射 + 页图视觉 OCR），不再只靠 pypdf 乱码文本
    from services import pdf_ingest
    ingested = await pdf_ingest.ingest_pdf_pages(raw, use_vision=True)
    if not ingested:
        raise HTTPException(400, "PDF 无法摄入（解析/渲染均失败）")

    nonempty = sum(1 for it in ingested if (it.get("text") or "").strip())
    if nonempty == 0:
        raise HTTPException(
            400,
            "这份 PDF 既抽不出文字、页图 OCR 也为空。请改用手机拍照上传。",
        )

    from models.models import UploadSession
    sid = gen_id()
    base_title = (title or "").strip()
    if not base_title:
        base_title = os.path.splitext(os.path.basename(filename))[0][:80] or "PDF导入试卷"

    ids: list[str] = []
    folders: list[str] = []
    try:
        for it in ingested:
            text = (it.get("text") or "").strip()
            if not text:
                continue
            qid = gen_id()
            folder = os.path.join(QUESTIONS_DIR, qid)
            os.makedirs(folder, exist_ok=False)
            folders.append(folder)
            # 页图落盘，process_image 可当原题图
            png = it.get("png") or b""
            img_path = ""
            if png:
                img_path = os.path.join(folder, "original.png")
                with open(img_path, "wb") as w:
                    w.write(png)
                    w.flush()
                    os.fsync(w.fileno())
            note_path = os.path.join(folder, "source.txt")
            with open(note_path, "w", encoding="utf-8") as w:
                w.write(f"来源：PDF 第 {it.get('page_no')} 页 多源摄入\n\n{text[:50000]}")
            multi_images = []
            if img_path:
                multi_images = [{"path": img_path, "filename": "original.png", "role": "question"}]
            db.add(Question(
                id=qid, folder_path=folder,
                subject=subject, grade=grade,
                status="staged",
                ocr_text=text[:50000],
                bank="default",
                source_type="pdf_page",
                capture_mode="single_question",
                capture_group_id=sid,
                capture_index=len(ids),
                raw_image_path=img_path,
                multi_images=multi_images,
            ))
            ids.append(qid)
        if not ids:
            raise HTTPException(400, "各页都未提取到文字，无法导入")
        db.add(UploadSession(
            id=sid, title=base_title, subject=subject, grade=grade,
            status="open", question_ids=ids,
            notes=f"PDF 导入：{filename}，{len(reader.pages)} 页中有文字 {len(ids)} 页",
        ))
        await db.commit()
    except HTTPException:
        await db.rollback()
        for folder in folders:
            if os.path.isdir(folder):
                shutil.rmtree(folder, ignore_errors=True)
        raise
    except Exception as exc:
        await db.rollback()
        for folder in folders:
            if os.path.isdir(folder):
                shutil.rmtree(folder, ignore_errors=True)
        log_error("ocr.import_pdf", str(exc)[:200])
        raise HTTPException(500, "PDF 导入失败") from exc

    processed = 0
    if process:
        from services import ocr_service as _ocs
        for qid in ids:
            try:
                async with async_session() as _db:
                    _q = await _db.get(Question, qid)
                    _text = (_q.ocr_text or "") if _q else ""
                desc = {
                    "question_id": qid, "type": "process_image",
                    "user_hint": "", "tags": [], "user_grade": grade,
                    "bank": "default", "session_id": sid,
                }
                _save_task_state(desc)
                _run_bg(
                    _ocs.process_image(
                        qid, "", [], grade,
                        skip_ocr=True,
                        existing_ocr_text=_text,
                        existing_subject=subject,
                        existing_grade=grade,
                    ),
                    desc,
                )
                processed += 1
            except Exception as exc:
                log_error("ocr.import_pdf_bg", f"{qid} {exc}"[:200])

    return {
        "session_id": sid,
        "total_pages": len(reader.pages),
        "imported_questions": len(ids),
        "empty_pages": len(reader.pages) - len(ids),
        "processing": processed,
        "message": f"已导入 {len(ids)} 页文字题"
                   + ("，并开始解题" if process else "；点「处理」后生成解答"),
    }


@router.post("/session/{session_id}/upload")
async def session_upload(
    session_id: str,
    files: List[UploadFile] = File(...),
    bank: str = Form("default"),
    upload_mode: str = Form("one_per_image"),
    db: AsyncSession = Depends(get_db)
):
    # 同一会话的并发上传（双击/重试/多标签页）必须串行，防止 question_ids 丢失更新
    lock = _get_session_upload_lock(session_id)
    try:
        async with lock:
            return await _session_upload_impl(session_id, files, bank, upload_mode, db)
    finally:
        if (len(_session_upload_locks) > 256 and not lock.locked()
                and not getattr(lock, "_waiters", None)
                and _session_upload_locks.get(session_id) is lock):
            _session_upload_locks.pop(session_id, None)


async def _session_upload_impl(
    session_id: str,
    files: List[UploadFile],
    bank: str,
    upload_mode: str,
    db: AsyncSession
):
    from models.models import UploadSession
    session = await db.get(UploadSession, session_id)
    if not session:
        raise HTTPException(404, detail="会话不存在")
    if not files:
        raise HTTPException(400, detail="请上传至少一张图片")
    canonical_mode = _normalize_upload_mode(upload_mode)
    existing_ids = list(session.question_ids or [])
    stored_mode = str(getattr(session, "upload_mode", "") or "one_per_image")
    if existing_ids and stored_mode != canonical_mode:
        raise HTTPException(409, detail="该整卷记录已有题目，不能切换上传模式")
    if len(files) > MAX_MULTI_IMAGES:
        raise HTTPException(400, detail=f"一次最多上传 {MAX_MULTI_IMAGES} 张图片")
    uploads = []
    total_size = 0
    for file in files:
        raw, ext = await _read_image_upload(file)
        total_size += len(raw)
        # 边读边累计，超限立即中断，避免最坏情况全量驻留内存后才拒绝
        if total_size > MAX_MULTI_TOTAL_SIZE:
            raise HTTPException(413, detail="本次图片总大小不能超过 60MB")
        uploads.append((file, raw, ext))

    session.upload_mode = canonical_mode
    ids = list(session.question_ids or [])
    created_ids = []
    created_folders = []

    if canonical_mode != "one_question_multi_image":
        capture_mode = "single_page" if canonical_mode == "auto_split" else "single_question"
        try:
            for offset, (file, raw, ext) in enumerate(uploads):
                qid = gen_id()
                folder = os.path.join(QUESTIONS_DIR, qid)
                os.makedirs(folder, exist_ok=False)
                created_folders.append(folder)
                path = os.path.join(folder, f"original{ext}")
                with open(path, "wb") as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                db.add(Question(
                    id=qid, folder_path=folder, raw_image_path=path,
                    status="staged", source_type="photo", bank=bank,
                    capture_mode=capture_mode, capture_group_id=session_id,
                    capture_index=len(ids) + offset,
                ))
                created_ids.append(qid)
            session.question_ids = ids + created_ids
            await db.commit()
        except Exception:
            await db.rollback()
            for folder in created_folders:
                if os.path.isdir(folder):
                    shutil.rmtree(folder, ignore_errors=True)
            raise
        return {
            "question_id": created_ids[0], "question_ids": created_ids,
            "session_total": len(session.question_ids or []), "upload_mode": canonical_mode,
            "message": f"已暂存 {len(created_ids)} 张图片",
        }

    qid = gen_id()
    folder = os.path.join(QUESTIONS_DIR, qid)
    os.makedirs(folder, exist_ok=False)
    created_folders.append(folder)

    try:
        # 单文件：兼容旧行为，直接保存为 original.jpg
        if len(uploads) == 1:
            file, raw, ext = uploads[0]
            path = os.path.join(folder, f"original{ext}")
            with open(path, "wb") as w:
                w.write(raw)
                w.flush()
                os.fsync(w.fileno())
            db.add(Question(id=qid, folder_path=folder, raw_image_path=path,
                             status="staged", source_type="photo", bank=bank))
        else:
            # 多文件：与 upload-multi 一致，识别角色并暂存
            saved = []
            for i, (file, raw, ext) in enumerate(uploads):
                dst = os.path.join(folder, f"original_{i}{ext}")
                with open(dst, "wb") as w:
                    w.write(raw)
                    w.flush()
                    os.fsync(w.fileno())
                saved.append({"filename": file.filename or f"original_{i}.jpg", "path": dst})

            images_for_classify = []
            for (file, raw, ext), item in zip(uploads, saved):
                images_for_classify.append({
                    "filename": item["filename"],
                    "base64": base64.b64encode(raw).decode(),
                    "mime_type": _mime_for_ext(ext),
                })

            try:
                roles = await ai_service.glm4v_classify_image_roles(images_for_classify)
            except Exception as exc:
                logger.warning("Session image role classification failed, using upload order: %s", exc)
                roles = [{"role": "question" if i == 0 else "extra"} for i in range(len(saved))]
            roles = _normalize_classified_roles(saved, roles)

            multi_images = []
            image_roles = []
            for idx, (item, role_info) in enumerate(zip(saved, roles)):
                role = "question"
                if isinstance(role_info, dict):
                    role = role_info.get("role", "question")
                multi_images.append({"path": item["path"], "filename": item["filename"], "role": role})
                image_roles.append({"path": item["path"], "role": role, "order": idx})

            question_items = [img for img in multi_images if img.get("role") == "question"]
            primary_path = question_items[0]["path"] if question_items else multi_images[0]["path"]
            primary_ext = os.path.splitext(primary_path)[1].lower() or ".jpg"
            original_path = os.path.join(folder, f"original{primary_ext}")
            shutil.copy2(primary_path, original_path)

            db.add(Question(
                id=qid,
                folder_path=folder,
                raw_image_path=original_path,
                status="staged",
                source_type="photo",
                bank=bank,
                image_roles=image_roles,
                multi_images=multi_images,
            ))

        ids.append(qid)
        session.question_ids = ids
        question = next((obj for obj in db.new if isinstance(obj, Question) and obj.id == qid), None)
        if question is not None:
            question.capture_mode = "single_question"
            question.capture_group_id = session_id
            question.capture_index = len(ids) - 1
        await db.commit()
    except Exception:
        # 写文件/分类/入库任一步失败都清理本次目录，避免孤儿图片目录残留
        await db.rollback()
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        raise
    return {
        "question_id": qid, "question_ids": [qid], "session_total": len(ids),
        "upload_mode": canonical_mode, "message": "已暂存",
    }


@router.post("/session/{session_id}/process")
async def session_process(
    session_id: str,
    user_hint: str = Form(""),
    user_tags: str = Form(""),
    user_grade: str = Form(""),
    split_mode: str = Form(""),
    upload_mode: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    _check_keys()
    from models.models import UploadSession
    session = await db.get(UploadSession, session_id)
    if not session:
        raise HTTPException(404, detail="会话不存在")
    ids = session.question_ids or []
    if not ids:
        raise HTTPException(400, detail="该会话下暂无题目")

    if upload_mode:
        canonical_mode = _normalize_upload_mode(upload_mode, split_mode)
        stored_mode = str(getattr(session, "upload_mode", "") or "one_per_image")
        if stored_mode != canonical_mode:
            raise HTTPException(409, detail="上传模式与该整卷记录不一致")
    elif split_mode:
        # 旧客户端兼容，但已有题目的整卷记录仍不允许隐式切换模式。
        canonical_mode = _normalize_upload_mode("", split_mode)
        stored_mode = str(getattr(session, "upload_mode", "") or "one_per_image")
        if stored_mode != canonical_mode:
            raise HTTPException(409, detail="上传模式与该整卷记录不一致")
    else:
        canonical_mode = _normalize_upload_mode(getattr(session, "upload_mode", "one_per_image"))
    split_mode = _split_mode_for_upload(canonical_mode)

    tags = canonicalize_tags([t.strip() for t in user_tags.split(",") if t.strip()] if user_tags else [])
    accepted = []
    rejected = []
    jobs = {}
    for qid in ids:
        q = await db.get(Question, qid)
        if not q:
            rejected.append({"question_id": qid, "reason": "题目不存在"})
            continue
        if getattr(q, "is_resolved", False):
            rejected.append({"question_id": qid, "reason": "题目已锁定"})
            continue
        if q.status not in ("staged", "error"):
            rejected.append({"question_id": qid, "reason": f"当前状态为 {q.status}"})
            continue
        final_grade = user_grade or session.grade or q.grade
        final_tags = tags or q.knowledge_tags
        final_hint = user_hint or q.user_hint
        claim = await db.execute(
            update(Question).where(
                Question.id == qid, Question.status.in_(["staged", "error"]),
                Question.is_resolved.is_(False),
            ).values(grade=final_grade, knowledge_tags=final_tags, user_hint=final_hint,
                     status="pending", error_message="")
        )
        if claim.rowcount != 1:
            rejected.append({"question_id": qid, "reason": "题目已被其他请求启动"})
            continue
        accepted.append(qid)
        target_bank = q.bank or "default"
        desc = {"question_id": qid,
                "type": "split_process" if split_mode == "auto" else "process_image",
                "split_mode": split_mode, "user_hint": final_hint, "tags": final_tags,
                "user_grade": final_grade, "bank": target_bank}
        if split_mode == "auto":
            desc["session_id"] = session_id
        jobs[qid] = {"desc": desc, "multi_images": list(q.multi_images or [])}
    if not accepted:
        await db.rollback()
        raise HTTPException(409, detail={"message": "没有可开始处理的题目", "rejected": rejected})
    session.status = "processing"
    try:
        for job in jobs.values():
            _save_task_state(job["desc"])
        await db.commit()
    except Exception:
        await db.rollback()
        for qid in accepted:
            _clear_task_state(qid)
        raise

    for qid in accepted:
        desc = jobs[qid]["desc"]
        if split_mode == "auto":
            _run_bg(_split_and_process(
                qid, desc["user_hint"], desc["tags"], desc["user_grade"],
                desc["bank"], session_id
            ), desc)
        else:
            _run_bg(ocr_service.process_image(
                qid, desc["user_hint"], desc["tags"], desc["user_grade"],
                multi_images=jobs[qid]["multi_images"]
            ), desc)

    return {"message": f"已启动 {len(accepted)} 道题的处理", "count": len(accepted),
            "accepted": accepted, "rejected": rejected}


@router.post("/session/{session_id}/make-paper")
async def session_make_paper(
    session_id: str,
    paper_type: str = Form("custom"),
    paper_size: str = Form("A4"),
    custom_prompt: str = Form(""),
    prompt_template_id: str = Form("default_paper"),
    db: AsyncSession = Depends(get_db)
):
    """将会话下的所有题目直接组卷"""
    from models.models import UploadSession
    from services.paper_service import paper_service

    session = await db.get(UploadSession, session_id)
    if not session:
        raise HTTPException(404, detail="会话不存在")
    ids = session.question_ids or []
    if not ids:
        raise HTTPException(400, detail="该会话下暂无题目")

    done_ids = []
    pending_count = 0
    error_count = 0
    for qid in ids:
        q = await db.get(Question, qid)
        if q and q.status == "done":
            done_ids.append(qid)
        elif q and q.status == "error":
            error_count += 1
        else:
            pending_count += 1

    if not done_ids:
        raise HTTPException(400, detail="该会话下暂无已处理完成的题目，请等待处理完毕后再组卷")
    if pending_count or error_count:
        raise HTTPException(409, detail=f"题目尚未全部就绪：处理中 {pending_count} 道，失败 {error_count} 道。请处理后再组卷")

    params = {
        "paper_type": paper_type,
        "subject": session.subject,
        "grade": session.grade,
        "question_ids": done_ids,
        "custom_prompt": custom_prompt or f"请将以下{len(done_ids)}道来自 {session.title} 的题目排版为试卷",
        "prompt_template_id": prompt_template_id,
        "title": session.title,
        "question_count": len(done_ids),
        "paper_size": paper_size,
        "knowledge_tags": [],
        "keyword": "",
        "extra_params": {},
    }
    try:
        paper = await paper_service.generate_paper(params, mode="modify")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log_error("ocr.make_paper", f"Session paper generation failed for {session_id}: {exc}")
        _msg = str(exc).lower()
        if "401" in _msg or "authentication" in _msg or "api key" in _msg or "illegal header" in _msg:
            raise HTTPException(status_code=503, detail="AI 服务认证失败，请检查模型 API Key 配置")
        raise HTTPException(status_code=502, detail="组卷生成失败，请稍后重试")
    session.paper_id = paper.id
    session.status = "done"
    await db.commit()
    return {"paper_id": paper.id, "title": paper.title, "message": "组卷完成"}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)):
    from models.models import UploadSession
    r = await db.execute(select(UploadSession).order_by(UploadSession.created_at.desc()).limit(50))
    sessions = r.scalars().all()
    all_ids = list({qid for session in sessions for qid in (session.question_ids or [])})
    status_map = {}
    if all_ids:
        qr = await db.execute(
            select(Question.id, Question.status, Question.is_resolved).where(Question.id.in_(all_ids))
        )
        status_map = {qid: (status, bool(resolved)) for qid, status, resolved in qr.fetchall()}
    return [summarize_upload_session(session, status_map) for session in sessions]


@router.get("/session/{session_id}")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    from models.models import UploadSession
    s = await db.get(UploadSession, session_id)
    if not s:
        raise HTTPException(404, detail="不存在")
    ids = list(s.question_ids or [])
    status_map = {}
    if ids:
        result = await db.execute(
            select(Question.id, Question.status, Question.is_resolved).where(Question.id.in_(ids))
        )
        status_map = {qid: (status, bool(resolved)) for qid, status, resolved in result.fetchall()}
    summary = summarize_upload_session(s, status_map)
    if s.status != summary["status"]:
        s.status = summary["status"]
        await db.commit()
    return summary


@router.delete("/session/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    from models.models import UploadSession
    s = await db.get(UploadSession, session_id)
    if not s:
        raise HTTPException(404, detail="不存在")
    await db.delete(s)
    await db.commit()
    return {"message": "已删除"}
