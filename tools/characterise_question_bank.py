"""Characterise the sample-question generator on the real table titles.

The ~1,000 official titles in questions/Generated_Question_Audit.csv go into a
temporary kb_tables catalogue; the bank is generated for several sizes, and
every title is also parsed and drafted directly (question_language.parse_title
/ draft_questions). Prints a digest of the banks, audits and drafts: two copies
of Sage that write the same questions print the same digest.

    python tools/characterise_question_bank.py SAGE_DIR
"""

import csv
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import logging  # noqa: E402

logging.disable(logging.CRITICAL)
from utils import question_language  # noqa: E402
from utils.question_generator import generate_question_bank  # noqa: E402

AUDIT = Path(__file__).resolve().parent.parent / "questions" / "Generated_Question_Audit.csv"


def titles():
    with open(AUDIT, encoding="utf-8-sig", newline="") as handle:
        rows = {(r["table_id"], r["table_title"]) for r in csv.DictReader(handle)}
    return sorted(rows)


def main():
    pairs = titles()
    record = []
    for table_id, title in pairs:
        spec = question_language.parse_title(title)
        drafts = question_language.draft_questions(title)
        record.append([table_id, repr(spec), repr(drafts)])
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "kb.sqlite3"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE kb_tables (table_id TEXT, title TEXT)")
        con.executemany("INSERT INTO kb_tables VALUES (?,?)", pairs)
        con.commit()
        con.close()
        for target in (25, 200, 600):
            out = Path(folder) / f"q{target}.txt"
            try:
                report = generate_question_bank(db, out, target=target)
                audit = (Path(folder) / "Generated_Question_Audit.csv").read_text(encoding="utf-8")
                report = json.loads(json.dumps(report, default=str).replace(folder, "<tmp>"))
                record.append(
                    [
                        target,
                        report,
                        out.read_text(encoding="utf-8"),
                        re.sub(r"\\d{4}-\\d\\d-\\d\\dT[\\d:]+", "T", audit),
                    ]
                )
            except ValueError as error:
                record.append([target, "refused", str(error)])
    text = json.dumps(record, sort_keys=True, default=str)
    print(len(pairs), "titles")
    print(hashlib.sha256(text.encode()).hexdigest(), len(record), "records")


if __name__ == "__main__":
    main()
