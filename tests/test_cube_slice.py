"""Tests for multi-dimensional ("cube") tables: one slice at a time, never combined."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.cube_slice import crosstab_overview, cube_query, parse_cube, split_footnote_marker  # noqa: E402


def _row(name, food_2022, housing_2022, food_2023, housing_2023):
    return {
        "rowText": name,
        "columns": [
            {
                "key": "2022",
                "columns": [
                    {"key": "Food", "value": str(food_2022)},
                    {"key": "Housing", "value": str(housing_2022)},
                ],
            },
            {
                "key": "2023",
                "columns": [
                    {"key": "Food", "value": str(food_2023)},
                    {"key": "Housing", "value": str(housing_2023)},
                ],
            },
        ],
    }


# Household spending by income quintile (rows) x spending type (column groups) x year.
PAYLOAD = {
    "row": [
        _row("Total", 500, 900, 520, 950),
        _row("1st Quintile", 300, 400, 310, 420),
        _row("5th Quintile", 800, 1500, 830, 1600),
    ]
}
SOURCE = dict(
    table_id="D10032",
    source_url="https://tablebuilder.singstat.gov.sg/table/CT/D10032",
    title="Household Spending",
)


class CubeOverview(unittest.TestCase):
    def test_default_slice_is_one_group_in_the_latest_period_without_the_total(self):
        overview = crosstab_overview(PAYLOAD, **SOURCE)
        self.assertEqual(overview["slice"], {"column_group": "Food", "period": "2023"})
        self.assertEqual(overview["chart_spec"]["categories"], ["1st Quintile", "5th Quintile"])
        self.assertEqual(overview["chart_spec"]["series"][0]["values"], [310.0, 830.0])
        self.assertEqual(
            overview["options"], {"groups": ["Food", "Housing"], "periods": ["2023", "2022"]}
        )

    def test_user_can_pick_another_slice(self):
        overview = crosstab_overview(PAYLOAD, **SOURCE, group="Housing", period="2022")
        self.assertEqual(overview["chart_spec"]["series"][0]["values"], [400.0, 1500.0])

    def test_the_note_says_which_slice_is_shown(self):
        self.assertIn("Showing “Food” for 2023", crosstab_overview(PAYLOAD, **SOURCE)["note"])


class CubeQueries(unittest.TestCase):
    def test_named_column_group_is_answered_on_its_own(self):
        result = cube_query(PAYLOAD, series_search="Housing")
        self.assertTrue(result["ok"])
        self.assertEqual(result["group"], "Housing")
        self.assertEqual(result["rows"][0]["values"], {"2022": 900.0, "2023": 950.0})
        self.assertEqual(result["other_groups"], ["Food"])
        self.assertIn("not combined", result["note"])

    def test_cells_published_twice_are_never_merged(self):
        duplicated = {"row": PAYLOAD["row"] + [_row("1st Quintile", 999, 999, 999, 999)]}
        cube = parse_cube(duplicated)
        # The four cells of "1st Quintile" now have two values each: none is usable.
        self.assertEqual(cube["ambiguous"], 4)
        self.assertNotIn(("1st Quintile", ("Food",), "2023"), cube["unique"])
        self.assertEqual(cube["unique"][("5th Quintile", ("Food",), "2023")], 830.0)


class FootnoteMarkers(unittest.TestCase):
    def test_markers_glued_to_words_are_split_off(self):
        self.assertEqual(split_footnote_marker("Median1/"), ("Median", ["1"]))
        self.assertEqual(split_footnote_marker("Aged 5-9"), ("Aged 5-9", []))


class RowListTests(unittest.TestCase):
    """A list of exact row labels returns those rows in one call (a real question needed
    nine calls when each occupation had to be asked for separately)."""

    def test_single_period_cube_returns_every_listed_row(self):
        rows = [
            ("Total", 300, 160, 140),
            ("Cleaners, Labourers & Related Workers", 100, 40, 60),
            ("Professionals", 150, 80, 70),
            ("Others1/", 50, 40, 10),
        ]
        payload = {
            "id": "C000001",
            "row": [
                {
                    "rowText": name,
                    "columns": [
                        {
                            "key": "Total",
                            "columns": [
                                {"key": "Total", "value": str(t)},
                                {"key": "Males", "value": str(m)},
                                {"key": "Females", "value": str(f)},
                            ],
                        }
                    ],
                }
                for name, t, m, f in rows
            ],
        }
        found = cube_query(
            payload,
            series_search="Professionals, Cleaners, Labourers & Related " "Workers, Others",
            max_rows=8,
        )
        self.assertTrue(found["ok"])
        self.assertEqual(
            [r["name"] for r in found["rows"]],
            ["Professionals", "Cleaners, Labourers & Related Workers", "Others"],
        )


if __name__ == "__main__":
    unittest.main()
