"""Read-only table discovery: which official tables could answer a question?

WHERE THIS FITS
===============
Before the analytical engine runs, Sage looks at the local catalogue of table
TITLES (``data/discovery_index.sqlite3``) and decides one of:

* ``skip``   - not a catalogue question; let the engine search the series,
* ``direct`` - one compatible table clearly fits; use it as a hint,
* ``choose`` - several distinct measures fit; ask the user to pick,
* ``review`` - only incompatible titles were found; show them as near misses.

A title is metadata, never evidence: every number is still verified
downstream. No internet, no SQL built from user text, no model calls here.

GENERIC BY DESIGN
=================
Every rule below runs identically for every table. Topic words come only from
``utils/vocabulary.py``; what a question or a title asks for / declares
(frequency, unit, kind of measure) comes from ``utils/query_text.py``; the
population basis (residents, citizens, ...) from ``utils/population_basis.py``.
Do not add rules that mention a subject, a title or a table ID here.

Used by: utils/ai_discovery.py, utils/discovery_service.py,
utils/kb_discovery_bridge.py.
Tested in: tests/test_generic_rules.py.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from utils.official_links import table_page_path
from utils.population_basis import population_bases
from utils.query_text import (
    DERIVED_KINDS,
    asks_for_level,
    explicit_frequency,
    measure_kind,
    requested_unit_dimension,
    title_frequency,
    title_unit_dimension,
)
from utils.vocabulary import MEASURE_CONCEPTS, MEASURE_WORDS_TOO_BROAD, expand_subject

_ID = re.compile(r"[A-Z]\d{5,8}\Z")
_WORDS = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "how has have what which the are is was were in singapore over years year "
    "time changed change trend trends all number numbers of for by from since show "
    "tell me about can you please list tables table datasets dataset official "
    "singstat data compare".split()
)
# Words too common in titles to identify a subject on their own.
_TOO_COMMON_TO_SEARCH = frozenset(
    {"index", "indices", "total", "latest", "growth", "rate", "rates", "quarterly", "annual"}
)

# Ranking weights. Each is a generic signal; larger = more discriminating.
_SCORE_BASE = 2.0
_SCORE_PER_SHARED_WORD = 0.8
_SCORE_MEASURE_CONCEPT = 3.0  # question uses a word naming the title's measure concept
_SCORE_SAME_POPULATION = 3.0  # same population basis named on both sides
_SCORE_SAME_FREQUENCY = 4.0
_SCORE_SAME_MEASURE_KIND = 6.0
_SCORE_SAME_UNIT = 10.0
_PENALTY_UNASKED_DERIVED = 3.0  # title is a rate/share/... the question did not ask for
_DIRECT_MARGIN = 2.0  # best must beat runner-up by this to skip the choice


# --- Small text helpers (public: also used by utils/ai_discovery.py) ----------


def tokens(text: str) -> set[str]:
    """Lower-case words of ``text`` minus question filler words."""
    return set(_WORDS.findall(str(text or "").casefold())) - _STOP


def is_list_request(question) -> bool:
    """True for "which tables cover X?" style catalogue requests."""
    q = str(question or "").casefold()
    return bool(
        re.search(r"\b(list|browse|find|show|which|what|available|give)\b", q)
        and re.search(r"\b(tables?|datasets?)\b", q)
    )


def names_table_id(question) -> bool:
    """True when the user typed a table ID such as Mxxxxxx."""
    return re.search(r"(?<![a-z0-9])[a-z]\d{5,8}(?![a-z0-9])", str(question), re.I) is not None


def needs_series_search(question) -> str | None:
    """Reason to skip title-level choices and search inside series instead, or None.

    Titles name broad topics. Two kinds of question are about something only
    the SERIES can show, so offering vaguely matching titles would mislead:

    * a relationship between two groups ("citizen-to-non-citizen", "A vs B");
    * a survey dimension (survey years, quintiles, deciles) nested inside a cube.
    """
    q = str(question or "").casefold()
    if re.search(r"\b[a-z]+[- ]to[- ](?:non[- ])?[a-z]+\b", q) and re.search(
        r"\b(?:non[- ]|versus|vs\.?|between)\b", q
    ):
        return "specific-relationship-requires-series-search"
    if re.search(r"\b(?:surveys?|survey[- ]years?|survey[- ]periods?|quintiles?|deciles?)\b", q):
        return "survey-dimension-requires-series-search"
    return None


def _official(table_id, url) -> bool:
    """True for a well-formed table ID with its HTTPS Table Builder URL."""
    if not isinstance(table_id, str) or not _ID.fullmatch(table_id):
        return False
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return (
        parsed.scheme == 'https'
        and parsed.hostname == 'tablebuilder.singstat.gov.sg'
        and parsed.path == table_page_path(table_id)
        and not parsed.query
        and not parsed.fragment
        and not parsed.username
        and not parsed.password
    )


def _subject_terms(question):
    """Title search terms: vocabulary expansion, else the question's own specific words."""
    expanded = expand_subject(question)
    if expanded:
        return expanded
    return tuple(
        sorted(t for t in tokens(question) if len(t) >= 5 and t not in _TOO_COMMON_TO_SEARCH)
    )


def _title_matches_subject(title, subject_terms) -> bool:
    """Short terms must match whole words; longer ones may match inside words."""
    lowered = title.casefold()
    return any(
        re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", lowered)
        if len(term) < 5
        else term in lowered
        for term in subject_terms
    )


def _measure_concept(title) -> tuple:
    """(concept id, label) used to group near-duplicate titles into one choice."""
    lowered = str(title or "").casefold()
    for identifier, label, title_phrases, _ in MEASURE_CONCEPTS:
        if any(phrase in lowered for phrase in title_phrases):
            return identifier, label
    return "other", "Other published measure"


def _concept_words_in(question, concept) -> bool:
    """True when the question uses a word that names ``concept``'s measure."""
    lowered = str(question or "").casefold()
    return any(
        re.search(r"\b" + re.escape(word) + r"\b", lowered)
        for identifier, _, _, words in MEASURE_CONCEPTS
        if identifier == concept
        for word in words
    )


def _user_specifies_measure(question) -> bool:
    """True when the wording is precise enough to tell measures apart."""
    lowered = str(question or "").casefold()
    return any(
        re.search(r"\b" + re.escape(word) + r"\b", lowered)
        for _, _, _, words in MEASURE_CONCEPTS
        for word in words
        if word not in MEASURE_WORDS_TOO_BROAD
    )


# --- What the question asks for (kept as public names for existing callers) ---


def requested_frequency(question):
    """Explicitly requested reporting frequency ("monthly"), else None."""
    return explicit_frequency(question)


def requested_measure(question):
    """Kind of measure the question asks for (see query_text.measure_kind)."""
    return measure_kind(question)


def _request_summary(question) -> dict:
    """What the question asked for, attached to every plan for the UI to show."""
    return {
        "requested_frequency": requested_frequency(question),
        "requested_measure": requested_measure(question),
        "requested_unit": requested_unit_dimension(question),
    }


# --- Compatibility: may this title answer this question at all? --------------


def compatibility(question: str, title: str) -> tuple:
    """Return (eligible, reason). Same rules for every table; a title proves no data.

    A table is ruled out when the question and the title state DIFFERENT values of
    the same fundamental property:

    1. kind of measure (projection, rate, density, growth, share, new flow);
       projections never answer a question about observed values, and a derived
       measure never answers an explicit "how many";
    2. population basis (residents vs citizens vs total population);
    3. unit / denominator ("per 1,000" vs "per 10,000"); a requested rate
       must be declared by the title;
    4. reporting frequency (monthly vs annual).

    When either side is silent about a property, that property does not exclude.
    """
    asked_kind, title_kind = measure_kind(question), measure_kind(title)
    if asked_kind and title_kind and asked_kind != title_kind:
        return False, f"measure:{title_kind}-not-{asked_kind}"
    if title_kind == "projection" and asked_kind != "projection":
        return False, "measure:projection-not-requested"
    if asks_for_level(question) and title_kind in DERIVED_KINDS:
        return False, f"measure:{title_kind}-not-level"
    asked_basis, title_basis = population_bases(question), population_bases(title)
    if asked_basis and title_basis and not asked_basis & title_basis:
        return False, "population-scope-mismatch"
    unit, title_unit = requested_unit_dimension(question), title_unit_dimension(title)
    if unit and title_unit and unit != title_unit:
        return False, f"unit:{title_unit}-not-{unit}"
    # A named denominator is compulsory: a bare count cannot stand in for a rate.
    if unit and unit.startswith("rate_per_") and title_unit != unit:
        return False, f"unit:{title_unit or 'unknown'}-not-{unit}"
    frequency, published = requested_frequency(question), title_frequency(title)
    if frequency and published != frequency:
        return False, f"frequency:{published or 'unknown'}-not-{frequency}"
    return True, None


def _score(question, title, concept):
    """Generic ranking score of one compatible title (higher = better fit)."""
    title_words = tokens(title)
    score = _SCORE_BASE + len(tokens(question) & title_words) * _SCORE_PER_SHARED_WORD
    if _concept_words_in(question, concept):
        score += _SCORE_MEASURE_CONCEPT
    if population_bases(question) & population_bases(title):
        score += _SCORE_SAME_POPULATION
    frequency = requested_frequency(question)
    if frequency and title_frequency(title) == frequency:
        score += _SCORE_SAME_FREQUENCY
    asked_kind, title_kind = measure_kind(question), measure_kind(title)
    if asked_kind and title_kind == asked_kind:
        score += _SCORE_SAME_MEASURE_KIND
    elif asked_kind is None and title_kind in DERIVED_KINDS:
        score -= _PENALTY_UNASKED_DERIVED
    unit = requested_unit_dimension(question)
    if unit and title_unit_dimension(title) == unit:
        score += _SCORE_SAME_UNIT
    return score


def safe_catalogue_rows(path: str | Path) -> tuple:
    """Load *only* verified metadata titles and URLs from a read-only index."""
    p = Path(path)
    if not p.is_file():
        return (), 0
    connection = None
    try:
        connection = sqlite3.connect(p.resolve().as_uri() + '?mode=ro', uri=True)
        rows = connection.execute(
            'SELECT DISTINCT table_id, title, source_url FROM docs WHERE '
            'doc_type IS NOT NULL ORDER BY table_id'
        ).fetchall()
    except (sqlite3.Error, OSError):
        return (), 0
    finally:
        if connection is not None:
            connection.close()
    approved = {}
    for tid, title, url in rows:
        if _official(tid, url) and isinstance(title, str) and title.strip() and len(title) <= 350:
            approved[tid] = {'table_id': tid, 'title': ' '.join(title.split()), 'url': url}
    return tuple(approved[k] for k in sorted(approved)), len(approved)


def discovery_plan(question: str, index_path: str | Path, max_choices: int = 6):
    """Return a deterministic plan: skip, direct, choose or review. Never data claims.

    Unknown subjects proceed to Sage's normal data search: the local index can
    be partial, so "no title matched" never means "no data exist".
    """
    skipped = {"action": "skip", "choices": [], "reason": "not-an-ambiguous-data-question"}
    if (
        not isinstance(question, str)
        or not question.strip()
        or len(question) > 4000
        or is_list_request(question)
        or names_table_id(question)
    ):
        return skipped
    reason = needs_series_search(question)
    if reason:
        return {**skipped, "reason": reason}
    subject = _subject_terms(question)
    if not subject:
        return skipped
    rows, total = safe_catalogue_rows(index_path)
    if not rows:
        return {**skipped, "reason": "local-catalogue-unavailable"}

    candidates, near_misses = [], []
    for item in rows:
        if not _title_matches_subject(item["title"], subject):
            continue
        eligible, incompatibility = compatibility(question, item["title"])
        if not eligible:
            # Shown transparently as near misses; never selectable.
            near_misses.append({**item, "incompatibility": incompatibility})
            continue
        concept, label = _measure_concept(item["title"])
        candidates.append(
            {
                **item,
                "concept": concept,
                "concept_label": label,
                "score": _score(question, item["title"], concept),
            }
        )
    candidates.sort(key=lambda row: (-row["score"], row["table_id"]))
    summary = _request_summary(question)

    if not candidates:
        if not near_misses:
            return {**skipped, "reason": "no-relevant-titles-in-local-index"}
        return {
            "action": "review",
            "choices": [],
            "near_matches": tuple(near_misses[:12]),
            "indexed_count": total,
            "partial_index": True,
            "reason": "no-compatible-titles-in-local-index",
            **summary,
        }

    best = candidates[0]
    runner = candidates[1] if len(candidates) > 1 else None
    clearly_best = (
        runner is not None
        and _user_specifies_measure(question)
        and best["score"] >= runner["score"] + _DIRECT_MARGIN
        and best["concept"] != "other"
    )
    if len(candidates) == 1 or clearly_best:
        return {
            "action": "direct",
            "choices": [best],
            "indexed_count": total,
            "reason": "one-measure-identified",
            "partial_index": True,
            **summary,
        }

    # Offer genuinely DISTINCT measures, not a wall of near-duplicate tables.
    selected, seen = [], set()
    for item in candidates:
        if item["concept"] in seen and item["concept"] != "other":
            continue
        selected.append(item)
        seen.add(item["concept"])
        if len(selected) >= max_choices:
            break
    # One measure published in several tables: let the user pick the source.
    if len(selected) == 1:
        selected = candidates[:max_choices]
    if len(selected) <= 1:
        return {
            "action": "direct",
            "choices": [best],
            "indexed_count": total,
            "reason": "one-matching-measure",
            "partial_index": True,
            **summary,
        }
    return {
        "action": "choose",
        "choices": selected,
        "all_matches": candidates,
        "indexed_count": total,
        "reason": "multiple-published-measures",
        "partial_index": True,
        **summary,
    }


def validate_selection(plan, table_id) -> bool:
    """Confirm a clicked table ID was actually offered for this exact question."""
    if not isinstance(plan, dict) or plan.get('action') != 'choose':
        return False
    return bool(
        isinstance(table_id, str)
        and _ID.fullmatch(table_id)
        and any(
            row.get('table_id') == table_id
            for row in (plan.get('all_matches') or plan.get('choices') or [])
        )
    )
