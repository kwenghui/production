"""Send Sage's own loggers to stdout, once per process.

Without a handler, Python drops INFO records: the ``sage.audit`` trail (sign-in,
lock-out, sign-out) and ``sage.security`` notices were never written anywhere.
Hosting platforms (e.g. Streamlit Community Cloud) collect stdout, so that is the sink.

Each line is a single JSON object: easy to query in Log Analytics
(``AppServiceConsoleLogs``). No question text or credentials are logged by
these loggers; see utils/privacy.py and utils/redaction.py.

Used by: app.py.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_CONFIGURED = False
SAGE_LOGGERS = ("sage", "sage_ui")  # "sage.audit", "sage.security", ... are children


class JsonLineFormatter(logging.Formatter):
    """Format each log record as one JSON line (time, level, logger, message)."""

    def format(self, record: logging.LogRecord) -> str:
        """The record as one JSON line (time, level, logger, message, and any exception)."""
        entry = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(stream=None) -> bool:
    """Attach one stdout handler to Sage's loggers. Safe to call on every rerun."""
    global _CONFIGURED
    with _LOCK:
        if _CONFIGURED:
            return False
        level = getattr(
            logging, os.environ.get("SAGE_LOG_LEVEL", "INFO").strip().upper(), logging.INFO
        )
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(JsonLineFormatter())
        handler.set_name("sage-stdout")
        for name in SAGE_LOGGERS:
            logger = logging.getLogger(name)
            logger.setLevel(level)
            if not any(h.get_name() == "sage-stdout" for h in logger.handlers):
                logger.addHandler(handler)
            logger.propagate = False  # no duplicates through the root logger
        _CONFIGURED = True
        return True
