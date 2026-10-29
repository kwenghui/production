"""Survey editions of the same table title (utils/survey_editions.py).

Consolidates the earlier pytest-style files (test_verified_survey_ranking*.py,
test_publication_metadata_v4.py), which CI's `unittest discover` never ran.
"""

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils import survey_editions as se  # noqa: E402

TITLE = (
    "Resident Students Aged 5 Years and Over by Usual Mode of Transport to School, "
    "Difficulty in Performing Basic Activities and Age Group"
)


def payload(tid, table_type, updated="30/06/2026", **extra):
    return {
        "Data": {
            "records": {"id": tid, "tableType": table_type, "dataLastUpdated": updated, **extra}
        }
    }


class ReferenceYears(unittest.TestCase):
    def test_years_come_only_from_survey_statements(self):
        self.assertEqual(
            se._published_reference_year("Reference: General Household Survey 2025"), 2025
        )
        self.assertEqual(se._published_reference_year("Census of Population 2020"), 2020)
        self.assertIsNone(se._published_reference_year("Updated 2026"))

    def test_official_metadata_shape(self):
        self.assertEqual(
            se._published_year_in_official_payload(
                payload("17621", "Census of Population 2020"), "17621"
            ),
            2020,
        )
        self.assertIsNone(
            se._published_year_in_official_payload(
                payload("C020182", "General Household Survey 2025"), "17621"
            )
        )  # wrong table
        self.assertIsNone(
            se._published_year_in_official_payload(payload("17621", "Unspecified"), "17621")
        )
        self.assertEqual(
            se._published_year_in_official_payload(
                payload("C020182", "", description="Reference: General Household Survey 2025"),
                "C020182",
            ),
            2025,
        )

    def test_reference_year_and_update_date_are_separate(self):
        older = se._official_publication_metadata(
            payload("17621", "Census of Population 2020", "30/06/2026"), "17621"
        )
        self.assertEqual(older["reference_year"], 2020)
        self.assertEqual(older["data_last_updated"], "30/06/2026")
        self.assertEqual(older["reference_source"], "Data.records.tableType")
        self.assertEqual(se._official_publication_metadata(payload("17621", "X", ""), "WRONG"), {})

    def test_catalogue_footnotes_and_descriptions(self):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        con.execute("CREATE TABLE kb_tables (table_id TEXT, description TEXT)")
        con.execute("CREATE TABLE kb_series (table_id TEXT, footnote TEXT, description TEXT)")
        con.executemany("INSERT INTO kb_tables VALUES (?,?)", [("17621", ""), ("C020182", "")])
        con.executemany(
            "INSERT INTO kb_series VALUES (?,?,?)",
            [
                ("17621", "Reference: Census of Population 2020", ""),
                ("C020182", "", "Reference: General Household Survey 2025"),
            ],
        )
        self.assertEqual(
            se._survey_years_from_catalog(con, ["17621", "C020182"]),
            {"17621": 2020, "C020182": 2025},
        )


class EditionOrder(unittest.TestCase):
    rows = [
        {"table_id": "17621", "score": 0.0522, "best": {"table_title": TITLE}},
        {"table_id": "C020182", "score": 0.051, "best": {"table_title": TITLE}},
        {"table_id": "17598", "score": 0.0434, "best": {"table_title": "Different subject"}},
    ]
    years = {"17621": 2020, "C020182": 2025}

    def rank(self, question, years):
        return se._rank_survey_editions([dict(r) for r in self.rows], question, years)[0][
            "table_id"
        ]

    def test_newest_first_unless_a_year_is_named(self):
        self.assertEqual(self.rank("How many students travel to school?", self.years), "C020182")
        self.assertEqual(self.rank("How many students in 2020?", self.years), "17621")
        self.assertEqual(self.rank("How many students in 2025?", self.years), "C020182")
        self.assertEqual(self.rank("How many students?", {}), "17621")  # never guessed

    def test_official_lookup_only_for_titles_with_several_editions(self):
        called = []

        def fake(tid):
            called.append(tid)
            return self.years.get(tid)

        with mock.patch.object(se, "_official_reference_year", fake):
            years = se._complete_edition_years([dict(r) for r in self.rows], {})
        self.assertEqual(called, ["17621", "C020182"])
        self.assertEqual(years, self.years)

    def test_failed_lookups_are_retried_after_ten_minutes(self):
        se._CACHED_AT.pop("M000099", None)
        with mock.patch(
            "utils.official_source.fetch_official_metadata", side_effect=ValueError("offline")
        ) as fetch:
            se._official_publication_details("M000099")
            se._official_publication_details("M000099")  # cached failure
            self.assertEqual(fetch.call_count, 1)
            stamp, found = se._CACHED_AT["M000099"]
            se._CACHED_AT["M000099"] = (stamp - 601, found)
            se._official_publication_details("M000099")  # retried
            self.assertEqual(fetch.call_count, 2)


class DataLastUpdated(unittest.TestCase):
    """The date SingStat shows as "Data Last Updated" is shown under the answer."""

    def test_date_is_read_from_the_stored_official_payload(self):
        from utils import official_source

        store = mock.Mock()
        store.get_table.return_value = {"Data": {"dataLastUpdated": "30/06/2026"}}
        with (
            mock.patch.object(official_source, "_LOCAL_STORE", store),
            mock.patch.object(official_source, "SAGE_STORE_ENABLED", True),
        ):
            self.assertEqual(official_source.data_last_updated("C020174"), "30 Jun 2026")
            store.get_table.return_value = {"Data": {}}
            self.assertIsNone(official_source.data_last_updated("C020174"))


if __name__ == "__main__":
    unittest.main()
