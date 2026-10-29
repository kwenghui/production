"""Count why answers had no chart, from Sage's own logs.

Reads any mix of:
  * debug.txt            - "[ANSWER_AUDIT]" lines (written when SAGE_FULL_DEBUG_LOG=1):
                           one per answer, with chart_failure_reason
  * a server log         - e.g. Streamlit Cloud "Manage app" logs saved to a file:
                           "Suppressed non-matching chart ..." and
                           "Verified scope chart not recovered: <reason>" lines

    python tools/chart_rejections_report.py debug.txt
    python tools/chart_rejections_report.py debug.txt streamlit_log.txt

Nothing is changed; no question text is read (the logs hold only digests).
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path

#: Plain-English meaning of each recorded reason.
MEANINGS = {
    "question_chart_mismatch": "a chart existed, but its labels did not match the question "
    "or answer (e.g. a slice of a multi-dimensional table)",
    "no_usable_series": "the data had no series a chart could show (one value, one period, "
    "or values without a common unit)",
    "official_data_incomplete": "the official table could not be proven complete",
    "chart_render_rejected": "the chart failed the final drawing checks (e.g. totals mixed "
    "with their parts, invalid values)",
    "answer not verified": "no chart is drawn for an answer whose numbers were not verified",
    "not-a-multi-year-trend": "a trend was needed but the data covers one period only",
}
_SCOPE = re.compile(r"Verified scope chart not recovered: ([\w-]+)")
_SUPPRESSED = re.compile(r"Suppressed non-matching chart: table=(\S+) kind=(\S+)")


def main(paths) -> int:
    """Print counts of answers with and without charts, and why."""
    answers, reasons, scope, kinds = 0, Counter(), Counter(), Counter()
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            if "[ANSWER_AUDIT]" in line:
                try:
                    record = json.loads(line.split("[ANSWER_AUDIT]", 1)[1])
                except ValueError:
                    continue
                answers += 1
                if record.get("displayed_chart"):
                    reason = "chart shown"
                elif record.get("answer_verified") is False:
                    reason = "answer not verified"
                else:
                    reason = record.get("chart_failure_reason") or "no reason recorded"
                reasons[reason] += 1
            if match := _SCOPE.search(line):
                scope[match.group(1)] += 1
            if match := _SUPPRESSED.search(line):
                kinds[match.group(2)] += 1
    print(f"Answers recorded: {answers}")
    for reason, count in reasons.most_common():
        print(f"  {count:5d}  {reason:<26} {MEANINGS.get(reason, '')}")
    if scope:
        print("\nNarrowed-chart recovery refused (server log):")
        for reason, count in scope.most_common():
            print(f"  {count:5d}  {reason:<26} {MEANINGS.get(reason, '')}")
    if kinds:
        print("\nSuppressed tool charts by kind (server log):")
        for kind, count in kinds.most_common():
            print(f"  {count:5d}  {kind}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    raise SystemExit(main(sys.argv[1:]))
