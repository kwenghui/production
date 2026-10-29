"""Characterise retrieval_debug.retrieval_rows (the "Get published values" audit rows).

3,000 responses built from the real tables in tests/fixtures/real_answers.py,
with fresh/cached/stale retrievals and valid, foreign and malformed request
URLs. Prints a digest: two copies of Sage that build the same rows print the
same digest.

    python tools/characterise_retrieval_rows.py SAGE_DIR
"""

import sys, json, random, hashlib, importlib.util

sys.path.insert(0, sys.argv[1])
from utils.retrieval_debug import retrieval_rows
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'ra', Path(__file__).resolve().parent.parent / 'tests' / 'fixtures' / 'real_answers.py'
)
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
R = random.Random(5)
out = []
urls = 0
for _ in range(3000):
    res = []
    for _ in range(R.randint(0, 5)):
        tid = R.choice(list(f.EVIDENCE_SETS) + [None])
        r = f.tool_result(tid, f.EVIDENCE_SETS[tid][0]) if tid else {"success": True}
        r['cache_status'] = R.choice(['fresh', 'cached', 'stale', None])
        r['retrieval_source'] = R.choice(['SingStat Table ' 'Builder / SQLite', '', None])
        r['retrieval_channel'] = R.choice(['SingStat API', 'SQLite cache', None])
        r['retrieval_details'] = R.choice(
            [
                'https://tablebuilder.singstat.gov.sg/api/table/tabledata/%s?limit=2500&offset=0'
                % tid,
                '',
                None,
            ]
        )
        r['retrieval_api_urls'] = R.choice(
            [
                [
                    f'https://tablebuilder.singstat.gov.sg/api/table/tabledata/{tid}?limit=2500&offset=0'
                ],
                ['https://evil.com/x'],
                'x',
                [],
            ]
        )
        r['official_api_requests'] = R.choice([0, 1, 2])
        res.append(r)
    resp = R.choice(
        [
            {'tool_results': res},
            {'tool_results': 'x'},
            None,
            {'tool_results': res, 'retrieval_trace': {'events': []}},
        ]
    )
    rows = retrieval_rows(resp)
    urls += sum(1 for x in (rows or []) if isinstance(x, dict) and x.get('API URL'))
    out.append(json.dumps(rows, sort_keys=True, default=str))
print(
    hashlib.sha256(''.join(out).encode()).hexdigest()[:16],
    urls,
    sum(o not in ('[]', 'null') for o in out),
)
