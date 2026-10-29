"""SingStat rate limits: a busy answer (HTTP 429/503) is retried politely, a bounded number of times."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils import official_source  # noqa: E402


class FakeResponse:
    def __init__(self, status, retry_after=None):
        self.status_code, self.headers, self.closed = status, {}, False
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after

    def close(self):
        self.closed = True


class PoliteRetry(unittest.TestCase):
    def run_with(self, statuses):
        replies = [FakeResponse(*s) if isinstance(s, tuple) else FakeResponse(s) for s in statuses]
        session = mock.Mock()
        session.get.side_effect = replies
        with (
            mock.patch.object(official_source, "_http_session", return_value=session),
            mock.patch.object(official_source.time, "sleep") as sleep,
        ):
            final = official_source._get_with_polite_retry("https://x", {}, 30)
        return final, session.get.call_count, [c.args[0] for c in sleep.call_args_list], replies

    def test_busy_then_ok(self):
        final, calls, waits, replies = self.run_with([(429, "3"), 200])
        self.assertEqual((final.status_code, calls, waits), (200, 2, [3.0]))
        self.assertTrue(replies[0].closed)

    def test_gives_up_after_two_retries_and_caps_the_wait(self):
        final, calls, waits, _ = self.run_with([(503, "120"), (503, "x"), 503])
        self.assertEqual((final.status_code, calls), (503, 3))
        self.assertEqual(waits, [8.0, 2.0])

    def test_other_answers_are_not_retried(self):
        final, calls, waits, _ = self.run_with([404])
        self.assertEqual((final.status_code, calls, waits), (404, 1, []))


if __name__ == "__main__":
    unittest.main()
