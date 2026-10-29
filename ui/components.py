"""Small reusable Streamlit pieces shared by the answer page and the table explorer.

* md_literal              - show official text through Markdown with "$" kept literal
* filter_words            - sanitised keywords from a filter box
* series_number_key       - sort official series numbers numerically (1 < 1.2 < 1.10)
* apply_series_table_edits - apply ticks from the series table, respecting a limit
* cached_vega_spec        - build a chart's Vega-Lite spec once per process
* render_lightweight_chart - draw a verified chart spec (bounded size)

Only ``render_lightweight_chart`` touches Streamlit; the rest are pure and can
be unit-tested directly.

Used by: ui/answer_view.py, ui/question_form.py, ui/table_explorer.py.
Tested in: tests/test_small_modules.py.
"""

import copy
import hashlib
import json
import re
import unicodedata

import streamlit as st
from streamlit.errors import StreamlitAPIException

from utils import process_state as _process_state

#: Lines drawn at once in the table explorer; more are unreadable and slow.
MAX_DRAWN_SERIES = 12
#: Above this many points a chart is omitted (never sampled), to protect browsers.
MAX_CHART_POINTS = 3200

_FILTER_ALLOWED = re.compile(r"[^\w\s.,&()/%'+:-]", re.UNICODE)


def filter_words(text, *, max_chars: int = 100, max_words: int = 8):
    """Search words from the series filter box, cleaned of anything that is not plain text.

    The filter is only ever compared against series names in Python -- never executed,
    sent to a database or shown back as HTML. Cleaning it anyway keeps it harmless if
    that ever changes: control characters and symbols such as < > " ; ` { } $ \\ are
    removed, and the length and number of words are capped.
    """
    value = unicodedata.normalize("NFKC", str(text or ""))[:max_chars]
    value = "".join(ch for ch in value if ch.isprintable())
    value = _FILTER_ALLOWED.sub(" ", value)
    return value.casefold().split()[:max_words]


_VEGA_CACHE_LOCK = _process_state.lock("vega_cache")
_VEGA_CACHE = _process_state.shared("vega_cache", dict)
_VEGA_CACHE_MAX = 64


def cached_vega_spec(spec, all_ticks: bool = False):
    """The finished Vega-Lite spec for a chart, built and checked ONCE per process.

    Charts depend only on their verified data (the chart theme is fixed), so the same
    selection always gives the same spec. Re-drawing a chart (a tick, typing in the
    filter, a rating) reuses it instead of rebuilding and re-validating it.
    """
    from utils.lightweight_charts import to_altair_chart
    from utils.sage_design import style_altair_chart

    key = hashlib.sha256(
        (json.dumps(spec, sort_keys=True, default=str) + f"|{bool(all_ticks)}").encode("utf-8")
    ).hexdigest()
    with _VEGA_CACHE_LOCK:
        found = _VEGA_CACHE.get(key)
    if found is not None:
        return found
    vega = style_altair_chart(
        to_altair_chart(spec, label_mode="Balanced", all_ticks=all_ticks)
    ).to_dict()
    with _VEGA_CACHE_LOCK:
        if len(_VEGA_CACHE) >= _VEGA_CACHE_MAX:
            _VEGA_CACHE.pop(next(iter(_VEGA_CACHE)))
        _VEGA_CACHE[key] = vega
    return vega


def apply_series_table_edits(edits, chosen, row_ids, limit) -> tuple:
    """Apply ticks/unticks from the series table: returns (new selection, limit was hit).

    `edits` is Streamlit's {row: {"Show": bool}}; `row_ids` are the table's series IDs in
    row order. Adding beyond `limit` is refused; everything else is applied in row order.
    """
    chosen, hit_limit = list(chosen or []), False
    for row, change in sorted((edits or {}).items(), key=lambda kv: int(kv[0])):
        if not isinstance(change, dict) or "Show" not in change or not 0 <= int(row) < len(row_ids):
            continue
        sid = row_ids[int(row)]
        if change["Show"] and sid not in chosen:
            if len(chosen) >= limit:
                hit_limit = True
                continue
            chosen.append(sid)
        elif not change["Show"] and sid in chosen:
            chosen.remove(sid)
    return chosen, hit_limit


def series_number_key(series_id) -> tuple:
    """Sort key for official series numbers: 1 < 1.1 < 1.2 < 1.10 < 2 (numbers, not text).

    Non-numeric parts (e.g. "cube:1") sort after numeric ones, by text.
    """
    parts = []
    for part in str(series_id or "").replace(":", ".").split("."):
        part = part.strip()
        parts.append((0, int(part), "") if part.isdigit() else (1, 0, part.casefold()))
    return tuple(parts)


def md_literal(text):
    """Official text shown through Markdown, with "$" kept literal.

    Streamlit renders text between two "$" signs as a maths formula, so a SingStat
    label such as "$15,000 - $17,499" lost both dollar signs and turned into
    formula text. Escaping keeps the amount exactly as published.
    """
    return re.sub(r"(?<!\\)\$", r"\\$", str(text if text is not None else ""))


def render_lightweight_chart(spec, *, key=None) -> None:
    """Use bounded Altair/Vega-Lite output, retaining original verified data.

    The chart is omitted, not sampled, if its browser payload is excessive.
    No observations, official sources, answer text, or stored results change.
    """
    if not isinstance(spec, dict):
        raise ValueError('Chart specification must be a dictionary')
    series = spec.get('series') or []
    categories = spec.get('categories') or []
    if len(series) * len(categories) > MAX_CHART_POINTS:
        st.info(
            'Too many points for a responsive graph. Choose fewer series. Verified source '
            'figures are unchanged.'
        )
        return
    from utils.chart_order import (
        SORT_OPTIONS,
        default_sort_mode,
        is_sortable,
        lines_by_latest,
        sort_spec,
    )
    from utils.lightweight_charts import to_altair_chart
    from utils.sage_design import style_altair_chart

    mode = SORT_OPTIONS[0]
    if is_sortable(spec):
        # Re-order only: the same verified values, shown alphabetically or highest first.
        chart_id = (
            key
            or hashlib.sha256(
                (str(spec.get('title')) + '|' + '|'.join(map(str, spec['categories']))).encode(
                    'utf-8'
                )
            ).hexdigest()[:12]
        )
        mode = st.radio(
            "Sort bars",
            SORT_OPTIONS,
            horizontal=True,
            key=f"sage_sort_{chart_id}",
            index=SORT_OPTIONS.index(default_sort_mode(spec)),
        )
    shown = lines_by_latest(sort_spec(spec, mode))
    all_ticks = mode != SORT_OPTIONS[0]
    try:
        vega = cached_vega_spec(shown, all_ticks)
        # Already checked and converted once: hand Streamlit the finished spec, so Altair
        # does not re-check the whole chart on every click (it did so twice per draw).
        st.vega_lite_chart(spec=copy.deepcopy(vega), width="stretch", theme=None, key=key)
    except (TypeError, StreamlitAPIException):
        # Older Streamlit without these arguments: the plain Altair route.
        st.altair_chart(
            style_altair_chart(to_altair_chart(shown, label_mode="Balanced", all_ticks=all_ticks)),
            width="stretch",
            theme=None,
            key=key,
        )
