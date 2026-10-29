"""Question-led, conservative chart coverage checks for Sage.

A graph is a visual answer, not a decoration. A requested two-measure
comparison cannot silently degrade to a single trace. No model output is
accepted as a source of new chart numbers.

Used by: ui/answer_pipeline.py, utils/answer_verification.py,
utils/chart_selection.py.
Tested in: tests/test_generic_rules.py.
"""

from __future__ import annotations
import re

from utils.vocabulary import COMPARISON_GROUPS, GROUP_EQUIVALENTS, GROUP_PHRASES

# Group words and phrases are vocabulary (utils/vocabulary.py); the logic is generic.
_EQUIV = GROUP_EQUIVALENTS
_GROUPS = COMPARISON_GROUPS


def _group_text(text):
    """Lower-case text with dashes unified and group phrases ("0-14" -> child) normalised."""
    value = str(text or '').lower().replace('–', '-').replace('—', '-')
    for pattern, replacement in GROUP_PHRASES:
        value = re.sub(pattern, replacement, value)
    value = re.sub(r'\b(?:aged?|age)\s+\d+\s*(?:-|to)\s*\d+\b', ' ', value)
    for a, b in _EQUIV.items():
        value = re.sub(r'\b' + re.escape(a) + r'\b', b, value)
    return value


def _age_ranges(text) -> list:
    """Recognise explicit age intervals, not standalone calendar years."""
    return [
        f"{a}-{b}"
        for a, b in re.findall(
            r"\b(?:aged?\s+)?(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b", str(text or "")
        )
    ]


def required_groups(question):
    """Only explicit, recognisable comparison pairs; unknowns fail safely upstream."""
    text = _group_text(question)
    needed = set()
    for pair in _GROUPS:
        if all(re.search(r'\b' + re.escape(term) + r'\b', text) for term in pair):
            needed.update(pair)
    ages = _age_ranges(question)
    if len(set(ages)) >= 2:
        needed.update(ages)
    return frozenset(needed)


def _covers(name, term) -> bool:
    """True when a series name covers a requested group (or age range)."""
    if re.fullmatch(r"\d{1,2}-\d{1,2}", term):
        return term in _age_ranges(name)
    text = _group_text(name)
    return bool(re.search(r'\b' + re.escape(term) + r'\b', text))


def chart_covers_requested_groups(question, spec) -> bool:
    """Inspect PLOTTED trace/category labels, never chart title or source title.

    Population-wide 'Total' traces cannot represent two different subgroups.
    A comparison may use either one trace per group or distinct bar categories.
    """
    needed = required_groups(question)
    if not needed:
        return True
    if not isinstance(spec, dict):
        return False
    series = spec.get('series') or []
    labels = [s.get('name', '') for s in series if isinstance(s, dict)]
    labels += [str(c) for c in (spec.get('categories') or [])]
    return all(any(_covers(label, group) for label in labels) for group in needed)


def choose_presentation(spec):
    """Select a visual format from validated data shape, not a prompt phrase.

    Other validated chart types (e.g. donuts) are preserved.
    No fabricated observations or units are introduced here.

    Presentation preference:
    - categorical comparisons remain bars;
    - short and medium verified time series prefer colourful bars/grouped bars;
    - long historical runs remain line trends because dense annual bars become unreadable.
    """
    if not isinstance(spec, dict):
        return spec
    kind = spec.get('kind')
    if kind not in ('trend', 'bar', 'grouped_bar'):
        return spec
    axis = spec.get('categories') or []
    lines = spec.get('series') or []
    axis_len = len(axis)
    # Bar-like comparisons should always use grouped bars when several series are present.
    if kind == 'bar' and len(lines) >= 2:
        return {**spec, 'kind': 'grouped_bar'}
    # Prefer more colourful bars for shorter verified historical sequences.
    # Keep long time series as lines because dozens of annual bars are difficult to read.
    if kind == 'trend' and not spec.get('x_axis_type') == 'linear_years':
        if len(lines) >= 2 and 2 <= axis_len <= 12:
            return {**spec, 'kind': 'grouped_bar'}
        if len(lines) == 1 and 2 <= axis_len <= 16:
            return {**spec, 'kind': 'bar'}
    return spec


def is_locked_evidence_chart(result):
    """Keep the original chart kind for strict verified/validated chart contracts."""
    return isinstance(result, dict) and str(result.get("chart_kind") or "").startswith(
        ("verified_", "validated_")
    )
