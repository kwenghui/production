"""RAG: a few short, curated notes given to the model with each question.

The notes are CONTEXT (definitions, caveats, table descriptions), never
evidence: every number still has to come from an official table.

    retrieve_rag_context              notes for a new question (top_k)
    enhance_rag_after_verified_tools  add descriptions of tables that returned data
    format_rag_context                the notes as labelled, untrusted text

Search: the persisted table catalogue (utils/rag_catalog.py, SQLite) when
FAST_RAG_ENABLED, else a small built-in KNOWLEDGE_BASE ranked by keywords and
embeddings (hybrid_rag_search). Query vectors are cached per process.

Used by: utils/core_secure.py.
Tested in: tests/test_discovery_and_ingest.py,
tests/test_summaries_and_notes.py.
"""

import json
import re

import numpy as np

from utils.core_config import _debug_print, APP_DIR, client, EMBEDDING_MODEL, logger
from utils.official_source import _LAZY_INIT_LOCK
from utils.token_usage import api_operation
import utils.rag_catalog
from utils.settings import env_flag


FAST_RAG_ENABLED = env_flag("SAGE_FAST_RAG", True)
# General statistical notes only. Table-specific documentation comes from the
# official metadata of whichever table is used (utils/rag_catalog.py), so every
# table is treated the same way.
KNOWLEDGE_BASE = [
    {
        "id": "KB006",
        "title": "Percentage points versus relative percentage",
        "source_label": "Sage Analytics",
        "source_url": "",
        "keywords": ["percentage point", "relative", "percentage change"],
        "text": (
            "A rate increasing from 40 percent to 50 percent rises by "
            "10 percentage points. Relative to the original rate, "
            "that is a 25 percent increase."
        ),
    },
]
KB_EMBEDDINGS = None
_QUERY_VECTOR_CACHE = {}  # text -> vector for single-text queries (same text, no second call)
_QUERY_VECTOR_CACHE_MAX = 512


def create_embeddings(
    texts,
    *,
    stage: str = "Reviewed notes search",
    purpose: str = ("Turn the question into a vector to find reviewed " "official-source notes."),
):
    """Create embedding vectors for the supplied text strings (labelled in the usage report)."""
    single = len(texts) == 1
    if single:
        cached = _QUERY_VECTOR_CACHE.get(texts[0])
        if cached is not None:
            return np.array([cached], dtype=np.float32)
    with api_operation(stage, purpose):
        response = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    vectors = np.array([item.embedding for item in response.data], dtype=np.float32)
    if single:
        if len(_QUERY_VECTOR_CACHE) >= _QUERY_VECTOR_CACHE_MAX:
            _QUERY_VECTOR_CACHE.pop(next(iter(_QUERY_VECTOR_CACHE)), None)
        _QUERY_VECTOR_CACHE[texts[0]] = vectors[0].tolist()
    return vectors


def normalize_vectors(matrix):
    """Return row-wise L2-normalised vectors for similarity calculations."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return matrix / norms


def initialize_rag() -> None:
    """Build and cache embeddings for the local RAG knowledge base."""
    global KB_EMBEDDINGS

    _debug_print("\nBuilding RAG index...")

    documents = [doc["title"] + "\n" + doc["text"] for doc in KNOWLEDGE_BASE]

    KB_EMBEDDINGS = normalize_vectors(
        create_embeddings(
            documents,
            stage="Index reviewed notes",
            purpose="One-time: turn the reviewed official-source notes into vectors.",
        )
    )

    _debug_print(f"RAG ready: {len(KNOWLEDGE_BASE)} chunks indexed.")


def tokenize(text) -> set:
    """Convert text into a set of lowercase alphanumeric tokens."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def keyword_score(query, document):
    """Calculate lexical overlap between a query and a knowledge-base document."""
    query_tokens = tokenize(query)

    doc_tokens = tokenize(
        document["title"] + " " + document["text"] + " " + " ".join(document["keywords"])
    )

    if not query_tokens:
        return 0.0

    return len(query_tokens & doc_tokens) / len(query_tokens)


def hybrid_rag_search(query, top_k: int = 4):
    """Retrieve the highest-scoring RAG documents using semantic and keyword scores."""
    global KB_EMBEDDINGS

    if KB_EMBEDDINGS is None:
        with _LAZY_INIT_LOCK:  # one embedding build, not one per concurrent user
            if KB_EMBEDDINGS is None:
                initialize_rag()

    query_vector = normalize_vectors(create_embeddings([query]))[0]

    semantic_scores = KB_EMBEDDINGS @ query_vector

    results = []

    for i, doc in enumerate(KNOWLEDGE_BASE):
        semantic = float(semantic_scores[i])
        keyword = keyword_score(query, doc)
        hybrid = semantic * 0.80 + keyword * 0.20

        results.append(
            {
                **doc,
                "semantic_score": round(semantic, 4),
                "keyword_score": round(keyword, 4),
                "hybrid_score": round(hybrid, 4),
            }
        )

    results.sort(key=lambda x: x["hybrid_score"], reverse=True)

    return results[:top_k]


_RAG_CATALOG = None


def get_rag_catalog():
    """Create a separate persistent index; never rewrite numeric SQLite snapshots."""
    global _RAG_CATALOG
    if _RAG_CATALOG is not None:
        return _RAG_CATALOG
    with _LAZY_INIT_LOCK:
        if _RAG_CATALOG is None:
            catalog = utils.rag_catalog.RagCatalog(APP_DIR)
            notes = APP_DIR / "sage_official_notes.json"
            if notes.exists():
                try:
                    imported = catalog.import_reviewed_manifest(notes)
                    logger.info("Imported %d reviewed official-source excerpts", imported)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    logger.warning("Optional RAG notes manifest rejected: %s", error)
            _RAG_CATALOG = catalog  # publish only once fully built
    return _RAG_CATALOG


def fast_rag_search(query, dataset_ids=None, top_k: int = 4):
    """Retrieve only substantively matching curated definitions and caveats."""
    from utils.query_text import specific_words

    ids = set(dataset_ids or [])
    candidates = []
    wanted = specific_words(query)
    for doc in KNOWLEDGE_BASE:
        source_label = str(doc.get("source_label", ""))
        # Any table ID named in the note's source label counts; no table is favoured.
        linked = set(re.findall(r"\b[MCD]\d{5,6}\b", source_label.upper()))
        match = bool(linked & ids)
        # Score on TOPIC words only. Words like "Singapore", "change" and "years" are in nearly
        # every
        # question and nearly every note, so counting them let an unrelated note (population ageing,
        # life expectancy) match a question about telecommunications.
        doc_words = specific_words(" ".join([doc["title"], doc["text"], " ".join(doc["keywords"])]))
        shared = wanted & doc_words
        lexical = len(shared) / len(wanted) if wanted else 0.0
        if not match and not (shared and lexical >= 0.34):
            continue
        score = lexical + (0.4 if match else 0)
        candidates.append(
            {
                **doc,
                "semantic_score": None,
                "keyword_score": round(lexical, 4),
                "hybrid_score": round(score, 4),
                "retrieval_mode": "curated_local_keyword",
            }
        )
    return sorted(candidates, key=lambda r: (-r["hybrid_score"], str(r["id"])))[:top_k]


def retrieve_rag_context(query, dataset_ids=None, top_k: int = 4):
    """Search both curated baseline and persistent verified table documentation.

    The same question must give the same candidates on every machine, so:
      * the wording is normalised (capitalisation and spacing never matter);
      * the persistent catalogue, which only contains tables THIS machine has answered before, is used only
        for tables already named in `dataset_ids` (after verification), never to pick the first
        candidates. Set SAGE_USE_HISTORY_CATALOG=1 to bring the old, history-dependent behaviour
        back;
      * equal scores always resolve in the same order.
    """
    from utils.query_text import normalise_query

    query = normalise_query(query)
    try:
        persisted = get_rag_catalog().retrieve(query, dataset_ids=dataset_ids, top_k=top_k)
    except (OSError, ValueError) as error:
        logger.warning("Persistent RAG search unavailable: %s", error)
        persisted = []
    if not env_flag("SAGE_USE_HISTORY_CATALOG", False):
        named = {str(item).upper() for item in (dataset_ids or [])}
        persisted = [item for item in persisted if str(item.get("table_id", "")).upper() in named]
    baseline = (
        fast_rag_search(query, dataset_ids=dataset_ids, top_k=top_k)
        if FAST_RAG_ENABLED
        else hybrid_rag_search(query, top_k=top_k)
    )
    combined = {item["id"]: item for item in persisted + baseline}
    existing = sorted(
        combined.values(), key=lambda r: (-(r.get("hybrid_score") or 0), str(r.get("id", "")))
    )[:top_k]
    # Opt-in discoverable SQLite FTS5 + Chroma catalogue. Indexed descriptions
    # are search hints only; the existing official tool still verifies data.
    # Keep a portion of legacy curated context and never fail a question just
    # because an optional index is absent or stale on Streamlit Cloud.
    try:
        from utils.hybrid_discovery import hybrid_discovery_enabled

        _hybrid_on = hybrid_discovery_enabled()
    except Exception:  # noqa: BLE001 - optional index check: fall back to the setting
        _hybrid_on = env_flag("SAGE_HYBRID_DISCOVERY", False)
    if _hybrid_on:
        try:
            from utils.hybrid_discovery import retrieve as search_hybrid

            extra = search_hybrid(query, dataset_ids=dataset_ids, top_k=top_k)
            if extra:
                # Explicitly reserve room for both old curated/RAG notes and
                # new series-level KB matches. Avoid comparing unlike scores.
                cap = max(1, top_k // 2)
                used = {item["id"] for item in existing[: top_k - cap]}
                chosen = list(existing[: top_k - cap])
                chosen.extend(item for item in extra if item["id"] not in used)
                return chosen[:top_k]
        except Exception as exc:  # noqa: BLE001 - optional extra notes: the existing notes remain
            logger.warning("Optional hybrid discovery skipped: %s", exc)
    return existing


def enhance_rag_after_verified_tools(query, initial, results, dataset_ids=None, top_k: int = 5):
    """Index table descriptions AFTER successful analytic verification.

    An absent official methodology note is always reported as absent, never
    generated from a table name or model statement.
    """
    catalog = get_rag_catalog()
    extra_ids = set(dataset_ids or [])
    for item in results:
        if not isinstance(item, dict) or item.get("success") is not True:
            continue
        if catalog.index_verified_result(item):
            extra_ids.add(item["table_id"])
    refreshed = retrieve_rag_context(query, dataset_ids=extra_ids, top_k=top_k)
    return list({item["id"]: item for item in (refreshed + initial)}.values())[:top_k]


def format_rag_context(results) -> str:
    """Format retrieved RAG documents for inclusion in an AI request."""
    blocks = []

    for item in results:
        blocks.append(
            f"""
RAG ID: {item['id']}
Title: {item['title']}
Source: {item['source_label']}
URL: {item['source_url']}
Content:
{item['text']}
""".strip()
        )

    return "\n\n---\n\n".join(blocks)
