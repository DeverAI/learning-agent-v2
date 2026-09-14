# -*- coding: utf-8 -*-
"""R39：反馈 + 巡检测试。"""
from __future__ import annotations
import asyncio, os, sys, tempfile, shutil
import pytest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_IS_FIRST = "main" not in sys.modules
_TMP = None
if _IS_FIRST:
    _TMP = tempfile.mkdtemp(prefix="la_r39_")
    import config as _c
    _c.STORAGE_DIR = _TMP
    _c.DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(_TMP,'app.db')}"
    _c.SETTINGS_FILE = os.path.join(_TMP, "settings.json")

def _run(coro): return asyncio.run(coro)

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import main as m
    from models.database import reset_engine, init_db
    reset_engine(); _run(init_db())
    with TestClient(m.app) as c: yield c

def test_feedback_page(client):
    r = client.get("/feedback")
    assert r.status_code == 200
    assert "fbSubmit" in r.text
    assert "fbKind" in r.text

def test_feedback_api(client):
    r = client.post("/api/feedback", json={"kind":"question","message":"第20题答案错了","page":"/questions"})
    assert r.status_code == 200, r.text
    fid = r.json()["id"]
    r2 = client.get("/api/feedback")
    assert r2.status_code == 200
    ids = [x.get("id") for x in r2.json()["items"]]
    assert fid in ids
    r3 = client.post(f"/api/feedback/{fid}/done")
    assert r3.status_code == 200
    r4 = client.get("/api/feedback?status=done")
    assert any(x.get("id")==fid for x in r4.json()["items"])

def test_feedback_empty_rejected(client):
    r = client.post("/api/feedback", json={"kind":"other","message":"  "})
    assert r.status_code == 400

def test_health_patrol_endpoint(client):
    r = client.get("/api/health/patrol")
    assert r.status_code == 200
    assert "problems" in r.json()
    assert "checked_at" in r.json()

def test_nav_has_feedback(client):
    r = client.get("/feedback")
    assert 'href="/feedback"' in r.text or 'page == \'feedback\'' in r.text

def teardown_module():
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP, ignore_errors=True)
