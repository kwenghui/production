"""Evidence-only per-stage pipeline diagnostics for Sage technical details.

Report what is present in the answer's recorded tool and RAG data. Never infer
model reasoning, API access, ChromaDB use or cache freshness from table IDs.

Used by: ui/answer_view.py, tools/characterise_audit_rows.py.
"""

from __future__ import annotations

import re

_TABLE_ID = re.compile(r'^[MCD]\d{5,8}$', re.I)


def _table_id(value):
    """A table ID upper-cased if it is well-formed, else None."""
    candidate = str(value or '').strip().upper()
    return candidate if _TABLE_ID.fullmatch(candidate) else None


def _rag_backend(item):
    """Plain-language name of the search method that produced a note."""
    mode = str(item.get('retrieval_mode') or '').lower()
    if mode == 'persisted_verified_metadata':
        return 'SQLite: Retrieved table descriptions for combined keyword and semantic ranking'
    if mode == 'curated_local_keyword':
        return 'Built-in keyword suggestions'
    if 'chroma' in mode:
        return 'Semantic search record (ChromaDB mode recorded)'
    if mode:
        return 'Search method: ' + (
            {'hybrid_table_ranking': 'combined keyword and semantic ranking'}.get(mode, mode[:90])
        )
    # Existing RagCatalog records from earlier versions may not have a marker.
    return 'Not recorded'


def _recorded_api_url(raw_url, table):
    """Show only an actually recorded SingStat tabledata URL for that table."""
    from urllib.parse import urlsplit, parse_qsl

    if not isinstance(raw_url, str) or len(raw_url) > 1300:
        return ''
    tid = _table_id(table)
    if not tid:
        return ''
    try:
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme != 'https'
            or parsed.hostname != 'tablebuilder.singstat.gov.sg'
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port not in (None, 443)
            or parsed.path.rstrip('/').lower() != '/api/table/tabledata/' + tid.lower()
            or any(
                key.lower() not in {'limit', 'offset'}
                for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
            )
        ):
            return ''
    except (ValueError, TypeError):
        return ''
    return raw_url


def _official_page(table):
    """An official reference page, explicitly not an observed request URL."""
    tid = _table_id(table)
    if not tid:
        return ''
    from utils.official_links import table_page_url

    return table_page_url(tid) or ''


def _cache_details(text):
    """Preserve recorded cache age without repeating opaque internal labels."""
    text = str(text or '').strip()
    text = text.replace('Verified SQLite snapshot reused', '').strip(' ;.')
    # Legacy free-text can aggregate several tables' links. Do not display
    # unvalidated URLs from that field as if they belong to this table.
    if '://' in text:
        return ''
    if text.lower().startswith('cache age '):
        text = 'Cache age: ' + text[len('cache age ') :]
    return text


def stage_rows(response):
    """The "how this answer was produced" table shown in the technical details.

    One row per recorded event, in pipeline order:

        1 Understand request -> 2 Find candidates (+ keyword / semantic search
        and shortlist) -> 3 Select candidate -> 4 Inspect table ->
        5 Get published values -> 6 Calculate and verify

    Only what the response actually RECORDS is reported; when a stage left no
    record the row says "Not recorded" rather than guessing. Each row is a
    dict with keys Stage, Status, "Database / service", "Decision / evidence".
    """
    if not isinstance(response, dict):
        return []
    rag = response.get('rag_results')
    tools = response.get('tool_results') if isinstance(response.get('tool_results'), list) else None
    rows = []
    rows += _understand_rows(response.get('intent_decision'))
    rows += _candidate_rows(rag)
    rows += _search_trace_rows(rag)
    rows += _selection_rows(tools)
    queried = _queried_results(tools)
    rows += _inspection_rows(tools, queried)
    rows += _retrieval_rows(response)
    rows += _verification_rows(tools, queried)
    return rows


def _row(stage, status, system, evidence) -> dict:
    """One table row (evidence text capped at 650 characters)."""
    return {
        'Stage': stage,
        'Status': status,
        'Database / service': system,
        'Decision / evidence': str(evidence)[:650],
    }


def _understand_rows(intent) -> list:
    """Stage 1: the recorded scope decision."""
    if not isinstance(intent, dict):
        return [
            _row(
                '1 · Understand request',
                'Not recorded',
                'Not recorded',
                'No structured intent decision is attached to this answer',
            )
        ]
    scope = intent.get('in_scope')
    status = 'In scope' if scope is True else 'Recorded' if scope is None else 'Out of scope'
    return [
        _row(
            '1 · Understand request',
            status,
            'Question interpretation',
            intent.get('summary') or intent.get('reason') or 'Scope decision available',
        )
    ]


def _candidate_rows(rag) -> list:
    """Stage 2: descriptions supplied to the model, and the table IDs they mention."""
    if not (isinstance(rag, list) and rag):
        return [
            _row(
                '2 · Find candidates',
                'No RAG records',
                'Not recorded',
                'A separate catalogue, explicit ID or tool route may have been used; '
                'no RAG result is attached',
            )
        ]
    sources, ids = [], []
    for item in rag[:30]:
        if not isinstance(item, dict):
            continue
        source = _rag_backend(item)
        if source not in sources:
            sources.append(source)
        text = ' '.join(str(item.get(k) or '') for k in ('source_url', 'text'))
        for table in re.findall(r'\b[MCD]\d{5,8}\b', text, re.I):
            tid = _table_id(table)
            if tid and tid not in ids:
                ids.append(tid)
    return [
        _row(
            '2 · Find candidates',
            'Candidates found',
            '; '.join(sources) or 'Not recorded',
            f'{len(rag)} descriptions supplied for answering. Possible official table IDs: '
            f'{", ".join(ids[:8]) or "none recorded"}.',
        )
    ]


# Plain-language names of the search methods recorded in a ranking trace.
_SEARCH_METHOD_NAMES = {
    'ChromaDB series': 'semantic search of individual indicators',
    'ChromaDB table': 'semantic search of table descriptions',
    'SQLite FTS5': 'keyword matching',
}


def _search_trace_rows(rag):
    """Stage 2 detail: the recorded keyword search, semantic search and shortlist."""
    traces = (
        [
            item.get('discovery_trace')
            for item in rag
            if isinstance(item, dict) and isinstance(item.get('discovery_trace'), dict)
        ]
        if isinstance(rag, list)
        else []
    )
    if not traces:
        return []
    trace = traces[0]
    keyword = trace.get('sqlite') or {}
    rows = [
        _row(
            '2 · SQLite FTS5 search',
            keyword.get('status', 'Not recorded'),
            'SQLite: Perform keyword search',
            f'Keyword search returned '
            f'{keyword.get("result_count", "an unrecorded number of")} '
            'matching table/series descriptions.',
        )
    ]
    semantic = trace.get('chroma') or {}
    rows.append(
        _row(
            '2 · ChromaDB search',
            str(semantic.get('status') or 'Not recorded'),
            'Perform Semantic search',
            _semantic_search_evidence(semantic),
        )
    )
    for candidate in ((trace.get('ranking') or {}).get('candidates') or [])[:10]:
        if isinstance(candidate, dict):
            rows.append(
                _row(
                    '2 · Shortlisted table',
                    'Shortlisting',
                    'Combined keyword + semantic ranking',
                    _shortlist_evidence(candidate),
                )
            )
    return rows


def _semantic_search_evidence(semantic):
    """Describe one recorded semantic (ChromaDB) search: size, matches, timing, errors."""
    indexed, elapsed = semantic.get('indexed_records'), semantic.get('elapsed_ms')
    evidence = (
        f'Semantic search ran against {indexed:,} indexed table/series records; '
        if isinstance(indexed, int)
        else 'Semantic search index size was not recorded; '
    )
    evidence += (
        f'{semantic.get("table_matches", "unknown")} table-description matches and '
        f'{semantic.get("series_matches", "unknown")} series-description matches were '
        f'returned. '
    )
    if elapsed is not None:
        evidence += f' Search duration: {elapsed} milliseconds.'
        parts = [
            f"question vector {semantic['vector_ms']} ms"
            if isinstance(semantic.get('vector_ms'), int)
            else '',
            f"both searches {semantic['search_ms']} ms"
            if isinstance(semantic.get('search_ms'), int)
            else '',
        ]
        if any(parts):
            evidence += ' (' + ', '.join(p for p in parts if p) + '.)'
    if semantic.get('reason'):
        evidence += '; fallback: ' + str(semantic['reason'])[:60]
    for kind in ('table', 'series'):
        if semantic.get(kind + '_error'):
            evidence += '; ' + kind + ' query: ' + str(semantic[kind + '_error'])[:160]
    return evidence


def _shortlist_evidence(candidate):
    """Describe one shortlisted table: rank, search methods and any series hint."""
    tid = candidate.get('table_id') or 'ID unavailable'
    methods = (
        ', '.join(_SEARCH_METHOD_NAMES.get(s, str(s)) for s in (candidate.get('sources') or []))
        or 'not recorded'
    )
    evidence = (
        f'Position {candidate.get("rank", "?")}: Official table {tid} '
        f'was shortlisted using {methods}. '
    )
    value = candidate.get('matching_series')
    if value is not None and str(value).strip():
        value_str = str(value).strip()
        # Some older indexes place a year in this field; that is not an indicator name.
        if re.fullmatch(r'\d{4}', value_str):
            evidence += (
                f'Series hint returned: {value_str} (a year, not a verified indicator ' f'name). '
            )
        else:
            evidence += (
                f'Returned series hint: {value_str[:120]} (not independently validated ' f'here). '
            )
    return evidence


def _selection_rows(tools):
    """Stage 3: which tables the analytical tools used, and why (one row per table)."""
    rows, audited, seen = [], [], set()
    for result in (tools or [])[:100]:
        if not isinstance(result, dict):
            continue
        tid = _table_id(result.get('table_id'))
        if tid and tid not in audited:
            audited.append(tid)
        audit = result.get('discovery_audit')
        if not isinstance(audit, dict) or (tid and tid in seen):
            continue
        if tid:
            seen.add(tid)
        action = str(audit.get('selection_action') or 'Approved analytical tool')[:130]
        reason = str(audit.get('discovered_via') or 'Origin not recorded')[:120]
        rows.append(
            _row(
                '3 · Select candidate',
                'Tool invoked',
                action,
                f'{tid or "Unknown table"}: {reason}.',
            )
        )
    if not rows:
        rows.append(
            _row(
                '3 · Select candidate',
                'Found' if audited else 'Not found',
                '-',
                f'Analytical tools referenced: {", ".join(audited[:8]) or "none"}.',
            )
        )
    return rows


def _queried_results(tools) -> list:
    """Tool results that returned selected series or a series summary."""
    return [
        t
        for t in (tools or [])
        if isinstance(t, dict) and ('selected_series' in t or 'series_summary' in t)
    ]


def _inspection_rows(tools, queried) -> list:
    """Stage 4: what inspecting the table reported (indicator count, match)."""
    inspected = [t for t in (tools or []) if isinstance(t, dict) and 'series' in t]
    if not (inspected or queried):
        return [
            _row(
                '4 · Inspect table',
                'Not recorded',
                'Not recorded',
                'No structured series inspection was attached',
            )
        ]
    found = inspected[0] if inspected else queried[0]
    match = str(found.get('series_match_status') or 'Not recorded')
    total = found.get('series_total_unfiltered', found.get('series_count_available'))
    return [
        _row(
            '4 · Inspect table',
            'Tool result recorded',
            '-',
            f'Table {found.get("table_id") or "unknown"}: the tool reported '
            f'{total if total is not None else "an unknown number of"} available published '
            f'indicator(s). Matching requested indicator: {match}.',
        )
    ]


def _retrieval_rows(response) -> list:
    """Stage 5: how official observations were obtained (fresh request or local copy).

    Identical events are grouped and counted. A real request URL is shown only
    if it was actually recorded; it is never rebuilt from a table ID.
    """
    from utils.retrieval_debug import retrieval_rows

    retrieved = retrieval_rows(response)
    if not (isinstance(retrieved, list) and retrieved):
        return [
            _row(
                '5 · Get published values',
                'Not recorded',
                '-',
                'No observation retrieval was attached to this answer.',
            )
        ]
    grouped = {}
    for row in retrieved[:100]:
        if isinstance(row, dict):
            key = (
                str(row.get('Table') or 'Unknown table'),
                str(row.get('Retrieval') or 'Not recorded'),
                str(row.get('Source') or 'Not recorded'),
                str(row.get('Details') or '').strip(),
                str(row.get('API URL') or '').strip(),
            )
            grouped[key] = grouped.get(key, 0) + 1
    return [_retrieval_row(*key, count) for key, count in list(grouped.items())[:20]]


def _retrieval_row(table, state, source, details, actual_url, count):
    """One stage-5 row for a group of identical retrieval events."""
    lower = (state + ' ' + source).lower()
    if 'cache' in lower or 'sqlite' in lower:
        explanation = (
            f'Previously retrieved official observations for table {table} were reused '
            f'from SQLite.'
        )
    elif 'fresh' in lower or 'network' in lower or 'api' in lower:
        explanation = f'Sage retrieved official observations for table {table}.'
    else:
        explanation = f'Retrieval for table {table}: {state}.'
    verified_url = _recorded_api_url(actual_url, table) if state == 'Fresh retrieval' else ''
    if verified_url:
        explanation += f' Actual SingStat API request: {verified_url}'
    else:
        reference = _official_page(table)
        if reference:
            explanation += f' Official SingStat table page: {reference}'
    clean_details = _cache_details(details)
    if clean_details and 'not recorded' not in clean_details.casefold():
        if not verified_url or verified_url not in clean_details:  # no repeated URL
            explanation += ' ' + clean_details[:180]
    if count > 1:
        explanation += f' Repeated {count} times in the tool trace.'
    if 'fresh' in lower and ('not recorded' in source.casefold() or 'network' in source.casefold()):
        source = 'Singstat Api'
    return _row('5 · Get published values', state, source, explanation)


def _verification_rows(tools, queried) -> list:
    """Stage 6: whether chart data were verified (not a claim about every sentence)."""
    verified = [
        t
        for t in (tools or [])
        if isinstance(t, dict)
        and t.get('success') is True
        and t.get('chart_source_verified') is True
    ]
    if verified:
        return [
            _row('6 · Calculate and verify', 'Chart data verified', '-', 'Chart inputs verified.')
        ]
    if queried:
        return [
            _row(
                '6 · Calculate and verify',
                'Published values available',
                '-',
                'The analytical tool returned selected-indicator results. This audit does not '
                'separately confirm that every number or calculation in the final written '
                'answer '
                'was checked.',
            )
        ]
    return [
        _row(
            '6 · Calculate and verify',
            'Not recorded',
            'Not recorded',
            'No structured numerical verification record was attached. This is missing audit '
            'evidence, not proof that the calculation was incorrect.',
        )
    ]
