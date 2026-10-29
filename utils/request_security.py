"""Security and input preparation for a Sage question.

The caller supplies the existing sanitiser, injection detector, and response
builder; this module does not change security policy or trust boundaries.

Used by: utils/request_gate.py.
"""

from dataclasses import dataclass
import logging
from typing import Callable, Optional

from utils.errors import SageError

LOG = logging.getLogger("sage.security")
_INVALID_INPUT = "This question could not be processed. Please rephrase it and try again."


def _blocked(question: str, reason: str, exc: ValueError) -> "PreparedRequest":
    """Refuse the request without echoing arbitrary exception text.

    Only messages from Sage's own :class:`~utils.errors.SageError` classes are
    written for users. Any other ``ValueError`` text may contain internals, so
    it goes to the log and the visitor sees a fixed sentence.
    """
    if isinstance(exc, SageError):
        answer = exc.user_message
    else:
        LOG.warning("Request rejected (%s): %s", reason, type(exc).__name__)
        answer = _INVALID_INPUT
    return PreparedRequest(
        question=question,
        blocked_response={
            "answer": answer,
            "rag_results": [],
            "tool_results": [],
            "security_blocked": True,
            "security_reason": reason,
            "analytics_executed": False,
        },
    )


@dataclass(frozen=True)
class PreparedRequest:
    """A cleaned request, or the ready-made response if it was blocked."""

    question: str
    combined_input: Optional[str] = None
    blocked_response: Optional[dict] = None


def prepare_request(
    question: str,
    *,
    chat_history,
    combined_user_input,
    sanitize: Callable,
    is_injection: Callable,
    security_response: Callable,
    combine: Callable,
    on_injection: Optional[Callable] = None,
) -> PreparedRequest:
    """Apply the original three ordered gates and return either data or a block."""
    try:
        clean = sanitize(question)
    except ValueError as exc:
        return _blocked("", "invalid_input", exc)

    if is_injection(clean):
        if on_injection is not None:
            on_injection()
        blocked = security_response("prompt_or_code_injection")
        blocked["analytics_executed"] = False
        return PreparedRequest(question=clean, blocked_response=blocked)

    try:
        combined = combine(
            clean, chat_history=chat_history, combined_user_input=combined_user_input
        )
    except ValueError as exc:
        return _blocked(clean, "invalid_combined_input", exc)
    return PreparedRequest(question=clean, combined_input=combined)
