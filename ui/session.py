"""Per-browser-tab state (st.session_state): create, clear and reset it.

Streamlit re-runs app.py on every click; anything that must survive a click
lives in st.session_state. This module owns that state's lifecycle:

* initialise_session_state - first visit, or a new software release;
* clear_previous_results   - a new question invalidates every earlier output;
* reset_like_clear         - the Clear button (keeps sign-in and preferences);
* select_sample_question   - queue a sample question to run on the next pass.

Used by: app.py, ui/answer_pipeline.py, ui/auto_test.py,
ui/question_form.py, ui/sidebar.py.
"""

import streamlit as st

from ui.config import LOGGER, SAGE_UI_RELEASE


def initialise_session_state() -> None:
    """Create the Streamlit session-state values used by the application."""
    defaults = {
        "messages": [],
        "core": None,
        "last_response": None,
        "show_debug": False,
        "token_usage_report": None,
        "capture_debug": False,
        "sample_bank_version": None,
        "test_question_index": 0,
        "sage_sequential_questions": False,
        "sage_sequence_start": "1st",
        "sage_sequence_cursor": None,
        "sage_sequence_category": None,
        "sage_generation_error": None,
        "sage_question_category": "ALL",
        "sage_low_resource_mode": False,
        "queued_question": None,
        "answer_phase": "ready",
        "question_feedback": None,
        "active_question": None,
        "question_editor": None,
        "sage_question_editing": True,
        "sage_submitted_question": "",
        "pending_test_scope": None,
        "pending_test_question": None,
        "auto_test_started": False,
        "auto_test_bank_hash": None,
        "auto_test_total": 0,
        "auto_test_interval": 10.0,
        "auto_test_completed": 0,
        "auto_test_running_index": None,
        "auto_test_running_question": None,
        "auto_test_next_due_at": None,
        "auto_test_finished": False,
        "auto_test_paused": False,
        "auto_test_error": None,
        "auto_test_log_failed": False,
        "startup_checked": False,
        "startup_data_warning": None,
        "startup_ready_label": None,
        "sync_table_selection": "",
        "discovery_pending": None,
        "discovery_source_hint": None,
        "discovery_confirmed_table": None,
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value

    # Invalidate only in-memory results produced by older UI validation logic.
    # Do not modify, invalidate, or delete the user's persisted SQLite database.
    if st.session_state.get("ui_release_marker") != SAGE_UI_RELEASE:
        st.session_state.messages = []
        st.session_state.last_response = None
        st.session_state.active_question = None
        st.session_state.queued_question = None
        st.session_state.answer_phase = "ready"
        st.session_state.question_feedback = None
        st.session_state.question_editor = ""
        st.session_state.discovery_pending = None
        st.session_state.discovery_source_hint = None
        st.session_state.discovery_confirmed_table = None
        # A software upgrade must not revive an in-flight test from an older
        # Streamlit session. The durable checkpoint remains untouched.
        st.session_state.auto_test_started = False
        st.session_state.auto_test_bank_hash = None
        st.session_state.auto_test_running_index = None
        st.session_state.auto_test_running_question = None
        st.session_state.auto_test_next_due_at = None
        st.session_state.auto_test_paused = False
        st.session_state.auto_test_error = None
        st.session_state.ui_release_marker = SAGE_UI_RELEASE
        LOGGER.info("Sage UI release %s loaded", SAGE_UI_RELEASE)


def clear_previous_results() -> None:
    """First action for every question button: invalidate all prior output.

    Called by Streamlit button callbacks *before* widgets are rendered on the
    next full app run. The queued request is executed by a later fragment tick,
    never during the initial cleared-screen pass.
    """
    st.session_state.messages = []
    st.session_state.last_response = None
    st.session_state.active_question = None
    st.session_state.queued_question = None
    st.session_state.question_feedback = None
    st.session_state.pending_test_scope = None
    st.session_state.pending_test_question = None
    st.session_state.discovery_pending = None
    st.session_state.discovery_source_hint = None
    st.session_state.discovery_confirmed_table = None
    st.session_state.token_usage_report = None
    # Per-answer widget state (series picks, chart options, ratings, preview) is
    # dropped too, so a repeated question starts exactly as clean as after Clear.
    stale_prefixes = (
        "sage_table_applied_selection_",
        "sage_table_draft_selection_",
        "sage_series_",
        "sage_totals_only_",
        "sage_combine_",
        "sage_extra_",
        "sage_select_all_",
        "sage_clear_all_",
        "sage_add_",
        "sage_redraw_",
        "sage_view_",
        "sage_sort_",
        "sage_slice_",
        "sage_rating_",
        "chart_",
    )
    for key in [
        k
        for k in list(st.session_state.keys())
        if isinstance(k, str) and k.startswith(stale_prefixes)
    ]:
        del st.session_state[key]
    st.session_state.answer_phase = "clearing"


# Everything that is a PREFERENCE or infrastructure rather than something on the screen.
# Submit and Surprise me keep these; everything else is wiped exactly as the Clear button does.
_KEEP_ON_NEW_QUESTION = frozenset(
    {
        "core",
        "sage_theme_choice",
        "sage_question_category",
        "sage_sequence_start",
        "sage_sequence_cursor",
        "sage_sequence_category",
        "sage_sequential_questions",
        "sage_selected_model",
        "sage_low_resource_mode",
        "show_debug",
        "capture_debug",
        "sample_bank_version",
        "test_question_index",
        "sage_flat_questions",
        "startup_checked",
        "startup_data_warning",
        "startup_ready_label",
        "sage_answer_slot",
        "_sage_security_session_id",
        "sage_generation_error",
        "sage_generation_note",
        # initialise_session_state() treats a missing marker as "new software release" and then
        # wipes the queued question and the question box, so the sample would never auto-run.
        "ui_release_marker",
        # Password gate: a new question must never sign the user out. The timestamps
        # drive idle/absolute expiry (utils/auth.py); dropping them would read as expired.
        "sage_authenticated",
        "sage_auth_at",
        "sage_auth_seen",
        "_sage_auth_email",
        "_sage_auth_role",  # admin vs general password: a new question must not demote an admin
        "sage_surprise_deck",  # Surprise me: the shuffled deck must survive each new question
    }
)


_KEEP_PREFIXES_ON_NEW_QUESTION = ("auto_test", "pending_test")


def reset_like_clear() -> None:
    """Do what pressing Clear does, then let the caller carry on with the new question.

    Clear wipes the whole session. A new question must do the same, otherwise leftovers
    (old answer, chart choices, ratings, token report, discovery panels...) survive. Only the
    user's preferences (topic filter, theme, model...) and the loaded engine are kept.
    The answer then moves to the other of two alternating page slots, so the slot that still
    shows the previous answer is replaced by an empty element (see the answer fragment).
    Call this ONLY from a button/form callback, like clear_question_editor().
    """
    keep = {
        key: value
        for key, value in st.session_state.items()
        if key in _KEEP_ON_NEW_QUESTION
        or (isinstance(key, str) and key.startswith(_KEEP_PREFIXES_ON_NEW_QUESTION))
    }
    for key in list(st.session_state.keys()):  # snapshot: deleting while iterating is unsafe
        del st.session_state[key]
    for key, value in keep.items():
        st.session_state[key] = value
    st.session_state.sage_answer_slot = 1 - (int(st.session_state.get("sage_answer_slot", 0)) % 2)


def select_sample_question(entry, index, *, like_clear: bool = False) -> None:
    """Clear old output and queue a sample; the sample runs automatically.

    like_clear=True (the Surprise me / Next buttons) first does exactly what Clear does.
    """
    if like_clear:
        reset_like_clear()
    clear_previous_results()
    st.session_state.test_question_index = index
    st.session_state.question_editor = entry["question"]
    st.session_state.sage_submitted_question = entry["question"]
    st.session_state.sage_question_editing = False
    st.session_state.queued_question = (entry["question"], False)
    st.session_state.pending_test_question = entry["question"]
    st.session_state.pending_test_scope = (
        "young_adults_20_35" if "Young Adults" in entry["perspective"] else None
    )
