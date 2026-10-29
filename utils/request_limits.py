"""Bound concurrent external model calls per Python worker.

This is a process-level safety guard, not distributed per-user rate limiting.

Used by: utils/token_usage.py.
"""

import os
import threading
from contextlib import contextmanager


def _integer_env(name, default, low, high):
    """An integer setting, clamped to [low, high]; a typo gives ``default``."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


MAX_CALLS = _integer_env('SAGE_MAX_PARALLEL_MODEL_CALLS', 4, 1, 32)
WAIT_SECONDS = _integer_env('SAGE_MODEL_QUEUE_WAIT_SECONDS', 20, 1, 120)
_GATE = threading.BoundedSemaphore(MAX_CALLS)


from utils.errors import ModelCapacityError  # noqa: F401 - re-exported


@contextmanager
def model_call_slot():
    """Context manager: wait for one of the limited concurrent model-call slots."""
    acquired = _GATE.acquire(timeout=WAIT_SECONDS)
    if not acquired:
        raise ModelCapacityError('Sage is busy processing other questions. Please retry shortly.')
    try:
        yield
    finally:
        _GATE.release()
