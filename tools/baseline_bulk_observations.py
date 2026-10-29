"""Import real, manually captured Sage observations in one atomic operation.

Input JSON is a list of {"id": canonical_case_id, "question": exact_question,
"result": observation}. Never creates observations or calls Sage.
"""

from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
from tools.baseline_record import template
from tools.baseline_regression_gate import CANONICAL
from tools.baseline_record_observation import record
from tools.baseline_worklist import worklist


def import_observations(baseline, entries, *, replace=False):
    if not isinstance(entries, list) or not entries:
        raise ValueError('Expected a nonempty JSON array of captured observations')
    updated = copy.deepcopy(baseline)
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'id', 'question', 'result'}:
            raise ValueError('Each entry must have exactly id, question, result')
        case_id = entry['id']
        if not isinstance(case_id, str) or case_id not in CANONICAL:
            raise ValueError('Unknown case ID')
        if case_id in seen:
            raise ValueError('Duplicate case ID in batch')
        seen.add(case_id)
        if entry['question'] != CANONICAL[case_id]:
            raise ValueError('Question does not match canonical question')
        record(updated, case_id, entry['result'], replace=replace)
    worklist(updated)  # validate every existing and newly recorded observation
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--observations', type=Path, required=True)
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    try:
        baseline = (
            json.loads(args.baseline.read_text(encoding='utf-8'))
            if args.baseline.exists()
            else template()
        )
        entries = json.loads(args.observations.read_text(encoding='utf-8'))
        updated = import_observations(baseline, entries, replace=args.replace)
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        temp = args.baseline.with_name(args.baseline.name + '.tmp')
        try:
            temp.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                encoding='utf-8',
            )
            temp.replace(args.baseline)
        finally:
            temp.unlink(missing_ok=True)
        summary = worklist(updated)
        print(
            f"Imported {len(entries)} observed cases; {summary['recorded']}/{summary['total']} "
            f"recorded. Recorded is not verified."
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.exit(1, f'Cannot import observations: {exc}\n')


if __name__ == '__main__':
    main()
