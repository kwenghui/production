"""Characterise utils.lightweight_charts.to_altair_chart on 1,500 random chart specs.

Altair is replaced by a recorder: every chart, encoding, axis, scale, mark and
layer call is captured as a nested record, so the exact chart a spec produces
can be compared without drawing it. Prints a digest: two copies of Sage that
build the same charts print the same digest.

    python tools/characterise_lightweight_chart.py OLD_SAGE_DIR
    python tools/characterise_lightweight_chart.py NEW_SAGE_DIR
"""

import hashlib
import json
import random
import sys
import types

sys.path.insert(0, sys.argv[1])


class Rec:
    """A recorded Altair object: what created it, and the methods called to derive it."""

    def __init__(self, name, args=(), kwargs=None):
        self.name, self.args, self.kwargs, self.calls = name, args, kwargs or {}, []

    def __getattr__(self, method):
        if method.startswith("__"):
            raise AttributeError(method)

        def call(*args, **kwargs):
            if method == "to_dict":
                return {}
            # Like Altair, every method returns a NEW object; the original is unchanged.
            copy = Rec(self.name, self.args, self.kwargs)
            copy.calls = self.calls + [(method, args, kwargs)]
            return copy

        return call

    def as_data(self):
        return [self.name, _data(self.args), _data(self.kwargs), _data(self.calls)]


def _data(value):
    if isinstance(value, Rec):
        return value.as_data()
    if isinstance(value, dict):
        return {str(k): _data(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_data(v) for v in value]
    return value if isinstance(value, (str, int, float, bool, type(None))) else repr(value)


altair = types.ModuleType("altair")
altair.__getattr__ = lambda name: (lambda *args, **kwargs: Rec(name, args, kwargs))
sys.modules["altair"] = altair
from utils.lightweight_charts import to_altair_chart  # noqa: E402

R = random.Random(9)
LABEL_SETS = [
    [str(y) for y in range(2010, 2024)],
    ["1957", "1960", "1980", "1990", "2020"],
    [f"{y} {q}Q" for y in range(2015, 2024) for q in range(1, 5)],
    ["Jan 2024", "Feb 2024", "Mar 2024"],
    [
        "Manufacturing",
        "Construction",
        "Services",
        "Others",
        "Utilities",
        "Real Estate, Professional Services And Administrative & Support Services",
    ],
    ["Male", "Female"],
    [f"Category {i}" for i in range(30)],
]


def random_spec():
    labels = list(R.choice(LABEL_SETS))
    kind = R.choice(["trend", "bar", "grouped_bar", "donut"])
    n_series = 1 if kind in ("bar", "donut") else R.randint(1, 6)
    series = []
    for index in range(n_series):
        values = [
            R.choice([None, R.randint(-50, 9_999_999), round(R.random() * 100, 2)]) for _ in labels
        ]
        name = R.choice(
            [
                "Total",
                "Value",
                "Total · Total",
                f"Series {index}",
                "Goods Producing Industries",
                "Total / Agricultural & Food Sciences",
            ]
        )
        series.append({"name": f"{name} {index}" if n_series > 1 else name, "values": values})
    return {
        "kind": kind,
        "title": R.choice(
            ["", "Some Official Title, Annual", "A Very Long Official Table Title " * 4]
        ),
        "categories": labels,
        "series": series,
        "unit": R.choice(["Number", "Per Cent", "Thousand Dollars", "Index", ""]),
    }


digests, outcomes = [], {}
for _ in range(1500):
    spec = random_spec()
    mode, all_ticks = R.choice(["Balanced", "All", "Minimal"]), R.random() < 0.2
    try:
        record = _data(to_altair_chart(spec, label_mode=mode, all_ticks=all_ticks))
        outcome = "ok:" + spec["kind"]
    except Exception as error:  # noqa: BLE001 - invalid specs are part of the check
        record, outcome = [type(error).__name__, str(error)], type(error).__name__
    outcomes[outcome] = outcomes.get(outcome, 0) + 1
    digests.append(
        hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()[:12]
    )
print(sorted(outcomes.items()))
print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")
