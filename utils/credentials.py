"""Hold the OpenAI credential in process memory instead of ``os.environ``.

Why: environment variables are inherited by every child process, appear in
``/proc/<pid>/environ`` on Linux, and are routinely captured by crash
reporters and debug dumps. The previous code copied the key into two
environment variables on *every* Streamlit rerun.

The key is never published back to the environment: every consumer
(app.py, utils/core_secure.py, utils/kb.py) reads it with
:func:`get_openai_api_key`.

Used by: ui/answer_view.py, ui/startup.py, utils/core_config.py,
utils/kb.py, tools/check_openai_access.py, tools/generate_question_bank.py.
Tested in: tests/test_semantic_search_key.py.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Mapping

from utils.errors import CredentialError

LOG = logging.getLogger("sage.security")
_ENV_NAMES = ("OPENAI_API_KEY", "KEY")
# Accept current OpenAI formats without pinning one prefix; reject whitespace,
# quotes and obvious placeholders that indicate a broken secrets file.
_KEY_SHAPE = re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$")
_PLACEHOLDERS = ("sk-your", "sk-xxx", "sk-replace", "sk-...", "sk-changeme")

_lock = threading.Lock()
_api_key: str | None = None


def _validated(candidate: object) -> str:
    """The key, trimmed, if it looks like a real OpenAI key; CredentialError otherwise (never
    echoing it)."""
    key = str(candidate or "").strip()
    if not key:
        raise CredentialError("OpenAI API key is not configured")
    if key.lower().startswith(_PLACEHOLDERS) or not _KEY_SHAPE.match(key):
        # Never include the value, not even partially, in the message.
        raise CredentialError("OpenAI API key is malformed")
    return key


def set_openai_api_key(candidate: object) -> None:
    """Validate and keep the OpenAI key in process memory (never in os.environ)."""
    global _api_key
    key = _validated(candidate)
    with _lock:
        _api_key = key


def _current_key() -> str | None:
    """The key held in memory, or None."""
    with _lock:
        return _api_key or None


def get_openai_api_key() -> str | None:
    """The key loaded by :func:`load_openai_api_key`, or a legacy env value."""
    with _lock:
        if _api_key:
            return _api_key
    legacy = os.environ.get("OPENAI_API_KEY") or os.environ.get("KEY")
    return legacy or None


def has_openai_api_key() -> bool:
    """True when a valid OpenAI key is loaded."""
    return bool(get_openai_api_key())


def _key_from_file() -> str | None:
    """Read the key from a file-based secret (preferred in production).

    Environment variables stay visible in /proc/<pid>/environ and in
    ``docker inspect`` even after os.environ is scrubbed. A file is not:
    * systemd ``LoadCredential=openai_api_key:/etc/sage/openai_api_key``
      (read from $CREDENTIALS_DIRECTORY/openai_api_key), or
    * ``SAGE_OPENAI_API_KEY_FILE``, e.g. /run/secrets/openai_api_key (Docker secrets).
    """
    candidates = []
    explicit = os.environ.get("SAGE_OPENAI_API_KEY_FILE", "").strip()
    if explicit:
        candidates.append(explicit)
    credentials_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if credentials_dir:
        candidates.append(os.path.join(credentials_dir, "openai_api_key"))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read(512).strip()
        except OSError:
            LOG.error("OpenAI key file %s could not be read", os.path.basename(path))
            continue
        if value:
            return value
    return None


def load_openai_api_key(
    secrets: Mapping[str, object] | None = None, *, scrub_environment: bool = True
) -> str | None:
    """Load the key from Streamlit secrets first, then the environment.

    Returns ``None`` when nothing is configured (the UI shows set-up help).
    Raises :class:`CredentialError` when a value exists but is malformed, so a
    broken deployment fails closed instead of sending a bad key upstream.
    """
    sources = key_sources(secrets)
    candidate = None
    if secrets is not None:
        for name in _ENV_NAMES:
            try:
                candidate = secrets.get(name)
            except (FileNotFoundError, KeyError, OSError):
                candidate = None  # no secrets.toml is normal in .env development
            if candidate:
                break
    if not candidate:
        candidate = _key_from_file()
    if not candidate:
        candidate = next((os.environ.get(n) for n in _ENV_NAMES if os.environ.get(n)), None)
    if not candidate:
        return None
    current = _current_key()
    if current is not None:
        # The key is fixed for the life of the process. Replacing it later (for example
        # when Streamlit republishes an older secret into the environment) made answers
        # and semantic search use DIFFERENT keys. Restart Sage after rotating a key.
        _note_sources(sources, key_fingerprint(current))
        if scrub_environment:
            for name in _ENV_NAMES:
                os.environ.pop(name, None)
        return current
    _note_sources(sources, key_fingerprint(str(candidate).strip()))
    set_openai_api_key(candidate)
    if scrub_environment:
        for name in _ENV_NAMES:
            os.environ.pop(name, None)
    return get_openai_api_key()


_SOURCE_STATE = {"sources": [], "chosen": None, "warned": set()}


def key_sources(secrets: Mapping[str, object] | None = None) -> list[tuple[str, str]]:
    """Every configured key as (source, fingerprint) -- never the key itself.

    Order matches the order Sage uses: Streamlit secrets, key file, environment
    (which includes .env).
    """
    found = []
    if secrets is not None:
        for name in _ENV_NAMES:
            try:
                value = secrets.get(name)
            except (FileNotFoundError, KeyError, OSError):
                value = None
            if value:
                found.append((f"Streamlit secrets {name}", key_fingerprint(str(value).strip())))
    from_file = _key_from_file()
    if from_file:
        found.append(("key file", key_fingerprint(from_file)))
    for name in _ENV_NAMES:
        value = os.environ.get(name)
        if value:
            found.append((f"environment/.env {name}", key_fingerprint(value.strip())))
    return found


def _note_sources(sources: list[tuple[str, str]], chosen: str) -> None:
    """Remember where keys come from; warn once per combination if they differ."""
    _SOURCE_STATE["sources"], _SOURCE_STATE["chosen"] = list(sources), chosen
    distinct = {fp for _, fp in sources}
    signature = tuple(sources)
    if len(distinct) > 1 and signature not in _SOURCE_STATE["warned"]:
        _SOURCE_STATE["warned"].add(signature)
        LOG.warning(
            "Different OpenAI keys are configured (%s); Sage uses fingerprint %s. "
            "Remove the old key(s) so every part of Sage uses the same one.",
            ", ".join(f"{source}={fp}" for source, fp in sources),
            chosen,
        )


def conflicting_keys_warning() -> str | None:
    """A warning when other, DIFFERENT OpenAI keys are also configured; None otherwise.

    Names only where those keys come from (never the keys or their fingerprints),
    so an operator can remove the stray setting. To compare keys, run
    python tools/check_openai_access.py on the server.
    """
    chosen = _SOURCE_STATE["chosen"] or key_fingerprint()
    others = sorted({source for source, fp in _SOURCE_STATE["sources"] if fp != chosen})
    if not others:
        return None
    return (
        "Other OpenAI keys are also configured (" + "; ".join(others) + "). Sage uses one of "
        "them; remove the others to avoid confusion (check with "
        "python tools/check_openai_access.py)."
    )


def key_fingerprint(key: str | None = None) -> str:
    """Short, non-reversible identifier of a key (first 8 hex of SHA-256).

    Lets an operator check that two places use the SAME key without ever
    showing the key. Returns "none" when no key is loaded.
    """
    import hashlib

    value = key if key is not None else get_openai_api_key()
    if not value:
        return "none"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _reset_for_tests() -> None:
    """Forget the in-memory key (tests only)."""
    global _api_key
    with _lock:
        _api_key = None
