"""Tests for table discovery, the curated-notes search, structured ingestion and
the sample-question generator. All use temporary files and fake model clients.
"""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)

from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
from tests.fixtures.real_answers import tool_result  # noqa: E402
from utils import (
    ai_discovery,
    hybrid_discovery,
    kb,
    kb_structured_ingest,  # noqa: E402
    model_evidence,
    question_generator,
    rag_engine,
)

_TITLES = [
    ("M081331", "Research And Development Expenditure By Institutional Sector, Annual"),
    ("M081301", "Organisations Performing Research And Development, Annual"),
    ("M015781", "Share Of Nominal Gross Value Added, By Industry, Annual"),
    ("M451111", "Merchandise Imports Of Machinery And Equipment, Monthly"),
]


def discovery_index(folder):
    """A small discovery index (table titles only), like data/discovery_index.sqlite3."""
    path = Path(folder) / "discovery_index.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE docs (table_id TEXT, title TEXT, source_url TEXT, doc_type TEXT)")
    con.executemany(
        "INSERT INTO docs VALUES (?,?,?, 'table')",
        [(t, title, f"https://tablebuilder.singstat.gov.sg/table/TS/{t}") for t, title in _TITLES],
    )
    con.commit()
    con.close()
    return path


class FakeModel:
    """Answers each model call with the next scripted JSON object."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **_kwargs):
        return types.SimpleNamespace(output_text=json.dumps(self.replies.pop(0)))


class AssistedDiscoveryTests(unittest.TestCase):
    """The model may only rank titles that are already in the index."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = discovery_index(self.tmp.name)
        self.question = "How has R&D spending by sector changed?"

    def test_model_choice_is_limited_to_indexed_tables(self):
        model = FakeModel(
            {"subject": "R&D", "search_phrases": ["research and development", "expenditure"]},
            {"table_ids": ["M081331", "M999999"], "ambiguous": False},
        )
        plan = ai_discovery.assisted_discovery_plan(
            self.question, self.index, client=model, enabled=True
        )
        self.assertEqual([c["table_id"] for c in plan["choices"]], ["M081331"])
        self.assertTrue(plan["ai_assisted"])

    def test_falls_back_without_a_model_or_on_bad_output(self):
        plain = ai_discovery.assisted_discovery_plan(
            self.question, self.index, client=None, enabled=True
        )
        self.assertNotIn("ai_assisted", plain)
        broken = FakeModel({"search_phrases": "not a list"})
        self.assertNotIn(
            "ai_assisted",
            ai_discovery.assisted_discovery_plan(
                self.question, self.index, client=broken, enabled=True
            ),
        )


class HybridSearchTests(unittest.TestCase):
    """Keyword search over the knowledge base finds candidate tables (no vector index)."""

    def test_keyword_candidates_with_a_trace(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "kb.sqlite3"
            con = kb.open_db(path)
            for table_id, title in _TITLES:
                kb.upsert_table(
                    con,
                    table_id,
                    title,
                    source_url=f"https://tablebuilder.singstat.gov.sg/table/TS/{table_id}",
                )
            con.commit()
            con.close()
            with mock.patch.dict(
                os.environ, {"SAGE_KB_SQLITE": str(path), "SAGE_CHROMA_DIR": folder + "/none"}
            ):
                found = hybrid_discovery.retrieve("research and development expenditure", top_k=3)
        self.assertTrue(found)
        self.assertIn("M081331", json.dumps(found))
        self.assertEqual(found[0]["discovery_trace"]["sqlite"]["status"], "executed")

    def test_missing_index_returns_nothing(self):
        with mock.patch.dict(os.environ, {"SAGE_KB_SQLITE": "/nonexistent/kb.sqlite3"}):
            self.assertEqual(hybrid_discovery.retrieve("anything"), [])


class NotesSearchTests(unittest.TestCase):
    """The built-in notes are ranked by keywords plus (fake) embeddings."""

    def setUp(self):
        def embed(model, input):
            vectors = [
                [float(len(text) % 7 + 1), float(text.count("e") + 1), 1.0] for text in input
            ]
            return types.SimpleNamespace(data=[types.SimpleNamespace(embedding=v) for v in vectors])

        fake_client = types.SimpleNamespace(embeddings=types.SimpleNamespace(create=embed))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from utils import rag_catalog

        for patcher in (
            mock.patch.object(rag_engine, "client", fake_client),
            # Any notes catalogue is created in a temporary folder, never in data/.
            mock.patch.object(
                rag_catalog, "DB_FILENAME", str(Path(self.tmp.name) / "notes.sqlite3")
            ),
            mock.patch.object(rag_engine, "_RAG_CATALOG", None),
            mock.patch.object(rag_engine, "FAST_RAG_ENABLED", False),
            mock.patch.object(rag_engine, "KB_EMBEDDINGS", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_retrieve_and_format(self):
        notes = rag_engine.retrieve_rag_context("population and labour force definitions", top_k=2)
        self.assertLessEqual(len(notes), 2)
        text = rag_engine.format_rag_context(notes)
        self.assertIsInstance(text, str)
        note = {"title": "Labour force", "text": "Labour force statistics", "keywords": "labour"}
        self.assertGreater(rag_engine.keyword_score("labour force", note), 0)
        self.assertIn("labour", rag_engine.tokenize("Labour force, 2024"))


class StructuredIngestTests(unittest.TestCase):
    """A complete official response is stored cell by cell; anything else is refused."""

    def payload(self):
        return {
            "Data": {
                "id": "M081331",
                "title": "R&D Expenditure, Annual",
                "frequency": "Annual",
                "row": [
                    {
                        "seriesNo": "2",
                        "rowText": "Private Sector",
                        "uoM": "Million Dollars",
                        "columns": [
                            {"key": "2022", "value": "8130.09"},
                            {"key": "2023", "value": "8955.99"},
                        ],
                    }
                ],
            }
        }

    def test_complete_payload_is_ingested(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)  # Python 3.13+ warns about unclosed connections
        kb_structured_ingest.init_schema(con)
        report = kb_structured_ingest.ingest_payload(
            con, self.payload(), "M081331", complete_response=True
        )
        self.assertTrue(report)
        self.assertEqual(kb_structured_ingest.numeric("8,955.99"), "8955.99")
        self.assertIsNone(kb_structured_ingest.numeric("na"))

    def test_wrong_table_or_unproven_completeness_is_refused(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)  # Python 3.13+ warns about unclosed connections
        kb_structured_ingest.init_schema(con)
        with self.assertRaises(ValueError):
            kb_structured_ingest.ingest_payload(
                con, self.payload(), "M999999", complete_response=True
            )
        with self.assertRaises(ValueError):
            kb_structured_ingest.ingest_payload(
                con, self.payload(), "M081331", complete_response=False
            )

    def test_fetch_complete_stops_on_a_short_page(self):
        pages = [self.payload()]
        fetched = kb_structured_ingest.fetch_complete(
            "M081331", page_size=5, fetch=lambda *a, **k: pages[0]
        )
        self.assertEqual(len(fetched["Data"]["row"]), 1)


class ModelEvidenceTests(unittest.TestCase):
    """What the model is shown: compact, valid JSON, never chart payloads."""

    def test_compact_evidence_is_valid_json_without_charts(self):
        result = tool_result("M015781", ["1", "2", "3"])
        text = model_evidence.compact_tool_result_for_model(result, max_chars=3000)
        data = json.loads(text)
        self.assertNotIn("chart_spec", data)
        self.assertLessEqual(len(text), 3000)
        chunks = model_evidence.evidence_chunks_for_model(result, max_chars=2000)
        self.assertTrue(all(len(chunk) <= 2000 for chunk in chunks))


class QuestionGeneratorTests(unittest.TestCase):
    """The sample bank is written only from the official title catalogue."""

    def test_generates_categorised_questions_from_titles(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "kb.sqlite3"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE kb_tables (table_id TEXT, title TEXT)")
            con.executemany("INSERT INTO kb_tables VALUES (?,?)", _TITLES)
            con.commit()
            con.close()
            out = Path(folder) / "questions.txt"
            report = question_generator.generate_question_bank(db, out, target=3)
            self.assertTrue(out.is_file())
            questions = [
                line
                for line in out.read_text(encoding="utf-8").splitlines()
                if "[CATEGORY:" in line
            ]
            self.assertEqual(len(questions), 3)
            self.assertEqual(report["tables_covered"], 3)

    def test_missing_catalogue_leaves_the_bank_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                question_generator.generate_question_bank(
                    Path(folder) / "none.sqlite3", Path(folder) / "q.txt", target=3
                )


if __name__ == "__main__":
    unittest.main()
