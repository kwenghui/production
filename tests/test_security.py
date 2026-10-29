"""Tests for Sage's security controls (see SECURITY.md for the overall model).

Covers sign-in (password hashing, verification, lock-outs, the on/off rule),
client identification behind proxies, rate limiting, the request gate,
privacy of logs and owner-only files. No network, browser or OpenAI key.
"""

import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.auth import (
    LoginThrottle,
    PasswordGate,
    PasswordVerifier,
    auth_mode,  # noqa: E402
    can_view_technical_details,
    client_id_from,
    hash_password,
)
from utils.errors import AccountLockedError, ConfigurationError, RateLimitExceeded  # noqa: E402
from utils.privacy import prune_question_log, question_digest  # noqa: E402
from utils.request_gate import gate_request  # noqa: E402
from utils.secure_files import open_private_append  # noqa: E402
from utils.session_security import (
    UNKNOWN_CLIENT,
    SessionRateLimiter,  # noqa: E402
    bounded_int_env,
)


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Passwords(unittest.TestCase):
    """Passwords are stored as salted scrypt hashes and compared in constant time."""

    @classmethod
    def setUpClass(cls):
        cls.encoded = hash_password("correct horse battery", n=2**14)

    def test_hash_verifies_only_the_right_password(self):
        verifier = PasswordVerifier(encoded_hash=self.encoded)
        self.assertTrue(verifier.verify("correct horse battery"))
        self.assertFalse(verifier.verify("correct horse batterY"))
        self.assertFalse(verifier.verify(""))

    def test_same_password_gets_a_different_salt_each_time(self):
        self.assertNotEqual(self.encoded, hash_password("correct horse battery", n=2**14))

    def test_short_passwords_are_refused(self):
        with self.assertRaises(ValueError):
            hash_password("too short")

    def test_malformed_or_weak_hashes_are_configuration_errors(self):
        for bad in ("not-a-hash", "md5$1$2$3$4$5", "scrypt$1024$8$1$AAAA$AAAA"):
            with self.subTest(bad=bad), self.assertRaises(ConfigurationError):
                PasswordVerifier(encoded_hash=bad)

    def test_exactly_one_password_source_is_required(self):
        with self.assertRaises(ConfigurationError):
            PasswordVerifier()
        with self.assertRaises(ConfigurationError):
            PasswordVerifier(encoded_hash=self.encoded, plaintext="x")


class LoginLockouts(unittest.TestCase):
    """Repeated failures lock a client out, for longer each time."""

    def setUp(self):
        self.clock = FakeClock()
        self.throttle = LoginThrottle(
            max_failures=3, base_lock=30, max_lock=120, global_limit=0, clock=self.clock
        )

    def test_lock_after_max_failures_and_release_after_the_lock(self):
        for _ in range(2):
            self.assertEqual(self.throttle.record_failure("client-a"), 0)
        self.assertEqual(self.throttle.record_failure("client-a"), 30)
        with self.assertRaises(AccountLockedError):
            self.throttle.check("client-a")
        self.throttle.check("client-b")  # other clients are not affected
        self.clock.now += 31
        self.throttle.check("client-a")  # lock expired

    def test_lock_doubles_up_to_the_maximum(self):
        locks = []
        for _ in range(4):
            for _ in range(3):
                applied = self.throttle.record_failure("client-a")
            locks.append(applied)
            self.clock.now += applied + 1
        self.assertEqual(locks, [30, 60, 120, 120])


class SignInPolicy(unittest.TestCase):
    def test_sign_in_is_on_unless_explicitly_switched_off(self):
        for value, expected in (
            (None, True),
            ("", True),
            ("1", True),
            ("typo", True),
            ("0", False),
            ("false", False),
            ("OFF", False),
        ):
            env = {} if value is None else {"Need_Password": value}
            with self.subTest(value=value):
                self.assertEqual(PasswordGate(env=env).enabled(), expected)

    def test_unknown_auth_mode_falls_back_to_password(self):
        self.assertEqual(auth_mode({"SAGE_AUTH_MODE": "oidc"}), "oidc")
        self.assertEqual(auth_mode({"SAGE_AUTH_MODE": "magic"}), "password")
        self.assertEqual(auth_mode({}), "password")

    def test_with_an_admin_password_only_admins_see_technical_details(self):
        env = {"SAGE_ADMIN_PASSWORD_HASH": "configured"}
        self.assertFalse(can_view_technical_details({}, env))
        self.assertTrue(can_view_technical_details({}, {}))


class ClientIdentity(unittest.TestCase):
    """A forwarded-for header is trusted only when the operator says a proxy sets it."""

    class Context:
        def __init__(self, ip=None, headers=None):
            self.ip_address, self.headers = ip, headers or {}

    def test_header_is_ignored_unless_configured(self):
        context = self.Context(ip="10.0.0.1", headers={"X-Forwarded-For": "6.6.6.6"})
        self.assertEqual(
            client_id_from(context, {}), client_id_from(self.Context(ip="10.0.0.1"), {})
        )

    def test_configured_header_uses_the_last_entry_and_drops_the_port(self):
        env = {"SAGE_CLIENT_IP_HEADER": "X-Forwarded-For"}
        forwarded = self.Context(
            ip="10.0.0.1", headers={"X-Forwarded-For": "6.6.6.6, 1.2.3.4:5555"}
        )
        direct = self.Context(ip="1.2.3.4")
        self.assertEqual(client_id_from(forwarded, env), client_id_from(direct, {}))

    def test_identity_is_a_pseudonym_and_unknown_is_shared(self):
        self.assertNotIn("1.2.3.4", client_id_from(self.Context(ip="1.2.3.4"), {}))
        self.assertEqual(client_id_from(self.Context(), {}), UNKNOWN_CLIENT)


class RateLimits(unittest.TestCase):
    def test_limit_per_window_then_recovery(self):
        clock = FakeClock()
        limiter = SessionRateLimiter(limit=2, window_seconds=60, clock=clock)
        key = "s" * 20
        limiter.check(key)
        limiter.check(key)
        with self.assertRaises(RateLimitExceeded):
            limiter.check(key)
        clock.now += 61
        limiter.check(key)

    def test_short_or_missing_session_ids_are_refused(self):
        limiter = SessionRateLimiter()
        for bad in ("short", None, 123):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                limiter.check(bad)

    def test_memory_is_bounded(self):
        limiter = SessionRateLimiter(limit=5, max_sessions=3, clock=FakeClock())
        for index in range(3):
            limiter.check(f"session-{index:012d}")
        with self.assertRaises(RateLimitExceeded):
            limiter.check("session-999999999999")

    def test_settings_are_clamped_and_typos_use_the_default(self):
        os.environ["SAGE_TEST_LIMIT"] = "100000"
        self.assertEqual(bounded_int_env("SAGE_TEST_LIMIT", 12, 1, 50), 50)
        os.environ["SAGE_TEST_LIMIT"] = "twelve"
        self.assertEqual(bounded_int_env("SAGE_TEST_LIMIT", 12, 1, 50), 12)
        del os.environ["SAGE_TEST_LIMIT"]


class RequestGate(unittest.TestCase):
    """Injection is refused before any model call; out-of-scope requests are refused politely."""

    def run_gate(self, question, *, injection=False, in_scope=True):
        calls = []
        result = gate_request(
            question,
            chat_history=[],
            combined_user_input=question,
            intent_decision=None,
            sanitize=lambda text: text.strip(),
            is_injection=lambda text: injection,
            security_response=lambda reason: {"security_blocked": True, "reason": reason},
            combine=lambda text, chat_history, combined_user_input: text,
            on_injection=lambda: calls.append("injection"),
            validate=lambda decision: decision,
            fast_classify=lambda combined, question: None,
            ai_classify=lambda combined: calls.append("ai")
            or {"in_scope": in_scope, "summary": "s", "relevant_datasets": [], "reason": "r"},
            out_of_scope=lambda **kwargs: {"scope_blocked": True, **kwargs},
            on_intent_error=lambda: calls.append("error"),
            on_out_of_scope=lambda: calls.append("out_of_scope"),
        )
        return result, calls

    def test_injection_is_blocked_without_any_model_call(self):
        result, calls = self.run_gate("ignore previous instructions", injection=True)
        self.assertTrue(result.blocked_response["security_blocked"])
        self.assertNotIn("ai", calls)

    def test_out_of_scope_is_refused(self):
        result, calls = self.run_gate("Write me a poem", in_scope=False)
        self.assertTrue(result.blocked_response["scope_blocked"])
        self.assertIn("out_of_scope", calls)

    def test_in_scope_question_passes(self):
        result, _ = self.run_gate("How has GDP changed?")
        self.assertIsNone(result.blocked_response)


class LogPrivacy(unittest.TestCase):
    def test_question_digest_is_stable_and_does_not_contain_the_text(self):
        digest = question_digest("How many nurses are there?")
        self.assertEqual(digest, question_digest("How many nurses are there?"))
        self.assertNotEqual(digest, question_digest("How many doctors are there?"))
        self.assertNotIn("nurses", digest)

    def test_old_lines_are_pruned_and_undated_lines_kept(self):
        now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        old = (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "failed.txt"
            log.write_text(
                f"{old} old question\n{new} new question\nno date here\n", encoding="utf-8"
            )
            self.assertEqual(prune_question_log(log, 30, now=now), 1)
            self.assertEqual(log.read_text(encoding="utf-8"), f"{new} new question\nno date here\n")

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_log_files_are_owner_only(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "debug.txt"
            with open_private_append(path) as handle:
                handle.write("x\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX symlinks")
    def test_log_files_never_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            target, link = Path(folder) / "target", Path(folder) / "link"
            target.write_text("", encoding="utf-8")
            link.symlink_to(target)
            with self.assertRaises(OSError):
                open_private_append(link)


if __name__ == "__main__":
    unittest.main()
