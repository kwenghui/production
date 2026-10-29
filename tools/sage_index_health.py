"""Read-only Sage local index diagnostics. No network, embeddings or database creation.

Usage: python tools/sage_index_health.py --root C:\\sage-v2
"""

from __future__ import annotations
import argparse
import json
import sqlite3
from pathlib import Path

DATABASES = ('knowledge_base.sqlite3', 'discovery_index.sqlite3')


def sqlite_health(path: Path) -> dict:
    result = {'path': str(path), 'status': 'missing', 'tables': {}, 'bytes': None}
    if not path.is_file():
        return result
    result['bytes'] = path.stat().st_size
    try:
        # SQLite URI read-only, with query_only and zero busy timeout.
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)
        try:
            connection.execute('PRAGMA query_only=ON')
            result['integrity'] = connection.execute('PRAGMA quick_check').fetchone()[0]
            tables = [
                r[0]
                for r in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                )
            ]
            for name in tables:
                safe_name = '"' + name.replace('"', '""') + '"'
                # Counts can be costly for enormous tables. Catalogue-specific
                # counts are enough to diagnose missing indexes.
                if name in {'docs', 'tables', 'series', 'observations', 'embedding_sync'}:
                    result['tables'][name] = connection.execute(
                        f'SELECT COUNT(*) FROM {safe_name}'
                    ).fetchone()[0]
                else:
                    result['tables'][name] = None
            result['status'] = 'ok' if result['integrity'] == 'ok' else 'integrity_problem'
            if path.name == 'discovery_index.sqlite3' and 'docs' not in tables:
                result['status'] = 'missing_docs_table'
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        result['status'] = 'unreadable'
        result['error_type'] = type(exc).__name__
    return result


def inspect(root: Path) -> dict:
    root = Path(root).expanduser().resolve()
    data = root / 'data'
    report = {'project': str(root), 'databases': {}, 'chroma': {}, 'recommendations': []}
    for name in DATABASES:
        info = sqlite_health(data / name)
        report['databases'][name] = info
        if info['status'] != 'ok':
            report['recommendations'].append(
                f'Check {name}: {info["status"]}; do not rebuild or ' f'delete automatically.'
            )
    chroma_dir = data / 'chroma_singstat'
    sqlite_files = list(chroma_dir.glob('*.sqlite3')) if chroma_dir.is_dir() else []
    report['chroma'] = {
        'path': str(chroma_dir),
        'directory_exists': chroma_dir.is_dir(),
        'sqlite_files': [p.name for p in sqlite_files],
        'status': 'present_unverified' if sqlite_files else 'missing_or_unrecognised',
    }
    if not sqlite_files:
        report['recommendations'].append(
            'ChromaDB storage not found at expected path; verify '
            'actual configured collection path before copying.'
        )
    else:
        report['recommendations'].append(
            'ChromaDB files found; collection identity, embedding '
            'model and vector counts still require compatibility '
            'verification.'
        )
    report['index_files_present'] = all(
        v['status'] == 'ok' for v in report['databases'].values()
    ) and bool(sqlite_files)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.', help='Sage project root containing data/')
    parser.add_argument('--output', help='Optional JSON report path')
    args = parser.parse_args()
    report = inspect(Path(args.root))
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
