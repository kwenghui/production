"""Reading cells out of a multi-dimensional SingStat table ("cube").

A cube row holds nested columns, e.g. ``2023 > Total > Average ($)``. These
helpers walk that nesting and read values, the same way for every cube. They
never choose a subject, category or table (that was the job of a removed,
food-expenditure-only extractor).

  * flatten        - yield (dimension path, raw value) for every leaf cell
  * parse_number   - official cell text -> finite float, else None
  * is_total_label - "Total", "All households", "Overall" (with a footnote marker)

Used by: utils/cube_slice.py.
Tested in: tests/test_generic_rules.py.
"""

from __future__ import annotations

import math
import re


def flatten(columns, parents=()):
    """Yield (dimension path, raw value) for every leaf of nested official columns."""
    for node in columns or []:
        if not isinstance(node, dict):
            continue
        key = str(node.get("key") or "").strip()
        path = parents + ((key,) if key else ())
        children = node.get("columns")
        if isinstance(children, list) and children:
            yield from flatten(children, path)
        elif "value" in node:
            yield path, node.get("value")


def parse_number(raw):
    """Return the cell as a finite float, or None ("na", "-", booleans, blanks)."""
    if isinstance(raw, bool):
        return None
    try:
        value = float(str(raw).replace(",", "").strip())
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def is_total_label(label) -> bool:
    """True for a label meaning "everything" (Total / All households / Overall)."""
    return bool(
        re.fullmatch(r"(?i)\s*(?:total|all(?:\s+households)?|overall)(?:\s*\d+\s*/)?\s*", label)
    )
