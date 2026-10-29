"""Keep every module's "Used by:" / "Tested in:" docstring lines true.

Each module in app.py, ui/ and utils/ ends its docstring with a short map:

    Used by: app.py, ui/answer_pipeline.py.
    Tested in: tests/test_generic_rules.py.

Hand-written maps rot (earlier versions named 24 test files that did not
exist). This tool derives the map from the actual imports instead:

    python tools/module_map.py          # report modules whose map is wrong
    python tools/module_map.py --fix    # rewrite the maps in place

tests/test_module_map.py fails when a map is out of date, so the comments
can be trusted.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("ui", "utils")
_MAP_LINE = re.compile(r"^(Used by:|Tested in:)")
_WRAP = 76


def source_files(root=ROOT):
    """app.py plus every module in ui/ and utils/ (package __init__ files skipped)."""
    files = [root / "app.py"]
    for package in PACKAGES:
        files += sorted(p for p in (root / package).glob("*.py") if p.name != "__init__.py")
    return files


def _module_name(path, root=ROOT):
    """'ui/answer_view.py' -> 'ui.answer_view'; 'app.py' -> 'app'."""
    return ".".join(path.relative_to(root).with_suffix("").parts)


def imported_modules(path):
    """Names of ui./utils. modules imported anywhere in ``path`` (incl. inside functions)."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            # "from utils import auth" imports the module utils.auth
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {name for name in found if name.split(".")[0] in PACKAGES}


def importers(root=ROOT):
    """{module name: sorted relative paths of the files that import it}."""
    users = {}
    importing_files = (
        source_files(root)
        + sorted((root / "tests").glob("test_*.py"))
        + sorted((root / "tools").glob("*.py"))
    )
    for path in importing_files:
        for module in imported_modules(path):
            if module != _module_name(path, root):  # a module does not "use" itself
                users.setdefault(module, set()).add(path.relative_to(root).as_posix())
    return {module: sorted(paths) for module, paths in users.items()}


def _wrapped(label, items):
    """'Used by: a, b, c.' wrapped to the docstring width."""
    text, lines = label + " " + ", ".join(items) + ".", []
    while len(text) > _WRAP:
        cut = text.rfind(", ", 0, _WRAP) + 1
        lines.append(text[:cut])
        text = text[cut + 1 :]
    return lines + [text]


def expected_map(path, users, root=ROOT):
    """The map lines this module's docstring should end with."""
    name = _module_name(path, root)
    if name == "app":
        return []  # the entry point is used by Streamlit itself
    callers = [p for p in users.get(name, []) if not p.startswith(("tests/", "tools/"))]
    tools = [p for p in users.get(name, []) if p.startswith("tools/")]
    tests = [p for p in users.get(name, []) if p.startswith("tests/")]
    lines = (
        _wrapped("Used by:", callers + tools) if callers or tools else ["Used by: no other module."]
    )
    if tests:
        lines += _wrapped("Tested in:", tests)
    return lines


def current_and_fixed(path, users, root=ROOT):
    """(current source, source with the docstring map replaced by the true one)."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    first = tree.body[0] if tree.body else None
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return source, source
    lines = source.split("\n")
    start, end = first.lineno - 1, first.end_lineno - 1
    doc = lines[start : end + 1]
    # Put the closing quotes on their own line, so the map can go above them.
    if doc[-1].strip() != '"""':
        doc[-1:] = [doc[-1].rstrip()[:-3], '"""']
    body, closing = doc[:-1], doc[-1]
    # Drop the old map: from a "Used by:"/"Tested in:" line up to the next blank line.
    kept, in_map = [], False
    for line in body:
        if _MAP_LINE.match(line.strip()):
            in_map = True
            continue
        if in_map and line.strip():
            continue
        in_map = False
        kept.append(line)
    while len(kept) > 1 and not kept[-1].strip():
        kept.pop()
    new_map = expected_map(path, users, root)
    if new_map:
        kept += [""] + new_map
    fixed = "\n".join(lines[:start] + kept + [closing] + lines[end + 1 :])
    return source, fixed


def main(argv=None):
    """Report (or with --fix, rewrite) modules whose docstring map is wrong."""
    fix = "--fix" in (argv if argv is not None else sys.argv[1:])
    users = importers()
    wrong = []
    for path in source_files():
        current, fixed = current_and_fixed(path, users)
        if current != fixed:
            wrong.append(path.relative_to(ROOT).as_posix())
            if fix:
                path.write_text(fixed, encoding="utf-8")
    print(("Fixed: " if fix else "Out of date: ") + (", ".join(wrong) or "none"))
    return 0 if fix or not wrong else 1


if __name__ == "__main__":
    raise SystemExit(main())
