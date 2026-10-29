"""The sample-question bank (questions/questions.txt) and its audit files.

* Loading: load_question_bank parses the plain-text bank (format: numbered
  sections and "[TAG] question" lines); cached_* versions re-read a file only
  when it changes (Streamlit re-runs the page on every click).
* Categories: question_categories groups samples for the sidebar.
* Audited sources: generated sample questions record the table that produced
  them (Generated_Question_Audit.csv). expected_table_for_generated_question
  returns it, and response_matches_expected_table checks an answer used it.

Used by: app.py, ui/answer_pipeline.py, ui/sidebar.py.
Tested in: tests/test_small_modules.py.
"""

from pathlib import Path
import copy
import csv
import hashlib
import re
import sqlite3

from utils import process_state as _process_state
from ui.config import (
    APP_DIR,
    GENERATED_AUDIT_FILE,
    LOGGER,
    MAX_UI_INPUT_CHARS,
    QUESTIONS_DIR,
    QUESTION_AUDIT_FILE,
)


# The text-file format is kept deliberately simple so the question bank can be
# updated without touching Python code. Only numbered [TAG] entries are questions.
SECTION_PATTERN = re.compile(r"^\d{2}\.\s+([A-Z][A-Z0-9 &(),/'–+-]+)$")


QUESTION_PATTERN = re.compile(r"^\d{2}\.\s+\[([A-Z-]+)\](?:\s+\[CATEGORY:\s*([^\]]+)\])?\s+(.+)$")


def load_question_bank(path):
    """Read audience-labelled sample questions from the adjacent UTF-8 text file.

    Keep question text and tags exactly as supplied. Introductory notes, section
    totals and summary material in the bank are not displayed as questions.
    Re-read the small file on every Streamlit run so edits take effect immediately.
    """
    bank = {}
    perspective = None

    for raw_line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        section = SECTION_PATTERN.fullmatch(line)
        if section:
            perspective = section.group(1).title()
            bank.setdefault(perspective, [])
            continue

        question = QUESTION_PATTERN.fullmatch(line)
        if question and perspective:
            category = str(question.group(2) or "").strip()
            text = question.group(3).strip()
            if text and len(text) <= MAX_UI_INPUT_CHARS:
                bank[perspective].append(
                    {
                        "perspective": perspective,
                        "tag": question.group(1),
                        "category": category or "Uncategorised",
                        "question": text,
                    }
                )

    # Ignore an empty section if the file is being edited at the same time.
    bank = {name: entries for name, entries in bank.items() if entries}
    if not bank:
        raise ValueError("No numbered [TAG] questions were found in the question bank.")
    return bank


def preferred_source_for_sample(question):
    """Local audit identifies a discovery hint, NOT proof of available series.

    Failed/unknown CSV entries never grant validation and must not alter the
    original user question, security checks, or numeric/graph safeguards.
    """
    if not isinstance(question, str) or not QUESTION_AUDIT_FILE.is_file():
        return None
    try:
        with QUESTION_AUDIT_FILE.open(encoding='utf-8-sig', newline='') as handle:
            matches = [
                row
                for row in csv.DictReader(handle)
                if row.get('question') == question
                and re.fullmatch(r'[A-Z]\d{5,8}', row.get('table_id', ''))
            ]
    except (OSError, UnicodeError, csv.Error):
        LOGGER.warning('Question audit unavailable; ordinary safe discovery is used')
        return None
    return matches[0]['table_id'] if len(matches) == 1 else None


def flatten_question_bank(bank):
    """Return every question in file order for reproducible sequential testing."""
    flattened = []
    global_number = 1
    for perspective, entries in bank.items():
        for section_number, entry in enumerate(entries, start=1):
            flattened.append(
                {
                    **entry,
                    "global_number": global_number,
                    "section_number": section_number,
                }
            )
            global_number += 1
    return flattened


# Streamlit reruns this script on EVERY click. These files are large (question
# bank ~150 KB, audit CSV ~400 KB) and categorising matches every question against
# every official table title, so each result is cached and keyed on the files'
# modification times: editing a file still takes effect on the next click.
# --- Per-process caches for data that changes only when a file changes ---------
_FILE_CACHE_LOCK = _process_state.lock("file_cache")


_FILE_CACHE = _process_state.shared("file_cache", dict)


_FILE_CACHE_MAX_ENTRIES = 8


def _file_signature(*paths) -> tuple:
    """(path, mtime_ns, size) for each path; missing files are part of the key too."""
    signature = []
    for path in paths:
        try:
            info = Path(path).stat()
            signature.append((str(path), info.st_mtime_ns, info.st_size))
        except OSError:
            signature.append((str(path), None, None))
    return tuple(signature)


def _cached(kind, key, build):
    """Return build() for (kind, key), computing it once per process.

    ``key`` is a file signature (see _file_signature), so an edited file gets
    a new key and is read again. The cache keeps at most
    _FILE_CACHE_MAX_ENTRIES results (oldest dropped first).
    """
    cache_key = (kind, key)
    with _FILE_CACHE_LOCK:
        if cache_key in _FILE_CACHE:
            return _FILE_CACHE[cache_key]
    value = build()
    with _FILE_CACHE_LOCK:
        if len(_FILE_CACHE) >= _FILE_CACHE_MAX_ENTRIES:
            _FILE_CACHE.pop(next(iter(_FILE_CACHE)))
        _FILE_CACHE[cache_key] = value
    return value


def cached_question_bank(path) -> tuple:
    """load_question_bank + flatten, re-read only when the file changes."""
    path = Path(path)

    def build() -> tuple:
        """Build the cached value when the source file is new or changed."""
        bank = load_question_bank(path)
        return bank, flatten_question_bank(bank)

    bank, flat = _cached("bank", _file_signature(path), build)
    return bank, copy.deepcopy(flat)


def cached_question_categories(questions, audit_path) -> dict:
    """question_categories, recomputed only when the questions or source files change."""
    texts = tuple((str(q.get("question") or ""), str(q.get("category") or "")) for q in questions)
    digest = hashlib.sha256(repr(texts).encode("utf-8")).hexdigest()
    key = (digest, _file_signature(audit_path, APP_DIR / "data" / "knowledge_base.sqlite3"))
    return dict(_cached("categories", key, lambda: question_categories(questions, audit_path)))


def sample_question_titles():
    """{sample question: its table's title}, from the audit file (re-read only when it changes)."""

    def build():
        """Build the cached value when the source file is new or changed."""
        found = {}
        try:
            with GENERATED_AUDIT_FILE.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    if row.get("question"):
                        found[row["question"].strip()] = str(row.get("table_title") or "")
        except (OSError, UnicodeError, csv.Error) as error:
            # Titles are a display nicety; say why they are missing instead of hiding it.
            LOGGER.warning(
                "Sample-question audit file unreadable (%s); titles omitted", type(error).__name__
            )
        return found

    return _cached("sample_titles", _file_signature(GENERATED_AUDIT_FILE), build)


_OFFICIAL_CATEGORIES = (
    "Economy & Price",
    "Industry",
    "Trade & Investment",
    "Population",
    "Households",
    "Society",
)


def question_categories(questions, audit_path, metadata_db=None):
    """Map bank questions to official SingStat themes, with metadata recovery.

    Exact question text from the generated-question audit remains authoritative.
    If that audit is missing or stale, recover the category from the official
    table title/theme metadata stored in Sage's local SingStat database
    (``metadata_db``, default data/knowledge_base.sqlite3).
    No keyword-based category guessing is used.

    Order for each question: the category written on its bank line, then the
    audit's exact question, then the longest matching official table title.
    """
    known, title_categories = _audit_categories(audit_path)
    db_path = Path(metadata_db) if metadata_db else APP_DIR / "data" / "knowledge_base.sqlite3"
    for key, category in _database_title_categories(db_path).items():
        title_categories.setdefault(key, category)
    # Prefer the longest matching official table title, so a short title cannot
    # take a question that belongs to a more specific table.
    title_matches = sorted(title_categories.items(), key=lambda item: len(item[0]), reverse=True)
    result = {}
    for item in questions:
        question = str(item.get("question") or "").strip()
        category = _canonical_category(item.get("category")) or known.get(question)
        if category is None and title_matches:
            qkey = _normalise_words(question)
            category = next((theme for key, theme in title_matches if key and key in qkey), None)
        result[question] = category or "Uncategorised"
    return result


def _canonical_category(value):
    """The official category name matching ``value`` (any case), or None."""
    value = str(value or "").strip()
    return next(
        (name for name in _OFFICIAL_CATEGORIES if name.casefold() == value.casefold()), None
    )


def _normalise_words(value) -> str:
    """Lower-case words only, for comparing titles and questions."""
    text = " ".join(str(value or "").casefold().replace("&", " and ").split())
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


def _title_key(value) -> str:
    """A table title normalised for matching (a trailing reporting frequency removed,
    because generated questions leave it out)."""
    text = _normalise_words(value)
    for suffix in (" half yearly", " quarterly", " monthly", " weekly", " annual"):
        if text.endswith(suffix):
            return text[: -len(suffix)].strip()
    return text


def _audit_categories(audit_path):
    """({question: category}, {title key: category}) from the generated-question audit."""
    known, title_categories = {}, {}
    try:
        with Path(audit_path).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                category = _canonical_category(row.get("category"))
                if not category:
                    continue
                question = str(row.get("question") or "").strip()
                if question:
                    known[question] = category
                key = _title_key(row.get("table_title"))
                if key:
                    title_categories[key] = category
    except (OSError, UnicodeError, csv.Error) as error:
        LOGGER.warning(
            "Question audit file unreadable (%s); categories from titles only", type(error).__name__
        )
    return known, title_categories


def _database_title_categories(db_path) -> dict:
    """{title key: category} from the official table themes in the local database (read-only)."""
    found = {}
    if not Path(db_path).is_file():
        return found
    conn = None
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
        fields = {row[1] for row in conn.execute("PRAGMA table_info(kb_tables)")}
        title_col = next(
            (name for name in ("table_title", "title", "name") if name in fields), None
        )
        if {"table_id", "theme"} <= fields and title_col:
            # Column names come from the fixed list above, never from data.
            for _table_id, theme, title in conn.execute(
                f"SELECT table_id, theme, {title_col} FROM kb_tables"
            ):
                category, key = _canonical_category(theme), _title_key(title)
                if category and key:
                    found.setdefault(key, category)
    except (sqlite3.Error, OSError) as error:
        LOGGER.warning("Category metadata database unreadable (%s); skipped", type(error).__name__)
    finally:
        if conn is not None:
            conn.close()
    return found


def expected_table_for_generated_question(question, audit_path=None):
    """Return the question bank's official table ID, never an inferred ID.

    The audit identifies which official table the question was generated from;
    it does not certify a numeric series or its observations.
    """
    if not isinstance(question, str) or not question.strip():
        return None
    path = audit_path or QUESTIONS_DIR / "Generated_Question_Audit.csv"
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if str(row.get("question") or "").strip() == question.strip():
                    table_id = str(row.get("table_id") or "").strip().upper()
                    return table_id if re.fullmatch(r"[A-Z]\d{5,8}", table_id) else None
    except (OSError, UnicodeError, csv.Error) as error:
        # Without the audit, a generated sample is answered by ordinary discovery
        # instead of its audited table: allowed, but the operator must know.
        LOGGER.warning(
            "Generated-question audit unreadable (%s); sample questions are not "
            "tied to their audited tables",
            type(error).__name__,
        )
        return None
    return None


def unverified_table_message(table_id, results):
    """Plain explanation when the required table produced no verified figures."""
    from utils.official_links import table_page_url

    texts = " ".join(
        str(r.get("error") or r.get("coverage_warning") or "")
        for r in results or []
        if isinstance(r, dict)
    )
    page = table_page_url(table_id)
    see = f" You can view the table directly on SingStat: {page}" if page else ""
    if (
        "could not be proved complete" in texts
        or "Staged-only metadata" in texts
        or any(isinstance(r, dict) and r.get("table_complete") is False for r in results or [])
    ):
        return (
            f"SingStat's service did not return table {table_id} in a way Sage could confirm "
            "as complete, so no figures are shown rather than risk incomplete official data. "
            "This may be temporary; please try again later." + see
        )
    return (
        f"The requested official table {table_id} did not return verified observations for "
        f"this "
        "question. No statistics from a different table have been substituted." + see
    )


def response_matches_expected_table(response, table_id) -> bool:
    """Only a successful exact source result can verify a generated prompt."""
    if not table_id:
        return True
    if not isinstance(response, dict):
        return False
    return any(
        isinstance(row, dict)
        and row.get("success") is True
        and row.get("table_id") == table_id
        and row.get("table_complete") is not False
        and row.get("analysis_ready") is not False
        for row in response.get("tool_results") or []
    )


def eligible_question_indices(questions, categories, selected_category) -> list:
    """Return stable original-bank indices for the currently selected category."""
    if selected_category == "ALL":
        return list(range(len(questions)))
    return [
        i
        for i, question in enumerate(questions)
        if categories.get(question.get("question", ""), "Uncategorised") == selected_category
    ]


def audited_chart_expected(question):
    """The question-bank [CHART] contract also applies to management audits."""
    if not QUESTION_AUDIT_FILE.is_file():
        return False
    try:
        with QUESTION_AUDIT_FILE.open('r', encoding='utf-8-sig', newline='') as f:
            return any(
                row.get('question', '').strip() == question.strip()
                and row.get('expected_chart', '').strip().casefold()
                in {'trend', 'comparison', 'bar', 'donut', 'pyramid', 'chart'}
                for row in csv.DictReader(f)
            )
    except (OSError, UnicodeError, csv.Error):
        return False
