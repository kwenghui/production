"""Characterise answer_verification.retry_verified_series_for_numeric_failure.

When an answer's numbers could not be verified, Sage may re-query the SAME
official table once for typed series records. Responses and the re-query
result are built from the real tables in tests/fixtures/real_answers.py and
varied (blocked responses, several tables, unrelated titles, failing or
incomplete re-queries, truncated or mislabelled charts). Prints outcome counts
and a digest: two copies of Sage that decide the same print the same digest.

    python tools/characterise_numeric_retry.py SAGE_DIR
"""

import copy
import hashlib
import importlib.util
import json
import random
import sys
import types
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import logging  # noqa: E402

logging.disable(logging.CRITICAL)
from utils.answer_verification import retry_verified_series_for_numeric_failure  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
R = random.Random(5)
QUESTIONS = [q for qs in fixtures.QUESTIONS.values() for q in qs] + [
    "How has expenditure on research and development changed since 1990?",
    "How have imports of machinery equipment changed over time?",
    "What is the latest value?",
]


def discovered(table_id):
    """A tool result that found the table but carries no typed series summary."""
    result = fixtures.tool_result(table_id, fixtures.EVIDENCE_SETS[table_id][0])
    result.pop("series_summary")
    return result


def requeried(table_id):
    """What the re-query returns, broken in one random way (or not at all)."""
    result = fixtures.tool_result(table_id, R.choice(fixtures.EVIDENCE_SETS[table_id]))
    result["series_count_available"] = R.choice([3, 8, 26])
    change = R.choice(
        ["none"] * 5
        + [
            "fail",
            "other_table",
            "incomplete",
            "truncated",
            "unofficial",
            "unverified_chart",
            "renamed_line",
            "no_summary",
            "raise",
        ]
    )
    if change == "fail":
        result["success"] = False
    elif change == "other_table":
        result["table_id"] = "M999999"
    elif change == "incomplete":
        result["table_complete"] = False
    elif change == "truncated":
        result["series_selection_truncated"] = True
    elif change == "unofficial":
        result["source_url"] = "https://example.com"
    elif change == "unverified_chart":
        result["chart_source_verified"] = False
    elif change == "renamed_line":
        result["chart_spec"]["series"][0]["name"] = "Something else"
    elif change == "no_summary":
        result["series_summary"] = []
    elif change == "raise":
        return ValueError("offline")
    return result


def main():
    digests, outcomes = [], {}
    tables = list(fixtures.EVIDENCE_SETS)
    for _ in range(2500):
        chosen = R.sample(tables, R.choice([1, 1, 1, 2]))
        response = {"tool_results": [discovered(t) for t in chosen]}
        for flag in ("security_blocked", "scope_blocked", "table_catalogue_listing"):
            if R.random() < 0.04:
                response[flag] = True
        reply = requeried(chosen[0])

        def query(table_id, **kwargs):
            if isinstance(reply, Exception):
                raise reply
            return copy.deepcopy(reply)

        core = types.SimpleNamespace(query_singstat_table=query) if R.random() < 0.95 else object()
        result = retry_verified_series_for_numeric_failure(R.choice(QUESTIONS), response, core)
        added = len(result.get("tool_results", [])) - len(response["tool_results"])
        outcomes[added] = outcomes.get(added, 0) + 1
        digests.append(
            hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()[
                :12
            ]
        )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
