"""Delivery-gate regression tests for the repaired end-to-end logic contracts."""

import asyncio
import base64
import importlib
import os
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routers.ocr import _normalize_classified_roles
from routers.search import _ai_rank_similarity
from routers.sessions import RenameRequest, SessionCreateRequest
from routers.settings import SettingsImportRequest
from schemas.schemas import PaperGenerateRequest
from services.ai_service import ai_service, normalize_correction_report, _complete_svg
from services.diagram_service import diagram_service
from services.export_service import export_service
from services.ocr_service import _split_solution_sections
from services.paper_service import paper_service
from services.question_challenge import (
    combine_question_challenges,
    has_unresolved_high_challenge,
    normalize_question_challenge,
)


def test_solution_sections_never_drop_detailed_answer_when_score_is_separate():
    answer, score = _split_solution_sections("<p>完整推导过程</p>", "列式正确 2分")
    assert answer == "<p>完整推导过程</p>"
    assert score == "列式正确 2分"

    answer, score = _split_solution_sections(
        "旧得分点<!-- SCORE_SPLIT --><p>旧版完整解析</p>", "新得分点"
    )
    assert answer == "<p>旧版完整解析</p>"
    assert score == "新得分点"


def test_multi_image_roles_always_cover_every_file_and_keep_a_question_image():
    saved = [
        {"filename": "question.png", "path": "a"},
        {"filename": "analysis.png", "path": "b"},
        {"filename": "answer.png", "path": "c"},
    ]
    roles = _normalize_classified_roles(saved, [{"filename": "analysis.png", "role": "analysis"}])
    assert len(roles) == len(saved)
    assert roles[0]["role"] == "question"
    assert roles[1]["role"] == "analysis"
    assert roles[2]["role"] == "extra"


def test_correction_report_clamps_invalid_nan_negative_and_over_max_scores():
    normalized = normalize_correction_report({
        "score": "nan",
        "max_score": -10,
        "points": [
            {"index": "bad", "score": 8, "max_score": 3, "hit": "false"},
            {"score": -2, "max_score": 2},
        ],
    })
    assert normalized["score"] == 3
    assert normalized["max_score"] == 5
    assert normalized["points"][0]["index"] == 0
    assert normalized["points"][0]["score"] == 3
    assert normalized["points"][0]["hit"] is False
    assert normalized["points"][1]["score"] == 0


def test_reference_svg_requires_a_complete_closing_tag():
    assert _complete_svg("prefix <svg viewBox='0 0 10 10'><path d='M0 0'/></svg> suffix").startswith("<svg")
    assert _complete_svg("<svg viewBox='0 0 10 10'><path") == ""


def test_search_ai_rank_accepts_json_object_contract(monkeypatch):
    async def fake_json(*args, **kwargs):
        return {"scores": [{"index": 1, "score": 1.4}, {"index": 0, "score": "0.25"}]}

    monkeypatch.setattr(ai_service, "deepseek_json", fake_json)
    scores = asyncio.run(_ai_rank_similarity("查询题", [
        {"ocr_text": "候选一"}, {"ocr_text": "候选二"},
    ]))
    assert scores == [(1, 1.0), (0, 0.25)]


def test_paper_uses_stable_question_ids_and_authoritative_stored_answers():
    questions = [
        {
            "id": "q1", "subject": "数学", "grade": "初中", "knowledge_tags": ["函数"],
            "question_html": "<p>题干</p>", "ocr_text": "", "region": "", "avg_score": None,
            "standard_answer": "2", "score_points_html": "列式 2分", "answer_html": "<p>推导</p>",
        }
    ]
    prompt = paper_service._format_questions_for_prompt(questions)
    assert 'data-question-id="q1"' in prompt
    assert paper_service._has_exact_question_coverage(prompt, ["q1"])
    assert not paper_service._has_exact_question_coverage(prompt + prompt, ["q1"])
    answer = paper_service._build_answer_html(questions)
    assert "列式 2分" in answer and ">2<" in answer and "推导" in answer


def test_note_source_images_are_stored_as_files_not_database_base64(tmp_path, monkeypatch):
    notes_module = importlib.import_module("routers.notes")
    monkeypatch.setattr(notes_module, "STORAGE_DIR", str(tmp_path))
    raw = b"\x89PNG\r\n\x1a\n" + b"content"
    encoded = "data:image/png;base64," + base64.b64encode(raw).decode()
    urls = notes_module._store_note_images("note1", [encoded])
    assert urls == ["/storage/notes/note1/source_0.png"]
    target = tmp_path / "notes" / "note1" / "source_0.png"
    assert target.read_bytes() == raw


def test_modify_paper_rejects_unbounded_explicit_question_sets():
    with pytest.raises(ValueError):
        PaperGenerateRequest(question_ids=[f"q{i}" for i in range(51)], mode="modify")


def test_word_export_does_not_duplicate_nested_html_blocks(tmp_path):
    from docx import Document

    target = tmp_path / "nested.docx"
    export_service._html_to_word(
        "<section><div><p>第一题</p><p>第二题</p></div></section>", str(target)
    )
    paragraphs = [p.text for p in Document(target).paragraphs if p.text]
    assert paragraphs == ["第一题", "第二题"]


def test_question_challenge_levels_are_bounded_and_need_evidence_consensus():
    weak_high = normalize_question_challenge({
        "level": "high", "confidence": "nan",
        "issue_types": ["missing_condition", "invented_type"],
        "reasons": ["缺少长度条件"],
    }, source="solver")
    assert weak_high["confidence"] == 0.85
    assert weak_high["issue_types"] == ["missing_condition"]

    # One unconfirmed high opinion is shown as low; two independent opinions promote it.
    assert combine_question_challenges(weak_high)["level"] == "low"
    confirmed = combine_question_challenges(
        weak_high,
        {"level": "high", "confidence": 0.8, "reasons": ["答案不唯一"],
         "issue_types": ["non_unique"], "source": "verify"},
    )
    assert confirmed["level"] == "high"
    flags = [{"type": "question_challenge_high", "challenge": confirmed}]
    assert has_unresolved_high_challenge(flags, False)
    assert not has_unresolved_high_challenge(flags, True)


def test_empty_reasoning_response_retries_with_larger_budget(monkeypatch):
    """空正文 + `finish_reason=length` 时必须重试，且**重试预算要跳得足够大**。

    这里刻意让 mock **只在预算 >= REASONING_SAFE_MIN 时**才返回正文 —— 模拟真实情况：
    本项目主力模型 `deepseek-v4-pro` 是**推理模型**，写 1500 字课稿时光 reasoning 就要
    3300+ tokens，实测 2048 与 4096 都拿不到正文（正文长度为 0），8192 才行。

    为什么要把这段写进测试注释：旧断言是 `calls == [128, 512]`，等于把
    "512 就够"这个**已经被实测推翻的假设**固化成了契约。后果是 2026-09-11
    有人（我）按"正文太长会撞上限"的直觉把备课预算从 4096 下调到 2048，
    测试照样全绿，而生产上备课**一片都写不出来**。
    """
    calls = []
    # REASONING_SAFE_MIN 是模块级常量，而本文件里的 ai_service 是单例，取的是模块。
    from services import ai_service as _ai_module
    floor = _ai_module.REASONING_SAFE_MIN

    class Response:
        status_code = 200
        text = ""

        def json(self):
            budget = calls[-1]
            if budget < floor:
                return {"choices": [{"message": {"content": "", "reasoning_content": "思考"},
                                     "finish_reason": "length"}]}
            return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            calls.append(json["max_tokens"])
            return Response()

    monkeypatch.setattr("services.ai_service.httpx.AsyncClient", Client)
    result = asyncio.run(ai_service._call(
        "https://example.invalid/chat/completions", "secret",
        {"model": "reasoning", "messages": [], "max_tokens": 128}, timeout=10,
    ))
    assert result == "OK"
    assert calls[0] == 128
    assert calls[1] >= floor, (
        f"重试预算只有 {calls[1]}，推理模型仍然拿不到正文（需要 >= {floor}）"
        f" —— 这正是 2026-09-11 备课写不出内容的成因")


def test_kimi_model_forces_supported_temperature(monkeypatch):
    captured = {}

    async def fake_call(url, key, payload, timeout=None):
        captured.update(payload)
        return "OK"

    monkeypatch.setattr(ai_service, "_call", fake_call)
    old_model = ai_service.km_model
    ai_service.km_model = "kimi-k2.6"
    try:
        assert asyncio.run(ai_service.kimi_chat([], temperature=0, max_tokens=512)) == "OK"
    finally:
        ai_service.km_model = old_model
    assert captured["temperature"] == 1


def test_invalid_svg_is_not_reported_as_a_successful_drawing():
    assert not diagram_service._has_drawing_content(
        '<svg viewBox="0 0 400 300"></svg>'
    )
    assert diagram_service._has_drawing_content(
        '<svg viewBox="0 0 400 300"><line x1="0" y1="0" x2="1" y2="1"/></svg>'
    )


def test_session_titles_have_a_bounded_request_contract():
    assert SessionCreateRequest().title == "新对话"
    assert RenameRequest(title="有效标题").title == "有效标题"
    with pytest.raises(ValidationError):
        SessionCreateRequest(title="x" * 81)
    with pytest.raises(ValidationError):
        RenameRequest(title="x" * 81)


def test_settings_import_rejects_oversized_or_excessive_payloads():
    with pytest.raises(ValidationError):
        SettingsImportRequest.model_validate({f"k{i}": i for i in range(201)})
    with pytest.raises(ValidationError):
        SettingsImportRequest.model_validate({"quote_topic": "x" * 500_001})
