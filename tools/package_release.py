"""Build a release zip of Sage WITHOUT secrets, logs or machine-specific files.

    python tools/package_release.py ../SAGE-release.zip

Only files that belong in source control are included. Anything matched by
EXCLUDED below is left out, most importantly:

* secrets: .env, .streamlit/secrets.toml, .sage_log_key (log pseudonym key);
* personal data: question logs (failed.txt, success.txt), debug.txt, feedback;
* machine state: caches, databases, test progress, __pycache__, virtualenvs.

The script prints what it left out, so a reviewer can confirm nothing
needed was dropped and nothing private was added.
"""

import fnmatch
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Relative-path patterns that must never be packaged.
EXCLUDED = (
    ".env",
    ".env.*",
    ".streamlit/secrets.toml",
    ".sage_log_key",
    "debug.txt",
    "questions/failed.txt",
    "questions/success.txt",
    "feedback*",
    "test_progress.json",
    "test_reset_once.json",
    "data/*",
    "charts/*",
    "*.sqlite3",
    "*.sqlite3-*",
    "*/__pycache__/*",
    "__pycache__/*",
    "*.pyc",
    ".venv/*",
    "venv/*",
    ".git/*",
)
#: Exceptions to EXCLUDED (templates that are safe and needed).
ALWAYS_INCLUDED = (".env.example",)


def is_excluded(relative):
    """True when ``relative`` (POSIX path) must not be packaged."""
    if relative in ALWAYS_INCLUDED:
        return False
    return any(fnmatch.fnmatch(relative, pattern) for pattern in EXCLUDED)


def build(target):
    """Write the zip; return (included, excluded) relative paths."""
    included, excluded = [], []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or path.resolve() == Path(target).resolve():
                continue
            relative = path.relative_to(ROOT).as_posix()
            if is_excluded(relative):
                excluded.append(relative)
                continue
            archive.write(path, relative)
            included.append(relative)
    return included, excluded


def main(argv=None):
    """Command line entry point."""
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    included, excluded = build(Path(args[0]))
    print(f"Packaged {len(included)} files into {args[0]}.")
    shown = [p for p in excluded if "__pycache__" not in p]
    print("Left out:", ", ".join(shown) if shown else "nothing besides caches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
