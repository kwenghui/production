"""The "Used by:" / "Tested in:" lines in module docstrings must be true.

They are derived from the real imports by tools/module_map.py. If this test
fails, run:  python tools/module_map.py --fix
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.module_map import current_and_fixed, importers, source_files  # noqa: E402


class ModuleMapIsTrue(unittest.TestCase):
    def test_every_docstring_map_matches_the_imports(self):
        users = importers()
        stale = [
            path.relative_to(ROOT).as_posix()
            for path in source_files()
            if current_and_fixed(path, users)[0] != current_and_fixed(path, users)[1]
        ]
        self.assertEqual(stale, [], "Run: python tools/module_map.py --fix")


if __name__ == "__main__":
    unittest.main()
