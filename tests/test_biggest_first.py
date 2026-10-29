"""Biggest values first: the answer's observation table, trend legends and category bars."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.chart_order import default_sort_mode, lines_by_latest  # noqa: E402


class ChartOrder(unittest.TestCase):
    def bar(self, categories):
        return {
            "kind": "bar",
            "categories": categories,
            "series": [{"name": "Value", "values": list(range(len(categories)))}],
        }

    def test_categories_open_highest_first_unless_they_have_a_natural_order(self):
        self.assertEqual(
            default_sort_mode(self.bar(["Professionals", "Clerical", "Others"])), "Highest first"
        )
        self.assertEqual(
            default_sort_mode(self.bar(["Up to 15 mins", "16 - 30 mins", "Over 60"])),
            "Original order",
        )
        self.assertEqual(
            default_sort_mode(self.bar(["Aged 15-24", "Aged 25-34", "Aged 35-44"])),
            "Original order",
        )
        self.assertEqual(
            default_sort_mode({"kind": "trend", "categories": ["2023", "2024"], "series": []}),
            "Original order",
        )

    def test_trend_lines_by_latest_value_and_nothing_changes_but_order(self):
        spec = {
            "kind": "trend",
            "categories": ["2023", "2024"],
            "series": [
                {"name": "Construction", "values": [439.0, 6561.3]},
                {"name": "Total", "values": [38372.7, 1652795.5]},
                {"name": "Manufacturing", "values": [8679.9, None]},
            ],
        }
        ordered = lines_by_latest(spec)
        self.assertEqual(
            [s["name"] for s in ordered["series"]], ["Total", "Manufacturing", "Construction"]
        )
        self.assertEqual(sorted(map(str, ordered["series"])), sorted(map(str, spec["series"])))


class AnswerTableOrder(unittest.TestCase):
    def test_observation_rows_are_biggest_latest_value_first(self):
        from utils.answer_verification import evidence_bound_generic_answer

        url = "https://tablebuilder.singstat.gov.sg/table/TS/M000005"
        rows = [
            ("Construction", 439.0, 6561.3),
            ("Total", 38372.7, 1652795.5),
            ("Manufacturing", 8679.9, 281677.3),
        ]
        result = {
            "success": True,
            "table_id": "M000005",
            "title": "Direct Investment Abroad",
            "table_complete": True,
            "source_url": url,
            "series_summary": [
                {
                    "series_name": n,
                    "series_no": str(i),
                    "unit": "Million Dollars",
                    "earliest_period": "1994",
                    "earliest_value": a,
                    "latest_period": "2024",
                    "latest_value": b,
                    "change_latest_minus_earliest": b - a,
                    "observation_count": 31,
                }
                for i, (n, a, b) in enumerate(rows, 1)
            ],
        }
        text = (
            evidence_bound_generic_answer(
                "How has direct investment abroad changed since 1994?", [result]
            )
            or ""
        )
        positions = [
            text.find(f"| {name} |") for name in ("Total", "Manufacturing", "Construction")
        ]
        self.assertTrue(all(p > 0 for p in positions), text[:300])
        self.assertEqual(positions, sorted(positions))


class LegendNames(unittest.TestCase):
    """Legends keep only the words that tell series apart (a real chart showed six legend
    entries that all read "Proportion Of Students In Primary Education Achieving At Least A ...")."""

    def test_shared_words_are_dropped(self):
        from utils.chart_order import short_series_names

        stem = (
            "Proportion Of Students In {} Education Achieving At Least A Minimum Proficiency "
            "Level In {}"
        )
        names = [
            stem.format("Primary", "Mathematics / Female"),
            stem.format("Secondary", "Reading / Male"),
        ]
        self.assertEqual(
            list(short_series_names(names).values()),
            ["Primary · Mathematics · Female", "Secondary · Reading · Male"],
        )

    def test_names_stay_when_shortening_would_confuse(self):
        from utils.chart_order import short_series_names

        for names in (
            ["Total"],
            ["Manufacturing", "Construction"],
            ["Total Imports", "Total Imports / Petroleum"],
        ):  # one would be empty
            with self.subTest(names=names):
                self.assertEqual(short_series_names(names), {n: n for n in names})


if __name__ == "__main__":
    unittest.main()
