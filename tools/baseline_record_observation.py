"""Record an actual Sage UI observation into a canonical regression baseline.

This utility never runs Sage, calls models, or fabricates statistical answers.
Provide a JSON observation copied from an actual run and identify its case ID.
"""

from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
from tools.baseline_record import template
from tools.baseline_regression_gate import CANONICAL, _validate

FIELDS = ('answer', 'sources', 'observations', 'warnings', 'elapsed_seconds', 'security_outcome')


def validate_observation(value):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError('Observation must contain exactly: ' + ', '.join(FIELDS))
    if not isinstance(value['answer'], str) or not value['answer'].strip():
        raise ValueError('A nonempty, observed answer is required')
    if not isinstance(value['security_outcome'], str) or not value['security_outcome'].strip():
        raise ValueError('A nonempty observed security_outcome is required')
    for field in ('sources', 'observations', 'warnings'):
        if not isinstance(value[field], list):
            raise ValueError(f'{field} must be a list')
    elapsed = value['elapsed_seconds']
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ValueError('elapsed_seconds must be a finite nonnegative number')
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError('Observation must be JSON serializable') from exc
    return value


def record(baseline, case_id, observation, *, replace=False):
    if case_id not in CANONICAL:
        raise ValueError('Unknown canonical case ID')
    if not isinstance(baseline, dict) or baseline.get('schema_version') != 1:
        raise ValueError('Unsupported baseline schema')
    cases = baseline.get('cases')
    if not isinstance(cases, list) or len(cases) != len(CANONICAL):
        raise ValueError('Baseline must contain all nine canonical cases')
    found = {c.get('id'): c for c in cases if isinstance(c, dict)}
    if (
        set(found) != set(CANONICAL)
        or len(found) != len(cases)
        or any(found[k].get('question') != q for k, q in CANONICAL.items())
    ):
        raise ValueError('Canonical case IDs/questions were changed')
    target = found[case_id]
    if target.get('result') is not None and not replace:
        raise ValueError('Case already recorded; use --replace to overwrite explicitly')
    # Validate before mutating to avoid corrupting the in-memory document.
    validate_observation(observation)
    target['result'] = observation
    return baseline


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--case', choices=tuple(CANONICAL), required=True)
    parser.add_argument('--observation', type=Path, required=True)
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    try:
        baseline = (
            json.loads(args.baseline.read_text(encoding='utf-8'))
            if args.baseline.exists()
            else template()
        )
        observation = json.loads(args.observation.read_text(encoding='utf-8'))
        record(baseline, args.case, observation, replace=args.replace)
        # Recheck the entire document, allowing other cases to remain unexecuted.
        for case in baseline['cases']:
            if case['result'] is not None:
                validate_observation(case['result'])
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        temp = args.baseline.with_name(args.baseline.name + '.tmp')
        temp.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
            encoding='utf-8',
        )
        temp.replace(args.baseline)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(1, f'Cannot record observation: {exc}\n')
    complete = sum(c['result'] is not None for c in baseline['cases'])
    print(
        f'Recorded {args.case}: {complete}/{len(CANONICAL)} cases. Incomplete baselines cannot '
        f'pass Phase 32.'
    )


if __name__ == '__main__':
    main()
