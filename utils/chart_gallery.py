"""Generic, fail-closed graph-gallery selection from official tool results.

Only graphs whose full displayed values can be reconciled to the returned official
observations are eligible. This module does not retrieve data or mutate state.

Used by: ui/answer_view.py, ui/table_explorer.py,
tools/characterise_chart_gallery.py, tools/characterise_table_choice.py.
"""

from __future__ import annotations
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import json
import math
import re
from utils.query_text import asks_for_level, explicit_frequency
from utils.vocabulary import CONCEPT_PHRASES, CONCEPT_WORDS
from utils.official_links import table_page_url


def describe_identified_tables(response):
    """Identify confirmed complete official tables without claiming chart readiness."""
    if not isinstance(response, dict):
        return []
    seen = set()
    output = []
    for result in response.get('tool_results') or []:
        if not isinstance(result, dict) or result.get('success') is not True:
            continue
        table = str(result.get('table_id') or '')
        if not re.fullmatch(r'(?:[A-Z][A-Z0-9]{2,18}|[0-9]{3,8})', table) or table in seen:
            continue
        if result.get('table_complete') is not True:
            continue
        source = str(result.get('source_url') or '')
        if source not in (
            f'https://tablebuilder.singstat.gov.sg/table/TS/{table}',
            f'https://tablebuilder.singstat.gov.sg/table/CT/{table}',
            f'https://tablebuilder.singstat.gov.sg/table/DC/{table}',
        ):
            continue
        seen.add(table)
        output.append(
            {
                'table_id': table,
                'title': str(result.get('title') or table)[:220],
                'source_url': source,
                'is_crosstab': '/CT/' in source,
            }
        )
    return output[:8]


def _verified_tool_charts(result):
    """Yield only independently checkable single-series trend graph specifications.

    Each line of a complete, verified, untruncated trend result becomes its own
    chart, but only when its series ID is unambiguous and every plotted value
    equals the observation the tool returned for that period.
    """
    checkable = _checkable_trend(result)
    if checkable is None:
        return []
    spec, categories, lines, observations, selected = checkable
    ids_by_name = defaultdict(list)
    for item in selected:
        if isinstance(item, dict) and item.get('series_no') is not None:
            ids_by_name[str(item.get('series_name'))].append(str(item['series_no']))
    observed = _observations_by_series(observations)
    if observed is None:
        return []
    output = []
    for entry in lines[:8]:
        chart = _single_series_chart(result, spec, categories, entry, ids_by_name, observed)
        if chart is not None:
            output.append(chart)
    return output


def _checkable_trend(result):
    """(spec, categories, lines, observations, selected) of a checkable trend result, or None."""
    if (
        not isinstance(result, dict)
        or result.get('success') is not True
        or result.get('table_complete') is not True
        or result.get('chart_source_verified') is not True
        or result.get('observations_truncated') is True
        or result.get('series_selection_truncated') is True
    ):
        return None
    spec = result.get('chart_spec')
    if not isinstance(spec, dict) or spec.get('kind') != 'trend':
        return None
    categories, lines = spec.get('categories'), spec.get('series')
    observations, selected = result.get('observations'), result.get('selected_series')
    if (
        not isinstance(categories, list)
        or not 2 <= len(categories) <= 500
        or len(set(map(str, categories))) != len(categories)
    ):
        return None
    if not all(isinstance(x, list) for x in (lines, observations, selected)):
        return None
    return spec, categories, lines, observations, selected


def _observations_by_series(observations):
    """{series ID: {period: value}}, or None if any observation is malformed or repeated."""
    observed = defaultdict(dict)
    for row in observations:
        if not isinstance(row, dict):
            return None
        sid, period, value = str(row.get('series_no')), str(row.get('period')), row.get('value')
        if type(value) not in (int, float) or not math.isfinite(value) or period in observed[sid]:
            return None
        observed[sid][period] = value
    return observed


def _single_series_chart(result, spec, categories, entry, ids_by_name, observed):
    """One line of the trend as its own chart, or None if it cannot be checked exactly."""
    if not isinstance(entry, dict):
        return None
    name = str(entry.get('name') or '')
    ids = ids_by_name.get(name, [])
    if len(ids) != 1:
        return None  # duplicated leaf labels require an exact ID
    sid = ids[0]
    values = entry.get('values')
    if not isinstance(values, list) or len(values) != len(categories):
        return None
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        return None
    if len(observed[sid]) != len(categories):
        return None
    if any(
        str(p) not in observed[sid]
        or not math.isclose(float(v), float(observed[sid][str(p)]), rel_tol=1e-10, abs_tol=1e-6)
        for p, v in zip(categories, values)
    ):
        return None
    one = deepcopy(spec)
    one['series'] = [deepcopy(entry)]
    one['title'] = f"{str(result.get('title') or 'Official series')[:110]} — {name[:70]}"
    token = sha256(
        json.dumps([result.get('table_id'), sid, categories, values], sort_keys=True).encode()
    ).hexdigest()[:20]
    return {
        'id': token,
        'table_id': str(result.get('table_id')),
        'series_id': sid,
        'series_name': name,
        'source_url': str(result.get('source_url') or ''),
        'chart_spec': one,
    }


def collect_verified_graphs(response, max_graphs: int = 24):
    """Deduplicate chartable official series across tool calls without inferring data."""
    if not isinstance(response, dict) or response.get('suppress_charts') is True:
        return []
    found = []
    seen = set()
    for result in response.get('tool_results') or []:
        for chart in _verified_tool_charts(result):
            key = (chart['table_id'], chart['series_id'], chart['id'])
            if key in seen:
                continue
            seen.add(key)
            found.append(chart)
            if len(found) >= max_graphs:
                return found
    return found


# Phase 3G: source-backed expansion of a discovered complete table.
def _period_order(period):
    """(year, month) for a STANDARD annual, monthly or quarterly label, else None.

    Stricter than utils.periods.period_sort_key on purpose: the table explorer
    only charts series whose every period is one of these plain forms, and
    None tells it to leave the series out.
    """
    from datetime import datetime

    label = str(period).strip()
    if re.fullmatch(r"(?:19|20)\d{2}", label):
        return (int(label), 0)
    match = re.fullmatch(r"((?:19|20)\d{2})\s+([A-Za-z]{3})", label)
    if match:
        try:
            return (int(match.group(1)), datetime.strptime(match.group(2).title(), '%b').month)
        except ValueError:
            pass
    match = re.fullmatch(r"((?:19|20)\d{2})\s*(?:[1-4]Q|Q[1-4])", label)
    if match:
        quarter = re.search(r'[1-4]', label[4:]).group()
        return (int(match.group(1)), int(quarter) * 3)
    return None


def graphs_from_complete_table(
    table_id, frame, metadata, *, max_series: int = 32, max_points: int = 120
):
    """Build optional, single-series charts from verified complete official table rows.

    Use exact series IDs; each plot is independent (different periods/units allowed).
    A catalogue, a staged snapshot, or a table with unsupported dimensions never
    establishes source-backed observations. No data is generated or interpolated.
    Leaf series come first; parent series stay optional (not selected by default).
    """
    if (
        not isinstance(metadata, dict)
        or metadata.get('table_complete') is not True
        or not isinstance(table_id, str)
        or not re.fullmatch(r'[A-Z][A-Z0-9]{2,18}', table_id)
        or metadata.get('source_url') != table_page_url(table_id)
    ):
        return []
    if frame is None or not hasattr(frame, 'columns'):
        return []
    if not {'series_no', 'series_name', 'unit', 'period', 'value'}.issubset(set(frame.columns)):
        return []
    catalogue = frame[['series_no', 'series_name', 'unit']].drop_duplicates()
    if len(catalogue) == 0 or len(catalogue) > 3000:
        return []
    ids = [str(x) for x in catalogue['series_no']]
    if len(set(ids)) != len(ids):
        return []
    parents = {
        sid for sid in ids if any(other.startswith(sid + '.') for other in ids if other != sid)
    }
    result = []
    for item in catalogue.itertuples(index=False):
        sid = str(item.series_no)
        if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', sid):
            continue
        observations = _consistent_observations(frame[frame['series_no'].astype(str) == sid])
        name, unit = str(item.series_name).strip(), str(item.unit).strip()
        if observations is None or not name or not unit:
            continue
        times = sorted(observations, key=_period_order)[-max_points:]
        series_name = _lineage_name(catalogue, sid, name)
        values = [observations[p] for p in times]
        spec = {
            'kind': 'trend',
            'title': f'{table_id} — {series_name}'[:215],
            'unit': unit[:95],
            'categories': times,
            'series': [{'name': series_name, 'values': values}],
        }
        token = sha256(
            json.dumps([table_id, sid, times, values], sort_keys=True).encode()
        ).hexdigest()[:20]
        result.append(
            {
                'id': token,
                'table_id': table_id,
                'series_id': sid,
                'series_name': series_name,
                'source_url': metadata['source_url'],
                'is_parent': sid in parents,
                'default_selected': sid not in parents,
                'chart_spec': spec,
            }
        )
    result.sort(key=lambda x: (x['is_parent'], tuple(int(n) for n in x['series_id'].split('.'))))
    return result[:max_series]


def _consistent_observations(rows):
    """{period: value} for one series, or None if under 2 periods or a period disagrees.

    Unrecognised period labels and missing values are skipped; the same value
    published twice for a period is accepted, two different values are not.
    """
    observations = {}
    for row in rows[['period', 'value']].itertuples(index=False):
        if not isinstance(row.period, str) or _period_order(row.period) is None:
            continue
        try:
            numeric = float(row.value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(numeric):
            continue
        if row.period in observations and not math.isclose(
            observations[row.period], numeric, rel_tol=1e-12, abs_tol=1e-9
        ):
            return None
        observations[row.period] = numeric
    return observations if len(observations) >= 2 else None


def _lineage_name(catalogue, sid, name) -> str:
    """The series name prefixed by up to two parent labels (at most 190 characters)."""
    segments = sid.split('.')
    lineage = []
    for depth in range(1, len(segments)):
        matches = catalogue[catalogue['series_no'].astype(str) == '.'.join(segments[:depth])]
        if len(matches) == 1:
            lineage.append(str(matches.iloc[0]['series_name']))
    series_name = ' / '.join(lineage[-2:] + [name]) if lineage else name
    return series_name[-190:] if len(series_name) > 190 else series_name


def load_table_gallery(table_id, loader, *, max_series: int = 32):
    """Reload official complete observations rather than using truncated tool previews."""
    if not callable(loader) or not re.fullmatch(r'[A-Z][A-Z0-9]{2,18}', str(table_id)):
        return []
    try:
        frame, metadata = loader(table_id)
    except (ValueError, KeyError, TypeError, RuntimeError, OSError):
        return []
    return graphs_from_complete_table(table_id, frame, metadata, max_series=max_series)


# Phase 3H: table-first, chronology-preserving combined trend charts.
# A published row whose own label says it is a total/overall/all figure.
TOTAL_NAME = re.compile(r'(?i)^\s*(?:grand\s+)?(?:total|overall|all\b)')


_NO_SERIES = {'series': [], 'charts': [], 'total_chartable': 0, 'limited': False}


def table_series_charts(
    table_id,
    frame,
    metadata,
    *,
    max_series: int = 512,
    max_points: int = 500,
    max_per_chart: int = 8,
) -> dict:
    """Return every eligible table series, chart-ready and partitioned by unit.

    Do not infer mathematical relationships between series. A group uses a
    chronological UNION of observed periods; absent values stay None rather
    than becoming zero. Each official series is located by its exact ID.

    Steps: the table must be complete, official and well-formed
    (_series_catalogue); each series needs 2+ unique, finite observations
    (_series_observations); names gain their parents' labels for display.
    """
    if not (1 <= max_series <= 512 and 2 <= max_points <= 500 and 1 <= max_per_chart <= 8):
        raise ValueError('Unsafe chart capacity.')
    catalogue = _series_catalogue(table_id, frame, metadata)
    if catalogue is None:
        return dict(_NO_SERIES)
    identities = [str(item.series_no).strip() for item in catalogue.itertuples(index=False)]
    lookup = {
        str(item.series_no).strip(): str(item.series_name).strip()
        for item in catalogue.itertuples(index=False)
    }
    all_series = []
    for item in catalogue.itertuples(index=False):
        sid, name, unit = (
            str(item.series_no).strip(),
            str(item.series_name).strip(),
            str(item.unit).strip(),
        )
        if (
            not re.fullmatch(r'\d+(?:\.\d+)*', sid)
            or not name
            or not unit
            or len(name) > 350
            or len(unit) > 120
        ):
            continue
        observations = _series_observations(frame[frame['series_no'].astype(str) == sid])
        if observations is not None:
            all_series.append(_series_entry(sid, name, unit, observations, lookup, identities))
    all_series.sort(key=lambda x: tuple(int(i) for i in x['series_id'].split('.')))
    total = len(all_series)
    chosen = all_series[:max_series]
    return {
        'series': chosen,
        'charts': create_table_charts(
            table_id, chosen, max_points=max_points, max_per_chart=max_per_chart
        ),
        'total_chartable': total,
        'limited': total > max_series,
    }


def _series_catalogue(table_id, frame, metadata):
    """The (series_no, series_name, unit) catalogue of a complete official table, or None.

    The complete-table loader is authoritative for observation identity, not
    the truncated 40-row analytical preview; series IDs must be unique.
    """
    if (
        not isinstance(table_id, str)
        or not re.fullmatch(r'[A-Z][A-Z0-9]{2,18}', table_id)
        or not isinstance(metadata, dict)
        or metadata.get('table_complete') is not True
        or metadata.get('source_url') != table_page_url(table_id)
    ):
        return None
    if (
        frame is None
        or not hasattr(frame, 'columns')
        or not {'series_no', 'series_name', 'unit', 'period', 'value'}.issubset(frame.columns)
    ):
        return None
    catalogue = frame[['series_no', 'series_name', 'unit']].drop_duplicates()
    if catalogue.empty or len(catalogue) > 10000:
        return None
    identities = [str(item.series_no).strip() for item in catalogue.itertuples(index=False)]
    return catalogue if len(set(identities)) == len(identities) else None


def _series_observations(rows):
    """{period: value} for one series, or None if fewer than 2 or not provably unique.

    Missing values are skipped (never zero); an unrecognised period label with
    a value, or a period published twice, rejects the series.
    """
    observations = {}
    for period, value in rows[['period', 'value']].itertuples(index=False):
        if not isinstance(period, str) or _period_order(period) is None:
            if value is not None:
                return None
            continue
        try:
            number = float(value)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(number):
            continue
        if period in observations:
            return None
        observations[period] = number
    return observations if len(observations) >= 2 else None


def _series_entry(sid, name, unit, observations, lookup, identities) -> dict:
    """One chart-ready series, its display name prefixed by up to two parent labels."""
    pieces = sid.split('.')
    parents = [
        lookup['.'.join(pieces[:i])]
        for i in range(1, len(pieces))
        if '.'.join(pieces[:i]) in lookup
    ]
    is_parent = any(x.startswith(sid + '.') for x in identities)
    parent_id = next(
        (
            '.'.join(pieces[:k])
            for k in range(len(pieces) - 1, 0, -1)
            if '.'.join(pieces[:k]) in lookup
        ),
        '',
    )
    return {
        'series_id': sid,
        'series_name': ' / '.join(parents[-2:] + [name])[:300],
        'unit': unit,
        'observations': observations,
        'is_parent': is_parent,
        # Presentation-only hierarchy hints (no values change).
        'leaf_name': name[:250],
        'parent_id': parent_id,
        'is_total': bool(is_parent or TOTAL_NAME.match(name)),
    }


def create_table_charts(table_id, selected, *, max_points: int = 500, max_per_chart: int = 8):
    """Build multiple chronological line charts without mixing incompatible units."""
    by_unit = {}
    for entry in selected:
        by_unit.setdefault(entry['unit'], []).append(entry)
    charts = []
    for unit, items in by_unit.items():
        for start in range(0, len(items), max_per_chart):
            chunk = items[start : start + max_per_chart]
            all_periods = {p for item in chunk for p in item['observations']}
            periods = sorted(all_periods, key=_period_order)[-max_points:]
            if len(periods) < 2:
                continue
            charts.append(
                {
                    'kind': 'trend',
                    'title': f'{table_id} — Published series ({unit})',
                    'unit': unit,
                    'categories': periods,
                    'series': [
                        {
                            'name': item['series_name'][:250],
                            'values': [item['observations'].get(t) for t in periods],
                        }
                        for item in chunk
                    ],
                    'x_axis_type': 'linear_years'
                    if all(re.fullmatch(r'(?:19|20)\d{2}', p) for p in periods)
                    else 'category',
                }
            )
    return charts


def _question_frequency(question):
    """Explicitly requested observation frequency (shared rule in query_text)."""
    return explicit_frequency(question)


def _measurement_words(value) -> set:
    """Normalise words to search concepts for discovery only (never values).

    Concepts come from utils/vocabulary.py (CONCEPT_WORDS, CONCEPT_PHRASES).
    Generic negation rule: "non X", "non-X" and "nonX" become "non" + concept,
    so opposite concepts (fatal / non-fatal) are never merged.
    """
    source = str(value).casefold()
    tokens = re.findall(r'[a-z0-9]+', source)
    normalized = [
        concept
        for pattern, context, concept in CONCEPT_PHRASES
        if re.search(pattern, source) and re.search(context, source)
    ]
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else ''
        if token == 'non' and following in CONCEPT_WORDS:
            normalized.append('non' + CONCEPT_WORDS[following])
            skip_next = True
        elif token.startswith('non') and token[3:] in CONCEPT_WORDS:
            normalized.append('non' + CONCEPT_WORDS[token[3:]])
        elif token in CONCEPT_WORDS:
            normalized.append(CONCEPT_WORDS[token])
        else:
            normalized.append(token[:-1] if token.endswith('s') and len(token) > 4 else token)
    return set(normalized)


def _requested_measure_unit(question):
    """Whether the question asks for a COUNT or a RATE (None if it does not say)."""
    q = str(question).casefold()
    if re.search(r'\b(rate|per 100[,.]?000|percentage|percent|proportion)\b', q):
        return 'rate'
    if asks_for_level(question):
        return 'count'
    return None


def _published_measure_unit(series):
    """Classify a reported series unit without interpreting its values."""
    unit = str(series.get('unit') or '').casefold()
    if re.search(r'\b(per|rate|percent|percentage)\b|%', unit):
        return 'rate'
    if re.search(r'\b(number|count|persons|people|cases)\b', unit):
        return 'count'
    return None


def _title_words(value):
    """Normalise words for coarse discovery only (never redefine a measure)."""
    stop = {
        'how',
        'has',
        'have',
        'the',
        'a',
        'an',
        'in',
        'of',
        'from',
        'to',
        'each',
        'year',
        'years',
        'monthly',
        'annually',
        'annual',
        'quarter',
        'quarterly',
        'changed',
        'change',
        'over',
        'time',
        'by',
        'what',
        'are',
        'were',
        'and',
        'value',
        'values',
        'number',
        'numbers',
        'singapore',
        'singapores',
    }
    words = set(re.findall(r'[a-z0-9]+', str(value).casefold())) - stop

    def canonical(word):
        # Common action verbs in official statistical titles and user questions.
        # Do not use unrestricted stemming (e.g. 'rate' and 'rated' need not mean
        # the same statistical measure).
        """A word reduced to a comparable form (light suffix trimming only)."""
        for ending, root in (
            ('constructed', 'construct'),
            ('constructing', 'construct'),
            ('construction', 'construct'),
            ('completed', 'complete'),
            ('completing', 'complete'),
        ):
            if word == ending:
                return root
        return word[:-1] if word.endswith('s') and len(word) > 4 else word

    return {canonical(w) for w in words}


# Terms which describe a question's grammatical form rather than the statistic.
# They can NEVER, by themselves, justify displaying an official graph.
_RELEVANCE_FILLER = frozenset(
    {
        'how',
        'many',
        'much',
        'what',
        'which',
        'where',
        'when',
        'has',
        'have',
        'was',
        'were',
        'recorded',
        'report',
        'reported',
        'register',
        'registered',
        'each',
        'per',
        'year',
        'monthly',
        'annual',
        'quarterly',
        'number',
        'count',
        'total',
        'value',
        'rate',
        'trend',
        'change',
        'changed',
        'increase',
        'decrease',
        'share',
        'percent',
        'singapore',
        'resident',
        'data',
        'statistic',
        'statistic',
        'the',
        'for',
        'about',
        'among',
        'acros',
        'across',
        'from',
        'into',
        'under',
    }
)


def relevant_official_table(question, table, response=None) -> bool:
    """Fail closed when a retrieved table measures an unrelated subject.

    RAG similarity, publication freshness, and observation frequency are not
    evidence of topic relevance. Evidence may come from the official table
    heading OR verified series metadata; no numerical values are inferred.
    A subject named in a question needs at least one non-generic semantic
    concept match. This conservative gate may decline some real synonyms;
    those require an independently grounded mapping, never a random graph.
    """
    q = (_title_words(question) | _measurement_words(question)) - _RELEVANCE_FILLER
    if not q:
        return False
    table_id = str(table.get('table_id') or '')
    phrases = [str(table.get('title') or '')]
    if isinstance(response, dict):
        for result in response.get('tool_results') or []:
            if not isinstance(result, dict) or str(result.get('table_id') or '') != table_id:
                continue
            # Catalogue metadata may identify the measure, but is not proof
            # that corresponding numeric observations are available.
            for series in (result.get('series') or []) + (result.get('selected_series') or []):
                if isinstance(series, dict):
                    phrases.append(str(series.get('series_name') or ''))
            for series in result.get('series_summary') or []:
                if isinstance(series, dict):
                    phrases.append(str(series.get('series_name') or ''))
    subject = set()
    for phrase in phrases:
        subject.update((_title_words(phrase) | _measurement_words(phrase)) - _RELEVANCE_FILLER)
    # Terms like 'speeding' / 'traffic offence' must not be satisfied by a
    # generic 'recorded' series or an unemployment table.
    if q & {'speeding', 'speed', 'traffic', 'violation', 'offence', 'offense'}:
        requested = q & {'speeding', 'speed', 'traffic', 'violation', 'offence', 'offense'}
        if 'speeding' in requested:
            return 'speeding' in subject or ({'speed', 'violation'} <= subject)
        return bool(requested & subject)
    return bool(q & subject)


def rank_identified_tables(question, tables):
    """Prioritise requested frequency and subject; retain original order on ties.

    This is table discovery ranking, NOT proof that a table measures precisely
    the requested population/technology or covers all requested dimensions.
    """
    freq = _question_frequency(question)
    wanted = _title_words(question)

    def quality(item) -> tuple:
        """Rank key of a table for the question: title overlap, completeness, then recency."""
        title = str(item.get('title') or '')
        title_words = _title_words(title)
        overlap = len(wanted & title_words)
        normalized_title = title.casefold()
        normalized_freq = normalized_title
        matching_freq = freq and bool(re.search(r'\b' + freq + r'\b', normalized_freq))
        wrong_freq = (
            freq
            and bool(re.search(r'\b(?:annual|quarterly|monthly)\b', normalized_freq))
            and not matching_freq
        )
        return (
            10 if matching_freq else (-10 if wrong_freq else 0),
            overlap,
            overlap / max(1, len(wanted)),
        )

    return sorted(tables, key=quality, reverse=True)


def choose_primary_identified_table(question, tables, response, answer=None):
    """Select one table; prefer verified answer citation within tied RAG matches.

    Hybrid retrieval scores are discovery signals, never numerical evidence.
    Freshness uses the latest explicitly reported period/year when available.

    Returns (table or None, whether several tables tied, IDs of the tied tables).
    Order of preference: relevant tables -> highest search score (ties kept)
    -> the requested reporting frequency -> a table the answer cites -> the
    most recent coverage -> the original ranking.
    """
    ranked = [
        t
        for t in rank_identified_tables(question, tables)
        if relevant_official_table(question, t, response)
    ]
    if not ranked:
        return None, False, []
    response = response if isinstance(response, dict) else {}
    tied = _top_scored_tables(ranked, _search_scores(response))
    # Score ties can include unrelated frequencies: keep the requested frequency.
    freq = _question_frequency(question)
    if freq and len(tied) > 1:
        tied = [t for t in tied if re.search(r'\b' + freq + r'\b', t['title'], re.I)] or tied
    ambiguous = len(tied) > 1
    candidates = tied if ambiguous else (tied or ranked[:1])
    # A cited source can break a genuine top-score tie, not override a clearly
    # better scored source. A citation is not itself proof of measurement fit.
    cited = (
        {
            str(row[0])
            for row in answer.get('official_sources') or []
            if isinstance(row, (tuple, list)) and row
        }
        if isinstance(answer, dict)
        else set()
    )
    candidates = [t for t in candidates if t['table_id'] in cited] or candidates
    # Most recent confirmed coverage first; stable ranked ordering breaks
    # remaining ties. Never imply an older/candidate table was checked live.
    selected = max(candidates, key=lambda t: _latest_covered_year(response, t['table_id']))
    return selected, ambiguous, [t['table_id'] for t in tied]


def _search_scores(response) -> dict:
    """{table ID: best search score} from the response's search notes (invalid scores skipped)."""
    scores = {}
    for row in response.get('rag_results') or []:
        if not isinstance(row, dict):
            continue
        match = re.search(
            r'/table/(?:TS|CT|DC)/([A-Z][A-Z0-9]{2,18})\b', str(row.get('source_url') or '')
        ) or re.search(r'\b([A-Z]\d{5,8})\b', str(row.get('text') or ''))
        if not match:
            continue
        try:
            score = float(row.get('hybrid_score'))
        except (ValueError, TypeError):
            continue
        if math.isfinite(score):
            scores[match.group(1)] = max(score, scores.get(match.group(1), float('-inf')))
    return scores


def _top_scored_tables(ranked, scores) -> list:
    """The ranked tables that share the top search score (empty if none was scored)."""
    scored = [t for t in ranked if t['table_id'] in scores]
    if not scored:
        return []
    top = max(scores[t['table_id']] for t in scored)
    return [
        t for t in scored if math.isclose(scores[t['table_id']], top, rel_tol=1e-9, abs_tol=1e-7)
    ]


def _latest_covered_year(response, table_id) -> int:
    """Latest year the response's results for this table cover (-1 if unknown)."""
    years = []
    for result in response.get('tool_results') or []:
        if not isinstance(result, dict) or result.get('table_id') != table_id:
            continue
        for key in ('year_max', 'end_year'):
            try:
                years.append(int(result[key]))
            except (KeyError, TypeError, ValueError):
                pass
        for row in result.get('series_summary') or []:
            try:
                years.append(int(str(row.get('latest_period') or '')[:4]))
            except (TypeError, ValueError):
                pass
    return max(years, default=-1)


def table_default_selection(question, table, entries, *, is_primary: bool = True) -> tuple:
    """Pick exact matching series or show relevant table's categories as related.

    An absent requested sex can never default to a combined-sex total.
    For a related primary table with a matching frequency, show a small number
    of available categories (such as both broadband speed bands), but label
    them as related rather than claiming they answer the narrower question.
    """
    if not is_primary or not entries:
        return set(), 'optional'
    exact = default_table_selection(question, entries)
    if exact:
        # Generic name token overlap, not a guarantee of semantic equivalence.
        picked = [x for x in entries if x['series_id'] in exact]
        if picked and score_table_series(question, picked[0])[4] > 0:
            wanted = _requested_measure_unit(question)
            actual = _published_measure_unit(picked[0])
            if wanted and actual and wanted != actual:
                return exact, 'related_unit'
            return exact, 'closest'
    if _requested_sex(question):
        return set(), 'unavailable_subgroup'
    freq = _question_frequency(question)
    title = str(table.get('title') or '')
    if (
        freq
        and re.search(r'\b' + freq + r'\b', title, re.I)
        and _title_words(question) & _title_words(title)
    ):
        # If no direct series match, show the TABLE'S available breakdown,
        # clearly described as related; never assert category = requested concept.
        if len(entries) <= 8:
            return {x['series_id'] for x in entries}, 'related'
        return {entries[0]['series_id']}, 'related'
    return exact, 'closest' if exact else 'unavailable'


def _requested_sex(question):
    """Find an explicit gender/sex constraint, if present, without guessing one."""
    words = set(re.findall(r"[a-z]+", str(question).casefold()))
    women = {
        'female',
        'females',
        'woman',
        'women',
        'girl',
        'girls',
        'bride',
        'brides',
        'wife',
        'wives',
        'mother',
        'mothers',
    }
    men = {
        'male',
        'males',
        'man',
        'men',
        'boy',
        'boys',
        'groom',
        'grooms',
        'husband',
        'husbands',
        'father',
        'fathers',
    }
    f = bool(words & women)
    m = bool(words & men)
    return 'female' if f and not m else 'male' if m and not f else None


def _series_sex(name):
    """Categorise explicitly sex-specific series; unqualified totals remain neutral."""
    words = set(re.findall(r"[a-z]+", str(name).casefold()))
    if words & {
        'female',
        'females',
        'woman',
        'women',
        'girl',
        'girls',
        'bride',
        'brides',
        'wife',
        'wives',
        'mother',
        'mothers',
    }:
        return 'female'
    if words & {
        'male',
        'males',
        'man',
        'men',
        'boy',
        'boys',
        'groom',
        'grooms',
        'husband',
        'husbands',
        'father',
        'fathers',
    }:
        return 'male'
    return None


def score_table_series(question, series) -> tuple:
    """Prefer the explicitly requested subgroup over overall or opposite-sex series."""
    stop = {
        'how',
        'has',
        'have',
        'the',
        'a',
        'an',
        'in',
        'of',
        'from',
        'to',
        'each',
        'year',
        'years',
        'monthly',
        'annually',
        'changed',
        'change',
        'over',
        'time',
        'by',
        'what',
        'are',
        'were',
        'and',
        'value',
        'values',
    }
    query = {x for x in re.findall(r'[a-z0-9]+', str(question).casefold()) if x not in stop}
    name = {
        x for x in re.findall(r'[a-z0-9]+', str(series['series_name']).casefold()) if x not in stop
    }
    # Consistent singular/plural normalisation across table and series ranking.
    # Distinguishes 'secondary school' from 'all levels' without special cases.
    query = _title_words(question) | _measurement_words(question)
    name = _title_words(series['series_name']) | _measurement_words(series['series_name'])
    overlap = len(query & name)
    # Never let a non-fatal injury series answer a fatality question.
    if 'fatal' in _measurement_words(question) and 'nonfatal' in _measurement_words(
        series['series_name']
    ):
        overlap = -100
    requested = _requested_sex(question)
    actual = _series_sex(series['series_name'])
    # A requested sex is a hard relevance constraint, not a soft tie-break.
    # Combined totals may remain accessible through manual selection.
    subgroup = (
        2
        if requested and actual == requested
        else (0 if requested and actual is None else (-2 if requested else 0))
    )
    # Prefer a national/overall total to an arbitrary region when the question
    # specifies a subject but no geographic subregion or sex. This preference
    # only breaks lexical matches, it never overrides explicit query terms.
    total_pref = int("total" in name and "total" not in query)
    wanted_unit = _requested_measure_unit(question)
    actual_unit = _published_measure_unit(series)
    unit_fit = 1 if wanted_unit and wanted_unit == actual_unit else 0
    # A request for a number/count cannot prefer a percentage simply because
    # the percentage title has more overlapping background nouns. A rate is
    # still allowed as an explicitly labelled *related* result when no count
    # series is available, handled by table_default_selection below.
    return (
        subgroup,
        int(overlap > 0),
        unit_fit,
        overlap / max(len(query), 1),
        overlap,
        total_pref,
        -len(name),
    )


def default_table_selection(question, entries) -> set:
    """Select the nearest exact published series, with sex-aware defaults."""
    if not entries:
        return set()
    requested = _requested_sex(question)
    candidates = (
        [x for x in entries if _series_sex(x['series_name']) == requested] if requested else entries
    )
    if requested and not candidates:
        # Don't assert combined-sex data answer a sex-specific question.
        return set()
    ranked = sorted(candidates, key=lambda item: score_table_series(question, item), reverse=True)
    if not requested and score_table_series(question, ranked[0])[4] <= 0:
        # No relevant category was discovered. Do not silently graph row one.
        return set()
    if len(ranked) > 1 and score_table_series(question, ranked[0]) == score_table_series(
        question, ranked[1]
    ):
        return set()
    return {ranked[0]['series_id']}


def _chart_title(table_title, series_names, suffix: str = ""):
    """A chart title that stands on its own: the table's title, plus the series when only one is drawn.

    Returns "" when there is no table title (callers then keep their older generic wording).
    """
    base = " ".join(str(table_title or "").split())
    if not base:
        return ""
    names = [n for n in dict.fromkeys(" ".join(str(n or "").split()) for n in series_names) if n]
    if len(names) == 1:
        a, b = names[0].casefold(), base.casefold()
        if a not in b and b not in a:  # do not repeat "X — X"
            base = f"{base} — {names[0]}"
    return f"{base} — {suffix}" if suffix else base


def create_single_table_chart(table_id, selected, *, max_points: int = 500, table_title=None):
    """One chronological overlay. Mixed published units are indexed to 100."""
    if not selected:
        return None
    periods = sorted({p for item in selected for p in item['observations']}, key=_period_order)[
        -max_points:
    ]
    if len(periods) < 2:
        return None
    units = {str(x['unit']) for x in selected}
    mixed = len(units) > 1
    lines = []
    for entry in selected:
        raw = [entry['observations'].get(p) for p in periods]
        if mixed:
            baseline = next((float(v) for v in raw if v is not None and float(v) != 0), None)
            if baseline is None:
                # A zero-only series can be plotted as zero indexed observations.
                if any(v is not None and float(v) != 0 for v in raw):
                    continue
                values = [0.0 if v is not None else None for v in raw]
            else:
                values = [(float(v) / baseline * 100) if v is not None else None for v in raw]
        else:
            values = raw
        lines.append(
            {
                'name': entry['series_name'],
                'values': values,
                'actual_values': raw,
                'actual_unit': entry['unit'],
            }
        )
    if not lines:
        return None
    title = _chart_title(table_title, [line['name'] for line in lines]) or (
        f'{table_id} — Selected published ' f'series'
    )
    return {
        'kind': 'trend',
        'title': title[:200],
        'unit': 'Index (first available nonzero value = 100)' if mixed else selected[0]['unit'],
        'categories': periods,
        'series': lines,
        'mixed_original_units': mixed,
        'x_axis_type': 'linear_years'
        if all(re.fullmatch(r'(?:19|20)\d{2}', p) for p in periods)
        else 'category',
    }


def load_complete_table_series(table_id, loader, *, max_series: int = 512):
    """Load the verified complete table once; only official TS tables are supported."""
    if not callable(loader) or not re.fullmatch(r'[A-Z][A-Z0-9]{2,18}', str(table_id)):
        return {'series': [], 'charts': [], 'total_chartable': 0, 'limited': False}
    try:
        frame, meta = loader(table_id)
    except (ValueError, KeyError, TypeError, RuntimeError, OSError):
        return {'series': [], 'charts': [], 'total_chartable': 0, 'limited': False}
    return table_series_charts(table_id, frame, meta, max_series=max_series)


# ---------------------------------------------------------------------------
# Readable grouping of selected series (presentation only; values unchanged)
# ---------------------------------------------------------------------------


def is_total_series(entry) -> bool:
    """True for a parent row or a row whose own label is a Total/Overall/All figure."""
    return bool(
        entry.get('is_total')
        or entry.get('is_parent')
        or TOTAL_NAME.match(str(entry.get('leaf_name') or entry.get('series_name') or ''))
    )


def related_series(entries, selected_ids, *, limit: int = 8) -> dict:
    """Suggest the totals and sibling series most related to a selection.

    totals:  ancestors of each selected row, plus Total-type rows in the same
             group and unit (flat tables keep totals beside their categories).
    related: other categories under the same parent with the same unit.
    Selected rows are never suggested again. Order follows the official table.
    """
    by_id = {e['series_id']: e for e in entries}
    chosen = [by_id[i] for i in selected_ids if i in by_id]
    picked = set(selected_ids)
    totals, related = [], []

    def add(bucket, sid) -> None:
        """Add a series to a selection bucket once (never a total or an already picked one)."""
        if sid not in picked and sid not in totals and sid not in related:
            bucket.append(sid)

    for entry in chosen:
        pieces = entry['series_id'].split('.')
        for k in range(len(pieces) - 1, 0, -1):
            ancestor = '.'.join(pieces[:k])
            if ancestor in by_id and by_id[ancestor]['unit'] == entry['unit']:
                add(totals, ancestor)
        group = entry.get('parent_id', '')
        for other in entries:
            if (
                other.get('parent_id', '') != group
                or other['unit'] != entry['unit']
                or other['series_id'] == entry['series_id']
            ):
                continue
            add(totals if is_total_series(other) else related, other['series_id'])
        if entry.get('is_parent'):
            # A selected total: its direct breakdown is the most related detail.
            for other in entries:
                if other.get('parent_id') == entry['series_id'] and other['unit'] == entry['unit']:
                    add(related, other['series_id'])
    order = {e['series_id']: i for i, e in enumerate(entries)}
    totals.sort(key=order.get)
    related.sort(key=order.get)
    return {'totals': totals[:limit], 'related': related[:limit]}


# Series whose typical sizes differ by more than this factor cannot share one axis without the
# smaller lines flattening against zero, so they are drawn on a separate chart.
# Series with the same unit always share ONE chart, whatever their sizes: people choose
# which series to add, and hovering shows any value even on a flat-looking line.
# (Was 10: series 10x smaller got a separate chart, which multiplied the charts.)
SCALE_RATIO_LIMIT = math.inf


def _typical_level(entry):
    """Typical size of a series: the median of its non-zero absolute values (0 if none)."""
    values = []
    for value in (entry.get('observations') or {}).values():
        try:
            number = abs(float(value))
        except (TypeError, ValueError):
            continue
        if number > 0 and number != float('inf') and number == number:
            values.append(number)
    values.sort()
    if not values:
        return 0.0
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


def group_selected_for_charts(
    table_id, selected, *, max_charts: int = 6, max_points: int = 500, table_title=None
) -> tuple:
    """Put the selected series on as few charts as possible.

    ONE chart with many lines. A separate chart is made only when the published unit
    differs (sizes no longer split charts: SCALE_RATIO_LIMIT is unlimited). Totals and their parts
    share a chart when they share a scale. Values are never changed, so no indexing is needed.
    Returns (specs, hidden_group_count).
    """
    groups, unit_count = _scale_groups(selected)
    specs = []
    for unit, items, index, cluster_count in groups[:max_charts]:
        names = [str(e.get('series_name')) for e in items]
        leaves = [str(e.get('leaf_name') or '') for e in items]
        use_leaf = all(leaves) and len(set(leaves)) == len(leaves) and len(set(names)) == len(names)
        named = [
            {**e, 'series_name': (leaf if use_leaf else name)}
            for e, leaf, name in zip(items, leaves, names)
        ]
        spec = create_single_table_chart(table_id, named, max_points=max_points)
        if spec is None:
            continue
        label = _scale_label(index, cluster_count)
        suffix = (
            ''
            if len(groups) == 1
            else ' · '.join(part for part in (label, unit if unit_count > 1 else '') if part)
        )
        drawn = [line['name'] for line in spec['series']]
        # With the table's title: "<table> — <series>" for one series, "<table>" (plus which
        # scale or unit, when charts are split) for several. Without it, generic wording.
        title = _chart_title(table_title, drawn, '' if len(set(drawn)) == 1 else suffix)
        if not title:
            title = 'Selected series' if len(groups) == 1 else (suffix or 'Selected series')
        spec['title'] = title[:200]
        spec['chart_group'] = 'combined'
        specs.append(spec)
    return specs, max(0, len(groups) - max_charts)


def _scale_groups(selected):
    """([(unit, entries, scale index, scale count), ...], number of units).

    One group per published unit, split further only by scale (larger scale
    first); entries keep their table order inside a group.
    """
    position = {id(entry): index for index, entry in enumerate(selected)}
    by_unit, unit_order = {}, []
    for entry in selected:
        key = str(entry.get('unit') or '').strip().casefold()
        if key not in by_unit:
            by_unit[key] = []
            unit_order.append(key)
        by_unit[key].append(entry)
    groups = []
    for key in unit_order:
        clusters, reference = [], 0.0
        for entry in sorted(by_unit[key], key=_typical_level):
            level = _typical_level(entry)
            if clusters and (
                level == 0 or reference == 0 or level / reference <= SCALE_RATIO_LIMIT
            ):
                clusters[-1].append(entry)
                reference = reference or level
            else:
                clusters.append([entry])
                reference = level
        clusters.sort(key=lambda items: -max(_typical_level(e) for e in items))
        for index, items in enumerate(clusters):
            items.sort(key=lambda e: position[id(e)])
            groups.append((str(items[0].get('unit') or ''), items, index, len(clusters)))
    return groups, len(unit_order)


def _scale_label(index, cluster_count) -> str:
    """ "Larger values" / "Medium values" / "Smaller values" when a unit is split by scale."""
    if cluster_count == 1:
        return ''
    if index == 0:
        return 'Larger values'
    if index == cluster_count - 1 and cluster_count <= 3:
        return 'Smaller values'
    return 'Medium values' if cluster_count <= 3 else f'Scale group {index + 1}'


def _parent_label(entry):
    """Parent name from the 'grandparent / parent / name' display label, if any."""
    parts = [p.strip() for p in str(entry.get('series_name', '')).split(' / ')]
    return parts[-2] if len(parts) >= 2 else ''
