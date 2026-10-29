"""Characterise table_tools._cube_query_fallback (the query tool for multi-dimensional tables).

Uses the random cubes of tools/characterise_cube_slices.py (plus flat tables and
single-period cubes), served by a fake table loader, and varies the series
search, year range, row limit, chart switch, metadata completeness and unit
hint. Prints outcome counts and a digest: two copies of Sage that build the
same results print the same digest.

    python tools/characterise_cube_fallback.py SAGE_DIR
"""

import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
import logging  # noqa: E402

logging.disable(logging.CRITICAL)
import utils.core_secure as core  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cube_slices", Path(__file__).resolve().parent / "characterise_cube_slices.py"
)
cubes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cubes)

try:  # after the split it lives in utils.table_tools
    import utils.table_tools as module  # noqa: E402
except ImportError:  # older copies: still in the core
    module = core
fallback = module._cube_query_fallback
R = random.Random(21)


def flat_table():
    return {
        "id": "M810001",
        "row": [{"rowText": "Total", "seriesNo": "1", "columns": [{"key": "2023", "value": "5"}]}],
    }


def single_period_cube():
    rows = [
        {
            "rowText": name,
            "columns": [
                {"key": g, "value": str(R.randint(1, 99))} for g in ("Male", "Female", "Total")
            ],
        }
        for name in R.sample(["Total", "Food", "Housing", "Transport"], R.randint(1, 4))
    ]
    return {"id": "D20001", "row": rows}


def main():
    digests, outcomes = [], {}
    for _ in range(1200):
        payload = R.choice([cubes.random_cube] * 6 + [flat_table, single_period_cube])()
        module._generic_table_payload = lambda table_id, limit=None, _p=payload: _p
        metadata = {
            "table_id": "D12345",
            "title": R.choice(["Spending, Annual", None]),
            "table_complete": R.choice([True, True, False]),
        }
        try:
            result = fallback(
                "D12345",
                metadata,
                R.choice([None, "Food", "Total", "Lowest 20%", "x"]),
                R.choice([None, 2010, 2023]),
                R.choice([None, 2023]),
                R.choice([1, 3, 8]),
                R.choice([True, False]),
                unit_hint=R.choice([None, "Dollars"]),
            )
        except Exception as error:  # noqa: BLE001 - a crash is part of the recorded behaviour
            result = {"raised": type(error).__name__, "message": str(error)[:200]}
        key = (
            "none"
            if result is None
            else "raised"
            if "raised" in result
            else ("ok" if result.get("success") else "refused")
            + ":"
            + str(result.get("chart_kind"))
        )
        outcomes[key] = outcomes.get(key, 0) + 1
        digests.append(
            hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()[
                :12
            ]
        )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
