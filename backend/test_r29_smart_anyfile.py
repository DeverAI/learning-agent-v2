"""R29：智能上传任意文件类型。"""

from __future__ import annotations

import asyncio
import io
import os
import struct
import sys
import tempfile
import shutil
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IS_FIRST = "main" not in sys.modules
_TMP = None
if _IS_FIRST:
    _TMP = tempfile.mkdtemp(prefix="la_r29_")
    import config as _c
    _c.STORAGE_DIR = _TMP
    _c.DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(_TMP, 'app.db')}"
    _c.SETTINGS_FILE = os.path.join(_TMP, "settings.json")


def _run(coro):
    return asyncio.run(coro)


def _tiny_png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def _mini_pdf(text: str) -> bytes:
    content = f"BT /F1 12 Tf 72 750 Td ({text}) Tj ET\n".encode("ascii", "replace")
    objs = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (2, b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>"),
        (3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        (4, (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R "
             b"/Resources << /Font << /F1 3 0 R >> >> >>")),
        (5, f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"endstream"),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = {}
    for oid, body in objs:
        offs[oid] = out.tell()
        out.write(f"{oid} 0 obj\n".encode())
        out.write(body)
        out.write(b"\nendobj\n")
    xref = out.tell()
    n = max(offs) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for i in range(1, n):
        out.write(f"{offs.get(i, 0):010d} 00000 n \n".encode())
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


def test_sniff_and_expand_text():
    from services.smart_upload_service import sniff_kind, expand_upload_items
    assert sniff_kind("a.txt", "text/plain", b"hello") == "text"
    assert sniff_kind("a.png", "image/png", _tiny_png()) == "image"
    assert sniff_kind("a.pdf", "application/pdf", _mini_pdf("Q1")) == "pdf"
    assert sniff_kind("a.bin", "application/octet-stream", b"\x00\x01\x02") == "unsupported"
    items = expand_upload_items("note.md", "text/markdown", "知识点：二次函数".encode("utf-8"))
    assert len(items) == 1
    assert items[0]["kind"] == "text"
    assert "二次函数" in items[0]["text"]


def test_expand_pdf_pages():
    from services.smart_upload_service import expand_upload_items
    items = expand_upload_items("p.pdf", "application/pdf", _mini_pdf("Q1 known x=1"))
    assert len(items) >= 1
    assert items[0]["kind"] == "pdf_page"


def test_api_accept(client):
    r = client.get("/api/smart-upload/accept")
    assert r.status_code == 200
    assert "pdf" in r.json()["hint"].lower() or "PDF" in r.json()["hint"]


def test_upload_txt_as_note(client):
    files = {"files": ("笔记.txt", "知识点：光合作用\n定义：…".encode("utf-8"), "text/plain")}
    data = {"dests": "note", "subject": "生物", "grade": "初二"}
    r = client.post("/api/smart-upload", files=files, data=data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] == 1
    nid = body["results"][0]["id"]
    async def _load():
        from models.database import async_session
        from models.models import Note
        async with async_session() as db:
            return await db.get(Note, nid)
    n = _run(_load())
    assert n is not None
    assert "光合作用" in (n.content or "")


def test_upload_pdf_as_question(client):
    pdf = _mini_pdf("Q2 prove triangle sum 180")
    files = {"files": ("q.pdf", pdf, "application/pdf")}
    data = {"dests": "question", "subject": "math"}
    r = client.post("/api/smart-upload", files=files, data=data)
    assert r.status_code == 200, r.text
    qid = r.json()["results"][0]["id"]
    async def _load():
        from models.database import async_session
        from models.models import Question
        async with async_session() as db:
            return await db.get(Question, qid)
    q = _run(_load())
    assert q is not None
    assert q.ocr_text
    assert q.source_type in ("pdf_page", "pdf")


def test_upload_png_still_works(client):
    files = {"files": ("t.png", _tiny_png(), "image/png")}
    data = {"dests": "question", "dry_run": "false"}
    r = client.post("/api/smart-upload", files=files, data=data)
    assert r.status_code == 200, r.text


def test_reject_garbage(client):
    files = {"files": ("x.xyz", b"\x00\x01\x02\x03", "application/octet-stream")}
    r = client.post("/api/smart-upload", files=files, data={})
    assert r.status_code == 400


def teardown_module():
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP, ignore_errors=True)
