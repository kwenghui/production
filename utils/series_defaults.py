"""Select top-level series by default; retain explicit subgroup requests.

Used by: ui/table_explorer.py.
Tested in: tests/test_longer_history.py.
"""

import re


def root_series_defaults(entries, original_defaults, question):
    """For generic table exploration, include IDs 1, 2, 3 etc, not descendants."""
    roots = {
        str(row.get('series_id'))
        for row in entries
        if re.fullmatch(r"[1-9][0-9]*", str(row.get('series_id', '')))
    }
    if not roots:
        return original_defaults
    # Select every top-level published ID by default. A descendant remains
    # selectable manually, or when its explicit dotted ID appears in the question.
    # Generic words such as "female" should not suppress all other root series.
    selected = {str(x) for x in original_defaults}
    descendants = selected - roots
    for descendant in descendants:
        if re.search(r"(?<![0-9.])" + re.escape(descendant) + r"(?![0-9.])", str(question)):
            return roots | {descendant}
    return roots


MAX_DEFAULT_SERIES = 6  # most series kept when several match the question equally well

_STOP = {
    "the",
    "and",
    "for",
    "are",
    "was",
    "were",
    "what",
    "which",
    "who",
    "how",
    "has",
    "have",
    "had",
    "did",
    "does",
    "from",
    "that",
    "this",
    "with",
    "than",
    "next",
    "last",
    "per",
    "any",
    "all",
    "each",
    "their",
    "they",
    "its",
    "into",
    "over",
    "under",
    "between",
    "among",
    "about",
}
_SYNONYMS = {
    "rise": {"higher", "increase", "increased", "up", "more"},
    "rises": {"higher", "increase", "increased", "up", "more"},
    "increase": {"higher", "rise", "up", "more"},
    "fall": {"lower", "decrease", "decreased", "down", "less", "decline"},
    "falls": {"lower", "decrease", "decreased", "down", "less", "decline"},
    "decline": {"lower", "fall", "down", "less"},
    "same": {"unchanged", "stay"},
    "stay": {"same", "unchanged"},
}


def _stem(word):
    """A crude English stem (plural endings removed) for matching series names."""
    word = word.lower()
    for suffix in ("sses", "ies", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)] + (
                "ss" if suffix == "sses" else "y" if suffix == "ies" else ""
            )
    return word


def _stemmed_words(text) -> set:
    """Stemmed words of a text (3+ letters, stop words removed)."""
    return {
        _stem(w) for w in re.findall(r"[A-Za-z]{3,}", str(text or "")) if w.lower() not in _STOP
    }


def cap_default_selection(entries, defaults, question, limit=MAX_DEFAULT_SERIES):
    """Start with ONE series -- the best match for the question -- so there is one chart.

    Ranked by how many of the question's words (and close synonyms: rise/higher, fall/lower,
    stay/same) appear in the series name, with a bonus for Total/overall series; ties keep the
    table's own order. Exception: when two or more series match the question's words equally
    well (e.g. "rise, stay the same or fall"), that comparison IS the answer, so all of them
    are kept (up to `limit`). Everything else stays one tick away in the series picker.
    """
    wanted_ids = {str(x) for x in defaults}
    chosen = [e for e in entries if str(e.get("series_id")) in wanted_ids]
    if len(chosen) <= 1:
        return set(defaults)
    words = _stemmed_words(question)
    for word in list(words):
        words |= {_stem(w) for w in _SYNONYMS.get(word, ())}

    def score(entry) -> tuple:
        """How well a series matches the question's words (totals score lower)."""
        name_words = _stemmed_words(f"{entry.get('series_name', '')} {entry.get('leaf_name', '')}")
        matched = len(words & name_words)
        total = (
            2
            if (
                entry.get("is_total")
                or entry.get("is_parent")
                or re.search(
                    r"\b(total|overall|all)\b",
                    f"{entry.get('series_name', '')} {entry.get('leaf_name', '')}",
                    re.I,
                )
            )
            else 0
        )
        # the general series beats a narrower one
        precision = matched / len(name_words) if name_words else 0.0
        return (-(matched + total), -round(precision, 6))

    ranked = sorted(range(len(chosen)), key=lambda i: (score(chosen[i]), i))
    best = score(chosen[ranked[0]])
    tier = [i for i in ranked if score(chosen[i]) == best]
    best_entry = chosen[ranked[0]]
    best_matches = len(
        words
        & _stemmed_words(
            f"{best_entry.get('series_name', '')} " f"{best_entry.get('leaf_name', '')}"
        )
    )
    # Two or more equally good matches to the QUESTION are the answer; otherwise one series.
    keep = tier[:limit] if len(tier) >= 2 and best_matches > 0 else ranked[:1]
    return {str(chosen[i]["series_id"]) for i in keep}


_PERCENT_UNIT = re.compile(r"(?i)\b(per\s*cent|percent(age)?)\b|%")


def is_constant_whole(entry) -> bool:
    """True for a percentage series that is 100 in every period (the whole of a distribution).

    "Population = 100%" beside Employed / Unemployed / Outside the labour force adds
    nothing and stretches the axis to 100, squashing the lines that matter.
    """
    values = list((entry or {}).get("observations", {}).values())
    return (
        bool(_PERCENT_UNIT.search(str((entry or {}).get("unit") or "")))
        and len(values) >= 2
        and all(isinstance(v, (int, float)) and abs(v - 100) < 0.05 for v in values)
    )


def without_constant_wholes(entries, defaults) -> set:
    """The default selection minus 100%-every-period series, unless nothing else would remain.

    The series stays in the table, one tick away.
    """
    by_id = {str(e.get("series_id")): e for e in entries}
    kept = {sid for sid in defaults if not is_constant_whole(by_id.get(str(sid)))}
    return kept or set(defaults)
