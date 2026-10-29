"""Characterise cube_slice.crosstab_overview and cube_query on 1,500 random cube tables.

Multi-dimensional SingStat tables ("cubes") nest columns as period > group >
measure. The random cubes vary the number of rows, periods (years, quarters,
months), column groups and measures, include Total rows, footnote markers,
missing cells ("na"), repeated labels and broken nesting. Each cube is sliced
automatically, by a chosen group/period, and queried by series name and year
range. Prints outcome counts and a digest: two copies of Sage that slice the
same print the same digest.

    python tools/characterise_cube_slices.py SAGE_DIR
"""

import hashlib
import json
import random
import sys

sys.path.insert(0, sys.argv[1])
from utils.cube_slice import crosstab_overview, cube_query  # noqa: E402

R = random.Random(13)
ROWS = ["Total", "Food", "Housing", "Transport", "Health", "Education", "Others 1/", "Food"]
GROUPS = ["Total", "Lowest 20%", "Middle 20%", "Highest 20%", "Average ($)", "Median 2/"]
MEASURES = ["Amount ($)", "Share (%)"]


def periods():
    kind = R.choice(["year", "quarter", "month"])
    if kind == "year":
        return [str(y) for y in R.sample(range(2008, 2024), R.randint(1, 4))]
    if kind == "quarter":
        return [f"2023 {q}Q" for q in range(1, R.randint(2, 5))]
    return [f"2023 {m}" for m in R.sample(["Jan", "Feb", "Mar", "Apr"], R.randint(1, 3))]


def cell():
    return R.choice([str(round(R.random() * 1000, 1)), str(R.randint(0, 99)), "na", "-", ""])


def random_cube():
    ps, gs = periods(), R.sample(GROUPS, R.randint(1, 4))
    nested = R.random() < 0.7
    ms = R.sample(MEASURES, R.randint(1, 2)) if nested else []
    rows = []
    for name in R.sample(ROWS, R.randint(1, 6)):
        columns = []
        for p in ps:
            groups = []
            for g in gs:
                if ms:
                    groups.append({"key": g, "columns": [{"key": m, "value": cell()} for m in ms]})
                else:
                    groups.append({"key": g, "value": cell()})
            columns.append({"key": p, "columns": groups})
        if R.random() < 0.05:
            columns.append({"key": "broken"})
        rows.append(
            {
                "rowText": name,
                "seriesNo": str(len(rows) + 1),
                "columns": columns,
                "footnote": R.choice(["", "1/ Includes imputed rent."]),
            }
        )
    # The "Data" part of an official response (what load_table_payload returns).
    return {
        "id": "D12345",
        "title": "Household Expenditure By Type, Annual",
        "row": rows,
        "footnote": R.choice(["", "2/ Median of households."]),
    }


def run(call):
    try:
        return call()
    except Exception as error:  # noqa: BLE001 - refusals are part of the recorded behaviour
        return {"raised": type(error).__name__, "message": str(error)[:200]}


def main():
    digests, outcomes = [], {}
    for _ in range(1500):
        cube = random_cube()
        group = R.choice([None, "Total", "Lowest 20%", "Nonexistent"])
        period = R.choice([None, "2023", "2023 1Q", "1999"])
        results = [
            run(
                lambda: crosstab_overview(
                    cube,
                    table_id="D12345",
                    title="t",
                    source_url="https://tablebuilder.singstat.gov.sg/table/CT/D12345",
                )
            ),
            run(
                lambda: crosstab_overview(
                    cube,
                    table_id="D12345",
                    title="t",
                    group=group,
                    period=period,
                    source_url="https://tablebuilder.singstat.gov.sg/table/CT/D12345",
                )
            ),
            run(
                lambda: cube_query(
                    cube,
                    series_search=R.choice([None, "Food", "Lowest 20%", "Average"]),
                    start_year=R.choice([None, 2010, 2023]),
                    end_year=R.choice([None, 2023]),
                    max_rows=R.choice([1, 3, 8]),
                )
            ),
        ]
        for result in results:
            key = result.get("raised", "ok") if isinstance(result, dict) else "other"
            outcomes[key] = outcomes.get(key, 0) + 1
        digests.append(
            hashlib.sha256(json.dumps(results, sort_keys=True, default=str).encode()).hexdigest()[
                :12
            ]
        )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
