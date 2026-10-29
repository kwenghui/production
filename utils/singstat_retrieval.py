"""SingStat HTTP table retrieval and tabular normalization.

The caller injects its safe HTTP client and headers; no network policy is bypassed.

Used by: utils/table_tools.py.
Tested in: tests/test_small_modules.py.
"""

import pandas as pd


def retrieve_singstat_table(table_id, url, *, http_get, headers, include_missing: bool = False):
    """Retrieve SingStat observations. Set include_missing=True to retain gaps for verification."""
    response = http_get(url, headers=headers, timeout=30)

    response.raise_for_status()

    payload = response.json()

    if "Data" not in payload:
        raise ValueError(f"No Data returned for {table_id}")

    if not isinstance(payload["Data"], dict):
        raise ValueError(f"Invalid Data structure for {table_id}")

    records = []

    for row in payload["Data"].get("row", []):
        series_no = str(row.get("seriesNo"))
        series_name = row.get("rowText")

        for column in row.get("columns", []) or []:
            records.append(
                {
                    "table_id": table_id,
                    "series_no": series_no,
                    "series_name": series_name,
                    "year": column.get("key"),
                    "period": column.get("key"),
                    "unit": row.get("uoM", row.get("unit")),
                    "value_raw": column.get("value"),
                    "value": column.get("value"),
                }
            )

    df = pd.DataFrame(records)

    if df.empty:
        raise ValueError(f"No observations returned for {table_id}")

    df["year"] = pd.to_numeric(df["year"], errors="coerce")

    df["value"] = pd.to_numeric(df["value"], errors="coerce")

    # Preserve suppressed/missing observations when explicitly requested: dropping
    # them makes incomplete historical series look like complete trends.
    if not include_missing:
        df = df.dropna(subset=["year", "value"])
        if df.empty:
            raise ValueError(f"No valid numeric observations returned for {table_id}")

    return df
