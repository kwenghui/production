"""Read-only, evidence-neutral Sage KB candidate retrieval.

Never upgrades catalogue metadata into verified numerical evidence.

Used by: utils/question_pipeline.py.
Tested in: tests/test_ui_helpers.py.
"""

from __future__ import annotations
import os
import sqlite3
from pathlib import Path

from utils.discovery_controller import compatibility
from utils.kb import hybrid_search, lexical_search_series_first, ChromaIndex
from utils.query_text import normalise_query
from utils.settings import env_flag


def retrieve_candidates(question: str, project_root: str | Path, *, limit: int = 12) -> list[dict]:
    """Compatible candidate tables from the local knowledge base (metadata only, never evidence)."""
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        return []
    root = Path(project_root)
    db = root / 'data' / 'knowledge_base.sqlite3'
    if not db.is_file():
        return []
    # same wording in any capitalisation searches identically
    search_text = normalise_query(question)
    con = None
    try:
        con = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
        con.row_factory = sqlite3.Row
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='kb_fts'").fetchone():
            return []
        chroma = None
        # Opt-in; local FTS works immediately without model downloads/API calls.
        if env_flag('SAGE_KB_CHROMA', False):
            try:
                chroma = ChromaIndex(
                    root / 'data' / 'chroma_singstat', os.getenv('SAGE_KB_EMBEDDING', 'openai')
                )
            except (ImportError, RuntimeError, ValueError, OSError):
                chroma = None
        rows = hybrid_search(con, search_text, chroma=chroma, limit=min(100, max(1, limit * 4)))
        # Include series-first hits so generic table titles cannot monopolise results.
        seen = {r['doc_id'] for r in rows}
        for hit in lexical_search_series_first(con, search_text, limit=min(100, limit * 4)):
            if hit['doc_id'] not in seen:
                # Keyword-only hits carry no fused score (hybrid_search is what adds one), so a
                # missing score is 0.0 rather than an error. They already rank after the hybrid
                # hits.
                hit.setdefault('retrieval_score', 0.0)
                rows.append(hit)
                seen.add(hit['doc_id'])
        approved = []
        for row in rows:
            title = row.get('series_title') or row.get('table_title') or ''
            if not title or not compatibility(question, title)[0]:
                continue
            approved.append(
                {
                    'table_id': row['table_id'],
                    'table_title': row['table_title'],
                    'series_id': row['series_id'],
                    'series_title': row['series_title'],
                    'unit': row['unit'],
                    'source_url': row['source_url'],
                    'retrieval_score': row.get('retrieval_score', 0.0),
                    'verification': 'metadata_only',
                }
            )
            if len(approved) >= limit:
                break
        return approved
    except (sqlite3.Error, OSError):
        return []
    finally:
        if con is not None:
            con.close()
