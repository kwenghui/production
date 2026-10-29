"""Opt-in full diagnostics for one question (operators only).

When ``st.session_state.capture_debug`` is on (``--debug`` start option, or
SAGE_FULL_DEBUG_LOG=1), capture_query_debug() copies the terminal output and
log records produced while ONE question runs into debug.txt, redacted. Other
users' concurrent questions are filtered out by thread.

Used by: app.py, ui/answer_pipeline.py.
Tested in: tests/test_ui_helpers.py.
"""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import logging
import re
import sys
import threading
import time

import streamlit as st

from utils import privacy as sage_privacy
from utils.chart_selection import question_chart_key
from utils.secure_files import open_private_append
from ui.config import (
    DEBUG_LOG_FILE,
    LOGGER,
    SAGE_FULL_DEBUG_LOG,
    ACTIVE_DEBUG_QUERY_THREADS,
    DEBUG_CONSOLE_LOCK,
    DEBUG_FILE_LOCK,
)
from ui.question_log import show_log_write_failure


def _redact_debug_text(value, limit: int = 100000):
    """Limit local debug output and obscure commonly leaked credential forms.

    Logs remain sensitive local diagnostics: do not upload them to public sites.
    """
    value = str(value or "")
    value = re.sub(r"(?i)\bsk-(?:proj-)?[a-z0-9_-]{12,}\b", "[REDACTED_API_KEY]", value)
    value = re.sub(r"(?i)\b(bearer\s+)[a-z0-9._~+/-]{16,}", r"\1[REDACTED_TOKEN]", value)
    value = re.sub(
        r"(?im)^\s*(?:KEY|OPENAI_API_KEY|API_KEY|PASSWORD|SECRET)\s*[=:]\s*.+$",
        "[REDACTED_CREDENTIAL_LINE]",
        value,
    )
    return value[:limit] + ("\n[TRUNCATED]" if len(value) > limit else "")


class _ThreadConsoleTee:
    """Forward console writes normally; record only this query's thread."""

    def __init__(self, original, target, owner_thread) -> None:
        """Copy writes from ``owner_thread`` to ``target``; all writes still reach ``original``."""
        self.original, self.target, self.owner_thread = original, target, owner_thread

    def write(self, message):
        """Copy output from the captured thread into the question's buffer; pass everything through."""
        if threading.get_ident() == self.owner_thread:
            self.target.write(message)
        return self.original.write(message)

    def flush(self):
        """Flush the real console stream."""
        return self.original.flush()

    def __getattr__(self, name):
        """Behave like the original stream for everything else (encoding, isatty ...)."""
        return getattr(self.original, name)


class _QueryLogFilter(logging.Filter):
    """Pass only log records produced by the thread answering this question."""

    def __init__(self, owner_thread) -> None:
        """Keep only records emitted by ``owner_thread``."""
        super().__init__()
        self.owner_thread = owner_thread

    def filter(self, record) -> bool:
        """Keep the record only if it came from the thread being captured."""
        return record.thread == self.owner_thread


_SERVER_DIAGNOSTIC_HANDLER = None


class _ServerDiagnosticHandler(logging.Handler):
    """Persist runtime warnings, coalescing identical Streamlit log repeats."""

    def __init__(self, level=logging.NOTSET) -> None:
        """Remember recent messages so repeats within a second are written once."""
        super().__init__(level)
        self._recent = {}
        self._recent_lock = threading.Lock()

    def emit(self, record) -> None:
        """Append a redacted WARNING+ record to debug.txt (at most once per second).

        Records from threads that are already capturing a question are skipped:
        capture_query_debug writes those itself.
        """
        if record.levelno < logging.WARNING or record.thread in ACTIVE_DEBUG_QUERY_THREADS:
            return
        try:
            message = _redact_debug_text(self.format(record), 12000)
            key = (record.thread, record.name, record.levelno, record.getMessage())
            now = time.monotonic()
            with self._recent_lock:
                if now - self._recent.get(key, -1e12) < 1.0:
                    return
                self._recent[key] = now
                if len(self._recent) > 300:
                    self._recent = {k: t for k, t in self._recent.items() if now - t < 5.0}
            with DEBUG_FILE_LOCK:
                with open_private_append(DEBUG_LOG_FILE) as fp:
                    fp.write('[STREAMLIT RUNTIME] ' + message + '\n')
        except (OSError, ValueError):
            # A diagnostic logger must never crash the application.
            self.handleError(record)


def enable_runtime_debug_logging() -> None:
    """In --debug mode, also retain Streamlit's out-of-query runtime warnings."""
    global _SERVER_DIAGNOSTIC_HANDLER
    if _SERVER_DIAGNOSTIC_HANDLER is not None:
        return
    handler = _ServerDiagnosticHandler(logging.WARNING)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    for logger_name in ('streamlit', 'sage', 'sage_ui'):
        logging.getLogger(logger_name).addHandler(handler)
    _SERVER_DIAGNOSTIC_HANDLER = handler


class capture_query_debug:
    """Capture one question, answers and its console diagnostics locally.

    This mode is opt-in, thread-filtered and output-bounded. The lock protects
    temporary stdout/stderr routing so concurrent diagnostic queries cannot
    replace one another's output streams. Other sessions still see stdout.
    """

    def __init__(self, question) -> None:
        """Prepare an empty console buffer for ``question``; nothing is captured yet."""
        self.question = question
        self.console = io.StringIO()  # this question's copy of console output

    def __enter__(self):
        """Start copying this thread's console output and log records."""
        DEBUG_CONSOLE_LOCK.acquire()
        self.thread = threading.get_ident()
        ACTIVE_DEBUG_QUERY_THREADS.add(self.thread)
        self.stdout_context = redirect_stdout(
            _ThreadConsoleTee(sys.stdout, self.console, self.thread)
        )
        self.stderr_context = redirect_stderr(
            _ThreadConsoleTee(sys.stderr, self.console, self.thread)
        )
        self.handler = logging.StreamHandler(self.console)
        self.handler.setLevel(logging.DEBUG)
        self.handler.addFilter(_QueryLogFilter(self.thread))
        self.handler.setFormatter(
            logging.Formatter('%(asctime)s %(levelname)s %(name)s: ' '%(message)s')
        )
        self.loggers = [logging.getLogger()]
        for name in ('sage', 'sage_ui', 'streamlit'):
            candidate = logging.getLogger(name)
            if not candidate.propagate:
                self.loggers.append(candidate)
        try:
            self.stdout_context.__enter__()
            self.stderr_context.__enter__()
            for logger in self.loggers:
                logger.addHandler(self.handler)
        except Exception:  # noqa: BLE001 - undo every redirection on ANY failure, then re-raise
            self.stderr_context.__exit__(None, None, None)
            self.stdout_context.__exit__(None, None, None)
            ACTIVE_DEBUG_QUERY_THREADS.discard(self.thread)
            DEBUG_CONSOLE_LOCK.release()
            raise
        return self

    def __exit__(self, error_type, error, tb) -> bool:
        """Stop capturing; write one redacted, size-bounded record to debug.txt.

        Never suppresses the question's own exception (returns False).
        """
        try:
            for logger in self.loggers:
                logger.removeHandler(self.handler)
            self.stderr_context.__exit__(error_type, error, tb)
            self.stdout_context.__exit__(error_type, error, tb)
            report = _debug_record(self.question, st.session_state, error_type)
            try:
                if SAGE_FULL_DEBUG_LOG:
                    with DEBUG_FILE_LOCK:
                        with open_private_append(DEBUG_LOG_FILE) as fp:
                            fp.write(report)
            except OSError:
                LOGGER.exception('Cannot write local diagnostic log')
                show_log_write_failure('debug.txt')
        finally:
            ACTIVE_DEBUG_QUERY_THREADS.discard(self.thread)
            DEBUG_CONSOLE_LOCK.release()
        return False


def _debug_record(question, state, error_type) -> str:
    """The redacted, size-bounded diagnostic record for one question.

    Only a question digest, the outcome, a chart audit and a metadata-only
    summary of the tool evidence: never the question, answers, console text,
    raw official data, API responses or keys.
    """
    response = state.get('last_response')
    messages = state.get('messages') or []
    answer = next((m for m in reversed(messages) if m.get('role') == 'assistant'), {})
    evidence = (response or {}).get('tool_results', []) if isinstance(response, dict) else []
    fields = [
        '\n' + '=' * 78,
        'UTC: ' + datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'Question digest: ' + sage_privacy.question_digest(question),
        'Test checkpoint next index: ' + str(state.get('auto_test_running_index', 'manual')),
        'Result: '
        + str(
            answer.get('delivery_status')
            or ('verified' if answer.get('verified') else 'unverified_or_failed')
        ),
        'Displayed In Brief: [omitted for privacy]',
        'Displayed Full Answer: [omitted for privacy]',
        # What is needed to diagnose a graph/question mismatch, without observation rows.
        'Displayed chart audit:\n'
        + json.dumps(_chart_audit(question, answer), ensure_ascii=False, default=str),
        'Draft Answer: [omitted for privacy]',
        'Tool evidence status (no data payloads):\n'
        + json.dumps(
            [_evidence_status(item) for item in evidence if isinstance(item, dict)][:20],
            ensure_ascii=False,
        ),
        'Captured console messages: [omitted for privacy]',
    ]
    if error_type is not None:
        fields.append('Unhandled exception type: ' + error_type.__name__)
    return _redact_debug_text('\n'.join(fields), 60000) + '\n'


def _chart_audit(question, answer) -> dict:
    """Which chart was shown (or why none was), for this question."""
    spec = answer.get('chart_spec') or {}
    context = answer.get('chart_context') or {}
    return {
        'chart_present': bool(answer.get('chart_spec')),
        'chart_kind': context.get('chart_kind'),
        'source_table': context.get('table_id'),
        'chart_title': spec.get('title'),
        'chart_fingerprint': question_chart_key(question, answer.get('chart_spec')),
        'displayed_series': [
            item.get('name') for item in spec.get('series', []) if isinstance(item, dict)
        ],
        'reason_if_absent': answer.get('chart_failure_reason'),
        'question_bound': answer.get('source_question') == question,
    }


def _evidence_status(item) -> dict:
    """Metadata-only status of one tool result (no rows of official data, no keys)."""
    summaries = item.get('series_summary')
    chart = item.get('chart_spec')
    return {
        'table_id': item.get('table_id'),
        'success': item.get('success'),
        'table_complete': item.get('table_complete'),
        'analysis_ready': item.get('analysis_ready'),
        'source_url': item.get('source_url'),
        'table_title': str(item.get('title') or item.get('table_title') or '')[:130],
        'series_summary_count': len(summaries) if isinstance(summaries, list) else None,
        'series_sample': [
            {
                'name': str(row.get('series_name') or '')[:90],
                'id': str(row.get('series_no') or '')[:35],
                'unit': str(row.get('unit') or '')[:35],
                'from': str(row.get('earliest_period') or '')[:16],
                'to': str(row.get('latest_period') or '')[:16],
            }
            for row in summaries[:8]
            if isinstance(row, dict)
        ]
        if isinstance(summaries, list)
        else [],
        'catalog_truncated': item.get('series_catalog_truncated'),
        'chart_kind': str(item.get('chart_kind') or '')[:65],
        'chart_source_verified': item.get('chart_source_verified'),
        'chart_series': [
            str(row.get('name') or '')[:80]
            for row in (chart.get('series') or [])[:8]
            if isinstance(row, dict)
        ]
        if isinstance(chart, dict)
        else [],
    }
