"""Characterise scope_chart_bridge.reconcile_scoped_preview on REAL Sage data.

The preview narrowing behind "Explore table further" is checked with the three
real answers in tests/fixtures/real_answers.py: every multi-series result
(and copies with one thing broken: source link, completeness, a value, a
unit, a series number, the year axis), against many question wordings and
answer texts. Records the decision AND its reason for every case and prints
a digest: two copies of Sage that behave the same print the same digest.

    python tools/characterise_scoped_preview.py OLD_SAGE_DIR
    python tools/characterise_scoped_preview.py NEW_SAGE_DIR
"""

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from utils.scope_chart_bridge import reconcile_scoped_preview  # noqa: E402

# Fixtures always come from THIS copy, so two copies are compared on the same cases.
_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)

SERIES_SETS = {
    "M015781": [["1", "2"], ["1", "2", "3"], ["2", "3"]],
    "M451111": [["1", "2"], ["1", "1.1"]],
    "M081331": [["2", "3", "4"], ["2", "4"], ["1.3", "1.4", "1.5"], ["2", "1.1"]],
}
EXTRA_QUESTIONS = [
    "Compare these series over time",
    "How has the female share changed?",
    "How have residents aged 25 to 34 changed?",
    "How has research and development expenditure for those aged 25 to 34 changed?",
    "M015781 trend please",
    "How has engineering changed?",
    "How has private sector spending changed by sector?",
]


def broken_copies(result):
    """The result itself, then copies that each break one guarantee."""
    yield "ok", result
    for name, change in (
        ("unofficial_url", lambda r: r.update(source_url="https://example.com/t")),
        ("incomplete", lambda r: r.update(table_complete=False)),
        ("unverified_chart", lambda r: r.update(chart_source_verified=False)),
        ("no_claims", lambda r: r.pop("numeric_claims")),
        (
            "altered_value",
            lambda r: r["chart_spec"]["series"][0]["values"].__setitem__(1, 123456.789),
        ),
        ("unit_mismatch", lambda r: r["chart_spec"].update(unit="Index")),
        ("id_mismatch", lambda r: r["selected_series"][0].update(series_no="99")),
        ("bar_chart", lambda r: r["chart_spec"].update(kind="bar")),
        ("one_line", lambda r: r["chart_spec"].update(series=r["chart_spec"]["series"][:1])),
        ("endpoint_mismatch", lambda r: r["series_summary"][0].update(earliest_value=-1.0)),
        ("missing_value", lambda r: r["chart_spec"]["series"][0]["values"].__setitem__(0, None)),
        ("bad_claims", lambda r: r.update(numeric_claims=["x"])),
        ("failed", lambda r: r.update(success=False)),
        ("bad_metadata", lambda r: r["selected_series"].__setitem__(0, "x")),
    ):
        broken = copy.deepcopy(result)
        try:
            change(broken)
        except (IndexError, KeyError):
            continue
        yield name, broken


def answers(table_id, result):
    """Answer texts: naming every series, only the first, none, and a different table."""
    names = [s["series_name"] for s in result["series_summary"]]
    all_named = (
        f"SingStat {table_id}: "
        + "; ".join(
            f"{s['series_name']} went from {s['earliest_value']:,} in {s['earliest_period']} to "
            f"{s['latest_value']:,} in {s['latest_period']}"
            for s in result["series_summary"]
        )
        + "."
    )
    texts = {
        "all_named": all_named,
        "first_only": f"SingStat {table_id}: {names[0]} rose.",
        "no_table": " ".join(names),
        "lower_case": all_named.lower(),
    }
    if table_id == "M081331":
        texts["real"] = fixtures.ANSWER_C
    return texts


def main():
    digests, outcomes = [], {}
    for table_id, sets in SERIES_SETS.items():
        for numbers in sets:
            base = fixtures.tool_result(table_id, numbers)
            for broken_name, result in broken_copies(base):
                for question in fixtures.QUESTIONS[table_id] + EXTRA_QUESTIONS:
                    for answer_name, answer in answers(table_id, base).items():
                        candidate, reason = reconcile_scoped_preview(
                            question, copy.deepcopy(result), answer
                        )
                        outcomes[reason] = outcomes.get(reason, 0) + 1
                        digests.append(
                            hashlib.sha256(
                                json.dumps(
                                    [
                                        table_id,
                                        numbers,
                                        broken_name,
                                        question,
                                        answer_name,
                                        candidate,
                                        reason,
                                    ],
                                    sort_keys=True,
                                    default=str,
                                ).encode()
                            ).hexdigest()[:12]
                        )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(digests), "cases")


if __name__ == "__main__":
    main()
