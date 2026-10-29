"""Sage's recorded behaviour must not change by accident.

tools/behaviour_digests.py runs the 11 characterisation checks (tens of
thousands of cases: real answers, random tables, fake data servers, every
chart path, the whole page) and reduces each to a digest. This test compares
them with tests/golden/behaviour_digests.json.

If it fails after an INTENDED change, review the change, then run
    python tools/behaviour_digests.py --update
and explain the behaviour change in the commit message.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.behaviour_digests import current_digests, environment, load_golden  # noqa: E402


class RecordedBehaviourIsUnchanged(unittest.TestCase):
    def test_every_characterisation_digest_matches_the_accepted_one(self):
        recorded_in, expected = load_golden()
        here = environment()
        if recorded_in is not None and recorded_in != here:
            # Library versions change some outputs (e.g. pandas 2 vs 3). CI compares this
            # commit with the previous one on the same runner instead (--against).
            self.skipTest(f"golden digests were recorded with {recorded_in}; this is {here}")
        actual = current_digests()
        changed = sorted(name for name in expected if actual.get(name) != expected[name])
        self.assertEqual(changed, [], "Behaviour changed; see tools/behaviour_digests.py")
        self.assertEqual(sorted(actual), sorted(expected))


if __name__ == "__main__":
    unittest.main()
