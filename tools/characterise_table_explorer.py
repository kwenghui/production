"""Characterise ui/table_explorer.render_table_explorer ("Explore table further").

Draws the explorer for real tables from tests/fixtures/real_answers.py in three
situations: a multi-dimensional (cube) table, a flat time-series table, and a
table that fails to load (the answer's own verified chart is shown instead).
Every Streamlit call is recorded; prints one digest per situation and an
overall digest: two copies of Sage that draw the same explorer print the same.

    python tools/characterise_table_explorer.py OLD_SAGE_DIR
    python tools/characterise_table_explorer.py NEW_SAGE_DIR
"""

import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, sys.argv[1])
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
calls = []


class _Block:
    """A recorded container (columns, spinner ...): usable with ``with``."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _recorder(name):
    def record(*args, **kwargs):
        calls.append(
            [name, [a for a in args if isinstance(a, (str, int, float))], sorted(k for k in kwargs)]
        )
        if name == "columns":
            count = args[0] if isinstance(args[0], int) else len(args[0])
            return [_Block() for _ in range(count)]
        if name in ("container", "spinner"):
            return _Block()
        if name == "selectbox":
            return args[1][kwargs.get("index", 0)]
        return None

    return record


for _name in (
    "markdown",
    "caption",
    "info",
    "warning",
    "text_input",
    "button",
    "data_editor",
    "columns",
    "container",
    "spinner",
    "selectbox",
):
    setattr(st, _name, _recorder(_name))
st.column_config = types.SimpleNamespace(
    CheckboxColumn=lambda *a, **k: None, TextColumn=lambda *a, **k: None
)
import ui.table_explorer as explorer  # noqa: E402
import utils.sage_design as design  # noqa: E402

design.render_data_attribution = _recorder("attribution")
design.render_readable_table = _recorder("readable_table")
explorer.render_lightweight_chart = lambda spec, **kwargs: calls.append(
    ["chart", spec.get("kind"), len(spec.get("series") or []), len(spec.get("categories") or [])]
)

TABLE_ID = "M081331"
TITLE, UNIT, SERIES = fixtures.TABLES[TABLE_ID]


class FakeCore:
    """Serves the real R&D table as a flat table, or as a two-column cube."""

    def __init__(self, broken=False):
        self.broken = broken

    def retrieve_generic_singstat_table(self, table_id):
        if self.broken:
            raise OSError("offline")
        rows = [
            {
                "series_no": number,
                "series_name": name,
                "unit": UNIT,
                "period": period,
                "year": int(period),
                "value": value,
            }
            for number, (name, periods, values) in SERIES.items()
            for period, value in zip(periods, values)
        ]
        return pd.DataFrame(rows), {
            "title": TITLE,
            "table_complete": True,
            "source_url": f"https://tablebuilder.singstat.gov.sg/table/TS/{table_id}",
        }

    def load_table_payload(self, table_id):
        return {
            "row": [
                {
                    "rowText": name,
                    "columns": [
                        {
                            "key": period,
                            "columns": [{"key": "Million Dollars", "value": str(value)}],
                        }
                        for period, value in list(zip(periods, values))[-3:]
                    ],
                }
                for name, periods, values in SERIES.values()
            ]
        }


def run(name, result, core):
    calls.clear()
    st.session_state.clear()
    st.session_state.active_question = "How has R&D expenditure changed over the years?"
    st.session_state.last_response = {"tool_results": [result]}
    shown = explorer.render_table_explorer(
        {"source_question": st.session_state.active_question}, core
    )
    return [name, shown, list(calls)]


def main():
    flat = fixtures.tool_result(TABLE_ID, ["2", "3", "4"])
    cube = dict(
        flat,
        table_id="D" + TABLE_ID[1:],
        is_crosstab=True,
        source_url="https://tablebuilder.singstat.gov.sg/table/CT/D" + TABLE_ID[1:],
    )
    fallback = fixtures.tool_result(TABLE_ID, ["2"])
    records = [
        run("flat_table", flat, FakeCore()),
        run("cube_table", cube, FakeCore()),
        run("failed_load", fallback, FakeCore(broken=True)),
    ]
    digests = []
    for name, shown, recorded in records:
        digest = hashlib.sha256(
            json.dumps([shown, recorded], sort_keys=True, default=str).encode()
        ).hexdigest()
        digests.append(digest)
        print(name, shown, len(recorded), "calls", digest[:16])
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(records), "situations")


if __name__ == "__main__":
    main()
