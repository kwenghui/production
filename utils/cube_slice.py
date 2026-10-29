"""Pick one clear, source-backed slice of a multi-dimensional official SingStat table.

A crosstab ("CT") table holds many slices: every row category, column group and
period is a different view. Rather than refuse to chart it, choose ONE slice
that is unambiguous (the "Total" column group, the latest period) and say how
many other slices exist. Any cell that is not unique is left out, never merged.

Used by: ui/table_explorer.py, utils/table_tools.py,
tools/characterise_cube_slices.py.
Tested in: tests/test_cube_slice.py.
"""

from __future__ import annotations
import html
import re
from collections import defaultdict

from utils.periods import period_sort_key
from utils.cube_cells import flatten, is_total_label as _total, parse_number as _number

_PERIOD = re.compile(
    r"^(?:19|20)\d{2}(?:\s*/\s*\d{2})?$"
    r"|^(?:19|20)\d{2}\s*(?:Q[1-4]|[1-4]Q|H[12]|[A-Za-z]{3,9})$"
)

_ATTACHED_MARKER = re.compile(r"(?<=[A-Za-z\)])(\d{1,2})/(?=\s|$)")
_SPACED_TOTAL_MARKER = re.compile(r"(?i)^\s*((?:total|all|overall))\s+(\d{1,2})/\s*$")


def split_footnote_marker(label):
    """("Median", ["1"]) for "Median1/": SingStat writes footnote references as digit + slash.

    Only a marker glued to a word ("Median1/", "Total2/") or "Total 1/" is recognised, so
    labels such as "Aged 5-9", "Type 2" or "Q1/Q2" are never altered.
    """
    text = str(label)
    spaced = _SPACED_TOTAL_MARKER.match(text)
    if spaced:
        return spaced.group(1), [spaced.group(2)]
    marks = []

    def take(match) -> str:
        """Remember a footnote marker found in a label and remove it."""
        marks.append(match.group(1))
        return ""

    stripped = " ".join(_ATTACHED_MARKER.sub(take, text).split())
    return (stripped, marks) if marks and stripped else (text, [])


def _display_labels(raw_labels) -> dict:
    """raw label -> (display label, footnote marks). A marker is only removed when doing so
    cannot make two different published labels identical."""
    split = {raw: split_footnote_marker(raw) for raw in raw_labels}
    claimed = defaultdict(set)
    for raw, (shown, _marks) in split.items():
        claimed[shown].add(raw)
    return {
        raw: ((shown, marks) if len(claimed[shown]) == 1 else (raw, []))
        for raw, (shown, marks) in split.items()
    }


def _period_key(label):
    """A column label normalised as a period key, or None if it is not a period."""
    text = str(label).strip()
    return re.sub(r"\s+", " ", text) if _PERIOD.match(text) else None


def _bare(label) -> str:
    """A label with whitespace collapsed."""
    return re.sub(r"\s+", " ", str(label)).strip()


def _group_label(group):
    """Readable column-group name: no repeated words and no redundant "Total"."""
    parts = []
    for part in group:
        if part.casefold() not in {p.casefold() for p in parts}:
            parts.append(part)
    specific = [p for p in parts if not _total(p)]
    return " · ".join(specific) if specific else ("Total" if group else "all")


_COUNT = re.compile(
    r"(?i)\bnumber of\b|\bno\.? " r"of\b|\(number\)|\bcount\b|\bsample\b|\brespondents?\b"
)
_MEASURE = re.compile(
    r"(?i)\$|dollar|average|mean|median|expenditure|income|spend|\bper\b|%|percent|"
    r"proportion|share|\brate\b|index|amount|cost"
)


def _clean_note(value, limit: int = 1500):
    """Plain text from a SingStat footnote field (string or list), without HTML."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v)
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_NUMBERED_NOTE = re.compile(r"(?:^|\s)(\d{1,2})/\s*(.+?)(?=\s\d{1,2}/\s|$)", re.S)


def footnote_lines(payload, footnote_marks, shown_rows, shown_group) -> list:
    """The footnotes that belong to what is on screen, as plain sentences.

    Numbered notes ("1/ ...") are matched to the label that carried the marker. If the published
    text is not numbered, or a marker has no matching note, the table's footnote is shown as
    published. Nothing is invented or reworded.
    """
    table_text = _clean_note(payload.get("footnote") or payload.get("footnotes"))
    numbered = {
        number: " ".join(text.split()) for number, text in _NUMBERED_NOTE.findall(table_text)
    }
    shown = (
        set(shown_rows)
        | {part for part in re.split(r" · ", shown_group or "") if part}
        | {shown_group}
    )
    lines, unresolved = [], False
    for label, marks in footnote_marks.items():
        if label not in shown:
            continue
        for mark in marks:
            if mark in numbered:
                lines.append(f"{label}: {numbered[mark]}")
            else:
                unresolved = True
    if table_text and (unresolved or (not numbered and not lines)):
        lines.append(table_text)
    for row in payload.get("row", []):
        if isinstance(row, dict):
            note = _clean_note(row.get("footnote"))
            name = split_footnote_marker(_bare(row.get("rowText") or ""))[0]
            if note and name in shown_rows:
                lines.append(f"{name}: {note}")
    return list(dict.fromkeys(lines))


def _group_score(group, label):
    """Higher = a more informative default. Money/average measures and "Total" groups beat
    plain counts (a household count is the same on every row, so it makes a useless chart)."""
    score = 0
    if group and all(_total(p) for p in group):
        score += 3
    if _MEASURE.search(label):
        score += 2
    if _COUNT.search(label):
        score -= 5
    return score


def _distinct(values) -> bool:
    """True when the values differ (at least two distinct numbers), so a chart says something."""
    return len({round(float(v), 6) for v in values}) >= 2


def parse_cube(payload) -> dict:
    """Read a multi-dimensional SingStat payload into rows x column groups x periods.

    Only cells that have exactly one published value are kept; a cell published more than
    once is never averaged or merged.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("row"), list):
        raise ValueError("The official table payload has no verified rows.")

    # (row label, column path, period, value) exactly as published
    raw_cells = []
    for row in payload["row"]:
        if not isinstance(row, dict):
            continue
        label = _bare(row.get("rowText") or "")
        if not label:
            continue
        for path, raw in flatten(row.get("columns")):
            value = _number(raw)
            if value is None:
                continue
            marks = [(i, _period_key(p)) for i, p in enumerate(path) if _period_key(p)]
            if len(marks) > 1:
                continue
            if marks:
                idx, found = marks[0]
                grp = tuple(_bare(p) for i, p in enumerate(path) if i != idx)
            else:
                found, grp = None, tuple(_bare(p) for p in path)
            raw_cells.append((label, grp, found, value))

    # Footnote markers ("Median1/") are removed from what people see, never from the data.
    shown_as = _display_labels({text for label, grp, _f, _v in raw_cells for text in (label, *grp)})
    footnote_marks = {}  # displayed label -> ["1", ...]
    cells = defaultdict(list)  # (row label, column group, period) -> values
    row_order, group_order, periods = [], [], set()
    for label_raw, grp_raw, found, value in raw_cells:
        label = shown_as[label_raw][0]
        grp = tuple(shown_as[part][0] for part in grp_raw)
        for raw_text in (label_raw, *grp_raw):
            shown, marks = shown_as[raw_text]
            if marks:
                footnote_marks.setdefault(shown, [])
                footnote_marks[shown] += [m for m in marks if m not in footnote_marks[shown]]
        cells[(label, grp, found)].append(value)
        if label not in row_order:
            row_order.append(label)
        if grp not in group_order:
            group_order.append(grp)
        if found:
            periods.add(found)
    if not cells:
        raise ValueError("No numeric observations were found in this table.")

    # Only cells with exactly one published value can be shown.
    unique = {k: v[0] for k, v in cells.items() if len(v) == 1}
    ambiguous = len(cells) - len(unique)
    ordered_periods = sorted(periods, key=period_sort_key)
    parts = [r for r in row_order if not _total(r)]

    names, by_name = {}, {}  # display names, made unique
    for grp in group_order:
        name = _group_label(grp)
        while name in by_name:
            name += " (2)"
        names[grp], by_name[name] = name, grp
    ranked = sorted(group_order, key=lambda g: (-_group_score(g, names[g]), group_order.index(g)))
    return {
        "unique": unique,
        "ambiguous": ambiguous,
        "cells": cells,
        "row_order": row_order,
        "group_order": group_order,
        "periods": ordered_periods,
        "parts": parts,
        "names": names,
        "by_name": by_name,
        "ranked": ranked,
        "footnote_marks": footnote_marks,
    }


def _count(n, singular, plural=None) -> str:
    """ "1 row" / "3 rows": a count with the right singular or plural word."""
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


def slice_note(n_rows, n_groups, n_periods, group_name, period) -> str:
    """Describe what is shown, mentioning selectors only when the app draws them.

    The app shows a "Show" selector when there are several column groups and a
    "Period" selector when there are several periods (see app.py). A table with
    one of each has exactly one slice, so there is nothing to select.
    """
    shown = f"\u201c{group_name}\u201d" + (f" for {period}" if period else "")
    if n_groups <= 1 and n_periods <= 1:
        return (
            f"This table has one view ({shown}) with "
            f"{_count(n_rows, 'category', 'categories')}; "
            "all are shown below."
        )
    choices = []
    if n_groups > 1:
        choices.append((_count(n_groups, "column group"), "\u201cShow\u201d"))
    if n_periods > 1:
        choices.append((_count(n_periods, "period"), "\u201cPeriod\u201d"))
    sizes = " \u00d7 ".join(size for size, _ in choices)
    menus = " and ".join(menu for _, menu in choices)
    return (
        f"This table has {sizes}, each a separate view of its "
        f"{_count(n_rows, 'category', 'categories')}. Showing {shown}; use the {menus} "
        f"selector{'s' if len(choices) > 1 else ''} above to see another."
    )


def crosstab_overview(payload, *, table_id, source_url, title: str = "", group=None, period=None):
    """Return one verified slice plus a plain description of the other slices.

    With no `group`/`period` the most informative slice is chosen automatically and a
    slice whose categories all show the same value is never chosen. A caller-supplied
    `group` (a display name from result["options"]["groups"]) and `period` are honoured.
    Raises ValueError when no usable slice exists, so the caller can fall back.

    Steps: choose the slice (_choose_slice); describe it (_slice_result); if no
    slice compares at least two different values, fall back to the "Total" row
    across periods (_total_trend_result).
    """
    cube = parse_cube(payload)
    chosen, chosen_period, values, warning = _choose_slice(cube, group, period)
    summary = {
        "row_categories": len(cube["row_order"]),
        "column_groups": len(cube["group_order"]),
        "periods": len(cube["periods"]),
        "published_cells": len(cube["cells"]),
        "ambiguous_cells_skipped": cube["ambiguous"],
    }
    # A slice is one column group in one period; the row categories are the bars
    # inside a slice, not separate slices.
    n_slices = max(len(cube["group_order"]), 1) * max(len(cube["periods"]), 1)
    if chosen is not None:
        return _slice_result(
            cube,
            payload,
            chosen,
            chosen_period,
            values,
            warning,
            table_id=table_id,
            source_url=source_url,
            title=title,
            summary=summary,
            n_slices=n_slices,
        )
    fallback = _total_trend_result(
        cube,
        table_id=table_id,
        source_url=source_url,
        title=title,
        summary=summary,
        n_slices=n_slices,
    )
    if fallback is None:
        raise ValueError("No slice with at least two different comparable values was found.")
    return fallback


def _latest_period(cube, grp):
    """The latest period in which at least two parts of the group have values."""
    for found in reversed(cube["periods"] or [None]):
        if sum(1 for r in cube["parts"] if (r, grp, found) in cube["unique"]) >= 2:
            return found
    return None


def _values_for(cube, grp, found) -> list:
    """(part, value) pairs of the group in one period (missing values left out)."""
    pairs = [(r, cube["unique"].get((r, grp, found))) for r in cube["parts"]]
    return [(r, v) for r, v in pairs if v is not None]


def _choose_slice(cube, group, period):
    """(group, period, values, warning) of the slice to show, or (None, None, [], "").

    A requested group is honoured (in the requested period if it has data,
    else its latest period) and may show equal values, with a warning.
    Otherwise the best-ranked group whose values differ is chosen.
    """
    ordered_periods = cube["periods"]
    wanted = cube["by_name"].get(group) if group else None
    if wanted is not None:
        for use in ([period] if period in ordered_periods else []) + [_latest_period(cube, wanted)]:
            pairs = _values_for(cube, wanted, use)
            if len(pairs) >= 2:
                warning = (
                    ""
                    if _distinct(v for _, v in pairs)
                    else "Every category has the same value in this slice (for example a "
                    "count that applies to all of them), so the bars are equal. Try "
                    "another selection."
                )
                return wanted, use, pairs, warning
        raise ValueError("The chosen slice has fewer than two values to compare.")
    for grp in cube["ranked"]:
        use = period if period in ordered_periods else _latest_period(cube, grp)
        pairs = _values_for(cube, grp, use)
        if len(pairs) >= 2 and _distinct(v for _, v in pairs):
            return grp, use, pairs, ""
    return None, None, [], ""


def _slice_result(
    cube,
    payload,
    chosen,
    chosen_period,
    values,
    warning,
    *,
    table_id,
    source_url,
    title,
    summary,
    n_slices,
):
    """The overview of one slice: bars, a period table, a note, footnotes and options."""
    names, ordered_periods = cube["names"], cube["periods"]
    options = {
        "groups": [names[g] for g in cube["ranked"]],
        "periods": list(reversed(ordered_periods)),
    }
    group_name = names[chosen]
    when = f" ({chosen_period})" if chosen_period else ""
    table = [
        {
            "Category": r,
            **{found: cube["unique"].get((r, chosen, found), "–") for found in ordered_periods},
        }
        for r in cube["row_order"]
    ]  # same slice across every period
    # Count the bars actually charted (the Total row and ambiguous cells are not bars).
    note = slice_note(
        len(values), len(options["groups"]), len(options["periods"]), group_name, chosen_period
    )
    overall = group_name in ("Total", "all")
    return {
        "table_id": table_id,
        "source_url": source_url,
        "slice": {"column_group": group_name, "period": chosen_period},
        "rows": [{"Category": r, "Value": v} for r, v in values],
        "period_table": table if len(ordered_periods) > 1 else None,
        "note": note,
        "warning": warning,
        "options": options,
        "footnotes": footnote_lines(
            payload, cube["footnote_marks"], [r for r, _ in values], group_name
        ),
        "summary": summary,
        "n_slices": n_slices,
        "chart_spec": {
            "kind": "bar",
            "title": f"{title or table_id}" + ("" if overall else f" — {group_name}") + when,
            "unit": "Value" if overall else group_name[:70],
            "categories": [r for r, _ in values],
            "series": [{"name": group_name, "values": [v for _, v in values]}],
        },
    }


def _total_trend_result(cube, *, table_id, source_url, title, summary, n_slices):
    """Fallback: the "Total" row of the first group whose values change across periods, or None."""
    total_row = next((r for r in cube["row_order"] if _total(r)), None)
    if not total_row or len(cube["periods"]) < 2:
        return None
    for grp in cube["ranked"]:
        points = [(found, cube["unique"].get((total_row, grp, found))) for found in cube["periods"]]
        points = [(found, v) for found, v in points if v is not None]
        if len(points) >= 2 and _distinct(v for _, v in points):
            group_name = cube["names"][grp]
            return {
                "table_id": table_id,
                "source_url": source_url,
                "slice": {"column_group": group_name, "period": None},
                "rows": [{"Period": f, "Value": v} for f, v in points],
                "period_table": None,
                "note": (
                    f"This table has many possible slices. Showing "
                    f"\u201c{total_row}\u201d, "
                    f"\u201c{group_name}\u201d, across periods."
                ),
                "warning": "",
                "options": {"groups": [], "periods": []},
                "summary": summary,
                "n_slices": n_slices,
                "chart_spec": {
                    "kind": "trend",
                    "title": f"{title or table_id} — {total_row}",
                    "unit": "Value",
                    "categories": [f for f, _ in points],
                    "series": [{"name": total_row, "values": [v for _, v in points]}],
                },
            }
    return None


def _canon(text) -> str:
    """A label reduced to comparable words (footnote marker removed, & as and, lower case)."""
    text = split_footnote_marker(str(text or ""))[0]
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.casefold().replace("&", " and ")).split())


def _matches(label, wanted) -> bool:
    """Exact label, or every word of the request appears in the label."""
    a, b = _canon(label), _canon(wanted)
    return bool(b) and (a == b or set(b.split()) <= set(a.split()))


def _short_labels(group_order, names):
    """Column-group labels without the wording they all share ("Primary Inputs by Final Demand · ").

    A group that IS the shared part keeps its full name. Never changes which cell is which.
    """
    parts = [g for g in group_order if g]
    prefix = ()
    if len(parts) > 1:
        first = parts[0]
        for index, value in enumerate(first):
            if all(len(g) > index and g[index] == value for g in parts):
                prefix = first[: index + 1]
            else:
                break
    labels = {}
    for g in group_order:
        rest = g[len(prefix) :] if prefix and g[: len(prefix)] == prefix else g
        labels[g] = " · ".join(rest) if rest else names[g]
    return labels


def _single_period_query(cube, search, max_rows) -> dict:
    """A table with NO time dimension (for example one year of an input-output table).

    There is nothing to compare across periods, so the comparison runs across the column groups
    (or across the rows when the request names one column group). Labels are NOT years.
    """
    unique, row_order = cube["unique"], cube["row_order"]
    group_order, names = cube["group_order"], cube["names"]
    short = _short_labels(group_order, names)
    row_hits = _listed_rows(row_order, search)  # a list of exact row labels, or []
    if not row_hits:
        row_hits = [r for r in row_order if search and _matches(r, search)]
        row_hits = [r for r in row_hits if _canon(r) == _canon(search)] or row_hits
    group_hit = (
        None
        if len(row_hits) >= 2 and "," in str(search)
        else next(
            (
                g
                for g in group_order
                if search and (_matches(names[g], search) or _matches(short[g], search))
            ),
            None,
        )
    )
    listing = {
        "available_rows": row_order[:40],
        "available_groups": [short[g] for g in group_order][:40],
    }
    if search and not row_hits and group_hit is None:
        return {
            "ok": False,
            "error": (
                "No row or column group of this multi-dimensional table matched the "
                "request; choose one of the listed rows or column groups."
            ),
            **listing,
        }
    if group_hit is not None and not row_hits:
        values = {
            r: unique[(r, group_hit, None)] for r in row_order if (r, group_hit, None) in unique
        }
        if len(values) < 2:
            return {
                "ok": False,
                "error": "That column group has fewer than two published values.",
                **listing,
            }
        label = short[group_hit]
        return {
            "ok": True,
            "axis": "rows",
            "group": label,
            "unit": "Value",
            "rows": [{"name": label, "values": values}],
            "periods": list(values),
            "other_groups": [],
            "n_groups": len(group_order),
            "note": (
                f"This table has no time dimension (one period). The figures compare the "
                f"ROWS for the "
                f"single column group '{label}'. The labels in 'period' are row names, "
                f"not years."
            ),
        }
    totals = [r for r in row_order if _total(r)]
    rows = (row_hits or totals or row_order)[: max(1, max_rows)]
    table = {}
    for r in rows:
        vals = {short[g]: unique[(r, g, None)] for g in group_order if (r, g, None) in unique}
        if vals:
            table[r] = vals
    if not table or max(len(v) for v in table.values()) < 2:
        return {
            "ok": False,
            "error": "The selected rows have fewer than two published values to compare.",
            **listing,
        }
    labels = [short[g] for g in group_order if any(short[g] in v for v in table.values())]
    return {
        "ok": True,
        "axis": "column_groups",
        "group": "all column groups",
        "unit": "Value",
        "rows": [{"name": r, "values": table[r]} for r in rows if r in table],
        "periods": labels,
        "other_groups": [],
        "n_groups": len(group_order),
        "note": (
            f"This table has no time dimension (one period). The figures compare the "
            f"COLUMN GROUPS "
            f"({len(labels)} of them) for each selected row. The labels in 'period' are "
            f"column-group "
            f"names, not years; each figure belongs to the column group named beside it."
        ),
    }


def cube_query(payload, *, series_search=None, start_year=None, end_year=None, max_rows: int = 3):
    """Verified numbers from ONE slice of a multi-dimensional table, for the written answer.

    The same slice rules as the chart: the most informative column group (never a plain
    count when a money or average measure exists), only singly-published cells, nothing
    merged. `series_search` may name a row (a goods category, "TOTAL") or a column group;
    with neither, the Total row(s) are used. Returns a plain dict:
      {"ok": True, "group": name, "unit": name, "rows": [{"name", "values": {period: value}}],
       "periods": [...], "other_groups": [...], "n_groups": int, "note": str}
    or {"ok": False, "error": str, "available_rows": [...], "available_groups": [...]}.
    """
    cube = parse_cube(payload)
    if not cube["periods"]:
        return _single_period_query(cube, str(series_search or "").strip(), max_rows)
    periods = _periods_in_range(cube["periods"], start_year, end_year)
    max_rows = max(1, min(int(max_rows or 3), 12))
    search = str(series_search or "").strip()
    rows, group_hit = _query_targets(cube, search, max_rows)
    if rows is None:
        return _cube_query_refusal(
            cube,
            "No row or column group of this multi-dimensional table "
            "matched the request; choose one of the listed rows or "
            "column groups.",
        )
    for grp in [group_hit] if group_hit is not None else cube["ranked"]:
        table = {
            r: values
            for r in rows
            if (
                values := {
                    p: cube["unique"][(r, grp, p)] for p in periods if (r, grp, p) in cube["unique"]
                }
            )
        }
        if not table:
            continue
        informative = len({v for vals in table.values() for v in vals.values()}) >= 2
        if informative or group_hit is not None:
            return _cube_query_answer(cube, grp, rows, table, periods)
    return _cube_query_refusal(
        cube,
        "No column group has at least two different published " "values for the selected rows.",
    )


def _periods_in_range(periods, start_year, end_year) -> list:
    """The periods whose year lies within [start_year, end_year] (either may be None)."""
    if start_year is None and end_year is None:
        return periods
    low = int(start_year) if start_year is not None else -(10**9)
    high = int(end_year) if end_year is not None else 10**9
    return [p for p in periods if low <= int(p[:4]) <= high]


def _query_targets(cube, search, max_rows):
    """(rows to report, column group named by the search or None); rows None = no match.

    An exact row name wins over longer labels containing it ("TOTAL" means that
    row). Without a search, the Total row(s), else the first rows.
    """
    names = cube["names"]
    listed = _listed_rows(cube["row_order"], search)
    if listed:
        # "Professionals, Cleaners, Labourers & Related Workers, Others": exactly those rows.
        return listed[:max_rows], None
    row_hits = [r for r in cube["row_order"] if search and _matches(r, search)]
    row_hits = [r for r in row_hits if _canon(r) == _canon(search)] or row_hits
    group_hit = next((g for g in cube["ranked"] if search and _matches(names[g], search)), None)
    if search and not row_hits and group_hit is None:
        return None, None
    if row_hits:
        return row_hits[:max_rows], group_hit
    totals = [r for r in cube["row_order"] if _total(r)]
    return (totals or cube["row_order"])[:max_rows], group_hit


def _listed_rows(row_order, search) -> list:
    """Rows named by a comma-separated list of EXACT row labels, in the order given; [] otherwise.

    Labels may contain commas themselves ("Cleaners, Labourers & Related
    Workers"), so the list is read greedily: the longest run of pieces that is
    a row label wins. Every piece must be used and at least two rows named;
    anything else is left to the ordinary single-label match.
    """
    text = str(search or "").strip()
    if "," not in text:
        return []
    by_label = {}
    for row in row_order:
        by_label.setdefault(_canon(row), row)
    pieces = [piece.strip() for piece in text.split(",")]
    rows, start = [], 0
    while start < len(pieces):
        for end in range(len(pieces), start, -1):
            row = by_label.get(_canon(", ".join(pieces[start:end])))
            if row is not None:
                rows.append(row)
                start = end
                break
        else:
            return []
    rows = list(dict.fromkeys(rows))
    return rows if len(rows) >= 2 else []


def _cube_query_answer(cube, grp, rows, table, periods) -> dict:
    """The successful result for one column group, with a note naming that group."""
    names, ranked, row_order = cube["names"], cube["ranked"], cube["row_order"]
    return {
        "ok": True,
        "group": names[grp],
        "unit": names[grp] if names[grp] != "Total" else "Value",
        "rows": [{"name": r, "values": table[r]} for r in rows if r in table],
        "periods": [p for p in periods if any(p in vals for vals in table.values())],
        "other_groups": [names[g] for g in ranked if g != grp][:12],
        "n_groups": len(ranked),
        "note": (
            f"This table is multi-dimensional ({len(row_order)} rows x {len(ranked)} "
            f"column groups x {len(periods)} period(s)). The figures below are for the "
            f"column group '{names[grp]}' only; other column groups are different measures "
            f"or breakdowns and are not combined. State which column group these figures "
            f"belong to."
        ),
    }


def _cube_query_refusal(cube, error) -> dict:
    """A refusal that lists what the table does contain, so the model can retry."""
    return {
        "ok": False,
        "error": error,
        "available_rows": cube["row_order"][:40],
        "available_groups": [cube["names"][g] for g in cube["ranked"]][:40],
    }
