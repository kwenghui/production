"""Additional fail-closed integrity checks for Sage recorded regression baselines."""

from __future__ import annotations
import math
from tools.baseline_record import validate


def inspect_baseline(doc: dict) -> list[str]:
    errors: list[str] = []
    try:
        validate(doc)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return [f'Invalid baseline: {exc}']
    for case in doc['cases']:
        key = case.get('id', '<unknown>')
        result = case.get('result')
        if result is None:
            errors.append(f'{key}: unexecuted')
            continue
        if not isinstance(result.get('answer'), str) or not result['answer'].strip():
            errors.append(f'{key}: missing answer')
        elapsed = result.get('elapsed_seconds')
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed)
        ):
            errors.append(f'{key}: elapsed_seconds must be finite and numeric')
        if (
            not isinstance(result.get('security_outcome'), str)
            or not result['security_outcome'].strip()
        ):
            errors.append(f'{key}: missing security outcome')
        for field in ('sources', 'observations', 'warnings'):
            if not isinstance(result.get(field), list):
                errors.append(f'{key}: {field} must be a list')
    return errors
