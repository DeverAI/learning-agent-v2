"""R25 缺口回归：改密 / 课稿页 / 难度字段 / 搜题→笔记 / P4 打断。

设计原则（沿用项目惯例）：
- 不依赖真实 AI Key：改密、难度、路由都走确定性代码路径。
- 断言按**意图**而不是抓偶然字符串。
- 对 P4 打断：直接测词表与任务列表消费，不测真实后台生成。
- anyio.run 只接受**可调用**协程函数，不接受 coroutine 对象（本轮踩过）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 首导入方惯例：仅当本次运行尚未导入 main 时才重定向（FreqErr [首导入方绑定]）
_IS_FIRST = "main" not in sys.modules
_TMP = None
if _IS_FIRST:
    _TMP = tempfile.mkdtemp(prefix="la_r25_")
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
        # 保证本模块开始时鉴权关闭
        from config import load_settings, save_settings
        s = load_settings()
        if s.get("api_password"):
            s["api_password"] = ""
            save_settings(s)
        yield c
    # 收尾：清掉可能残留的密码，避免污染后续模块
    try:
        from config import load_settings, save_settings
        s = load_settings()
        if s.get("api_password"):
            s["api_password"] = ""
            save_settings(s)
    except Exception:
        pass


def _hdr(token: str = "") -> dict:
    return {"X-Auth-Token": token}


# --------------------------------------------------------------------------
# 1. 设置页改密码
# --------------------------------------------------------------------------

def test_password_status_default(client):
    r = client.get("/api/settings/password/status")
    assert r.status_code == 200
    assert r.json().get("has_password") is False


def test_password_set_and_verify_and_clear(client):
    from config import load_settings

    # 设置
    r = client.post("/api/settings/password", json={
        "current_password": "", "new_password": "r25test", "confirm_new_password": "r25test",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_password"] is True
    assert body["token"] == "r25test"
    assert load_settings().get("api_password") == "r25test"

    # status 带 token 能看到已设置
    r2 = client.get("/api/settings/password/status", headers=_hdr("r25test"))
    assert r2.status_code == 200
    assert r2.json()["has_password"] is True

    # 错误当前密码必须拒绝（不带正确 token 时 401；带错误 current 时 400）
    r3 = client.post("/api/settings/password", json={
        "current_password": "wrong", "new_password": "r25test2", "confirm_new_password": "r25test2",
    })
    assert r3.status_code in (400, 401)
    # 带正确 token + 错误 current → 400
    r3b = client.post("/api/settings/password", headers=_hdr("r25test"), json={
        "current_password": "wrong", "new_password": "r25test2", "confirm_new_password": "r25test2",
    })
    assert r3b.status_code == 400
    assert "当前密码" in r3b.json()["detail"]

    # 两次不一致必须拒绝
    r4 = client.post("/api/settings/password", headers=_hdr("r25test"), json={
        "current_password": "r25test", "new_password": "abcde", "confirm_new_password": "abcdf",
    })
    assert r4.status_code == 400

    # 正确当前密码可改
    r5 = client.post("/api/settings/password", headers=_hdr("r25test"), json={
        "current_password": "r25test", "new_password": "r25test2", "confirm_new_password": "r25test2",
    })
    assert r5.status_code == 200
    assert r5.json()["token"] == "r25test2"

    # 关闭鉴权（新密码留空）
    r6 = client.post("/api/settings/password", headers=_hdr("r25test2"), json={
        "current_password": "r25test2", "new_password": "", "confirm_new_password": "",
    })
    assert r6.status_code == 200
    assert r6.json()["has_password"] is False
    r7 = client.get("/api/settings/password/status")
    assert r7.status_code == 200
    assert r7.json()["has_password"] is False


def test_password_too_short_rejected(client):
    # 此时密码应已关闭
    r = client.post("/api/settings/password", json={
        "current_password": "", "new_password": "ab", "confirm_new_password": "ab",
    })
    assert r.status_code == 400
    assert "位" in r.json()["detail"]


# --------------------------------------------------------------------------
# 2. 课稿页
# --------------------------------------------------------------------------

def test_lessons_page_route(client):
    r = client.get("/lessons")
    assert r.status_code == 200
    assert "课稿" in r.text
    assert "createLessonBtn" in r.text
    assert "cacheOfflineBtn" in r.text


def test_lessons_in_nav(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "/lessons" in r.text


def test_lesson_crud_roundtrip(client):
    r = client.post("/api/lessons", json={
        "topic": "二次函数", "subject": "数学", "grade": "九上",
        "title": "R25 二次函数", "generate": False,
    })
    assert r.status_code == 200, r.text
    lid = r.json()["lesson_id"]
    assert lid

    r2 = client.get("/api/lessons")
    assert r2.status_code == 200
    ids = [x.get("lesson_id") for x in r2.json().get("lessons", [])]
    assert lid in ids

    r3 = client.get(f"/api/lessons/{lid}?max_chars=50000")
    assert r3.status_code == 200
    d = r3.json()
    assert "sections" in d
    assert "total_sections" in d
    assert "truncated" in d

    r4 = client.post(f"/api/lessons/{lid}/status", json={"status": "final"})
    assert r4.status_code == 200
    assert r4.json()["status"] == "final"

    r5 = client.delete(f"/api/lessons/{lid}")
    assert r5.status_code == 200
    r6 = client.get(f"/api/lessons/{lid}")
    assert r6.status_code == 404


# --------------------------------------------------------------------------
# 3. Question 难度字段
# --------------------------------------------------------------------------

def test_question_difficulty_migrate_and_filter(client):
    from models.database import async_session, engine
    from models.models import Question

    async def _check_col():
        async with engine.begin() as conn:
            rows = await conn.run_sync(
                lambda c: c.exec_driver_sql("PRAGMA table_info(questions)").fetchall()
            )
            cols = {r[1] for r in rows}
            return "difficulty" in cols
    assert _run(_check_col()), "questions.difficulty 列未迁移"

    qid = "r25diffq1"

    async def _insert():
        async with async_session() as db:
            db.add(Question(
                id=qid, folder_path=f"storage/questions/{qid}",
                subject="数学", grade="九上", status="done",
                ocr_text="二次函数图像", difficulty="自招", bank="default",
            ))
            await db.commit()
    _run(_insert())

    r = client.get("/api/questions", params={"difficulty": "自招", "limit": 50})
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()]
    assert qid in ids

    r2 = client.get("/api/questions", params={"difficulty": "基础", "limit": 50})
    ids2 = [x["id"] for x in r2.json()]
    assert qid not in ids2

    r3 = client.put(f"/api/questions/{qid}", json={"difficulty": "难"})
    assert r3.status_code == 200, r3.text
    assert r3.json().get("difficulty") == "难"


# --------------------------------------------------------------------------
# 4. 搜题页「存笔记」入口（静态契约）
# --------------------------------------------------------------------------

def test_search_page_has_note_entry(client):
    r = client.get("/search")
    assert r.status_code == 200
    html = r.text
    assert "data-note-qid" in html, "搜题匹配结果缺「存笔记」按钮"
    assert "saveMatchToNote" in html
    assert "saveOcrToNote" in html
    assert "ocrToNoteBtn" in html, "未匹配卡片缺「识别文本存笔记」"


def test_questions_list_has_difficulty_filter(client):
    r = client.get("/questions")
    assert r.status_code == 200
    assert "filterDifficulty" in r.text
    assert "自招" in r.text


# --------------------------------------------------------------------------
# 5. P4 打断判定
# --------------------------------------------------------------------------

def test_interrupt_regexes():
    from routers.sessions import _INTERRUPT_CANCEL_RE, _INTERRUPT_KEEP_RE
    assert _INTERRUPT_CANCEL_RE.search("取消任务")
    assert _INTERRUPT_CANCEL_RE.search("别做了")
    assert _INTERRUPT_CANCEL_RE.search("stop it now")
    # 已知边界（写进断言防将来误以为「怎么取消分母」不该命中）：
    # 含「取消」的数学话术可能误伤。当前策略是宁可误取消也不让任务烧额度。
    # 若将来误伤严重，再收紧为句首/独立成句匹配。
    assert _INTERRUPT_CANCEL_RE.search("这道题怎么取消分母")

    assert _INTERRUPT_KEEP_RE.search("继续讲")
    assert _INTERRUPT_KEEP_RE.search("接着做")
    assert not _INTERRUPT_KEEP_RE.search("这个知识点的后续是什么")


def test_interrupt_no_active_tasks_returns_none(client):
    from routers.sessions import _try_interrupt_running_tasks

    async def _go():
        return await _try_interrupt_running_tasks("r25_nosuch_sid", "取消任务")
    assert _run(_go()) is None


def test_interrupt_cancel_hits_running_task(client):
    from routers.sessions import _try_interrupt_running_tasks
    from services import background_agent as BA

    async def _go():
        task = await BA.create_task(sid="r25_int_sid", tool="prepare_lesson",
                                    title="R25打断测试课", params={})
        tid = task["task_id"]
        from models.database import async_session
        from models.models import AgentTask
        async with async_session() as db:
            t = await db.get(AgentTask, tid)
            t.status = "running"
            await db.commit()
        result = await _try_interrupt_running_tasks("r25_int_sid", "先取消，别做了")
        return tid, result

    tid, result = _run(_go())
    assert result is not None, "有 running 任务且说「取消」时必须处理打断"
    assert result["type"] == "task_interrupt"
    assert result["data"]["decision"] == "cancel"
    assert result["_interrupt_reply"]

    async def _check():
        from models.database import async_session
        from models.models import AgentTask
        async with async_session() as db:
            t = await db.get(AgentTask, tid)
            return t.cancel_requested
    assert _run(_check()) is True


def test_interrupt_keep_does_not_swallow_question(client):
    """R27：有运行中任务时说「继续讲…」必须**回落分类器**，不能吞成「我不打断」。"""
    from routers.sessions import _try_interrupt_running_tasks
    from services import background_agent as BA

    async def _go():
        task = await BA.create_task(sid="r25_keep_sid", tool="prepare_lesson",
                                    title="R25保留测试课", params={})
        tid = task["task_id"]
        from models.database import async_session
        from models.models import AgentTask
        async with async_session() as db:
            t = await db.get(AgentTask, tid)
            t.status = "running"
            await db.commit()
        result = await _try_interrupt_running_tasks("r25_keep_sid", "继续讲二次函数")
        return tid, result

    tid, result = _run(_go())
    assert result is None, "keep 不得短路，应继续走意图分类"
    async def _check():
        from models.database import async_session
        from models.models import AgentTask
        async with async_session() as db:
            t = await db.get(AgentTask, tid)
            return t.cancel_requested
    assert _run(_check()) is False


# --------------------------------------------------------------------------
# 6. 静态巡检 / 工具表
# --------------------------------------------------------------------------

def test_layout_audit_still_clean(client):
    from audit_layout import run_all
    issues = run_all()
    assert issues == [], issues


def test_tool_registry_has_handler_for_every_tool():
    from services import agent_tools
    from services import agent_core
    agent_tools.register_all()
    missing = [t.name for t in agent_core.all_tools() if t.handler is None]
    assert missing == [], f"以下工具无处理器: {missing}"


def test_settings_page_has_password_card(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "changePasswordBtn" in r.text
    assert "pwdCurrent" in r.text
