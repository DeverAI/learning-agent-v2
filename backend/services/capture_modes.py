"""Shared capture-mode contract for photo search and photo checking."""

CAPTURE_MODES = ("single_question", "single_page", "whole_paper")
CAPTURE_MODE_ALIASES = {
    "single": "single_question",
    "question": "single_question",
    "page": "single_page",
    "paper": "whole_paper",
}
MAX_CAPTURE_IMAGES = 12
MAX_CAPTURE_TOTAL_SIZE = 60 * 1024 * 1024


def normalize_capture_mode(value: str | None, default: str = "single_question") -> str:
    mode = str(value or default).strip().lower()
    mode = CAPTURE_MODE_ALIASES.get(mode, mode)
    if mode not in CAPTURE_MODES:
        raise ValueError("拍照模式无效，请选择单题、单页或整卷")
    return mode


def capture_mode_label(mode: str) -> str:
    normalized = normalize_capture_mode(mode)
    return {
        "single_question": "单题",
        "single_page": "单页",
        "whole_paper": "整卷",
    }[normalized]
