"""Request-scoped retrieval provenance; no question text, bodies or credentials.

Used by: utils/core_secure.py, utils/official_source.py.
Tested in: tests/test_data_layer.py, tests/test_sources_and_catalogue.py.
"""

from __future__ import annotations
from contextvars import ContextVar
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

_EVENTS = ContextVar('sage_retrieval_events', default=None)
_ALLOWED_QUERY = {'limit', 'offset', 'sortBy', 'seriesNo', 'rowNo', 'page', 'series'}


def begin():
    """Start a new, empty retrieval trace for this context; returns a reset token."""
    return _EVENTS.set(())


def end(token) -> None:
    """End the trace started by begin() (pass its token)."""
    _EVENTS.reset(token)


def _safe_url(url):
    """Retain actual tabledata URLs only; omit metadata and credentials."""
    try:
        parts = urlsplit(str(url))
        if (
            parts.scheme != 'https'
            or parts.hostname != 'tablebuilder.singstat.gov.sg'
            or parts.username
            or parts.password
            or parts.port not in (None, 443)
        ):
            return None
        if not parts.path.startswith('/api/table/tabledata/'):
            return None
        query = urlencode(
            [
                (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k in _ALLOWED_QUERY
            ]
        )
        return urlunsplit(('https', parts.hostname, parts.path, query, ''))
    except (ValueError, TypeError):
        return None


def record_http(url) -> None:
    """Record a completed tabledata HTTPS request; ignore metadata requests."""
    current = _EVENTS.get()
    if current is None:
        return
    safe = _safe_url(url)
    if safe:
        _EVENTS.set(current + (('http', safe, None),))
    elif str(url).startswith('https://data.gov.sg/'):
        # No query strings from mirrors, which might contain private tokens.
        _EVENTS.set(current + (('mirror_http', 'data.gov.sg', None),))


def _record_cache_event(table_id, status, *, cache_age_seconds=None) -> None:
    """Note that a table was served from the cache or fetched fresh (inside a trace)."""
    if status not in {'cached', 'fresh'}:
        return
    current = _EVENTS.get()
    if current is None:
        return
    age = None
    if cache_age_seconds is not None:
        try:
            age = max(0, round(float(cache_age_seconds) / 60, 1))
        except (ValueError, TypeError, OverflowError):
            pass
    _EVENTS.set(current + (('cache', (str(table_id)[:32], status), age),))


def _base_summary() -> dict:
    """Summary of the traced cache events: status, count and table IDs."""
    events = _EVENTS.get() or ()
    data = [entry for entry in events if entry[0] == 'cache']
    if not data:
        return {}
    statuses = {entry[1][1] for entry in data}
    classification = (
        'cached' if statuses == {'cached'} else ('fresh' if statuses == {'fresh'} else 'mixed')
    )
    ids = sorted({entry[1][0] for entry in data if entry[1][0]})[:15]
    urls = list(dict.fromkeys(entry[1] for entry in events if entry[0] == 'http'))
    has_mirror = any(entry[0] == 'mirror_http' for entry in events)
    has_official_api = bool(urls)
    # Never call a network request 'SingStat API' unless its HTTPS URL is recorded.
    origin = (
        'SingStat API + SQLite'
        if classification == 'mixed' and has_official_api
        else 'SingStat API'
        if has_official_api and classification == 'fresh'
        else 'Official mirror (data.gov.sg)'
        if has_mirror and classification == 'fresh'
        else 'SQLite cache'
        if classification == 'cached'
        else 'Mixed retrieval'
        if classification == 'mixed'
        else 'Network endpoint not recorded'
    )
    details = '; '.join(urls[:3]) if urls else ''
    if len(urls) > 3:
        details += f' (+{len(urls)-3} more HTTPS requests)'
    if not details and has_mirror:
        details = 'HTTPS data.gov.sg (query parameters hidden)'
    if classification == 'cached':
        ages = [entry[2] for entry in data if entry[2] is not None]
        details = 'Verified SQLite snapshot reused' + (
            f'; cache age {max(ages):g} min' if ages else ''
        )
    elif not details:
        details = 'Actual HTTPS endpoint not recorded'
    return {
        'cache_status': classification,
        'cache_events': len(data),
        'cache_table_ids': ids,
        'retrieval_channel': origin,
        'retrieval_details': details,
        'official_api_requests': len(urls),
    }


def with_trace(operation):
    """Run ``operation`` while tracing retrieval; attach the trace summary to a dict result."""
    token = begin()
    try:
        result = operation()
        if isinstance(result, dict):
            info = summary()
            if info:
                result = dict(result)
                result.update(info)
        return result
    finally:
        end(token)


def record(table_id, status, *, url=None, cache_age_seconds=None) -> None:
    """Record a cache/fresh retrieval, plus the official tabledata URL when fresh."""
    _record_cache_event(table_id, status, cache_age_seconds=cache_age_seconds)
    if status != 'fresh' or url is None:
        return
    # Retain *only* completed official tabledata requests; _safe_url also strips
    # every query argument except paging/series selectors.
    try:
        parts = urlsplit(str(url))
        if (
            parts.scheme != 'https'
            or parts.hostname != 'tablebuilder.singstat.gov.sg'
            or parts.username
            or parts.password
            or parts.fragment
            or parts.port not in (None, 443)
            or parts.path.rstrip('/').lower() != '/api/table/tabledata/' + str(table_id).lower()
            or any(
                k.lower() not in {'limit', 'offset'}
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
            )
        ):
            return
    except (ValueError, TypeError):
        return
    safe = _safe_url(url)
    if not safe:
        return
    existing = _EVENTS.get()
    if existing is None:
        return
    # Avoid recording the same URL twice when the HTTP transport itself has
    # already called record_http() earlier in the same tool request.
    if not any(
        isinstance(event, tuple) and len(event) >= 2 and event[0] == 'http' and event[1] == safe
        for event in existing
    ):
        record_http(url)


def summary():
    """Provenance summary, including the official URLs actually fetched."""
    original = _base_summary()
    if not isinstance(original, dict) or original.get('cache_status') not in {'fresh', 'mixed'}:
        return original
    events = _EVENTS.get() or ()
    urls = []
    for event in events:
        if not isinstance(event, tuple) or len(event) < 2 or event[0] != 'http':
            continue
        safe = _safe_url(event[1])
        if safe and safe not in urls:
            urls.append(safe)
    if not urls:
        return original
    enriched = dict(original)
    enriched['retrieval_api_urls'] = urls[:12]
    return enriched
