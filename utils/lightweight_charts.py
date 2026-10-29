"""Accessible, bounded Vega-Lite charts from Sage's already verified values.

UI-only: no statistics are estimated, dropped, sampled or rearranged.  When a
series has many periods we show fewer *axis labels*, never fewer observations.
Hover continues to expose every period and its original value.

Used by: ui/answer_view.py, ui/components.py,
tools/characterise_lightweight_chart.py.
"""

from __future__ import annotations

import logging
import math
import json
import re
from html import unescape

import altair as alt

from utils.chart_order import (  # noqa: F401 - re-exported for existing callers
    SORT_OPTIONS,
    default_sort_mode,
    is_sortable,
    lines_by_latest,
    short_series_names,
    sort_spec,
)

MAX_RENDERED_SERIES = 40
MAX_RENDERED_POINTS = 500

# Placeholder words that carry no meaning on a chart. They are never shown as
# axis titles or as a one-line legend; real units and names still are.
_GENERIC_LABELS = {"", "value", "values", "category", "categories", "series", "series 1"}


def _meaningful(label):
    """Return the label, or None when it is only a placeholder like 'Value'."""
    text = str(label or "").strip()
    return None if text.casefold() in _GENERIC_LABELS else text


def _year_axis(categories) -> bool:
    """True for annual data, including historical series with missing years."""
    return (
        len(categories) > 1
        and all(re.fullmatch(r"(?:18|19|20)\d{2}", str(x)) for x in categories)
        and len({str(x) for x in categories}) == len(categories)
    )


def _label_positions(categories, limit: int = 9) -> list:
    """Axis ticks at a REGULAR step (every year, every 2nd year, ...) with both end periods
    labelled, so no single year goes missing in the middle (2017, 2018, 2019, 2021 ...).

    Up to 12 periods every one is labelled (the browser hides labels that would overlap
    on a narrow screen); beyond that the step is the smallest one that fits ``limit``.
    """
    count = len(categories)
    if count <= max(limit, 12):
        return [str(x) for x in categories]
    step = -(-(count - 1) // (limit - 1))  # ceiling division
    indexes = list(range(0, count, step))
    if indexes[-1] != count - 1:
        if count - 1 - indexes[-1] < step / 2:
            indexes.pop()  # too close to the last period
        indexes.append(count - 1)  # always label both ends
    return [str(categories[i]) for i in indexes]


def _label_budget(mode) -> int:
    # A single, automatic layout. Vega hides overlapping labels on small screens.
    """How many axis labels to aim for (one automatic layout; Vega hides overlaps)."""
    return 7


def full_name_legend(count: int = 1):
    """Reflow legends BELOW the plot; long names remain complete on hover."""
    return alt.Legend(
        title=None,
        orient="bottom",
        direction="horizontal",
        columns=1,
        labelLimit=480,
        labelColor="#233b32",
        titleColor="#233b32",
        labelFontSize=16,
        titleFontSize=17,
        labelFontWeight="bold",
        symbolSize=110,
        # Explicit labelColor is injected by the site theme at encoding level.
    )


def _chrome_height(series_count, title: str = ""):
    """Pixels for the title, axes and one-legend-row-per-series, so the plot area itself
    is never squeezed when Streamlit fits the whole chart into the height we give it."""
    legend = 30 * max(1, int(series_count)) + 24
    # a second title line needs room
    heading = (46 + 26 * (len(_title_lines(title)) - 1)) if title else 12
    return 140 + heading + legend  # includes the frame padding


# room either side of a year-axis line chart for the first/last value label
EDGE_PADDING_PX = 38
_TYPICAL_PLOT_PX = 600  # typical plot width, to size the gap on period (non-year) axes


def _period_padding_steps(count):
    """Gap before the first / after the last point on a period axis ("1991 1Q"), in steps.

    Vega-Lite measures this gap in steps (the distance between two points); its default
    of half a step is ~2 px for 140 quarters, so the first point sat on the y-axis and
    its value labels ran into the axis numbers. This aims at EDGE_PADDING_PX on a
    typical width, and never less than the default half step.
    """
    count = max(1, int(count))
    return max(0.5, EDGE_PADDING_PX * count / _TYPICAL_PLOT_PX)


MAX_LABELLED_SERIES = 6  # beyond this many series, value labels would only pile up


# px; far taller than any real table, still within browser canvas limits
MAX_CHART_HEIGHT = 12000


def _wrap_two_lines(text, width):
    """Split a long label into at most two balanced lines of about `width` characters.

    Text that does not fit in two lines is cut with an ellipsis (the full text stays in the
    hover tooltip). Short labels are returned unchanged.
    """
    text = " ".join(str(text).split())
    if len(text) <= width:
        return text
    limit = int(width * 2.8)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    words = text.split(" ")
    if len(words) == 1:  # no spaces: split in the middle
        middle = len(text) // 2
        return text[:middle] + "\n" + text[middle:]
    best, best_size = None, None
    for index in range(1, len(words)):
        first, second = " ".join(words[:index]), " ".join(words[index:])
        size = max(len(first), len(second))
        if best_size is None or size < best_size:
            best, best_size = (first, second), size
    return best[0] + "\n" + best[1]


TITLE_LINE_CHARS = 80  # about 800px of bold title text, which fits the usual chart width


def _title_lines(title):
    """The chart title as one line, or two balanced lines when it is long (never more than two).

    Text too long even for two lines is cut with an ellipsis; the table title in full stays on the
    page above the chart.
    """
    text = " ".join(str(title or "").split())
    if not text:
        return [""]
    lines = _wrap_two_lines(text, TITLE_LINE_CHARS).split("\n")
    # Keep the space at the line break: copied text and screen readers join the two title
    # lines directly ("Mode of Transportto School" without it).
    return [line + " " for line in lines[:-1]] + lines[-1:]


def _label_expression(categories, width):
    """Vega expression that shows wrapped text for long categories, or None if none are long.

    The data keeps the original text (sorting, hover and verification are unchanged); only the
    axis caption is wrapped.
    """
    mapping = {}
    for category in dict.fromkeys(str(c) for c in categories):
        wrapped = _wrap_two_lines(category, width)
        if wrapped != category:
            mapping[category] = wrapped
    if not mapping:
        return None
    body = ",".join(
        f"{json.dumps(key, ensure_ascii=False)}:{json.dumps(value, ensure_ascii=False)}"
        for key, value in mapping.items()
    )
    return "({" + body + "})[datum.value] || datum.value"


def _tidy_label(text):
    """Collapse repeated parts: "Total · Total · Total" -> "Total", "A / B / A" -> "A / B"."""
    out = str(text or "")
    for sep in (" · ", " / "):
        if sep in out:
            seen, parts = set(), []
            for part in out.split(sep):
                key = part.strip().casefold()
                if key and key not in seen:
                    seen.add(key)
                    parts.append(part.strip())
            out = sep.join(parts)
    return out


def _fmt_label(value, compact: bool = False):
    """Figure for a data label with thousand separators (6,040,000); the tooltip keeps the full value.

    compact=True switches to 6.04M / 1.2B only when many labels share one chart.
    """
    number = float(value)
    size = abs(number)
    if compact:
        for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
            if size >= limit:
                return f"{number / limit:,.2f}".rstrip("0").rstrip(".") + suffix
    if size >= 10000 or (size >= 1000 and number.is_integer()):
        text = f"{number:,.0f}"
    elif size >= 100:
        text = f"{number:,.1f}"
    else:
        text = f"{number:,.2f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _choose_labels(rows, series_names, plot_kind, horizontal) -> None:
    """Mark which points carry a printed value so labels stay readable, never cluttered.

    Every observation is still plotted and stays available on hover; this only decides
    which ones also get their number written on the chart.
    """
    by_series = {}
    for row in rows:
        by_series.setdefault(row["Series"], []).append(row)
    if len(by_series) > MAX_LABELLED_SERIES:
        # dozens of overlapping numbers help nobody; hover shows each value
        for row in rows:
            row["Label"], row["LabelText"] = False, ""
        return
    total, many = len(rows), len(by_series) > 1
    for items in by_series.values():
        if plot_kind == "bar":
            show_all = total <= (90 if horizontal else 30)
            picks = {id(r) for r in items} if show_all else _extremes(items, last=True)
        elif not many:
            picks = (
                {id(r) for r in items}
                if len(items) <= 24
                else _extremes(items, first=True, last=True)
            )
        else:
            picks = {id(r) for r in items} if total <= 12 else _extremes(items, last=True)
        for row in items:
            row["Label"] = id(row) in picks
    shown = [row for row in rows if row["Label"]]
    # Full numbers by default; shorten (6.04M) only when many labels would collide.
    compact = (
        not horizontal
        and len(shown) > 8
        and max((abs(row.get("Published", row["Value"])) for row in shown), default=0) >= 1e6
    )
    for row in rows:
        row["LabelText"] = (
            _fmt_label(row.get("Published", row["Value"]), compact) if row["Label"] else ""
        )


# --- Value-label placement for line charts ------------------------------------------
# Vega-Lite cannot stop text marks overlapping, so Sage plans each label's position
# itself. The browser's chart width is unknown here, so a position must be clear at
# every width from a phone to a large desktop (points spread apart as the chart widens
# while labels keep their size, so clear at one width does not imply clear at another).
# Measured on 2,000 random charts: this drops ~3% of labels and removes nearly all
# overlaps. Peak, trough, first and last values are never dropped.
_NARROW_PLOT_PX = 330
_WIDE_PLOT_PX = 640
_CHECK_WIDTHS_PX = (_NARROW_PLOT_PX, 480, _WIDE_PLOT_PX, 900)
_LABEL_GAP_PX = 11  # distance between a point and its label (matches the old dy)
_LABEL_HEIGHT_PX = 17  # 14 px bold text plus a little air
_LABEL_MARGIN_PX = 4  # minimum clear space between two labels
_LABEL_NUDGE_PX = 5  # sideways gap for labels placed left/right of their point
_LINE_BOTTOM_ROOM = 0.12  # value-axis room below a line's lowest point (fits one label)
_EDGE_ALLOWANCE_PX = 30  # labels may extend this far past the plot edge (axis padding)
# name -> (vertical side, horizontal anchor): the text's position relative to its point.
LABEL_POSITIONS = {
    "above": ("above", "center"),
    "below": ("below", "center"),
    "above_left": ("above", "left"),
    "above_right": ("above", "right"),
    "below_left": ("below", "left"),
    "below_right": ("below", "right"),
    # Last resort: one text row further out, stacked over/under a neighbouring label.
    "above_far": ("above_far", "center"),
    "below_far": ("below_far", "center"),
}
_CHAR_PX = {",": 4.0, ".": 4.0, " ": 4.0, "-": 5.5, "%": 12.5, "M": 12.0, "K": 9.0, "B": 9.0}


def _label_width(text):
    """Approximate rendered width of 14 px bold Segoe UI text, in pixels."""
    return sum(_CHAR_PX.get(ch, 8.4) for ch in str(text)) + 4.0


def _label_box(x, y, width, position) -> tuple:
    """Bounding box (left, top, right, bottom) in pixels of a value label at a position."""
    side, anchor = LABEL_POSITIONS[position]
    left = (
        x - width / 2
        if anchor == "center"
        else (x - width - _LABEL_NUDGE_PX if anchor == "left" else x + _LABEL_NUDGE_PX)
    )
    far = _LABEL_HEIGHT_PX + 2 if side.endswith("_far") else 0
    top = (
        (y - _LABEL_GAP_PX - _LABEL_HEIGHT_PX - far)
        if side.startswith("above")
        else (y + _LABEL_GAP_PX + far)
    )
    return left, top, left + width, top + _LABEL_HEIGHT_PX


def _overlap(a, b) -> bool:
    """True when two label boxes overlap (with a small margin)."""
    m = _LABEL_MARGIN_PX
    return not (a[2] + m <= b[0] or b[2] + m <= a[0] or a[3] + m <= b[1] or b[3] + m <= a[1])


def _overlap_area(a, b):
    """Area in square pixels shared by two label boxes (0 when apart)."""
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _place_line_labels(rows, categories, plot_height, *, year_axis) -> None:
    """Give every labelled line point a LabelPos that avoids overlapping other labels.

    Sets row["LabelPos"] to a key of LABEL_POSITIONS ("" when unlabelled). A label
    that cannot fit even on a wide chart is removed, unless it marks the peak,
    trough, first or last value of its series.
    """
    for row in rows:
        row["LabelPos"] = ""
    shown = [row for row in rows if row.get("Label") and row.get("LabelText")]
    if shown:
        _assign_positions(rows, shown, categories, plot_height, year_axis=year_axis)
    # One text column per position, so each label layer reads the SAME data without
    # filters (filtered layers once broke the shared axis ordering).
    for row in rows:
        for position in LABEL_POSITIONS:
            row[_position_field(position)] = row["LabelText"] if row["LabelPos"] == position else ""


def _position_field(position):
    """Row field that holds the label text for one label position."""
    return "LabelText_" + position


def _assign_positions(rows, shown, categories, plot_height, *, year_axis) -> None:
    """Place each shown value label above/below its point so labels overlap as little as possible.

    Key labels (each series' first, last, highest and lowest) are placed first
    and always keep a label; other labels take the first free position in
    their preferred order (_label_preferences) or are dropped.
    """
    placer = _LabelPlacer(rows, categories, plot_height, year_axis)
    must_keep, preferred = _label_preferences(rows)
    order = sorted(shown, key=lambda r: (id(r) not in must_keep, r.get("Order", 0)))
    placed = []
    for row in order:
        options = preferred[id(row)]
        choice = next((p for p in options if placer.free(row, p, placed)), None)
        if choice is None and id(row) in must_keep:
            # Key values always keep a label: take the least-overlapping position.
            choice = min(options, key=lambda p: placer.overlap_at_narrowest(row, p, placed))
        if choice is None:
            row["Label"], row["LabelText"] = False, ""
            continue
        row["LabelPos"] = choice
        placed.append((row, choice))


def _label_preferences(rows):
    """(IDs of key rows, {row id: label positions in order of preference}).

    Peaks prefer a label above, valleys below; the first and last labels of a
    series lean INWARD first, so they never reach into the axis numbers.
    """
    by_series = {}
    for row in rows:
        by_series.setdefault(row["Series"], []).append(row)
    must_keep, preferred = set(), {}
    for items in by_series.values():
        ordered = sorted(items, key=lambda r: r.get("Order", 0))
        must_keep.update(
            {
                id(ordered[0]),
                id(ordered[-1]),
                id(max(ordered, key=lambda r: r["Value"])),
                id(min(ordered, key=lambda r: r["Value"])),
            }
        )
        for i, row in enumerate(ordered):
            neighbours = [ordered[j]["Value"] for j in (i - 1, i + 1) if 0 <= j < len(ordered)]
            valley = bool(neighbours) and all(row["Value"] < v for v in neighbours)
            side, other = ("below", "above") if valley else ("above", "below")
            if i == len(ordered) - 1 and len(ordered) > 1:
                horizontal = ("_left", "", "_right")
            elif i == 0 and len(ordered) > 1:
                horizontal = ("_right", "", "_left")
            else:
                horizontal = ("", "_left", "_right")
            preferred[id(row)] = (
                [side + h for h in horizontal]
                + [other + h for h in horizontal]
                + [side + "_far", other + "_far"]
            )
    return must_keep, preferred


class _LabelPlacer:
    """Pixel geometry of value labels on a line chart, checked at several plot widths."""

    def __init__(self, rows, categories, plot_height, year_axis) -> None:
        """Value range (mirrors _value_scale) and the period axis of this chart."""
        values = [row["Value"] for row in rows]
        low, high = min(values), max(values)
        span = (high - low) or abs(high) or 1.0
        self.bottom, self.top = low - span * _LINE_BOTTOM_ROOM, high + span * 0.14
        self.count = max(1, len(categories))
        self.index = {str(c): i for i, c in enumerate(categories)}
        self.years = [int(str(c)) for c in categories] if year_axis else []
        self.year_axis, self.plot_height = year_axis, plot_height

    def x_at(self, row, width) -> float:
        """Horizontal pixel position of a row's point at a given plot width."""
        if self.year_axis:
            first, last = self.years[0], self.years[-1]
            share = (
                0.5
                if last == first
                else (int(row.get("Year", row["Period"])) - first) / (last - first)
            )
            return EDGE_PADDING_PX + share * (width - 2 * EDGE_PADDING_PX)
        # Point scale with the same outer gap as the chart (see _period_padding_steps).
        pad = _period_padding_steps(self.count)
        step = width / max(1e-9, (self.count - 1) + 2 * pad)
        return (pad + self.index.get(str(row["Period"]), 0)) * step

    def y_at(self, row) -> float:
        """Vertical pixel position of a row's point."""
        return self.plot_height * (self.top - row["Value"]) / ((self.top - self.bottom) or 1.0)

    def box(self, row, position, width):
        """Label box of a row at a position and plot width."""
        return _label_box(
            self.x_at(row, width), self.y_at(row), _label_width(row["LabelText"]), position
        )

    def inside(self, b, width) -> bool:
        """True when a label box stays within the plot vertically (and nearly so sideways):
        outside, it would land on the title or the x-axis labels."""
        return (
            b[0] >= -_EDGE_ALLOWANCE_PX
            and b[2] <= width + _EDGE_ALLOWANCE_PX
            and b[1] >= -4
            and b[3] <= self.plot_height
        )

    def free(self, row, position, placed) -> bool:
        """True when a label fits at this position without overlapping placed labels at any width."""
        for width in _CHECK_WIDTHS_PX:
            b = self.box(row, position, width)
            if not self.inside(b, width) or any(
                _overlap(b, self.box(o, op, width)) for o, op in placed
            ):
                return False
        return True

    def overlap_at_narrowest(self, row, position, placed) -> float:
        """Total overlap with placed labels at the narrowest checked plot width."""
        return sum(
            _overlap_area(
                self.box(row, position, _NARROW_PLOT_PX), self.box(o, op, _NARROW_PLOT_PX)
            )
            for o, op in placed
        )


def _extremes(items, first: bool = False, last: bool = False):
    """Rows worth labelling: the highest and lowest, and optionally the first and last."""
    chosen = {id(max(items, key=lambda r: r["Value"])), id(min(items, key=lambda r: r["Value"]))}
    ordered = sorted(items, key=lambda r: r.get("Order", 0))
    if first:
        chosen.add(id(ordered[0]))
    if last:
        chosen.add(id(ordered[-1]))
    return chosen


def _value_scale(rows, zero_base, top_fraction: float = 0.14):
    """Value-axis range with head-room so the printed numbers are never cut off."""
    values = [row["Value"] for row in rows]
    low, high = min(values), max(values)
    span = (high - min(low, 0.0)) if zero_base else (high - low)
    span = span or abs(high) or 1.0
    # Line charts: room under the lowest point for a value label placed below it.
    bottom = min(0.0, low - span * 0.05) if zero_base else low - span * _LINE_BOTTOM_ROOM
    if zero_base and low >= 0:
        bottom = 0.0
    return alt.Scale(domain=[bottom, high + span * top_fraction], nice=True, zero=False)


_TEXT_COLOUR = "#233b32"


def _build_chart(
    rows,
    categories,
    series_names,
    *,
    plot_kind: str = "line",
    title: str = "",
    unit: str = "Value",
    horizontal: bool = False,
    label_mode: str = "Balanced",
    all_ticks: bool = False,
):
    """Build the Altair chart for verified rows (one renderer for every chart).

    ``rows`` are dicts with Period, Series, Value (and optionally Published,
    the original value of an index-normalised series). Steps: tidy repeated
    words in names, choose which values get printed labels, then build a
    horizontal bar chart or a vertical bar/line chart, and finally add
    hover read-outs to line charts when they validate.
    """
    if not rows:
        raise ValueError("No verified chartable values")
    rows, series_names, title = _tidied_names(rows, series_names, title)
    _choose_labels(rows, series_names, plot_kind, horizontal)
    if plot_kind == "line" and not horizontal:
        plot_height = 400 if len(categories) >= 30 or len(series_names) >= 5 else 340
        _place_line_labels(rows, categories, plot_height, year_axis=_year_axis(categories))
        for row in rows:  # value shown next to the line on hover / tap
            # A point that already shows its value keeps only that label: two copies of
            # the same number side by side ("15,711 15,711") read as a glitch.
            labelled = row.get("Label") and str(row.get("LabelText") or "").strip()
            row["HoverText"] = (
                "" if labelled else _fmt_label(row.get("Published", row["Value"]), False)
            )
    dataset = alt.Data(values=rows)
    single_generic = len(series_names) == 1 and _meaningful(series_names[0]) is None
    shared = {
        "dataset": dataset,
        "categories": categories,
        "series_names": series_names,
        "title": title,
        "unit_title": _meaningful(unit),
        "single_generic": single_generic,
        # An explicit list keeps the category order identical in every layer (sorting
        # by a field breaks when a layer has another data source: alphabetical axis).
        "period_order": list(dict.fromkeys(str(c) for c in categories)),
        "color": alt.Color(
            "Series:N", legend=None if single_generic else full_name_legend(len(series_names))
        ),
        "tooltips": _tooltips(rows, single_generic, _meaningful(unit)),
    }
    if horizontal:
        mark, labels, height = _horizontal_bar_layers(rows, **shared)
        hover_layers = []
    else:
        mark, labels, hover_layers, height = _vertical_layers(
            rows,
            plot_kind=plot_kind,
            budget=_label_budget(label_mode),
            all_ticks=all_ticks,
            **shared,
        )
    return _assembled_chart(mark, labels, hover_layers, height, title)


def _tidied_names(rows, series_names, title) -> tuple:
    """Collapse repeated words ("Total · Total") unless two series would then share a name."""
    tidy = {name: _tidy_label(name) for name in series_names}
    if len(set(tidy.values())) == len(set(tidy.keys())):
        rows = [dict(row, Series=tidy.get(row["Series"], row["Series"])) for row in rows]
        series_names = [tidy[name] for name in series_names]
    # Legend: only the words that tell the lines apart ("Primary · Mathematics · Female");
    # the words every name shares are in the title. The full name stays in the tooltip.
    short = short_series_names(series_names)
    if any(short[name] != name for name in series_names):
        rows = [
            dict(row, Series=short.get(row["Series"], row["Series"]), FullName=row["Series"])
            for row in rows
        ]
        series_names = [short[name] for name in series_names]
    return rows, series_names, _tidy_label(title)


def _tooltips(rows, single_generic, unit_title):
    """Hover fields: period, series (unless there is only one unnamed series), value(s)."""
    tooltips = [alt.Tooltip("Period:N", title="Period")]
    if not single_generic:
        field = "FullName:N" if any("FullName" in row for row in rows) else "Series:N"
        tooltips.append(alt.Tooltip(field, title="Series"))
    tooltips.append(alt.Tooltip("Value:Q", title=unit_title or "Figure", format=",.4~f"))
    if any("Published" in row for row in rows):
        # Index-normalised series keep their original published values on hover.
        tooltips.append(alt.Tooltip("Published:Q", title="Published value", format=",.4~f"))
    return tooltips


def _value_axis(unit_title, scale, axis_name):
    """The numeric axis encoding shared by bar and line charts."""
    encoding = alt.X if axis_name == "x" else alt.Y
    return encoding(
        "Value:Q",
        title=unit_title,
        scale=scale,
        axis=alt.Axis(
            format=",.4~f",
            labelColor=_TEXT_COLOUR,
            titleColor=_TEXT_COLOUR,
            labelFontSize=16,
            titleFontSize=17,
        ),
    )


def _horizontal_bar_layers(
    rows,
    *,
    dataset,
    categories,
    series_names,
    title,
    unit_title,
    single_generic,
    period_order,
    color,
    tooltips,
) -> tuple:
    """(bars, value labels, height) with categories down the side, read top to bottom.

    Long category names wrap onto two lines; the chart grows to show EVERY category.
    """
    wrap_expression = _label_expression(categories, 28)
    axis = alt.Axis(
        labelLimit=360,
        labelFontSize=15 if wrap_expression else 16,
        labelPadding=9,
        labelColor=_TEXT_COLOUR,
        titleColor=_TEXT_COLOUR,
        labelLineHeight=18,
        titleFontSize=17,
        labelOverlap=False,
        title=None,
        **({"labelExpr": wrap_expression} if wrap_expression else {}),
    )
    base = alt.Chart(dataset).encode(
        y=alt.Y("Period:N", sort=period_order, axis=axis),
        x=_value_axis(unit_title, _value_scale(rows, True, 0.2), "x"),
        **({"yOffset": alt.YOffset("Series:N")} if len(series_names) > 1 else {}),
        color=color,
        tooltip=tooltips,
    )
    band = max(58 if wrap_expression else 38, 20 * len(series_names) + 14)
    height = max(320, min(MAX_CHART_HEIGHT, band * len(categories) + 100)) + _chrome_height(
        0 if single_generic else len(series_names), title
    )
    mark = base.mark_bar(size=22 if wrap_expression else 19)
    labels = base.mark_text(
        align="left",
        baseline="middle",
        dx=6,
        fontSize=14,
        fontWeight="bold",
        font="Segoe UI",
        clip=False,
    ).encode(text=alt.Text("LabelText:N"), color=alt.value("#1f3b30"))
    return mark, labels, height


def _period_axis(categories, *, plot_kind, budget, all_ticks, period_order) -> tuple:
    """(x encoding, wrapped labels?) for the time or category axis of a vertical chart.

    Line charts over plain years use a numeric axis, so irregular periods
    (1957, 1960, ... 1980) are spaced correctly; anything else is categorical.
    """
    count = len(categories)
    if _year_axis(categories) and plot_kind == "line":
        ticks = [int(x) for x in _label_positions(categories, budget)]
        return alt.X(
            "Year:Q",
            title="Year",
            # Pixel padding keeps the end points and their values off the plot edges.
            scale=alt.Scale(zero=False, nice=False, padding=EDGE_PADDING_PX),
            axis=alt.Axis(
                values=ticks,
                format="d",
                labelAngle=0,
                labelOverlap=True,
                labelSeparation=10,
                labelPadding=9,
                labelColor=_TEXT_COLOUR,
                titleColor=_TEXT_COLOUR,
                labelFontSize=16,
                titleFontSize=17,
            ),
        ), None
    ticks = [str(c) for c in categories] if all_ticks else _label_positions(categories, budget)
    long_labels = any(len(str(x)) > 8 for x in ticks)
    wrap_expression = _label_expression(ticks, 12) if long_labels else None
    return alt.X(
        "Period:N",
        title="Reporting period",
        sort=period_order,
        **(
            {"scale": alt.Scale(padding=_period_padding_steps(count))}
            if plot_kind == "line"
            else {}
        ),
        axis=alt.Axis(
            values=ticks,
            labelAngle=0 if (wrap_expression or not long_labels) else -30,
            labelLimit=170 if wrap_expression else 115,
            labelOverlap=True,
            labelSeparation=10,
            labelLineHeight=18,
            labelColor=_TEXT_COLOUR,
            titleColor=_TEXT_COLOUR,
            labelFontSize=16,
            titleFontSize=17,
            labelPadding=9,
            **({"labelExpr": wrap_expression} if wrap_expression else {}),
        ),
    ), wrap_expression


def _vertical_layers(
    rows,
    *,
    plot_kind,
    budget,
    all_ticks,
    dataset,
    categories,
    series_names,
    title,
    unit_title,
    single_generic,
    period_order,
    color,
    tooltips,
) -> tuple:
    """(mark, value labels, hover layers, height) for a vertical bar or line chart."""
    count = len(categories)
    x_axis, wrap_expression = _period_axis(
        categories,
        plot_kind=plot_kind,
        budget=budget,
        all_ticks=all_ticks,
        period_order=period_order,
    )
    base = alt.Chart(dataset).encode(
        x=x_axis,
        y=_value_axis(unit_title, _value_scale(rows, plot_kind == "bar"), "y"),
        **(
            {"xOffset": alt.XOffset("Series:N")}
            if plot_kind == "bar" and len(series_names) > 1
            else {}
        ),
        color=color,
        tooltip=tooltips,
    )
    plot_area = 400 if count >= 30 or len(series_names) >= 5 else 340
    angled = not (_year_axis(categories) and plot_kind == "line") and any(
        len(str(x)) > 8
        for x in (map(str, categories) if all_ticks else _label_positions(categories, budget))
    )
    height = (
        plot_area
        + _chrome_height(0 if single_generic else len(series_names), title)
        + (30 if wrap_expression else 45 if angled else 0)
    )
    if plot_kind == "bar":
        labels = base.mark_text(
            align="center",
            baseline="bottom",
            dy=-6,
            fontSize=14,
            fontWeight="bold",
            font="Segoe UI",
            clip=False,
        ).encode(text=alt.Text("LabelText:N"), color=alt.value("#1f3b30"))
        return base.mark_bar(), labels, [], height
    mark = base.mark_line(point=(count <= 60), strokeWidth=2.7)
    return mark, _line_label_layers(base, rows), _hover_layers(base, dataset, x_axis), height


def _assembled_chart(mark, labels, hover_layers, height, title):
    """Layer everything, with a multi-line title when needed.

    Hover read-outs are optional: if the chart with them does not validate,
    the complete chart without them is returned instead.
    """
    label_layers = labels if isinstance(labels, list) else [labels]
    lines = _title_lines(title)
    title_param = alt.TitleParams(text=lines) if len(lines) > 1 else lines[0]
    plain = alt.layer(mark, *label_layers).properties(
        width="container", height=height, title=title_param
    )
    if not hover_layers:
        return plain
    try:
        chart = alt.layer(mark, *label_layers, *hover_layers).properties(
            width="container", height=height, title=title_param
        )
        # validate now; never lose the chart over hover extras
        chart.to_dict()
        return chart
    except Exception as error:  # noqa: BLE001 - optional enhancement; the plain chart is complete
        logging.getLogger("sage_ui").info("Hover read-out skipped: %s", type(error).__name__)
        return plain


def _hover_layers(base, dataset, x_encoding):
    """Hover / tap read-out for line charts: guide line, dot and value at the nearest period.

    Layers only change OPACITY with the selection (no filters), so every layer keeps the
    same data and the explicit axis order. Without a hover the chart looks unchanged.
    """

    def visible_when(selection, shown, hidden):
        """An opacity that is ``shown`` while hovering and ``hidden`` otherwise (any Altair 5)."""
        if hasattr(alt, "when"):  # Altair 5.4+
            return alt.when(selection).then(alt.value(shown)).otherwise(alt.value(hidden))
        return alt.condition(selection, alt.value(shown), alt.value(hidden))

    hover = alt.selection_point(
        name="sage_hover", nearest=True, on="pointerover", fields=["Period"], empty=False
    )
    catcher = base.mark_point(size=500, opacity=0).add_params(hover)  # invisible; nearest year wins
    guide = (
        alt.Chart(dataset)
        .mark_rule(color="#7d9a8c", strokeWidth=1.2, strokeDash=[4, 3])
        .encode(x=x_encoding, opacity=visible_when(hover, 0.9, 0))
    )
    dot = base.mark_point(filled=True, size=90).encode(opacity=visible_when(hover, 1, 0))
    value = base.mark_text(
        align="left", dx=9, dy=-11, fontSize=14, fontWeight="bold", font="Segoe UI"
    ).encode(
        text=alt.Text("HoverText:N"), color=alt.value("#1f3b30"), opacity=visible_when(hover, 1, 0)
    )
    return [guide, catcher, dot, value]


def _label_dy(side):
    """Vertical pixel offset of a label drawn above or below its point."""
    far = _LABEL_HEIGHT_PX + 2 if side.endswith("_far") else 0
    return -(_LABEL_GAP_PX + far) if side.startswith("above") else (_LABEL_GAP_PX + far)


def _line_label_layers(base, rows):
    """One text layer per label position chosen by _place_line_labels (a list)."""
    align_for = {
        "center": "center",
        "left": "right",
        "right": "left",
    }  # text left of point = right-aligned
    layers = []
    for position in LABEL_POSITIONS:
        if not any(row.get("LabelPos") == position for row in rows):
            continue
        side, anchor = LABEL_POSITIONS[position]
        layers.append(
            base.mark_text(
                align=align_for[anchor],
                baseline="bottom" if side.startswith("above") else "top",
                dx={"left": -_LABEL_NUDGE_PX, "right": _LABEL_NUDGE_PX}.get(anchor, 0),
                dy=_label_dy(side),
                fontSize=14,
                fontWeight="bold",
                font="Segoe UI",
                clip=False,
            ).encode(text=alt.Text(_position_field(position) + ":N"), color=alt.value("#1f3b30"))
        )
    return layers


def to_altair_chart(spec, *, label_mode: str = "Balanced", all_ticks: bool = False):
    """Translate one verified Sage chart specification without losing values."""
    if not isinstance(spec, dict):
        raise ValueError("Invalid chart spec")
    categories, series = spec.get("categories"), spec.get("series")
    if not isinstance(categories, list) or not isinstance(series, list):
        raise ValueError("Chart categories and series are required")
    if not 1 <= len(categories) <= MAX_RENDERED_POINTS:
        raise ValueError("Unsupported category count")
    if not 1 <= len(series) <= MAX_RENDERED_SERIES:
        raise ValueError("Select at most 40 series for readable charts")
    rows = []
    for item in series:
        values = item.get("values")
        if not isinstance(values, list) or len(values) != len(categories):
            raise ValueError("Series length does not match categories")
        for idx, (period, value) in enumerate(zip(categories, values)):
            if value is None:
                continue
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError("Unverified value")
            entry = {
                "Period": str(period),
                "Order": idx,
                "Series": str(item.get("name") or "Value"),
                "Value": float(value),
            }
            if _year_axis(categories):
                entry["Year"] = int(str(period))
            rows.append(entry)
    kind = spec.get("kind")
    plot_kind = "bar" if kind in ("bar", "grouped_bar") else "line"
    horizontal = (
        plot_kind == "bar"
        and kind == "bar"
        and (len(categories) >= 8 or max(map(lambda x: len(str(x)), categories)) > 18)
        and not _year_axis(categories)
    )
    return _build_chart(
        rows,
        categories,
        [str(item.get("name") or "Value") for item in series],
        plot_kind=plot_kind,
        horizontal=horizontal,
        title=spec.get("title", ""),
        unit=str(spec.get("unit") or "Value"),
        label_mode=label_mode,
        all_ticks=all_ticks,
    )


def from_plotly_figure(figure, *, label_mode: str = "Balanced"):
    """Present prevalidated Plotly traces with Sage theme and responsive axes.

    Explicitly map horizontal bars' numeric X and nominal Y; older conversion
    treated their Y labels as numbers and made entire age charts disappear.
    """
    traces = getattr(figure, "data", None)
    if not traces:
        raise ValueError("No validated figure traces")
    rows, categories, seen, series_names = [], [], set(), []
    plot_kind, horizontal = "line", False
    for index, trace in enumerate(traces[:MAX_RENDERED_SERIES]):
        trace_type = str(getattr(trace, "type", "") or "").lower()
        name = unescape(
            re.sub(
                r"<br\s*/?>",
                " ",
                str(getattr(trace, "name", "") or f"Series {index+1}"),
                flags=re.IGNORECASE,
            )
        ).strip()
        series_names.append(name)
        if trace_type == "pie":
            plot_kind = "bar"
            labels, values = getattr(trace, "labels", None), getattr(trace, "values", None)
        elif trace_type == "bar" and str(getattr(trace, "orientation", "") or "") == "h":
            plot_kind, horizontal = "bar", True
            labels, values = getattr(trace, "y", None), getattr(trace, "x", None)
        else:
            labels, values = getattr(trace, "x", None), getattr(trace, "y", None)
            if trace_type == "bar":
                plot_kind = "bar"
        if labels is None or values is None:
            continue
        published = getattr(trace, "customdata", None)
        for position, (period, value) in enumerate(zip(labels, values)):
            if position >= MAX_RENDERED_POINTS:
                break
            if value is None:
                continue
            try:
                number = float(value)
            except (ValueError, TypeError):
                continue
            if not math.isfinite(number):
                continue
            period = str(period)
            if period not in seen:
                seen.add(period)
                categories.append(period)
            row = {"Period": period, "Order": position, "Series": name, "Value": number}
            if published is not None:
                try:
                    original = float(published[position])
                    if math.isfinite(original):
                        row["Published"] = original
                except (IndexError, TypeError, ValueError):
                    pass
            rows.append(row)
    if not rows:
        raise ValueError("Figure has no valid numerical points")
    if _year_axis(categories):
        for row in rows:
            row["Year"] = int(row["Period"])
    # Long categorical comparisons must turn horizontally rather than squeezing
    # dozens of bar captions along the X axis. Annual series remain timelines.
    horizontal = horizontal or (
        plot_kind == "bar" and len(categories) >= 8 and not _year_axis(categories)
    )
    layout = getattr(figure, "layout", None)
    value_axis = getattr(layout, "xaxis" if horizontal else "yaxis", None)
    axis_title = getattr(getattr(value_axis, "title", None), "text", None)
    unit = str(axis_title or "Value")
    title_html = str(getattr(getattr(layout, "title", None), "text", None) or "")
    title = unescape(re.sub(r"<br\s*/?>", " ", title_html, flags=re.IGNORECASE))
    return _build_chart(
        rows,
        categories,
        series_names,
        plot_kind=plot_kind,
        horizontal=horizontal,
        unit=unit,
        title=title,
        label_mode=label_mode,
    )
