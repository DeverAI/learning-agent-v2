"""Regression coverage for OCR reference SVG, unified upload modes and adaptive UI."""

import asyncio
import importlib
import os
import re
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routers.ocr import _normalize_upload_mode, _split_mode_for_upload
from routers.questions import _replace_diagram_markers
from services.ai_service import ai_service
from services.config_service import normalize_home_layout
from services.diagram_service import diagram_service
from services.structure_graph_service import process_structure_graph


ROOT = Path(__file__).resolve().parent


def test_upload_modes_are_mutually_exclusive_and_backward_compatible():
    assert _normalize_upload_mode("one_per_image") == "one_per_image"
    assert _normalize_upload_mode("one_question_multi_image") == "one_question_multi_image"
    assert _normalize_upload_mode("auto_split") == "auto_split"
    assert _normalize_upload_mode("single") == "one_per_image"
    assert _normalize_upload_mode("multi") == "one_question_multi_image"
    assert _normalize_upload_mode("", "auto") == "auto_split"
    assert _split_mode_for_upload("auto_split") == "auto"
    assert _split_mode_for_upload("one_question_multi_image") == "single"
    with pytest.raises(HTTPException):
        _normalize_upload_mode("multi_and_split")


def test_home_layout_filters_unknown_disabled_and_duplicate_widgets():
    result = normalize_home_layout([
        "knowledge", "knowledge", "unknown", "upload_question", "",
    ])
    assert result == ["knowledge", "upload_question"]
    assert normalize_home_layout([]) == []


def test_reference_marker_is_allowed_but_untrusted_url_is_not_rendered():
    safe = _replace_diagram_markers(
        "before [[DIAGRAM:0]] after",
        [{"path": "/storage/questions/abc123/reference.svg", "w": 640}],
    )
    assert 'src="/storage/questions/abc123/reference.svg"' in safe
    assert 'width="640"' in safe

    hostile = _replace_diagram_markers(
        "[[DIAGRAM:0]]",
        [{"path": '\" onerror=\"alert(1)'}],
    )
    assert hostile == "[[DIAGRAM:0]]"
    assert "onerror" not in hostile


def test_reference_svg_is_sanitized_and_saved_in_separate_file(tmp_path, monkeypatch):
    diagram_module = importlib.import_module("services.diagram_service")
    monkeypatch.setattr(diagram_module, "QUESTIONS_DIR", str(tmp_path))
    raw = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180" fill="red">'
        '<script>alert(1)</script><foreignObject x="0" y="0"><div>bad</div></foreignObject>'
        '<rect x="5" y="5" width="100" height="50" fill="#ff0000" onclick="x()"/>'
        '<text x="12" y="30">A</text></svg>'
    )
    saved = asyncio.run(diagram_service.save_reference_svg("refcase", raw))
    target = tmp_path / "refcase" / "reference.svg"
    assert target.is_file()
    assert saved["path"] == "/storage/questions/refcase/reference.svg"
    text = target.read_text(encoding="utf-8")
    assert "script" not in text.lower()
    assert "foreignObject" not in text
    assert "onclick" not in text
    assert "red" not in text.lower() and "#ff0000" not in text.lower()
    assert 'viewBox="0 0 320 180"' in text


def test_reference_svg_rejects_invalid_viewbox(tmp_path, monkeypatch):
    diagram_module = importlib.import_module("services.diagram_service")
    monkeypatch.setattr(diagram_module, "QUESTIONS_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="viewBox"):
        asyncio.run(diagram_service.save_reference_svg("badview", '<svg viewBox="0 0 0 10"></svg>'))


def test_solver_receives_reference_svg_as_cross_checked_visual_evidence(monkeypatch):
    captured = {}

    async def fake_chat_json(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        captured["scope"] = kwargs.get("scope")
        return {
            "question_html": "题面 [[DIAGRAM:0]]",
            "standard_answer": "1",
            "answer_html": "过程",
            "diagram_prompts": [],
        }

    monkeypatch.setattr(ai_service, "_chat_json", fake_chat_json)
    result = asyncio.run(ai_service.deepseek_solve(
        subject="数学",
        grade="初中",
        ocr_text="如图，求证。",
        knowledge_tags=["几何"],
        reference_svg='<svg viewBox="0 0 10 10"><line x1="0" y1="0" x2="10" y2="10"/></svg>',
        source_reference="参考答案写 1",
    ))
    assert result["standard_answer"] == "1"
    assert "原题参考SVG" in captured["prompt"]
    assert "权重较高但并非绝对正确" in captured["prompt"]
    assert "question_challenge" in captured["prompt"]
    assert "不可信证据" in captured["prompt"]
    assert "不得为了匹配它" in captured["prompt"]
    assert "严禁根据上传的参考答案" in captured["prompt"]
    assert "[[DIAGRAM:0]]" in captured["prompt"]
    assert "从 [[DIAGRAM:1]] 开始" in captured["prompt"]
    assert "严禁在 diagram_prompts 中重画" in captured["prompt"]
    assert "题面1张（question）" not in captured["prompt"]
    assert captured["scope"] == "solve"


def test_explicit_question_challenge_is_independent_and_does_not_rewrite(monkeypatch):
    captured = {}

    async def fake_deepseek_json(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return {"level": "high", "confidence": 0.95, "reasons": ["条件矛盾"]}

    monkeypatch.setattr(ai_service, "deepseek_json", fake_deepseek_json)
    result = asyncio.run(ai_service.deepseek_challenge_question(
        ocr_text="已知 x>1 且 x<0，求 x。",
        question_html="<p>已知 x&gt;1 且 x&lt;0，求 x。</p>",
        answer_html="<p>x=1</p>",
        standard_answer="1",
        subject="数学",
        grade="初中",
        source_reference="答案：1",
    ))
    assert result["level"] == "high"
    assert "主动寻找题目是否有问题" in captured["prompt"]
    assert "禁止为了匹配答案" in captured["prompt"]
    assert "只报告能指出具体证据的疑点" in captured["prompt"]


def test_structure_graph_uses_theme_tokens_and_external_cross_level_lanes():
    graph = {
        "nodes": [
            {"id": 0, "level": 0, "text": "已知", "type": "condition"},
            {"id": 1, "level": 1, "text": "中间条件", "type": "key"},
            {"id": 2, "level": 3, "text": "结论", "type": "conclusion"},
        ],
        "edges": [{"from": 0, "to": 1}, {"from": 0, "to": 2}],
    }
    result = process_structure_graph(graph)
    assert result
    svg = result["svg"]
    for token in ("--paper-bg", "--text", "--sg-condition-bg", "--sg-key-bg", "--sg-conclusion-bg"):
        assert token in svg
    assert 'data-node-type="condition"' in svg
    assert 'data-node-type="key"' in svg
    assert 'data-node-type="conclusion"' in svg
    paths = re.findall(r'<path d="([^"]+)"[^>]+marker-end=', svg)
    assert any(path.count(" L ") >= 5 for path in paths), paths


def test_templates_expose_one_upload_choice_and_adaptive_canvas_controls():
    questions = (ROOT / "templates" / "questions.html").read_text(encoding="utf-8")
    questions_js = (ROOT / "static" / "js" / "questions.js").read_text(encoding="utf-8")
    batch = (ROOT / "templates" / "batch_upload.html").read_text(encoding="utf-8")
    dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    for page in (questions, batch):
        assert 'value="one_per_image"' in page
        assert 'value="one_question_multi_image"' in page
        assert 'value="auto_split"' in page
        assert 'id="uploadSplit"' not in page
    assert "<script src=\"/static/js/questions.js" in questions
    assert "_sgAdaptCardHeight" in questions_js
    assert "_sgPinching" in questions_js
    assert "SG_MIN_SCALE = 0.002" in questions_js
    assert "+m.title+" not in dashboard
    assert "+m.content+" not in dashboard


def test_daily_quote_cache_survives_non_dict_json(tmp_path, monkeypatch):
    """合法 JSON 但非对象（[] / "x" / 数字）的引用缓存按损坏处理，不得 500。"""
    from routers import system as system_router
    monkeypatch.setattr(system_router, "QUOTE_DIR", str(tmp_path))
    path = Path(system_router._get_quote_filepath())
    for bad in ("[]", '"x"', "123", "null", "{broken"):
        path.write_text(bad, encoding="utf-8")
        assert system_router._load_quote_from_file() is None
    path.write_text('{"quote": "学而不思则罔", "source": "孔子"}', encoding="utf-8")
    loaded = system_router._load_quote_from_file()
    assert loaded and loaded["quote"] == "学而不思则罔"


def test_emotion_confidence_nan_and_inf_fall_back():
    """NaN/inf 置信度不得绕过 0~1 夹紧（max/min 对 NaN 比较失效）。"""
    from services.face_analysis_service import _normalize_emotion_report
    assert _normalize_emotion_report({"confidence": float("nan")})["confidence"] == 0.5
    assert _normalize_emotion_report({"confidence": float("inf")})["confidence"] == 0.5
    assert _normalize_emotion_report({"confidence": 0.9})["confidence"] == 0.9
    assert _normalize_emotion_report({"confidence": "bad"})["confidence"] == 0.5


def test_comparison_view_reuses_unified_sanitizer():
    """对比模式必须复用 app.js 全局消毒；独立正则消毒（双写重组可绕过）不得回归。"""
    app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    questions_js = (ROOT / "static" / "js" / "questions.js").read_text(encoding="utf-8")
    assert "window._sanitizeHtml=_sanitizeHtml" in app_js
    assert "return window._sanitizeHtml(html)" in questions_js
    assert "s.replace(/javascript:/gi, '')" not in questions_js
