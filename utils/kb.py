"""Persistent SingStat knowledge base: SQLite FTS5 + Chroma semantic search.

Official data and source metadata live in SQLite. Chroma is a disposable index.
No unverified statistical observations are ever fabricated by this module.

Used by: utils/hybrid_discovery.py, utils/kb_discovery_bridge.py,
tools/characterise_hybrid_retrieve.py, tools/characterise_kb_inspect.py,
tools/test_search.py, tools/test_search_trace.py.
Tested in: tests/test_discovery_and_ingest.py,
tests/test_discovery_ranking.py, tests/test_semantic_search_key.py,
tests/test_sources_and_catalogue.py, tests/test_storage_and_tools.py,
tests/test_ui_helpers.py.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The official-API client lives in utils/singstat_api.py (shared with kb_structured_ingest).
from utils.singstat_api import request_json, safe_id
from utils.settings import env_opt_in


def timestamp() -> str:
    """Current UTC time as an ISO-8601 string (seconds)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the knowledge-base SQLite file with safe settings and schema."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(file), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS kb_tables (
      table_id TEXT PRIMARY KEY,
      title TEXT NOT NULL,
      theme TEXT NOT NULL DEFAULT '',
      subject TEXT NOT NULL DEFAULT '',
      frequency TEXT NOT NULL DEFAULT '',
      source_agency TEXT NOT NULL DEFAULT '',
      source_updated TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      source_url TEXT NOT NULL,
      metadata_status TEXT NOT NULL DEFAULT 'catalogue_only',
      series_status TEXT NOT NULL DEFAULT 'not_started',
      series_count INTEGER,
      last_ingested TEXT NOT NULL DEFAULT '',
      last_error TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS kb_series (
      table_id TEXT NOT NULL REFERENCES kb_tables(table_id) ON DELETE CASCADE,
      series_id TEXT NOT NULL,
      title TEXT NOT NULL,
      unit TEXT NOT NULL DEFAULT '',
      footnote TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      observed_periods INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY(table_id, series_id)
    );
    CREATE TABLE IF NOT EXISTS kb_observations (
      table_id TEXT NOT NULL,
      series_id TEXT NOT NULL,
      period TEXT NOT NULL,
      raw_value TEXT NOT NULL,
      numeric_value TEXT,
      fetched_at TEXT NOT NULL,
      PRIMARY KEY(table_id,series_id,period),
      FOREIGN KEY(table_id, series_id) REFERENCES kb_series(table_id,series_id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS kb_ingestion (
      table_id TEXT PRIMARY KEY,
      attempt_count INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pending',
      detail TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS kb_vector_sync (
      doc_id TEXT PRIMARY KEY,
      content_sha256 TEXT NOT NULL,
      indexed_at TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(doc_id UNINDEXED, text, tokenize='unicode61');
    """)
    con.commit()
    return con


def doc_text(row: sqlite3.Row | dict[str, Any], *, series: bool = False) -> str:
    """The searchable text of one table (or, with ``series=True``, one series) row."""
    d = dict(row)
    if series:
        return (
            f"SingStat {d['table_id']} | {d.get('table_title', '')} | series {d['series_id']}: "
            f"{d['title']} | unit {d.get('unit', '')} | {d.get('description', '')} | "
            f"{d.get('footnote', '')}"
        )
    return (
        f"SingStat {d['table_id']}: {d['title']} | {d.get('theme', '')} | "
        f"{d.get('subject', '')} | {d.get('frequency', '')} | "
        f"{d.get('description', '')} | {d.get('source_agency', '')}"
    )


def index_fts(con: sqlite3.Connection, doc_id: str, content: str) -> None:
    """Replace the full-text-search entry for ``doc_id`` (text capped at 15,000 chars)."""
    con.execute("DELETE FROM kb_fts WHERE doc_id=?", (doc_id,))
    con.execute("INSERT INTO kb_fts(doc_id,text) VALUES (?,?)", (doc_id, content[:15000]))


def upsert_table(con: sqlite3.Connection, table_id: str, title: str, **kw: str) -> None:
    """Insert or update one table's official metadata (title is required)."""
    tid = safe_id(table_id)
    if not title or not str(title).strip():
        raise ValueError("Missing official title")
    permitted = ('theme', 'subject', 'frequency', 'source_agency', 'source_updated', 'description')
    values = [str(kw.get(k) or '') for k in permitted]
    con.execute(
        """INSERT INTO kb_tables(table_id,title,theme,subject,frequency,source_agency,source_updated,description,source_url)
       VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(table_id) DO UPDATE SET
       title=excluded.title,theme=CASE WHEN excluded.theme!='' THEN excluded.theme ELSE kb_tables.theme END,
       subject=CASE WHEN excluded.subject!='' THEN excluded.subject ELSE kb_tables.subject END,
       frequency=CASE WHEN excluded.frequency!='' THEN excluded.frequency ELSE kb_tables.frequency END,
       source_agency=CASE WHEN excluded.source_agency!='' THEN excluded.source_agency ELSE kb_tables.source_agency END,
       source_updated=CASE WHEN excluded.source_updated!='' THEN excluded.source_updated ELSE kb_tables.source_updated END,
       description=CASE WHEN excluded.description!='' THEN excluded.description ELSE kb_tables.description END,
       source_url=excluded.source_url""",
        (
            tid,
            str(title),
            *values,
            (
                f"https://tablebuilder.singstat.gov.sg/api/table/tabledata/{tid}"
                if tid.isdigit()
                else _official_page(tid)
            ),
        ),
    )
    row = con.execute("SELECT * FROM kb_tables WHERE table_id=?", (tid,)).fetchone()
    index_fts(con, 't:' + tid, doc_text(row))


def upsert_series(
    con: sqlite3.Connection,
    tid: str,
    series_id: str,
    name: str,
    unit: str = '',
    footnote: str = '',
    description: str = '',
) -> None:
    """Insert or update one series of a table (ID and label are required)."""
    if not str(series_id).strip() or not str(name).strip():
        raise ValueError("Series id and label must not be blank")
    con.execute(
        """INSERT INTO kb_series(table_id,series_id,title,unit,footnote,description)
      VALUES(?,?,?,?,?,?) ON CONFLICT(table_id,series_id) DO UPDATE SET
      title=excluded.title,unit=excluded.unit,footnote=excluded.footnote,
      description=excluded.description""",
        (tid, str(series_id), str(name), unit, footnote, description),
    )
    parent = con.execute("SELECT title FROM kb_tables WHERE table_id=?", (tid,)).fetchone()
    index_fts(
        con,
        's:' + tid + ':' + str(series_id),
        doc_text(
            {
                'table_id': tid,
                'table_title': parent['title'],
                'series_id': series_id,
                'title': name,
                'unit': unit,
                'footnote': footnote,
                'description': description,
            },
            series=True,
        ),
    )


def parse_numeric(value: Any) -> str | None:
    """The cell as an exact decimal string, or None if it is not a plain number."""
    raw = str('' if value is None else value).strip().replace(',', '')
    if re.fullmatch(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)', raw):
        return raw  # preserve exact decimal representation
    return None


def import_catalogue_csv(con: sqlite3.Connection, filename: str | Path) -> dict:
    """Import official or locally exported catalogue metadata without marking as verified.

    Required CSV columns: table_id (or id), title. Optional theme, subject,
    frequency, source_agency, source_updated, description.
    No user-entered values are treated as official observations.
    """
    count = 0
    skipped = 0
    with open(filename, 'r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        if (
            not reader.fieldnames
            or not ({'table_id', 'id'} & set(reader.fieldnames))
            or 'title' not in reader.fieldnames
        ):
            raise ValueError('Catalogue CSV requires table_id/id and title columns')
        with con:
            for row in reader:
                try:
                    upsert_table(
                        con,
                        row.get('table_id') or row.get('id'),
                        row.get('title'),
                        **{
                            k: row.get(k) or ''
                            for k in (
                                'theme',
                                'subject',
                                'frequency',
                                'source_agency',
                                'source_updated',
                                'description',
                            )
                        },
                    )
                    count += 1
                except ValueError:
                    skipped += 1
    return {
        'imported': count,
        'skipped': skipped,
        'verification': 'catalogue_only until official API inspection',
    }


def discover_keyword(con: sqlite3.Connection, keyword: str, *, fetch=request_json) -> int:
    """Ingest *reported* records, never claim keyword coverage is exhaustive."""
    if not keyword or len(keyword) > 100:
        raise ValueError('Keyword required, maximum 100 chars')
    obj = fetch('/resourceid', {'keyword': keyword, 'searchOption': 'all'})
    data = obj.get('Data')
    rows = data.get('records', []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        raise ValueError('Unexpected catalogue payload')
    count = 0
    with con:
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            try:
                title = rec.get('title') or rec.get('tableTitle') or ''
                upsert_table(
                    con,
                    rec.get('id'),
                    title,
                    theme=str(rec.get('theme') or ''),
                    subject=str(rec.get('subject') or ''),
                )
                count += 1
            except ValueError:
                continue
    return count


def inspect_table(
    con: sqlite3.Connection,
    tid: str,
    *,
    fetch=request_json,
    page_limit: int = 250,
    max_pages: int = 200,
    include_observations: bool = False,
) -> dict:
    """Fetch a complete observation stream before changing legacy metadata.

    Legacy observations can represent only flat period/value pairs. Nested
    observations are stored by kb_structured_ingest, never coerced into NULLs.
    On any failure the table is marked incomplete (existing data kept) and the
    error is re-raised.
    """
    from utils.kb_structured_ingest import fetch_complete, ingest_payload

    tid = safe_id(tid)
    try:
        payload = fetch_complete(tid, page_size=page_limit, max_pages=max_pages, fetch=fetch)
        structure_info = payload.get('structure_diagnostics') or {}
        key_only_paths = int(structure_info.get('key_only_paths', 0))
        now = timestamp()
        series_data, observations, shape_counts = _read_series_rows(
            tid, payload['Data'], include_observations, now
        )
        with con:
            _store_inspection(
                con,
                tid,
                payload['Data'],
                series_data,
                observations,
                include_observations,
                key_only_paths,
                now,
            )
        # Structured cells retain complete dimensional paths. Legacy observations
        # remain restricted to actual period/value series.
        structured = (
            ingest_payload(con, payload, tid, complete_response=True)
            if include_observations
            else None
        )
        numeric = sum(row[4] is not None for row in observations)
        return {
            'table_id': tid,
            'series': len(series_data),
            'structured_ingestion': structured,
            'status': 'incomplete' if key_only_paths else 'complete',
            'reason': (
                'Key-only dimension paths unresolved'
                if key_only_paths
                else 'Series-row terminal page verified'
            ),
            'key_only_paths': key_only_paths,
            'structure_complete': not bool(key_only_paths),
            'observations_included': include_observations,
            'legacy_observations': len(observations),
            'numeric_observations': numeric,
            'observation_status': (
                ('numeric_periods_persisted' if numeric else 'no_numeric_period_observations')
                if include_observations
                else 'not_requested'
            ),
            'shape_counts': shape_counts,
        }
    except Exception as exc:  # noqa: BLE001 - mark the table incomplete on ANY failure; keep old data
        # Preserve existing data and avoid certifying a partial fetch.
        with con:
            if con.execute('SELECT 1 FROM kb_tables WHERE table_id=?', (tid,)).fetchone():
                con.execute(
                    "UPDATE kb_tables SET series_status='incomplete',last_error=? WHERE "
                    "table_id=?",
                    (str(exc)[:500], tid),
                )
        raise


def _read_series_rows(tid, data, include_observations, now):
    """(series rows, legacy observations, shape counts) from a complete official payload."""
    from utils.kb_structured_ingest import classify, leaves

    series_data, observations, missing_paths = [], [], []
    shape_counts = {"time_series": 0, "cross_sectional": 0}
    for rec in data['row']:
        sid = str(rec.get('seriesNo') or rec.get('rowNo') or '').strip()
        if not sid:
            raise ValueError('Missing series identifier')
        flat = list(leaves(rec.get('columns') or [], missing=missing_paths))
        shape = classify(data, [path for path, _ in flat])
        shape_counts[shape] += 1
        series_data.append(
            (
                sid,
                str(rec.get('rowText') or sid),
                str(rec.get('uoM') or ''),
                str(rec.get('footnote') or ''),
            )
        )
        if include_observations and shape == 'time_series':
            for path, raw in flat:
                value = str(raw if raw is not None else '')
                observations.append((tid, sid, path[0], value, parse_numeric(value), now))
    return series_data, observations, shape_counts


def _store_inspection(
    con, tid, data, series_data, observations, include_observations, key_only_paths, now
) -> None:
    """Write the table, its series, optional observations and the ingestion status (one
    transaction)."""
    upsert_table(
        con,
        tid,
        str(data.get('title') or ''),
        theme=str(data.get('theme') or ''),
        subject=str(data.get('subject') or ''),
        frequency=str(data.get('frequency') or ''),
        source_agency=str(data.get('dataSource') or data.get('datasource') or ''),
        source_updated=str(data.get('dataLastUpdated') or ''),
    )
    for sid, name, unit, footnote in series_data:
        upsert_series(con, tid, sid, name, unit, footnote)
    if include_observations:
        # Clear stale, incorrectly parsed observations for this source.
        con.execute('DELETE FROM kb_observations WHERE table_id=?', (tid,))
        con.executemany(
            """INSERT INTO kb_observations
          (table_id,series_id,period,raw_value,numeric_value,fetched_at)
          VALUES(?,?,?,?,?,?)""",
            observations,
        )
        con.execute(
            """UPDATE kb_series SET observed_periods=(
          SELECT COUNT(*) FROM kb_observations o WHERE
          o.table_id=kb_series.table_id AND o.series_id=kb_series.series_id)
          WHERE table_id=?""",
            (tid,),
        )
    con.execute(
        """UPDATE kb_tables SET series_status=?,
      series_count=?,last_ingested=?,metadata_status=?,last_error=?
      WHERE table_id=?""",
        (
            'incomplete' if key_only_paths else 'complete',
            len(series_data),
            now,
            'unverified' if key_only_paths else 'verified',
            f'{key_only_paths} key-only hierarchy nodes lack values' if key_only_paths else '',
            tid,
        ),
    )
    con.execute(
        """INSERT INTO kb_ingestion(table_id,attempt_count,updated_at,status,detail)
      VALUES(?,1,?,?,?)
      ON CONFLICT(table_id) DO UPDATE SET attempt_count=attempt_count+1,
      updated_at=excluded.updated_at,status=excluded.status,detail=excluded.detail""",
        (
            tid,
            now,
            'incomplete' if key_only_paths else 'complete',
            'Key-only dimension paths unresolved'
            if key_only_paths
            else 'Series-row terminal page verified',
        ),
    )


def ingest_pending(
    con: sqlite3.Connection,
    *,
    max_tables: int,
    include_observations: bool,
    delay: float,
    fetch=request_json,
    page_limit: int = 250,
) -> list[dict]:
    """Fetch series metadata (and optionally observations) for tables not yet complete."""
    rows = con.execute(
        "SELECT table_id FROM kb_tables WHERE series_status IN "
        "('not_started','incomplete','failed') ORDER BY table_id LIMIT ?",
        (max_tables,),
    ).fetchall()
    out = []
    for row in rows:
        try:
            out.append(
                inspect_table(
                    con,
                    row['table_id'],
                    fetch=fetch,
                    page_limit=page_limit,
                    include_observations=include_observations,
                )
            )
        except Exception as exc:  # noqa: BLE001 - batch tool: one failing table must not stop the batch
            out.append({'table_id': row['table_id'], 'status': 'failed', 'reason': str(exc)[:250]})
        if delay > 0:
            time.sleep(delay)
    return out


def lexical_search_series_first(
    con: sqlite3.Connection, question: str, limit: int = 80
) -> list[dict]:
    """Search series *and* tables directly, rather than letting broad titles
    crowd out specific observations.  FTS records are untrusted for numerical
    content: final document metadata is always hydrated from SQLite.

    Each query uses FTS5's parameterized MATCH syntax, with Unicode words
    safely quoted. Result ranks are only discovery hints, never confidence.
    """
    if not 1 <= limit <= 500:
        raise ValueError('Invalid search limit')
    raw = re.findall(r'[\w]+', question.lower(), flags=re.UNICODE)
    stop = {
        'the',
        'and',
        'how',
        'has',
        'have',
        'over',
        'time',
        'singapore',
        'what',
        'are',
        'for',
        'per',
        'from',
        'with',
        'which',
        'year',
        'changed',
        'change',
        'years',
        'month',
        'monthly',
        'quarterly',
        'annual',
        'population',
    }
    tokens = [x for x in dict.fromkeys(raw) if len(x) >= 2 and x not in stop]
    if not tokens:
        return []
    expr = ' OR '.join('"' + x.replace('"', '') + '"' for x in tokens[:16])
    # Query both series and table docs independently; preserve series variety.
    ids = []
    for prefix in ('s:', 't:'):
        try:
            rows = con.execute(
                """SELECT doc_id FROM kb_fts
                WHERE kb_fts MATCH ? AND doc_id LIKE ?
                ORDER BY bm25(kb_fts) LIMIT ?""",
                (expr, prefix + '%', limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        ids.extend(r['doc_id'] for r in rows)
    return hydrate(con, list(dict.fromkeys(ids)), min(limit * 2, 1000))


def lexical_search(con: sqlite3.Connection, question: str, limit: int = 15) -> list[dict]:
    """Full-text search of the knowledge base for the question's words (BM25 ranked)."""
    words = re.findall(r'[\w]+', question.lower(), flags=re.UNICODE)
    words = [
        w
        for w in words
        if len(w) >= 2
        and w
        not in {
            'the',
            'and',
            'how',
            'has',
            'have',
            'over',
            'time',
            'singapore',
            'what',
            'are',
            'for',
            'per',
            'from',
            'with',
            'which',
            'year',
            'changed',
        }
    ]
    if not words:
        return []
    expr = ' OR '.join('"' + w.replace('"', '') + '"' for w in dict.fromkeys(words[:12]))
    try:
        hits = con.execute(
            'SELECT doc_id,bm25(kb_fts) AS score FROM kb_fts WHERE kb_fts MATCH ? '
            'ORDER BY score LIMIT ?',
            (expr, limit * 3),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return hydrate(con, [r['doc_id'] for r in hits], limit)


def hydrate(con: sqlite3.Connection, docids: list[str], limit: int = 30) -> list[dict]:
    """Turn search document IDs ("t:..." table, "s:..." series) into result dicts."""
    results = []
    for docid in docids:
        if docid.startswith('t:'):
            r = con.execute('SELECT * FROM kb_tables WHERE table_id=?', (docid[2:],)).fetchone()
            if r:
                results.append(
                    {
                        'doc_id': docid,
                        'table_id': r['table_id'],
                        'table_title': r['title'],
                        'series_id': None,
                        'series_title': None,
                        'unit': '',
                        'frequency': r['frequency'],
                        'source_url': r['source_url'],
                        'series_status': r['series_status'],
                    }
                )
        elif docid.startswith('s:'):
            _, tid, sid = docid.split(':', 2)
            r = con.execute(
                '''SELECT s.*,t.title table_title,t.frequency,t.source_url,t.series_status
               FROM kb_series s JOIN kb_tables t USING(table_id)
               WHERE s.table_id=? AND s.series_id=?''',
                (tid, sid),
            ).fetchone()
            if r:
                results.append(
                    {
                        'doc_id': docid,
                        'table_id': tid,
                        'table_title': r['table_title'],
                        'series_id': sid,
                        'series_title': r['title'],
                        'unit': r['unit'],
                        'frequency': r['frequency'],
                        'source_url': r['source_url'],
                        'series_status': r['series_status'],
                    }
                )
        if len(results) >= limit:
            break
    return results


# --- Query embeddings ------------------------------------------------------------------
# Chroma 1.x can rebuild its embedding function from the settings saved in the index,
# and that rebuilt function reads its API key from an ENVIRONMENT variable -- possibly
# an old, revoked key (seen as "AuthenticationError" in the semantic-search audit).
# So Sage embeds queries itself with the current in-memory key and its tracked client
# (token counting, daily ceiling, per-question limits) and gives Chroma the vector.
EMBEDDING_MODEL = 'text-embedding-3-small'  # must match the model the index was built with
_EMBED_STATE = {'client': None, 'key_digest': None}
_EMBED_LOCK = threading.Lock()


def _embedding_client():
    """An OpenAI client for embeddings using the current key (CredentialError without one)."""
    from utils.credentials import get_openai_api_key
    from utils.errors import CredentialError

    key = get_openai_api_key()
    if not key:
        raise CredentialError('OPENAI_API_KEY required for Chroma semantic search')
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
    with _EMBED_LOCK:
        if _EMBED_STATE['client'] is None or _EMBED_STATE['key_digest'] != digest:
            from openai import OpenAI
            from utils.token_usage import TrackedOpenAI

            _EMBED_STATE['client'] = TrackedOpenAI(OpenAI(api_key=key, timeout=20.0, max_retries=1))
            _EMBED_STATE['key_digest'] = digest  # a rotated key builds a new client
        return _EMBED_STATE['client']


_VECTOR_CACHE: dict = {}  # (model, text) -> vector; same text -> no second API call
_VECTOR_CACHE_MAX = 512


def embed_query(text: str) -> list[float]:
    """One embedding vector for a search question (same model as the index).

    Recently embedded texts are reused: the same question used to be embedded
    twice per answer. Vectors are not secrets; questions stay in memory only.
    """
    from utils.token_usage import api_operation

    text = str(text)[:4000]
    key = (EMBEDDING_MODEL, text)
    with _EMBED_LOCK:
        cached = _VECTOR_CACHE.get(key)
    if cached is not None:
        return list(cached)
    with api_operation(
        "Semantic search",
        "Turn the question into a vector to find related official tables in the " "ChromaDB index.",
    ):
        response = _embedding_client().embeddings.create(model=EMBEDDING_MODEL, input=[text])
    vector = tuple(response.data[0].embedding)
    with _EMBED_LOCK:
        if len(_VECTOR_CACHE) >= _VECTOR_CACHE_MAX:
            _VECTOR_CACHE.pop(next(iter(_VECTOR_CACHE)))
        _VECTOR_CACHE[key] = vector
    return list(vector)


def _official_page(tid):
    """The official Table Builder page of a table ID."""
    from utils.official_links import table_page_url

    return table_page_url(tid)


class ChromaIndex:
    """Secondary index; SQLite is authoritative and all results are rehydrated."""

    def __init__(self, path: str | Path, mode: str = 'openai') -> None:
        """Open the Chroma index at ``path`` with OpenAI or local (``mode``) embeddings."""
        import chromadb
        from chromadb.utils import embedding_functions

        if mode == 'openai':
            from utils.credentials import get_openai_api_key
            from utils.errors import CredentialError

            api_key = get_openai_api_key()  # held in memory, not os.environ
            if not api_key:
                raise CredentialError('OPENAI_API_KEY required for Chroma semantic embeddings')
            # Used only when BUILDING the index (sync); searches pass vectors from embed_query.
            ef = embedding_functions.OpenAIEmbeddingFunction(
                api_key=api_key, model_name=EMBEDDING_MODEL
            )
        elif mode == 'local':
            # The local default model downloads on first use. Refuse that in
            # production unless the operator has pre-baked it and opted in.
            import os

            if not env_opt_in('SAGE_ALLOW_LOCAL_EMBEDDING_MODEL'):
                raise ValueError(
                    'Local embedding mode needs SAGE_ALLOW_LOCAL_EMBEDDING_MODEL=1 '
                    '(pre-install the model; no runtime downloads)'
                )
            ef = embedding_functions.DefaultEmbeddingFunction()
        else:
            raise ValueError('embedding mode must be openai or local')
        from chromadb.config import Settings

        # No usage telemetry from a government system; no implicit model downloads.
        self.client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        )
        self.col = self.client.get_or_create_collection(
            'sage_singstat_official', embedding_function=ef
        )

    def sync(self, con: sqlite3.Connection, limit: int = 1000, batch: int = 50) -> dict:
        """Add or update up to ``limit`` changed knowledge-base documents in the Chroma index,
        ``batch`` at a time."""
        if not 1 <= batch <= 100 or limit < 1:
            raise ValueError('Invalid sync bounds')
        # Detect changes from authoritative FTS record text; do not skip updated docs.
        # Compute hashes in Python; no dependency on optional SQLite SHA3 builds.
        rows = con.execute('SELECT doc_id,text FROM kb_fts ORDER BY doc_id').fetchall()
        pending = []
        for r in rows:
            h = hashlib.sha256(r['text'].encode()).hexdigest()
            existing = con.execute(
                'SELECT content_sha256 FROM kb_vector_sync WHERE doc_id=?', (r['doc_id'],)
            ).fetchone()
            if existing is None or existing[0] != h:
                pending.append((r['doc_id'], r['text'], h))
            if len(pending) >= limit:
                break
        total = 0
        for i in range(0, len(pending), batch):
            chunk = pending[i : i + batch]
            self.col.upsert(
                ids=[x[0] for x in chunk],
                documents=[x[1] for x in chunk],
                metadatas=[{'type': 'series' if x[0].startswith('s:') else 'table'} for x in chunk],
            )
            with con:
                con.executemany(
                    'INSERT INTO kb_vector_sync(doc_id,content_sha256,indexed_at) '
                    'VALUES(?,?,?) '
                    'ON CONFLICT(doc_id) DO UPDATE SET '
                    'content_sha256=excluded.content_sha256,indexed_at=excluded.indexed_at',
                    [(id_, h, timestamp()) for id_, _, h in chunk],
                )
            total += len(chunk)
        return {
            'indexed': total,
            'pending_in_batch': len(pending),
            'chroma_count': self.col.count(),
        }

    def search(self, query: str, limit: int = 15) -> list[str]:
        """IDs of the ``limit`` documents most similar to ``query``."""
        result = self.col.query(query_texts=[query], n_results=max(1, limit))
        return list((result.get('ids') or [[]])[0])


def hybrid_search(
    con: sqlite3.Connection, question: str, chroma: ChromaIndex | None = None, limit: int = 12
) -> list[dict]:
    """Rank catalogue candidates, prioritising *explicit* official table IDs.

    Exact IDs are authoritative identifiers, not a claim that any observation
    has been verified. Only IDs present in the question and existing in SQLite
    are promoted. Semantic results are always rehydrated from SQLite.
    """
    if not 1 <= limit <= 500:
        raise ValueError('Invalid search limit')
    if not isinstance(question, str) or not question.strip():
        return []
    lexical = lexical_search(con, question, limit * 3)
    vectors = []
    if chroma:
        try:
            vectors = chroma.search(question, limit * 3)
        except Exception as exc:  # noqa: BLE001 - optional index; FTS remains authoritative
            logging.getLogger('sage.kb').warning(
                'Chroma unavailable; using FTS: %s', type(exc).__name__
            )
    scores = {}
    # Deduplicate IDs inside each channel so duplicates cannot inflate rank.
    for rank, docid in enumerate(dict.fromkeys(r['doc_id'] for r in lexical)):
        scores[docid] = scores.get(docid, 0) + 1 / (60 + rank + 1)
    for rank, docid in enumerate(dict.fromkeys(vectors)):
        if isinstance(docid, str) and (docid.startswith('t:') or docid.startswith('s:')):
            scores[docid] = scores.get(docid, 0) + 1 / (60 + rank + 1)
    # Match complete identifiers only; never match substrings of another token.
    # Preserve first-mentioned order for questions containing multiple IDs.
    mentioned = list(
        dict.fromkeys(re.findall(r'(?<![\w])(?:[A-Za-z][0-9]{6}|[0-9]{1,12})(?![\w])', question))
    )
    exact = []
    for ident in mentioned:
        tid = ident.upper()
        if con.execute('SELECT 1 FROM kb_tables WHERE table_id=?', (tid,)).fetchone():
            exact.append('t:' + tid)
    # Exact-ID matches precede heuristic matches; scores remain heuristics,
    # never probabilities or verification status.
    ranked = list(dict.fromkeys(exact + sorted(scores, key=lambda x: (-scores[x], x))))
    results = hydrate(con, ranked, limit)
    for r in results:
        r['retrieval_score'] = round(scores.get(r['doc_id'], 0.0), 7)
    return results


def counts(con: sqlite3.Connection) -> dict:
    """How many tables, series and observations the knowledge base holds."""
    return {
        'tables': con.execute('SELECT COUNT(*) FROM kb_tables').fetchone()[0],
        'series': con.execute('SELECT COUNT(*) FROM kb_series').fetchone()[0],
        'observations': con.execute('SELECT COUNT(*) FROM kb_observations').fetchone()[0],
        'series_complete_tables': con.execute(
            "SELECT COUNT(*) FROM kb_tables WHERE " "series_status='complete'"
        ).fetchone()[0],
        'series_incomplete_tables': con.execute(
            "SELECT COUNT(*) FROM kb_tables WHERE " "series_status='incomplete'"
        ).fetchone()[0],
        'vector_synced_docs': con.execute('SELECT COUNT(*) FROM kb_vector_sync').fetchone()[0],
    }


def main(argv: list[str] | None = None) -> int:
    """Command line: build, update and search the knowledge base (python -m utils.kb -h)."""
    p = argparse.ArgumentParser(
        description='Sage: SingStat SQLite FTS5 and Chroma knowledge-base ' 'builder'
    )
    p.add_argument('--db', default='data/knowledge_base.sqlite3')
    p.add_argument('--chroma-dir', default='data/chroma_singstat')
    p.add_argument('--embedding', choices=('openai', 'local'), default='openai')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    imp = sub.add_parser('import-catalogue')
    imp.add_argument('csv_file')
    discover = sub.add_parser('discover')
    discover.add_argument('keywords', nargs='+')
    ingest = sub.add_parser('ingest')
    ingest.add_argument('--limit', type=int, default=10)
    ingest.add_argument('--page-size', type=int, default=250)
    ingest.add_argument('--delay', type=float, default=0.5)
    ingest.add_argument('--observations', action='store_true')
    one = sub.add_parser('table')
    one.add_argument('table_id')
    one.add_argument('--observations', action='store_true')
    one.add_argument('--page-size', type=int, default=250)
    sync = sub.add_parser('sync-chroma')
    sync.add_argument('--limit', type=int, default=500)
    search = sub.add_parser('search')
    search.add_argument('question')
    search.add_argument('--limit', type=int, default=12)
    search.add_argument('--fts-only', action='store_true')
    sub.add_parser('status')
    a = p.parse_args(argv)
    con = open_db(a.db)
    try:
        if a.command == 'init':
            result = {'db': str(Path(a.db).resolve()), 'status': 'initialized'}
        elif a.command == 'import-catalogue':
            result = import_catalogue_csv(con, a.csv_file)
        elif a.command == 'discover':
            result = {term: discover_keyword(con, term) for term in a.keywords}
        elif a.command == 'ingest':
            result = ingest_pending(
                con,
                max_tables=a.limit,
                delay=a.delay,
                page_limit=a.page_size,
                include_observations=a.observations,
            )
        elif a.command == 'table':
            result = inspect_table(
                con, a.table_id, page_limit=a.page_size, include_observations=a.observations
            )
        elif a.command == 'sync-chroma':
            result = ChromaIndex(a.chroma_dir, a.embedding).sync(con, limit=a.limit)
        elif a.command == 'search':
            result = hybrid_search(
                con,
                a.question,
                None if a.fts_only else ChromaIndex(a.chroma_dir, a.embedding),
                limit=a.limit,
            )
        else:
            result = counts(con)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        con.close()


if __name__ == '__main__':
    raise SystemExit(main())
