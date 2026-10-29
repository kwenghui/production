"""Normalize untrusted user-selected source preferences before discovery.

Used by: utils/core_secure.py.
"""

from __future__ import annotations
import re

_TABLE_ID = re.compile(r"[A-Z]\d{5,8}\Z")


def normalize_source_preference(preferred_table_id, user_selected_table) -> tuple:
    """Return (validated table ID or None, explicitly_selected flag).

    A preferred table is only a discovery hint, never verified evidence.
    """
    table_id = (
        preferred_table_id
        if isinstance(preferred_table_id, str) and _TABLE_ID.fullmatch(preferred_table_id)
        else None
    )
    return table_id, bool(user_selected_table and table_id)
