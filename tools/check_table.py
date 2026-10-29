"""Download one SingStat table exactly the way Sage does, and say why it fails.

    python tools/check_table.py M213881

Uses a TEMPORARY database, so your real data is never changed. Prints the number
of rows and periods on success, or Sage's exact completeness / download error.
Also shows what your real local database already holds for the table.
"""

from __future__ import annotations

import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    table_id = argv[1].strip().upper()
    print(f"Checking SingStat table {table_id}\n")
    try:
        import utils.store as store
        import utils.core_secure as core  # Sage's own download code and limits
    except Exception as exc:  # noqa: BLE001 - diagnostic tool
        print(f"Could not load Sage: {type(exc).__name__}: {exc}")
        return 1

    # 1) What the real database already has (read-only).
    try:
        real = core.get_local_store()
        age = real.table_cache_age_seconds(table_id)
        staged = real.staged_catalog(table_id) or []
        print(
            f"[info] Local database: "
            f"{'stored, ' + str(round(age / 3600, 1)) + ' h old' if age is not None else 'not stored'}"
            f"; staged series names: {len(staged)}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[info] Local database not readable: {type(exc).__name__}: {exc}")

    # 2) A fresh download into a temporary database, exactly as Sage fetches it.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        temp = store.LocalStore(str(Path(folder) / "check.sqlite3"))
        started = time.monotonic()
        try:
            payload = temp.get_or_fetch(table_id, core._live_http_get)
        except store.UnverifiablePaginationError as exc:
            print(
                f"[FAIL] Download not verifiable as complete after "
                f"{time.monotonic() - started:.1f} s:"
            )
            print(f"       {exc}")
            print("       Sage refuses incomplete official data, so it answered without figures.")
            return 1
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {type(exc).__name__} after {time.monotonic() - started:.1f} s: {exc}")
            print("".join(traceback.format_exc(limit=6)))
            return 1
        data = (payload or {}).get("Data") or {}
        rows = data.get("row") or []
        periods = sorted(
            {str(c.get("key")) for r in rows for c in (r.get("columns") or []) if c.get("key")}
        )
        print(
            f"[OK  ] Downloaded and verified in {time.monotonic() - started:.1f} s: "
            f"{len(rows)} series, {len(periods)} periods"
        )
        if periods:
            print(f"       first periods: {periods[:3]}  last periods: {periods[-3:]}")
        print(f"       title: {data.get('title')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
