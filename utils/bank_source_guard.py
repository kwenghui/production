"""Exact source control for Sage generated questions.

The published question-bank audit maps a sample question to one official table ID.
This helper constrains the generic tool route to that ID without ever treating
catalogue metadata as statistical evidence. No HTTP calls occur in this helper.

Used by: utils/core_secure.py.
"""

from __future__ import annotations

import re

_TABLE_ID = re.compile(r"[MCD]\d{5,8}\Z")
_TABLE_TOOLS = frozenset(
    {
        "inspect_singstat_table",
        "list_singstat_series",
        "query_singstat_table",
        "compare_singstat_series",
    }
)


def valid_required_table(table_id):
    """Accept only a properly formatted audited SingStat TS/CT/DC identifier."""
    return table_id if isinstance(table_id, str) and _TABLE_ID.fullmatch(table_id) else None


def constrain_tool_source(
    name, arguments, required_table_id, *, first_round: bool = False
) -> tuple:
    """Return (safe arguments or None, optional blocked result, correction flag).

    A wrong-ID inspection/inventory request is safely rerouted to the audited
    source, with any foreign-table series filter cleared. A wrong-ID numerical
    query/comparison is *blocked*, not rewritten, because its named series may
    refer to a different measure. The model can reissue it after inspecting the
    audited table. No special-case table identifiers or numeric values exist.
    """
    required = valid_required_table(required_table_id)
    if required is None:
        return arguments, None, False
    if name not in _TABLE_TOOLS:
        return (
            None,
            {
                "success": False,
                "error_code": "source_bound_question",
                "expected_table_id": required,
                "error": (
                    "This generated question is bound to official table "
                    + required
                    + ". Use inspect_singstat_table, list_singstat_series or "
                    "the generic analytical tools for this exact table."
                ),
            },
            False,
        )
    if not isinstance(arguments, dict):
        return (
            None,
            {
                "success": False,
                "error_code": "invalid_table_arguments",
                "error": "Expected a table-specific argument object.",
            },
            False,
        )
    actual = arguments.get("table_id")
    if actual == required:
        # The first analysis round is intentionally a broad source inventory.
        # A model's speculative series phrase must not hide other published series.
        if first_round and name == "inspect_singstat_table" and arguments.get("series_search"):
            return {**arguments, "series_search": None}, None, True
        return arguments, None, False
    if name in {"inspect_singstat_table", "list_singstat_series"}:
        return {**arguments, "table_id": required, "series_search": None}, None, True
    return (
        None,
        {
            "success": False,
            "error_code": "wrong_official_table",
            "expected_table_id": required,
            "rejected_table_id": str(actual or "")[:20],
            "error": (
                "Do not use observations or series selectors from another table. "
                "The sample question's audited source is " + required + ". "
                "Inspect that table's published series, then query exactly "
                "that table using the matching official series identifier."
            ),
        },
        False,
    )
