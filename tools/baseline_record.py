"""Capture and compare Sage baseline results without changing production routing.

Usage:
 python tools/baseline_record.py init --out reports/baseline_before.json
 # Fill each result after running the question in Sage.
 python tools/baseline_record.py compare --before reports/baseline_before.json --after
 reports/baseline_after.json
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path

try:
    from tools.baseline_cases import CASES
except ModuleNotFoundError:
    from baseline_cases import CASES

REQUIRED = ('answer', 'sources', 'observations', 'warnings', 'elapsed_seconds', 'security_outcome')


def template():
    return {
        'schema_version': 1,
        'instructions': 'Run each question in the Sage UI and enter observed results. Do not '
        'invent data. Empty results are NOT a passing baseline.',
        'cases': [
            {
                'id': name,
                'question': question,
                'expected_table_hint_not_assertion': hint,
                'result': None,
            }
            for name, question, hint in CASES
        ],
    }


def validate(doc):
    if doc.get('schema_version') != 1 or not isinstance(doc.get('cases'), list):
        raise ValueError('Unsupported baseline schema')
    ids = [c.get('id') for c in doc['cases']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate case IDs')
    for c in doc['cases']:
        result = c.get('result')
        if result is None:
            continue
        if not isinstance(result, dict) or any(k not in result for k in REQUIRED):
            raise ValueError(f"Incomplete result: {c.get('id')}")
        if (
            not isinstance(result['sources'], list)
            or not isinstance(result['observations'], list)
            or not isinstance(result['warnings'], list)
        ):
            raise ValueError(f"Sources, observations and warnings must be lists: {c.get('id')}")
        if not isinstance(result['elapsed_seconds'], (int, float)) or result['elapsed_seconds'] < 0:
            raise ValueError(f"Invalid elapsed_seconds: {c.get('id')}")
    return doc


def compare(before, after):
    validate(before)
    validate(after)
    a = {c['id']: c for c in before['cases']}
    b = {c['id']: c for c in after['cases']}
    rows = []
    for key in sorted(set(a) | set(b)):
        x = a.get(key)
        y = b.get(key)
        if x is None or y is None:
            rows.append({'id': key, 'status': 'missing_case'})
            continue
        if x['question'] != y['question']:
            rows.append({'id': key, 'status': 'question_changed'})
            continue
        p = x.get('result')
        q = y.get('result')
        if p is None or q is None:
            rows.append({'id': key, 'status': 'not_executed'})
            continue
        changed = [
            k
            for k in ('answer', 'sources', 'observations', 'warnings', 'security_outcome')
            if p[k] != q[k]
        ]
        rows.append(
            {
                'id': key,
                'status': 'review_required' if changed else 'matching_recorded_fields',
                'changed_fields': changed,
                'before_seconds': p['elapsed_seconds'],
                'after_seconds': q['elapsed_seconds'],
            }
        )
    return {
        'pass': bool(rows) and all(r['status'] == 'matching_recorded_fields' for r in rows),
        'results': rows,
        'note': 'Matching recorded fields do not independently prove statistical correctness. '
        'Manually verify official evidence and security.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--out', required=True)
    cmp = sub.add_parser('compare')
    cmp.add_argument('--before', required=True)
    cmp.add_argument('--after', required=True)
    cmp.add_argument('--out')
    args = parser.parse_args()
    if args.command == 'init':
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(template(), indent=2, ensure_ascii=False) + '\n', encoding='utf-8'
        )
        print(f'Created {out}; all cases remain unexecuted')
    else:
        result = compare(
            json.loads(Path(args.before).read_text(encoding='utf-8')),
            json.loads(Path(args.after).read_text(encoding='utf-8')),
        )
        payload = json.dumps(result, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(payload + '\n', encoding='utf-8')
        print(payload)
        raise SystemExit(0 if result['pass'] else 1)


if __name__ == '__main__':
    main()
