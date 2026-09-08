"""专注模式（Focus Mode）端到端冒烟测试"""
import os
import json
import re
import pytest
from fastapi.testclient import TestClient

# 测试前替换 config 为临时目录
import tempfile
tmp_dir = tempfile.mkdtemp()

import config
config.STORAGE_DIR = os.path.join(tmp_dir, "storage")
config.SETTINGS_FILE = os.path.join(tmp_dir, "settings.json")

# 确保临时目录存在
os.makedirs(config.STORAGE_DIR, exist_ok=True)

from main import app

_AUTH = {"x-auth-token": "Ntmhzsgtc"}


@pytest.fixture
def client():
    """创建测试客户端"""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def mock_ai(monkeypatch):
    """Mock AI 调用（黑板开启时讲解走 deepseek_json，必须一并 mock，否则打到真实外网）"""
    async def fake_deepseek_chat(messages, max_tokens=1024, scope=""):
        return "这是一段测试讲解内容。讲解了一个重要概念。\n\n对吧"

    async def fake_deepseek_json(messages, **kwargs):
        return {"content": "这是一段测试讲解内容。讲解了一个重要概念。\n\n对吧"}

    async def fake_zhipuai_vision(image_base64, prompt, parse_json=True):
        return {"expression": "focused", "confidence": 0.85, "overall_state": "engaged", "suggestion": "continue"}

    async def fake_kimi_vision(image_base64, prompt, parse_json=False):
        return ""

    from services import ai_service
    monkeypatch.setattr(ai_service.ai_service, "deepseek_chat", fake_deepseek_chat)
    monkeypatch.setattr(ai_service.ai_service, "deepseek_json", fake_deepseek_json)
    monkeypatch.setattr(ai_service.ai_service, "zhipuai_vision", fake_zhipuai_vision)
    monkeypatch.setattr(ai_service.ai_service, "kimi_vision", fake_kimi_vision)


class TestFocusStart:
    """测试启动专注模式"""

    def test_start_topic_mode(self, client, mock_ai):
        """主题模式启动"""
        resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "二次函数",
        }, headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "session" in data
        assert data["session"]["mode"] == "topic"
        assert data["session"]["topic"] == "二次函数"
        assert data["session"]["status"] == "teaching"
        assert len(data["session"]["segments"]) == 1

    def test_start_missing_topic(self, client, mock_ai):
        """主题模式缺少 topic 应返回 400"""
        resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "",
        }, headers=_AUTH)
        assert resp.status_code == 400

    def test_start_question_mode(self, client, mock_ai):
        """题目模式启动"""
        resp = client.post("/api/focus/start", json={
            "mode": "question",
            "question_id": "test_q123",
        }, headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["session"]["mode"] == "question"

    def test_start_invalid_mode(self, client, mock_ai):
        """无效模式应返回 422"""
        resp = client.post("/api/focus/start", json={
            "mode": "invalid",
            "topic": "test",
        }, headers=_AUTH)
        assert resp.status_code == 422

    def test_start_missing_question_id(self, client, mock_ai):
        """题目模式缺少 question_id 应返回 400"""
        resp = client.post("/api/focus/start", json={
            "mode": "question",
        }, headers=_AUTH)
        assert resp.status_code == 400


class TestFocusCheckpoint:
    """测试检查点提交"""

    def test_submit_checkpoint(self, client, mock_ai):
        """提交检查点"""
        start_resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "测试主题",
        }, headers=_AUTH)
        session_id = start_resp.json()["session"]["id"]

        resp = client.post(f"/api/focus/{session_id}/checkpoint", json={
            "voice_text": "我理解了",
            "voice_features": {"speed": "normal", "volume": "normal", "pause_count": 0},
        }, headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    def test_checkpoint_invalid_session(self, client, mock_ai):
        """无效会话 ID 应返回 400"""
        resp = client.post("/api/focus/invalid$id/checkpoint", json={
            "voice_text": "test",
        }, headers=_AUTH)
        assert resp.status_code == 400

    def test_checkpoint_no_session(self, client, mock_ai):
        """不存在的会话应返回 404 或 400"""
        resp = client.post("/api/focus/nonexist123456789/checkpoint", json={
            "voice_text": "test",
        }, headers=_AUTH)
        assert resp.status_code in (400, 404, 500)


class TestFocusLifecycle:
    """测试会话生命周期"""

    def test_pause_resume(self, client, mock_ai):
        """暂停和恢复"""
        start_resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "测试",
        }, headers=_AUTH)
        session_id = start_resp.json()["session"]["id"]

        resp = client.post(f"/api/focus/{session_id}/pause", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["session"]["status"] == "paused"

        resp = client.post(f"/api/focus/{session_id}/resume", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["session"]["status"] == "teaching"

    def test_end_session(self, client, mock_ai):
        """结束会话"""
        start_resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "测试",
        }, headers=_AUTH)
        session_id = start_resp.json()["session"]["id"]

        resp = client.post(f"/api/focus/{session_id}/end", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["session"]["status"] == "completed"

    def test_get_state(self, client, mock_ai):
        """获取状态"""
        start_resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "测试",
        }, headers=_AUTH)
        session_id = start_resp.json()["session"]["id"]

        resp = client.get(f"/api/focus/{session_id}/state", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["session"]["id"] == session_id

    def test_history(self, client, mock_ai):
        """获取历史"""
        client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "测试历史",
        }, headers=_AUTH)

        resp = client.get("/api/focus/history", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert isinstance(resp.json()["sessions"], list)


class TestHippocampus:
    """测试海马体记忆 API"""

    def test_get_memories_empty(self, client, mock_ai):
        """空记忆"""
        resp = client.get("/api/hippocampus/memories", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_get_meta(self, client, mock_ai):
        """获取元认知"""
        resp = client.get("/api/hippocampus/meta", headers=_AUTH)
        assert resp.status_code == 200
        assert "meta" in resp.json()

    def test_update_meta(self, client, mock_ai):
        """更新元认知"""
        resp = client.put("/api/hippocampus/meta", json={
            "preferred_style": "example_first",
        }, headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["meta"]["preferred_style"] == "example_first"

    def test_decay(self, client, mock_ai):
        """触发衰减"""
        resp = client.post("/api/hippocampus/decay", headers=_AUTH)
        assert resp.status_code == 200
        assert "faded" in resp.json()
        assert "forgotten" in resp.json()


class TestFocusDisabled:
    """测试模块开关关闭时的行为"""

    def test_focus_disabled(self, monkeypatch, client, mock_ai):
        """开关关闭时端点应返回 404"""
        import routers.focus as focus_mod
        monkeypatch.setattr(focus_mod, "ENABLE_FOCUS_MODE", False)
        resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "test",
        }, headers=_AUTH)
        assert resp.status_code == 404


class TestTeachingContent:
    """测试教学内容格式"""

    def test_checkpoint_marker(self, client, mock_ai):
        """每段讲解末尾应有「对吧」标记"""
        resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "测试",
        }, headers=_AUTH)
        assert resp.status_code == 200
        segments = resp.json()["session"]["segments"]
        assert len(segments) > 0
        content = segments[0]["content"]
        assert "对吧" in content

    def test_session_id_format(self, client, mock_ai):
        """会话 ID 格式正确"""
        resp = client.post("/api/focus/start", json={
            "mode": "topic",
            "topic": "测试",
        }, headers=_AUTH)
        assert resp.status_code == 200
        session_id = resp.json()["session"]["id"]
        assert len(session_id) > 0
        assert re.match(r"^[a-zA-Z0-9_-]+$", session_id)

    def test_invalid_session_id_rejected(self, client, mock_ai):
        """非法会话 ID 格式应返回 400"""
        resp = client.get("/api/focus/bad id!/state", headers=_AUTH)
        assert resp.status_code == 400

    def test_history_limit_bounded(self, client, mock_ai):
        """limit 参数应有上下界"""
        resp = client.get("/api/focus/history?limit=999999", headers=_AUTH)
        # 不应 500
        assert resp.status_code in (200, 422)


# ========== 黑板板书测试（2026-09-06） ==========

import asyncio
from services import focus_service as _fs


@pytest.fixture
def mock_ai_board(monkeypatch):
    """Mock AI：讲解 JSON 携带板书指令（text + function）"""
    async def fake_deepseek_json(messages, **kwargs):
        return {
            "content": "我们来看这个函数的图像，注意开口方向。\n\n对吧",
            "board": {"ops": [
                {"op": "write", "kind": "text", "content": "y = x**2 的图像"},
                {"op": "write", "kind": "function", "spec": {"expr": "x**2", "x_min": -5, "x_max": 5, "title": "二次函数"}},
                {"op": "snapshot", "label": "函数图像"},
            ]},
        }

    async def fake_deepseek_chat(messages, **kwargs):
        return "讲解。\n\n对吧"

    from services import ai_service
    monkeypatch.setattr(ai_service.ai_service, "deepseek_json", fake_deepseek_json)
    monkeypatch.setattr(ai_service.ai_service, "deepseek_chat", fake_deepseek_chat)
    monkeypatch.setattr(ai_service.ai_service, "zhipuai_vision", fake_deepseek_chat)
    monkeypatch.setattr(ai_service.ai_service, "kimi_vision", fake_deepseek_chat)


class TestFocusBlackboard:
    """黑板板书"""

    def test_start_includes_board(self, client, mock_ai_board):
        resp = client.post("/api/focus/start", json={"mode": "topic", "topic": "二次函数"}, headers=_AUTH)
        assert resp.status_code == 200
        session = resp.json()["session"]
        assert "board" in session
        assert session["board"]["pages"], "至少一页"
        entries = session["board"]["pages"][0]["entries"]
        kinds = [e["kind"] for e in entries]
        assert "text" in kinds and "svg" in kinds
        # 函数图真实渲染并落盘
        svg_entry = [e for e in entries if e["kind"] == "svg"][0]
        assert re.match(r"^board_\d+\.svg$", svg_entry["asset"])
        assert session["board"]["snapshots"][0]["label"] == "函数图像"
        # segment 带执行摘要
        assert session["segments"][0]["board_summary"]["svg_ok"] == 1

    def test_board_asset_roundtrip(self, client, mock_ai_board):
        resp = client.post("/api/focus/start", json={"mode": "topic", "topic": "画图"}, headers=_AUTH)
        sid = resp.json()["session"]["id"]
        entries = resp.json()["session"]["board"]["pages"][0]["entries"]
        asset = [e for e in entries if e["kind"] == "svg"][0]["asset"]
        got = client.get(f"/api/focus/{sid}/board/asset/{asset}", headers=_AUTH)
        assert got.status_code == 200
        assert "<svg" in got.text

    def test_board_asset_whitelist(self, client):
        for bad in ("evil.svg", "board_1.png", "reference.svg", "..%5Csettings.json", "board_x.svg"):
            got = client.get(f"/api/focus/somesid123/board/asset/{bad}", headers=_AUTH)
            assert got.status_code in (400, 404), bad

    def test_board_asset_missing_404(self, client):
        got = client.get("/api/focus/somesid123/board/asset/board_999.svg", headers=_AUTH)
        assert got.status_code == 404

    def test_board_snapshot_api(self, client, mock_ai_board):
        resp = client.post("/api/focus/start", json={"mode": "topic", "topic": "快照"}, headers=_AUTH)
        sid = resp.json()["session"]["id"]
        before = len(resp.json()["session"]["board"]["snapshots"])
        snap = client.post(f"/api/focus/{sid}/board/snapshot", json={"label": "我的快照"}, headers=_AUTH)
        assert snap.status_code == 200
        assert snap.json()["snapshot"]["label"] == "我的快照"
        assert snap.json()["snapshot"]["by"] == "student"
        state = client.get(f"/api/focus/{sid}/state", headers=_AUTH)
        assert len(state.json()["session"]["board"]["snapshots"]) == before + 1

    def test_board_snapshot_empty_board_400(self, client, mock_ai_board):
        """无板书页时保存快照返回 400：先构造无 board 的会话"""
        resp = client.post("/api/focus/start", json={"mode": "topic", "topic": "空板"}, headers=_AUTH)
        sid = resp.json()["session"]["id"]
        # 手工移除 board 模拟无板书
        sess = _fs._load_focus(sid)
        sess.pop("board", None)
        _fs._save_focus(sid, sess)
        snap = client.post(f"/api/focus/{sid}/board/snapshot", json={}, headers=_AUTH)
        assert snap.status_code == 400


class TestBoardOpsUnit:
    """板书指令执行器单元测试"""

    def _session(self):
        return {"id": "unittestsid1", "board": _fs._board_new()}

    def test_write_erase_clear(self):
        s = self._session()
        summary = asyncio.run(_fs._apply_board_ops(s, [
            {"op": "write", "kind": "text", "content": "第一行"},
            {"op": "write", "kind": "text", "content": "第二行"},
            {"op": "erase", "last": 1},
        ]))
        assert summary["written"] == 2 and summary["erased"] == 1
        assert summary["page_entries"] == 1
        # entry 序号擦除
        summary2 = asyncio.run(_fs._apply_board_ops(s, [{"op": "erase", "entry": 0}]))
        assert summary2["erased"] == 1 and summary2["page_entries"] == 0

    def test_clear_newpage_snapshot(self):
        s = self._session()
        summary = asyncio.run(_fs._apply_board_ops(s, [
            {"op": "write", "kind": "text", "content": "内容"},
            {"op": "newpage"},
            {"op": "write", "kind": "text", "content": "新页内容"},
            {"op": "snapshot", "label": "推导完成"},
        ]))
        assert summary["newpage"] == 1 and summary["snapshot"] == 1
        assert summary["page"] == 2 and summary["page_entries"] == 1
        board = s["board"]
        assert board["snapshots"][0]["page"] == 2

    def test_page_overflow_autonewpage(self):
        s = self._session()
        # 每段 ops 上限 8：两段累积 16 条，12 条满页后剩余自动滚入新页
        ops1 = [{"op": "write", "kind": "text", "content": f"条目{i}"} for i in range(8)]
        asyncio.run(_fs._apply_board_ops(s, ops1))
        ops2 = [{"op": "write", "kind": "text", "content": f"条目{i + 8}"} for i in range(8)]
        summary = asyncio.run(_fs._apply_board_ops(s, ops2))
        assert summary["written"] == 8
        assert summary["page"] == 2
        assert len(s["board"]["pages"][0]["entries"]) == 12
        assert len(s["board"]["pages"][1]["entries"]) == 4

    def test_ops_limit_and_invalid(self):
        s = self._session()
        # 非法条目放前 8 条内才会被执行（ops 列表按 _BOARD_OPS_MAX 截断）
        ops = [{"op": "unknown_op"}, {"op": "write", "kind": "weird"}, "not-a-dict"]
        ops += [{"op": "write", "kind": "text", "content": f"c{i}"} for i in range(7)]
        summary = asyncio.run(_fs._apply_board_ops(s, ops))
        assert summary["written"] == 5   # 前 8 条里有效 write 只有 5 条
        assert summary["skipped"] == 3   # 未知 op / 未知 write 种类 / 非法条目

    def test_text_truncated_to_limit(self):
        s = self._session()
        long = "x" * 2000
        asyncio.run(_fs._apply_board_ops(s, [{"op": "write", "kind": "text", "content": long}]))
        assert len(s["board"]["pages"][0]["entries"][0]["content"]) == _fs._BOARD_TEXT_MAX_LEN

    def test_function_render_unit(self):
        svg = asyncio.run(_fs._render_board_function({"expr": "x**2", "x_min": -5, "x_max": 5}))
        assert "<svg" in svg
        # ^ 幂友好转换
        svg2 = asyncio.run(_fs._render_board_function({"expr": "x^2", "x_min": -5, "x_max": 5}))
        assert "<svg" in svg2

    def test_function_render_invalid(self):
        with pytest.raises(ValueError):
            asyncio.run(_fs._render_board_function({"expr": "", "x_min": -5, "x_max": 5}))
        with pytest.raises(ValueError):
            asyncio.run(_fs._render_board_function({"expr": "import os", "x_min": -5, "x_max": 5}))
        with pytest.raises(ValueError):
            asyncio.run(_fs._render_board_function({"expr": "x**2", "x_min": 5, "x_max": -5}))


class TestBoardDisabled:
    """开关关闭时黑板完全隐身"""

    @pytest.fixture
    def disable_board(self, monkeypatch):
        import config as _cfg
        import routers.focus as _rf
        monkeypatch.setattr(_cfg, "ENABLE_FOCUS_BLACKBOARD", False)
        monkeypatch.setattr(_fs, "ENABLE_FOCUS_BLACKBOARD", False)
        monkeypatch.setattr(_rf, "ENABLE_FOCUS_BLACKBOARD", False)

    def test_start_no_board_field(self, client, mock_ai, disable_board):
        resp = client.post("/api/focus/start", json={"mode": "topic", "topic": "无板书"}, headers=_AUTH)
        assert resp.status_code == 200
        assert "board" not in resp.json()["session"]

    def test_board_endpoints_404_when_disabled(self, client, disable_board):
        snap = client.post("/api/focus/somesid123/board/snapshot", json={}, headers=_AUTH)
        assert snap.status_code == 404
        asset = client.get("/api/focus/somesid123/board/asset/board_1.svg", headers=_AUTH)
        assert asset.status_code == 404
