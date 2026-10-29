"""Evidence-bound assumptions and scope for any supported SingStat topic.

Only describes source/series actually selected by the current answer. It does not
infer synonyms, sums, disjointness, causality, or official umbrella definitions.

Used by: ui/answer_view.py.
"""

import re

_TABLE_ID = re.compile(r'^[A-Z][A-Z0-9]{2,18}$')


def _labels(rows):
    """Clean series labels of up to 40 rows, or [] if the rows are malformed."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= 40:
        return []
    output = []
    for row in rows:
        if not isinstance(row, dict):
            return []
        label = row.get('series_name') or row.get('name')
        if not isinstance(label, str) or not label.strip() or len(label) > 180:
            return []
        label = label.strip()
        if label.casefold() not in {s.casefold() for s in output}:
            output.append(label)
    return output


_SCOPE_NOTE = (
    'These are the selected published series used in this answer. '
    'They are not necessarily an exhaustive list or a single combined total. '
    'Parent groups and subcategories may overlap; values are not added unless '
    'the source explicitly supports doing so.'
)


def scope_from_evidence(answer, response):
    """Return verified data-scope descriptions for the current displayed answer.

    One entry per cited, complete, chart-verified table: the series it used
    (only those on the displayed chart, when there is one), its unit and a
    note that the list is not necessarily exhaustive. At most six entries.
    """
    if not isinstance(answer, dict) or not isinstance(response, dict):
        return []
    if answer.get('verified') is not True:
        return []
    data = response.get('tool_results')
    if not isinstance(data, list):
        return []
    allowed = {t for t, _ in (answer.get('official_sources') or []) if isinstance(t, str)}
    if not allowed:
        return []
    chart = answer.get('chart_spec')
    plotted = _labels(chart.get('series')) if isinstance(chart, dict) else []
    unique = {}
    for result in data:
        labels = _scope_labels(result, allowed, plotted)
        if labels:
            # Duplicate sources (a discovery call and a chart call) collapse to one entry.
            key = (result['table_id'], tuple(x.casefold() for x in labels))
            unique[key] = {
                'table_id': result['table_id'],
                'labels': labels,
                'unit': str(result.get('unit') or '').strip()[:100],
                'note': _SCOPE_NOTE,
            }
    return list(unique.values())[:6]


def _scope_labels(result, allowed, plotted) -> list:
    """The series labels a cited, complete, chart-verified result contributes ([] otherwise).

    When a chart is displayed, only labels on that exact chart count, never
    unrelated series returned by the same broad discovery call.
    """
    if not isinstance(result, dict):
        return []
    table = result.get('table_id')
    if (
        not isinstance(table, str)
        or not _TABLE_ID.fullmatch(table)
        or table not in allowed
        or result.get('success') is not True
        or result.get('table_complete') is not True
        or result.get('chart_source_verified') is not True
    ):
        return []
    labels = _labels(result.get('selected_series'))
    if not labels:
        spec = result.get('chart_spec')
        labels = _labels(spec.get('series')) if isinstance(spec, dict) else []
    if labels and plotted:
        shown = {p.casefold() for p in plotted}
        labels = [label for label in labels if label.casefold() in shown]
    return labels
