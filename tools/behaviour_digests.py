"""Run every behaviour-characterisation tool on ONE copy of Sage; print/save digests.

Each tools/characterise_*.py check reduces thousands of cases to one digest.
tests/golden/behaviour_digests.json stores the digests of the accepted
behaviour, and tests/test_behaviour_golden.py fails when any changes, so the
checks run on every commit in CI.

    python tools/behaviour_digests.py                 # print current digests
    python tools/behaviour_digests.py --update        # accept the current behaviour
    python tools/behaviour_digests.py --against DIR   # same checks on another copy; fail on any
    difference

Use --update ONLY for an intended behaviour change, and say why in the commit.

The digests depend on the Python and pandas/numpy versions (pandas 2 and 3 order
or format some results differently), so the golden file records the environment
it was made in. In a different environment the unit test skips, and CI instead
compares this commit with the previous one ON THE SAME runner (--against), which
is the reliable check.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "behaviour_digests.json"
# Tools that print "<digest> ..." as their last line.
_DIGEST_TOOLS = (
    "characterise_query_table",
    "characterise_audit_rows",
    "characterise_download",
    "characterise_build_chart",
    "characterise_answer_checks",
    "characterise_lightweight_chart",
    "characterise_scoped_preview",
    "characterise_composition",
    "characterise_table_explorer",
    "characterise_page_scenarios",
    "characterise_chart_relevance",
    "characterise_cube_slices",
    "characterise_cube_fallback",
    "characterise_table_choice",
    "characterise_numeric_retry",
    "characterise_chart_gallery",
    "characterise_hybrid_retrieve",
    "characterise_sentence_salvage",
    "characterise_evidence_summary",
    "characterise_series_brief",
    "characterise_question_bank",
    "characterise_retrieval_rows",
    "characterise_kb_inspect",
)
# Tools that write a JSON file of scenarios.
_JSON_TOOLS = ("characterise_process_question", "characterise_ask_sage")


def _run(args):
    """Run a tool in a clean environment (no PYTHONPATH tricks); return stdout."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=tempfile.gettempdir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{args[0]} failed:\n{completed.stderr[-2000:]}")
    return completed.stdout


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_digests(root=ROOT):
    """{tool name: digest} for the copy of Sage at ``root``."""
    digests = {}
    for name in _DIGEST_TOOLS:
        last_line = _run([str(ROOT / "tools" / f"{name}.py"), str(root)]).strip().splitlines()[-1]
        digests[name] = last_line.split()[0]
    for name in _JSON_TOOLS:
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "result.json"
            _run([str(ROOT / "tools" / f"{name}.py"), str(root), str(out)])
            text = out.read_text(encoding="utf-8")
        # The crash scenario quotes a random incident reference: not behaviour.
        digests[name] = _sha(re.sub(r"reference [0-9A-F]{8}", "reference X", text))
    page = _run([str(ROOT / "tools" / "characterise_page.py"), str(root)]).strip().splitlines()
    digests["characterise_page"] = _sha(
        "\n".join(
            line
            for line in page
            if line.startswith(("main finished", "second pass finished", "delivery:"))
            or line[:1].isdigit()
        )
    )
    return digests


def environment() -> dict:
    """The versions the digests depend on (Python minor version, pandas, numpy)."""
    import numpy
    import pandas

    return {
        "python": "%d.%d" % sys.version_info[:2],
        "pandas": pandas.__version__,
        "numpy": numpy.__version__,
    }


def load_golden():
    """(environment or None, {tool: digest}) from the golden file (old flat format accepted)."""
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if "digests" in data:
        return data.get("environment"), data["digests"]
    return None, data


def compare_against(other_root) -> int:
    """Run every check on this copy and on ``other_root`` here; 1 if anything differs."""
    mine, theirs = current_digests(ROOT), current_digests(Path(other_root).resolve())
    changed = sorted(name for name in mine if mine[name] != theirs.get(name))
    if changed:
        print("Behaviour differs from", other_root, "in:", ", ".join(changed))
        return 1
    print(f"Behaviour identical to {other_root} ({len(mine)} checks).")
    return 0


def main(argv=None):
    """Print the digests; --update saves them; --against DIR compares with another copy."""
    args = argv if argv is not None else sys.argv[1:]
    if "--against" in args:
        return compare_against(args[args.index("--against") + 1])
    digests = current_digests()
    print(json.dumps(digests, indent=2))
    if "--update" in args:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(
            json.dumps({"environment": environment(), "digests": digests}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        print(f"Saved {GOLDEN.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
