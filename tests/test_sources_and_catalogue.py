"""The official-data door (safe_http_get) with a real local store, knowledge-base
ingestion and search, and the AI wording/category helpers of the question bank.
"""

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)

from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
from tests.test_storage_and_tools import FakeResponse, fake_server, official_rows  # noqa: E402
from utils import kb, official_source, question_ai, retrieval_trace, store  # noqa: E402

TABLE_URL = "https://tablebuilder.singstat.gov.sg/api/table/tabledata/M810001?limit=2500&offset=0"


class OfficialDoorTests(unittest.TestCase):
    """Local first: download once, then serve the verified copy; failures pause retries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = store.LocalStore(Path(self.tmp.name) / "cache.sqlite3")
        self.fetch, self.calls = fake_server(official_rows(6))
        for name, value in (
            ("get_local_store", lambda: self.local),
            ("SAGE_STORE_ENABLED", True),
            ("SAGE_OFFLINE_ONLY", False),
            ("STALE_WHILE_REVALIDATE", False),
            ("_live_http_get", lambda url, **kw: self.fetch(url)),
        ):
            patcher = mock.patch.object(official_source, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        official_source._TABLE_FETCH_FAILURES.clear()

    def table(self, response):
        return response.json()["Data"]["row"]

    def test_first_request_downloads_then_the_copy_is_reused(self):
        first = official_source.safe_http_get(TABLE_URL)
        downloads = len(self.calls)
        second = official_source.safe_http_get(TABLE_URL)
        self.assertEqual(self.table(first), self.table(second))
        self.assertEqual(len(self.calls), downloads)

    def test_expired_copy_is_refreshed(self):
        official_source.safe_http_get(TABLE_URL)
        before = len(self.calls)
        with mock.patch.object(official_source, "CACHE_DURATION_SECONDS", -1):
            refreshed = official_source.safe_http_get(TABLE_URL)
        self.assertGreater(len(self.calls), before)
        self.assertEqual(len(self.table(refreshed)), 6)

    def test_a_failing_table_is_paused(self):
        broken = lambda url, **kw: FakeResponse({}, status=503)  # noqa: E731
        with mock.patch.object(official_source, "_live_http_get", broken):
            with self.assertRaises(Exception):
                official_source.safe_http_get(TABLE_URL)
            with self.assertRaises(store.UnverifiablePaginationError):
                official_source.safe_http_get(TABLE_URL)  # paused: no second network call

    def test_retrieval_is_traced(self):
        token = retrieval_trace.begin()
        try:
            official_source.safe_http_get(TABLE_URL)
            official_source.safe_http_get(TABLE_URL)
            summary = retrieval_trace.summary()
        finally:
            retrieval_trace.end(token)
        self.assertIn("M810001", json.dumps(summary, default=str))


class KnowledgeBaseIngestTests(unittest.TestCase):
    """Catalogue import, keyword discovery and table inspection with a fake official API."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.con = kb.open_db(Path(self.tmp.name) / "kb.sqlite3")
        self.addCleanup(self.con.close)

    def test_catalogue_csv_import_skips_bad_ids(self):
        path = Path(self.tmp.name) / "catalogue.csv"
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerows(
                [
                    ["table_id", "title", "theme"],
                    ["M081331", "Research And Development Expenditure, Annual", "Industry"],
                    ["../bad", "Bad", ""],
                ]
            )
        report = kb.import_catalogue_csv(self.con, path)
        self.assertEqual((report["imported"], report["skipped"]), (1, 1))

    def test_keyword_discovery_records_reported_tables(self):
        fake = lambda path, params=None: {
            "Data": {
                "records": [  # noqa: E731
                    {"id": "M081331", "title": "R&D Expenditure, Annual", "theme": "Industry"},
                    {"id": "bad id", "title": "x"},
                ]
            }
        }
        self.assertEqual(kb.discover_keyword(self.con, "research", fetch=fake), 1)
        hits = kb.lexical_search_series_first(self.con, "R&D expenditure")
        self.assertTrue(any(h.get("table_id") == "M081331" for h in hits))

    def test_inspect_table_stores_series_metadata(self):
        payload = {
            "Data": {
                "id": "M081331",
                "title": "R&D Expenditure, Annual",
                "frequency": "Annual",
                "row": [
                    {
                        "seriesNo": "2",
                        "rowText": "Private Sector",
                        "uoM": "Million Dollars",
                        "columns": [{"key": "2023", "value": "8955.99"}],
                    }
                ],
            }
        }
        report = kb.inspect_table(
            self.con, "M081331", fetch=lambda *a, **k: payload, include_observations=True
        )
        self.assertTrue(report)
        found = kb.hybrid_search(self.con, "private sector R&D", chroma=None)
        self.assertTrue(found)


class QuestionWordingAITests(unittest.TestCase):
    """The optional AI helpers may only use official categories and safe rewrites."""

    def test_categories_must_be_official(self):
        ai = lambda *a: json.dumps({"M081331": "Industry", "M015781": "Made-up Category"})  # noqa: E731
        result = question_ai._ai_classify(
            ai,
            [
                {"table_id": "M081331", "title": "R&D"},
                {"table_id": "M015781", "title": "GVA shares"},
            ],
        )
        self.assertEqual(result, {"M081331": "Industry"})

    def test_model_failures_keep_the_deterministic_result(self):
        self.assertEqual(
            question_ai._ai_classify(lambda *a: "not json", [{"table_id": "M1", "title": "x"}]), {}
        )
        self.assertEqual(question_ai._ai_polish(lambda *a: 1 / 0, [], set()), {})

    def test_rewrites_are_checked_before_use(self):
        items = [
            {
                "table_id": "M081331",
                "title": "Research And Development Expenditure, Annual",
                "question": "How has research and development expenditure in Singapore changed "
                "over the years?",
                "kind": "trend",
            }
        ]
        bad = lambda *a: json.dumps([{"id": "M081331", "question": "Buy now!"}])  # noqa: E731
        self.assertEqual(question_ai._ai_polish(bad, items, set()), {})


if __name__ == "__main__":
    unittest.main()
