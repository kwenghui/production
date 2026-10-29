"""Process-local abuse controls; not a replacement for an edge/WAF limiter.

Two limiters run side by side:

* ``LIMITER`` is keyed by Sage's per-browser-session identifier. It stops one
  tab from flooding the model.
* ``CLIENT_LIMITER`` is keyed by a *pseudonymised* client network address.
  A fresh Streamlit session costs an attacker nothing (open a new tab), so a
  per-session limit alone is trivially bypassed.

Raw IP addresses are personal data under Singapore's PDPA. They are never
stored or logged: :func:`client_fingerprint` keeps only a salted, truncated
HMAC whose salt is random per process.

Used by: ui/answer_pipeline.py, utils/auth.py, utils/core_secure.py,
utils/official_source.py, utils/privacy.py, tools/characterise_page.py,
tools/characterise_page_scenarios.py,
tools/characterise_process_question.py.
Tested in: tests/test_security.py.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from collections import deque
from typing import Callable

from utils.errors import RateLimitExceeded  # noqa: F401 - re-exported

LOG = logging.getLogger("sage.security")
_FINGERPRINT_SALT = secrets.token_bytes(32)
UNKNOWN_CLIENT = "client-unknown"


def bounded_int_env(name: str, default: int, low: int, high: int) -> int:
    """Read an integer setting, clamped; a typo never crashes start-up."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        LOG.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default
    return min(high, max(low, value))


def client_fingerprint(address: str | None) -> str:
    """Pseudonymous, per-process identifier for a client address."""
    if not address:
        return UNKNOWN_CLIENT
    digest = hmac.new(_FINGERPRINT_SALT, str(address).encode("utf-8"), hashlib.sha256)
    return "client-" + digest.hexdigest()[:24]


class SessionRateLimiter:
    """Sliding-window limiter with bounded memory."""

    def __init__(
        self,
        limit: int = 12,
        window_seconds: float = 60,
        max_sessions: int = 5000,
        clock: Callable[[], float] | None = None,
        *,
        min_key_length: int = 16,
    ) -> None:
        """At most ``limit`` requests per key in ``window_seconds``; ``clock`` is for tests."""
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_sessions = max_sessions
        self.min_key_length = min_key_length
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._events: dict[str, deque] = {}

    def check(self, session_id: str) -> None:
        """Count one request; raise RateLimitExceeded over the limit (ValueError for a bad session
        ID)."""
        if not isinstance(session_id, str) or len(session_id) < self.min_key_length:
            raise ValueError("Invalid session identifier")
        now = self.clock()
        with self._lock:
            # Bounded memory, remove idle sessions first.
            if len(self._events) >= self.max_sessions:
                for key, events in list(self._events.items()):
                    if not events or now - events[-1] >= self.window_seconds:
                        del self._events[key]
            events = self._events.get(session_id)
            if events is None:
                if len(self._events) >= self.max_sessions:
                    raise RateLimitExceeded("Service is busy. Please try again shortly.")
                events = deque()
                self._events[session_id] = events
            while events and now - events[0] >= self.window_seconds:
                events.popleft()
            if len(events) >= self.limit:
                raise RateLimitExceeded(
                    "Too many questions. Please wait a minute before trying " "again."
                )
            events.append(now)


LIMITER = SessionRateLimiter(limit=bounded_int_env("SAGE_SESSION_REQUESTS_PER_MINUTE", 12, 1, 100))

# Several people can share one address (office NAT), hence a higher default.
CLIENT_LIMITER = SessionRateLimiter(
    limit=bounded_int_env("SAGE_CLIENT_REQUESTS_PER_MINUTE", 30, 1, 1000),
    min_key_length=len(UNKNOWN_CLIENT),
)
