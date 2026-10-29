"""Tests for the generic rules that replaced subject-specific shortcuts.

Each rule is checked on SEVERAL subjects, to show it is not tuned to one.
Run with:  python -m unittest discover -s tests
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.chart_intent import required_groups  # noqa: E402
from utils.chart_selection import _age_groups_supported, _missing_concept_family  # noqa: E402
from utils.cube_cells import flatten, is_total_label, parse_number  # noqa: E402
from utils.discovery_controller import compatibility, discovery_plan, needs_series_search  # noqa: E402
from utils.query_text import (
    asks_for_level,
    explicit_frequency,
    measure_kind,  # noqa: E402
    requested_frequency,
    title_frequency,
)


class FrequencyRules(unittest.TestCase):
    """One set of frequency rules for questions and titles (was three copies)."""

    def test_explicit_vs_soft(self):
        self.assertEqual(explicit_frequency("monthly retail sales"), "monthly")
        self.assertEqual(explicit_frequency("hotel occupancy each quarter"), "quarterly")
        self.assertEqual(requested_frequency("GDP over the years"), ("annual", "soft"))
        self.assertIsNone(explicit_frequency("GDP over the years"))

    def test_title_frequency_takes_the_last_word(self):
        self.assertEqual(title_frequency("Telecommunications, Annual"), "annual")
        self.assertEqual(title_frequency("Labour Force Survey (Annual)"), "annual")
        self.assertEqual(title_frequency("Monthly Household Income From Work, Annual"), "annual")
        self.assertIsNone(title_frequency("Resident Population by Age Group"))


class MeasureKindRules(unittest.TestCase):
    """Same classifier for questions and titles, on many subjects."""

    def test_kinds(self):
        cases = {
            "Population Density": "density",
            "Doctors per 1,000 population": "rate",
            "Projected old-age support ratio": "projection",
            "Percentage change in retail sales": "growth",
            "Share of households with broadband": "share",
            "New registrations of motor vehicles": "new_flow",
            "Total residents": None,
        }
        for text, kind in cases.items():
            with self.subTest(text=text):
                self.assertEqual(measure_kind(text), kind)

    def test_level_requests(self):
        self.assertTrue(asks_for_level("How many hotel rooms were there?"))
        self.assertFalse(asks_for_level("What is the unemployment rate?"))


class CompatibilityRules(unittest.TestCase):
    """A title is ruled out only by a conflicting fundamental property."""

    def test_measure_kind_conflicts_on_any_subject(self):
        for question, title in [
            ("What is the population density?", "Population Projections"),
            ("What share of workers are female?", "Employment Growth Rate"),
            ("How many visitors arrived?", "Visitor Arrivals, Percentage Change"),
        ]:
            with self.subTest(question=question):
                self.assertFalse(compatibility(question, title)[0])

    def test_projections_never_answer_observed_questions(self):
        self.assertFalse(compatibility("Resident population in 2020", "Population Projections")[0])
        self.assertTrue(compatibility("Projected population in 2030", "Population Projections")[0])

    def test_population_basis_conflict(self):
        self.assertFalse(compatibility("How many citizens are there?", "Resident Population")[0])
        self.assertTrue(compatibility("How many residents are there?", "Resident Population")[0])

    def test_frequency_and_unit(self):
        self.assertFalse(
            compatibility("monthly car registrations", "Motor Vehicle Population, Annual")[0]
        )
        self.assertFalse(compatibility("doctors per 1,000 people", "Number of Doctors")[0])
        self.assertTrue(
            compatibility("doctors per 1,000 people", "Doctors per 1,000 Population")[0]
        )

    def test_silence_never_excludes(self):
        self.assertTrue(compatibility("Tell me about tourism", "Visitor Arrivals")[0])


class DiscoveryPlans(unittest.TestCase):
    """Discovery uses one generic ranking for every subject."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "discovery_index.sqlite3"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE docs (table_id TEXT, title TEXT, source_url TEXT, doc_type TEXT)")
        rows = [
            ("M810001", "Indicators On Population, Annual"),
            ("M810011", "Population Density, Annual"),
            ("M810091", "Population Projections, Annual"),
            ("M550001", "Visitor Arrivals, Monthly"),
            ("M550002", "Visitor Arrivals By Country, Annual"),
        ]
        con.executemany(
            "INSERT INTO docs VALUES (?,?,?, 'table')",
            [
                (tid, title, f"https://tablebuilder.singstat.gov.sg/table/TS/{tid}")
                for tid, title in rows
            ],
        )
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_incompatible_titles_are_near_misses_not_choices(self):
        plan = discovery_plan("How has population density changed?", self.db)
        offered = {row["table_id"] for row in plan["choices"]}
        self.assertNotIn("M810091", offered)  # projections never offered
        self.assertIn("M810011", offered)

    def test_frequency_decides_between_tables_of_any_subject(self):
        plan = discovery_plan("monthly visitor arrivals", self.db)
        self.assertEqual([row["table_id"] for row in plan["choices"]], ["M550001"])

    def test_series_level_questions_skip_title_choices(self):
        self.assertIsNotNone(needs_series_search("spending by income quintile in each survey"))
        self.assertIsNotNone(needs_series_search("citizen-to-non-citizen marriages vs all"))
        self.assertIsNone(needs_series_search("How many visitors arrived?"))


class ChartRules(unittest.TestCase):
    def test_concept_family_rule_is_generic(self):
        self.assertTrue(_missing_concept_family({"insurance"}, {"population"}))
        self.assertFalse(_missing_concept_family({"insurance"}, {"policy", "life"}))
        self.assertFalse(_missing_concept_family({"tourism"}, {"visitor"}))  # no family named

    def test_age_groups_need_words_or_bands(self):
        self.assertTrue(_age_groups_supported({"elderly"}, set(), "Residents aged 65+"))
        self.assertFalse(_age_groups_supported({"child"}, {"elderly"}, "Residents aged 65+"))
        self.assertTrue(_age_groups_supported({"population"}, set(), "anything"))

    def test_comparison_groups(self):
        self.assertEqual(required_groups("births and deaths since 2000"), {"birth", "death"})
        self.assertEqual(required_groups("total population"), frozenset())


class CubeCells(unittest.TestCase):
    """Generic cube reading, independent of any subject."""

    def test_flatten_and_parse(self):
        columns = [
            {
                "key": "2023",
                "columns": [{"key": "Total", "value": "1,234.5"}, {"key": "Other", "value": "na"}],
            }
        ]
        cells = list(flatten(columns))
        self.assertEqual(cells, [(("2023", "Total"), "1,234.5"), (("2023", "Other"), "na")])
        self.assertEqual(parse_number("1,234.5"), 1234.5)
        self.assertIsNone(parse_number("na"))
        self.assertIsNone(parse_number(True))
        self.assertTrue(is_total_label("All Households"))
        self.assertTrue(is_total_label("Total 1/"))
        self.assertFalse(is_total_label("Food"))


if __name__ == "__main__":
    unittest.main()
