"""Strict, fail-closed comparison of *recorded* Sage regression observations.

No network requests, model calls or production routing changes. A passing report
means only that the supplied observations match; it is not source verification.
"""

from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
from tools.baseline_cases import CASES
from tools.baseline_integrity import inspect_baseline

FIELDS = ('answer', 'sources', 'observations', 'warnings', 'security_outcome')
CANONICAL = {case_id: question for case_id, question, _ in CASES}


def _validate(document, label):
    if not isinstance(document, dict):
        return [f'{label}: baseline must be an object']
    errors = [f'{label}: {e}' for e in inspect_baseline(document)]
    cases = document.get('cases', [])
    if not isinstance(cases, list):
        return errors + [f'{label}: cases must be a list']
    actual = {c.get('id'): c.get('question') for c in cases if isinstance(c, dict)}
    if actual != CANONICAL or len(cases) != len(CANONICAL):
        errors.append(f'{label}: expected exactly the nine canonical questions')
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('result'), dict):
            continue
        result = case['result']
        for key in ('sources', 'observations', 'warnings'):
            values = result.get(key)
            if isinstance(values, list):
                try:
                    json.dumps(values, allow_nan=False)
                except (TypeError, ValueError):
                    errors.append(f"{label}/{case.get('id')}: invalid {key} values")
        elapsed = result.get('elapsed_seconds')
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed)
        ):
            errors.append(f"{label}/{case.get('id')}: invalid elapsed_seconds")
    return errors


def assess(before, after):
    errors = _validate(before, 'before') + _validate(after, 'after')
    if errors:
        return {
            'pass': False,
            'errors': errors,
            'results': [],
            'note': 'Incomplete or invalid observations cannot pass.',
        }
    old = {c['id']: c['result'] for c in before['cases']}
    new = {c['id']: c['result'] for c in after['cases']}
    results = []
    for case_id in CANONICAL:
        a, b = old[case_id], new[case_id]
        changes = [key for key in FIELDS if a[key] != b[key]]
        results.append(
            {
                'id': case_id,
                'status': 'review_required' if changes else 'matching_recorded_fields',
                'changed_fields': changes,
                'before_seconds': a['elapsed_seconds'],
                'after_seconds': b['elapsed_seconds'],
                'latency_delta_seconds': round(b['elapsed_seconds'] - a['elapsed_seconds'], 6),
            }
        )
    return {
        'pass': all(not r['changed_fields'] for r in results),
        'errors': [],
        'results': results,
        'note': 'Matching recorded fields do not prove accuracy, official provenance or live '
        'application behavior.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    try:
        result = assess(
            json.loads(args.before.read_text(encoding='utf-8')),
            json.loads(args.after.read_text(encoding='utf-8')),
        )
    except (OSError, ValueError, TypeError) as exc:
        result = {'pass': False, 'errors': [str(exc)], 'results': []}
    output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output, encoding='utf-8')
    print(output, end='')
    raise SystemExit(0 if result['pass'] else 1)


if __name__ == '__main__':
    main()
