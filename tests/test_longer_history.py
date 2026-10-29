"""A change-over-time answer with ONE published period gets the same subject's longer series.

From a real answer: SingStat's growth-rate table M602551 currently publishes 2025 only,
so "how much did it change each year?" had one number and no chart.
"""

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.answer_verification import (
    full_answer_numbers_supported,  # noqa: E402
    longer_history_for_single_period,
    official_evidence,
)

URL = "https://tablebuilder.singstat.gov.sg/table/TS/"
GROWTH = "Foreign Wholesale Trade Series, In Chained Volume Terms, Year On Year Growth Rate, Annual"
INDEX = "Foreign Wholesale Trade Index, (2017 = 100), In Chained Volume Terms, Annual"
QUESTION = (
    "How much did foreign wholesale trade in Singapore change each year compared with "
    "the previous year, after adjusting for price changes?"
)


def growth_result():
    return {
        "success": True,
        "table_id": "M602551",
        "title": GROWTH,
        "table_complete": True,
        "source_url": URL + "M602551",
        "numeric_claims": [6.4],
        "series_summary": [
            {
                "series_name": "Total",
                "series_no": "1",
                "unit": "Percentage",
                "latest_period": "2025",
                "latest_value": 6.4,
                "earliest_period": "2025",
                "earliest_value": 6.4,
                "change_latest_minus_earliest": 0.0,
                "observation_count": 1,
            }
        ],
    }


def index_result(name="Total", count=18):
    return {
        "success": True,
        "table_id": "M601981",
        "title": INDEX,
        "table_complete": True,
        "source_url": URL + "M601981",
        "chart_source_verified": True,
        "numeric_claims": [80.5, 110.2],
        "series_summary": [
            {
                "series_name": name,
                "series_no": "1",
                "unit": "Index",
                "latest_period": "2025",
                "latest_value": 110.2,
                "earliest_period": "2008",
                "earliest_value": 80.5,
                "change_latest_minus_earliest": 29.7,
                "observation_count": count,
            }
        ],
    }


def response():
    return {
        "tool_results": [growth_result()],
        "rag_results": [
            {"table_id": "M602551", "title": GROWTH},
            {"table_id": "M601981", "title": INDEX},
            {"table_id": "M000001", "title": "Retail Sales Index, Monthly"},
        ],
    }


class LongerHistory(unittest.TestCase):
    def core(self, result):
        calls = []

        def query(table_id, **kwargs):
            calls.append((table_id, kwargs))
            return result

        return types.SimpleNamespace(query_singstat_table=query), calls

    def test_related_index_is_added_with_a_sourced_note(self):
        core, calls = self.core(index_result())
        new_response, note = longer_history_for_single_period(QUESTION, response(), core)
        self.assertEqual([c[0] for c in calls], ["M601981"])  # same subject + frequency only
        self.assertEqual(calls[0][1]["series_search"], "Total")
        self.assertEqual(len(new_response["tool_results"]), 2)
        self.assertIn("M602551 currently publishes 2025 only", note)
        self.assertIn("80.5 in 2008 and 110.2 in 2025", note)
        answer = "Foreign wholesale trade grew 6.4% in 2025.\n\n" + note
        self.assertTrue(
            full_answer_numbers_supported(answer, official_evidence(new_response), QUESTION)
        )

    def test_nothing_changes_when_the_related_table_does_not_qualify(self):
        for result in (
            index_result(name="General Wholesale Trade"),
            index_result(count=1),
            {"success": False},
        ):
            with self.subTest(result=str(result)[:40]):
                core, _calls = self.core(result)
                self.assertIsNone(longer_history_for_single_period(QUESTION, response(), core))

    def test_only_for_change_questions_with_a_single_period(self):
        core, calls = self.core(index_result())
        self.assertIsNone(
            longer_history_for_single_period("What is the 2025 figure?", response(), core)
        )
        two_periods = response()
        two_periods["tool_results"][0]["series_summary"][0]["observation_count"] = 2
        self.assertIsNone(longer_history_for_single_period(QUESTION, two_periods, core))
        self.assertEqual(calls, [])


class ExplorerOpensOnTheAnswersSeries(unittest.TestCase):
    """'Explore table further' starts on the series the answer is about (a real page showed
    Professional Services while the answer discussed Manufacturing, Construction, Wholesale)."""

    def test_answer_series_are_preferred(self):
        from tools._stand_ins import install_stand_ins

        install_stand_ins()
        from ui.table_explorer import answer_series_ids

        entries = [{"series_id": i} for i in ("1", "1.1", "1.2", "1.3", "1.9")]
        result = {
            "success": True,
            "table_id": "M000002",
            "table_complete": True,
            "series_summary": [{"series_no": "1.2"}, {"series_no": "1.1"}, {"series_no": "9.9"}],
        }
        response = {"tool_results": [result, dict(result, table_id="M000003")]}
        self.assertEqual(answer_series_ids(response, "M000002", entries), ["1.2", "1.1"])
        self.assertEqual(answer_series_ids(response, "M000004", entries), [])
        self.assertEqual(
            answer_series_ids(
                {"tool_results": [dict(result, table_complete=False)]}, "M000002", entries
            ),
            [],
        )


class NoHundredPercentLine(unittest.TestCase):
    """A 'Population = 100%' line beside its shares is left out of the default chart."""

    def test_constant_whole_is_dropped_but_stays_available(self):
        from utils.series_defaults import is_constant_whole, without_constant_wholes

        years = ("2019", "2020", "2021")
        entries = [
            {"series_id": "1", "unit": "Per Cent", "observations": dict.fromkeys(years, 100.0)},
            {
                "series_id": "1.1",
                "unit": "Per Cent",
                "observations": dict(zip(years, (28.2, 29.0, 30.1))),
            },
            {"series_id": "2", "unit": "Number", "observations": dict.fromkeys(years, 100.0)},
        ]
        self.assertTrue(is_constant_whole(entries[0]))
        self.assertFalse(is_constant_whole(entries[2]))  # 100 people is not "the whole"
        self.assertEqual(without_constant_wholes(entries, {"1", "1.1"}), {"1.1"})
        self.assertEqual(without_constant_wholes(entries, {"1"}), {"1"})  # never empty


if __name__ == "__main__":
    unittest.main()
