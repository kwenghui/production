"""Characterise kb.inspect_table (the knowledge-base builder's table inspection).

300 fake official payloads (flat series, nested columns, key-only nodes,
missing series IDs, missing values), with and without observations and an
existing table row. Records the returned report and the database contents
afterwards (timestamps removed). Prints a digest: two copies of Sage that
store the same data print the same digest.

    python tools/characterise_kb_inspect.py SAGE_DIR
"""

import sys, json, random, hashlib, tempfile, sqlite3
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import logging

logging.disable(logging.CRITICAL)
from utils import kb

R = random.Random(12)
out = []
folder = Path(tempfile.mkdtemp())
for i in range(300):
    con = kb.open_db(folder / f'kb{i}.sqlite3')
    rows = []
    for n in range(R.randint(1, 4)):
        sid = R.choice([str(n + 1), '', None]) if R.random() < 0.1 else str(n + 1)
        cols = R.choice(
            [
                [
                    {"key": "2022", "value": str(R.randint(1, 9))},
                    {"key": "2023", "value": R.choice(["5", "na", ""])},
                ],
                [{"key": "Male", "columns": [{"key": "2023", "value": "3"}]}],
                [{"key": "Total"}],
            ]
        )
        rows.append({"seriesNo": sid, "rowText": f"Series {n}", "uoM": "Number", "columns": cols})
    payload = {
        "Data": {"id": "M810001", "title": "Test Table, Annual", "frequency": "Annual", "row": rows}
    }
    if R.random() < 0.3:
        kb.upsert_table(con, "M810001", "Old title")
        con.commit()
    try:
        rep = kb.inspect_table(
            con,
            "M810001",
            fetch=lambda *a, **k: payload,
            include_observations=R.choice([True, False]),
        )
    except Exception as e:
        rep = {'raised': type(e).__name__, 'msg': str(e)[:100]}
    dump = [
        list(r)
        for r in con.execute(
            "SELECT table_id,title,series_status,series_count,metadata_status,last_error FROM kb_tables"
        )
    ]
    dump += [list(r) for r in con.execute("SELECT * FROM kb_series ORDER BY series_id")]
    dump += [
        list(r)[:5]
        for r in con.execute("SELECT * FROM kb_observations ORDER BY series_id, " "period")
    ]
    dump += [
        list(r)[:2] + list(r)[3:]
        for r in con.execute(
            "SELECT table_id,attempt_count,updated_at,status,detail FROM kb_ingestion"
        )
    ]
    con.close()
    s = json.dumps([rep, dump], sort_keys=True, default=str)
    import re

    s = re.sub(r'\d{4}-\d\d-\d\dT[\d:+.]+', 'T', s)
    out.append(s)
print(hashlib.sha256(''.join(out).encode()).hexdigest()[:16], sum('raised' in o for o in out))
