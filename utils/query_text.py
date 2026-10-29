"""What does a piece of text ASK FOR, or a table title DECLARE?

One module, one set of rules, used for BOTH questions and table titles, so
discovery, ranking and chart checks can never disagree about the same words.
(Earlier there were three different copies of the frequency rules.)

  * normalise_query          - capitalisation/spacing/punctuation never change a search
  * requested_frequency      - (frequency, strength) the wording asks for
  * explicit_frequency       - the frequency only when it is stated explicitly
  * title_frequency          - the frequency a title announces ("..., Annual")
  * requested_unit_dimension / title_unit_dimension - "per 1,000", "% by sex"
  * measure_kind             - projection / rate / density / growth / share / new_flow
  * asks_for_level           - "how many", "number of": a plain count or level
  * specific_words           - the words that identify a topic

All rules are generic: none mentions a table or a subject.

Used by: utils/ai_discovery.py, utils/chart_gallery.py,
utils/core_secure.py, utils/discovery_controller.py,
utils/discovery_service.py, utils/hybrid_discovery.py,
utils/kb_discovery_bridge.py, utils/rag_engine.py,
tools/test_search_trace.py.
Tested in: tests/test_generic_rules.py.
"""

from __future__ import annotations

import re
import unicodedata

_TABLE_ID = re.compile(r"\b[a-z]\d{5,8}\b")
_FREQUENCIES = ("annual", "quarterly", "monthly", "half-yearly", "weekly", "daily")

GENERIC_QUERY_WORDS = frozenset(
    """
a an the of in on at to for and or but is are was were be been being has have had do does did how what which who whom
when where why whose over under between from with without by about into than then that this these those there their
them they it its as can could would should will may might shall me my our your please tell show give explain describe
change changed changes changing years year time times trend trends history historical latest recent recently since now
today singapore singapores official statistics statistic data figures figure numbers number information info
""".split()
)


def normalise_query(text) -> str:
    """Casefold, tidy spacing and drop trailing punctuation, so "How did X change?" == "how did x change".

    Table IDs (Mxxxxxx, Cxxxxxx, Dxxxxx) keep their capitals because they are matched literally.
    """
    value = (
        unicodedata.normalize("NFKC", str(text or "")).replace("\u2019", "'").replace("\u2018", "'")
    )
    value = " ".join(value.split()).casefold().rstrip(" ?!.\u3002")
    return _TABLE_ID.sub(lambda match: match.group(0).upper(), value)


def requested_frequency(question) -> tuple:
    """(frequency, strength): what the wording asks for.

    strength is "explicit" ("each quarter", "monthly") or "soft" (a long-run phrase such as "over
    the years", which suggests annual data without demanding it). (None, None) when nothing is
    asked.
    """
    q = str(question or "").casefold()
    if re.search(
        r"\b(quarterly|quarter[- ]by[- ]quarter|each quarter|per quarter|every "
        r"quarter|quarters?)\b",
        q,
    ):
        return "quarterly", "explicit"
    if re.search(r"\b(monthly|month[- ]by[- ]month|each month|per month|every month|months)\b", q):
        return "monthly", "explicit"
    if re.search(r"\b(half[- ]yearly|half[- ]year)\b", q):
        return "half-yearly", "explicit"
    if re.search(
        r"\b(annually|annual|year[- ]by[- ]year|each year|per year|every year|yearly)\b", q
    ):
        return "annual", "explicit"
    if re.search(
        r"\b(over the (?:last |past )?(?:few |many |\d+ )?years|since "
        r"(?:19|20)\d{2}|histor(?:y|ical|ically)|"
        r"long[- ]term|decades?)\b",
        q,
    ):
        return "annual", "soft"
    return None, None


def explicit_frequency(question) -> str | None:
    """The frequency only when the wording states it ("monthly"), else None.

    Long-run phrases such as "over the years" do NOT count here.
    """
    frequency, strength = requested_frequency(question)
    return frequency if strength == "explicit" else None


def title_frequency(title) -> str | None:
    """The frequency a table title announces, e.g. "<subject>, Annual".

    SingStat titles put the frequency last, sometimes in brackets, so the LAST
    frequency word in the title wins ("Monthly Income ..., Annual" -> annual).
    """
    found = re.findall(
        r"(?i)\b(annual|quarterly|monthly|half-yearly|weekly|daily)\b", str(title or "")
    )
    return found[-1].lower() if found else None


# --- Units and kinds of measure (same rules for questions and titles) --------


def requested_unit_dimension(question) -> str | None:
    """An explicit denominator in the question: "per 1,000 residents", "% by sex"."""
    q = str(question or "").casefold().replace(",", "")
    if re.search(
        r"\b(?:per|for "
        r"every)\s*(?:10000|10\s*thousand)\s*(?:people|persons|population|residents|inhabitants)?\b",
        q,
    ):
        return "rate_per_10000"
    if re.search(
        r"\b(?:per|for "
        r"every)\s*(?:1000|one\s*thousand)\s*(?:people|persons|population|residents|inhabitants)?\b",
        q,
    ):
        return "rate_per_1000"
    if re.search(
        r"\b(?:percentage|percent|per "
        r"cent|share|proportion)\s+(?:of\s+)?(?:male|female|men|women|sex|gender)\b|\b(?:by|among)\s+(?:sex|gender)\b",
        q,
    ):
        return "sex_percentage"
    return None


def title_unit_dimension(title) -> str | None:
    """The denominator a title declares, read without guessing units."""
    t = str(title or "").casefold().replace(",", "")
    if re.search(r"\bper\s*10000\b", t):
        return "rate_per_10000"
    if re.search(r"\bper\s*1000\b", t):
        return "rate_per_1000"
    if re.search(r"\b(?:percentage|percent|per cent|share|proportion)\b", t) and re.search(
        r"\b(?:by|of|among)\s+(?:sex|gender)\b", t
    ):
        return "sex_percentage"
    return None


# Checked in order; the first match wins ("percentage change" is growth, not share).
_MEASURE_KINDS = (
    ("projection", r"\b(?:projections?|projected|forecasts?)\b"),
    ("rate", r"\bper\s*(?:capita|head|1\s*000|10\s*000|100\s*000|million|one thousand)\b"),
    ("density", r"\bdensity\b|\bper\s+(?:square|sq\.?)\s*(?:k(?:ilo)?m|kilomet)"),
    ("growth", r"\bgrowth\b|\b(?:percentage|per cent|percent|%)\s+change\b"),
    ("share", r"\b(?:percentage|percent|per cent|proportion|share)\b"),
    ("new_flow", r"\bnew(?:ly)?\s+(?:registrations?|registered)\b"),
)
# Kinds that are derived from a level and cannot answer "how many".
DERIVED_KINDS = frozenset({"rate", "density", "growth", "share", "projection"})


def measure_kind(text) -> str | None:
    """Kind of measure named in a question OR a title; None for a plain level.

    The same function reads both sides, so "density" in a question and in a
    title always means the same thing.
    """
    value = str(text or "").casefold().replace(",", "")
    for kind, pattern in _MEASURE_KINDS:
        if re.search(pattern, value):
            return kind
    return None


def asks_for_level(question) -> bool:
    """True when the question explicitly wants a count or level ("how many ...")."""
    return bool(
        re.search(
            r"\b(?:how many|number of|total number|count of)\b", str(question or "").casefold()
        )
    )


def specific_words(text) -> set:
    """The words in `text` that help identify a topic (everything except GENERIC_QUERY_WORDS)."""
    return {
        w
        for w in re.findall(r"[a-z0-9]+", str(text or "").casefold())
        if w not in GENERIC_QUERY_WORDS and len(w) > 2
    }
