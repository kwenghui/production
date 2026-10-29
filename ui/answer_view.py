"""Drawing a stored answer: text, chart, sources, rating, technical details.

render_conversation() draws st.session_state.messages (written by
ui/answer_pipeline.py). It never re-decides what is verified: an approved
chart is drawn as approved, and only a rendering error can hide it.

* Text - clean_ai_markdown keeps AI Markdown safe (official links only,
  literal "$", no raw HTML).
* Chart - render_verified_graphs, or the table explorer (ui/table_explorer.py).
* Feedback - thumbs up/down with an optional note (record_answer_rating).
* Technical details - token usage and the full response, operators only.

Used by: app.py.
"""

from datetime import datetime, timezone
import html
import json
import re

import streamlit as st

from ui.components import md_literal
from ui.config import DEBUG_FILE_LOCK, DEBUG_LOG_FILE, LOGGER
from ui.table_explorer import render_table_explorer
from utils import credentials as sage_credentials, privacy as sage_privacy
from utils.answer_verification import is_official_source, OFFICIAL_LINK_HOSTS
from utils.chart_selection import question_chart_key
from utils.secure_files import open_private_append


def token_totals_html(report) -> str:
    """Three token-count boxes (input / output / total) as self-contained HTML."""

    def count(key):
        """A token count from the report, with thousands separators (0 if missing)."""
        try:
            return f"{int(report.get(key, 0) or 0):,}"
        except (TypeError, ValueError):
            return "0"

    cells = "".join(
        f'<div class="sage-token-box"><div class="sage-token-label">{html.escape(label)}</div>'
        f'<div class="sage-token-value">{count(key)}</div></div>'
        for label, key in (
            ("Input tokens", "input_tokens"),
            ("Output tokens", "output_tokens"),
            ("Total tokens", "total_tokens"),
        )
    )
    return (
        '<style>.sage-token-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));'
        'gap:.8rem;margin:.4rem 0 .8rem}.sage-token-box{background:var(--sage-surface);'
        'border:1px solid var(--sage-line);border-radius:14px;padding:.75rem 1rem}'
        '.sage-token-label{color:var(--sage-muted);font-size:.85rem}'
        '.sage-token-value{color:var(--sage-text);font-size:1.7rem;font-weight:700;line-height:1.3}</style>'
        f'<div class="sage-token-row">{cells}</div>'
    )


def clean_ai_markdown(text):
    """Sanitise AI Markdown and remove unsupported inline chart code."""
    if not isinstance(text, str):
        return ""
    # Backstop: web-search citation markers must never reach the page.
    # local: keeps this function self-contained
    from utils.text_cleanup import strip_tool_citations

    text = strip_tool_citations(text)

    # Mermaid/xychart written by an AI is not an approved Sage visualisation.
    # Strip BEFORE removing backticks, otherwise a fenced block becomes raw text.
    text = re.sub(
        r"(?ims)^[ \t]*`{3,}[ \t]*mermaid[^\n]*\n.*?^[ \t]*`{3,}[ \t]*$\n?",
        "",
        text,
    )
    text = re.sub(
        r"(?im)^[ \t]*(?:mermaid[ \t]+)?xychart(?:-beta)?\b[^\n]*$\n?",
        "",
        text,
    )

    # Prevent remote image embedding and browser-side tracking via Markdown.
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[image removed: \1]", text)

    # Remove inline-code backticks to keep answer typography consistent.
    text = text.replace("`", "")

    # Links in model output are untrusted: a prompt-injected source or question
    # can make the model emit phishing or data-exfiltration URLs. Government
    # (*.gov.sg, https only) links stay clickable. Any other URL is still shown,
    # but as inert code text that cannot be clicked or auto-linked.
    def _trusted(url) -> bool:
        """True for an https link to an official host (no user@host tricks)."""
        found = re.match(r"(?i)https://([^/?#\s]+)", url.strip())
        if not found or "@" in found.group(1):  # reject user@host tricks
            return False
        host = found.group(1).split(":")[0].lower().rstrip(".")
        return host in OFFICIAL_LINK_HOSTS

    def _inert(url):
        """A URL shown as code, so it is visible but not clickable."""
        return "`" + url.replace("`", "") + "`"

    def _link(match):
        """Keep a Markdown or bare link only if it is official; otherwise show it inert."""
        label, target, angle, bare, www = match.groups()
        if target is not None:  # [label](target "title")
            if _trusted(target):
                return match.group(0)
            return (label + " (" + _inert(target) + ")") if label.strip() else _inert(target)
        url = angle or bare or www
        trail = ""
        if bare or www:  # keep sentence punctuation outside
            stripped = url.rstrip(".,;:!?)")
            trail, url = url[len(stripped) :], stripped
        if angle is None and _trusted(url):
            return url + trail
        return (url if _trusted(url) else _inert(url)) + trail

    text = re.sub(
        r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"  # inline link
        r"|<((?:https?|ftp|mailto|javascript|data):[^>\s]+)>"  # autolink
        r"|(?<![\w`(])((?:https?|ftp)://[^\s<>`]+)"  # bare URL
        r"|(?<![\w./`])(www\.[^\s<>`]+)",  # bare www.
        _link,
        text,
    )
    # Reference-style definitions: [1]: https://example.com
    text = re.sub(
        r"(?im)^([ \t]*\[[^\]]+\]:[ \t]*)(\S+)(.*)$",
        lambda m: m.group(0) if _trusted(m.group(2)) else m.group(1) + _inert(m.group(2)),
        text,
    )

    # Prevent dollar signs from being interpreted as LaTeX delimiters.
    text = re.sub(r"(?<!\\)\$", r"\\$", text)

    # Normalise excessive Markdown emphasis markers.
    text = re.sub(r"\*{3,}", "**", text)

    # Collapse repeated horizontal whitespace while preserving line breaks.
    text = re.sub(r"[ \t]+", " ", text)

    # Thousand separators for long plain numbers (11519.8715 -> 11,519.8715). Links,
    # URLs, IDs such as Cxxxxxx, years and already-grouped numbers are left untouched.
    def _group(match):
        """Add thousands separators to a long whole number (decimals kept)."""
        whole, _, fraction = match.group(0).partition(".")
        return f"{int(whole):,}" + ("." + fraction if fraction else "")

    pieces = re.split(r"(\]\([^)]*\)|https?://\S+)", text)
    for index in range(0, len(pieces), 2):
        pieces[index] = re.sub(
            r"(?<![\w.,/=:#-])(?:\d{5,}(?:\.\d+)?|\d{4}\.\d+)(?![\w,])", _group, pieces[index]
        )
    text = "".join(pieces)

    return text


def render_chart_with_summary(figure, spec, selected, *, key_prefix):
    """Render a prevalidated graph only; chart summaries are intentionally retired.

    Retain this callable for existing validated legacy chart paths, so removing
    the summary cannot change source validation or break callers.
    """
    if spec.get('hierarchy_note'):
        st.caption(md_literal(spec['hierarchy_note']))
    # This is a presentation cap only; verified evidence and numerical values
    # remain untouched. A 40-series x 500-point Vega chart can overwhelm a
    # mobile browser even when it has passed every statistical check.
    traces = list(getattr(figure, 'data', ()) or ())

    def _trace_length(value):
        """Length of a value, or 0 when it has none."""
        try:
            return len(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    estimated_points = sum(
        max(
            _trace_length(getattr(t, 'y', None)),
            _trace_length(getattr(t, 'x', None)),
            _trace_length(getattr(t, 'values', None)),
        )
        for t in traces
    )
    if estimated_points > 3200:
        st.info(
            'This graph is too large for reliable browser display. The verified answer remains '
            'above; narrow the series selection where available.'
        )
        return
    with st.container(border=True):
        from utils.lightweight_charts import from_plotly_figure
        from utils.sage_design import style_altair_chart

        st.altair_chart(
            style_altair_chart(from_plotly_figure(figure, label_mode="Balanced")),
            width='stretch',
            theme=None,
            key=f'chart_{key_prefix}',
        )


def display_debug(response) -> None:
    """Display optional intent, RAG and analytical-tool diagnostics."""
    if not st.session_state.show_debug:
        return

    intent_decision = response.get("intent_decision")
    if isinstance(intent_decision, dict):
        with st.expander("🧭 Request intent and scope decision"):
            st.write(f"Summary: {intent_decision.get('summary', '')}")
            st.write(f"In scope: {intent_decision.get('in_scope', False)}")
            st.write(
                "Relevant datasets: " + ", ".join(intent_decision.get("relevant_datasets", []))
            )
            st.write(f"Reason: {intent_decision.get('reason', '')}")
            st.write(f"Analytical tools executed: {response.get('analytics_executed', False)}")

    from utils.pipeline_audit import stage_rows

    with st.expander("Question processing · stage-by-stage audit", expanded=True):
        st.caption("Recorded processing steps and their data sources.")
        from utils.sage_design import render_readable_table

        render_readable_table(
            [
                {key: value for key, value in row.items() if key != "Status"}
                for row in stage_rows(response)
            ],
            key="question_processing_audit",
            page_size=20,
        )

    with st.expander("🔎 RAG retrieval details"):
        rag_results = response.get("rag_results", [])

        if not rag_results:
            st.write(
                "No matching source-linked documentation was retrieved. "
                "Numerical analytical results are displayed separately; "
                "Sage never invents methodology notes."
            )
        try:
            st.caption(
                f"Persistent verified table descriptions: "
                f"{st.session_state.core.get_rag_catalog().count():,}"
            )
        except (AttributeError, OSError, ValueError):
            pass

        for item in rag_results:
            st.caption("Source type: " + str(item.get("doc_type") or "curated reference"))
            st.markdown(md_literal(f"**{item.get('id', '')} — {item.get('title', '')}**"))
            st.write(f"Hybrid score: {item.get('hybrid_score', '')}")
            st.write(item.get("text", ""))

            if item.get("source_url"):
                st.caption(item["source_url"])

            st.divider()

    with st.expander("🛠️ Analytical tool results"):
        tool_results = response.get("tool_results", [])
        set_aside = response.get("set_aside_tool_results") or []

        if not tool_results and set_aside:
            st.warning(
                "The answer was replaced because none of these results could verify the "
                "required table. They are kept here so the reason is visible."
            )
            for index, result in enumerate(set_aside, start=1):
                if not isinstance(result, dict):
                    continue
                status = "OK" if result.get("success") is True else "FAILED"
                complete = (
                    "" if result.get("table_complete") is not False else (" · table incomplete")
                )
                reason = str(result.get("error") or result.get("coverage_warning") or "").strip()
                st.markdown(
                    md_literal(
                        f"**Set-aside result {index}:** {status} · "
                        f"{result.get('table_id') or 'no table'}{complete}"
                    )
                )
                if reason:
                    st.caption(md_literal(reason[:600]))
        elif not tool_results:
            st.write("No numerical tool was required.")

        for index, result in enumerate(tool_results, start=1):
            st.markdown(f"**Tool result {index}**")
            from utils.sage_design import render_debug_json_text

            render_debug_json_text(result, key=f"tool_result_{index}")


def render_verified_graphs(answer) -> None:
    """Draw only the current question's verified, relevance-checked graph."""
    active = st.session_state.get("active_question")
    if answer.get("source_question") != active:
        LOGGER.warning("Skipped chart from another or unspecified question")
        return
    if not (answer.get("verified", False) and not answer.get("suppress_charts")):
        return
    if isinstance(answer.get("chart_spec"), dict):
        selected = {"success": True, "chart_spec": answer["chart_spec"], "chart_priority": 100}
        selected.update(answer.get("chart_context", {}))
        # This chart already passed source, selection, scope, hierarchy and
        # numerical preflight during answer construction.  Re-running
        # a second rendering pass here used to apply another set of topic-specific
        # verification gates and could make a VERIFIED chart disappear.
        # Rendering has no authority to reject an approved analytical result.
        try:
            from utils.charts import build_chart  # Plotly: imported once a chart is drawn

            approved_figure = build_chart(answer["chart_spec"])
            render_chart_with_summary(
                approved_figure,
                answer["chart_spec"],
                selected,
                key_prefix="chart_" + question_chart_key(active, answer["chart_spec"]),
            )
        except (ValueError, TypeError, KeyError) as exc:
            # A genuine renderer error is not a statistical rejection.  Keep
            # the verified written answer and report the technical problem.
            LOGGER.exception('Approved chart could not be rendered: %s', type(exc).__name__)
            st.error(
                'The verified chart could not be displayed because of a rendering error. '
                'The checked numerical answer remains available below.'
            )
    else:
        # A missing graph should be discoverable rather than silently looking
        # like an accidentally removed feature. Do not infer or plot numbers.
        missing_reason = answer.get('chart_failure_reason')
        explanations = {
            'official_data_incomplete': 'Official source data for the requested breakdown are '
            'incomplete.',
            'question_chart_mismatch': 'Available graphs did not match the exact question or '
            'population.',
            'chart_render_rejected': 'The proposed graph did not pass rendering or source checks.',
            'no_usable_series': 'No complete matching numerical series were available for a graph.',
        }
        st.caption(
            'No verified graph: '
            + explanations.get(
                missing_reason,
                'No complete, matching and renderable statistical chart was returned.',
            )
            + ' Checked written results, if any, remain available below.'
        )


def format_store_status_full_answer(content):
    """Convert raw line-broken storage counters into a labelled two-column table.

    Only format the recognisable status answer. Never change ordinary official
    statistical answers or extract unrecognised model-generated numbers.
    """
    text = str(content or '')
    labels = ("Integrity-checked SQLite tables", "Validated official mirrors")
    if not all(label in text for label in labels):
        return text
    patterns = (
        ("Integrity-checked SQLite tables", r"Integrity-checked SQLite tables\s*[:\n]\s*([0-9,]+)"),
        ("Validated official mirrors", r"Validated official mirrors\s*[:\n]\s*([0-9,]+)"),
        ("Saved observations", r"([0-9,]+)\s+saved observations"),
        ("Archived/staged copies", r"([0-9,]+)\s+archived/staged copies retained"),
    )
    rows = []
    for label, pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            return text
        rows.append(f"| {label} | {match.group(1)} |")
    note = re.search(
        r"([0-9,]+) table\(s\) have [0-9,]+ unique pending series\.[^\n]*", text, flags=re.I
    )
    trailing = (
        '\n\n**Separate unfinished downloads (not the verified-table total):** '
        + note.group(0).strip()
        if note
        else ''
    )
    return '| Metric | Count |\n|:--|--:|\n' + '\n'.join(rows) + trailing


def publication_notes_by_table(response):
    """{table_id: [notes]} from verified tool results (SingStat footnote facts), in order."""
    found = {}
    for item in (response or {}).get("tool_results") or [] if isinstance(response, dict) else []:
        if not (isinstance(item, dict) and item.get("success") and item.get("table_id")):
            continue
        notes = [str(n) for n in item.get("publication_notes") or [] if str(n).strip()]
        if notes and str(item["table_id"]) not in found:
            found[str(item["table_id"])] = notes
    return found


def render_publication_notes(response) -> None:
    """'Notes from SingStat' under the answer: data last updated, survey year, rounding, '–'.

    Taken from the table's own metadata and footnote, so they appear whatever the AI
    wrote. The "Data last updated" date is the one SingStat shows on the table page.
    """
    from utils.official_source import data_last_updated

    notes_by_table = publication_notes_by_table(response)
    for item in (response or {}).get("tool_results") or [] if isinstance(response, dict) else []:
        if isinstance(item, dict) and item.get("success") and item.get("table_id"):
            notes_by_table.setdefault(str(item["table_id"]), [])
    for table_id, notes in notes_by_table.items():
        updated = data_last_updated(table_id)
        parts = ([f"Data last updated {updated}."] if updated else []) + list(notes)
        if parts:
            st.caption(md_literal(f"**Notes from SingStat ({table_id}):** " + " ".join(parts)))


def render_assumptions_and_scope(answer) -> None:
    """Explain source-bound series selection for every SingStat domain."""
    from utils.scope_general import scope_from_evidence

    scopes = scope_from_evidence(answer, st.session_state.get('last_response'))
    if not scopes:
        return
    with st.expander('Data sources & scope', expanded=False):
        st.caption(
            'Series used to prepare the written answer. The interactive chart can display '
            'other verified series from the same table.'
        )
        for scope in scopes:
            st.markdown('**SingStat ' + scope['table_id'] + '**')
            for label in scope['labels']:
                st.text('• ' + label)
            if scope['unit']:
                st.caption('Reported unit: ' + scope['unit'])
        st.caption(
            'The selected series are not necessarily an exhaustive or additive total. '
            'Some groups may overlap. These are Sage\'s selections from verified '
            'table results, not an official definition of a broader term.'
        )


def hide_redundant_evidence_heading(content):
    """Remove a redundant evidence-section title from DISPLAY text only.

    Source IDs, citations, underlying evidence and verification results stay
    unchanged; a source section with useful body text is not deleted.
    """
    if not isinstance(content, str):
        return content
    lines = content.splitlines(keepends=True)
    heading = re.compile(
        r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?(?:Evidence from official tables|Observed pattern)"
        r"(?:\*\*|__)?\s*:?\s*$",
        re.IGNORECASE,
    )
    return ''.join(line for line in lines if not heading.fullmatch(line.strip()))


def render_token_usage_footer(report) -> None:
    """At the end of technical details show actual API token usage per call."""
    if not st.session_state.get("show_debug") or not isinstance(report, dict):
        return
    st.divider()
    st.subheader("Token usage — current question")
    count = report.get("reported_calls", 0)
    total = report.get("api_calls", 0)
    if not total:
        st.caption(
            "No OpenAI API call was made for this question; all processing stayed in local "
            "functions."
        )
        return
    st.caption(
        f"{total} OpenAI API call(s). Each task label is recorded when Sage requests the "
        f"call; token counts come from the API response."
    )
    # The key fingerprint is no longer shown; only a conflicting second key is worth a warning.
    conflict = sage_credentials.conflicting_keys_warning()
    if conflict:
        st.warning(conflict)
    # Plain HTML instead of st.metric: st.metric loads an extra browser file on demand,
    # which fails with "Failed to fetch dynamically imported module" when the app was
    # redeployed while the page was open. These boxes need nothing extra.
    st.markdown(token_totals_html(report), unsafe_allow_html=True)
    st.caption(
        f"Cached input tokens: {report.get('cached_input_tokens', 0):,} "
        f"(already included in input); reasoning output tokens: "
        f"{report.get('reasoning_output_tokens', 0):,} (already included in output)."
    )
    rows = []
    for index, call in enumerate(report.get("calls", []), start=1):
        rows.append(
            {
                "Call": index,
                "Task": call.get("stage", "Unlabelled call"),
                "What Sage was doing": call.get("purpose", "API request"),
                "API": call.get("endpoint", ""),
                "Model": call.get("model", ""),
                "Input": call.get("input_tokens") if call.get("reported") else None,
                "Output": call.get("output_tokens") if call.get("reported") else None,
                "Cached input": call.get("cached_input_tokens") if call.get("reported") else None,
                "Reasoning output": call.get("reasoning_output_tokens")
                if call.get("reported")
                else None,
                "Total": call.get("total_tokens") if call.get("reported") else None,
            }
        )
    from utils.sage_design import render_readable_table

    render_readable_table(rows, key="token_usage_calls", page_size=20)
    if report.get("unreported_calls"):
        st.warning(
            "Some API responses did not include usage. Totals above are partial, not " "estimates."
        )
    st.caption(
        "Figures reflect this question's SDK calls only, including any embedding calls made "
        "during it. "
        "Earlier index-building or other sessions are not included. No prompts, responses "
        "or keys are stored in this usage report."
    )


def record_answer_rating(answer_id, rating, question) -> None:
    """Persist an explicit human answer rating to debug.txt, never automatically."""
    if rating not in ("up", "down"):
        raise ValueError("Unsupported feedback rating")
    if not answer_id or not isinstance(answer_id, str):
        raise ValueError("Missing answer identity")
    # Bind feedback to one displayed answer; navigating clears the visible state.
    st.session_state.sage_answer_ratings = {
        **st.session_state.get("sage_answer_ratings", {}),
        answer_id: rating,
    }
    entry = (
        "[USER_FEEDBACK] "
        + json.dumps(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "answer_id": answer_id,
                "rating": rating,
                "question_hash": sage_privacy.question_digest(question),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    try:
        with DEBUG_FILE_LOCK:
            with open_private_append(DEBUG_LOG_FILE) as fp:
                fp.write(entry)
    except OSError:
        LOGGER.exception("Unable to save user rating to debug.txt")
        st.session_state.sage_feedback_log_error = True
    else:
        st.session_state.sage_feedback_log_error = False


def render_feedback_download() -> None:
    """Administrators: counts of 👍 / 👎 and a CSV of every rating since the last restart."""
    from utils.feedback_export import read_feedback, summary, to_csv

    rows = read_feedback(DEBUG_LOG_FILE)
    with st.expander(f"Answer feedback · {len(rows)} rating click(s) recorded"):
        if not rows:
            st.caption(
                "No ratings recorded yet. On Streamlit Community Cloud the file is "
                "cleared whenever the app restarts, so download it regularly."
            )
            return
        counts = summary(rows)
        st.markdown(
            f"**{counts['up']}** answers rated helpful 👍 · **{counts['down']}** rated not "
            f"helpful 👎 (latest rating per answer; {counts['answers']} answers rated)."
        )
        st.caption(
            "Questions are stored only as keyed codes, never as text. On Streamlit "
            "Community "
            "Cloud this file is cleared whenever the app restarts, so download it regularly."
        )
        arguments = dict(
            label="Download feedback (CSV)",
            data=to_csv(rows),
            file_name=f"sage-feedback-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv",
            mime="text/csv",
            key="sage_feedback_download",
        )
        try:
            st.download_button(on_click="ignore", **arguments)  # no page re-run on download
        except TypeError:
            st.download_button(**arguments)


def render_answer_rating(answer) -> None:
    """Offer feedback under the displayed answer, without triggering question reruns."""
    if not isinstance(answer, dict):
        return
    current_question = next(
        (
            str(msg.get("content", ""))
            for msg in reversed(st.session_state.get("messages", []))
            if msg.get("role") == "user"
        ),
        "",
    )
    # Fingerprint *this* answer so earlier ratings don't leak to another question.
    # Keyed (HMAC) like every other pseudonym in the logs: an unkeyed hash of a short
    # question can be reversed by hashing guesses.
    answer_id = sage_privacy.question_digest(
        current_question
        + "\x00"
        + str(answer.get("content", ""))
        + "\x00"
        + str(answer.get("delivery_status", "")),
        24,
    )

    # In its own fragment: a click re-runs only these buttons, not the whole page
    # (answer, chart, table), which made the page pale and unresponsive.
    @st.fragment
    def _rating_fragment():
        """The rating buttons as their own fragment (a click redraws only them)."""
        _render_rating_controls(answer_id, current_question)

    _rating_fragment()


def _render_rating_controls(answer_id, current_question) -> None:
    """The two rating buttons and the thank-you note (drawn inside a fragment)."""
    existing = st.session_state.get("sage_answer_ratings", {}).get(answer_id)
    # The chosen rating is drawn as a primary (solid) button below, which the theme makes
    # clearly different; the old pale tint applied with CSS was barely visible.
    st.markdown("**Was this answer helpful?**")
    try:  # newer Streamlit: buttons side by side, no wide columns
        row = st.container(horizontal=True, gap="small")
    except TypeError:
        row = st.columns([1.3, 1.3, 5])[0]
    with row:
        st.button(
            "👍 Helpful",
            key=f"sage_rating_up_{answer_id}",
            help="This answer was helpful",
            type="primary" if existing == "up" else "secondary",
            on_click=record_answer_rating,
            args=(answer_id, "up", current_question),
        )
        st.button(
            "👎 Not helpful",
            key=f"sage_rating_down_{answer_id}",
            help="This answer needs improvement",
            type="primary" if existing == "down" else "secondary",
            on_click=record_answer_rating,
            args=(answer_id, "down", current_question),
        )
    if existing in ("up", "down"):
        st.caption(
            "Thanks for your feedback. You marked this answer as "
            + ("helpful." if existing == "up" else "not helpful.")
            + " You can change your choice at any time."
        )
    if st.session_state.get("sage_feedback_log_error"):
        st.warning("Feedback could not be saved to debug.txt.")


def render_conversation() -> None:
    """Display the verified chart, scope, series catalogue and full answer."""
    messages = st.session_state.messages
    if not messages:
        return
    answer = next((item for item in reversed(messages) if item["role"] == "assistant"), None)
    if answer is None:
        return

    # No duplicate question or 'Graph' heading: the graph follows the editor. (The divider line
    # that used to sit here added a tall gap above the answer; spacing is set in the stylesheet.)
    with st.container(key="sage_answer_surface"):
        # Compact, recognisable answer heading. The marker works in light and dark themes.
        st.markdown(
            '<div class="sage-response-heading"><span class="sage-response-dot" '
            'aria-hidden="true"></span><span>Sage’s answer</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            clean_ai_markdown(
                format_store_status_full_answer(
                    hide_redundant_evidence_heading(answer.get("content", ""))
                )
            )
        )
        render_publication_notes(st.session_state.get("last_response"))
        render_assumptions_and_scope(answer)
        # A user-selectable reading safeguard avoids Altair/Vega canvases on
        # older phones, security-isolated browsers, or devices with little GPU
        # memory. It affects rendering only: verified text, observations and
        # source citations are not modified or recalculated.
        if st.session_state.get("sage_low_resource_mode", False):
            st.caption("Lightweight reading is on. Interactive graphs are hidden on this device.")
            sources = answer.get("official_sources") or []
            if sources:
                st.markdown("**Official source tables**")
                for table_id, source_url in sources[:12]:
                    label = re.sub(r"[^A-Za-z0-9_-]", "", str(table_id))[:32] or "table"
                    if is_official_source(source_url):
                        st.markdown(clean_ai_markdown(f"[SingStat {label}]({source_url})"))
                    else:
                        st.markdown(f"SingStat {label}")
        else:
            # Table-first presentation: prefer complete-table series where
            # possible, then validated chart previews as the fallback.
            from utils.chart_gallery import describe_identified_tables

            _identified_tables = describe_identified_tables(st.session_state.get('last_response'))
            try:
                if _identified_tables:
                    render_table_explorer(answer, st.session_state.core)
                else:
                    render_verified_graphs(answer)
            except Exception:  # noqa: BLE001 - optional chart rendering must never hide the verified answer
                LOGGER.exception(
                    'Optional chart/gallery rendering failed; retaining verified ' 'answer'
                )
                st.warning(
                    'The graph could not be displayed on this device. The verified written '
                    'answer and official sources remain available above.'
                )
        # The interactive graph gallery already lists selectable series; do not
        # repeat the separate legacy catalogue expander in the main page.

        render_answer_rating(answer)
        st.caption(
            "Sage can make mistakes. Check important figures against the official SingStat "
            "source."
        )
        if st.session_state.show_debug and isinstance(st.session_state.last_response, dict):
            # Own fragment: paging through its tables re-runs only this section.
            @st.fragment
            def _technical_details():
                # Keep the compact token summary ahead of the potentially very large
                # diagnostic payload.  This makes usage visible without waiting for
                # thousands of lines of tool/debug details to render first.
                """Draw the operator-only technical details: token usage, then the full diagnostics."""
                render_token_usage_footer(st.session_state.get("token_usage_report"))
                render_feedback_download()
                st.divider()
                st.subheader("Tear it apart")
                display_debug(st.session_state.last_response)

            _technical_details()


def render_processing_indicator(target=None) -> None:
    """Show a CSS-animated indeterminate bar while the browser awaits analysis.

    CSS animation runs in the browser even when Streamlit's Python thread is
    blocked by a synchronous analytical request. It indicates activity rather
    than estimating task completion. The markup contains no external assets.
    """
    (target if target is not None else st).markdown(
        """
        <style>
        @keyframes sage-indeterminate {
          0% { transform: translateX(-110%); }
          100% { transform: translateX(440%); }
        }
        .sage-processing { margin: 0.4rem 0 1rem; }
        .sage-processing-label {
          color: var(--sage-text); font-size: 1rem; margin-bottom: 0.55rem;
        }
        .sage-processing-track {
          width: 100%; height: 6px; border-radius: 99px;
          background: var(--sage-line); overflow: hidden;
        }
        .sage-processing-sweep {
          height: 100%; width: 23%; border-radius: 99px;
          background: var(--sage-accent);
          animation: sage-indeterminate 1.45s ease-in-out infinite;
          will-change: transform;
        }
        @media (prefers-reduced-motion: reduce) {
          .sage-processing-sweep { animation: none; width: 100%; }
        }
        </style>
        <div class="sage-processing" role="status" aria-label="Processing question">
          <div class="sage-processing-label">Checking official statistics and preparing the answer…</div>
          <div class="sage-processing-track" role="progressbar" aria-label="Analysis in progress">
            <div class="sage-processing-sweep"></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
