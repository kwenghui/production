#!/usr/bin/env python3
"""Offline secret and forbidden-file scanner (pre-commit hook and CI gate).

Usage:
    python tools/check_secrets.py [PATH ...]      # default: the repository
Exit status 1 when anything is found. Findings show the file, line and rule,
never the matched secret itself.

Complements gitleaks (see .pre-commit-config.yaml); this one needs no network
or binary, so it also runs in locked-down build environments.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RULES = (
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
)
# Name = literal assignments. Python: only a quoted literal (variables such as
# max_tokens are not secrets). Config files: any non-placeholder value.
_SECRET_NAME = (
    r"[A-Za-z0-9_]*(?:password|passwd|secret|api_key|apikey|access_token|auth_token)[A-Za-z0-9_]*"
)
PY_ASSIGNMENT = re.compile(
    rf"(?i)\b{_SECRET_NAME}\s*[:=]\s*(?:str\s*=\s*)?['\"][^'\"\s]{{8,}}['\"]"
)
CONFIG_ASSIGNMENT = re.compile(
    rf"(?im)^\s*(?:export\s+)?{_SECRET_NAME}\s*[=:]\s*['\"]?"
    r"(?!<|\$\{|your|change|replace|xxx|example)[^\s'\"#]{8,}"
)
CONFIG_SUFFIXES = {
    ".env",
    ".toml",
    ".ini",
    ".cfg",
    ".yml",
    ".yaml",
    ".conf",
    ".sh",
    ".ps1",
    ".txt",
    ".json",
    ".example",
    "",
}
# Files that must never be committed or shipped, whatever their content.
FORBIDDEN = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$)[^/]+$"),
    re.compile(r"(^|/)secrets\.toml$"),
    re.compile(r"(^|/)\.sage_log_key$"),
    re.compile(r"(^|/)debug\.txt(\.\d+)?$"),
    re.compile(r"(^|/)questions/(failed|success)\.txt$"),
    re.compile(r"\.(pem|key|p12|pfx)$"),
    # Compiled bytecode must never ship: it can shadow the reviewed source.
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"\.py[co]$"),
)
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
}  # local caches; committed .pyc files are still caught via git ls-files
TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".txt",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".ini",
    ".cfg",
    ".env",
    ".example",
    ".sh",
    ".ps1",
    ".csv",
    ".sql",
    ".html",
    ".conf",
    "",
}
# Test fixtures that intentionally contain fake credentials.
ALLOWLIST_MARKER = "check-secrets: allow"


def candidate_files(paths: list[str]) -> list[Path]:
    if paths:
        found = []
        for raw in paths:
            path = Path(raw)
            found.extend(
                p
                for p in ([path] if path.is_file() else path.rglob("*"))
                if p.is_file()
                and (path.is_file() or not SKIP_DIRS.intersection(p.relative_to(path).parts))
            )
        return found
    try:  # in a git checkout, scan exactly what would be committed
        out = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [ROOT / line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        return [
            p
            for p in ROOT.rglob("*")
            if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)
        ]


def scan(files: list[Path]) -> list[str]:
    findings = []
    for path in files:
        try:
            rel = path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        if any(rule.search(rel) for rule in FORBIDDEN):
            findings.append(f"{rel}: forbidden file (secrets, keys or visitor logs)")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 5_000_000:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), 1):
            if ALLOWLIST_MARKER in line:
                continue
            for name, rule in RULES:
                if rule.search(line):
                    findings.append(f"{rel}:{number}: possible {name}")
            suffix = path.suffix.lower()
            if (suffix == ".py" and PY_ASSIGNMENT.search(line)) or (
                suffix in CONFIG_SUFFIXES and CONFIG_ASSIGNMENT.search(line)
            ):
                findings.append(f"{rel}:{number}: possible hard-coded secret assignment")
    return findings


def main(argv: list[str]) -> int:
    findings = scan(candidate_files(argv))
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"\n{len(findings)} finding(s). Remove the secret, rotate it if it was ever "
            "committed or shared, and use .env / Streamlit secrets instead.",
            file=sys.stderr,
        )
        return 1
    print("No secrets or forbidden files found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
