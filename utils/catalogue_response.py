"""Metadata-only catalogue response, independent of statistical observations.

The caller must complete input security and intent checks before using this helper.

Used by: utils/core_secure.py.
Tested in: tests/test_ui_helpers.py.
"""

from typing import Callable, Optional


def maybe_catalogue_response(
    question: str,
    request_summary: str,
    intent_decision: dict,
    *,
    is_catalogue_request: Callable,
    list_tables: Callable,
) -> Optional[dict]:
    """Return a catalogue listing response, or None to continue normal discovery."""
    if not is_catalogue_request(question):
        return None
    catalogue = list_tables(question)
    if catalogue is None:
        return None
    count = catalogue['count']
    return {
        'answer': catalogue['answer'],
        'simple_answer': (
            f"Found {count} matching SingStat table titles in Sage's local index."
            if count
            else 'No matching table titles were found in the available local index.'
        ),
        'table_catalogue_listing': True,
        'metadata_only': True,
        'catalogue_match_count': count,
        'catalogue_indexed_count': catalogue['indexed_count'],
        'rag_results': [],
        'tool_results': [],
        'security_blocked': False,
        'scope_blocked': False,
        'analytics_executed': False,
        'request_summary': request_summary,
        'intent_decision': intent_decision,
        'suppress_charts': True,
    }
