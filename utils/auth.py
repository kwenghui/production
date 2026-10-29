"""Access control for Sage: a fail-closed, shared-password gate.

What changed from the previous gate, and why
--------------------------------------------
* **No built-in password.** A default password published in source code is a
  known credential. If the gate is on and no credential is configured, Sage
  now refuses access (fail closed) instead of falling back to it.
* **On by default.** Only an explicit ``Need_Password=0/false/no/off`` turns
  it off, so a missing variable in a new deployment cannot expose the app.
* **Hashed credential.** ``SAGE_PASSWORD_HASH`` holds a salted scrypt hash,
  so a leaked ``.env``/secrets file does not reveal the password. The legacy
  plaintext ``Sage_Password`` is rejected (fail closed).
* **Throttling the attacker can't reset.** Failures were counted in
  ``st.session_state``; opening a new tab gave five fresh guesses. Failures are
  now counted per pseudonymised client address, process-wide, with an
  exponential back-off, plus a global circuit breaker against distributed
  guessing.
* **Session lifetime.** Signed-in sessions expire after inactivity and after
  an absolute maximum, and can be signed out.
* **Audit trail.** Success, failure, lock-out and sign-out are logged to the
  ``sage.audit`` logger with a pseudonymous client ID, never the raw address
  or the attempted password.

This remains a *shared secret*: it gives no individual accountability. For
anything beyond a small trusted pilot, put Sage behind SSO (Streamlit's
``st.login`` with your OIDC provider, or an authenticating reverse proxy).

Generate a hash with::

    python -m utils.auth

Used by: ui/answer_pipeline.py, ui/sidebar.py, ui/startup.py.
Tested in: tests/test_access_and_logs.py, tests/test_security.py.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, MutableMapping

from utils.errors import AccountLockedError, AuthenticationError, ConfigurationError
from utils.session_security import UNKNOWN_CLIENT, bounded_int_env, client_fingerprint

AUDIT = logging.getLogger("sage.audit")
LOG = logging.getLogger("sage.security")

_OFF_VALUES = frozenset({"0", "false", "no", "off"})
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**15, 8, 1, 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_MIN_PASSWORD_LENGTH = 12
# Each scrypt check uses ~32 MB and ~0.1 s CPU. Bound how many run at once so a
# burst of sign-in attempts cannot exhaust memory.
_VERIFY_SLOTS = threading.BoundedSemaphore(bounded_int_env("SAGE_AUTH_PARALLEL_VERIFY", 4, 1, 32))
_VERIFY_WAIT_SECONDS = 5.0

# Session-state keys owned by this module.
AUTH_KEY = "sage_authenticated"
AUTH_AT_KEY = "sage_auth_at"
AUTH_SEEN_KEY = "sage_auth_seen"
SECURITY_SESSION_KEY = "_sage_security_session_id"
AUTH_EMAIL_KEY = "_sage_auth_email"  # SSO only: verified e-mail of the signed-in person
LOCKED_ROLE_KEY = "_sage_locked_role"  # set while an idle session is locked (work kept)
AUTH_ROLE_KEY = "_sage_auth_role"  # password mode: "admin" (admin password) or "user"
ROLE_ADMIN, ROLE_USER = "admin", "user"
# Must be preserved by anything that resets session state (see app.reset_like_clear).
AUTH_STATE_KEYS = (
    AUTH_KEY,
    AUTH_AT_KEY,
    AUTH_SEEN_KEY,
    AUTH_EMAIL_KEY,
    AUTH_ROLE_KEY,
    LOCKED_ROLE_KEY,
)


# --- Password hashing -----------------------------------------------------------
def _b64(data: bytes) -> str:
    """URL-safe base64 without padding (used inside password hashes)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    """Decode URL-safe base64 written by _b64 (padding restored)."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(
    password: str, *, n: int = _SCRYPT_N, r: int = _SCRYPT_R, p: int = _SCRYPT_P
) -> str:
    """Return ``scrypt$n$r$p$salt$hash`` for storage in ``SAGE_PASSWORD_HASH``."""
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"Use at least {_MIN_PASSWORD_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(digest)}"


class PasswordVerifier:
    """Constant-time verification against a hash or a legacy plaintext value."""

    def __init__(self, *, encoded_hash: str | None = None, plaintext: str | None = None) -> None:
        """Exactly one source: an encoded scrypt hash (preferred) or a plaintext password."""
        if bool(encoded_hash) == bool(plaintext):
            raise ConfigurationError("exactly one password source is required")
        self._params: tuple[int, int, int, bytes, bytes] | None = None
        self._plain_digest: bytes | None = None
        self._pepper = secrets.token_bytes(32)
        if encoded_hash:
            self._params = self._parse(encoded_hash)
        else:
            # Compare fixed-length MACs so neither length nor content leaks.
            self._plain_digest = self._mac(str(plaintext))

    @staticmethod
    def _parse(encoded: str) -> tuple[int, int, int, bytes, bytes]:
        """Split ``scrypt$n$r$p$salt$hash`` into parameters; malformed or weak hashes raise
        ConfigurationError."""
        try:
            scheme, n, r, p, salt, digest = encoded.strip().split("$")
            if scheme != "scrypt":
                raise ValueError("unsupported scheme")
            params = int(n), int(r), int(p), _unb64(salt), _unb64(digest)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("SAGE_PASSWORD_HASH is malformed") from exc
        if params[0] < 2**14 or len(params[3]) < 16 or len(params[4]) < 16:
            raise ConfigurationError("SAGE_PASSWORD_HASH parameters are too weak")
        return params

    def _mac(self, value: str) -> bytes:
        """HMAC of a value under a per-process random key (fixed length, so comparisons leak
        nothing)."""
        return hmac.new(self._pepper, value.encode("utf-8"), hashlib.sha256).digest()

    def verify(self, candidate: str) -> bool:
        """True when ``candidate`` is the password; constant-time comparison either way."""
        candidate = str(candidate or "")
        if self._params is not None:
            n, r, p, salt, expected = self._params
            actual = hashlib.scrypt(
                candidate.encode("utf-8"),
                salt=salt,
                n=n,
                r=r,
                p=p,
                maxmem=_SCRYPT_MAXMEM,
                dklen=len(expected),
            )
            return hmac.compare_digest(actual, expected)
        return hmac.compare_digest(self._mac(candidate), self._plain_digest or b"")


# --- Throttling -------------------------------------------------------------------
@dataclass
class _ClientRecord:
    """Failed-login history of one client: recent failures, lock-outs so far, lock expiry."""

    failures: int = 0
    lockouts: int = 0
    locked_until: float = 0.0
    last_failure: float = 0.0


@dataclass
class LoginThrottle:
    """Process-wide failed-login tracking with exponential back-off.

    After ``max_failures`` failures a client is locked for ``base_lock`` seconds,
    doubling on each subsequent lock-out up to ``max_lock``. Records are
    forgotten after ``forget_after`` seconds without failures.

    The global breaker trips when ``global_limit`` failures arrive from *all*
    clients within ``global_window`` seconds (distributed guessing). It pauses
    every login for ``global_lock`` seconds. This trades a short availability
    loss for confidentiality; tune or disable it (``global_limit=0``) if
    availability matters more for your deployment.
    """

    max_failures: int = 5
    base_lock: float = 30.0
    max_lock: float = 900.0
    forget_after: float = 3600.0
    global_limit: int = 100
    global_window: float = 600.0
    global_lock: float = 60.0
    max_clients: int = 10000
    # Clients that signed in successfully within this window are exempt from the
    # GLOBAL pause, so an attacker spraying wrong passwords cannot lock out
    # everyone who uses Sage regularly. Their own per-client limit still applies.
    trusted_window: float = 24 * 3600.0
    clock: Callable[[], float] = time.monotonic
    _clients: dict[str, _ClientRecord] = field(default_factory=dict, repr=False)
    _trusted: dict[str, float] = field(default_factory=dict, repr=False)
    _global_failures: list[float] = field(default_factory=list, repr=False)
    _global_locked_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check(self, client: str) -> None:
        """Raise :class:`AccountLockedError` if *client* may not try now."""
        now = self.clock()
        with self._lock:
            trusted_at = self._trusted.get(client)
            recently_trusted = trusted_at is not None and now - trusted_at < self.trusted_window
            if now < self._global_locked_until and not recently_trusted:
                raise AccountLockedError(self._global_locked_until - now, "global login pause")
            record = self._clients.get(client)
            if record and now < record.locked_until:
                raise AccountLockedError(record.locked_until - now)

    def record_failure(self, client: str) -> float:
        """Count a failure; return the lock duration applied (0 if none)."""
        now = self.clock()
        with self._lock:
            self._prune(now)
            record = self._clients.setdefault(client, _ClientRecord())
            record.failures += 1
            record.last_failure = now
            applied = 0.0
            if record.failures >= self.max_failures:
                record.lockouts += 1
                record.failures = 0
                applied = min(self.max_lock, self.base_lock * 2 ** (record.lockouts - 1))
                record.locked_until = now + applied
            if self.global_limit:
                cutoff = now - self.global_window
                self._global_failures = [t for t in self._global_failures if t >= cutoff]
                self._global_failures.append(now)
                if len(self._global_failures) >= self.global_limit:
                    self._global_locked_until = now + self.global_lock
                    self._global_failures.clear()
                    AUDIT.critical(
                        "auth.global_pause seconds=%d reason=distributed_failures",
                        int(self.global_lock),
                    )
            return applied

    def record_success(self, client: str) -> None:
        """Forget a client's failures and trust it during global login pauses (stable client IDs
        only)."""
        now = self.clock()
        with self._lock:
            self._clients.pop(client, None)
            if client.startswith("session-"):
                return  # per-tab fallback keys are not a stable identity
            if len(self._trusted) >= self.max_clients:
                cutoff = now - self.trusted_window
                self._trusted = {k: t for k, t in self._trusted.items() if t >= cutoff}
                while len(self._trusted) >= self.max_clients:
                    self._trusted.pop(next(iter(self._trusted)))
            self._trusted.pop(client, None)
            self._trusted[client] = now

    def _prune(self, now: float) -> None:
        """Forget unlocked, long-idle clients once the table is full (bounded memory)."""
        if len(self._clients) < self.max_clients:
            return
        stale = [
            k
            for k, r in self._clients.items()
            if now >= r.locked_until and now - r.last_failure >= self.forget_after
        ]
        for key in stale:
            del self._clients[key]
        if len(self._clients) >= self.max_clients:  # still full: drop oldest
            for key in sorted(self._clients, key=lambda k: self._clients[k].last_failure)[
                : len(self._clients) - self.max_clients + 1
            ]:
                del self._clients[key]


THROTTLE = LoginThrottle(
    max_failures=bounded_int_env("SAGE_AUTH_MAX_FAILURES", 5, 3, 20),
    base_lock=float(bounded_int_env("SAGE_AUTH_LOCK_SECONDS", 30, 5, 3600)),
    global_limit=bounded_int_env("SAGE_AUTH_GLOBAL_FAILURE_LIMIT", 100, 0, 100000),
)


# --- Gate -------------------------------------------------------------------------
@dataclass
class PasswordGate:
    """Policy object: decides whether, and against what, to authenticate."""

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    throttle: LoginThrottle = field(default_factory=lambda: THROTTLE)
    clock: Callable[[], float] = time.time
    _verifier: PasswordVerifier | None = field(default=None, repr=False)
    _verifier_source: tuple[str, str] | None = field(default=None, repr=False)
    _admin_verifier: PasswordVerifier | None = field(default=None, repr=False)
    _admin_source: str | None = field(default=None, repr=False)

    # -- configuration --
    def enabled(self) -> bool:
        """On unless explicitly switched off. Typos keep it on (fail closed)."""
        raw = self.env.get("Need_Password", self.env.get("NEED_PASSWORD"))
        if raw is None or not str(raw).strip():
            return True
        return str(raw).strip().lower() not in _OFF_VALUES

    def idle_timeout(self) -> int:
        """Seconds without activity before the session locks (SAGE_SESSION_IDLE_MINUTES, default 60)."""
        return 60 * self._int("SAGE_SESSION_IDLE_MINUTES", 60, 5, 24 * 60)

    def absolute_timeout(self) -> int:
        """Seconds after sign-in before it expires regardless of activity (SAGE_SESSION_MAX_HOURS,
        default 8)."""
        return 3600 * self._int("SAGE_SESSION_MAX_HOURS", 8, 1, 7 * 24)

    def _int(self, name: str, default: int, low: int, high: int) -> int:
        """An integer setting from the environment, clamped to [low, high]; a typo gives
        ``default``."""
        try:
            return min(high, max(low, int(str(self.env.get(name, default)).strip())))
        except ValueError:
            return default

    def verifier(self) -> PasswordVerifier:
        """The verifier for the general password (SAGE_PASSWORD_HASH), rebuilt only when the setting
        changes."""
        encoded = str(self.env.get("SAGE_PASSWORD_HASH") or "").strip()
        source = ("hash", encoded)
        if self._verifier is not None and self._verifier_source == source:
            return self._verifier
        if not encoded:
            if self.env.get("Sage_Password") or self.env.get("SAGE_PASSWORD"):
                # Plaintext passwords are no longer accepted: fail closed.
                raise ConfigurationError(
                    "Sage_Password (plaintext) is no longer supported; set SAGE_PASSWORD_HASH "
                    "(generate it with: python -m utils.auth)"
                )
            raise ConfigurationError("Need_Password is on but SAGE_PASSWORD_HASH is not set")
        verifier = PasswordVerifier(encoded_hash=encoded)
        self._verifier, self._verifier_source = verifier, source
        return verifier

    def admin_verifier(self) -> PasswordVerifier | None:
        """Optional second password (SAGE_ADMIN_PASSWORD_HASH) for administrators."""
        encoded = str(self.env.get("SAGE_ADMIN_PASSWORD_HASH") or "").strip()
        if not encoded:
            return None
        if self._admin_verifier is not None and self._admin_source == encoded:
            return self._admin_verifier
        if encoded == str(self.env.get("SAGE_PASSWORD_HASH") or "").strip():
            raise ConfigurationError("SAGE_ADMIN_PASSWORD_HASH must differ from SAGE_PASSWORD_HASH")
        verifier = PasswordVerifier(encoded_hash=encoded)
        self._admin_verifier, self._admin_source = verifier, encoded
        return verifier

    # -- session lifecycle --
    def is_authenticated(self, state: MutableMapping[str, Any]) -> bool:
        """True while this session's sign-in is within both the idle and the absolute time limits."""
        if state.get(AUTH_KEY) is not True:
            return False
        now = self.clock()
        started = float(state.get(AUTH_AT_KEY) or 0)
        seen = float(state.get(AUTH_SEEN_KEY) or started)
        if now - started > self.absolute_timeout():
            AUDIT.info("auth.session_expired")
            self.sign_out(state, reason="expired")
            return False
        if now - seen > self.idle_timeout():
            # Idle: LOCK rather than wipe. The questions and answers of this browser
            # session stay on the server, hidden behind the password, so a visitor
            # who was cross-checking elsewhere continues where they left off.
            AUDIT.info("auth.session_locked")
            state[LOCKED_ROLE_KEY] = state.get(AUTH_ROLE_KEY) or ROLE_USER
            state[AUTH_KEY] = False
            return False
        state[AUTH_SEEN_KEY] = now
        return True

    @staticmethod
    def is_locked(state: MutableMapping[str, Any]) -> bool:
        """True when this session was locked after inactivity (its work is kept)."""
        return state.get(LOCKED_ROLE_KEY) is not None and state.get(AUTH_KEY) is not True

    def attempt(self, state: MutableMapping[str, Any], password: str, client: str) -> None:
        """Authenticate *password*; raise on failure, mutate *state* on success."""
        self.throttle.check(client)
        verifier = self.verifier()  # ConfigurationError propagates: fail closed
        admin_verifier = self.admin_verifier()
        if not _VERIFY_SLOTS.acquire(timeout=_VERIFY_WAIT_SECONDS):
            raise AccountLockedError(_VERIFY_WAIT_SECONDS, "sign-in busy")
        try:
            role = None
            if admin_verifier is not None and admin_verifier.verify(password):
                role = ROLE_ADMIN
            elif verifier.verify(password):
                role = ROLE_USER
        finally:
            _VERIFY_SLOTS.release()
        matched = role is not None
        if not matched:
            lock = self.throttle.record_failure(client)
            AUDIT.warning("auth.failure client=%s locked_for=%d", client, int(lock))
            if lock:
                raise AccountLockedError(lock)
            raise AuthenticationError("password mismatch")
        self.throttle.record_success(client)
        locked_role = state.pop(LOCKED_ROLE_KEY, None)
        if locked_role is not None and locked_role != role:
            # A different password (user vs admin) never inherits the locked session's work.
            self.sign_out(state, reason="role_changed")
        now = self.clock()
        state[AUTH_KEY] = True
        state[AUTH_AT_KEY] = now
        state[AUTH_SEEN_KEY] = now
        state[AUTH_ROLE_KEY] = role
        # New privilege level -> new security identifier for rate limiting/logs.
        state[SECURITY_SESSION_KEY] = secrets.token_urlsafe(32)
        AUDIT.info("auth.success client=%s role=%s", client, role)

    @staticmethod
    def sign_out(state: MutableMapping[str, Any], *, reason: str = "user") -> None:
        """Drop the sign-in and everything produced while signed in."""
        for key in list(state.keys()):
            if key not in ("sage_theme_choice",):  # harmless UI preference survives
                del state[key]
        AUDIT.info("auth.signed_out reason=%s", reason)


def client_id_from(context: Any, env: Mapping[str, str] | None = None) -> str:
    """Pseudonymous client ID from Streamlit's ``st.context`` (if available).

    Behind a reverse proxy every visitor has the proxy's address. Set
    ``SAGE_CLIENT_IP_HEADER`` (e.g. ``X-Forwarded-For``) **only** when a proxy
    you control overwrites or appends that header; the last entry, the one
    your proxy added, is used. Without such a proxy the header is
    attacker-controlled, so it is ignored by default.

    Returns :data:`UNKNOWN_CLIENT` when no address is available (localhost,
    some hosting platforms). Callers must not throttle that shared bucket as
    if it were one client, or one attacker could lock everyone out.
    """
    env = os.environ if env is None else env
    address = None
    try:
        header = str(env.get("SAGE_CLIENT_IP_HEADER") or "").strip()
        if header:
            headers = getattr(context, "headers", None)
            value = headers.get(header) if headers is not None else None
            address = str(value).split(",")[-1].strip() if value else None
        if not address:
            address = getattr(context, "ip_address", None)
    except (AttributeError, RuntimeError, KeyError, TypeError):
        address = None
    return client_fingerprint(_host_only(address))


def _host_only(address: Any) -> str | None:
    """Drop a client port: some proxies send "ip:port".

    Every new connection has a new source port, so keeping it would give an
    attacker a fresh throttling identity per connection.
    """
    if not address:
        return None
    text = str(address).strip()
    if text.startswith("["):  # [IPv6]:port
        return text[1 : text.index("]")] if "]" in text else text
    if text.count(":") == 1:  # IPv4:port
        return text.split(":", 1)[0]
    return text  # bare IPv4 or IPv6


def throttle_key(client: str, state: MutableMapping[str, Any]) -> str:
    """The key failed logins are counted under.

    A known client is throttled process-wide. When the address is unknown the
    session is used instead (weaker: a new tab resets it), and the global
    breaker remains the backstop against guessing. See SECURITY.md, R-04.
    """
    if client != UNKNOWN_CLIENT:
        return client
    session = state.get(SECURITY_SESSION_KEY)
    if not isinstance(session, str) or len(session) < 16:
        session = secrets.token_urlsafe(32)
        state[SECURITY_SESSION_KEY] = session
    return "session-" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:24]


# --- Streamlit rendering ----------------------------------------------------------
def require_password(
    st: Any, gate: PasswordGate | None = None, *, client: str | None = None
) -> None:
    """Stop the Streamlit script at a sign-in form until authenticated.

    ``st`` is passed in (rather than imported) so the flow is unit-testable.
    """
    gate = gate or PasswordGate()
    if not gate.enabled():
        return
    state = st.session_state
    if gate.is_authenticated(state):
        return
    if client is None:
        client = client_id_from(getattr(st, "context", None))
    client = throttle_key(client, state)
    _, middle, _ = st.columns([1, 1.4, 1])
    with middle:
        st.markdown("## Sage")
        if gate.is_locked(state):
            st.caption(
                f"Locked after {gate.idle_timeout() // 60} minutes without activity. "
                "Enter the password to continue where you left off."
            )
        else:
            st.caption("This application is access-controlled. Enter the password to continue.")
        try:
            gate.verifier()
        except ConfigurationError as exc:
            LOG.error("Password gate misconfigured: %s", exc)
            st.error(exc.user_message)
            st.stop()
            return
        with st.form("sage_login", clear_on_submit=True):
            entered = st.text_input(
                "Password", type="password", key="sage_login_password", max_chars=256
            )
            submitted = st.form_submit_button("Enter", width="stretch")
        if submitted:
            try:
                gate.attempt(state, entered, client)
            except (AuthenticationError, AccountLockedError) as exc:
                st.error(exc.user_message)
            else:
                st.rerun()
    st.stop()


# --- Single sign-on (OIDC) -----------------------------------------------------------
def auth_mode(env: Mapping[str, str] | None = None) -> str:
    """``password`` (default) or ``oidc``. Unknown values fail closed to password."""
    env = os.environ if env is None else env
    mode = str(env.get("SAGE_AUTH_MODE") or "password").strip().lower()
    if mode not in ("password", "oidc"):
        LOG.error("Unknown SAGE_AUTH_MODE=%r; using the password gate", mode)
        return "password"
    return mode


@dataclass
class SsoGate:
    """Authorisation policy on top of Streamlit's built-in OIDC (``st.login``).

    Streamlit authenticates the person with your identity provider; this class
    decides whether that person may use Sage. Being able to sign in to the
    provider is *not* enough: the e-mail must be verified and on an allow-list.
    """

    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    clock: Callable[[], float] = time.time
    password_gate: PasswordGate | None = None  # reused for enabled() and timeouts

    def _policy(self) -> PasswordGate:
        """The password gate whose on/off switch and session lifetimes also apply to single sign-on."""
        return self.password_gate or PasswordGate(env=self.env, clock=self.clock)

    def enabled(self) -> bool:
        """Whether access control is on (same rule as passwords: on unless explicitly off)."""
        return self._policy().enabled()

    def provider(self) -> str | None:
        """The configured identity-provider name (SAGE_OIDC_PROVIDER), or None for Streamlit's
        default."""
        return str(self.env.get("SAGE_OIDC_PROVIDER") or "").strip() or None

    def _allow_lists(self) -> tuple[frozenset[str], frozenset[str]]:
        """(allowed e-mail addresses, allowed e-mail domains) from the environment, lower-cased."""

        def parse(name: str) -> frozenset[str]:
            """A comma-separated allow-list setting as a set of lower-case entries."""
            return frozenset(
                item.strip().lower().lstrip("@")
                for item in str(self.env.get(name) or "").split(",")
                if item.strip()
            )

        return parse("SAGE_OIDC_ALLOWED_EMAILS"), parse("SAGE_OIDC_ALLOWED_DOMAINS")

    def check_configuration(self) -> None:
        """Refuse to start single sign-on without an allow-list (unless SAGE_OIDC_ALLOW_ANY_USER=1)."""
        emails, domains = self._allow_lists()
        open_to_all = str(self.env.get("SAGE_OIDC_ALLOW_ANY_USER") or "").strip() == "1"
        if not (emails or domains or open_to_all):
            raise ConfigurationError(
                "SAGE_AUTH_MODE=oidc needs SAGE_OIDC_ALLOWED_EMAILS or SAGE_OIDC_ALLOWED_DOMAINS"
            )

    def authorise(self, user: Any) -> str:
        """Return the verified e-mail of an allowed *user*, else raise."""
        self.check_configuration()
        email = str(_claim(user, "email") or "").strip().lower()
        verified = _claim(user, "email_verified")
        if not email or "@" not in email:
            raise AuthenticationError(
                "identity has no e-mail claim",
                user_message="Your account did not provide an e-mail address.",
            )
        # Fail closed: a *missing* email_verified claim is treated as unverified.
        # Some providers (e.g. Microsoft Entra ID) omit it; e-mail claims that are
        # not verified can be set by the account holder (the "nOAuth" pattern).
        # Opt in with SAGE_OIDC_TRUST_UNVERIFIED_EMAIL=1 only for a single-tenant
        # provider whose e-mail attribute users cannot edit.
        trust_unverified = (
            str(self.env.get("SAGE_OIDC_TRUST_UNVERIFIED_EMAIL") or "").strip() == "1"
        )
        is_verified = verified is True or str(verified).lower() == "true"
        if not is_verified and not (trust_unverified and verified is None):
            raise AuthenticationError(
                "e-mail not verified by the provider",
                user_message="Your e-mail address is not verified.",
            )
        emails, domains = self._allow_lists()
        domain = email.rsplit("@", 1)[1]
        open_to_all = str(self.env.get("SAGE_OIDC_ALLOW_ANY_USER") or "").strip() == "1"
        if not (open_to_all or email in emails or domain in domains):
            raise AuthenticationError(
                "identity not on the allow-list",
                user_message="Your account is not authorised to use Sage.",
            )
        return email

    def session_valid(self, state: MutableMapping[str, Any]) -> bool:
        """Apply Sage's idle/absolute lifetime on top of the provider's cookie."""
        policy, now = self._policy(), self.clock()
        started = state.get(AUTH_AT_KEY)
        if started is None:
            return False
        seen = float(state.get(AUTH_SEEN_KEY) or started)
        if now - float(started) > policy.absolute_timeout() or now - seen > policy.idle_timeout():
            return False
        state[AUTH_SEEN_KEY] = now
        return True


def _claim(user: Any, name: str) -> Any:
    """One claim (e.g. "email") from a signed-in user object, or None."""
    try:
        getter = getattr(user, "get", None)
        return getter(name) if callable(getter) else getattr(user, name, None)
    except (AttributeError, KeyError, TypeError):
        return None


def _pseudonym(value: str) -> str:
    """A stable, keyed pseudonym for a user's e-mail, for audit logs (never the address itself)."""
    from utils.privacy import question_digest  # keyed HMAC, stable across restarts

    return "user-" + question_digest(value, 16)


def require_sso(st: Any, gate: SsoGate) -> None:
    """Stop the script until an allowed, verified identity is signed in."""
    if not gate.enabled():
        return
    state = st.session_state
    login, logout, user = (
        getattr(st, "login", None),
        getattr(st, "logout", None),
        getattr(st, "user", None),
    )
    if not callable(login) or user is None:
        st.error(ConfigurationError.default_user_message)
        LOG.error("SAGE_AUTH_MODE=oidc needs Streamlit with st.login (>= 1.42) and Authlib")
        st.stop()
        return
    if not _claim(user, "is_logged_in"):
        state.pop(AUTH_KEY, None)
        _, middle, _ = st.columns([1, 1.4, 1])
        with middle:
            st.markdown("## Sage")
            st.caption(
                "This application is access-controlled. Sign in with your organisation " "account."
            )
            if st.button("Sign in", key="sage_sso_login", type="primary", width="stretch"):
                if gate.provider():
                    login(gate.provider())
                else:
                    login()
        st.stop()
        return
    try:
        email = gate.authorise(user)
    except ConfigurationError as exc:
        LOG.error("SSO gate misconfigured: %s", exc)
        st.error(exc.user_message)
        st.stop()
        return
    except AuthenticationError as exc:
        AUDIT.warning(
            "auth.sso_denied user=%s reason=%s",
            _pseudonym(str(_claim(user, "email") or "unknown")),
            exc.code,
        )
        st.error(exc.user_message)
        if callable(logout) and st.button("Sign out", key="sage_sso_denied_logout"):
            logout()
        st.stop()
        return
    if state.get(AUTH_KEY) is True and not gate.session_valid(state):
        AUDIT.info("auth.session_expired user=%s", _pseudonym(email))
        PasswordGate.sign_out(state, reason="expired")
        if callable(logout):
            logout()  # also clears Streamlit's identity cookie
        st.stop()
        return
    if state.get(AUTH_KEY) is not True:
        now = gate.clock()
        state[AUTH_KEY], state[AUTH_AT_KEY], state[AUTH_SEEN_KEY] = True, now, now
        state[SECURITY_SESSION_KEY] = secrets.token_urlsafe(32)
        AUDIT.info("auth.sso_success user=%s", _pseudonym(email))
    state[AUTH_EMAIL_KEY] = email


def require_access(st: Any, password_gate: PasswordGate, sso_gate: SsoGate | None = None) -> None:
    """Dispatch to the configured access-control mechanism."""
    if auth_mode(password_gate.env) == "oidc":
        require_sso(st, sso_gate or SsoGate(env=password_gate.env, password_gate=password_gate))
    else:
        require_password(st, password_gate)


def can_view_technical_details(
    state: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> bool:
    """Who may see diagnostics once the deployment sets TECHNICAL_DETAILS=1.

    Password mode: every signed-in user (the operator turned the flag on for this
    deployment; diagnostics are read-only and already redacted). SSO mode: only
    administrators listed in SAGE_ADMIN_EMAILS. Controls that change behaviour or
    cost (the model selector) still require is_administrator().
    """
    env = os.environ if env is None else env
    if auth_mode(env) != "oidc":
        # With an admin password configured, diagnostics are for admin sessions only
        # (e.g. a lecturer demo); without one, every signed-in user may see them.
        if str(env.get("SAGE_ADMIN_PASSWORD_HASH") or "").strip():
            return is_administrator(state, env)
        return True
    return is_administrator(state, env)


def is_administrator(state: Mapping[str, Any], env: Mapping[str, str] | None = None) -> bool:
    """Whether this session may use administrator controls.

    Password mode: sessions signed in with the admin password
    (SAGE_ADMIN_PASSWORD_HASH), or everyone when SAGE_ALLOW_SHARED_PASSWORD_ADMIN=1
    (single-operator deployments only). SSO mode: the signed-in, verified e-mail
    must be listed in SAGE_ADMIN_EMAILS.
    """
    env = os.environ if env is None else env
    if auth_mode(env) != "oidc":
        # Signed in with the separate admin password (SAGE_ADMIN_PASSWORD_HASH).
        if (
            state.get(AUTH_KEY) is True
            and state.get(AUTH_ROLE_KEY) == ROLE_ADMIN
            and str(env.get("SAGE_ADMIN_PASSWORD_HASH") or "").strip()
        ):
            return True
        # Legacy single-operator switch: everyone who knows the general password is admin.
        allowed = str(env.get("SAGE_ALLOW_SHARED_PASSWORD_ADMIN") or "").strip() == "1"
        if allowed:
            LOG.warning(
                "SAGE_ALLOW_SHARED_PASSWORD_ADMIN=1: every signed-in user has "
                "administrator controls"
            )
        return allowed
    admins = frozenset(
        item.strip().lower()
        for item in str(env.get("SAGE_ADMIN_EMAILS") or "").split(",")
        if item.strip()
    )
    email = str(state.get(AUTH_EMAIL_KEY) or "").strip().lower()
    return bool(email) and email in admins


def sign_out(st: Any, password_gate: PasswordGate) -> None:
    """Sign out of Sage (and of the identity cookie in SSO mode)."""
    password_gate.sign_out(st.session_state)
    if auth_mode(password_gate.env) == "oidc" and callable(getattr(st, "logout", None)):
        st.logout()


def _cli() -> None:  # pragma: no cover - interactive
    """python -m utils.auth          -> general password (SAGE_PASSWORD_HASH)
    python -m utils.auth --admin  -> admin password   (SAGE_ADMIN_PASSWORD_HASH)"""
    import getpass

    admin = "--admin" in sys.argv[1:]
    label, name = (
        ("admin", "SAGE_ADMIN_PASSWORD_HASH") if admin else ("general", "SAGE_PASSWORD_HASH")
    )
    first = getpass.getpass(f"New Sage {label} password: ")
    if getpass.getpass("Repeat password: ") != first:
        raise SystemExit("Passwords do not match.")
    try:
        print(f"{name}=" + hash_password(first))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":  # pragma: no cover
    _cli()
