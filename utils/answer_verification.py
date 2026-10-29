"""Answer verification: decide which AI-written numbers may be shown.

WHERE THIS FITS
===============
``app.py`` -> ``process_question`` receives a response dict from
``utils/core_secure.ask_sage``. Before any text reaches the screen, the
functions here answer three questions:

1. Which tool results count as *official evidence*?
   (``official_evidence`` / ``is_official_source`` / ``displayed_answer``)
2. Is every number in the AI's prose backed by that evidence?
   (``numeric_tokens`` -> ``full_answer_numbers_supported``)
3. If not, can a smaller, fully checked answer be salvaged?
   (``remove_unverified_optional_sentence``, ``evidence_bound_generic_answer``,
   ``retry_verified_series_for_numeric_failure``)

GOLDEN RULE: when in doubt, return ``False`` / ``None`` ("fail closed"). The
caller then shows a fixed "couldn't verify" message instead of the AI text.

Every function in this module is PURE with respect to the UI: no Streamlit,
no session state. That is deliberate, so the checks can be unit-tested and
reused by the command-line interface or another front end.

Glossary
--------
* tool result / row: one ``dict`` produced by a Sage analytical tool.
  ``row["success"] is True`` means the tool fetched and checked official data.
* evidence: the list returned by :func:`official_evidence`.
* token: a number as it appears in text, normalised (``"1,234.5%"`` -> ``"1234.5"``).

Moved out of ``app.py`` unchanged (behaviour-preserving refactor, Oct 2026).

Used by: ui/answer_pipeline.py, ui/answer_view.py,
utils/chart_selection.py, utils/plain_summary.py,
tools/characterise_answer_checks.py, tools/characterise_numeric_retry.py,
tools/characterise_sentence_salvage.py, tools/characterise_series_brief.py.
Tested in: tests/test_answer_checks.py, tests/test_biggest_first.py,
tests/test_longer_history.py, tests/test_real_answers.py,
tests/test_summaries_and_notes.py.
"""

import json
import logging
import math
import re
from urllib.parse import urlparse

from utils.chart_intent import chart_covers_requested_groups
from utils.vocabulary import (
    EVIDENCE_TOPIC_EQUIVALENTS,
    NARROW_SUBJECT_WORDS,
    REQUIRED_SUBGROUP_PATTERNS,
)
from utils.evidence_ledger import reconciled_summary_tokens, bibliographic_number_stripped_answer

# Same logger name as app.py so existing log filters keep working.
LOGGER = logging.getLogger("sage_ui")

# The ONLY hosts that may appear as clickable links or evidence citations.
# Exact names, not "*.gov.sg": any compromised or open-redirect page on another
# gov.sg subdomain must not become a clickable link in an AI answer.
OFFICIAL_LINK_HOSTS = frozenset(
    {
        "tablebuilder.singstat.gov.sg",
        "www.singstat.gov.sg",
        "singstat.gov.sg",
        "data.gov.sg",
        "www.data.gov.sg",
    }
)


def official_evidence(response):
    """Collect only successful complete results with a verifiable official source.

    Source discovery and incomplete staging alone cannot justify a numerical
    claim. Only explicitly successful, non-partial analytical results qualify.
    """
    if (
        not isinstance(response, dict)
        or response.get("security_blocked")
        or response.get("scope_blocked")
    ):
        return []
    verified = []
    for item in response.get("tool_results", []):
        if not isinstance(item, dict) or item.get("success") is not True:
            continue
        if item.get("table_complete") is False or item.get("analysis_ready") is False:
            continue
        ids = ([item["table_id"]] if item.get("table_id") else []) + (
            item.get("table_ids", []) if isinstance(item.get("table_ids"), list) else []
        )
        urls = ([item["source_url"]] if item.get("source_url") else []) + (
            item.get("source_urls", []) if isinstance(item.get("source_urls"), list) else []
        )
        urls = [u for u in urls if isinstance(u, str) and is_official_source(u)]
        if not ids or not urls:
            continue
        verified.append(item)
    return verified


def is_official_source(url) -> bool:
    """Only allow known official data-source URLs into evidence citations."""
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
        and parsed.hostname in OFFICIAL_LINK_HOSTS
    )


def displayed_answer(response) -> tuple:
    """Fail closed for requests with no independently sourced table evidence."""
    if response.get("security_blocked") or response.get("scope_blocked"):
        return (
            str(
                response.get("answer") or "Please rephrase this as a Singapore statistics question."
            ),
            False,
        )
    if response.get('table_catalogue_listing') is True and response.get('metadata_only') is True:
        return str(response.get('answer') or ''), True
    if response.get('selected_source_unavailable') is True:
        return str(response.get('answer') or 'The chosen source is unavailable.'), False
    if not official_evidence(response):
        return (
            "I couldn't verify enough complete official table data to answer "
            "this question reliably. Please rephrase it more specifically, "
            "choose another sample, or include a SingStat table ID. "
            "I won't guess or draw an unverified graph.",
            False,
        )
    return str(
        response.get("answer") or "Verified data were found, but no written answer was returned."
    ), True


def numeric_tokens(text) -> set:
    """Extract visible numbers while ignoring URLs and official table identifiers."""
    without_links = re.sub(r"https?://[^\s)>]+", " ", str(text), flags=re.I)
    without_ids = re.sub(r"\b[A-Z]\d{5,8}\b", " ", without_links)
    # Markdown numbered *section labels* aren't statistical observations.
    # Strip only line-start list markers followed by whitespace; numerical
    # claims within list items remain subject to the strict official gate.
    without_ids = re.sub(r"(?m)^\s{0,3}\d{1,3}[.)]\s+", "", without_ids)
    return {
        token.replace(",", "").rstrip("%").lstrip("+-")
        for token in re.findall(r"(?<![A-Za-z0-9])[-+]?\d[\d,]*(?:\.\d+)?%?", without_ids)
    }


def verified_rounded_percentage_tokens(evidence):
    """Return *only* one/two-decimal renderings of structured tool percentages.

    The engine can verify ``percentage_change=-17.74`` and intentionally print
    its magnitude as ``17.7%``. Exact-text comparison must not reject that
    legitimate formatting, but unrelated decimals must NEVER receive an
    arbitrary tolerance. Only known, typed percentage fields in successful,
    official, complete tool results can supply these extra renderings.
    """
    percentage_keys = {
        "percentage_change",
        "percentage",
        "percent_change",
        "change_percent",
        "pct_change",
        "change_percentage_points",
        "percentage_points",
        "start_rate",
        "end_rate",
    }
    permitted = set()

    def visit(node) -> None:
        """Collect permitted renderings of typed percentage fields, walking nested results."""
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in percentage_keys
                    and type(value) in (float, int)
                    and not isinstance(value, bool)
                ):
                    number = float(value)
                    # Unbounded, NaN and infinite percentages aren't facts to
                    # validate. Absolute magnitude matches the deterministic
                    # core's explicit "declined by abs(change)" presentation.
                    if -1000000.0 <= number <= 1000000.0:
                        for digits in (1, 2):
                            permitted.add(f"{abs(number):.{digits}f}")
                elif isinstance(value, (dict, list)):
                    visit(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    visit(item)

    for result in evidence:
        if (
            isinstance(result, dict)
            and result.get("success") is True
            and result.get("table_complete") is not False
            and result.get("analysis_ready") is not False
        ):
            visit(result)
    return permitted


def verified_generic_endpoint_insight(entry, *, brief: bool = False):
    """Describe only reconciled endpoint values in a checked series summary.

    This is presentation, not historical interpolation or causal inference.
    The values and change must be typed, finite and arithmetically consistent.
    """
    if not isinstance(entry, dict):
        return None
    required = (
        'series_name',
        'unit',
        'earliest_period',
        'latest_period',
        'earliest_value',
        'latest_value',
        'change_latest_minus_earliest',
    )
    if any(key not in entry for key in required):
        return None
    first, last, delta = (
        entry['earliest_value'],
        entry['latest_value'],
        entry['change_latest_minus_earliest'],
    )
    if any(
        type(v) not in (int, float) or not math.isfinite(v) for v in (first, last, delta)
    ) or not math.isclose(last - first, delta, rel_tol=1e-8, abs_tol=1e-4):
        return None
    first_year = str(entry['earliest_period'])
    last_year = str(entry['latest_period'])
    if not all(re.fullmatch(r'[A-Za-z0-9./ -]{1,20}', year) for year in (first_year, last_year)):
        return None
    series = re.sub(r'[^\w /&()+.,\-]', '', str(entry['series_name'])).strip()
    if not series or len(series) > 85:
        return None
    # The official series label is used as published (same rule for every series).
    subject = series[0].upper() + series[1:]
    unit = str(entry['unit']).strip().casefold()
    percent = unit in {'%', 'percent', 'percentage', 'per cent'}

    # Use short, deterministic decimal strings. No relative percentage change,
    # interpolation or rounding beyond the tool's reported endpoint precision.
    formatted = lambda v: format(float(v), '.12g')
    start_val, end_val = formatted(first), formatted(last)
    if percent:
        start_val += '%'
        end_val += '%'
    if first_year == last_year:
        return f'{subject} stood at {end_val} in {last_year}.'
    verb = 'rose' if delta > 0 else 'fell' if delta < 0 else 'remained unchanged'
    if not delta:
        return f'{subject} remained at {start_val} between ' f'{first_year} and {last_year}.'
    if percent:
        if brief:
            return (
                f'{subject} {verb} by {formatted(abs(delta))} percentage '
                f'points between {first_year} and {last_year}.'
            )
        return (
            f'{subject} {verb} from {start_val} in {first_year} to '
            f'{end_val} in {last_year} '
            f'({formatted(abs(delta))} percentage points).'
        )
    # General selected-series wording: do not assert an annual trend from
    # just first/latest observations; these are endpoint differences only.
    return f'{subject} {verb} from {start_val} in {first_year} to ' f'{end_val} in {last_year}.'


_RETRY_STOPWORDS = frozenset(
    {
        'how',
        'have',
        'been',
        'published',
        'percentages',
        'percentage',
        'percent',
        'selected',
        'changed',
        'across',
        'years',
        'over',
        'time',
        'singapore',
        'singstat',
        'official',
        'data',
        'annual',
    }
)


def retry_verified_series_for_numeric_failure(question, response, core):
    """One bounded, fail-closed retry for a matching official *time-series* table.

    Some specialised tools return a matching table and series labels without the
    typed endpoint ledger required by the answer validator. An official generic
    query of the SAME table can sometimes supply the missing verified records.
    Never convert mere table discovery into numeric evidence, and never silently
    select an arbitrary subset of a large catalogue.

    Steps: find the one table worth retrying (_retry_table); query it once;
    accept the result only if it passes every check (_acceptable_retry).
    Returns the response, with the checked result appended when accepted.
    """
    table_id = _retry_table(question, response)
    query = getattr(core, 'query_singstat_table', None)
    if table_id is None or not callable(query):
        return response
    try:
        checked = query(table_id, max_series=8, make_chart=True)
    except (ValueError, TypeError, RuntimeError, OSError, KeyError) as exc:
        LOGGER.info(
            'Typed official series retry unavailable for %s: %s', table_id, type(exc).__name__
        )
        return response
    if not _acceptable_retry(checked, table_id):
        return response
    # Promote only the newly retrieved, independently checked tool payload.
    LOGGER.info('Recovered typed verified series from %s for numeric validation', table_id)
    return {**response, 'tool_results': list(response.get('tool_results') or []) + [checked]}


def _retry_table(question, response):
    """The one complete official time-series table worth re-querying, or None.

    Only for change-over-time questions, never for blocked or catalogue
    responses, only when all evidence comes from one table that lacks typed
    series summaries, and only when the table's title shares at least two
    subject words with the question (an unrelated table is never promoted).
    """
    if (
        not isinstance(question, str)
        or not isinstance(response, dict)
        or response.get('table_catalogue_listing')
        or response.get('security_blocked')
        or response.get('scope_blocked')
        or not re.search(r'(?i)\b(change|changed|trend|over time|across years|since)\b', question)
    ):
        return None
    evidence = official_evidence(response)
    candidates = [
        item
        for item in evidence
        if isinstance(item.get('table_id'), str)
        and re.fullmatch(r'[A-Z]\d{6}', item['table_id'])
        and not item.get('series_summary')
        and item.get('table_complete') is True
    ]
    if not candidates or len({item['table_id'] for item in evidence}) != 1:
        return None
    title = ' '.join(
        str(item.get('table_title') or item.get('title') or '') for item in candidates
    ).casefold()
    keywords = {
        x for x in re.findall(r'[a-z]{5,}', question.casefold()) if x not in _RETRY_STOPWORDS
    }
    if len(keywords & set(re.findall(r'[a-z]{5,}', title))) < 2:
        return None
    return candidates[0]['table_id']


def _acceptable_retry(checked, table_id) -> bool:
    """True when the re-query is a complete, official, untruncated result for this table
    whose chart contains exactly its verified series (no title-only placeholders)."""
    if (
        not isinstance(checked, dict)
        or checked.get('success') is not True
        or checked.get('table_id') != table_id
        or checked.get('table_complete') is not True
        or checked.get('analysis_ready') is False
        or not is_official_source(checked.get('source_url'))
        or not isinstance(checked.get('series_summary'), list)
        or not checked['series_summary']
        or checked.get('series_selection_truncated') is True
        or int(checked.get('series_count_available', 0) or 0) > 8
        or checked.get('chart_source_verified') is not True
    ):
        return False
    spec = checked.get('chart_spec') or {}
    spec_names = {
        str(x.get('name', '')).casefold().strip()
        for x in spec.get('series', [])
        if isinstance(x, dict)
    }
    summary_names = {
        str(x.get('series_name', '')).casefold().strip()
        for x in checked['series_summary']
        if isinstance(x, dict)
    }
    return bool(summary_names) and summary_names.issubset(spec_names)


# --- Rebuilding an answer from checked series endpoints -----------------------

#: Questions a set of series ENDPOINTS cannot answer (rankings, shares, breakdowns).
_BREAKDOWN_QUESTION = re.compile(
    r"(?i)\b(biggest|largest difference|highest|lowest|ranking|rank|most|leading|top|"
    r"share|proportion|distributed|distribution|breakdown|"
    r"how many.*(?:each|every)|across.*(?:categories|groups|sectors|industries)|"
    r"which.*(?:categories|groups|sectors|industries))\b"
)
#: Words too general to show that evidence is about the question's subject.
_GENERIC_QUESTION_WORDS = frozenset(
    {
        'what',
        'when',
        'which',
        'where',
        'singapore',
        'official',
        'table',
        'latest',
        'available',
        'since',
        'over',
        'time',
        'year',
        'years',
        'with',
        'from',
        'between',
        'different',
        'data',
        'statistics',
        'show',
        'chart',
        'could',
        'about',
        'change',
        'changed',
        'figures',
        'historical',
    }
)
_PERCENT_UNITS = {'%', 'percent', 'percentage', 'per cent'}
_SAFE_PERIOD = re.compile(r'[A-Za-z0-9./ -]{1,20}')
_MAX_ENDPOINT_ROWS = 5


def evidence_bound_generic_answer(question, evidence):
    """Rebuild an answer ONLY from checked series endpoints, or return None.

    Used when the AI's text quoted numbers that could not be verified. The
    replacement is a fixed table of each series' first and latest published
    value and the change between them, all copied from ONE complete official
    table, followed by one plain sentence per series. It never infers shares,
    rankings, residual categories or causes, so questions asking for those
    get None (Sage then says it could not verify an answer).

    Steps: the question must suit endpoints; the evidence must come from one
    complete official table about the question's subject; every endpoint
    must be finite and internally consistent; the finished text must pass the
    same number check as any AI answer (full_answer_numbers_supported).
    """
    if not isinstance(question, str) or not isinstance(evidence, list):
        return None
    percent_trend = bool(
        re.search(r'(?i)\b(percentages?|per cent)\b', question)
        and re.search(r'(?i)\b(changed|change|trend|over time|since)\b', question)
    )
    if _BREAKDOWN_QUESTION.search(question):
        return None
    qualifying = _complete_summary_results(evidence)
    if not qualifying or len({item.get('table_id') for item in qualifying}) != 1:
        return None
    result = qualifying[0]
    if percent_trend and not all(
        str(x.get('unit', '')).strip().casefold() in _PERCENT_UNITS
        for x in result['series_summary']
        if isinstance(x, dict)
    ):
        return None
    summaries = _merged_series_summaries(qualifying)
    if not summaries or not _evidence_is_about_question(question, result, summaries):
        return None
    rows = _endpoint_rows(summaries)
    if rows is None:
        return None
    candidate = _endpoint_answer_text(result, summaries, rows, percent_trend)
    if candidate is None:
        return None
    # The same hard gate as every AI answer: any number not in the evidence fails.
    return candidate if full_answer_numbers_supported(candidate, evidence, question) else None


def _complete_summary_results(evidence) -> list:
    """Successful, complete, analysis-ready official results that carry series summaries."""
    return [
        item
        for item in evidence
        if isinstance(item, dict)
        and item.get('success') is True
        and item.get('table_complete') is not False
        and item.get('analysis_ready') is not False
        and item.get('series_summary')
        and isinstance(item.get('series_summary'), list)
        and is_official_source(item.get('source_url'))
    ]


def _merged_series_summaries(qualifying):
    """Series summaries from every result of the (single) table, each series once.

    Merging matters: one query for Males and one for Females must not be
    answered with Males alone. At most _MAX_ENDPOINT_ROWS series are kept;
    a malformed entry rejects the whole set (None).
    """
    summaries, seen_series = [], set()
    for item in qualifying:
        for entry in item.get('series_summary') or []:
            if not isinstance(entry, dict):
                return None
            key = str(entry.get('series_no') or entry.get('series_name') or '')
            if key in seen_series:
                continue
            seen_series.add(key)
            summaries.append(entry)
    return summaries[:_MAX_ENDPOINT_ROWS]


def _topic_words(value) -> set:
    """Subject words of ``value`` (4+ letters, synonyms merged, generic words dropped)."""
    return {
        EVIDENCE_TOPIC_EQUIVALENTS.get(token, token)
        for token in re.findall(r'[a-z]{4,}', str(value).lower())
        if token not in _GENERIC_QUESTION_WORDS
    }


def _evidence_is_about_question(question, result, summaries) -> bool:
    """True when the table/series share a subject word with the question.

    A complete but unrelated table found during the search must not answer;
    and a narrow subgroup the question names (sex, sector, ...) must be named
    by the series themselves, not just by a broad table title.
    """
    requested = _topic_words(question)
    series_names = ' '.join(str(x.get('series_name') or '') for x in summaries)
    if not requested & _topic_words(str(result.get('title') or '') + ' ' + series_names):
        return False
    specific = requested & NARROW_SUBJECT_WORDS
    return not specific or bool(specific & _topic_words(series_names))


def _endpoint_rows(summaries):
    """(name, first period, latest period, first, latest, change) per series, or None.

    Rejects missing fields, non-finite values, a change that does not equal
    latest minus first, or mixed units. Values are written as Python floats
    (str(float)), so no rounded number is ever invented.
    """
    rows, units = [], set()
    required = (
        'series_name',
        'unit',
        'earliest_period',
        'latest_period',
        'earliest_value',
        'latest_value',
        'change_latest_minus_earliest',
    )
    for entry in summaries:
        if any(key not in entry for key in required):
            return None
        if any(
            type(entry[key]) not in (int, float) or not math.isfinite(entry[key])
            for key in ('earliest_value', 'latest_value', 'change_latest_minus_earliest')
        ):
            return None
        begin, end = float(entry['earliest_value']), float(entry['latest_value'])
        change = float(entry['change_latest_minus_earliest'])
        if not math.isclose(end - begin, change, rel_tol=1e-8, abs_tol=1e-4):
            return None
        units.add(str(entry['unit']).strip().casefold())
        rows.append(
            (
                str(entry['series_name']).strip(),
                str(entry['earliest_period']),
                str(entry['latest_period']),
                str(begin),
                str(end),
                str(change),
            )
        )
    if not rows or len(units) != 1 or not rows[0][0]:
        return None
    return rows


def _endpoint_answer_text(result, summaries, rows, percent_trend):
    """The Markdown answer: heading, unit note, endpoint table, plain sentences, source.

    Rows (and the sentences under them) are ordered by the latest value, biggest first.
    """
    order = sorted(range(len(rows)), key=lambda i: -float(rows[i][4]))  # stable for ties
    rows, summaries = [rows[i] for i in order], [summaries[i] for i in order]
    table, url = str(result['table_id']), str(result['source_url'])
    unit = str(summaries[0]['unit']).strip()
    delta_unit = 'Percentage-point change' if percent_trend else 'Change'
    body = [
        f"## Verified observations from SingStat {table}",
        '',
        "The following figures are taken directly from the complete official table. "
        f"Their reported unit is **{unit or 'as stated in the official series'}**."
        + (' Changes are in percentage points, not relative percentages.' if percent_trend else ''),
        '',
        f'| Series | Earliest shown period | Value | Latest shown period | Value | '
        f'{delta_unit} |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for name, first, last, begin, end, change in rows:
        safe_name = name.replace('|', '/').replace('\n', ' ')[:140]
        if not safe_name or not _SAFE_PERIOD.fullmatch(first) or not _SAFE_PERIOD.fullmatch(last):
            return None
        body.append(f'| {safe_name} | {first} | {begin} | {last} | {end} | {change} |')
    # One source-bound sentence per series (first three); never extra numbers.
    insights = [
        text
        for text in (verified_generic_endpoint_insight(entry) for entry in summaries[:3])
        if text
    ]
    if insights:
        body.extend(
            ['', '### What the numbers show', '']
            + [f'- {text}' if len(insights) > 1 else text for text in insights]
        )
    body.extend(['', f'**Official source:** [SingStat {table}]({url})'])
    return '\n'.join(body)


# --- A chart for an answer rebuilt from endpoints ----------------------------

_ENDPOINT_HEADING = re.compile(r'^## Verified observations from SingStat ([A-Z]\d{5,8})\s*$')


def structured_endpoint_chart_from_answer(question, full_answer, evidence):
    """A first-and-latest chart for an answer written by evidence_bound_generic_answer.

    Only for that fixed answer format (its heading names the table). Exactly
    one source must have typed summary rows that match the answer's table
    cell by cell; duplicates of the same result are fine, conflicting matches
    are not. Only the two endpoints are drawn: values between them are never
    invented, and the prose is never used as a data source. Returns a chart
    candidate (tool-result-like dict) or None.
    """
    lines = str(full_answer or '').splitlines()
    heading = _ENDPOINT_HEADING.match(lines[0] if lines else '')
    if not heading:
        return None
    wanted_id = heading.group(1)
    unique = {}
    for source in evidence or []:
        match = _endpoint_source_match(source, wanted_id, full_answer, question)
        if match is not None:
            src, rows, first, last, observations = match
            key = (
                src['table_id'],
                src['source_url'],
                first,
                last,
                str(rows[0].get('unit') or ''),
                tuple(observations),
            )
            unique[key] = match
    if len(unique) != 1:
        return None
    source, rows, first, last, observations = next(iter(unique.values()))
    spec = _endpoint_chart_spec(rows, first, last, observations)
    if not chart_covers_requested_groups(question, spec):
        return None
    return {
        'success': True,
        'table_id': wanted_id,
        'table_complete': True,
        'analysis_ready': True,
        'source_url': source['source_url'],
        'chart_kind': 'verified_summary_endpoints',
        'chart_priority': 90,
        'chart_integrity': 'exact_reconciled_summary_rows',
        'chart_spec': spec,
    }


def _endpoint_source_match(source, wanted_id, full_answer, question):
    """(source, rows, first, last, observations) if this source backs the answer, else None.

    The source must be a complete official result for ``wanted_id`` with 1-4
    summary rows sharing one unit and one pair of endpoint periods, and every
    row must appear in the answer's table with the same three values.
    """
    if (
        not isinstance(source, dict)
        or source.get('success') is not True
        or source.get('table_complete') is not True
        or source.get('analysis_ready') is False
        or source.get('table_id') != wanted_id
        or not is_official_source(source.get('source_url'))
        or not isinstance(source.get('series_summary'), list)
    ):
        return None
    rows = source['series_summary']
    endpoints = _shared_endpoints(rows)
    if endpoints is None:
        return None
    first, last = endpoints
    observations = _rows_in_answer_table(rows, full_answer, first, last)
    if observations is None:
        return None
    # A total beside its own parts would double count.
    if len(observations) > 1 and any(
        re.search(r'(?i)^(?:total|all|overall)\b', name) for name, _, _ in observations
    ):
        return None
    if not _series_name_required_subgroups(question, [name for name, _, _ in observations]):
        return None
    if not chart_covers_requested_groups(
        question,
        {
            'kind': 'bar',
            'categories': [first, last],
            'series': [
                {'name': name, 'values': [begin, finish]} for name, begin, finish in observations
            ],
        },
    ):
        return None
    return source, rows, first, last, observations


def _shared_endpoints(rows):
    """(first period, last period) shared by 1-4 summary rows of one unit, or None."""
    if not 1 <= len(rows) <= 4:
        return None
    units = {str(r.get('unit', '')).strip().casefold() for r in rows if isinstance(r, dict)}
    starts = {str(r.get('earliest_period')) for r in rows if isinstance(r, dict)}
    ends = {str(r.get('latest_period')) for r in rows if isinstance(r, dict)}
    if len(units) != 1 or len(starts) != 1 or len(ends) != 1:
        return None
    first, last = next(iter(starts)), next(iter(ends))
    if not (first and last and _SAFE_PERIOD.fullmatch(first) and _SAFE_PERIOD.fullmatch(last)):
        return None
    return first, last


def _rows_in_answer_table(rows, full_answer, first, last):
    """[(name, first value, last value)] when EVERY row is consistent and in the answer, else None."""
    observations = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        begin, finish, change = (
            row.get(k) for k in ('earliest_value', 'latest_value', 'change_latest_minus_earliest')
        )
        name = str(row.get('series_name') or '').strip()
        if (
            not name
            or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in (begin, finish, change)
            )
            or not math.isclose(finish - begin, change, rel_tol=1e-9, abs_tol=1e-5)
        ):
            return None
        clean_name = name.replace('|', '/').replace('\n', ' ')[:140]
        if not _answer_has_endpoint_row(
            full_answer, clean_name, first, last, begin, finish, change
        ):
            return None
        observations.append((clean_name, float(begin), float(finish)))
    return observations


def _answer_has_endpoint_row(full_answer, name, first, last, begin, finish, change) -> bool:
    """True when the answer's table has this exact row (name, periods and three values).

    Whole cells are compared, never substrings inside a different row.
    """
    for line in str(full_answer).splitlines():
        if not line.strip().startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) < 6 or cells[0] != name or cells[1] != first or cells[3] != last:
            continue
        try:
            row_begin, row_end, row_change = (float(cells[i].replace(',', '')) for i in (2, 4, 5))
        except ValueError:
            continue
        if (
            math.isclose(row_begin, float(begin), rel_tol=0, abs_tol=1e-6)
            and math.isclose(row_end, float(finish), rel_tol=0, abs_tol=1e-6)
            and math.isclose(row_change, float(change), rel_tol=0, abs_tol=1e-6)
        ):
            return True
    return False


def _series_name_required_subgroups(question, names) -> bool:
    """False when the question names a narrow subgroup that some series does not.

    The subgroups are vocabulary (utils/vocabulary.REQUIRED_SUBGROUP_PATTERNS):
    e.g. a "first-born only" question cannot be answered by a series for all
    birth orders even though both are in the same table.
    """
    asked = str(question or '').casefold()
    for question_pattern, series_pattern in REQUIRED_SUBGROUP_PATTERNS:
        if re.search(question_pattern, asked) and any(
            not re.search(series_pattern, name, re.IGNORECASE) for name in names
        ):
            return False
    return True


def _endpoint_chart_spec(rows, first, last, observations) -> dict:
    """Bars for the endpoints: one snapshot, one series, or grouped series."""
    unit = str(rows[0].get('unit') or 'Value')
    if first == last:
        return {
            'kind': 'bar',
            'title': f'Verified snapshot · {last}',
            'unit': unit,
            'categories': [name for name, _, _ in observations],
            'series': [{'name': 'Published values', 'values': [b for _, _, b in observations]}],
        }
    title = 'First and latest verified observations (endpoints only)'
    if len(rows) == 1:
        name, begin, finish = observations[0]
        return {
            'kind': 'bar',
            'title': title,
            'unit': unit,
            'categories': [first, last],
            'series': [{'name': name, 'values': [begin, finish]}],
        }
    return {
        'kind': 'grouped_bar',
        'title': title,
        'unit': unit,
        'categories': [first, last],
        'series': [{'name': name, 'values': [a, b]} for name, a, b in observations],
    }


def _verified_explicit_percentage_equations(answer, evidence):
    """Approve ONLY explicitly shown and independently recomputed A/B shares.

    A and B must be observations of the SAME complete official table and SAME
    period. This is not a general license to accept unsupported model maths.
    """
    allowed = set()
    for item in evidence:
        if (
            not isinstance(item, dict)
            or item.get('success') is not True
            or item.get('table_complete') is False
            or item.get('analysis_ready') is False
            or not is_official_source(item.get('source_url'))
        ):
            continue
        # Only results from a complete official table can qualify.
        if item.get('table_complete') is not True:
            continue
        by_period = {}
        for row in item.get('observations') or []:
            if (
                isinstance(row, dict)
                and type(row.get('value')) in (int, float)
                and math.isfinite(row['value'])
            ):
                by_period.setdefault(str(row.get('period')), set()).add(float(row['value']))
        # Accept a literal arithmetic line only, not a free-text model claim.
        equation = re.compile(
            r'(?<!\w)([\d,]+(?:\.\d+)?)\s*[÷/]\s*'
            r'([\d,]+(?:\.\d+)?)\s*[×x*]\s*100\s*=\s*'
            r'([\d,]+(?:\.\d+)?)\s*%'
        )
        for match in equation.finditer(answer):
            numerator, denominator, claimed = [float(x.replace(',', '')) for x in match.groups()]
            if denominator <= 0 or numerator < 0 or numerator > denominator:
                continue
            if not any(
                numerator in values and denominator in values for values in by_period.values()
            ):
                continue
            exact = numerator / denominator * 100
            precision = len(match.group(3).split('.')[1]) if '.' in match.group(3) else 0
            if abs(exact - claimed) <= 0.5 * 10 ** (-precision) + 1e-10:
                allowed.update(('100', match.group(3).replace(',', '')))
                for rounded_places in (1, 2):
                    allowed.add(f'{exact:.{rounded_places}f}')
    return allowed


def full_answer_numbers_supported(answer, evidence, question, *, log: bool = True) -> bool:
    """Match all answer numbers to verified tools, allowing verified rounding.

    This is deliberately NOT an approximate numerical comparison. The ONLY
    formatting exception is an exact 1/2-decimal rendering of a specifically
    typed, verified percentage, appearing as a percentage in the answer. A
    different number or an arbitrary derived statistic is still rejected.
    """
    factual = "\n".join(json.dumps(result, ensure_ascii=False, default=str) for result in evidence)
    # Publication table numbers and a disease's name are metadata, not
    # statistical claims. Do not generally exempt calendar years or figures.
    checked_text = numeric_claim_metadata_removed(bibliographic_number_stripped_answer(answer))
    unmatched = numeric_tokens(checked_text) - (numeric_tokens(factual) | numeric_tokens(question))
    # A reconciled typed series summary permits exact display formatting and
    # independently computed endpoint changes. It is NOT a licence to admit
    # arbitrary AI-generated statistics. Percentages require an explicit '%'.
    ledger = reconciled_summary_tokens(evidence, is_official_source)
    # The original checker only saw the first 40 generic observations. This
    # compact ledger contains additional values from the same selected FULL
    # official table; it does not permit arbitrary derived values.
    for item in evidence:
        if (
            not isinstance(item, dict)
            or item.get("success") is not True
            or item.get("table_complete") is not True
            or item.get("analysis_ready") is False
            or not is_official_source(item.get("source_url"))
        ):
            continue
        claims = item.get("numeric_claims")
        if (
            not isinstance(claims, list)
            or len(claims) > 750
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in claims)
        ):
            continue
        for value in claims:
            number = abs(float(value))
            ledger.add(str(int(number)) if number.is_integer() else str(number))
            # A normalised 2-decimal rendering is only legal if the rounding
            # really reproduces the printed value; no extra tolerance.
            if not number.is_integer():
                ledger.add(format(number, ".2f"))
    ledger.update(_verified_explicit_percentage_equations(answer, evidence))
    ledger.update(verified_exact_display_claims(evidence, checked_text))
    for token in list(unmatched):
        if token in ledger and isinstance(token, str):
            unmatched.remove(token)
        elif ('percentage', token) in ledger and re.search(
            rf'(?<![\d.])[-+]?{re.escape(token)}\s*%', checked_text
        ):
            unmatched.remove(token)
    if not unmatched:
        return True

    rounded = verified_rounded_percentage_tokens(evidence)
    for token in list(unmatched):
        if token in rounded and re.search(rf"(?<![\d.])[-+]?{re.escape(token)}\s*%", answer):
            unmatched.remove(token)
    if unmatched and log:
        # Log the mismatched numeric strings, not the full answer/question or
        # user-supplied tool payload. This helps diagnose future false rejects.
        LOGGER.warning("Unmatched full-answer numeric tokens: %s", sorted(unmatched)[:12])
    return not unmatched


_SOURCE_WORDS = re.compile(r'(?i)\b(?:source|definition|methodology)\b')


def remove_unverified_optional_sentence(answer, evidence, question):
    """Salvage a verified DATA TABLE when one commentary sentence is unsupported.

    This does not add numbers, change table cells, change units or replace a
    source. Only a single numeric prose sentence *outside* a Markdown data
    table may be omitted, and the ENTIRE surviving answer must pass the same
    strict numerical gate. This handles a model's incorrect optional sum or
    descriptive range without accidentally accepting that calculation.
    """
    if not isinstance(answer, str) or not isinstance(evidence, list):
        return None
    if not any(len(rows) >= 3 for _, rows in _verified_answer_table_rows(answer)):
        return None
    if len(answer) > 24000 or full_answer_numbers_supported(answer, evidence, question, log=False):
        return None
    lines = answer.splitlines(keepends=True)
    # No unbounded search or arbitrary multi-sentence rewriting: 20 tries at most.
    for candidate in _removable_sentences(lines)[:20]:
        reduced = _without(lines, candidate)
        # Keep most of the original response and at least one checked source.
        if len(reduced) < len(answer) * 0.65 or not re.search(
            r'(?i)\bSingStat(?:\s+Table\s+Builder)?\s+[A-Z]\d{5,8}\b', reduced
        ):
            continue
        if full_answer_numbers_supported(reduced, evidence, question, log=False):
            return reduced + (
                '\n\n*A supplementary numerical statement was '
                'omitted because it could not be verified.*'
            )
    return None


def _removable_sentences(lines) -> list:
    """Numeric prose that may be dropped: (line, None) for a whole line, or
    (line, sentence index, sentences) for one sentence of a paragraph.

    Never the headline (the first prose line, whose first sentence stays),
    tables, headings, code, links, or text naming a source, definition or method.
    """
    table_mode, headline_seen, possible = False, False, []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('|'):
            table_mode = True
            continue
        if table_mode and stripped:
            table_mode = False
        is_headline = (
            bool(stripped) and not stripped.startswith(('#', '|', '```')) and not headline_seen
        )
        headline_seen = headline_seen or is_headline
        if (
            not stripped
            or stripped.startswith(('#', '|', '```'))
            or 'http://' in stripped
            or 'https://' in stripped
            or re.search(r'(?i)\b(?:source|singstat|official table)\b', stripped)
            or not numeric_tokens(stripped)
        ):
            continue
        # Split only at sentence boundaries (never at a decimal point), keeping Markdown.
        pieces = re.split(r'(?<=\.\*\*)\s+(?=[A-Z])|(?<=\.)\s+(?=[A-Z])', line.rstrip('\n'))
        if len(pieces) < 2:
            # A one-sentence bullet or paragraph (e.g. an AI-added "27 in total").
            if not is_headline and not _SOURCE_WORDS.search(stripped):
                possible.append((i, None))
            continue
        possible += [
            (i, j, pieces)
            for j, part in enumerate(pieces)
            if (j > 0 or not is_headline)
            and numeric_tokens(part)
            and not _SOURCE_WORDS.search(part)
        ]
    return possible


def _without(lines, candidate) -> str:
    """The answer with one candidate line or sentence removed."""
    trial = lines[:]
    if candidate[1] is None:
        trial[candidate[0]] = ''
    else:
        pieces = candidate[2]
        trial[candidate[0]] = ' '.join(v for j, v in enumerate(pieces) if j != candidate[1]) + '\n'
    return ''.join(trial).strip()


def verified_exact_integer_tokens(evidence):
    """Exact integer formatting of fully checked typed series-summary values.

    Treat 12 and 12.0 as identical only when the ORIGINAL typed official
    value is exactly integral and the reconciled summary is from a complete
    official table. Never permit approximate rounding or free-form results.
    """
    approved = set()
    for result in evidence or []:
        if (
            not isinstance(result, dict)
            or result.get('success') is not True
            or result.get('table_complete') is not True
            or result.get('analysis_ready') is False
            or not is_official_source(result.get('source_url'))
        ):
            continue
        for entry in result.get('series_summary') or []:
            if not isinstance(entry, dict):
                continue
            values = [
                entry.get(k)
                for k in ('earliest_value', 'latest_value', 'change_latest_minus_earliest')
            ]
            if any(
                type(v) not in (int, float) or not math.isfinite(v) for v in values
            ) or not math.isclose(values[1] - values[0], values[2], rel_tol=1e-9, abs_tol=1e-5):
                continue
            approved.update(str(int(v)) for v in values if float(v).is_integer())
    return approved


def simple_answer_numbers_supported(summary, full_answer, evidence) -> bool:
    """Reject numbers absent from verified evidence; permit exact integers."""
    context = (
        full_answer
        + "\n"
        + "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in evidence)
    )
    exact_helper = globals().get('verified_exact_integer_tokens')
    extra = exact_helper(evidence) if callable(exact_helper) else set()
    return numeric_tokens(summary) <= (numeric_tokens(context) | extra)


def simple_answer_is_refusal(summary) -> bool:
    """A second-model refusal is not a simplified answer to an evidenced result."""
    value = re.sub(r"\s+", " ", str(summary)).strip().lower()
    if not value:
        return True
    refusal_phrases = (
        "cannot safely explain",
        "can't safely explain",
        "couldn't simplify",
        "cannot simplify",
        "not enough verified data",
        "don't have enough checked",
        "do not have enough verified",
        "please rephrase your question",
        "not enough information to explain",
        "i can't check enough",
        "i cannot check enough",
        "cannot verify enough",
    )
    return any(phrase in value for phrase in refusal_phrases)


def simple_answer_year_value_pairs_supported(summary, full_answer):
    """Reject swapped value/year associations even if all numbers are real.

    The numeric-token test alone would permit a model to attach the wrong
    year to an otherwise verified number. Require each explicitly phrased
    value/year pair to appear in the checked full answer in either order.
    Conservative rejection falls back to exact source extraction.
    """
    number = r"\d[\d,]*(?:\.\d+)?%?"
    year = r"(?:19|20)\d{2}"
    forward = re.compile(rf"(?<![\w.])({number})\s+in\s+({year})\b", re.I)
    reverse = re.compile(
        rf"\b(?:in\s+)?({year})\s*(?:[:,]|\b(?:was|at)\b)\s*({number})(?!\w)", re.I
    )

    def pairs(text):
        """(value, year) pairs stated in a text, in either order ("5.2 in 2020" / "2020: 5.2")."""
        clean = re.sub(r"\*{1,3}|__|`", "", str(text))
        found = {(v.replace(',', ''), y) for v, y in forward.findall(clean)}
        found |= {(v.replace(',', ''), y) for y, v in reverse.findall(clean)}
        return found

    return pairs(summary) <= pairs(full_answer)


def simple_answer_trend_consistent(summary, full_answer) -> bool:
    """Block a reversed trend if the verified source contains no matching direction.

    In mixed-direction results, retain the full-answer-only fallback rather
    than guess which comparison an LLM sentence refers to.
    """
    up = r"\b(?:increase[ds]?|increasing|rose|rising|grew|grown|growth|upward|went up|higher)\b"
    down = (
        r"\b(?:decrease[ds]?|decreasing|fell|fallen|decline[ds]?|declining|downward|went "
        r"down|lower)\b"
    )
    summary_up = bool(re.search(up, summary, re.I))
    summary_down = bool(re.search(down, summary, re.I))
    source_up = bool(re.search(up, full_answer, re.I))
    source_down = bool(re.search(down, full_answer, re.I))
    if summary_down and not source_down:
        return False
    if summary_up and not source_up:
        return False
    # A summary asserting BOTH opposite trends from one-direction-only input
    # must not be accepted as a coherent account of the same figures.
    return True


def strip_unnecessary_chart_commentary(answer):
    """Remove the precise optional-chart boilerplate from an otherwise factual answer.

    This is strictly a presentation change: do not erase an actual source/data
    failure or pretend that a chart exists. Only strip the repeated sentence
    when a substantive result (a data table or numeric findings) remains.
    """
    if not isinstance(answer, str):
        return answer
    # Numeric Markdown table or a numerical narrative must remain after removal.
    has_results = bool(re.search(r"(?m)^\s*\|[^\n]*?\|[^\n]*?\d", answer)) or bool(
        re.search(r"(?im)^\s*[-*]\s+[^\n]*\d", answer)
    )
    if not has_results:
        return answer
    cleaned = re.sub(
        r"(?i)\s*,?\s+but\s+(?:\*\*)?no verified (?:combined )?"
        r"chart was generated by (?:the )?approved analytical tools(?:\*\*)?\.",
        ".",
        answer,
    )
    cleaned = re.sub(
        r"(?im)^\s*(?:no verified (?:combined )?chart was generated by "
        r"(?:the )?approved analytical tools|a verified combined chart was not "
        r"generated by the approved analytical tools)\.\s*\n?",
        "",
        cleaned,
    )
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


# ---------------------------------------------------------------------------
# Checks on numbers quoted in the answer text (shared with chart_selection)
# ---------------------------------------------------------------------------


def _verified_answer_table_rows(answer):
    """Return checked Markdown data rows with their ORIGINAL column associations.

    This is presentation evidence only. The caller must cross-match each row
    against its corresponding official structured series before using it.
    """
    tables = []
    lines = str(answer or '').splitlines()
    for pos, line in enumerate(lines):
        if not line.strip().startswith('|'):
            continue
        header = [c.strip() for c in line.strip().strip('|').split('|')]
        # A two-column official snapshot (age group | deaths) is valid.
        # V30 required three columns here, inadvertently disabling the
        # same-year categorical recovery for many otherwise checked tables.
        if (
            len(header) < 2
            or pos + 1 >= len(lines)
            or not re.fullmatch(r'\s*\|[\s:|\-]+\|?\s*', lines[pos + 1])
        ):
            continue
        rows = []
        for row_line in lines[pos + 2 :]:
            if not row_line.strip().startswith('|'):
                break
            cells = [c.strip().replace('**', '') for c in row_line.strip().strip('|').split('|')]
            if len(cells) != len(header):
                break
            rows.append(cells)
        if rows:
            tables.append((header, rows))
    return tables


def numeric_claim_metadata_removed(answer) -> str:
    """Ignore actual table units and ordinal rank columns, never data cells."""
    value = str(answer or '')
    # Unit notation, not a statistical claim: (\u2018000), ('000), or `000.
    value = re.sub(r"(?im)(?<=\()\s*[\u2018\u2019'`]000\s*(?=\))", 'thousands', value)
    value = re.sub(r"(?im)(?<=\s)[\u2018\u2019'`]000(?=\s*\|)", 'thousands', value)
    lines = value.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith('|'):
            continue
        header = [cell.strip().casefold() for cell in line.strip().strip('|').split('|')]
        if (
            not header
            or header[0] != 'rank'
            or index + 1 >= len(lines)
            or not re.fullmatch(r'\s*\|[\s:|\-]+\|?\s*', lines[index + 1])
        ):
            continue
        row_indexes = []
        for row_index in range(index + 2, len(lines)):
            row = lines[row_index]
            if not row.lstrip().startswith('|'):
                break
            columns = [v.strip() for v in row.strip().strip('|').split('|')]
            if len(columns) != len(header):
                break
            row_indexes.append((row_index, columns[0]))
        # A heading alone cannot excuse numerical claims. Require a complete,
        # strictly sequential markdown rank table and touch ONLY its rank cells.
        if not 2 <= len(row_indexes) <= 20 or [value for _, value in row_indexes] != [
            str(i) for i in range(1, len(row_indexes) + 1)
        ]:
            continue
        for row_index, _ in row_indexes:
            lines[row_index] = re.sub(
                r'^(\s*\|\s*)\d{1,2}(\s*\|)', r'\1rank\2', lines[row_index], count=1
            )
    return '\n'.join(lines)


def verified_exact_display_claims(evidence, answer):
    """Only allow exact 1-3 decimal representations of TYPED official numbers.

    No approximate tolerance: formatting 62.29 as 62.290 is fine, while
    presenting 62.29 as 62.291 is not. A 'million' abbreviation is admitted
    only next to its unit and only after rounding a complete checked count.
    """
    accepted = set()
    text = str(answer or '')
    for item in evidence or []:
        if (
            not isinstance(item, dict)
            or item.get('success') is not True
            or item.get('table_complete') is not True
            or item.get('analysis_ready') is False
            or not is_official_source(item.get('source_url'))
        ):
            continue
        numbers = []
        for value in item.get('numeric_claims') or []:
            if type(value) in (int, float) and math.isfinite(value):
                numbers.append(float(value))
        for row in item.get('series_summary') or []:
            if isinstance(row, dict):
                for key in ('earliest_value', 'latest_value', 'change_latest_minus_earliest'):
                    value = row.get(key)
                    if type(value) in (int, float) and math.isfinite(value):
                        numbers.append(float(value))
        for number in numbers[:750]:
            absolute = abs(number)
            for decimals in (1, 2, 3):
                formatted = f'{absolute:.{decimals}f}'
                if float(formatted) == absolute:
                    accepted.add(formatted)
            if absolute >= 1_000:
                for decimals in (0, 1):
                    short = f'{absolute/1_000:.{decimals}f}'
                    if re.search(rf'(?<![\d.]){re.escape(short)}\s+thousand\b', text, re.I):
                        accepted.add(short)
            if absolute >= 1_000_000:
                for decimals in (1, 2):
                    short = f'{absolute/1_000_000:.{decimals}f}'
                    if re.search(rf'(?<![\d.]){re.escape(short)}\s+million\b', text, re.I):
                        # A one/two-decimal million suffix explicitly signals
                        # rounding. This exception is deliberately unit-bound.
                        accepted.add(short)
    return accepted


# --- Longer history when the answering table has one period ---------------------

_CHANGE_QUESTION = re.compile(
    r'(?i)\b(change|changed|changes|trend|over time|across years|since|each year|every year|'
    r'growth|grow|grew|previous year|year on year|year-on-year)\b'
)
_FREQUENCY_WORD = re.compile(r'(?i)\b(annual|quarterly|monthly|half-yearly)\b')


def _published_number(value) -> str:
    """A published value written as in the source (1,234.5; whole numbers without '.0')."""
    number = float(value)
    return f'{int(number):,}' if number.is_integer() else f'{number:,}'


def longer_history_for_single_period(question, response, core):
    """(response with one extra official result, note) when the answer has ONE period only.

    For a change-over-time question whose verified evidence covers a single
    period (e.g. a growth-rate table that currently publishes 2025 only), query
    the closest other candidate table of the SAME subject and frequency for the
    same series name, and return it with a short, fully sourced note. The new
    figures are published values (no growth is calculated). Returns None when
    nothing suitable is found; the answer is then left exactly as it was.
    """
    from utils.search_terms import subject_terms

    if not isinstance(question, str) or not _CHANGE_QUESTION.search(question):
        return None
    evidence = official_evidence(response)
    summaries = [
        row
        for item in evidence
        for row in (item.get('series_summary') or [])
        if isinstance(row, dict)
    ]
    if (
        not evidence
        or not summaries
        or any(int(row.get('observation_count') or 0) > 1 for row in summaries)
    ):
        return None
    base = evidence[0]
    base_id, base_title = str(base.get('table_id') or ''), str(base.get('title') or '')
    name = str(summaries[0].get('series_name') or '').strip()
    frequency = _FREQUENCY_WORD.search(base_title)
    query = getattr(core, 'query_singstat_table', None)
    if not (name and frequency and callable(query)):
        return None
    base_words = subject_terms(base_title)
    tried = 0
    for row in response.get('rag_results') or []:
        tid, title = str(row.get('table_id') or ''), str(row.get('title') or '')
        if (
            not tid
            or tid == base_id
            or tried >= 3
            or not re.search(r'(?i)\b' + frequency.group(1) + r'\b', title)
            or len(base_words & subject_terms(title)) < 3
        ):
            continue
        tried += 1
        try:
            found = query(tid, series_search=name, max_series=1, make_chart=True)
        except (ValueError, TypeError, RuntimeError, OSError, KeyError) as exc:
            LOGGER.info('Longer-history query unavailable for %s: %s', tid, type(exc).__name__)
            continue
        note = _longer_history_note(found, tid, name, base_id, summaries[0])
        if note:
            found['supplementary_history_for'] = base_id
            return {
                **response,
                'tool_results': list(response.get('tool_results') or []) + [found],
            }, note
    return None


def _longer_history_note(found, tid, name, base_id, base_summary):
    """The sourced note for an accepted longer series, or None if the result does not qualify."""
    rows = found.get('series_summary') if isinstance(found, dict) else None
    if (
        not isinstance(found, dict)
        or found.get('success') is not True
        or found.get('table_id') != tid
        or found.get('table_complete') is not True
        or not is_official_source(found.get('source_url'))
        or found.get('chart_source_verified') is not True
        or not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
    ):
        return None
    row = rows[0]
    if (
        str(row.get('series_name') or '').strip().casefold() != name.casefold()
        or int(row.get('observation_count') or 0) < 2
    ):
        return None
    first, last = str(row.get('earliest_period')), str(row.get('latest_period'))
    title = str(found.get('title') or tid)
    unit = str(row.get('unit') or '').strip()
    return (
        f"SingStat table {base_id} currently publishes {base_summary.get('latest_period')} "
        f"only. For a longer view, the chart shows the related official series "
        f"\"{name}\" from SingStat {tid} ({title}): "
        f"{_published_number(row['earliest_value'])} in {first} and "
        f"{_published_number(row['latest_value'])} in {last}" + (f" ({unit})." if unit else ".")
    )
