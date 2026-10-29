"""Order of the bars and lines in a verified chart (pure: no charting library needed).

* sort_spec / SORT_OPTIONS - the "Sort bars" choice: original order, A-Z or highest first;
* default_sort_mode        - highest first for separate categories, but the table's own
                             order for labels with a natural order (ages, bands, years);
* lines_by_latest          - trend lines (and their legend) by latest value, biggest first.

Only the ORDER ever changes: every verified value is kept exactly.

Used by: ui/components.py, utils/lightweight_charts.py.
Tested in: tests/test_biggest_first.py.
"""

import re


SORT_OPTIONS = ("Original order", "A–Z", "Highest first")


_BAR_KINDS = ("bar", "grouped_bar")


def is_sortable(spec) -> bool:
    """Only bar charts of separate categories can be re-ordered; time lines never are."""
    return (
        isinstance(spec, dict)
        and spec.get("kind") in _BAR_KINDS
        and isinstance(spec.get("categories"), list)
        and len(spec["categories"]) >= 3
        and isinstance(spec.get("series"), list)
        and bool(spec["series"])
    )


#: Category labels with a built-in order (ages, time or income bands, periods) keep it.
_ORDERED_LABEL = re.compile(r"\d")


def default_sort_mode(spec) -> str:
    """ "Highest first" for bars of separate categories; the table's own order when the labels
    have a natural order of their own (age groups, travel-time or income bands, years)."""
    if not is_sortable(spec):
        return SORT_OPTIONS[0]
    if any(_ORDERED_LABEL.search(str(label)) for label in spec["categories"]):
        return SORT_OPTIONS[0]
    return "Highest first"


def lines_by_latest(spec):
    """A trend chart with its lines (and legend) ordered by their latest value, biggest first.

    Only the ORDER changes: every series keeps all its verified values.
    """
    if not isinstance(spec, dict) or spec.get("kind") not in ("trend", "line", "area"):
        return spec
    series = spec.get("series")
    if not isinstance(series, list) or len(series) < 2:
        return spec

    def latest(item):
        """Last published value of a line (minus infinity when it has none)."""
        values = [v for v in (item.get("values") or []) if isinstance(v, (int, float))]
        return values[-1] if values else float("-inf")

    return {**spec, "series": sorted(series, key=latest, reverse=True)}  # stable for ties


def _natural_key(text) -> list:
    """'Aged 5-9' sorts before 'Aged 10-14'; letters ignore case."""
    return [
        int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", str(text))
    ]


def sort_spec(spec, mode):
    """Return the same verified values with the categories re-ordered. Nothing is added,
    dropped or changed: every series is permuted with the same order."""
    if mode == SORT_OPTIONS[0] or not is_sortable(spec) or mode not in SORT_OPTIONS:
        return spec
    categories, series = spec["categories"], spec["series"]
    order = list(range(len(categories)))
    if mode == "A–Z":
        order.sort(key=lambda i: _natural_key(categories[i]))
    else:  # Highest first, by the total across series

        def total(i):
            """Sum of all series at category ``i`` (minus infinity when none has a number)."""
            values = [
                item["values"][i]
                for item in series
                if isinstance(item.get("values"), list)
                and i < len(item["values"])
                and isinstance(item["values"][i], (int, float))
            ]
            return sum(values) if values else float("-inf")

        order.sort(key=total, reverse=True)  # stable, so ties keep their original order
    return {
        **spec,
        "categories": [categories[i] for i in order],
        "series": [{**item, "values": [item["values"][i] for i in order]} for item in series],
    }


_SEPARATORS = {"/", "-", "–", "·", "|", ",", ":"}


def short_series_names(names) -> dict:
    """{full name: short legend name} keeping only the words that tell the series apart.

    "Proportion Of Students In Primary Education Achieving ... In Mathematics / Female"
    and "... Secondary Education ... In Reading / Male" become "Primary · Mathematics ·
    Female" and "Secondary · Reading · Male": words shared by EVERY name (they are in
    the chart title) are dropped. Names are kept unchanged when there is only one, when
    nothing is shared, or when shortening would make two names equal or one empty.
    """
    names = [str(n) for n in names]
    if len(names) < 2:
        return {n: n for n in names}
    tokens = {n: re.findall(r"[^\s/·|,:–]+|[/·|,:–]", n) for n in names}
    shared = None
    for words in tokens.values():
        counted = {}
        for word in words:
            if word not in _SEPARATORS:
                counted[word.casefold()] = counted.get(word.casefold(), 0) + 1
        shared = (
            counted
            if shared is None
            else {w: min(c, counted[w]) for w, c in shared.items() if w in counted}
        )
    if not shared:
        return {n: n for n in names}
    short = {}
    for name, words in tokens.items():
        budget, kept = dict(shared), []
        for word in words:
            key = word.casefold()
            if word not in _SEPARATORS and budget.get(key, 0) > 0:
                budget[key] -= 1
                kept.append("/")  # dropped words also separate parts
                continue
            kept.append(word)
        parts, current = [], []
        for word in kept:  # separators split the kept words
            if word in _SEPARATORS:
                if current:
                    parts.append(" ".join(current))
                current = []
            else:
                current.append(word)
        if current:
            parts.append(" ".join(current))
        short[name] = " · ".join(parts)
    values = list(short.values())
    if any(not v for v in values) or len(set(v.casefold() for v in values)) != len(values):
        return {n: n for n in names}
    return short
