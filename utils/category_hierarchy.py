"""Conservative, table-independent prevention of double-counted composition charts.

This module operates only on already-verified numerical chart specifications.
It never fetches, estimates or changes an observation. It removes a child from
an additive *top-level composition* only when its parent is also present and
there is independent label/ID evidence AND an exact published-value roll-up.
Unresolved possible nesting is withheld rather than represented as additive.

Used by: ui/answer_pipeline.py, tools/characterise_composition.py.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy


def _terms(label):
    """Normalised words of a category label (plurals trimmed, filler words dropped)."""
    text = str(label or '').casefold().replace('&', ' and ')
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    words = text.split()
    return frozenset(
        word[:-1] if word.endswith('s') and not word.endswith('ss') and len(word) > 4 else word
        for word in words
        if word not in {'the', 'of', 'and', 'under', 'by', 'for'}
    )


def _ids(selected, labels) -> list:
    """Find unique official series IDs by exact label, if provenance is present."""
    lookup = {}
    for source in (selected or {}).get('selected_series') or []:
        if isinstance(source, dict):
            name = source.get('series_name') or source.get('name')
            sid = source.get('series_no') or source.get('series_id')
            if name and sid:
                lookup.setdefault(str(name).casefold(), set()).add(str(sid))
    for source in (selected or {}).get('series_summary') or []:
        if isinstance(source, dict):
            name = source.get('series_name') or source.get('name')
            sid = source.get('series_no') or source.get('series_id')
            if name and sid:
                lookup.setdefault(str(name).casefold(), set()).add(str(sid))
    return [
        next(iter(lookup.get(str(label).casefold(), ())))
        if len(lookup.get(str(label).casefold(), ())) == 1
        else None
        for label in labels
    ]


#: Labels that mean "everything" (after _terms normalisation).
_TOTAL_TERMS = (
    frozenset({'total'}),
    frozenset({'overall'}),
    frozenset({'all'}),
    frozenset({'grand', 'total'}),
)


def review_composition(spec, selected=None) -> tuple:
    """Make a "parts of a whole" chart safe from double counting.

    Returns ``(spec, note, reason)``:

    * charts that are not a single snapshot of categories are returned as is;
    * when a parent and its children are both present (by official series-ID
      ancestry, e.g. 2 and 2.1, or by one name containing the other), the
      children are dropped ONLY if they add up exactly to the parent;
    * a published "Total" row is dropped only if the rest add up to it;
    * anything that cannot be reconciled returns ``(None, None, reason)``,
      so no chart is drawn rather than a double-counted one.

    Kept values are never changed; ``note`` explains what was dropped.
    """
    if not isinstance(spec, dict) or spec.get('kind') not in {'bar', 'donut'}:
        return spec, None, 'not-a-composition-layout'
    cats = spec.get('categories')
    traces = spec.get('series')
    if (
        not isinstance(cats, list)
        or not isinstance(traces, list)
        or len(traces) != 1
        or not isinstance(traces[0], dict)
        or len(cats) != len(traces[0].get('values', []))
        or len(cats) < 3
    ):
        return spec, None, 'not-a-category-snapshot'
    vals = traces[0]['values']
    if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in vals):
        return spec, None, 'not-complete-nonnegative-data'
    parents = _parents_and_children(cats, vals, _ids(selected, cats))
    if isinstance(parents, str):
        return None, None, parents
    if not parents and not any(_terms(c) in _TOTAL_TERMS for c in cats):
        # No overlap to resolve. (Even so, this never claims that independent
        # indices or rates add up to a whole; other rules govern those.)
        return spec, None, 'no-detected-parent-child-overlap'
    keep = _keep_after_rollups(cats, vals, parents)
    if isinstance(keep, str):
        return None, None, keep
    unit = str(spec.get('unit', '')).casefold()
    if ('%' in unit or 'percent' in unit or 'per cent' in unit) and not math.isclose(
        sum(vals[i] for i in keep), 100, abs_tol=0.11
    ):
        # Percentages that do not add up to 100 are not a complete composition.
        return None, None, 'non-exhaustive-or-overlapping-percentage-composition'
    cleaned = deepcopy(spec)
    cleaned['categories'] = [cats[i] for i in keep]
    cleaned['series'][0]['values'] = [vals[i] for i in keep]
    cleaned['hierarchy_checked'] = True
    cleaned['hierarchy_note'] = (
        'Top-level categories only. '
        + '; '.join(
            f"{cats[p]} includes " + ', '.join(str(cats[c]) for c in children)
            for p, children in parents.items()
        )
        + '. Subgroups are excluded from the main chart to avoid double-counting.'
    )
    return cleaned, cleaned['hierarchy_note'], 'verified-parent-child-rollup'


def _parents_and_children(cats, vals, ids):
    """{parent index: [child indexes]}, or a rejection reason (str).

    A child belongs to a parent by official ID ancestry ("2.1" under "2") or
    when every word of the parent's name (2+ words) is in the child's name.
    "Non-X" is a sibling of "X", never its child. A child larger than its
    parent is a contradiction and rejects the chart.
    """
    parents = {}
    for i, label in enumerate(cats):
        parent_terms = _terms(label)
        if len(parent_terms) < 2 or parent_terms & {'total', 'overall', 'all'}:
            continue
        children = []
        for j, child in enumerate(cats):
            if i == j or not vals[j]:
                continue
            child_terms = _terms(child)
            negated_sibling = bool(
                re.fullmatch(r'non[\s-]+' + re.escape(str(label).casefold()), str(child).casefold())
            )
            exact_lineage = bool(ids[i] and ids[j] and ids[j].startswith(ids[i] + '.'))
            contains_name = (
                parent_terms < child_terms
                and len(parent_terms) >= 2
                and all(len(term) > 1 for term in parent_terms)
                and len(child_terms - parent_terms) >= 1
            )
            if exact_lineage or (contains_name and not negated_sibling):
                children.append(j)
        if not children:
            continue
        if any(vals[i] < vals[j] - 1e-8 for j in children):
            return 'suspected-parent-child-values-conflict'
        parents[i] = children
    return parents


def _keep_after_rollups(cats, vals, parents):
    """Indexes to plot after dropping reconciled children and a reconciled Total.

    Returns a rejection reason (str) when a parent is not exactly the sum of
    its children, when there are several "Total" rows or the Total does not
    add up, or when fewer than two independent categories remain.
    """
    removed = set()
    for parent, children in parents.items():
        if parent in removed:
            continue
        if not math.isclose(
            vals[parent], sum(vals[j] for j in children), rel_tol=1e-9, abs_tol=1e-6
        ):
            # Likely membership is known, but not a complete split: draw nothing.
            return 'unreconciled-overlapping-categories'
        removed.update(children)
    keep = [i for i in range(len(cats)) if i not in removed]
    # A published Total is a checksum of the other rows, not another group.
    total_rows = [i for i in keep if _terms(cats[i]) in _TOTAL_TERMS]
    if total_rows:
        if len(total_rows) != 1:
            return 'ambiguous-published-total'
        others = [i for i in keep if i != total_rows[0]]
        if not math.isclose(
            vals[total_rows[0]], sum(vals[i] for i in others), rel_tol=1e-9, abs_tol=1e-6
        ):
            return 'published-total-does-not-reconcile'
        keep = others
    if len(keep) < 2:
        return 'insufficient-independent-categories'
    return keep
