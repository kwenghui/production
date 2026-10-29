"""Guard rails: Sage must treat every official table the same way.

These tests fail if a "shortcut" for one table, subject or question creeps
back in. They read the source code (no Streamlit, no network needed).

What they forbid, and why:

1. A SingStat table ID written as a literal in code: the engine would treat
   that table differently from every other.
2. Statistics typed into the code (lists of large numbers): every number
   shown must come from an official table that Sage downloaded and checked.
3. The UI reaching into private (underscore) functions of the analytical
   core: use its public functions so the core can change safely.
4. Subject words inside the decision modules: topic vocabulary belongs in
   utils/vocabulary.py, used only through generic rules.
5. Modules that existed only to special-case one subject.

If a test fails, move the words into utils/vocabulary.py or make the rule
generic. Do not add exceptions to these lists lightly.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_FILES = [
    ROOT / "app.py",
    *sorted((ROOT / "ui").glob("*.py")),
    *sorted((ROOT / "utils").glob("*.py")),
]

# Modules that DECIDE what is shown. They must be free of subject words.
DECISION_MODULES = [
    "app.py",
    *sorted(f"ui/{path.name}" for path in (ROOT / "ui").glob("*.py")),
    "utils/answer_verification.py",
    "utils/plain_summary.py",
    "utils/chart_selection.py",
    "utils/discovery_controller.py",
    "utils/query_text.py",
    "utils/chart_intent.py",
    "utils/scope_chart_bridge.py",
    "utils/cube_cells.py",
    "utils/question_pipeline.py",
]
SUBJECT_WORDS = re.compile(
    r"\b(?:food|expenditures?|labou?r|hdb|nurses?|doctors?|insurance|schools?|pupils?|"
    r"vehicles?|marriages?|unemployment|cpi|gdp|tourism|fatal|injur(?:y|ies)|hawkers?|"
    r"energy|electricity)\b",
    re.IGNORECASE,
)
REMOVED_SPECIAL_CASE_MODULES = [
    "utils/official_survey_fallback.py",  # hard-coded HES 2023 food figures
    "utils/cube_survey.py",  # food-expenditure-only extractor
    "utils/discovery_auto.py",  # population-only routing shortcut
    "utils/kb_resolver.py",  # question-tuned resolver
]


def _string_constants(tree):
    """Yield (node, text) for every string literal that is not a docstring."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node, node.value


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class NoTableSpecificCode(unittest.TestCase):
    def test_no_table_id_literals(self):
        offenders = []
        for path in CODE_FILES:
            for node, text in _string_constants(_parse(path)):
                if re.fullmatch(r"[MDC]\d{5,8}", text.strip()):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {text!r}")
        self.assertEqual(offenders, [], "Table IDs must not be hard-coded")

    def test_no_typed_in_statistics(self):
        """A literal list/tuple/dict holding 3+ numbers >= 100 looks like data."""
        offenders = []
        for path in CODE_FILES:
            for node in ast.walk(_parse(path)):
                if not isinstance(node, (ast.List, ast.Tuple, ast.Dict)):
                    continue
                items = node.values if isinstance(node, ast.Dict) else node.elts
                numbers = [
                    item.value
                    for item in items
                    if isinstance(item, ast.Constant)
                    and type(item.value) in (int, float)
                    and abs(item.value) >= 100
                ]
                # HTTP status codes (e.g. 502, 503, 504) are protocol constants, not data.
                http_codes = all(type(n) is int and 100 <= n <= 599 for n in numbers)
                if len(numbers) >= 3 and not http_codes:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {numbers[:4]}")
        self.assertEqual(offenders, [], "Statistics must come from official tables, not code")

    def test_ui_uses_only_public_core_functions(self):
        offenders = []
        for path in [ROOT / "app.py", *sorted((ROOT / "ui").glob("*.py"))]:
            tree = _parse(path)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr.startswith("_")
                    and not node.attr.startswith("__")
                    and isinstance(node.value, ast.Name)
                    and node.value.id in {"core", "_core"}
                ):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} " f"{node.value.id}.{node.attr}"
                    )
                if isinstance(node, ast.Import) and any(
                    alias.name == "utils.core_secure" for alias in node.names
                ):
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} import " f"utils.core_secure"
                    )
        self.assertEqual(offenders, [], "Use the core's public functions (e.g. load_table_payload)")

    def test_decision_modules_have_no_subject_words(self):
        offenders = []
        for name in DECISION_MODULES:
            path = ROOT / name
            for node, text in _string_constants(_parse(path)):
                match = SUBJECT_WORDS.search(text)
                if match:
                    offenders.append(f"{name}:{node.lineno} {match.group(0)!r} in {text[:60]!r}")
        self.assertEqual(offenders, [], "Move subject words to utils/vocabulary.py")

    def test_special_case_modules_stay_removed(self):
        present = [name for name in REMOVED_SPECIAL_CASE_MODULES if (ROOT / name).exists()]
        self.assertEqual(present, [])

    def test_vocabulary_holds_no_numbers_or_logic_for_one_table(self):
        tree = _parse(ROOT / "utils" / "vocabulary.py")
        functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
        self.assertEqual(functions, ["expand_subject"], "vocabulary.py is data plus one lookup")
        for _node, text in _string_constants(tree):
            self.assertIsNone(re.fullmatch(r"[MDC]\d{5,8}", text.strip()))


class NoTableNamesAnywhere(unittest.TestCase):
    """Not even comments, docstrings or prompt examples name a real table.

    Examples use placeholders (Mxxxxxx, Cxxxxxx, "<subject>, Annual"), so a
    search of the code for a table ID or title finds nothing at all.
    """

    def product_sources(self):
        files = [
            ROOT / "app.py",
            *sorted((ROOT / "ui").glob("*.py")),
            *sorted((ROOT / "utils").glob("*.py")),
        ]
        return {path: path.read_text(encoding="utf-8") for path in files}

    def test_no_table_id_shaped_text(self):
        hits = [
            f"{path.relative_to(ROOT)}: {match}"
            for path, text in self.product_sources().items()
            for match in re.findall(r"\b[A-Z][0-9]{5,8}\b", text)
        ]
        self.assertEqual(hits, [])

    def test_no_official_table_title(self):
        audit = ROOT / "questions" / "Generated_Question_Audit.csv"
        if not audit.is_file():
            self.skipTest("no question audit file in this copy")
        import csv

        with open(audit, encoding="utf-8-sig", newline="") as handle:
            titles = {
                row["table_title"].strip().casefold()
                for row in csv.DictReader(handle)
                if len(row.get("table_title", "").strip()) > 20
            }
        hits = [
            f"{path.relative_to(ROOT)}: {title}"
            for path, text in self.product_sources().items()
            for title in titles
            if title in text.casefold()
        ]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
