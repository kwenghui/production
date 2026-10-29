"""Build the Sage sample-question bank (questions.txt) and its audit trail.

How a question is made (see question_language.py for the wording rules):
  1. read the official table titles from the local catalogue (kb_tables), read-only;
  2. parse each title and draft several clear questions a teenager or young adult could ask, in plain
     words ("gender", never "sex"), always naming Singapore, the period and the breakdown the title
     gives;
  3. keep only drafts that pass the quality gate; prefer general-interest tables, and use specialist
     (input-output) tables only if more questions are needed to reach the target;
  4. give every table one of the six Sage categories (question_categories.py), decided from metadata,
     never from the question wording;
  5. optionally let a model polish the wording (question_ai.py): every rewrite is re-checked, and the
     deterministic draft is kept whenever there is any doubt;
  6. write the bank in the format the app reads, plus an audit CSV that records how each question
     and category was produced.
The titles certify neither series nor numbers, so questions never name categories that a title does
not name.

Used by: tools/characterise_page_scenarios.py,
tools/characterise_question_bank.py, tools/generate_question_bank.py.
Tested in: tests/test_discovery_and_ingest.py.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import Counter
from pathlib import Path

from utils.question_ai import _ai_classify, _ai_polish
from utils.question_categories import OFFICIAL_CATEGORIES, _raw_themes, best_category
from utils.question_language import draft_questions, problems  # noqa: F401

MAX_VARIANTS_PER_TABLE = 6


def _sources(db_path: Path, metadata_db_paths=None) -> list[dict]:
    """Read ONLY the official SQLite `kb_tables` catalogue, in read-only mode.

    `metadata_db_paths` is accepted for call compatibility, never searched here.
    No fallback index, external source, or implicit SQLite creation is allowed.
    """
    path = Path(db_path)
    if not path.is_file():
        raise ValueError(
            f'Official SQLite table-title catalogue is missing: {path}. Question bank '
            f'unchanged.'
        )
    conn = None
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(kb_tables)')}
        if not {'table_id', 'title'} <= cols:
            raise ValueError(
                'Official SQLite kb_tables(table_id,title) is unavailable; question '
                'bank unchanged.'
            )
        rows = conn.execute('SELECT table_id,title FROM kb_tables ORDER BY table_id').fetchall()
    except sqlite3.Error as exc:
        raise ValueError(
            f'Official SQLite table-title read failed ({type(exc).__name__}); '
            f'question bank unchanged.'
        ) from exc
    finally:
        if conn is not None:
            conn.close()
    result = []
    seen = set()
    for tid, title in rows:
        ident = str(tid or '').strip().upper()
        heading = str(title or '').strip()
        if re.fullmatch(r'[MCD]\d{5,8}', ident) and heading and ident not in seen:
            result.append({'table_id': ident, 'title': heading, 'kind': 'table_metadata'})
            seen.add(ident)
    if not result:
        raise ValueError(
            'Official SQLite kb_tables contains no usable published titles; question '
            'bank unchanged.'
        )
    return result


# specialist (input-output) tables: variety, not the bulk, of a bank for young readers
MAX_TECHNICAL_SHARE = 0.05


def plan_questions(records: list, target: int):
    """Choose which questions go in the bank.

    One clear question per general-interest table first (breadth); then a small, capped helping of
    specialist tables; then further distinct wordings of general tables, spread evenly across the
    catalogue; specialist tables are used beyond their cap only if the target cannot be reached
    otherwise. Returns (chosen, skipped): chosen = [(Draft, record, TitleSpec)], skipped = tables
    with no clear question.
    """
    prepared, skipped = [], []
    for item in records:
        spec, good, rejected = draft_questions(item["title"])
        if good:
            prepared.append((item, spec, good))
        else:
            skipped.append(
                {
                    "table_id": item["table_id"],
                    "title": item["title"],
                    "reasons": sorted({r for _, issues in rejected for r in issues}),
                }
            )
    general = [p for p in prepared if not p[1].technical]
    technical = [p for p in prepared if p[1].technical]
    chosen, used = [], set()

    def take(group, round_index, limit) -> None:
        """Take up to ``limit`` questions from a group, spread evenly after the first round."""
        if not group or limit <= 0:
            return
        stride = 1 if round_index == 0 else max(1, len(group) // limit)
        sequence = (
            group
            if stride == 1
            else group[::stride] + [p for i, p in enumerate(group) if i % stride]
        )
        taken = 0
        for item, spec, drafts in sequence:
            if len(chosen) >= target or taken >= limit:
                return
            if round_index >= len(drafts):
                continue
            draft = drafts[round_index]
            key = draft.text.casefold()
            if key in used:
                continue
            used.add(key)
            chosen.append((draft, item, spec))
            taken += 1

    take(general, 0, target)
    take(technical, 0, int(target * MAX_TECHNICAL_SHARE))
    for round_index in range(1, MAX_VARIANTS_PER_TABLE):
        take(general, round_index, target - len(chosen))
    for round_index in range(MAX_VARIANTS_PER_TABLE):
        take(technical, round_index, target - len(chosen))
    return chosen, skipped


def generate_question_bank(
    db_path,
    output_path,
    *,
    target: int = 1000,
    metadata_db_paths=None,
    ai=None,
    category_overrides=None,
) -> dict:
    """Write the question bank plus its audit CSV; the existing bank is backed up first.

    Every question gets one of the six Sage categories (SingStat theme, then title rules, then
    keyword scoring, then the model for a rare leftover, finally the broad "Society" bucket). `ai`
    is optional: a callable ai(stage, purpose, instructions, payload_text) -> model text. With it,
    drafts are polished AFTER the category is fixed; the category, table ID and audit trail never
    change, and any rewrite that is not clearly as good as the draft is discarded.

    `category_overrides` ({table_id: category}) keeps the categories of an existing bank when only
    the wording is being redone: a valid official category given here wins over every other source.
    """
    db_path, output_path = Path(db_path), Path(output_path)
    if not (1 <= target <= 10000):
        raise ValueError('Question count must be between 1 and 10,000.')
    records = _sources(db_path, metadata_db_paths=metadata_db_paths)
    table_titles = {item["table_id"].upper(): item["title"] for item in records}
    raw_themes = _raw_themes(metadata_db_paths)
    chosen, skipped = plan_questions(records, target)
    if len(chosen) < target:
        raise ValueError(
            f'Only {len(chosen)} distinct clear questions could be created; unchanged.'
        )

    categories = _table_categories(chosen, table_titles, raw_themes, category_overrides, ai)
    # 2) Optional polish. The category above is final and is not re-decided here.
    polished = {}
    if ai is not None:
        used_final = {draft.text.casefold() for draft, _i, _s in chosen}
        items, seen_ids = [], set()
        for draft, item, spec in chosen:
            tid = item["table_id"].upper()
            if tid not in seen_ids:
                seen_ids.add(tid)
                items.append(
                    {
                        "table_id": tid,
                        "title": item["title"],
                        "question": draft.text,
                        "kind": draft.kind,
                        "technical": spec.technical,
                        "content": draft.content,
                    }
                )
        polished = _ai_polish(ai, items, used_final)
    final = []
    for draft, item, spec in chosen:
        tid = item["table_id"].upper()
        new_wording = polished.pop(tid, None)  # one rewrite per table; repeats keep their own draft
        final.append((new_wording or draft.text, draft, item, spec, bool(new_wording)))

    lines, audits = _bank_lines_and_audits(final, categories)
    audit_path = _write_bank(output_path, lines, audits)
    return _bank_report(final, records, skipped, audits, audit_path)


def _table_categories(chosen, table_titles, raw_themes, category_overrides, ai) -> dict:
    """{table ID: (category, how it was decided)} for every chosen table.

    SingStat theme, then title rules, then keyword scoring; an existing bank's
    category (override) wins; the optional model only places leftovers.
    """
    categories = {}
    for _draft, item, _spec in chosen:
        tid = item["table_id"].upper()
        if tid not in categories:
            categories[tid] = best_category(raw_themes.get(tid, ""), item["title"])
    for tid, category in (category_overrides or {}).items():
        if tid.upper() in categories and category in OFFICIAL_CATEGORIES:
            categories[tid.upper()] = (category, "existing")
    if ai is not None:
        leftovers = [
            {"table_id": tid, "title": table_titles[tid]}
            for tid, (_, source) in categories.items()
            if source == "default"
        ]
        for tid, category in _ai_classify(ai, leftovers).items():
            if tid in categories:
                categories[tid] = (category, "ai")

    return categories


def _bank_lines_and_audits(final, categories):
    """(lines of questions.txt, audit rows): numbered batches of 50 in the app's format."""
    # The bank parser expects 2-digit headings and numbered [TAG] entries.
    lines = [
        "SAGE GENERATED QUESTION BANK — SOURCE-METADATA-BASED",
        "Questions reference published table titles. Series and numerical coverage are NOT "
        "assumed verified.",
        "",
    ]
    audits = []
    for block, index in enumerate(range(0, len(final), 50), 1):
        lines.append(f"{block:02d}. OFFICIAL TABLE QUESTIONS BATCH {block:02d}")
        for n, (question, draft, item, spec, was_polished) in enumerate(
            final[index : index + 50], 1
        ):
            category, category_source = categories[item["table_id"].upper()]
            lines.append(f"{n:02d}. [CHART] [CATEGORY: {category}] {question}")
            audits.append(
                {
                    "question": question,
                    "table_id": item["table_id"],
                    "table_title": item["title"],
                    "category": category,
                    "metadata_level": "published_table_title_only",
                    "series_verified": "no",
                    "numeric_observations_verified": "no",
                    "original_question": draft.text,
                    "category_source": category_source,
                    "simplified_by_ai": "yes" if was_polished else "no",
                    "question_kind": draft.kind,
                    "audience": "technical" if spec.technical else "general",
                    "quality_check": "passed"
                    if not problems(question, technical=spec.technical)
                    else "review",
                }
            )
        lines.append("")
    return lines, audits


def _write_bank(output_path, lines, audits):
    """Write the bank atomically (backing up the old one) and its audit CSV; the CSV path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        backup = output_path.with_suffix(output_path.suffix + ".before_generated.bak")
        backup.write_bytes(output_path.read_bytes())
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(output_path)
    audit_path = output_path.with_name("Generated_Question_Audit.csv")
    with audit_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(audits[0]))
        writer.writeheader()
        writer.writerows(audits)
    return audit_path


def _bank_report(final, records, skipped, audits, audit_path) -> dict:
    """Counts describing the generated bank (questions, coverage, AI use, categories)."""
    return {
        "questions": len(final),
        "tables": len(records),
        "audit": str(audit_path),
        "simplified_by_ai": sum(1 for row in audits if row["simplified_by_ai"] == "yes"),
        "category_fallbacks": sum(
            1 for row in audits if row["category_source"] in ("keyword_score", "default")
        ),
        "uncategorised": sum(1 for row in audits if row["category"] not in OFFICIAL_CATEGORIES),
        "skipped_tables": len(skipped),
        "tables_covered": len({row["table_id"] for row in audits}),
        "technical_questions": sum(1 for row in audits if row["audience"] == "technical"),
        "by_kind": dict(Counter(row["question_kind"] for row in audits)),
    }
