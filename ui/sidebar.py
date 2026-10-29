"""The sidebar: sample questions by category, theme, and operator controls.

Operator controls (model choice, technical details) appear only for
administrators when TECHNICAL_DETAILS=1 (see technical_details_enabled).
"Next"/"Previous"/"Surprise me" move through the sample bank
(advance_sidebar_question).

Used by: app.py, ui/question_form.py.
"""

import os
import secrets

import streamlit as st

from ui.config import GENERATED_AUDIT_FILE, QUESTIONS_DIR
from ui.question_bank import (
    cached_question_categories,
    eligible_question_indices,
    question_categories,
    sample_question_titles,
)
from ui.session import select_sample_question
from utils import auth as sage_auth


def _technical_details_flag() -> bool:
    """True when the deployment sets TECHNICAL_DETAILS=1 (environment wins over secrets)."""
    value = os.getenv("TECHNICAL_DETAILS")
    if value is None:
        try:
            value = st.secrets.get("TECHNICAL_DETAILS", 0)
        except (KeyError, FileNotFoundError, OSError):
            value = 0
    return str(value).strip() == "1"


def technical_details_enabled():
    """Diagnostics and testing controls: shown when TECHNICAL_DETAILS=1.

    Password mode: to every signed-in user. SSO mode: to SAGE_ADMIN_EMAILS only.
    Missing, 0 or any other value hides them.
    """
    return _technical_details_flag() and sage_auth.can_view_technical_details(st.session_state)


def model_selection_enabled():
    """The model selector changes cost and behaviour: real administrators only
    (SSO + SAGE_ADMIN_EMAILS, or SAGE_ALLOW_SHARED_PASSWORD_ADMIN=1)."""
    return _technical_details_flag() and sage_auth.is_administrator(st.session_state)


def render_sidebar(api_key) -> None:
    """Keep the left panel focused on end-user controls, not download diagnostics.

    The full staging inventory, unfinished table IDs, partial-age warnings and
    long maintenance input are deliberately NOT rendered. The verified SQLite
    database and staging contents themselves are unchanged and remain available
    to the analytical core and external maintenance scripts.
    """
    with st.sidebar:
        from utils.sage_design import theme_keys, theme_label

        st.selectbox(
            "Appearance",
            theme_keys(),
            format_func=theme_label,
            key="sage_theme_choice",
            help="Change Sage's appearance. Graphs retain a readable light background.",
        )
        # Optional recovery route if a particular device cannot render a
        # data-heavy interactive graph. It never changes statistical results.
        if technical_details_enabled():
            st.checkbox(
                "Reduce visual effects (skip interactive charts)",
                key="sage_low_resource_mode",
                help="Accessibility and device performance option: all verified results and source "
                "links remain available.",
            )
        st.divider()
        questions = st.session_state.get("sage_flat_questions") or []
        category_map = cached_question_categories(questions, GENERATED_AUDIT_FILE)
        categories = [
            "ALL",
            "Economy & Price",
            "Industry",
            "Trade & Investment",
            "Population",
            "Households",
            "Society",
        ]
        if st.session_state.get("sage_question_category", "ALL") not in categories:
            st.session_state.sage_question_category = "ALL"
        st.markdown("**Find something interesting**")
        st.selectbox(
            "Topic", categories, key="sage_question_category", label_visibility="collapsed"
        )
        eligible = eligible_question_indices(
            questions, category_map, st.session_state.get("sage_question_category", "ALL")
        )
        if questions:
            selected_category = st.session_state.get("sage_question_category", "ALL")
            st.caption(f"{len(eligible)} sample questions in this topic")
            if not eligible:
                st.caption(
                    "No questions could be matched to this category from the current audit "
                    "or official SingStat metadata. Check the question-bank audit and local "
                    "knowledge metadata."
                )
            disabled = bool(st.session_state.get("auto_test_bank_hash")) or not eligible
            st.button(
                "Surprise me ✨",
                key="sage_random_question",
                width="stretch",
                disabled=disabled,
                on_click=advance_sidebar_question,
                args=("random",),
            )
            # Sequential navigation is for administrators/testing only.
            # Ordinary users retain the Surprise me button.
            if technical_details_enabled():
                st.button(
                    "Next question",
                    key="sage_sequential_button",
                    width="stretch",
                    disabled=disabled,
                    on_click=advance_sidebar_question,
                    args=("next",),
                )
            # Start position is also an administrator/testing control.
            if technical_details_enabled():
                st.selectbox("Start from:", ["1st", "current question"], key="sage_sequence_start")
            else:
                st.session_state["sage_sequence_start"] = "1st"

        # Initialise the configured model even when administrator controls are hidden.
        # The runtime uses this session key when answering user questions.
        from utils.model_client import MODEL_OPTIONS

        if st.session_state.get("sage_selected_model") not in MODEL_OPTIONS:
            configured_model = os.getenv("SAGE_CHAT_MODEL", "gpt-5.6-luna")
            st.session_state["sage_selected_model"] = (
                configured_model if configured_model in MODEL_OPTIONS else MODEL_OPTIONS[0]
            )

        # Administrator controls must be explicitly enabled by the deployment.
        # Do not expose the technical diagnostics on previously established sessions.
        if not technical_details_enabled():
            st.session_state.show_debug = False
        else:
            with st.expander("Advanced settings", expanded=False):
                st.markdown("**Language model**")
                if model_selection_enabled():
                    st.selectbox(
                        "Model for new questions", MODEL_OPTIONS, key="sage_selected_model"
                    )
                else:
                    st.caption(
                        f"Model: {st.session_state.get('sage_selected_model')} "
                        "(changing it needs an administrator)"
                    )
                st.checkbox(
                    "Show technical details",
                    key="show_debug",
                    help="Show Sage's processing audit and additional technical information.",
                )
            st.divider()
            ready_label = st.session_state.get("startup_ready_label")
            if ready_label:
                st.caption(str(ready_label).removeprefix("✅ "))
            elif st.session_state.get("startup_data_warning"):
                st.caption("Local verified data is not yet available")
            if not api_key:  # only shown when something is wrong
                st.caption("Configure OPENAI_API_KEY in Streamlit secrets")


def advance_sidebar_question(action) -> None:
    """Navigate only the selected category; reset conversation via existing selector."""
    questions = st.session_state.get("sage_flat_questions") or []
    categories = question_categories(questions, QUESTIONS_DIR / "Generated_Question_Audit.csv")
    eligible = eligible_question_indices(
        questions, categories, st.session_state.get("sage_question_category", "ALL")
    )
    if not eligible:
        return
    if action == "next":
        category = st.session_state.get("sage_question_category", "ALL")
        start = st.session_state.get("sage_sequence_start", "1st")
        signature = (category, start)
        cursor = st.session_state.get("sage_sequence_cursor")
        if st.session_state.get("sage_sequence_category") != signature or cursor is None:
            if start == "current question":
                current = int(st.session_state.get("test_question_index", 0))
                index = next((i for i in eligible if i >= current), eligible[0])
            else:
                index = eligible[0]
        else:
            index = next((i for i in eligible if i > cursor), eligible[0])
        st.session_state.sage_sequence_category = signature
        st.session_state.sage_sequence_cursor = index
    else:
        # A shuffled deck: every question once before any repeat, avoiding the table
        # families and categories just shown (independent random picks felt like looping).
        from utils.surprise_deck import draw, topic_of

        titles = sample_question_titles()
        current = int(st.session_state.get("test_question_index", -1))
        deck_state = st.session_state.setdefault("sage_surprise_deck", {})
        index = draw(
            eligible,
            deck_state,
            current=current,
            topic=lambda i: topic_of(titles.get(questions[i].get("question", ""), "")),
            category=lambda i: categories.get(questions[i].get("question", ""), ""),
        )
        st.session_state.sage_sequence_cursor = None
        st.session_state.sage_sequence_category = None
    select_sample_question(questions[index], index, like_clear=True)


def choose_sample_index(current, length, action, *, draw=None):
    """Choose a previous, next or genuinely different random sample safely."""
    if length <= 0:
        raise ValueError("Question bank cannot be empty.")
    current = max(0, min(int(current), length - 1))
    if action == "previous":
        return max(0, current - 1)
    if action == "next":
        return min(length - 1, current + 1)
    if action == "random":
        if length == 1:
            return current
        # The current entry is excluded without creating a 244-item sample pool.
        random_slot = (draw or secrets.randbelow)(length - 1)
        return random_slot if random_slot < current else random_slot + 1
    raise ValueError("Unsupported question navigation action.")
