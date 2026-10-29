"""Persistent, validated, offline-first SingStat cache shared by every StatsSG tool.

Only official table-data URLs reach ``LocalStore`` from the analytical core.
The full raw JSON, metadata AND flattened values are preserved. New snapshots
are staged, read-back verified, then atomically activated; no automatic purge.

Used by: utils/core_secure.py, utils/official_source.py,
utils/table_tools.py, tools/characterise_download.py, tools/check_table.py.
Tested in: tests/test_sources_and_catalogue.py,
tests/test_storage_and_tools.py.
"""

from __future__ import annotations

from contextlib import contextmanager, closing
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import zlib

# When a table has already been downloaded into durable staging but the
# upstream server is known to ignore pagination, ordinary questions should
# read the local status rather than repeatedly making the same broken request.
# The manual refresh action bypasses this cooldown so new official releases
# can still be downloaded immediately at the user's request.
PENDING_RETRY_SECONDS = 15 * 60


from utils.errors import UnverifiablePaginationError  # noqa: F401 - re-exported
from utils.table_download import (
    TableDownloader,
    _hash,
    _revision,
    _serialized,
    _table_signature,
    _flatten,
    _merge_row,
    _partial_payload,
    _row_id,
    table_url,
    validated_id,
)

_LOG = logging.getLogger("sage.store")

_LOCK = threading.RLock()
_SYNC_LOCK = threading.RLock()  # legacy name; guards only the per-table lock registry below
_TABLE_LOCKS: dict = {}


def _table_lock(table_id):
    """One re-entrant download lock per table: a slow download of table A no
    longer blocks the first fetch of table B."""
    with _SYNC_LOCK:
        lock = _TABLE_LOCKS.get(table_id)
        if lock is None:
            lock = _TABLE_LOCKS[table_id] = threading.RLock()
        return lock


class _TableLocks:
    """Acquire several tables' locks in a fixed (sorted) order: no deadlocks."""

    def __init__(self, table_ids) -> None:
        """One lock per table, sorted by ID so concurrent users never deadlock."""
        self._locks = [_table_lock(t) for t in sorted(set(table_ids))]

    def __enter__(self):
        """Acquire the locks in table-ID order."""
        for lock in self._locks:
            lock.acquire()
        return self

    def __exit__(self, *exc) -> None:
        """Release the locks in reverse order."""
        for lock in reversed(self._locks):
            lock.release()


class _VerifiedSnapshotCache:
    """Byte-bounded LRU of snapshot bytes that already passed the full integrity check.

    Saves zlib decompression, SHA-256 hashing and a COUNT(*) query on every table
    read. ``verify_all()`` and backups still re-check everything from disk.
    """

    def __init__(self, max_bytes) -> None:
        """An empty least-recently-used cache holding at most ``max_bytes``."""
        from collections import OrderedDict

        self.max_bytes = max_bytes
        self._items = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key):
        """The cached payload bytes for ``key`` (marking it recently used), or None."""
        with self._lock:
            raw = self._items.get(key)
            if raw is not None:
                self._items.move_to_end(key)
            return raw

    def put(self, key, raw) -> None:
        """Cache ``raw`` under ``key``, evicting the least recently used entries to stay within
        max_bytes."""
        if len(raw) > self.max_bytes:
            return
        with self._lock:
            if key in self._items:
                return
            self._items[key] = raw
            self._bytes += len(raw)
            while self._bytes > self.max_bytes:
                _, dropped = self._items.popitem(last=False)
                self._bytes -= len(dropped)

    def clear(self) -> None:
        """Empty the cache (e.g. after the database file changed)."""
        with self._lock:
            self._items.clear()
            self._bytes = 0


def _cache_megabytes():
    """Size of the in-memory verified-snapshot cache in MB (SAGE_SNAPSHOT_CACHE_MB, 0-1024, default
    64)."""
    try:
        return min(1024, max(0, int(os.environ.get("SAGE_SNAPSHOT_CACHE_MB", "64"))))
    except ValueError:
        return 64


_VERIFIED_CACHE = _VerifiedSnapshotCache(_cache_megabytes() * 1024 * 1024)


class LocalStore(TableDownloader):
    """SQLite snapshots with a separate atomic pointer for each active table."""

    def __init__(self, path) -> None:
        """Open (creating if needed) the store at ``path`` and its schema."""
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        """Open a SQLite connection with foreign keys, a busy timeout and crash-safe WAL settings."""
        con = sqlite3.connect(str(self.path), timeout=25)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=25000")
        # Safe with WAL: committed transactions survive an application crash; only an
        # OS crash/power loss can drop the last commits (snapshots re-download).
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA temp_store=MEMORY")
        return con

    @contextmanager
    def session(self):
        """Context manager: a connection that commits on success and rolls back on any error."""
        con = self.connect()
        try:
            yield con
            con.commit()
        except Exception:  # noqa: BLE001 - roll back on ANY error, then re-raise
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        """Create the store's tables and indexes if they do not exist yet (safe to call repeatedly)."""
        with self.session() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_id TEXT NOT NULL,
                    collected_at TEXT NOT NULL DEFAULT (datetime('now')),
                    source_url TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    observation_count INTEGER NOT NULL,
                    reported_total INTEGER,
                    title TEXT NOT NULL,
                    payload_zlib BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                    series_id TEXT NOT NULL,
                    series_name TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    period_path TEXT NOT NULL,
                    value_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS observation_by_snapshot
                    ON observations(snapshot_id, series_id);
                CREATE TABLE IF NOT EXISTS active_snapshots (
                    table_id TEXT PRIMARY KEY,
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS response_cache (
                    url TEXT PRIMARY KEY,
                    saved_at TEXT NOT NULL DEFAULT (datetime('now')),
                    sha256 TEXT NOT NULL,
                    payload_zlib BLOB NOT NULL
                );
                -- Unlike SQLite TEMP tables, this staging area survives an app
                -- restart so successive downloads can add missing official IDs.
                -- It is NEVER queried by the statistical-analysis functions.
                CREATE TABLE IF NOT EXISTS pending_series (
                    table_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    series_id TEXT NOT NULL,
                    row_json TEXT NOT NULL,
                    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY(table_id, revision, series_id)
                );
                CREATE TABLE IF NOT EXISTS pending_refreshes (
                    table_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    expected_count INTEGER,
                    metadata_confirmed INTEGER NOT NULL DEFAULT 0,
                    table_signature TEXT NOT NULL,
                    completed_snapshot_id INTEGER REFERENCES snapshots(id),
                    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
                    attempts INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(table_id, revision)
                );
            """)

    def _retain_pending(self, table_id, partial, expected, *, authoritative: bool = False):
        """Deduplicate and persist a failed download without changing active data.

        Rows from different official revisions are stored separately. Unknown
        revisions can be retained diagnostically, but can never be combined
        across downloads to claim a complete statistical table.
        """
        if partial is None:
            return None
        rows = partial.get("Data", {}).get("row", [])
        revision = _revision(partial) or "unknown"
        ids = [_row_id(row) for row in rows]
        if not rows or not all(ids) or len(ids) != len(set(ids)):
            return None
        # Even staged data must have well-formed official observations.
        _flatten(partial)
        signature = _table_signature(partial)
        with _LOCK, self.session() as con:
            previous = con.execute(
                """
                SELECT expected_count,metadata_confirmed,table_signature
                FROM pending_refreshes
                WHERE table_id=? AND revision=?
            """,
                (table_id, revision),
            ).fetchone()
            if previous and previous[1] and authoritative and previous[0] != expected:
                raise ValueError("Official series count changed within one table revision.")
            if previous and previous[2] != signature:
                raise ValueError(
                    "Table schema/source changed within one revision; "
                    "previous staging is preserved separately."
                )
            con.execute(
                """
                INSERT INTO pending_refreshes(table_id,revision,expected_count,
                                              metadata_confirmed,table_signature)
                VALUES(?,?,?,?,?)
                ON CONFLICT(table_id,revision) DO UPDATE SET
                    expected_count=CASE WHEN excluded.metadata_confirmed=1
                        THEN excluded.expected_count ELSE pending_refreshes.expected_count END,
                    metadata_confirmed=MAX(pending_refreshes.metadata_confirmed,
                                           excluded.metadata_confirmed),
                    completed_snapshot_id=NULL,
                    last_seen=datetime('now'), attempts=attempts+1
            """,
                (table_id, revision, expected, int(bool(authoritative)), signature),
            )
            for row in rows:
                ident = _row_id(row)
                found = con.execute(
                    """
                    SELECT row_json FROM pending_series
                    WHERE table_id=? AND revision=? AND series_id=?
                """,
                    (table_id, revision, ident),
                ).fetchone()
                merged = _merge_row(json.loads(found[0]), row) if found else row
                con.execute(
                    """
                    INSERT INTO pending_series(table_id,revision,series_id,row_json)
                    VALUES(?,?,?,?)
                    ON CONFLICT(table_id,revision,series_id) DO UPDATE SET
                        row_json=excluded.row_json,last_seen=datetime('now')
                """,
                    (table_id, revision, ident, json.dumps(merged, ensure_ascii=False)),
                )
            count = con.execute(
                """
                SELECT COUNT(*) FROM pending_series
                WHERE table_id=? AND revision=?
            """,
                (table_id, revision),
            ).fetchone()[0]
        return {
            "revision": revision,
            "rows": count,
            "expected": expected,
            "independently_counted": bool(authoritative),
        }

    def _complete_pending(self, table_id, partial, expected, *, authoritative, expected_ids=None):
        """Promote cumulative downloads only with a stable, official revision/count."""
        if authoritative and expected is not None and _revision(partial):
            prior = self.get_table(table_id)
            if (
                prior is not None
                and _revision(prior) == _revision(partial)
                and prior["Data"].get("title") == partial["Data"].get("title")
                and len(prior["Data"]["row"]) <= expected
            ):
                # Existing ACTIVE records are eligible only from the exact
                # SAME official revision. The latest incoming rows override
                # overlapping old observations below.
                self._retain_pending(table_id, prior, expected)
        status = self._retain_pending(table_id, partial, expected, authoritative=authoritative)
        if not status or not status["independently_counted"]:
            return None
        # An unversioned API response might refer to a different release on
        # every download, so it cannot make a mixed-session 'complete' dataset.
        if status["revision"] == "unknown" or status["rows"] != expected:
            return None
        with _LOCK, self.session() as con:
            rows = [
                json.loads(r[0])
                for r in con.execute(
                    """
                SELECT row_json FROM pending_series
                WHERE table_id=? AND revision=? ORDER BY series_id
            """,
                    (table_id, status["revision"]),
                )
            ]
        if expected_ids is not None and set(_row_id(row) for row in rows) != set(expected_ids):
            return None  # A manifest supersedes matching the row count alone.
        combined = _partial_payload(partial, rows)
        combined["Data"]["total"] = expected
        _flatten(combined)
        return combined

    def _snapshot_record(self, con, table_id, *, snapshot_id=None):
        """The active (or given) snapshot row of a table: id, compressed payload, hash and counts;
        None if absent."""
        if snapshot_id is None:
            return con.execute(
                """
                SELECT s.id,s.payload_zlib,s.sha256,s.row_count,s.observation_count
                FROM active_snapshots a JOIN snapshots s ON s.id=a.snapshot_id
                WHERE a.table_id=?
            """,
                (table_id,),
            ).fetchone()
        return con.execute(
            """
            SELECT id,payload_zlib,sha256,row_count,observation_count
            FROM snapshots WHERE id=? AND table_id=?
        """,
            (snapshot_id, table_id),
        ).fetchone()

    def _snapshot_manifest(self, con, table_id, *, snapshot_id=None):
        """(id, sha256, row_count, observation_count) without reading the payload blob."""
        if snapshot_id is None:
            return con.execute(
                """
                SELECT s.id,s.sha256,s.row_count,s.observation_count
                FROM active_snapshots a JOIN snapshots s ON s.id=a.snapshot_id
                WHERE a.table_id=?
            """,
                (table_id,),
            ).fetchone()
        return con.execute(
            """
            SELECT id,sha256,row_count,observation_count
            FROM snapshots WHERE id=? AND table_id=?
        """,
            (snapshot_id, table_id),
        ).fetchone()

    def _verified_payload(self, con, table_id, *, snapshot_id=None, use_cache: bool = True):
        """Decompress a saved snapshot and re-check its hash and counts; None if it is missing or
        fails the check."""
        manifest = self._snapshot_manifest(con, table_id, snapshot_id=snapshot_id)
        if manifest is None:
            return None
        sid, expected, row_count, cell_count = manifest
        cache_key = (str(self.path), sid, expected)
        raw = _VERIFIED_CACHE.get(cache_key) if use_cache else None
        if raw is not None:
            # Snapshots are append-only and keyed by id + SHA-256, so bytes verified
            # once stay valid; a fresh dict is parsed for every caller.
            return json.loads(raw)
        compressed = con.execute(
            "SELECT payload_zlib FROM snapshots WHERE id=?", (sid,)
        ).fetchone()[0]
        raw = zlib.decompress(compressed)
        if _hash(raw) != expected:
            raise ValueError(
                f"Local integrity check failed for {table_id}; no unverified data " f"served."
            )
        payload = json.loads(raw)
        actual_id = str(payload.get("Data", {}).get("id") or table_id).strip().upper()
        if actual_id != table_id:
            raise ValueError("Stored payload has a mismatched table ID.")
        if len(payload["Data"]["row"]) != row_count:
            raise ValueError("Cached row count does not match its manifest.")
        saved = con.execute(
            "SELECT COUNT(*) FROM observations WHERE snapshot_id=?", (sid,)
        ).fetchone()[0]
        if saved != cell_count:
            raise ValueError("Cached observation count does not match its manifest.")
        _VERIFIED_CACHE.put(cache_key, raw)
        return payload

    def get_table(self, table_id):
        """The verified saved copy of one complete official table, or None if there is none."""
        table_id = validated_id(table_id)
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            return self._verified_payload(con, table_id)

    def table_cache_age_seconds(self, table_id, *, now=None):
        """Age of ACTIVE verified snapshot, from SQLite's UTC collected_at."""
        table_id = validated_id(table_id)
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            row = con.execute(
                """
                SELECT strftime('%s', s.collected_at)
                FROM active_snapshots a JOIN snapshots s ON s.id=a.snapshot_id
                WHERE a.table_id=?
            """,
                (table_id,),
            ).fetchone()
        if not row or row[0] is None:
            return None
        return max(0.0, (time.time() if now is None else now) - float(row[0]))

    def mirror_cache_age_seconds(self, url, *, now=None):
        """Age of saved URL response; response still needs validator on read."""
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            row = con.execute(
                "SELECT strftime('%s', saved_at) FROM response_cache WHERE url=?", (url,)
            ).fetchone()
        if not row or row[0] is None:
            return None
        return max(0.0, (time.time() if now is None else now) - float(row[0]))

    def refresh_table_if_expired(self, table_id, ttl_seconds, fetch_page, *, fetch_metadata=None):
        """Synchronise stale tables; verify before atomic activation.

        Double-check under the shared synchronisation lock to avoid redundant
        refreshes by concurrent users. Never activate partial API results.
        Returns True only if a network refresh was attempted and succeeded.
        """
        table_id = validated_id(table_id)
        with _table_lock(table_id):
            age = self.table_cache_age_seconds(table_id)
            if age is not None and age <= ttl_seconds:
                return False
            self.sync_one(table_id, fetch_page, fetch_metadata=fetch_metadata)
            return True

    def pending_progress(self, table_id) -> list:
        """Report durable distinct-series progress, never claiming completeness."""
        table_id = validated_id(table_id)
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            rows = con.execute(
                """
                SELECT p.revision,p.expected_count,p.metadata_confirmed,
                       p.attempts,p.last_seen,COUNT(s.series_id)
                FROM pending_refreshes p
                LEFT JOIN pending_series s ON s.table_id=p.table_id
                  AND s.revision=p.revision
                WHERE p.table_id=? AND p.completed_snapshot_id IS NULL
                GROUP BY p.table_id,p.revision
                ORDER BY p.last_seen DESC
            """,
                (table_id,),
            ).fetchall()
        return [
            {
                "revision": revision,
                "rows": count,
                "expected": expected if confirmed else None,
                "attempts": attempts,
                "last_seen": last_seen,
            }
            for revision, expected, confirmed, attempts, last_seen, count in rows
        ]

    def _recent_pending(self, table_id) -> bool:
        """Avoid an identical automatic API retry shortly after a failed sync."""
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            last = con.execute(
                """
                SELECT MAX(strftime('%s',last_seen)) FROM pending_refreshes
                WHERE table_id=? AND completed_snapshot_id IS NULL
            """,
                (table_id,),
            ).fetchone()[0]
        return last is not None and time.time() - int(last) < PENDING_RETRY_SECONDS

    def staged_catalog(self, table_id):
        """Expose provenance-labelled staged SERIES labels, not a complete table.

        Only the latest unambiguous revision is shown. A staged row is an
        official observation that passed structural checks; its presence does
        not establish that the whole table or every reporting year was fetched.
        """
        table_id = validated_id(table_id)
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            revisions = con.execute(
                """
                SELECT revision FROM pending_refreshes
                WHERE table_id=? AND completed_snapshot_id IS NULL
                ORDER BY last_seen DESC
            """,
                (table_id,),
            ).fetchall()
            if len(revisions) != 1:
                return []  # Avoid mixing different or ambiguous revisions.
            records = con.execute(
                """
                SELECT series_id,row_json FROM pending_series
                WHERE table_id=? AND revision=? ORDER BY series_id
            """,
                (table_id, revisions[0][0]),
            ).fetchall()
        catalog = []
        for series_id, raw in records:
            row = json.loads(raw)
            if _row_id(row) != series_id or not isinstance(row.get("columns"), list):
                raise ValueError("A staged official series failed read-back validation.")
            catalog.append(
                {
                    "series_id": series_id,
                    "series_name": str(row.get("rowText") or "").strip(),
                    "revision": revisions[0][0],
                }
            )
        return catalog

    def exact_staged_series(self, table_id, *, series_id=None, series_name=None):
        """Return one explicitly identified row, NEVER a purported full dataset.

        Exact label matching is rejected when the official hierarchy contains
        multiple identically labelled male/female/national series. The caller
        must identify a unique series ID instead in that case.
        """
        if bool(series_id) == bool(series_name):
            raise ValueError("Supply exactly one official series ID or exact label.")
        catalog = self.staged_catalog(table_id)
        if series_id is not None:
            matches = [r for r in catalog if r["series_id"] == str(series_id)]
        else:
            normalized = " ".join(str(series_name).casefold().split())
            matches = [
                r for r in catalog if " ".join(r["series_name"].casefold().split()) == normalized
            ]
        if len(matches) != 1:
            return None
        selected = matches[0]
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            raw = con.execute(
                """
                SELECT row_json FROM pending_series
                WHERE table_id=? AND revision=? AND series_id=?
            """,
                (validated_id(table_id), selected["revision"], selected["series_id"]),
            ).fetchone()
        if not raw:
            return None
        row = json.loads(raw[0])
        if _row_id(row) != selected["series_id"]:
            raise ValueError("Staged official series changed during lookup.")
        _flatten({"Data": {"row": [row]}})
        return {
            "table_id": validated_id(table_id),
            "revision": selected["revision"],
            "series_id": selected["series_id"],
            "row": row,
            "table_complete": False,
            "notice": (
                "Only this saved official series was inspected. "
                "The complete source table and its latest period "
                "have NOT been independently established."
            ),
        }

    def stage(self, table_id, fetch_page, *, allow_shrink: bool = False, fetch_metadata=None):
        """Stage and verify an entire new table; NEVER change the active pointer."""
        table_id = validated_id(table_id)
        try:
            download_started = time.monotonic()
            payload, reported = self._fetch_complete(
                table_id, fetch_page, fetch_metadata=fetch_metadata
            )
            try:
                downloaded_rows = len(((payload or {}).get("Data") or {}).get("row") or [])
            except AttributeError:
                downloaded_rows = -1
            logging.getLogger("sage.timing").info(
                "table_download table=%s rows=%s seconds=%.1f",
                table_id,
                downloaded_rows,
                time.monotonic() - download_started,
            )
        except UnverifiablePaginationError as error:
            if error.partial is None:
                raise
            complete = self._complete_pending(
                table_id,
                error.partial,
                error.expected,
                authoritative=error.authoritative_total,
                expected_ids=error.expected_ids,
            )
            if complete is None:
                raise  # Pending rows preserved; active snapshot unchanged.
            payload, reported = complete, error.expected
        rows = payload["Data"]["row"]
        # The incoming table has now proved its SERIES coverage. Preserve old
        # historical periods only for matching official identities/units.
        # Never resurrect a series that the new complete release removed.
        previous = self.get_table(table_id)
        if (
            previous is not None
            and previous["Data"].get("title") == payload["Data"].get("title")
            and previous["Data"].get("frequency") == payload["Data"].get("frequency")
        ):
            by_id = {_row_id(row): row for row in previous["Data"]["row"] if _row_id(row)}
            enriched = [
                _merge_row(by_id[_row_id(row)], row) if _row_id(row) in by_id else row
                for row in rows
            ]
            payload = _partial_payload(payload, enriched)
            rows = payload["Data"]["row"]
        cells = _flatten(payload)
        blob = _serialized(payload)
        expected_sha = _hash(blob)
        with _LOCK, self.session() as con:
            old = con.execute(
                """
                SELECT s.row_count,s.observation_count FROM active_snapshots a
                JOIN snapshots s ON s.id=a.snapshot_id WHERE a.table_id=?
            """,
                (table_id,),
            ).fetchone()
            if old and not allow_shrink and (len(rows) < old[0] or len(cells) < old[1]):
                raise ValueError(
                    "New snapshot is smaller than the local verified version; "
                    "manual review is required before replacing it."
                )
            cursor = con.execute(
                """
                INSERT INTO snapshots(table_id,source_url,sha256,row_count,
                     observation_count,reported_total,title,payload_zlib)
                VALUES(?,?,?,?,?,?,?,?)
            """,
                (
                    table_id,
                    table_url(table_id),
                    expected_sha,
                    len(rows),
                    len(cells),
                    reported,
                    str(payload["Data"].get("title", "")),
                    zlib.compress(blob, 6),
                ),
            )
            sid = cursor.lastrowid
            con.executemany(
                """
                INSERT INTO observations(snapshot_id,series_id,series_name,unit,period_path,value_json)
                VALUES(?,?,?,?,?,?)
            """,
                ((sid, *cell) for cell in cells),
            )
        # Verify using a NEW connection after the staging transaction commits.
        with _LOCK, self.session() as verify_con:
            checked = self._verified_payload(verify_con, table_id, snapshot_id=sid, use_cache=False)
            if _serialized(checked) != blob:
                raise ValueError("SQLite read-back differs from the downloaded complete table.")
        return sid

    def sync_tables(self, table_ids, fetch_page, *, on_progress=None, fetch_metadata=None):
        """Stage ALL requested tables, verify all, atomically activate as a set."""
        ids = list(dict.fromkeys(validated_id(t) for t in table_ids))
        if not ids:
            raise ValueError("No table IDs supplied for synchronisation.")
        with _TableLocks(ids):
            staged = {}
            for index, table_id in enumerate(ids, start=1):
                if on_progress:
                    on_progress(index, len(ids), table_id)
                staged[table_id] = self.stage(table_id, fetch_page, fetch_metadata=fetch_metadata)
            with _LOCK, self.session() as con:
                # Validate again inside the same activation transaction.
                for table_id, sid in staged.items():
                    self._verified_payload(con, table_id, snapshot_id=sid, use_cache=False)
                for table_id, sid in staged.items():
                    con.execute(
                        """
                        INSERT INTO active_snapshots(table_id,snapshot_id) VALUES(?,?)
                        ON CONFLICT(table_id) DO UPDATE SET snapshot_id=excluded.snapshot_id
                    """,
                        (table_id, sid),
                    )
                    full = self._verified_payload(con, table_id, snapshot_id=sid)
                    revision = _revision(full)
                    if revision:
                        saved_ids = {_row_id(row) for row in full["Data"]["row"]}
                        pending_ids = {
                            row[0]
                            for row in con.execute(
                                """
                            SELECT series_id FROM pending_series
                            WHERE table_id=? AND revision=?
                        """,
                                (table_id, revision),
                            )
                        }
                        if pending_ids and pending_ids.issubset(saved_ids):
                            # Completion is marked only AFTER verifying the
                            # activated snapshot; keep the staged originals
                            # archived for independent backup/audit.
                            con.execute(
                                """
                                UPDATE pending_refreshes
                                SET completed_snapshot_id=?
                                WHERE table_id=? AND revision=?
                            """,
                                (sid, table_id, revision),
                            )
            return staged

    def sync_one(self, table_id, fetch_page, *, fetch_metadata=None):
        """Download (or refresh) one table and save it; see sync_tables for the rules."""
        return self.sync_tables([table_id], fetch_page, fetch_metadata=fetch_metadata)[
            validated_id(table_id)
        ]

    def get_or_fetch(self, table_id, fetch_page, *, offline: bool = False, fetch_metadata=None):
        """The saved verified table, downloading and saving it first if there is no saved copy."""
        table_id = validated_id(table_id)
        payload = self.get_table(table_id)
        if payload is not None:
            return payload
        if offline:
            raise ValueError(f"{table_id} is not in the local database. Synchronise while online.")
        with _table_lock(table_id):
            payload = self.get_table(table_id)
            if payload is None:
                if self._recent_pending(table_id):
                    progress = self.pending_progress(table_id)
                    count = sum(item["rows"] for item in progress)
                    raise UnverifiablePaginationError(
                        f"{table_id}: {count} distinct official series already "
                        "downloaded and merged into SQLite staging. An automatic "
                        "retry was skipped because the API recently repeated "
                        "its pagination. Use the manual Stage and merge action "
                        "to request additional data; incomplete table-wide "
                        "statistics remain unavailable."
                    )
                self.sync_one(table_id, fetch_page, fetch_metadata=fetch_metadata)
                payload = self.get_table(table_id)
        if payload is None:
            raise ValueError("No verified active local snapshot was created.")
        return payload

    def cache_response(
        self, url, fetch_page, *, offline: bool = False, validator=None, ttl_seconds=None
    ):
        """Durably cache exact official mirror URLs, labelled as partial responses.

        Mirror responses aren't represented as full SingStat-table snapshots.
        """
        age = self.mirror_cache_age_seconds(url) if ttl_seconds is not None else None
        expired = age is not None and age > ttl_seconds
        with _LOCK, self.session() as con:
            record = con.execute(
                "SELECT sha256,payload_zlib FROM response_cache WHERE url=?", (url,)
            ).fetchone()
            if record:
                raw = zlib.decompress(record[1])
                if _hash(raw) != record[0]:
                    raise ValueError("Local mirror-response checksum mismatch.")
                payload = json.loads(raw)
                if validator is not None:
                    try:
                        validator(payload)  # Never serve an invalid cached mirror.
                    except (ValueError, TypeError, KeyError):
                        # A previous release could cache only part of the
                        # data.gov.sg result.  Retain those bytes, but replace
                        # only AFTER downloading and verifying a valid result.
                        if offline:
                            raise ValueError(
                                "The saved official mirror is incomplete. "
                                "Connect once to download and validate its replacement."
                            )
                    else:
                        if not expired or offline:
                            return payload
                else:
                    if not expired or offline:
                        return payload
        if offline:
            raise ValueError("This mirror response has not been cached yet.")
        response = fetch_page(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload:
            raise ValueError("Invalid official mirror response.")
        if validator is not None:
            validator(payload)  # Validate complete data BEFORE local persistence.
        raw = _serialized(payload)
        with _LOCK, self.session() as con:
            con.execute(
                """INSERT INTO response_cache(url,saved_at,sha256,payload_zlib)
                        VALUES(?,datetime('now'),?,?)
                        ON CONFLICT(url) DO UPDATE SET
                        saved_at=datetime('now'),sha256=excluded.sha256,
                        payload_zlib=excluded.payload_zlib""",
                (url, _hash(raw), zlib.compress(raw, 6)),
            )
        with _LOCK, self.session() as con:
            saved = con.execute(
                "SELECT sha256,payload_zlib FROM response_cache WHERE url=?", (url,)
            ).fetchone()
            checked = zlib.decompress(saved[1])
            if _hash(checked) != _hash(raw):
                raise ValueError("Official mirror response failed local read-back verification.")
        return payload

    def get_cached_response(self, url, *, validator=None):
        """Read an independently validated mirror without performing network I/O.

        An invalid old cached response remains in SQLite for investigation but
        must never be used as statistical data. ``cache_response`` can refresh
        that entry once an official complete replacement is available.
        """
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            record = con.execute(
                "SELECT sha256,payload_zlib FROM response_cache WHERE url=?", (url,)
            ).fetchone()
        if record is None:
            return None
        raw = zlib.decompress(record[1])
        if _hash(raw) != record[0]:
            raise ValueError("Local official mirror checksum verification failed.")
        payload = json.loads(raw)
        if validator is not None:
            try:
                validator(payload)
            except (ValueError, KeyError, TypeError):
                return None  # Retain, but never serve, the incomplete old copy.
        return payload

    def refresh_response(self, url, fetch_page, *, validator):
        """Replace one mirror only after external and SQLite read-back validation.

        On an HTTP, schema, checksum or read-back failure the original cache
        entry remains unchanged, including during an explicit manual refresh.
        """
        response = fetch_page(url)
        response.raise_for_status()
        payload = response.json()
        validator(payload)
        raw = _serialized(payload)
        digest = _hash(raw)
        with _LOCK, self.session() as con:
            con.execute(
                "INSERT OR REPLACE INTO response_cache(url,sha256,payload_zlib) VALUES(?,?,?)",
                (url, digest, zlib.compress(raw, 6)),
            )
            check = con.execute(
                "SELECT sha256,payload_zlib FROM response_cache WHERE url=?", (url,)
            ).fetchone()
            if check[0] != digest or _hash(zlib.decompress(check[1])) != digest:
                raise ValueError("Official mirror failed transactional SQLite read-back.")
        return payload

    def status(self):
        """One row per saved table (ID, title, when collected, rows, cells) for operators."""
        with self.session() as con:  # read-only: no process-wide lock (WAL)
            rows = con.execute("""
                SELECT a.table_id,s.title,s.collected_at,s.row_count,s.observation_count,s.id
                FROM active_snapshots a JOIN snapshots s ON s.id=a.snapshot_id
                ORDER BY a.table_id
            """).fetchall()
            archived = con.execute("""
                SELECT COUNT(*) FROM snapshots s LEFT JOIN active_snapshots a ON a.snapshot_id=s.id
                WHERE a.snapshot_id IS NULL
            """).fetchone()[0]
            mirror = con.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0]
            pending = con.execute("""
                SELECT p.table_id,p.revision,p.expected_count,p.metadata_confirmed,
                       p.attempts,p.last_seen,COUNT(r.series_id)
                FROM pending_refreshes p LEFT JOIN pending_series r
                    ON r.table_id=p.table_id AND r.revision=p.revision
                WHERE p.completed_snapshot_id IS NULL
                GROUP BY p.table_id,p.revision
                ORDER BY p.last_seen DESC,p.table_id
            """).fetchall()
            return {
                "path": str(self.path),
                "tables": len(rows),
                "observations": sum(r[4] for r in rows),
                "archived_versions": archived,
                "mirror_responses": mirror,
                "pending_tables": len(set(r[0] for r in pending)),
                "pending_rows": sum(r[6] for r in pending),
                "pending_items": [
                    {
                        "table_id": r[0],
                        "revision": r[1],
                        "expected": r[2] if r[3] else None,
                        "rows": r[6],
                        "attempts": r[4],
                        "last_seen": r[5],
                        "ready_for_analysis": False,
                    }
                    for r in pending
                ],
                "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "items": [
                    {
                        "table_id": r[0],
                        "title": r[1],
                        "saved_at": r[2],
                        "rows": r[3],
                        "observations": r[4],
                        "snapshot_id": r[5],
                        "analysis_ready": True,
                    }
                    for r in rows
                ],
            }

    def verify_all(self):
        """Read/check every active AND archived snapshot and exact mirror response."""
        with _LOCK, self.session() as con:
            quick = con.execute("PRAGMA integrity_check").fetchone()[0]
            if quick != "ok":
                raise ValueError("SQLite integrity check failed: " + str(quick))
            snapshots = con.execute(
                "SELECT id,table_id,sha256 FROM snapshots ORDER BY id"
            ).fetchall()
            for sid, table_id, _sha in snapshots:
                self._verified_payload(con, table_id, snapshot_id=sid, use_cache=False)
            mirrors = con.execute("SELECT sha256,payload_zlib FROM response_cache").fetchall()
            for sha, blob in mirrors:
                if _hash(zlib.decompress(blob)) != sha:
                    raise ValueError("An archived mirror response failed its checksum.")
            pending = con.execute("""
                SELECT table_id,revision,series_id,row_json FROM pending_series
                ORDER BY table_id,revision,series_id
            """).fetchall()
            for table_id, revision, series_id, raw in pending:
                row = json.loads(raw)
                if _row_id(row) != series_id or not isinstance(row.get("columns"), list):
                    raise ValueError("A retained pending series failed structural verification.")
                _flatten({"Data": {"row": [row]}})
            active = con.execute("SELECT COUNT(*) FROM active_snapshots").fetchone()[0]
            manifest = [(sid, table_id, sha) for sid, table_id, sha in snapshots]
            mirror_hashes = sorted(sha for sha, _ in mirrors)
            pending_manifest = [(t, r, i, _hash(raw.encode("utf-8"))) for t, r, i, raw in pending]
            return {
                "verified_tables": active,
                "verified_snapshots": len(snapshots),
                "verified_mirrors": len(mirrors),
                "verified_pending_series": len(pending),
                "sqlite_integrity": quick,
                "archived_versions_retained": True,
                "manifest_sha256": _hash(_serialized([manifest, mirror_hashes, pending_manifest])),
            }

    def backup(self, destination) -> str:
        """SQLite online backup; independently verify every saved version."""
        before = self.verify_all()
        dest = Path(destination).expanduser().resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest == self.path:
            raise ValueError("Backup destination must differ from the active database.")
        with _LOCK, self.session() as src, closing(sqlite3.connect(str(dest))) as target:
            src.backup(target)
        copied = LocalStore(dest)
        after = copied.verify_all()
        for key in (
            "verified_tables",
            "verified_snapshots",
            "verified_mirrors",
            "verified_pending_series",
            "manifest_sha256",
        ):
            if after[key] != before[key]:
                raise ValueError("Backup fails snapshot/mirror manifest equality check: " + key)
        return str(dest)


def default_db_path(app_dir):
    """Path of the local data cache (STATSSG_DB_PATH overrides)."""
    return os.getenv("STATSSG_DB_PATH", str(Path(app_dir) / "data" / "app_cache.sqlite3"))
