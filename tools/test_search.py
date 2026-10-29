"""Read-only Sage SQLite FTS5 vs ChromaDB search diagnostic."""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / "utils").is_dir():
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main():
    parser = argparse.ArgumentParser(description='Compare Sage SQLite and ChromaDB discovery')
    parser.add_argument('question', nargs='*', help='Search question (otherwise prompted)')
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    question = ' '.join(args.question).strip() or input('Question: ').strip()
    if not question:
        print('No question supplied.')
        return 2
    limit = max(1, min(args.limit, 30))
    from utils import hybrid_discovery as hd
    from utils import kb

    db = hd._path('SAGE_KB_SQLITE', 'data/knowledge_base.sqlite3')
    chroma_dir = hd._path('SAGE_CHROMA_DIR', 'data/chroma_singstat')
    print(f'\nSQLite: {db}\nChroma: {chroma_dir}\nQuestion: {question}\n')
    if not hd._is_real_sqlite(db):
        print('ERROR: Knowledge-base SQLite file missing or invalid. No database was created.')
        return 2
    con = hd._open_existing(db)
    try:
        print('=== SQLite FTS5 (Sage lexical_search_series_first) ===')
        try:
            rows = kb.lexical_search_series_first(con, question, limit=limit)
            print(f'{len(rows)} hits')
            for i, r in enumerate(rows, 1):
                print(
                    f'{i:2}. {r.get("table_id", "")} | {r.get("table_title", "")} | {r.get("series_title") or "(table)"}'
                )
        except Exception as exc:
            print(f'SQLite query failed: {type(exc).__name__}: {exc}')

        print('\n=== ChromaDB (Sage collection, table and series searches) ===')
        if not hd._is_real_sqlite(chroma_dir / 'chroma.sqlite3'):
            print('Chroma index missing or invalid; not creating an empty index.')
        elif not hd.chroma_search_enabled():
            print('Chroma disabled by SAGE_CHROMA_SEARCH environment setting.')
        else:
            try:
                # Use the project's real collection and embedding configuration.
                mode = os.getenv('SAGE_CHROMA_EMBEDDING', 'openai')
                index = hd._chroma_index(chroma_dir, mode)
                print(f'Collection: {index.col.name}; indexed documents: {index.col.count()}')
                if index.col.count():
                    if mode == 'openai':
                        print(
                            'Creating question embedding with Sage (may use OpenAI API credits)...'
                        )
                        vector = kb.embed_query(question)
                    else:
                        vector = None
                    for kind in ('table', 'series'):
                        print(f'\n{kind.upper()} results:')
                        ids = hd._vector_ids(index, question, kind, limit, vector)
                        hydrated = kb.hydrate(con, ids, limit=limit)
                        if not ids:
                            print('  No matches')
                        for i, docid in enumerate(ids, 1):
                            row = next((r for r in hydrated if r.get('doc_id') == docid), None)
                            if row:
                                print(
                                    f'  {i:2}. {row.get("table_id")} | {row.get("table_title")} | {row.get("series_title") or "(table)"}'
                                )
                            else:
                                print(f'  {i:2}. {docid} | no matching SQLite metadata')
            except Exception as exc:
                print(f'Chroma query failed: {type(exc).__name__}: {exc}')
                print(
                    'Check installed dependencies, index compatibility, and embedding credentials.'
                )

        print('\n=== Sage combined hybrid ranking ===')
        try:
            ranked = hd.retrieve(question, top_k=min(limit, 15))
            if not ranked:
                print('No ranked candidates')
            for item in ranked:
                print(
                    f'{item.get("candidate_rank")}. {item.get("table_id")} | {item.get("title")} | {item.get("discovery_sources")}'
                )
            if ranked:
                trace = ranked[0].get('discovery_trace') or {}
                print('SQLite status:', trace.get('sqlite'))
                print('Chroma status:', trace.get('chroma'))
        except Exception as exc:
            print(f'Hybrid ranking failed: {type(exc).__name__}: {exc}')
    finally:
        con.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
