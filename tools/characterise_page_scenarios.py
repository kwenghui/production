"""Characterise the page in more situations than tools/characterise_page.py.

Runs app.main() through a sequence of realistic situations with a recording
Streamlit stand-in and a fake analytical core, and prints one digest of every
Streamlit call (with its first argument) and the stored answers:

  1. empty page                      5. table choices waiting for the user
  2. a verified answer               6. a verified answer with a chart
  3. the same with technical details 7. rating an answer (thumbs up)
  4. an answer that cannot be verified 8. operator self-test mode

    python tools/characterise_page_scenarios.py SAGE_DIR

Two copies of Sage that draw the same pages print the same digest. No
browser, network or OpenAI key is needed.
"""

import hashlib
import json
import os
import sys
import types
from pathlib import Path

tree = sys.argv[1]
sys.path.insert(0, tree)
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
os.environ.update(Need_Password="0", TECHNICAL_DETAILS="1", SAGE_ALLOW_ADMIN_TESTS="1")
# Independent of the machine: no model override from the environment.
for _name in ("SAGE_CHAT_MODEL", "MIDLIFE_CHAT_MODEL", "SAGE_REASONING_EFFORT"):
    os.environ.pop(_name, None)
from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
import logging  # noqa: E402

import streamlit as st  # noqa: E402

logging.disable(logging.CRITICAL)
trace = []  # (call name, first argument) in order
press = set()  # widget keys whose button "is pressed" in this pass


class Block:
    """A container/column/expander/form: records calls made inside it."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return recorder(name)


def _first_argument(args, kwargs):
    value = args[0] if args else kwargs.get("label", kwargs.get("key", ""))
    return value[:60] if isinstance(value, str) else type(value).__name__


def recorder(name):
    """A stand-in for st.<name> that records the call and returns a sensible value."""

    def call(*args, **kwargs):
        trace.append((name, _first_argument(args, kwargs)))
        if name == "columns":
            spec = args[0] if args else kwargs.get("spec", 1)
            return [Block() for _ in range(spec if isinstance(spec, int) else len(spec))]
        if name in (
            "container",
            "spinner",
            "expander",
            "form",
            "popover",
            "status",
            "empty",
            "tabs",
            "chat_message",
        ):
            return [Block(), Block()] if name == "tabs" else Block()
        if name in ("button", "form_submit_button", "checkbox", "toggle"):
            return kwargs.get("key") in press
        if name in ("text_area", "text_input"):
            return kwargs.get("value", "")
        if name in ("selectbox", "radio"):
            options = list(args[1] if len(args) > 1 else kwargs.get("options", []))
            return options[kwargs.get("index", 0) or 0] if options else None
        if name == "feedback":
            return None
        return None

    return call


st.__getattr__ = recorder
for _name in (
    "markdown",
    "caption",
    "info",
    "warning",
    "error",
    "success",
    "write",
    "title",
    "subheader",
    "header",
    "divider",
    "set_page_config",
    "html",
    "image",
    "toast",
    "rerun",
    "download_button",
    "data_editor",
    "dataframe",
    "code",
    "json",
    "metric",
    "progress",
    "columns",
    "container",
    "spinner",
    "empty",
    "expander",
    "button",
    "text_area",
    "text_input",
    "selectbox",
    "radio",
    "toggle",
    "checkbox",
    "form",
    "form_submit_button",
    "popover",
    "tabs",
    "plotly_chart",
    "altair_chart",
    "vega_lite_chart",
    "link_button",
    "feedback",
):
    setattr(st, _name, recorder(_name))
st.sidebar = Block()
st.stop = lambda: (_ for _ in ()).throw(SystemExit("st.stop"))
st.fragment = lambda f=None, **k: (f if f else (lambda g: g))
st.secrets, st.query_params = {}, {}
st.context = types.SimpleNamespace(
    headers={}, ip_address=None, theme=types.SimpleNamespace(type="light")
)
st.column_config = types.SimpleNamespace(
    **{
        n: (lambda *a, **k: None)
        for n in ("CheckboxColumn", "TextColumn", "NumberColumn", "LinkColumn")
    }
)

import app  # noqa: E402


def _fixed_question_bank():
    """A small sample-question bank built from fixed titles, so the page never depends on
    the questions/ folder of the copy being checked (its size shows on the page)."""
    import sqlite3
    import tempfile
    from utils.question_generator import generate_question_bank

    folder = Path(tempfile.mkdtemp())
    con = sqlite3.connect(folder / "kb.sqlite3")
    con.execute("CREATE TABLE kb_tables (table_id TEXT, title TEXT)")
    con.executemany(
        "INSERT INTO kb_tables VALUES (?,?)",
        [
            ("M810001", "Indicators On Population, Annual"),
            ("M810011", "Population Density, Annual"),
            ("M015781", "Share Of Nominal Gross Value Added, By Industry, Annual"),
            ("M081331", "Research And Development Expenditure By Institutional Sector, Annual"),
        ],
    )
    con.commit()
    con.close()
    generate_question_bank(folder / "kb.sqlite3", folder / "questions.txt", target=6)
    return folder / "questions.txt", folder / "Generated_Question_Audit.csv"


_BANK, _AUDIT = _fixed_question_bank()
for _module in [
    m for name, m in list(sys.modules.items()) if name == "app" or name.startswith("ui.")
]:
    if hasattr(_module, "QUESTION_BANK_FILE"):
        _module.QUESTION_BANK_FILE = _BANK
    if hasattr(_module, "GENERATED_AUDIT_FILE"):
        _module.GENERATED_AUDIT_FILE = _AUDIT

URL = "https://tablebuilder.singstat.gov.sg/table/TS/M810001"
OBSERVATIONS = [
    {"series_name": "Total Population", "series_no": "1", "unit": "Number", "period": p, "value": v}
    for p, v in (("2023", 5917600.0), ("2024", 6036900.0))
]


def verified_result(with_chart=False):
    """One complete official result (optionally with a verified trend chart)."""
    result = {
        "success": True,
        "table_id": "M810001",
        "source_url": URL,
        "table_complete": True,
        "title": "Total Population, Annual",
        "observations": OBSERVATIONS,
        "series_summary": [
            {
                "series_name": "Total Population",
                "series_no": "1",
                "unit": "Number",
                "earliest_period": "2023",
                "earliest_value": 5917600.0,
                "latest_period": "2024",
                "latest_value": 6036900.0,
                "change_latest_minus_earliest": 119300.0,
                "observation_count": 2,
            }
        ],
        "selected_series": [
            {"series_no": "1", "series_name": "Total Population", "unit": "Number"}
        ],
        "numeric_claims": [5917600.0, 6036900.0],
    }
    if with_chart:
        result.update(
            chart_kind="time_series",
            chart_source_verified=True,
            chart_priority=45,
            chart_spec={
                "kind": "trend",
                "title": "Total Population, Annual",
                "unit": "Number",
                "categories": ["2023", "2024"],
                "x_axis_type": "linear_years",
                "series": [{"name": "Total Population", "values": [5917600.0, 6036900.0]}],
            },
        )
    return result


class FakeCore:
    """The analytical core, answering from a scripted response."""

    _config = __import__("ui.config", fromlist=["x"])
    SAGE_CORE_RELEASE = SAGE_CHILD_SOURCE_PATCH_VERSION = getattr(
        _config,
        "SAGE_REQUIRED_CORE_RELEASE",
        getattr(_config, "SAGE_REQUIRED_CORE_PATCH_VERSION", None),
    )
    CHAT_MODEL = "fake"
    client = types.SimpleNamespace(
        responses=types.SimpleNamespace(
            create=lambda **k: types.SimpleNamespace(
                output_text="Total population rose to 6,036,900 " "in 2024."
            )
        )
    )
    response = None

    def preview_user_intent(self, **kwargs):
        return {"intent": "new"}

    def ask_sage(self, **kwargs):
        return json.loads(json.dumps(self.response))

    def enhance_rag_after_verified_tools(self, question, rag, rows, dataset_ids=None):
        return list(rag)

    def compact_tool_result_for_model(self, item):
        return "{}"

    def retrieve_generic_singstat_table(self, table_id):
        raise OSError("offline")

    def load_table_payload(self, table_id):
        raise OSError("offline")


core = FakeCore()
for _module_name, _module in list(sys.modules.items()):
    if _module_name == "app" or _module_name.startswith("ui."):
        if hasattr(_module, "load_core"):
            _module.load_core = lambda api_key: core
        for _fn in (
            "log_failed_question",
            "log_successful_question",
            "log_answer_decision",
            "record_answer_rating",
            "write_test_checkpoint",
        ):
            if hasattr(_module, _fn) and _fn != "record_answer_rating":
                setattr(_module, _fn, lambda *a, **k: True)
from utils.session_security import CLIENT_LIMITER, LIMITER  # noqa: E402

LIMITER.check = CLIENT_LIMITER.check = lambda *a, **k: None


def run_page(label, argv=("app.py",)):
    """Draw the page once; return a summary of what was drawn and stored."""
    trace.clear()
    sys.argv = list(argv)
    try:
        app.main()
        outcome = "finished"
    except SystemExit as stop:
        outcome = f"stopped: {stop}"
    except Exception as error:  # noqa: BLE001 - a crash is part of the recorded behaviour
        outcome = f"crashed: {type(error).__name__}: {error}"[:200]
    messages = st.session_state.get("messages") or []
    stored = [
        {k: m.get(k) for k in ("role", "verified", "delivery_status", "limited_answer")}
        for m in messages
    ]
    return {"label": label, "outcome": outcome, "trace": trace[:], "stored": stored}


def ask(question, response):
    """Queue a typed question and set what the core will answer."""
    core.response = response
    st.session_state.queued_question = (question, True)
    st.session_state.answer_phase = "processing"


def main():
    pages = [run_page("1 empty page")]
    ask(
        "How has Singapore's total population changed?",
        {
            "answer": "Singapore's total population rose from 5,917,600 in 2023 to 6,036,900 in 2024 "
            "(SingStat M810001).",
            "tool_results": [verified_result()],
        },
    )
    pages.append(run_page("2 verified answer"))
    st.session_state.show_debug = True
    pages.append(run_page("3 technical details"))
    st.session_state.show_debug = False
    ask(
        "How many unicorns live in Singapore?",
        {"answer": "There are 42 unicorns.", "tool_results": []},
    )
    pages.append(run_page("4 unverifiable answer"))
    st.session_state.discovery_pending = {
        "question": "How has population changed?",
        "plan": {
            "action": "choose",
            "reason": "multiple-published-measures",
            "indexed_count": 2,
            "choices": [
                {
                    "table_id": "M810001",
                    "title": "Indicators On Population, Annual",
                    "url": URL,
                    "concept": "population-count",
                    "concept_label": "Population counts",
                },
                {
                    "table_id": "M810011",
                    "title": "Population Density, Annual",
                    "url": "https://tablebuilder.singstat.gov.sg/table/TS/M810011",
                    "concept": "other",
                    "concept_label": "Other published measure",
                },
            ],
        },
    }
    pages.append(run_page("5 table choices"))
    st.session_state.discovery_pending = None
    ask(
        "Show a chart of how Singapore's total population changed",
        {
            "answer": "Total Population rose from 5,917,600 in 2023 to 6,036,900 in 2024 "
            "(SingStat M810001).",
            "tool_results": [verified_result(with_chart=True)],
        },
    )
    pages.append(run_page("6 verified answer with chart"))
    press.update(key for name, key in trace if isinstance(key, str) and "rating" in key.casefold())
    pages.append(run_page("7 rating clicked"))
    press.clear()
    pages.append(run_page("8 self-test mode", argv=("app.py", "--test", "--test-interval", "10")))
    text = json.dumps(pages, sort_keys=True, default=str)
    for page in pages:
        print(
            page["label"], "|", page["outcome"], "|", len(page["trace"]), "calls |", page["stored"]
        )
    print(hashlib.sha256(text.encode("utf-8")).hexdigest(), len(pages), "pages")


if __name__ == "__main__":
    main()
