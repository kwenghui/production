"""Answer ratings (👍 / 👎) from debug.txt, for the administrator's CSV download.

Each click is one "[USER_FEEDBACK] {json}" line. Only those lines are read, from at
most the last READ_LIMIT_BYTES of the file. No question text is stored or exported:
only a keyed question code, an answer ID, the rating and the time.

Used by: ui/answer_view.py.
Tested in: tests/test_question_tools.py.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

PREFIX = "[USER_FEEDBACK] "
READ_LIMIT_BYTES = 5 * 1024 * 1024
COLUMNS = ("timestamp_utc", "rating", "answer_id", "question_hash")


def read_feedback(path) -> list[dict]:
    """Every rating click, oldest first. A missing or unreadable file gives []."""
    path = Path(path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > READ_LIMIT_BYTES:
                handle.seek(size - READ_LIMIT_BYTES)
                handle.readline()  # skip a partial first line
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in data.splitlines():
        if not line.startswith(PREFIX):
            continue
        try:
            item = json.loads(line[len(PREFIX) :])
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("rating") in ("up", "down"):
            rows.append({key: str(item.get(key, "")) for key in COLUMNS})
    return rows


def summary(rows) -> dict:
    """Counts using each answer's LATEST rating (changing your mind replaces it)."""
    latest = {}
    for row in rows:
        latest[row["answer_id"] or id(row)] = row["rating"]
    values = list(latest.values())
    return {
        "answers": len(values),
        "up": values.count("up"),
        "down": values.count("down"),
        "clicks": len(rows),
    }


def _safe_cell(value: str) -> str:
    """Neutralise spreadsheet formulas (=, +, -, @ and control prefixes)."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def to_csv(rows) -> bytes:
    """CSV bytes (UTF-8 with BOM, so Excel shows it correctly)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([_safe_cell(row.get(key, "")) for key in COLUMNS])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")
