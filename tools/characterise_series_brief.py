"""Characterise plain_summary.structured_series_management_brief (the one-line brief).

Uses the real tables of tests/fixtures/real_answers.py: Sage's own verified-
observation answers (rebuilt by evidence_bound_generic_answer), copies with a
changed number or heading, unofficial or incomplete evidence, and a Total
row beside its parts. Prints a digest of every brief: two copies of Sage that
write the same briefs print the same digest.

    python tools/characterise_series_brief.py SAGE_DIR
"""

import copy
import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import logging  # noqa: E402

logging.disable(logging.CRITICAL)
from utils.answer_verification import evidence_bound_generic_answer  # noqa: E402
from utils.plain_summary import structured_series_management_brief  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
R = random.Random(41)
QUESTIONS = [q for qs in fixtures.QUESTIONS.values() for q in qs]


def main():
    digests, outcomes = [], {}
    for _ in range(1500):
        table_id = R.choice(list(fixtures.EVIDENCE_SETS))
        evidence = [fixtures.tool_result(table_id, R.choice(fixtures.EVIDENCE_SETS[table_id]))]
        answer = evidence_bound_generic_answer(R.choice(QUESTIONS), copy.deepcopy(evidence)) or ""
        change = R.choice(["none"] * 4 + ["number", "heading", "unofficial", "incomplete", "total"])
        if change == "number" and answer:
            answer = answer.replace(".", ".9", 1) if "| " in answer else answer
        elif change == "heading":
            answer = answer.replace("## Verified observations", "## Observations")
        elif change == "unofficial":
            evidence[0]["source_url"] = "https://example.com"
        elif change == "incomplete":
            evidence[0]["table_complete"] = False
        elif change == "total" and len(evidence[0]["series_summary"]) > 1:
            evidence[0]["series_summary"][0]["series_name"] = "Total"
        brief = structured_series_management_brief(answer, evidence)
        outcomes[bool(brief)] = outcomes.get(bool(brief), 0) + 1
        digests.append(hashlib.sha256(json.dumps([answer, brief]).encode()).hexdigest()[:12])
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
