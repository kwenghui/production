"""Conservative, offline-verifiable numerical evidence for Sage presentation.

Do not treat metadata or a successful catalogue lookup as numerical evidence.
Only reconciled typed series summaries from a successful, complete official query
can introduce calculated endpoint deltas and percentages. The caller must first
filter source URLs with its official allow-list.

Used by: ui/answer_pipeline.py, utils/answer_verification.py.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_HALF_UP


def _token(value: float) -> str:
    """A number as text the way answers write it ("12" not "12.0")."""
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def reconciled_summary_tokens(evidence, is_official):
    """Return canonical tokens for typed observations and checked arithmetic.

    The exact endpoint difference must reconcile with the tool-provided difference.
    A percentage change is allowed only against a nonzero known starting value.
    Never invent breakdowns or infer numbers from the table's title.
    """
    values = set()
    for result in evidence or []:
        if (
            not isinstance(result, dict)
            or result.get('success') is not True
            or result.get('table_complete') is not True
            or result.get('analysis_ready') is False
            or not is_official(result.get('source_url'))
        ):
            continue
        for series in result.get('series_summary') or []:
            if not isinstance(series, dict):
                continue
            parts = [
                series.get(key)
                for key in ('earliest_value', 'latest_value', 'change_latest_minus_earliest')
            ]
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in parts):
                continue
            start, end, delta = (Decimal(str(x)) for x in parts)
            # A six-decimal round-trip in the retrieval layer is normal; do not
            # permit broad relative tolerances on statistical claims.
            if abs((end - start) - delta) > Decimal('0.000001'):
                continue
            for item in (start, end, delta, abs(delta)):
                values.add(format(item.normalize(), 'f'))
                if item == item.to_integral_value():
                    values.add(str(int(item)))
            if start != 0:
                pct = abs(delta / start * 100)
                # Only accept percentage figures explicitly marked with '%'.
                for places in (1, 2):
                    quant = Decimal(1).scaleb(-places)
                    values.add(
                        ('percentage', format(pct.quantize(quant, rounding=ROUND_HALF_UP), 'f'))
                    )
    return values


def bibliographic_number_stripped_answer(answer):
    """Remove only identifiable NON-STATISTICAL publication references.

    Avoid a blanket year exception: years appearing in real result statements
    must still come from the query, source metadata or successful observations.
    """
    content = str(answer or '')
    content = re.sub(r'https?://[^\s)>]+', ' ', content, flags=re.I)
    content = re.sub(
        r'\b(?:SingStat\s+)?(?:table|figure|statistical\s+release)\s*' r'(?:no\.?\s*)?\d{1,4}\b',
        ' ',
        content,
        flags=re.I,
    )
    content = re.sub(r'\bCOVID[-\u2013]19\b', 'COVID', content, flags=re.I)
    content = re.sub(
        r'\bCensus\s+of\s+Population\s+\d{4}\b', 'Census of Population', content, flags=re.I
    )
    content = re.sub(r'\bCensus\s+\d{4}\b', 'Census', content, flags=re.I)
    return content


def required_comparison_covered(question, full_answer, chart_spec, required_groups, covers) -> bool:
    """Classify a chart-dependent pair request without trusting prose alone."""
    needed = required_groups(question)
    if not needed:
        return True
    # A pairing is only complete when the actual plotted categories/series
    # cover ALL groups. Merely naming a group in a generated paragraph fails.
    return bool(isinstance(chart_spec, dict) and covers(question, chart_spec))
