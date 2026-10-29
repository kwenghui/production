"""Answering ONE question: process_question() and the steps it runs.

    process_question
      0. _admit_question / _read_source_preferences   rate limits, length, table choices
      1. _ask_core                                     core.ask_sage does the analysis
      2. _enforce_selected_source / _enforce_bank_source   hard source rules
      3. _verify_answer_text / _write_simple_answer    keep only checked numbers
      4. _select_chart / _align_text_with_chart        at most one re-checked chart
      5. _store_and_log_answer                         save for the page, write audit

Every step can only keep or NARROW what is shown ("fail closed"); none can
turn a rejected answer back into a verified one. The decisions themselves
live in Streamlit-free modules (utils/answer_verification.py,
utils/plain_summary.py, utils/chart_selection.py); this module orchestrates
them and stores the result in st.session_state.messages.

Behaviour is characterised by tools/characterise_process_question.py.

Used by: app.py.
"""

from contextlib import nullcontext
from dataclasses import dataclass
import copy
import hashlib
import re
import secrets

import streamlit as st

from utils import auth as sage_auth
from utils.answer_verification import (
    displayed_answer,
    evidence_bound_generic_answer,
    full_answer_numbers_supported,
    is_official_source,
    longer_history_for_single_period,
    official_evidence,
    remove_unverified_optional_sentence,
    retry_verified_series_for_numeric_failure,
    simple_answer_is_refusal,
    strip_unnecessary_chart_commentary,
    structured_endpoint_chart_from_answer,
)
from utils.category_hierarchy import review_composition
from utils.chart_intent import (
    chart_covers_requested_groups,
    choose_presentation,
    is_locked_evidence_chart,
    required_groups,
)
from utils.chart_selection import (
    align_chart_description_to_delivery,
    choose_chart_result,
    is_generic_table_preview,
    limited_category_ranking_text,
    question_chart_key,
    relevant_chart_evidence,
    resolve_chart_spec,
    selected_series_display_title,
)
from utils.errors import log_unexpected, SageError
from utils.evidence_ledger import required_comparison_covered
from utils.stage_timing import timed_stage
from utils.plain_summary import generate_simple_answer
from utils.population_basis import population_conflict
from utils.session_security import CLIENT_LIMITER, LIMITER, RateLimitExceeded
from utils.token_usage import (
    budget_setting_for,
    QuestionModelBudgetExceeded,
    QuestionTimeBudgetExceeded,
    QuestionTokenBudgetExceeded,
)
from utils.schemas import ChartSpec, CoreResponse
from ui.config import LOGGER, MAX_UI_INPUT_CHARS
from ui.debug_capture import capture_query_debug
from ui.question_bank import (
    audited_chart_expected,
    expected_table_for_generated_question,
    preferred_source_for_sample,
    response_matches_expected_table,
    unverified_table_message,
)
from ui.question_log import (
    failure_category,
    log_answer_decision,
    log_failed_question,
    log_successful_question,
    show_log_write_failure,
)
from ui.session import clear_previous_results


#: Provenance of the chosen chart, stored with the answer for the audit log and
#: the renderer. Generic fields only: they exist on every verified chart result.
CHART_CONTEXT_KEYS = (
    "chart_kind",
    "table_id",
    "source_url",
    "table_complete",
    "chart_source_verified",
    "chart_integrity",
    "start_year",
    "end_year",
    "measure",
)


#: A citable source id: a SingStat table id (Mxxxxxx, Cxxxxxx, ...) or a numeric
#: data.gov.sg dataset id. Anything else is never shown as a source link.
_SOURCE_ID_PATTERN = re.compile(r"(?:[A-Z][A-Z0-9]{1,18}[0-9][A-Z0-9]*|[0-9]{3,20})")


#: "show me a chart of ...", "plot ...", "can I see a graph ..." etc.
_EXPLICIT_CHART_REQUEST = re.compile(
    r'(?i)\b(?:show|draw|create|plot|display|see|give|generate|make)\b.{0,120}\b(?:chart|graph|plot)\b'
)


# --- Fixed wording shown to users (one place to review or translate) ---------
_MSG_POPULATION_CONFLICT = (
    'The official data I found cover a different population '
    'from the one you asked about ({conflict}). I won\'t '
    'substitute one population for another. Please rephrase, '
    'or name the table you want to use.'
)


_MSG_NO_SUPPORTED_ANSWER = (
    "I found official results but couldn't produce a "
    "supported answer to this question. Please rephrase "
    "your question or choose another sample."
)


_MSG_NUMBERS_UNVERIFIED = (
    "I couldn't verify enough official observations to describe this "
    "historical trend reliably. The available evidence did "
    "not support a complete, consistent series across the "
    "requested periods, so I have not inferred a direction "
    "or quoted unverified figures. Check the source and "
    "series shown above, if available. You can select "
    "Modify to specify a period or statistical series "
    "and try again."
)


_MSG_SIMPLE_UNVERIFIED = (
    "I can't check enough official data for this question yet. " "Please try asking it another way."
)


_MSG_RANKING_UNVERIFIED = (
    'The official results returned do not establish '
    'a reliable ranking across the complete source. '
    'No ranking chart has been displayed.'
)


_MSG_BUDGET_STOP = (
    "Sage reached its AI processing safety limit before it could verify "
    "a complete answer. No further AI requests were made. "
    "Try selecting one published measure or breakdown and a time period."
)


@dataclass
class _SourcePreferences:
    """Which official table the answer should (or must) come from.

    selected_table_id  The user clicked this table in discovery. HARD rule:
                       figures from any other table are never shown instead.
    bank_table_id      A generated sample question with an audited table. HARD rule.
    hinted_table_id    Discovery suggested this table. SOFT hint only.
    """

    selected_table_id: str | None
    bank_table_id: str | None
    hinted_table_id: str | None


@dataclass
class _AnswerDraft:
    """Working copy of one answer while it moves through the checks.

    Invariant: once ``verified`` is False, no later step sets it back to True.
    """

    response: CoreResponse  # returned by core.ask_sage (may be replaced)
    full_answer: str = ""  # detailed answer text that will be shown
    verified: bool = False  # True only if every shown number is backed
    limited_answer: bool = False  # verified, but narrowed/trimmed by a check
    numeric_blocked: bool = False  # AI text had numbers not found in the evidence
    no_supported_answer: bool = False  # evidence existed but did not answer the question
    simple_answer: str = ""  # one-line summary shown above the details
    chart_response: dict | None = None  # evidence the chart may be drawn from
    selected_chart: dict | None = None  # the one chart candidate chosen (or None)
    chart_spec: ChartSpec | None = None  # validated spec to draw (or None)
    chart_render_rejected: bool = False  # a chart existed but failed validation

    def drop_chart(self, *, rejected: bool = False) -> None:
        """Remove the chart. ``rejected`` records that it failed validation."""
        self.chart_spec = None
        self.selected_chart = None
        if rejected:
            self.chart_render_rejected = True


def process_question(question, *, is_manual: bool = False):
    """Answer one new question and store the checked result for the page.

    Called once per question (not on every Streamlit rerun). Each line below is
    one step; read the step's docstring for the detail.

    Args:
        question:  The text typed by the user or taken from the sample bank.
        is_manual: True when typed by the user (earlier dialogue may be used and
                   the AI first classifies the intent); False for samples/tests.

    Returns:
        True when a new answer (or an error message) was stored in
        ``st.session_state.messages``; False when the question was rejected
        before any model call (empty, too long or rate-limited).
    """
    question = _admit_question(question, is_manual=is_manual)
    if question is None:
        return False

    # Navigation/debug questions are isolated; Submit may use earlier dialogue.
    previous_dialogue = st.session_state.pop("submit_history", []) if is_manual else []
    prefs = _read_source_preferences(question, is_manual=is_manual)
    clear_previous_results()  # Must come AFTER the reads above: it wipes those keys.
    st.session_state.active_question = question
    st.session_state.active_is_manual = bool(is_manual)

    draft = None
    debug_capture = (
        capture_query_debug(question) if st.session_state.get("capture_debug") else nullcontext()
    )  # One loading notice is drawn by render_answer_phase().
    with debug_capture:
        try:
            # 1. The analytical engine does the real work (utils/core_secure.py).
            draft = _AnswerDraft(
                response=_ask_core(
                    question, is_manual=is_manual, previous_dialogue=previous_dialogue, prefs=prefs
                )
            )
            # 2. Hard source rules: never show another table's figures instead.
            draft.response = _enforce_selected_source(draft.response, prefs.selected_table_id)
            draft.response = _enforce_bank_source(draft.response, prefs.bank_table_id, question)
            _attach_verified_table_descriptions(draft.response, question)
            # 3. Keep the AI text only if official evidence backs every number.
            _verify_answer_text(draft, question)
            if not (prefs.selected_table_id or prefs.bank_table_id):
                _add_longer_history(draft, question)
            draft.simple_answer = _write_simple_answer(draft, question)
            # 4. At most one chart, re-checked; any doubt -> no chart.
            _select_chart(draft, question)
            _align_text_with_chart(draft, question)
            _log_chart_selection(draft, question)
            # 5. Save for the page and record the outcome.
            _store_and_log_answer(draft, question, is_manual=is_manual)
            return True
        except (
            QuestionTokenBudgetExceeded,
            QuestionModelBudgetExceeded,
            QuestionTimeBudgetExceeded,
        ) as exc:
            _store_budget_stop(
                exc, question, is_manual=is_manual, response=draft.response if draft else None
            )
            return True
        except Exception as error:  # noqa: BLE001 - request boundary; the UI must survive
            _store_processing_error(
                error, question, is_manual=is_manual, response=draft.response if draft else None
            )
            return True


# --- Step 0: admission and source preferences ---------------------------------


def _admit_question(question, *, is_manual):
    """Return the cleaned question, or None if it must not reach the model.

    Enforces abuse limits BEFORE any (paid) model call: per-session and
    per-client rate limits, then the maximum length. Shows the reason to the user.
    """
    if not isinstance(question, str) or not question.strip():
        return None
    question = question.strip()
    # The session identity is generated server-side, never supplied by the browser.
    session_id = st.session_state.get('_sage_security_session_id')
    if not session_id:
        session_id = secrets.token_urlsafe(32)
        st.session_state['_sage_security_session_id'] = session_id
    try:
        LIMITER.check(session_id)
        # A new tab is a new session, so also limit per (pseudonymised) client.
        # An unknown address is a bucket shared by everyone: never limit it.
        client = sage_auth.client_id_from(getattr(st, "context", None))
        if client != sage_auth.UNKNOWN_CLIENT:
            CLIENT_LIMITER.check(client)
    except RateLimitExceeded as exc:
        st.warning(exc.user_message)
        return None
    if len(question) > MAX_UI_INPUT_CHARS:
        if not log_failed_question(question, "question_too_long", is_manual=is_manual):
            show_log_write_failure("failed.txt")
        st.error(f"Question is too long. Maximum {MAX_UI_INPUT_CHARS} characters.")
        return None
    return question


def _read_source_preferences(question, *, is_manual):
    """Collect table choices made earlier for THIS exact question (see _SourcePreferences)."""
    chosen = st.session_state.get('discovery_confirmed_table')
    hint = st.session_state.get('discovery_source_hint')
    return _SourcePreferences(
        selected_table_id=(
            chosen['table_id']
            if isinstance(chosen, dict) and chosen.get('question') == question
            else None
        ),
        bank_table_id=(expected_table_for_generated_question(question) if not is_manual else None),
        hinted_table_id=(
            hint['table_id']
            if isinstance(hint, dict) and hint.get('question') == question
            else None
        ),
    )


# --- Step 1: ask the analytical core -----------------------------------------


def _ask_core(question, *, is_manual, previous_dialogue, prefs):
    """Call core.ask_sage and return its response dict (see utils/core_secure.py).

    Typed questions are first classified (preview_user_intent) so the core can
    tell a follow-up from a new topic. The preferred table is, in priority
    order: the user's click, the sample's audited table, the discovery hint,
    then any audited management sample.
    """
    core = st.session_state.core
    decision = None
    if is_manual:
        with timed_stage("preview_user_intent"):
            decision = core.preview_user_intent(
                question=question, chat_history=previous_dialogue, combined_user_input=question
            )
    # The automatic test harness may narrow the scope of its own queued question.
    is_queued_test = not is_manual and question == st.session_state.get("pending_test_question")
    with timed_stage("ask_sage"):
        return core.ask_sage(
            question=question,
            chat_history=previous_dialogue,  # Empty for navigation and debug tests.
            combined_user_input=question,
            show_debug=False,
            intent_decision=decision,
            preferred_table_id=(
                prefs.selected_table_id
                or prefs.bank_table_id
                or prefs.hinted_table_id
                or preferred_source_for_sample(question)
            ),
            user_selected_table=bool(prefs.selected_table_id),
            required_table_id=(prefs.bank_table_id if not prefs.selected_table_id else None),
            test_scope=(st.session_state.get("pending_test_scope") if is_queued_test else None),
        )


# --- Step 2: hard source rules ----------------------------------------------


def _response_is_blocked(response) -> bool:
    """True when the core refused the request (security or out-of-scope)."""
    return bool(response.get('security_blocked') or response.get('scope_blocked'))


def _set_aside_all_evidence(response, answer_text) -> dict:
    """Return a copy of ``response`` with no usable evidence and a refusal text.

    The removed rows are kept under ``set_aside_tool_results`` so the technical
    details can still show WHY nothing could be verified.
    """
    return {
        **response,
        'tool_results': [],
        'set_aside_tool_results': list(response.get('tool_results') or []),
        'answer': answer_text,
        'selected_source_unavailable': True,
        'suppress_charts': True,
    }


def _enforce_selected_source(response, selected_table_id):
    """A table the user clicked is a constraint, not a hint.

    If that table did not supply complete, verified rows, refuse rather than
    show a different measure's figures as if they were the selected one.
    """
    if not selected_table_id or not isinstance(response, dict) or _response_is_blocked(response):
        return response
    verified_selected = any(
        isinstance(row, dict)
        and row.get('success') is True
        and (
            row.get('table_id') == selected_table_id
            or selected_table_id in (row.get('table_ids') or [])
        )
        and row.get('table_complete') is not False
        and row.get('analysis_ready') is not False
        for row in (response.get('tool_results') or [])
    )
    if verified_selected:
        return response
    LOGGER.warning('Selected source did not supply verified evidence: %s', selected_table_id)
    return _set_aside_all_evidence(
        response,
        'The selected SingStat table '
        + selected_table_id
        + ' could not be verified for the original question. '
        'No figures or graph from a different measure have been substituted.',
    )


def _enforce_bank_source(response, bank_table_id, question):
    """Generated sample questions have an authoritative question -> table mapping.

    Never let a different table answer such a question; refuse instead.
    """
    if (
        not bank_table_id
        or not isinstance(response, dict)
        or _response_is_blocked(response)
        or response_matches_expected_table(response, bank_table_id)
    ):
        return response
    actual = [
        str(row.get("table_id"))
        for row in response.get("tool_results") or []
        if isinstance(row, dict) and row.get("table_id")
    ]
    LOGGER.warning(
        "SOURCE_MISMATCH requested=%s returned=%s question=%r",
        bank_table_id,
        actual,
        question[:240],
    )
    unverified = _set_aside_all_evidence(
        response, unverified_table_message(bank_table_id, response.get("tool_results"))
    )
    unverified["rag_results"] = []
    return unverified


def _attach_verified_table_descriptions(response, question) -> None:
    """Add descriptions of the tables actually used to the response (in place).

    Context only: this never changes any number, and the added text has no
    authority over verification or security rules. Failure is logged, not fatal.
    """
    if not isinstance(response, dict) or _response_is_blocked(response):
        return
    verified_rows = [
        row
        for row in response.get("tool_results", [])
        if isinstance(row, dict) and row.get("success") is True
    ]
    if not verified_rows:
        return
    try:
        dataset_ids = [str(row["table_id"]) for row in verified_rows if row.get("table_id")]
        with timed_stage("enhance_rag_after_verified_tools"):
            response["rag_results"] = st.session_state.core.enhance_rag_after_verified_tools(
                question, response.get("rag_results") or [], verified_rows, dataset_ids=dataset_ids
            )
    except (OSError, ValueError) as error:
        LOGGER.warning("Additional RAG metadata unavailable: %s", error)


# --- Step 3: verify the written answer ------------------------------------------


def _verify_answer_text(draft, question) -> None:
    """Decide whether the AI's text may be shown, narrowing it if necessary.

    Runs four checks in order; each can only reduce what is shown:
      a. the official data must describe the population that was asked about;
      b. an empty answer or a refusal is not an answer;
      c. every number must appear in the evidence (else trim or rebuild it);
      d. remove boilerplate about charts (chart delivery is decided later).
    """
    draft.full_answer, draft.verified = displayed_answer(draft.response)
    _reject_population_mismatch(draft, question)
    _reject_empty_or_refused_answer(draft)
    _enforce_numeric_support(draft, question)
    if draft.verified:
        draft.full_answer = strip_unnecessary_chart_commentary(draft.full_answer)


def _reject_population_mismatch(draft, question) -> None:
    """Check (a): question names citizens but data cover residents -> refuse.

    Same rule for every table: never present another population's figures.
    """
    if not draft.verified:
        return
    conflict = population_conflict(question, official_evidence(draft.response))
    if conflict:
        LOGGER.warning('Population basis mismatch: %s', conflict)
        draft.verified = False
        draft.no_supported_answer = True
        draft.response['suppress_charts'] = True
        draft.full_answer = _MSG_POPULATION_CONFLICT.format(conflict=conflict)


def _reject_empty_or_refused_answer(draft) -> None:
    """Check (b): a successful tool does not mean the AI answered the question."""
    if draft.verified and (
        not str(draft.response.get("answer") or "").strip()
        or simple_answer_is_refusal(draft.full_answer[:200])
    ):
        draft.verified = False
        draft.no_supported_answer = True
        draft.full_answer = _MSG_NO_SUPPORTED_ANSWER


def _enforce_numeric_support(draft, question) -> None:
    """Check (c): every number in the text must be found in the evidence.

    If some are not, try in order: drop the one optional sentence that holds
    them; re-fetch the series and rebuild the answer from checked observations
    only; otherwise block the answer with a fixed explanation.
    Catalogue listings ("which tables cover X?") contain no statistics: skipped.
    """
    response = draft.response
    if (
        not draft.verified
        or response.get('table_catalogue_listing')
        or full_answer_numbers_supported(draft.full_answer, official_evidence(response), question)
    ):
        return
    pruned = remove_unverified_optional_sentence(
        draft.full_answer, official_evidence(response), question
    )
    if pruned is not None:
        draft.full_answer = pruned
        draft.limited_answer = True
        LOGGER.warning(
            'Unsupported optional commentary removed; '
            'all surviving numbers independently checked'
        )
        return
    # Don't call the whole source missing because the AI invented percentages:
    # rebuild from typed, reconciled observations when that is responsive enough.
    draft.response = retry_verified_series_for_numeric_failure(
        question, response, st.session_state.core
    )
    deterministic = evidence_bound_generic_answer(question, official_evidence(draft.response))
    if deterministic is not None:
        draft.full_answer = deterministic
        draft.limited_answer = True
        LOGGER.warning('Unsupported derived draft replaced by ' 'structured verified observations')
        return
    draft.numeric_blocked = True
    draft.verified = False
    LOGGER.warning("Blocked answer with numbers missing from verified tool results")
    draft.full_answer = _MSG_NUMBERS_UNVERIFIED


def _add_longer_history(draft, question) -> None:
    """A change-over-time answer with ONE period gets the same subject's longer official series.

    Example: a growth-rate table that currently publishes 2025 only. One related
    table of the same subject and frequency is queried for the same series; if
    it publishes several periods, a short note naming both tables is added and
    the chart can show it. The note's numbers are re-checked like the answer's.
    Never used when the visitor chose the table themselves.
    """
    if not draft.verified or draft.response.get('table_catalogue_listing'):
        return
    found = longer_history_for_single_period(question, draft.response, st.session_state.core)
    if found is None:
        return
    response, note = found
    text = draft.full_answer.rstrip() + "\n\n" + note
    if not full_answer_numbers_supported(text, official_evidence(response), question):
        LOGGER.info('Longer-history note not added: its numbers did not verify')
        return
    draft.response, draft.full_answer = response, text
    LOGGER.info('Added a longer official series for a single-period answer')


def _write_simple_answer(draft, question):
    """Return the one-line summary. Never summarise an unverified answer."""
    response = draft.response
    if response.get('table_catalogue_listing'):
        return response.get('simple_answer', '')
    if draft.verified:
        return generate_simple_answer(
            response, draft.full_answer, st.session_state.core, question=question
        )
    return _MSG_SIMPLE_UNVERIFIED


# --- Step 4: choose and re-check at most one chart -----------------------------


def _select_chart(draft, question) -> None:
    """Choose at most one chart; every later check may only remove it.

    The chart must come from the SAME official evidence that backs the text: a
    separately successful but unsourced tool result must not provide a figure.
    """
    response = draft.response
    sourced_response = {**response, "tool_results": official_evidence(response)}
    if draft.verified:
        draft.chart_response = relevant_chart_evidence(
            sourced_response, question, draft.full_answer
        )
        draft.selected_chart = choose_chart_result(draft.chart_response)
        draft.chart_spec = copy.deepcopy(
            resolve_chart_spec(draft.chart_response, draft.selected_chart)
        )
    else:
        # No independent evidence -> no chart.
        draft.chart_response = {**sourced_response, "tool_results": []}
        draft.selected_chart = None
        draft.chart_spec = None
    # A rejected AI calculation can still leave a fully checked typed summary.
    # Give that limited answer an honest endpoint chart rather than none.
    if (
        draft.verified
        and draft.chart_spec is None
        and str(draft.full_answer).startswith('## Verified observations from SingStat ')
    ):
        endpoint = structured_endpoint_chart_from_answer(
            question, draft.full_answer, official_evidence(response)
        )
        if endpoint is not None:
            draft.selected_chart = endpoint
            draft.chart_spec = copy.deepcopy(endpoint['chart_spec'])
    _choose_chart_presentation(draft, question)
    _withhold_overlapping_composition(draft)
    _preflight_chart(draft)


def _choose_chart_presentation(draft, question) -> None:
    """Pick line/bar presentation; drop charts that miss a requested group."""
    if draft.chart_spec is None:
        return
    try:
        # Never convert an integrity-bound trend into a different chart type.
        if not is_locked_evidence_chart(draft.selected_chart):
            draft.chart_spec = choose_presentation(draft.chart_spec)
    except (ValueError, TypeError, KeyError) as exc:
        LOGGER.warning('Chart presentation rejected: %s', type(exc).__name__)
        draft.drop_chart(rejected=True)
    if draft.chart_spec is not None and not chart_covers_requested_groups(
        question, draft.chart_spec
    ):
        # "Compare men and women" with only one line drawn is misleading.
        LOGGER.warning(
            'Suppressing partial chart: required groups=%s, plotted=%s',
            sorted(required_groups(question)),
            [row.get('name') for row in draft.chart_spec.get('series', [])],
        )
        draft.drop_chart()


def _withhold_overlapping_composition(draft) -> None:
    """Drop a 'parts of a whole' chart whose categories overlap (double counting)."""
    if draft.chart_spec is None:
        return
    draft.chart_spec, _note, reason = review_composition(draft.chart_spec, draft.selected_chart)
    if draft.chart_spec is None:
        LOGGER.warning('Withheld overlapping composition: %s', reason)
        draft.drop_chart(rejected=True)


def _preflight_chart(draft) -> None:
    """Build the figure once now, so an unrenderable chart is never counted as delivered.

    Graph failures must not erase independently verified text. Only the
    exception TYPE is logged, never the (untrusted) chart metadata.
    """
    if draft.chart_spec is None:
        return
    try:
        # Imported here: Plotly is heavy and only needed once a chart survives.
        from utils.charts import build_chart

        build_chart(draft.chart_spec)
    except Exception as exc:  # noqa: BLE001 - any builder failure means "no chart"
        LOGGER.warning('Graph preflight rejected invalid chart: %s', type(exc).__name__)
        draft.drop_chart(rejected=True)


def _align_text_with_chart(draft, question) -> None:
    """Make the text match the chart actually delivered (or its absence)."""
    has_chart = draft.chart_spec is not None
    draft.full_answer = align_chart_description_to_delivery(draft.full_answer, has_chart)
    draft.simple_answer = align_chart_description_to_delivery(draft.simple_answer, has_chart)
    _narrow_partial_ranking(draft, question)
    if draft.chart_spec is not None and is_generic_table_preview(draft.selected_chart):
        new_title = selected_series_display_title(draft.chart_spec)
        if new_title:
            draft.chart_spec['title'] = new_title


def _narrow_partial_ranking(draft, question) -> None:
    """'Most/leading' needs the WHOLE classification; narrow the claim otherwise.

    The bars prove the individual checked categories, not that every category
    was retrieved. If even the narrowed wording cannot be verified, refuse.
    """
    selected = draft.selected_chart
    if not (
        draft.verified
        and draft.chart_spec is not None
        and selected is not None
        and selected.get('ranking_scope_limited')
    ):
        return
    honest = limited_category_ranking_text(selected)
    if honest and full_answer_numbers_supported(
        honest, official_evidence(draft.response), question
    ):
        draft.full_answer = honest
        draft.limited_answer = True
        draft.simple_answer = (
            f'The checked source contains {len(selected["checked_category_rows"])} '
            'reported categories for the requested period. '
            'An exhaustive ranking has not been verified.'
        )
        return
    LOGGER.warning('Selected-category ranking text could not be verified')
    draft.drop_chart()
    draft.verified = False
    draft.no_supported_answer = True
    draft.full_answer = _MSG_RANKING_UNVERIFIED
    draft.simple_answer = 'A complete category ranking could not be verified.'


def _log_chart_selection(draft, question) -> None:
    """Record which chart was chosen (digest of the question only, no raw text)."""
    if not draft.verified:
        return
    LOGGER.info(
        "Question-specific chart selection: question_digest=%s title=%r "
        "kind=%r table=%r generic_preview=%s candidate_count=%d fingerprint=%s",
        hashlib.sha256(question.encode('utf-8')).hexdigest()[:12],
        (draft.chart_spec or {}).get('title'),
        (draft.selected_chart or {}).get('chart_kind'),
        (draft.selected_chart or {}).get('table_id'),
        is_generic_table_preview(draft.selected_chart),
        len(draft.chart_response.get('tool_results') or []),
        question_chart_key(question, draft.chart_spec) if draft.chart_spec else 'no_plotly_spec',
    )


# --- Step 5: store the result and record the outcome -------------------------


def _chart_context(draft):
    """Facts about the chosen chart that the renderer needs (see CHART_CONTEXT_KEYS)."""
    if draft.chart_spec is None or draft.selected_chart is None:
        return {}
    context = {
        key: draft.selected_chart[key] for key in CHART_CONTEXT_KEYS if key in draft.selected_chart
    }
    if context:
        context["generic_preview"] = is_generic_table_preview(draft.selected_chart)
    return context


def _chart_failure_reason(draft):
    """Short, stable label explaining why a verified answer has no chart (or None)."""
    if not draft.verified or draft.chart_spec is not None:
        return None
    evidence = official_evidence(draft.response)
    if draft.chart_render_rejected:
        return 'chart_render_rejected'
    if any(
        isinstance(r, dict) and r.get('table_complete') is False
        for r in draft.response.get('tool_results') or []
    ):
        return 'official_data_incomplete'
    if draft.selected_chart is None and any(
        isinstance(r, dict) and (r.get('chart_spec') or r.get('chart_variants')) for r in evidence
    ):
        return 'question_chart_mismatch'
    if any(isinstance(r, dict) and r.get('success') is True for r in evidence):
        return 'no_usable_series'
    return 'official_data_incomplete'


def _official_source_links(response):
    """Unique (table_id, url) pairs to cite. Only official hosts; ids must look valid."""
    sources = []
    for result in official_evidence(response):
        ids = ([result["table_id"]] if result.get("table_id") else []) + (
            result.get("table_ids", []) if isinstance(result.get("table_ids"), list) else []
        )
        urls = ([result["source_url"]] if result.get("source_url") else []) + (
            result.get("source_urls", []) if isinstance(result.get("source_urls"), list) else []
        )
        for identifier, url in zip(ids, urls):
            if (
                isinstance(identifier, str)
                and _SOURCE_ID_PATTERN.fullmatch(identifier)
                and isinstance(url, str)
                and is_official_source(url)
                and (identifier, url) not in sources
            ):
                sources.append((identifier, url))
    return sources


def _delivery_category(draft, question):
    """Classify the outcome for the audit log: None means fully delivered.

    Valid numbers are not a full success if a requested chart or a requested
    comparison is missing; those answers are counted separately so dashboards
    do not overstate the success rate.
    """
    response = draft.response
    category = failure_category(
        response,
        verified=draft.verified,
        numeric_blocked=draft.numeric_blocked,
        no_supported_answer=draft.no_supported_answer,
    )
    explicit_chart = bool(_EXPLICIT_CHART_REQUEST.search(question))
    if (
        category is None
        and not response.get('table_catalogue_listing')
        and (explicit_chart or audited_chart_expected(question))
        and draft.chart_spec is None
    ):
        category = 'requested_chart_unavailable'
        LOGGER.warning(
            'Question requested a chart but none matched '
            'the verified evidence; retained checked text.'
        )
    if (
        category is None
        and required_groups(question)
        and not required_comparison_covered(
            question,
            draft.full_answer,
            draft.chart_spec,
            required_groups,
            chart_covers_requested_groups,
        )
    ):
        category = 'requested_comparison_incomplete'
        draft.limited_answer = True
        LOGGER.warning(
            'Incomplete comparison: requested=%s, plotted=%s',
            sorted(required_groups(question)),
            [row.get('name') for row in (draft.chart_spec or {}).get('series', [])],
        )
    return category


def _store_and_log_answer(draft, question, *, is_manual) -> None:
    """Save the final answer (an AnswerMessage, see utils/schemas.py) and write audit records."""
    response = draft.response
    st.session_state.messages = [
        {"role": "user", "content": question, "input_source": "manual" if is_manual else "preset"},
        {
            "role": "assistant",
            "content": draft.full_answer,
            "simple_answer": draft.simple_answer,
            "verified": draft.verified,
            "limited_answer": draft.limited_answer,
            "source_question": question,
            "official_sources": _official_source_links(response),
            "chart_spec": draft.chart_spec,
            "chart_context": _chart_context(draft),
            "chart_failure_reason": _chart_failure_reason(draft),
            "allow_multiple_charts": response.get("allow_multiple_charts", False),
            'suppress_charts': response.get('suppress_charts', False),
        },
    ]
    st.session_state.last_response = response
    answer = st.session_state.messages[-1]
    category = _delivery_category(draft, question)
    answer['limited_answer'] = draft.limited_answer  # may have been narrowed just above
    answer['delivery_status'] = (
        category if category else ('verified_limited' if draft.limited_answer else 'verified')
    )
    log_answer_decision(question, answer, response, failure_category=category)
    if category:
        if not log_failed_question(question, category, is_manual=is_manual):
            show_log_write_failure("failed.txt")
    elif not log_successful_question(
        question,
        is_manual=is_manual,
        status='verified_limited' if draft.limited_answer else 'verified',
    ):
        show_log_write_failure("success.txt")


def _store_budget_stop(exc, question, *, is_manual, response) -> None:
    """A configured safety budget was reached: an expected stop, not a crash.

    No further model calls are made for this question.
    """
    category = (
        "question_token_budget"
        if isinstance(exc, QuestionTokenBudgetExceeded)
        else "question_time_budget"
        if isinstance(exc, QuestionTimeBudgetExceeded)
        else "question_model_call_budget"
    )
    setting, current_limit = budget_setting_for(exc)
    LOGGER.warning(
        "Sage stopped safely at configured question budget: %s "
        "(%s=%s; raise it in .env and restart to allow more)",
        category,
        setting,
        current_limit,
    )
    if not log_failed_question(question, category, is_manual=is_manual):
        show_log_write_failure("failed.txt")
    error_message = _MSG_BUDGET_STOP
    if st.session_state.get("show_debug"):
        # Operators only (technical details switched on); ordinary users never see settings.
        error_message += (
            f"\n\n*Operator note: this stop is controlled by {setting} "
            f"(currently {current_limit}). Raise it in .env and restart Sage "
            "to allow more. 0 turns the guard off only together with "
            "SAGE_ALLOW_UNLIMITED=1.*"
        )
    st.session_state.messages = [
        {"role": "user", "content": question},
        {
            "role": "assistant",
            "content": error_message,
            "simple_answer": "Sage reached its AI processing safety limit.",
            "verified": False,
            "limited_answer": True,
        },
    ]
    log_answer_decision(
        question, st.session_state.messages[-1], response, failure_category=category
    )


def _store_processing_error(error, question, *, is_manual, response) -> None:
    """Any other failure: show a safe message with an incident reference.

    Anticipated SageErrors (capacity, upstream outage, data integrity) are
    logged without a traceback; anything else gets a full incident log entry.
    """
    if isinstance(error, SageError):
        LOGGER.log(
            error.log_level, "Sage request stopped: %s (%s)", error.code, type(error).__name__
        )
        error_message = error.user_message
    else:
        incident = log_unexpected(LOGGER, "Sage request failed", error)
        error_message = (
            "Sorry, I hit technical issues and I am not able "
            f"to complete your request (reference {incident})."
        )
    if not log_failed_question(question, "processing_exception", is_manual=is_manual):
        show_log_write_failure("failed.txt")
    st.session_state.messages = [
        {"role": "user", "content": question},
        {
            "role": "assistant",
            "content": error_message,
            "simple_answer": "I couldn't safely check the data this time.",
            "verified": False,
        },
    ]
    log_answer_decision(
        question, st.session_state.messages[-1], response, failure_category="processing_exception"
    )
