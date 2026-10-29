"""Request-scoped OpenAI API token usage, without logging prompts or credentials.

The SDK `usage` response is authoritative; tiktoken is optional and only
provides estimates. Tracking is isolated with contextvars for concurrent users.

Used by: app.py, ui/answer_pipeline.py, utils/core_config.py,
utils/core_secure.py, utils/kb.py, utils/model_client.py,
utils/rag_engine.py, tools/characterise_process_question.py.
Tested in: tests/test_semantic_search_key.py.
"""

from contextlib import contextmanager
from utils.request_limits import model_call_slot
from contextvars import ContextVar
from dataclasses import dataclass, field
import logging
import os
import inspect
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.settings import env_opt_in

_ACTIVE = ContextVar("sage_request_token_usage", default=None)
_OPERATION = ContextVar("sage_api_operation", default=None)


def _unlimited_allowed() -> bool:
    """A spending guard may be switched off (0) only with this explicit second setting."""
    return env_opt_in("SAGE_ALLOW_UNLIMITED")


def _limit(name, default, high):
    """Read a budget; 0/negative means "off" only when SAGE_ALLOW_UNLIMITED=1."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return 0 if _unlimited_allowed() else default
    return min(high, value)


def _question_call_limit():
    """Model-call attempts per question. Limits count attempts, not only billed responses."""
    return _limit("SAGE_MAX_MODEL_CALLS_PER_QUESTION", 12, 100)


def _question_token_limit():
    """SDK-reported total-token ceiling for one question."""
    return _limit("SAGE_MAX_TOKENS_PER_QUESTION", 75000, 2_000_000)


def _question_seconds_limit():
    """Wall-clock seconds one question may keep starting new model calls."""
    return _limit("SAGE_MAX_SECONDS_PER_QUESTION", 120, 1800)


FINAL_ANSWER_GRACE_SECONDS = 60  # time allowed after the soft limit for the final answer
TIMING = logging.getLogger("sage.timing")


def question_time_exhausted() -> bool:
    """True once this question passed SAGE_MAX_SECONDS_PER_QUESTION (the soft limit).

    The agent then starts no new analysis round or tool and composes its answer
    from what it already gathered (that final AI call is still allowed).
    """
    report = _ACTIVE.get()
    if report is None:
        return False
    max_seconds = getattr(report, "max_seconds", 0)
    started = getattr(report, "started", None)
    return bool(max_seconds and started is not None and time.monotonic() - started >= max_seconds)


def question_elapsed_seconds():
    """Seconds since the current question started (0 outside a question)."""
    report = _ACTIVE.get()
    started = getattr(report, "started", None) if report is not None else None
    return 0.0 if started is None else time.monotonic() - started


def _daily_token_limit():
    """Process-wide reported tokens per Singapore calendar day (all users together)."""
    return _limit("SAGE_DAILY_TOKEN_LIMIT", 5_000_000, 1_000_000_000)


class _DailyBudget:
    """Process-wide daily token ceiling: per-question limits alone cannot bound the bill.

    Counts SDK-reported tokens and resets at midnight Singapore time. Per process:
    with several workers, the real ceiling is workers x limit (set a hard monthly
    limit in the OpenAI dashboard as the final backstop).
    """

    _SGT = timezone(timedelta(hours=8))

    def __init__(self) -> None:
        """Start with nothing used; the count resets when the date changes."""
        self._lock = threading.Lock()
        self._day = None
        self._used = 0

    def _today(self):
        """Today's date in Singapore time (the daily budget resets at local midnight)."""
        return datetime.now(self._SGT).date()

    def check(self) -> None:
        """Raise DailyTokenBudgetExceeded once today's token ceiling (SAGE_DAILY_TOKEN_LIMIT) is
        reached."""
        limit = _daily_token_limit()
        if not limit:
            return
        with self._lock:
            if self._day != self._today():
                self._day, self._used = self._today(), 0
            if self._used >= limit:
                raise DailyTokenBudgetExceeded(
                    f"daily token limit reached ({self._used} of {limit})"
                )

    def add(self, tokens) -> None:
        """Add ``tokens`` to today's total (starting a new day when the date changed)."""
        if tokens <= 0:
            return
        with self._lock:
            if self._day != self._today():
                self._day, self._used = self._today(), 0
            self._used += tokens

    def used_today(self):
        """Tokens used so far today in this process."""
        with self._lock:
            return self._used if self._day == self._today() else 0


DAILY_BUDGET = _DailyBudget()


from utils.errors import (  # noqa: F401 - re-exported
    DailyTokenBudgetExceeded,
    QuestionModelBudgetExceeded,
    QuestionTimeBudgetExceeded,
    QuestionTokenBudgetExceeded,
)


def budget_setting_for(exc) -> tuple:
    """(environment variable, current value) behind a per-question budget stop.

    Both are read from the environment (.env) at the start of every question; 0 disables
    the guard. They exist so operators can raise the limit for large multi-table questions.
    """
    if isinstance(exc, QuestionTokenBudgetExceeded):
        return "SAGE_MAX_TOKENS_PER_QUESTION", _question_token_limit()
    if isinstance(exc, QuestionTimeBudgetExceeded):
        return "SAGE_MAX_SECONDS_PER_QUESTION", _question_seconds_limit()
    return "SAGE_MAX_MODEL_CALLS_PER_QUESTION", _question_call_limit()


@contextmanager
def api_operation(stage, purpose):
    """Explicit request-scoped operation label; never stores prompts or credentials."""
    if (
        not isinstance(stage, str)
        or not isinstance(purpose, str)
        or not stage.strip()
        or not purpose.strip()
    ):
        raise ValueError("API operation requires non-empty stage and purpose")
    token = _OPERATION.set((stage[:80], purpose[:220]))
    try:
        yield
    finally:
        _OPERATION.reset(token)


def refine_last_api_operation(stage=None, purpose=None) -> bool:
    """Refine the most recent call label after Sage sees the model decision.

    This stores only a short operational description.  It never records prompts,
    model output text, credentials or full tool arguments.
    """
    report = _ACTIVE.get()
    if report is None or not report.calls:
        return False
    entry = report.calls[-1]
    if isinstance(stage, str) and stage.strip():
        entry["stage"] = stage.strip()[:80]
    if isinstance(purpose, str) and purpose.strip():
        entry["purpose"] = purpose.strip()[:500]
    return True


def _get(obj: Any, name: str, default=None):
    """Field ``name`` of an SDK object or dict (the OpenAI SDK returns either)."""
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _count(obj: Any, name: str) -> int:
    """A non-negative token count from a usage object; anything else counts as 0."""
    value = _get(obj, name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _call_origin():
    """Find the originating Sage function without recording prompt arguments."""
    frame = inspect.currentframe()
    try:
        cursor = frame.f_back if frame is not None else None
        while cursor is not None:
            name = Path(cursor.f_code.co_filename).name
            if name.startswith('sage_') and name not in {
                'utils.token_usage.py',
                'utils.model_client.py',
            }:
                return f"{name}:{cursor.f_lineno} — {cursor.f_code.co_name}"
            cursor = cursor.f_back
        return "Unknown call site"
    finally:
        del frame


def _call_purpose(endpoint, origin) -> str:
    """A plain-language purpose for an unlabelled call, guessed from its endpoint and caller."""
    if endpoint == 'embeddings':
        return 'Convert text into vectors for semantic retrieval'
    if 'discovery' in origin.lower():
        return 'Find the most relevant official data source'
    if 'simplif' in origin.lower():
        return 'Simplify the verified analytical answer'
    if 'verify' in origin.lower() or 'validat' in origin.lower():
        return 'Review the answer against verified evidence'
    return 'Generate or refine an answer using official table evidence'


@dataclass
class UsageReport:
    """Usage for one question only, including all model/embedding calls."""

    calls: list = field(default_factory=list)
    unknown_calls: int = 0
    attempted_calls: int = 0
    max_calls: int = 0
    max_tokens: int = 0

    def add(
        self,
        response,
        endpoint: str,
        requested_model: str,
        caller: str = "Unknown call site",
        operation=None,
    ) -> None:
        """Record one API response's token usage under its task label (or as unknown if usage is
        missing)."""
        stage, purpose = (
            operation
            if operation is not None
            else ("Unlabelled call", _call_purpose(endpoint, caller))
        )
        usage = _get(response, "usage")
        if usage is None:
            self.unknown_calls += 1
            self.calls.append(
                {
                    "endpoint": endpoint,
                    "model": str(_get(response, "model", requested_model)),
                    "reported": False,
                    "caller": caller,
                    "stage": stage,
                    "purpose": purpose,
                }
            )
            return
        if endpoint == "embeddings":
            input_tokens = _count(usage, "prompt_tokens") or _count(usage, "input_tokens")
            output_tokens = 0
        else:
            input_tokens = _count(usage, "input_tokens") or _count(usage, "prompt_tokens")
            output_tokens = _count(usage, "output_tokens") or _count(usage, "completion_tokens")
        total = _count(usage, "total_tokens") or input_tokens + output_tokens
        details = _get(usage, "input_tokens_details") or _get(usage, "prompt_tokens_details")
        output_details = _get(usage, "output_tokens_details") or _get(
            usage, "completion_tokens_details"
        )
        self.calls.append(
            {
                "endpoint": endpoint,
                "model": str(_get(response, "model", requested_model)),
                "reported": True,
                "caller": caller,
                "stage": stage,
                "purpose": purpose,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": total,
                "cached_input_tokens": _count(details, "cached_tokens"),
                "reasoning_output_tokens": _count(output_details, "reasoning_tokens"),
            }
        )

    def summary(self) -> dict:
        """Return a serialisable, non-sensitive summary for the Streamlit page."""
        reported = [entry for entry in self.calls if entry["reported"]]
        return {
            "calls": [dict(entry) for entry in self.calls],
            "attempted_calls": self.attempted_calls,
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "api_calls": len(self.calls),
            "reported_calls": len(reported),
            "unreported_calls": self.unknown_calls,
            "input_tokens": sum(x["input_tokens"] for x in reported),
            "output_tokens": sum(x["output_tokens"] for x in reported),
            "total_tokens": sum(x["total_tokens"] for x in reported),
            "cached_input_tokens": sum(x["cached_input_tokens"] for x in reported),
            "reasoning_output_tokens": sum(x["reasoning_output_tokens"] for x in reported),
        }


@contextmanager
def usage_scope():
    """Isolate one question's usage; reset even after an API exception."""
    report = UsageReport(max_calls=_question_call_limit(), max_tokens=_question_token_limit())
    report.max_seconds = _question_seconds_limit()
    report.started = time.monotonic()
    token = _ACTIVE.set(report)
    try:
        yield report
    finally:
        _ACTIVE.reset(token)


_REASONING_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _openai_store_enabled() -> bool:
    """Responses API retention. Off by default (PDPA data minimisation).

    The Responses API stores requests and responses by default. Sage sends the
    whole conversation on every call, so it never needs server-side state.
    Set SAGE_OPENAI_STORE=1 only if your organisation has approved retention.
    """
    # Provider-side retention relaxes a privacy safeguard: only the exact value "1".
    return env_opt_in("SAGE_OPENAI_STORE")


def _apply_retention_policy(endpoint, kwargs):
    """Set OpenAI's ``store`` flag from Sage's policy (off unless SAGE_OPENAI_STORE=1) on response
    calls."""
    if endpoint != "responses" or "store" in kwargs:
        return kwargs
    kwargs["store"] = _openai_store_enabled()
    model = str(kwargs.get("model") or "")
    if not kwargs["store"] and model.startswith(_REASONING_MODEL_PREFIXES):
        # Reasoning items fed back as input in the tool loop need their
        # encrypted content when nothing is stored server-side.
        include = list(kwargs.get("include") or [])
        if "reasoning.encrypted_content" not in include:
            include.append("reasoning.encrypted_content")
        kwargs["include"] = include
    return kwargs


class _EndpointProxy:
    """Wraps one OpenAI endpoint so every call is budget-checked, labelled and counted."""

    def __init__(self, raw, endpoint) -> None:
        """Wrap one OpenAI endpoint (``endpoint`` names it in the usage report)."""
        self._raw = raw
        self._endpoint = endpoint

    def create(self, *args, **kwargs):
        """Check budgets, make the API call, then record its token usage for the current question."""
        kwargs = _apply_retention_policy(self._endpoint, kwargs)
        caller = _call_origin()
        DAILY_BUDGET.check()  # raises before any request once today's ceiling is reached
        report = _ACTIVE.get()
        if report is not None:
            max_seconds = getattr(report, "max_seconds", 0)
            started = getattr(report, "started", None)
            # Soft limit (max_seconds): the agent stops starting new rounds/tools and asks
            # for a final answer. Hard limit (+ grace): no AI request at all.
            if (
                max_seconds
                and started is not None
                and time.monotonic() - started >= max_seconds + FINAL_ANSWER_GRACE_SECONDS
            ):
                raise QuestionTimeBudgetExceeded(
                    "Sage reached the time limit for this question; no further AI requests were "
                    "sent."
                )
            if report.max_calls and report.attempted_calls >= report.max_calls:
                raise QuestionModelBudgetExceeded(
                    "Sage reached the API-call safety limit for this question. "
                    "The available evidence must be checked before retrying."
                )
            # Enforce reported usage only. Never mistake cached tokens for free tokens:
            # the API total already includes them, so do not add them twice.
            if report.max_tokens:
                consumed = sum(c.get("total_tokens", 0) for c in report.calls if c.get("reported"))
                if consumed >= report.max_tokens:
                    raise QuestionTokenBudgetExceeded(
                        "Sage reached the reported-token safety limit for this question. "
                        "No further AI requests were sent; inspect the available evidence."
                    )
            report.attempted_calls += 1
        call_started = time.monotonic()
        with model_call_slot():
            response = self._raw.create(*args, **kwargs)
        usage = _get(response, "usage", None)
        DAILY_BUDGET.add(_count(usage, "total_tokens"))
        TIMING.info(
            "model_call endpoint=%s model=%s seconds=%.1f total_tokens=%s operation=%s",
            self._endpoint,
            kwargs.get("model", "unknown"),
            time.monotonic() - call_started,
            _count(usage, "total_tokens"),
            str(_OPERATION.get() or "")[:80],
        )
        report = _ACTIVE.get()
        if report is not None:
            report.add(
                response,
                self._endpoint,
                kwargs.get("model", "unknown"),
                caller=caller,
                operation=_OPERATION.get(),
            )
        return response

    def __getattr__(self, name):
        """Every other attribute comes from the wrapped endpoint unchanged."""
        return getattr(self._raw, name)


class TrackedOpenAI:
    """SDK proxy retaining native Responses/Embeddings APIs and other methods."""

    def __init__(self, client) -> None:
        """Wrap the endpoints Sage uses so every call is counted and budget-checked."""
        self._client = client
        self.responses = _EndpointProxy(client.responses, "responses")
        self.embeddings = _EndpointProxy(client.embeddings, "embeddings")
        self.chat = _ChatProxy(client.chat)

    def with_options(self, **options):
        """Keep tracking, the concurrency slot and retention policy on derived clients.

        Without this, ``client.with_options(...)`` fell through ``__getattr__`` to
        the raw SDK client and bypassed every guard in this module.
        """
        return TrackedOpenAI(self._client.with_options(**options))

    def __getattr__(self, name):
        """Every other attribute comes from the OpenAI client unchanged."""
        return getattr(self._client, name)


class _ChatProxy:
    """Wraps ``client.chat`` so chat.completions calls are counted like the others."""

    def __init__(self, raw) -> None:
        """Wrap ``chat.completions`` so it is counted like the other endpoints."""
        self._raw = raw
        self.completions = _EndpointProxy(raw.completions, "chat.completions")

    def __getattr__(self, name):
        """Every other attribute comes from the wrapped ``chat`` object unchanged."""
        return getattr(self._raw, name)


def estimate_tokens(text, model: str = "gpt-4o-mini"):
    """Optional tiktoken approximation; NEVER use as API-billed usage."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")
    return len(encoding.encode(text))
