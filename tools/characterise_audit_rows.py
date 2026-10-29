"""Characterise pipeline_audit.stage_rows on 3,000 random responses.

Prints a digest of all audit tables: two copies of Sage that behave the same
print the same digest.

    python tools/characterise_audit_rows.py OLD_SAGE_DIR
    python tools/characterise_audit_rows.py NEW_SAGE_DIR
"""

import hashlib
import json
import random
import sys

sys.path.insert(0, sys.argv[1])
from utils.pipeline_audit import stage_rows

R = random.Random(7)


def maybe(v, p=0.5):
    return v if R.random() < p else None


def pick(*xs):
    return R.choice(xs)


def tool():
    t = {}
    for k, v in [
        ("table_id", pick("M810001", "D10032", "C010019", "bad", None, 123)),
        ("success", pick(True, False, "yes")),
        ("chart_source_verified", pick(True, False)),
        ("series", [1]),
        ("selected_series", [1]),
        ("series_summary", [1]),
        ("series_match_status", pick("exact", None)),
        ("series_total_unfiltered", pick(5, None)),
        ("series_count_available", pick(3, None)),
        (
            "discovery_audit",
            pick({"selection_action": "Inspect", "discovered_via": "search"}, {}, "x"),
        ),
        ("cache_hit", pick(True, False)),
        ("from_cache", pick(True, False)),
        ("fetched_live", pick(True, False)),
        (
            "retrieval_api_urls",
            pick(
                ["https://tablebuilder.singstat.gov.sg/api/table/tabledata/M810001?limit=5"],
                ["http://evil"],
                [],
            ),
        ),
        (
            "retrieval_details",
            pick(
                "cache age 2 days",
                "Verified SQLite snapshot reused; cache age 1 d",
                "see https://x",
                "",
            ),
        ),
        ("retrieval_source", pick("network", "SQLite", None)),
        ("retrieval_channel", pick("api", "cache", None)),
        ("data_source", pick("SingStat API", None)),
        ("cache_status", pick("hit", "miss", None)),
    ]:
        if R.random() < 0.5:
            t[k] = v
    return t


def rag_item():
    it = {
        "retrieval_mode": pick(
            "persisted_verified_metadata",
            "curated_local_keyword",
            "chroma_x",
            "hybrid_table_ranking",
            "",
            None,
        ),
        "source_url": pick("https://tablebuilder.singstat.gov.sg/table/TS/M810001", ""),
        "text": pick("see M810011 and D10032", "none"),
    }
    if R.random() < 0.4:
        it["discovery_trace"] = {
            "sqlite": maybe({"status": "ok", "result_count": R.randint(0, 9)}),
            "chroma": maybe(
                {
                    "status": pick("ok", None),
                    "indexed_records": pick(12345, None),
                    "elapsed_ms": pick(40, None),
                    "vector_ms": pick(5, None),
                    "search_ms": pick(9, None),
                    "reason": maybe("timeout"),
                    "table_error": maybe("boom"),
                    "table_matches": R.randint(0, 5),
                }
            ),
            "ranking": maybe(
                {
                    "candidates": [
                        pick(
                            {
                                "table_id": "M1",
                                "rank": 1,
                                "sources": ["SQLite FTS5", "ChromaDB table"],
                                "matching_series": pick("2020", "Total", None),
                            },
                            "x",
                        )
                        for _ in range(R.randint(0, 3))
                    ]
                }
            ),
        }
    return it


out = []
for i in range(3000):
    resp = {}
    if R.random() < 0.7:
        resp["intent_decision"] = pick(
            {"in_scope": True, "summary": "s"},
            {"in_scope": None, "reason": "r"},
            {"in_scope": False},
            "bad",
        )
    if R.random() < 0.7:
        resp["rag_results"] = pick([rag_item() for _ in range(R.randint(0, 4))], "bad", None)
    if R.random() < 0.8:
        resp["tool_results"] = pick([tool() for _ in range(R.randint(0, 5))], "bad", None)
    rows = stage_rows(resp if R.random() < 0.97 else "not a dict")
    out.append(
        hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:12]
    )
print(hashlib.sha256("".join(out).encode()).hexdigest(), len(set(out)), "distinct outputs")
