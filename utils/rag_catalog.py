"""Persistent, source-bound retrieval for verified StatsSG table metadata.

No network fetches, generated methodology, executable documents or user-controlled
SQL. Table metadata is context only. Curated methodology needs actual approved text.

Used by: utils/rag_engine.py.
Tested in: tests/test_discovery_and_ingest.py,
tests/test_storage_and_tools.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from utils.sqlite_session import SQLiteSession
import re
from urllib.parse import urlparse

DB_FILENAME = "source_catalog.sqlite3"
ALLOWED_HOSTS = {
    "tablebuilder.singstat.gov.sg",
    "singstat.gov.sg",
    "www.singstat.gov.sg",
    "data.gov.sg",
}
TABLE_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{2,18}$")
MAX_TEXT = 10000
MAX_RECORDS = 40000
STOPWORDS = {
    "the",
    "and",
    "for",
    "are",
    "has",
    "have",
    "there",
    "been",
    "much",
    "change",
    "changed",
    "over",
    "time",
    "singapore",
    "singstat",
    "stats",
    "table",
    "official",
    "data",
    "from",
    "what",
    "when",
    "which",
    "where",
    "with",
    "does",
    "using",
    "between",
    "years",
    "year",
}


def _search_words(value):
    """Search words of a text (3+ characters, stop words removed)."""
    return set(re.findall(r"[a-z0-9]{3,}", str(value).casefold())) - STOPWORDS


def _verified_url(value) -> bool:
    """True only for an HTTPS URL on an allowed official host, without credentials."""
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname in ALLOWED_HOSTS
        and not parsed.username
        and not parsed.password
        and len(value) < 600
    )


class RagCatalog:
    """Persist grounded documentation independent of numeric table snapshots."""

    def __init__(self, base_dir) -> None:
        # Always store the catalog beside the other SQLite databases in data/.
        # Resolve relative to this module, not the process working directory.
        """Open (creating if needed) the catalogue database in the Sage data/ folder."""
        self.path = Path(__file__).resolve().parent.parent / "data" / DB_FILENAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS docs (
                id TEXT PRIMARY KEY, table_id TEXT NOT NULL, title TEXT NOT NULL,
                source_label TEXT NOT NULL, source_url TEXT NOT NULL,
                text TEXT NOT NULL, keywords TEXT NOT NULL, doc_type TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_rag_table ON docs(table_id)")

    def _session(self):
        """A short-lived SQLite connection to the catalogue database."""
        return SQLiteSession(str(self.path), timeout=25)

    def upsert(
        self,
        *,
        table_id,
        title,
        source_url,
        text,
        keywords: str = "",
        doc_type: str = "table_metadata",
    ) -> bool:
        """Save only bounded, attributable, verified metadata or approved notes."""
        table_id = str(table_id).strip().upper()
        if not TABLE_PATTERN.fullmatch(table_id) or not _verified_url(source_url):
            return False
        if doc_type not in {"table_metadata", "reviewed_source_excerpt"}:
            return False
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return False
        title = str(title or "")[:220].strip()
        if not title:
            return False
        unique_id = (
            "RAG_"
            + hashlib.sha256((table_id + "|" + doc_type + "|" + source_url).encode()).hexdigest()[
                :24
            ]
        )
        with self._session() as db:
            row_count = db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            exists = db.execute("SELECT 1 FROM docs WHERE id=?", (unique_id,)).fetchone()
            if row_count >= MAX_RECORDS and not exists:
                return False
            db.execute(
                """INSERT INTO docs (id, table_id, title, source_label, source_url,
                text, keywords, doc_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,
                text=excluded.text, keywords=excluded.keywords,
                updated_at=CURRENT_TIMESTAMP""",
                (
                    unique_id,
                    table_id,
                    title,
                    "SingStat " + table_id,
                    source_url,
                    text.strip(),
                    str(keywords)[:2000],
                    doc_type,
                ),
            )
        return True

    def index_verified_result(self, result):
        """Never promote unverified/partial statistical results into RAG knowledge."""
        if not isinstance(result, dict) or result.get("success") is not True:
            return False
        if result.get("table_complete") is False or result.get("analysis_ready") is False:
            return False
        table_id = str(result.get("table_id") or "").upper()
        source_url = result.get("source_url")
        if not TABLE_PATTERN.fullmatch(table_id) or not _verified_url(source_url):
            return False
        title = str(result.get("title") or result.get("table_title") or "").strip()
        if not title:
            return False
        series = result.get("selected_series") or result.get("series_summary") or []
        if not series:
            series = [
                result[key] for key in ("series_a", "series_b") if isinstance(result.get(key), dict)
            ]
        labels, units = [], []
        for item in series[:30] if isinstance(series, list) else []:
            if isinstance(item, dict):
                label = str(item.get("series_name") or "").strip()
                unit = str(item.get("unit") or "").strip()
                if label and label not in labels:
                    labels.append(label[:160])
                if unit and unit not in units:
                    units.append(unit[:80])
        parts = ["Official table identifier: " + table_id, "Published table title: " + title]
        if labels:
            parts.append("Verified selected series descriptions: " + "; ".join(labels))
        if units:
            parts.append("Units for selected series: " + "; ".join(units))
        parts.append(
            "This entry describes validated table metadata and selected series ONLY. "
            "It is not an official definition, explanatory note, numerical value, "
            "or statement about time-series comparability. Consult the published "
            "methodology/footnotes for those claims."
        )
        return self.upsert(
            table_id=table_id,
            title=title,
            source_url=source_url,
            text="\n".join(parts),
            keywords=" ".join(labels),
            doc_type="table_metadata",
        )

    def retrieve(self, query, *, dataset_ids=(), top_k: int = 4):
        """Ground retrieval using a dataset match OR substantive lexical overlap."""
        request_terms = _search_words(query)
        requested = {str(item).upper() for item in (dataset_ids or [])}
        requested.update(re.findall(r"\b[A-Z][A-Z0-9]{2,18}\d\b", str(query).upper()))
        with self._session() as db:
            rows = db.execute(
                "SELECT id, table_id, title, source_label, source_url, text, "
                "keywords, doc_type FROM docs"
            ).fetchall()
        candidates = []
        for identifier, table_id, title, label, url, body, keywords, doc_type in rows:
            overlap = request_terms & _search_words(title + " " + keywords + " " + body)
            exact_table = table_id in requested
            if not (exact_table or len(overlap) >= 2):
                continue
            score = (2.0 if exact_table else 0.0) + len(overlap) / max(1, len(request_terms))
            candidates.append(
                {
                    "id": identifier,
                    "table_id": table_id,
                    "title": title,
                    "source_label": label,
                    "source_url": url,
                    "text": body,
                    "doc_type": doc_type,
                    "semantic_score": None,
                    "keyword_score": round(score, 4),
                    "hybrid_score": round(score, 4),
                    "retrieval_mode": "persisted_verified_metadata",
                }
            )
        candidates.sort(
            key=lambda item: (-item["hybrid_score"], item["table_id"], item["id"])
        )  # fixed tie-break
        return candidates[:top_k]

    def import_reviewed_manifest(self, path):
        """Import user-reviewed OFFICIAL excerpts; no arbitrary remote fetching.

        This is intentionally not labelled independently verified methodology.
        The operator is responsible for matching excerpts to the source URLs.
        """
        file_path = Path(path)
        if not file_path.exists():
            return 0
        if file_path.stat().st_size > 1_000_000:
            raise ValueError("RAG notes manifest exceeds 1 MB")
        documents = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(documents, list) or len(documents) > 200:
            raise ValueError("RAG notes manifest must be a list of at most 200 excerpts")
        added = 0
        for row in documents:
            if not isinstance(row, dict) or row.get("reviewed") is not True:
                continue
            if self.upsert(
                table_id=row.get("table_id", ""),
                title=row.get("title", ""),
                source_url=row.get("source_url", ""),
                text=row.get("text", ""),
                keywords=row.get("keywords", ""),
                doc_type="reviewed_source_excerpt",
            ):
                added += 1
        return added

    def count(self):
        """Number of notes in the catalogue."""
        with self._session() as db:
            return db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
