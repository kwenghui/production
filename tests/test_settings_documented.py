"""Every setting Sage reads is documented in .env.example, and switches behave as documented."""

import glob
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.settings import env_flag, env_opt_in  # noqa: E402

_READ = re.compile(
    r"""(?:getenv|environ\.get|env\.get|env_flag|env_opt_in|bounded_int_env|_bounded_int_env|"""
    r"""_limit|_flag|self\._int)\(\s*(?:env\s*,\s*)?["']([A-Z][A-Za-z0-9_]+)["']"""
)


class SettingsAreDocumented(unittest.TestCase):
    def test_every_setting_read_by_the_code_is_in_env_example(self):
        documented = (ROOT / ".env.example").read_text(encoding="utf-8")
        read = set()
        for name in [
            str(ROOT / "app.py"),
            *glob.glob(str(ROOT / "ui" / "*.py")),
            *glob.glob(str(ROOT / "utils" / "*.py")),
        ]:
            read.update(_READ.findall(Path(name).read_text(encoding="utf-8")))
        missing = sorted(n for n in read if n not in documented and n != "PATH")
        self.assertEqual(missing, [], "Add these to .env.example with their default")


class SwitchSemantics(unittest.TestCase):
    def test_feature_flags_accept_common_spellings_and_keep_the_default_on_typos(self):
        for value, expected in (
            ("1", True),
            ("on", True),
            ("YES", True),
            ("0", False),
            ("off", False),
            ("ture", None),
            ("", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    env_flag("X", True, {"X": value}), True if expected is None else expected
                )
                self.assertEqual(
                    env_flag("X", False, {"X": value}), False if expected is None else expected
                )

    def test_safeguard_relaxing_switches_need_exactly_1(self):
        self.assertTrue(env_opt_in("X", {"X": "1"}))
        for value in ("true", "yes", "on", "01", ""):
            with self.subTest(value=value):
                self.assertFalse(env_opt_in("X", {"X": value}))


if __name__ == "__main__":
    unittest.main()
