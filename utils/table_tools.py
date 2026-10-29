"""The four SingStat table tools the model may call, and how they read tables.

    inspect_singstat_table  - a table's title, frequency and series inventory
    list_singstat_series    - a page of that inventory (large tables)
    query_singstat_table    - checked observations of chosen series, with
                              summaries and at most one chart spec
    compare_singstat_series - two series side by side, aligned by period

Every tool works the same way for every table: load the COMPLETE official
table (retrieve_generic_singstat_table, via the local store), select series
by exact official ID or by name, keep numeric observations only, and build
any chart spec from those same verified rows. Multi-dimensional "cube"
tables go through _cube_query_fallback (utils/cube_slice.py).

Arguments are validated before a tool runs (utils/tool_arguments.py); the
tool loop that calls these lives in utils/core_secure.py.

Used by: utils/core_secure.py, tools/characterise_cube_fallback.py.
Tested in: tests/test_core_modules.py, tests/test_real_answers.py,
tests/test_storage_and_tools.py.
"""

import math
import re

import numpy as np
import requests

from utils.official_links import table_page_url
from utils.official_source import (
    get_local_store,
    HEADERS,
    safe_http_get,
    SAGE_STORE_ENABLED,
)
from utils.periods import period_sort_key, sort_period_dataframe, year_from_period
from utils.schemas import ChartSpec, SeriesSummary, ToolResult
from utils.singstat_generic_normalization import normalize_generic_table as _normalize_generic_table
from utils.singstat_retrieval import retrieve_singstat_table as _retrieve_singstat_table_impl
from utils.tool_arguments import (
    _require_int,
    _validate_optional_series_search,
    MAX_GENERIC_SERIES,
    normalize_singstat_table_id,
    validate_tool_arguments,
)
from utils.vocabulary import SERIES_LABEL_ALIASES
import utils.store


MAX_GENERIC_OBSERVATIONS = 40
MAX_GENERIC_SERIES_CATALOG = 120
GENERIC_TABLE_FETCH_LIMIT = 2500
BASE_API = "https://tablebuilder.singstat.gov.sg/api/table/tabledata"


def retrieve_singstat_table(table_id, url):
    """Compatibility facade for the official SingStat table retrieval path."""
    return _retrieve_singstat_table_impl(table_id, url, http_get=safe_http_get, headers=HEADERS)


def singstat_table_source_url(table_id):
    """Build the official Table Builder URL from a validated table ID."""
    table_id = normalize_singstat_table_id(table_id)
    return table_page_url(table_id)


def _coerce_singstat_value(value):
    """Convert a SingStat cell to float when numeric, otherwise return NaN."""
    if value is None:
        return np.nan
    text = str(value).strip()
    if not text or text.lower() in {"na", "n.a.", "nil", "-", "..", "..."}:
        return np.nan
    text = text.replace(",", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return np.nan


def _generic_table_payload(table_id, limit=GENERIC_TABLE_FETCH_LIMIT):
    """Fetch an official SingStat table from the fixed allow-listed API endpoint."""
    table_id = normalize_singstat_table_id(table_id)
    _require_int(limit, "limit", 1, GENERIC_TABLE_FETCH_LIMIT)
    url = f"{BASE_API}/{table_id}?limit={limit}"
    response = safe_http_get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("Data")
    if not isinstance(data, dict) or not isinstance(data.get("row"), list):
        message = str(payload.get("Message", "No valid SingStat table data returned."))[:500]
        raise ValueError(f"SingStat table {table_id} was not available: {message}")
    return data


def load_table_payload(table_id, limit=GENERIC_TABLE_FETCH_LIMIT):
    """PUBLIC: the raw official payload of one table (``Data`` section).

    Used by the table explorer for multi-dimensional "cube" tables, whose cells
    must be read with their dimension paths. Same safe, allow-listed fetch as
    every analytical tool; other modules must call this, never the private
    ``_generic_table_payload``.
    """
    return _generic_table_payload(table_id, limit=limit)


def retrieve_generic_singstat_table(
    table_id, limit=GENERIC_TABLE_FETCH_LIMIT, *, data_override=None
):
    """Retrieve through the existing safe path and normalize via the data layer."""
    table_id = normalize_singstat_table_id(table_id)
    data = (
        data_override
        if data_override is not None
        else _generic_table_payload(table_id, limit=limit)
    )
    return _normalize_generic_table(
        table_id,
        data,
        year_from_period=year_from_period,
        coerce_value=_coerce_singstat_value,
        source_url=singstat_table_source_url,
    )


def _series_match_score(series_name, search_text):
    """How well a series name matches a search phrase (higher is better; 1 when there is no phrase)."""
    if not search_text:
        return 1
    name = str(series_name).lower()
    phrase = str(search_text).strip().lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", phrase) if len(t) > 1]
    return (100 if phrase and phrase in name else 0) + sum(5 for t in tokens if t in name)


_SERIES_SELECTOR = r"series\s*(?:id|no)?\s*[:=]\s*([a-z0-9._-]{1,64})"


def _exact_series_selector(search_text):
    """Parse ONE explicit series identity ("series id:2.1"); never read a label as an ID."""
    match = re.fullmatch("(?i)" + _SERIES_SELECTOR, str(search_text or "").strip())
    return match.group(1) if match else None


def _exact_series_selectors(search_text):
    """Every series ID in a selector list such as "series id:1, series id:2", or None.

    The model often asks for several exact series in one call. Each item must
    itself be an exact selector; anything else (a label, a mixed list) is not
    a list of IDs and returns None, so it is matched as a phrase instead.
    """
    parts = [part.strip() for part in str(search_text or "").split(",")]
    if len(parts) < 2:
        return None
    ids = [_exact_series_selector(part) for part in parts]
    if any(identifier is None for identifier in ids):
        return None
    return list(dict.fromkeys(ids))  # keep order, drop repeats


def _hierarchical_series_match(catalogue, question):
    """Resolve a unique child series using its ID ancestors as semantic context.

    Only return an ID when an exact leaf word and a distinct ancestor phrase
    are both established in the question. Equal-scoring alternatives fail closed.
    Hierarchical dotted series IDs are official source identities, not row indexes.
    """
    if not isinstance(question, str) or len(question) > 500:
        return None
    words = set(re.findall(r"[a-z]+|\d+", question.casefold()))
    ignored = {
        "how",
        "has",
        "have",
        "the",
        "value",
        "values",
        "of",
        "each",
        "every",
        "month",
        "monthly",
        "year",
        "yearly",
        "changed",
        "change",
        "over",
        "time",
        "singapore",
        "in",
        "by",
        "what",
        "is",
        "are",
        "total",
        "from",
        "to",
        "and",
        "for",
    }
    meaningful = words - ignored
    if len(meaningful) < 2:
        return None
    records = [dict(row) for row in catalogue.to_dict("records")]
    index = {str(row["series_no"]): row for row in records}
    ranked = []
    for row in records:
        sid = str(row["series_no"])
        if "." not in sid:
            continue
        leaf = set(re.findall(r"[a-z]+|\d+", str(row["series_name"]).casefold()))
        if not leaf or not leaf.issubset(words):
            continue
        # A semantically meaningful leaf is required; otherwise an arbitrary
        # 'total' child can win on its parent terms.
        if not (leaf & meaningful):
            continue
        parts = sid.split(".")
        ancestors = [index.get(".".join(parts[:i])) for i in range(1, len(parts))]
        parent_words = set()
        for ancestor in ancestors:
            if ancestor:
                parent_words |= set(
                    re.findall(r"[a-z]+|\d+", str(ancestor["series_name"]).casefold())
                )
        support = (parent_words & words) - ignored - leaf
        # Require at least one independent dimension from an official parent;
        # words such as 'total' or 'oil' repeated in an ancestor are insufficient.
        if not support:
            continue
        conflict = len((leaf - words) | ((parent_words - words) & meaningful))
        score = (len(leaf & meaningful) * 10 + len(support) * 8 - conflict * 2, -len(parts))
        ranked.append((score, sid))
    ranked.sort(reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        return None
    return ranked[0][1]


def _refine_generic_query_arguments(question, arguments, retrieve=None):
    """Replace fuzzy series searches only on a uniquely verified hierarchy.

    When discovery is ambiguous or table retrieval fails, preserve original
    arguments. This is deliberately table-neutral and does not cache results.
    """
    if not isinstance(arguments, dict) or not arguments.get("series_search"):
        return arguments
    if _exact_series_selector(arguments["series_search"]) or _exact_series_selectors(
        arguments["series_search"]
    ):
        return arguments
    if not isinstance(question, str) or len(question) > 500:
        return arguments
    loader = retrieve or retrieve_generic_singstat_table
    try:
        df, metadata = loader(arguments["table_id"])
        if metadata.get("table_complete") is not True:
            return arguments
        catalogue = df[["series_no", "series_name", "unit"]].drop_duplicates(subset=["series_no"])
        sid = _hierarchical_series_match(catalogue, question)
        if sid is None:
            return arguments
        refined = dict(arguments)
        refined.update(series_search=f"series id:{sid}", max_series=1)
        validate_tool_arguments("query_singstat_table", refined)
        return refined
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        return arguments


def _select_generic_series(df, series_search=None, max_series=MAX_GENERIC_SERIES):
    """The catalogue rows (series_no, series_name, unit) a query should read.

    ``series_search`` may be one exact selector ("series id:2.1"), a list of
    them ("series id:1, series id:2"), a phrase matched against series names
    (best matches first), or None (the first series). At most ``max_series``.

    A label is NOT a unique series ID in hierarchical SingStat tables: for
    example "5 - 9 Years" appears under Total Residents, Male, Female and each
    ethnic group, so exact selectors always use the official series number.
    """
    catalog = df[["series_no", "series_name", "unit"]].drop_duplicates().copy()
    exact_id = _exact_series_selector(series_search)
    if exact_id is not None:
        return catalog[catalog["series_no"].astype(str) == exact_id].head(max_series)
    exact_ids = _exact_series_selectors(series_search)
    if exact_ids is not None:
        # Several exact IDs in one call: those rows, in the order asked for.
        chosen = catalog[catalog["series_no"].astype(str).isin(exact_ids)].copy()
        chosen["_order"] = chosen["series_no"].astype(str).map(exact_ids.index)
        return chosen.sort_values("_order", kind="stable").drop(columns="_order").head(max_series)
    named = _exact_name_list(series_search, catalog)
    if named is not None:
        # A list of exact official names, each naming ONE series: those rows, in order.
        chosen = catalog[catalog["series_no"].astype(str).isin(named)].copy()
        chosen["_order"] = chosen["series_no"].astype(str).map(named.index)
        return chosen.sort_values("_order", kind="stable").drop(columns="_order").head(max_series)
    if series_search:
        catalog["_score"] = catalog["series_name"].map(
            lambda x: _series_match_score(x, series_search)
        )
        catalog = catalog[catalog["_score"] > 0].sort_values(
            "_score", ascending=False, kind="stable"
        )
    return catalog.head(max_series).drop(columns=["_score"], errors="ignore")


def _exact_name_list(series_search, catalog):
    """Series IDs for a comma-separated list of EXACT series names, or None.

    Names may themselves contain commas ("Real Estate, Professional Services"),
    so the list is read greedily: the longest run of pieces that is a series
    name wins. Every piece must be used, and every name must belong to exactly
    one series (a label repeated in a hierarchy needs an exact ID instead).
    Anything else returns None and the phrase is matched by score as before.
    """
    text = str(series_search or "").strip()
    if "," not in text:
        return None
    by_name = {}
    for row in catalog.itertuples(index=False):
        by_name.setdefault(str(row.series_name).strip().casefold(), []).append(str(row.series_no))
    pieces = [piece.strip() for piece in text.split(",")]
    ids, start = [], 0
    while start < len(pieces):
        for end in range(len(pieces), start, -1):
            name = ", ".join(pieces[start:end]).casefold()
            if name in by_name:
                if len(by_name[name]) != 1:
                    return None
                ids.append(by_name[name][0])
                start = end
                break
        else:
            return None
    return list(dict.fromkeys(ids)) if len(ids) >= 2 else None


def list_singstat_series(
    table_id, series_search=None, offset: int = 0, page_size: int = 100
) -> dict:
    """Page through ALL distinct series from an official table, without plotting.

    Reports explicitly whether the official source table was fully verified.
    For incomplete staged data the inventory is explicitly staged-only and
    cannot support exhaustive conclusions. Pages preserve source row order
    and unique hierarchical series identifiers.
    """
    table_id = normalize_singstat_table_id(table_id)
    _validate_optional_series_search(series_search)
    _require_int(offset, "offset", 0, 1000000)
    _require_int(page_size, "page_size", 1, 150)
    complete = False
    metadata = {}
    try:
        df, metadata = retrieve_generic_singstat_table(table_id)
        complete = metadata.get("table_complete") is True
        if not all(x in df.columns for x in ("series_no", "series_name", "unit")):
            raise ValueError("Official table lacks auditable series metadata")
        catalogue = df[["series_no", "series_name", "unit"]].drop_duplicates(
            subset=["series_no"], keep="first"
        )
        records = [
            {"series_no": str(r.series_no), "series_name": str(r.series_name), "unit": str(r.unit)}
            for r in catalogue.itertuples(index=False)
        ]
    except utils.store.UnverifiablePaginationError:
        if not SAGE_STORE_ENABLED:
            raise
        staged = get_local_store().staged_catalog(table_id)
        records = [
            {
                "series_no": str(row.get("series_id") or ""),
                "series_name": str(row.get("series_name") or ""),
                "unit": str(row.get("unit") or ""),
            }
            for row in staged
        ]
        metadata = {"table_complete": False}
    # Restrict filtering to the explicit search phrase; never guess broad
    # synonyms or silently remove series from the unfiltered inventory.
    count_unfiltered = len(records)
    if series_search:
        records = [
            row for row in records if _series_match_score(row["series_name"], series_search) > 0
        ]
    total = len(records)
    sliced = records[offset : offset + page_size]
    return {
        "success": True,
        "table_id": table_id,
        "source_url": singstat_table_source_url(table_id),
        "table_complete": complete,
        "analysis_ready": complete,
        "inventory_status": "verified_complete_table"
        if complete
        else "incomplete_staged_catalogue",
        "series_total_unfiltered": count_unfiltered,
        "series_total_matching": total,
        "series_search": series_search,
        "offset": offset,
        "page_size": page_size,
        "next_offset": offset + len(sliced) if offset + len(sliced) < total else None,
        "has_more": offset + len(sliced) < total,
        "series": sliced,
        "coverage_warning": (
            None
            if complete
            else "Staged-only metadata: catalogue may omit official series. "
            "Do not infer table-wide completeness or plot these records."
        ),
    }


def inspect_singstat_table(table_id, series_search=None) -> dict:
    """Inspect official metadata and available series for a SingStat table."""
    table_id = normalize_singstat_table_id(table_id)
    _validate_optional_series_search(series_search)
    try:
        df, metadata = retrieve_generic_singstat_table(table_id)
    except utils.store.UnverifiablePaginationError:
        if not SAGE_STORE_ENABLED:
            raise
        # Make incremental downloads discoverable without describing the
        # staged catalog as a verified complete statistical table.
        available = get_local_store().staged_catalog(table_id)
        if not available:
            raise
        if series_search:
            term = _canonical_series_label(series_search)
            available = [r for r in available if term in _canonical_series_label(r["series_name"])]
        return {
            "success": bool(available),
            "table_id": table_id,
            "source_url": singstat_table_source_url(table_id),
            "table_complete": False,
            "analysis_ready": False,
            "series_catalog_truncated": len(available) > MAX_GENERIC_SERIES_CATALOG,
            "series_count_returned": min(len(available), MAX_GENERIC_SERIES_CATALOG),
            "series": [
                {"series_no": row["series_id"], "series_name": row["series_name"]}
                for row in available[:MAX_GENERIC_SERIES_CATALOG]
            ],
            "coverage_warning": (
                "These are saved official series in an INCOMPLETE SQLite "
                "staging set, not proof of the whole official table. Only "
                "a unique EXACT series can be read separately; query using "
                "max_series=1. Do not claim the latest official period."
            ),
        }
    catalog = df[["series_no", "series_name", "unit"]].drop_duplicates().copy()
    # The unfiltered catalogue size is independent of the model's search wording.
    # A zero phrase-match count must never be misrepresented as an empty table.
    series_total_unfiltered = int(catalog["series_no"].nunique())
    if series_search:
        catalog["match_score"] = catalog["series_name"].map(
            lambda x: _series_match_score(x, series_search)
        )
        catalog = catalog[catalog["match_score"] > 0].sort_values(
            "match_score", ascending=False, kind="stable"
        )
    catalogue_total = len(catalog)
    no_phrase_match = bool(series_search) and catalogue_total == 0
    if no_phrase_match:
        # Return names only for discovery; do not substitute numeric observations.
        catalog = (
            df[["series_no", "series_name", "unit"]]
            .drop_duplicates()
            .head(MAX_GENERIC_SERIES_CATALOG)
            .copy()
        )
    else:
        catalog = catalog.head(MAX_GENERIC_SERIES_CATALOG)
    years = [int(x) for x in df["year"].dropna().unique().tolist()]
    return {
        "success": True,
        **metadata,
        "series_search": series_search,
        "series_count_available": int(catalogue_total),
        "series_total_unfiltered": series_total_unfiltered,
        "series_total_matching": int(catalogue_total),
        "series_match_status": "no_phrase_match" if no_phrase_match else "matched",
        "recovery_hint": (
            "The search phrase did not match an official series label. The following "
            "records are catalogue examples, NOT selected observations. Choose an exact "
            "series_no using 'series id:<id>' or inspect the hierarchy before querying."
            if no_phrase_match
            else None
        ),
        "series_count_returned": int(len(catalog)),
        "series_catalog_truncated": (
            series_total_unfiltered > MAX_GENERIC_SERIES_CATALOG
            if no_phrase_match
            else catalogue_total > MAX_GENERIC_SERIES_CATALOG
        ),
        "next_offset": (
            None if no_phrase_match else len(catalog) if catalogue_total > len(catalog) else None
        ),
        "series": catalog.drop(columns=["match_score"], errors="ignore").to_dict("records"),
        "period_examples": sorted(
            df["period"].drop_duplicates().astype(str).tolist(),
            key=period_sort_key,
        )[:12],
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
    }


def _generic_chart_kind(df) -> tuple:
    """Classify verified observations without mistaking categories for time.

    One period + several independent series is a category comparison, NOT a
    time-series; a proper trend needs at least one series with two periods.
    Never merge different SingStat series numbers just because labels repeat.
    """
    if df is None or df.empty:
        return "none", 0
    values = df.dropna(subset=["value"])
    if values.empty:
        return "none", 0
    periods = values["period"].dropna().astype(str).str.strip()
    periods = periods[periods.ne("")].unique().tolist()
    series_id = "series_no" if "series_no" in values.columns else "series_name"
    groups = values.groupby(series_id, dropna=False)
    if len(periods) == 1:
        # Duplicate official descriptions without hierarchy context are NOT
        # reliably identifiable independent categories.
        distinct = values[[series_id, "series_name"]].drop_duplicates()
        if len(distinct) >= 2 and distinct["series_name"].nunique() == len(distinct):
            return "category_comparison", 1
        return "none", 1
    if len(periods) > 1 and any(len(set(group["period"].astype(str))) >= 2 for _, group in groups):
        return "time_series", len(periods)
    return "none", len(periods)


def _single_unit(frame):
    """The one published unit of a table (for example "Million Dollars"), or None if mixed/unknown."""
    try:
        units = {str(u).strip() for u in frame["unit"].dropna() if str(u).strip()}
    except Exception:  # noqa: BLE001 - a missing column just means "unknown"
        return None
    return units.pop() if len(units) == 1 else None


def _cube_query_fallback(
    table_id, metadata, series_search, start_year, end_year, max_series, make_chart, unit_hint=None
):
    """Verified numbers for multi-dimensional tables that the flat series reader cannot use.

    A table such as Dxxxxx (goods category x income measure x period) has one nested column
    tree, so the flat reader sees empty series IDs and either no numbers or repeated cells.
    Here ONE slice is read with the same rules as the chart (best column group, singly
    published cells only) and returned in the normal result format, so every figure is
    still checked against numeric_claims. Returns None when the table is not nested.
    """
    try:
        loaded = _nested_cube_slice(table_id, series_search, start_year, end_year, max_series)
    except (ValueError, KeyError, TypeError, OSError, requests.RequestException):
        return None
    if loaded is None:
        return None
    payload, found = loaded
    if not found.get("ok"):
        return {
            "success": False,
            **metadata,
            "error": found["error"],
            "cube_table": True,
            "available_rows": found.get("available_rows", []),
            "available_column_groups": found.get("available_groups", []),
        }
    axis = found.get("axis", "periods")
    # No time dimension: the published unit is the table's own.
    unit = found["unit"] if axis == "periods" else (unit_hint or "Value")
    periods = found["periods"]
    selected, summaries, observations = _cube_series(found, axis, periods, unit)
    if not observations:
        return None
    complete = metadata.get("table_complete") is True
    unique_values = sorted({item["value"] for item in observations})
    title = metadata.get("title") or table_id
    if axis == "periods":
        title = f"{title} — {found['group']}"
    chart_spec, chart_kind = (
        _cube_chart(found, axis, periods, unit, title) if make_chart else (None, "none")
    )
    return {
        "success": True,
        **metadata,
        "series_search": series_search,
        "start_year": start_year,
        "end_year": end_year,
        "selected_series": selected,
        "series_summary": summaries,
        "series_count_available": len(payload.get("row", [])),
        "series_count_selected": len(selected),
        "series_selection_truncated": False,
        "observation_count_returned": len(observations),
        "observations_truncated": False,
        "observations": observations,
        "numeric_claims": unique_values if complete and len(unique_values) <= 750 else [],
        "chart_kind": chart_kind,
        "chart_period_count": len(periods),
        "chart_priority": {"time_series": 45, "category_comparison": 12}.get(chart_kind, 0),
        "chart_spec": chart_spec,
        "latest_comparison_spec": None,
        "chart_source_verified": bool(complete and chart_spec is not None),
        "chart_suppressed_reason": None,
        "cube_table": True,
        "cube_slice": {"column_group": found["group"], "periods": list(periods)},
        "cube_other_column_groups": found["other_groups"],
        "cube_column_group_count": found["n_groups"],
        "cube_notice": found["note"],
    }


def _nested_cube_slice(table_id, series_search, start_year, end_year, max_series):
    """(payload, cube_query result) for a nested ("cube") table, or None for a flat one."""
    from utils.cube_slice import cube_query

    payload = _generic_table_payload(table_id)
    nested = any(
        isinstance(col, dict) and "columns" in col
        for row in payload.get("row", [])
        if isinstance(row, dict)
        for col in (row.get("columns") or [])
    )
    if not nested:
        return None
    return payload, cube_query(
        payload,
        series_search=series_search,
        start_year=start_year,
        end_year=end_year,
        max_rows=max_series,
    )


def _cube_series(found, axis, periods, unit):
    """(selected series, summaries, observations) for the rows of one cube slice.

    Over time: earliest, latest and change, like flat tables. Without a time
    axis: highest and lowest only ("change" would be meaningless).
    """
    selected, summaries, observations = [], [], []
    for number, row in enumerate(found["rows"], 1):
        series_no = f"cube:{number}"
        pairs = [(p, float(row["values"][p])) for p in periods if p in row["values"]]
        if not pairs:
            continue
        selected.append({"series_no": series_no, "series_name": row["name"], "unit": unit})
        if axis == "periods":
            (first_p, first_v), (last_p, last_v) = pairs[0], pairs[-1]
            summaries.append(
                {
                    "series_name": row["name"],
                    "series_no": series_no,
                    "unit": unit,
                    "latest_period": last_p,
                    "latest_value": round(last_v, 6),
                    "earliest_period": first_p,
                    "earliest_value": round(first_v, 6),
                    "change_latest_minus_earliest": round(last_v - first_v, 6),
                    "observation_count": len(pairs),
                }
            )
        else:
            top = max(pairs, key=lambda item: item[1])
            bottom = min(pairs, key=lambda item: item[1])
            summaries.append(
                {
                    "series_name": row["name"],
                    "series_no": series_no,
                    "unit": unit,
                    "highest_label": top[0],
                    "highest_value": round(top[1], 6),
                    "lowest_label": bottom[0],
                    "lowest_value": round(bottom[1], 6),
                    "observation_count": len(pairs),
                }
            )
        observations += [
            {
                "series_name": row["name"],
                "series_no": series_no,
                "unit": unit,
                "period": period,
                "value": round(value, 6),
            }
            for period, value in pairs
        ]
    return selected, summaries, observations


def _cube_chart(found, axis, periods, unit, title):
    """(chart spec, chart kind) for a cube slice: a trend over time, else category bars."""
    title = title[:200]
    if axis == "periods" and len(periods) >= 2:
        lines = [
            {"name": r["name"], "values": [r["values"].get(p) for p in periods]}
            for r in found["rows"]
            if len(r["values"]) >= 2
        ]
        if not lines:
            return None, "none"
        years = all(re.fullmatch(r"(?:19|20)\d{2}", p) for p in periods)
        return (
            {
                "kind": "trend",
                "title": title,
                "unit": unit,
                "categories": list(periods),
                "x_axis_type": "linear_years" if years else "category",
                "series": lines,
            },
            "time_series",
        )
    if axis != "periods" and len(periods) >= 2:
        return (
            {
                "kind": "bar",
                "title": title,
                "unit": unit,
                "categories": list(periods),
                "series": [
                    {"name": r["name"], "values": [r["values"].get(p) for p in periods]}
                    for r in found["rows"]
                ],
            },
            "category_comparison",
        )
    if len(found["rows"]) >= 2:
        first = periods[0]
        return (
            {
                "kind": "bar",
                "title": title,
                "unit": unit,
                "categories": [r["name"] for r in found["rows"]],
                "series": [
                    {
                        "name": found["group"],
                        "values": [
                            r["values"][first] if first in r["values"] else None
                            for r in found["rows"]
                        ],
                    }
                ],
            },
            "category_comparison",
        )
    return None, "none"


def query_singstat_table(
    table_id,
    series_search=None,
    start_year=None,
    end_year=None,
    max_series: int = 3,
    make_chart: bool = True,
) -> ToolResult:
    """Query official observations from any validated SingStat Table Builder table.

    The same steps run for every table:
      1. load the complete table (or one exactly identified saved series);
      2. choose the series that match ``series_search`` (all, for a small flat table);
      3. keep numeric observations in the requested year range;
      4. summarise each series (earliest, latest, change), refusing conflicting values;
      5. build at most one trend or bar chart spec from the same verified rows.

    Returns a tool-result dict. ``success`` is False, with an ``error`` the model
    can act on, when nothing verifiable was found. Multi-dimensional tables
    whose phrase names a column group are handed to _cube_query_fallback.
    """
    arguments = {
        "table_id": table_id,
        "series_search": series_search,
        "start_year": start_year,
        "end_year": end_year,
        "max_series": max_series,
        "make_chart": make_chart,
    }
    validate_tool_arguments("query_singstat_table", arguments)
    table_id = arguments["table_id"]
    df, metadata, from_staged_series = _load_table_for_query(table_id, series_search, max_series)
    if from_staged_series:
        make_chart = False  # Never auto-visualise an unproven historical range.

    def cube_fallback():
        # In a multi-dimensional table the phrase may name a COLUMN group (for
        # example an income decile) rather than a row.
        """Try the cube slicer, for a phrase that names a column group rather than a row."""
        return _cube_query_fallback(
            table_id,
            metadata,
            series_search,
            start_year,
            end_year,
            max_series,
            make_chart,
            unit_hint=_single_unit(df),
        )

    # Record the full catalogue size so the interface never mistakes the
    # selection for the whole table.
    catalogue_all = df[["series_no", "series_name", "unit"]].drop_duplicates(subset=["series_no"])
    catalogue_total = int(len(catalogue_all))
    if not series_search and make_chart and _is_small_uniform_flat_table(catalogue_all):
        max_series = max(max_series, catalogue_total)
    selected = _select_generic_series(df, series_search=series_search, max_series=max_series)
    if selected.empty:
        cube_result = cube_fallback()
        if cube_result is not None and cube_result.get("success"):
            return cube_result
        return _no_series_match_result(df, metadata, catalogue_total)

    numeric_df = _numeric_observations(df, selected, start_year, end_year)
    if numeric_df.empty:
        cube_result = cube_fallback()
        if cube_result is not None:
            return cube_result
        return {
            "success": False,
            **metadata,
            "error": "The selected series/year range contained no numeric observations.",
            "selected_series": selected.to_dict("records"),
        }
    summaries = _series_summaries(selected, numeric_df)
    if summaries is None:  # two different values for one series and period
        cube_result = cube_fallback()
        if cube_result is not None:
            return cube_result
        return {
            "success": False,
            **metadata,
            "error": "Conflicting values for the same SingStat series and period; no graph was drawn.",
        }

    observations = [
        {
            "series_name": r.series_name,
            "series_no": str(r.series_no),
            "unit": r.unit,
            "period": r.period,
            "value": round(float(r.value), 6),
        }
        for r in numeric_df.head(MAX_GENERIC_OBSERVATIONS).itertuples(index=False)
    ]
    chart_kind, chart_period_count = _generic_chart_kind(numeric_df)
    title = metadata.get("title") or f"SingStat {table_id}"
    chart_spec = None
    if make_chart and chart_kind == "time_series":
        chart_spec = _trend_chart_spec(summaries, numeric_df, title)
    elif make_chart and chart_kind == "category_comparison":
        chart_spec = _category_chart_spec(numeric_df, title)
    latest_comparison_spec = (
        _latest_comparison_spec(summaries, numeric_df, metadata.get("title") or table_id)
        if metadata.get("table_complete") is True
        else None
    )
    if chart_spec is not None and len(selected) == 1:
        _label_with_parent_series(
            chart_spec, series_search, catalogue_all, metadata.get("title") or table_id
        )
    if chart_spec is None and latest_comparison_spec is None:
        chart_kind = "none"
    return {
        "success": True,
        **metadata,
        "series_search": series_search,
        "start_year": start_year,
        "end_year": end_year,
        "selected_series": selected.to_dict("records"),
        "series_summary": summaries,
        "series_count_available": catalogue_total,
        "series_count_selected": int(len(selected)),
        "series_selection_truncated": int(len(selected)) < catalogue_total,
        "observation_count_returned": len(observations),
        "observations_truncated": len(numeric_df) > len(observations),
        "observations": observations,
        "numeric_claims": _numeric_claims(numeric_df, metadata),
        "chart_kind": chart_kind,
        "chart_period_count": chart_period_count,
        "chart_priority": (
            45 if chart_kind == "time_series" else 12 if chart_kind == "category_comparison" else 0
        ),
        "chart_spec": chart_spec,
        "latest_comparison_spec": latest_comparison_spec,
        "chart_source_verified": bool(
            metadata.get("table_complete") is True and chart_spec is not None
        ),
        "chart_suppressed_reason": (
            metadata.get("coverage_warning") if metadata.get("table_complete") is False else None
        ),
    }


_AGGREGATE_LABEL = re.compile(r"(?i)(?:total|all|overall)(?:\s+population)?")


def _load_table_for_query(table_id, series_search, max_series) -> tuple:
    """(rows, metadata, from_staged_series) for a query.

    A complete saved table always wins. If the official API keeps returning
    the same clipped page, ONE exactly identified saved series ("series id:1.2"
    or its exact name) may still be read, without pretending the whole table
    is present. Broad or fuzzy partial selection is never allowed.
    """
    staged_row = None
    if SAGE_STORE_ENABLED and max_series == 1 and series_search:
        store = get_local_store()
        if store.get_table(table_id) is None:
            identifier = re.fullmatch(
                r"(?i)series\s*(?:id|no)?\s*[:=]\s*([a-z0-9._-]{1,64})", str(series_search).strip()
            )
            staged_row = (
                store.exact_staged_series(table_id, series_id=identifier.group(1))
                if identifier
                else store.exact_staged_series(table_id, series_name=series_search)
            )
    if staged_row is None:
        df, metadata = retrieve_generic_singstat_table(table_id)
        return df, metadata, False
    row = staged_row["row"]
    if any("columns" in col for col in row.get("columns", [])):
        raise ValueError("Nested staged series require the complete table parser.")
    partial_data = {
        "id": table_id,
        "title": "Individually saved official statistical series",
        "row": [row],
        "staged_single_series": True,
    }
    df, metadata = retrieve_generic_singstat_table(table_id, data_override=partial_data)
    return df, metadata, True


def _is_small_uniform_flat_table(catalogue) -> bool:
    """True when every series can be shown at once: few, one unit, unique names, no total.

    Mixed units, duplicate names or an aggregate row would make an
    automatically widened selection misleading.
    """
    total = len(catalogue)
    return (
        2 <= total <= MAX_GENERIC_SERIES
        and catalogue["unit"].nunique() == 1
        and catalogue["series_name"].nunique() == total
        and not any(
            str(v).casefold().strip() in {"total", "all", "overall"}
            for v in catalogue["series_name"]
        )
    )


def _no_series_match_result(df, metadata, catalogue_total) -> dict:
    """Error result when no series name matched, with a sample to retry from."""
    sample = df[["series_no", "series_name", "unit"]].drop_duplicates().head(25)
    return {
        "success": False,
        **metadata,
        "error": "No official series name matched the requested phrase; no observations "
        "selected.",
        "series_match_status": "no_phrase_match",
        "series_total_unfiltered": catalogue_total,
        "series_total_matching": 0,
        "recovery_hint": (
            "Inspect the official series inventory, then retry with an "
            "exact 'series id:<id>' selector. Do not treat this "
            "phrase-match failure as an empty official table."
        ),
        "available_series_sample": sample.to_dict("records"),
    }


def _numeric_observations(df, selected, start_year, end_year):
    """Numeric rows of the selected series within the year range, in period order."""
    selected_ids = selected["series_no"].astype(str).tolist()
    rows = df[df["series_no"].astype(str).isin(selected_ids)].copy()
    if start_year is not None:
        rows = rows[rows["year"].notna() & (rows["year"] >= start_year)]
    if end_year is not None:
        rows = rows[rows["year"].notna() & (rows["year"] <= end_year)]
    return sort_period_dataframe(rows.dropna(subset=["value"]).copy())


def _series_summaries(selected, numeric_df) -> list[SeriesSummary] | None:
    """Earliest, latest and change for each selected series; None on conflicting values."""
    summaries = []
    for series in selected.itertuples(index=False):
        group = numeric_df[numeric_df["series_no"].astype(str) == str(series.series_no)]
        if group.empty:
            continue
        if (group.groupby("period")["value"].nunique() > 1).any():
            return None
        group = sort_period_dataframe(group.drop_duplicates(["series_no", "period"]))
        oldest, newest = group.iloc[0], group.iloc[-1]
        summaries.append(
            {
                "series_name": series.series_name,
                "series_no": str(newest["series_no"]),
                "unit": str(newest["unit"]),
                "latest_period": str(newest["period"]),
                "latest_value": round(float(newest["value"]), 6),
                "earliest_period": str(oldest["period"]),
                "earliest_value": round(float(oldest["value"]), 6),
                "change_latest_minus_earliest": round(float(newest["value"] - oldest["value"]), 6),
                "observation_count": int(len(group)),
            }
        )
    return summaries


def _numeric_claims(numeric_df, metadata) -> list:
    """Every value the answer may quote (complete tables only, at most 750 values).

    This ledger comes from the SAME validated selection as the summaries; the
    40-row observation preview is not the verification set.
    """
    if metadata.get("table_complete") is not True:
        return []
    unique_values = numeric_df["value"].dropna().unique()
    if len(unique_values) > 750:
        return []
    return [round(float(value), 6) for value in unique_values if math.isfinite(float(value))]


def _trend_chart_spec(summaries, numeric_df, title) -> ChartSpec | None:
    """A line chart of the summarised series (at most 120 periods each), or None.

    Series with fewer than two periods are left out; series with different
    units are never drawn on one axis.
    """
    plotted = []
    for item in summaries:
        rows = sort_period_dataframe(
            numeric_df[numeric_df["series_no"].astype(str) == str(item["series_no"])]
        ).tail(120)
        if rows["period"].nunique() >= 2:
            plotted.append((str(item["series_name"]), rows))
    axis = sorted(
        {str(period) for _, rows in plotted for period in rows["period"]}, key=period_sort_key
    )
    units = {str(rows.iloc[0]["unit"]) for _, rows in plotted}
    if not (plotted and len(units) == 1 and len(axis) >= 2):
        return None
    return {
        "kind": "trend",
        "title": title,
        "unit": next(iter(units)) or "Value",
        "categories": axis,
        "x_axis_type": (
            "linear_years" if all(re.fullmatch(r"(?:19|20)\d{2}", x) for x in axis) else "category"
        ),
        "series": [
            {
                "name": label,
                "values": [
                    dict(zip(rows["period"].astype(str), rows["value"].astype(float))).get(period)
                    for period in axis
                ],
            }
            for label, rows in plotted
        ],
    }


def _category_chart_spec(numeric_df, title) -> ChartSpec | None:
    """A bar chart of categories in the LATEST period, or None if not comparable.

    Requires 2..MAX_GENERIC_SERIES uniquely named categories, one unit, and no
    aggregate ("Total") bar beside its own parts.
    """
    latest_period = max(numeric_df["period"].astype(str).unique(), key=period_sort_key)
    rows = numeric_df[numeric_df["period"].astype(str) == latest_period].copy()
    if not (
        2 <= len(rows) <= MAX_GENERIC_SERIES
        and rows["series_name"].nunique() == len(rows)
        and rows["series_no"].astype(str).nunique() == len(rows)
        and rows["unit"].nunique() == 1
        and not any(_AGGREGATE_LABEL.fullmatch(str(s).strip()) for s in rows["series_name"])
    ):
        return None
    return {
        "kind": "bar",
        "title": title,
        "unit": str(rows.iloc[0]["unit"]) or "Value",
        "categories": rows["series_name"].astype(str).tolist(),
        "series": [{"name": "Value", "values": rows["value"].astype(float).tolist()}],
    }


def _latest_comparison_spec(summaries, numeric_df, title) -> ChartSpec | None:
    """A second candidate: the series compared in their latest COMMON period, or None.

    Lets a "latest distribution" question be answered by a bar chart instead
    of a time-series chart. The UI still checks subject and group coverage.
    """
    if len(summaries) < 2:
        return None
    common_periods, by_series = None, []
    for item in summaries:
        subset = numeric_df[numeric_df["series_no"].astype(str) == str(item["series_no"])]
        values = {str(row.period): float(row.value) for row in subset.itertuples(index=False)}
        common_periods = set(values) if common_periods is None else common_periods & set(values)
        by_series.append((str(item["series_name"]), str(item["unit"]), values))
    names = [name for name, _, _ in by_series]
    includes_total = len(names) > 1 and any(
        _AGGREGATE_LABEL.fullmatch(name.strip()) for name in names
    )
    if not (
        common_periods
        and 2 <= len(names) <= 8
        and len(set(names)) == len(names)
        and len({unit.casefold() for _, unit, _ in by_series}) == 1
        and not includes_total
    ):
        return None
    latest = max(common_periods, key=period_sort_key)
    return {
        "kind": "bar",
        "title": f"{title} · {latest}",
        "unit": by_series[0][1],
        "categories": names,
        "series": [
            {"name": "Published values", "values": [values[latest] for _, _, values in by_series]}
        ],
    }


def _label_with_parent_series(chart_spec, series_search, catalogue, title) -> None:
    """For an exact child ID ("series id:2.3"), name the line "Parent / Child" (in place).

    Repeated leaf labels (e.g. "Oil" under several parents) are otherwise
    ambiguous to users and to the chart-matching checks.
    """
    exact_id = _exact_series_selector(series_search)
    if not exact_id:
        return
    path_bits = exact_id.split(".")
    lookup = {str(r.series_no): str(r.series_name) for r in catalogue.itertuples(index=False)}
    labels = [lookup.get(".".join(path_bits[:index])) for index in range(1, len(path_bits) + 1)]
    labels = [item for item in labels if item]
    if len(labels) > 1:
        context_name = " / ".join(labels[-2:])
        chart_spec["series"][0]["name"] = context_name
        chart_spec["title"] = f"{title} — {context_name}"


def _canonical_series_label(value) -> str:
    """Normalise harmless punctuation without confusing parent and child CPI rows."""
    text = str(value or "").casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _resolve_comparison_series(catalog, phrase):
    """Require a clearly identifiable official row, never select lookalikes."""
    # The model is told to prefer exact selectors such as "series id:1.1"; honour them
    # as an exact series_no match (previously they were matched as NAMES and always
    # failed, so "compare males and females" fell back to an incomplete answer).
    selector = _exact_series_selector(phrase)
    if selector is not None:
        exact_id = catalog[catalog["series_no"].astype(str) == selector]
        return exact_id.iloc[0] if len(exact_id) == 1 else None
    requested = _canonical_series_label(phrase)
    candidates = catalog.copy()
    candidates["_normal"] = candidates["series_name"].map(_canonical_series_label)
    # Aggregates ("All Items", "Total ...") and labels with a known official
    # spelling (utils/vocabulary.SERIES_LABEL_ALIASES) must match EXACTLY, so a
    # sub-component or a similar-looking series is never substituted.
    alias = SERIES_LABEL_ALIASES.get(requested)
    target = alias or requested
    strict = alias is not None or bool(re.match(r"(?:all|total|overall)\b", target))
    exact = candidates[candidates["_normal"] == target]
    if len(exact) == 1:
        return exact.iloc[0]
    if len(exact) > 1:
        return None
    if strict:
        return None
    # For other comparisons, accept only a unique complete-token match.
    requested_tokens = set(target.split())
    matches = candidates[
        candidates["_normal"].map(
            lambda name: bool(requested_tokens) and requested_tokens.issubset(set(name.split()))
        )
    ].copy()
    if matches.empty:
        return None
    matches["_extra"] = matches["_normal"].map(
        lambda name: len(set(name.split()) - requested_tokens)
    )
    best = matches.sort_values("_extra", kind="stable")
    if len(best) > 1 and best.iloc[0]["_extra"] == best.iloc[1]["_extra"]:
        return None
    return best.iloc[0]


def compare_singstat_series(
    table_id,
    series_a_search,
    series_b_search,
    start_year=None,
    end_year=None,
    make_chart: bool = True,
) -> dict:
    """Compare two uniquely identified official rows over common periods only."""
    args = dict(
        table_id=table_id,
        series_a_search=series_a_search,
        series_b_search=series_b_search,
        start_year=start_year,
        end_year=end_year,
        make_chart=make_chart,
    )
    validate_tool_arguments("compare_singstat_series", args)
    table_id = args["table_id"]
    df, metadata = retrieve_generic_singstat_table(table_id)
    catalog = df[["series_no", "series_name", "unit"]].drop_duplicates().copy()
    left_row = _resolve_comparison_series(catalog, series_a_search)
    right_row = _resolve_comparison_series(catalog, series_b_search)
    if left_row is None or right_row is None:
        return {
            "success": False,
            **metadata,
            "error": "Both requested series could not be uniquely verified in this official table; "
            "no comparison was made.",
            "requested_series": [series_a_search, series_b_search],
            "matching_candidates": [
                row
                for row in catalog.to_dict("records")
                if any(
                    _canonical_series_label(term).split()[0]
                    in _canonical_series_label(row["series_name"])
                    for term in (series_a_search, series_b_search)
                )
            ][:20],
        }
    left_no, right_no = str(left_row["series_no"]), str(right_row["series_no"])
    if left_no == right_no:
        return {"success": False, **metadata, "error": "Both phrases matched the same series."}
    left_unit = str(left_row["unit"]).strip().casefold()
    right_unit = str(right_row["unit"]).strip().casefold()
    if left_unit != right_unit:
        return {
            "success": False,
            **metadata,
            "error": "The two official series have incompatible units, so no common-axis chart "
            "was generated.",
            "units": [str(left_row["unit"]), str(right_row["unit"])],
        }

    selected = df[df["series_no"].astype(str).isin({left_no, right_no})].copy()
    if start_year is not None:
        selected = selected[selected["year"].notna() & (selected["year"] >= start_year)]
    if end_year is not None:
        selected = selected[selected["year"].notna() & (selected["year"] <= end_year)]
    selected = selected.dropna(subset=["value"])
    # Deduplication is safe only for identical duplicate period/value pairs.
    if selected.duplicated(["series_no", "period"]).any():
        duplicated = selected[selected.duplicated(["series_no", "period"], keep=False)]
        if (duplicated.groupby(["series_no", "period"])["value"].nunique() > 1).any():
            return {
                "success": False,
                **metadata,
                "error": "Conflicting official observations were returned for the same "
                "series/period.",
            }
        selected = selected.drop_duplicates(["series_no", "period"])
    left = selected[selected["series_no"].astype(str) == left_no][["period", "value"]]
    right = selected[selected["series_no"].astype(str) == right_no][["period", "value"]]
    paired = left.merge(
        right, on="period", how="inner", validate="one_to_one", suffixes=("_a", "_b")
    )
    if paired.empty:
        return {
            "success": False,
            **metadata,
            "error": "Both series were found, but they have no identical reporting periods "
            "with numeric observations.",
        }
    paired["_sort"] = paired["period"].map(period_sort_key)
    paired = paired.sort_values("_sort", kind="stable").drop(columns="_sort")
    # The tool output lists the latest aligned periods as PAIRS, so a row limit
    # can never drop one of the two series.
    latest = paired.tail(MAX_GENERIC_OBSERVATIONS)
    output_rows = [
        {
            "period": str(row.period),
            "series_a_value": round(float(row.value_a), 6),
            "series_b_value": round(float(row.value_b), 6),
            "difference_a_minus_b": round(float(row.value_a - row.value_b), 6),
        }
        for row in latest.itertuples(index=False)
    ]
    return {
        "success": True,
        **metadata,
        "series_a": {
            "series_no": left_no,
            "series_name": str(left_row["series_name"]),
            "unit": str(left_row["unit"]),
        },
        "series_b": {
            "series_no": right_no,
            "series_name": str(right_row["series_name"]),
            "unit": str(right_row["unit"]),
        },
        "aligned_period_count": int(len(paired)),
        "earliest_common_period": str(paired.iloc[0]["period"]),
        "latest_common_period": str(paired.iloc[-1]["period"]),
        "observations": output_rows,
        "observations_truncated": len(paired) > len(output_rows),
        "chart_priority": 60,
        "chart_spec": (
            {
                "kind": "trend",
                "title": f"{left_row['series_name']} vs {right_row['series_name']}",
                "unit": str(left_row['unit']) or "Index",
                "categories": paired["period"].astype(str).tolist(),
                "series": [
                    {
                        "name": str(left_row['series_name']),
                        "values": paired["value_a"].astype(float).tolist(),
                    },
                    {
                        "name": str(right_row['series_name']),
                        "values": paired["value_b"].astype(float).tolist(),
                    },
                ],
            }
            if make_chart and len(paired) >= 2
            else None
        ),
    }
