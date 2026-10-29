"""Characterise the chart gallery (utils/chart_gallery.py) on real-table data.

Frames and tool results are built from tests/fixtures/real_answers.py and
varied (series subsets, missing values, an extra unit, repeated labels, very
few periods). Checks graphs_from_complete_table, table_series_charts,
group_selected_for_charts (with random selections of the chart-ready series)
and _verified_tool_charts. Prints a digest: two copies of Sage that build the
same galleries print the same digest.

    python tools/characterise_chart_gallery.py SAGE_DIR
"""

import copy
import hashlib
import importlib.util
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import logging  # noqa: E402

import pandas as pd  # noqa: E402

logging.disable(logging.CRITICAL)
from utils import chart_gallery as gallery  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
R = random.Random(17)


def frame(table_id, numbers, change):
    """A table frame (the shape the table reader returns), altered in one way."""
    title, unit, series = fixtures.TABLES[table_id]
    rows = []
    for n in numbers:
        name, periods, values = series[n]
        for p, v in zip(periods, values):
            rows.append(
                {
                    "series_no": n,
                    "series_name": name,
                    "unit": unit,
                    "period": p,
                    "year": int(p[:4]),
                    "value": v,
                }
            )
    if change == "missing" and rows:
        for row in R.sample(rows, max(1, len(rows) // 5)):
            row["value"] = math.nan
    elif change == "extra_unit" and rows:
        rows += [
            dict(row, series_no="9", series_name="Index Version", unit="Index") for row in rows[:10]
        ]
    elif change == "repeated_label" and len(numbers) > 1:
        first = series[numbers[0]][0]
        rows = [dict(row, series_name=first) for row in rows]
    elif change == "two_periods":
        keep = sorted({row["period"] for row in rows})[-2:]
        rows = [row for row in rows if row["period"] in keep]
    meta = {
        "table_id": table_id,
        "title": title,
        "table_complete": True,
        "source_url": f"https://tablebuilder.singstat.gov.sg/table/TS/{table_id}",
    }
    return pd.DataFrame(rows), meta


def run(call):
    try:
        return call()
    except Exception as error:  # noqa: BLE001 - refusals are part of the recorded behaviour
        return {"raised": type(error).__name__, "message": str(error)[:160]}


def main():
    digests, outcomes = [], {}
    for _ in range(600):
        table_id = R.choice(list(fixtures.EVIDENCE_SETS))
        numbers = R.choice(fixtures.EVIDENCE_SETS[table_id])
        change = R.choice(
            ["none", "none", "missing", "extra_unit", "repeated_label", "two_periods"]
        )
        df, meta = frame(table_id, numbers, change)
        graphs = run(lambda: gallery.graphs_from_complete_table(table_id, df.copy(), dict(meta)))
        catalogue = run(lambda: gallery.table_series_charts(table_id, df.copy(), dict(meta)))
        grouped = None
        entries = (
            (catalogue.get("series") or catalogue.get("entries") or [])
            if isinstance(catalogue, dict)
            else []
        )
        if entries:
            chosen = R.sample(entries, R.randint(1, len(entries)))
            grouped = run(
                lambda: gallery.group_selected_for_charts(
                    table_id,
                    copy.deepcopy(chosen),
                    max_charts=R.choice([1, 2, 6]),
                    table_title=R.choice([None, meta["title"]]),
                )
            )
        result = fixtures.tool_result(table_id, numbers)
        if R.random() < 0.3:
            result["chart_source_verified"] = False
        tool_charts = run(lambda: list(gallery._verified_tool_charts(result)))
        record = [graphs, catalogue, grouped, tool_charts]
        outcomes[change] = outcomes.get(change, 0) + 1
        digests.append(
            hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()[
                :12
            ]
        )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
