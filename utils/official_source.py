"""The ONLY door to the internet, and the local copy of official tables.

safe_http_get fetches official SingStat data and nothing else:

* Allowed host only (ALLOWED_OUTBOUND_HOSTS), HTTPS, bounded size and time.
* Local first: a saved copy (utils/store.py, via get_local_store) is used
  while fresh (CACHE_DURATION_MINUTES). A slightly stale copy is served at
  once and refreshed in the background (STALE_WHILE_REVALIDATE).
* SAGE_OFFLINE_ONLY=1 never goes online.
* A table that keeps failing is skipped for a cool-down period
  (_blocked_table_fetch), so one broken table cannot exhaust a question's
  budget.

local_store_status / local_store_verify / backup_local_store are operator
commands for the saved copy (see utils/core_secure.main).

Used by: ui/answer_view.py, utils/core_secure.py, utils/rag_engine.py,
utils/survey_editions.py, utils/table_tools.py.
Tested in: tests/test_data_layer.py, tests/test_polite_retry.py,
tests/test_sources_and_catalogue.py, tests/test_survey_editions.py.
"""

from pathlib import Path
from urllib.parse import urlparse
import json
import logging
import threading
import time

import requests

from utils import retrieval_trace
from utils.session_security import bounded_int_env as _bounded_int_env
import utils.store
from utils.table_download import extract_table_id
from utils.settings import env_flag


APP_DIR = Path(__file__).resolve().parent.parent
# CACHE_DURATION is minutes, 1 to 525600 (one year); default 1440 (one day).
# Fail fast on malformed configuration instead of silently serving stale data.
CACHE_DURATION_MINUTES = _bounded_int_env("CACHE_DURATION", 1440, 1, 60 * 24 * 365)
CACHE_DURATION_SECONDS = CACHE_DURATION_MINUTES * 60
# Persistent local-first store. A missing table is downloaded ONCE, its complete
# pages validated and committed to SQLite, then reused offline and on restarts.
# Existing snapshots are archived, not deleted, during explicit synchronisation.
SAGE_STORE_ENABLED = env_flag("SAGE_STORE_ENABLED", True)
SAGE_OFFLINE_ONLY = env_flag("SAGE_OFFLINE_ONLY", False)
_LOCAL_STORE = None
# Do not repeatedly re-download a table whose pagination/HTTP access has
# already failed during the same user's short analytical interaction. This
# is a transient circuit breaker, not a data cache and never deletes data.
_TABLE_FETCH_FAILURES = {}
TABLE_FAILURE_COOLDOWN_SECONDS = 120


def _blocked_table_fetch(table_id):
    """Return a recent download failure, if any, without retrying the API."""
    saved = _TABLE_FETCH_FAILURES.get(str(table_id).upper())
    if saved is None:
        return None
    when, explanation = saved
    if time.monotonic() - when < TABLE_FAILURE_COOLDOWN_SECONDS:
        return explanation
    _TABLE_FETCH_FAILURES.pop(str(table_id).upper(), None)
    return None


_LAZY_INIT_LOCK = threading.RLock()  # first-use initialisers run once, even under concurrency


def get_local_store():
    """Open the shared SQLite cache lazily; never require a network at import."""
    global _LOCAL_STORE
    if _LOCAL_STORE is None:
        with _LAZY_INIT_LOCK:
            if _LOCAL_STORE is None:
                _LOCAL_STORE = utils.store.LocalStore(utils.store.default_db_path(APP_DIR))
    return _LOCAL_STORE


def local_store_status():
    """Metadata for the UI startup indicator and status panel."""
    return get_local_store().status()


def local_store_verify():
    """Re-check all active snapshots AND database integrity before backup."""
    return get_local_store().verify_all()


def backup_local_store(destination):
    """Create an online SQLite backup and independently validate it."""
    return get_local_store().backup(destination)


MAX_HTTP_RESPONSE_BYTES = 5 * 1024 * 1024
ALLOWED_OUTBOUND_HOSTS = {"tablebuilder.singstat.gov.sg"}
logger = logging.getLogger("sage")
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 Sage"}
# One pooled HTTPS session for every official request: reusing TLS connections
# avoids a fresh handshake per page. Retries only on transient gateway errors.
_HTTP_SESSION_LOCK = threading.Lock()
_HTTP_SESSION = None
HTTP_TOTAL_DEADLINE_SECONDS = 45


def _http_session():
    """The shared HTTP session (connection pooling and bounded retries), created once."""
    global _HTTP_SESSION
    with _HTTP_SESSION_LOCK:
        if _HTTP_SESSION is None:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            session = requests.Session()
            retry = Retry(
                total=2,
                connect=2,
                read=1,
                backoff_factor=0.5,
                status_forcelist=(502, 503, 504),
                allowed_methods=frozenset({"GET"}),
                raise_on_status=False,
            )
            session.mount(
                "https://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=retry)
            )
            _HTTP_SESSION = session
        return _HTTP_SESSION


#: Answers that mean "busy, try again shortly" (SingStat enforces API rate limits).
_RETRY_STATUSES = (429, 503)
_MAX_RETRIES, _MAX_WAIT_SECONDS = 2, 8


def _get_with_polite_retry(url, request_headers, read_timeout):
    """GET with up to two polite retries when SingStat says it is busy (HTTP 429 or 503).

    A large table is downloaded in several pages; one rate-limited page used to end
    the whole download as "incomplete". The wait follows the server's Retry-After
    (capped at 8 seconds), so a question is delayed by seconds, never minutes.
    """
    for attempt in range(_MAX_RETRIES + 1):
        response = _http_session().get(
            url,
            headers=request_headers,
            timeout=(5, read_timeout),
            allow_redirects=False,
            stream=True,
        )
        if response.status_code not in _RETRY_STATUSES or attempt == _MAX_RETRIES:
            return response
        try:
            wait = float(response.headers.get("Retry-After") or 0)
        except (TypeError, ValueError):
            wait = 0.0
        wait = min(_MAX_WAIT_SECONDS, max(1.0 + attempt, wait))
        response.close()
        logger.info("SingStat busy (HTTP %s); retrying in %.0f s", response.status_code, wait)
        time.sleep(wait)
    return response


def _live_http_get(url, *, headers=None, timeout: int = 30):
    """Perform a size-bounded HTTPS GET from approved official hosts only.

    This function is also passed to the SQLite downloader as a *callback*.
    That callback invokes it with only ``url``; it does not pass headers.
    Always add the required official API headers here, not merely at the
    higher-level ``safe_http_get`` call sites.  SingStat documents HTTP 403
    responses when User-Agent and Accept are omitted.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_OUTBOUND_HOSTS:
        raise ValueError("Blocked outbound URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Blocked malformed outbound URL.")

    # Requests' default User-Agent is not a substitute for the explicit
    # headers required by SingStat's developer API.  Keep approved callers'
    # optional headers without allowing an accidental empty/missing required
    # header to break the SQLite background/first-use downloader again.
    request_headers = dict(HEADERS)
    if headers:
        request_headers.update(headers)
    for required in ("Accept", "User-Agent"):
        if not any(
            key.casefold() == required.casefold() and str(value).strip()
            for key, value in request_headers.items()
        ):
            request_headers[required] = HEADERS[required]

    started = time.monotonic()
    # (connect, read) timeouts: fail fast when the host is unreachable.
    read_timeout = timeout if isinstance(timeout, (int, float)) else 30
    response = _get_with_polite_retry(url, request_headers, read_timeout)
    if response.status_code == 403 and parsed.hostname == "tablebuilder.singstat.gov.sg":
        response.close()
        raise requests.HTTPError(
            "SingStat returned HTTP 403 despite the required Accept and "
            "User-Agent headers. Check access from your network/proxy, "
            "review the official API terms or contact SingStat support. "
            "An existing verified local snapshot has not been deleted.",
            response=response,
        )
    response.raise_for_status()

    length = response.headers.get("Content-Length")
    try:
        declared = int(length) if length else 0
    except ValueError:
        response.close()
        raise ValueError("Government data response had an invalid Content-Length.")
    if declared > MAX_HTTP_RESPONSE_BYTES:
        response.close()
        raise ValueError("Government data response exceeded the configured size limit.")

    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        # A per-read timeout alone lets a slow-drip upstream hold this thread for minutes.
        if time.monotonic() - started > HTTP_TOTAL_DEADLINE_SECONDS:
            response.close()
            raise requests.Timeout("Government data response exceeded the total time limit.")
        if total > MAX_HTTP_RESPONSE_BYTES:
            response.close()
            raise ValueError("Government data response exceeded the configured size limit.")
        chunks.append(chunk)
    response._content = b"".join(chunks)
    response._content_consumed = True
    # SAGE42_HTTP_PROVENANCE: only log successfully fetched URLs.
    # Audit telemetry must never prevent an otherwise valid data response.
    _record_completed_http = getattr(retrieval_trace, 'record_http', None)
    if callable(_record_completed_http):
        try:
            _record_completed_http(getattr(response, 'url', url))
        except Exception as error:  # noqa: BLE001 - telemetry must never break a fetch
            logger.debug("HTTP completion telemetry skipped: %s", type(error).__name__)
    return response


def _local_response(payload, url):
    """Provide the subset of requests.Response used by existing tool parsers."""
    result = requests.Response()
    result.status_code = 200
    result.url = url
    result.headers["Content-Type"] = "application/json"
    result._content = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    result._content_consumed = True
    return result


STALE_WHILE_REVALIDATE = env_flag("SAGE_STALE_WHILE_REVALIDATE", True)
# Serve a stale snapshot at most this many cache periods old (default 7 days at 1-day TTL).
STALE_MAX_FACTOR = _bounded_int_env("SAGE_STALE_MAX_FACTOR", 7, 1, 365)
_REFRESH_LOCK = threading.Lock()
_REFRESHING = set()
_REFRESH_EXECUTOR = None


def _background_refresh(table_id) -> None:
    """Refresh one stale table in the background; failures are logged, never raised."""
    try:
        get_local_store().refresh_table_if_expired(
            table_id, CACHE_DURATION_SECONDS, _live_http_get, fetch_metadata=_live_http_get
        )
        logger.info("Background refresh finished for %s", table_id)
    except Exception as error:  # noqa: BLE001 - background work; the stale snapshot stays active
        logger.warning("Background refresh failed for %s: %s", table_id, type(error).__name__)
    finally:
        with _REFRESH_LOCK:
            _REFRESHING.discard(table_id)


def _schedule_background_refresh(table_id) -> None:
    """Queue at most one refresh per table; at most two downloads run at once."""
    global _REFRESH_EXECUTOR
    with _REFRESH_LOCK:
        if table_id in _REFRESHING:
            return
        _REFRESHING.add(table_id)
        if _REFRESH_EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor

            _REFRESH_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sage-refresh")
    _REFRESH_EXECUTOR.submit(_background_refresh, table_id)


def safe_http_get(url, *, headers=None, timeout: int = 30):
    """Single local-first entry point shared by every approved analytical tool.

    Full SingStat table requests use COMPLETE verified local snapshots; every
    table is fetched, cached and refreshed the same way.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_OUTBOUND_HOSTS:
        raise ValueError("Blocked outbound URL.")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Blocked malformed outbound URL.")
    if not SAGE_STORE_ENABLED:
        if SAGE_OFFLINE_ONLY:
            raise ValueError("The SQLite store is disabled while offline mode is enabled.")
        response = _live_http_get(url, headers=headers, timeout=timeout)
        retrieval_trace.record(
            extract_table_id(url) or 'official_api', 'fresh', url=getattr(response, 'url', url)
        )
        return response
    table_id = extract_table_id(url)
    store = get_local_store()
    if table_id is not None:
        current = store.get_table(table_id)
        if current is not None:
            age = store.table_cache_age_seconds(table_id)
            if SAGE_OFFLINE_ONLY or (age is not None and age <= CACHE_DURATION_SECONDS):
                _TABLE_FETCH_FAILURES.pop(table_id, None)
                retrieval_trace.record(table_id, 'cached')
                return _local_response(current, url)
            # Stale but recent enough: answer now from the verified snapshot and refresh
            # in the background (stale-while-revalidate), so the visitor never waits for
            # a full re-download. Very old snapshots still refresh before answering.
            if (
                STALE_WHILE_REVALIDATE
                and age is not None
                and age <= CACHE_DURATION_SECONDS * STALE_MAX_FACTOR
            ):
                _schedule_background_refresh(table_id)
                _TABLE_FETCH_FAILURES.pop(table_id, None)
                retrieval_trace.record(table_id, 'cached', cache_age_seconds=age)
                return _local_response(current, url)
            # Stale snapshot: refresh using the existing complete-table validator.
            # sync_one only activates a newly verified and complete snapshot;
            # the previously active snapshot survives any failed fetch.
            fetched_refresh = [False]
            downloaded_refresh_urls = []

            def _refresh_download(download_url, **kwargs):
                """Download during a refresh and remember that, and which URL, was fetched."""
                result = _live_http_get(download_url, **kwargs)
                fetched_refresh[0] = True
                downloaded_refresh_urls.append(getattr(result, "url", download_url))
                return result

            store.refresh_table_if_expired(
                table_id,
                CACHE_DURATION_SECONDS,
                _refresh_download,
                fetch_metadata=_live_http_get,
            )
            refreshed = store.get_table(table_id)
            if refreshed is None:
                raise ValueError(f"{table_id}: refreshed snapshot unavailable")
            _TABLE_FETCH_FAILURES.pop(table_id, None)
            retrieval_trace.record(
                table_id,
                'fresh' if fetched_refresh[0] else 'cached',
                url=downloaded_refresh_urls[-1] if downloaded_refresh_urls else None,
            )
            return _local_response(refreshed, url)
        previous_failure = _blocked_table_fetch(table_id)
        if previous_failure is not None:
            raise utils.store.UnverifiablePaginationError(
                f"{table_id}: this API retrieval already failed recently. "
                "Repeated network calls have been paused. " + previous_failure
            )
        fetched = [False]
        downloaded_urls = []

        def _tracked_table_download(download_url, **kwargs):
            """Download through the allowed path and remember that, and which URL, was fetched."""
            response = _live_http_get(download_url, **kwargs)
            fetched[0] = True
            downloaded_urls.append(getattr(response, "url", download_url))
            return response

        try:
            payload = store.get_or_fetch(
                table_id,
                _tracked_table_download,
                offline=SAGE_OFFLINE_ONLY,
                fetch_metadata=_live_http_get,
            )
        except (requests.RequestException, ValueError, OSError) as error:
            if not SAGE_OFFLINE_ONLY:
                _TABLE_FETCH_FAILURES[table_id] = (
                    time.monotonic(),
                    f"{type(error).__name__}: {str(error)[:260]}",
                )
            raise
        _TABLE_FETCH_FAILURES.pop(table_id, None)
        # The callback is called only for an actual completed table-data fetch.
        # A concurrent worker may have already populated the local store.
        retrieval_trace.record(
            table_id,
            'fresh' if fetched[0] else 'cached',
            url=downloaded_urls[-1] if downloaded_urls else None,
        )
        return _local_response(payload, url)
    if SAGE_OFFLINE_ONLY:
        raise ValueError("This official endpoint is not available from the local store.")
    response = _live_http_get(url, headers=headers, timeout=timeout)
    retrieval_trace.record(
        extract_table_id(url) or 'official_api', 'fresh', url=getattr(response, 'url', url)
    )
    return response


_METADATA_URL = "https://tablebuilder.singstat.gov.sg/api/table/metadata/{table_id}"


def fetch_official_metadata(table_id: str, *, timeout: int = 6) -> dict:
    """SingStat's metadata record for one table (title, survey designation, last updated).

    Goes through the same door as table data: official host only, HTTPS, no
    redirects, size and time limits. Raises ValueError for a malformed ID or
    response, and requests exceptions for network failures; callers decide.
    """
    from utils.singstat_api import safe_id

    response = _live_http_get(_METADATA_URL.format(table_id=safe_id(table_id)), timeout=timeout)
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Official metadata response was not an object.")
    return payload


def data_last_updated(table_id: str) -> str | None:
    """SingStat's "Data last updated" date of the verified local copy, as "30 Jun 2026".

    Read from the stored official payload (Data.dataLastUpdated); None when the
    table is not stored or the date is missing. Display only: it is never given
    to the AI, so it can never be mistaken for a statistic.
    """
    import sqlite3

    # Never create the cache just to read a date (display code must not write files).
    if not SAGE_STORE_ENABLED or (
        _LOCAL_STORE is None and not Path(utils.store.default_db_path(APP_DIR)).exists()
    ):
        return None
    try:
        payload = get_local_store().get_table(str(table_id))
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error) as error:
        logger.debug("Last-updated date unavailable for %s: %s", table_id, type(error).__name__)
        return None
    data = payload.get("Data") if isinstance(payload, dict) else None
    raw = str((data or {}).get("dataLastUpdated") or "").strip() if isinstance(data, dict) else ""
    if not raw:
        return None
    from datetime import datetime

    for pattern in ("%d/%m/%Y", "%Y-%m-%d", "%d %b %Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], pattern).strftime("%d %b %Y").lstrip("0")
        except ValueError:
            continue
    return raw[:40]
