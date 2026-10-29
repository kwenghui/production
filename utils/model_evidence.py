"""Bounded, JSON-valid model evidence; full verified tool results stay in the caller.

Never truncate a JSON string mid-field: partial numerical evidence is unsafe for
statistical narration. Oversized evidence is explicitly marked as unavailable
for narrative use, while the caller keeps the complete deterministic result.

Used by: utils/core_secure.py, tools/characterise_evidence_summary.py.
Tested in: tests/test_data_layer.py, tests/test_discovery_and_ingest.py.
"""

import json

# Chart data the model never needs to see (it is told only that a chart exists).
_VISUALIZATION_FIELDS = frozenset({"chart_spec", "chart_variants"})


def _encode(value):
    """Compact JSON of a value (unknown types as text)."""
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def compact_tool_result_for_model(result, *, max_chars: int = 24000):
    """Return valid JSON within the model-context limit, never partial values.

    On overflow, report that verified evidence was omitted. The model must not
    infer any statistical conclusions from the short diagnostic wrapper.
    """
    if max_chars < 2:
        raise ValueError("max_chars must allow at least a JSON object")
    if not isinstance(result, dict):
        payload = {"success": False, "error": "Malformed tool result."}
    else:
        payload = {key: value for key, value in result.items() if key not in _VISUALIZATION_FIELDS}
        payload["verified_chart_available"] = bool(result.get("chart_spec"))
        if isinstance(result.get("chart_spec"), dict):
            payload["chart_format"] = str(result["chart_spec"].get("kind", ""))

    encoded = _encode(payload)
    if len(encoded) <= max_chars:
        return encoded

    summarized = _safe_observation_summary(result, max_chars) if isinstance(result, dict) else None
    if summarized is not None:
        return summarized

    # Fail closed for model narration. Full evidence is still in tool_results,
    # but this model input has insufficient room to carry it without omission.
    candidates = (
        {
            "model_evidence_complete": False,
            "error": "Tool evidence exceeds model context budget; do not infer numerical values or "
            "trends.",
        },
        {"model_evidence_complete": False, "error": "Evidence omitted."},
        {"model_evidence_complete": False},
        {},
    )
    for candidate in candidates:
        packed = _encode(candidate)
        if len(packed) <= max_chars:
            return packed
    return "{}"


_MISSING_MARKERS = ('', '-', '..', 'na', 'NA')
#: Optional detail dropped, in this order, until the overview fits.
_DROPPABLE = (
    'warning',
    'first_to_last_absolute_change',
    'first_available',
    'last_available',
    'table_id',
    'series_id',
    'series',
    'unit',
    'frequency',
    'first_period',
    'last_period',
)


def _safe_observation_summary(result, max_chars):
    """A bounded, explicitly incomplete overview of homogeneous observations.

    Does not certify observations, interpolate gaps or replace complete evidence.
    Data are only summarized when the caller explicitly declares verification.
    Only one series of plain annual periods qualifies; returns JSON text or None.
    """
    observations = result.get('observations')
    if not (
        result.get('success') is True
        and result.get('verified') is True
        and isinstance(observations, list)
        and observations
        and _single_annual_series(observations)
    ):
        return None
    ordered = sorted(observations, key=lambda row: int(str(row['period'])))
    values = _observed_values(ordered)
    if values is None:
        return None
    observed, missing = values
    periods = [str(row['period']) for row in observations]
    overview = {
        'model_evidence_complete': False,
        'summary_only': True,
        'warning': 'Only a descriptive subset is shown. Do not claim a complete historical trend '
        'or infer missing years.',
        'source_verified_by_tool': True,
        'reporting_periods': len(ordered),
        'observed_periods': len(observed),
        'missing_periods': len(missing),
        'first_period': periods[0] if len(periods) == 1 else str(ordered[0]['period']),
        'last_period': str(ordered[-1]['period']),
    }
    for field in ('table_id', 'series_id', 'series', 'unit', 'frequency'):
        if field in result and isinstance(result[field], (str, int)):
            overview[field] = result[field]
    if observed:
        overview['first_available'] = {'period': observed[0][0], 'value': str(observed[0][1])}
        overview['last_available'] = {'period': observed[-1][0], 'value': str(observed[-1][1])}
    # Avoid a spurious 0-change trend when fewer than two periods have values.
    if len(observed) >= 2:
        overview['first_to_last_absolute_change'] = str(observed[-1][1] - observed[0][1])
    return _fitted(overview, max_chars)


def _single_annual_series(observations) -> bool:
    """One series, unit, frequency and category; unique four-digit years only.

    Mixing categories, units or series creates misleading trends, and only
    unambiguous annual periods can be ordered without extra metadata.
    """
    if not all(
        isinstance(row, dict) and 'period' in row and 'value' in row for row in observations
    ):
        return False
    for field in ('series', 'series_id', 'unit', 'frequency', 'category'):
        if len({str(row[field]) for row in observations if field in row}) > 1:
            return False
    periods = [str(row['period']) for row in observations]
    return len(set(periods)) == len(periods) and all(len(p) == 4 and p.isdigit() for p in periods)


def _observed_values(ordered):
    """([(period, Decimal)], [missing periods]), or None for any non-numeric value."""
    from decimal import Decimal, InvalidOperation

    observed, missing = [], []
    for row in ordered:
        raw = row['value']
        if raw is None or (isinstance(raw, str) and raw.strip() in _MISSING_MARKERS):
            missing.append(str(row['period']))
            continue
        if isinstance(raw, bool):
            return None
        try:
            number = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
        if not number.is_finite():
            return None
        observed.append((str(row['period']), number))
    return observed, missing


def _fitted(overview, max_chars):
    """The overview as JSON within max_chars, dropping optional detail; None if impossible.

    Detail is removed rather than emitting invalid or partially truncated JSON.
    """
    packed = _encode(overview)
    if len(packed) <= max_chars:
        return packed
    for field in _DROPPABLE:
        overview.pop(field, None)
        packed = _encode(overview)
        if len(packed) <= max_chars:
            return packed
    return None


def evidence_chunks_for_model(result, *, max_chars: int = 24000):
    """Produce independently valid, bounded JSON chunks for vetted observations.

    Intended for future orchestrators with an explicit chunk-processing budget.
    The current Sage caller still sends one output per tool invocation; this
    function does not silently inject multiple messages into that protocol.
    """
    if max_chars < 256:
        raise ValueError('max_chars must be at least 256 for evidence chunks')
    if not isinstance(result, dict) or not isinstance(result.get('observations'), list):
        return [compact_tool_result_for_model(result, max_chars=max_chars)]
    rows = result['observations']
    if not rows:
        return [compact_tool_result_for_model(result, max_chars=max_chars)]
    base = {
        key: value
        for key, value in result.items()
        if key not in _VISUALIZATION_FIELDS
        and key != 'observations'
        and isinstance(value, (str, bool, int, float, type(None)))
    }
    # Never silently claim that one chunk represents the entire series.
    base.update({'model_evidence_complete': False, 'chunked': True})
    chunks = []
    current = []
    for row in rows:
        candidate = current + [row]
        payload = dict(
            base,
            observations=candidate,
            chunk_index=len(chunks) + 1,
            chunk_count=len(rows),
            total_observations=len(rows),
        )
        if len(_encode(payload)) > max_chars:
            if not current:
                raise ValueError('Single observation exceeds max_chars; refusing to truncate.')
            chunks.append(current)
            current = [row]
            payload = dict(
                base,
                observations=current,
                chunk_index=len(chunks) + 1,
                chunk_count=len(rows),
                total_observations=len(rows),
            )
            if len(_encode(payload)) > max_chars:
                raise ValueError('Single observation exceeds max_chars; refusing to truncate.')
        else:
            current = candidate
    if current:
        chunks.append(current)
    encoded_chunks = []
    for index, chunk in enumerate(chunks, 1):
        payload = dict(
            base,
            observations=chunk,
            chunk_index=index,
            chunk_count=len(chunks),
            total_observations=len(rows),
        )
        packed = _encode(payload)
        if len(packed) > max_chars:
            raise ValueError('Chunk metadata exceeds max_chars; increase budget.')
        encoded_chunks.append(packed)
    return encoded_chunks
