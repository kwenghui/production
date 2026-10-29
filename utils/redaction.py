"""Redact diagnostics before they leave the trust boundary.

Exception text from HTTP clients, SQLite and the filesystem can contain local
paths, full request URLs (with query strings), credentials and e-mail
addresses. Tool results are sent to the model provider and can be paraphrased
into the visible answer, and audit lines are written to disk, so diagnostic
fields are scrubbed at those choke points with :func:`redact_result`.

Used by: ui/question_log.py, utils/core_secure.py,
utils/hybrid_discovery.py, tools/check_openai_access.py.
Tested in: tests/test_semantic_search_key.py.
"""

from __future__ import annotations

import re
from typing import Any

# Keys whose string values are diagnostics rather than data.
DIAGNOSTIC_KEYS = frozenset(
    {"error", "errors", "reason", "issues", "failures", "detail", "details", "message", "exception"}
)
_MAX_DEPTH = 6

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-(?:proj-|test-)?[A-Za-z0-9_\-]{12,}"), "[REDACTED_KEY]"),
    # Masked keys in provider errors ("sk-proj-abcd****WXYZ") still reveal 8 characters.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]*\*+[A-Za-z0-9_\-]*"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}"), r"\1 [REDACTED_TOKEN]"),
    (
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)\s*[=:]\s*\S+"),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),  # user:pass@host
    (re.compile(r"(?i)(https?://[^\s?#'\"]+)\?[^\s#'\"]*"), r"\1?[query removed]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?i)\b[a-z]:\\(?:[^\\\s'\"]+\\)*[^\\\s'\"]*"), "[PATH]"),  # C:\...
    (
        re.compile(
            r"(?<![\w/:.])/(?:home|Users|srv|opt|var|tmp|usr|etc|mnt|root|app)" r"(?:/[^\s'\":]+)+"
        ),
        "[PATH]",
    ),
)


def redact_diagnostic(text: Any, limit: int = 400) -> str:
    """Return *text* with secrets, paths, URL queries and e-mails removed."""
    value = str(text or "")
    for pattern, replacement in _RULES:
        value = pattern.sub(replacement, value)
    return value if len(value) <= limit else value[:limit] + "…"


def redact_result(value: Any, *, _key: str | None = None, _depth: int = 0) -> Any:
    """Copy of *value* with diagnostic string fields redacted (recursively)."""
    if _depth > _MAX_DEPTH:
        return value
    if isinstance(value, dict):
        return {
            k: redact_result(v, _key=str(k).lower(), _depth=_depth + 1) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [redact_result(v, _key=_key, _depth=_depth + 1) for v in value]
        return type(value)(items) if isinstance(value, tuple) else items
    if isinstance(value, str) and _key in DIAGNOSTIC_KEYS:
        return redact_diagnostic(value)
    return value
