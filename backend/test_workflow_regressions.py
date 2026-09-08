"""Regression tests for upload/search/note workflow boundaries."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from routers.ocr import _has_supported_image_signature as ocr_image_signature
from routers.search import _has_supported_image_signature as search_image_signature
from routers.correction import _has_supported_image_signature as correction_image_signature
from services.note_service import merge_notes


def test_upload_signature_validation_rejects_renamed_text_files():
    fake = b"this is not an image"
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 16
    webp = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"x" * 8
    for validator in (ocr_image_signature, search_image_signature, correction_image_signature):
        assert validator(fake) is False
        assert validator(png) is True
        assert validator(webp) is True


def test_note_merge_never_crosses_subjects():
    new_note = {"subject": "数学", "knowledge_tags": ["函数"], "content": "新内容"}
    existing = [{"subject": "物理", "knowledge_tags": ["函数"], "content": "旧内容"}]
    idx, merged = merge_notes(new_note, existing)
    assert idx == -1
    assert merged is None


def test_note_merge_still_merges_same_subject_overlap():
    new_note = {"subject": "数学", "knowledge_tags": ["函数"], "content": "新内容"}
    existing = [{"subject": "数学", "knowledge_tags": ["函数"], "content": "旧内容"}]
    idx, merged = merge_notes(new_note, existing)
    assert idx == 0
    assert "旧内容" in merged and "新内容" in merged
