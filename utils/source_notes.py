"""Short, factual notes taken from a SingStat table's own footnote.

A reader needs three things that SingStat only states in the footnote:
* which survey / census and year the figures come from (single-period tables
  have no year column, e.g. "General Household Survey 2025");
* that figures are rounded, so parts may not add up to totals;
* what a "-" cell means (usually "nil or negligible").

Only what the footnote says is reported; nothing is inferred. Used by
utils/core_secure.py (notes travel with each query result, so the AI sees
them) and app.py (shown under the answer as "Notes from SingStat").

Used by: utils/core_secure.py.
Tested in: tests/test_question_tools.py.
"""

from __future__ import annotations

import html
import re

from utils.vocabulary import REFERENCE_SURVEYS

# Survey names are vocabulary (utils/vocabulary.py), like every other subject word.
_SURVEYS = REFERENCE_SURVEYS
_ROUNDED = re.compile(
    r"rounded (?:off )?to the "
    r"nearest\s+([\d,]+(?:\.\d+)?|thousand|hundred|million|"
    r"one decimal place|\w+ decimal places?)",
    re.I,
)
_DASH = re.compile(
    r"[‘'\"“]?\s*[-–—]\s*[’'\"”]?\s*(?:denotes|means|refers "
    r"to|represents|indicates)\s+"
    r"(nil or negligible|nil|negligible|not applicable|not available)",
    re.I,
)


def _plain(value) -> str:
    """Plain text from HTML or a list of fragments (tags removed, whitespace collapsed)."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v)
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return " ".join(text.split())


def footnote_text(payload) -> str:
    """The table-level footnote from a SingStat payload ({"Data": {...}} or the inner dict)."""
    if not isinstance(payload, dict):
        return ""
    data = payload.get("Data") if isinstance(payload.get("Data"), dict) else payload
    return _plain(data.get("footnote") or data.get("footnotes") or "")


def publication_notes(footnote: str) -> list[str]:
    """Up to three plain sentences: reference survey/year, rounding, meaning of "-"."""
    text = _plain(footnote)
    if not text:
        return []
    notes = []
    for pattern, name in _SURVEYS:
        found = re.search(rf"\b(?:{pattern})\s*(?:\(\w+\)\s*)?((?:19|20)\d\d)\b", text)
        if found:
            notes.append(f"Reference: {name} {found.group(1)}.")
            break
    rounded = _ROUNDED.search(text)
    if rounded:
        notes.append(
            f"Figures are rounded to the nearest {rounded.group(1)}, so parts may not add "
            f"up "
            "exactly to totals."
        )
    dash = _DASH.search(text)
    if dash:
        notes.append(f"A '–' in the published table means {dash.group(1).lower()}.")
    return notes
