"""The shapes of the dictionaries passed between Sage's layers.

Sage passes plain dicts (they travel as JSON to the model, into Streamlit's
session state and into logs). These TypedDicts DOCUMENT their keys for readers
and type checkers; they add no runtime checks. ``total=False`` because most
keys are present only on some results.

How they flow:

    core_secure tools  ->  ToolResult            (one per tool call)
    core_secure.ask_sage  ->  CoreResponse      (answer text + ToolResults)
    ui/answer_pipeline  ->  AnswerMessage        (stored in st.session_state.messages)
    a ToolResult or AnswerMessage may carry a ChartSpec (drawn by utils/charts.py)

Used by: ui/answer_pipeline.py, utils/core_secure.py, utils/table_tools.py.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ChartSeries(TypedDict, total=False):
    """One line or bar group: a name and one value per category (None = no data)."""

    name: str
    values: list[float | None]


class ChartSpec(TypedDict, total=False):
    """A chart built only from verified observations (see utils/charts.py)."""

    kind: str  # "trend" | "bar" | "grouped_bar" | "donut"
    title: str
    unit: str  # e.g. "Number", "Per Cent"
    categories: list[str]  # x-axis: periods (trend) or category names (bar)
    series: list[ChartSeries]
    x_axis_type: str  # "linear_years" or "category"


class SeriesSummary(TypedDict, total=False):
    """Endpoints of one official series within the requested period."""

    series_name: str
    series_no: str  # official series number, e.g. "1.2"
    unit: str
    earliest_period: str
    earliest_value: float
    latest_period: str
    latest_value: float
    change_latest_minus_earliest: float
    observation_count: int


class ToolResult(TypedDict, total=False):
    """What one analytical tool returned. Evidence only if ``success is True``.

    ``official_evidence`` (utils/answer_verification.py) additionally requires
    a complete table and an official source URL before any number is shown.
    """

    success: bool
    error: str  # set when success is False; safe to show the model
    error_code: str  # e.g. "official_table_id_not_found"
    table_id: str  # e.g. "Mxxxxxx"
    title: str
    source_url: str  # official Table Builder page
    table_complete: bool  # the whole table was downloaded and verified
    analysis_ready: bool
    series_summary: list[SeriesSummary]
    observations: list[dict[str, Any]]  # preview rows: series, period, value
    numeric_claims: list[float]  # every value an answer may quote
    chart_kind: str  # "time_series" | "category_comparison" | "none"
    chart_spec: ChartSpec | None
    latest_comparison_spec: ChartSpec | None
    chart_source_verified: bool


class CoreResponse(TypedDict, total=False):
    """What ``core_secure.ask_sage`` returns for one question."""

    answer: str  # Markdown written by the model (re-checked by the UI)
    tool_results: list[ToolResult]
    rag_results: list[dict[str, Any]]  # curated notes given to the model
    security_blocked: bool  # refused for safety
    scope_blocked: bool  # not a statistics question
    request_summary: str
    intent_decision: dict[str, Any]
    analytics_executed: bool  # at least one official tool actually ran
    tool_budget_exhausted: bool
    allow_multiple_charts: bool
    suppress_charts: bool
    table_catalogue_listing: bool  # "which tables cover X?" (no numbers)


class AnswerMessage(TypedDict, total=False):
    """One assistant message as stored in ``st.session_state.messages``."""

    role: str  # "assistant" (user messages have role "user")
    content: str  # the detailed answer shown (verified, or a fixed refusal)
    simple_answer: str  # one-line summary above it
    verified: bool  # every shown number is backed by official evidence
    limited_answer: bool  # verified, but narrowed or trimmed by a check
    source_question: str
    official_sources: list[tuple[str, str]]  # (table_id, url) pairs to cite
    chart_spec: ChartSpec | None
    chart_context: dict[str, Any]  # provenance of the chart (table, kind)
    chart_failure_reason: str | None  # why a verified answer has no chart
    delivery_status: str  # "verified", "verified_limited" or a failure label
