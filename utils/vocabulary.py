"""Sage's domain vocabulary: the ONLY place where subject words live.

WHY THIS FILE EXISTS
====================
Sage must treat every SingStat table the same way (see the "golden rule" in
``utils/core_secure.py``). Earlier versions scattered topic words through the
code as special cases (e.g. "if the question mentions food AND expenditure, do
X"). Such branches quietly favour one table, are hard to find, and break when
the catalogue changes.

The rule now is:

* This module holds DATA only: words, synonyms and phrase patterns.
* Code elsewhere uses these tables through GENERIC mechanisms that run for
  every question and every table alike (search expansion, relevance checks,
  comparison coverage).
* Adding or changing a word here can change how a question is *understood*,
  but it can never add a code path for a particular table or topic.
* No statistic (number) may ever appear here.

``tests/test_no_table_specific_code.py`` enforces the most important parts of
this rule.

Used by: utils/answer_verification.py, utils/chart_gallery.py,
utils/chart_intent.py, utils/chart_selection.py, utils/core_secure.py,
utils/discovery_controller.py, utils/hybrid_discovery.py,
utils/request_gate.py, utils/scope_chart_bridge.py, utils/search_terms.py,
utils/source_notes.py, utils/survey_editions.py, utils/table_tools.py.
"""

from __future__ import annotations

# -----------------------------------------------------------------------------
# 1. Search expansion: everyday wording -> words that appear in official titles
# -----------------------------------------------------------------------------
# SEARCH TERMS only, never definitions: a match says "look at this table",
# not "these two statistics are the same". Longest phrase wins.
SUBJECT_SYNONYMS = {
    "hdb": ("housing and development board", "hdb", "public housing"),
    "public housing": ("housing and development board", "hdb", "public housing"),
    "housing": ("housing and development board", "hdb", "housing", "residential"),
    "healthcare": ("health", "hospital", "nurs", "doctor", "medical", "dental"),
    "medical": ("health", "hospital", "nurs", "doctor", "medical", "dental"),
    "doctors": ("doctor", "physician", "health personnel", "medical practitioner"),
    "doctor": ("doctor", "physician", "health personnel", "medical practitioner"),
    "nurses": ("nurse", "nurs", "health personnel"),
    "nurse": ("nurse", "nurs", "health personnel"),
    "dentists": ("dentist", "dental", "health personnel"),
    "pharmacists": ("pharmacist", "pharmacy", "health personnel"),
    "employment": ("employment", "labour", "occupation", "workforce"),
    "labour": ("employment", "labour", "occupation", "workforce"),
    "education": ("education", "school", "student", "enrolment"),
    "schools": ("education", "school", "student", "enrolment"),
    "population": ("population",),
    "motor vehicle": ("motor vehicle", "vehicle registration", "vehicle population"),
    "motor-vehicle": ("motor vehicle", "vehicle registration", "vehicle population"),
    "vehicles": ("motor vehicle", "vehicle registration", "vehicle population"),
}

# Kinds of measurement, used ONLY to group near-duplicate table titles into
# readable choices for the user ("which of these did you mean?").
# Each entry: (id, label shown to the user, title phrases, question words).
MEASURE_CONCEPTS = (
    (
        "housing-stock",
        "Properties under management",
        ("under the management", "properties under", "housing stock"),
        ("managed", "management", "stock", "properties"),
    ),
    (
        "housing-building",
        "Flats built or sold",
        ("constructed and sold", "flats constructed", "units constructed"),
        ("constructed", "built", "construction", "sold"),
    ),
    (
        "housing-estate",
        "Housing by estate",
        ("by town", "by estate", "estate", "town"),
        ("estate", "town", "location"),
    ),
    (
        "housing-prices",
        "Resale prices",
        ("resale price", "resale index"),
        ("resale", "prices", "price"),
    ),
    (
        "employment-change",
        "Change in employment",
        ("changes in employment", "employment change"),
        ("change", "growth", "jobs"),
    ),
    (
        "employment-level",
        "Employment levels",
        ("employed persons", "employment by"),
        ("employed", "levels", "workforce"),
    ),
    ("life-expectancy", "Life expectancy", ("life expectancy",), ("expectancy", "longevity")),
    (
        "population-count",
        "Population counts",
        ("population by", "total population"),
        ("population", "residents"),
    ),
    (
        "health-personnel",
        "Health personnel",
        ("registered health personnel",),
        ("healthcare", "health", "medical", "professionals"),
    ),
)
# Question words too broad to say WHICH measure is meant.
MEASURE_WORDS_TOO_BROAD = frozenset(
    {
        "change",
        "growth",
        "population",
        "levels",
        "workforce",
        "jobs",
        "health",
        "medical",
        "professionals",
    }
)

# -----------------------------------------------------------------------------
# 2. Chart relevance: does a chart's subject match the question's subject?
# -----------------------------------------------------------------------------
CHART_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "change",
        "chart",
        "compare",
        "data",
        "during",
        "each",
        "figures",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "latest",
        "many",
        "measure",
        "more",
        "much",
        "number",
        "of",
        "on",
        "one",
        "over",
        "published",
        "recent",
        "report",
        "reported",
        "see",
        "show",
        "singapore",
        "singstat",
        "statistic",
        "statistics",
        "table",
        "than",
        "that",
        "the",
        "their",
        "them",
        "there",
        "these",
        "this",
        "time",
        "to",
        "trend",
        "trends",
        "was",
        "were",
        "what",
        "when",
        "which",
        "who",
        "with",
        "year",
        "years",
        "annual",
        "based",
        "official",
        "series",
        "value",
        "values",
        "total",
        "rate",
        "rates",
    }
)
CHART_EQUIVALENTS = {
    "pupils": "enrolment",
    "pupil": "enrolment",
    "student": "enrolment",
    "students": "enrolment",
    "schooling": "school",
    "schools": "school",
    "insured": "insurance",
    "policies": "policy",
    "children": "child",
    "girls": "female",
    "boys": "male",
    "women": "female",
    "men": "male",
    "births": "birth",
    "born": "birth",
    "seniors": "elderly",
    "aging": "ageing",
    "older": "elderly",
    "salaries": "salary",
    "employees": "employment",
    "employed": "employment",
    "prices": "price",
    "residents": "resident",
    "citizens": "citizen",
    "inflation": "cpi",
}
# Words that name the same subject. If the question uses a word from a family,
# the chart must use at least one word of the SAME family; a chart about a
# different subject that merely shares a broad word ("number") is rejected.
CONCEPT_FAMILIES = (
    frozenset({"insurance", "policy"}),
    frozenset({"school", "enrolment"}),
)
# Age groups named in words, and the numeric age bands that may stand in for them
# on a chart label. Patterns are applied to text with en dashes already replaced by "-".
AGE_GROUP_BANDS = {
    "child": r"\b0-14\b|\b5-9\b|\b10-14\b",
    "youth": r"\b15-19\b|\b20-24\b|\b20-35\b",
    "elderly": r"\b65\+|\b65-69\b|\b70-74\b",
}
# Population qualifiers that an overall series can never stand in for.
SUBGROUP_QUALIFIERS = frozenset({"male", "female", "citizen", "resident"})
# Any wording that narrows the question to a subgroup (sex, citizenship, age).
SUBGROUP_WORDS_PATTERN = (
    r"(?i)\b(?:male|female|men|women|girls|boys|citizen|resident|"
    r"children|child|youth|older|elderly|aged?\s+\d+)\b"
)
# Words that frame WHO is counted rather than WHAT is measured.
BROAD_POPULATION_WORDS = frozenset({"singapore", "population", "resident", "citizen"})
# Extra synonyms used when matching a generic table preview to a question.
PREVIEW_TOPIC_EQUIVALENTS = {
    "economic": "gdp",
    "activity": "gdp",
    "industries": "industry",
    "sectors": "industry",
    "inflation": "price",
    "prices": "price",
}
# One-word measures distinctive enough to identify a table on their own
# (unlike "population" or "employment", which many tables share).
DISTINCTIVE_SINGLE_MEASURES = frozenset(
    {
        "ridership",
        "training",
        "qualification",
        "fertility",
        "visitor",
        "tourism",
        "retail",
        "wholesale",
        "tariff",
        "consumption",
        "investment",
        "earning",
        "earnings",
        "schooling",
        "marriage",
        "divorce",
        "industrial",
    }
)
# Wording that asks for a breakdown across categories (sex, age, industry ...).
BREAKDOWN_DIMENSION_PATTERN = (
    r"\b(?:by|across|among|different)\s+"
    r"(?:sex|gender|age|occupations?|industries|sectors?|"
    r"groups?|types?|categories|education|school\s+levels?)\b"
)
# Column headings that mark a table column as a list of categories.
CATEGORY_HEADING_PATTERN = (
    r"\b(?:cause|category|sector|industry|occupation|type|level|" r"age|vehicle|group|facility)\b"
)

# Word forms merged when checking that rebuilt answer text matches its evidence.
EVIDENCE_TOPIC_EQUIVALENTS = {
    "working": "employment",
    "employed": "employment",
    "industries": "industry",
    "policies": "policy",
    "residents": "resident",
}
# Narrow subjects: if the question names one, the series used must name it too
# (a broad table title alone cannot stand in for a sex, sector or policy subgroup).
NARROW_SUBJECT_WORDS = frozenset(
    {
        "insurance",
        "policy",
        "female",
        "male",
        "healthcare",
        "education",
        "school",
        "tourism",
        "manufacturing",
        "construction",
        "finance",
        "citizen",
        "nonresident",
    }
)

# Word forms merged when matching a scoped preview chart to its question.
SCOPE_WORD_EQUIVALENTS = {
    "healthcare": "health",
    "medical": "health",
    "professionals": "personnel",
    "professional": "personnel",
    "professions": "personnel",
    "workers": "personnel",
    "workforce": "personnel",
    "industries": "industry",
    "sectors": "industry",
    "employed": "employment",
    "jobs": "employment",
    "students": "student",
    "schools": "school",
    "nurses": "nurse",
    "doctors": "doctor",
}
# Subgroup words that a scoped chart must carry if the question names them.
SCOPE_QUALIFIER_WORDS = ("citizen", "resident", "female", "male", "children", "youth", "elderly")

# A narrow subgroup named in the question that EVERY charted series must name
# too: (pattern in the question, pattern each series name must match).
REQUIRED_SUBGROUP_PATTERNS = (
    (r"\b(?:first[- ]born|first\s+order|1st\s+order)\b", r"\b(?:1st|first)\s*(?:order|born)\b"),
)

# -----------------------------------------------------------------------------
# 3. Comparisons: "compare X and Y" must show BOTH groups
# -----------------------------------------------------------------------------
GROUP_EQUIVALENTS = {
    "children": "child",
    "kids": "child",
    "young": "child",
    "elderly": "elderly",
    "older": "elderly",
    "seniors": "elderly",
    "men": "male",
    "boys": "male",
    "women": "female",
    "girls": "female",
    "births": "birth",
    "babies": "birth",
    "deaths": "death",
    "goods-producing": "goods",
    "services-producing": "services",
}
# (regex, replacement) applied before GROUP_EQUIVALENTS.
GROUP_PHRASES = (
    (r"\b0\s*-\s*14\b", " child "),
    (r"\b65\s*(?:\+|and over\b|years and over\b)", " elderly "),
    (r"\bpermanent residents?\b", " permanent resident "),
    (r"\bgoods[- ]producing\b", " goods "),
    (r"\bservices[- ]producing\b", " services "),
)
COMPARISON_GROUPS = (
    frozenset(("child", "elderly")),
    frozenset(("male", "female")),
    frozenset(("birth", "death")),
    frozenset(("goods", "services")),
    frozenset(("citizen", "permanent resident")),
)

# -----------------------------------------------------------------------------
# 4. Discovery word normalisation (table explorer ranking)
# -----------------------------------------------------------------------------
# Single words mapped to one search concept. A word preceded by "non" keeps its
# negation ("non-fatal" -> "nonfatal"), so opposite concepts never merge.
CONCEPT_WORDS = {
    "death": "fatal",
    "deaths": "fatal",
    "died": "fatal",
    "dying": "fatal",
    "fatal": "fatal",
    "fatality": "fatal",
    "fatalities": "fatal",
    "injury": "injury",
    "injuries": "injury",
    "workplace": "workplace",
    "workplaces": "workplace",
    "work": "workplace",
    "occupational": "workplace",
}
# (regex on the whole text, concept added when it matches). Every pattern must
# also be satisfied by the CONTEXT pattern, so a phrase is not read out of context.
CONCEPT_PHRASES = (
    # Citizen-and-non-citizen marriage pairs are "transnational" marriages.
    (
        (
            r"\b(?:citizen|citizens)\s*(?:-to-|to|and|with|/)\s*non[- ]?citizen(?:s)?\b"
            r"|\bnon[- ]?citizen(?:s)?\s*(?:-to-|to|and|with|/)\s*citizen(?:s)?\b"
            r"|\bmarriages?\b.{0,85}\b(?:citizen|citizens)\b.{0,45}\bnon[- ]?citizen(?:s)?\b"
        ),
        r"\bmarri(?:age|ages|ed)\b|\bweddings?\b",
        "transnational",
    ),
)

# Everyday names of official series -> the official label (after the core's
# label normalisation). A label listed here must then match EXACTLY.
SERIES_LABEL_ALIASES = {
    "all items cpi": "all items",
    "overall cpi": "all items",
    "housing utilities": "housing and utilities",
    "housing and utilities": "housing and utilities",
}

# -----------------------------------------------------------------------------
# 5. Scope: is this a question about official statistics at all?
# -----------------------------------------------------------------------------
SCOPE_TOPIC_PATTERNS = (
    r"(?i)\b(population|demograph|birth|death|marriage|divorce|life "
    r"expectancy|household|income|expenditure)\b",
    r"(?i)\b(?:living|live)\s+(?:much\s+)?longer\b|\blongevity\b|\blifespans?\b",
    r"(?i)\b(gdp|gross domestic product|cpi|consumer price|inflation|price index|exchange "
    r"rate|economy|economic)\b",
    r"(?i)\b(trade|imports?|exports?|investment|manufactur|industry|construction|retail|services|tourism)\b",
    r"(?i)\b(employment|unemployment|labou?r force|workforce|wages?|earnings?|productivity)\b",
    r"(?i)\b(education|students?|schools?|health|healthcare|transport|housing|crime|energy|electricity|environment)\b",
    r"(?i)\b(mid[- ]?life|ageing|aging|elderly|older "
    r"(?:people|persons|population|households?)|seniors?|65\+|aged? 65|population "
    r"(?:aged|age|structure|pyramid))\b",
    r"(?i)\b(?:population|age|demographic)(?:[- ](?:group|sex))?(?:[- ]distribution)?[- ]pyramid\b",
    r"(?i)\bpopulation\s+by\s+(?:age|age group)\s+and\s+(?:sex|gender)\b",
)


# Short, vague questions mentioning exactly one of these topics get a
# "did you mean Singapore's <topic>?" clarification instead of a refusal.
CLARIFICATION_TOPICS = (
    ("population", ("people", "population", "resident", "citizen", "ageing", "birth")),
    ("jobs and employment", ("job", "employment", "unemployment", "wage", "salary")),
    ("prices and inflation", ("price", "inflation", "cost of living", "expensive", "cpi")),
    ("housing and households", ("house", "housing", "household", "rent")),
    ("education", ("school", "student", "education", "university")),
    ("health", ("hospital", "health", "doctor", "patient")),
    ("transport", ("transport", "train", "bus", "commute", "traffic")),
    ("trade and the economy", ("economy", "gdp", "trade", "export", "import")),
)


def expand_subject(text):
    """Return official-title search terms for the longest subject phrase in ``text``.

    Returns an empty tuple when no known subject is named; callers then fall
    back to the question's own specific words.
    """
    import re

    lowered = str(text or "").casefold()
    for phrase in sorted(SUBJECT_SYNONYMS, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", lowered):
            return SUBJECT_SYNONYMS[phrase]
    return ()


# -----------------------------------------------------------------------------
# Official surveys named in SingStat footnotes (pattern, display name).
# Used only to quote the footnote's own "reference survey and year".
# -----------------------------------------------------------------------------
REFERENCE_SURVEYS = (
    (r"General Household Survey|GHS", "General Household Survey"),
    (r"Census of Population|Population Census|Census", "Census of Population"),
    (r"Household Expenditure Survey|HES", "Household Expenditure Survey"),
    (r"Comprehensive Labour Force Survey|Labour Force Survey|LFS", "Labour Force Survey"),
)

# -----------------------------------------------------------------------------
# ISO 4217 currency codes people type in questions ("compare sgd and myr"),
# with the currency names official titles and units use. Search only: the codes
# never decide a number or a table, they just let "myr" find "Malaysian Ringgit".
# -----------------------------------------------------------------------------
CURRENCY_CODES = {
    "sgd": "singapore dollar",
    "usd": "us dollar",
    "myr": "malaysian ringgit",
    "eur": "euro",
    "gbp": "sterling pound",
    "jpy": "japanese yen",
    "cny": "chinese renminbi",
    "rmb": "chinese renminbi",
    "hkd": "hong kong dollar",
    "aud": "australian dollar",
    "nzd": "new zealand dollar",
    "cad": "canadian dollar",
    "chf": "swiss franc",
    "idr": "indonesian rupiah",
    "thb": "thai baht",
    "php": "philippine peso",
    "inr": "indian rupee",
    "krw": "korean won",
    "twd": "taiwan dollar",
    "vnd": "vietnamese dong",
    "sar": "saudi arabia riyal",
    "aed": "uae dirham",
    "sek": "swedish krona",
    "nok": "norwegian krone",
    "dkk": "danish krone",
}
