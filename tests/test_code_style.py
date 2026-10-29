"""Layout rules that ruff format cannot check (ruff format itself runs in CI)."""

import glob
import io
import re
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FILES = sorted(
    {
        str(ROOT / "app.py"),
        *glob.glob(str(ROOT / "ui" / "*.py")),
        *glob.glob(str(ROOT / "utils" / "*.py")),
        *glob.glob(str(ROOT / "tools" / "*.py")),
        *glob.glob(str(ROOT / "tests" / "*.py")),
    }
)


class CodeStyleTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(tokenize, "FSTRING_START"), "needs the Python 3.12 tokenizer")
    def test_no_line_breaks_inside_f_string_expressions(self):
        offenders = []
        for name in FILES:
            depth = 0
            source = Path(name).read_text(encoding="utf-8")
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.FSTRING_START:
                    depth += 1
                elif token.type == tokenize.FSTRING_END:
                    depth -= 1
                elif token.type == tokenize.NL and depth:
                    offenders.append(f"{Path(name).relative_to(ROOT)}:{token.start[0]}")
        self.assertEqual(offenders, [])


class ReadableTableStyle(unittest.TestCase):
    """Text tables (e.g. the token-usage table) never split a short word across lines."""

    def test_columns_size_to_content_and_words_stay_whole(self):
        # "overflow-wrap: anywhere" lets the browser shrink a column to one letter (it
        # also lowers the column's minimum width), so it must never apply to table cells.
        css = re.sub(
            r"/\*.*?\*/",
            "",
            (ROOT / "assets" / "sage_theme.css").read_text(encoding="utf-8"),
            flags=re.S,
        )  # comments do not style anything
        rules = [
            rule
            for rule in css.split("}")
            if "readable-table" in rule
            or ":is(p,li,td,th)" in rule
            or ".st-key-sage_answer_surface {" in rule
        ]
        self.assertTrue(rules)
        for rule in rules:
            with self.subTest(rule=rule.strip()[:60]):
                self.assertNotIn("anywhere", rule)
                self.assertNotIn("break-all", rule)
        self.assertIn("table-layout: auto", css)

    def test_short_values_stay_whole_and_prose_keeps_a_readable_width(self):
        from tools._stand_ins import install_stand_ins

        install_stand_ins()
        from utils.sage_design import readable_table_html

        html = readable_table_html(
            [
                {
                    "API": "embeddings",
                    "Model": "text-embedding-3-small",
                    "What": "Turn the question into a vector to find related " "tables.",
                }
            ]
        )
        self.assertIn('<td class="sage-nowrap" data-label="API">embeddings</td>', html)
        self.assertIn('<td class="sage-nowrap" data-label="Model">', html)
        self.assertIn('<td class="sage-prose" data-label="What">', html)

    def test_phone_cards_never_overflow_their_box(self):
        css = (ROOT / "assets" / "sage_theme.css").read_text(encoding="utf-8")
        self.assertIn(
            ".sage-readable-table :is(tbody,tr,td) { display:block; width:100%; "
            "box-sizing:border-box; }",
            css,
        )


if __name__ == "__main__":
    unittest.main()
