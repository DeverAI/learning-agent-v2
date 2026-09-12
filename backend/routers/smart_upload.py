"""智能上传 API：混合页一次上传，AI/启发式分流到题库 / 笔记 / 试卷。"""

from __future__ import annotations

import asyncio
import base64
from typing import List

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from logger import get_logger
from services import smart_upload_service as SU
from routers.ocr import _read_image_upload

router = APIRouter(prefix="/api/smart-upload", tags=["smart-upload"])
logger = get_logger()

MAX_FILES = 12


@router.get("/destinations")
async def list_destinations():
    return {"destinations": SU.DESTINATIONS}


async def _read_all(files: List[UploadFile]) -> list[dict]:
    if not files:
        raise HTTPException(400, detail="请上传至少一张图片")
    if len(files) > MAX_FILES:
        raise HTTPException(400, detail=f"一次最多 {MAX_FILES} 张")
    pages = []
    for i, f in enumerate(files):
        try:
            raw, ext = await _read_image_upload(f)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, detail=f"第 {i + 1} 张读取失败") from exc
        pages.append({
            "index": i, "filename": f.filename or f"p{i}{ext}",
            "raw": raw, "ext": ext,
            "mime": f.content_type or "image/jpeg",
            "b64": base64.b64encode(raw).decode(),
        })
    return pages


async def _ocr_all(pages: list[dict]) -> list[str]:
    sem = asyncio.Semaphore(3)

    async def _one(p):
        async with sem:
            return await SU.ocr_text_of(p["b64"])

    return list(await asyncio.gather(*(_one(p) for p in pages)))


async def _classify(pages: list[dict], ocr_texts: list[str], use_ai: bool) -> list[str]:
    ai_results: list[dict] = []
    if use_ai:
        imgs = [{"base64": p["b64"], "mime_type": p["mime"], "filename": p["filename"]}
                for p in pages]
        ai_results = await SU.ai_classify_pages(imgs)
    dests = []
    for i, text in enumerate(ocr_texts):
        dest, conf, reason = SU.heuristic_classify(text)
        ai = next((x for x in ai_results if x.get("index") == i), None)
        if ai and ai.get("confidence", 0) >= 0.55:
            dest = ai["dest"]
        dests.append(dest)
    return dests


@router.post("/classify")
async def classify_only(
    files: List[UploadFile] = File(...),
    use_ai: bool = Form(default=False),
):
    """只分类不落库：返回每页建议目标，供前端确认后再执行。"""
    pages = await _read_all(files)
    ocr_texts = await _ocr_all(pages)
    dests = await _classify(pages, ocr_texts, use_ai)

    plan = []
    for p, text, dest in zip(pages, ocr_texts, dests):
        # 再算一次置信度用于展示
        _d, conf, reason = SU.heuristic_classify(text)
        plan.append({
            "index": p["index"],
            "filename": p["filename"],
            "dest": dest,
            "confidence": round(float(conf), 2),
            "reason": reason,
            "source": "heuristic",
            "ocr_preview": (text or "")[:160],
            "label": SU.DESTINATIONS.get(dest, SU.DESTINATIONS["unknown"])["label"],
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
    """一次上传并分流。dests 空 → 自动分类；否则按传入目标（人工可改）。"""
    pages = await _read_all(files)
    override_list = [x.strip().lower() for x in (dests or "").split(",") if x.strip()]
    if override_list and len(override_list) != len(pages):
        raise HTTPException(400, detail="dests 数量必须与文件数一致")

    if not override_list:
        ocr_texts = await _ocr_all(pages)
        plan_dests = await _classify(pages, ocr_texts, use_ai)
    else:
        ocr_texts = [""] * len(pages)
        plan_dests = override_list

    results = []
    paper_sid = ""
    for p, dest, text in zip(pages, plan_dests, ocr_texts):
        if dry_run:
            results.append({"index": p["index"], "dest": dest, "ok": True, "dry_run": True,
                            "message": f"将送往 {SU.DESTINATIONS.get(dest, {}).get('label', dest)}"})
            continue
        if dest == "note":
            r = await SU.route_note(
                p["raw"], p["ext"],
                title=p["filename"], subject=subject, grade=grade, ocr_text=text,
            )
        elif dest == "paper":
            if paper_sid:
                r = await SU.route_paper(
                    p["raw"], p["ext"], subject=subject, grade=grade,
                    session_id=paper_sid, title=paper_title or "智能上传试卷",
                )
            else:
                r = await SU.route_paper(
                    p["raw"], p["ext"], subject=subject, grade=grade,
                    title=paper_title or "智能上传试卷",
                )
            paper_sid = r.get("id") or paper_sid
        else:
            r = await SU.route_question(
                p["raw"], p["ext"], bank=bank, subject=subject, grade=grade, ocr_text=text,
            )
            if dest == "unknown":
                r["note"] = "分类不确定，已按题目处理"
        results.append({"index": p["index"], **r})

    ok_n = sum(1 for r in results if r.get("ok"))
    return {"total": len(results), "ok": ok_n, "results": results, "dry_run": dry_run}
