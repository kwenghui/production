"""Tests for sample-question generation, sidebar "surprise me", source notes and feedback export.

The three real questions in tests/fixtures/real_answers.py were generated from
table titles by utils/question_language.py; these tests pin that down.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.feedback_export import read_feedback, summary, to_csv  # noqa: E402
from utils.question_categories import best_category  # noqa: E402
from utils.question_language import draft_questions, parse_title, plain, problems  # noqa: E402
from utils.source_notes import footnote_text, publication_notes  # noqa: E402
from utils.surprise_deck import draw, topic_of  # noqa: E402


class QuestionsFromTitles(unittest.TestCase):
    """A published title becomes a clear, Singapore-specific question."""

    def test_the_three_real_questions_are_the_best_drafts_of_their_titles(self):
        expected = {
            "Share Of Nominal Gross Value Added, By Industry (SSIC 2020), Annual": "What is the make-up of nominal gross value added in Singapore, by industry, "
            "and how has it changed over the years?",
            "Merchandise Imports Of Machinery And Equipment, Monthly": "How have merchandise imports of machinery and equipment in Singapore changed "
            "month by month?",
            "Research And Development Expenditure By Institutional Sector And Area Of Research, "
            "Annual": "How has research and development expenditure in Singapore changed over the years, "
            "by institutional sector and area of research?",
        }
        for title, question in expected.items():
            with self.subTest(title=title):
                _spec, drafts, rejected = draft_questions(title)
                self.assertEqual(drafts[0].text, question)
                self.assertEqual(rejected, [])

    def test_title_parts_are_recognised(self):
        spec = parse_title("Resident Population By Age Group And Sex, End June, Annual")
        self.assertEqual(spec.frequency, "annual")
        self.assertEqual(spec.dims, ["Age Group", "Sex"])

    def test_every_draft_passes_the_quality_gate(self):
        _spec, drafts, _ = draft_questions(
            "Resident Population By Age Group And Sex, End June, " "Annual"
        )
        self.assertGreaterEqual(len(drafts), 3)
        for draft in drafts:
            with self.subTest(question=draft.text):
                self.assertEqual(problems(draft.text), [])

    def test_quality_gate_rejects_vague_questions(self):
        self.assertIn("does not say where (Singapore)", problems("what?"))

    def test_jargon_is_made_plain(self):
        self.assertEqual(
            plain("Gross Domestic Product At Current Market Prices"), "GDP at current market prices"
        )

    def test_every_table_gets_a_category(self):
        category, source = best_category("Economy", "Share Of Nominal Gross Value Added, Annual")
        self.assertTrue(category)
        self.assertTrue(source)


class SurpriseMe(unittest.TestCase):
    def test_draws_every_question_once_before_repeating(self):
        state = {}
        first_round = [draw([1, 2, 3, 4], state) for _ in range(4)]
        self.assertEqual(sorted(first_round), [1, 2, 3, 4])

    def test_topic_is_the_table_family(self):
        self.assertEqual(topic_of("Resident Households By Type"), "resident households by type")


class SourceNotes(unittest.TestCase):
    def test_reference_survey_is_reported_plainly(self):
        notes = publication_notes(
            "Data are rounded. Figures for 2023 are preliminary. "
            "Source: Labour Force Survey 2023."
        )
        self.assertIn("Reference: Labour Force Survey 2023.", notes)

    def test_footnote_is_read_from_the_payload(self):
        self.assertEqual(footnote_text({"Data": {"footnote": "Note one"}}), "Note one")


class FeedbackExport(unittest.TestCase):
    ROWS = [
        {"answer_id": "a", "rating": "up", "timestamp_utc": "2026-10-26T00:00:00Z"},
        {"answer_id": "a", "rating": "down", "timestamp_utc": "2026-10-26T00:01:00Z"},
        {"answer_id": "b", "rating": "up", "timestamp_utc": "2026-10-26T00:02:00Z"},
    ]

    def test_latest_rating_per_answer_counts(self):
        self.assertEqual(summary(self.ROWS), {"answers": 2, "up": 1, "down": 1, "clicks": 3})

    def test_csv_opens_correctly_in_excel(self):
        self.assertTrue(to_csv(self.ROWS).startswith(b"\xef\xbb\xbftimestamp_utc,"))

    def test_missing_file_gives_no_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(read_feedback(Path(folder) / "none.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
