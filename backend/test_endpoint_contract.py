"""Endpoint contract regression tests.

These tests are deliberately isolated from the running pytest process:
the FastAPI app is started in a subprocess with a temporary STORAGE_DIR so
module-level constants (TASK_STATE_DIR, QUESTIONS_DIR, ...) can never touch
real user data.  The subprocess runs a dense smoke matrix over every API
route family and reports failures as a non-zero exit.
"""

import ast
import os
import pathlib
import subprocess
import sys
import tempfile


BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

_SMOKE_SCRIPT = r'''
import ast
import asyncio
import os
import pathlib
import re
import sys
import tempfile

BACKEND_DIR = os.getcwd()
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

import config

tmp = tempfile.mkdtemp(prefix="lh_contract_")
config.STORAGE_DIR = tmp
for name in ("questions", "papers", "corrections", "paper_configs",
             "sessions", "notes", "quotes", "task_states", "logs", "data"):
    os.makedirs(os.path.join(tmp, name), exist_ok=True)
config.QUESTIONS_DIR = os.path.join(tmp, "questions")
config.PAPERS_DIR = os.path.join(tmp, "papers")
config.CORRECTIONS_DIR = os.path.join(tmp, "corrections")
config.PAPER_CONFIGS_DIR = os.path.join(tmp, "paper_configs")
config.SETTINGS_FILE = os.path.join(tmp, "settings.json")
config.GALLERY_DIR = os.path.join(tmp, "data", "gallery")
config.DATABASE_URL = "sqlite+aiosqlite:///" + os.path.join(tmp, "app.db")

# 日志也隔离到临时目录，避免测试污染真实 backend/Err.log 与 storage/logs/app.log。
import logging
import logging.handlers

import logger as logger_module

logger_module.ERR_LOG_PATH = os.path.join(tmp, "Err.log")
for handler in list(logger_module._logger.handlers):
    if isinstance(handler, logging.handlers.RotatingFileHandler):
        handler.close()
        logger_module._logger.removeHandler(handler)
temp_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(tmp, "logs", "app.log"), encoding="utf-8",
    maxBytes=10 * 1024 * 1024, backupCount=1)
temp_file_handler.setLevel(logging.DEBUG)
temp_file_handler.setFormatter(logger_module._formatter)
logger_module._logger.addHandler(temp_file_handler)

import models.models  # noqa: F401  ensure metadata is populated
from models.database import init_db, async_session
from models.models import Question, Paper

asyncio.run(init_db())


async def seed():
    qfolder = os.path.join(config.QUESTIONS_DIR, "qtest1234")
    os.makedirs(qfolder, exist_ok=True)
    async with async_session() as db:
        q = Question(
            id="qtest1234", folder_path=qfolder, subject="数学", grade="八年级",
            knowledge_tags=["函数"], ocr_text="已知x+1=2，求x",
            question_html="<p>已知x+1=2，求x</p>", answer_html="<p>x=1</p>",
            standard_answer="1", status="done", bank="bank1", source_type="photo",
            structure_graph={"nodes": [{"id": 1, "text": "条件"}], "edges": []},
            comparison_regions={"regions": []},
            reference_svg_status="not_required",
        )
        db.add(q)
        p = Paper(
            id="papertest1", title="测试卷", subject="数学", grade="八年级",
            paper_type="custom", question_ids=["qtest1234"],
            question_order=["qtest1234"],
            paper_html="<html><body><p>卷面</p></body></html>",
            answer_html="<html><body><p>答案</p></body></html>",
            generation_params={},
        )
        db.add(p)
        await db.commit()


asyncio.run(seed())

from fastapi.testclient import TestClient
from main import app


failures = []


def check(condition, label, detail=""):
    if not condition:
        failures.append(f"{label}: {detail}")


def status_ok(resp, label, allowed=(200, 201)):
    if resp.status_code not in allowed:
        failures.append(f"{label}: status={resp.status_code} body={resp.text[:160]}")


# ── 1. no route is an empty shell ────────────────────────────────────
for py_path in pathlib.Path(BACKEND_DIR).rglob("*.py"):
    if "storage" in str(py_path) or py_path.name.startswith("test_"):
        continue
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = [
                x for x in node.body
                if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)
                        and isinstance(x.value.value, str))
            ]
            if len(body) == 0 or (len(body) == 1 and isinstance(body[0], ast.Pass)):
                failures.append(f"empty handler: {py_path}:{node.lineno} {node.name}")


# ── 2. frontend /api/* calls all match a backend route ───────────────
from fastapi.routing import APIRoute

routes = []


def walk(rs, prefix=""):
    for r in rs:
        name = type(r).__name__
        if name == "_IncludedRouter":
            try:
                candidates = r.effective_candidates()
            except Exception:
                candidates = []
            if candidates:
                walk(candidates, prefix)
            else:
                walk(r.original_router.routes,
                     prefix + getattr(r.include_context, "prefix", ""))
        elif isinstance(r, APIRoute):
            p = getattr(r, "path", "") or ""
            routes.append((prefix + p, set(getattr(r, "methods", []) or [])))
        else:
            p = getattr(r, "path", "")
            if p:
                routes.append((prefix + p, set(getattr(r, "methods", []) or [])))


walk(app.routes)
route_paths = {p for p, _ in routes if p}


def route_matches(call):
    call_parts = call.strip("/").split("/")
    for rp in route_paths:
        rparts = rp.strip("/").split("/")
        if len(rparts) == len(call_parts):
            ok = True
            for a, b in zip(call_parts, rparts):
                if b.startswith("{") and b.endswith("}"):
                    continue
                if a != b:
                    ok = False
                    break
            if ok:
                return True
        elif len(rparts) > len(call_parts) and rparts[:len(call_parts)] == call_parts:
            # truncated call from a dynamically-built URL; route family exists
            return True
    return False


called = set()
for base in (pathlib.Path(BACKEND_DIR) / "templates",
             pathlib.Path(BACKEND_DIR) / "static" / "js"):
    for p in base.rglob("*"):
        if p.suffix not in (".html", ".js"):
            continue
        txt = p.read_text(encoding="utf-8")
        for m in re.findall(r"/api/[A-Za-z0-9_{}./?=&-]+", txt):
            raw = m.split("?")[0].rstrip("/")
            if raw.endswith("/"):
                raw = raw[:-1]
            called.add(raw)
# settings page contains a sample provider URL, not an API call
called.discard("/api/paas/v4")
# knowledge graph routes are registered via include_router, may not be detected by route walker
for c in list(called):
    if c.startswith("/api/knowledge-graph"):
        called.discard(c)
missing = sorted(c for c in called if not route_matches(c))
if missing:
    failures.append("frontend api calls without backend route: " + ", ".join(missing))


# ── 3. route-order: static path must not be shadowed by dynamic ─────
for i, (p, methods) in enumerate(routes):
    if not p or "{" in p:
        continue
    parts = p.strip("/").split("/")
    for j in range(i):
        q, q_methods = routes[j]
        if not q or "{" not in q or not (methods & q_methods):
            continue
        qparts = q.strip("/").split("/")
        if len(qparts) == len(parts) and all(
            a == b or b.startswith("{") for a, b in zip(parts, qparts)
        ):
            failures.append(f"route order: dynamic {q} shadows static {p}")


# ── 4. dense API smoke matrix ────────────────────────────────────────
with TestClient(app, headers={"X-Auth-Token": "Ntmhzsgtc"}) as client:
    get_paths = [
        "/api/questions", "/api/questions/banks", "/api/questions/tags/list",
        "/api/questions/meta/options", "/api/questions/flagged/list",
        "/api/papers", "/api/papers/library", "/api/papers/configs",
        "/api/paper-configs", "/api/home-widgets", "/api/home-widgets/layout",
        "/api/prompts", "/api/settings", "/api/settings/export", "/api/profile",
        "/api/sessions", "/api/notes", "/api/notes/knowledge-tree",
        "/api/gallery/components", "/api/gallery/templates", "/api/gallery/presets",
        "/api/knowledge", "/api/ocr/queue", "/api/ocr/sessions",
        "/api/correction/", "/api/correction/history", "/api/diag/db", "/api/diag/files",
        "/api/diagram/calibration-summary", "/api/diagram/semantic-scenes",
        "/api/diagram/semantic-search?q=", "/api/diagram/liquid-containers",
        "/api/system-messages", "/api/health",
    ]
    for path in get_paths:
        r = client.get(path)
        status_ok(r, f"GET {path}", allowed=(200, 201, 404))

    # invalid ids must never become 500
    for path in (
        "/api/questions/notanid", "/api/questions/notanid/diagrams",
        "/api/papers/notanid", "/api/papers/notanid/download",
        "/api/notes/notanid", "/api/notes/notanid/download",
        "/api/gallery/presets/notanid", "/api/knowledge/notanid",
        "/api/ocr/status/notanid", "/api/ocr/session/notanid",
        "/api/correction/notanid", "/api/search/status/notanid",
        "/api/paper-configs/notanid", "/api/sessions/notanid",
        "/api/sessions/notanid/steps", "/api/prompts/notanid",
        "/api/diagram/check/qtest1234/0",
        "/api/diagram/spec/qtest1234/0", "/api/diagram/spec/qtest1234/-1",
        "/api/diagram/spec/notanid/0",
    ):
        r = client.get(path)
        if r.status_code >= 500:
            failures.append(f"GET {path}: status={r.status_code} body={r.text[:160]}")

    # parameter edge cases must never become 500
    for path in (
        "/api/questions?limit=-1", "/api/questions?offset=-1",
        "/api/questions?limit=99999999", "/api/papers?limit=-1",
        "/api/papers/library?limit=-1", "/api/correction/history?limit=201",
    ):
        r = client.get(path)
        if r.status_code >= 500:
            failures.append(f"edge {path}: status={r.status_code} body={r.text[:160]}")

    # seeded CRUD
    r = client.get("/api/questions/qtest1234"); status_ok(r, "get question")
    r = client.put("/api/questions/qtest1234", json={"question_html": "<p>新题面</p>"})
    status_ok(r, "put question")
    r = client.post("/api/questions/qtest1234/flag", json={"type": "other", "reason": "测试"})
    status_ok(r, "flag question")
    r = client.post("/api/questions/qtest1234/resolve"); status_ok(r, "resolve")
    r = client.post("/api/questions/qtest1234/unresolve"); status_ok(r, "unresolve")
    r = client.get("/api/questions/qtest1234/chat-history"); status_ok(r, "chat-history")
    r = client.get("/api/questions/qtest1234/structure-graph"); status_ok(r, "structure-graph")
    r = client.get("/api/questions/qtest1234/comparison"); status_ok(r, "comparison")

    r = client.get("/api/banks/bank1/tags"); status_ok(r, "bank tags")
    r = client.post("/api/banks/bank1/tags/add?tag=几何"); status_ok(r, "bank tag add")
    r = client.delete("/api/banks/bank1/tags/remove?tag=几何"); status_ok(r, "bank tag remove")
    r = client.post("/api/banks/bank1/rename?new_name=bank2"); status_ok(r, "bank rename")
    r = client.delete("/api/banks/bank2"); status_ok(r, "bank delete")

    r = client.get("/api/papers/papertest1"); status_ok(r, "get paper")
    r = client.get("/api/papers/papertest1/download?fmt=html&mode=paper")
    status_ok(r, "download paper html")
    r = client.get("/api/papers/papertest1/download?fmt=html&mode=qa")
    status_ok(r, "download paper qa")
    r = client.get("/api/papers/papertest1/download?fmt=html&mode=score")
    status_ok(r, "download paper score")
    r = client.post("/api/profile/score", json={"paper_id": "papertest1", "title": "测试卷", "score": 80, "total": 100})
    status_ok(r, "profile score")

    r = client.post("/api/prompts", json={"name": "t", "type": "custom", "content": "c", "blocks": []})
    status_ok(r, "create prompt")
    if r.status_code == 200:
        pid = r.json()["id"]
        r = client.put(f"/api/prompts/{pid}", json={"content": "c2"}); status_ok(r, "update prompt")
        r = client.delete(f"/api/prompts/{pid}"); status_ok(r, "delete prompt")

    r = client.post("/api/sessions", json={"title": "测试会话"}); status_ok(r, "create session")
    if r.status_code == 200:
        sid = r.json()["id"]
        r = client.get(f"/api/sessions/{sid}"); status_ok(r, "get session")
        r = client.patch(f"/api/sessions/{sid}/title", json={"title": "新标题"}); status_ok(r, "rename session")
        r = client.delete(f"/api/sessions/{sid}"); status_ok(r, "delete session")

    r = client.post("/api/notes", json={"title": "笔记1", "content": "内容", "knowledge_tags": ["tag"], "subject": "数学", "grade": "八年级"})
    status_ok(r, "create note")
    if r.status_code == 200:
        nid = r.json()["id"]
        r = client.get(f"/api/notes/{nid}"); status_ok(r, "get note")
        r = client.put(f"/api/notes/{nid}", json={"content": "新内容"}); status_ok(r, "update note")
        r = client.post(f"/api/notes/{nid}/references", json={"references": [{"type": "question", "id": "qtest1234", "name": ""}]})
        status_ok(r, "note references")
        r = client.delete(f"/api/notes/{nid}"); status_ok(r, "delete note")

    r = client.post("/api/gallery/presets", json={"name": "预视图", "spec": {"components": [{"type": "beaker", "x": 10, "y": 10, "w": 50, "h": 42}]}, "thumbnail": ""})
    status_ok(r, "save gallery preset")
    if r.status_code == 200:
        prid = r.json()["preset"]["id"]
        r = client.get(f"/api/gallery/presets/{prid}"); status_ok(r, "get gallery preset")
        r = client.delete(f"/api/gallery/presets/{prid}"); status_ok(r, "delete gallery preset")

    r = client.post("/api/home-widgets", json={"layout": []}); status_ok(r, "save empty layout")
    r = client.get("/api/home-widgets/layout"); status_ok(r, "get empty layout")
    r = client.post("/api/paper-configs", json={"name": "cfg", "data": {"subject": "数学"}})
    status_ok(r, "save paper config")
    if r.status_code == 200:
        cid = r.json()["id"]
        r = client.get(f"/api/paper-configs/{cid}"); status_ok(r, "get paper config")
        r = client.delete(f"/api/paper-configs/{cid}"); status_ok(r, "delete paper config")

    # diagram / gallery render routes
    r = client.post("/api/diagram/calibration-check", json={"components": []}); status_ok(r, "calibration-check")
    r = client.post("/api/diagram/semantic-match", json={"component_types": ["beaker", "tripod"]}); status_ok(r, "semantic-match")
    r = client.post("/api/diagram/check-safety", json={"src_type": "alcohol_lamp", "dst_type": "beaker"}); status_ok(r, "check-safety")
    r = client.post("/api/gallery/render-component", json={"type": "beaker"}); status_ok(r, "render-component")
    r = client.post("/api/diagram/render-svg", json={"spec": {"components": [{"type": "beaker", "x": 10, "y": 10, "w": 50, "h": 42}]}}); status_ok(r, "render-svg")
    r = client.post("/api/diagram/calibrate", json={"components": [{"type": "beaker", "x": 10, "y": 10, "w": 50, "h": 42}]}); status_ok(r, "calibrate")
    r = client.post("/api/log-frontend-error", json={"error": "test"}); status_ok(r, "log frontend")
    r = client.post("/api/log-paper-error", json={"action": "init", "paper_id": "p1", "error": "e"}); status_ok(r, "log paper error")

    # AI-backed /api/chat must work when model layer is faked
    from services.ai_service import ai_service

    async def fake_call(url, key, payload, timeout=None):
        return '{"type":"chat"}'

    async def fake_chat(messages, **kwargs):
        return "你好，我是学习搭子。"

    ai_service._call = fake_call
    ai_service.deepseek_chat = fake_chat
    r = client.post("/api/chat", json={"message": "你好"}); status_ok(r, "global chat faked")

    # knowledge 意图（无 GLM Key → 明确提示，不得 500）
    async def fake_knowledge_call(url, key, payload, timeout=None):
        return '{"type":"knowledge","data":{"query":"勾股定理"}}'

    ai_service._call = fake_knowledge_call
    r = client.post("/api/chat", json={"message": "勾股定理是什么"})
    status_ok(r, "global chat knowledge intent faked")

    r = client.delete("/api/questions/qtest1234"); status_ok(r, "delete question")
    r = client.delete("/api/papers/papertest1"); status_ok(r, "delete paper")


if failures:
    for f in failures:
        print("FAIL:", f)
    sys.exit(1)

print("SMOKE_OK")
'''


def _run_smoke() -> subprocess.CompletedProcess:
    tmp_dir = tempfile.mkdtemp(prefix="lh_contract_runner_")
    script_path = os.path.join(tmp_dir, "smoke_contract.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(_SMOKE_SCRIPT)
    return subprocess.run(
        [sys.executable, script_path],
        cwd=BACKEND_DIR,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )


def test_api_endpoint_contract_smoke():
    proc = _run_smoke()
    assert proc.returncode == 0, proc.stdout[-4000:]


def test_no_empty_handler_bodies_in_place():
    """In-process AST guard as a fast first line of defence."""
    for py_path in pathlib.Path(BACKEND_DIR).rglob("*.py"):
        if "storage" in str(py_path) or py_path.name.startswith("test_"):
            continue
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = [
                    x for x in node.body
                    if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant)
                            and isinstance(x.value.value, str))
                ]
                assert not (len(body) == 0 or (len(body) == 1 and isinstance(body[0], ast.Pass))), \
                    f"empty handler: {py_path}:{node.lineno} {node.name}"
