"""Ordering official period labels ("2023", "2023 Q1", "Jan 2024", "2017/18" ...).

SingStat does not promise to return observations in time order, and period
labels come in many styles. This module is the ONE place that knows how to
put them in order; every chart, summary and table slice uses it.

    period_sort_key(label)          sort key for one label (never raises)
    sort_period_dataframe(frame)    rows of a DataFrame in period order
    year_from_period(label)         the four-digit year in a label, or None

Used by: utils/cube_slice.py, utils/table_tools.py.
Tested in: tests/test_core_modules.py, tests/test_data_layer.py.
"""

import re


def year_from_period(period):
    """Extract a four-digit year from annual, quarterly or monthly period labels."""
    match = re.search(r"(?<!\d)(?:18|19|20|21)\d{2}(?!\d)", str(period or ""))
    return int(match.group(0)) if match else None


MONTH_NUMBER = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def period_sort_key(period) -> tuple:
    """Return a natural ascending sort key for common SingStat period labels.

    The API is not assumed to return observations in chronological order. This
    helper recognises annual, half-year, quarterly, monthly, date-like and
    survey-period labels before falling back to numeric/text natural sorting.
    """
    text = str(period or "").strip()
    lower = text.lower()

    year = year_from_period(text)
    if year is not None:
        # Month names: Jan 2026, January 2026, 2026 Sep, etc.
        for name, number in MONTH_NUMBER.items():
            if re.search(rf"(?i)(?<![a-z]){re.escape(name)}(?![a-z])", lower):
                return (0, year, 3, number, 0, lower)

        # Explicit M/month notation: 2026M01, M12 2026, 2026 Month 3.
        month_match = re.search(
            r"(?i)m(?:onth)?\s*0?(1[0-2]|[1-9])(?!\d)",
            text,
        )
        if month_match:
            return (0, year, 3, int(month_match.group(1)), 0, lower)

        # Quarterly labels: 2026 Q1, Q1 2026, 1Q2026, 2026Q1.
        quarter_match = re.search(r"(?i)(?:q\s*([1-4])|([1-4])\s*q)", text)
        if quarter_match:
            quarter = int(next(group for group in quarter_match.groups() if group is not None))
            return (0, year, 2, quarter, 0, lower)

        # Half-year labels: 2026 H1, H2 2026, 1H2026.
        half_match = re.search(r"(?i)(?:h\s*([1-2])|([1-2])\s*h)", text)
        if half_match:
            half = int(next(group for group in half_match.groups() if group is not None))
            return (0, year, 1, half, 0, lower)

        # Survey/financial periods such as 2017/18. Treat a two-digit suffix as
        # a survey year only when it is the immediately following calendar year;
        # otherwise labels such as 2026/01 can still be interpreted as a month.
        survey_match = re.search(
            r"(?<!\d)((?:18|19|20|21)\d{2})\s*[/\-]\s*(\d{2,4})(?!\d)",
            text,
        )
        if survey_match:
            start_year = int(survey_match.group(1))
            end_token = survey_match.group(2)
            if len(end_token) == 4:
                end_year = int(end_token)
            else:
                end_year = (start_year // 100) * 100 + int(end_token)
                if end_year < start_year:
                    end_year += 100
            if end_year == start_year + 1:
                return (0, start_year, 4, end_year, 0, lower)

        # Numeric month/date labels: 2026-01 or 2026/01.
        numeric_month_match = re.search(
            r"(?<!\d)((?:18|19|20|21)\d{2})[-/](0?[1-9]|1[0-2])(?!\d)",
            text,
        )
        if numeric_month_match:
            return (
                0,
                int(numeric_month_match.group(1)),
                3,
                int(numeric_month_match.group(2)),
                0,
                lower,
            )

        # Plain annual labels or other year-bearing labels.
        return (0, year, 0, 0, 0, lower)

    # Numeric categories/periods sort numerically rather than lexicographically.
    numbers = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if numbers:
        numeric = [float(number) for number in numbers[:4]]
        numeric.extend([float("inf")] * (4 - len(numeric)))
        return (1, numeric[0], numeric[1], numeric[2], numeric[3], lower)

    # Final fallback is case-insensitive text order.
    return (2, 0, 0, 0, 0, lower)


def sort_period_dataframe(dataframe):
    """Return a stable copy ordered from the smallest/earliest period upward."""
    if dataframe.empty or "period" not in dataframe.columns:
        return dataframe.copy()

    result = dataframe.copy()
    result["period_sort_key"] = result["period"].map(period_sort_key)
    sort_columns = ["period_sort_key"]
    if "series_name" in result.columns:
        sort_columns = ["series_name", "period_sort_key"]
    result = result.sort_values(sort_columns, kind="stable").drop(columns=["period_sort_key"])
    return result.reset_index(drop=True)
