"""Release zips must never contain secrets, logs or machine state."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.package_release import is_excluded  # noqa: E402


class ReleasePackaging(unittest.TestCase):
    def test_secrets_and_personal_data_are_left_out(self):
        for path in (
            ".env",
            ".env.local",
            ".streamlit/secrets.toml",
            ".sage_log_key",
            "debug.txt",
            "questions/failed.txt",
            "questions/success.txt",
            "data/app_cache.sqlite3",
            "utils/__pycache__/x.pyc",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_excluded(path))

    def test_source_and_templates_are_kept(self):
        for path in (
            "app.py",
            "ui/answer_view.py",
            "utils/core_secure.py",
            ".env.example",
            ".streamlit/secrets.toml.example",
            "questions/questions.txt",
            "assets/sage_theme.css",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_excluded(path))


if __name__ == "__main__":
    unittest.main()
