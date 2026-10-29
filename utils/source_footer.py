"""Format source references from evidence that supplied the answer.

This module does not verify provenance. Upstream tools must authenticate sources.

Used by: utils/core_secure.py.
"""

from __future__ import annotations


def build_source_footer(rag_results, tool_results) -> str:
    """Cite the tables that actually supplied data; omit discovery-only clutter."""
    sources = {}
    successful = [
        item for item in tool_results if isinstance(item, dict) and item.get("success") is True
    ]
    # A table that was only inspected should not appear beside a different
    # table that supplied the numerical evidence for the final answer.
    numerical = [
        item
        for item in successful
        if (item.get("observations") or item.get("series_summary") or item.get("chart_spec"))
    ]
    for result in numerical or successful:
        table_id, source_url = result.get("table_id"), result.get("source_url")
        if table_id and source_url:
            sources[f"SingStat {table_id}"] = source_url
        table_ids, source_urls = result.get("table_ids"), result.get("source_urls")
        if isinstance(table_ids, list) and isinstance(source_urls, list):
            for combined_table_id, combined_url in zip(table_ids, source_urls):
                if combined_table_id and combined_url:
                    sources[f"SingStat {combined_table_id}"] = combined_url
    # RAG background references should not be passed off as numerical sources
    # once one or more official analytical tools supplied the real observations.
    if not sources:
        for item in rag_results:
            if item.get("source_url"):
                sources[item["source_label"]] = item["source_url"]
    if not sources:
        return ""
    return "\n".join(["Sources used:"] + [f"- {label}: {url}" for label, url in sources.items()])
