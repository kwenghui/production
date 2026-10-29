"""Characterise core.query_singstat_table on 1,500 random synthetic tables.

Tables vary in series count, names (incl. "Total"), units, period types
(years, quarters, months), missing and conflicting values; arguments vary in
series search, year range, max_series and make_chart. Prints a digest of all
results: two copies of Sage that behave the same print the same digest.

    python tools/characterise_query_table.py OLD_SAGE_DIR
    python tools/characterise_query_table.py NEW_SAGE_DIR
"""

import hashlib
import json
import random
import sys
from pathlib import Path

tree = sys.argv[1]
sys.path.insert(0, tree)
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
from tools._stand_ins import install_stand_ins

install_stand_ins()
import logging

logging.disable(logging.CRITICAL)
import pandas as pd
import utils.core_secure as core

R = random.Random(11)
state = {}


def fake_retrieve(table_id, limit=None, *, data_override=None):
    return state["df"].copy(), dict(state["meta"])


# Patch wherever the query tool lives (utils.core_secure in older copies,
# utils.table_tools after the split), so both copies are checked alike.
tool_module = sys.modules[core.query_singstat_table.__module__]
for module in {core, tool_module}:
    module.retrieve_generic_singstat_table = fake_retrieve
    module._cube_query_fallback = lambda *a, **k: state["cube"]
    module.SAGE_STORE_ENABLED = False
NAMES = [
    "Total",
    "Males",
    "Females",
    "Manufacturing",
    "Services",
    "Oil",
    "Food",
    "All",
    "Total Population",
]


def periods():
    kind = R.choice(["year", "quarter", "month", "one"])
    if kind == "year":
        return [str(y) for y in range(R.randint(1990, 2015), 2024, R.choice([1, 5]))][
            : R.randint(1, 12)
        ]
    if kind == "quarter":
        return [f"{y} {q}Q" for y in range(2019, 2023) for q in range(1, 5)][: R.randint(1, 10)]
    if kind == "month":
        return [f"2023 {m}" for m in ["Jan", "Feb", "Mar", "Apr", "May"]][: R.randint(1, 5)]
    return ["2023"]


out = []
kinds = {}
for case in range(1500):
    n = R.randint(1, 7)
    ids = R.sample(["1", "2", "3", "1.1", "1.2", "2.1", "4", "5"], n)
    ps = periods()
    rows = []
    for sid in ids:
        name = R.choice(NAMES) if R.random() < 0.8 else f"Series {sid}"
        unit = R.choice(["Number", "Number", "Per Cent", "$"])
        for p in ps:
            if R.random() < 0.1:
                continue
            v = R.choice([R.randint(0, 9999), float("nan"), R.random() * 100])
            y = int(p[:4])
            rows.append(dict(series_no=sid, series_name=name, unit=unit, period=p, year=y, value=v))
            if R.random() < 0.03:
                rows.append(
                    dict(series_no=sid, series_name=name, unit=unit, period=p, year=y, value=v + 1)
                )
    if not rows:
        rows.append(
            dict(
                series_no="1",
                series_name="Total",
                unit="Number",
                period="2023",
                year=2023,
                value=1.0,
            )
        )
    state["df"] = pd.DataFrame(rows)
    state["meta"] = {
        "table_id": "M810001",
        "title": R.choice(["Some Table, Annual", None]),
        "table_complete": R.choice([True, True, False]),
        "coverage_warning": "partial",
        "source_url": "https://tablebuilder.singstat.gov.sg/table/TS/M810001",
    }
    state["cube"] = R.choice([None, {"success": True, "cube": 1}, {"success": False, "cube": 0}])
    R.choice([True, False])  # (kept so every later random draw stays the same)
    args = dict(
        series_search=R.choice(
            [None, None, "Total", "Males", "series id:1.1", "series id:2", "nothing here", "Oil"]
        ),
        start_year=R.choice([None, 2000, 2021]),
        end_year=R.choice([None, 2022]),
        max_series=R.choice([1, 3, 8]),
        make_chart=R.choice([True, False]),
    )
    try:
        res = core.query_singstat_table("M810001", **args)
    except Exception as e:
        res = {"RAISED": type(e).__name__ + str(e)}
    k = (
        res.get("success"),
        (res.get("chart_spec") or {}).get("kind"),
        bool(res.get("latest_comparison_spec")),
        " / " in str((res.get("chart_spec") or {}).get("title")),
        "RAISED" in res,
        res.get("cube") is not None,
    )
    kinds[k] = kinds.get(k, 0) + 1
    if isinstance(res, dict):  # older versions also returned (always empty) PNG fields
        res.pop("chart_file", None)
        res.pop("chart_files", None)
    out.append(
        hashlib.sha256(json.dumps(res, sort_keys=True, default=str).encode()).hexdigest()[:12]
    )

print(sorted(kinds.items(), key=str))
print(hashlib.sha256("".join(out).encode()).hexdigest(), len(set(out)), "distinct results")
