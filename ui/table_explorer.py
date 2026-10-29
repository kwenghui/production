"""'Explore table further': browse every published series of the answer's table.

WHERE THIS FITS
===============
Below a verified answer, app.py calls :func:`render_table_explorer`. It shows
the ONE official table that best matches the question and lets the user chart
any of its series. It is independent of the AI text: every value drawn comes
straight from the official table, loaded again through the analytical core.

    render_table_explorer
     |- pick the primary table (utils/chart_gallery ranking)
     |- _render_source_heading      title, official link, licence attribution
     |- multi-dimensional "cube"  -> _cube_slice_fragment   (one slice at a time)
     |- flat time-series table    -> _render_series_explorer (tick series to chart)
     '- table could not be loaded -> _render_answer_chart_fallback

SAME RULES FOR EVERY TABLE
==========================
There is no code path for any particular table, subject or survey. A cube is
handled by the generic slicer (utils/cube_slice.py) whatever its subject; a
flat table by the generic series loader. Do not add topic-specific branches
or published numbers here: improve the generic helpers instead.

The analytical core is passed in (``core``) and used only through its PUBLIC
functions ``load_table_payload`` and ``retrieve_generic_singstat_table``.

Used by: ui/answer_view.py, tools/characterise_table_explorer.py.
Tested in: tests/test_core_modules.py, tests/test_longer_history.py.
"""

import hashlib
import logging
import math

import streamlit as st

from ui.components import (
    MAX_DRAWN_SERIES,
    apply_series_table_edits,
    filter_words,
    md_literal,
    render_lightweight_chart,
    series_number_key,
)
from utils.chart_gallery import (
    choose_primary_identified_table,
    create_single_table_chart,
    describe_identified_tables,
    group_selected_for_charts,
    load_complete_table_series,
    rank_identified_tables,
    table_default_selection,
)
from utils.cube_slice import crosstab_overview
from utils.series_defaults import (
    MAX_DEFAULT_SERIES,
    cap_default_selection,
    root_series_defaults,
    without_constant_wholes,
)

LOGGER = logging.getLogger("sage_ui")

#: Upper bound on series loaded from one table (a safety cap, not a sample).
MAX_TABLE_SERIES = 512
#: Series-table geometry, in pixels. Streamlit's default row (35 px) holds one
#: line; taller rows WRAP long text onto extra lines instead of cutting it off.
_HEADER_HEIGHT_PX = 35
_LINE_HEIGHT_PX = 21  # one wrapped line of table text
_ROW_PADDING_PX = 14  # space above and below the text in a row
#: Characters that fit on one line of the "Series" column (a conservative
#: estimate for a typical laptop window; a wider window simply leaves space).
_SERIES_CHARS_PER_LINE = 60
#: Rows never grow beyond this many lines; the full label is still shown when
#: the user hovers over or clicks the cell.
_MAX_WRAPPED_LINES = 4


def render_table_explorer(answer, core) -> bool:
    """Draw the table explorer for the current answer.

    Args:
        answer: the assistant message dict stored by ``process_question``.
        core:   the loaded analytical core (``utils.core_secure`` module).

    Returns:
        True when an official table's data were shown, False otherwise.
    """
    if st.session_state.get('auto_test_started'):
        return False
    question = st.session_state.get('active_question')
    if not isinstance(question, str) or answer.get('source_question') != question:
        return False  # Never show a table for a different (older) question.
    response = st.session_state.get('last_response')
    if not isinstance(response, dict):
        return False
    tables = rank_identified_tables(question, describe_identified_tables(response))
    primary, tied, _tied_ids = choose_primary_identified_table(question, tables, response, answer)
    if primary is None:
        return False

    st.markdown('### Explore table further')
    if tied:
        st.caption('Several tables match. Showing one source; refine the question if needed.')
    _render_source_heading(primary)

    if _is_multidimensional(primary):
        # Own fragment: choosing another slice re-runs only this section.
        _cube_slice_fragment(primary, question, core)
        return True

    gallery = load_complete_table_series(
        primary['table_id'], core.retrieve_generic_singstat_table, max_series=MAX_TABLE_SERIES
    )
    if not gallery['series']:
        # A title match is not a loaded table: never imply data that did not load.
        LOGGER.info('CHART_DECISION table=%s outcome=no_complete_table_data', primary['table_id'])
        _render_answer_chart_fallback(primary, response)
        return False
    _render_series_explorer(primary, question, gallery)
    return True


# --- Heading ------------------------------------------------------------------


def _render_source_heading(table) -> None:
    """Table title, official SingStat link and the open-data licence attribution."""
    from utils.sage_design import render_data_attribution

    st.markdown(md_literal(f"**{table['title']}** · SingStat {table['table_id']}"))
    st.markdown(f"[View official source on SingStat ↗]({table['source_url']})")
    render_data_attribution(table['title'], table['source_url'])  # Singapore Open Data Licence


def _is_multidimensional(table) -> bool:
    """Cube tables (crosstabs; SingStat IDs starting with D) need the slice explorer.

    In a cube a flat series ID / period key does not identify a single cell,
    so the time-series explorer would merge distinct dimensions.
    """
    return bool(table.get('is_crosstab') or str(table.get('table_id', '')).startswith('D'))


def _issue_logged_once(prefix, question, table_id, exc) -> None:
    """Log a non-fatal display issue once per (question, table, reason), not every rerun."""
    issue_key = (
        prefix
        + hashlib.sha256((question + str(table_id) + str(exc)).encode('utf-8')).hexdigest()[:16]
    )
    if st.session_state.get(issue_key):
        return
    LOGGER.info('%s %s: %s', prefix.rstrip('_'), table_id, exc)
    st.session_state[issue_key] = True


# --- Multi-dimensional tables ("cubes") ---------------------------------------


@st.fragment
def _cube_slice_fragment(table, question, core):
    """Show ONE unambiguous slice of a cube and let the user pick another.

    A cube has many slices (column group x period); slices are never combined,
    and only cells holding a single published value are shown.
    """
    from utils.sage_design import render_readable_table

    tid = table['table_id']
    try:
        payload = core.load_table_payload(tid)
        overview = crosstab_overview(
            payload, table_id=tid, source_url=table['source_url'], title=table['title']
        )
        overview = _let_user_pick_slice(overview, payload, table)
        st.info(md_literal(overview['note']))
        if overview.get('warning'):
            st.warning(md_literal(overview['warning']))
        render_lightweight_chart(overview['chart_spec'], key=f"sage_cube_slice_{tid}")
        _render_footnotes(overview.get('footnotes') or [])
        st.markdown('**Official observations for this slice**')
        render_readable_table(overview['period_table'] or overview['rows'], key='cube_slice_' + tid)
        st.caption(
            'Source: official SingStat table. Only cells with a single published value are '
            'shown; '
            'other column groups and periods are separate slices and are not combined.'
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        _issue_logged_once('sage_cube_slice_issue_', question, tid, exc)
        st.info(
            'This official source has multiple dimensions. A validated slice is required '
            'before drawing a trend.'
        )


def _let_user_pick_slice(overview, payload, table):
    """Offer column-group / period pickers; return the overview of the chosen slice."""
    options, shown = overview['options'], overview['slice']
    if len(options['groups']) <= 1 and len(options['periods']) <= 1:
        return overview
    tid = table['table_id']
    pick_group, pick_period = shown['column_group'], shown['period']
    group_col, period_col = (
        st.columns([3, 1]) if len(options['periods']) > 1 else (st.container(), None)
    )
    with group_col:
        if len(options['groups']) > 1:
            pick_group = st.selectbox(
                'Show',
                options['groups'],
                index=options['groups'].index(pick_group) if pick_group in options['groups'] else 0,
                key=f'sage_slice_group_{tid}',
            )
    if period_col is not None:
        with period_col:
            pick_period = st.selectbox(
                'Period',
                options['periods'],
                index=options['periods'].index(pick_period)
                if pick_period in options['periods']
                else 0,
                key=f'sage_slice_period_{tid}',
            )
    if (pick_group, pick_period) == (shown['column_group'], shown['period']):
        return overview
    try:
        return crosstab_overview(
            payload,
            table_id=tid,
            source_url=table['source_url'],
            title=table['title'],
            group=pick_group,
            period=pick_period,
        )
    except ValueError:
        st.caption('That selection has too few values to chart; showing the default instead.')
        return overview


def _render_footnotes(notes) -> None:
    """SingStat footnote markers (e.g. "Median1/") are hidden in labels; show the notes here."""
    if not notes:
        return
    body = '\n'.join('- ' + note.replace('$', '\\$') for note in notes)
    st.caption(
        '**Footnote' + ('s' if len(notes) > 1 else '') + ' (as published by SingStat)**\n\n' + body
    )


# --- Table could not be loaded: show the answer's own verified chart only -------


def _render_answer_chart_fallback(table, response) -> None:
    """Draw the verified answer-specific trend for this table, if there is one."""
    for result in response.get('tool_results') or []:
        if not _is_complete_verified_trend(result, table['table_id']):
            continue
        try:
            render_lightweight_chart(
                result['chart_spec'], key=f"sage_verified_preview_{table['table_id']}"
            )
            st.caption(
                'Verified answer-specific chart only. Full-table comparison is unavailable; '
                'other published series are not shown.'
            )
            return
        except (ValueError, TypeError, KeyError) as exc:
            LOGGER.warning(
                'Verified fallback chart failed %s: %s', table['table_id'], type(exc).__name__
            )
    st.caption('No verified time-series graph is available for this table.')
    LOGGER.info('CHART_DECISION table=%s outcome=no_verified_time_series', table['table_id'])


def _is_complete_verified_trend(result, table_id) -> bool:
    """A successful, source-verified trend for ``table_id`` with aligned values."""
    if not isinstance(result, dict) or result.get('table_id') != table_id:
        return False
    if result.get('success') is not True or result.get('chart_source_verified') is not True:
        return False
    spec = result.get('chart_spec')
    if not isinstance(spec, dict) or spec.get('kind') != 'trend':
        return False
    series, categories = spec.get('series'), spec.get('categories')
    if not isinstance(series, list) or not isinstance(categories, list):
        return False
    if not series or len(categories) < 2:
        return False
    return all(
        len(line.get('values', [])) == len(categories) for line in series if isinstance(line, dict)
    )


# --- Flat tables: tick series to chart them -----------------------------------


class _SelectionKeys:
    """Session-state keys for one (question, table) explorer, in one place."""

    def __init__(self, question, table_id) -> None:
        """Derive every key from a short hash of (question, table)."""
        tag = hashlib.sha256((question + table_id).encode('utf-8')).hexdigest()[:12]
        self.tag = tag
        self.initialised = f"sage_table_initialized_v3ak_{tag}"
        self.match_kind = f"{self.initialised}_kind"
        self.applied = f"sage_table_applied_selection_{tag}"  # what the chart shows
        self.draft = f"sage_table_draft_selection_{tag}"  # survives paging/filtering
        self.chart_cache = f"sage_table_chart_cached_{tag}"
        self.table_version = f"sage_series_table_version_{tag}"
        self.limit_hit = f"sage_series_table_limit_{tag}"
        self.filter = f"sage_series_filter_{tag}"
        self.chart_area = f"sage_chart_area_{tag}"
        self.clear_button = f"sage_clear_all_{tag}"

    def editor(self) -> str:
        """The data-editor key changes when the table must be rebuilt (new version)."""
        return f"sage_series_table_{self.tag}_{st.session_state.get(self.table_version, 0)}"

    def bump_version(self) -> None:
        """Force the series table to be rebuilt (its widget key changes)."""
        st.session_state[self.table_version] = st.session_state.get(self.table_version, 0) + 1


def _render_series_explorer(table, question, gallery) -> None:
    """Heading notes, default selection, then the chart + series table fragment."""
    entries = gallery['series']
    if gallery['limited']:
        st.warning(
            f"Showing the first {len(entries)} of {gallery['total_chartable']} "
            'chartable series. Further series are not plotted due to the safety cap.'
        )
    keys = _SelectionKeys(question, table['table_id'])
    _initialise_default_selection(keys, question, table, entries)
    match_kind = st.session_state.get(keys.match_kind)
    if match_kind == "related_unit":
        st.info(
            'Related measure: the selected series has a different unit from the requested '
            'statistic. The published values are not converted.'
        )
    elif match_kind == "related":
        st.markdown(
            '**Related measure:** These published series are relevant but may not '
            'measure the precise statistic in your question.'
        )
    if keys.draft not in st.session_state:
        st.session_state[keys.draft] = list(st.session_state.get(keys.applied, []))
    _series_selector_fragment(table, question, entries)


def _initialise_default_selection(keys, question, table, entries) -> None:
    """Tick the series that match the question, once per (question, table).

    Starts from the top-level series that match, then caps the count so a flat
    table of dozens of series does not open with every box ticked.
    """
    if st.session_state.get(keys.initialised):
        return
    used = answer_series_ids(st.session_state.get('last_response'), table.get('table_id'), entries)
    if used:
        # Open on the series the written answer is about, so the chart and the
        # "What the numbers show" bullets describe the same thing.
        defaults, match_kind = set(used[:MAX_DEFAULT_SERIES]), 'closest'
    else:
        defaults, match_kind = table_default_selection(question, table, entries, is_primary=True)
        defaults = root_series_defaults(entries, defaults, question)
        defaults = cap_default_selection(entries, defaults, question)
    defaults = without_constant_wholes(entries, defaults)  # no "Total = 100%" line
    st.session_state[keys.match_kind] = match_kind
    st.session_state[keys.applied] = list(defaults)
    st.session_state[keys.initialised] = True


def answer_series_ids(response, table_id, entries) -> list:
    """IDs of this table's series that the verified answer's tool results used, in order.

    Only successful, complete results for the same table count, and only IDs the
    explorer can draw; an empty list means "fall back to matching the question".
    """
    drawable = {str(x.get('series_id')) for x in entries}
    found = []
    results = (response or {}).get('tool_results') if isinstance(response, dict) else None
    for item in results or []:
        if not (
            isinstance(item, dict)
            and item.get('success') is True
            and str(item.get('table_id') or '') == str(table_id or '')
            and item.get('table_complete') is True
        ):
            continue
        for row in item.get('series_summary') or item.get('selected_series') or []:
            sid = str((row or {}).get('series_no') or '') if isinstance(row, dict) else ''
            if sid in drawable and sid not in found:
                found.append(sid)
    return found


@st.fragment
def _series_selector_fragment(table, question, entries):
    """The chart, then ONE table of every series with a tick box per row.

    Both live in this fragment, so a tick redraws the chart without re-running
    the whole page.
    """
    keys = _SelectionKeys(question, table['table_id'])
    with st.container(key=keys.chart_area):  # dimmed if an update is slow
        with st.spinner("Updating the chart…"):
            _draw_selected_chart(table, entries, keys)
    _render_series_table(table, entries, keys)


def _draw_selected_chart(table, entries, keys) -> None:
    """Chart the applied selection (at most MAX_DRAWN_SERIES lines)."""
    applied_ids = set(st.session_state.get(keys.applied, []))
    selected = [item for item in entries if item['series_id'] in applied_ids]
    if not selected:
        return  # Clearing all hides the chart, never the selection controls.
    if len(selected) > MAX_DRAWN_SERIES:
        st.caption(
            f'{len(selected)} series are ticked, but more than {MAX_DRAWN_SERIES} lines '
            f'are unreadable and slow the browser, so the first {MAX_DRAWN_SERIES} are '
            f'drawn.'
        )
        selected = selected[:MAX_DRAWN_SERIES]
    specs, hidden_groups = _chart_specs_for(table, selected, keys)
    if not specs:
        st.warning('Selected series have insufficient verified numerical observations for a graph.')
    for chart_index, spec in enumerate(specs):
        try:
            render_lightweight_chart(
                spec, key=f"sage_table_chart_{table['table_id']}_{chart_index}"
            )
            if spec.get('mixed_original_units'):
                st.caption(
                    'Different published units are indexed to 100 for visual comparison; '
                    'hover for the original values and units.'
                )
        except (ValueError, TypeError, KeyError) as exc:
            LOGGER.warning('Table graph error %s: %s', table['table_id'], type(exc).__name__)
            st.warning(
                'The selected series could not be rendered. Verified source values were not '
                'altered.'
            )
    if hidden_groups:
        st.caption(
            f'{hidden_groups} more group(s) selected could not be drawn. '
            'Remove some series to draw the rest.'
        )


def _chart_specs_for(table, selected, keys) -> tuple:
    """(specs, hidden group count) for the selection, cached until it changes."""
    signature = tuple((item['series_id'], tuple(sorted(item['observations']))) for item in selected)
    cached = st.session_state.get(keys.chart_cache)
    if isinstance(cached, dict) and cached.get('signature') == signature:
        return cached['specs'], cached['hidden']
    specs, hidden = group_selected_for_charts(
        table['table_id'], selected, table_title=table['title']
    )
    if hidden:
        # Too many separate groups: draw every ticked series together instead.
        single = create_single_table_chart(table['table_id'], selected, table_title=table['title'])
        if single:
            specs, hidden = [single], 0
    st.session_state[keys.chart_cache] = {'signature': signature, 'specs': specs, 'hidden': hidden}
    return specs, hidden


def _wrapped_row_height(labels):
    """Row height (px) that lets the longest series name wrap onto extra lines.

    One shared height for all rows (Streamlit tables have uniform rows): just
    enough lines for the longest label, at most _MAX_WRAPPED_LINES. Short
    labels keep the normal single-line row.
    """
    longest = max((len(label) for label in labels), default=0)
    lines = min(_MAX_WRAPPED_LINES, max(1, math.ceil(longest / _SERIES_CHARS_PER_LINE)))
    return _ROW_PADDING_PX + _LINE_HEIGHT_PX * lines


def _render_series_table(table, entries, keys) -> None:
    """Filter box, Clear button and the tick-box table of every series."""
    import pandas as pd  # Imported here: only needed once a table is shown.

    ordered = sorted(entries, key=lambda item: series_number_key(item['series_id']))
    words = filter_words(st.session_state.get(keys.filter))
    visible = [
        item
        for item in ordered
        if all(
            word
            in (
                str(item['series_id']) + " " + str(item['series_name']) + " " + str(item['unit'])
            ).casefold()
            for word in words
        )
    ]
    visible_ids = [item['series_id'] for item in visible]
    editor_key = keys.editor()

    def _apply_table_edits() -> None:
        """Apply ticks from the series table to the selection (max MAX_DRAWN_SERIES)."""
        edits = (st.session_state.get(editor_key) or {}).get("edited_rows") or {}
        chosen, hit_limit = apply_series_table_edits(
            edits, st.session_state.get(keys.draft, []), visible_ids, MAX_DRAWN_SERIES
        )
        st.session_state[keys.draft] = chosen
        st.session_state[keys.applied] = chosen
        if hit_limit:
            # The refused tick is still shown in the grid: rebuild it from the selection.
            st.session_state[keys.limit_hit] = True
            keys.bump_version()
        LOGGER.info(
            'CHART_DECISION table=%s outcome=user_table_selection selected_count=%d',
            table['table_id'],
            len(chosen),
        )

    def _clear_selection() -> None:
        """Untick every series and rebuild the table."""
        st.session_state[keys.draft] = []
        st.session_state[keys.applied] = []
        keys.bump_version()

    shown_now = set(st.session_state.get(keys.applied, []))
    st.markdown(
        f"**All {len(ordered)} series in this table** · {len(shown_now)} shown on the chart"
    )
    filter_col, clear_col = st.columns([5, 1], vertical_alignment="bottom")
    with filter_col:
        st.text_input(
            "Filter the list",
            key=keys.filter,
            max_chars=100,
            on_change=keys.bump_version,
            placeholder="Type keywords, e.g. hotel occupancy, or a number such as 1.4",
        )
    with clear_col:
        st.button(
            "Clear selection",
            key=keys.clear_button,
            width="stretch",
            disabled=not shown_now,
            on_click=_clear_selection,
        )
    if words:
        st.caption(f"Showing {len(visible)} of {len(ordered)} series matching the filter.")
    st.caption("Tick a box to show that series on the chart; untick to remove it.")
    if st.session_state.pop(keys.limit_hit, False):
        st.caption(
            f"At most {MAX_DRAWN_SERIES} series can be shown at once. "
            "Untick one before adding another."
        )
    if not visible:
        st.caption("No series match those keywords.")
    row_height = _wrapped_row_height([str(item['series_name']) for item in visible])
    st.data_editor(
        pd.DataFrame(
            [
                {
                    "Show": item['series_id'] in shown_now,
                    "No.": str(item['series_id']),
                    "Series": str(item['series_name']),
                    "Unit": str(item['unit']),
                }
                for item in visible
            ],
            columns=["Show", "No.", "Series", "Unit"],
        ),
        key=editor_key,
        on_change=_apply_table_edits,
        hide_index=True,
        width="stretch",
        num_rows="fixed",
        disabled=["No.", "Series", "Unit"],
        column_config={
            "Show": st.column_config.CheckboxColumn("Show", width=70),
            "No.": st.column_config.TextColumn("No.", width=80),
            "Series": st.column_config.TextColumn("Series", width="large"),
            "Unit": st.column_config.TextColumn("Unit", width="medium"),
        },
        row_height=row_height,  # rows taller than one line wrap long series names
        # Tall enough for EVERY row: no scrollbar inside the table; the page scrolls.
        height=_HEADER_HEIGHT_PX + row_height * max(len(visible), 1) + 3,
    )
