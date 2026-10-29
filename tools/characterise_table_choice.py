"""Characterise chart_gallery.choose_primary_identified_table: which table the explorer shows.

Responses are built from the real tables in tests/fixtures/real_answers.py:
several verified tool results, search notes with tied, different, missing or
invalid scores, answer citations, and questions with and without a reporting
frequency. Prints outcome counts and a digest: two copies of Sage that choose
the same tables print the same digest.

    python tools/characterise_table_choice.py SAGE_DIR
"""

import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from utils.chart_gallery import choose_primary_identified_table, describe_identified_tables  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
R = random.Random(8)
QUESTIONS = [q for qs in fixtures.QUESTIONS.values() for q in qs] + [
    "How have monthly imports changed?",
    "Annual R&D expenditure by sector",
    "What is the latest value?",
]


def note(table_id):
    """One search note for a table, with a random (sometimes invalid) score."""
    score = R.choice([1.0, 1.0, 0.5, "x", None, float("nan"), 2.3])
    if R.random() < 0.3:
        return {"text": f"Table {table_id} description", "hybrid_score": score}
    return {
        "source_url": f"https://tablebuilder.singstat.gov.sg/table/TS/{table_id}",
        "hybrid_score": score,
    }


def main():
    digests, outcomes = [], {}
    tables = list(fixtures.EVIDENCE_SETS)
    for _ in range(2500):
        chosen = R.sample(tables, R.randint(1, 3))
        results = [fixtures.tool_result(t, R.choice(fixtures.EVIDENCE_SETS[t])) for t in chosen]
        for result in results:
            if R.random() < 0.2:
                result["year_max"] = R.choice([2020, 2025, "x"])
        response = {
            "tool_results": results,
            "rag_results": [note(t) for t in R.sample(tables, R.randint(0, 3))],
        }
        answer = {"official_sources": [(t, "u") for t in R.sample(tables, R.randint(0, 2))]}
        question = R.choice(QUESTIONS)
        identified = describe_identified_tables(response)
        primary, tied, tied_ids = choose_primary_identified_table(
            question, identified, response, R.choice([answer, None])
        )
        key = (primary or {}).get("table_id")
        outcomes[str(key)] = outcomes.get(str(key), 0) + 1
        digests.append(
            hashlib.sha256(
                json.dumps([question, key, tied, tied_ids], default=str).encode()
            ).hexdigest()[:12]
        )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
