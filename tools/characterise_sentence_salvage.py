"""Characterise answer_verification.remove_unverified_optional_sentence.

Starts from the real R&D answer (tests/fixtures/real_answers.py) and its
official evidence, then inserts one or two unsupported numeric statements in
different places and forms (headline, sentence inside a paragraph, bullet,
bold sentence, source line). Prints a digest of every salvage decision: two
copies of Sage that decide the same print the same digest.

    python tools/characterise_sentence_salvage.py SAGE_DIR
"""

import hashlib
import importlib.util
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import logging  # noqa: E402

logging.disable(logging.CRITICAL)
from utils.answer_verification import remove_unverified_optional_sentence  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
R = random.Random(31)
INSERTS = [
    "Together the sectors spent 13,746.91 million in 2023.",
    "**This is 42.5 times the 1990 level.** The rise was steady.",
    "- In total, 27 areas were tracked.",
    "Spending rose by 9,999.99 million overall.",
    "Source: SingStat M081331 reports 1,234.5 in a footnote.",
    "The private sector rose from $309.50m in 1990 to $8,955.99m in 2023.",
]
QUESTIONS = [
    "How has research and development expenditure in Singapore changed over the years?",
    "Which sector spent the most on R&D?",
]


def variant():
    # The real answer ended with this sources list (see the page you sent).
    lines = (
        fixtures.ANSWER_C + "\n\nSources used:\n\nSingStat M081331: "
        "https://tablebuilder.singstat.gov.sg/table/TS/M081331"
    ).split("\n")
    for _ in range(R.choice([1, 1, 2])):
        where = R.choice(["headline", "after_table", "paragraph", "end"])
        text = R.choice(INSERTS)
        if where == "headline":
            lines[0] = text + " " + lines[0]
        elif where == "paragraph":
            idx = next(
                i for i, line in enumerate(lines) if line.startswith("The private sector dipped")
            )
            lines[idx] = lines[idx] + " " + text
        elif where == "after_table":
            idx = max(i for i, line in enumerate(lines) if line.startswith("|")) + 1
            lines.insert(idx, text)
        else:
            lines.append(text)
    if R.random() < 0.1:
        lines = [line for line in lines if "M081331" not in line]
    return "\n".join(lines)


def main():
    evidence = [
        fixtures.tool_result("M081331", numbers)
        for numbers in (["2", "3", "4"], ["1.1", "1.3", "1.4", "1.5", "1.10"])
    ]
    digests, outcomes = [], {}
    for _ in range(400):
        answer = variant()
        result = remove_unverified_optional_sentence(answer, evidence, R.choice(QUESTIONS))
        key = "salvaged" if result else "none"
        outcomes[key] = outcomes.get(key, 0) + 1
        digests.append(hashlib.sha256(json.dumps([answer, result]).encode()).hexdigest()[:12])
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
