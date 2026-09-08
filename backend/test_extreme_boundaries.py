"""Extreme boundary regression tests.

These tests are intentionally run in a subprocess with a temporary STORAGE_DIR,
following the same isolation pattern as test_endpoint_contract.py.  They cover
the edge cases that previously caused 500s: huge/negative limit/offset values,
oversized prompt names, note download header injection, and AI endpoints without
configured API keys.
"""

import os
import subprocess
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))

_SMOKE_SCRIPT = r'''
import asyncio
import json
import logging
import logging.handlers
import os
import sys
import tempfile

BACKEND_DIR = os.getcwd()
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

import config

tmp = tempfile.mkdtemp(prefix="lh_extreme_")
config.STORAGE_DIR = tmp
for name in ("questions", "papers", "corrections", "paper_configs",
             "sessions", "notes", "quotes", "task_states", "logs", "pending"):
    os.makedirs(os.path.join(tmp, name), exist_ok=True)
config.QUESTIONS_DIR = os.path.join(tmp, "questions")
config.PAPERS_DIR = os.path.join(tmp, "papers")
config.CORRECTIONS_DIR = os.path.join(tmp, "corrections")
config.PAPER_CONFIGS_DIR = os.path.join(tmp, "paper_configs")
config.SETTINGS_FILE = os.path.join(tmp, "settings.json")
config.GALLERY_DIR = os.path.join(tmp, "data", "gallery")
config.DATABASE_URL = "sqlite+aiosqlite:///" + os.path.join(tmp, "app.db")

import logger as logger_module
logger_module.ERR_LOG_PATH = os.path.join(tmp, "Err.log")
for handler in list(logger_module._logger.handlers):
    if isinstance(handler, logging.handlers.RotatingFileHandler):
        handler.close()
        logger_module._logger.removeHandler(handler)
file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(tmp, "logs", "app.log"), encoding="utf-8",
    maxBytes=10 * 1024 * 1024, backupCount=1)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logger_module._formatter)
logger_module._logger.addHandler(file_handler)

# 关闭 AI 重试等待，空 Key 只失败一次且不阻塞测试。
import services.ai_service as ai_module
ai_module.MAX_RETRIES = 1
ai_module.RETRY_DELAY = 0

import models.models  # noqa: F401
from models.database import init_db, async_session
from models.models import Question, Paper, Note, UploadSession

asyncio.run(init_db())


async def seed():
    os.makedirs(os.path.join(config.QUESTIONS_DIR, "qtest1234"), exist_ok=True)
    async with async_session() as db:
        db.add(Question(
            id="qtest1234",
            folder_path=os.path.join(config.QUESTIONS_DIR, "qtest1234"),
            subject="数学", grade="八年级", knowledge_tags=["函数"],
            ocr_text="已知x+1=2，求x", question_html="<p>已知x+1=2，求x</p>",
            answer_html="<p>x=1</p>", standard_answer="1", status="done",
            bank="bank1", source_type="photo",
            structure_graph={"nodes": [{"id": 1, "text": "条件"}], "edges": []},
            comparison_regions={"regions": []},
            reference_svg_status="not_required",
        ))
        db.add(Paper(
            id="papertest1", title="测试卷", subject="数学", grade="八年级",
            paper_type="custom", question_ids=["qtest1234"],
            question_order=["qtest1234"],
            paper_html="<html><body><p>卷面</p></body></html>",
            answer_html="<html><body><p>答案</p></body></html>",
            generation_params={
                "subject": "数学", "grade": "八年级",
                "paper_type": "custom", "question_count": 1,
            },
        ))
        db.add(Note(
            id="notehdr", title="x\r\nX-Evil: 1", content="内容",
            subject="数学", grade="八年级",
        ))
        db.add(UploadSession(
            id="sestest1", title="整卷上传边界会话",
            subject="数学", grade="八年级",
            question_ids=["qtest1234"], status="ready",
            upload_mode="one_per_image",
        ))
        # 学生作答临时记录（correction_query）：其图片不得经公开静态路径读取
        cq_dir = os.path.join(config.QUESTIONS_DIR, "cqtest1234")
        os.makedirs(cq_dir, exist_ok=True)
        with open(os.path.join(cq_dir, "original_0.jpg"), "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"0" * 32)
        db.add(Question(
            id="cqtest1234", folder_path=cq_dir,
            raw_image_path=os.path.join(cq_dir, "original_0.jpg"),
            status="search_staged", source_type="correction_query",
            bank="correction_queries", capture_mode="single_question",
        ))
        await db.commit()


asyncio.run(seed())

# 提前写入 settings 标记文件，用于验证 pending 路径穿越不会删除它。
with open(config.SETTINGS_FILE, "w", encoding="utf-8") as f:
    json.dump({"marker": "keep"}, f)

# system_messages 也写入临时目录，避免污染真实 storage。
import services.audit_service as audit_service
audit_service.STORAGE_DIR = tmp
audit_service.SYSTEM_MSG_PATH = os.path.join(tmp, "system_messages.json")

from fastapi.testclient import TestClient
from main import app

failures = []


def no_500(resp, label):
    if resp.status_code == 500:
        failures.append(f"{label}: status=500 body={resp.text[:200]}")


def expect_status(resp, label, statuses):
    if resp.status_code not in statuses:
        failures.append(
            f"{label}: expected {statuses}, got {resp.status_code} body={resp.text[:200]}"
        )


with TestClient(app, raise_server_exceptions=False, headers={"X-Auth-Token": "Ntmhzsgtc"}) as client:
    for path in (
        "/api/questions?limit=999999999999999999999",
        "/api/questions?offset=-1",
        "/api/questions?offset=999999999999999999999",
        "/api/questions?limit=-1",
        "/api/questions/flagged/list?limit=99999999",
        "/api/questions/flagged/list?limit=-1",
        "/api/papers?limit=999999999999999999999",
        "/api/papers?offset=999999999999999999999",
        "/api/papers/library?limit=-1",
        "/api/paper-configs?limit=-1&offset=-5",
        "/api/paper-configs?offset=999999999999999999999",
        "/api/correction/?offset=999999999999999999999",
        "/api/correction/history?offset=999999999999999999999",
        "/api/notes?limit=-1",
    ):
        r = client.get(path)
        no_500(r, f"GET {path}")

    r = client.post("/api/prompts", json={"name": "x" * 100000})
    expect_status(r, "POST /api/prompts oversized name", {422})

    r = client.post("/api/diagram/calibrate", json={"components": [{"x": 10, "y": 20}]})
    expect_status(r, "POST /api/diagram/calibrate missing type", {400})

    r = client.get("/api/notes/notehdr/download")
    expect_status(r, "GET note download", {200})
    cd = r.headers.get("content-disposition", "")
    if "\r" in cd or "\n" in cd:
        failures.append(f"note download header injection not sanitized: {cd!r}")

    # pending 路径穿越不得删除 settings.json
    r = client.post("/api/questions/..%5Csettings/cancel-pending")
    expect_status(r, "cancel-pending traversal", {400})
    if not os.path.exists(config.SETTINGS_FILE):
        failures.append("cancel-pending traversal deleted settings.json")

    # 设置页的 style_notes 必须与聊天/解题路径共用并持久化
    r = client.put("/api/profile", json={"style_notes": "设置页测试偏好"})
    expect_status(r, "PUT profile style_notes", {200})
    r = client.get("/api/profile")
    if r.status_code != 200 or r.json().get("style_notes") != "设置页测试偏好":
        failures.append(f"profile style_notes not persisted: {r.status_code} {r.text[:200]}")

    # 创建会话，供会话聊天无 Key 测试使用
    r = client.post("/api/sessions", json={"title": "边界测试会话"})
    session_id = r.json().get("id") if r.status_code == 200 else "missing"

    # AI 端点无 Key 时必须返回 502/503，不得以 500 或伪成功结束。
    ai_cases = [
        ("POST", "/api/chat", {"message": "你好"}),
        ("POST", "/api/questions/qtest1234/chat", {"message": "你好"}),
        ("POST", "/api/questions/qtest1234/challenge", {}),
        ("POST", "/api/questions/qtest1234/regenerate", {}),
        ("POST", "/api/questions/qtest1234/structure-graph/generate", {}),
        ("POST", "/api/questions/qtest1234/structure-graph/check", {}),
        ("POST", "/api/questions/qtest1234/structure-graph/rewrite", {}),
        ("POST", "/api/questions/qtest1234/comparison/generate", {}),
        ("POST", "/api/diag/ai-analyze/qtest1234", {}),
        ("POST", f"/api/sessions/{session_id}/chat", {"message": "你好"}),
        ("POST", "/api/notes/notehdr/chat", {"message": "你好"}),
        ("POST", "/api/diagram/generate", {"question_id": "qtest1234", "prompt": "画一个三角形"}),
        ("POST", "/api/diagram/insert", {"question_id": "qtest1234", "prompt": "加一条辅助线"}),
        ("POST", "/api/papers/generate", {"subject": "数学", "grade": "八年级", "paper_type": "custom", "question_count": 1}),
        ("POST", "/api/papers/generate-worksheet", {"subject": "数学", "grade": "八年级", "custom_prompt": "函数"}),
        ("POST", "/api/papers/papertest1/regenerate", {}),
        ("POST", "/api/ocr/session/sestest1/make-paper", {}),
    ]
    for method, path, body in ai_cases:
        r = client.post(path, json=body)
        if r.status_code == 500:
            failures.append(f"{path}: AI missing key returned 500 body={r.text[:200]}")
        elif r.status_code not in (502, 503):
            failures.append(
                f"{path}: AI missing key expected 502/503, got {r.status_code} body={r.text[:200]}"
            )

    # 分类器返回 data:null 时，/api/chat 也必须归一化为 {}，不能 500。
    async def fake_call(url, key, payload, timeout=None):
        return '{"type":"style","data":null}'

    ai_module.ai_service._call = fake_call
    r = client.post("/api/chat", json={"message": "确认修改偏好 请把解答风格改为简洁"})
    if r.status_code == 500:
        failures.append(f"/api/chat data:null returned 500 body={r.text[:200]}")
    elif r.status_code not in (200, 201):
        failures.append(f"/api/chat data:null expected 200, got {r.status_code} body={r.text[:200]}")

    # 分类器返回合法 JSON 但非对象（null）时也必须回退 chat，不能 500。
    async def fake_call_null(url, key, payload, timeout=None):
        return "null"

    ai_module.ai_service._call = fake_call_null
    r = client.post("/api/chat", json={"message": "你好"})
    if r.status_code == 500:
        failures.append(f"/api/chat null intent returned 500 body={r.text[:200]}")
    elif r.status_code not in (200, 201, 502, 503):
        failures.append(f"/api/chat null intent expected non-500, got {r.status_code} body={r.text[:200]}")

    # 示意图 spec 端点：非法 ID / 越界 index 必须 4xx，无 spec 必须 404，不得 500。
    for path, expected in (
        ("/api/diagram/spec/notanid/0", {400, 404}),
        ("/api/diagram/spec/qtest1234/-1", {400}),
        ("/api/diagram/spec/qtest1234/99999", {400}),
        ("/api/diagram/spec/qtest1234/0", {404}),
        ("/api/diagram/spec/qtest1234%2F..%2F1", {400, 404}),
    ):
        r = client.get(path)
        expect_status(r, f"GET {path}", expected)

    # 隐私边界：correction_query/search_query 临时记录的图片不得经 /storage/questions 公开读取
    r = client.get("/storage/questions/cqtest1234/original_0.jpg")
    expect_status(r, "storage privacy correction_query image", {404})

    # settings import：NaN/Inf/超大整数分数必须 400，不得绕过校验写入画像
    for bad_score in ("NaN", "Infinity", 10 ** 400, -1):
        r = client.put("/api/settings/import", json={
            "profile": {"paper_history": [{"paper_id": "x", "title": "t", "score": bad_score, "total": 100}]},
        })
        expect_status(r, f"settings import bad score {bad_score!r}", {400})
    r = client.put("/api/settings/import", json={
        "profile": {"paper_history": [{"paper_id": "x", "title": "t", "score": 80, "total": 100}]},
    })
    expect_status(r, "settings import valid score", {200})
    r = client.get("/api/profile")
    if r.status_code != 200 or not r.json().get("paper_history"):
        failures.append(f"profile paper_history lost after valid import: {r.status_code} {r.text[:200]}")
    else:
        item = r.json()["paper_history"][0]
        if item.get("score") != 80 or item.get("total") != 100:
            failures.append(f"profile paper_history wrong after import: {item}")

    # knowledge 意图（无 GLM Key 时必须给出可理解的回复，不能 500）。
    async def fake_call_knowledge(url, key, payload, timeout=None):
        return '{"type":"knowledge","data":{"query":"勾股定理"}}'

    ai_module.ai_service._call = fake_call_knowledge
    r = client.post("/api/chat", json={"message": "勾股定理是什么"})
    if r.status_code == 500:
        failures.append(f"/api/chat knowledge intent returned 500 body={r.text[:200]}")
    elif r.status_code not in (200, 201, 502, 503):
        failures.append(f"/api/chat knowledge intent expected non-500, got {r.status_code} body={r.text[:200]}")

    # 知识库端点必须走临时 STORAGE_DIR，不触碰真实 backend/knowledge 数据。
    r = client.get("/api/knowledge")
    expect_status(r, "GET /api/knowledge", {200})
    r = client.get("/api/knowledge/search?q=%E5%87%BD%E6%95%B0")
    expect_status(r, "GET /api/knowledge/search", {200})
    r = client.delete("/api/knowledge/notanid")
    expect_status(r, "DELETE /api/knowledge/notanid", {404})
    r = client.get("/api/knowledge/notanid")
    expect_status(r, "GET /api/knowledge/notanid", {404})

    # AI 成功但 diagram_prompts 为空时，regenerate 必须正常保存（此前会 NameError）。
    async def fake_regenerate(*args, **kwargs):
        return {
            "question_html": "<p>重新生成的题面内容</p>",
            "answer_html": "<p>重新生成的详细解析内容，长度足够通过完整性校验。</p>",
            "standard_answer": "x=1",
            "question_type": "解答",
            "diagram_prompts": [],
        }

    ai_module.ai_service.deepseek_regenerate_question = fake_regenerate
    r = client.post("/api/questions/qtest1234/regenerate", json={})
    expect_status(r, "regenerate without diagram_prompts", {200})

if failures:
    for f in failures:
        print("FAIL:", f)
    sys.exit(1)

print("EXTREME_OK")
'''


def _run_smoke() -> subprocess.CompletedProcess:
    tmp_dir = tempfile.mkdtemp(prefix="lh_extreme_runner_")
    script_path = os.path.join(tmp_dir, "extreme_contract.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(_SMOKE_SCRIPT)
    return subprocess.run(
        [sys.executable, script_path],
        cwd=BACKEND_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )


def test_extreme_boundaries_and_ai_missing_key():
    proc = _run_smoke()
    assert proc.returncode == 0, proc.stdout[-4000:]
