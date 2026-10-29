"""Unit tests for the Streamlit-free answer and chart checks.

These modules were moved out of app.py so they can be tested WITHOUT starting
Streamlit or calling OpenAI. Run with:

    python -m unittest discover -s tests

Each test states the rule it protects ("fail closed" = show nothing rather
than an unverified figure).
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.answer_verification import (  # noqa: E402
    displayed_answer,
    full_answer_numbers_supported,
    is_official_source,
    numeric_tokens,
    official_evidence,
)
from utils.chart_selection import choose_chart_result, resolve_chart_spec  # noqa: E402
from utils.plain_summary import generate_simple_answer  # noqa: E402

OFFICIAL_URL = "https://tablebuilder.singstat.gov.sg/table/TS/M810001"


def verified_row(**changes):
    """A minimal successful, complete tool result from an official table."""
    row = {
        "success": True,
        "table_id": "M810001",
        "source_url": OFFICIAL_URL,
        "title": "Total Population, Annual",
        "observations": [
            {"period": "2023", "value": 5917600},
            {"period": "2024", "value": 6036900},
        ],
    }
    row.update(changes)
    return row


class OfficialSourceTests(unittest.TestCase):
    """Only exact official hosts over HTTPS may be cited."""

    def test_accepts_official_https_host(self):
        self.assertTrue(is_official_source(OFFICIAL_URL))

    def test_rejects_lookalikes_and_unsafe_urls(self):
        for url in (
            "http://tablebuilder.singstat.gov.sg/x",  # not HTTPS
            "https://evil.singstat.gov.sg.example.com/",  # look-alike host
            "https://user:pw@data.gov.sg/",  # credentials
            "https://data.gov.sg:8443/",  # odd port
            "not a url",
            None,
        ):
            with self.subTest(url=url):
                self.assertFalse(is_official_source(url))


class OfficialEvidenceTests(unittest.TestCase):
    """Only successful, complete, officially sourced rows count as evidence."""

    def test_keeps_only_complete_successful_rows(self):
        good = verified_row()
        response = {
            "tool_results": [
                good,
                verified_row(table_complete=False),  # incomplete table
                verified_row(analysis_ready=False),  # not ready for analysis
                verified_row(success="true"),  # must be the boolean True
                verified_row(source_url="https://example.com/x"),  # unofficial source
                "not a dict",
            ]
        }
        self.assertEqual(official_evidence(response), [good])

    def test_blocked_requests_have_no_evidence(self):
        for flag in ("security_blocked", "scope_blocked"):
            with self.subTest(flag=flag):
                self.assertEqual(
                    official_evidence({flag: True, "tool_results": [verified_row()]}), []
                )


class DisplayedAnswerTests(unittest.TestCase):
    """The AI text is shown only when official evidence exists (fail closed)."""

    def test_no_evidence_means_unverified(self):
        text, verified = displayed_answer({"answer": "6 million", "tool_results": []})
        self.assertFalse(verified)
        self.assertNotIn("6 million", text)

    def test_evidence_keeps_ai_text(self):
        self.assertEqual(
            displayed_answer({"answer": "hi", "tool_results": [verified_row()]}), ("hi", True)
        )


class NumericCheckTests(unittest.TestCase):
    """Every number in the answer must appear in the evidence."""

    def test_numeric_tokens_ignore_urls_and_table_ids(self):
        tokens = numeric_tokens(
            "Population was 6,036,900 (+2.0%) in 2024 " "(M810001, https://example.gov/123)."
        )
        self.assertEqual(tokens, {"6036900", "2.0", "2024"})

    def test_supported_numbers_pass(self):
        self.assertTrue(
            full_answer_numbers_supported(
                "Population rose from 5,917,600 in 2023 to 6,036,900 in 2024.",
                [verified_row()],
                "How has the population changed?",
            )
        )

    def test_invented_number_fails(self):
        with self.assertLogs("sage_ui", level="WARNING"):
            self.assertFalse(
                full_answer_numbers_supported(
                    "Population was 9,999,999 in 2024.",
                    [verified_row()],
                    "What was the population in 2024?",
                )
            )


class ChartSelectionTests(unittest.TestCase):
    """At most one chart; integrity-checked charts beat generic previews."""

    def test_verified_chart_beats_high_priority_preview(self):
        preview = {"success": True, "chart_spec": {"title": "preview"}, "chart_priority": 999}
        checked = {
            "success": True,
            "chart_spec": {"title": "checked"},
            "chart_kind": "verified_trend",
        }
        self.assertIs(choose_chart_result({"tool_results": [preview, checked]}), checked)

    def test_suppressed_charts_return_none(self):
        response = {"suppress_charts": True, "tool_results": [{"success": True, "chart_spec": {}}]}
        self.assertIsNone(choose_chart_result(response))

    def test_view_follows_question_wording(self):
        selected = {
            "chart_spec": {"view": "default"},
            "chart_variants": {"share": {"view": "share"}},
        }
        self.assertEqual(
            resolve_chart_spec({"request_summary": "share of residents"}, selected),
            {"view": "share"},
        )
        self.assertEqual(
            resolve_chart_spec({"request_summary": "total residents"}, selected),
            {"view": "default"},
        )


class SimpleAnswerTests(unittest.TestCase):
    """No summary is invented when there is no evidence (and no model call)."""

    def test_no_evidence_gives_fixed_message_without_model(self):
        # core=None proves no model call is attempted on this path.
        text = generate_simple_answer({"tool_results": []}, "anything", None)
        self.assertIn("don't have enough checked data", text)


if __name__ == "__main__":
    unittest.main()
