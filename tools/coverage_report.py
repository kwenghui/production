"""Line coverage of app.py, ui/ and utils/ by the unit tests, using only the standard library.

    python tools/coverage_report.py              # all tests except the slow golden check
    python tools/coverage_report.py --all        # include tests/test_behaviour_golden.py
    python tools/coverage_report.py --behaviour  # also count the 11 behaviour checks
    python tools/coverage_report.py --min 60     # exit 1 if total coverage is below 60%

Works without installing anything (it uses sys.settrace). CI uses the
faster coverage.py instead; both count the same thing: the share of
executable lines that ran at least once.

The behaviour checks (tools/characterise_*.py) exercise most of the code but
run as separate programs; --behaviour runs each under the same tracer and
merges what they reach.
"""

import ast
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TARGETS = [
    ROOT / "app.py",
    *sorted((ROOT / "ui").glob("*.py")),
    *sorted((ROOT / "utils").glob("*.py")),
]


def executable_lines(path):
    """Line numbers of statements in ``path`` (docstrings excluded)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            lines.add(node.lineno)
    return lines


def run_tests_with_tracing(include_golden):
    """Run the unit tests, recording every (file, line) executed in Sage's code."""
    hits = set()
    tracer = _tracer_for({str(path) for path in _TARGETS}, hits)
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    if not include_golden:
        suite = unittest.TestSuite(
            test
            for group in _flatten(suite)
            for test in [group]
            if "test_behaviour_golden" not in test.id()
        )
    threading.settrace(tracer)
    sys.settrace(tracer)
    try:
        result = unittest.TextTestRunner(verbosity=0, stream=sys.stderr).run(suite)
    finally:
        sys.settrace(None)
        threading.settrace(None)
    return hits, result.wasSuccessful()


def _tracer_for(wanted, hits):
    """A sys.settrace function recording (file, line) for Sage's own files."""

    def tracer(frame, event, _arg):
        if frame.f_code.co_filename not in wanted:
            return None
        if event in ("call", "line"):
            hits.add((frame.f_code.co_filename, frame.f_lineno))
        return tracer

    return tracer


def _child(output, tool, tool_args):
    """Run one behaviour tool in THIS process under the tracer; save the hits as JSON."""
    import json
    import runpy

    hits = set()
    tracer = _tracer_for({str(path) for path in _TARGETS}, hits)
    sys.argv = [tool, *tool_args]
    sys.path.insert(0, str(Path(tool).parent))
    threading.settrace(tracer)
    sys.settrace(tracer)
    try:
        runpy.run_path(tool, run_name="__main__")
    except SystemExit:
        pass
    finally:
        sys.settrace(None)
        Path(output).write_text(json.dumps(sorted(hits)), encoding="utf-8")


def behaviour_hits():
    """Lines reached by every behaviour-characterisation tool (each in its own process)."""
    import json
    import os
    import subprocess
    import tempfile
    from tools.behaviour_digests import _DIGEST_TOOLS, _JSON_TOOLS

    hits = set()
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    with tempfile.TemporaryDirectory() as folder:
        for name in (*_DIGEST_TOOLS, *_JSON_TOOLS, "characterise_page"):
            output = Path(folder) / f"{name}.json"
            extra = [str(Path(folder) / f"{name}.out.json")] if name in _JSON_TOOLS else []
            subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--child",
                    str(output),
                    str(ROOT / "tools" / f"{name}.py"),
                    str(ROOT),
                    *extra,
                ],
                cwd=folder,
                env=env,
                capture_output=True,
                timeout=1800,
                check=False,
            )
            if output.exists():
                hits |= {tuple(item) for item in json.loads(output.read_text(encoding="utf-8"))}
    return hits


def _flatten(suite):
    """Every individual test case in a (nested) suite."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def main(argv=None):
    """Print per-module and total coverage; optionally enforce a minimum."""
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["--child"]:
        _child(args[1], args[2], args[3:])
        return 0
    minimum = float(args[args.index("--min") + 1]) if "--min" in args else None
    hits, passed = run_tests_with_tracing("--all" in args)
    if "--behaviour" in args:
        hits |= behaviour_hits()
    total_lines = total_hit = 0
    rows = []
    for path in _TARGETS:
        lines = executable_lines(path)
        covered = {line for line in lines if (str(path), line) in hits}
        total_lines += len(lines)
        total_hit += len(covered)
        rows.append(
            (
                100 * len(covered) / max(1, len(lines)),
                path.relative_to(ROOT).as_posix(),
                len(covered),
                len(lines),
            )
        )
    for percent, name, covered, lines in sorted(rows):
        print(f"{percent:5.1f}%  {covered:5d}/{lines:<5d} {name}")
    total = 100 * total_hit / max(1, total_lines)
    print(
        f"TOTAL {total:.1f}% of {total_lines} executable lines (tests "
        f"{'passed' if passed else 'FAILED'})"
    )
    return 1 if (minimum is not None and total < minimum) or not passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
