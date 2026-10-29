"""Characterise the three answer-checking functions on REAL Sage answers.

Built from three real answers (tests/fixtures/real_answers.py) and their
official observations, varied systematically: every question wording x every
evidence set x every answer text (the real answer, the checked table Sage
writes itself, latest-year and ranked category tables, and copies with one
number altered). The functions checked are:

  utils.answer_verification.evidence_bound_generic_answer
  utils.answer_verification.structured_endpoint_chart_from_answer
  utils.chart_selection.verified_same_year_categories_from_answer
  utils.chart_selection.verified_single_series_trend_candidate

Prints outcome counts and a digest: two copies of Sage whose checks behave the
same print the same digest.

    python tools/characterise_answer_checks.py OLD_SAGE_DIR
    python tools/characterise_answer_checks.py NEW_SAGE_DIR
"""

import copy
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
import logging  # noqa: E402

logging.disable(logging.CRITICAL)
import importlib.util  # noqa: E402

# The fixtures always come from THIS copy of Sage, so two copies are compared on
# exactly the same cases (the code under test comes from the folder given).
_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)
(
    ANSWER_C,
    ANSWER_C_AREAS,
    ANSWER_C_AREAS_BAD_TOTAL,
    ANSWER_C_LATEST,
    ANSWER_C_RANKED,
    EVIDENCE_SETS,
    QUESTIONS,
    TABLES,
    tool_result,
) = (
    getattr(_fixtures, name)
    for name in (
        "ANSWER_C",
        "ANSWER_C_AREAS",
        "ANSWER_C_AREAS_BAD_TOTAL",
        "ANSWER_C_LATEST",
        "ANSWER_C_RANKED",
        "EVIDENCE_SETS",
        "QUESTIONS",
        "TABLES",
        "tool_result",
    )
)
from utils.answer_verification import (
    evidence_bound_generic_answer,  # noqa: E402
    structured_endpoint_chart_from_answer,
)
from utils.chart_selection import (
    verified_same_year_categories_from_answer,  # noqa: E402
    verified_single_series_trend_candidate,
)


def altered(text):
    """The same text with its last number changed by one (a wrong figure)."""
    numbers = list(re.finditer(r"\d[\d,]*\.\d+", text or ""))
    if not numbers:
        return None
    last = numbers[-1]
    return text[: last.start()] + "9" + text[last.start() :]


def evidence_variants(table_id, series_sets):
    """Each series set, plus copies with an unofficial source or an incomplete table."""
    for numbers in series_sets:
        yield "+".join(numbers), [tool_result(table_id, numbers)]
    yield "all", [tool_result(table_id, numbers) for numbers in series_sets]
    good = tool_result(table_id, series_sets[0])
    yield "unofficial", [tool_result(table_id, series_sets[0], url="https://example.com/x")]
    yield "incomplete", [tool_result(table_id, series_sets[0], complete=False)]
    yield "duplicate", [good, copy.deepcopy(good)]
    yield (
        "other_table",
        [
            good,
            tool_result(
                next(t for t in TABLES if t != table_id),
                list(TABLES[next(t for t in TABLES if t != table_id)][2])[:1],
            ),
        ],
    )


def trend_sentence(table_id, summary, *, commas):
    """A one-series answer in the style the model writes ("X rose from A in P to B in Q")."""

    def number(value):
        return f"{value:,}" if commas else repr(float(value))

    return (
        f"{summary['series_name']} changed from {number(summary['earliest_value'])} in "
        f"{summary['earliest_period']} to {number(summary['latest_value'])} in "
        f"{summary['latest_period']}. Source: SingStat {table_id}."
    )


def answers_for(table_id, evidence, question):
    """Answer texts to check against this evidence."""
    structured = evidence_bound_generic_answer(question, evidence)
    texts = {
        "structured": structured,
        "structured_altered": altered(structured),
        "empty": "",
        "prose": f"The figures come from SingStat {table_id}.",
    }
    first = evidence[0]["series_summary"][0]
    texts["trend"] = trend_sentence(table_id, first, commas=True)
    texts["trend_plain"] = trend_sentence(table_id, first, commas=False)
    texts["trend_altered"] = altered(texts["trend"])
    if table_id == "M081331":
        texts.update(
            real=ANSWER_C,
            latest=ANSWER_C_LATEST,
            ranked=ANSWER_C_RANKED,
            latest_altered=altered(ANSWER_C_LATEST),
            ranked_altered=altered(ANSWER_C_RANKED),
            areas=ANSWER_C_AREAS,
            areas_bad_total=ANSWER_C_AREAS_BAD_TOTAL,
        )
    return {name: text for name, text in texts.items() if text is not None}


def main():
    digests, outcomes = [], {}
    for table_id, series_sets in EVIDENCE_SETS.items():
        for question in QUESTIONS[table_id]:
            for evidence_name, evidence in evidence_variants(table_id, series_sets):
                bound = evidence_bound_generic_answer(question, copy.deepcopy(evidence))
                record = {"bound": bound}
                outcomes["bound:" + ("text" if bound else "none")] = (
                    outcomes.get("bound:" + ("text" if bound else "none"), 0) + 1
                )
                for name, text in answers_for(table_id, evidence, question).items():
                    endpoint = structured_endpoint_chart_from_answer(
                        question, text, copy.deepcopy(evidence)
                    )
                    response = {"tool_results": copy.deepcopy(evidence)}
                    same_year = verified_same_year_categories_from_answer(response, question, text)
                    suppressed = verified_same_year_categories_from_answer(
                        {**response, "suppress_charts": True}, question, text
                    )
                    trends = [
                        verified_single_series_trend_candidate(question, copy.deepcopy(item), text)
                        for item in evidence
                    ]
                    trend = next((t for t in trends if t), None)
                    record[name] = [endpoint, same_year, suppressed, trends]
                    for label, value in (
                        ("endpoint", endpoint),
                        ("same_year", same_year),
                        ("single_trend", trend),
                    ):
                        key = f"{label}:{'chart' if value else 'none'}"
                        outcomes[key] = outcomes.get(key, 0) + 1
                digests.append(
                    hashlib.sha256(
                        json.dumps(
                            [table_id, question, evidence_name, record], sort_keys=True, default=str
                        ).encode()
                    ).hexdigest()[:12]
                )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(digests), "cases")


if __name__ == "__main__":
    main()
