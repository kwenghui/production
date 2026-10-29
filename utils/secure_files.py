"""Owner-only, symlink-safe append for local log files.

Sage's logs (debug.txt, questions/failed.txt, questions/success.txt) contain
visitors' questions, which can include personal data. Python's ``open("a")``
creates files with the process umask, which is often world-readable (0644),
and follows symbolic links, so a planted link could redirect writes to an
arbitrary file. This helper creates files as 0600 and refuses symlinks where
the platform supports it.

Used by: ui/answer_view.py, ui/debug_capture.py, ui/question_log.py,
utils/privacy.py.
Tested in: tests/test_security.py.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
PRIVATE_FILE_MODE = 0o600


def open_private_append(path: str | os.PathLike[str], *, encoding: str = "utf-8") -> IO[str]:
    """Open *path* for appending text with owner-only permissions.

    Raises ``OSError`` (including when *path* is a symlink on POSIX), which
    callers already handle as "log could not be written".
    """
    target = Path(path)
    fd = os.open(target, _FLAGS, PRIVATE_FILE_MODE)
    try:
        if os.name == "posix":
            os.fchmod(fd, PRIVATE_FILE_MODE)  # tighten files created by older versions
        return os.fdopen(fd, "a", encoding=encoding, newline="\n")
    except BaseException:
        os.close(fd)
        raise
