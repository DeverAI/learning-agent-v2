"""Normalize and combine AI doubts about the source question.

The OCR text, OCR-produced SVG, uploaded reference answer and solver output are
independent pieces of evidence.  This module keeps the confidence policy in
deterministic code instead of letting one model freely decide downstream use.
"""

from __future__ import annotations

import math


CHALLENGE_LEVELS = {"none", "low", "high"}
ISSUE_TYPES = {
    "contradictory_conditions",
    "missing_condition",
    "non_unique",
    "no_solution",
    "diagram_text_conflict",
    "reference_answer_conflict",
    "ocr_ambiguity",
    "option_mismatch",
    "unit_or_domain_conflict",
    "out_of_scope",
    "other",
}


def _finite_float(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _short_texts(value, *, limit: int = 8, width: int = 300) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text[:width])
        if len(result) >= limit:
            break
    return result


def normalize_question_challenge(value, *, source: str = "") -> dict:
    """Return a bounded, stable challenge object for storage and UI."""
    raw = value if isinstance(value, dict) else {}
    aliases = {
        "无": "none", "正常": "none", "pass": "none", "approve": "none",
        "低": "low", "较低": "low", "可能": "low", "uncertain": "low",
        "高": "high", "较高": "high", "严重": "high", "invalid": "high",
    }
    level = str(raw.get("level", raw.get("risk", "none")) or "none").strip().lower()
    level = aliases.get(level, level)
    if level not in CHALLENGE_LEVELS:
        level = "none"

    default_confidence = {"none": 0.0, "low": 0.55, "high": 0.85}[level]
    confidence = max(0.0, min(1.0, _finite_float(raw.get("confidence"), default_confidence)))
    reasons = _short_texts(raw.get("reasons", raw.get("reason", [])))
    evidence = _short_texts(raw.get("evidence", []), limit=8, width=400)

    raw_types = raw.get("issue_types", raw.get("issue_type", []))
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    issue_types = []
    for issue in raw_types if isinstance(raw_types, list) else []:
        normalized = str(issue or "").strip().lower()
        if normalized in ISSUE_TYPES and normalized not in issue_types:
            issue_types.append(normalized)
    if level != "none" and not issue_types:
        issue_types = ["other"]

    recommendation = str(raw.get("recommended_action", "") or "").strip()[:300]
    if not recommendation:
        recommendation = {
            "none": "可按正常题目使用",
            "low": "保留当前解答，但请结合原图核对疑点和假设",
            "high": "暂停用于组卷和批改，人工核对原图、条件与参考答案",
        }[level]

    return {
        "level": level,
        "confidence": round(confidence, 3),
        "issue_types": issue_types,
        "reasons": reasons,
        "evidence": evidence,
        "recommended_action": recommendation,
        "source": str(source or raw.get("source", "") or "")[:40],
    }


def combine_question_challenges(*values) -> dict:
    """Combine automatic model opinions without promoting a lone weak claim.

    Two high-risk opinions confirm a high challenge.  A single opinion needs at
    least 0.9 confidence; otherwise it is surfaced as low probability.
    """
    items = []
    seen_sources = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        item = normalize_question_challenge(value)
        source = item.get("source", "")
        if source and source in seen_sources:
            continue
        if source:
            seen_sources.add(source)
        items.append(item)
    active = [item for item in items if item["level"] != "none"]
    if not active:
        return normalize_question_challenge({})

    high_items = [item for item in active if item["level"] == "high"]
    if len(high_items) >= 2 or any(item["confidence"] >= 0.9 for item in high_items):
        level = "high"
    else:
        level = "low"

    combined = {
        "level": level,
        "confidence": max(item["confidence"] for item in active),
        "issue_types": [],
        "reasons": [],
        "evidence": [],
        "recommended_action": "",
        "source": "+".join(item["source"] for item in active if item["source"]),
    }
    for key in ("issue_types", "reasons", "evidence"):
        for item in active:
            for entry in item[key]:
                if entry not in combined[key]:
                    combined[key].append(entry)
    return normalize_question_challenge(combined)


def has_unresolved_high_challenge(audit_flags, is_resolved: bool = False) -> bool:
    if is_resolved or not isinstance(audit_flags, list):
        return False
    return any(
        isinstance(flag, dict)
        and (
            flag.get("type") == "question_challenge_high"
            or (isinstance(flag.get("challenge"), dict)
                and flag["challenge"].get("level") == "high")
        )
        for flag in audit_flags
    )
