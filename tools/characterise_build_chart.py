"""Characterise utils.charts.build_chart on 2,000 random chart specs.

Plotly is replaced by a recorder that logs every trace and layout call with
its arguments, so the exact figure each spec produces can be compared without
drawing anything. Prints a digest: two copies of Sage that build the same
figures print the same digest.

    python tools/characterise_build_chart.py OLD_SAGE_DIR
    python tools/characterise_build_chart.py NEW_SAGE_DIR
"""

import hashlib
import json
import random
import sys
import types

sys.path.insert(0, sys.argv[1])
calls = []


class _Trace(dict):
    """A recorded trace: its type and keyword arguments."""


def _trace_type(name):
    return lambda **kwargs: _Trace(type=name, **kwargs)


class Figure:
    """Records add_trace / update_* / add_annotation calls in order."""

    def __getattr__(self, method):
        return lambda *args, **kwargs: calls.append((method, args, kwargs))


go = types.ModuleType("plotly.graph_objects")
go.Figure, go.Scatter, go.Bar, go.Pie = (
    Figure,
    _trace_type("Scatter"),
    _trace_type("Bar"),
    _trace_type("Pie"),
)
plotly = types.ModuleType("plotly")
plotly.graph_objects = go
sys.modules.update({"plotly": plotly, "plotly.graph_objects": go})
from utils.charts import build_chart  # noqa: E402

R = random.Random(3)
LABEL_SETS = [
    [str(y) for y in range(2010, 2024)],
    ["1957", "1960", "1980", "1990", "2020"],
    ["2023 1Q", "2022 4Q", "2023 2Q", "2022 3Q"],
    ["Jan 2024", "Mar 2024", "Feb 2024"],
    [
        "Manufacturing",
        "Construction",
        "Services",
        "Others",
        "Utilities",
        "A very long official category name here",
    ],
    ["Male", "Female"],
    ["Age 0-4", "Age 5-9", "Age 10-14"],
]


def random_spec():
    labels = list(R.choice(LABEL_SETS))
    kind = R.choice(["trend", "bar", "grouped_bar", "donut", "pyramid"])
    n_series = 1 if kind in ("bar", "donut") else R.randint(1, 4)
    series = []
    for index in range(n_series):
        values = [R.choice([None, R.randint(0, 9999), round(R.random() * 100, 2)]) for _ in labels]
        item = {"name": f"Series {index} " + "word " * R.randint(0, 20), "values": values}
        if R.random() < 0.2:
            item["actual_values"] = [v for v in values]
            item["actual_unit"] = "Dollars"
        series.append(item)
    spec = {
        "kind": kind,
        "title": "Some official title",
        "categories": labels,
        "series": series,
        "unit": R.choice(["Number", "Per Cent", "%", "Thousand", "Index", ""]),
    }
    if R.random() < 0.3:
        spec["x_axis_type"] = "linear_years"
    if R.random() < 0.3:
        spec["sort_by_value"] = R.choice([True, False, "ascending", "descending"])
    if R.random() < 0.15:
        spec["mixed_original_units"] = True
    if R.random() < 0.05:
        spec["series"] = []
    return spec


digests, outcomes = [], {}
for _ in range(2000):
    calls.clear()
    spec = random_spec()
    try:
        build_chart(spec)
        outcome = "ok:" + spec["kind"]
        record = calls
    except Exception as error:  # noqa: BLE001 - invalid specs are part of the check
        outcome = type(error).__name__
        record = [outcome, str(error)]
    outcomes[outcome] = outcomes.get(outcome, 0) + 1
    digests.append(
        hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()[:12]
    )
print(sorted(outcomes.items()))
print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")
