"""R27：文字版试卷 PDF 导入。"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IS_FIRST = "main" not in sys.modules
_TMP = None
if _IS_FIRST:
    _TMP = tempfile.mkdtemp(prefix="la_r27pdf_")
    import config as _c
    _c.STORAGE_DIR = _TMP
    _c.DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(_TMP, 'app.db')}"
    _c.SETTINGS_FILE = os.path.join(_TMP, "settings.json")


def _run(coro):
    return asyncio.run(coro)


def _make_pdf(pages: list[str]) -> bytes:
    """手写最小 PDF（不依赖 reportlab）：每页一行 Helvetica 文本（ASCII）。"""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    # 对象布局：1=catalog 2=pages 3=font，其后每页 content+page
    kids = []
    extra = []  # (oid, body bytes/str) 在 font 之后
    next_id = 4
    for text in pages:
        content = f"BT /F1 12 Tf 72 750 Td ({esc(text)}) Tj ET\n".encode("ascii", "replace")
        cid = next_id
        pid = next_id + 1
        next_id += 2
        extra.append((cid, f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream"))
        extra.append((pid, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {cid} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        )))
        kids.append(f"{pid} 0 R")
    objects = [
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"),
        (3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
    ] + extra

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for oid, body in objects:
        offsets[oid] = out.tell()
        out.write(f"{oid} 0 obj\n".encode())
        out.write(body if isinstance(body, bytes) else body.encode())
        out.write(b"\nendobj\n")
    xref = out.tell()
    n = max(offsets) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for i in range(1, n):
        out.write(f"{offsets.get(i, 0):010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main as m
    from models.database import reset_engine, init_db
    reset_engine()
    _run(init_db())
    with TestClient(m.app) as c:
        yield c


def test_import_pdf_rejects_non_pdf(client):
    r = client.post("/api/ocr/session/import-pdf",
                    files={"file": ("a.txt", b"hello", "text/plain")},
                    data={"title": "x"})
    assert r.status_code == 400


def test_import_text_pdf_creates_session(client):
    pdf = _make_pdf(["Q1 known x=1 find y", "Q2 prove triangle sum 180"])
    r = client.post(
        "/api/ocr/session/import-pdf",
        files={"file": ("math.pdf", pdf, "application/pdf")},
        data={"title": "R27test", "subject": "math", "grade": "9", "process": "false"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_questions"] >= 1
    sid = body["session_id"]

    async def _load():
        from models.database import async_session
        from models.models import UploadSession, Question
        async with async_session() as db:
            s = await db.get(UploadSession, sid)
            qs = []
            for qid in (s.question_ids or []):
                qs.append(await db.get(Question, qid))
            return s, qs
    s, qs = _run(_load())
    assert s is not None
    assert s.title == "R27test"
    assert len(qs) == body["imported_questions"]
    for q in qs:
        assert q.status == "staged"
        assert q.ocr_text, "文字 PDF 必须带 ocr_text"
        assert q.source_type == "pdf_page"
        assert q.capture_group_id == sid


def teardown_module():
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP, ignore_errors=True)
