"""Sign-in flows (password and single sign-on), outcome logs and opt-in diagnostics.

Streamlit is replaced by a small recorder, so the real sign-in and logging code
runs without a browser.
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)

from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
from utils import auth  # noqa: E402
from utils.errors import AccountLockedError, AuthenticationError, ConfigurationError  # noqa: E402

PASSWORD, ADMIN_PASSWORD = "correct horse battery", "admin staple battery"
HASH = auth.hash_password(PASSWORD, n=2**14)  # the weakest setting Sage accepts
ADMIN_HASH = auth.hash_password(ADMIN_PASSWORD, n=2**14)


class Stop(Exception):
    """Raised by the fake st.stop()."""


class FakeStreamlit:
    """Just enough of Streamlit for the sign-in pages."""

    def __init__(self, *, submit=False, typed="", user=None):
        self.session_state, self.calls, self._submit, self._typed = {}, [], submit, typed
        self.context, self.user = None, user
        self.logins = []

    def columns(self, spec):
        return [self] * (spec if isinstance(spec, int) else len(spec))

    def form(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def text_input(self, *a, **k):
        return self._typed

    def form_submit_button(self, *a, **k):
        return self._submit

    def button(self, *a, **k):
        return self._submit

    def login(self, *args):
        self.logins.append(args)

    def stop(self):
        raise Stop()

    def rerun(self):
        self.calls.append("rerun")

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append((name, a[0] if a else ""))


def gate(**env):
    """A password gate with its own throttle (no shared state between tests)."""
    return auth.PasswordGate(env={"SAGE_PASSWORD_HASH": HASH, **env}, throttle=auth.LoginThrottle())


class PasswordSignInTests(unittest.TestCase):
    def test_right_password_signs_in_as_user(self):
        st = FakeStreamlit(submit=True, typed=PASSWORD)
        with self.assertRaises(Stop):
            auth.require_password(st, gate(), client="c1")
        self.assertIn("rerun", st.calls)
        self.assertTrue(gate().is_authenticated(st.session_state))
        self.assertEqual(st.session_state[auth.AUTH_ROLE_KEY], auth.ROLE_USER)

    def test_wrong_password_shows_an_error_and_stays_out(self):
        st = FakeStreamlit(submit=True, typed="wrong password!!")
        with self.assertRaises(Stop):
            auth.require_password(st, gate(), client="c2")
        self.assertTrue(any(c[0] == "error" for c in st.calls if isinstance(c, tuple)))
        self.assertNotIn(auth.AUTH_KEY, st.session_state)

    def test_admin_password_gives_the_admin_role(self):
        g = gate(SAGE_ADMIN_PASSWORD_HASH=ADMIN_HASH)
        state = {}
        g.attempt(state, ADMIN_PASSWORD, "c3")
        self.assertEqual(state[auth.AUTH_ROLE_KEY], auth.ROLE_ADMIN)

    def _clocked_gate(self, now, **env):
        return auth.PasswordGate(
            env={"SAGE_PASSWORD_HASH": HASH, **env},
            throttle=auth.LoginThrottle(),
            clock=lambda: now[0],
        )

    def test_idle_sessions_lock_and_keep_the_work(self):
        now = [1000.0]
        g = self._clocked_gate(now)
        state = {}
        g.attempt(state, PASSWORD, "c4")
        state["messages"] = ["an answer the visitor was cross-checking"]
        now[0] += 61 * 60  # default idle limit is 60 minutes
        self.assertFalse(g.is_authenticated(state))
        self.assertTrue(g.is_locked(state))
        self.assertEqual(state["messages"], ["an answer the visitor was cross-checking"])
        g.attempt(state, PASSWORD, "c4")  # same password: continue where they left off
        self.assertTrue(g.is_authenticated(state))
        self.assertEqual(state["messages"], ["an answer the visitor was cross-checking"])
        self.assertFalse(g.is_locked(state))

    def test_a_different_password_never_inherits_a_locked_session(self):
        now = [1000.0]
        g = self._clocked_gate(now, SAGE_ADMIN_PASSWORD_HASH=ADMIN_HASH)
        state = {}
        g.attempt(state, ADMIN_PASSWORD, "c5")
        state["messages"] = ["admin-only diagnostics"]
        now[0] += 61 * 60
        self.assertFalse(g.is_authenticated(state))
        g.attempt(state, PASSWORD, "c5")  # general password after an admin session
        self.assertNotIn("messages", state)
        self.assertEqual(state[auth.AUTH_ROLE_KEY], auth.ROLE_USER)

    def test_the_absolute_limit_still_signs_out_and_drops_results(self):
        now = [1000.0]
        g = self._clocked_gate(now)
        state = {}
        g.attempt(state, PASSWORD, "c6")
        for _ in range(9):  # active every 59 minutes, past the 8-hour limit
            now[0] += 59 * 60
            g.is_authenticated(state)
        self.assertFalse(g.is_authenticated(state))
        self.assertNotIn(auth.AUTH_KEY, state)

    def test_repeated_failures_lock_the_client(self):
        g = gate()
        with self.assertRaises((AuthenticationError, AccountLockedError)):
            for _ in range(20):
                try:
                    g.attempt({}, "wrong password!!", "c5")
                except AuthenticationError:
                    continue
        with self.assertRaises(AccountLockedError):
            g.attempt({}, PASSWORD, "c5")

    def test_plaintext_password_setting_is_refused(self):
        st = FakeStreamlit()
        g = auth.PasswordGate(env={"Sage_Password": "x"}, throttle=auth.LoginThrottle())
        with self.assertRaises(Stop):
            auth.require_password(st, g, client="c6")
        with self.assertRaises(ConfigurationError):
            g.verifier()

    def test_gate_switched_off_lets_everyone_in(self):
        auth.require_password(FakeStreamlit(), gate(Need_Password="0"), client="c7")


class SingleSignOnTests(unittest.TestCase):
    def sso(self, **env):
        return auth.SsoGate(env={"SAGE_OIDC_ALLOWED_DOMAINS": "agency.gov.sg", **env})

    def test_verified_allowed_email_is_accepted(self):
        user = {"email": "Ana@Agency.gov.sg", "email_verified": True}
        self.assertEqual(self.sso().authorise(user), "ana@agency.gov.sg")

    def test_unverified_or_outside_emails_are_refused(self):
        for user in (
            {"email": "ana@agency.gov.sg", "email_verified": False},
            {"email": "ana@agency.gov.sg"},
            {"email": "eve@elsewhere.com", "email_verified": True},
            {"email_verified": True},
        ):
            with self.subTest(user=user), self.assertRaises(AuthenticationError):
                self.sso().authorise(user)

    def test_no_allow_list_is_a_configuration_error(self):
        with self.assertRaises(ConfigurationError):
            auth.SsoGate(env={}).check_configuration()

    def test_signed_out_user_sees_the_sign_in_button(self):
        st = FakeStreamlit(submit=True, user={"is_logged_in": False})
        with self.assertRaises(Stop):
            auth.require_sso(st, self.sso())
        self.assertEqual(st.logins, [()])

    def test_signed_in_allowed_user_passes(self):
        st = FakeStreamlit(
            user={"is_logged_in": True, "email": "ana@agency.gov.sg", "email_verified": True}
        )
        auth.require_sso(st, self.sso())
        self.assertTrue(st.session_state.get(auth.AUTH_KEY))


class OutcomeLogTests(unittest.TestCase):
    """failed.txt / success.txt / debug.txt lines: written safely, no question text in clear."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from ui import question_log

        self.log = question_log
        folder = Path(self.tmp.name)
        for name, value in (
            ("FAILED_QUESTIONS_FILE", folder / "failed.txt"),
            ("SUCCESS_QUESTIONS_FILE", folder / "success.txt"),
            ("DEBUG_LOG_FILE", folder / "debug.txt"),
        ):
            patcher = mock.patch.object(question_log, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_failed_and_successful_questions_are_recorded(self):
        self.assertTrue(self.log.log_failed_question("How many unicorns?", "no_supported_answer"))
        self.assertTrue(self.log.log_successful_question("How many residents?", status="verified"))
        failed = (Path(self.tmp.name) / "failed.txt").read_text(encoding="utf-8")
        self.assertIn("no_supported_answer", failed)

    def test_failure_categories(self):
        self.assertEqual(
            self.log.failure_category(
                {"security_blocked": True},
                verified=False,
                numeric_blocked=False,
                no_supported_answer=False,
            ),
            "security_blocked",
        )
        self.assertIsNone(
            self.log.failure_category(
                {"tool_results": [{}]},
                verified=True,
                numeric_blocked=False,
                no_supported_answer=False,
            )
        )

    def test_answer_decision_is_one_json_line(self):
        answer = {"role": "assistant", "content": "x", "verified": True, "chart_spec": None}
        self.log.log_answer_decision("How many residents?", answer, {"tool_results": []})
        line = (
            (Path(self.tmp.name) / "debug.txt").read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        self.assertIsInstance(json.loads(line[line.index("{") :]), dict)


class StageTimingTests(unittest.TestCase):
    def test_stage_timing_is_silent_unless_enabled_and_never_changes_results(self):
        from utils.stage_timing import timed_stage

        with timed_stage("quiet"):
            value = 1
        with (
            mock.patch.dict(os.environ, {"SAGE_STAGE_TIMING": "1"}),
            self.assertLogs("sage.stage_timing", level="WARNING") as logs,
        ):
            with timed_stage("loud"):
                value += 1
        self.assertEqual(value, 2)
        self.assertTrue(any("STAGE_END stage=loud" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
