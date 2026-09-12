"""智能上传：混合内容（题目 / 笔记 / 试卷）一页一页分流。

## 为什么需要它

真实错题本一页上往往**同时**有：题干、解答过程、老师批注、公式笔记。
原先三条上传管线互相独立：
- `/api/ocr/upload-multi` → 整包当一道题
- `/api/notes/upload-images` → 整包当笔记
- `/api/ocr/sessions` → 整包当试卷

学生必须自己「先想清楚这是什么再去点哪个入口」，分错了还得删。

本模块的职责：**先看内容，再决定去哪**，并在 AI 不可用时用 OCR 文本启发式兜底，
且**永远不假装分类成功**（unknown 就是 unknown，可人工改目标）。
"""

from __future__ import annotations

import base64
import os
import re
from typing import Any, Optional

from config import QUESTIONS_DIR, STORAGE_DIR
from logger import get_logger, log_error
from models.database import async_session
from models.models import Note, Question, UploadSession, gen_id

logger = get_logger()

# 目标类型 → 中文标签 / 说明。前端下拉与路由共用这一张表（单一事实源）。
DESTINATIONS: dict[str, dict[str, str]] = {
    "question": {
        "label": "题库题目",
        "hint": "单题或练习，进 OCR→解题 管线",
    },
    "note": {
        "label": "学习笔记",
        "hint": "概念/公式/整理，进 OCR→AI 结构化笔记",
    },
    "paper": {
        "label": "试卷页",
        "hint": "整卷/半页多题，进试卷会话，可再组卷",
    },
    "unknown": {
        "label": "待确认",
        "hint": "识别不确定，默认按题目处理，可人工改",
    },
}

# OCR 文本启发式：不烧模型额度，离线/无 Key 时也能分流。
# 判据故意偏保守：多信号命中才改默认，否则 unknown。
_NOTE_HINTS = (
    "笔记", "知识点", "定义", "公式", "定理", "推导", "总结", "整理",
    "要点", "概念", "例题讲解", "错因", "方法归纳",
)
_PAPER_HINTS = (
    "试卷", "考试", "一模", "二模", "期中", "期末", "月考", "模拟卷",
    "第Ⅰ卷", "第II卷", "选择题", "填空题", "解答题", "总分",
)
_QUESTION_HINTS = (
    "已知", "求证", "解：", "解答", "证明", "计算", "化简", "作图",
    "得分", "评分", "例", "题",
)


def heuristic_classify(text: str) -> tuple[str, float, str]:
    """按 OCR 文本猜目标。返回 (dest, confidence 0-1, reason)。"""
    t = (text or "").strip()
    if not t:
        return "unknown", 0.2, "OCR 无文本"
    # 分数统计：加权命中
    scores = {"note": 0, "paper": 0, "question": 0}
    for w in _NOTE_HINTS:
        if w in t:
            scores["note"] += 1
    for w in _PAPER_HINTS:
        if w in t:
            scores["paper"] += 1
    for w in _QUESTION_HINTS:
        if w in t:
            scores["question"] += 1
    # 结构信号：题号密度高 → 试卷；「解：」多 → 题目；无题号且短 → 笔记
    qnum = len(re.findall(r"(?:^|\n)\s*(?:\d{1,2}[\.、．]|[一二三四五六七八九十]+[、.])", t))
    if qnum >= 3:
        scores["paper"] += 3
    elif qnum == 2:
        scores["paper"] += 1
    if t.count("解：") + t.count("证明：") + t.count("解答：") >= 2:
        scores["question"] += 2
    if len(t) < 40 and scores["note"] > 0:
        scores["note"] += 1

    best = max(scores, key=lambda k: scores[k])
    best_score = scores[best]
    if best_score <= 0:
        return "unknown", 0.3, "无明确信号"
    total = sum(scores.values()) or 1
    conf = min(0.95, 0.35 + 0.55 * (best_score / total) + 0.05 * best_score)
    reason = f"启发式：{best}={scores[best]}, paper={scores['paper']}, note={scores['note']}, qnum={qnum}"
    if best_score < 2 and total > 2:
        return "unknown", max(0.3, conf - 0.2), reason + "（信号弱）"
    return best, conf, reason


async def ai_classify_pages(images: list[dict]) -> list[dict]:
    """用视觉模型对多页做内容类型分类。失败返回空列表，调用方回落启发式。"""
    if not images:
        return []
    try:
        from services.ai_service import ai_service
        prompt = (
            "这是一页学习资料拍照。判断它最像什么，只返回 JSON："
            "{\"dest\":\"question|note|paper|unknown\",\"confidence\":0到1,"
            "\"reason\":\"短因\",\"subject\":\"学科可空\",\"title\":\"短标题可空\"}。"
            "question=单题/练习；note=概念笔记/公式整理/错因归纳；paper=试卷或考试页。"
            "不确定用 unknown。"
        )
        out: list[dict] = []
        for i, im in enumerate(images):
            b64 = im.get("base64") or ""
            mime = im.get("mime_type") or "image/jpeg"
            result = await ai_service.vision_mimo_first(b64, prompt, mime_type=mime, parse_json=True)
            if not isinstance(result, dict):
                out.append({"index": i, "dest": "unknown", "confidence": 0.0,
                            "reason": "视觉不可用", "subject": "", "title": ""})
                continue
            dest = str(result.get("dest") or "").strip().lower()
            if dest not in DESTINATIONS:
                dest = "unknown"
            try:
                conf = float(result.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            out.append({
                "index": i,
                "dest": dest,
                "confidence": max(0.0, min(1.0, conf)),
                "reason": str(result.get("reason") or "视觉模型")[:120],
                "subject": str(result.get("subject") or "")[:32],
                "title": str(result.get("title") or "")[:80],
            })
        return out
    except Exception as exc:
        logger.warning("ai_classify_pages failed: %s", str(exc)[:200])
        return []


async def ocr_text_of(b64: str) -> str:
    """单图 OCR 文本；失败返回空串（不抛）。"""
    try:
        from services.note_service import ocr_image_async
        data = b64.split(",")[-1] if "," in b64 else b64
        return (await ocr_image_async(data)) or ""
    except Exception as exc:
        logger.warning("smart_upload OCR failed: %s", str(exc)[:200])
        return ""


def _save_raw_image(qid: str, raw: bytes, ext: str, index: int) -> str:
    folder = os.path.join(QUESTIONS_DIR, qid)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"original_{index}{ext}")
    with open(path, "wb") as w:
        w.write(raw)
        w.flush()
        os.fsync(w.fileno())
    return path


async def route_question(raw: bytes, ext: str, *, bank: str = "default",
                         subject: str = "", grade: str = "",
                         ocr_text: str = "") -> dict:
    """把一页落成题库题目，并启动处理。

    与 `/api/ocr/upload-multi` 对齐的三点（R27 读码修正，原先三点都缺）：
    1. 同时写 `original_{i}` 与 `original{ext}`（process_image 走 multi_images；
       旧路径 `_get_image` 走 raw_image_path=original）；
    2. **必须写 multi_images**，否则 process_image 落到空 multi_images 分支，
       仍能靠 raw_image_path OCR，但后续参考图/角色信息全空；
    3. 状态用 `staged`（与 OCR 管线一致），不是 `pending`。
    """
    qid = gen_id()
    folder = os.path.join(QUESTIONS_DIR, qid)
    os.makedirs(folder, exist_ok=True)
    src_path = os.path.join(folder, f"original_0{ext}")
    with open(src_path, "wb") as w:
        w.write(raw)
        w.flush()
        os.fsync(w.fileno())
    original_path = os.path.join(folder, f"original{ext}")
    try:
        import shutil as _sh
        _sh.copy2(src_path, original_path)
    except Exception:
        original_path = src_path
    multi_images = [{
        "path": src_path,
        "filename": os.path.basename(src_path),
        "role": "question",
    }]
    async with async_session() as db:
        db.add(Question(
            id=qid, folder_path=folder,
            subject=subject, grade=grade, status="staged",
            ocr_text=ocr_text or "", bank=bank or "default",
            source_type="photo", capture_mode="single_question",
            raw_image_path=original_path,
            multi_images=multi_images,
            image_roles=[{"path": src_path, "role": "question", "order": 0}],
        ))
        await db.commit()
    try:
        from services import ocr_service
        from routers.ocr import _run_bg, _save_task_state
        desc = {
            "question_id": qid, "type": "process_image",
            "user_hint": "", "tags": [], "user_grade": grade,
            "bank": bank or "default",
        }
        _save_task_state(desc)
        _run_bg(ocr_service.process_image(qid, "", [], grade, multi_images=multi_images), desc)
    except Exception as exc:
        log_error("smart_upload.question_bg", str(exc)[:200])
    return {"dest": "question", "id": qid, "ok": True,
            "message": f"已入题库，开始识别（{qid}）"}


async def route_note(raw: bytes, ext: str, *, title: str = "",
                     subject: str = "", grade: str = "",
                     ocr_text: str = "") -> dict:
    """把一页落成笔记；原图存 notes 目录供回看。"""
    nid = gen_id()
    notes_dir = os.path.join(STORAGE_DIR, "notes", nid)
    os.makedirs(notes_dir, exist_ok=True)
    img_path = os.path.join(notes_dir, f"original_0{ext}")
    with open(img_path, "wb") as w:
        w.write(raw)
        w.flush()
        os.fsync(w.fileno())
    content = (ocr_text or "").strip() or "（OCR 未识别到文字，请打开图片核对）"
    async with async_session() as db:
        db.add(Note(
            id=nid,
            title=(title or "智能上传笔记").strip()[:200] or "智能上传笔记",
            content=content,
            subject=subject, grade=grade,
            source_type="manual", is_structured=False, auto_generated=False,
            source_images=[img_path],
        ))
        await db.commit()
    return {"dest": "note", "id": nid, "ok": True,
            "message": f"已存为笔记（{nid}）"}


async def route_paper(raw: bytes, ext: str, *, title: str = "",
                      subject: str = "", grade: str = "",
                      session_id: str = "") -> dict:
    """把一页落成**试卷会话里的一道题**（与整卷上传同一契约）。

    R27 修正：原先只把文件丢进 `storage/upload_sessions/{sid}/`，
    **没有任何管线读这个目录** —— 会话 question_ids 始终为空，
    学生以为上传成功，实际页面永远进不了处理/组卷。
    现在按 `ocr.py` 会话上传的契约：每页一个 Question，
    `capture_group_id=session_id` + 追加 `session.question_ids`。
    """
    qid = gen_id()
    folder = os.path.join(QUESTIONS_DIR, qid)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"original{ext}")
    with open(path, "wb") as w:
        w.write(raw)
        w.flush()
        os.fsync(w.fileno())

    async with async_session() as db:
        if session_id:
            sess = await db.get(UploadSession, session_id)
            if sess is None:
                session_id = ""
        if not session_id:
            session_id = gen_id()
            sess = UploadSession(
                id=session_id, title=(title or "智能上传试卷")[:200],
                subject=subject, grade=grade, status="open",
                question_ids=[],
            )
            db.add(sess)
            await db.flush()
        db.add(Question(
            id=qid, folder_path=folder, raw_image_path=path,
            status="staged", source_type="photo", bank="default",
            subject=subject, grade=grade,
            capture_mode="single_question",
            capture_group_id=session_id,
            capture_index=len(sess.question_ids or []),
        ))
        ids = list(sess.question_ids or [])
        ids.append(qid)
        sess.question_ids = ids
        await db.commit()

    # 启动该页处理（与会话「处理」同款：process_image）
    try:
        from services import ocr_service
        from routers.ocr import _run_bg, _save_task_state
        desc = {
            "question_id": qid, "type": "process_image",
            "user_hint": "", "tags": [], "user_grade": grade,
            "bank": "default", "session_id": session_id,
        }
        _save_task_state(desc)
        _run_bg(ocr_service.process_image(qid, "", [], grade), desc)
    except Exception as exc:
        log_error("smart_upload.paper_bg", str(exc)[:200])

    return {"dest": "paper", "id": session_id, "question_id": qid, "ok": True,
            "message": f"已加入试卷会话（{session_id}）第 {len(ids)} 题"}


async def route_page(dest: str, raw: bytes, ext: str, **kw) -> dict:
    """统一路由入口。unknown 按 question 处理并如实标注。"""
    if dest not in DESTINATIONS:
        dest = "unknown"
    if dest == "note":
        result = await route_note(raw, ext, **{k: v for k, v in kw.items()
                                               if k in ("title", "subject", "grade", "ocr_text")})
        return result
    if dest == "paper":
        result = await route_paper(raw, ext, **{k: v for k, v in kw.items()
                                                if k in ("title", "subject", "grade", "session_id")})
        return result
    # question / unknown → 题库；unknown 额外标记
    result = await route_question(raw, ext, **{k: v for k, v in kw.items()
                                               if k in ("bank", "subject", "grade", "ocr_text")})
    if dest == "unknown":
        result["note"] = "分类不确定，已按题目处理，请到题库核对"
    return result
