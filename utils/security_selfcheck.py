"""Startup security self-check: Sage inspects its own configuration.

Run once per process at start-up. Each misconfiguration becomes a *finding* with
an ID, a severity and the exact fix; a clean deployment logs
"security self-check: 0 findings". Nothing is changed and no secret value is
ever logged -- only the NAME of the setting.

Also runnable by an operator:   python -m utils.security_selfcheck

Used by: ui/startup.py.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from utils.settings import env_flag

SECURITY = logging.getLogger("sage.security")

_OFF = {"0", "false", "no", "off"}
MIN_SCRYPT_N = 2**15


@dataclass(frozen=True)
class Finding:
    """One security configuration problem and how to fix it."""

    id: str
    severity: str  # "high" | "medium" | "low"
    problem: str
    fix: str


def _flag(env: Mapping[str, str], name: str) -> bool:
    """True when an environment setting looks switched on (1/true/yes/on).

    Deliberately lenient: a risky switch set to "true" is reported even though
    Sage itself only honours the exact value "1" (utils.settings.env_opt_in).
    """
    return env_flag(name, False, env)


def _scrypt_n(encoded: str) -> int | None:
    """The scrypt cost parameter N of a stored password hash, or None."""
    parts = encoded.split("$")
    if len(parts) >= 2 and parts[0] == "scrypt":
        try:
            return int(parts[1])
        except ValueError:
            return None
    return None


def platform_managed(env: Mapping[str, str] | None = None) -> bool:
    """True on Streamlit Community Cloud, which sets XSRF and telemetry itself.

    Detected by its fixed source mount (/mount/src) or an explicit
    SAGE_PLATFORM=streamlit-cloud setting.
    """
    env = os.environ if env is None else env
    if str(env.get("SAGE_PLATFORM") or "").strip().lower() == "streamlit-cloud":
        return True
    return Path("/mount/src").is_dir() and Path(__file__).resolve().as_posix().startswith(
        "/mount/src/"
    )


def run_checks(
    env: Mapping[str, str] | None = None,
    get_option: Callable[[str], object] | None = None,
    key_file: Path | None = None,
) -> list[Finding]:
    """Return every configuration finding. Pure: reads settings, changes nothing.

    Four groups, in this order: access control, spending and debug switches,
    log privacy, and (when ``get_option`` is given) Streamlit hardening.
    """
    env = os.environ if env is None else env
    findings: list[Finding] = []
    _access_findings(env, findings.append)
    _switch_findings(env, findings.append)
    _privacy_findings(env, key_file, findings.append)
    if get_option is not None:
        _streamlit_findings(env, get_option, findings.append)
    return findings


def _access_findings(env, add) -> None:
    """Sign-in switched off, plaintext or weak passwords, shared admin, diagnostics for all."""
    mode = str(env.get("SAGE_AUTH_MODE") or "password").strip().lower()
    gate_on = str(env.get("Need_Password", "1")).strip().lower() not in _OFF
    if mode != "oidc":
        if not gate_on:
            add(
                Finding(
                    "SC-01",
                    "high",
                    "Sign-in is switched off (Need_Password=0).",
                    "Remove Need_Password or set it to 1, or use SSO (SAGE_AUTH_MODE=oidc).",
                )
            )
        if env.get("Sage_Password") or env.get("SAGE_PASSWORD"):
            add(
                Finding(
                    "SC-02",
                    "medium",
                    "A plaintext password setting is present (it is ignored).",
                    "Delete Sage_Password / SAGE_PASSWORD; keep only SAGE_PASSWORD_HASH.",
                )
            )
        for name in ("SAGE_PASSWORD_HASH", "SAGE_ADMIN_PASSWORD_HASH"):
            value = str(env.get(name) or "").strip()
            n = _scrypt_n(value) if value else None
            if value and (n is None or n < MIN_SCRYPT_N):
                add(
                    Finding(
                        "SC-03",
                        "medium",
                        f"{name} is weak or not a scrypt hash.",
                        "Regenerate it with: python -m utils.auth (or --admin).",
                    )
                )
        if _flag(env, "SAGE_ALLOW_SHARED_PASSWORD_ADMIN"):
            add(
                Finding(
                    "SC-04",
                    "medium",
                    "Everyone who knows the general password is an administrator.",
                    "Unset SAGE_ALLOW_SHARED_PASSWORD_ADMIN; use SAGE_ADMIN_PASSWORD_HASH "
                    "instead.",
                )
            )
        if (
            str(env.get("TECHNICAL_DETAILS") or "").strip() == "1"
            and not str(env.get("SAGE_ADMIN_PASSWORD_HASH") or "").strip()
        ):
            add(
                Finding(
                    "SC-05",
                    "low",
                    "TECHNICAL_DETAILS=1 shows diagnostics to every signed-in user.",
                    "Set SAGE_ADMIN_PASSWORD_HASH (admins only) or TECHNICAL_DETAILS=0.",
                )
            )
    elif (
        not str(env.get("SAGE_ADMIN_EMAILS") or "").strip()
        and str(env.get("TECHNICAL_DETAILS") or "") == "1"
    ):
        add(
            Finding(
                "SC-06",
                "low",
                "SSO with TECHNICAL_DETAILS=1 but no SAGE_ADMIN_EMAILS: nobody can use them.",
                "List administrator e-mails in SAGE_ADMIN_EMAILS, or set TECHNICAL_DETAILS=0.",
            )
        )


def _switch_findings(env, add) -> None:
    """Switches that relax spending guards, allow test modes or let the provider keep data."""
    if _flag(env, "SAGE_ALLOW_UNLIMITED"):
        add(
            Finding(
                "SC-07",
                "medium",
                "Spending guards may be switched off (SAGE_ALLOW_UNLIMITED=1).",
                "Unset SAGE_ALLOW_UNLIMITED.",
            )
        )
    if _flag(env, "SAGE_ALLOW_ADMIN_TESTS"):
        add(
            Finding(
                "SC-08",
                "medium",
                "Debug/test modes are allowed (SAGE_ALLOW_ADMIN_TESTS=1).",
                "Unset SAGE_ALLOW_ADMIN_TESTS on shared servers.",
            )
        )
    if _flag(env, "SAGE_DEV_RELOAD"):
        add(
            Finding(
                "SC-09",
                "low",
                "Development hot-reload is on (SAGE_DEV_RELOAD=1).",
                "Unset SAGE_DEV_RELOAD.",
            )
        )
    if _flag(env, "SAGE_OPENAI_STORE"):
        add(
            Finding(
                "SC-10",
                "low",
                "Prompts and answers are retained by the AI provider (SAGE_OPENAI_STORE=1).",
                "Unset SAGE_OPENAI_STORE unless retention is approved.",
            )
        )


def _privacy_findings(env, key_file, add) -> None:
    """No persistent pseudonym key: log pseudonyms would change on every restart."""
    key_path = key_file or Path(
        env.get("SAGE_LOG_KEY_FILE") or Path(__file__).resolve().parent.parent / ".sage_log_key"
    )
    if not str(env.get("SAGE_LOG_PSEUDONYM_KEY") or "").strip() and not key_path.is_file():
        add(
            Finding(
                "SC-11",
                "low",
                "No persistent pseudonym key: log pseudonyms change on every restart.",
                "Set SAGE_LOG_PSEUDONYM_KEY (a long random secret) in the secrets store.",
            )
        )


def _streamlit_findings(env, get_option, add) -> None:
    """Streamlit settings: error details, cross-site request protection, telemetry."""

    def option(name):
        """A Streamlit option's value, or None when this Streamlit version lacks it."""
        try:
            return get_option(name)
        except Exception:  # noqa: BLE001 - unknown option on this Streamlit version
            return None

    # showErrorDetails is now "full" | "stacktrace" | "type" | "none" (older: true/false).
    details = option("client.showErrorDetails")
    if details is True or str(details).strip().lower() in {"true", "full", "stacktrace"}:
        add(
            Finding(
                "SC-12",
                "medium",
                f"Error details (tracebacks) are shown to visitors (current: {details!r}).",
                'Set client.showErrorDetails = "none" in .streamlit/config.toml.',
            )
        )
    if not platform_managed(env):
        # Streamlit Community Cloud forces these two settings itself; they cannot be
        # changed there, so they are reported only where the operator controls them.
        if option("server.enableXsrfProtection") is False:
            add(
                Finding(
                    "SC-13",
                    "medium",
                    "Cross-site request forgery protection is off.",
                    "Set server.enableXsrfProtection = true in .streamlit/config.toml.",
                )
            )
        if option("browser.gatherUsageStats") is True:
            add(
                Finding(
                    "SC-14",
                    "low",
                    "Streamlit usage telemetry is on.",
                    "Set browser.gatherUsageStats = false in .streamlit/config.toml.",
                )
            )


def log_findings(findings: list[Finding], env: Mapping[str, str] | None = None) -> None:
    """Log the self-check result (0 findings, or one line per finding)."""
    if platform_managed(env):
        SECURITY.info(
            "security self-check: running on Streamlit Community Cloud; XSRF protection "
            "and "
            "usage statistics are set by the platform and not checked here."
        )
    if not findings:
        SECURITY.info("security self-check: 0 findings")
        return
    for f in findings:
        level = logging.WARNING if f.severity in {"high", "medium"} else logging.INFO
        SECURITY.log(
            level, "security self-check %s [%s]: %s Fix: %s", f.id, f.severity, f.problem, f.fix
        )
    SECURITY.warning("security self-check: %d finding(s)", len(findings))


def main() -> int:  # pragma: no cover - operator command
    """Command line: print the security self-check (python -m utils.security_selfcheck)."""
    findings = run_checks()
    if not findings:
        print("Security self-check: 0 findings.")
        return 0
    for f in findings:
        print(f"{f.id} [{f.severity}] {f.problem}\n    Fix: {f.fix}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
