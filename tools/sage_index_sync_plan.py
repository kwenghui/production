"""Offline, read-only Sage index synchronisation planner.

Compares authoritative SQLite FTS documents with kb_vector_sync hashes.
Optionally inspects an existing Chroma collection without creating or
embedding anything. Never writes to SQLite or Chroma.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

COLLECTION = 'sage_singstat_official'


def plan_sqlite(db_path: Path, sample_limit: int = 20) -> dict:
    db_path = Path(db_path)
    result = {'path': str(db_path), 'status': 'missing', 'counts': {}, 'examples': {}}
    if not db_path.is_file():
        return result
    try:
        con = sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            con.execute('PRAGMA query_only=ON')
            present = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN " "('table','view')"
                )
            }
            required = {'kb_fts', 'kb_vector_sync'}
            if not required <= present:
                result.update(status='incompatible_schema', missing=sorted(required - present))
                return result
            counts = {
                'fts_documents': 0,
                'unchanged': 0,
                'new': 0,
                'changed': 0,
                'orphan_sync': 0,
                'duplicate_fts_ids': 0,
            }
            examples = {key: [] for key in ('new', 'changed', 'orphan_sync', 'duplicate_fts_ids')}
            sync = {
                r[0]: r[1]
                for r in con.execute('SELECT doc_id, content_sha256 FROM ' 'kb_vector_sync')
            }
            found = set()
            for doc_id, content in con.execute('SELECT doc_id, text FROM kb_fts'):
                counts['fts_documents'] += 1
                if doc_id in found:
                    counts['duplicate_fts_ids'] += 1
                    if len(examples['duplicate_fts_ids']) < sample_limit:
                        examples['duplicate_fts_ids'].append(doc_id)
                    continue
                found.add(doc_id)
                digest = hashlib.sha256((content or '').encode('utf-8')).hexdigest()
                if doc_id not in sync:
                    kind = 'new'
                elif sync[doc_id] != digest:
                    kind = 'changed'
                else:
                    kind = 'unchanged'
                counts[kind] += 1
                if kind in examples and len(examples[kind]) < sample_limit:
                    examples[kind].append(doc_id)
            orphans = sync.keys() - found
            counts['orphan_sync'] = len(orphans)
            examples['orphan_sync'] = sorted(orphans)[:sample_limit]
            result.update(
                status='ok',
                counts=counts,
                examples=examples,
                needs_review=bool(counts['duplicate_fts_ids'] or counts['orphan_sync']),
            )
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        result.update(status='unreadable', error_type=type(exc).__name__)
    return result


def inspect_chroma(chroma_path: Path) -> dict:
    """Optional collection metadata check. No get_or_create or embeddings."""
    chroma_path = Path(chroma_path)
    result = {'path': str(chroma_path), 'collection': COLLECTION, 'status': 'missing'}
    if not chroma_path.is_dir():
        return result
    try:
        import chromadb
    except ImportError:
        result['status'] = 'chromadb_not_installed'
        return result
    try:
        # PersistentClient may alter internal operational files on access.
        # Consequently offline mode never opens it; report collection as unverified.
        result['status'] = 'present_unverified'
        result['note'] = (
            'Collection and embedding compatibility require a separate controlled ' 'validation.'
        )
    except Exception as exc:
        result.update(status='unreadable', error_type=type(exc).__name__)
    return result


def build_plan(root: Path, sample_limit: int = 20) -> dict:
    root = Path(root).resolve()
    db = root / 'data' / 'knowledge_base.sqlite3'
    sql = plan_sqlite(db, sample_limit=sample_limit)
    chroma = inspect_chroma(root / 'data' / 'chroma_singstat')
    notes = []
    if sql['status'] != 'ok':
        notes.append(
            'SQLite KB unavailable or incompatible; do not start indexing until '
            'paths/schema are confirmed.'
        )
    else:
        c = sql['counts']
        notes.append(
            f"{c['new']} new and {c['changed']} modified FTS documents are candidates for "
            f"vector updates, subject to verifying collection/model compatibility."
        )
        if c['orphan_sync']:
            notes.append('Stale sync records exist; review before removing any Chroma documents.')
        if c['duplicate_fts_ids']:
            notes.append('Duplicate FTS IDs need repair before synchronisation.')
        notes.append(
            'Sync hashes alone do not prove vectors are present or match the current '
            'embedding model.'
        )
    if chroma['status'] != 'present_unverified':
        notes.append(
            'Chroma persistence is absent or not verifiable here; no vectors were created.'
        )
    else:
        notes.append(
            'Chroma files exist but collection identity/model/count remain unverified; do '
            'not sync blindly.'
        )
    return {
        'mode': 'read_only',
        'sqlite': sql,
        'chroma': chroma,
        'safe_to_sync_automatically': False,
        'recommendations': notes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.', help='Project root')
    parser.add_argument('--output', help='Optional JSON report path')
    parser.add_argument('--sample-limit', type=int, default=20)
    args = parser.parse_args()
    if not 0 <= args.sample_limit <= 100:
        parser.error('--sample-limit must be between 0 and 100')
    report = build_plan(Path(args.root), args.sample_limit)
    output = json.dumps(report, indent=2, ensure_ascii=False)
    print(output)
    if args.output:
        Path(args.output).write_text(output + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
