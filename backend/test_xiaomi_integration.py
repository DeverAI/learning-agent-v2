"""小米 MiMo 集成回归：TTS 服务与端点、视觉回退链、配置键优先级、模块开关。

全部依赖 mock，不发起真实外部请求；真实冒烟由人工验收执行。
隔离策略（2026-08-27 复查修正）：
- 不再在导入期改写 config.STORAGE_DIR/SETTINGS_FILE——pytest 先收集后执行，
  晚导入模块若自行重定向会与首个导入方分叉（settings 与 SQLite 各指一处）。
  本模块复用套件中先导入方建立的环境，与既有晚导入测试一致。
- 通过 autouse fixture 把 logger.ERR_LOG_PATH 重定向到临时目录，
  避免错误注入用例把模拟故障写进真实 backend/Err.log。
- 单例 ai_service 的 xm_key 由 _svc(monkeypatch) 显式给定，不读真实配置。
"""
import base64
import os
import sys
import tempfile
import pytest
from fastapi.testclient import TestClient

import config

# 套件中字母序更早的测试文件已导入 main 并完成临时目录重定向（首导入方绑定），
# 此时不得再次改写；仅当本文件是本次运行的唯一/首个导入方（如单文件运行）时
# 才做包含 DATABASE_URL 的完整重定向，保证任何运行组合都不触碰真实 storage。
if "main" not in sys.modules:
    _single_tmp = tempfile.mkdtemp(prefix="dsh_xiaomi_single_")
    config.STORAGE_DIR = os.path.join(_single_tmp, "storage")
    config.SETTINGS_FILE = os.path.join(_single_tmp, "settings.json")
    config.GALLERY_DIR = os.path.join(_single_tmp, "gallery")
    config.DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(config.STORAGE_DIR, 'app.db')}"
    os.makedirs(config.STORAGE_DIR, exist_ok=True)

from main import app

_AUTH = {"x-auth-token": "Ntmhzsgtc"}


@pytest.fixture(autouse=True)
def _isolate_err_log(tmp_path, monkeypatch):
    """本模块所有用例的 Err.log 写入都进临时目录。"""
    import logger as _logger
    monkeypatch.setattr(_logger, "ERR_LOG_PATH", str(tmp_path / "Err.log"))


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ===================== TTS 服务层 =====================

class TestXiaomiTTSService:
    def _svc(self, monkeypatch=None):
        from services.ai_service import ai_service
        if monkeypatch is not None:
            # 测试进程的 ai_service 单例在导入时可能读到无 Key 的临时配置，
            # 显式给定 Key，不依赖真实 settings.json
            monkeypatch.setattr(ai_service, "xm_key", "tp-test")
        return ai_service

    def test_tts_success_decodes_base64(self, monkeypatch):
        svc = self._svc(monkeypatch)
        raw = b"ID3 fake mp3 bytes"
        payload_b64 = base64.b64encode(raw).decode()

        async def fake_call_raw(url, key, p, timeout=None, _is_fallback=False, **kwargs):
            assert "chat/completions" in url
            assert key == "tp-test"
            assert p["model"] == svc.xm_tts_model
            assert p["messages"][0]["role"] == "assistant"
            assert p["messages"][0]["content"] == "你好"
            return {"audio": {"data": payload_b64}}

        monkeypatch.setattr(svc, "_call_raw", fake_call_raw)
        got_bytes, mime = _run(svc.xiaomi_tts("你好", "mimo_default"))
        assert got_bytes == raw
        assert mime == "audio/mpeg"

    def test_tts_no_audio_raises(self, monkeypatch):
        """回退文本模型不会返回 audio；空数据必须报错而不是静默成功。"""
        svc = self._svc(monkeypatch)

        async def fake_call_raw(url, key, p, timeout=None, _is_fallback=False, **kwargs):
            return {"content": "这不是音频"}

        monkeypatch.setattr(svc, "_call_raw", fake_call_raw)
        with pytest.raises(Exception, match="no audio data"):
            _run(svc.xiaomi_tts("测试"))

    def test_tts_bad_base64_raises(self, monkeypatch):
        svc = self._svc(monkeypatch)

        async def fake_call_raw(url, key, p, timeout=None, _is_fallback=False, **kwargs):
            return {"audio": {"data": "!!!not-base64!!!"}}

        monkeypatch.setattr(svc, "_call_raw", fake_call_raw)
        with pytest.raises(Exception, match="decode failed"):
            _run(svc.xiaomi_tts("测试"))

    def test_tts_empty_text_rejected(self, monkeypatch):
        svc = self._svc(monkeypatch)
        with pytest.raises(ValueError):
            _run(svc.xiaomi_tts("   "))

    def test_tts_no_key_raises(self, monkeypatch):
        svc = self._svc()
        monkeypatch.setattr(svc, "xm_key", "")
        with pytest.raises(Exception, match="not configured"):
            _run(svc.xiaomi_tts("测试"))

    def test_tts_format_normalization(self, monkeypatch):
        """pcm16 归一为 pcm；非法格式回退 mp3。"""
        svc = self._svc(monkeypatch)
        seen_formats = []

        async def fake_call_raw(url, key, p, timeout=None, _is_fallback=False, **kwargs):
            seen_formats.append(p["audio"]["format"])
            return {"audio": {"data": base64.b64encode(b"x").decode()}}

        monkeypatch.setattr(svc, "_call_raw", fake_call_raw)
        b1, m1 = _run(svc.xiaomi_tts("a", "", "pcm16"))
        assert m1 == "audio/L16"
        b2, m2 = _run(svc.xiaomi_tts("b", "", "ogg"))
        assert m2 == "audio/mpeg"
        assert seen_formats[0] == "pcm"


# ===================== /api/tts 端点 =====================

class TestTTSApi:
    def test_tts_endpoint_success(self, client, monkeypatch):
        import routers.audio as audio_mod
        from services.ai_service import ai_service
        monkeypatch.setattr(audio_mod, "ENABLE_XIAOMI_TTS", True)
        monkeypatch.setattr(audio_mod, "load_settings",
                            lambda: {"xiaomi_token_plan_api_key": "tp-x"})

        async def fake_xiaomi_tts(text, voice="", audio_format="mp3"):
            assert text.strip() == "你好世界"
            return b"RIFFxxxx", "audio/wav"

        monkeypatch.setattr(ai_service, "xiaomi_tts", fake_xiaomi_tts)
        resp = client.post("/api/tts", json={"text": "你好世界"}, headers=_AUTH)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("audio/")
        assert resp.headers["cache-control"] == "no-store"
        assert resp.content == b"RIFFxxxx"

    def test_tts_endpoint_no_key_503(self, client, monkeypatch):
        import routers.audio as audio_mod
        async def fail(*a, **k):  # 不应被调用
            raise AssertionError("xiaomi_tts should not be called")
        from services.ai_service import ai_service
        monkeypatch.setattr(audio_mod, "ENABLE_XIAOMI_TTS", True)
        monkeypatch.setattr(audio_mod, "load_settings",
                            lambda: {"xiaomi_token_plan_api_key": ""})
        monkeypatch.setattr(ai_service, "xiaomi_tts", fail)
        resp = client.post("/api/tts", json={"text": "hi"}, headers=_AUTH)
        assert resp.status_code == 503

    def test_tts_endpoint_disabled_503(self, client, monkeypatch):
        import routers.audio as audio_mod
        monkeypatch.setattr(audio_mod, "ENABLE_XIAOMI_TTS", False)
        resp = client.post("/api/tts", json={"text": "hi"}, headers=_AUTH)
        assert resp.status_code == 503

    def test_tts_endpoint_blank_text_422(self, client, monkeypatch):
        import routers.audio as audio_mod
        monkeypatch.setattr(audio_mod, "ENABLE_XIAOMI_TTS", True)
        monkeypatch.setattr(audio_mod, "load_settings",
                            lambda: {"xiaomi_token_plan_api_key": "tp-x"})
        resp = client.post("/api/tts", json={"text": ""}, headers=_AUTH)
        assert resp.status_code == 422

    def test_tts_endpoint_upstream_error_502(self, client, monkeypatch):
        import routers.audio as audio_mod
        from services.ai_service import ai_service
        monkeypatch.setattr(audio_mod, "ENABLE_XIAOMI_TTS", True)
        monkeypatch.setattr(audio_mod, "load_settings",
                            lambda: {"xiaomi_token_plan_api_key": "tp-x"})

        async def boom(text, voice="", audio_format="mp3"):
            raise Exception("AI API 500: upstream down")

        monkeypatch.setattr(ai_service, "xiaomi_tts", boom)
        resp = client.post("/api/tts", json={"text": "hi"}, headers=_AUTH)
        assert resp.status_code == 502

    def test_tts_requires_auth(self, client, monkeypatch):
        # 鉴权契约测试：显式启用密码（临时 settings 无 api_password 时
        # 中间件按"未配置=放行"处理——2026-09-10 语义恢复后的正确写法）
        import main as _main
        monkeypatch.setattr(_main, "_get_auth_password", lambda: "Ntmhzsgtc")
        resp = client.post("/api/tts", json={"text": "hi"})
        assert resp.status_code == 401


# ===================== 视觉回退链 =====================

class TestVisionFallbackChain:
    def test_xiaomivision_returns_dict_when_parse_json(self, monkeypatch):
        from services.ai_service import ai_service

        async def fake_chat(messages, temperature=0.3, max_tokens=8192):
            assert messages[0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
            return '{"ok": true}'

        monkeypatch.setattr(ai_service, "xiaomi_chat", fake_chat)
        result = _run(ai_service.xiaomi_vision("abc123", "描述图片"))
        assert result == {"ok": True}

    def test_vision_chain_prefers_mimo_then_zhipu_then_kimi(self, monkeypatch):
        import services.ai_service as m
        """视觉链顺序（Fact.md 2026-09-06 定规）：MiMo 全模态优先，
        ZhipuAI 视觉第一回退，Kimi 仅作最后文本兜底。"""
        calls = []
        svc = m.ai_service

        async def zpoor(image_base64, prompt, parse_json=True):
            calls.append("zhipu")
            raise Exception("zp down")

        async def xmok(image_base64, prompt, parse_json=False):
            calls.append("mimo")
            return "视觉分析结果"

        async def kimi(image_base64, prompt, parse_json=False):
            calls.append("kimi")
            return "不该到这里"

        monkeypatch.setattr(svc, "xm_key", "tp-x")
        monkeypatch.setattr(svc, "zhipuai_vision", zpoor)
        monkeypatch.setattr(svc, "xiaomi_vision", xmok)
        monkeypatch.setattr(svc, "kimi_vision", kimi)
        result = _run(m._call_vision_model_async("看图", "AAAA"))
        assert result == "视觉分析结果"
        assert calls == ["mimo"]

    def test_vision_chain_skips_mimo_without_key(self, monkeypatch):
        """无 xm_key 时跳过 MiMo 直落 ZhipuAI；ZhipuAI 也失败则返回 None
        （2026-09-09 契约：Kimi 无视觉输入能力，已移出视觉兜底链——FreqErr）。"""
        import services.ai_service as m
        calls = []
        svc = m.ai_service

        async def zpoor(image_base64, prompt, parse_json=True):
            calls.append("zhipu")
            raise Exception("zp down")

        async def kmok(image_base64, prompt, parse_json=False):
            calls.append("kimi")
            return "kimi 结果"

        monkeypatch.setattr(svc, "xm_key", "")
        monkeypatch.setattr(svc, "zhipuai_vision", zpoor)
        monkeypatch.setattr(svc, "kimi_vision", kmok)
        result = _run(m._call_vision_model_async("看图", "AAAA"))
        assert result is None
        assert calls == ["zhipu"]


# ===================== 配置与设置页 =====================

class TestSettingsKeys:
    def test_project_settings_priority_over_shared_api_txt(self, monkeypatch):
        """settings.json 显式 Key 必须优先于共享 api.txt（用户决策）。"""
        tmp_settings = os.path.join(tempfile.mkdtemp(), "settings.json")

        def write(d):
            import json as _json
            with open(tmp_settings, "w", encoding="utf-8") as f:
                _json.dump(d, f)

        # 场景1：显式 Key 存在 → 即使共享库有值也用显式值
        write({"deepseek_api_key": "sk-project-explicit"})
        monkeypatch.setattr(config, "SETTINGS_FILE", tmp_settings)
        monkeypatch.setattr(config, "get_api_key", lambda name, fallback="": "sk-shared-file")
        s = config.load_settings()
        assert s["deepseek_api_key"] == "sk-project-explicit"

        # 场景2：显式 Key 为空 → 回退共享库
        write({"deepseek_api_key": ""})
        s = config.load_settings()
        assert s["deepseek_api_key"] == "sk-shared-file"

        # 场景3：都没有 → 空
        monkeypatch.setattr(config, "get_api_key", lambda name, fallback="": "")
        s = config.load_settings()
        assert s["deepseek_api_key"] == ""

    def test_get_settings_exposes_xiaomi_fields(self, client, monkeypatch):
        import routers.settings as st_mod
        import routers.audio as audio_mod
        monkeypatch.setattr(st_mod, "load_settings", lambda: {
            "deepseek_api_key": "sk-x", "kimi_api_key": "", "zhipuai_api_key": "",
            "xiaomi_token_plan_api_key": "tp-y",
            "xiaomi_token_plan_base_url": "https://token-plan-cn.xiaomimimo.com/v1",
            "xiaomi_vision_model": "mimo-v2.5",
            "xiaomi_tts_voice": "冰糖",
            "focus_voice_engine": "auto",
        })
        monkeypatch.setattr(audio_mod, "ENABLE_XIAOMI_TTS", True)
        resp = client.get("/api/settings", headers=_AUTH)
        assert resp.status_code == 200
        d = resp.json()
        assert d["has_xiaomi"] is True
        assert d["tts_available"] is True
        assert d["focus_voice_engine"] == "auto"
        assert d["xiaomi_tts_voice"] == "冰糖"
        # 掩码不泄露完整 Key
        assert "tp-y" not in d["xiaomi_token_plan_api_key"]

    def test_update_settings_accepts_focus_engine(self, client, monkeypatch):
        from routers import settings as st_router
        saved = {}
        monkeypatch.setattr(st_router, "load_settings",
                            lambda: dict(config._default_settings))
        monkeypatch.setattr(st_router, "save_settings",
                            lambda d: saved.update(d))
        resp = client.put("/api/settings", json={"focus_voice_engine": "browser"},
                          headers=_AUTH)
        assert resp.status_code == 200
        assert saved.get("focus_voice_engine") == "browser"

    def test_update_settings_rejects_bad_engine(self, client):
        resp = client.put("/api/settings", json={"focus_voice_engine": "aliyun"},
                          headers=_AUTH)
        assert resp.status_code == 422

    def test_import_config_rejects_bad_engine_400(self, client, monkeypatch):
        """import_config 对 focus_voice_engine 手写校验返回 400（与 PUT 的 422 是
        两条独立通道，行为分别锁定）。"""
        from routers import settings as st_router
        monkeypatch.setattr(st_router, "load_settings",
                            lambda: dict(config._default_settings))
        monkeypatch.setattr(st_router, "save_settings", lambda d: None)
        resp = client.put("/api/settings/import",
                          json={"focus_voice_engine": "aliyun"}, headers=_AUTH)
        assert resp.status_code == 400

    def test_import_config_rejects_long_voice_400(self, client, monkeypatch):
        from routers import settings as st_router
        monkeypatch.setattr(st_router, "load_settings",
                            lambda: dict(config._default_settings))
        monkeypatch.setattr(st_router, "save_settings", lambda d: None)
        resp = client.put("/api/settings/import",
                          json={"xiaomi_tts_voice": "v" * 33}, headers=_AUTH)
        assert resp.status_code == 400

    def test_import_config_accepts_valid_voice(self, client, monkeypatch):
        from routers import settings as st_router
        saved = {}
        monkeypatch.setattr(st_router, "load_settings",
                            lambda: dict(config._default_settings))
        monkeypatch.setattr(st_router, "save_settings",
                            lambda d: saved.update(d))
        resp = client.put("/api/settings/import",
                          json={"focus_voice_engine": "browser",
                                "xiaomi_tts_voice": "冰糖"},
                          headers=_AUTH)
        assert resp.status_code == 200
        assert saved.get("focus_voice_engine") == "browser"
        assert saved.get("xiaomi_tts_voice") == "冰糖"


# ===================== 模块开关 =====================

class TestModuleSwitch:
    def test_switch_exists_and_default_on(self):
        assert config.ENABLE_XIAOMI_TTS is True

    def test_unavailable_reason_respects_switch(self, monkeypatch):
        import routers.audio as audio_mod
        monkeypatch.setattr(audio_mod, "ENABLE_XIAOMI_TTS", False)
        assert audio_mod.tts_unavailable_reason() != ""
        monkeypatch.setattr(audio_mod, "ENABLE_XIAOMI_TTS", True)
        monkeypatch.setattr(audio_mod, "load_settings",
                            lambda: {"xiaomi_token_plan_api_key": "tp-ok"})
        assert audio_mod.tts_unavailable_reason() == ""


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
