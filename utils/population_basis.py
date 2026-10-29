"""General check: does the official data cover the population the user asked about?

Many SingStat tables look alike but measure different populations, for
example "Total Population" (everyone, including non-residents), "Residents"
(citizens plus permanent residents) and "Citizens". Answering a question about
citizens from a residents table gives a wrong number that still looks
official.

This module works the same way for EVERY table. It never looks at table IDs.
It reads the population words in the question and in the evidence's own
metadata (table title and selected series names) and reports a conflict only
when:

1. the question names a population basis, AND
2. the evidence names one or more bases, AND
3. none of the evidence's bases is the one asked about.

If the metadata does not mention any basis, there is no conflict: the check
only acts on positive evidence of a different population.

Used by: ui/answer_pipeline.py, utils/discovery_controller.py.
"""

from __future__ import annotations

import re

# Order matters: the narrower phrases are matched and removed first, so that
# "permanent residents" and "non-residents" are not also counted as "residents".
# Each entry is (basis, pattern, case_sensitive). "PR" must be upper case so
# that ordinary words are never mistaken for it.
_BASIS_PATTERNS = (
    ("non_resident", r"\bnon[\s-]?residents?\b", False),
    ("permanent_resident", r"\bpermanent\s+residents?\b", False),
    ("permanent_resident", r"\bPRs?\b", True),
    ("citizen", r"\bcitizens?\b|\bcitizenship\b", False),
    ("resident", r"\bresidents?\b", False),
    ("total", r"\btotal\s+population\b|\bwhole\s+population\b", False),
)

BASIS_LABELS = {
    "non_resident": "non-residents",
    "permanent_resident": "permanent residents",
    "citizen": "citizens",
    "resident": "residents",
    "total": "the total population",
}


def population_bases(text):
    """Return the set of population bases named in ``text``.

    >>> sorted(population_bases("Singapore citizens and permanent residents"))
    ['citizen', 'permanent_resident']
    """
    remaining = str(text or "")
    found = set()
    for name, pattern, case_sensitive in _BASIS_PATTERNS:
        flags = 0 if case_sensitive else re.IGNORECASE
        if re.search(pattern, remaining, flags):
            found.add(name)
            remaining = re.sub(pattern, " ", remaining, flags=flags)
    return found


def _evidence_labels(item) -> str:
    """Metadata text describing which population a tool result covers."""
    if not isinstance(item, dict):
        return ""
    parts = [item.get("title"), item.get("table_title"), item.get("definition")]
    for key in ("selected_series", "series_summary"):
        for row in item.get(key) or []:
            if isinstance(row, dict):
                parts.append(row.get("series_name"))
    for key in ("series_a", "series_b"):
        if isinstance(item.get(key), dict):
            parts.append(item[key].get("series_name"))
    return " ".join(str(p) for p in parts if p)


def population_conflict(question, evidence):
    """Return a short reason string if the evidence covers a different population.

    ``evidence`` is the list of successful official tool results. Returns
    None when there is no conflict (including when nothing can be decided).
    """
    asked = population_bases(question)
    if not asked:
        return None
    covered = set()
    for item in evidence or []:
        covered |= population_bases(_evidence_labels(item))
    if not covered or asked & covered:
        return None
    return (
        "asked about "
        + ", ".join(BASIS_LABELS[b] for b in sorted(asked))
        + "; the data cover "
        + ", ".join(BASIS_LABELS[b] for b in sorted(covered))
    )
