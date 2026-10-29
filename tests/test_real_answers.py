"""Regression tests built from three REAL Sage answers (tests/fixtures/real_answers.py).

They pin down how the answer checks treat real official data: what Sage may
rebuild from checked endpoints, which charts it may draw from an answer's own
table, and that one wrong digit is always refused.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fixtures.real_answers import (
    ANSWER_C_AREAS,
    ANSWER_C_AREAS_BAD_TOTAL,  # noqa: E402
    ANSWER_C_LATEST,
    ANSWER_C_RANKED,
    TABLES,
    tool_result,
)
from utils.answer_verification import (
    evidence_bound_generic_answer,  # noqa: E402
    structured_endpoint_chart_from_answer,
)
from utils.chart_selection import (
    verified_same_year_categories_from_answer,  # noqa: E402
    verified_single_series_trend_candidate,
)

QUESTION_A = (
    "What is the make-up of nominal gross value added in Singapore, by industry, "
    "and how has it changed over the years?"
)


class EndpointAnswers(unittest.TestCase):
    """Question A: Sage replaced an unverifiable draft with checked endpoints."""

    def setUp(self):
        self.evidence = [tool_result("M015781", ["1", "2", "3"])]

    def test_rebuilt_answer_matches_what_users_saw(self):
        answer = evidence_bound_generic_answer(QUESTION_A, self.evidence)
        self.assertTrue(answer.startswith("## Verified observations from SingStat M015781"))
        self.assertIn("| Goods Producing Industries | 1960 | 21.2 | 2025 | 24.1 | 2.9 |", answer)
        self.assertIn(
            "| Services Producing Industries | 1960 | 75.4 | 2025 | 71.9 | -3.5 |", answer
        )

    def test_endpoint_chart_is_drawn_from_the_rebuilt_answer(self):
        answer = evidence_bound_generic_answer(QUESTION_A, self.evidence)
        chart = structured_endpoint_chart_from_answer(QUESTION_A, answer, self.evidence)
        self.assertEqual(chart["chart_spec"]["kind"], "grouped_bar")
        self.assertEqual(chart["chart_spec"]["categories"], ["1960", "2025"])
        self.assertEqual(len(chart["chart_spec"]["series"]), 3)

    def test_one_wrong_digit_means_no_chart(self):
        answer = evidence_bound_generic_answer(QUESTION_A, self.evidence)
        wrong = answer.replace("| 24.1 |", "| 24.2 |")
        self.assertIsNone(structured_endpoint_chart_from_answer(QUESTION_A, wrong, self.evidence))

    def test_breakdown_questions_are_not_answered_from_endpoints(self):
        self.assertIsNone(
            evidence_bound_generic_answer(
                "What is the latest breakdown of gross value added by industry?", self.evidence
            )
        )

    def test_unofficial_source_is_never_used(self):
        evidence = [tool_result("M015781", ["1"], url="https://example.com/x")]
        self.assertIsNone(evidence_bound_generic_answer(QUESTION_A, evidence))


class SameYearCategoryCharts(unittest.TestCase):
    """Question C: bars only for categories the answer's own table states correctly."""

    def setUp(self):
        self.response = {"tool_results": [tool_result("M081331", ["2", "3", "4"])]}
        self.question = "What is the latest breakdown of R&D expenditure by institutional sector?"

    def test_latest_table_gives_three_checked_bars(self):
        chart = verified_same_year_categories_from_answer(
            self.response, self.question, ANSWER_C_LATEST
        )
        self.assertEqual(chart["checked_period"], "2023")
        self.assertEqual(chart["chart_spec"]["series"][0]["values"], [8955.99, 1872.64, 2918.28])
        self.assertFalse(chart["ranking_scope_limited"])

    def test_ranked_table_is_marked_as_a_subset_not_a_full_ranking(self):
        chart = verified_same_year_categories_from_answer(
            self.response,
            "Which institutional sector spent the most on R&D in the latest year?",
            ANSWER_C_RANKED,
        )
        self.assertTrue(chart["ranking_scope_limited"])

    def test_trend_questions_get_no_category_chart(self):
        self.assertIsNone(
            verified_same_year_categories_from_answer(
                self.response, "How has R&D expenditure changed over time?", ANSWER_C_LATEST
            )
        )

    def test_total_row_must_equal_the_sum_of_the_categories(self):
        response = {"tool_results": [tool_result("M081331", ["1.1", "1.3", "1.4", "1.5", "1.10"])]}
        question = "What is the latest breakdown of R&D expenditure by research category?"
        self.assertIsNotNone(
            verified_same_year_categories_from_answer(response, question, ANSWER_C_AREAS)
        )
        self.assertIsNone(
            verified_same_year_categories_from_answer(response, question, ANSWER_C_AREAS_BAD_TOTAL)
        )


class SingleSeriesTrendCharts(unittest.TestCase):
    """Question B: one series over time, its endpoints stated in the answer."""

    def setUp(self):
        self.result = tool_result("M451111", ["1"])
        self.question = "How have merchandise imports of electronic products changed since 2025?"

    def test_trend_chart_when_the_answer_states_both_endpoints(self):
        answer = (
            "Total Electronic Products merchandise imports rose from 21,330,228.1 in 2025 "
            "Jan "
            "to 40,350,081.0 in 2026 Sep (SingStat M451111)."
        )
        chart = verified_single_series_trend_candidate(self.question, self.result, answer)
        self.assertEqual(chart["chart_kind"], "verified_single_subject_trend")

    def test_no_chart_when_an_endpoint_is_wrong_or_missing(self):
        wrong = (
            "Total Electronic Products merchandise imports rose from 21,330,228.1 in 2025 Jan "
            "to 40,350,082.0 in 2026 Sep (SingStat M451111)."
        )
        self.assertIsNone(verified_single_series_trend_candidate(self.question, self.result, wrong))
        self.assertIsNone(
            verified_single_series_trend_candidate(
                self.question,
                self.result,
                "Imports of electronic products rose (SingStat M451111).",
            )
        )

    def test_comparison_questions_are_not_single_series_trends(self):
        answer = (
            "Total Electronic Products merchandise imports rose from 21,330,228.1 in 2025 "
            "Jan "
            "to 40,350,081.0 in 2026 Sep (SingStat M451111)."
        )
        self.assertIsNone(
            verified_single_series_trend_candidate(
                "Compare electronic and non-electronic imports since 2025", self.result, answer
            )
        )


class ScopedPreviewCharts(unittest.TestCase):
    """Question C: a preview of three sector lines is accepted only if the answer vouches for each."""

    def setUp(self):
        from utils.scope_chart_bridge import reconcile_scoped_preview

        self.reconcile = reconcile_scoped_preview
        self.result = tool_result("M081331", ["2", "3", "4"])
        self.question = (
            "How has research and development expenditure in Singapore changed over "
            "the years, by institutional sector?"
        )
        self.answer = (
            "SingStat M081331: Private Sector, Institutes Of Higher Learning and "
            "Government Sector (Including Public Research Institutes) all rose."
        )

    def test_accepted_when_every_line_is_named_in_the_answer(self):
        candidate, reason = self.reconcile(self.question, self.result, self.answer)
        self.assertEqual(reason, "verified")
        self.assertEqual(candidate["chart_kind"], "verified_scope_series_trend")

    def test_refused_when_the_answer_omits_a_line(self):
        _, reason = self.reconcile(
            self.question, self.result, "SingStat M081331: Private Sector rose."
        )
        self.assertEqual(reason, "series-not-in-verified-answer")

    def test_refused_when_a_plotted_value_is_not_published(self):
        self.result["chart_spec"]["series"][0]["values"][3] = 123456.789
        _, reason = self.reconcile(self.question, self.result, self.answer)
        self.assertEqual(reason, "value-not-in-verified-claims")

    def test_narrow_question_does_not_get_the_other_lines(self):
        _, reason = self.reconcile(
            "How has private sector R&D expenditure changed?", self.result, self.answer
        )
        self.assertEqual(reason, "narrow-request-extra-series")


class SeveralExactSeriesInOneCall(unittest.TestCase):
    """Questions A and C: the model asked for "series id:1, series id:2, series id:3"
    in one call. That used to match nothing and cost an extra model round."""

    def test_selector_list_selects_those_series_in_order(self):
        os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)
        from tools._stand_ins import install_stand_ins

        install_stand_ins()
        import pandas as pd
        from utils.table_tools import _select_generic_series

        title, unit, series = TABLES["M015781"]
        frame = pd.DataFrame(
            [
                {"series_no": n, "series_name": name, "unit": unit}
                for n, (name, _, _) in series.items()
            ]
        )
        chosen = _select_generic_series(frame, "series id:3, series id:1", max_series=8)
        self.assertEqual(chosen["series_no"].tolist(), ["3", "1"])
        # A label mixed into the list is not an ID list: matched as a phrase instead.
        mixed = _select_generic_series(frame, "series id:1, Services", max_series=8)
        self.assertNotEqual(mixed["series_no"].tolist(), ["1"])


if __name__ == "__main__":
    unittest.main()
