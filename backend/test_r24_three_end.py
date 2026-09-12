# -*- coding: utf-8 -*-
"""R24：三端对齐相关的落点钉子。

覆盖本轮（R24）为「三端功能对齐」做的四件事：
1. **安卓原生语音识别桥**（WebView 无 Web Speech API → focus 模式语音作答此前恒定不可用）；
2. **focus 模式接原生 TTS 桥**（讲课页早就接了，专注模式一直没接 → App 内无声）；
3. **笔记 ← 题目 的反向显示**（题目能存进笔记，但笔记里看不到那道题）；
4. **编辑器保存后自动质量自检**（把 /api/diagram/check 的新字段真正露给用户）。

写法：能读源码断言的就读源码（跨文件契约必须两侧都断言），不引入 Qt/Android 依赖。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BACKEND = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BACKEND)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ==========================================================================
# 1. 安卓原生语音识别桥
# ==========================================================================

def test_android_speech_bridge_exists_and_is_registered():
    java = _read(os.path.join("android", "app", "src", "main", "java", "com",
                              "learningagent", "app", "MainActivity.java"))
    assert "SpeechRecognizer" in java
    assert "new SRBridge()" in java and '"AndroidSR"' in java, "桥没有注册到 WebView"
    assert "pollEvents" in java, "没有事件出口，JS 拿不到识别结果"


def test_android_speech_permission_handled():
    java = _read(os.path.join("android", "app", "src", "main", "java", "com",
                              "learningagent", "app", "MainActivity.java"))
    assert "REQ_SPEECH" in java
    assert "Manifest.permission.RECORD_AUDIO" in java
    # 拒绝授权必须如实告知（not-allowed），不能静默不出声
    assert '"not-allowed"' in java
    # 资源释放：麦克风不能被长期占用
    assert "speechRecognizer.destroy()" in java


def test_android_speech_has_error_streak_guard():
    """连续 no-match 不能无限重启（空转耗电）。"""
    java = _read(os.path.join("android", "app", "src", "main", "java", "com",
                              "learningagent", "app", "MainActivity.java"))
    assert "SR_MAX_ERROR_STREAK" in java
    assert "srErrorStreak" in java


def test_adapter_mirrors_web_speech_api():
    js = _read(os.path.join("backend", "static", "js", "android_sr.js"))
    # 与 Web Speech API 同形：focus.js 靠这几个字段消费，形状变了就静默失效
    assert "window.AndroidSpeechRecognition" in js
    for token in ("onresult", "onerror", "onend", "isFinal", "transcript", "resultIndex"):
        assert token in js, "适配层缺少 Web Speech 同形字段: " + token
    assert "JSON.parse" in js and "catch" in js, "桥返回非法 JSON 时必须跳过而不是抛异常"


def test_base_includes_adapter_and_focus_prefers_it():
    base = _read(os.path.join("backend", "templates", "base.html"))
    assert "android_sr.js" in base
    focus = _read(os.path.join("backend", "static", "js", "focus.js"))
    assert "pickSpeechRecognitionCtor" in focus
    assert "AndroidSpeechRecognition" in focus
    # 原生桥优先：普通浏览器里它自己会返回 null，不影响 Web Speech 路径
    seg = focus.split("function pickSpeechRecognitionCtor")[1].split("function startListening")[0]
    assert seg.index("AndroidSpeechRecognition") < seg.index("window.SpeechRecognition")


# ==========================================================================
# 2. focus 模式接原生 TTS 桥
# ==========================================================================

def test_focus_uses_android_tts_bridge():
    focus = _read(os.path.join("backend", "static", "js", "focus.js"))
    speak_seg = focus.split("function browserSpeak")[1].split("function stopSpeaking")[0]
    assert "window.AndroidTTS" in speak_seg, "专注模式没接原生 TTS 桥（App 内会完全没声音）"
    stop_seg = focus.split("function stopSpeaking")[1][:600]
    assert "AndroidTTS.stop" in stop_seg


# ==========================================================================
# 3. 笔记 ← 题目 反向显示 + 引用登记
# ==========================================================================

def test_note_creation_registers_reference():
    """只写 question_ids 不够：[[QUESTION:id]] 只有进 references 才会被解析成链接。"""
    js = _read(os.path.join("backend", "static", "js", "questions.js"))
    assert "/references" in js, "建笔记后没有登记引用 → 正文里是裸标记文本"
    seg = js.split("function saveNoteFromQuestion")[1].split("function retryQ")[0]
    assert "type:'question'" in seg.replace(" ", "")


def test_notes_page_shows_linked_questions():
    html = _read(os.path.join("backend", "templates", "notes.html"))
    seg = html.split("// Typical questions tab")[1].split("// Source images tab")[0]
    assert "n.questions" in seg, "笔记详情没有渲染关联题库题目（反向看不到那道题）"
    assert "/questions?qid=" in seg, "关联题目没有可点入口"


# ==========================================================================
# 4. 编辑器质量自检接线
# ==========================================================================

def test_editor_reports_diagram_quality():
    js = _read(os.path.join("backend", "static", "js", "editor.js"))
    assert "checkDiagramQuality" in js
    seg = js.split("checkDiagramQuality: function")[1][:900]
    assert "/api/diagram/check/" in seg
    assert "quality_issues" in seg
    # 保存成功后必须调用它，否则等于没接
    assert "editor.checkDiagramQuality(qid, 0)" in js


def test_quality_check_is_non_blocking_in_editor():
    """自检失败不能影响"已保存"这个事实（保存成功就该告诉用户成功）。"""
    js = _read(os.path.join("backend", "static", "js", "editor.js"))
    seg = js.split("checkDiagramQuality: function")[1][:900]
    assert ".catch(" in seg

# ==========================================================================
# 5. 下载页版本号不许再漂移（R12 漏掉的一条）
# ==========================================================================

def test_download_page_version_comes_from_gradle():
    """版本号必须从构建脚本读，不能再手写（R12 只改了体积/日期，版本号又漂了）。"""
    import re as _re
    import sys as _sys
    _sys.path.insert(0, os.path.join(ROOT, "backend"))
    gradle = _read(os.path.join("android", "app", "build.gradle.kts"))
    name_m = _re.search(r'versionName\s*=\s*"([^"]+)"', gradle)
    code_m = _re.search(r"versionCode\s*=\s*(\d+)", gradle)
    assert name_m and code_m, "构建脚本里找不到版本号"

    from main import _apk_version_from_gradle
    got = _apk_version_from_gradle()
    assert got, "解析不到 APK 版本（下载页会不显示版本而不是显示错的）"
    assert name_m.group(1) in got and code_m.group(1) in got, (
        "下载页版本与构建脚本不一致：" + got)


def test_download_template_has_no_hardcoded_version():
    html = _read(os.path.join("backend", "templates", "download.html"))
    assert not _re_dev_version(html), "下载页又出现手写版本号：" + _re_dev_version(html)


def _re_dev_version(html):
    import re as _re2
    m = _re2.search(r"v\d+\.\d+（versionCode \d+）", html)
    return m.group(0) if m else ""
