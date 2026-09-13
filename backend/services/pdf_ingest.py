# -*- coding: utf-8 -*-
"""PDF 摄入：多源证据合并，避免只靠 pypdf 字符导致 AI 全量质疑。

来源（R34 用户原话：抽出的字符 & 全量 OCR & PDF 图片单独 OCR 都丢给 AI）：
1. pypdf 抽字 + Adobe Symbol PUA 重映射
2. pypdfium2 把每页渲染成 PNG → 视觉 OCR（MiMo 优先）
3. 合并结果写进 ocr_text / source_reference，解题时多源交叉

扫描版：pypdf 无字 → 主要靠页图 OCR。
文字版但公式乱码：PUA 重映射 + 页图 OCR 纠正。
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any, Optional

from logger import get_logger, log_error

logger = get_logger()

MAX_PAGES = 60
PAGE_RENDER_SCALE = 2.0  # 2x 提高小公式可读性


def extract_text_pages(raw: bytes) -> list[str]:
    """pypdf 抽字 + PUA 重映射。失败/空白页返回空串。"""
    try:
        from pypdf import PdfReader
        from services.pdf_math_remap import remap_symbol_pua, count_pua
    except Exception as exc:
        logger.warning("pdf extract_text_pages import fail: %s", exc)
        return []
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as exc:
        logger.warning("pdf open fail: %s", exc)
        return []
    pages = []
    for page in reader.pages[:MAX_PAGES]:
        try:
            t = (page.extract_text() or "").strip()
            t = remap_symbol_pua(t)
            pages.append(t)
        except Exception:
            pages.append("")
    return pages


def render_pages_to_png(raw: bytes, max_pages: int = MAX_PAGES) -> list[bytes]:
    """把 PDF 每页渲染成 PNG 字节。无 pypdfium2 时返回空列表。"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        logger.warning("pypdfium2 not installed; skip PDF page rasterize")
        return []
    try:
        doc = pdfium.PdfDocument(raw)
    except Exception as exc:
        logger.warning("pdfium open fail: %s", exc)
        return []
    out = []
    try:
        n = min(len(doc), max_pages)
        for i in range(n):
            try:
                page = doc[i]
                bitmap = page.render(scale=PAGE_RENDER_SCALE)
                pil = bitmap.to_pil()
                buf = io.BytesIO()
                pil.save(buf, format="PNG", optimize=True)
                out.append(buf.getvalue())
            except Exception as exc:
                logger.warning("pdfium render page %d fail: %s", i, exc)
                out.append(b"")
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out


async def vision_ocr_png(png_bytes: bytes) -> str:
    """视觉 OCR 一页图。失败返回空串。"""
    if not png_bytes:
        return ""
    try:
        from services.ai_service import ai_service
        b64 = base64.b64encode(png_bytes).decode()
        prompt = (
            "这是一张试卷/讲义的一页照片。请忠实转写页面上的文字与数学公式。"
            "数学用 LaTeX：行内 $...$，行间 $$...$$。"
            "保留题号（如 1. 2. 17.）。不要解题、不要评论、不要编造。"
            "只输出转写正文。"
        )
        result = await ai_service.vision_mimo_first(
            b64, prompt, mime_type="image/png", parse_json=False
        )
        text = (result or "").strip() if isinstance(result, str) else str(result or "").strip()
        return text
    except Exception as exc:
        logger.warning("vision_ocr_png fail: %s", str(exc)[:200])
        return ""


def _looks_garbage(text: str) -> bool:
    """pypdf 数学讲义常见的「抽坏了」特征。"""
    if not text or len(text) < 20:
        return True
    # 大量私有区残留
    pua = sum(1 for ch in text if "\uf000" <= ch <= "\uf0ff")
    if pua >= 3:
        return True
    # R34：重映射后 `2y x x a x= - -` 仍有短 token，但**已可读**。
    # PUA 清零 + 含中文/题号 → 视为可用，不再当垃圾去质疑。
    has_cn = any("\u4e00" <= ch <= "\u9fff" for ch in text)
    has_num_q = any(s in text for s in ("1.", "2.", "17.", "一、", "（1）"))
    if pua == 0 and (has_cn or has_num_q) and len(text) >= 40:
        return False
    tokens = [t for t in text.replace("\n", " ").split() if t]
    if len(tokens) >= 8:
        short = sum(1 for t in tokens if len(t) <= 2)
        if short / len(tokens) > 0.55:
            return True
    return False


def merge_sources(page_no: int, pypdf_text: str, vision_text: str) -> str:
    """合并一页的多源文本，给解题模型一份**自洽**的题干。

    - 只有视觉 OCR 可用 → 用视觉
    - 只有 pypdf 且不像垃圾 → 用 pypdf
    - 两者都有：视觉为主（数学更准），pypdf 作附录交叉验证
    - 都像垃圾：原样附上并标明，交给模型 reconstruct 而不是 silent challenge
    """
    p = (pypdf_text or "").strip()
    v = (vision_text or "").strip()
    p_bad = _looks_garbage(p) if p else True
    v_bad = _looks_garbage(v) if v else True

    if v and not v_bad:
        if p and not p_bad:
            return (
                f"【本页视觉转写（优先采信）】\n{v}\n\n"
                f"【本页 pypdf 抽字（交叉核对，可能含公式乱码）】\n{p}"
            )
        return f"【本页视觉转写】\n{v}"
    if p and not p_bad:
        return f"【本页文本抽取】\n{p}"
    if p or v:
        parts = [f"【第 {page_no} 页】多源抽取均不完整，请根据下列片段还原题意，不要因乱码直接质疑："]
        if p:
            parts.append("pypdf：" + p[:3000])
        if v:
            parts.append("视觉：" + v[:3000])
        return "\n".join(parts)
    return ""


async def ingest_pdf_pages(raw: bytes, *, use_vision: bool = True,
                           max_pages: int = MAX_PAGES) -> list[dict]:
    """完整摄入一页 PDF。返回 [{page_no, text, png_b64, pypdf_text, vision_text, quality}]"""
    pypdf_pages = extract_text_pages(raw)
    pngs = render_pages_to_png(raw, max_pages=max_pages)
    n = max(len(pypdf_pages), len(pngs))
    if n == 0:
        return []
    n = min(n, max_pages)
    items = []
    for i in range(n):
        p_text = pypdf_pages[i] if i < len(pypdf_pages) else ""
        png = pngs[i] if i < len(pngs) else b""
        v_text = ""
        if use_vision and png:
            v_text = await vision_ocr_png(png)
        merged = merge_sources(i + 1, p_text, v_text)
        pua = sum(1 for ch in p_text if "\uf000" <= ch <= "\uf0ff")
        items.append({
            "page_no": i + 1,
            "text": merged,
            "pypdf_text": p_text,
            "vision_text": v_text,
            "png": png,
            "png_b64": base64.b64encode(png).decode() if png else "",
            "quality": {
                "pypdf_len": len(p_text),
                "vision_len": len(v_text),
                "pua_count": pua,
                "merged_len": len(merged),
            },
        })
    return items
