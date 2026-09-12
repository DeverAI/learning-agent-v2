"""R27 深检回归：智能上传真入库契约 + 打断 keep 不吞提问。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import shutil
import struct
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IS_FIRST = "main" not in sys.modules
_TMP = None
if _IS_FIRST:
    _TMP = tempfile.mkdtemp(prefix="la_r27_")
    import config as _c
    _c.STORAGE_DIR = _TMP
    _c.DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(_TMP, 'app.db')}"
    _c.SETTINGS_FILE = os.path.join(_TMP, "settings.json")


def _run(coro):
    return asyncio.run(coro)


def _tiny_png() -> bytes:
    """1x1 纯 PNG，满足字节签名校验。"""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = zlib.compress(b"\x00\xff\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main as m
    from models.database import reset_engine, init_db
    reset_engine()
    _run(init_db())
    with TestClient(m.app) as c:
        yield c


def test_route_question_sets_multi_images_and_staged(client):
    from services import smart_upload_service as SU
    from models.database import async_session
    from models.models import Question
    import os as _os

    raw = _tiny_png()
    result = _run(SU.route_question(raw, ".png", subject="数学", grade="九上",
                                    ocr_text="已知 x=1，求 y"))
    assert result["ok"] is True
    qid = result["id"]
    assert qid

    async def _load():
        async with async_session() as db:
            return await db.get(Question, qid)
    q = _run(_load())
    assert q is not None
    assert q.status == "staged", "必须与 OCR 管线一致，不能是 pending"
    mi = q.multi_images or []
    assert mi and mi[0].get("role") == "question"
    assert _os.path.isfile(mi[0]["path"])
    assert q.raw_image_path and _os.path.isfile(q.raw_image_path)


def test_route_paper_creates_question_in_session(client):
    from services import smart_upload_service as SU
    from models.database import async_session
    from models.models import Question, UploadSession

    raw = _tiny_png()
    r1 = _run(SU.route_paper(raw, ".png", title="R27卷", subject="数学", grade="九上"))
    assert r1["ok"] is True
    sid = r1["id"]
    qid1 = r1["question_id"]
    r2 = _run(SU.route_paper(raw, ".png", session_id=sid))
    assert r2["id"] == sid
    qid2 = r2["question_id"]
    assert qid1 != qid2

    async def _load():
        async with async_session() as db:
            s = await db.get(UploadSession, sid)
            q1 = await db.get(Question, qid1)
            q2 = await db.get(Question, qid2)
            return s, q1, q2
    s, q1, q2 = _run(_load())
    assert s is not None
    assert list(s.question_ids or []) == [qid1, qid2]
    for q in (q1, q2):
        assert q is not None
        assert q.capture_group_id == sid
        assert q.status == "staged"
        assert q.raw_image_path and os.path.isfile(q.raw_image_path)


def test_interrupt_keep_falls_through(client):
    from routers.sessions import _try_interrupt_running_tasks
    from services import background_agent as BA

    async def _go():
        task = await BA.create_task(sid="r27_keep", tool="prepare_lesson",
                                    title="R27课", params={})
        from models.database import async_session
        from models.models import AgentTask
        async with async_session() as db:
            t = await db.get(AgentTask, task["task_id"])
            t.status = "running"
            await db.commit()
        return await _try_interrupt_running_tasks("r27_keep", "继续讲这个公式")

    assert _run(_go()) is None


def test_interrupt_cancel_still_intercepts(client):
    from routers.sessions import _try_interrupt_running_tasks
    from services import background_agent as BA

    async def _go():
        task = await BA.create_task(sid="r27_cancel", tool="prepare_lesson",
                                    title="R27取消课", params={})
        from models.database import async_session
        from models.models import AgentTask
        async with async_session() as db:
            t = await db.get(AgentTask, task["task_id"])
            t.status = "running"
            await db.commit()
        return await _try_interrupt_running_tasks("r27_cancel", "取消任务")

    r = _run(_go())
    assert r is not None
    assert r["data"]["decision"] == "cancel"


def test_smart_upload_api_route_question_with_override(client):
    """人工指定 question 时：落库 multi_images + staged。"""
    raw = _tiny_png()
    files = {"files": ("t.png", raw, "image/png")}
    data = {"dests": "question", "subject": "物理", "grade": "九上", "use_ai": "false"}
    r = client.post("/api/smart-upload", files=files, data=data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] == 1
    qid = body["results"][0]["id"]

    async def _load():
        from models.database import async_session
        from models.models import Question
        async with async_session() as db:
            return await db.get(Question, qid)
    q = _run(_load())
    assert q is not None
    assert q.multi_images, "override 路径也必须写 multi_images"
    assert q.status == "staged"


def teardown_module():
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP, ignore_errors=True)
