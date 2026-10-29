"""Small shared pieces: series-table helpers, the model-client wrapper, the raw
table reader and the question-bank categories.
"""

import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)

from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
from tests.test_storage_and_tools import FakeResponse  # noqa: E402
from ui import components  # noqa: E402
from utils import model_client, question_categories  # noqa: E402
from utils.singstat_retrieval import retrieve_singstat_table  # noqa: E402


class SeriesTableHelperTests(unittest.TestCase):
    def test_filter_words_are_bounded_and_cleaned(self):
        self.assertEqual(
            components.filter_words("Hotel  OCCUPANCY <script>"), ["hotel", "occupancy", "script"]
        )
        self.assertLessEqual(len(components.filter_words("a b c d e f g h i j")), 8)
        self.assertEqual(components.filter_words(None), [])

    def test_series_numbers_sort_numerically(self):
        ordered = sorted(["1.10", "1.2", "1", "2", "1.1"], key=components.series_number_key)
        self.assertEqual(ordered, ["1", "1.1", "1.2", "1.10", "2"])

    def test_ticks_respect_the_limit(self):
        chosen, hit_limit = components.apply_series_table_edits(
            {0: {"Show": True}, 1: {"Show": True}}, ["a"], ["b", "c"], 2
        )
        self.assertEqual(len(chosen), 2)
        self.assertTrue(hit_limit)
        unticked, _ = components.apply_series_table_edits(
            {0: {"Show": False}}, ["a", "b"], ["a"], 5
        )
        self.assertEqual(unticked, ["b"])

    def test_dollar_signs_stay_literal(self):
        self.assertEqual(components.md_literal("$15,000 - $17,499"), "\\$15,000 - \\$17,499")


class ModelClientTests(unittest.TestCase):
    """Every model call is labelled; a rejected reasoning setting is retried once without it."""

    def test_selected_model_and_reasoning_are_applied(self):
        seen = []
        tracked = types.SimpleNamespace(
            responses=types.SimpleNamespace(create=lambda **k: seen.append(k) or "ok"),
            embeddings="emb",
            with_options=lambda **o: tracked,
        )
        client = model_client.SageModelClient(tracked)
        allowed = next(m for m in model_client.MODEL_OPTIONS if str(m).startswith("gpt-5"))
        with (
            mock.patch.dict(os.environ, {"SAGE_REASONING_EFFORT": "low"}),
            model_client.selected_model_scope(allowed),
        ):
            self.assertEqual(
                client.responses.create(model="other", input="x", sage_stage="s", sage_purpose="p"),
                "ok",
            )
        self.assertEqual(seen[0]["model"], allowed)
        with self.assertRaises(ValueError):
            with model_client.selected_model_scope("not-a-model"):
                pass
        self.assertEqual(seen[0]["reasoning"], {"effort": "low"})
        self.assertEqual(client.embeddings, "emb")
        self.assertIsInstance(client.with_options(timeout=1), model_client.SageModelClient)

    def test_rejected_reasoning_setting_is_dropped(self):
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            if "reasoning" in kwargs:
                error = ValueError("unsupported reasoning value")
                error.status_code = 400
                raise error
            return "ok"

        client = model_client.SageModelClient(
            types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
        )
        with mock.patch.dict(os.environ, {"SAGE_REASONING_EFFORT": "high"}):
            self.assertEqual(client.responses.create(model="gpt-5-x", input="x"), "ok")
        self.assertEqual(len(calls), 2)


class RawTableReaderTests(unittest.TestCase):
    def test_rows_become_typed_records(self):
        payload = {
            "Data": {
                "row": [
                    {
                        "seriesNo": "1",
                        "rowText": "Total",
                        "uoM": "Number",
                        "columns": [
                            {"key": "2023", "value": "1000"},
                            {"key": "2024", "value": "na"},
                        ],
                    }
                ]
            }
        }
        records = retrieve_singstat_table(
            "M810001", "https://x", headers={}, http_get=lambda *a, **k: FakeResponse(payload)
        )
        self.assertEqual(records["value"].tolist(), [1000.0])  # "na" is dropped, never 0

    def test_missing_data_is_an_error(self):
        with self.assertRaises(ValueError):
            retrieve_singstat_table(
                "M810001",
                "https://x",
                headers={},
                http_get=lambda *a, **k: FakeResponse({"nope": 1}),
            )


class CategoryTests(unittest.TestCase):
    def test_titles_map_to_official_categories(self):
        for title, theme in (
            ("Research And Development Expenditure, Annual", "Industry"),
            ("Merchandise Imports Of Machinery And Equipment, Monthly", ""),
            ("Resident Population By Age Group", ""),
        ):
            category, _source = question_categories.best_category(theme, title)
            with self.subTest(title=title):
                self.assertIn(category, question_categories.OFFICIAL_CATEGORIES)


class QuestionCategoryRecoveryTests(unittest.TestCase):
    """Without the audit file, categories come from the official table themes in the database.

    (Before round 17 this recovery never ran: the module did not import sqlite3.)
    """

    def test_theme_from_the_knowledge_base_when_the_audit_is_missing(self):
        import sqlite3
        import tempfile
        from ui.question_bank import question_categories

        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "kb.sqlite3"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE kb_tables (table_id TEXT, title TEXT, theme TEXT)")
            con.execute(
                "INSERT INTO kb_tables VALUES ('M081331', "
                "'Research And Development Expenditure, Annual', 'Industry')"
            )
            con.commit()
            con.close()
            found = question_categories(
                [
                    {"question": "How has research and development expenditure changed?"},
                    {"question": "Something unrelated?"},
                ],
                Path(folder) / "missing.csv",
                metadata_db=db,
            )
        self.assertEqual(found["How has research and development expenditure changed?"], "Industry")
        self.assertEqual(found["Something unrelated?"], "Uncategorised")


if __name__ == "__main__":
    unittest.main()
