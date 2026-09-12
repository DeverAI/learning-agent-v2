# -*- coding: utf-8 -*-
"""R23：九大重点第一批落点的回归钉子。

本组测试对应 2026-09-12 的 R23 检修，覆盖六件事：

1. **后台任务完成 → 召回 Agent 继续工作**（用户点名的「工具执行完毕召回Agent继续工作」）
   三个环节缺一不可：工具表登记 / 会话确定性旁路 / 前端自动召回且只召回一次。
2. **讲课页「边看边问」**（讲解不中断，提问带当前步骤上下文）
3. **课稿离线缓存**（GET 白名单里必须有 /api/lessons，否则备好的课离线看不了）
4. **鉴权韧性**（token 落 localStorage + 401 补密重试**一次**；否则改密码＝全站 401 且无入口）
5. **安卓 TTS**（长文分片 / 初始化排队 / 屏幕常亮）
6. **SVG 质量自检接线**（_validate_svg_quality 此前是零调用点的死代码）

写法约定与既有轮次一致：能实测的实测（直接调处理器、直接写盘），
跨文件契约用「单一来源 + 两侧断言」而不是各写一份常量。
"""
import asyncio
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BACKEND = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(BACKEND, rel), encoding="utf-8") as f:
        return f.read()


# ==========================================================================
# 1. 召回：工具表 + 分发表
# ==========================================================================

def test_task_recall_registered_with_handler():
    from services import agent_tools
    agent_tools.register_all()
    from services.agent_core import REGISTRY
    assert "task_recall" in REGISTRY, "task_recall 未登记进工具表"
    assert "task_recall" in agent_tools._HANDLERS, "task_recall 没有处理器"
    assert REGISTRY["task_recall"].foreground, "召回必须走前台（要立刻回话）"


def test_task_recall_appears_in_generated_intent_prompt():
    """prompt 由表生成；漏掉就等于分类器永远吐不出这个类型（R11 的 edit_paper 教训）。"""
    from services import agent_tools
    agent_tools.register_all()
    from services.agent_core import build_intent_prompt
    assert "task_recall" in build_intent_prompt()


def test_every_registered_tool_has_handler():
    from services import agent_tools
    agent_tools.register_all()
    from services.agent_core import REGISTRY
    missing = [n for n in REGISTRY if n not in agent_tools._HANDLERS]
    assert not missing, f"这些工具没有处理器: {missing}"


# ==========================================================================
# 2. 召回：前后端消息格式契约（两侧都对着同一个来源断言）
# ==========================================================================

def test_recall_prefix_is_a_single_source_of_truth():
    from routers import sessions as S
    html = _read("templates/agent.html")
    # 前端必须用同一前缀拼消息；否则后端旁路永不命中，召回转成普通对话
    assert "'" + S._RECALL_PREFIX + " " in html, (
        "agent.html 没有使用 sessions._RECALL_PREFIX 拼召回消息 —— 前后端格式已经分叉")


def test_recall_task_id_regex_matches_frontend_message():
    from routers import sessions as S
    msg = S._RECALL_PREFIX + " 后台任务已完成，task_id=" + "a1b2c3d4e5f6" + "，请读取结果并继续讲解。"
    m = S._RECALL_TASK_ID_RE.search(msg)
    assert m and m.group(1) == "a1b2c3d4e5f6", "后端解析不出前端发来的 task_id"


def test_sessions_bypasses_classifier_for_recall():
    """召回必须确定性路由：过分类器就可能被误判到别的工具，召回静默失效。"""
    src = _read("routers/sessions.py")
    assert "startswith(_RECALL_PREFIX)" in src
    assert 'itype = "task_recall"' in src


# ==========================================================================
# 3. 召回：处理器真的读任务产出并回话
# ==========================================================================

class _Req:
    def __init__(self, message):
        self.message = message


def _mk_ctx(message, data):
    from services.agent_core import Ctx
    session = {"messages": [{"role": "user", "content": message}]}
    saved = {"n": 0}

    def _save():
        saved["n"] += 1

    ctx = Ctx(sid="testsid_r23", req=_Req(message), session=session,
              messages=list(session["messages"]), steps=[], data=data,
              itype="task_recall", save=_save)
    return ctx, session, saved


def _done_task(task_id="t_r23_0001"):
    return {
        "task_id": task_id, "sid": "testsid_r23", "tool": "prepare_lesson",
        "title": "备课：勾股定理", "status": "done", "progress": 100.0,
        "output": {"lesson_id": "abc12345", "title": "勾股定理", "total_sections": 4,
                   "filled_now": 4, "already_filled": 0, "empty_sections": 0},
        "partial": {}, "steps": [],
    }


def test_h_task_recall_reports_output_and_continues(monkeypatch):
    from services import agent_tools
    from services import background_agent as BA
    from services.ai_service import ai_service

    async def _fake_get_task(task_id):
        return _done_task(task_id)

    seen = {}

    async def _fake_chat(messages, **kw):
        seen["messages"] = messages
        return "课稿《勾股定理》已经写好了，我们接着讲第二个模型。"

    monkeypatch.setattr(BA, "get_task", _fake_get_task)
    monkeypatch.setattr(agent_tools.ai_service, "deepseek_chat", _fake_chat)

    ctx, session, saved = _mk_ctx("[系统召回] 后台任务已完成，task_id=t_r23_0001",
                                  {"task_id": "t_r23_0001"})
    out = asyncio.run(agent_tools.h_task_recall(ctx))

    assert out["action"]["type"] == "task_recall"
    assert "勾股定理" in out["reply"] or "接着讲" in out["reply"]
    assert saved["n"] == 1, "回复没有落盘 → 用户刷新后看不到召回结果"
    # 产出摘要必须真的进了模型上下文（否则「召回」只是空喊一句）
    assert any("勾股定理" in str(m.get("content", "")) for m in seen["messages"])


def test_h_task_recall_refuses_when_task_not_done(monkeypatch):
    from services import agent_tools
    from services import background_agent as BA

    async def _fake_get_task(task_id):
        t = _done_task(task_id)
        t["status"] = "running"
        return t

    called = {"chat": 0}

    async def _fake_chat(messages, **kw):
        called["chat"] += 1
        return "不该被调用"

    monkeypatch.setattr(BA, "get_task", _fake_get_task)
    monkeypatch.setattr(agent_tools.ai_service, "deepseek_chat", _fake_chat)

    ctx, _session, _saved = _mk_ctx("[系统召回] task_id=t_r23_0002", {"task_id": "t_r23_0002"})
    out = asyncio.run(agent_tools.h_task_recall(ctx))
    assert called["chat"] == 0, "任务没完成就调用模型 → 会讲出并不存在的结果"
    assert "running" in out["reply"] or "状态" in out["reply"]


def test_h_task_recall_handles_missing_task(monkeypatch):
    from services import agent_tools
    from services import background_agent as BA

    async def _boom(task_id):
        raise BA.TaskNotFound(task_id)

    monkeypatch.setattr(BA, "get_task", _boom)
    ctx, _session, saved = _mk_ctx("[系统召回] task_id=t_r23_0003", {"task_id": "t_r23_0003"})
    out = asyncio.run(agent_tools.h_task_recall(ctx))
    assert "不存在" in out["reply"] or "清理" in out["reply"]
    assert saved["n"] == 1


# ==========================================================================
# 4. 召回：前端自动召回 + 只召回一次 + 跨会话不误召
# ==========================================================================

def test_agent_html_auto_recalls_exactly_once():
    html = _read("templates/agent.html")
    assert "_agentRecalled" in html, "缺少去重表 → 轮询每次都会再召回一次"
    assert "maybeRecallTask(s);" in html, "任务卡片终态没有调用召回"
    assert "if(_agentRecalled[s.task_id]) return;" in html
    assert "s.sid !== _currentId" in html, "跨会话误召：别的会话的任务会打断当前对话"
    assert "c.appendChild(chip)" in html, "召回过程没有可视反馈"


def test_agent_html_recall_does_not_render_user_bubble():
    """召回是系统行为，不该伪装成用户发言。"""
    html = _read("templates/agent.html")
    assert "opts.recall" in html
    assert "{recall:true}" in html


# ==========================================================================
# 5. 讲课页边看边问
# ==========================================================================

def test_lecture_page_has_ask_box():
    html = _read("templates/lecture.html")
    assert 'id="lecAskInput"' in html
    assert "LectureApp.ask()" in html
    assert 'id="lecAskArea"' in html


def test_lecture_js_ask_carries_step_context():
    js = _read("static/js/lecture.js")
    assert "function askAboutStep()" in js
    assert "ask: askAboutStep" in js, "没有暴露到 LectureApp.ask"
    assert "currentStep()" in js.split("function askAboutStep()")[1].split("window.LectureApp")[0], \
        "提问没有带上当前步骤上下文"
    assert "/api/chat" in js


# ==========================================================================
# 6. 课稿离线缓存
# ==========================================================================

def test_offline_layer_caches_lessons():
    js = _read("static/js/offline.js")
    m = re.search(r"var GET_CACHE_PREFIXES = \[(.*?)\];", js, re.S)
    assert m, "找不到 GET_CACHE_PREFIXES"
    assert "'/api/lessons'" in m.group(1), (
        "课稿不在离线 GET 白名单 → 提前备好的课离线看不到（本次要修的就是这个问题）")


# ==========================================================================
# 7. 鉴权韧性
# ==========================================================================

def test_app_js_auth_token_from_local_storage():
    js = _read("static/js/app.js")
    assert "la_auth_token" in js
    assert "localStorage.getItem('la_auth_token')" in js


def test_app_js_401_reauth_retries_once():
    js = _read("static/js/app.js")
    assert "authErr.needAuth=true" in js or "needAuth = true" in js.replace(" ", "")
    assert "_reauthPromise" in js, "并发 401 会弹一堆密码框"
    assert "_retried" in js, "没有重试上限 → 密码错会无限弹窗"
    assert "window.$API._request(u,opts,type,true)" in js


# ==========================================================================
# 8. 安卓端
# ==========================================================================

def test_android_tts_chunks_long_text():
    java = _read(os.path.join("..", "android", "app", "src", "main", "java",
                              "com", "learningagent", "app", "MainActivity.java"))
    assert "TTS_CHUNK_LIMIT" in java
    assert "splitForTts" in java
    assert "getMaxSpeechInputLength" in java or "4000" in java, "分片依据没写清"
    # 4000 是 TextToSpeech 的硬上限，常量必须小于它
    m = re.search(r"TTS_CHUNK_LIMIT = (\d+)", java)
    assert m and int(m.group(1)) < 4000


def test_android_tts_queues_before_ready_and_keeps_screen_on():
    java = _read(os.path.join("..", "android", "app", "src", "main", "java",
                              "com", "learningagent", "app", "MainActivity.java"))
    assert "pendingTtsChunks" in java
    assert "flushPendingTts" in java
    assert "FLAG_KEEP_SCREEN_ON" in java


def test_android_version_bumped_for_rebuild():
    g = _read(os.path.join("..", "android", "app", "build.gradle.kts"))
    m = re.search(r"versionCode = (\d+)", g)
    assert m and int(m.group(1)) >= 6, "改动后必须提升 versionCode，否则装了新版仍是旧包"


# ==========================================================================
# 9. SVG 质量自检接线（不再是死代码）
# ==========================================================================

def test_write_svg_records_quality_issues():
    from services.diagram_service import diagram_service
    folder = tempfile.mkdtemp(prefix="r23_svg_")
    path = os.path.join(folder, "diagram_0.svg")
    bad = '<svg xmlns="http://www.w3.org/2000/svg"><text x="10" y="10">只有文字</text></svg>'
    asyncio.run(diagram_service._write_svg(path, bad))
    assert os.path.exists(path)
    issues = diagram_service._quality_issues.get(path)
    assert issues, "落盘后没有跑质量自检（_validate_svg_quality 又变回死代码了）"
    assert any("viewBox" in i for i in issues)


def test_write_svg_does_not_reject_good_svg():
    """只观测不拒收：合法图必须照常落盘。"""
    from services.diagram_service import diagram_service
    folder = tempfile.mkdtemp(prefix="r23_svg_ok_")
    path = os.path.join(folder, "diagram_1.svg")
    good = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<line x1="0" y1="0" x2="50" y2="50" style="stroke:#333;stroke-width:2"/>'
            '<circle cx="20" cy="20" r="5" style="fill:none;stroke:#333"/></svg>')
    asyncio.run(diagram_service._write_svg(path, good))
    assert os.path.exists(path)
    issues = diagram_service._quality_issues.get(path) or []
    assert not any("viewBox" in i for i in issues)
    assert not any("无绘图元素" in i for i in issues)


def test_validate_diagram_reports_missing_file():
    from services.diagram_service import diagram_service
    assert diagram_service.validate_diagram("r23_no_such_qid", 0) == ["SVG文件不存在"]


def test_experiment_svg_not_judged_by_math_palette():
    """彩色实验图不能被数学图的黑白灰判据误报（这是我们不拿它当门禁的原因）。"""
    from services.diagram_service import _is_experiment_svg
    cream = '<svg><rect fill="#f5f0e8"/><circle fill="#3a7bd5"/></svg>'
    assert _is_experiment_svg(cream) is True
    assert _is_experiment_svg(cream, {"components": [{"type": "beaker"}]}) is True
    assert _is_experiment_svg('<svg><line stroke="#333"/></svg>') is False


def test_diagram_check_endpoint_exposes_quality():
    src = _read("routers/diagram.py")
    assert "quality_issues" in src and "quality_ok" in src

# ==========================================================================
# 10. 题目 → 笔记（R23：管线缺的最后一段）
# ==========================================================================

def test_question_list_has_note_entry():
    js = _read("static/js/questions.js")
    assert "saveNoteFromQuestion" in js, "题库没有「存笔记」实现"
    # 桌面表格行 + 移动卡片 两处渲染都要有按钮（三端一致：手机与电脑都能用）
    assert js.count('btn-note"') >= 2, "「存笔记」按钮没有同时接在桌面行与移动卡片上"
    assert js.count("querySelectorAll('.btn-note')") >= 1, "按钮没有事件接线"


def test_note_creation_links_back_to_question():
    js = _read("static/js/questions.js")
    assert "question_ids:[qid]" in js, "笔记没有回链到题目（question_ids 为空）"
    assert "[[QUESTION:" in js, "笔记正文缺少题目引用标记，笔记页不会渲染成题目卡"
    assert "'/api/notes'" in js
    assert "q.status==='done'" in js, "未完成的题不应允许存笔记（题干可能是半截）"


def test_notes_page_supports_deep_link():
    """存笔记后的「去笔记页」跳转必须真的能打开那一篇，否则是空诺。"""
    html = _read("templates/notes.html")
    assert "new URLSearchParams(location.search).get('note')" in html
    assert "openNote(want)" in html
    js = _read("static/js/questions.js")
    assert "/notes?note=" in js, "跳转地址与笔记页深链参数不一致"


def test_note_save_is_deterministic_not_ai():
    """确定性搬运：一键存笔记不该消耗模型额度（AI 整理是笔记页自己的事）。"""
    js = _read("static/js/questions.js")
    seg = js.split("function saveNoteFromQuestion")[1].split("function retryQ")[0]
    assert "ai-generate" not in seg and "auto-organize" not in seg
