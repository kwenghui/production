"""Opt-in Sage discovery over verified SQLite metadata and disposable Chroma vectors.

Rank distinct official TABLES rather than allowing hundreds of related series
from one table to exhaust the shortlist. No numerical data is used here.

Used by: app.py, utils/rag_engine.py,
tools/characterise_hybrid_retrieve.py, tools/diagnose_survey_ranking.py,
tools/evaluate_discovery.py, tools/test_search.py,
tools/test_search_trace.py.
Tested in: tests/test_discovery_and_ingest.py,
tests/test_discovery_ranking.py, tests/test_semantic_search_key.py.
"""

from __future__ import annotations

from utils.query_text import requested_frequency, title_frequency
import contextvars
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from utils.search_terms import currency_names, subject_terms, title_terms, vocabulary_for
from utils.vocabulary import CURRENCY_CODES
from utils.survey_editions import (  # noqa: F401 - also used by tests through this module
    _complete_edition_years,
    _official_publication_metadata,
    _official_reference_year,
    _published_reference_year,
    _published_year_in_official_payload,
    _rank_survey_editions,
    _survey_years_from_catalog,
    survey_edition_metadata,
)

LOG = logging.getLogger(__name__)

# --- Speed: one open index per process, work in parallel ------------------------------
# The ChromaDB index (100k records, ~600 MB of vectors) used to be opened and counted for
# EVERY question, and the question vector, the table search and the series search ran one
# after another. Now the index stays open, the vector request overlaps the keyword search,
# and both searches run at once. Worker threads run inside the question's context, so the
# vector call still counts towards token usage, the daily cap and the time limits.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sage-search")
_INDEXES: dict = {}
_INDEX_LOCK = threading.Lock()
_COUNT_TTL_SECONDS = 600


def _in_context(fn, *args):
    """Run fn on a worker thread with the caller's context (usage tracking, limits)."""
    return _POOL.submit(contextvars.copy_context().run, fn, *args)


def _chroma_index(path: Path, mode: str):
    """The opened index for (path, mode), shared by every question in this process."""
    from utils import kb

    key = (str(Path(path).resolve()), mode)
    with _INDEX_LOCK:
        index = _INDEXES.get(key)
    if index is None:
        opened = kb.ChromaIndex(path, mode)
        with _INDEX_LOCK:
            index = _INDEXES.setdefault(key, opened)
    return index


def _record_count(index) -> int:
    """Number of indexed records, refreshed every 10 minutes rather than per question."""
    now = time.monotonic()
    cached = getattr(index, "_sage_count", None)
    if cached is None or now - cached[1] > _COUNT_TTL_SECONDS:
        cached = (index.col.count(), now)
        index._sage_count = cached
    return cached[0]


def warm_up_index() -> bool:
    """Open the index and run one tiny search, so the first visitor does not wait.

    Called once per process in the background at start-up. Uses a stored vector from
    the index itself, so no OpenAI call is made. Failure is harmless: the first real
    question simply opens the index as before.
    """
    try:
        if not chroma_search_enabled():
            return False
        path = _path('SAGE_CHROMA_DIR', 'data/chroma_singstat')
        if not path.is_dir() or not any(path.iterdir()):
            return False
        index = _chroma_index(path, os.getenv('SAGE_CHROMA_EMBEDDING', 'openai'))
        sample = index.col.peek(1)
        vectors = sample.get("embeddings") if isinstance(sample, dict) else None
        if vectors is None or len(vectors) == 0:
            return False
        index.col.query(query_embeddings=[list(vectors[0])], n_results=1, include=[])
        _record_count(index)
        LOG.info("Semantic search index warmed up")
        return True
    except Exception as exc:  # noqa: BLE001 - optional optimisation
        LOG.info("Semantic search warm-up skipped: %s", type(exc).__name__)
        return False


ROOT = Path(__file__).resolve().parent.parent


def _path(env: str, default: str) -> Path:
    """A path setting from the environment, relative paths resolved against the Sage folder."""
    p = Path(os.getenv(env, default)).expanduser()
    return p if p.is_absolute() else ROOT / p


def _approved_url(url: str) -> bool:
    """True only for an HTTPS Table Builder URL without credentials."""
    try:
        parsed = urlsplit(str(url))
        return (
            parsed.scheme == 'https'
            and parsed.hostname == 'tablebuilder.singstat.gov.sg'
            and not parsed.username
            and not parsed.password
        )
    except (ValueError, TypeError):
        return False


def _is_real_sqlite(path: Path) -> bool:
    """True only for a genuine SQLite file (not a missing file or a Git LFS pointer stub)."""
    try:
        with open(path, 'rb') as handle:
            return handle.read(16) == b'SQLite format 3\x00'
    except OSError:
        return False


def _is_lfs_pointer(path: Path) -> bool:
    """True when a file is a Git LFS pointer, not the real index (the clone skipped LFS)."""
    try:
        with open(path, 'rb') as handle:
            return handle.read(40).startswith(b'version https://git-lfs')
    except OSError:
        return False


def _flag(name: str, auto: bool) -> bool:
    """Explicit 1/0 in the environment wins; when unset, use the automatic default."""
    value = (os.getenv(name) or '').strip().lower()
    if value in ('1', 'true', 'yes', 'on'):
        return True
    if value in ('0', 'false', 'no', 'off'):
        return False
    return auto


def hybrid_discovery_enabled() -> bool:
    """On by default whenever a real local knowledge base exists; SAGE_HYBRID_DISCOVERY=0 turns it
    off."""
    return _flag(
        'SAGE_HYBRID_DISCOVERY',
        _is_real_sqlite(_path('SAGE_KB_SQLITE', 'data/knowledge_base.sqlite3')),
    )


def chroma_search_enabled() -> bool:
    """On by default whenever a real Chroma index exists; SAGE_CHROMA_SEARCH=0 turns it off."""
    chroma_db = _path('SAGE_CHROMA_DIR', 'data/chroma_singstat') / 'chroma.sqlite3'
    return _flag('SAGE_CHROMA_SEARCH', _is_real_sqlite(chroma_db))


def _open_existing(path: Path):
    """Open an existing SQLite index read-only, or None if the file is missing."""
    if not path.is_file():
        return None
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _table_from_docid(docid: str) -> str:
    """The table ID inside an index document ID ("t:Mxxxxxx" or "s:Mxxxxxx:1.2")."""
    if docid.startswith('t:'):
        return docid[2:]
    if docid.startswith('s:') and ':' in docid[2:]:
        return docid.split(':', 2)[1]
    return ''


# Survey editions (same title, different survey years): utils/survey_editions.py.


def _ranked_tables(hits: list[dict], *, question: str, limit: int) -> list[dict]:
    """RRF per backend and distinct tables; supporting series do not crowd out tables.

    A table gets its strongest document contribution from each source, plus
    a small evidence boost for an exact subject match. Vector distances are
    never compared to SQLite BM25 values.
    """
    words_of = subject_terms  # singular = plural, filler words dropped
    qwords = words_of(question)
    table_info: dict[str, dict] = {}
    for hit in hits:
        tid = str(hit.get('table_id') or '').strip().upper()
        if not tid or not _approved_url(hit.get('source_url', '')):
            continue
        entry = table_info.setdefault(
            tid,
            {
                'table_id': tid,
                'hits': [],
                'sources': set(),
                'score': 0.0,
                'best': None,
                'best_match': -1,
            },
        )
        entry['hits'].append(hit)
        backend = hit.get('_backend') or 'SQLite FTS5'
        entry['sources'].add(backend)
        entry['score'] += 0.0  # Scores added after per-backend table deduplication.
        overlap = len(
            qwords
            & words_of(str(hit.get('table_title') or '') + ' ' + str(hit.get('series_title') or ''))
        )
        if overlap > entry['best_match'] or (
            overlap == entry['best_match']
            and entry['best']
            and hit.get('series_title')
            and not entry['best'].get('series_title')
        ):
            entry['best'], entry['best_match'] = hit, overlap
    for backend in ('SQLite FTS5', 'ChromaDB table', 'ChromaDB series'):
        order = []
        for h in hits:
            if h.get('_backend') != backend:
                continue
            t = str(h.get('table_id') or '').upper()
            if t in table_info and t not in order:
                order.append(t)
        for rank, tid in enumerate(order, 1):
            table_info[tid]['score'] += 1.0 / (60 + rank)
    # Lexical overlap is weak evidence; it must not overwhelm multi-source ranks.
    for entry in table_info.values():
        entry['score'] += min(4, max(0, entry['best_match'])) * 0.0005
    _add_subject_coverage_ranking(table_info, qwords)
    _prefer_requested_frequency(table_info, question)
    # Fixed tie-break: equal scores always resolve the same way on every machine.
    return sorted(
        table_info.values(), key=lambda e: (-round(e['score'], 9), -e['best_match'], e['table_id'])
    )[:limit]


#: What naming EVERY subject word of the question is worth: as much as being found
#: first by two search methods (2 x 1/61). Naming half of them is worth half.
_FULL_COVERAGE_WEIGHT = 2.0 / 61


def _add_subject_coverage_ranking(table_info, qwords) -> None:
    """Reward tables whose own names contain the question's subject words.

    Coverage is the share of the question's subject words found in the official
    table title, its best-matching series name and its unit (singular = plural; an acronym
    the question uses counts when the title spells it out). A table that names
    the subject ("Students And Teachers ...", series "Total Teachers") is then
    not buried under tables found only by meaning ("Education Profile ..."),
    and "CPF withdrawals" prefers a withdrawals table over a CPF contributions
    table. Tables naming none of the subject words gain nothing.
    """
    if not qwords:
        return
    for entry in table_info.values():
        best = entry['best'] or {}
        text = ' '.join(str(best.get(key) or '') for key in ('table_title', 'series_title', 'unit'))
        share = len(qwords & title_terms(text, qwords)) / len(qwords)
        entry['score'] += _FULL_COVERAGE_WEIGHT * share


def _prefer_requested_frequency(table_info, question) -> None:
    """Nudge scores towards the reporting frequency the question asks for.

    Annual and Quarterly editions of one subject have near-identical
    descriptions and tie on every source, so the wording decides: "each
    quarter" wants Quarterly (and demotes Annual); "over the years" gently
    prefers Annual. The boost is small next to a one-rank RRF gap
    (about 0.0003-0.016).
    """
    frequency, strength = requested_frequency(question)
    if not frequency:
        return
    boost = 0.004 if strength == 'explicit' else 0.0008
    for entry in table_info.values():
        best = entry['best'] or {}
        published = (
            title_frequency(str(best.get('table_title') or ''))
            or str(best.get('frequency') or '').strip().lower()
            or None
        )
        if published == frequency:
            entry['score'] += boost
        elif published and strength == 'explicit':
            entry['score'] -= boost


def _page_for(tid, stored):
    """The official Table Builder page of a table (falls back to the stored URL)."""
    from utils.official_links import table_page_url

    return table_page_url(tid) or stored


def _rag_record(entry: dict, *, rank: int, trace: dict) -> dict:
    """One ranked search hit as a RAG note: table, best series hint, source page, trace."""
    hit = entry['best']
    tid = entry['table_id']
    series = str(hit.get('series_title') or '')
    title = str(hit.get('table_title') or '')
    sid = str(hit.get('series_id') or '')
    content = (
        f'Official SingStat table {tid}: {title}. '
        f'Series: {series} (ID {sid}). Unit: {hit.get("unit") or ""}. '
        f'Frequency: {hit.get("frequency") or ""}.'
    )
    return {
        'id': 'KB_hybrid_' + tid,
        'title': (title + (' — ' + series if series else ''))[:230],
        'source_label': 'SingStat ' + tid,
        # Older index rows stored /table/TS/ for every table; use the correct page by type.
        'source_url': _page_for(tid, hit['source_url']),
        'text': content[:1800],
        'doc_type': 'table_metadata',
        'table_id': tid,
        'keyword_score': None,
        'semantic_score': None,
        'hybrid_score': round(entry['score'], 7),
        'retrieval_mode': 'hybrid_table_ranking',
        'candidate_rank': rank,
        'discovery_sources': sorted(entry['sources']),
        'series_evidence': series,
        'discovery_trace': trace,
    }


def _error_summary(exc) -> str:
    """Error type plus a short, redacted reason (never a key) for the audit trail."""
    from utils.redaction import redact_diagnostic

    text = redact_diagnostic(str(exc), 400)
    scopes = re.search(r"Missing scopes:\s*([A-Za-z0-9_.\-]+(?:,\s*[A-Za-z0-9_.\-]+)*)", text)
    if scopes:
        reason = (
            "API key lacks permission for embeddings (missing scope: "
            + scopes.group(1).rstrip(".")
            + ")"
        )
    elif re.search(r"(?i)incorrect api key|invalid api key|revoked", text):
        reason = "API key rejected (incorrect or revoked)"
    else:
        reason = text.strip()[:120]
    return type(exc).__name__ + (" - " + reason if reason else "")


def _vector_ids(chroma, question: str, kind: str, limit: int, query_embedding=None) -> list[str]:
    # Filtering at Chroma query time prevents broad series matches from
    # drowning out table-level similarity results.
    """Document IDs most similar to the question in the Chroma index, for tables or series."""
    if query_embedding is not None:
        # Sage's own embedding (current key, tracked usage): Chroma needs no API key.
        result = chroma.col.query(
            query_embeddings=[query_embedding],
            n_results=limit,
            where={'type': kind},
            include=['distances'],
        )
    else:
        result = chroma.col.query(
            query_texts=[question], n_results=limit, where={'type': kind}, include=['distances']
        )
    return list((result.get('ids') or [[]])[0])


def retrieve(question: str, *, top_k: int = 4, dataset_ids=()) -> list[dict]:
    """Get distinct, official table candidates. Does not fetch observations.

    Each returned candidate carries an execution trace. A Chroma query is
    marked executed ONLY after the query returns successfully.

    Steps: keyword search in the SQLite index (_keyword_hits); semantic search
    in the Chroma index when available (_semantic_hits; the question's vector
    is requested while the keyword search runs); one ranking of distinct
    tables (_ranked_tables), with tables named in ``dataset_ids`` first.
    """
    from utils import kb
    from utils.query_text import normalise_query

    if not isinstance(question, str) or not question.strip():
        return []
    question = normalise_query(question)  # capitalisation/spacing never change the tables found
    top_k = min(15, max(1, int(top_k)))
    kb_path = _path('SAGE_KB_SQLITE', 'data/knowledge_base.sqlite3')
    con = _open_existing(kb_path)
    if con is None:
        return []
    corrections = {}
    # Misspelt words are matched to words of official names in the index itself;
    # the user's question is unchanged, only the search text and the trace show it.
    vocabulary = vocabulary_for(kb_path)
    if vocabulary is not None:
        question, corrections = vocabulary.correct(question)
    # Currency codes are searched together with the names official tables use
    # ("myr" -> "malaysian ringgit"); the codes stay in the text for ranking.
    expansions = {
        code: name
        for code, name in zip(
            [w for w in re.findall(r'[A-Za-z]+', question.casefold()) if w in CURRENCY_CODES],
            currency_names(question),
        )
    }
    if expansions:
        question = question + ' ' + ' '.join(dict.fromkeys(expansions.values()))
    trace = {
        'version': 1,
        'sqlite': {'status': 'not run', 'result_count': 0},
        'chroma': {
            'status': 'disabled',
            'indexed_records': None,
            'table_matches': 0,
            'series_matches': 0,
            'elapsed_ms': None,
        },
        'ranking': {'method': 'distinct-table RRF', 'candidates': []},
    }
    trace['query_corrections'] = corrections
    if expansions:
        trace['query_expansions'] = expansions
    start = time.monotonic()
    embedding_mode = os.getenv('SAGE_CHROMA_EMBEDDING', 'openai')
    vector_future, vector_started = None, time.monotonic()
    index_dir = _path('SAGE_CHROMA_DIR', 'data/chroma_singstat')
    if (
        chroma_search_enabled()
        and embedding_mode == 'openai'
        and index_dir.is_dir()
        and any(index_dir.iterdir())
    ):
        # The question's vector (an OpenAI call) is fetched WHILE the keyword search runs.
        vector_future = _in_context(kb.embed_query, question)
    try:
        hits = _keyword_hits(con, question, trace)
        hits += _semantic_hits(con, question, trace, embedding_mode, vector_future, vector_started)
        ids = {str(x).upper() for x in (dataset_ids or []) if isinstance(x, str)}
        ranking = _ranked_tables(hits, question=question, limit=max(top_k * 4, 16))
        survey_years = _survey_years_from_catalog(con, [e['table_id'] for e in ranking])
        survey_years = _complete_edition_years(ranking, survey_years)
        ranking = _rank_survey_editions(ranking, question, survey_years)
        trace['ranking']['published_survey_years'] = survey_years
        # A survey's reference year and the date its data was last updated are
        # distinct facts. Only reference_year influences edition ranking.
        trace['ranking']['survey_edition_metadata'] = {
            tid: dict(details)
            for tid, details in survey_edition_metadata().items()
            if tid in {entry['table_id'] for entry in ranking}
        }
        if ids:
            # Explicit table IDs get preference, never invent candidate data.
            ranking.sort(key=lambda r: (r['table_id'] not in ids, -r['score'], r['table_id']))
        selected = ranking[:top_k]
        frequency_hint, hint_strength = requested_frequency(question)
        trace['ranking']['frequency_hint'] = (
            {'frequency': frequency_hint, 'strength': hint_strength} if frequency_hint else None
        )
        trace['ranking']['candidates'] = [
            {
                'rank': i,
                'table_id': e['table_id'],
                'sources': sorted(e['sources']),
                'score': round(e['score'], 7),
                'matching_series': str((e['best'] or {}).get('series_title') or '')[:120],
            }
            for i, e in enumerate(selected, 1)
        ]
        trace['elapsed_ms'] = round((time.monotonic() - start) * 1000)
        return [_rag_record(e, rank=i, trace=trace) for i, e in enumerate(selected, 1)]
    finally:
        con.close()


def _keyword_hits(con, question, trace) -> list:
    """SQLite FTS5 matches (series first, grouped to tables later); [] if the search fails."""
    from utils import kb

    try:
        # Search many lexical documents then group to tables; original FTS order is kept.
        lexical = kb.lexical_search_series_first(con, question, limit=120)
        trace['sqlite'] = {'status': 'executed', 'result_count': len(lexical)}
    except (sqlite3.Error, ValueError) as exc:
        LOG.warning('SQLite FTS5 retrieval failed: %s', exc)
        lexical = []
        trace['sqlite'] = {'status': 'failed', 'result_count': 0, 'reason': type(exc).__name__}
    return [dict(row, _backend='SQLite FTS5') for row in lexical]


def _semantic_hits(con, question, trace, embedding_mode, vector_future, vector_started) -> list:
    """Chroma matches for tables and series, hydrated from SQLite; [] when unavailable.

    The trace says exactly why semantic search did not run (switched off, Git
    LFS pointer, index missing or empty, embedding or query failure).
    """
    chroma_path = _path('SAGE_CHROMA_DIR', 'data/chroma_singstat')
    if not chroma_search_enabled():
        if _is_lfs_pointer(chroma_path / 'chroma.sqlite3'):
            trace['chroma']['status'] = 'index is a Git LFS pointer (real data not downloaded)'
        elif (os.getenv('SAGE_CHROMA_SEARCH') or '').strip() in ('0', 'false', 'no', 'off'):
            trace['chroma']['status'] = 'disabled (SAGE_CHROMA_SEARCH=0)'
        else:
            trace['chroma']['status'] = 'index missing'
        return []
    if not chroma_path.is_dir() or not any(chroma_path.iterdir()):
        trace['chroma']['status'] = 'index missing'
        return []
    try:
        chroma = _chroma_index(chroma_path, embedding_mode)  # opened once per process
        count = _record_count(chroma)
        trace['chroma']['indexed_records'] = count
        if count <= 0:
            trace['chroma']['status'] = 'empty index'
            return []
        return _query_semantic_index(con, chroma, question, trace, vector_future, vector_started)
    except Exception as exc:  # noqa: BLE001 - semantic search is optional; keyword results remain
        LOG.warning('Chroma unavailable: %s', exc)
        trace['chroma']['status'] = 'failed to open'
        trace['chroma']['reason'] = type(exc).__name__
        return []


def _query_semantic_index(con, chroma, question, trace, vector_future, vector_started) -> list:
    """Run the table and series searches at the same time and hydrate their results."""
    from utils import kb

    hits = []
    cstart = time.monotonic()
    query_embedding, embed_error = None, None
    try:
        if vector_future is not None:
            query_embedding = vector_future.result()
            trace['chroma']['vector_ms'] = round((time.monotonic() - vector_started) * 1000)
    except Exception as exc:  # noqa: BLE001 - semantic search is optional
        embed_error = _error_summary(exc)
        LOG.warning('Query embedding unavailable: %s', embed_error)
    searches = {}
    if embed_error:
        for kind in ('table', 'series'):
            trace['chroma'][kind + '_error'] = embed_error
    else:
        sstart = time.monotonic()
        searches = {
            kind: _in_context(_vector_ids, chroma, question, kind, 60, query_embedding)
            for kind in ('table', 'series')
        }
    for kind, future in searches.items():
        try:
            ids = future.result()
            trace['chroma'][kind + '_matches'] = len(ids)
            # Hydration from SQLite is mandatory: no vector metadata is treated as an
            # authoritative statistical description. (Main thread only: SQLite
            # connections are not shared.)
            for row in kb.hydrate(con, ids, limit=len(ids)):
                hits.append(dict(row, _backend='ChromaDB ' + kind))
        except Exception as exc:  # noqa: BLE001 - semantic search is optional; keyword results remain
            LOG.warning('Chroma %s query unavailable: %s', kind, exc)
            trace['chroma'][kind + '_error'] = type(exc).__name__
    if searches:
        trace['chroma']['search_ms'] = round((time.monotonic() - sstart) * 1000)
    trace['chroma']['elapsed_ms'] = round((time.monotonic() - cstart) * 1000)
    trace['chroma']['status'] = (
        'executed'
        if any(key not in trace['chroma'] for key in ('table_error', 'series_error'))
        else 'failed'
    )
    return hits


def merge_existing(existing: list[dict], extra: list[dict], top_k: int) -> list[dict]:
    """Append ``extra`` candidates not already in ``existing`` (by URL + title), up to ``top_k``."""
    result = list(existing or [])[:top_k]
    present = {str(i.get('source_url', '')) + '|' + str(i.get('title', '')) for i in result}
    for item in extra:
        identifier = item['source_url'] + '|' + item['title']
        if identifier not in present and len(result) < top_k:
            result.append(item)
            present.add(identifier)
    return result
