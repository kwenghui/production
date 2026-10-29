"""Ranking several editions of the same official survey table (e.g. GHS 2015 / 2025).

SingStat publishes some survey tables more than once under the SAME title,
one table per survey edition. When several editions are candidates, they are
ordered by their published reference year: the newest first, or the year the
question names. Years come only from published text (the table's survey
designation, reference notes or footnotes), never from table IDs or from the
"data last updated" date. Unknown years are never guessed, and tables with
different titles keep their order.

Used by: utils/hybrid_discovery.py.
Tested in: tests/test_survey_editions.py.
"""

import logging
import re
import sqlite3
import threading
import time

import requests

from utils.vocabulary import REFERENCE_SURVEYS

LOG = logging.getLogger('sage.hybrid_discovery')


# Reference periods must come from published metadata/footnotes, never table IDs.
# Survey names come from the vocabulary (utils/vocabulary.py), like every subject word.
_SURVEY_REFERENCE = re.compile(
    r"\b(?:" + "|".join(pattern for pattern, _name in REFERENCE_SURVEYS) + r")"
    r"\s*(?:\([^)]{1,12}\)\s*)?((?:19|20)\d{2})\b",
    re.I,
)
_EXPLICIT_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def _published_reference_year(text: str) -> int | None:
    """Read only explicit survey-year statements from official metadata."""
    match = _SURVEY_REFERENCE.search(str(text or ''))
    return int(match.group(1)) if match else None


def _survey_years_from_catalog(con, table_ids) -> dict[str, int]:
    """Find published reference years in existing SQLite table and series metadata."""
    ids = sorted({str(t) for t in table_ids if t})
    years = {}
    if not ids:
        return years
    try:
        for start in range(0, len(ids), 300):
            batch = ids[start : start + 300]
            marks = ','.join('?' for _ in batch)
            for tid, description in con.execute(
                f'SELECT table_id, description FROM kb_tables WHERE table_id IN ({marks})', batch
            ):
                year = _published_reference_year(description)
                if year is not None:
                    years[tid] = year
            for tid, footnote, description in con.execute(
                f'SELECT table_id, footnote, description FROM kb_series WHERE table_id IN ({marks})',
                batch,
            ):
                for value in (footnote, description):
                    year = _published_reference_year(value)
                    if year is not None:
                        years[tid] = max(years.get(tid, year), year)
    except sqlite3.Error as exc:
        LOG.debug('Published survey-year lookup unavailable: %s', type(exc).__name__)
    return years


# Only compare different editions of the SAME official table title. Never
# contact SingStat for every search hit; metadata requests are bounded.
_OFFICIAL_YEAR_LOCK = threading.Lock()
#: How long a looked-up record is reused: a found record for a day, a failed
#: lookup only for ten minutes (so a short outage is not remembered all day).
_FOUND_SECONDS, _FAILED_SECONDS = 24 * 3600, 600


def _official_publication_metadata(payload, table_id: str) -> dict:
    """Separate survey reference year from data revision date in SingStat metadata.

    The reference year is the statistical period; dataLastUpdated is the
    publication's revision date. Neither is inferred from the other.
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get('Data')
    record = data.get('records') if isinstance(data, dict) else None
    if not isinstance(record, dict):
        return {}
    if str(record.get('id') or '').strip().upper() != str(table_id).upper():
        return {}
    designation = str(record.get('tableType') or '').strip()
    year = _published_reference_year(designation)
    # Explicit survey designation takes precedence. If missing, check other
    # textual reference descriptions, but never dataLastUpdated/dateGenerated.
    if year is None:
        for key in ('referencePeriod', 'reference', 'description', 'footnote', 'notes'):
            year = _published_reference_year(record.get(key))
            if year is not None:
                break
    updated = str(record.get('dataLastUpdated') or '').strip()
    # Do not interpret the update date as a reference year.
    return {
        'reference_year': year,
        'reference_source': 'Data.records.tableType'
        if _published_reference_year(designation) is not None
        else ('Data.records reference text' if year is not None else None),
        'survey_designation': designation or None,
        'data_last_updated': updated or None,
        'last_updated_source': 'Data.records.dataLastUpdated' if updated else None,
    }


def _published_year_in_official_payload(payload, table_id: str) -> int | None:
    """Compatibility helper for existing tests and callers."""
    return _official_publication_metadata(payload, table_id).get('reference_year')


_OFFICIAL_METADATA_CACHE: dict[str, dict] = {}
_CACHED_AT: dict[str, tuple[float, bool]] = {}


def _official_publication_details(table_id: str) -> dict:
    """Bounded official lookup through the internet door, cached; fail closed ({}).

    Uses official_source.fetch_official_metadata (official host only, no
    redirects, size and time limits) rather than the knowledge-base builder's
    client, which never runs while a question is answered.
    """
    now = time.monotonic()
    with _OFFICIAL_YEAR_LOCK:
        cached = _CACHED_AT.get(table_id)
        if cached and now - cached[0] < (_FOUND_SECONDS if cached[1] else _FAILED_SECONDS):
            return dict(_OFFICIAL_METADATA_CACHE.get(table_id) or {})
    result = {}
    try:
        from utils.official_source import fetch_official_metadata

        result = _official_publication_metadata(fetch_official_metadata(table_id), table_id)
    except (requests.RequestException, ValueError, OSError) as exc:
        LOG.info(
            'Official publication metadata unavailable for %s: %s', table_id, type(exc).__name__
        )
    with _OFFICIAL_YEAR_LOCK:
        _OFFICIAL_METADATA_CACHE[table_id] = dict(result)
        _CACHED_AT[table_id] = (now, bool(result))
    return dict(result)


def _official_reference_year(table_id: str) -> int | None:
    """The survey reference year in SingStat's metadata for one table, or None."""
    return _official_publication_details(table_id).get('reference_year')


def _complete_edition_years(ranking, local_years):
    """Enrich ONLY same-title ambiguous editions (max four official requests)."""
    groups = {}
    for entry in ranking:
        title = ' '.join(str((entry.get('best') or {}).get('table_title') or '').casefold().split())
        if title:
            groups.setdefault(title, []).append(entry['table_id'])
    years = dict(local_years)
    attempts = 0
    for ids in groups.values():
        if len(ids) < 2:
            continue
        for tid in ids:
            if tid not in years and attempts < 4:
                attempts += 1
                year = _official_reference_year(tid)
                if year is not None:
                    years[tid] = year
    return years


def _rank_survey_editions(ranking: list[dict], question: str, years: dict[str, int]):
    """Reorder equivalent editions ONLY when their reference years are verified.

    Unrelated subjects keep their relative order. Unknown-year editions are not
    guessed to be old or new. Explicit years take precedence over latest.
    """
    if not ranking or not years:
        return ranking
    requested = [int(y) for y in _EXPLICIT_YEAR.findall(question)]
    explicit = requested[0] if len(set(requested)) == 1 else None
    groups = {}
    for entry in ranking:
        best = entry.get('best') or {}
        title = ' '.join(str(best.get('table_title') or '').casefold().split())
        if title:
            groups.setdefault(title, []).append(entry)
    for group in groups.values():
        verified = [e for e in group if e['table_id'] in years]
        if len(verified) < 2:
            continue
        # Only reorder the slots occupied by verified editions of the SAME title.
        slots = [i for i, e in enumerate(ranking) if e in verified]
        editions = [ranking[i] for i in slots]
        if explicit is not None:
            editions.sort(
                key=lambda e: (years[e['table_id']] != explicit, -e['score'], e['table_id'])
            )
        else:
            editions.sort(key=lambda e: (-years[e['table_id']], -e['score'], e['table_id']))
        for slot, edition in zip(slots, editions):
            ranking[slot] = edition
    return ranking


def survey_edition_metadata() -> dict:
    """A copy of the metadata records looked up so far ({table ID: details}), for the trace."""
    with _OFFICIAL_YEAR_LOCK:
        return {tid: dict(details) for tid, details in _OFFICIAL_METADATA_CACHE.items()}
