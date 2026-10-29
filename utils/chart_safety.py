"""Verified chart presentation safeguards extracted from core_secure.

This module deliberately contains no model calls, data retrieval, or state.

Used by: utils/core_secure.py.
"""

import re


def strip_inline_chart_markup(answer):
    """Remove AI-drawn Mermaid charts; only approved analytical charts may be shown.

    This is a presentation guard, not a statistical validation mechanism.
    It handles both fenced Mermaid charts and an occasional unfenced single-line
    ``mermaid xychart-beta ...`` representation.
    """
    if not isinstance(answer, str):
        return ""
    answer = re.sub(
        r"(?ims)^[ \t]*`{3,}[ \t]*mermaid[^\n]*\n.*?^[ \t]*`{3,}[ \t]*$\n?",
        "",
        answer,
    )
    answer = re.sub(
        r"(?im)^[ \t]*(?:mermaid[ \t]+)?xychart(?:-beta)?\b[^\n]*$\n?",
        "",
        answer,
    )
    return answer.strip()


def safe_model_chart_answer(answer, tool_results):
    """Never display an AI-authored chart as if it were an official-data plot."""
    if not isinstance(answer, str):
        return "Sage did not return a written answer."
    attempted_inline_chart = bool(
        re.search(
            r"(?im)^[ \t]*(?:`{3,}[ \t]*mermaid|(?:mermaid[ \t]+)?xychart(?:-beta)?\b)",
            answer,
        )
    )
    cleaned = strip_inline_chart_markup(answer)
    if attempted_inline_chart:
        verified_chart = any(
            isinstance(result, dict) and result.get("success") is True and result.get("chart_spec")
            for result in tool_results
        )
        if not verified_chart:
            return (
                "Sage could not generate a verified chart for this request. "
                "It will not substitute an AI-written diagram for a chart built "
                "from approved SingStat data. Please try the question again."
            )
    return cleaned or "Sage could not produce a supported written answer."


def answer_disclaims_verified_chart(answer) -> bool:
    """Fail closed when a generic answer explicitly says no chart was verified.

    Some exploratory tools return a valid chart of a SOURCE TABLE even when
    the requested combined comparison could not be verified. Never display
    that unrelated source chart below a prose disclaimer about missing data.
    """
    return isinstance(answer, str) and bool(
        re.search(
            r"(?i)\b(?:no|not)\s+(?:(?:verified|validated|approved|reliable|matching)\s+)?"
            r"(?:(?:combined|comparison)\s+)?(?:chart|graph|plot)\s+"
            r"(?:(?:was|could\s+be|has\s+been|is)\s+)?"
            r"(?:generated|produced|available|created|made)\b",
            answer,
        )
    )
