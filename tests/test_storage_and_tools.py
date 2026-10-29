"""Tests for the local data store, the table tools, the knowledge base and the notes catalogue.

Everything runs against temporary SQLite files and a fake SingStat server, so
no network, OpenAI key or real data folder is touched.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)

from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
import pandas as pd  # noqa: E402
import requests  # noqa: E402

from tests.fixtures.real_answers import TABLES  # noqa: E402
from utils import kb, rag_catalog, store, table_tools  # noqa: E402
from utils.errors import UnverifiablePaginationError  # noqa: E402


class FakeResponse:
    """The parts of requests.Response the store uses."""

    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return json.loads(json.dumps(self.body))


def fake_server(rows, *, page_cap=None, ignore_offset=False):
    """A fake tabledata endpoint serving ``rows`` page by page (with a true total)."""
    calls = []

    def fetch_page(url):
        calls.append(url)
        query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        offset = 0 if ignore_offset else int(query.get("offset", 0))
        limit = min(int(query.get("limit", 10**6)), page_cap or 10**6)
        return FakeResponse(
            {
                "Data": {
                    "id": "M810001",
                    "title": "Test Table",
                    "total": len(rows),
                    "row": rows[offset : offset + limit],
                }
            }
        )

    return fetch_page, calls


def official_rows(count):
    """``count`` official-looking rows with one annual value each."""
    return [
        {
            "seriesNo": str(i),
            "rowText": f"Series {i}",
            "uoM": "Number",
            "columns": [{"key": "2023", "value": str(i * 10)}],
        }
        for i in range(1, count + 1)
    ]


class LocalStoreTests(unittest.TestCase):
    """The store activates a table only after proving the download is complete."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store.LocalStore(Path(self.tmp.name) / "cache.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_download_is_saved_and_reused(self):
        fetch, calls = fake_server(official_rows(8))
        payload = self.store.get_or_fetch("M810001", fetch)
        self.assertEqual(len(payload["Data"]["row"]), 8)
        downloads = len(calls)  # a short first page is followed by one confirming request
        again = self.store.get_or_fetch("M810001", fetch)  # from the saved copy
        self.assertEqual(again["Data"]["row"], payload["Data"]["row"])
        self.assertEqual(len(calls), downloads)
        self.assertIsNotNone(self.store.table_cache_age_seconds("M810001"))

    def test_offline_mode_never_downloads(self):
        fetch, calls = fake_server(official_rows(3))
        with self.assertRaises(ValueError):
            self.store.get_or_fetch("M810001", fetch, offline=True)
        self.assertEqual(calls, [])

    def test_unprovable_download_is_staged_not_activated(self):
        fetch, _ = fake_server(official_rows(12), page_cap=5, ignore_offset=True)
        with self.assertRaises((UnverifiablePaginationError, ValueError)):
            self.store.stage("M810001", fetch)
        self.assertIsNone(self.store.get_table("M810001"))

    def test_status_verify_and_backup(self):
        fetch, _ = fake_server(official_rows(4))
        self.store.get_or_fetch("M810001", fetch)
        status = self.store.status()
        self.assertIn("M810001", json.dumps(status, default=str))
        report = self.store.verify_all()  # raises if any checksum or structure is wrong
        self.assertEqual(report["sqlite_integrity"], "ok")
        self.assertEqual(report["verified_tables"], 1)
        backup = self.store.backup(Path(self.tmp.name) / "backup.sqlite3")
        self.assertTrue(Path(backup).is_file())

    def test_cached_responses_are_validated(self):
        url = "https://tablebuilder.singstat.gov.sg/api/table/resourceid?keyword=x"
        fetch = lambda _url: FakeResponse({"Data": {"records": [1, 2]}})  # noqa: E731
        first = self.store.cache_response(url, fetch, validator=lambda body: "Data" in body)
        self.assertEqual(first["Data"]["records"], [1, 2])
        self.assertEqual(self.store.get_cached_response(url, validator=lambda body: True), first)
        self.assertIsNone(
            self.store.get_cached_response(url + "&other=1", validator=lambda body: True)
        )


def table_frame(table_id, numbers):
    """The DataFrame shape retrieve_generic_singstat_table returns, from fixture data."""
    title, unit, series = TABLES[table_id]
    rows = [
        {
            "series_no": n,
            "series_name": series[n][0],
            "unit": unit,
            "period": p,
            "year": int(p[:4]),
            "value": v,
        }
        for n in numbers
        for p, v in zip(series[n][1], series[n][2])
    ]
    meta = {
        "table_id": table_id,
        "title": title,
        "table_complete": True,
        "frequency": "Annual",
        "source_url": f"https://tablebuilder.singstat.gov.sg/table/TS/{table_id}",
    }
    return pd.DataFrame(rows), meta


class TableToolTests(unittest.TestCase):
    """The four model tools, on the real R&D table (no network)."""

    def setUp(self):
        frame, meta = table_frame("M081331", ["2", "3", "4", "1.1", "1.3"])
        patcher = mock.patch.object(
            table_tools, "retrieve_generic_singstat_table", return_value=(frame, meta)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_inspect_lists_every_series(self):
        result = table_tools.inspect_singstat_table("M081331")
        self.assertTrue(result["success"])
        self.assertEqual(result["series_count_available"], 5)
        self.assertEqual({s["series_no"] for s in result["series"]}, {"2", "3", "4", "1.1", "1.3"})

    def test_list_pages_through_the_inventory(self):
        first = table_tools.list_singstat_series("M081331", page_size=2)
        self.assertEqual(len(first["series"]), 2)
        self.assertIsNotNone(first.get("next_offset"))

    def test_query_several_exact_series_in_one_call(self):
        result = table_tools.query_singstat_table(
            "M081331", "series id:4, series id:2", max_series=8
        )
        self.assertEqual([s["series_no"] for s in result["series_summary"]], ["4", "2"])
        self.assertEqual(result["series_summary"][1]["latest_value"], 8955.99)

    def test_compare_two_series(self):
        result = table_tools.compare_singstat_series("M081331", "series id:2", "series id:3")
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["series_a"]["series_name"], "Private Sector")


class KnowledgeBaseTests(unittest.TestCase):
    """The table/series metadata index used for discovery (metadata only, never evidence)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = kb.open_db(Path(self.tmp.name) / "kb.sqlite3")
        kb.upsert_table(self.con, "M081331", "Research And Development Expenditure, Annual")
        kb.upsert_series(self.con, "M081331", "2", "Private Sector", unit="Million Dollars")

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_keyword_search_finds_the_table(self):
        hits = kb.lexical_search(self.con, "research and development expenditure")
        self.assertTrue(any(hit.get("table_id") == "M081331" for hit in hits))
        self.assertGreaterEqual(kb.counts(self.con)["tables"], 1)

    def test_identifiers_and_numbers_are_validated(self):
        self.assertEqual(kb.safe_id(" m081331 "), "M081331")
        with self.assertRaises(ValueError):
            kb.safe_id("../x")
        self.assertEqual(kb.parse_numeric("1,234.50"), "1234.50")
        self.assertIsNone(kb.parse_numeric("na"))


class NotesCatalogueTests(unittest.TestCase):
    """Curated notes are stored and found per table (in a temporary database)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(
            rag_catalog, "DB_FILENAME", str(Path(self.tmp.name) / "rag.sqlite3")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.catalog = rag_catalog.RagCatalog(ROOT)

    def test_upsert_then_retrieve(self):
        self.assertTrue(
            self.catalog.upsert(
                table_id="M081331",
                title="R&D expenditure",
                source_url="https://tablebuilder.singstat.gov.sg/table/TS/M081331",
                text="Research and development expenditure by institutional sector.",
            )
        )
        found = self.catalog.retrieve("research expenditure by sector", dataset_ids=["M081331"])
        self.assertTrue(found)
        self.assertEqual(self.catalog.count(), 1)

    def test_unofficial_sources_are_refused(self):
        self.assertFalse(
            self.catalog.upsert(
                table_id="M081331", title="x", source_url="https://example.com/x", text="y"
            )
        )


if __name__ == "__main__":
    unittest.main()
