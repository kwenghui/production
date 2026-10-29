"""The "Surprise me" picker: a shuffled deck instead of independent random picks.

Independent picks repeat early (about a 50% chance of a repeat within 38 clicks among
1,000 questions) and often land on near-identical tables, which feels like looping.
A deck shows every question once, in random order, before any repeats. Each pick also
avoids the topics (first words of the table title) and categories just shown, when
another question is available.

Used by: ui/sidebar.py.
Tested in: tests/test_question_tools.py.
"""

from __future__ import annotations

import hashlib
import re
import secrets

_RNG = secrets.SystemRandom()  # OS randomness: no predictable sequence per process
RECENT_TOPICS = 6  # avoid the same table family for the next 6 picks
RECENT_CATEGORIES = 2  # and the same category for the next 2 (when "ALL")


def topic_of(title: str) -> str:
    """Table family: the first four words of its title ("resident households by monthly")."""
    return " ".join(re.findall(r"[a-z0-9]+", str(title or "").lower())[:4])


def _signature(eligible) -> str:
    """Short hash identifying a list of eligible question numbers."""
    return hashlib.sha256(",".join(map(str, eligible)).encode("utf-8")).hexdigest()[:16]


def draw(eligible, state: dict, *, topic=lambda i: "", category=lambda i: "", current=None) -> int:
    """Next question index. `state` is a plain dict kept in session_state (mutated here)."""
    eligible = list(eligible)
    if not eligible:
        raise ValueError("No questions to choose from")
    signature = _signature(eligible)
    if state.get("signature") != signature or not state.get("deck"):
        deck = list(eligible)
        _RNG.shuffle(deck)
        state["signature"], state["deck"] = signature, deck  # a new round: every question once
    deck = state["deck"]
    recent_topics = state.setdefault("recent_topics", [])
    recent_categories = state.setdefault("recent_categories", [])

    def fresh(i, check_category) -> bool:
        """True when a question is a good "surprise": not the current one, not a recent topic."""
        if i == current and len(eligible) > 1:
            return False
        if topic(i) and topic(i) in recent_topics[-RECENT_TOPICS:]:
            return False
        return not (
            check_category and category(i) and category(i) in recent_categories[-RECENT_CATEGORIES:]
        )

    # Prefer: new topic AND new category; then new topic only; then anything but the current one.
    position = None
    for check_category in (True, False):
        position = next(
            (p for p in range(len(deck) - 1, -1, -1) if fresh(deck[p], check_category)), None
        )
        if position is not None:
            break
    if position is None:
        position = next(
            (p for p in range(len(deck) - 1, -1, -1) if deck[p] != current), len(deck) - 1
        )
    index = deck.pop(position)
    recent_topics.append(topic(index))
    recent_categories.append(category(index))
    del recent_topics[:-RECENT_TOPICS], recent_categories[:-RECENT_CATEGORIES]
    return index
