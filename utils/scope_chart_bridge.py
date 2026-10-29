"""Accept a broad table preview as a verified chart when the answer vouches for it.

A tool's preview chart often shows several series of a table whose title is
broader than the question (e.g. three research areas from a table of R&D by
sector AND area). The ordinary relevance check rightly rejects such
previews. reconcile_scoped_preview is the narrow exception: it accepts one
only when every line is named in the answer and matches official summaries
exactly. It never creates a value, a category or a source table.

Used by: utils/chart_selection.py, tools/characterise_scoped_preview.py.
Tested in: tests/test_real_answers.py.
"""

import math
import re
from utils.official_links import table_page_url
from utils.vocabulary import SCOPE_QUALIFIER_WORDS, SCOPE_WORD_EQUIVALENTS


def _normal(value) -> str:
    """Lower-case words and digits separated by single spaces (dashes unified)."""
    return ' '.join(re.findall(r'[a-z0-9]+', str(value or '').casefold().replace('–', '-')))


def _subject_words(value) -> set:
    """Subject words of ``value``: question filler removed, synonyms merged."""
    tokens = _normal(value).split()
    equivalents = SCOPE_WORD_EQUIVALENTS
    stop = {
        'how',
        'has',
        'have',
        'the',
        'in',
        'and',
        'to',
        'from',
        'over',
        'time',
        'of',
        'by',
        'singapore',
        'singstat',
        'what',
        'does',
        'are',
        'is',
        'many',
        'much',
        'these',
        'which',
        'were',
        'years',
        'year',
        'changed',
        'change',
        'trend',
        'compare',
        'across',
        'among',
        'number',
        'numbers',
        'registered',
        'annual',
        'latest',
    }
    return {equivalents.get(t, t) for t in tokens if t not in stop}


def _float(value):
    """The value as a finite float; ValueError for anything else (incl. bool, NaN)."""
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError('non-finite-value')
    return float(value)


def _close(left, right):
    """True when two finite numbers are equal within rounding noise."""
    return math.isclose(_float(left), _float(right), rel_tol=1e-9, abs_tol=1e-6)


def reconcile_scoped_preview(question, result, answer) -> tuple:
    """Accept a multi-series table preview as a verified chart, or say why not.

    Returns ``(candidate, "verified")`` or ``(None, reason)``. The reason is a
    short stable label (e.g. "series-not-in-verified-answer") written to the
    audit log. Checks, in order; the first failure decides:

    1. source:   successful, complete, verified chart from the official page
                 of a valid table, and the answer cites that table;
    2. shape:    a 2-500 year trend with 2-12 lines, one summary per line;
    3. subject:  the question is about this table, and any narrow population
                 or age band it names is on the title or every line;
    4. lines:    every line is named in the answer and agrees with its
                 summary (ID, unit, every value published, both endpoints);
    5. scope:    a question naming ONE of the lines does not get the others.

    Nothing is relaxed: this only re-labels a preview whose every value the
    answer and the official summaries already vouch for.
    """
    for check in (_source_problem, _shape_problem):
        reason = check(result, answer)
        if reason:
            return None, reason
    spec = result['chart_spec']
    reason = _subject_problem(question, result, spec) or _lines_problem(result, spec, answer)
    if reason:
        return None, reason
    if _extra_lines_for_narrow_question(question, spec['series']):
        return None, 'narrow-request-extra-series'
    return {
        **result,
        'generic_preview': False,
        'chart_kind': 'verified_scope_series_trend',
        'chart_integrity': 'official-series-and-answer-reconciled',
        'chart_priority': 118,
    }, 'verified'


def _source_problem(result, answer):
    """Step 1: a complete, verified result from the table's official page, cited in the answer."""
    if not isinstance(result, dict) or result.get('success') is not True:
        return 'no-successful-tool-result'
    if (
        result.get('table_complete') is not True
        or result.get('analysis_ready') is False
        or result.get('chart_source_verified') is not True
    ):
        return 'incomplete-or-unverified-source'
    table = str(result.get('table_id') or '')
    if not re.fullmatch(r'[A-Z][A-Z0-9]{2,18}', table) or str(
        result.get('source_url') or ''
    ) != table_page_url(table):
        return 'official-source-mismatch'
    if table.casefold() not in str(answer).casefold():
        return 'table-not-cited-in-answer'
    return None


def _shape_problem(result, _answer):
    """Step 2: a year-by-year trend of 2-12 lines, each with a summary and selected series."""
    spec = result.get('chart_spec')
    if not isinstance(spec, dict) or spec.get('kind') != 'trend':
        return 'not-a-multi-year-trend'
    axis, lines = spec.get('categories'), spec.get('series')
    summaries, selected = result.get('series_summary'), result.get('selected_series')
    if (
        not isinstance(axis, list)
        or not isinstance(lines, list)
        or not isinstance(summaries, list)
        or not isinstance(selected, list)
        or not 2 <= len(axis) <= 500
        or not 2 <= len(lines) <= 12
        or len(lines) != len(summaries)
        or len(lines) != len(selected)
    ):
        return 'missing-periods-or-series-summaries'
    if (
        len(set(map(str, axis))) != len(axis)
        or not all(re.fullmatch(r'\d{4}', str(y)) for y in axis)
        or list(map(int, axis)) != sorted(map(int, axis))
    ):
        return 'invalid-year-axis'
    return None


def _subject_problem(question, result, spec):
    """Step 3: the question is about this table, and its narrow scope is covered.

    A population word (residents, female, ...) or an age band such as
    "aged 25 to 34" in the question must appear on the table title or on
    every plotted line; a broader series must not stand in for it.
    """
    qtext = str(question or '')
    table = str(result.get('table_id') or '')
    title = str(result.get('table_title') or result.get('title') or spec.get('title') or '')
    if not (_subject_words(qtext) & _subject_words(title) or table.casefold() in qtext.casefold()):
        return 'unrelated-table-subject'
    lines = spec['series']
    q_norm = _normal(qtext)
    descriptor = _normal(title + ' ' + str(spec.get('title') or ''))
    for qualifier in SCOPE_QUALIFIER_WORDS:
        if (
            re.search(r'\b' + qualifier + r'\b', q_norm)
            and not re.search(r'\b' + qualifier + r'\b', descriptor)
            and not all(
                qualifier in _normal(line.get('name')) for line in lines if isinstance(line, dict)
            )
        ):
            return 'population-or-subgroup-mismatch'
    age = re.search(r'\b(?:age|aged)\s+(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\b', q_norm)
    if age and not all(
        f'{age.group(1)} {age.group(2)}' in _normal(line.get('name'))
        for line in lines
        if isinstance(line, dict)
    ):
        return 'specific-age-band-not-covered'
    return None


def _lines_problem(result, spec, answer):
    """Step 4: each line is named in the answer and agrees exactly with its summary."""
    axis, lines = spec['categories'], spec['series']
    allowed = None
    claims = result.get('numeric_claims')
    if isinstance(claims, list) and claims:
        try:
            allowed = {round(_float(v), 6) for v in claims}
        except ValueError:
            return 'invalid-claims'
    names, seen_ids = [], set()
    for line, summary, chosen in zip(lines, result['series_summary'], result['selected_series']):
        if not all(isinstance(x, dict) for x in (line, summary, chosen)):
            return 'bad-series-metadata'
        key = _normal(str(line.get('name') or '').strip())
        if (
            not key
            or key in names
            or _normal(summary.get('series_name')) != key
            or key not in _normal(answer)
        ):
            return 'series-not-in-verified-answer'
        names.append(key)
        sid = str(summary.get('series_no') or '')
        if not sid or sid in seen_ids or sid != str(chosen.get('series_no') or ''):
            return 'series-id-mismatch'
        seen_ids.add(sid)
        if _normal(summary.get('unit')) != _normal(spec.get('unit')):
            return 'series-unit-mismatch'
        reason = _line_values_problem(line.get('values'), axis, summary, allowed)
        if reason:
            return reason
    return None


def _line_values_problem(values, axis, summary, allowed):
    """Every value finite and published; first/latest point and change equal the summary."""
    if not isinstance(values, list) or len(values) != len(axis):
        return 'missing-year-values'
    try:
        for value in values:
            _float(value)
            if allowed is not None and round(float(value), 6) not in allowed:
                return 'value-not-in-verified-claims'
        if (
            str(summary.get('earliest_period') or '') != str(axis[0])
            or str(summary.get('latest_period') or '') != str(axis[-1])
            or not _close(values[0], summary.get('earliest_value'))
            or not _close(values[-1], summary.get('latest_value'))
            or not _close(values[-1] - values[0], summary.get('change_latest_minus_earliest'))
        ):
            return 'endpoint-summary-mismatch'
    except (ValueError, TypeError, OverflowError):
        return 'non-numeric-or-incompatible-values'
    return None


def _extra_lines_for_narrow_question(question, lines) -> bool:
    """Step 5: the question names some lines only, and does not ask to compare.

    "How has engineering changed?" must not quietly get every research area;
    "compare", "by", "across", "breakdown" and similar words allow the rest.
    """
    asked = _subject_words(question)
    named = [
        i
        for i, line in enumerate(lines)
        if (tokens := _subject_words(line.get('name'))) and tokens <= asked
    ]
    return bool(
        named
        and len(named) < len(lines)
        and not re.search(
            r'\b(compare|comparison|versus|vs|other|all|different|across|by|breakdown)\b',
            str(question or ''),
            re.I,
        )
    )
