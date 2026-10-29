"""Pure generic SingStat payload normalization; no HTTP or model calls.

Used by: utils/table_tools.py.
Tested in: tests/test_data_layer.py.
"""

import pandas as pd


def normalize_generic_table(table_id, data, *, year_from_period, coerce_value, source_url) -> tuple:
    """Flatten an official table payload into one row per (series, period) with typed values."""
    records = []
    for row in data.get("row", []):
        series_no = str(row.get("seriesNo", "")).strip()
        series_name = str(row.get("rowText", "")).strip() or f"Series {series_no}"
        unit = str(row.get("uoM", row.get("unit", "")) or "").strip()
        for column in row.get("columns", []) or []:
            period = str(column.get("key", "")).strip()
            raw_value = column.get("value")
            records.append(
                {
                    "table_id": table_id,
                    "series_no": series_no,
                    "series_name": series_name,
                    "unit": unit,
                    "period": period,
                    "year": year_from_period(period),
                    "value_raw": raw_value,
                    "value": coerce_value(raw_value),
                }
            )
    df = pd.DataFrame(records)
    if df.empty:
        raise ValueError(f"No observations returned for SingStat table {table_id}.")
    metadata = {
        "table_id": table_id,
        "title": str(data.get("title", "")).strip(),
        "theme": str(data.get("theme", "")).strip(),
        "frequency": str(data.get("frequency", "")).strip(),
        "data_source": str(data.get("datasource", data.get("dataSource", ""))).strip(),
        "source_url": source_url(table_id),
        "retrieval_source": (
            "Validated official data.gov.sg mirror stored in SQLite"
            if data.get("verified_mirror")
            else "SingStat Table Builder / SQLite"
        ),
        "mirror_url": data.get("mirror_url"),
        "table_complete": not bool(data.get("staged_single_series")),
    }
    if data.get("staged_single_series"):
        metadata["retrieval_source"] = (
            "Official SingStat row saved in SQLite staging (PARTIAL " "TABLE)"
        )
        metadata["coverage_warning"] = (
            "Exactly one retrieved official series is available. The complete "
            "table, other series and latest reporting period are not verified. "
            "No table-wide inference or automatic graph is permitted."
        )
    return df, metadata
