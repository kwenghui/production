"""Sage's single exception hierarchy.

Design rules
------------
1. Every exception Sage raises deliberately derives from :class:`SageError`.
   ``except SageError`` therefore means "an anticipated failure" and anything
   else reaching a UI boundary is a genuine defect.
2. Each class carries a *user_message* that is safe to show to an anonymous
   visitor. ``str(exc)`` is the *internal* message and may contain table IDs,
   URLs or other diagnostics; it must only ever go to the server log.
3. Classes that historically subclassed a built-in (``ValueError``,
   ``RuntimeError``) keep that base through multiple inheritance, so existing
   ``except ValueError`` handlers keep working unchanged.
4. This module imports nothing from Sage or Streamlit, so any module can import
   it without creating an import cycle.

Used by: app.py, ui/answer_pipeline.py, ui/core_loader.py,
ui/question_log.py, utils/auth.py, utils/core_secure.py,
utils/credentials.py, utils/input_security.py, utils/intent_resolution.py,
utils/kb.py, utils/request_limits.py, utils/request_security.py,
utils/session_security.py, utils/store.py, utils/table_download.py,
utils/token_usage.py, tools/characterise_process_question.py.
Tested in: tests/test_access_and_logs.py, tests/test_core_modules.py,
tests/test_security.py, tests/test_semantic_search_key.py,
tests/test_storage_and_tools.py.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, ClassVar

__all__ = [
    "SageError",
    "ConfigurationError",
    "CredentialError",
    "SecurityError",
    "AuthenticationError",
    "AccountLockedError",
    "InputRejectedError",
    "PromptInjectionDetected",
    "CapacityError",
    "RateLimitExceeded",
    "ModelCapacityError",
    "BudgetExceededError",
    "QuestionTokenBudgetExceeded",
    "QuestionModelBudgetExceeded",
    "DataIntegrityError",
    "CalculationError",
    "PlanError",
    "UnverifiablePaginationError",
    "IntentResolutionError",
    "UpstreamServiceError",
    "StorageError",
    "PARSE_ERRORS",
    "new_incident_id",
    "log_unexpected",
    "user_message_for",
]

GENERIC_USER_MESSAGE = "Something went wrong while handling your request. Please try again."


class SageError(Exception):
    """Base class for every anticipated Sage failure.

    ``user_message`` is safe to display. ``code`` is a stable, machine-readable
    identifier for logs and metrics. ``retryable`` tells the UI whether
    suggesting "try again" makes sense. ``log_level`` lets boundaries log
    anticipated errors quietly and defects loudly.
    """

    default_user_message: ClassVar[str] = GENERIC_USER_MESSAGE
    code: ClassVar[str] = "sage_error"
    retryable: ClassVar[bool] = False
    log_level: ClassVar[int] = logging.WARNING

    def __init__(
        self,
        message: str = "",
        *,
        user_message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Internal ``message`` for logs; ``user_message`` is the safe text shown on the page."""
        super().__init__(message or self.default_user_message)
        self.user_message = user_message or self.default_user_message
        # Structured, non-secret context for logs. Never put user text,
        # credentials or raw upstream payloads in here.
        self.details = dict(details or {})

    def __repr__(self) -> str:  # concise, no user_message duplication
        """Class, code and internal message (the user message is not repeated)."""
        return f"{type(self).__name__}(code={self.code!r}, message={str(self)!r})"


# --- Configuration ------------------------------------------------------------
class ConfigurationError(SageError):
    """The deployment is mis-configured; the app must fail closed."""

    code = "configuration_error"
    log_level = logging.ERROR
    default_user_message = (
        "Sage is not configured correctly. " "Please contact the service administrator."
    )


class CredentialError(ConfigurationError):
    """A required credential is missing or malformed."""

    code = "credential_error"


# --- Security -----------------------------------------------------------------
class SecurityError(SageError):
    """A security control rejected the request."""

    code = "security_error"
    default_user_message = "This request could not be accepted."


class AuthenticationError(SecurityError):
    """The supplied credential was wrong. Deliberately non-specific."""

    code = "authentication_failed"
    default_user_message = "Incorrect password."


class AccountLockedError(SecurityError):
    """Too many failed attempts; the caller must wait."""

    code = "authentication_locked"

    def __init__(self, retry_after_seconds: float, message: str = "") -> None:
        """Locked for ``retry_after_seconds`` (rounded up, at least 1 second)."""
        self.retry_after_seconds = max(1, int(retry_after_seconds) + 1)
        super().__init__(
            message or f"locked for {self.retry_after_seconds}s",
            user_message=(
                "Too many incorrect attempts. Please wait " f"{self.retry_after_seconds} seconds."
            ),
        )


class InputRejectedError(SecurityError, ValueError):
    """User input failed validation. ``user_message`` explains why, safely."""

    code = "invalid_input"

    def __init__(self, message: str, *, user_message: str | None = None, **kwargs: Any) -> None:
        # Validation messages are written by us for the user, so by default
        # the internal and user-facing message are the same text.
        """The validation message is written for the user, so it is also shown by default."""
        super().__init__(message, user_message=user_message or message, **kwargs)


class PromptInjectionDetected(SecurityError):
    """Input matched a prompt/code-injection pattern."""

    code = "prompt_or_code_injection"


# --- Capacity and budgets -----------------------------------------------------
class CapacityError(SageError, RuntimeError):
    """The service is temporarily saturated; retrying later may succeed."""

    code = "capacity"
    retryable = True
    log_level = logging.INFO
    default_user_message = "Sage is busy. Please try again shortly."

    def __init__(self, message: str = "", **kwargs: Any) -> None:
        # Historical call sites pass a user-facing sentence as the message.
        """Older call sites pass the user-facing sentence as ``message``."""
        kwargs.setdefault("user_message", message or None)
        super().__init__(message, **kwargs)


class RateLimitExceeded(CapacityError):
    """Too many questions from one session or client in the last minute."""

    code = "rate_limited"
    default_user_message = "Too many questions. Please wait a minute before trying again."


class ModelCapacityError(CapacityError):
    """Model request capacity is temporarily exhausted."""

    code = "model_capacity"
    default_user_message = "Sage is busy processing other questions. Please retry shortly."


class BudgetExceededError(SageError, RuntimeError):
    """A question needed more model calls, tokens or time than one request may use."""

    code = "budget_exceeded"
    log_level = logging.INFO
    default_user_message = (
        "This question needed more analysis than Sage allows for "
        "one request. Please try a narrower question."
    )


class QuestionTokenBudgetExceeded(BudgetExceededError):
    """A question exhausted its reported-token budget before another call."""

    code = "token_budget_exceeded"


class DailyTokenBudgetExceeded(BudgetExceededError):
    """The whole service reached its daily token ceiling (SAGE_DAILY_TOKEN_LIMIT)."""

    code = "daily_budget_exceeded"
    log_level = logging.WARNING
    default_user_message = "Sage has reached its daily usage limit. " "Please try again tomorrow."


class QuestionTimeBudgetExceeded(BudgetExceededError):
    """A question ran past SAGE_MAX_SECONDS_PER_QUESTION before another model call."""

    code = "time_budget_exceeded"


class QuestionModelBudgetExceeded(BudgetExceededError):
    """A question reached its configured API-call limit before another request."""

    code = "model_call_budget_exceeded"


# --- Data and evidence ---------------------------------------------------------
class DataIntegrityError(SageError, ValueError):
    """Official data could not be verified; Sage must not present a figure."""

    code = "data_integrity"
    default_user_message = (
        "Sage could not verify the official figures needed for " "this answer, so none are shown."
    )


class CalculationError(DataIntegrityError):
    """Insufficient or incompatible verified statistical evidence."""

    code = "calculation_error"


class PlanError(DataIntegrityError):
    """A proposed statistical plan is invalid or unsupported."""

    code = "plan_error"


class UnverifiablePaginationError(DataIntegrityError):
    """The remote endpoint did not prove complete row coverage.

    Validated *partial* records may be retained in a non-active staging area.
    Their presence never authorises use of the table in an analysis.
    """

    code = "unverifiable_pagination"

    def __init__(
        self,
        message: str,
        *,
        partial: Any = None,
        expected: Any = None,
        authoritative_total: bool = False,
        expected_ids: Any = None,
    ) -> None:
        """Keep the partial download and what was expected, for staging and logs."""
        super().__init__(message)
        self.partial = partial
        self.expected = expected
        self.authoritative_total = authoritative_total
        self.expected_ids = expected_ids


class IntentResolutionError(SageError):
    """Intent classification could not be completed."""

    code = "intent_resolution_failed"
    default_user_message = "The current request could not be classified safely."


# --- Infrastructure -------------------------------------------------------------
class UpstreamServiceError(SageError):
    """A dependency (SingStat, data.gov.sg, model API) failed."""

    code = "upstream_unavailable"
    retryable = True
    default_user_message = (
        "An official data service is temporarily unavailable. " "Please try again later."
    )


class StorageError(SageError, OSError):
    """Local persistence (SQLite, log files) failed."""

    code = "storage_error"
    log_level = logging.ERROR


# Frequently repeated "malformed data" tuple, previously hand-written ~40 times.
PARSE_ERRORS: tuple[type[BaseException], ...] = (ValueError, TypeError, KeyError)


# --- Boundary helpers -------------------------------------------------------------
def new_incident_id() -> str:
    """Short, unguessable reference the user can quote to support staff."""
    return secrets.token_hex(4).upper()


def log_unexpected(logger: logging.Logger, message: str, exc: BaseException) -> str:
    """Log an unanticipated exception with a correlation ID and return the ID.

    The traceback goes to the server log only. Callers show the visitor the ID,
    never ``str(exc)``, which can contain paths, URLs or upstream payloads.
    """
    incident = new_incident_id()
    logger.error(
        "%s [incident=%s type=%s]",
        message,
        incident,
        type(exc).__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return incident


def user_message_for(exc: BaseException, *, incident: str | None = None) -> str:
    """The text that may be shown to a visitor for *exc*."""
    if isinstance(exc, SageError):
        return exc.user_message
    suffix = f" (reference {incident})" if incident else ""
    return GENERIC_USER_MESSAGE + suffix
