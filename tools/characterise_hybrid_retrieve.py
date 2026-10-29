"""Characterise hybrid_discovery.retrieve: keyword + semantic table discovery.

A temporary knowledge base holds the real tables and series of
tests/fixtures/real_answers.py plus look-alike annual/quarterly/monthly
editions. The semantic index is a fake that returns chosen document IDs, and
can fail to embed, fail to query, be empty or be switched off. Questions vary
wording, frequency and named table IDs. Prints a digest of every ranked
result and its trace (timings removed): two copies of Sage that rank the same
print the same digest.

    python tools/characterise_hybrid_retrieve.py SAGE_DIR
"""

import hashlib
import importlib.util
import json
import os
import random
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, sys.argv[1])
sys.path.insert(1, str(Path(__file__).resolve().parent.parent))
from tools._stand_ins import install_stand_ins  # noqa: E402

install_stand_ins()
import logging  # noqa: E402

logging.disable(logging.CRITICAL)
from utils import hybrid_discovery as hd  # noqa: E402
from utils import kb  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "real_answers",
    Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "real_answers.py",
)
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)
R = random.Random(29)
EXTRA_TABLES = [
    ("M081332", "Research And Development Expenditure By Sector, Quarterly"),
    ("M451112", "Merchandise Imports Of Machinery And Equipment, Annual"),
    ("M015782", "Share Of Nominal Gross Value Added, By Industry, Quarterly"),
]
QUESTIONS = [q for qs in fixtures.QUESTIONS.values() for q in qs] + [
    "How have quarterly R&D expenditures changed?",
    "Show each month's machinery imports",
    "annual gross value added shares",
    "M081331 private sector",
    "   ",
    "x",
]
_TIMING_KEYS = ("elapsed_ms", "vector_ms", "search_ms")


def build_kb(folder):
    path = Path(folder) / "kb.sqlite3"
    con = kb.open_db(path)
    for table_id, (title, unit, series) in fixtures.TABLES.items():
        kb.upsert_table(con, table_id, title, frequency=title.rsplit(", ", 1)[-1])
        for number, (name, _, _) in series.items():
            kb.upsert_series(con, table_id, number, name, unit=unit)
    for table_id, title in EXTRA_TABLES:
        kb.upsert_table(con, table_id, title, frequency=title.rsplit(", ", 1)[-1])
    con.commit()
    doc_ids = [r[0] for r in con.execute("SELECT 't:' || table_id FROM kb_tables")]
    doc_ids += [
        r[0] for r in con.execute("SELECT 's:' || table_id || ':' || series_id FROM " "kb_series")
    ]
    con.close()
    return path, doc_ids


def without_timings(value):
    if isinstance(value, dict):
        return {k: without_timings(v) for k, v in value.items() if k not in _TIMING_KEYS}
    if isinstance(value, list):
        return [without_timings(v) for v in value]
    return value


def main():
    digests, outcomes = [], {}
    with tempfile.TemporaryDirectory() as folder:
        path, doc_ids = build_kb(folder)
        chroma_dir = Path(folder) / "chroma"
        chroma_dir.mkdir()
        (chroma_dir / "placeholder").write_text("x")
        os.environ.update(
            SAGE_KB_SQLITE=str(path),
            SAGE_CHROMA_DIR=str(chroma_dir),
            SAGE_CHROMA_EMBEDDING="openai",
        )
        for _ in range(800):
            mode = R.choice(["off", "ok", "ok", "embed_fails", "query_fails", "empty"])
            tables = [d for d in doc_ids if d.startswith("t:")]
            series = [d for d in doc_ids if d.startswith("s:")]
            picks = {
                "table": R.sample(tables, R.randint(0, 4)),
                "series": R.sample(series, R.randint(0, 6)),
            }

            def query(
                query_embeddings=None, query_texts=None, n_results=60, where=None, include=None
            ):
                if mode == "query_fails" and where["type"] == "series":
                    raise RuntimeError("index unavailable")
                return {"ids": [picks[where["type"]]]}

            index = types.SimpleNamespace(
                col=types.SimpleNamespace(count=lambda: 0 if mode == "empty" else 100, query=query)
            )
            hd.chroma_search_enabled = lambda: mode != "off"
            hd._chroma_index = lambda p, m: index
            hd._record_count = lambda i: i.col.count()

            def embed(text):
                if mode == "embed_fails":
                    raise RuntimeError("Missing scopes: model.request")
                return [0.1, 0.2]

            kb.embed_query = embed
            question = R.choice(QUESTIONS)
            ids = R.choice([(), ("M081331",), ("M451112", "M015781")])
            found = hd.retrieve(question, top_k=R.choice([1, 4, 8]), dataset_ids=ids)
            outcomes[mode] = outcomes.get(mode, 0) + 1
            digests.append(
                hashlib.sha256(
                    json.dumps(without_timings(found), sort_keys=True, default=str).encode()
                ).hexdigest()[:12]
            )
    print(sorted(outcomes.items()))
    print(hashlib.sha256("".join(digests).encode()).hexdigest(), len(set(digests)), "distinct")


if __name__ == "__main__":
    main()
