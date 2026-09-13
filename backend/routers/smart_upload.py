"""智能上传 API：任意文件类型混传，分流到题库 / 笔记 / 试卷。

支持：图片（jpg/png/webp/gif/bmp）· PDF（文字版，按页）· Word（docx）· 纯文本（txt/md）
扫描版 PDF 与不支持类型**如实失败**，不生成空壳。
"""

from __future__ import annotations

import asyncio
import base64
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from logger import get_logger
from models.models import gen_id
from services import smart_upload_service as SU

router = APIRouter(prefix="/api/smart-upload", tags=["smart-upload"])
logger = get_logger()

MAX_FILES = 20  # PDF 按页展开前的「文件」上限


@router.get("/destinations")
async def list_destinations():
    return {"destinations": SU.DESTINATIONS}


@router.get("/accept")
async def accept_types():
    return {
        "images": ["image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"],
        "documents": ["application/pdf", ".docx", "text/plain", "text/markdown"],
        "max_files": MAX_FILES,
        "max_bytes": SU.MAX_FILE_BYTES,
        "hint": "图片走 OCR；PDF/Word/文本先抽字再分流；扫描版 PDF 请改传图片",
    }


async def _read_expand(files: List[UploadFile]) -> list[dict]:
    if not files:
        raise HTTPException(400, detail="请上传至少一个文件")
    if len(files) > MAX_FILES:
        raise HTTPException(400, detail=f"一次最多 {MAX_FILES} 个文件")
    items: list[dict] = []
    for i, f in enumerate(files):
        raw = await f.read()
        try:
            parts = SU.expand_upload_items(
                f.filename or f"f{i}", f.content_type or "", raw
            )
        except ValueError as exc:
            raise HTTPException(400, detail=f"第 {i + 1} 个文件：{exc}") from exc
        except Exception as exc:
            raise HTTPException(400, detail=f"第 {i + 1} 个文件解析失败：{exc}") from exc
        for p in parts:
            p["file_index"] = i
            p["index"] = len(items)
            # 图片才需要 b64 供视觉分类/OCR
            if p.get("kind") == "image" and p.get("raw"):
                p["b64"] = base64.b64encode(p["raw"]).decode()
                p["mime"] = f.content_type or "image/jpeg"
            else:
                p["b64"] = ""
                p["mime"] = f.content_type or ""
            items.append(p)
    return items


async def _ocr_image_items(items: list[dict]) -> list[str]:
    sem = asyncio.Semaphore(3)

    async def _one(p):
        if p.get("kind") != "image" or not p.get("b64"):
            return p.get("text") or ""
        async with sem:
            return await SU.ocr_text_of(p["b64"])

    return list(await asyncio.gather(*(_one(p) for p in items)))


async def _classify_all_async(items: list[dict], texts: list[str], use_ai: bool) -> list[dict]:
    plan = []
    ai_results: list[dict] = []
    if use_ai:
        imgs = []
        idx_map = []
        for i, p in enumerate(items):
            if p.get("kind") == "image" and p.get("b64"):
                imgs.append({"base64": p["b64"], "mime_type": p.get("mime") or "image/jpeg",
                             "filename": p.get("filename") or ""})
                idx_map.append(i)
        if imgs:
            ai_results = await SU.ai_classify_pages(imgs)
        ai_by_item = {idx_map[j]: r for j, r in enumerate(ai_results) if j < len(idx_map)}
    else:
        ai_by_item = {}

    for i, (p, text) in enumerate(zip(items, texts)):
        if p.get("kind") == "image":
            dest, conf, reason = SU.heuristic_classify(text or "")
            src = "heuristic"
        else:
            dest, conf, reason = SU.classify_by_kind_and_text({**p, "text": text or ""})
            src = "text"
        ai = ai_by_item.get(i)
        if ai and ai.get("confidence", 0) >= 0.55:
            dest = ai["dest"]
            conf = max(conf, float(ai.get("confidence") or conf))
            reason = f"视觉：{ai.get('reason') or ''}"
            src = "vision"
        # 空白 PDF 页保持 unknown
        if p.get("kind") == "pdf_page" and not (text or "").strip():
            dest = "unknown"
        plan.append({
            "dest": dest, "confidence": round(float(conf or 0), 2),
            "reason": reason, "source": src,
        })
    return plan


async def _route_one(item: dict, dest: str, text: str, *, subject: str, grade: str,
                     bank: str, paper_title: str, paper_sid: str) -> tuple[dict, str]:
    """返回 (result, paper_sid)。"""
    kind = item.get("kind") or "image"
    title = item.get("filename") or ""

    if kind == "image":
        if dest == "note":
            r = await SU.route_note(
                item["raw"], item["ext"], title=title,
                subject=subject, grade=grade, ocr_text=text,
            )
        elif dest == "paper":
            r = await SU.route_paper(
                item["raw"], item["ext"], subject=subject, grade=grade,
                session_id=paper_sid or "", title=paper_title or "智能上传试卷",
            )
            paper_sid = r.get("id") or paper_sid
        else:
            r = await SU.route_question(
                item["raw"], item["ext"], bank=bank, subject=subject,
                grade=grade, ocr_text=text,
            )
            if dest == "unknown":
                r["note"] = "分类不确定，已按题目处理"
        return r, paper_sid

    # 文本类（pdf_page / docx / text）
    body = (text or "").strip()
    if not body:
        return {"dest": "unknown", "ok": False, "message": "无文字内容，已跳过"}, paper_sid

    if dest == "note":
        r = await SU.route_text_note(
            body, title=title, subject=subject, grade=grade, source_kind=kind,
        )
    elif dest == "paper":
        # 多页 PDF 进同一试卷会话：每页一道 staged 题
        r = await SU.route_text_question(
            body, title=title, subject=subject, grade=grade,
            bank=bank, source_kind="pdf_page",
        )
        # 挂会话
        r2 = await _attach_question_to_session(
            r.get("id") or "", paper_sid or "", title=paper_title or "智能上传试卷",
            subject=subject, grade=grade,
        )
        paper_sid = r2.get("session_id") or paper_sid
        r["session_id"] = paper_sid
        r["message"] = f"PDF 页已入试卷会话并开始解题（{r.get('id')}）"
    else:
        r = await SU.route_text_question(
            body, title=title, subject=subject, grade=grade,
            bank=bank, source_kind=kind,
        )
        if dest == "unknown":
            r["note"] = "分类不确定，已按文本题目处理"
    return r, paper_sid


async def _attach_question_to_session(qid: str, session_id: str, *, title: str,
                                      subject: str, grade: str) -> dict:
    from models.database import async_session
    from models.models import Question, UploadSession
    async with async_session() as db:
        if session_id:
            sess = await db.get(UploadSession, session_id)
            if sess is None:
                session_id = ""
        if not session_id:
            session_id = gen_id()
            sess = UploadSession(
                id=session_id, title=title[:200], subject=subject, grade=grade,
                status="open", question_ids=[],
            )
            db.add(sess)
            await db.flush()
        q = await db.get(Question, qid) if qid else None
        if q is not None:
            q.capture_group_id = session_id
            q.capture_index = len(sess.question_ids or [])
            ids = list(sess.question_ids or [])
            if qid not in ids:
                ids.append(qid)
            sess.question_ids = ids
        await db.commit()
    return {"session_id": session_id}


@router.post("/classify")
async def classify_only(
    files: List[UploadFile] = File(...),
    use_ai: bool = Form(default=False),
):
    """只分类不落库。PDF 会展开成多页。"""
    items = await _read_expand(files)
    texts = await _ocr_image_items(items)
    plan_d = await _classify_all_async(items, texts, use_ai)
    plan = []
    for p, text, d in zip(items, texts, plan_d):
        plan.append({
            "index": p["index"],
            "filename": p.get("filename") or "",
            "kind": p.get("kind") or "",
            "page_no": p.get("page_no") or 1,
            "dest": d["dest"],
            "confidence": d["confidence"],
            "reason": d["reason"],
            "source": d["source"],
            "ocr_preview": (text or "")[:160],
            "label": SU.DESTINATIONS.get(d["dest"], SU.DESTINATIONS["unknown"])["label"],
        })
    return {"total": len(plan), "plan": plan, "destinations": SU.DESTINATIONS}


@router.post("")
async def smart_upload_execute(
    files: List[UploadFile] = File(...),
    dests: str = Form(default=""),
    use_ai: bool = Form(default=False),
    subject: str = Form(default=""),
    grade: str = Form(default=""),
    bank: str = Form(default="default"),
    paper_title: str = Form(default=""),
    dry_run: bool = Form(default=False),
):
    """一次上传并分流。dests 为空 → 自动分类（按展开后的逻辑页对齐）。"""
    items = await _read_expand(files)
    override_list = [x.strip().lower() for x in (dests or "").split(",") if x.strip()]
    if override_list and len(override_list) != len(items):
        raise HTTPException(
            400,
            detail=f"dests 数量({len(override_list)})须与展开后逻辑页数({len(items)})一致；"
                   f"PDF 会按页展开",
        )

    if not override_list:
        texts = await _ocr_image_items(items)
        plan_d = await _classify_all_async(items, texts, use_ai)
        plan_dests = [x["dest"] for x in plan_d]
    else:
        texts = [p.get("text") or "" for p in items]
        # 非图片仍有 text；图片在 override 时不 OCR，交给 process_image
        plan_dests = override_list

    results = []
    paper_sid = ""
    for item, dest, text in zip(items, plan_dests, texts):
        if dry_run:
            results.append({
                "index": item["index"], "dest": dest, "ok": True, "dry_run": True,
                "kind": item.get("kind"),
                "message": f"将送往 {SU.DESTINATIONS.get(dest, {}).get('label', dest)}",
            })
            continue
        try:
            r, paper_sid = await _route_one(
                item, dest, text, subject=subject, grade=grade,
                bank=bank, paper_title=paper_title, paper_sid=paper_sid,
            )
        except Exception as exc:
            logger.warning("smart_upload route fail %s: %s", item.get("filename"), exc)
            r = {"ok": False, "dest": dest, "message": f"处理失败：{exc}"}
        results.append({"index": item["index"], "kind": item.get("kind"), **r})

    ok_n = sum(1 for r in results if r.get("ok"))
    return {
        "total": len(results), "ok": ok_n, "results": results,
        "dry_run": dry_run, "paper_session_id": paper_sid or "",
    }
