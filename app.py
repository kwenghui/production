"""Sage: the Streamlit page. Start here.

Run with ``python -m streamlit run app.py``. Streamlit re-runs this whole
script on EVERY click, so anything that must survive a click lives in
``st.session_state`` (one dictionary per browser tab).

THE PAGE, TOP TO BOTTOM (main)
==============================
1. Start-up: logging, page look, sign-in, OpenAI key      -> ui/startup.py
2. The sample-question bank for the sidebar               -> ui/question_bank.py
3. Page shell: sidebar, header, analytical core            -> ui/sidebar.py, ui/core_loader.py
4. The question box (Submit only QUEUES a question)        -> ui/question_form.py
5. The answer area (render_answer_phase, below): the queued question is
   answered by ui/answer_pipeline.process_question and drawn by
   ui/answer_view.render_conversation.

WHERE THE DECISIONS LIVE
========================
Nothing in ui/ or here decides whether a number may be shown. That is done,
the same way for every official table, by Streamlit-free modules:

* utils/core_secure.py         - the analysis: model + official-data tools
* utils/answer_verification.py - is every number backed by official evidence?
* utils/chart_selection.py     - which chart (if any) answers the question?
* utils/vocabulary.py          - the only place subject words may appear

``tests/test_no_table_specific_code.py`` keeps it that way.
"""

# --- Standard library --------------------------------------------------------
import os
import sys
import threading

# --- Third-party ---------------------------------------------------------------
import streamlit as st

# --- Sage ------------------------------------------------------------------------
from utils import process_state as _process_state
from utils.errors import SageError, log_unexpected
from utils.log_setup import configure_logging
from utils.token_usage import usage_scope
from ui.answer_pipeline import process_question
from ui.answer_view import render_conversation, render_processing_indicator
from ui.auto_test import (
    auto_test_tick,
    mark_auto_test_question_complete,
    parse_test_options,
    render_auto_test_status,
)
from ui.config import LOGGER, QUESTION_BANK_FILE
from ui.core_loader import ensure_core_loaded
from ui.debug_capture import enable_runtime_debug_logging
from ui.question_bank import cached_question_bank
from ui.question_form import (
    render_discovery_choices,
    render_header,
    render_question_controls,
    run_deferred_discovery,
)
from ui.question_log import (
    log_failed_question,
    show_log_write_failure,
    start_log_retention_in_background,
)
from ui.session import initialise_session_state
from ui.sidebar import render_sidebar
from ui.startup import (
    ensure_app_path,
    inject_theme_styles,
    load_api_key,
    render_sign_out,
    require_password,
    warn_if_theme_config_missing,
)


def main() -> None:
    """Draw the page (Streamlit calls this on every click)."""
    options = _start_options()
    # Streamlit requires page configuration before other Streamlit UI calls.
    st.set_page_config(
        page_title="Sage", page_icon="📊", layout="wide", initial_sidebar_state="auto"
    )

    # 1) Logging, page look, sign-in, log retention, OpenAI key.
    configure_logging()  # once per process; audit/security events go to stdout
    warn_if_theme_config_missing()
    inject_theme_styles()
    require_password()  # nothing below runs before sign-in (unless Need_Password=0)
    start_log_retention_in_background()  # at most every 6 h; never blocks this click
    api_key = _api_key_or_stop()
    ensure_app_path()
    initialise_session_state()
    st.session_state.capture_debug = bool(options.debug)
    if options.debug:
        enable_runtime_debug_logging()
    # Start-up never downloads official data: observations are fetched (and
    # verified) only when a question needs them.
    if not st.session_state.startup_checked:
        st.session_state.startup_checked = True
        st.session_state.startup_data_warning = None
        st.session_state.startup_ready_label = None

    # 2) Sample questions shown in the sidebar (questions/questions.txt).
    flat_questions = _load_sample_bank()

    # 3) Page shell first, then the (slow on a cold start) analytical core.
    render_sidebar(api_key)
    render_sign_out()
    render_header(len(flat_questions))
    with st.spinner("Preparing Sage's statistical analysis engine..."):
        ensure_core_loaded(api_key)
        # Once per server: warm the semantic-search index in the background,
        # so the first visitor's question does not wait for it to load.
        if _process_state.first_time("semantic_index_warmup"):
            from utils.hybrid_discovery import warm_up_index

            threading.Thread(target=warm_up_index, name="sage-index-warmup", daemon=True).start()
    if not hasattr(st, "fragment"):
        st.error("Sage needs Streamlit 1.37+ for automatic testing.")
        st.stop()

    # 4) The question box. Nothing runs until Submit / a sample is chosen.
    #    In test mode the box is drawn inside the answer area instead (step 5),
    #    so the question and its answer always change together.
    if not st.session_state.get("auto_test_started"):
        render_question_controls(flat_questions, options=options)

    # 5) The answer area, as a "fragment" (redrawn without re-running the page).
    #    Only the operator test runner is scheduled (every second); ordinary
    #    questions are never put on a timer, which used to rebuild the whole
    #    answer each second and blank the page while scrolling. run_every is
    #    fixed when the fragment is created, so it is decided here, per run.
    #    NEVER call st.rerun(scope="app") from the scheduled fragment: that
    #    orphaned the previous browser timer ("fragment does not exist").
    batch_pending = bool(st.session_state.get("auto_test_started")) and not bool(
        st.session_state.get("auto_test_finished")
    )

    def _answer_fragment() -> None:
        """The answer area as a Streamlit fragment (redrawn without re-running the page)."""
        _answer_area(flat_questions, options)

    st.fragment(run_every="1s" if batch_pending else None)(_answer_fragment)()
    from utils.sage_design import render_disclaimer

    render_disclaimer()


def _start_options():
    """Parse ``streamlit run app.py -- --debug | --test`` (operators only)."""
    try:
        options = parse_test_options(sys.argv[1:])  # arguments after the "--"
    except (SystemExit, ValueError) as error:
        raise ValueError(
            "Sage test-mode arguments are invalid. "
            "Use -- --debug or -- --test [--test-interval 10] [--test-reset or "
            "--reset]."
        ) from error
    # Debug/test mode is off in deployments unless explicitly allowed.
    if (options.debug or getattr(options, 'test', False)) and os.getenv(
        'SAGE_ALLOW_ADMIN_TESTS', '0'
    ) != '1':
        raise RuntimeError(
            'Debug/test mode is disabled. Set SAGE_ALLOW_ADMIN_TESTS=1 only on '
            'trusted deployments.'
        )
    return options


def _api_key_or_stop():
    """The OpenAI key; a malformed key stops the page (fail closed, no API call)."""
    try:
        return load_api_key()
    except SageError as error:
        LOGGER.error("Credential configuration rejected: %s", error)
        st.error(error.user_message)
        st.stop()


def _load_sample_bank():
    """The flat list of sample questions; restart test order if the bank changed."""
    try:
        _bank, flat_questions = cached_question_bank(QUESTION_BANK_FILE)
        bank_version = QUESTION_BANK_FILE.stat().st_mtime_ns
    except (OSError, UnicodeError, ValueError):
        LOGGER.exception("Unable to read Sage question bank")
        st.error(
            "The sample-question bank could not be loaded. Place "
            "questions/questions.txt in the application folder "
            "and check its numbered [TAG] question format."
        )
        st.stop()
    _restart_if_bank_changed(bank_version)
    st.session_state.sage_flat_questions = flat_questions
    return flat_questions


def _restart_if_bank_changed(bank_version) -> None:
    """The bank is editable: if it changed while running, start again at Question 1."""
    if st.session_state.sample_bank_version == bank_version:
        return
    st.session_state.test_question_index = 0
    st.session_state.messages = []
    st.session_state.last_response = None
    st.session_state.active_question = None
    st.session_state.question_editor = ""
    st.session_state.sage_question_editing = True
    st.session_state.auto_test_started = False
    st.session_state.auto_test_bank_hash = None
    st.session_state.auto_test_running_index = None
    st.session_state.auto_test_running_question = None
    st.session_state.auto_test_next_due_at = None
    st.session_state.sample_bank_version = bank_version
    st.session_state.queued_question = None
    st.session_state.answer_phase = "ready"
    st.session_state.question_feedback = None


def _answer_area(flat_questions, options) -> None:
    """The answer area: the queued question is answered and drawn here.

    In operator test mode this is also where the next sample is scheduled, so
    the question box and its answer always change together.
    """
    if st.session_state.get("auto_test_started"):
        # Run the scheduler before painting: editor and old graph update
        # atomically when advancing to the next question.
        try:
            auto_test_tick(flat_questions)
        except Exception as error:  # noqa: BLE001 - keep the admin runner alive
            log_unexpected(LOGGER, "Automatic test pacing failed", error)
            st.session_state.auto_test_error = (
                "Automatic test paused after a scheduling error; see debug.txt"
            )
            st.session_state.auto_test_paused = True
        render_auto_test_status(flat_questions)
        render_question_controls(flat_questions, options=options)
    try:
        # A new question is processed synchronously in this same run, and Streamlit
        # keeps the previous answer on screen until each element is overwritten.
        # Emptying and rebuilding ONE slot does not clear it: the server merges two
        # updates sent to the same spot, so the rebuilt container inherits the old
        # answer. Instead the answer alternates between two slots. A new question
        # draws in the other slot, and the slot holding the previous answer is
        # replaced by an empty element, which removes it from the screen at once.
        slots = (st.empty(), st.empty())
        active_slot = int(st.session_state.get("sage_answer_slot", 0)) % 2
        with slots[active_slot].container():
            render_answer_phase()
    except Exception as error:  # noqa: BLE001 - outermost UI boundary
        incident = log_unexpected(LOGGER, "Question rendering or processing failed", error)
        running = st.session_state.get("auto_test_running_question")
        if running and not st.session_state.get("auto_test_log_failed"):
            if not log_failed_question(running, "processing_exception"):
                show_log_write_failure("failed.txt")
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": "Unable to complete this question. Check debug.txt if enabled.",
                "simple_answer": "This question could not be completed.",
                "verified": False,
            }
        ]
        st.session_state.answer_phase = "ready"
        if running:
            mark_auto_test_question_complete(running)
        # A Streamlit exception can occur after widgets were registered.
        # Never attempt a second full render in the same fragment run: it
        # duplicates widget keys (including the answer rating buttons).
        st.error(f"This question could not be displayed (reference {incident}).")


def render_answer_phase() -> None:
    """Render normal questions ONCE, without a perpetual browser refresh.

    Previously a question started a 1-second scheduled fragment. Streamlit
    keeps that schedule after the answer is ready because the fragment
    decorator was already registered; each tick rebuilt the full answer,
    chart gallery and many widgets. This can exhaust the browser or leave a
    white screen during scrolling, particularly on phones.

    For an ordinary query, clear and process in this same Streamlit pass.
    The streamed processing placeholder is displayed while the synchronous
    official-data request runs. Batch testing retains its separate scheduler.
    """
    phase = st.session_state.get("answer_phase", "ready")
    if phase == "clearing":
        st.session_state.answer_phase = "processing"
        if st.session_state.get("auto_test_started"):
            # Only the operator's automatic test sequence needs a second tick.
            render_processing_indicator()
            return
        # Normal requests MUST fall through and finish in this one pass.
        phase = "processing"
    if phase == "processing":
        queued = st.session_state.get("queued_question")
        if queued is None:
            # A browser refresh can interrupt the prior running analysis after
            # the queue is consumed. Retry that *unfinished* test sample rather
            # than silently hanging the automatic sequence.
            interrupted = st.session_state.get("auto_test_running_question")
            if interrupted:
                st.session_state.queued_question = (interrupted, False)
                st.session_state.answer_phase = "clearing"
            else:
                st.session_state.answer_phase = "ready"
            render_processing_indicator() if interrupted else None
            return
        st.session_state.queued_question = None  # Exactly once per clicked question.
        processing_slot = st.empty()
        render_processing_indicator(processing_slot)
        deferred = st.session_state.pop("deferred_discovery", None)
        if deferred is not None and deferred == queued[0]:
            try:
                needs_choice = run_deferred_discovery(deferred)
            except Exception:  # noqa: BLE001 - a planning failure must not lose the question
                LOGGER.exception("Interactive discovery planning failed; answering without it")
                needs_choice = False
            if needs_choice:
                processing_slot.empty()
                st.session_state.answer_phase = "ready"
                render_discovery_choices()
                return
        st.session_state.auto_test_log_failed = False
        try:
            with usage_scope() as token_counter:
                from utils.model_client import selected_model_scope

                with selected_model_scope(
                    st.session_state.get("sage_selected_model", "gpt-5.6-luna")
                ):
                    try:
                        process_question(queued[0], is_manual=queued[1])
                    finally:
                        st.session_state.token_usage_report = token_counter.summary()
        finally:
            # The progress line must disappear in this same browser update;
            # don't leave it stuck above a finished answer until the next tick.
            processing_slot.empty()
        mark_auto_test_question_complete(queued[0])
        st.session_state.answer_phase = "ready"
        render_conversation()  # One stable result render; ordinary questions have no timer.
        return
    if st.session_state.get("question_feedback"):
        st.info(st.session_state.question_feedback)
    if render_discovery_choices():
        return
    render_conversation()


if __name__ == "__main__":
    main()
