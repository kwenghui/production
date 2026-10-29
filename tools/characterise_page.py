"""Characterise the whole page: run app.main() twice and record every Streamlit call.

Pass 1 draws the empty page; pass 2 answers one queued question with a fake
analytical core. The printed call traces (last line of each pass) must be
identical for two copies of Sage that should behave the same:

    python tools/characterise_page.py OLD_SAGE_DIR > old.txt
    python tools/characterise_page.py NEW_SAGE_DIR > new.txt
    (compare the last line of each file)

Streamlit is replaced by a recorder; no browser, network or key is needed.
"""

import os
import sys
import types
from pathlib import Path

tree = sys.argv[1]
sys.path.insert(0, tree)
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
os.environ.update(Need_Password="0")
from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
import streamlit as st

trace = []  # names of Streamlit calls, in order


class CM:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, n):
        return rec(n)

    def container(self, **k):
        return CM()

    def empty(self):
        return CM()


def rec(name):
    def f(*a, **k):
        trace.append(name)
        if name in ("columns",):
            n = a[0] if isinstance(a[0], int) else len(a[0])
            return [CM() for _ in range(n)]
        if name in ("container", "spinner", "expander", "form", "popover", "status"):
            return CM()
        if name == "empty":
            return CM()
        if name in ("button", "checkbox", "toggle", "form_submit_button"):
            return False
        if name in ("text_area", "text_input"):
            return k.get("value", "")
        if name in ("selectbox", "radio"):
            opts = a[1] if len(a) > 1 else k.get("options", [None])
            return list(opts)[k.get("index", 0) or 0] if opts else None
        return None

    return f


st.__getattr__ = lambda n: rec(n)
for n in [
    "markdown",
    "caption",
    "info",
    "warning",
    "error",
    "write",
    "title",
    "subheader",
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
]:
    setattr(st, n, rec(n))
st.sidebar = CM()
st.columns = rec("columns")
st.container = rec("container")
st.spinner = rec("spinner")
st.empty = rec("empty")
st.expander = rec("expander")
st.button = rec("button")
st.text_area = rec("text_area")
st.text_input = rec("text_input")
st.selectbox = rec("selectbox")
st.radio = rec("radio")
st.toggle = rec("toggle")
st.checkbox = rec("checkbox")
st.form = rec("form")
st.form_submit_button = rec("form_submit_button")
st.popover = rec("popover")
st.stop = lambda: (_ for _ in ()).throw(SystemExit("st.stop"))
st.fragment = lambda f=None, **k: (f if f else (lambda g: g))
st.secrets = {}
st.query_params = {}
st.context = types.SimpleNamespace(
    headers={}, ip_address=None, theme=types.SimpleNamespace(type="light")
)
import logging

logging.disable(logging.CRITICAL)
import app

sys.argv = ["app.py"]
try:
    app.main()
    print("main finished")
except SystemExit as e:
    print("stopped:", e)
except Exception as e:
    import traceback

    traceback.print_exc(limit=3)
print(len(trace), trace[:60])

# ---- second pass: answer a queued question through the whole page ----------
URL = "https://tablebuilder.singstat.gov.sg/table/TS/M810001"


class FakeCore:
    # The release the page requires (named differently in older copies of Sage).
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
                output_text="Population rose to 6,036,900 in 2024."
            )
        )
    )

    def preview_user_intent(self, **k):
        return {"intent": "new"}

    def ask_sage(self, **k):
        return {
            "answer": "Singapore's total population rose from 5,917,600 in 2023 to 6,036,900 "
            "in 2024.",
            "tool_results": [
                {
                    "success": True,
                    "table_id": "M810001",
                    "source_url": URL,
                    "title": "Total Population, Annual",
                    "table_complete": True,
                    "observations": [
                        {"period": "2023", "value": 5917600},
                        {"period": "2024", "value": 6036900},
                    ],
                }
            ],
        }

    def enhance_rag_after_verified_tools(self, q, rag, rows, dataset_ids=None):
        return list(rag)

    def compact_tool_result_for_model(self, item):
        return "{}"

    def retrieve_generic_singstat_table(self, tid):
        raise OSError("offline")

    def load_table_payload(self, tid):
        raise OSError("offline")


fake = FakeCore()
for name, module in list(sys.modules.items()):
    if (name == "app" or name.startswith("ui.")) and hasattr(module, "load_core"):
        module.load_core = lambda api_key: fake
from utils.session_security import LIMITER, CLIENT_LIMITER

LIMITER.check = CLIENT_LIMITER.check = lambda *a, **k: None
for name, module in list(sys.modules.items()):
    if name == "app" or name.startswith("ui."):
        for fn in ("log_failed_question", "log_successful_question", "log_answer_decision"):
            if hasattr(module, fn):
                setattr(module, fn, lambda *a, **k: True)
st.session_state.queued_question = ("How has Singapore's total population changed?", True)
st.session_state.answer_phase = "processing"
trace.clear()
try:
    app.main()
    print("second pass finished")
except SystemExit as e:
    print("stopped:", e)
except Exception:
    import traceback

    traceback.print_exc(limit=4)
msgs = st.session_state.get("messages") or [{}]
print("delivery:", msgs[-1].get("delivery_status"), "verified:", msgs[-1].get("verified"))
print(len(trace), trace)
