"""Fail-closed gate for comparing real recorded question baselines.

Does not call AI, change production routing, or claim unexecuted cases passed.
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path
from tools.baseline_cases import CASES
from tools.baseline_record import validate, compare
from tools.baseline_integrity import inspect_baseline

EXPECTED = {case_id: question for case_id, question, _ in CASES}


def assess(before: dict, after: dict) -> dict:
    for label, document in [('before', before), ('after', after)]:
        errors = inspect_baseline(document)
        if errors:
            return {
                'pass': False,
                'reason': f'{label} baseline failed integrity checks',
                'errors': errors,
            }
        cases = document['cases']
        actual = {case['id']: case['question'] for case in cases}
        if actual != EXPECTED or len(cases) != len(EXPECTED):
            return {
                'pass': False,
                'reason': f'{label} baseline must contain exactly the nine unchanged canonical '
                f'questions',
            }
        if any(case.get('result') is None for case in cases):
            return {'pass': False, 'reason': f'{label} baseline contains unexecuted questions'}
    outcome = compare(before, after)
    outcome['note'] = (
        'Matching manually recorded results is not proof of accuracy. '
        'Review live official source provenance and security before production '
        'changes.'
    )
    return outcome


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', required=True)
    parser.add_argument('--after', required=True)
    parser.add_argument('--out')
    args = parser.parse_args()
    result = assess(
        json.loads(Path(args.before).read_text(encoding='utf-8')),
        json.loads(Path(args.after).read_text(encoding='utf-8')),
    )
    payload = json.dumps(result, indent=2, ensure_ascii=False) + '\n'
    if args.out:
        dest = Path(args.out)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(payload, encoding='utf-8')
    print(payload, end='')
    raise SystemExit(0 if result['pass'] else 1)


if __name__ == '__main__':
    main()
