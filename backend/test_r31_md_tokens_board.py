"""R31：token 默认值、vendor 路径、讲课黑板、deepseek_chat None 默认。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IS_FIRST = "main" not in sys.modules
_TMP = None
if _IS_FIRST:
    _TMP = tempfile.mkdtemp(prefix="la_r31_")
    import config as _c
    _c.STORAGE_DIR = _TMP
    _c.DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(_TMP, 'app.db')}"
    _c.SETTINGS_FILE = os.path.join(_TMP, "settings.json")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main as m
    from models.database import reset_engine, init_db
    reset_engine()
    _run(init_db())
    src = os.path.join(os.path.dirname(__file__), "storage", "vendor")
    dst = os.path.join(_TMP or ".", "vendor")
    if os.path.isdir(src) and not os.path.isdir(dst):
        shutil.copytree(src, dst)
    with TestClient(m.app) as c:
        yield c


def test_vendor_files_exist_on_disk():
    root = os.path.join(os.path.dirname(__file__), "storage", "vendor")
    assert os.path.isfile(os.path.join(root, "marked.min.js"))
    assert os.path.isfile(os.path.join(root, "katex", "katex.min.js"))
    assert os.path.isfile(os.path.join(root, "katex", "katex.min.css"))
    assert os.path.isfile(os.path.join(root, "katex", "auto-render.min.js"))


def test_vendor_served(client):
    import config as _c
    src = os.path.join(os.path.dirname(__file__), "storage", "vendor")
    dst = os.path.join(_c.STORAGE_DIR, "vendor")
    if os.path.isdir(src) and not os.path.isdir(dst):
        shutil.copytree(src, dst)
    for u in ("/storage/vendor/marked.min.js", "/storage/vendor/katex/katex.min.js"):
        r = client.get(u)
        assert r.status_code == 200, u
        assert len(r.content) > 1000


def test_base_includes_marked_and_katex(client):
    r = client.get("/")
    html = r.text
    assert "marked.min.js" in html
    assert "katex" in html


def test_deepseek_default_tokens_is_provider_max():
    from config import _default_settings
    assert _default_settings["deepseek_max_tokens"] == 131072


def test_deepseek_chat_none_uses_ds_max(monkeypatch):
    from services.ai_service import AIService

    called = {}

    async def fake_inner(messages, model, temperature, max_tokens, fmt=None, scope=""):
        called["max_tokens"] = max_tokens
        return "ok"

    svc = object.__new__(AIService)
    svc.ds_max_tokens = 131072
    svc.ds_model = "deepseek-v4-flash"
    svc._chat_inner = fake_inner
    r = _run(AIService.deepseek_chat(svc, [{"role": "user", "content": "hi"}]))
    assert r == "ok"
    assert called["max_tokens"] == 131072


def test_lecture_page_has_board(client):
    r = client.get("/lecture")
    assert r.status_code == 200
    assert "lecBoard" in r.text
    assert "board-surface" in r.text


def test_lecture_js_writes_board():
    path = os.path.join(os.path.dirname(__file__), "static", "js", "lecture.js")
    text = open(path, encoding="utf-8").read()
    assert "lecBoard" in text
    assert "board-text" in text


def test_settings_page_default_tokens(client):
    r = client.get("/settings")
    assert "131072" in r.text


def teardown_module():
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP, ignore_errors=True)
