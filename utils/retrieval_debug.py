"""Conservative, display-only provenance of statistical data retrieval.

Never infer cache hits from existence of a local database or from OpenAI cached
prompt tokens. Only explicit per-tool retrieval indicators are classified.

Used by: utils/pipeline_audit.py, tools/characterise_retrieval_rows.py.
"""

from __future__ import annotations

from typing import Any


def _status(result: dict[str, Any]) -> str:
    """ "Cached" or "Fresh retrieval" for a tool result, from whichever field records it."""
    hit = result.get('cache_hit')
    if type(hit) is bool:
        return 'Cached' if hit else 'Fresh retrieval'
    if result.get('served_from_cache') is True or result.get('from_cache') is True:
        return 'Cached'
    if result.get('fetched_live') is True or result.get('retrieved_live') is True:
        return 'Fresh retrieval'
    explicit = str(result.get('cache_status') or '').strip().casefold()
    if explicit == 'mixed':
        return 'Mixed (cached and fresh)'
    if explicit in {'hit', 'cached', 'cache_hit'}:
        return 'Cached'
    if explicit in {'miss_fetched', 'live_fetch', 'fresh', 'fetched_live'}:
        return 'Fresh retrieval'
    # A phrase such as 'Table Builder / SQLite' does NOT prove which path ran.
    return 'Not recorded'


def _basic_retrieval_rows(response: object) -> list[dict[str, str]]:
    """Return bounded, sanitized display rows from real analytical tool results."""
    if not isinstance(response, dict):
        return []
    results = response.get('tool_results')
    if not isinstance(results, list):
        return []
    output = []
    for result in results[:30]:
        if not isinstance(result, dict):
            continue
        table = str(result.get('table_id') or '').strip()[:32]
        source = str(result.get('retrieval_source') or result.get('data_source') or '').strip()[
            :180
        ]
        # Only show data retrieval entries, not unrelated tools or model cache details.
        if (
            not table
            and not source
            and not any(
                k in result
                for k in (
                    'cache_hit',
                    'cache_status',
                    'served_from_cache',
                    'from_cache',
                    'fetched_live',
                    'retrieved_live',
                )
            )
        ):
            continue
        # Preserve the actual recorded retrieval channel when available.
        channel = str(result.get('retrieval_channel') or '').strip()[:180]
        if channel:
            source = channel
        # The audit must accept older records which do not carry Details.
        # Display-only: never reconstruct or guess an HTTP request.
        details = str(result.get('retrieval_details') or '').strip()[:1500]
        output.append(
            {
                'Table': table or 'Not recorded',
                'Retrieval': _status(result),
                'Source': source or 'Not recorded',
                'Details': details,
            }
        )
    return output


import re


def _verified_tabledata_url(raw_url, table):
    """Return a recorded URL only if it is the expected table's actual API URL.

    This is deliberately not a URL constructor. Missing provenance stays missing.
    """
    from urllib.parse import parse_qsl, urlsplit

    if not isinstance(raw_url, str) or len(raw_url) > 1300:
        return ''
    if not isinstance(table, str) or not re.fullmatch(r'[MCD]\d{5,8}', table, re.I):
        return ''
    try:
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme != 'https'
            or parsed.hostname != 'tablebuilder.singstat.gov.sg'
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.path.rstrip('/').lower() != '/api/table/tabledata/' + table.lower()
            or any(
                key.lower() not in ('limit', 'offset')
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            )
        ):
            return ''
    except ValueError:
        return ''
    return raw_url


_CACHE_FLAGS = (
    'cache_hit',
    'cache_status',
    'served_from_cache',
    'from_cache',
    'fetched_live',
    'retrieved_live',
)


def retrieval_rows(response: object) -> list[dict[str, str]]:
    """The basic retrieval rows, each enriched with the API URL actually requested.

    Records without an actual captured request URL do not gain a fabricated URL.
    Repeated records for the same table and retrieval state are matched in order.
    """
    original_rows = _basic_retrieval_rows(response)
    if not isinstance(original_rows, list):
        return original_rows
    results = response.get('tool_results') if isinstance(response, dict) else None
    if not isinstance(results, list):
        return original_rows
    matches = _results_by_table_and_state(results)
    return [
        _with_request_url(row, matches) if isinstance(row, dict) else row for row in original_rows
    ]


def _results_by_table_and_state(results) -> dict:
    """{(table, retrieval state): [results]} for the first 30 results that say where data came from."""
    matches = {}
    for result in results[:30]:
        if not isinstance(result, dict):
            continue
        table = str(result.get('table_id') or '').strip()[:32] or 'Not recorded'
        source = str(result.get('retrieval_source') or result.get('data_source') or '').strip()[
            :180
        ]
        if table == 'Not recorded' and not source and not any(k in result for k in _CACHE_FLAGS):
            continue
        matches.setdefault((table, _status(result)), []).append(result)
    return matches


def _with_request_url(original, matches) -> dict:
    """One row with the verified Table Builder URL (fresh retrievals only) and details."""
    row = dict(original)
    table = str(row.get('Table') or 'Not recorded')
    state = str(row.get('Retrieval') or 'Not recorded')
    candidates = matches.get((table, state), [])
    result = candidates.pop(0) if candidates else None
    raw_urls = result.get('retrieval_api_urls') if isinstance(result, dict) else []
    if not isinstance(raw_urls, (list, tuple)):
        raw_urls = []
    verified_url = ''
    if state == 'Fresh retrieval':
        verified_url = next(
            (
                url
                for url in (_verified_tabledata_url(value, table) for value in raw_urls[:12])
                if url
            ),
            '',
        )
    row['API URL'] = verified_url
    if not row.get('Details'):
        row['Details'] = str(result.get('retrieval_details') or '')[:220] if result else ''
    # This is a source classification, not a claim about an unrecorded URL.
    source_text = str(row.get('Source') or '').strip()
    if state == 'Fresh retrieval' and (
        not source_text
        or source_text.casefold() in {'network endpoint not recorded', 'not recorded'}
    ):
        row['Source'] = 'Singstat Api'
    return row
