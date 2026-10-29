"""Remove tool-citation markup that the OpenAI API can leave in answer text.

With the web_search tool, the model may write citation markers meant for
OpenAI's own apps, e.g. "\\ue200cite\\ue202turn0search0\\ue201": private-use
characters (shown as boxes) around "cite" and a search-result reference.
Sage shows its sources separately, so these markers are removed entirely.

Used by: ui/answer_view.py, utils/core_secure.py.
"""

from __future__ import annotations

import re

# \ue200 ... \ue201 wraps a citation / entity / navigation marker.
_WRAPPED = re.compile(r"\ue200[^\ue200\ue201]{0,400}\ue201")
# The same marker after the private-use characters were already lost.
_BARE = re.compile(
    r"\b(?:file)?cite(?:\s*turn\d+(?:search|news|fetch|view|open|image|file|product)\d+)+",
    re.IGNORECASE,
)
_LOOSE_REFS = re.compile(
    r"\bturn\d+(?:search|news|fetch|view|open|image|file|product)\d+\b", re.IGNORECASE
)
# Older bracket style: 【4†source】
_BRACKETED = re.compile(r"【[^】\n]{1,80}】")
_PRIVATE_USE = re.compile(r"[\ue000-\uf8ff]")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([.,;:!?])")
_DOUBLE_SPACE = re.compile(r"[ \t]{2,}")


def strip_tool_citations(text):
    """Return text without tool-citation markers; other text is left unchanged."""
    if not isinstance(text, str) or not text:
        return text
    if not (
        _PRIVATE_USE.search(text)
        or "cite" in text.lower()
        or "【" in text
        or re.search(r"turn\d", text)
    ):
        return text
    cleaned = _WRAPPED.sub("", text)
    cleaned = _BARE.sub("", cleaned)
    cleaned = _LOOSE_REFS.sub("", cleaned)
    cleaned = _BRACKETED.sub("", cleaned)
    cleaned = _PRIVATE_USE.sub("", cleaned)
    if cleaned == text:
        return text
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _DOUBLE_SPACE.sub(" ", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.split("\n"))
