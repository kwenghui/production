"""Single public entry point for Sage catalogue discovery.

The existing AI and deterministic implementations remain intact. This facade
lets callers depend on one interface while subsequent refactors are tested.
Discovery identifies candidate metadata; it never verifies observations.

Used by: ui/question_form.py, utils/question_pipeline.py.
"""

from __future__ import annotations

from utils.ai_discovery import assisted_discovery_plan
from utils.discovery_controller import (
    discovery_plan,
    validate_selection,
    requested_frequency,
    requested_measure,
)
from utils.query_text import requested_unit_dimension

__all__ = [
    "assisted_discovery_plan",
    "discovery_plan",
    "validate_selection",
    "requested_frequency",
    "requested_measure",
    "requested_unit_dimension",
]
