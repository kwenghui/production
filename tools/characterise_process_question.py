"""Characterisation check for process_question() (behaviour must not change).

Runs app.process_question() through fixed scenarios with a fake analytical core
and records everything observable: return value, stored messages, last
response, audit-log calls and warnings shown. Run it on two copies of Sage
(e.g. before and after a refactor) and compare the JSON files:

    python tools/characterise_process_question.py OLD_SAGE_DIR old.json
    python tools/characterise_process_question.py NEW_SAGE_DIR new.json
    python tools/characterise_process_question.py --compare old.json new.json

Only the random incident reference in the "crash" scenario may differ, plus
any scenario named with --allow (an intended change). Example: the round-2
refactor deliberately stores fewer chart-context fields, so compare it with
    --compare old.json new.json --allow chart_delivered
Install plotly (or the real requirements) so that charts can pass preflight.
Streamlit is replaced by a tiny in-memory stand-in, so no browser, network or
OpenAI key is needed. Nothing is written except the output JSON.
"""

import json, sys, types, copy

if sys.argv[1:2] == ["--compare"]:
    # Optional: --allow a,b  lists scenarios whose difference is intended.
    allowed = {"crash"}
    if "--allow" in sys.argv:
        allowed |= set(sys.argv[sys.argv.index("--allow") + 1].split(","))
    old, new = (json.load(open(p)) for p in sys.argv[2:4])
    diff = [k for k in old if old[k] != new.get(k)]
    print("Scenarios differing:", diff or "none")
    sys.exit(0 if set(diff) <= allowed else 1)

sys.path.insert(0, sys.argv[1])
sys.path.insert(1, str(__import__('pathlib').Path(__file__).resolve().parent.parent))


from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
import streamlit as st
import app
from utils.errors import SageError
from utils.token_usage import QuestionTokenBudgetExceeded

calls = []


def _patch(name, value):
    """Replace ``name`` wherever the page code imported it (app.py and ui/*)."""
    for module_name, module in list(sys.modules.items()):
        if (module_name == "app" or module_name.startswith("ui.")) and hasattr(module, name):
            setattr(module, name, value)


_patch(
    "log_failed_question",
    lambda q, cat, is_manual=False: calls.append(("failed", q[:30], cat, is_manual)) or True,
)
_patch(
    "log_successful_question",
    lambda q, is_manual=False, status=None: calls.append(("success", q[:30], status, is_manual))
    or True,
)
_patch(
    "log_answer_decision",
    lambda q, ans, resp, failure_category=None: calls.append(
        ("decision", failure_category, sorted((ans or {}).keys()))
    ),
)
from utils.session_security import CLIENT_LIMITER, LIMITER  # noqa: E402  (shared objects)

LIMITER.check = lambda *a, **k: None
CLIENT_LIMITER.check = lambda *a, **k: None
shown = []
st.warning = lambda m, *a, **k: shown.append(("warning", str(m)))
st.error = lambda m, *a, **k: shown.append(("error", str(m)))

URL = "https://tablebuilder.singstat.gov.sg/table/TS/M810001"


def row(**extra):
    r = {
        "success": True,
        "table_id": "M810001",
        "source_url": URL,
        "title": "Total Population, Annual",
        "observations": [
            {"period": "2023", "value": 5917600},
            {"period": "2024", "value": 6036900},
        ],
    }
    r.update(extra)
    return r


class FakeCore:
    CHAT_MODEL = "fake"

    def __init__(self, response=None, exc=None):
        self._response, self._exc = response, exc
        self.client = types.SimpleNamespace(
            responses=types.SimpleNamespace(
                create=lambda **k: types.SimpleNamespace(
                    output_text="Population rose to 6,036,900 in " "2024."
                )
            )
        )

    def preview_user_intent(self, **k):
        calls.append(("preview", sorted(k)))
        return {"intent": "new"}

    def ask_sage(self, **k):
        calls.append(("ask_sage", {x: k[x] for x in sorted(k) if x not in ("chat_history",)}))
        if self._exc:
            raise self._exc
        return copy.deepcopy(self._response)

    def enhance_rag_after_verified_tools(self, q, rag, rows, dataset_ids=None):
        calls.append(("rag", dataset_ids))
        return list(rag) + ["extra"]

    def compact_tool_result_for_model(self, item):
        return json.dumps(item)[:2000]

    def __getattr__(self, name):
        def f(*a, **k):
            calls.append(("core." + name,))
            return a[1] if len(a) > 1 else None

        return f


class Boom(SageError):
    code = "boom"
    user_message = "Capacity issue."


import logging

logging.disable(logging.CRITICAL)

scenarios = {
    "empty": dict(q="   "),
    "too_long": dict(q="x" * 5000),
    "security": dict(resp={"security_blocked": True, "answer": "No.", "tool_results": []}),
    "scope": dict(resp={"scope_blocked": True, "answer": "Out of scope.", "tool_results": []}),
    "no_evidence": dict(resp={"answer": "Population is 6 million.", "tool_results": []}),
    "verified": dict(
        resp={
            "answer": "Singapore's total population rose from 5,917,600 in 2023 to "
            "6,036,900 in 2024.",
            "tool_results": [row()],
        }
    ),
    "unsupported_numbers": dict(
        resp={
            "answer": "Total population grew 47.3% to 9,999,999 in 2024.",
            "tool_results": [row()],
        }
    ),
    "refusal": dict(resp={"answer": "I can't answer that question.", "tool_results": [row()]}),
    "empty_answer": dict(resp={"answer": "", "tool_results": [row()]}),
    "incomplete_table": dict(
        resp={
            "answer": "Population was 6,036,900 in 2024.",
            "tool_results": [row(table_complete=False)],
        }
    ),
    "unofficial_url": dict(
        resp={
            "answer": "Population was 6,036,900 in 2024.",
            "tool_results": [row(source_url="https://evil.example.com/x")],
        }
    ),
    "catalogue": dict(
        resp={
            "answer": "Tables: M810001",
            "table_catalogue_listing": True,
            "metadata_only": True,
            "simple_answer": "One table.",
            "tool_results": [],
        }
    ),
    "manual": dict(
        manual=True,
        resp={
            "answer": "Singapore's total population was 6,036,900 in 2024.",
            "tool_results": [row()],
        },
    ),
    "selected_ok": dict(
        manual=True,
        selected="M810001",
        resp={"answer": "Total population was 6,036,900 in 2024.", "tool_results": [row()]},
    ),
    "selected_mismatch": dict(
        manual=True,
        selected="M999999",
        resp={"answer": "Total population was 6,036,900 in 2024.", "tool_results": [row()]},
    ),
    "hint": dict(
        hint="M810001",
        resp={"answer": "Total population was 6,036,900 in 2024.", "tool_results": [row()]},
    ),
    "population_conflict": dict(
        q="How many Singapore citizens were there in 2024?",
        resp={
            "answer": "There were 6,036,900 residents in 2024.",
            "tool_results": [
                row(title="Total Population, Annual", series=[{"series_name": "Total Population"}])
            ],
        },
    ),
    "chart_present": dict(
        q="Show a chart of total population over time",
        resp={
            "answer": "Total population rose from 5,917,600 in 2023 to 6,036,900 " "in 2024.",
            "tool_results": [
                row(
                    chart_spec={
                        "type": "line",
                        "title": "Total Population",
                        "x": ["2023", "2024"],
                        "series": [{"name": "Total Population", "values": [5917600, 6036900]}],
                        "y_title": "Number",
                    },
                    chart_priority=5,
                )
            ],
        },
    ),
    "chart_delivered": dict(
        q="Show a chart of total population over time",
        resp={
            "answer": "Total population rose from 5,917,600 in 2023 to " "6,036,900 in 2024.",
            "tool_results": [
                row(
                    chart_kind="time_series",
                    table_complete=True,
                    chart_source_verified=True,
                    start_year=2023,
                    end_year=2024,
                    age_groups=["x"],
                    chart_spec={
                        "kind": "trend",
                        "title": "Total Population",
                        "unit": "Number",
                        "categories": ["2023", "2024"],
                        "series": [{"name": "Total Population", "values": [5917600, 6036900]}],
                    },
                    chart_priority=5,
                )
            ],
        },
    ),
    "comparison_request": dict(
        q="Compare males and females in the total population in 2024",
        resp={"answer": "Total population was 6,036,900 in 2024.", "tool_results": [row()]},
    ),
    "budget": dict(exc=QuestionTokenBudgetExceeded("over")),
    "budget_debug": dict(exc=QuestionTokenBudgetExceeded("over"), show_debug=True),
    "sage_error": dict(exc=Boom("x")),
    "crash": dict(exc=RuntimeError("kaboom")),
}

out = {}
for name, sc in scenarios.items():
    calls.clear()
    shown.clear()
    st.session_state.clear()
    app.initialise_session_state() if hasattr(app, "initialise_session_state") else None
    st.session_state.core = FakeCore(sc.get("resp"), sc.get("exc"))
    st.session_state["_sage_security_session_id"] = "fixed"
    st.session_state["show_debug"] = sc.get("show_debug", False)
    st.session_state["capture_debug"] = False
    q = sc.get("q", "What was Singapore's total population in 2024?")
    if sc.get("selected"):
        st.session_state["discovery_confirmed_table"] = {
            "table_id": sc["selected"],
            "question": q.strip(),
        }
    if sc.get("hint"):
        st.session_state["discovery_source_hint"] = {"table_id": sc["hint"], "question": q.strip()}
    if sc.get("manual"):
        st.session_state["submit_history"] = [{"role": "user", "content": "earlier"}]
    try:
        ret = app.process_question(q, is_manual=sc.get("manual", False))
    except Exception as e:
        ret = "RAISED " + type(e).__name__ + ": " + str(e)

    def clean(o):
        return json.loads(json.dumps(o, default=lambda x: f"<{type(x).__name__}>", sort_keys=True))

    out[name] = clean(
        dict(
            ret=ret,
            messages=st.session_state.get("messages"),
            last_response=st.session_state.get("last_response"),
            calls=[c for c in calls],
            shown=shown,
            active=st.session_state.get("active_question"),
        )
    )
json.dump(out, open(sys.argv[2], "w"), indent=1, sort_keys=True)
print(
    "scenarios:",
    len(out),
    {
        k: (v["ret"], (v["messages"] or [{}])[-1].get("delivery_status") if v["messages"] else None)
        for k, v in out.items()
    },
)
