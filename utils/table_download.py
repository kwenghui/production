"""Downloading a COMPLETE official table, and proving that it is complete.

The SingStat API returns tables in pages, and some servers repeat rows,
ignore offsets or report a per-page "total". A short page is not proof of a
whole table, so TableDownloader (mixed into utils.store.LocalStore) proves
completeness in one of these ways, strongest first:

1. an observation manifest from the separate metadata endpoint;
2. a row count / series-ID manifest that the paged download must match;
3. the table-data endpoint's own total, or a proper empty last page.

Servers that repeat rows are merged by official row ID; servers that ignore
offsets are recovered from alternative sort orders or by requesting the
manifest's IDs in small batches. Anything that cannot be proven complete
raises UnverifiablePaginationError, and the partial rows are kept in
staging (never activated). Also here: the official URLs used, and helpers
that read, flatten and merge rows of official payloads.

Used by: utils/official_source.py, utils/store.py.
"""

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse
import hashlib
import json
import logging
import re

from utils.errors import UnverifiablePaginationError


_LOG = logging.getLogger("sage.store")  # same logger as utils/store.py

TABLE_ID_RE = re.compile(r"^(?:[A-Z][A-Z0-9]{1,18}[0-9][A-Z0-9]*|[0-9]{3,20})$")


PAGE_SIZE = 2500


MAX_PAGES = 100


MAX_ROWS = PAGE_SIZE * MAX_PAGES


# SingStat supports selecting explicit seriesNo/rowNo identifiers. If a
# particular server response ignores offset pagination, request independently
# verified metadata identifiers in small bounded groups instead of guessing
# where the next page starts. Keep batches below observed server-side caps.
SERIES_BATCH_SIZE = 20


def validated_id(value):
    """Return a validated, upper-case SingStat table ID, or raise ValueError."""
    text = str(value or "").strip().upper()
    if not TABLE_ID_RE.fullmatch(text):
        raise ValueError("An invalid SingStat table ID was supplied.")
    return text


def table_url(table_id, *, offset: int = 0, limit=PAGE_SIZE) -> str:
    """Official API URL for one page of a table's data."""
    table_id = validated_id(table_id)
    return (
        "https://tablebuilder.singstat.gov.sg/api/table/tabledata/"
        f"{table_id}?limit={int(limit)}&offset={int(offset)}"
    )


def alternate_order_url(table_id, sort_by: str = "seriesNo desc") -> str:
    """One bounded recovery attempt when the server ignores row offsets.

    This option is documented only for time-series tables. If unsupported,
    retain the ordinary page as pending instead of treating it as complete.
    """
    if sort_by not in {"seriesNo desc", "seriesNo asc", "rowText asc", "rowText desc"}:
        raise ValueError("Unrecognised official sort mode.")
    query = urlencode({"limit": 3000, "offset": 0, "sortBy": sort_by})
    return (
        "https://tablebuilder.singstat.gov.sg/api/table/tabledata/"
        f"{validated_id(table_id)}?{query}"
    )


def metadata_url(table_id):
    """SingStat's separate official series manifest for completeness checks."""
    return "https://tablebuilder.singstat.gov.sg/api/table/metadata/" + validated_id(table_id)


def _metadata_manifest(payload, table_id) -> tuple:
    """Extract only verifiable row-count/ID claims from the official manifest.

    A missing or malformed manifest must never serve as evidence that a short
    table-data response is complete. Cross-sectional metadata can omit row IDs.
    """
    data = payload.get("Data") if isinstance(payload, dict) else None
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, dict):
        raise ValueError("Official table metadata has no series manifest.")
    metadata_id = str(records.get("id") or table_id).strip().upper()
    if metadata_id != table_id:
        raise ValueError("Metadata table ID differs from requested dataset.")
    try:
        count = int(records.get("total"))
    except (TypeError, ValueError) as error:
        raise ValueError("Metadata does not contain a valid full row count.") from error
    if count <= 0 or count > MAX_ROWS:
        raise ValueError("Metadata row count is outside the supported range.")
    listed_rows = records.get("row")
    identifiers = None
    if isinstance(listed_rows, list) and listed_rows:
        # Metadata often contains a complete series list; use it as a second
        # check when all listed entries have unique identifiers.
        ids = [
            str(item.get("seriesNo") or item.get("rowNo") or "").strip()
            for item in listed_rows
            if isinstance(item, dict)
        ]
        if len(listed_rows) == count and len(ids) == count and all(ids):
            if len(ids) != len(set(ids)):
                raise ValueError("Official metadata contains duplicate series IDs.")
            # Preserve metadata order.  It is useful when rebuilding a whole
            # table from disjoint filtered requests, and lets us reproduce the
            # same hierarchy as the official full-table endpoint.
            identifiers = tuple(ids)
    return count, identifiers


def _observation_manifest(payload, table_id):
    """Identify time-series metadata where total describes DATA POINTS, not series.

    Only the specific evidence pattern is eligible: a named, unique series list,
    and a total larger than that list. Coverage is not accepted here; all IDs,
    historical observations and revision must also validate at fetch time.
    In particular, we do not apply this shortcut to cubes or age tables with
    suspiciously small official metadata lists.
    """
    data = payload.get("Data") if isinstance(payload, dict) else None
    rec = data.get("records") if isinstance(data, dict) else None
    if not isinstance(rec, dict):
        return None
    mid = str(rec.get("id") or "").strip().upper()
    if mid != table_id:
        raise ValueError("Metadata table ID differs from requested dataset.")
    rows = rec.get("row")
    if not isinstance(rows, list) or not rows:
        return None
    # This branch applies ONLY to named time-series. Multidimensional cubes
    # need their own row/column coverage proof.
    if any(not isinstance(row, dict) or not row.get("seriesNo") for row in rows):
        return None
    ids = tuple(str(row["seriesNo"]).strip() for row in rows)
    if not all(ids) or len(set(ids)) != len(ids):
        return None
    try:
        observations = int(rec["total"])
    except (KeyError, TypeError, ValueError):
        return None
    if observations <= len(ids) or observations > MAX_ROWS * PAGE_SIZE:
        return None
    return observations, ids, {str(row["seriesNo"]).strip(): row for row in rows}, rec


def _checked_observation_candidate(
    table_id, template, rows, expected_observations, expected_ids, metadata_rows, metadata_record
):
    """Return a validated full-table candidate or None; never guess totals."""
    if (
        not isinstance(template, dict)
        or not isinstance(rows, list)
        or len(rows) != len(expected_ids)
    ):
        return None
    data = template.get("Data")
    if not isinstance(data, dict) or str(data.get("id") or "").upper() != table_id:
        return None
    if (
        metadata_record.get("frequency")
        and data.get("frequency")
        and metadata_record["frequency"] != data["frequency"]
    ):
        return None
    by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        ident = _row_id(row)
        if not ident or ident in by_id or ident not in metadata_rows:
            return None
        expected = metadata_rows[ident]
        if (
            expected.get("rowText")
            and str(expected["rowText"]).strip() != str(row.get("rowText") or "").strip()
        ):
            return None
        if expected.get("uoM") and row.get("uoM") and expected["uoM"] != row["uoM"]:
            return None
        period_paths = [path for path, _ in _cells_from_columns(row.get("columns", []))]
        if not period_paths or len(period_paths) != len(set(period_paths)):
            return None
        by_id[ident] = row
    if set(by_id) != set(expected_ids):
        return None
    combined = _partial_payload(template, [by_id[ident] for ident in expected_ids])
    actual = len(_flatten(combined))
    if actual != expected_observations:
        return None
    # IMPORTANT: preserve the original reported observation total in the raw
    # official response. Return the number of verified SERIES separately for
    # snapshots.reported_total; it is not the number of observations.
    return combined, len(expected_ids)


def _manifest_series_url(table_id, identifiers) -> str:
    """Create a bounded official request for *only* known metadata series IDs."""
    if not identifiers or len(identifiers) > SERIES_BATCH_SIZE:
        raise ValueError("Invalid manifest-series request size.")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", item) for item in identifiers):
        raise ValueError("Unrecognised series identifier in official metadata.")
    query = urlencode({"seriesNoORrowNo": ",".join(identifiers), "limit": PAGE_SIZE, "offset": 0})
    return (
        "https://tablebuilder.singstat.gov.sg/api/table/tabledata/"
        f"{validated_id(table_id)}?{query}"
    )


def extract_table_id(url):
    """The table ID in an official API URL, or None for any other URL."""
    parsed = urlparse(url)
    prefix = "/api/table/tabledata/"
    if parsed.scheme != "https" or parsed.hostname != "tablebuilder.singstat.gov.sg":
        return None
    if not parsed.path.startswith(prefix):
        return None
    name = parsed.path[len(prefix) :]
    if "/" in name or not TABLE_ID_RE.fullmatch(name):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    # Only full, unfiltered table requests can be represented by a full snapshot.
    if set(query) - {"limit", "offset"} or int(query.get("offset", [0])[0]) != 0:
        return None
    return name


def _cells_from_columns(columns, prefix=()):
    """Yield (dimension path, value) for every leaf of a row's nested official columns; malformed
    columns raise ValueError."""
    if not isinstance(columns, list):
        raise ValueError("Official table columns are not a list.")
    for entry in columns:
        if not isinstance(entry, dict):
            raise ValueError("Official table contains a malformed column.")
        part = str(entry.get("key", "")).strip()
        path = prefix + ((part,) if part else ())
        if "value" in entry:
            yield path, entry["value"]
        if "columns" in entry:
            yield from _cells_from_columns(entry["columns"], path)


def _flatten(payload):
    """All observations of a payload as (row ID, label, unit, path JSON, value JSON) tuples.

    Raises ValueError when the table is empty, malformed, repeats a series ID
    or has rows but no values (an incomplete snapshot is never saved).
    """
    data = payload.get("Data")
    if not isinstance(data, dict) or not isinstance(data.get("row"), list) or not data["row"]:
        raise ValueError("SingStat did not return a nonempty official table.")
    rows = data["row"]
    identity_seen = set()
    observations = []
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Malformed official table row.")
        row_id = str(row.get("seriesNo") or row.get("rowNo") or "").strip()
        if row_id:
            if row_id in identity_seen:
                raise ValueError("Duplicate official series/row identifier in downloaded table.")
            identity_seen.add(row_id)
        else:
            # Preserve row positions for cubes without public IDs; no guessing IDs.
            row_id = f"row_{position}"
        label = str(row.get("rowText") or "").strip()
        unit = str(row.get("uoM") or row.get("unit") or "").strip()
        for path, value in _cells_from_columns(row.get("columns", [])):
            raw_value = json.dumps(value, ensure_ascii=False, allow_nan=False)
            observations.append(
                (row_id, label, unit, json.dumps(path, ensure_ascii=False), raw_value)
            )
    if not observations:
        raise ValueError("Official table has rows but no values; refusing incomplete snapshot.")
    return observations


def _reported_total(payload):
    """The table's own row total (Data.total) as an int, or None when missing or not a whole number."""
    data = payload.get("Data", {})
    value = data.get("total")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _row_id(row) -> str:
    """Only genuine official identities may be used to merge downloaded rows."""
    return str(row.get("seriesNo") or row.get("rowNo") or "").strip()


def _partial_payload(first, rows):
    """Make a standalone copy so failures never mutate an HTTP response."""
    if first is None:
        return None
    result = dict(first)
    result["Data"] = dict(first["Data"])
    result["Data"]["row"] = list(rows)
    result["Data"]["offset"] = 0
    result["Data"]["limit"] = len(rows)
    return result


def _merge_columns(previous, incoming):
    """Keep historical column periods; a newer value wins for the same path.

    The operation is recursive to retain SingStat's multidimensional columns.
    Never merge anonymous columns, which have no stable period/category key.
    """
    old = [dict(c) for c in previous]
    positions = {str(c.get("key")): i for i, c in enumerate(old) if c.get("key") is not None}
    if len(positions) != len(old):
        raise ValueError("Old statistical columns have missing/duplicate keys.")
    seen = set()
    for entry in incoming:
        if not isinstance(entry, dict) or entry.get("key") is None:
            raise ValueError("New statistical columns have no stable key.")
        key = str(entry["key"])
        if key in seen:
            raise ValueError("Duplicate new statistical period/category key.")
        seen.add(key)
        if key not in positions:
            positions[key] = len(old)
            old.append(dict(entry))
            continue
        target = old[positions[key]]
        if "columns" in entry or "columns" in target:
            if not isinstance(entry.get("columns"), list) or not isinstance(
                target.get("columns"), list
            ):
                raise ValueError("Incompatible nested column schema; merge rejected.")
            target["columns"] = _merge_columns(target["columns"], entry["columns"])
        elif "value" in entry:
            target["value"] = entry["value"]  # the fresh official release wins
        else:
            raise ValueError("New column is missing its statistical value.")
    return old


def _merge_row(old, new):
    """Merge two copies of the SAME official row (same ID and label) from overlapping pages;
    conflicting values raise ValueError."""
    if _row_id(old) != _row_id(new) or not _row_id(old):
        raise ValueError("Cannot merge observations for different series IDs.")
    if str(old.get("rowText") or "").strip() != str(new.get("rowText") or "").strip():
        raise ValueError("Official series description changed; manual review required.")
    for unit in ("uoM", "unit"):
        if old.get(unit) and new.get(unit) and old[unit] != new[unit]:
            raise ValueError("Official series units changed; merge rejected.")
    combined = dict(old)
    combined.update({k: v for k, v in new.items() if k != "columns"})
    combined["columns"] = _merge_columns(old.get("columns", []), new.get("columns", []))
    return combined


@dataclass
class _PagedDownload:
    """Progress of one paged table download (see LocalStore._fetch_complete)."""

    metadata_count: int | None  # rows the metadata manifest promises (None = no manifest)
    metadata_ids: tuple | None  # series IDs it lists, when complete and unique
    expected: int | None  # rows we must end with (may come from Data.total)
    rows: list = field(default_factory=list)
    seen: set = field(default_factory=set)  # official row IDs already downloaded
    first: dict | None = None  # first page payload (its metadata is kept)


def _read_metadata_manifest(table_id, fetch_metadata):
    """(observation manifest, row count, series IDs) from the metadata endpoint.

    The endpoint is optional (some networks block it), so failures give
    (None, None, None). A manifest that names a DIFFERENT table is a data-
    integrity failure and is raised, never ignored.
    """
    if fetch_metadata is None:
        return None, None, None
    try:
        meta_response = fetch_metadata(metadata_url(table_id))
        meta_response.raise_for_status()
        metadata_body = meta_response.json()
        observation_manifest = _observation_manifest(metadata_body, table_id)
        if observation_manifest is not None:
            return observation_manifest, None, None
        count, ids = _metadata_manifest(metadata_body, table_id)
        return None, count, ids
    except ValueError as error:
        if "Metadata table ID differs" in str(error):
            raise
        return None, None, None
    except Exception:  # noqa: BLE001 - the metadata endpoint is optional (often blocked)
        return None, None, None


def _reconcile_reported_total(download, payload, rows, page_num) -> None:
    """Use the page's Data.total, unless the metadata manifest already decides.

    On some endpoints Data.total is the size of the CURRENT PAGE, not the
    table: that is recognised and then ignored. A total that changes during
    the download, or exceeds the manifest, is rejected.
    """
    reported = _reported_total(payload)
    if reported is None:
        return
    if download.metadata_count is not None:
        if reported > download.metadata_count:
            raise ValueError("Data row count exceeds official series metadata.")
        return
    if page_num == 0:
        download.expected = reported
    elif download.expected is not None and reported != download.expected:
        first_size = len(download.first["Data"]["row"]) if download.first else 0
        if download.expected == first_size and reported == len(rows):
            download.expected = None
        else:
            raise ValueError("Official total changed during pagination; refresh rejected.")


def _merge_overlapping_page(download, rows) -> bool:
    """Merge a page that repeats earlier rows but adds new ones. True = download done."""
    by_id = {_row_id(row): pos for pos, row in enumerate(download.rows)}
    for row in rows:
        ident = _row_id(row)
        if ident in by_id:
            download.rows[by_id[ident]] = _merge_row(download.rows[by_id[ident]], row)
        else:
            by_id[ident] = len(download.rows)
            download.seen.add(ident)
            download.rows.append(row)
    if len(download.rows) > MAX_ROWS:
        raise ValueError("Merged pages exceed the safe official row limit.")
    if download.metadata_count is None and download.expected == len(download.first["Data"]["row"]):
        # More distinct IDs than page one's "total": that total was a per-page cap.
        download.expected = None
    if download.metadata_count is not None and len(download.rows) == download.metadata_count:
        return True
    if download.metadata_count is not None and len(download.rows) > download.metadata_count:
        raise ValueError("Merged rows exceed the independently confirmed total.")
    return False


def _page_completes_download(download, rows, page_num) -> bool:
    """True when this page ends the download (empty page, or expected total reached).

    Without independent metadata, a short first page whose "total" equals its
    own size may only be the server's per-request cap (one server returned 55
    rows despite a larger limit), so one more page is requested to be sure.
    """
    suspicious_server_cap = (
        download.metadata_count is None
        and page_num == 0
        and download.expected == len(download.rows)
        and len(download.rows) < PAGE_SIZE
    )
    if not rows or (
        download.expected is not None
        and len(download.rows) == download.expected
        and not suspicious_server_cap
    ):
        if download.expected is not None and len(download.rows) != download.expected:
            raise ValueError("Not all officially reported rows were downloaded.")
        return True
    return False


def _verified_download(table_id, download) -> tuple:
    """Final checks against the manifest; the combined payload and expected count."""
    if download.metadata_count is not None and len(download.rows) != download.metadata_count:
        raise UnverifiablePaginationError(
            f"{table_id}: downloaded {len(download.rows)} rows but the independent "
            f"official metadata lists {download.metadata_count}; no incomplete "
            "snapshot will be activated.",
            partial=_partial_payload(download.first, download.rows),
            expected=download.metadata_count,
            authoritative_total=True,
            expected_ids=download.metadata_ids,
        )
    if download.metadata_ids is not None and download.seen != set(download.metadata_ids):
        raise UnverifiablePaginationError(
            f"{table_id}: the downloaded series IDs do not match the official "
            "metadata manifest; local snapshot rejected.",
            partial=_partial_payload(download.first, download.rows),
            expected=download.metadata_count,
            authoritative_total=True,
            expected_ids=download.metadata_ids,
        )
    combined = dict(download.first)
    combined["Data"] = dict(download.first["Data"])
    combined["Data"]["row"] = download.rows
    combined["Data"]["offset"] = 0
    combined["Data"]["limit"] = len(download.rows)
    return combined, download.expected


# --- Content fingerprints (used to detect changed official releases) ----------


def _serialized(payload):
    """Canonical JSON bytes of a payload (sorted keys, no NaN): the input to the snapshot hash."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(payload):
    """SHA-256 hex digest of serialized payload bytes (integrity check of a saved snapshot)."""
    return hashlib.sha256(payload).hexdigest()


def _revision(payload):
    """An official table revision is needed before merging separate downloads."""
    data = payload.get("Data", {})
    # dateGenerated may describe the current HTTP request, not a statistical
    # release. Never combine separate requests on that timestamp alone.
    for name in ("dataLastUpdated", "lastUpdated"):
        value = data.get(name)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _table_signature(payload):
    """Prevent merging similarly numbered series from incompatible table layouts."""
    data = payload.get("Data", {})
    return _hash(
        _serialized({key: data.get(key) for key in ("id", "title", "frequency", "datasource")})
    )


class TableDownloader:
    """The download methods of utils.store.LocalStore (a mixin).

    Kept apart from the store's SQLite bookkeeping so the completeness rules
    can be read, tested and changed on their own. The class using it must
    provide ``_retain_pending(table_id, partial, expected, authoritative=...)``,
    which keeps a partial download in staging.
    """

    def _recover_sorted_slices(
        self, table_id, fetch_page, first, rows, metadata_count=None
    ) -> list:
        """Download/merge bounded complementary sort views of a broken endpoint.

        Every distinct row is preserved by series ID, even if the table lacks
        a full series manifest. Progress is useful but is NOT completeness.
        """
        by_id = {_row_id(row): row for row in rows}
        if len(by_id) != len(rows) or not all(by_id):
            raise ValueError("Cannot merge sorted pages without unique official IDs.")
        for order in ("seriesNo desc", "rowText asc", "rowText desc", "seriesNo asc"):
            try:
                response = fetch_page(alternate_order_url(table_id, order))
                response.raise_for_status()
                alternative = response.json()
                data = alternative.get("Data") if isinstance(alternative, dict) else None
                if not isinstance(data, dict) or not isinstance(data.get("row"), list):
                    continue
                if str(data.get("id") or table_id).upper() != table_id:
                    continue
                if _revision(first) != _revision(alternative) or _table_signature(
                    first
                ) != _table_signature(alternative):
                    continue  # Do not merge different publication revisions/layouts.
                # Validate the ENTIRE optional response before using any of
                # its rows. An invalid last row must not leave a half-merged
                # sort page in the verified candidate.
                page_ids = set()
                candidate = dict(by_id)
                for row in data["row"]:
                    ident = _row_id(row) if isinstance(row, dict) else ""
                    if not ident or ident in page_ids:
                        raise ValueError("Duplicate or absent ID in alternate response.")
                    page_ids.add(ident)
                    if ident in candidate:
                        candidate[ident] = _merge_row(candidate[ident], row)
                    else:
                        candidate[ident] = row
                if len(candidate) > MAX_ROWS or (
                    metadata_count is not None and len(candidate) > metadata_count
                ):
                    raise ValueError(
                        "Sorted response exceeds the independently confirmed row " "count."
                    )
                by_id = candidate
                if metadata_count is not None and len(by_id) == metadata_count:
                    break
            except (ValueError, TypeError, KeyError, AttributeError):
                continue  # An unsupported sort must not discard prior good rows.
            except Exception as error:  # noqa: BLE001 - optional endpoint; keep earlier rows
                _LOG.debug("Optional sort endpoint skipped: %s", type(error).__name__)
                continue
        if len(by_id) > MAX_ROWS:
            raise ValueError("Merged official series exceed the safe row limit.")
        return list(by_id.values())

    def _fetch_by_manifest_ids(self, table_id, fetch_page, metadata_count, metadata_ids) -> tuple:
        """Recover a full table with independently enumerated official IDs.

        This is NOT an alternative way to declare a clipped download complete.
        Every metadata identifier must appear exactly once in the corresponding
        filtered response; unfiltered, mis-filtered or repeated data is rejected.
        Nothing is persisted until the caller's full staging checks succeed.
        """
        if (
            metadata_ids is None
            or len(metadata_ids) != metadata_count
            or len(set(metadata_ids)) != metadata_count
        ):
            raise UnverifiablePaginationError(
                f"{table_id}: offsets repeat and official metadata did not "
                "enumerate every expected series. The dataset cannot be "
                "reconstructed safely; existing SQLite data was preserved."
            )
        recovered = []
        seen = set()
        template = None
        for start in range(0, metadata_count, SERIES_BATCH_SIZE):
            requested = metadata_ids[start : start + SERIES_BATCH_SIZE]
            response = fetch_page(_manifest_series_url(table_id, requested))
            response.raise_for_status()
            payload = response.json()
            data = payload.get("Data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("row"), list):
                raise UnverifiablePaginationError(
                    f"{table_id}: filtered official API returned malformed data; "
                    "the old snapshot remains unchanged."
                )
            response_id = str(data.get("id") or table_id).strip().upper()
            if response_id != table_id:
                raise ValueError("Filtered official response identifies another table.")
            rows = data["row"]
            found = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Malformed official row in filtered recovery.")
                identifier = str(row.get("seriesNo") or row.get("rowNo") or "").strip()
                if not identifier or identifier in seen or identifier in found:
                    raise UnverifiablePaginationError(
                        f"{table_id}: missing/duplicate series ID in filtered "
                        "recovery; incomplete data will not be activated."
                    )
                found.append(identifier)
            if len(rows) != len(requested) or set(found) != set(requested):
                raise UnverifiablePaginationError(
                    f"{table_id}: the API did not honour explicit series "
                    f"selection (batch {start // SERIES_BATCH_SIZE + 1}). "
                    "Existing verified data is safe; contact SingStat if "
                    "the official endpoint continues to ignore filters."
                )
            # Filtered batches are separate HTTP calls. Reject mixing
            # different official publication revisions or table definitions.
            if template is not None and (
                _revision(template) != _revision(payload)
                or _table_signature(template) != _table_signature(payload)
            ):
                raise UnverifiablePaginationError(
                    f"{table_id}: filtered batches came from different "
                    "revisions or statistical definitions; no new snapshot activated."
                )
            seen.update(found)
            recovered.extend(rows)
            if template is None:
                template = payload
        if len(recovered) != metadata_count or seen != set(metadata_ids):
            raise UnverifiablePaginationError(
                f"{table_id}: the recovered rows do not cover the official "
                "metadata manifest. No new dataset has been activated."
            )
        # A batch may be returned in arbitrary order: reconstruct the original
        # official metadata hierarchy, not the order of HTTP response arrival.
        by_id = {str(row.get("seriesNo") or row.get("rowNo")).strip(): row for row in recovered}
        combined = dict(template)
        combined["Data"] = dict(template["Data"])
        combined["Data"]["row"] = [by_id[item] for item in metadata_ids]
        combined["Data"]["offset"] = 0
        combined["Data"]["limit"] = len(metadata_ids)
        combined["Data"]["total"] = metadata_count
        return combined, metadata_count

    def _fetch_observation_manifest(self, table_id, fetch_page, manifest):
        """Recover TS where SingStat metadata.total counts historical records.

        Standard `offset=len(series)` incorrectly repeats records when the
        endpoint paginates observations. Use sorted views if needed, then
        individual explicitly named series, with exact independent count and
        metadata-ID checks. Never activate on an ID match alone.
        """
        observation_total, identifiers, metadata_rows, metadata_record = manifest
        first = None
        collected = []
        try:
            response = fetch_page(table_url(table_id))
            response.raise_for_status()
            first = response.json()
            data = first.get("Data") if isinstance(first, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("row"), list):
                raise ValueError("Official table-data endpoint returned malformed data.")
            if str(data.get("id") or "").upper() != table_id:
                raise ValueError("Table-data ID does not match metadata ID.")
            collected = data["row"]
            for row in collected:
                if not isinstance(row, dict) or not _row_id(row):
                    raise ValueError("Missing series IDs in initial time-series response.")
            if len(set(_row_id(row) for row in collected)) != len(collected):
                raise ValueError("Duplicated IDs within initial time-series response.")
            if not set(_row_id(row) for row in collected).issubset(set(identifiers)):
                raise ValueError("Time-series data contained IDs absent from metadata.")

            checked = _checked_observation_candidate(
                table_id,
                first,
                collected,
                observation_total,
                identifiers,
                metadata_rows,
                metadata_record,
            )
            if checked:
                return checked
            # A larger sorted response may bypass a per-request observation cap
            # (seen on one official table: 13 rows normally vs 15 with limit=3000).
            recovered = self._recover_sorted_slices(
                table_id, fetch_page, first, collected, len(identifiers)
            )
            collected = recovered
            checked = _checked_observation_candidate(
                table_id,
                first,
                collected,
                observation_total,
                identifiers,
                metadata_rows,
                metadata_record,
            )
            if checked:
                return checked

            # A sorted page can still omit history or whole series. Request
            # each metadata-named series separately; small batches avoid the
            # same observation-limit issue encountered by bulk requests.
            recovered = []
            template = None
            for ident in identifiers:
                response = fetch_page(_manifest_series_url(table_id, [ident]))
                response.raise_for_status()
                payload = response.json()
                page = payload.get("Data") if isinstance(payload, dict) else None
                if not isinstance(page, dict) or str(page.get("id") or "").upper() != table_id:
                    raise ValueError("Filtered request returned an unexpected table.")
                if (
                    not isinstance(page.get("row"), list)
                    or len(page["row"]) != 1
                    or (_row_id(page["row"][0]) != ident)
                ):
                    raise ValueError("SingStat did not honour exact single-series filter.")
                if _revision(first) != _revision(payload) or _table_signature(
                    first
                ) != _table_signature(payload):
                    raise ValueError("Filtered series belonged to a different table revision.")
                if template is None:
                    template = payload
                recovered.append(page["row"][0])
            checked = _checked_observation_candidate(
                table_id,
                template,
                recovered,
                observation_total,
                identifiers,
                metadata_rows,
                metadata_record,
            )
            if checked:
                return checked
            collected = recovered
            raise ValueError(
                f"Named series are present but historical observations do not "
                f"match metadata total={observation_total}; incomplete snapshot rejected."
            )
        except Exception as exc:  # noqa: BLE001 - keep the partial download in staging, then raise
            # Staging is for audit/recovery only. We explicitly do NOT give
            # _complete_pending an authoritative row count; it does not know
            # the independent historical observation requirement.
            if first is not None and collected:
                partial = _partial_payload(first, collected)
                raise UnverifiablePaginationError(
                    f"{table_id}: observation-total verification failed "
                    f"({exc}); retained partial series, no new activation.",
                    partial=partial,
                    expected=None,
                    authoritative_total=False,
                ) from exc
            raise

    def _fetch_complete(self, table_id, fetch_page, fetch_metadata=None):
        """Download a whole table, proving it is complete; return (payload, expected rows).

        A short page is NOT proof of a whole dataset, so completeness is proven
        in one of these ways (strongest first):

        1. an observation manifest from the separate metadata endpoint
           (_fetch_observation_manifest);
        2. a row count / series-ID manifest from that endpoint, which the
           paged download must match exactly;
        3. without a manifest: the table-data endpoint's own reported total,
           or a proper empty terminal page.

        Pages are requested by offset (PAGE_SIZE rows each). Servers that
        repeat rows across pages are handled by merging on the official row
        ID; servers that ignore offsets are recovered from alternative sort
        orders or by requesting the manifest's IDs directly. Anything that
        cannot be proven complete raises (UnverifiablePaginationError keeps
        the partial download in staging); verified data is never replaced
        by an incomplete download.
        """
        table_id = validated_id(table_id)
        observation_manifest, metadata_count, metadata_ids = _read_metadata_manifest(
            table_id, fetch_metadata
        )
        # Retrieval errors must NEVER be swallowed by the optional metadata
        # step above: a proven mismatch must not fall back to the offset loop.
        if observation_manifest is not None:
            return self._fetch_observation_manifest(table_id, fetch_page, observation_manifest)
        download = _PagedDownload(
            metadata_count=metadata_count, metadata_ids=metadata_ids, expected=metadata_count
        )
        for page_num in range(MAX_PAGES):
            offset = len(download.rows)
            payload = self._fetch_checked_page(table_id, fetch_page, download, offset)
            rows = payload["Data"]["row"]
            _reconcile_reported_total(download, payload, rows, page_num)
            if not rows and page_num == 0:
                raise ValueError(f"Official table {table_id} returned no records.")
            if len(rows) > PAGE_SIZE:
                raise ValueError(
                    "Official API ignored the page size; unable to verify " "completeness."
                )
            if download.first is None:
                download.first = payload
            page_ids = [_row_id(row) if isinstance(row, dict) else "" for row in rows]
            if page_ids and all(page_ids) and len(page_ids) != len(set(page_ids)):
                raise ValueError("Official API returned duplicate IDs within one page.")
            # Some servers repeat the last rows of one page at the start of the
            # next. Merge such a page by official ID (it still makes progress).
            if (
                page_ids
                and all(page_ids)
                and download.seen.intersection(page_ids)
                and set(page_ids) - download.seen
            ):
                if _merge_overlapping_page(download, rows):
                    break
                continue
            recovered = self._append_page(table_id, fetch_page, download, rows, offset)
            if recovered is not None:
                return recovered
            if _page_completes_download(download, rows, page_num):
                break
        else:
            raise ValueError("Pagination limit reached before download completed.")
        return _verified_download(table_id, download)

    def _fetch_checked_page(self, table_id, fetch_page, download, offset):
        """One page at ``offset``, checked to be a table payload for ``table_id``.

        A connection may fail AFTER good pages: those are kept in staging
        for a later run (never activated), then the error is re-raised.
        """
        try:
            response = fetch_page(table_url(table_id, offset=offset, limit=PAGE_SIZE))
            response.raise_for_status()
        except Exception:  # noqa: BLE001 - keep good pages in staging, then re-raise
            if download.rows:
                self._retain_pending(
                    table_id,
                    _partial_payload(download.first, download.rows),
                    download.metadata_count,
                    authoritative=download.metadata_count is not None,
                )
            raise
        payload = response.json()
        data = payload.get("Data")
        if not isinstance(data, dict) or not isinstance(data.get("row"), list):
            raise ValueError(f"SingStat returned a non-table payload for {table_id}.")
        if str(data.get("id") or table_id).strip().upper() != table_id:
            raise ValueError("Downloaded response ID differs from the requested table.")
        return payload

    def _append_page(self, table_id, fetch_page, download, rows, offset):
        """Append a page's rows; on a repeated row ID, switch to a recovery method.

        Returns a finished (payload, expected) when recovery completed the
        table, else None. Raises when the table cannot be proven complete.
        """
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("A malformed table row was returned.")
            identity = str(row.get("seriesNo") or row.get("rowNo") or "").strip()
            if identity:
                if identity in download.seen:
                    return self._recover_from_repeated_ids(table_id, fetch_page, download, offset)
                download.seen.add(identity)
            download.rows.append(row)
        if len(download.rows) > MAX_ROWS:
            raise ValueError("Official table exceeds the configured safe row limit.")
        if download.expected is not None and len(download.rows) > download.expected:
            # Some endpoints report an observation count instead of a row
            # count as Data.total; a mismatch is never silently accepted.
            raise ValueError("Fetched rows exceed the official reported total.")
        return None

    def _recover_from_repeated_ids(self, table_id, fetch_page, download, offset):
        """The server ignored the offset (rows repeat): rebuild the table another way.

        With a series-ID manifest: request exactly those IDs. Without one:
        merge several official sort orders; every distinct row is kept in
        staging, but the table is activated only if the manifest count is met.
        """
        metadata_count = download.metadata_count
        if download.metadata_ids is not None:
            try:
                return self._fetch_by_manifest_ids(
                    table_id, fetch_page, metadata_count, download.metadata_ids
                )
            except Exception:  # noqa: BLE001 - keep the partial download in staging, then re-raise
                self._retain_pending(
                    table_id,
                    _partial_payload(download.first, download.rows),
                    metadata_count,
                    authoritative=True,
                )
                raise
        recovered_rows = self._recover_sorted_slices(
            table_id, fetch_page, download.first, download.rows, metadata_count
        )
        if metadata_count is not None and len(recovered_rows) == metadata_count:
            combined = _partial_payload(download.first, recovered_rows)
            combined["Data"]["total"] = metadata_count
            _flatten(combined)
            return combined, metadata_count
        raise UnverifiablePaginationError(
            f"{table_id}: Repeated series IDs at offset {offset}; "
            "official metadata lacks a complete series-ID "
            "manifest. "
            f"Downloaded/merged {len(recovered_rows)} distinct official "
            "series into persistent staging; full-table coverage "
            "remains unconfirmed. Previous verified data is intact.",
            partial=_partial_payload(download.first, recovered_rows),
            expected=metadata_count,
            authoritative_total=metadata_count is not None,
        )
