"""Consistent upload-session state used by the upload page and paper library."""


def summarize_upload_session(session, question_statuses: dict[str, tuple[str, bool]]) -> dict:
    ids = list(session.question_ids or [])
    counts = {"done": 0, "error": 0, "processing": 0, "staged": 0,
              "missing": 0, "locked": 0}
    for question_id in ids:
        info = question_statuses.get(question_id)
        if info is None:
            counts["missing"] += 1
            counts["error"] += 1
            continue
        status, is_resolved = info
        if status == "done":
            counts["done"] += 1
        elif is_resolved:
            counts["locked"] += 1
            counts["error"] += 1
        elif status == "error":
            counts["error"] += 1
        elif status == "staged":
            counts["staged"] += 1
        else:
            counts["processing"] += 1

    total = len(ids)
    ready = total > 0 and counts["done"] == total
    if session.status == "done" and session.paper_id:
        effective_status = "done"
    elif ready:
        effective_status = "ready"
    elif counts["processing"]:
        effective_status = "processing"
    elif counts["error"] and not counts["staged"]:
        effective_status = "attention"
    else:
        effective_status = "open"

    return {
        "id": session.id,
        "title": session.title,
        "subject": session.subject,
        "grade": session.grade,
        "notes": session.notes,
        "status": effective_status,
        "question_ids": ids,
        "question_count": total,
        "upload_mode": getattr(session, "upload_mode", "one_per_image") or "one_per_image",
        "paper_id": session.paper_id,
        "created_at": session.created_at,
        "counts": counts,
        "ready": ready,
        "progress": (counts["done"] + counts["error"]) / total if total else 0,
    }
