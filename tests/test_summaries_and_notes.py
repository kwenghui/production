"""Tests for the plain-language summary and the curated-notes (RAG) helpers.

Uses the three real answers in tests/fixtures/real_answers.py where possible.
No model calls are made: every path tested here is deterministic.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)
from tools._stand_ins import install_stand_ins  # noqa: E402  (no OpenAI package needed)

install_stand_ins()

from tests.fixtures.real_answers import tool_result  # noqa: E402
from utils.answer_verification import evidence_bound_generic_answer  # noqa: E402
from utils.plain_summary import (
    brief_is_source_boilerplate,  # noqa: E402
    generate_simple_answer,
    management_takeaway_fallback,
    structured_series_management_brief,
)
from utils.question_categories import best_category  # noqa: E402

QUESTION_A = (
    "What is the make-up of nominal gross value added in Singapore, by industry, "
    "and how has it changed over the years?"
)


class PlainLanguageSummary(unittest.TestCase):
    """The one-line summary above an answer never adds a number or a claim."""

    def setUp(self):
        self.evidence = [tool_result("M015781", ["1", "2", "3"])]
        self.answer = evidence_bound_generic_answer(QUESTION_A, self.evidence)

    def test_structured_answers_get_a_deterministic_brief(self):
        brief = structured_series_management_brief(self.answer, self.evidence)
        # Same wording users saw: the change, in percentage points, between the endpoints.
        self.assertEqual(
            brief,
            "Goods Producing Industries rose by 2.9 percentage points " "between 1960 and 2025.",
        )

    def test_summary_needs_no_model_for_a_structured_answer(self):
        response = {"tool_results": self.evidence}
        # core=None: any attempt to call the model would fail this test.
        summary = generate_simple_answer(response, self.answer, None, question=QUESTION_A)
        self.assertTrue(summary)

    def test_fallback_and_boilerplate_detection(self):
        self.assertTrue(
            brief_is_source_boilerplate(
                "The following figures are taken directly from the complete official table."
            )
        )
        self.assertFalse(brief_is_source_boilerplate("Private sector spending rose."))
        self.assertTrue(management_takeaway_fallback(self.answer))


class CuratedNotes(unittest.TestCase):
    """RAG helpers: keyword scoring and the labelled text given to the model."""

    def test_keyword_score_is_the_share_of_question_words_found(self):
        from utils.rag_engine import keyword_score

        document = {
            "title": "Total Population",
            "text": "residents and citizens",
            "keywords": ["census"],
        }
        self.assertEqual(keyword_score("population census", document), 1.0)
        self.assertEqual(keyword_score("population tourism", document), 0.5)
        self.assertEqual(keyword_score("", document), 0.0)

    def test_notes_are_labelled_with_their_source(self):
        from utils.rag_engine import format_rag_context

        text = format_rag_context(
            [
                {
                    "id": "N1",
                    "title": "Note",
                    "source_label": "SingStat",
                    "source_url": "https://www.singstat.gov.sg/",
                    "text": "Definition.",
                }
            ]
        )
        for part in (
            "RAG ID: N1",
            "Source: SingStat",
            "URL: https://www.singstat.gov.sg/",
            "Definition.",
        ):
            self.assertIn(part, text)
        self.assertEqual(format_rag_context([]), "")


class SampleCategories(unittest.TestCase):
    def test_every_table_gets_a_category(self):
        for theme, title in (
            ("Economy & Prices", "Share Of Nominal Gross Value Added"),
            ("Industry", "Research And Development Expenditure"),
            ("", "Some Unknown Table"),
        ):
            with self.subTest(title=title):
                category, _ = best_category(theme, title)
                self.assertTrue(category)


class ComparisonBriefIsGenericTests(unittest.TestCase):
    """The one-line comparison summary words every subject the same way."""

    def test_energy_questions_get_the_generic_sentence(self):
        from utils.plain_summary import verified_comparison_table_brief

        answer = (
            "In 2023, the largest was industry.\n\n| Sector | Consumption |\n|---|---|\n"
            "| Industry | 400 |\n| Households | 100 |\n"
        )
        self.assertEqual(
            verified_comparison_table_brief(answer, "Which sector used the most energy?"),
            "Industry had the highest consumption in 2023.",
        )


if __name__ == "__main__":
    unittest.main()
