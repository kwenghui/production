"""Characterise LocalStore._fetch_complete (the paged table download) on 1,500 fake servers.

Each fake SingStat server varies: table size, page cap, repeated rows across
pages, ignored offsets, duplicate IDs, reported totals (true, per-page, wrong,
missing), the metadata manifest (none, failing, count only, full ID list,
wrong table, wrong count) and connection failures mid-download. Prints the
outcome counts and a digest of every result, every URL requested and every
partial download kept: two copies of Sage that behave the same print the
same digest.

    python tools/characterise_download.py OLD_SAGE_DIR
    python tools/characterise_download.py NEW_SAGE_DIR
"""

import hashlib
import json
import random
import sys
from urllib.parse import parse_qs, urlparse

tree = sys.argv[1]
sys.path.insert(0, tree)
import utils.store as store

R = random.Random(5)
# Small pages so pagination is exercised. The limits live in utils.table_download
# after the store split (utils.store before it): set them wherever they are read.
_download_module = sys.modules[store.LocalStore._fetch_complete.__module__]
for _module in {store, _download_module}:
    _module.PAGE_SIZE, _module.MAX_PAGES = 6, 12
    _module.MAX_ROWS = 6 * 12


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            e = (
                store.requests.HTTPError(str(self.status))
                if hasattr(store, "requests")
                else RuntimeError(str(self.status))
            )
            raise e

    def json(self):
        return json.loads(json.dumps(self.body))


def make_server(cfg):
    rows = [
        {
            "seriesNo": str(i),
            "rowText": f"Series {i:02d}",
            "uoM": "Number",
            "columns": [{"key": "2023", "value": str(i * 10)}],
        }
        for i in range(1, cfg["n"] + 1)
    ]
    calls = []

    def fetch_page(url):
        calls.append(url.split("tabledata/")[1])
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        cfg["page_calls"] = cfg.get("page_calls", 0) + 1
        if cfg.get("fail_at") and cfg["page_calls"] == cfg["fail_at"]:
            return Resp({}, 503)
        if "seriesNoORrowNo" in q:
            want = q["seriesNoORrowNo"].split(",")
            sel = [r for r in rows if r["seriesNo"] in want]
        elif "sortBy" in q:
            field, direction = q["sortBy"].split()
            sel = sorted(rows, key=lambda r: r[field], reverse=direction == "desc")[: cfg["cap"]]
        else:
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 10**6))
            if cfg["ignore_offset"]:
                off = 0
            start = max(0, off - cfg["overlap"]) if off else 0
            sel = rows[start : start + min(lim + (cfg["overlap"] if off else 0), cfg["cap"])]
            if cfg["dup_in_page"] and sel:
                sel = sel + [sel[0]]
        body = {"Data": {"id": cfg["resp_id"], "row": sel}}
        if cfg["total"] == "true":
            body["Data"]["total"] = len(rows)
        elif cfg["total"] == "page":
            body["Data"]["total"] = len(sel)
        elif cfg["total"] == "wrong":
            body["Data"]["total"] = len(rows) + 3
        return Resp(body)

    def fetch_metadata(url):
        calls.append("META")
        if cfg["meta"] == "fail":
            return Resp({}, 500)
        rec = {"id": cfg["meta_id"], "total": cfg["meta_total"]}
        if cfg["meta"] == "ids":
            rec["row"] = [{"seriesNo": r["seriesNo"]} for r in rows]
        return Resp({"Data": {"records": rec}})

    return fetch_page, fetch_metadata, calls


out = []
kinds = {}
for case in range(1500):
    n = R.randint(1, 20)
    cfg = dict(
        n=n,
        cap=R.choice([6, 6, 4, 3, 100]),
        overlap=R.choice([0, 0, 1, 2]),
        ignore_offset=R.random() < 0.08,
        dup_in_page=R.random() < 0.04,
        total=R.choice(["true", "page", "none", "wrong"]),
        resp_id=R.choice(["M810001"] * 12 + ["M999999"]),
        meta=R.choice(["none", "fail", "count", "ids"]),
        meta_id=R.choice(["M810001"] * 12 + ["M888888"]),
        meta_total=R.choice([n, n, n, n + 1, 0]),
        fail_at=R.choice([None] * 8 + [2, 3]),
    )
    fp, fm, calls = make_server(cfg)
    s = store.LocalStore.__new__(store.LocalStore)
    retained = []
    s._retain_pending = lambda tid, partial, expected, authoritative=False: retained.append(
        (len((partial or {}).get("Data", {}).get("row", [])), expected, authoritative)
    )
    try:
        res, expected = s._fetch_complete("M810001", fp, fm if cfg["meta"] != "none" else None)
        result = [
            "OK",
            [r["seriesNo"] for r in res["Data"]["row"]],
            expected,
            res["Data"].get("limit"),
            res["Data"].get("total"),
        ]
        k = "ok"
    except Exception as e:
        extra = [getattr(e, a, None) for a in ("expected", "authoritative_total", "expected_ids")]
        part = getattr(e, "partial", None)
        result = [
            "ERR",
            type(e).__name__,
            str(e),
            extra,
            len(part["Data"]["row"]) if isinstance(part, dict) else None,
        ]
        k = type(e).__name__
    kinds[k] = kinds.get(k, 0) + 1
    out.append(
        hashlib.sha256(json.dumps([result, calls, retained], default=str).encode()).hexdigest()[:12]
    )
print(sorted(kinds.items()))
print(hashlib.sha256("".join(out).encode()).hexdigest(), len(set(out)), "distinct")
