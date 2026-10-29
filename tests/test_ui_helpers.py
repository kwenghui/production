"""The operator self-test runner, opt-in diagnostics, the discovery pipeline and
two small core helpers, run with a stand-in Streamlit and temporary files.
"""

import json
import logging
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-" + "a" * 40)

from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
import streamlit as st  # noqa: E402

from tests.test_discovery_and_ingest import discovery_index  # noqa: E402
from ui import auto_test, debug_capture  # noqa: E402
from utils import catalogue_response, kb, kb_discovery_bridge, question_pipeline  # noqa: E402
from utils.tool_budget_finalization import finalize_after_tool_budget  # noqa: E402

QUESTIONS = [
    {"question": f"Sample question {i}?", "section": "Population", "tag": "CHART"} for i in range(3)
]


class SelfTestRunnerTests(unittest.TestCase):
    def setUp(self):
        st.session_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.checkpoint = Path(self.tmp.name) / "progress.json"

    def test_start_options(self):
        options = auto_test.parse_test_options(["--", "--test", "--test-interval", "5"])
        self.assertTrue(options.test)
        self.assertEqual(options.test_interval, 5)
        for bad in (["--test-interval", "0.5"], ["--test-reset"]):
            with self.subTest(args=bad), self.assertRaises(ValueError):
                auto_test.parse_test_options(bad)

    def test_checkpoint_round_trip_and_bank_change(self):
        auto_test.write_test_checkpoint(2, "bank-a", 3, path=self.checkpoint)
        self.assertEqual(auto_test.read_test_checkpoint(self.checkpoint, "bank-a", 3), 2)
        # A changed question bank must never be resumed half-way: refuse loudly.
        with self.assertRaises(ValueError):
            auto_test.read_test_checkpoint(self.checkpoint, "bank-b", 3)

    def test_tick_runs_the_next_question_when_due(self):
        st.session_state.update(
            auto_test_bank_hash="bank-a",
            answer_phase="ready",
            auto_test_running_question=None,
            auto_test_completed=1,
            auto_test_next_due_at=time.time() - 1,
            messages=[],
        )
        with mock.patch.object(auto_test, "select_sample_question") as select:
            auto_test.auto_test_tick(QUESTIONS)
        select.assert_called_once_with(QUESTIONS[1], 1)
        self.assertEqual(st.session_state.auto_test_running_index, 1)

    def test_tick_waits_and_finishes(self):
        st.session_state.update(
            auto_test_bank_hash="bank-a",
            answer_phase="ready",
            auto_test_running_question=None,
            auto_test_completed=3,
            auto_test_next_due_at=time.time() - 1,
        )
        auto_test.auto_test_tick(QUESTIONS)
        self.assertTrue(st.session_state.auto_test_finished)

    def test_pause_toggles(self):
        st.session_state.auto_test_paused = False
        auto_test.toggle_auto_test_pause()
        self.assertTrue(st.session_state.auto_test_paused)


class DiagnosticsTests(unittest.TestCase):
    """Opt-in diagnostics are redacted and written for one question only."""

    def test_secrets_and_paths_are_redacted(self):
        text = debug_capture._redact_debug_text(
            "key sk-abcdefghijklmnopqrstuvwx1234 at " "C:\\Users\\ana\\x"
        )
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwx1234", text)

    def test_one_question_is_captured_to_the_debug_file(self):
        # Written only when the operator opted in (SAGE_FULL_DEBUG_LOG=1).
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "debug.txt"
            st.session_state.clear()
            st.session_state.update(
                last_response={"tool_results": []},
                messages=[{"role": "assistant", "content": "x", "verified": False}],
            )
            with (
                mock.patch.object(debug_capture, "DEBUG_LOG_FILE", target),
                mock.patch.object(debug_capture, "SAGE_FULL_DEBUG_LOG", True),
            ):
                with debug_capture.capture_query_debug("How many residents?"):
                    print("tool output during the question")
                    logging.getLogger("sage").warning("a warning during the question")
            record = target.read_text(encoding="utf-8")
            # One record per question: a digest, the outcome, the chart audit; never the
            # question, the answers or the console text (they may contain personal data).
            self.assertIn("Question digest:", record)
            self.assertNotIn("How many residents?", record)
            self.assertNotIn("tool output during the question", record)


class DiscoveryPipelineTests(unittest.TestCase):
    """Discovery runs without Streamlit and adds knowledge-base candidates as metadata only."""

    def test_plan_with_index_and_knowledge_base(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            data.mkdir()
            index = discovery_index(data)
            con = kb.open_db(data / "knowledge_base.sqlite3")
            kb.upsert_table(con, "M081331", "Research And Development Expenditure, Annual")
            con.commit()
            con.close()
            outcome = question_pipeline.plan_question_discovery(
                "How has R&D expenditure changed?", index
            )
            self.assertIsNotNone(outcome)
            candidates = kb_discovery_bridge.retrieve_candidates("research and development", folder)
            self.assertIsInstance(candidates, list)
            self.assertEqual(
                question_pipeline.plan_question_discovery("x", index, unsafe=True).plan, None
            )


class SmallCoreHelperTests(unittest.TestCase):
    def test_catalogue_listing_has_no_numbers(self):
        listing = catalogue_response.maybe_catalogue_response(
            "Which tables cover R&D?",
            "R&D tables",
            {"relevant_datasets": []},
            is_catalogue_request=lambda q: True,
            list_tables=lambda q: {"answer": "M081331 R&D", "count": 1, "indexed_count": 10},
        )
        self.assertTrue(listing["table_catalogue_listing"])
        self.assertEqual(listing["tool_results"], [])
        self.assertIsNone(
            catalogue_response.maybe_catalogue_response(
                "How many?", "", {}, is_catalogue_request=lambda q: False, list_tables=None
            )
        )

    def test_final_answer_after_the_tool_budget(self):
        client = types.SimpleNamespace(
            responses=types.SimpleNamespace(
                create=lambda **k: types.SimpleNamespace(
                    output_text="Answer from verified results."
                )
            )
        )
        text = finalize_after_tool_budget(
            client=client,
            conversation=[],
            tool_results=[{"success": True}],
            system_instructions="x",
            chat_model="m",
            logger=logging.getLogger("test"),
        )
        self.assertIn("Answer from verified results.", text)


if __name__ == "__main__":
    unittest.main()
