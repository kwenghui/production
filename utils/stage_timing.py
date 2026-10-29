"""Opt-in timing of slow request stages, with a stack summary if one hangs.

Set SAGE_STAGE_TIMING=1 before starting Streamlit (the older name
SAGE_PHASE8_DIAGNOSTICS=1 still works). Each wrapped stage then logs its start
and end; while it runs longer than 30 seconds, a summary of the Sage functions
on the waiting thread's stack is logged every 30 seconds.

Privacy: no question text, model output, SQL parameters, tokens or local
variables are ever logged, only stage names, timings and function names.

    with timed_stage("ask_sage"):
        response = core.ask_sage(...)

Used by: ui/answer_pipeline.py.
Tested in: tests/test_access_and_logs.py.
"""

import logging
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager

_LOG = logging.getLogger("sage.stage_timing")
_WATCH_INTERVAL_SECONDS = 30
_MAX_STACK_FRAMES = 32


def _enabled() -> bool:
    """True when stage timing was switched on for this process."""
    return "1" in (
        os.getenv("SAGE_STAGE_TIMING", "").strip(),
        os.getenv("SAGE_PHASE8_DIAGNOSTICS", "").strip(),
    )


def _stack_names(thread_id):
    """'file:function:line' for Sage frames on a thread's stack (innermost first)."""
    frame = sys._current_frames().get(thread_id)
    names = []
    while frame is not None and len(names) < _MAX_STACK_FRAMES:
        code = frame.f_code
        filename = code.co_filename.replace("\\", "/")
        # Only Sage-local frames; never locals, argument values or line text.
        if "/sage/" in filename.lower() or filename.endswith("/app.py"):
            names.append(f"{filename.rsplit('/', 1)[-1]}:{code.co_name}:{frame.f_lineno}")
        frame = frame.f_back
    return " <- ".join(names) if names else "no-sage-frames"


@contextmanager
def timed_stage(name):
    """Log the start, slow progress and end of one named stage (no-op unless enabled)."""
    if not _enabled():
        yield
        return
    ident = uuid.uuid4().hex[:10]
    owner = threading.get_ident()
    start = time.monotonic()
    stopped = threading.Event()
    _LOG.warning("STAGE_START stage=%s id=%s", name, ident)

    def watcher() -> None:
        # One snapshot per interval; the thread ends when the stage completes.
        """Log a stack summary every interval until the stage ends."""
        while not stopped.wait(_WATCH_INTERVAL_SECONDS):
            _LOG.warning(
                "STAGE_WAIT stage=%s id=%s elapsed_s=%.1f stack=%s",
                name,
                ident,
                time.monotonic() - start,
                _stack_names(owner),
            )

    threading.Thread(target=watcher, name="sage-stage-watch", daemon=True).start()
    try:
        yield
    except BaseException:
        _LOG.exception(
            "STAGE_FAILURE stage=%s id=%s elapsed_s=%.1f", name, ident, time.monotonic() - start
        )
        raise
    finally:
        stopped.set()
        _LOG.warning(
            "STAGE_END stage=%s id=%s elapsed_s=%.1f", name, ident, time.monotonic() - start
        )
