"""UI-independent discovery decision for Sage questions.

This module does not fetch observations, assert statistical correctness, or
create official source IDs. The existing discovery and source-compatibility
logic remain authoritative. UI session state belongs to app.py.

Used by: ui/question_form.py.
Tested in: tests/test_ui_helpers.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.discovery_service import assisted_discovery_plan
from utils.kb_discovery_bridge import retrieve_candidates


@dataclass(frozen=True)
class DiscoveryOutcome:
    """Decision returned to the UI; source_hint is unverified metadata only."""

    plan: dict[str, Any] | None = None
    source_hint: str | None = None
    requires_selection: bool = False


def plan_question_discovery(
    question: str,
    index_path: str | Path,
    *,
    client: Any = None,
    model: str = 'gpt-5.6-luna',
    unsafe: bool = False,
) -> DiscoveryOutcome:
    """Preserve Sage's discovery routing without touching Streamlit state.

    Unsafe inputs are passed to the existing downstream security handler.
    A direct source hint does not certify a series, year, or observation.
    """
    if unsafe:
        return DiscoveryOutcome()
    plan = assisted_discovery_plan(question, index_path, client=client, model=model)
    # KB is a discovery supplement only. It must never select a source ID,
    # override a safety decision, or claim a verified observation.
    try:
        root = Path(index_path).resolve().parent.parent
        kb_candidates = retrieve_candidates(question, root, limit=12)
    except (OSError, ValueError, ImportError):
        kb_candidates = []
    if isinstance(plan, dict):
        plan = {**plan, 'kb_candidates': kb_candidates, 'kb_evidence_status': 'metadata_only'}
    # Treat malformed or missing discovery plans as unavailable metadata.
    # Never manufacture a direct source hint from an invalid response.
    if not isinstance(plan, dict):
        return DiscoveryOutcome()
    # Several distinct measures fit: the USER chooses. There is no topic-specific
    # shortcut that picks one for them (the same rule holds for every subject).
    if plan.get('action') in {'choose', 'review'}:
        return DiscoveryOutcome(plan=plan, requires_selection=True)
    choices = plan.get('choices')
    if plan.get('action') == 'direct' and isinstance(choices, list) and choices:
        first = choices[0]
        table_id = first.get('table_id') if isinstance(first, dict) else None
        if isinstance(table_id, str) and table_id.strip():
            return DiscoveryOutcome(plan=plan, source_hint=table_id.strip())
    return DiscoveryOutcome(plan=plan)
