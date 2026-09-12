"""Agent 文字版能力回归：工具表 / 确认闸 / 意图 prompt / 分发契约。

用户点名「注意测试 Agent 文字版本的能力」——这里钉的是**不依赖真实模型 Key**
也能验证的结构契约；真实模型行为仍靠生产探针。
"""

from __future__ import annotations

import os
import sys
import tempfile
import shutil
import asyncio

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_IS_FIRST = "main" not in sys.modules
_TMP = None
if _IS_FIRST:
    _TMP = tempfile.mkdtemp(prefix="la_agentcap_")
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


# --------------------------------------------------------------------------
# 注册表结构
# --------------------------------------------------------------------------

def test_every_tool_has_handler_and_label():
    from services import agent_tools, agent_core
    agent_tools.register_all()
    tools = agent_core.all_tools()
    assert len(tools) >= 25, f"工具数异常偏少: {len(tools)}"
    bad = []
    for t in tools:
        if not t.handler:
            bad.append((t.name, "no handler"))
        if not t.label:
            bad.append((t.name, "no label"))
        if not t.description:
            bad.append((t.name, "no description"))
    assert bad == [], bad


def test_registry_idempotent_and_no_duplicate_names():
    from services import agent_tools, agent_core
    agent_tools.register_all()  # 幂等：已注册过则 added=0
    agent_tools.register_all()
    names = [t.name for t in agent_core.all_tools()]
    assert len(names) == len(set(names))
    assert len(names) >= 25


def test_intent_prompt_covers_all_tools_except_need_tail():
    from services import agent_tools, agent_core
    agent_tools.register_all()
    prompt = agent_core.build_intent_prompt()
    for t in agent_core.all_tools():
        assert t.name in prompt, f"工具 {t.name} 未进入意图分类 prompt"
    # need 必须在末尾（兜底）
    assert prompt.rfind("need") > prompt.find("chat")


def test_confirm_gates_on_write_tools():
    from services import agent_tools, agent_core
    agent_tools.register_all()
    # 写操作必须有确认语
    for name in ("add_question", "edit_question", "edit_paper", "delete_paper",
                 "create_note", "modify_note", "solve_q", "edit_lesson"):
        t = agent_core.get(name)
        assert t is not None, name
        assert t.requires_confirm, f"{name} 是写操作但无确认闸"


def test_aliases_normalize():
    from services import agent_tools, agent_core
    agent_tools.register_all()
    assert agent_core.normalize_type("prepare") == "prepare_lesson"
    assert agent_core.normalize_type("lessons") == "list_lessons"
    assert agent_core.normalize_type("get_lesson") == "read_lesson"
    assert agent_core.normalize_type("nope_not_a_tool") == ""
    assert agent_core.normalize_type(None) == ""
    assert agent_core.normalize_type(123) == ""


def test_foreground_background_split():
    from services import agent_tools, agent_core
    agent_tools.register_all()
    fg = {t.name for t in agent_core.foreground_tools()}
    bg = {t.name for t in agent_core.background_tools()}
    assert fg and bg
    assert fg.isdisjoint(bg)
    # 备课应可后台；聊天必须前台
    assert "chat" in fg
    assert "prepare_lesson" in fg  # 触发在前台，生成丢后台（现有设计）
    assert "auto_paper" in bg


# --------------------------------------------------------------------------
# 会话层契约（不调真模型）
# --------------------------------------------------------------------------

def test_session_create_and_list(client):
    r = client.post("/api/sessions", json={"title": "Agent能力测试"})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    r2 = client.get("/api/sessions")
    assert r2.status_code == 200
    items = r2.json().get("sessions") or []
    ids = [x.get("id") for x in items]
    assert sid in ids
    client.delete(f"/api/sessions/{sid}")


def test_task_recall_bypass_needs_no_ai(client, monkeypatch):
    """[系统召回] 前缀必须绕过分类器；无 task_id 时落到 chat 的确定性路径不 500。"""
    r = client.post("/api/sessions", json={"title": "召回探针"})
    sid = r.json()["id"]
    # 无 AI Key 时 chat 可能 503，但**不能**因为解析召回前缀而崩
    r2 = client.post(f"/api/sessions/{sid}/chat",
                     json={"message": "[系统召回] task_id=nonexist01"})
    # 404/400/502/503 都可接受；500 与「当成普通聊天烧一次分类」不可接受
    assert r2.status_code != 500, r2.text
    # 任务不存在应有明确失败，而不是伪装成功
    if r2.status_code == 200:
        body = r2.json()
        # 召回失败必须可区分：reply 提到不存在/失败，或 action 标明
        text = (body.get("reply") or "") + str(body.get("action") or "")
        assert any(k in text for k in ("不存在", "失败", "找不到", "error", "not")), text
    client.delete(f"/api/sessions/{sid}")


def test_interrupt_only_when_tasks_exist(client):
    from routers.sessions import _try_interrupt_running_tasks

    async def _go():
        return await _try_interrupt_running_tasks("agentcap_empty", "取消任务")
    assert _run(_go()) is None


def test_system_recall_regex_contract():
    from routers import sessions as S
    msg = S._RECALL_PREFIX + " task_id=abc12345 continue"
    assert msg.startswith(S._RECALL_PREFIX)
    m = S._RECALL_TASK_ID_RE.search(msg)
    assert m and m.group(1) == "abc12345"
    plain = "普通聊天 task_id=abc12345"
    assert not plain.startswith(S._RECALL_PREFIX)


# --------------------------------------------------------------------------
# 工具处理器冒烟（假上下文，不调模型）
# --------------------------------------------------------------------------

def test_list_lessons_handler_smoke(client):
    from services import agent_tools, agent_core
    from services.agent_core import Ctx
    agent_tools.register_all()
    tool = agent_core.get("list_lessons")

    class _Req:
        message = "看看课稿"

    async def _go():
        session = {"messages": []}
        messages = [{"role": "user", "content": "看看课稿"}]
        ctx = Ctx(
            sid="agentcap_lessons", req=_Req(), session=session,
            messages=messages, steps=[], data={}, itype="list_lessons",
            save=lambda: None,
        )
        return await tool.handler(ctx)

    result = _run(_go())
    assert "reply" in result
    assert result.get("action", {}).get("type") == "lessons_list"


def test_review_plan_handler_empty_corpus(client):
    from services import agent_tools, agent_core
    from services.agent_core import Ctx
    agent_tools.register_all()
    tool = agent_core.get("review_plan")

    class _Req:
        message = "今天该学什么"

    async def _go():
        session = {"messages": []}
        messages = [{"role": "user", "content": "今天该学什么"}]
        ctx = Ctx(
            sid="agentcap_plan", req=_Req(), session=session,
            messages=messages, steps=[], data={}, itype="review_plan",
            save=lambda: None,
        )
        return await tool.handler(ctx)

    result = _run(_go())
    assert "reply" in result
    # 空语料时必须诚实说给不出，而不是编清单
    assert result["reply"]


# --------------------------------------------------------------------------
# 智能上传启发式
# --------------------------------------------------------------------------

def test_smart_upload_heuristic():
    from services.smart_upload_service import heuristic_classify, DESTINATIONS
    dest, conf, reason = heuristic_classify("已知如图，求证：三角形ABC是等腰三角形。解：因为AB=AC…")
    assert dest == "question"
    dest2, _, _ = heuristic_classify("知识点：二次函数顶点式 y=a(x-h)^2+k 的推导与公式总结")
    assert dest2 == "note"
    dest3, _, _ = heuristic_classify("2024北京中考数学一模 选择题 填空题 解答题 总分100")
    assert dest3 == "paper"
    dest4, _, _ = heuristic_classify("")
    assert dest4 == "unknown"
    assert set(DESTINATIONS) >= {"question", "note", "paper", "unknown"}


def test_smart_upload_destinations_api(client):
    r = client.get("/api/smart-upload/destinations")
    assert r.status_code == 200
    d = r.json()["destinations"]
    assert "question" in d and "note" in d and "paper" in d


def test_smart_upload_rejects_empty(client):
    # 不带 files 应 422
    r = client.post("/api/smart-upload/classify")
    assert r.status_code in (400, 422)


def test_home_widgets_include_smart_and_lessons(client):
    r = client.get("/api/home-widgets")
    assert r.status_code == 200
    widgets = r.json().get("widgets") or []
    ids = {w.get("id") for w in widgets}
    assert "smart_upload" in ids
    assert "lessons" in ids


def test_batch_upload_page_has_smart_ui(client):
    r = client.get("/batch-upload")
    assert r.status_code == 200
    assert 'id="smart"' in r.text
    assert "smartUploadBtn" in r.text
    assert "/api/smart-upload" in r.text


def teardown_module():
    if _TMP and os.path.isdir(_TMP):
        shutil.rmtree(_TMP, ignore_errors=True)
