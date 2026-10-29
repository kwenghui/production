"""Generic discovery ranking: plurals, acronyms the question uses, typo tolerance from
the index's own words. No subject-specific rules."""

import os
import sqlite3
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
from utils import hybrid_discovery, kb  # noqa: E402
from utils.search_terms import (
    SearchVocabulary,
    acronym_in_title,
    subject_terms,  # noqa: E402
    vocabulary_for,
)

URL = "https://tablebuilder.singstat.gov.sg/table/TS/"


def hit(table_id, title, backend, series=""):
    return {
        "table_id": table_id,
        "table_title": title,
        "series_title": series,
        "source_url": URL + table_id,
        "_backend": backend,
    }


class SearchTerms(unittest.TestCase):
    def test_singular_and_plural_match_and_filler_is_dropped(self):
        self.assertEqual(subject_terms("tell me about CPF withdrawals info"), {"cpf", "withdrawal"})
        self.assertIn("industry", subject_terms("Industries"))

    def test_acronyms_come_only_from_title_initials(self):
        self.assertTrue(acronym_in_title("cpf", "Withdrawals Of Central Provident Fund By Type"))
        self.assertTrue(acronym_in_title("ict", "Information And Communications Technology Skills"))
        self.assertFalse(acronym_in_title("car", "Cars And Rentals"))
        self.assertFalse(acronym_in_title("toolong", "Some Title"))

    def test_typos_are_fixed_from_the_index_vocabulary_only(self):
        vocabulary = SearchVocabulary(["teachers", "students", "growth", "education"])
        fixed, corrections = vocabulary.correct("growth of teackers for pas few yrs")
        self.assertEqual(corrections, {"teackers": "teachers"})
        self.assertIn("teachers", fixed)
        self.assertEqual(vocabulary.correct("growth of teachers")[1], {})  # known word
        self.assertEqual(vocabulary.correct("xyzzy gizmos")[1], {})  # nothing close

    def test_two_equally_close_words_are_not_guessed(self):
        vocabulary = SearchVocabulary(["batter", "butter"])
        self.assertEqual(vocabulary.correct("bitter")[1], {})


class Ranking(unittest.TestCase):
    def test_subject_coverage_decides_between_close_candidates(self):
        hits = [
            hit("M1", "Default Of CPF Contributions By Employers, Monthly", "SQLite FTS5"),
            hit("M1", "Default Of CPF Contributions By Employers, Monthly", "ChromaDB table"),
            hit("M2", "Withdrawals Of Central Provident Fund By Type, Annual", "ChromaDB table"),
            hit("M2", "Withdrawals Of Central Provident Fund By Type, Annual", "SQLite FTS5"),
        ]
        ranked = hybrid_discovery._ranked_tables(
            hits, question="tell me about cpf withdrawal info", limit=10
        )
        self.assertEqual(ranked[0]["table_id"], "M2")

    def test_a_table_naming_the_subject_beats_tables_found_only_by_meaning(self):
        """From a real trace: 'can i ask abt the growth of treachers in singapore'. The teachers
        table was found by keyword search only and ended 9th behind 'Education Profile'
        tables found by meaning; chat words ('can', 'ask', 'abt') diluted the subject."""
        question = "can i ask abt the growth of teachers in singapore"
        self.assertEqual(subject_terms(question), {"growth", "teacher"})
        hits = [
            hit(
                "M000010",
                "Students And Teachers In Educational Institutions, Annual",
                "SQLite FTS5",
                "Total Teachers",
            ),
            hit(
                "M000011",
                "Average Retail Prices Of Selected Consumer Items, Monthly",
                "SQLite FTS5",
                "Canned Drink With Ice",
            ),
            hit(
                "M000012",
                "Education Profile Of Residents By Highest Qualification, Annual",
                "ChromaDB table",
            ),
            hit(
                "M000012",
                "Education Profile Of Residents By Highest Qualification, Annual",
                "ChromaDB series",
                "Primary",
            ),
        ]
        ranked = hybrid_discovery._ranked_tables(hits, question=question, limit=5)
        self.assertEqual(ranked[0]["table_id"], "M000010")

    def test_currency_codes_find_the_exchange_rate_table(self):
        """From a real trace: 'compare sgd and myr grwoth' ranked investment tables
        mentioning 'Malaysia' above the exchange-rate table, and 'compare' was wrongly
        'corrected' to 'compared'."""
        vocabulary = SearchVocabulary(
            ["compared", "growth", "exchange", "ringgit", "import", "imports", "petroleum"]
        )
        self.assertEqual(vocabulary.correct("compare sgd and myr grwoth")[1], {"grwoth": "growth"})
        # A cut-off word is a typo too ('petrolum impor' from another real trace).
        self.assertEqual(
            vocabulary.correct("petrolum impor to singapore")[1],
            {"petrolum": "petroleum", "impor": "import"},
        )
        hits = [
            hit(
                "M000020",
                "Portfolio Investment Assets By Destination Economy, Annual",
                "ChromaDB table",
            ),
            hit(
                "M000020",
                "Portfolio Investment Assets By Destination Economy, Annual",
                "ChromaDB series",
                "Malaysia",
            ),
            dict(
                hit(
                    "M000021",
                    "Exchange Rates (Average For Period), Annual",
                    "ChromaDB series",
                    "Malaysian Ringgit",
                ),
                unit="Singapore Dollar Per Malaysian Ringgit",
            ),
        ]
        ranked = hybrid_discovery._ranked_tables(
            hits, question="compare sgd and myr growth", limit=5
        )
        self.assertEqual(ranked[0]["table_id"], "M000021")

    def test_a_misspelt_question_finds_the_right_table(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "kb.sqlite3"
            con = kb.open_db(path)
            kb.upsert_table(
                con,
                "M000003",
                "Students And Teachers In Educational Institutions, Annual",
                source_url=URL + "M000003",
            )
            kb.upsert_series(con, "M000003", "5", "Total Teachers", unit="Number")
            kb.upsert_table(
                con,
                "M000004",
                "Contribution To Growth In Total Demand, Annual",
                source_url=URL + "M000004",
            )
            con.commit()
            con.close()
            with mock.patch.dict(
                os.environ, {"SAGE_KB_SQLITE": str(path), "SAGE_CHROMA_DIR": folder + "/none"}
            ):
                found = hybrid_discovery.retrieve(
                    "tell me about growth of teackers for pas few yrs", top_k=3
                )
            self.assertEqual(found[0]["table_id"], "M000003")
            self.assertEqual(
                found[0]["discovery_trace"]["query_corrections"], {"teackers": "teachers"}
            )

    def test_vocabulary_is_read_from_the_knowledge_base_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "kb.sqlite3"
            con = kb.open_db(path)
            kb.upsert_table(con, "M000005", "Hotel Occupancy Rates, Monthly")
            kb.upsert_series(con, "M000005", "1", "Room Nights", unit="Number")
            con.commit()
            con.close()
            words = vocabulary_for(path).words
        self.assertTrue({"hotel", "occupancy", "nights"} <= words)


class EvaluationHelpers(unittest.TestCase):
    def test_typo_rank_and_summary(self):
        from tools.evaluate_discovery import rank_of, summarise, with_typo

        self.assertNotEqual(
            with_typo("How many teachers are there?", 1), "How many teachers are there?"
        )
        self.assertEqual(rank_of("M2", [{"table_id": "M1"}, {"table_id": "m2"}]), 2)
        self.assertEqual(summarise([1, 3, None, 12])["hit@5"], 0.5)


if __name__ == "__main__":
    unittest.main()
