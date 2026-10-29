"""Sage reusable Plotly chart templates.

No SingStat access, model calls, filesystem access or arbitrary code execution.
Accepts only small, validated JSON-like chart specifications from the analytical
core. One chart builder handles time trends, simple/grouped bars and
composition donuts, the same way for every table and subject.

Used by: ui/answer_pipeline.py, ui/answer_view.py,
tools/characterise_build_chart.py.
"""

from __future__ import annotations

from html import escape
import math
import re
import textwrap
from datetime import datetime

import plotly.graph_objects as go

BLUE = "#246c55"
ORANGE = "#b35a24"
TEAL = "#23649c"
PURPLE = "#7548a1"
PINK = "#a12c65"
COLORS = (BLUE, ORANGE, TEAL, PURPLE, PINK)
BG = "#FFFFFF"
PLOT_BG = "#FFFFFF"
TEXT = "#233b32"
MUTED = "#465d51"
GRID = "#e0e9e3"
CHART_TYPES = {"trend", "bar", "grouped_bar", "donut"}
MAX_POINTS = 500
MAX_SERIES = 512


def _safe_string(value, limit: int = 160):
    """A non-empty chart text of at most ``limit`` characters, HTML-escaped; ValueError otherwise."""
    if not isinstance(value, str):
        raise ValueError("Chart text must be a string.")
    value = value.strip()
    if not value or len(value) > limit:
        raise ValueError("Chart text is empty or too long.")
    # Suppress HTML/JS markup from external metadata. Plotly treats some text as HTML.
    return escape(value, quote=True)


def _validated_spec(spec) -> tuple:
    """(kind, labels, series, title, unit) from a chart spec, after strict validation.

    Raises ValueError for anything that is not a well-formed verified chart:
    an unknown kind, a bad category axis, malformed or non-finite values, or a
    layout that does not fit the data (_check_layout).
    """
    if not isinstance(spec, dict) or spec.get("kind") not in CHART_TYPES:
        raise ValueError("Unsupported chart type.")
    labels = _validated_labels(spec.get("categories"))
    series = spec.get("series")
    if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
        raise ValueError("A chart needs 1–512 validated series.")
    cleaned_series = [
        _validated_series(row, len(labels), bool(spec.get('mixed_original_units')))
        for row in series
    ]
    _check_layout(spec["kind"], labels, cleaned_series)
    return (
        spec["kind"],
        labels,
        cleaned_series,
        _safe_string(spec.get("title", "Sage chart"), 220),
        _safe_string(spec.get("unit", "Value"), 100),
    )


def _validated_labels(categories) -> list:
    """1 to MAX_POINTS distinct, cleaned category labels."""
    if not isinstance(categories, list):
        raise ValueError("Chart categories are required.")
    if not 1 <= len(categories) <= MAX_POINTS:
        raise ValueError("Unsupported number of chart categories.")
    labels = [_safe_string(str(value), 120) for value in categories]
    if len(set(labels)) != len(labels):
        raise ValueError("Duplicate category labels are ambiguous.")
    return labels


def _finite_or_missing(value) -> bool:
    """True for None or a finite int/float (bool is not a number here)."""
    return value is None or (type(value) in (int, float) and math.isfinite(value))


def _validated_series(row, n_labels, mixed_units) -> dict:
    """One series as {"name", "values"} (+ published values for index-normalised charts)."""
    if not isinstance(row, dict) or not isinstance(row.get("values"), list):
        raise ValueError("Malformed chart series.")
    values = row["values"]
    if len(values) != n_labels:
        raise ValueError("Every series must use the SAME ordered category axis.")
    if not all(_finite_or_missing(value) for value in values):
        raise ValueError("Chart values must be finite verified numbers or missing.")
    cleaned = {
        "name": _safe_string(row.get("name", "Series"), 1024),
        "values": [None if value is None else float(value) for value in values],
    }
    if mixed_units:
        actual = row.get('actual_values')
        if not isinstance(actual, list) or len(actual) != n_labels:
            raise ValueError('Mixed units require original observations for hover.')
        if not all(_finite_or_missing(value) for value in actual):
            raise ValueError('Invalid original observations.')
        cleaned['actual_values'] = list(actual)
        cleaned['actual_unit'] = _safe_string(row.get('actual_unit', ''), 120)
    return cleaned


def _check_layout(kind, labels, cleaned_series) -> None:
    """The chart kind must fit the data (one series for bars and donuts, two points per trend...)."""
    if kind in {"bar", "donut"} and len(cleaned_series) != 1:
        raise ValueError("This chart layout requires one numerical series.")
    if kind == "grouped_bar" and len(cleaned_series) < 2:
        raise ValueError("A grouped comparison requires at least two series.")
    if kind == "trend":
        if len(labels) < 2:
            raise ValueError("A trend requires at least two different periods.")
        if any(sum(v is not None for v in s["values"]) < 2 for s in cleaned_series):
            raise ValueError("Every plotted trend needs two verified observations.")
    if kind == "donut" and (
        any(v is None or v < 0 for v in cleaned_series[0]["values"])
        or sum(cleaned_series[0]["values"]) <= 0
    ):
        raise ValueError("A donut needs nonnegative components of one nonzero whole.")


def _temporal_order_key(label):
    """Recognise common official reporting-period labels without guessing dates.

    If every time label is recognisable, the chart sorts the positions and the
    corresponding series values TOGETHER. Unrecognised labels retain their
    validated input order instead of silently misinterpreting a category.
    """
    text = str(label).strip()
    annual = re.fullmatch(r"(19|20)\d{2}", text)
    if annual:
        return (int(text), 0, 0)
    # SingStat commonly labels quarters as "2023 1Q" (rather than "2023 Q1").
    # Support both conventions and quarter-first spellings without treating
    # arbitrary categories or index levels as calendar periods.
    quarter = re.fullmatch(r"(19\d{2}|20\d{2})\s*[-/]?\s*(?:[Qq]([1-4])|([1-4])[Qq])", text)
    if quarter:
        year, q_prefix, q_suffix = quarter.groups()
        return (int(year), (int(q_prefix or q_suffix) - 1) * 3 + 1, 0)
    quarter = re.fullmatch(r"(?:[Qq]([1-4])|([1-4])[Qq])\s*[-/]?\s*(19\d{2}|20\d{2})", text)
    if quarter:
        q_prefix, q_suffix, year = quarter.groups()
        return (int(year), (int(q_prefix or q_suffix) - 1) * 3 + 1, 0)
    # Official SingStat dates commonly use "2026 Jan", "2026 Feb", etc.
    official_month = re.fullmatch(r"(19\d{2}|20\d{2})\s+([A-Za-z]{3})", text)
    if official_month:
        try:
            return (
                int(official_month.group(1)),
                datetime.strptime(official_month.group(2).title(), "%b").month,
                0,
            )
        except ValueError:
            return None
    month = re.fullmatch(r"(19\d{2}|20\d{2})\s*[-/Mm]\s*(0?[1-9]|1[0-2])", text)
    if month:
        yr, mo = month.groups()
        return (int(yr), int(mo), 0)
    try:
        parsed = datetime.strptime(text, "%b %Y")
        return parsed.year, parsed.month, 0
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%B %Y")
            return parsed.year, parsed.month, 0
        except ValueError:
            return None


def _ordered_bar_indices(spec, labels, values) -> list:
    """Return value-sorted nominal categories while preserving semantic axes.

    A chart's numeric values and labels must ALWAYS move as a pair. The
    original chart spec stays unchanged for independent evidence validation.
    Age groups, years, quarters, months and ordinal levels keep their natural
    ordering unless a trusted chart spec explicitly requests a sort.
    """
    mode = spec.get("sort_by_value", "auto")
    if mode not in {"auto", "descending", "ascending", "none"}:
        raise ValueError("Unsupported bar sorting mode.")
    # Reporting periods are a semantic axis, not ranked categories. Always
    # sort them chronologically, irrespective of an upstream value-sort flag.
    # Reorder values with their corresponding labels in build_chart().
    period_keys = [_temporal_order_key(label) for label in labels]
    if all(key is not None for key in period_keys):
        return sorted(range(len(labels)), key=lambda i: period_keys[i])

    if mode == "none":
        return list(range(len(labels)))

    if mode == "auto":
        month_names = {
            "jan",
            "january",
            "feb",
            "february",
            "mar",
            "march",
            "apr",
            "april",
            "may",
            "jun",
            "june",
            "jul",
            "july",
            "aug",
            "august",
            "sep",
            "sept",
            "september",
            "oct",
            "october",
            "nov",
            "november",
            "dec",
            "december",
        }
        is_time = all(_temporal_order_key(label) is not None for label in labels)
        is_month = all(label.casefold().strip() in month_names for label in labels)
        is_quarter = all(re.fullmatch(r"Q[1-4]", label, re.I) for label in labels)

        def age_or_ordinal(label) -> bool:
            """True for an age band or ordinal label ("15-19", "65+", "1st Order"): such axes keep
            their order."""
            label = label.casefold().strip()
            # 15–19, 65+, 15 Years & Over, 1st Order etc.
            return bool(
                re.match(r"^\d{1,3}\s*(?:[–-]\s*\d{1,3}|\+)", label)
                or re.match(r"^\d{1,3}\s*(?:years?|yrs?|age)", label)
                or re.fullmatch(r"\d{1,2}(?:st|nd|rd|th)(?:\s+order)?", label)
            )

        is_ordered = all(age_or_ordinal(label) for label in labels)
        if is_month:
            canonical = {
                "jan": 1,
                "feb": 2,
                "mar": 3,
                "apr": 4,
                "may": 5,
                "jun": 6,
                "jul": 7,
                "aug": 8,
                "sep": 9,
                "oct": 10,
                "nov": 11,
                "dec": 12,
            }
            months = {
                name: num
                for abbr, num in canonical.items()
                for name in (abbr, datetime(2000, num, 1).strftime("%B").casefold())
            }
            months["sept"] = 9
            return sorted(range(len(labels)), key=lambda i: months[labels[i].casefold().strip()])
        if is_quarter:
            return sorted(range(len(labels)), key=lambda i: int(labels[i][-1]))
        if is_time or is_ordered:
            return list(range(len(labels)))
        mode = "descending"

    if mode == "descending":
        # Missing values (None) come last; stable sorting retains input order
        # for ties. `None` must not be converted into fabricated zeros.
        return sorted(
            range(len(values)),
            key=lambda i: (values[i] is None, -values[i] if values[i] is not None else 0),
        )
    return sorted(
        range(len(values)),
        key=lambda i: (values[i] is None, values[i] if values[i] is not None else 0),
    )


def build_chart(spec):
    """Build a readable, light-themed Plotly figure from a VERIFIED chart spec.

    Steps: validate the spec (_validated_spec), put categories in display
    order (_display_order), add the traces for the chart kind, then apply
    the shared layout (_apply_layout). Raises ValueError for an invalid spec;
    nothing is drawn from unchecked data.
    """
    kind, labels, series, title, unit = _validated_spec(spec)
    labels, series = _display_order(spec, kind, labels, series)
    fig = go.Figure()
    add_traces = {
        "trend": _add_trend_traces,
        "bar": _add_bar_trace,
        "grouped_bar": _add_grouped_bar_traces,
        "donut": _add_donut_trace,
    }[kind]
    height = add_traces(fig, spec, labels, series, unit)
    _apply_layout(fig, kind, series, title, height)
    return fig


_DEFAULT_HEIGHT = 460


def _reordered(labels, series, order) -> tuple:
    """Labels and every series' values (and published values) in ``order``."""
    return (
        [labels[i] for i in order],
        [
            {
                **item,
                "values": [item["values"][i] for i in order],
                **(
                    {"actual_values": [item["actual_values"][i] for i in order]}
                    if "actual_values" in item
                    else {}
                ),
            }
            for item in series
        ],
    )


def _display_order(spec, kind, labels, series):
    """Time axes run oldest to newest; ranked bars follow _ordered_bar_indices.

    Historical and grouped axes are only put in time order, never re-ranked,
    and every value stays attached to its own label.
    """
    if kind in ("trend", "grouped_bar"):
        keys = [_temporal_order_key(label) for label in labels]
        if all(key is not None for key in keys):
            return _reordered(labels, series, sorted(range(len(labels)), key=lambda i: keys[i]))
        return labels, series
    if kind == "bar":
        order = _ordered_bar_indices(spec, labels, series[0]["values"])
        return (
            [labels[i] for i in order],
            [{**series[0], "values": [series[0]["values"][i] for i in order]}],
        )
    return labels, series


def _wrapped_name(value) -> str:
    """A legend name wrapped at 54 characters (Plotly line breaks)."""
    return '<br>'.join(
        textwrap.wrap(value, width=54, break_long_words=False, break_on_hyphens=False)
    )


def _is_percent_unit(unit, *, per_cent_spelling: bool = False) -> bool:
    """True for percentage units ("%", "Percent", optionally SingStat's "Per Cent")."""
    lowered = unit.casefold()
    prefixes = ("percent", "per cent") if per_cent_spelling else ("percent",)
    return "%" in unit or lowered.startswith(prefixes)


def _add_trend_traces(fig, spec, labels, series, unit) -> int:
    """One line per series. Short single-series charts also print each value.

    Sparse historical years (1957, 1960, ... 1980) use a numeric year axis only
    when the spec opts in (x_axis_type "linear_years"), so time is not
    distorted by evenly spaced text labels.
    """
    numeric_years = spec.get("x_axis_type") == "linear_years"
    if numeric_years and not all(re.fullmatch(r"(?:19|20)\d{2}", t) for t in labels):
        raise ValueError("A linear year axis requires numeric annual labels.")
    x_positions = [int(t) for t in labels] if numeric_years else labels
    is_percent = _is_percent_unit(unit) or "rate (%)" in unit.casefold()
    compact = len(labels) <= 10 and len(series) == 1
    hover_unit = "%" if is_percent else ""
    for idx, item in enumerate(series):
        point_text = (
            [
                (f"{v:,.1f}%" if is_percent else f"{v:,.0f}") if v is not None else ""
                for v in item["values"]
            ]
            if compact
            else None
        )
        fig.add_trace(
            go.Scatter(
                x=x_positions,
                y=item["values"],
                name=_wrapped_name(item["name"]),
                mode="lines+markers+text" if compact else "lines+markers",
                text=point_text,
                textposition="top center",
                textfont={"color": TEXT, "size": 10},
                line={"width": 3.2, "color": COLORS[idx % len(COLORS)]},
                marker={"size": 7},
                connectgaps=False,
                customdata=item.get('actual_values'),
                # Indexed series (different units scaled to 100) show the published value on hover.
                hovertemplate=(
                    "%{x}<br>Index: %{y:,.2f}<br>Published: %{customdata:,.4f} "
                    + str(item.get('actual_unit', ''))
                    + "<extra>%{fullData.name}</extra>"
                )
                if spec.get('mixed_original_units')
                else "%{x}<br>%{y:,.2f}" + hover_unit + "<extra>%{fullData.name}</extra>",
            )
        )
    if numeric_years:
        span = x_positions[-1] - x_positions[0]
        tick_step = 1 if span <= 6 else 2 if span <= 12 else 5 if span <= 30 else 10
        fig.update_xaxes(
            title_text="Year",
            type="linear",
            dtick=tick_step,
            range=[x_positions[0] - 1, x_positions[-1] + 1],
        )
    else:
        fig.update_xaxes(
            title_text="Reporting period",
            type="category",
            categoryorder="array",
            categoryarray=labels,
            tickangle=-35 if len(labels) > 8 else 0,
            nticks=min(13, len(labels)),
        )
    fig.update_yaxes(title_text=unit, tickformat=",~s")
    return _DEFAULT_HEIGHT


def _add_bar_trace(fig, spec, labels, series, unit) -> int:
    """One measure across categories, in one colour, each bar labelled with its value.

    Horizontal when there are 5+ bars or long labels. Percentages are
    percentage-point values (14.6 means 14.6%) and are never rounded to 15;
    "Thousand" units keep the source's decimal place.
    """
    values = series[0]["values"]
    percent_axis = _is_percent_unit(unit, per_cent_spelling=True)
    use_decimal = 'thousand' in unit.casefold() or any(
        v is not None and not float(v).is_integer() for v in values
    )
    bar_labels = [
        ((f"{value:,.1f}" if use_decimal else f"{value:,.0f}") + ("%" if percent_axis else ""))
        if value is not None
        else ""
        for value in values
    ]
    precision = ",.1f" if use_decimal else ",.0f"
    suffix = "%" if percent_axis else ""
    if len(labels) >= 5 or max(map(len, labels)) > 18:
        fig.add_trace(
            go.Bar(
                x=values,
                y=labels,
                orientation="h",
                marker_color=BLUE,
                text=bar_labels,
                textposition="outside",
                cliponaxis=False,
                hovertemplate="%{y}<br>%{x:" + precision + "}" + suffix + "<extra></extra>",
            )
        )
        fig.update_yaxes(categoryorder="array", categoryarray=list(reversed(labels)))
        fig.update_xaxes(title_text=unit, rangemode="tozero")
        return max(420, 48 * len(labels) + 110)
    fig.add_trace(
        go.Bar(
            x=labels,
            y=values,
            marker_color=BLUE,
            text=bar_labels,
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{x}<br>%{y:" + precision + "}" + suffix + "<extra></extra>",
        )
    )
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=labels)
    fig.update_yaxes(title_text=unit, rangemode="tozero")
    return _DEFAULT_HEIGHT


def _add_grouped_bar_traces(fig, spec, labels, series, unit) -> int:
    """One coloured bar group per series, side by side for each category."""
    for idx, item in enumerate(series):
        fig.add_trace(
            go.Bar(
                x=labels,
                y=item["values"],
                name=_wrapped_name(item["name"]),
                marker_color=COLORS[idx % len(COLORS)],
                hovertemplate="%{x}<br>%{y:,.2f}<extra>%{fullData.name}</extra>",
            )
        )
    fig.update_layout(barmode="group")
    fig.update_xaxes(type="category", categoryorder="array", categoryarray=labels)
    fig.update_yaxes(title_text=unit, rangemode="tozero")
    return _DEFAULT_HEIGHT


def _add_donut_trace(fig, spec, labels, series, unit) -> int:
    """Parts of a whole, as percentages around a labelled hole."""
    fig.add_trace(
        go.Pie(
            labels=labels,
            values=series[0]["values"],
            hole=0.64,
            sort=False,
            direction="clockwise",
            marker={"colors": list(COLORS)},
            textinfo="percent",
            textfont={"size": 16},
            hovertemplate="%{label}<br>%{value:,.0f} (%{percent})<extra></extra>",
        )
    )
    fig.add_annotation(
        text="Composition", x=0.5, y=0.51, font={"size": 17, "color": TEXT}, showarrow=False
    )
    return 445


def _apply_layout(fig, kind, series, title, height) -> None:
    """Shared look: title, colours, fonts, margins and the legend.

    Line and grouped-bar legends sit below the X axis with room for several
    long, wrapped official series names, so labels are never clipped.
    """
    legend_below = kind in {"trend", "grouped_bar"} and len(series) > 0
    legend_bottom = max(120, 72 + 52 * len(series)) if legend_below else 65
    if legend_below:
        height = max(height, 340 + legend_bottom)
    fig.update_layout(
        template="plotly_white",
        title={"text": title, "x": 0.02, "font": {"size": 21}},
        paper_bgcolor=BG,
        plot_bgcolor=PLOT_BG,
        font={"color": TEXT, "family": "Segoe UI, Arial, sans-serif", "size": 15},
        autosize=True,
        height=height,
        margin={"l": 40, "r": 95 if kind == "bar" else 25, "t": 84, "b": legend_bottom},
        legend={
            "orientation": "h",
            "y": -0.12 if legend_below else -0.18,
            "yanchor": "top",
            "x": 0,
            "font": {"size": 15, "color": TEXT},
        },
        hoverlabel={"bgcolor": "#ffffff", "font_color": TEXT},
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor=GRID,
        zerolinecolor=GRID,
        tickfont={"color": MUTED, "size": 14},
        title_font={"color": TEXT},
    )
    fig.update_yaxes(
        showgrid=False, tickfont={"color": TEXT, "size": 14}, title_font={"color": TEXT}
    )


PLOTLY_CONFIG = {
    "displaylogo": False,
    "responsive": True,
    "toImageButtonOptions": {
        "format": "png",
        "filename": "Sage_chart",
        "height": 900,
        "width": 1500,
        "scale": 2,
    },
}
