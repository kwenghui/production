"""The question box and table discovery shown before an answer.

* render_question_controls - the editable question box with Submit / Clear.
  Submit only QUEUES the question (submit_manual_question); it runs on the
  next pass, so the page clears instantly.
* Discovery - when several official tables could answer a typed question,
  run_deferred_discovery finds them and render_discovery_choices lets the
  user pick one (or search more broadly) before any analysis runs.

Used by: app.py.
"""

from pathlib import Path

from streamlit.errors import StreamlitAPIException
import streamlit as st

from ui.components import md_literal
from ui.config import MAX_UI_INPUT_CHARS
from ui.core_loader import build_core_history
from ui.session import clear_previous_results, reset_like_clear
from utils.discovery_service import validate_selection
from utils.question_pipeline import plan_question_discovery
from ui.sidebar import advance_sidebar_question


def render_header(_total_questions) -> None:
    """Show the responsive Sage identity above the unchanged question workflow."""
    from utils.sage_design import render_hero, render_landing_cards

    render_hero()
    render_landing_cards()


def submit_manual_question(current_sample=None) -> None:
    """Submit callback: clear output BEFORE Streamlit renders any widgets.

    A form-submit callback receives the submitted text_area value from
    session_state, so the user can edit a sample and submit it once.
    """
    question = (st.session_state.get("question_editor") or "").strip()
    if not question:
        # Exactly the same random-question path as the sidebar Surprise me button.
        advance_sidebar_question("random")
        return
    # Preserve dialogue only for an explicitly typed submission. Capture it BEFORE the reset
    # (which, like Clear, wipes the messages), then put the typed question back.
    history = build_core_history()
    reset_like_clear()
    st.session_state.question_editor = question
    st.session_state.submit_history = history
    clear_previous_results()
    st.session_state.sage_submitted_question = question
    st.session_state.sage_question_editing = False
    is_manual = current_sample is None or question != current_sample["question"]
    if is_manual:
        st.session_state.pending_test_scope = None
        st.session_state.pending_test_question = None
    else:
        st.session_state.pending_test_question = question
        st.session_state.pending_test_scope = (
            "young_adults_20_35" if "Young Adults" in current_sample["perspective"] else None
        )
    # Interactive discovery runs only on an explicit manual Submit. Demo and
    # sample-navigation questions remain deterministic and context-isolated.
    if is_manual and not st.session_state.get("auto_test_bank_hash"):
        # Planning searches the catalogue (keyword + semantic search) and calls the model, which
        # takes
        # seconds. Doing that inside this callback leaves the OLD page frozen and faded until it
        # ends,
        # so it is deferred to the processing step, which runs after the page has cleared.
        st.session_state.deferred_discovery = question
    st.session_state.queued_question = (question, True)


def question_editor_height(question):
    """Estimate enough room for the full wrapped sample on ordinary desktops.

    A pixel height is compatible with older Streamlit versions that support
    st.fragment but not the newer text_area(height="content") option. Longer
    questions can still be viewed by scrolling or resizing the text box.
    """
    lines = str(question or "").splitlines() or [""]
    # A conservative wrap estimate also accommodates narrower browser windows.
    visual_lines = sum(max(1, (len(line) + 57) // 58) for line in lines)
    return min(520, max(118, 48 + visual_lines * 29))


def clear_question_editor() -> None:
    """Return Sage to a fresh, empty page without touching persistent data.

    Invoked as a Streamlit widget callback, before the next full app rerun.
    All old answer/chart/widget/navigation state is removed together, so
    neither the question pane nor the right-hand response can retain results.
    Keep the loaded core to avoid repeating expensive engine initialization.
    Streamlit secrets, browser theme, SQLite and ChromaDB are not session state.
    """
    loaded_core = st.session_state.get("core")
    chosen_theme = st.session_state.get("sage_theme_choice")
    signed_in = st.session_state.get("sage_authenticated") is True
    # Sign-in timestamps (utils/auth.py) and the rate-limit identity must survive
    # Clear. Previously Clear issued a new identity, i.e. a fresh question quota.
    retained = {
        key: st.session_state[key]
        for key in (
            "sage_auth_at",
            "sage_auth_seen",
            "_sage_security_session_id",
            "_sage_auth_role",
        )
        if key in st.session_state
    }
    # Snapshot keys: deleting from session_state while iterating is unsafe.
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    # This single retained object is infrastructure, not displayed content.
    if loaded_core is not None:
        st.session_state["core"] = loaded_core
    # The chosen appearance is a preference, not an answer, so Clear keeps it.
    if chosen_theme is not None:
        st.session_state["sage_theme_choice"] = chosen_theme
    # Clearing the page must never sign the user out.
    if signed_in:
        st.session_state["sage_authenticated"] = True
    for key, value in retained.items():
        st.session_state[key] = value
    # The caller is a button/form callback. Streamlit automatically performs
    # the full script rerun and initialise_session_state() restores defaults.


def enable_question_editing() -> None:
    """Restore the exact submitted question for editing, preserving the answer."""
    submitted = str(st.session_state.get("sage_submitted_question") or "")
    if not submitted:
        submitted = str(st.session_state.get("question_editor") or "")
    # Run as a button callback, before the text-area widget is created.
    st.session_state.question_editor = submitted
    st.session_state.sage_question_editing = True


def render_manual_question_form(current_sample=None) -> None:
    """Question and right-hand actions, with preserved draft and copy control.

    Use keyed Streamlit containers rather than brittle column-position selectors.
    Both buttons have explicit heights, and the label sits ABOVE the entire row.
    """
    busy = bool(st.session_state.get("auto_test_bank_hash"))
    question = str(st.session_state.get("question_editor") or "")
    submitted = str(st.session_state.get("sage_submitted_question") or question)
    editing = bool(st.session_state.get("sage_question_editing", True)) or not submitted.strip()
    shown_question = question if editing else submitted
    # Fixed visual geometry makes the action stack align regardless of text length.
    # Long question text scrolls inside the editor rather than altering alignment.
    field_height = 160

    with st.container(key="sage_question_layout"):
        # One simple heading; no label over the action column or help icon.
        st.markdown('<div class="sage-field-label">Your question</div>', unsafe_allow_html=True)
        if editing:
            with st.form(
                "sage_question_form", clear_on_submit=False, enter_to_submit=False, border=False
            ):
                field, controls = st.columns([5, 1.45], gap="small", vertical_alignment="top")
                with field:
                    editor_options = dict(
                        key="question_editor",
                        label_visibility="collapsed",
                        max_chars=MAX_UI_INPUT_CHARS,
                        placeholder="Not sure what to ask? Click Submit for a little surprise, or "
                        "type your own question!",
                    )
                    try:
                        # Newer Streamlit: the box grows with its content as you type.
                        st.text_area("Question", height="content", **editor_options)
                    except (TypeError, StreamlitAPIException):
                        # Older Streamlit rejects height="content". Any other error is a
                        # real defect and must propagate, not be masked by this fallback.
                        st.text_area(
                            "Question",
                            height=question_editor_height(shown_question),
                            **editor_options,
                        )
                with controls:
                    with st.container(key="sage_question_controls"):
                        st.form_submit_button(
                            "Submit ✨",
                            key="sage_question_submit",
                            type="primary",
                            width="stretch",
                            disabled=busy,
                            on_click=submit_manual_question,
                            args=(current_sample,),
                        )
                        st.form_submit_button(
                            "Clear",
                            key="sage_question_clear_edit",
                            type="secondary",
                            width="stretch",
                            disabled=busy,
                            on_click=clear_question_editor,
                        )
        else:
            field, controls = st.columns([5, 1.45], gap="small", vertical_alignment="top")
            with field:
                with st.container(key="sage_question_readonly"):
                    st.code(submitted, language=None, wrap_lines=True)
            with controls:
                with st.container(key="sage_question_controls"):
                    st.button(
                        "Modify",
                        key="sage_question_modify",
                        type="secondary",
                        width="stretch",
                        disabled=busy,
                        on_click=enable_question_editing,
                    )
                    st.button(
                        "Clear",
                        key="sage_question_clear_view",
                        type="secondary",
                        width="stretch",
                        disabled=busy,
                        on_click=clear_question_editor,
                    )


def continue_discovered_analysis() -> None:
    """Continue the ORIGINAL question with an explicitly selected source.

    Called by the Streamlit button before widget rendering. Prevent stale
    selections, injected IDs and double execution of the same query.
    """
    pending = st.session_state.get('discovery_pending')
    selected = st.session_state.get('sage_discovery_other_table') or st.session_state.get(
        'sage_discovery_table'
    )
    if not isinstance(pending, dict) or not validate_selection(pending.get('plan'), selected):
        st.session_state.question_feedback = (
            'That source selection is no longer available. ' 'Please submit the question again.'
        )
        return
    original = pending.get('question')
    if not isinstance(original, str) or not original.strip():
        st.session_state.question_feedback = (
            'The original question is unavailable. Please submit ' 'it again.'
        )
        return
    clear_previous_results()
    st.session_state.discovery_confirmed_table = {'question': original, 'table_id': selected}
    st.session_state.queued_question = (original, True)
    st.session_state.question_editor = original
    st.session_state.submit_history = []  # A choice is an isolated continuation.


def continue_discovery_broader_search() -> None:
    """Explicitly request deeper discovery when local titles were insufficient.

    We do not bind a nearby, incompatible table or claim it is a valid answer.
    The normal Sage official retrieval and numeric verification remain active.
    """
    pending = st.session_state.get('discovery_pending')
    if not isinstance(pending, dict) or (pending.get('plan') or {}).get('action') != 'review':
        return
    original = pending.get('question')
    if not isinstance(original, str) or not original.strip():
        return
    clear_previous_results()
    st.session_state.discovery_pending = None
    st.session_state.discovery_source_hint = None
    st.session_state.discovery_confirmed_table = None
    st.session_state.queued_question = (original, True)
    st.session_state.question_editor = original
    st.session_state.submit_history = []


def clear_optional_discovery_selection() -> None:
    """Choosing a main option overrides any previously selected extra table."""
    st.session_state.sage_discovery_other_table = ''


def render_discovery_choices() -> bool:
    """Display plain-English measures first; show table metadata for auditing."""
    pending = st.session_state.get('discovery_pending')
    if not isinstance(pending, dict):
        return False
    plan = pending.get('plan') or {}
    choices = plan.get('choices') or []
    if plan.get('action') == 'review':
        st.subheader('No exact table match in the local index')
        st.info(
            'Sage could not confirm a table matching the requested measure '
            'and reporting frequency from its local metadata. This does '
            'not mean that the statistic is unavailable in SingStat.'
        )
        if plan.get('requested_frequency'):
            st.write('Requested frequency:', plan['requested_frequency'])
        if plan.get('requested_measure'):
            st.write('Requested measure:', plan['requested_measure'].replace('_', ' '))
        if plan.get('requested_unit'):
            st.write('Requested unit:', plan['requested_unit'].replace('_', ' '))
        nearby = plan.get('near_matches') or []
        if nearby:
            with st.expander('Related tables excluded from automatic selection', expanded=False):
                for item in nearby:
                    st.write(md_literal(f"{item['table_id']} — {item['title']}"))
                    st.caption(
                        md_literal(
                            f"Not an exact match: "
                            f"{item.get('incompatibility', 'different measure or frequency')}"
                        )
                    )
                    st.link_button(f"Open SingStat {item['table_id']}", item['url'])
        st.button(
            'Search more official tables',
            key='sage_discovery_broader_search',
            on_click=continue_discovery_broader_search,
            width='stretch',
        )
        st.caption(
            'This broader search is not guaranteed to find a suitable series. '
            'Any resulting figures or charts must still pass official-data verification.'
        )
        return True
    if not choices:
        return False
    st.subheader('Choose what to analyse')
    st.write(
        'Sage found several published measures related to your question. '
        'Select the one you intended; your original question will continue automatically.'
    )
    options = [r['table_id'] for r in choices]
    labels = {
        r['table_id']: f"{r['concept_label']} — {r['title']} ({r['table_id']})" for r in choices
    }
    current = st.session_state.get('sage_discovery_table')
    if current not in options:
        st.session_state.sage_discovery_table = options[0]
    st.radio(
        'Measure to analyse',
        options=options,
        format_func=lambda value: labels[value],
        key='sage_discovery_table',
        label_visibility='collapsed',
        on_change=clear_optional_discovery_selection,
    )
    other_rows = [row for row in plan.get('all_matches', []) if row['table_id'] not in options]
    if other_rows:
        with st.expander(f'Browse {len(other_rows)} additional matching tables', expanded=False):
            other_options = [''] + [row['table_id'] for row in other_rows]
            label_by_id = {
                row['table_id']: f"{row['title']} ({row['table_id']})" for row in other_rows
            }
            if st.session_state.get('sage_discovery_other_table') not in other_options:
                st.session_state.sage_discovery_other_table = ''
            st.selectbox(
                'Other official table',
                other_options,
                format_func=lambda tid: label_by_id.get(tid, 'Select another table…'),
                key='sage_discovery_other_table',
            )
    else:
        st.session_state.sage_discovery_other_table = ''
    if plan.get('requested_unit'):
        st.caption(
            'Requested measure: '
            + plan['requested_unit'].replace('_', ' ')
            + '. The selected table must also contain the requested population/series.'
        )
    st.caption(
        'Options are drawn from the local SingStat metadata index. They are '
        'not guaranteed to cover the entire live catalogue. A table title '
        'does not verify numerical observations.'
    )
    st.button(
        'Continue analysis',
        key='sage_discovery_continue',
        type='secondary',
        on_click=continue_discovered_analysis,
        width='stretch',
    )
    with st.expander('Official table details', expanded=False):
        for item in choices:
            st.write(md_literal(f"{item['table_id']} — {item['title']}"))
            st.link_button(f"Open SingStat {item['table_id']}", item['url'])
    return True


def render_question_controls(flat_questions, *, options=None) -> None:
    """Keep the question editor full-width; the navigation lives in the sidebar."""
    if not flat_questions:
        st.warning("No sample questions are available.")
        return
    current = max(0, min(int(st.session_state.test_question_index), len(flat_questions) - 1))
    if st.session_state.get("question_editor") is None:
        st.session_state.question_editor = ""
    render_manual_question_form(flat_questions[current])


def run_deferred_discovery(question) -> bool:
    """Interactive discovery for a typed question. Returns True when the user must pick a source.

    Runs in the processing step (page already cleared, progress line showing), outside the
    per-question AI budget exactly as it did when it ran inside the Submit callback.
    """
    core = st.session_state.get("core")
    outcome = plan_question_discovery(
        question,
        Path(__file__).resolve().parent / 'data' / 'discovery_index.sqlite3',
        client=getattr(core, 'client', None),
        model=st.session_state.get(
            'sage_selected_model', getattr(core, 'CHAT_MODEL', 'gpt-5.6-luna')
        ),
        unsafe=bool(
            core is not None
            and callable(getattr(core, 'looks_like_prompt_or_code_injection', None))
            and core.looks_like_prompt_or_code_injection(question)
        ),
    )
    if outcome.source_hint:
        st.session_state.discovery_source_hint = {
            'question': question,
            'table_id': outcome.source_hint,
        }
    if outcome.requires_selection:
        st.session_state.discovery_pending = {'question': question, 'plan': outcome.plan}
        st.session_state.sage_discovery_other_table = ''
        st.session_state.question_feedback = None
        return True
    return False
