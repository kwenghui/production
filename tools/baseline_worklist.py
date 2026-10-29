"""Generate a non-fabricated checklist for collecting real Sage baseline results.

Read-only with respect to the baseline. This does not call Sage or SingStat.
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path
from tools.baseline_record import template
from tools.baseline_regression_gate import CANONICAL, _validate
from tools.baseline_record_observation import validate_observation


def worklist(document):
    if not isinstance(document, dict) or document.get('schema_version') != 1:
        raise ValueError('Unsupported baseline schema')
    cases = document.get('cases')
    if not isinstance(cases, list) or len(cases) != len(CANONICAL):
        raise ValueError('Expected exactly nine canonical cases')
    by_id = {}
    for case in cases:
        if not isinstance(case, dict) or case.get('id') not in CANONICAL or case['id'] in by_id:
            raise ValueError('Invalid or duplicate case ID')
        if case.get('question') != CANONICAL[case['id']]:
            raise ValueError('Canonical question changed')
        by_id[case['id']] = case
    if set(by_id) != set(CANONICAL):
        raise ValueError('Canonical case missing')
    rows = []
    for case_id, question in CANONICAL.items():
        result = by_id[case_id].get('result')
        if result is not None:
            validate_observation(result)
        rows.append(
            {
                'id': case_id,
                'question': question,
                'status': 'recorded' if result is not None else 'pending',
            }
        )
    recorded = sum(row['status'] == 'recorded' for row in rows)
    return {
        'recorded': recorded,
        'total': len(rows),
        'complete': recorded == len(rows),
        'cases': rows,
        'note': 'Recorded does not mean independently verified against SingStat.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    try:
        document = (
            json.loads(args.baseline.read_text(encoding='utf-8'))
            if args.baseline.exists()
            else template()
        )
        result = worklist(document)
        payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload, encoding='utf-8')
        print(payload, end='')
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f'Cannot inspect baseline: {exc}\n')


if __name__ == '__main__':
    main()
