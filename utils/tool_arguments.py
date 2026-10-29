"""Validate the arguments the model passes to a tool (they are untrusted input).

Every tool call goes through validate_tool_arguments BEFORE anything runs:
only known tools (ALLOWED_TOOL_NAMES), exactly the expected keys, integers
and booleans of the right type and range, a well-formed table ID
(normalize_singstat_table_id) and a bounded series search. A violation
raises ValueError; the tool loop turns it into a safe error result.

Pure functions: no model, network or Streamlit.

Used by: utils/core_secure.py, utils/official_links.py,
utils/table_tools.py.
Tested in: tests/test_core_modules.py.
"""

import re


GENERIC_TOOL_NAMES = {
    "inspect_singstat_table",
    "list_singstat_series",
    "query_singstat_table",
    "compare_singstat_series",
}
SINGSTAT_TABLE_ID_PATTERN = re.compile(r"^(?:[A-Z][A-Z0-9]{1,18}[0-9][A-Z0-9]*|[0-9]{3,20})$")
MAX_GENERIC_SERIES = 8
# Only the four generic tools exist; they treat every SingStat table the same way.
# The model may call these tools and nothing else (web search is a separate,
# domain-restricted OpenAI tool).
ALLOWED_TOOL_NAMES = frozenset(GENERIC_TOOL_NAMES)


def _require_exact_keys(arguments, required) -> None:
    """Validate that a tool-argument dictionary contains exactly the required keys."""
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    actual = set(arguments)
    required = set(required)
    if actual != required:
        raise ValueError("Tool arguments did not match the approved schema.")


def _require_int(value, name, minimum=None, maximum=None, allow_none: bool = False) -> None:
    """Validate an integer tool argument and its optional allowed range."""
    if allow_none and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} is below the allowed range.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} is above the allowed range.")


def _require_bool(value, name) -> None:
    """Validate that a tool argument is a Boolean value."""
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false.")


def normalize_singstat_table_id(table_id):
    """Return a validated uppercase SingStat table ID; reject URL/path injection."""
    if not isinstance(table_id, str):
        table_id = str(table_id or "")
    table_id = table_id.strip().upper()
    if not SINGSTAT_TABLE_ID_PATTERN.fullmatch(table_id):
        raise ValueError("Invalid SingStat table ID.")
    return table_id


_SELECTOR_LIST = re.compile(
    r"\s*series\s*(?:id|no)?\s*[:=]\s*[A-Za-z0-9._-]{1,64}"
    r"(?:\s*,\s*series\s*(?:id|no)?\s*[:=]\s*[A-Za-z0-9._-]{1,64})*\s*",
    re.I,
)


def _validate_optional_series_search(value) -> None:
    """Validate a series filter: a phrase, a list of exact series names, or a list of exact
    selectors ("series id:1, series id:2.1"); 2,000 characters at most, no control
    characters. The query tool reads at most max_series series."""
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError("series_search must be null or a short text phrase.")
    if any(char in value for char in ("\x00", "\r", "\n")):
        raise ValueError("series_search contains blocked control characters.")
    text = value.strip()
    # The filter is only matched locally against official series names, so a long
    # list of names or selectors is harmless; 2,000 characters bounds the work.
    if len(text) > 2000:
        # The message is read by the model: it says how to retry in ONE call.
        raise ValueError(
            "series_search is too long (2,000 characters at most). Use exact "
            "selectors such as 'series id:1, series id:2.1' (the tool reads at "
            f"most {MAX_GENERIC_SERIES} series per call)."
        )


def validate_tool_arguments(tool_name, arguments):
    """Second validation layer; never trust model-generated tool arguments."""
    if tool_name not in ALLOWED_TOOL_NAMES:
        raise ValueError("Blocked unapproved tool.")

    if tool_name == "inspect_singstat_table":
        _require_exact_keys(arguments, {"table_id", "series_search"})
        arguments["table_id"] = normalize_singstat_table_id(arguments["table_id"])
        _validate_optional_series_search(arguments["series_search"])

    elif tool_name == "list_singstat_series":
        _require_exact_keys(arguments, {"table_id", "series_search", "offset", "page_size"})
        arguments["table_id"] = normalize_singstat_table_id(arguments["table_id"])
        _validate_optional_series_search(arguments["series_search"])
        _require_int(arguments["offset"], "offset", 0, 1000000)
        _require_int(arguments["page_size"], "page_size", 1, 150)

    elif tool_name == "compare_singstat_series":
        _require_exact_keys(
            arguments,
            {
                "table_id",
                "series_a_search",
                "series_b_search",
                "start_year",
                "end_year",
                "make_chart",
            },
        )
        arguments["table_id"] = normalize_singstat_table_id(arguments["table_id"])
        for key in ("series_a_search", "series_b_search"):
            _validate_optional_series_search(arguments[key])
            if not isinstance(arguments[key], str) or not arguments[key].strip():
                raise ValueError(f"{key} must be a nonempty series phrase.")
        if arguments["series_a_search"].casefold() == arguments["series_b_search"].casefold():
            raise ValueError("A comparison requires two different series.")
        _require_int(arguments["start_year"], "start_year", 1800, 2200, allow_none=True)
        _require_int(arguments["end_year"], "end_year", 1800, 2200, allow_none=True)
        _require_bool(arguments["make_chart"], "make_chart")
        if (
            arguments["start_year"] is not None
            and arguments["end_year"] is not None
            and arguments["start_year"] > arguments["end_year"]
        ):
            raise ValueError("start_year cannot be later than end_year.")

    elif tool_name == "query_singstat_table":
        _require_exact_keys(
            arguments,
            {"table_id", "series_search", "start_year", "end_year", "max_series", "make_chart"},
        )
        arguments["table_id"] = normalize_singstat_table_id(arguments["table_id"])
        _validate_optional_series_search(arguments["series_search"])
        _require_int(arguments["start_year"], "start_year", 1800, 2200, allow_none=True)
        _require_int(arguments["end_year"], "end_year", 1800, 2200, allow_none=True)
        _require_int(arguments["max_series"], "max_series", 1, MAX_GENERIC_SERIES)
        _require_bool(arguments["make_chart"], "make_chart")
        if (
            arguments["start_year"] is not None
            and arguments["end_year"] is not None
            and arguments["start_year"] > arguments["end_year"]
        ):
            raise ValueError("start_year cannot be later than end_year.")

    return arguments
