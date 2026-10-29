"""Characterise chart_selection.chart_matches_question on real-answer data.

Chart candidates are built from the three real tables in
tests/fixtures/real_answers.py (single and multi-series, generic previews,
bar instead of trend, one period only, unlabelled), then checked against many
question wordings (trend, age band, subgroup, explicit table ID, comparison)
and answer texts. Prints outcome counts and a digest: two copies of Sage that
decide the same print the same digest.

    python tools/characterise_chart_relevance.py SAGE_DIR
"""

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from utils.chart_selection import chart_matches_question  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)

SETS = {
    "M015781": [["1"], ["2"], ["1", "2"]],
    "M451111": [["1"], ["1.1"], ["1", "2"]],
    "M081331": [["2"], ["2", "3", "4"], ["1.3"], ["1.1", "1.3"]],
}
EXTRA_QUESTIONS = [
    "Which sector had the highest R&D expenditure?",
    "Compare private sector and institutes of higher learning R&D expenditure",
    "Show M081331 trend",
    "Show M015781 over time",
    "How has the female labour force changed?",
    "How have residents aged 25 to 34 changed since 2000?",
    "Compare children and the elderly over time",
    "How has insurance changed?",
    "What is the latest value?",
    "How have private sector research expenditure grown since 1990?",
]


def variants(result):
    """The candidate itself and copies changed in one way each."""
    yield "plain", result
    changes = {
        "generic_preview": lambda r: r.update(generic_preview=True, chart_kind="time_series"),
        "generic_bar": lambda r: (
            r.update(generic_preview=True, chart_kind="category_comparison"),
            r["chart_spec"].update(
                kind="bar",
                categories=[s["name"] for s in r["chart_spec"]["series"]],
                series=[
                    {
                        "name": "Value",
                        "values": [s["values"][-1] for s in r["chart_spec"]["series"]],
                    }
                ],
            ),
        ),
        "generic_repeated": lambda r: (
            r.update(generic_preview=True, chart_kind="time_series"),
            [s.update(name="Total") for s in r["chart_spec"]["series"]],
        ),
        "bar": lambda r: r["chart_spec"].update(kind="bar"),
        "one_period": lambda r: r["chart_spec"].update(
            categories=r["chart_spec"]["categories"][:1]
        ),
        "unlabelled": lambda r: (
            r["chart_spec"].update(title=""),
            r.update(title="", table_title=""),
            [s.update(name="") for s in r["chart_spec"]["series"]],
        ),
        "failed": lambda r: r.update(success=False),
        "age_label": lambda r: r["chart_spec"].update(categories=["Aged 25-34", "Aged 35-44"]),
    }
    for name, change in changes.items():
        copied = copy.deepcopy(result)
        change(copied)
        yield name, copied


def answers(table_id, result):
    names = " and ".join(s["series_name"] for s in result["series_summary"])
    return {
        "names": f"SingStat {table_id}: {names} changed over time.",
        "empty": "",
        "other": "Population figures are published by SingStat.",
    }


def main():
    digests, outcomes = [], {}
    for table_id, sets in SETS.items():
        for numbers in sets:
            base = fixtures.tool_result(table_id, numbers)
            for variant, result in variants(base):
                for question in fixtures.QUESTIONS[table_id] + EXTRA_QUESTIONS:
                    for answer_name, answer in answers(table_id, base).items():
                        decision = chart_matches_question(question, copy.deepcopy(result), answer)
                        outcomes[decision] = outcomes.get(decision, 0) + 1
                        digests.append(
                            hashlib.sha256(
                                json.dumps(
                                    [table_id, numbers, variant, question, answer_name, decision]
                                ).encode()
                            ).hexdigest()[:12]
                        )
    print(sorted(outcomes.items(), key=str))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(digests), "cases")


if __name__ == "__main__":
    main()
