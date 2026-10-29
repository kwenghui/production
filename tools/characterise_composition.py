"""Characterise category_hierarchy.review_composition on 3,000 composition charts.

Categories and values come from the real tables in tests/fixtures/real_answers.py
(GVA shares by industry, R&D by sector and by research area, latest periods),
plus parent rows whose value is (or is not) the exact sum of their children,
"Total" rows, look-alike names and "Non-" siblings. Each chart is checked with
and without official series IDs. Prints outcome counts and a digest: two copies
of Sage that behave the same print the same digest.

    python tools/characterise_composition.py OLD_SAGE_DIR
    python tools/characterise_composition.py NEW_SAGE_DIR
"""

import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from utils.category_hierarchy import review_composition  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)


def latest_rows(table_id):
    """(name, series number, latest value) for every series of a fixture table."""
    return [
        (name, number, values[-1])
        for number, (name, _, values) in fixtures.TABLES[table_id][2].items()
    ]


POOLS = {
    "M015781": latest_rows("M015781")
    + [
        ("Manufacturing", "1.1", 17.0),
        ("Construction", "1.2", 4.1),
        ("Utilities", "1.3", 1.4),
        ("Other Goods Industries", "1.4", 1.6),
        ("Other Services", "2.7", 10.0),
        ("Other Services - Others", "2.7.5", 1.2),
        ("Non-Other Services", "2.8", 2.0),
        ("Total", "0", 100.0),
        ("Overall", "00", 100.0),
    ],
    "M081331": latest_rows("M081331")
    + [
        ("Total", "1", 13746.91),
        ("Private Sector Engineering & Technology", "2.3", 7000.0),
        ("Private Sector Biomedical & Related Sciences", "2.4", 1955.99),
    ],
}


def main():
    rng = random.Random(4)
    digests, outcomes = [], {}
    for _ in range(3000):
        table_id = rng.choice(list(POOLS))
        rows = rng.sample(POOLS[table_id], rng.randint(2, min(9, len(POOLS[table_id]))))
        if rng.random() < 0.15:  # one value perturbed: roll-ups must stop reconciling
            name, number, value = rows[0]
            rows[0] = (name, number, round(value + rng.choice([-1, 0.5, 3]), 2))
        if rng.random() < 0.05:
            rows[-1] = (rows[-1][0], rows[-1][1], rng.choice([None, -5.0, float("nan")]))
        unit = fixtures.TABLES[table_id][1] if rng.random() < 0.85 else rng.choice(["%", "Index"])
        spec = {
            "kind": rng.choice(["bar", "bar", "donut", "trend"]),
            "unit": unit,
            "categories": [r[0] for r in rows],
            "series": [{"name": "Value", "values": [r[2] for r in rows]}],
        }
        selected = (
            {"selected_series": [{"series_name": r[0], "series_no": r[1]} for r in rows]}
            if rng.random() < 0.6
            else None
        )
        result = review_composition(spec, selected)
        outcomes[result[2]] = outcomes.get(result[2], 0) + 1
        digests.append(
            hashlib.sha256(
                json.dumps([spec, selected, result], sort_keys=True, default=str).encode()
            ).hexdigest()[:12]
        )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
