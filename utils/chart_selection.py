"""Chart selection: pick at most ONE chart that truly answers the question.

WHERE THIS FITS
===============
``app.py`` -> ``process_question`` calls, in order:

1. :func:`relevant_chart_evidence` - keep only tool results whose chart is
   about the same topic as the question (``chart_matches_question``), or
   build a small verified chart from numbers already checked in the answer
   (``verified_single_series_trend_candidate``,
   ``verified_same_year_categories_from_answer``).
2. :func:`choose_chart_result` - choose one candidate from those.
3. :func:`resolve_chart_spec` - pick the variant (trend / share /
   composition) that suits the wording of the question.

A chart that is valid but *irrelevant* is as harmful as a wrong one, so every
test here leans towards "no chart". The caller still re-validates the spec
with the Plotly builder before it is shown.

No Streamlit here: these functions can be unit-tested directly.

Moved out of ``app.py`` unchanged (behaviour-preserving refactor, Oct 2026).

Used by: ui/answer_pipeline.py, ui/answer_view.py, ui/debug_capture.py,
tools/characterise_answer_checks.py, tools/characterise_chart_relevance.py.
Tested in: tests/test_answer_checks.py, tests/test_generic_rules.py,
tests/test_real_answers.py.
"""

import hashlib
import json
import logging
import math
import re

from utils.answer_verification import (
    _verified_answer_table_rows,
    is_official_source,
    numeric_tokens,
)
from utils.chart_intent import chart_covers_requested_groups, required_groups
from utils.scope_chart_bridge import reconcile_scoped_preview
from utils.vocabulary import (
    AGE_GROUP_BANDS,
    BREAKDOWN_DIMENSION_PATTERN,
    BROAD_POPULATION_WORDS,
    CATEGORY_HEADING_PATTERN,
    CHART_EQUIVALENTS,
    CHART_STOPWORDS,
    CONCEPT_FAMILIES,
    DISTINCTIVE_SINGLE_MEASURES,
    PREVIEW_TOPIC_EQUIVALENTS,
    SUBGROUP_QUALIFIERS,
    SUBGROUP_WORDS_PATTERN,
)

LOGGER = logging.getLogger("sage_ui")


# The subject words used below (stopwords, synonyms, age bands, concept
# families) live in utils/vocabulary.py; every check here is generic.
_CHART_STOPWORDS = CHART_STOPWORDS
_CHART_EQUIVALENTS = CHART_EQUIVALENTS


def _missing_concept_family(asked, offered, *, whole_family: bool = False) -> bool:
    """True if the question names a subject family that the chart does not.

    ``whole_family``: only when the question names EVERY word of a family must
    the chart name every word too (used for series labels).
    """
    for family in CONCEPT_FAMILIES:
        if whole_family:
            if family <= asked and not family <= offered:
                return True
        elif family & asked and not family & offered:
            return True
    return False


def _age_groups_supported(asked, offered, text) -> bool:
    """Age groups named in words must appear in words or as a numeric band.

    "children" in the question is satisfied by "child" on the chart, or by a
    band such as "0-14" (see AGE_GROUP_BANDS). Otherwise the chart shows a
    different (usually overall) population and is rejected.
    """
    named = asked & set(AGE_GROUP_BANDS)
    if not named or named & offered:
        return True
    normalised = str(text).replace("\u2013", "-")
    return any(re.search(AGE_GROUP_BANDS[group], normalised) for group in named)


def _chart_topic_tokens(value) -> set:
    """Extract question/chart concepts without treating generic words as proof."""
    raw = re.findall(r"[a-zA-Z]{3,}", str(value or "").casefold())
    return {_CHART_EQUIVALENTS.get(word, word) for word in raw if word not in _CHART_STOPWORDS}


def is_generic_table_preview(result) -> bool:
    """Identify automatic source-table previews, not question-specific analyses.

    The generic SingStat inspector plots the first matching series under the
    official *table* title. That title alone is not proof that its plotted
    measures answer the user's question. The core's dedicated, internally
    reconciled tools are unaffected by this restriction.
    """
    return (
        isinstance(result, dict)
        and result.get("chart_kind") in {"time_series", "category_comparison"}
        and (
            result.get("generic_preview") is True
            or "selected_series" in result
            or "series_search" in result
        )
    )


def generic_preview_matches_question(question, result, full_answer) -> bool:
    """Admit generic previews only when their ACTUAL series fit the request.

    Reject a graph selected by a matching metadata title if its plotted series
    is unlabelled, mixes unrequested groups, or adds arbitrary extra series.
    This is deliberately stricter than topical relevance of the table title.
    """
    spec = result.get("chart_spec")
    if not isinstance(spec, dict):
        return False  # Older generic PNGs lack enough inspectable chart evidence.
    lines = spec.get("series") or []
    if not lines or not all(isinstance(x, dict) for x in lines):
        return False
    q = str(question).casefold()
    labels = _preview_labels(result, spec, lines, q)
    # Identical generic series labels can conceal separate populations or
    # geographies. Without unique labels their meaning cannot be audited.
    if labels is None or len(set(label.casefold().strip() for label in labels)) != len(labels):
        return False
    asked = _chart_topic_tokens(question)
    if not all(
        _preview_label_fits(label, labels, asked, _chart_topic_tokens(full_answer))
        for label in labels
    ):
        return False
    band = re.search(r"\baged?\s+(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b", q)
    return not band or any(
        band.group(1) + "-" + band.group(2) in label.replace("–", "-").replace(" to ", "-")
        for label in labels
    )


def _preview_labels(result, spec, lines, q):
    """The labels a preview really plots, or None when the question does not ask for them.

    A trend's labels are its line names (several lines only for comparison
    questions); a category bar chart's labels are its categories (the generic
    renderer calls its one trace "Value").
    """
    if result.get("chart_kind") == "time_series":
        labels = [str(line.get("name") or "") for line in lines]
        if any(not _chart_topic_tokens(label) for label in labels):
            return None  # 'Total'/'Value' alone is not a measure definition.
        if len(labels) > 1 and not re.search(
            r"\b(compare|versus|vs\.?|between|by (?:sex|gender|sector|industry|"
            r"age|group)|breakdown|composition|all (?:three|four)|separate lines)\b",
            q,
        ):
            return None
        return labels
    if result.get("chart_kind") == "category_comparison":
        labels = [str(v) for v in (spec.get("categories") or [])]
        if not labels or not re.search(
            r"\b(compare|which|versus|vs\.?|breakdown|by |share|composition|"
            r"distribution|different|highest|lowest)\b",
            q,
        ):
            return None
        return labels
    return None


def _preview_label_fits(label, labels, asked, prose) -> bool:
    """One plotted label shares the question's subject (also named in the answer),
    belongs to the asked concept family, and carries any requested subgroup or age group."""
    tokens = _chart_topic_tokens(label)
    substantive = asked - BROAD_POPULATION_WORDS - {"singstat", "total"}
    # Never accept insurance claims merely because the table is titled 'Life
    # Insurance Policies', or a population-wide total as an age-cohort answer.
    overlap = substantive & tokens
    if not overlap or not (overlap & prose):
        return False
    if _missing_concept_family(substantive, tokens, whole_family=True):
        return False
    # "resident" is too broad to require on an individual series label.
    required = asked & (SUBGROUP_QUALIFIERS - {"resident"})
    if required and not required <= tokens:
        # A genuine comparison can have distinct traces 'Male'/'Female'.
        if len(labels) == 1 or not required <= set().union(
            *(_chart_topic_tokens(x) for x in labels)
        ):
            return False
    # Numeric age bands are permitted when they are in the *series label*.
    return _age_groups_supported(asked, tokens, label)


def selected_series_display_title(spec):
    """Label a generic view with what it REALLY plots, not its broad table title."""
    labels = [
        str(item.get('name') or '').strip()
        for item in spec.get('series', [])
        if isinstance(item, dict)
    ]
    labels = [name for name in labels if name]
    if len(labels) == 1:
        return labels[0][:112]
    if len(labels) > 1:
        paired = ' vs '.join(labels[:2])
        return (paired + (f' and {len(labels) - 2} other series' if len(labels) > 2 else ''))[:112]
    return None


def question_chart_key(question, chart):
    """Give each actual question+chart payload a distinct Streamlit widget key."""
    identity = json.dumps(
        {"question": str(question), "chart": chart}, sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


_TREND_WORDS = re.compile(
    r"\b(trend|over time|changed|grown|growth|since|last \d+ years|past \d+ years)\b", re.I
)


def chart_matches_question(question, result, full_answer: str = "") -> bool:
    """Fail closed unless the verified chart's subject fits this exact question.

    The question's substantive topic must overlap the chart title or labelled series. This gate is
    independent of numeric verification: neither one replaces the other.

    Steps: requested comparison groups are plotted; a generic table preview
    needs its own, stricter test; otherwise the chart must share the
    question's subject, cover any age band or subgroup it names, have the
    right shape for a trend question, and be described by the answer.
    """
    if not isinstance(result, dict) or result.get('success') is not True:
        return False
    if isinstance(result.get('chart_spec'), dict) and not chart_covers_requested_groups(
        question, result['chart_spec']
    ):
        return False
    if is_generic_table_preview(result):
        if verified_generic_answer_match(question, result, full_answer):
            return True
        if not generic_preview_matches_question(question, result, full_answer):
            return _explicitly_requested_table_preview(question, result, full_answer)
    spec = result.get('chart_spec') or {}
    title_text = _chart_title_text(result, spec)
    # An unlabelled generic chart has no defensible topical match.
    topic = _chart_topic_tokens(title_text)
    asked = _chart_topic_tokens(question)
    overlap = topic & asked
    if not overlap:
        return False
    if not _chart_covers_named_scope(question, result, spec, title_text, topic, asked):
        return False
    if _TREND_WORDS.search(str(question)) and (
        spec.get('kind') not in {'trend', 'line', 'area', 'time_series'}
        or len(spec.get('categories') or []) < 2
    ):
        return False
    # The prose and graph must both cover the requested comparison.
    if any(
        group not in _chart_topic_tokens(full_answer)
        and group not in str(full_answer).lower().replace('–', '-')
        for group in required_groups(question)
    ):
        return False
    # Also require actual chart topic overlap with the verified answer.
    return bool(overlap & _chart_topic_tokens(full_answer))


def _explicitly_requested_table_preview(question, result, full_answer) -> bool:
    """A generic preview is normally too broad; this is the one exception.

    A user who EXPLICITLY names this official table may see its single
    selected, labelled trend (never an arbitrary multi-series preview), and
    only if the question names no subgroup and the answer discusses that series.
    """
    spec = result.get('chart_spec') or {}
    plotted = spec.get('series') or []
    table_id = str(result.get('table_id') or '')
    explicit_id = bool(
        table_id
        and re.search(rf'(?<![A-Za-z0-9]){re.escape(table_id)}(?![A-Za-z0-9])', str(question), re.I)
    )
    label = (
        str(plotted[0].get('name') or '')
        if len(plotted) == 1 and isinstance(plotted[0], dict)
        else ''
    )
    if not (
        explicit_id
        and label
        and _chart_topic_tokens(label)
        and spec.get('kind') == 'trend'
        and len(spec.get('categories') or []) >= 2
        and not re.search(SUBGROUP_WORDS_PATTERN, str(question))
    ):
        return False
    return bool(_chart_topic_tokens(label) & _chart_topic_tokens(full_answer))


def _chart_title_text(result, spec) -> str:
    """Every label that says what the chart is about: titles and series names."""
    titles = [
        result.get('definition'),
        result.get('table_title'),
        result.get('chart_title'),
        spec.get('title'),
    ]
    titles.extend(item.get('name') for item in spec.get('series', []) if isinstance(item, dict))
    return ' '.join(str(t) for t in titles if t)


def _chart_covers_named_scope(question, result, spec, title_text, topic, asked) -> bool:
    """Age groups, numeric age bands, subgroups and subject families named in the question.

    An overall series cannot answer a question about a specific cohort or
    subgroup merely by sharing a broad word like "population" (necessary, not
    sufficient, conditions).
    """
    qtext = str(question).casefold()
    descriptor = (
        title_text
        + ' '
        + ' '.join(str(c) for c in (spec.get('categories') or []))
        + ' '
        + str(result.get('age_group') or '')
    ).casefold()
    descriptor = descriptor.replace('–', '-').replace(' to ', '-')
    # A chart labelled 0-14 or 65+ may use numeric age groups instead of words.
    if not _age_groups_supported(asked, topic, descriptor):
        return False
    numeric_band = re.search(r'\baged?\s+(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b', qtext)
    if numeric_band and numeric_band.group(1) + '-' + numeric_band.group(2) not in descriptor:
        return False
    qualifiers = SUBGROUP_QUALIFIERS & asked
    if qualifiers and not qualifiers <= (
        topic | _chart_topic_tokens(' '.join(str(t) for t in spec.get('categories', []) or []))
    ):
        return False
    return not _missing_concept_family(asked, topic)


_CHANGE_WORDS = re.compile(
    r"(?i)\b(trend|over time|since|between|years|year|growth|grown|changed|change)\b"
)


def verified_generic_answer_match(question, result, full_answer) -> bool:
    """Permit general-domain charts only when every plotted label is sourced.

    This widens beyond the old five tables but does not accept an unrelated
    source-table preview merely because its metadata shares one common term.

    Needs: a complete 2-500 period trend with 1-8 lines; a change-over-time
    question that names no narrow subgroup; the answer cites the table; the
    title shares the question's subject; every line is uniquely named in the
    answer and has two or more numbers.
    """
    if (
        not isinstance(result, dict)
        or result.get("success") is not True
        or result.get("table_complete") is False
        or result.get("analysis_ready") is False
    ):
        return False
    spec = result.get("chart_spec")
    if not isinstance(spec, dict) or spec.get("kind") != "trend":
        return False
    axis, lines = spec.get("categories") or [], spec.get("series") or []
    if not (2 <= len(axis) <= 500 and 1 <= len(lines) <= 8) or not _CHANGE_WORDS.search(question):
        return False
    answer_text = str(full_answer).casefold()
    table_id = str(result.get("table_id") or "")
    if not table_id or table_id.casefold() not in answer_text:
        return False
    title = str(result.get("title") or result.get("table_title") or spec.get("title") or "")
    topics = {PREVIEW_TOPIC_EQUIVALENTS.get(x, x) for x in _chart_topic_tokens(question)}
    if not topics & _chart_topic_tokens(title):
        return False
    # A high-level chart cannot replace a narrow subgroup request.
    if re.search(SUBGROUP_WORDS_PATTERN, question):
        return False
    if not isinstance(axis, list) or len(axis) != len(set(map(str, axis))):
        return False
    return _lines_named_in_answer(lines, len(axis), answer_text)


def _lines_named_in_answer(lines, n_periods, answer_text) -> bool:
    """Every line is uniquely named, named in the answer, aligned, with 2+ numbers."""
    names = []
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("values"), list):
            return False
        name, values = str(line.get("name") or "").strip(), line["values"]
        if (
            not name
            or name.casefold() in names
            or len(values) != n_periods
            or name.casefold() not in answer_text
        ):
            return False
        if sum(type(v) in (int, float) and math.isfinite(v) for v in values) < 2:
            return False
        names.append(name.casefold())
    return True


def _verified_latest_category_candidates(response, question, full_answer):
    """Build a checked latest snapshot from a query tool's same-period rows."""
    q = str(question).casefold()
    if not re.search(
        r'\b(latest|which|distributed|distribution|breakdown|by age|by sex|across '
        r'age|across sector)\b',
        q,
    ):
        return []
    found = []
    for item in response.get('tool_results', []):
        if (
            not isinstance(item, dict)
            or item.get('success') is not True
            or item.get('table_complete') is not True
            or item.get('analysis_ready') is False
            or not is_official_source(item.get('source_url'))
        ):
            continue
        spec = item.get('latest_comparison_spec')
        if not isinstance(spec, dict):
            continue
        # Require the question to mention the table or at least one specific
        # measured concept present in independently checked series labels.
        all_labels = ' '.join(str(v) for v in spec.get('categories') or [])
        qtopics = _chart_topic_tokens(question) - BROAD_POPULATION_WORDS
        if not qtopics & _chart_topic_tokens(all_labels + ' ' + str(item.get('title') or '')):
            continue
        # Do not infer percentage shares from independent rates/indexes.
        if re.search(r'\b(share|proportion|percentage|percent)\b', q):
            continue
        candidate = {
            **item,
            'chart_spec': spec,
            'chart_kind': 'verified_same_period_category',
            'chart_integrity': 'same_period_same_unit_distinct_series',
            'chart_priority': 65,
        }
        if chart_matches_question(question, candidate, full_answer):
            found.append(candidate)
    return found


def verified_single_series_trend_candidate(question, result, full_answer):
    """Recover ONE verified single-series trend chart when a generic title hid its subject.

    The ordinary relevance check rightly rejects many table previews whose
    title is too broad. This narrow exception accepts a preview when ALL of:

    1. the question asks how one measure changed over time (no ranking,
       share, breakdown or comparison);
    2. the result is one complete official series whose line, name, number
       and unit agree with its summary, and every plotted value is published;
    3. the chart's first and latest points equal the summary's endpoints;
    4. the answer names the table and its subject, and states both endpoints.

    Returns the result relabelled as a verified chart candidate, or None.
    """
    if not (_is_complete_verified_result(result) and _asks_how_one_measure_changed(question)):
        return None
    chart = _single_series_chart(result)
    if chart is None:
        return None
    spec, row, axis, values = chart
    endpoints = _matching_endpoints(row, axis, values)
    if endpoints is None or not _answer_names_table_and_subject(
        question, result, spec, full_answer
    ):
        return None
    if not _answer_states_endpoints(full_answer, *endpoints):
        return None
    if not chart_covers_requested_groups(question, spec):
        return None
    candidate = dict(result)
    # No longer an arbitrary preview: series, unit, both checked endpoints and
    # their periods are bound to the question and the answer.
    candidate.pop('selected_series', None)
    candidate.pop('series_search', None)
    candidate['chart_kind'] = 'verified_single_subject_trend'
    candidate['chart_integrity'] = 'one_full_table_series_and_answer_endpoint_match'
    candidate['chart_priority'] = 110
    return candidate


def _is_complete_verified_result(result) -> bool:
    """A successful, complete, analysis-ready official result with a verified chart."""
    return (
        isinstance(result, dict)
        and result.get('success') is True
        and result.get('table_complete') is True
        and result.get('analysis_ready') is not False
        and result.get('chart_source_verified') is True
        and is_official_source(result.get('source_url'))
    )


def _asks_how_one_measure_changed(question) -> bool:
    """A change-over-time question that is not a ranking, share, breakdown or comparison."""
    q = str(question or '').casefold()
    return bool(
        re.search(
            r'\b(?:chang(?:e|ed)|trend|over time|years?|since|'
            r'growth|declin(?:e|ed)|increas(?:e|ed))\b',
            q,
        )
        and not re.search(
            r'\b(?:highest|lowest|which|ranking|rank|'
            r'distribution|breakdown|share|percent(?:age)?|proportion|'
            r'compare|comparison|versus|vs\.?|between\s+\w+\s+and)\b',
            q,
        )
        and not re.search(BREAKDOWN_DIMENSION_PATTERN, q)
        and not required_groups(question)
    )


def _single_series_chart(result):
    """(spec, summary row, axis, values) for a one-line trend that agrees with its summary.

    Name, series number and unit must agree; the axis has 2-500 distinct
    periods; every value is a finite number and, when the result lists the
    numbers it may quote (numeric_claims), one of them. Any doubt: None.
    """
    spec, summaries = result.get('chart_spec'), result.get('series_summary')
    selected = result.get('selected_series')
    if (
        not isinstance(spec, dict)
        or spec.get('kind') != 'trend'
        or not isinstance(summaries, list)
        or len(summaries) != 1
        or not isinstance(selected, list)
        or len(selected) != 1
        or len(spec.get('series') or []) != 1
    ):
        return None
    row, line = summaries[0], spec['series'][0]
    if not isinstance(row, dict) or not isinstance(line, dict):
        return None
    if (
        str(row.get('series_name') or '').casefold() != str(line.get('name') or '').casefold()
        or str(row.get('series_no')) != str(selected[0].get('series_no'))
        or str(row.get('unit') or '').strip().casefold()
        != str(spec.get('unit') or '').strip().casefold()
    ):
        return None
    axis, values = spec.get('categories'), line.get('values')
    if (
        not isinstance(axis, list)
        or not isinstance(values, list)
        or not 2 <= len(axis) == len(values) <= 500
        or len(set(map(str, axis))) != len(axis)
    ):
        return None
    return (spec, row, axis, values) if _all_values_published(result, values) else None


def _all_values_published(result, values) -> bool:
    """True when EVERY plotted point is a finite number and, when the result lists the
    numbers it may quote (numeric_claims), one of them (not just the endpoints)."""
    if any(type(value) not in (float, int) or not math.isfinite(value) for value in values):
        return False
    approved = result.get('numeric_claims')
    if not (isinstance(approved, list) and approved):
        return True
    try:
        allowed = {
            round(float(value), 6)
            for value in approved
            if type(value) in (int, float) and math.isfinite(value)
        }
    except (TypeError, ValueError, OverflowError):
        return False
    return all(round(float(value), 6) in allowed for value in values)


def _matching_endpoints(row, axis, values):
    """(start, end, first value, latest value) when the plot's endpoints equal the summary's."""
    start, end = str(row.get('earliest_period') or ''), str(row.get('latest_period') or '')
    if not start or not end or start == end or start not in axis or end not in axis:
        return None
    try:
        before, after = float(row['earliest_value']), float(row['latest_value'])
        change = float(row['change_latest_minus_earliest'])
        plotted_before = float(values[axis.index(start)])
        plotted_after = float(values[axis.index(end)])
    except (KeyError, ValueError, TypeError):
        return None
    if (
        not all(math.isfinite(x) for x in (before, after, change, plotted_before, plotted_after))
        or not math.isclose(after - before, change, rel_tol=1e-9, abs_tol=1e-5)
        or not math.isclose(before, plotted_before, rel_tol=1e-12, abs_tol=1e-6)
        or not math.isclose(after, plotted_after, rel_tol=1e-12, abs_tol=1e-6)
    ):
        return None
    return start, end, before, after


def _answer_names_table_and_subject(question, result, spec, full_answer) -> bool:
    """The answer names the table ID, and question, title and answer share its subject.

    A matching table ID alone proves nothing about the measure: at least two
    subject words must be shared, or one distinctive single-word measure
    (e.g. "ridership"); ordinary words like "population" are not enough.
    """
    table_id = str(result.get('table_id') or '')
    if not table_id or not re.search(
        rf'(?<![A-Z0-9]){re.escape(table_id)}(?![A-Z0-9])', full_answer, re.I
    ):
        return False
    title = str(result.get('title') or result.get('table_title') or spec.get('title') or '')
    overlap = (
        (_chart_topic_tokens(question) - BROAD_POPULATION_WORDS)
        & _chart_topic_tokens(title)
        & _chart_topic_tokens(full_answer[:1400])
    )
    return len(overlap) >= 2 or (len(overlap) == 1 and overlap <= DISTINCTIVE_SINGLE_MEASURES)


def _answer_states_endpoints(full_answer, start, end, before, after) -> bool:
    """The answer states both endpoint values and both periods."""
    tokens = set()
    for token in numeric_tokens(full_answer):
        try:
            tokens.add(float(token))
        except ValueError:
            continue
    if start.isdigit() and end.isdigit():  # years: also numbers in the text
        return {float(start), float(end), before, after} <= tokens
    return before in tokens and after in tokens and start in full_answer and end in full_answer


_AGGREGATE_KEYS = frozenset({'total', 'overall', 'all'})


def verified_same_year_categories_from_answer(response, question, full_answer):
    """A bar chart of same-year categories that the answer's own table states, or None.

    For "latest breakdown / which category" questions. The answer must contain
    a Markdown table whose category rows and values EACH match a typed series
    summary (same name, latest value, period, unit and series ID) from one
    complete official table, so every bar is a checked published number.
    For ranking questions the result is marked as a SUBSET view: it does not
    prove these are the top N of every category.

    Steps: the question must ask for a latest-period breakdown; for each
    official source the answer names, build a lookup of categories; then
    find an answer table that matches 3-20 of them with no hierarchy overlap.
    """
    if (
        not isinstance(response, dict)
        or response.get('suppress_charts')
        or not isinstance(full_answer, str)
        or not _asks_for_latest_categories(question)
    ):
        return None
    q = str(question or '').casefold()
    for (table_id, url), items in _complete_results_by_source(response).items():
        if not table_id or str(table_id).casefold() not in full_answer.casefold():
            continue
        title = next(
            (
                str(i.get('title') or i.get('table_title') or '')
                for i in items
                if i.get('title') or i.get('table_title')
            ),
            '',
        )
        if not (
            _chart_topic_tokens(question) - BROAD_POPULATION_WORDS & _chart_topic_tokens(title)
        ):
            continue
        lookup, ambiguous = _latest_category_lookup(items)
        if len(lookup) < 3:
            continue
        for header, rows in _verified_answer_table_rows(full_answer):
            found = _categories_in_answer_table(header, rows, lookup, ambiguous)
            if found is None:
                continue
            matched, rank = found
            if not _checked_category_set(matched, rank, full_answer):
                continue
            year = matched[0]['period']
            spec = {
                'kind': 'bar',
                'title': f'Checked published categories · {year}',
                'unit': matched[0]['unit'],
                'categories': [m['name'] for m in matched],
                'series': [{'name': 'Published value', 'values': [m['value'] for m in matched]}],
            }
            if not chart_covers_requested_groups(question, spec):
                continue
            ranking = bool(rank or re.search(r'\b(?:most|largest|highest|top|leading)\b', q))
            return {
                'success': True,
                'table_id': table_id,
                'source_url': url,
                'table_complete': True,
                'analysis_ready': True,
                'chart_kind': 'verified_answer_bound_categories',
                'chart_integrity': 'checked_same_year_row_value_id_and_unit',
                'chart_priority': 128,
                'chart_spec': spec,
                'ranking_scope_limited': ranking,
                'checked_category_rows': [
                    {'name': m['name'], 'value': m['value']} for m in matched
                ],
                'checked_period': year,
            }
    return None


def _asks_for_latest_categories(question) -> bool:
    """True for a latest-period breakdown question (not a trend, not a share)."""
    q = str(question or '').casefold()
    return bool(
        re.search(
            r'\b(?:latest|which|across|compare|distributed|distribution|breakdown|'
            r'by age|by industry|by occupation)\b',
            q,
        )
        and not re.search(
            r'\b(?:over time|trend|growth|changed|past decade|across years|'
            r'through the years)\b',
            q,
        )
        and not re.search(r'\b(?:percentage|percent|proportion|share)\b', q)
    )


def _complete_results_by_source(response):
    """{(table_id, source_url): [complete official results with series summaries]}."""
    groups = {}
    for item in response.get('tool_results') or []:
        if (
            not isinstance(item, dict)
            or item.get('success') is not True
            or item.get('table_complete') is not True
            or item.get('analysis_ready') is False
            or not is_official_source(item.get('source_url'))
            or not isinstance(item.get('series_summary'), list)
        ):
            continue
        groups.setdefault((item.get('table_id'), item.get('source_url')), []).append(item)
    return groups


def _latest_category_lookup(items) -> tuple:
    """(lookup, ambiguous): latest value per category name, and names that clash.

    A name that appears twice with a different series ID, period, unit or
    value (the same label in two parts of a hierarchy) is ambiguous and can
    never become a bar. Aggregates (Total, All ...) are skipped.
    """
    lookup, ambiguous = {}, set()
    for item in items:
        for entry in item.get('series_summary') or []:
            if not isinstance(entry, dict):
                continue
            key = _canonical_chart_category(entry.get('series_name'))
            value = entry.get('latest_value')
            period = str(entry.get('latest_period') or '')
            unit = str(entry.get('unit') or '').strip()
            sid = str(entry.get('series_no') or '')
            if (
                not key
                or key in _AGGREGATE_KEYS | {'value'}
                or type(value) not in (int, float)
                or not math.isfinite(value)
                or not period
                or not sid
                or not unit
            ):
                continue
            if key in lookup:
                other = lookup[key]
                if (
                    other['sid'] != sid
                    or other['period'] != period
                    or other['unit'] != unit
                    or not math.isclose(other['value'], float(value), rel_tol=0, abs_tol=1e-9)
                ):
                    ambiguous.add(key)
            lookup[key] = {
                'name': str(entry['series_name']),
                'period': period,
                'unit': unit,
                'value': float(value),
                'sid': sid,
            }
    return lookup, ambiguous


def _categories_in_answer_table(header, rows, lookup, ambiguous):
    """(matched entries, is_ranked) when an answer table's rows all match the lookup.

    The table needs a category heading (sector, industry, age ...) and a value
    column that is not a share/rate/change. A trailing "Total" row is kept
    only as a cross-check (it must equal the sum); any other total, repeat or
    unmatched value rejects the table (None).
    """
    rank = header[0].strip().casefold() == 'rank'
    cat_col, value_col = (1, 2) if rank else (0, 1)
    if len(header) <= value_col or not rows or len(rows) > 25:
        return None
    if not re.search(CATEGORY_HEADING_PATTERN, header[cat_col].casefold()) or re.search(
        r'\b(?:share|percent|rate|change|index)\b', header[value_col].casefold()
    ):
        return None
    if rank and [r[0] for r in rows] != [str(i) for i in range(1, len(rows) + 1)]:
        return None
    matched, seen, total_footer = [], set(), None
    for row_index, row in enumerate(rows):
        key = _canonical_chart_category(row[cat_col])
        if key in _AGGREGATE_KEYS and row_index == len(rows) - 1 and len(rows) >= 4 and not rank:
            total_footer = _exact_markdown_number(row[value_col])
            if total_footer is None or not math.isfinite(total_footer):
                return None
            break
        if key in _AGGREGATE_KEYS or key in seen or key in ambiguous:
            return None
        entry = lookup.get(key)
        numeric = _exact_markdown_number(row[value_col])
        if (
            entry is None
            or numeric is None
            or not math.isclose(numeric, entry['value'], rel_tol=0, abs_tol=1e-6)
        ):
            return None
        seen.add(key)
        matched.append(entry)
    if not 3 <= len(matched) <= 20:
        return None
    if total_footer is not None and not math.isclose(
        sum(item['value'] for item in matched), total_footer, rel_tol=0, abs_tol=1e-6
    ):
        return None
    return matched, rank


def _checked_category_set(matched, rank, full_answer) -> bool:
    """One period and unit, period stated in the answer, rank order true, no parent+child."""
    if (
        len({r['period'] for r in matched}) != 1
        or len({r['unit'].casefold() for r in matched}) != 1
    ):
        return False
    # Never imply the period is the latest unless the answer says which period it is.
    if matched[0]['period'] not in full_answer:
        return False
    if rank and any(matched[i]['value'] < matched[i + 1]['value'] for i in range(len(matched) - 1)):
        return False
    ids = [r['sid'] for r in matched]
    if len(set(ids)) != len(ids):
        return False
    return not any(a + '.' in b or b + '.' in a for i, a in enumerate(ids) for b in ids[i + 1 :])


def limited_category_ranking_text(chart):
    """Replace unsupported broad 'top N' prose with an honest subset summary."""
    if (
        not isinstance(chart, dict)
        or chart.get('chart_kind') != 'verified_answer_bound_categories'
        or not chart.get('ranking_scope_limited')
        or not chart.get('checked_category_rows')
    ):
        return None
    table = str(chart.get('table_id') or '')
    period = str(chart.get('checked_period') or '')
    unit = str((chart.get('chart_spec') or {}).get('unit') or '')
    url = chart.get('source_url')
    if not is_official_source(url) or not re.fullmatch(r'[A-Z]\d{5,8}', table):
        return None
    rows = sorted(chart['checked_category_rows'], key=lambda r: r['value'], reverse=True)
    out = [
        '## Checked published categories',
        '',
        f'The following {period} observations were checked against the complete official table.',
        'These are the categories returned for this answer; **coverage of every category in the '
        'source hierarchy has not been established**. '
        'Their ordering must not be interpreted as a verified ranking of every possible category.',
        '',
        '| Published category | Published value |',
        '|---|---:|',
    ]
    for r in rows:
        name = str(r['name']).replace('|', '/').replace('\n', ' ')[:110]
        value = r['value']
        if not name or type(value) not in (int, float) or not math.isfinite(value):
            return None
        rendered = (
            f'{int(value):,}'
            if float(value).is_integer()
            else f'{value:,.6f}'.rstrip('0').rstrip('.')
        )
        out.append(f'| {name} | {rendered} |')
    out.extend(['', f'**Unit:** {unit}. **Official source:** [SingStat {table}]({url})'])
    return '\n'.join(out)


def relevant_chart_evidence(response, question, full_answer) -> dict:
    """Keep only verified plots tied to the current question and answer.

    The same rules apply to every table. A tool's chart is kept when it
    matches the question (``chart_matches_question``); otherwise the general
    answer-bound checks may recover a single verified series or a scoped
    preview. Anything else is dropped (fail closed).
    """
    chosen = _verified_latest_category_candidates(response, question, full_answer)
    same_year = verified_same_year_categories_from_answer(response, question, full_answer)
    if same_year is not None:
        chosen.append(same_year)
    for result in response.get('tool_results', []):
        if not isinstance(result, dict):
            continue
        if chart_matches_question(question, result, full_answer):
            chosen.append(result)
            continue
        recovery = verified_single_series_trend_candidate(question, result, full_answer)
        if recovery is None and is_generic_table_preview(result):
            # Let the exact selected series and official numeric evidence
            # resolve broad natural-language concepts. This does NOT
            # bypass chart completeness, relevance or preflight checks.
            recovery, scope_reason = reconcile_scoped_preview(question, result, full_answer)
            if recovery is None and result.get('chart_spec'):
                LOGGER.info('Verified scope chart not recovered: %s', scope_reason)
        if recovery is not None:
            chosen.append(recovery)
        elif result.get('chart_spec'):
            # Expected safety behaviour, not a fault: logged for diagnosis only.
            LOGGER.info(
                'Suppressed non-matching chart: table=%s kind=%s '
                'generic_preview=%s plotted_series=%r',
                result.get('table_id'),
                result.get('chart_kind'),
                is_generic_table_preview(result),
                [
                    line.get('name')
                    for line in (result.get('chart_spec') or {}).get('series', [])
                    if isinstance(line, dict)
                ][:12],
            )
    return {**response, 'tool_results': chosen}


def _canonical_chart_category(value) -> str:
    """Normalize punctuation in labels, NEVER remove population-defining words."""
    normalized = str(value or '').casefold().replace('–', '-').replace('—', '-')
    normalized = normalized.replace('&', ' and ')
    normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
    return ' '.join(normalized.split())


def _exact_markdown_number(value):
    """Parse one printed observation; never evaluate arithmetic/prose/footnotes."""
    cleaned = str(value or '').replace('**', '').replace(',', '').strip()
    cleaned = cleaned.replace('−', '-').replace('–', '-')
    cleaned = re.sub(r'\s*(?:%|\([a-zA-Z ]+\))\s*$', '', cleaned).strip()
    if not re.fullmatch(r'[+\-]?(?:\d+(?:\.\d*)?|\.\d+)', cleaned):
        return None
    number = float(cleaned)
    return number if math.isfinite(number) else None


def align_chart_description_to_delivery(answer, chart_present):
    """Do not claim to have displayed a graph that failed final preflight."""
    if not isinstance(answer, str):
        return answer
    if chart_present:
        # A model draft may assert 'no chart' before the independently checked
        # chart is actually selected and rendered. Remove only UI-status prose.
        answer = re.sub(
            r'(?im)^.*\bno (?:full[- ]distribution |combined |verified )?'
            r'(?:chart|graph|plot) (?:was|has been) generated\.?\s*$',
            '',
            answer,
        )
        return re.sub(r'\n{3,}', '\n\n', answer).strip()
    sentences = [
        # A generated-but-rejected draft can mention combined multi-series charts
        # with trailing clauses. Remove chart status prose, NOT numeric results.
        r'\bA\s+(?:verified\s+)?combined\s+(?:multi[- ]series\s+)?trend\s+chart\s+'
        r'(?:was|has been)\s+generated[^.\n]*\.\s*',
        # "A combined time-series chart was generated ..." must not remain
        # when the UI has withheld the chart after verification/preflight.
        r'\bA\s+(?:verified\s+)?combined\s+(?:multi[- ]series\s+)?'
        r'(?:time[- ]series|line)\s+chart\s+(?:was|has been)\s+'
        r'generated[^.\n]*\.\s*',
        # Includes “A verified trend chart comparing all five groups was generated”.
        # Covers e.g. "A verified multi-series trend chart was generated ..."
        # and "A verified trend chart comparing all five groups was generated".
        r'\bA\s+verified\s+(?:multi[- ]series\s+)?trend\s+chart\b'
        r'[^.\n]*?was\s+generated[^.\n]*\.\s*',
        r'(?:An?|The)\s+(?:verified\s+|combined\s+|accompanying\s+|full\s+|annual\s+|trend\s+)*chart\s+(?:has been|was)\s+generated(?:\s+from[^.\n]+)?\.',
        r'The\s+(?:accompanying|verified)\s+chart\s+(?:shows|compares)[^.\n]+\.',
        r'A\s+(?:verified\s+)?trend\s+chart\s+(?:has been|was)\s+generated[^.\n]*\.',
    ]
    for pattern in sentences:
        answer = re.sub(pattern, '', answer, flags=re.I)
    return re.sub(r'\n{3,}', '\n\n', answer).strip()


def choose_chart_result(response):
    """Pick the single best chart candidate from the verified tool results.

    Args:
        response: response dict whose ``tool_results`` were already filtered by
            :func:`relevant_chart_evidence`.

    Returns:
        The chosen tool-result dict, or None when charts are suppressed or no
        successful result carries a chart (``chart_spec`` or an image file).

    Ranking, highest first: (1) integrity-checked, question-specific charts
    (``verified_*`` / ``validated_*`` kinds) beat generic table previews,
    whatever priority the preview declares; (2) ``chart_priority``, raised to a
    floor for known chart kinds; (3) on a tie, the earliest tool result wins.
    """
    if not isinstance(response, dict) or response.get('suppress_charts') is True:
        return None
    candidates = []
    for idx, result in enumerate(response.get('tool_results', [])):
        if not isinstance(result, dict) or result.get('success') is not True:
            continue
        if not isinstance(result.get('chart_spec'), dict):
            continue
        try:
            priority = int(result.get('chart_priority', 0))
        except (TypeError, ValueError):
            priority = 0
        # Evidence tier first: an integrity-checked chart (built from numbers
        # already verified in the answer) beats a generic table preview, even if
        # the preview declares an arbitrarily high chart_priority.
        is_specific = bool(
            result.get('chart_integrity')
            or str(result.get('chart_kind') or '').startswith(('verified_', 'validated_'))
        )
        if result.get('chart_kind') == 'time_series':
            priority = max(priority, 45)
        elif result.get('chart_kind') == 'category_comparison':
            priority = max(priority, 12)
        candidates.append((int(is_specific), priority, -idx, result))
    return max(candidates, key=lambda item: (item[0], item[1], item[2]))[3] if candidates else None


def resolve_chart_spec(response, selected):
    """Choose which view of the selected chart to draw.

    A tool may offer several verified views in ``chart_variants`` (e.g.
    ``composition`` or ``share``). The view is chosen from the
    wording of ``response["request_summary"]``; otherwise the default
    ``chart_spec`` is used.

    Returns:
        A chart-spec dict, or None if ``selected`` is not a usable candidate.
    """
    if not isinstance(selected, dict):
        return None
    choices = selected.get('chart_variants') or {}
    topic = str(response.get('request_summary') or '').casefold()
    if isinstance(choices, dict):
        if any(x in topic for x in ('composition', 'breakdown', 'age structure')) and isinstance(
            choices.get('composition'), dict
        ):
            return choices['composition']
        if any(x in topic for x in ('share', 'percentage', 'percent', 'proportion')) and isinstance(
            choices.get('share'), dict
        ):
            return choices['share']
    return selected.get('chart_spec') if isinstance(selected.get('chart_spec'), dict) else None
