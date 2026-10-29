"""Recording what happened to each question (for operators, never for users).

* questions/failed.txt and success.txt - one line per question with a short,
  stable outcome label (see failure_category); written by log_*_question.
* debug.txt - one compact JSON audit line per answer (log_answer_decision):
  why an answer and its chart were or were not delivered. No prompts, secrets
  or observation values.
* Retention - old entries are pruned in a background thread
  (start_log_retention_in_background) under the privacy policy.

Used by: app.py, ui/answer_pipeline.py, ui/debug_capture.py.
Tested in: tests/test_access_and_logs.py.
"""

from datetime import datetime, timezone
import json
import re
import threading
import time

import streamlit as st

from utils import privacy as sage_privacy, process_state as _process_state
from utils.errors import log_unexpected
from utils.redaction import redact_diagnostic
from utils.secure_files import open_private_append
from ui.config import (
    APP_DIR,
    DEBUG_LOG_FILE,
    FAILED_QUESTIONS_FILE,
    LOGGER,
    MAX_UI_INPUT_CHARS,
    SUCCESS_QUESTIONS_FILE,
    DEBUG_FILE_LOCK,
    FAILED_FILE_LOCK,
    SUCCESS_FILE_LOCK,
)


def _write_question_outcome(question, category, destination, lock, *, is_manual: bool = False):
    """Write a redacted, single-line question outcome to its designated log.

    Sample-bank questions are logged verbatim (they contain no personal data).
    A visitor's own wording is logged as a keyed pseudonym unless the operator
    opts in with SAGE_LOG_MANUAL_QUESTION_TEXT=1 (PDPA data minimisation).
    """
    if not isinstance(question, str) or not question.strip():
        return False
    if is_manual and not sage_privacy.log_manual_question_text():
        question = "[question " + sage_privacy.question_digest(question) + "]"
    clean_question = re.sub(r"[\x00-\x1f\x7f]+", " ", question).strip()
    clean_question = re.sub(
        r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b", "[REDACTED_API_KEY]", clean_question, flags=re.I
    )[:MAX_UI_INPUT_CHARS]
    record = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        + " | "
        + ("manual" if is_manual else "sample")
        + " | "
        + category
        + " | "
        + clean_question
        + "\n"
    )
    try:
        with lock:
            with open_private_append(destination) as handle:
                handle.write(record)
        return True
    except OSError:
        LOGGER.exception("Could not write the local question log: %s", destination)
        return False


def log_failed_question(question, reason, *, is_manual: bool = False, path=None):
    """Record one failed question without storing answers or credentials."""
    category = re.sub(r"[^a-z_]+", "_", str(reason).lower()).strip("_")[:48]
    return _write_question_outcome(
        question,
        category or "unverified_answer",
        path if path is not None else FAILED_QUESTIONS_FILE,
        FAILED_FILE_LOCK,
        is_manual=is_manual,
    )


def log_successful_question(
    question, *, is_manual: bool = False, path=None, status: str = "verified"
):
    """Record one already-verified question without storing its answer."""
    return _write_question_outcome(
        question,
        "verified_limited" if status == "verified_limited" else "verified",
        path if path is not None else SUCCESS_QUESTIONS_FILE,
        SUCCESS_FILE_LOCK,
        is_manual=is_manual,
    )


def show_log_write_failure(file_name) -> None:
    """Warn and suspend batch mode so no unlogged query is skipped."""
    st.warning(f"Cannot write {file_name}. Check write permissions in the Sage folder.")
    if st.session_state.get("auto_test_bank_hash"):
        st.session_state.auto_test_log_failed = True
        st.session_state.auto_test_paused = True
        st.session_state.auto_test_error = (
            f"Cannot write {file_name}. Automatic testing stopped at this question. "
            "Correct folder permissions, then restart with --test to retry it."
        )


def failure_category(
    response, *, verified, numeric_blocked: bool = False, no_supported_answer: bool = False
):
    """Use a stable, non-sensitive label instead of logging raw exceptions."""
    if isinstance(response, dict):
        if response.get("security_blocked"):
            return "security_blocked"
        if response.get("scope_blocked"):
            return "out_of_scope"
    if no_supported_answer:
        return "no_supported_answer"
    if numeric_blocked:
        return "numeric_evidence_unmatched"
    if not verified:
        return "unverified_or_unavailable"
    return None


def log_answer_decision(question, answer, response, *, failure_category=None) -> bool:
    """Append bounded structured audit metadata to debug.txt on every completed query.

    The per-question full transcript remains opt-in via --debug. This concise
    always-on record captures why an answer and its graph were or were not
    delivered; it never includes full prompts, API secrets or observation data.
    """
    if not isinstance(answer, dict):
        return False
    results = (response.get("tool_results") or []) if isinstance(response, dict) else []
    candidates = []
    for item in results[:40]:
        if not isinstance(item, dict):
            continue
        candidate = {
            "table_id": str(item.get("table_id") or "")[:32],
            "success": item.get("success") is True,
            "table_complete": item.get("table_complete"),
            "analysis_ready": item.get("analysis_ready"),
            "chart_source_verified": item.get("chart_source_verified"),
            "chart_suppressed_reason": str(item.get("chart_suppressed_reason") or "")[:120],
            "error": redact_diagnostic(item.get("error"), 160),
            "selected_series": [
                {
                    "id": str(v.get("series_no") or "")[:32],
                    "name": str(v.get("series_name") or "")[:110],
                }
                for v in (item.get("selected_series") or [])[:8]
                if isinstance(v, dict)
            ]
            if isinstance(item.get("selected_series"), list)
            else [],
        }
        candidates.append(candidate)
    spec = answer.get("chart_spec")
    spec = spec if isinstance(spec, dict) else {}
    chart_context = answer.get("chart_context")
    chart_context = chart_context if isinstance(chart_context, dict) else {}
    selected_series = [
        str(item.get("name") or "")[:110]
        for item in (spec.get("series") or [])[:12]
        if isinstance(item, dict)
    ]
    payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question_hash": sage_privacy.question_digest(question),
        "delivery_status": str(answer.get("delivery_status") or "unknown")[:80],
        "answer_verified": answer.get("verified") is True,
        "answer_limited": answer.get("limited_answer") is True,
        "failure_category": str(failure_category or "")[:80],
        "displayed_chart": bool(spec),
        "chart_table": str(chart_context.get("table_id") or "")[:32],
        "chart_series": selected_series,
        "chart_failure_reason": str(answer.get("chart_failure_reason") or "")[:120],
        "chart_suppressed": answer.get("suppress_charts") is True,
        "tool_results": candidates,
    }
    try:
        with DEBUG_FILE_LOCK:
            with open_private_append(DEBUG_LOG_FILE) as fp:
                fp.write(
                    "[ANSWER_AUDIT] "
                    + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
                    + "\n"
                )
    except (OSError, ValueError, TypeError):
        LOGGER.exception("Cannot write answer audit to debug.txt")
        return False
    return True


_RETENTION_STATE = _process_state.shared(
    "retention_state", lambda: {"thread": None, "started": 0.0}
)


_RETENTION_STATE_LOCK = _process_state.lock("retention_state")


def _run_log_retention() -> None:
    """Prune old log entries under the privacy policy (utils/privacy.py)."""
    try:
        sage_privacy.enforce_retention(
            APP_DIR,
            debug_log=DEBUG_LOG_FILE,
            locks={
                "questions/failed.txt": FAILED_FILE_LOCK,
                "questions/success.txt": SUCCESS_FILE_LOCK,
                "debug.txt": DEBUG_FILE_LOCK,
            },
        )
    except Exception as error:  # noqa: BLE001 - maintenance must never affect visitors
        log_unexpected(LOGGER, "Log retention failed", error)


def start_log_retention_in_background() -> None:
    """Rewrite/rotate logs on a daemon thread instead of inside a visitor's request.

    enforce_retention() itself decides whether work is due (every 6 h); this only
    avoids spawning a thread more than once a minute or while one is running.
    """
    now = time.monotonic()
    with _RETENTION_STATE_LOCK:
        running = _RETENTION_STATE["thread"]
        if (running is not None and running.is_alive()) or (
            _RETENTION_STATE["started"] and now - _RETENTION_STATE["started"] < 60
        ):
            return
        worker = threading.Thread(target=_run_log_retention, name="sage-log-retention", daemon=True)
        _RETENTION_STATE.update(thread=worker, started=now)
    worker.start()
