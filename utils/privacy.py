"""Privacy-by-default helpers for Sage's local logs (Singapore PDPA).

* :func:`question_digest` pseudonymises a question with a *keyed* HMAC. A plain
  SHA-256 of a short question ("how many people aged 65 in 2024") can be
  reversed by hashing guesses; without the key that is not possible.
* :func:`enforce_retention` deletes question-log entries older than the
  retention period and rotates ``debug.txt`` by size.

The key comes from ``SAGE_LOG_PSEUDONYM_KEY`` or is generated once into
``.sage_log_key`` (owner-only, git-ignored). Keeping it stable lets operators
correlate entries across restarts; deleting it unlinks all past digests.

Used by: ui/answer_view.py, ui/debug_capture.py, ui/question_log.py,
utils/auth.py.
Tested in: tests/test_security.py.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Mapping

from utils.secure_files import PRIVATE_FILE_MODE
from utils.session_security import bounded_int_env
from utils.settings import env_opt_in

LOG = logging.getLogger("sage.privacy")
_KEY_LOCK = threading.Lock()
_KEY: bytes | None = None
_MAINTENANCE_LOCK = threading.Lock()
_LAST_MAINTENANCE = 0.0
MAINTENANCE_INTERVAL_SECONDS = 6 * 3600


def _key_file() -> Path:
    """Where the log pseudonym key is kept (SAGE_LOG_KEY_FILE, default .sage_log_key)."""
    return Path(
        os.environ.get("SAGE_LOG_KEY_FILE")
        or Path(__file__).resolve().parent.parent / ".sage_log_key"
    )


def _load_key() -> bytes:
    """The pseudonym key: from SAGE_LOG_PSEUDONYM_KEY, else the key file (created owner-only)."""
    global _KEY
    with _KEY_LOCK:
        if _KEY is not None:
            return _KEY
        configured = os.environ.get("SAGE_LOG_PSEUDONYM_KEY", "").strip()
        if configured:
            _KEY = hashlib.sha256(configured.encode("utf-8")).digest()
            return _KEY
        path = _key_file()
        try:
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                PRIVATE_FILE_MODE,
            )
        except FileExistsError:
            try:
                read_fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(read_fd, "rb") as handle:
                    raw = handle.read(64)
            except OSError:
                raw = b""
            if len(raw) < 32:
                # Logging must never break a request: fall back, but loudly.
                LOG.error(
                    "%s is unreadable or too short; using a per-process key "
                    "(delete the file to regenerate it)",
                    path.name,
                )
                raw = secrets.token_bytes(32)
            _KEY = raw[:64]
        except OSError:
            LOG.warning("Cannot persist the log pseudonym key; using a per-process key")
            _KEY = secrets.token_bytes(32)
        else:
            raw = secrets.token_bytes(32)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
            _KEY = raw
        return _KEY


def question_digest(question: object, length: int = 20) -> str:
    """Keyed, non-reversible pseudonym for *question* (hex)."""
    normalised = " ".join(str(question or "").split()).lower()
    return hmac.new(_load_key(), normalised.encode("utf-8"), hashlib.sha256).hexdigest()[:length]


def log_manual_question_text() -> bool:
    """Store visitors' own wording only when the operator opts in."""
    # Keeping personal data relaxes a safeguard: only the exact value "1" counts.
    return env_opt_in("SAGE_LOG_MANUAL_QUESTION_TEXT")


# --- Retention -----------------------------------------------------------------
def _line_time(line: str) -> datetime | None:
    """The UTC timestamp at the start of a log line, or None."""
    stamp = line[:20]
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def prune_question_log(path: Path, max_age_days: int, *, now: datetime | None = None) -> int:
    """Remove timestamped lines older than *max_age_days*; return lines removed.

    Lines without a recognisable timestamp are kept (never silently lose data
    whose age is unknown). The file is replaced atomically, owner-only.
    """
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    kept = [line for line in lines if (_line_time(line) or cutoff) >= cutoff]
    removed = len(lines) - len(kept)
    if removed:
        _replace_private(path, kept)
    return removed


_JSON_TIMESTAMP = re.compile(r'"timestamp_utc"\s*:\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)')


def _debug_line_time(line: str) -> datetime | None:
    """The timestamp of a debug-log line (leading stamp or JSON ``timestamp_utc``), or None."""
    found = _line_time(line)
    if found is not None:
        return found
    match = _JSON_TIMESTAMP.search(line)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def prune_debug_log(path: Path, max_age_days: int, *, now: datetime | None = None) -> int:
    """Drop the part of an append-only debug log that is older than the cut-off.

    debug.txt is written in time order and some entries span several lines, so
    everything before the first entry dated within the retention period is
    removed (undated continuation lines go with the entry they belong to). A
    file with no dated entry is left alone. Returns the number of lines removed.
    """
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    first_kept, saw_old = None, False
    for index, line in enumerate(lines):
        stamp = _debug_line_time(line)
        if stamp is None:
            continue
        if stamp >= cutoff:
            first_kept = index
            break
        saw_old = True
    if not saw_old:
        return 0
    kept = [] if first_kept is None else lines[first_kept:]
    _replace_private(path, kept)
    return len(lines) - len(kept)


def _replace_private(path: Path, lines: list[str]) -> None:
    """Atomically replace a file with ``lines``, owner-only, never following symlinks."""
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        PRIVATE_FILE_MODE,
    )
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.writelines(lines)
    os.replace(temporary, path)


def rotate_by_size(
    path: Path, max_bytes: int, max_age_days: int, *, now: float | None = None
) -> bool:
    """Rotate *path* to ``<name>.1`` above *max_bytes*; expire old rotations."""
    path, now = Path(path), (time.time() if now is None else now)
    rotated = path.with_name(path.name + ".1")
    if rotated.is_file() and now - rotated.stat().st_mtime > max_age_days * 86400:
        rotated.unlink()
    if path.is_file() and not path.is_symlink() and path.stat().st_size > max_bytes:
        os.replace(path, rotated)  # previous rotation is overwritten (one generation)
        return True
    return False


def enforce_retention(
    app_dir: Path,
    *,
    debug_log: Path | None = None,
    force: bool = False,
    locks: Mapping[str, Any] | None = None,
) -> dict:
    """Apply retention to Sage's local logs, at most every few hours per process.

    *locks* maps a log name ("questions/failed.txt", "questions/success.txt",
    "debug.txt") to the lock its writers hold, so that no line appended while
    a file is being rewritten is lost.
    """
    locks = locks or {}
    global _LAST_MAINTENANCE
    days = bounded_int_env("SAGE_LOG_RETENTION_DAYS", 30, 0, 3650)
    if days == 0:
        return {"skipped": "retention disabled (SAGE_LOG_RETENTION_DAYS=0)"}
    with _MAINTENANCE_LOCK:
        if (
            not force
            and time.monotonic() - _LAST_MAINTENANCE < MAINTENANCE_INTERVAL_SECONDS
            and _LAST_MAINTENANCE
        ):
            return {"skipped": "recently run"}
        _LAST_MAINTENANCE = time.monotonic()
    app_dir = Path(app_dir)
    report = {}
    question_max_mb = bounded_int_env("SAGE_QUESTION_LOG_MAX_MB", 20, 1, 1024)
    for name in ("questions/failed.txt", "questions/success.txt"):
        try:
            with locks.get(name) or nullcontext():
                report[name] = prune_question_log(app_dir / name, days)
                # Age alone does not bound disk use: also cap the size (disk-fill protection).
                report[name + " rotated"] = rotate_by_size(
                    app_dir / name, question_max_mb * 1024 * 1024, days
                )
        except (OSError, UnicodeError) as error:
            LOG.warning("Retention for %s failed: %s", name, type(error).__name__)
    max_mb = bounded_int_env("SAGE_DEBUG_LOG_MAX_MB", 10, 1, 1024)
    debug_path = Path(debug_log or app_dir / "debug.txt")
    try:
        with locks.get("debug.txt") or nullcontext():
            report["debug.txt pruned"] = prune_debug_log(debug_path, days)
            report["debug.txt rotated"] = rotate_by_size(debug_path, max_mb * 1024 * 1024, days)
    except (OSError, UnicodeError) as error:
        LOG.warning("debug.txt rotation failed: %s", type(error).__name__)
    if any(report.values()):
        LOG.info("Log retention applied (%d days): %s", days, report)
    return report
