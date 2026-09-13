"""R30：上传过滤契约 + 端口对齐 + import_and_prep 工具。"""

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
    _TMP = tempfile.mkdtemp(prefix="la_r30_")
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
    with TestClient(m.app) as c:
        yield c


def test_batch_upload_ui_allows_any_file(client):
    r = client.get("/batch-upload")
    html = r.text
    assert "没有可用图片" not in html or "图片/PDF/Word/文本" in html
    assert "_smartAddFiles" in html
    # 前端不再只收 image/* 做智能上传
    assert "isDoc" in html or "pdf" in html.lower()
    # accept 放开
    assert ".pdf" in html


def test_port_align_beaker_on_stand():
    from services.diagram_components.assembler import _resolve_connections
    from services.diagram_components.component_db import COMPONENT_DB as DB

    def port_xy(comp, port_id):
        d = DB[comp["type"]]
        for p in d["ports"]:
            if p["id"] == port_id:
                dw, dh = d["default_w"], d["default_h"]
                sx, sy = (comp.get("w") or dw) / dw, (comp.get("h") or dh) / dh
                dy = p["dy"]
                if comp["type"] == "iron_stand" and port_id in ("ring_top", "ring_bottom"):
                    base = max(4.0, min(73.0, float(comp.get("clamp_y") or 42)))
                    dy = base - 2.5 if port_id == "ring_top" else base + 2.5
                return (comp["x"] + p["dx"] * sx, comp["y"] + dy * sy)
        return None

    comps = [
        {"type": "iron_stand", "x": 0, "y": 0, "w": 40, "h": 80},
        {"type": "beaker", "x": 100, "y": 0, "w": 30, "h": 36},
    ]
    out = _resolve_connections(comps, [("beaker", "bottom", "iron_stand", "ring_top")])
    sc = next(c for c in out if c["type"] == "beaker")
    dc = next(c for c in out if c["type"] == "iron_stand")
    p1, p2 = port_xy(sc, "bottom"), port_xy(dc, "ring_top")
    dist = ((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2) ** 0.5
    assert dist < 0.5, f"beaker/iron_stand 端口未对齐 dist={dist}"


def test_import_and_prep_registered(client):
    from services import agent_tools, agent_core
    agent_tools.register_all()
    t = agent_core.get("import_and_prep")
    assert t is not None
    assert t.handler is not None
    assert agent_core.normalize_type("整理并备课") == "import_and_prep"


def test_import_and_prep_empty_corpus(client):
    from services import agent_tools, agent_core
    from services.agent_core import Ctx
    agent_tools.register_all()
    tool = agent_core.get("import_and_prep")

    class _Req:
        message = "整理并备课"

    async def _go():
        ctx = Ctx(sid="r30_empty", req=_Req(), session={"messages": []},
                  messages=[{"role": "user", "content": "整理并备课"}],
                  steps=[], data={}, itype="import_and_prep", save=lambda: None)
        return await tool.handler(ctx)

    r = _run(_go())
    assert "reply" in r
    # 空语料必须诚实说先去上传，不能假装备了课
    assert "上传" in r["reply"] or "导入" in r["reply"]


def test_vision_comment_says_mimo_primary():
    path = os.path.join(os.path.dirname(__file__), "services", "ai_service.py")
    text = open(path, encoding="utf-8").read()
    assert "MiMo" in text
    assert "视觉主力" in text or "MiMo 优先" in text or "MiMo-first" in text


def teardown_module():
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP, ignore_errors=True)
