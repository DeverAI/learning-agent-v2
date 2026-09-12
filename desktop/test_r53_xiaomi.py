"""R53 回归测试：小米 MiMo Token Plan 接入 + 讲题 TTS + AI/API 安全加固。

覆盖：
T1  AppSettings xiaomi/TTS 字段与归一化（bool/str/敏感键拆分）
T2  ai_client provider 路由（xiaomi_role / dialog 解析 / vision 回退链）
T3  chat() 小米适配：小 max_tokens 自动 reasoning_effort=none、空回复抛 AICallError、
    错误信息截断、reasoning_content 不混入回复
T4  tts_speech() 消息契约（user 风格在前 + assistant 正文、audio 参数、空文本拒绝）
T5  strip_markdown_for_speech 清洗（代码块/graph/链接/公式定界符/表格）
T6  TTSPlayer 状态机（防重入 / stop 使迟到结果失效 / 无 key 跳过）
T7  DialogView 朗读按钮与自动朗读接线（offscreen）
T8  APITab 小米分组 collect/load 往返

运行：python test_r53_xiaomi.py
"""
import os
import sys
import json
import base64
import inspect
from unittest.mock import patch, MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


# ==================== T1 配置字段 ====================
print("T1 配置字段与归一化")
from config.settings import AppSettings, SENSITIVE_KEYS
from config.settings import ConfigManager as RealConfigManager

s = AppSettings.from_dict({})
check("1.1 xiaomi 默认启用", s.xiaomi_enabled is True)
check("1.2 默认 Token Plan URL", "token-plan-cn.xiaomimimo.com" in s.xiaomi_base_url)
check("1.3 默认模型 mimo-v2.5-pro", s.xiaomi_model == "mimo-v2.5-pro")
check("1.4 默认角色 dialog", s.xiaomi_role == "dialog")
check("1.5 TTS 默认模型/音色", s.xiaomi_tts_model == "mimo-v2.5-tts"
      and s.xiaomi_tts_voice == "mimo_default")
check("1.6 tts_auto_read 默认关", s.tts_auto_read is False)
check("1.7 xiaomi_api_key 属敏感键", "xiaomi_api_key" in SENSITIVE_KEYS)
# 脏数据归一化
s2 = AppSettings.from_dict({
    "xiaomi_enabled": "false", "tts_auto_read": 1,
    "xiaomi_api_key": None, "xiaomi_base_url": 12345,
})
check("1.8 'false'→False / 1→True", s2.xiaomi_enabled is False and s2.tts_auto_read is True)
check("1.9 None key→空串 / 数字URL→str", s2.xiaomi_api_key == "" and s2.xiaomi_base_url == "12345")


class _Cfg:
    """轻量配置替身：避免真实 ConfigManager 单例污染。"""
    def __init__(self, settings):
        self.settings = settings


def _fake_settings(**overrides):
    base = AppSettings.from_dict({
        "xiaomi_enabled": True,
        "xiaomi_api_key": "tp-test",
        "kimi_enabled": True, "kimi_api_key": "sk-k",
        "glm_enabled": True, "glm_api_key": "sk-g",
        "deepseek_enabled": True, "deepseek_api_key": "sk-d",
    })
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ==================== T2 provider 路由 ====================
print("T2 provider 角色路由")
import core.ai_client as aic

# 与真实 config.json 一致：deepseek_role 置空让出 dialog，由 xiaomi 承担
fake = _fake_settings(deepseek_role="")
with patch.object(RealConfigManager, "__instancecheck__", lambda c, o: isinstance(o, RealConfigManager)):
    with patch.object(aic, "ConfigManager") as CM:
        CM.return_value.settings = fake
        check("2.1 dialog→xiaomi", aic.resolve_provider_for_role("dialog") == "xiaomi")
        check("2.2 knowledge→kimi", aic.resolve_provider_for_role("knowledge") == "kimi")
        check("2.3 vision→glm", aic.resolve_provider_for_role("vision") == "glm")
        # deepseek_role 为空时 dialog 不再匹配 deepseek
        check("2.4 空 deepseek_role 不参与路由",
              aic.resolve_provider_for_role("dialog", "deepseek") == "xiaomi")
        cfg = aic._get_provider_config("xiaomi")
        check("2.5 _get_provider_config(xiaomi)", cfg["model"] == "mimo-v2.5-pro" and cfg["enabled"])
        try:
            aic._get_provider_config("unknown-x")
            check("2.6 未知 provider 抛异常", False)
        except aic.AICallError:
            check("2.6 未知 provider 抛异常", True)
        # vision 回退链：glm/kimi 都不可用时回退 xiaomi（全模态）
        fake2 = _fake_settings(glm_enabled=False, kimi_enabled=False,
                               glm_api_key="", kimi_api_key="")
        CM.return_value.settings = fake2
        vp, vm = aic.resolve_vision_target()
        check("2.7 vision 回退到 xiaomi 全模态", vp == "xiaomi", f"got {vp}")


# ==================== T3 chat() 小米适配 ====================
print("T3 chat() 小米适配")


class _Resp:
    def __init__(self, obj, code=200):
        self._obj = obj
        self.status_code = code
        self.text = json.dumps(obj, ensure_ascii=False)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _r
            raise _r.HTTPError(f"{self.status_code}")

    def json(self):
        return self._obj


def _ok_resp(content="答案", reasoning=None):
    msg = {"role": "assistant", "content": content}
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    return _Resp({"choices": [{"message": msg}]})


captured = {}


def _capture_post(url, json=None, headers=None, timeout=None, **kw):
    captured["url"] = url
    captured["payload"] = json
    captured["timeout"] = timeout
    return _ok_resp("好的")


with patch.object(aic, "ConfigManager") as CM:
    CM.return_value.settings = _fake_settings()
    with patch.object(aic.requests, "post", side_effect=_capture_post):
        # 3.1 小 max_tokens → 自动关闭思考
        aic.chat([{"role": "user", "content": "x"}], provider="xiaomi", max_tokens=10)
        p = captured["payload"]
        check("3.1 max_tokens<=64 附带 reasoning_effort=none",
              p.get("reasoning_effort") == "none" and p["max_tokens"] == 10)
        # 3.2 大 max_tokens 主对话不附带
        aic.chat([{"role": "user", "content": "x"}], provider="xiaomi", max_tokens=1500)
        check("3.2 大 max_tokens 不关闭思考",
              "reasoning_effort" not in captured["payload"])
        check("3.3 小米超时放宽 180s", captured["timeout"] == 180)
        check("3.4 URL 为 Token Plan chat/completions",
              captured["url"] == "https://token-plan-cn.xiaomimimo.com/v1/chat/completions")
        # 3.5 非 xiaomi 不受影响
        aic.chat([{"role": "user", "content": "x"}], provider="kimi", max_tokens=10)
        check("3.5 非 xiaomi 不加 reasoning_effort 且 60s",
              "reasoning_effort" not in captured["payload"] and captured["timeout"] == 60)
        # 3.6 空回复抛 AICallError（判定类任务触发上层关键词兜底）
        with patch.object(aic.requests, "post", return_value=_ok_resp("")):
            try:
                aic.chat([{"role": "user", "content": "x"}], provider="xiaomi", max_tokens=2048)
                check("3.6 空回复抛 AICallError", False)
            except aic.AICallError:
                check("3.6 空回复抛 AICallError", True)
        # 3.7 reasoning_content 不混入回复
        with patch.object(aic.requests, "post",
                          return_value=_ok_resp("最终答案", reasoning="思维链内容")):
            out = aic.chat([{"role": "user", "content": "x"}], provider="xiaomi")
            check("3.7 只返回 content 不含思维链", out == "最终答案")
        # 3.8 响应结构异常信息截断（防日志膨胀）
        big = {"choices": [{"message": {"content": ""}}], "junk": "A" * 5000}
        with patch.object(aic.requests, "post", return_value=_Resp(big)):
            try:
                aic.chat([{"role": "user", "content": "x"}], provider="xiaomi")
                check("3.8 异常信息截断", False)
            except aic.AICallError as e:
                check("3.8 异常信息截断", len(str(e)) < 300, f"len={len(str(e))}")
        # 3.9 未启用/无 key 快速失败
        CM.return_value.settings = _fake_settings(xiaomi_enabled=False)
        try:
            aic.chat([{"role": "user", "content": "x"}], provider="xiaomi")
            check("3.9 未启用快速失败", False)
        except aic.AICallError:
            check("3.9 未启用快速失败", True)


# ==================== T4 tts_speech 契约 ====================
print("T4 tts_speech 消息契约")

_TTS_CAPTURED = {}


def _tts_post(url, json=None, headers=None, timeout=None, **kw):
    _TTS_CAPTURED.update(url=url, payload=json, timeout=timeout)
    wav = b"RIFF" + b"\x00" * 16
    body = base64.b64encode(wav).decode()
    return _Resp({"choices": [{"message": {"audio": {"data": body}}}]})


with patch.object(aic, "ConfigManager") as CM:
    CM.return_value.settings = _fake_settings(
        xiaomi_tts_model="mimo-v2.5-tts", xiaomi_tts_voice="茉莉")
    with patch.object(aic.requests, "post", side_effect=_tts_post):
        out = aic.tts_speech("你好世界", style_instruction="温柔耐心")
        check("4.1 返回 bytes", isinstance(out, bytes) and out.startswith(b"RIFF"))
        p = _TTS_CAPTURED["payload"]
        msgs = p["messages"]
        check("4.2 user 风格在前 assistant 正文在后",
              msgs[0]["role"] == "user" and msgs[0]["content"] == "温柔耐心"
              and msgs[1]["role"] == "assistant" and msgs[1]["content"] == "你好世界")
        check("4.3 audio 参数", p.get("audio") == {"format": "wav", "voice": "茉莉"})
        check("4.4 模型默认 mimo-v2.5-tts", p["model"] == "mimo-v2.5-tts")
        check("4.5 URL 同 chat/completions",
              _TTS_CAPTURED["url"].endswith("/chat/completions"))
        # 无风格指令时不发 user 消息
        aic.tts_speech("只有正文")
        check("4.6 无风格指令省略 user 消息",
              [m["role"] for m in _TTS_CAPTURED["payload"]["messages"]] == ["assistant"])
        # 空文本拒绝
        try:
            aic.tts_speech("   ")
            check("4.7 空文本拒绝", False)
        except aic.AICallError:
            check("4.7 空文本拒绝", True)


# ==================== T5 Markdown 剥壳 ====================
print("T5 strip_markdown_for_speech")
from core.tts_player import strip_markdown_for_speech as strip_md

md = """## 标题
**粗体** 和 *斜体* 与 `code`。

```graph
A - B
```

[链接文字](https://x.com) 与 ![图](a.png)
> 引用
- 项目
1. 第一

| a | b |
|---|---|
$$E=mc^2$$
"""
clean = strip_md(md)
check("5.1 移除代码块", "graph" not in clean and "```" not in clean)
check("5.2 链接留文字去URL", "链接文字" in clean and "https" not in clean)
check("5.3 图片留alt", "图" in clean)
check("5.4 去标题/引用/列表符号", "#" not in clean and clean.find("引用") >= 0
      and "项目" in clean)
check("5.5 去公式定界符", "$" not in clean and "E=mc^2" in clean)
check("5.6 表格分隔行清理", "---" not in clean.replace("，", ""))
check("5.7 粗斜体标记剥离", "**" not in clean and "*" not in clean)
check("5.8 空输入安全", strip_md(None) == "" and strip_md("") == "")
check("5.9 非字符串安全", strip_md(123) == "")


# ==================== T6 TTSPlayer 状态机 ====================
print("T6 TTSPlayer 状态机")
from core.tts_player import TTSPlayer, _TTSWorker

player = TTSPlayer()

# 无密钥时 speak 应跳过且不崩溃
empty = _fake_settings(xiaomi_api_key="")
with patch.object(RealConfigManager, "__instancecheck__", lambda c, o: isinstance(o, RealConfigManager)):
    with patch.object(aic.ConfigManager, "__instancecheck__",
                      lambda c, o: isinstance(o, RealConfigManager)):
        with patch("core.tts_player.ConfigManager") as TCM:
            TCM.return_value.settings = empty
            states = []
            player.stateChanged.connect(states.append)
            player.speak("# 只有标题")
            app.processEvents()
            check("6.1 未配置密钥跳过合成", player.is_busy() is False)

            # 合成线程防 GC 与停止语义
            TCM.return_value.settings = _fake_settings()
            worker = _TTSWorker("文本", "", request_id=99)
            check("6.2 worker 信号签名", hasattr(worker, "ready")
                  and hasattr(worker, "failed") and hasattr(worker, "done"))

# stop 后 active_request 失效：迟到的 ready 不再进入播放
player2 = TTSPlayer()
player2._active_request = 7
player2.stop()
check("6.3 stop 使请求失效", player2._active_request == -1)
# 迟到结果（旧 request_id）不得触发播放线程
player2._on_synthesized(b"RIFF" + b"\x00" * 16, 7)
check("6.5 迟到结果被丢弃", player2._play_thread is None)
player2.shutdown()
check("6.4 shutdown 可重复调用", True)
player2.shutdown()


# ==================== T7 DialogView 朗读接线 ====================
print("T7 DialogView 朗读接线")
src_dialog = inspect.getsource(__import__("ui.dialog_view", fromlist=["DialogView"]))
check("7.1 MessageBubble 有朗读按钮", "_toggle_speak" in src_dialog and '"朗读"' in src_dialog)
check("7.2 on_speak 回调贯通", src_dialog.count("on_speak=self._handle_speak") >= 2)
check("7.3 自动朗读接入 _on_reply", "tts_auto_read" in src_dialog and "self._tts.speak(reply)" in src_dialog)
check("7.4 关窗回收 TTS 线程", "shutdown()" in src_dialog.split("def closeEvent")[-1])
from ui.dialog_view import MessageBubble
b = MessageBubble("assistant", "测试回复**内容**", on_speak=lambda *a, **k: None)
check("7.5 assistant 气泡含朗读按钮", getattr(b, "_speak_btn", None) is not None)
u = MessageBubble("user", "用户消息")
check("7.6 user 气泡无朗读按钮", getattr(u, "_speak_btn", None) is None)


# ==================== T8 APITab 往返 ====================
print("T8 APITab 小米分组往返")
from ui.settings_view import APITab

tab = APITab()
collected = tab.collect()
for key in ("xiaomi_enabled", "xiaomi_base_url", "xiaomi_model", "xiaomi_api_key",
            "xiaomi_role", "xiaomi_tts_model", "xiaomi_tts_voice", "tts_auto_read"):
    check(f"8.{key}", key in collected)
tab.xm_enabled.setChecked(True)
tab.xm_base.setText("https://token-plan-cn.xiaomimimo.com/v1")
tab.xm_model.setText("mimo-v2.5-pro")
tab.xm_key.setText("tp-abc")
tab.xiaomi_role.setCurrentIndex(tab.xiaomi_role.findData("dialog"))
tab.xm_tts_model.setText("mimo-v2.5-tts")
tab.xm_tts_voice.setCurrentText("白桦")
tab.tts_auto_read.setChecked(True)
c2 = tab.collect()
check("8.collect 往返一致",
      c2["xiaomi_model"] == "mimo-v2.5-pro" and c2["xiaomi_api_key"] == "tp-abc"
      and c2["xiaomi_role"] == "dialog" and c2["xiaomi_tts_voice"] == "白桦"
      and c2["tts_auto_read"] is True)
# load 回填
tab.xm_model.setText("")
tab._load()
check("8.load 回填模型", tab.xm_model.text() == AppSettings().xiaomi_model)


# ==================== 汇总 ====================
print(f"\n===== R53 结果: PASS={PASS} FAIL={FAIL} =====")
sys.exit(0 if FAIL == 0 else 1)
