"""Regression tests for three-mode photo workflows and paper-library visibility."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from routers.correction import router as correction_router
from routers.papers import router as papers_router
from services.capture_modes import normalize_capture_mode
from services.upload_session_service import summarize_upload_session


ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "templates"


def test_capture_mode_contract_accepts_three_modes_and_legacy_aliases():
    assert normalize_capture_mode("single_question") == "single_question"
    assert normalize_capture_mode("single_page") == "single_page"
    assert normalize_capture_mode("whole_paper") == "whole_paper"
    assert normalize_capture_mode("single") == "single_question"
    assert normalize_capture_mode("paper") == "whole_paper"
    with pytest.raises(ValueError):
        normalize_capture_mode("auto")


def test_upload_session_attention_and_ready_states_are_visible():
    session = SimpleNamespace(
        id="s1", title="测试数据", subject="数学", grade="初二", notes="",
        status="open", paper_id="", question_ids=["q1", "q2"],
        upload_mode="one_per_image", created_at=None,
    )
    attention = summarize_upload_session(
        session, {"q1": ("pending", True), "q2": ("error", False)}
    )
    assert attention["status"] == "attention"
    assert attention["question_count"] == 2
    assert attention["counts"]["error"] == 2

    ready = summarize_upload_session(
        session, {"q1": ("done", False), "q2": ("done", False)}
    )
    assert ready["status"] == "ready"
    assert ready["ready"] is True


def test_static_library_and_batch_routes_precede_dynamic_ids():
    paper_paths = [route.path for route in papers_router.routes]
    correction_paths = [route.path for route in correction_router.routes]
    assert paper_paths.index("/api/papers/library") < paper_paths.index("/api/papers/{paper_id}")
    assert correction_paths.index("/api/correction/batch") < correction_paths.index(
        "/api/correction/{correction_id}"
    )


def test_search_and_check_templates_expose_three_modes_and_multi_image_inputs():
    for name in ("search.html", "correct.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        for mode in ("single_question", "single_page", "whole_paper"):
            assert f'value="{mode}"' in text
        assert " multiple" in text
        assert "MAX_IMAGES = 12" in text

    paper_correct = (TEMPLATES / "paper_correct.html").read_text(encoding="utf-8")
    assert 'id="paperFileInput"' in paper_correct
    assert " multiple" in paper_correct


def test_paper_library_frontend_uses_aggregate_endpoint():
    text = (TEMPLATES / "papers.html").read_text(encoding="utf-8")
    assert "/api/papers/library" in text
    assert "upload_session" in text
    assert "attention:'需处理'" in text
