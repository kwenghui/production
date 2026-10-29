"""Characterise model_evidence._safe_observation_summary (the bounded overview the model sees).

Random verified results vary period labels (annual, quarterly, duplicates),
values (numbers, numeric strings, missing markers, booleans, junk), mixed
units/series and the size limit. Prints a digest of every overview: two
copies of Sage that summarise the same print the same digest.

    python tools/characterise_evidence_summary.py SAGE_DIR
"""

import hashlib
import json
import random
import sys

sys.path.insert(0, sys.argv[1])
from utils.model_evidence import _safe_observation_summary  # noqa: E402

R = random.Random(37)
VALUES = [
    lambda: R.randint(0, 9999),
    lambda: round(R.random() * 100, 2),
    lambda: str(R.randint(1, 99)),
    lambda: None,
    lambda: "na",
    lambda: "-",
    lambda: True,
    lambda: "abc",
    lambda: "1e400",
]


def random_result():
    years = R.sample(range(1990, 2025), R.randint(1, 8))
    periods = [str(y) for y in years]
    if R.random() < 0.1:
        periods[0] = "2023 1Q"
    if R.random() < 0.08 and len(periods) > 1:
        periods[1] = periods[0]
    rows = []
    for p in periods:
        row = {"period": p, "value": R.choice(VALUES[:4] * 3 + VALUES[4:])()}
        for field in ("unit", "series"):
            if R.random() < 0.5:
                row[field] = (
                    R.choice(["Number", "Number", "Per Cent"]) if field == "unit" else ("Total")
                )
        rows.append(row)
    result = {
        "success": True,
        "verified": R.random() < 0.9,
        "observations": rows,
        "table_id": "M810001",
        "series": "Total",
        "unit": "Number",
    }
    if R.random() < 0.05:
        result["observations"] = rows + ["not a row"]
    return result


def main():
    digests, outcomes = [], {}
    for _ in range(3000):
        summary = _safe_observation_summary(random_result(), R.choice([60, 200, 400, 2000]))
        key = "none" if summary is None else "summary"
        outcomes[key] = outcomes.get(key, 0) + 1
        digests.append(hashlib.sha256(json.dumps(summary).encode()).hexdigest()[:12])
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
