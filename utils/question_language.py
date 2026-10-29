"""Plain-language question design for the Sage sample bank.

Official SingStat table titles are written for statisticians ("Resident Households by Monthly
Household Market Income and Sex of Household Reference Person"). This module turns a title into a
question that a teenager or young adult would actually ask, and that makes a clear answer possible:

    title  ->  parse_title()      structured facts: subject, breakdowns, period, frequency,
    qualifiers
           ->  plain_*()          jargon replaced with everyday words ("Sex" -> "gender")
           ->  build_questions()  several clear, distinct questions that always state the place
                                  (Singapore), the period (when the title gives one) and the
                                  breakdown
           ->  problems()         a quality gate; a question that fails it is never used

Rules this module keeps:
  * Only facts that are IN the title are used. Nothing is invented: no categories, numbers or periods.
  * Context is always explicit: where (Singapore), when (the years in the title) and how it is split.
  * "Sex" is always written as "gender"; "males and females" is used when gender is the split.
  * No statistical jargon reaches the reader (decile, 2-digit, SSIC, chained dollars, ...): it is
    either replaced by a plain phrase or left out.
Pure functions only: no I/O, no Streamlit, no model calls.

Used by: utils/question_ai.py, utils/question_generator.py,
tools/characterise_question_bank.py.
Tested in: tests/test_question_tools.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------------
# 1. Parsing the title
# --------------------------------------------------------------------------------------------
_PERIOD = r"(?:19|20)\d{2}(?:/\d{2})?"
_PERIOD_RUN = re.compile(rf"(?<![\w.]){_PERIOD}(?:\s*(?:-|–|,|&|and|to)\s*{_PERIOD})*(?![\w.])")
_FREQUENCY = re.compile(r"(?i)(?:,\s*)?\b(annual|monthly|quarterly|half-yearly|weekly|daily)\b\s*$")
_ETHNIC_FILTERS = {
    "chinese": "Chinese",
    "malays": "Malay",
    "malay": "Malay",
    "indians": "Indian",
    "indian": "Indian",
    "eurasians": "Eurasian",
    "others": "other ethnic groups",
}
_COMPOUNDS = (
    r"goods\s+and\s+services",
    r"food\s+and\s+beverage(?:s)?",
    r"husband\s+and\s+wife",
    r"arts,\s*entertainment\s*(?:and|&)\s*recreation",
    r"health\s+and\s+social(?:\s+services)?",
    r"information\s+and\s+communications?",
    r"transportation\s+and\s+storage",
    r"wholesale\s+and\s+retail(?:\s+trade)?",
    r"finance\s+and\s+insurance",
    r"professional\s+services",
    r"administrative\s+and\s+support(?:\s+services)?",
    r"forward\s+and\s+backward",
    r"imports?\s+and\s+exports?",
    r"men\s+and\s+women",
    r"male\s+and\s+female",
    r"accommodation\s+and\s+food",
    r"water\s+supply",
    r"arts\s+and\s+entertainment",
    r"sewerage\s+and\s+waste",
)
_NOT_A_DIMENSION = re.compile(
    r"(?i)^(?:end\s+june|end\s+of\s+period|mid-?year|seasonally\s+adjusted|"
    r"annual|monthly|quarterly|current|at\s+current\s+prices|base\s+year)$"
)
_TECHNICAL = re.compile(
    r"(?i)input[- ]output|requirement\s+coefficients?|multipliers?|linkages|"
    r"supply\s+table|use\s+table|primary\s+inputs?|final\s+demand|import\s+content|"
    r"purchasers'?\s+prices|basic\s+prices"
)


@dataclass
class TitleSpec:
    """What a table title says, in structured form. Everything comes from the title itself."""

    raw: str
    subject: str = ""
    dims: list = field(default_factory=list)
    # as published, e.g. ["2015", "2025"] or ["2002/03", "2023"]
    periods: list = field(default_factory=list)
    # annual / quarterly / monthly / half-yearly / ...
    frequency: str | None = None
    # "real" (adjusted for price changes) or "current"
    price_basis: str | None = None
    index_base: str | None = None  # e.g. "2017 = 100"
    filters: list = field(default_factory=list)  # e.g. ["Chinese"] from "(Chinese)"
    notes: list = field(default_factory=list)  # kept parenthetical remarks
    flavour: str | None = None  # distribution | share | percent_change
    growth: str | None = None  # what a growth rate is measured against
    # the part after " - " (what exactly is measured)
    detail: str | None = None
    seasonally_adjusted: bool = False
    is_cube: bool = False
    technical: bool = False


def _protect(text: str) -> str:
    """Hide spaces and commas inside known compound names so splitting on them keeps the name whole."""
    for pattern in _COMPOUNDS:
        text = re.sub(
            pattern,
            lambda m: m.group(0).replace(" ", "\x00").replace(",", "\x01"),
            text,
            flags=re.I,
        )
    return text


def _restore(text: str) -> str:
    """Undo _protect."""
    return text.replace("\x00", " ").replace("\x01", ",")


def _split_dims(clause: str) -> list:
    """The dimensions named in a title clause ("By Sex And Age Group" -> ["Sex", "Age Group"])."""
    pieces = re.split(
        r",\s*|\s+and\s+|\s*&\s*", _protect(clause), flags=re.I
    )  # Title Case "And" too
    cleaned = [
        re.sub(
            r"(?i)^(?:and|&)\s+",
            "",
            re.sub(
                r"^\(?[A-Za-z0-9]\)\s*",
                "",
                # "A) Sex And B) Age Group"
                re.sub(r"\s+", " ", _restore(p)).strip(" ,-"),
            ),
        )
        for p in pieces
    ]
    # "A, B, And C" (serial comma) leaves no stray "And"
    return [p for p in cleaned if p]


def _initialism_of(acronym: str, before: str) -> bool:
    """True when "(HQA)" merely abbreviates the words just before it ("Highest Qualification
    Attained")."""
    letters = acronym.replace("&", "")
    words = [
        w
        for w in re.findall(r"[A-Za-z']+", before)
        if w.lower() not in {"of", "and", "the", "in", "for", "to", "a"}
    ]
    return (
        len(words) >= len(letters)
        and "".join(w[0].upper() for w in words[-len(letters) :]) == letters
    )


def parse_title(title: str) -> TitleSpec:
    """Break a published table title into its parts. Never raises; unknown shapes stay in `subject`.

    Each step records one kind of information in the spec and removes it from
    the remaining wording, in this order: bracketed notes, index base and
    growth basis, adjustment and price basis, periods and frequency, the
    title's "flavour" (share, distribution, change), then subject and
    breakdown dimensions.
    """
    spec = TitleSpec(raw=str(title or ""))
    text = " ".join(spec.raw.split())
    if re.match(r"(?i)^\[multi-dimensional data cube\]", text):
        spec.is_cube = True
        text = re.sub(r"(?i)^\[multi-dimensional data cube\]\s*", "", text)
    spec.technical = bool(_TECHNICAL.search(text))
    for step in (
        _take_bracket_notes,
        _take_base_and_growth,
        _take_adjustment_and_prices,
        _take_periods_and_frequency,
        _take_flavour,
    ):
        text = step(spec, text)
    _take_subject_and_dimensions(spec, text)
    if spec.growth and spec.flavour is None:
        spec.flavour = "percent_change"
    spec.periods = list(dict.fromkeys(spec.periods))
    return spec


def _take_bracket_notes(spec, text) -> str:
    """Classify each parenthetical remark (index base, code list, ethnic filter, acronym, note)."""

    def classify(match):
        """Record what a bracketed title note means (index base, unit, period ...)."""
        inner = match.group(1).strip()
        if re.fullmatch(r"\d{4}\s*=\s*100", inner):
            spec.index_base = re.sub(r"\s+", " ", inner)
            return ""
        based = re.fullmatch(r"(?i)based on ((?:19|20)\d{2})\s*=\s*100(?:\s+index)?", inner)
        if based:
            spec.index_base = f"{based.group(1)} = 100"
            return ""
        if re.fullmatch(r"\d{4}", inner):  # "Chained (2015) Dollars"
            return ""
        if re.fullmatch(
            r"(?i)(?:ssic|ssoc)\s*\d{4}|\d+-digit(?:\s+level)?(?:\s+to\s+\d+-digit(?:\s+level)?)?|"
            r"end of period|flows?|stocks?",
            inner,
        ):
            return ""
        if inner.lower() in _ETHNIC_FILTERS:
            spec.filters.append(_ETHNIC_FILTERS[inner.lower()])
            return ""
        if re.fullmatch(
            r"[A-Z][A-Z&]{1,6}", inner
        ):  # an acronym: part of the name unless redundant
            return "" if _initialism_of(inner, match.string[: match.start()]) else match.group(0)
        spec.notes.append(inner)
        return ""

    text = re.sub(r"\(([^()]*)\)", classify, text)

    return text


def _take_base_and_growth(spec, text) -> str:
    """Record the index base year and the growth basis ("year-on-year", "annualised" ...)."""
    base = re.search(r"(?i),?\s*\bbase\s+year\s+((?:19|20)\d{2})\s*=\s*100\b", text)
    if base:
        spec.index_base = spec.index_base or f"{base.group(1)} = 100"
        text = text.replace(base.group(0), "")
    base = re.search(r"(?i),?\s*\b((?:19|20)\d{2})\s+as\s+base\s+year\b", text)
    if base:
        spec.index_base = spec.index_base or f"{base.group(1)} as the base year"
        text = text.replace(base.group(0), "")
    for pattern, basis in (
        (
            r"(?i),?\s*\byear[- ]on[- ]year(?:\s+growth(?:\s+rate)?)?\b",
            "the same period a year earlier",
        ),
        (r"(?i),?\s*\bquarter[- ]on[- ]quarter\b", "the previous quarter"),
        (r"(?i),?\s*\bmonth[- ]on[- ]month\b", "the previous month"),
        (
            r"(?i),?\s*\bover corresponding period of previous year\b",
            "the same period a year earlier",
        ),
        (r"(?i),?\s*\bover (?:the )?previous year\b", "the previous year"),
        (r"(?i),?\s*\bover (?:the )?previous (period|quarter|month)\b", "the previous period"),
    ):
        found = re.search(pattern, text)
        if found:
            spec.growth = spec.growth or basis
            text = text.replace(found.group(0), "")
    for pattern in (
        r"(?i),?\s*\bannualised(?:\s+growth)?(?:\s+rate)?\b",
        r"(?i),?\s*\bgrowth\s+rate\b",
    ):
        if re.search(pattern, text):
            spec.growth = spec.growth or "the previous period"
            text = re.sub(pattern, "", text, count=1)

    return text


def _take_adjustment_and_prices(spec, text) -> str:
    """Record seasonal adjustment and the price basis (real/chained or current prices)."""
    if re.search(r"(?i)\bseasonally\s+adjusted\b", text):
        spec.seasonally_adjusted = True
        text = re.sub(r"(?i),?\s*\bseasonally\s+adjusted\b", "", text)
    real = re.search(
        r"(?i),?\s*\b(?:in\s+)?chained\b[^,]*?\b(?:dollars|volume\s+terms)\b|,?\s*\bat\s+constant\b[^,]*?\bprices\b",
        text,
    )
    if real:
        spec.price_basis = "real"
        text = text.replace(real.group(0), "")
    current = re.search(
        r"(?i),?\s*\bat\s+current\s+(?:market\s+)?prices\b|,\s*current\s*(?=,|$)", text
    )
    if current:
        spec.price_basis = spec.price_basis or "current"
        text = text.replace(current.group(0), "")

    return text


def _take_periods_and_frequency(spec, text) -> str:
    """Record every published year / year pair and the reporting frequency."""
    # Periods: every year / year-pair as published, then removed from the wording.
    for run in _PERIOD_RUN.finditer(text):
        spec.periods += re.findall(_PERIOD, run.group(0))
    text = re.sub(rf"\s*,?\s*{_PERIOD_RUN.pattern}", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,-")

    freq = _FREQUENCY.search(text)
    if freq:
        spec.frequency = freq.group(1).lower()
        text = text[: freq.start()].strip(" ,-")

    return text


def _take_flavour(spec, text) -> str:
    """Record a leading "Share of", "Distribution of" or "Change in" and drop it."""
    flavours = (
        (r"(?i)^(?:percentage\s+)?distribution\s+of\s+", "distribution"),
        (r"(?i)^share\s+of\s+", "share"),
        (r"(?i)^proportion\s+of\s+", "share"),
        (r"(?i)^percent(?:age)?\s+change\s+in\s+", "percent_change"),
        (r"(?i)^changes?\s+in\s+", "percent_change"),
    )
    for pattern, name in flavours:
        if re.match(pattern, text):
            spec.flavour = name
            text = re.sub(pattern, "", text, count=1)
            break

    return text


def _take_subject_and_dimensions(spec, text) -> None:
    """Split "<subject> by <dimension>, <dimension> ..." and tidy the parts."""
    parts = re.split(r"\s+by\s+", text, flags=re.I)
    spec.subject = re.sub(r"\s+", " ", parts[0]).strip(" ,-")
    for clause in parts[1:]:
        for dim in _split_dims(clause):
            low = dim.lower()
            if re.fullmatch(r"(?:middle|top|bottom|lowest|highest|upper|lower)\s+\d+%", low):
                spec.filters.append(f"the {low} of households by income")
            elif re.search(r"over corresponding period of previous year$", low):
                dim = re.sub(r"(?i)\s*over corresponding period of previous year$", "", dim).strip()
                if dim:
                    spec.dims.append(dim)
            elif not _NOT_A_DIMENSION.match(dim):
                spec.dims.append(dim)
    dash = re.split(r"(?<!\d)\s+-\s+(?!\d)", spec.subject, maxsplit=1)
    if len(dash) == 2:
        main, rest = dash
        if rest.strip().lower() in {
            "estimated",
            "provisional",
            "preliminary",
            "advance estimates",
            "advance",
        }:
            spec.subject = main
            spec.notes.append(rest.strip().lower())
        else:
            spec.subject, spec.detail = main.strip(), rest.strip()
    cleaned = []
    for dim in spec.dims:
        pair = re.split(r"(?<!\d)\s+-\s+(?!\d)", dim, maxsplit=1)
        if len(pair) == 2 and not re.fullmatch(r"\d+", pair[0]):
            dim, spec.detail = pair[0].strip(), spec.detail or pair[1].strip()
        cleaned.append(dim)
    spec.dims = cleaned
    if any(re.match(r"(?i)household income group$", d) for d in spec.dims) and any(
        "by income" in f for f in spec.filters
    ):
        spec.dims = [d for d in spec.dims if not re.match(r"(?i)household income group$", d)]


# --------------------------------------------------------------------------------------------
# 2. Plain language
# --------------------------------------------------------------------------------------------
_KEEP_UPPER = {
    "cpi",
    "gdp",
    "gni",
    "hdb",
    "cpf",
    "pr",
    "ict",
    "gst",
    "coe",
    "fdi",
    "sme",
    "smes",
    "asean",
    "usd",
    "sgd",
    "rd",
    "ipo",
    "ipos",
    "mrt",
    "lrt",
    "bto",
    "nea",
    "pub",
    "jtc",
    "ura",
    "dos",
}
_PROPER = (
    "singapore's",
    "singapore",
    "chinese",
    "malays",
    "malay",
    "indians",
    "indian",
    "eurasians",
    "eurasian",
    "asean",
    "china",
    "malaysia",
    "indonesia",
    "japan",
    "india",
    "taiwan",
    "korea",
    "thailand",
    "vietnam",
    "philippines",
    "australia",
    "europe",
    "european",
    "america",
    "united states",
    "hong kong",
    "buddhism",
    "christianity",
    "islam",
    "hinduism",
    "taoism",
    "sikhism",
    "english",
    "mandarin",
    "tamil",
    "hokkien",
    "cantonese",
    "teochew",
    "orchard",
)

# (pattern on lower-case text, replacement). Order matters: specific phrases before general words.
_GLOSSARY = (
    (
        r"\bsex of (?:the )?(?:household reference person|head of (?:the )?household)\b",
        "gender of the head of the household",
    ),
    (
        r"\b(?:the )?(?:household reference person|head of (?:the )?household)\b",
        "the head of the household",
    ),
    (r"\bsex\b", "gender"),
    (r"\bmain income earner\b", "main income earner"),
    (r"(?<!singapore )\bemployed residents\b", "employed Singapore residents"),
    (r"(?<!singapore )\bresident population\b", "Singapore residents"),
    (
        r"(?<!singapore )\bresident (households|students|university "
        r"graduates|graduates|couples|workers)\b",
        r"Singapore resident \1",
    ),
    (r"(?<!singapore )(?<!resident )\bresidents\b", "Singapore residents"),
    (r"\baged (\d+)\s*[-–]\s*(\d+)(?: years)?\b", r"aged \1 to \2"),
    (r"\baged (\d+)(?: years)? and over\b", r"aged \1 and over"),
    (r"\baged (\d+)(?: years)? to (\d+)(?: years)?\b", r"aged \1 to \2"),
    (r"\bexpenditure (?:decile|quintile)s?\b", "spending group"),
    (r"\bincome (?:decile|quintile)s?\b", "income group"),
    (r"\b(?:deciles|quintiles|decile|quintile)\b", "income group"),
    (r"\btype of goods and services\b", "type of spending"),
    (r"\bhighest qualification attained\b", "highest education level"),
    (
        r"\bplace where highest qualification was attained\b",
        "where they got their highest qualification",
    ),
    (
        r"\blevel of difficulty in performing basic activities\b",
        "difficulty doing everyday activities",
    ),
    (
        r"\b(?:presence of at least one member with )?difficulty in performing basic activities\b",
        "difficulty doing everyday activities",
    ),
    (r"\blanguage most / second most frequently spoken at home\b", "language spoken at home"),
    (r"\blanguage literate in\b", "languages they can read and write"),
    (r"\busual mode of transport to (school|work)\b", r"how they usually travel to \1"),
    (r"\btravelling time to work\b", "travelling time to work"),
    (r"\bplanning area/subzone of residence\b", "area and subzone they live in"),
    (r"\bplanning area of residence\b", "area they live in"),
    (r"\bplanning region of residence\b", "region they live in"),
    (r"\bplanning area of workplace\b", "area they work in"),
    (r"\bplanning region of workplace\b", "region they work in"),
    (r"\bplanning (area|region)\b", r"\1"),
    (r"\btype of dwelling\b", "type of home"),
    (r"\bfloor area of residence\b", "floor area of their home"),
    (r"\btenancy\b", "tenancy (owned or rented)"),
    (r"\bresidential status\b", "citizen or PR status"),
    (r"\bhousehold living arrangement\b", "living arrangement"),
    (r"\bnumber of employed persons in household\b", "number of working people in the household"),
    (r"\bethnic composition of households\b", "ethnic make-up of the household"),
    (r"\blevel of education attending\b", "level of education they are attending"),
    (r"\bper household member\b", "per person in the household"),
    (r"\bmajor trading partner\b", "main trading partner"),
    (r"\bcommodity (?:section|division|group)\b", "type of goods"),
    (r"\bgross domestic product\b", "GDP"),
    (r"\bconsumer price index(?:\s*\(cpi\))?(?!\w)", "consumer prices (CPI)"),
    (r"\bmonthly household employment income\b", "monthly household income from work"),
    (r"\bincome source\b", "source of income"),
    (r"\bnumber of children born\b", "number of children born"),
    (r"\bservices category\b", "type of service"),
    (r"\beconomic sector\b", "sector of the economy"),
)

_ARTICLE_NOUNS = {
    "index",
    "indicator",
    "rate",
    "ratio",
    "balance",
    "position",
    "total",
    "number",
    "level",
    "value",
}
_MEASURE_WORDS = re.compile(
    r"\b(average|median|total|index|rate|expenditure|income|price|prices|value|number|percent|percentage|ratio|"
    r"sales|output|production|trade|exports?|imports?|investment|visitor|arrivals|balance|cost|costs|tax|taxes|debt|"
    r"gdp|supply|demand|spending|earnings|wages?|inflation|coefficients?|multipliers?|linkages|tourists|births|deaths)\b"
)


def _sentence_case(text: str) -> str:
    """Lower-case a Title Case phrase, keeping acronyms and proper nouns."""
    out = []
    for token in re.split(r"(\s+)", text):
        bare = re.sub(r"[^\w'&]", "", token)
        low = bare.lower()
        if not token.strip():
            out.append(token)
        elif low in _KEEP_UPPER or (bare.isupper() and len(bare) >= 2 and bare.isalpha()):
            out.append(token.upper() if low in _KEEP_UPPER else token)
        elif low in _PROPER:
            out.append(token[:1].upper() + token[1:].lower())
        else:
            out.append(token.lower())
    result = "".join(out)
    return re.sub(r"\b(singapore(?:'s)?)\b", lambda m: m.group(1).capitalize(), result)


def plain(text: str) -> str:
    """A title fragment in everyday words (lower case, jargon replaced)."""
    result = _sentence_case(" ".join(str(text or "").split()))
    for pattern, replacement in _GLOSSARY:
        result = re.sub(pattern, replacement, result, flags=re.I)
    result = re.sub(r"\bthe the\b", "the", result)
    result = re.sub(r"\b(non-)?smes?\b", lambda m: f"{m.group(1) or ''}SMEs", result, flags=re.I)
    result = re.sub(r"\s+", " ", result).strip(" ,-")
    # Restore proper nouns the glossary may have lowered or introduced in lower case.
    return re.sub(r"\bsingapore\b", "Singapore", result)


def _join(items: list) -> str:
    """Join items as plain English: "a", "a and b", "a, b and c"."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _the(subject: str) -> str:
    """'the' only where English needs it (the retail sales index, but not average income)."""
    first = subject.split(" ")[0]
    if first in ("the", "singapore's", "average", "median", "total") or first.endswith("'s"):
        return subject
    head = re.split(r"\s+(?:among|of|in|for|at|by|per)\b|\s+\(", subject, maxsplit=1)[0].split(" ")[
        -1
    ]
    return f"the {subject}" if head in _ARTICLE_NOUNS else subject


# --------------------------------------------------------------------------------------------
# 3. Building the questions
# --------------------------------------------------------------------------------------------
@dataclass
class Draft:
    """One candidate rewording of a sample question, with its kind (trend, snapshot, ...)."""

    # trend | snapshot | change | growth | breakdown | count | composition | technical
    kind: str
    text: str
    # the plain subject and breakdown the question is about (used to check polished rewrites)
    content: str = ""


_FREQ_PHRASE = {
    "annual": "over the years",
    "quarterly": "quarter by quarter",
    "monthly": "month by month",
    "half-yearly": "every half year",
    "weekly": "week by week",
    "daily": "day by day",
}
_PREPOSITIONS = re.compile(r"\s+(?:among|of|in|for|at|per|to|that|aged|on|from|with|by)\s+|\s*[(,]")
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def _plural(subject: str) -> bool:
    """Whether the subject takes 'have/are' (imports, households, consumer prices) or 'has/is'."""
    head = _PREPOSITIONS.split(subject, maxsplit=1)[0].split()
    # "tariffs applied to imports" -> tariffs
    while len(head) > 1 and head[-1].endswith("ed"):
        head.pop()
    word = head[-1] if head else ""
    if word in {"statistics", "sales", "earnings", "wages", "manpower"}:
        return word in {"statistics", "sales", "earnings", "wages"}
    return word.endswith("s") and not word.endswith(("ss", "us", "is"))


def _note_text(note: str) -> str:
    """A title's bracketed note as wording for a question (e.g. "as at June")."""
    low = " ".join(note.lower().split())
    if low in _MONTHS:
        return f"as at {low.capitalize()}"
    return plain(note)


def _context(spec: TitleSpec) -> dict:
    """Everything a question needs, as plain words."""
    subject = plain(spec.subject)
    if re.match(r"GDP\b(?! \()", subject):  # explain GDP once, when it is what is being asked about
        subject = re.sub(
            r"^GDP\b", "GDP (the value of everything Singapore produces)", subject, count=1
        )
    if spec.index_base:
        subject += f" ({plain(spec.index_base) if 'base' in spec.index_base else spec.index_base})"
    dims = [d for d in dict.fromkeys(plain(d) for d in spec.dims) if d]
    first = dims[:3]
    gender = next((d for d in dims if "gender" in d), None)
    # gender is the point of many questions: never hide it
    if gender and gender not in first:
        first = dims[:2] + [gender]
    shown = first + (["more"] if len(dims) > 3 else [])
    periods = spec.periods
    when = (
        f"in {periods[0]}"
        if len(periods) == 1
        else f"from {periods[0]} to {periods[-1]}"
        if len(periods) >= 2
        else ""
    )
    ethnic = [f for f in spec.filters if f in _ETHNIC_FILTERS.values()]
    other = [f for f in spec.filters if f not in _ETHNIC_FILTERS.values()]
    only = (
        f", looking only at the {ethnic[0]} group"
        if ethnic
        else f", for {plain(other[0])}"
        if other
        else ""
    )
    tail_bits = []
    if spec.price_basis == "real":
        tail_bits.append("after adjusting for price changes")
    tail_bits += [_note_text(n) for n in spec.notes[:2]]
    tail = f" ({'; '.join(tail_bits)})" if tail_bits else ""
    return {
        "subject": subject,
        "dims": dims,
        "by": f"by {_join(shown)}" if shown else "",
        "gender_only": bool(dims)
        and all(d in ("gender", "gender of the head of the household") for d in dims),
        "has_gender": any("gender" in d for d in dims),
        "when": when,
        "only": only,
        "tail": tail,
        "freq": _FREQ_PHRASE.get(spec.frequency or "", ""),
        "in_sg": "" if "Singapore" in subject else " in Singapore",
    }


def _is_entity(subject: str) -> bool:
    """True when a title's subject is a group of people or units, not a measure."""
    return not _MEASURE_WORDS.search(subject) and bool(
        re.match(
            r"(?:singapore resident|employed (?:\w+ )?singapore|married couples|singapore "
            r"residents|households?|students?|"
            r"people|persons|workers|residents)",
            subject,
            re.I,
        )
    )


def _official_name(spec: TitleSpec) -> str:
    """The table's own name without the cube tag, period or frequency (kept in its published
    wording)."""
    text = re.sub(r"(?i)^\[multi-dimensional data cube\]\s*", "", " ".join(spec.raw.split()))
    text = re.sub(rf"\s*,?\s*{_PERIOD_RUN.pattern}", "", text)
    text = re.sub(r"(?i),?\s*\b(annual|monthly|quarterly|half-yearly|weekly|daily)\b\s*$", "", text)
    return re.sub(r"\s+", " ", text).strip(" ,-").replace('"', "'")


def _squash_spaces(text: str) -> str:
    """Collapse runs of whitespace to single spaces."""
    return " ".join(str(text or "").split())


def _family_drafts(spec: TitleSpec, c: dict):
    """Recurring families whose titles need their own plain wording. None when not one of them."""
    main, detail = _squash_spaces(spec.subject).lower(), _squash_spaces(spec.detail).lower()
    freq, by, only = c["freq"], c["by"], c["only"]
    out: list[Draft] = []

    def add(kind, text) -> None:
        """Add one tidied draft question (capitalised, spacing fixed)."""
        text = re.sub(r"\s+([,?])", r"\1", re.sub(r"\s+", " ", text)).strip()
        out.append(
            Draft(
                kind,
                text[:1].upper() + text[1:],
                " ".join([c["subject"]] + c["dims"] + [plain(spec.detail)]),
            )
        )

    sector = re.match(r"business expectations of (?:the )?(.+?)(?: sector)?$", main)
    if sector:
        who = f"Singapore's {plain(sector.group(1))} sector"
        topic = next(
            (
                t
                for t in ("employment", "operating revenue", "general business outlook")
                if t in detail
            ),
            None,
        )
        looks = {
            "employment": "employment",
            "operating revenue": "their operating revenue",
            "general business outlook": "the general business outlook",
        }
        up, down = (
            ("improve", "worsen") if topic == "general business outlook" else ("rise", "fall")
        )
        horizon = (
            ("over the next " + re.search(r"next (\d+ months)", detail).group(1))
            if re.search(r"next \d+ months", detail)
            else "next quarter"
        )
        if topic and "weighted percentages" in detail:
            add(
                "snapshot",
                f"What percentage of businesses in {who} expect {looks[topic]} to {up}, "
                f"stay the same or {down} {horizon}?",
            )
            add(
                "trend",
                f"How have the expectations of businesses in {who} about {looks[topic]} changed "
                f"quarter by quarter?",
            )
            add(
                "snapshot",
                f"Are more businesses in {who} expecting {looks[topic]} to {up} or to {down} "
                f"{horizon}?",
            )
        elif topic and "net weighted balance" in detail:
            add(
                "snapshot",
                f"On balance, do businesses in {who} expect {looks[topic]} to {up} or {down} "
                f"{horizon}?",
            )
            add(
                "trend",
                f"How has the balance of businesses expecting {looks[topic]} to {up} rather than "
                f"{down} "
                f"in {who} changed quarter by quarter?",
            )
            add(
                "snapshot",
                f"Are businesses in {who} becoming more or less optimistic about {looks[topic]}?",
            )
        else:
            add(
                "trend",
                f"What are the business expectations for "
                f"{who}{', broken down ' + by if by else ''}, "
                f"{freq or 'over time'}?",
            )
            add(
                "snapshot",
                f"What do businesses in {who} expect{', broken down ' + by if by else ''}?",
            )
        return out
    stats = re.match(r"principal statistics of (.+)$", main)
    if stats and detail:
        what = plain(spec.detail)
        who = f"Singapore's {plain(stats.group(1))} sector"
        split = f", {by}" if by else ""
        add("trend", f"How has {what} in {who} changed {freq or 'over time'}{split}{only}?")
        add("snapshot", f"What is the latest figure for {what} in {who}{split}{only}?")
        add("trend", f"Is {what} in {who} going up or down {freq or 'over time'}{only}?")
        return out
    return None


def build_questions(spec: TitleSpec) -> list:
    """Clear, distinct questions for one table, best first. Every one names Singapore, the period
    (if the title gives one) and the breakdown (if the title gives one).

    The title's shape picks one set of wordings (_drafts_*): specialist,
    composition, growth, counts, regular series, a range of years, one year,
    or no period. Duplicates (ignoring case) are removed.
    """
    c = _context(spec)
    subject = c["subject"]
    if not subject:
        return []
    plural = _plural(subject)
    has, do, is_, was = ("have", "do", "are", "were") if plural else ("has", "does", "is", "was")
    when, freq = c["when"], c["freq"]
    words = (
        subject,
        c["by"],
        when,
        c["only"],
        c["tail"],
        freq,
        c["in_sg"],
        plural,
        has,
        do,
        is_,
        was,
        _the(subject),
        f" {when}" if when else "",
    )
    drafts: list[Draft] = []
    content = " ".join([subject] + c["dims"]).strip()

    def add(kind: str, text: str) -> None:
        """Add one tidied draft question (capitalised, spacing fixed)."""
        text = re.sub(r"\s+([,?])", r"\1", re.sub(r"\s+", " ", text)).strip()
        drafts.append(Draft(kind, text[:1].upper() + text[1:], content))

    family = _family_drafts(spec, c)
    if family:
        return family
    if spec.technical:
        _drafts_technical(spec, c, words, add)
    elif spec.flavour in ('distribution', 'share') and (not _is_entity(subject)):
        _drafts_composition(spec, c, words, add)
    elif spec.growth or spec.flavour == 'percent_change':
        _drafts_growth(spec, c, words, add)
    elif _is_entity(subject):
        _drafts_entity(spec, c, words, add)
    elif freq:
        _drafts_frequency(spec, c, words, add)
    elif len(spec.periods) >= 2:
        _drafts_period_range(spec, c, words, add)
    elif len(spec.periods) == 1:
        _drafts_single_period(spec, c, words, add)
    else:
        _drafts_no_period(spec, c, words, add)
    seen, unique = set(), []
    for draft in drafts:
        key = draft.text.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(draft)
    return unique


def _drafts_technical(spec, c, words, add) -> None:
    """Specialist tables (input-output, supply/use): name the table exactly as published."""
    subject, by, when, only, tail, freq, in_sg, plural, has, do, is_, was, the_subject, w = words
    # Specialist tables (input-output, supply/use) have no plainer name, and a half-translated
    # sentence would mislead. Name the table exactly as published, in quotes, and say where and
    # when.
    name = _official_name(spec)
    add("technical", f'What does the official "{name}" data show for Singapore{w}?')
    add("technical", f'Can you explain what "{name}" shows for Singapore{w}?')
    add("technical", f'What do the official figures for "{name}" say about Singapore{w}?')
    add("technical", f'How should I read the "{name}" figures for Singapore{w}?')


def _drafts_composition(spec, c, words, add) -> None:
    """Shares and distributions: what makes up the whole, and how it is divided."""
    subject, by, when, only, tail, freq, in_sg, plural, has, do, is_, was, the_subject, w = words
    add(
        "composition",
        f"What is the make-up of {subject}{in_sg}{w}{', ' + by if by else ''}{only}{tail}"
        f"{', and how has it changed ' + freq if freq else ''}?",
    )
    if by:
        add("composition", f"How is {subject}{in_sg} divided up {by}{w}{only}{tail}?")
    add("composition", f"Which parts make up the biggest share of {subject}{in_sg}{w}{only}{tail}?")


def _drafts_growth(spec, c, words, add) -> None:
    """Growth rates and changes: by how much, rising or falling, the latest change."""
    subject, by, when, only, tail, freq, in_sg, plural, has, do, is_, was, the_subject, w = words
    basis = f", compared with {spec.growth}" if spec.growth else ""
    add(
        "growth",
        f"By how much {has} {the_subject}{in_sg} changed "
        f"{freq or w.strip() or 'over time'}{basis}"
        f"{', ' + by if by else ''}{only}{tail}?",
    )
    add(
        "growth",
        f"{'Are' if plural else 'Is'} {the_subject}{in_sg} rising or falling "
        f"{freq or w.strip() or 'over time'}{only}{tail}?",
    )
    add("growth", f"What is the latest change in {the_subject}{in_sg}{basis}{only}{tail}?")


def _drafts_entity(spec, c, words, add) -> None:
    """Counts of people, households or things: how many, and how they split."""
    subject, by, when, only, tail, freq, in_sg, plural, has, do, is_, was, the_subject, w = words
    split = f", broken down {by}" if by else ""
    if spec.flavour == "share":
        add("composition", f"What proportion of {subject}{in_sg}{w}{split}{only}{tail}?")
        add(
            "composition",
            f"What do the official figures say about the proportion of "
            f"{subject}{in_sg}{w}{only}{tail}?",
        )
    elif spec.flavour == "distribution" and by:
        add("composition", f"How are {subject}{in_sg} spread out {by}{w}{only}{tail}?")
        add(
            "composition",
            f"What does the breakdown of {subject}{in_sg} {by} look like{w}{only}{tail}?",
        )
    else:
        if c["gender_only"]:
            add(
                "count",
                f"How many males and females are there among {subject}{in_sg}{w}{only}{tail}?",
            )
        add("count", f"How many {subject} are there{in_sg}{w}{split}{only}{tail}?")
        if c["has_gender"] and not c["gender_only"]:
            add(
                "count",
                f"How many {subject} are there{in_sg}{w}{only}, and how does this differ "
                f"between males and females{tail}?",
            )
        if by:
            add(
                "breakdown",
                f"What does the breakdown of {subject}{in_sg} {by} look like{w}{only}{tail}?",
            )
            add(
                "breakdown",
                f"Which group has the most {subject}{in_sg}{w}, when we split them "
                f"{by}{only}{tail}?",
            )
        add(
            "count",
            f"What do the official figures say about how many {subject} there "
            f"are{in_sg}{w}{only}{tail}?",
        )


def _drafts_frequency(spec, c, words, add) -> None:
    """Regular series (annual, quarterly, monthly): how it has changed and the latest figures."""
    subject, by, when, only, tail, freq, in_sg, plural, has, do, is_, was, the_subject, w = words
    split = f", {by}" if by else ""
    add("trend", f"How {has} {the_subject}{in_sg} changed {freq}{split}{only}{tail}?")
    add(
        "trend",
        f"{'Are' if plural else 'Is'} {the_subject}{in_sg} going up or down {freq}{only}{tail}?",
    )
    add(
        "snapshot",
        f"What {'are' if plural else 'is'} the latest figures for "
        f"{the_subject}{in_sg}{', ' + by if by else ''}"
        f"{only}{tail}, and how {'do they' if plural else 'does it'} compare with "
        f"earlier "
        f"{spec.frequency.replace('-', ' ')} figures?",
    )
    add(
        "trend",
        f"Can you show me how {the_subject}{in_sg} {'have' if plural else 'has'} moved "
        f"{freq}{only}{tail}?",
    )


def _drafts_period_range(spec, c, words, add) -> None:
    """Titles covering two or more years: change between them and the breakdown."""
    subject, by, when, only, tail, freq, in_sg, plural, has, do, is_, was, the_subject, w = words
    add(
        "change",
        f"How {has} {the_subject}{in_sg} changed {when}{', ' + by if by else ''}{only}{tail}?",
    )
    add(
        "change",
        f"{'Are' if plural else 'Is'} {the_subject}{in_sg} higher or lower now than in "
        f"{spec.periods[0]}{only}{tail}?",
    )
    if by:
        add(
            "breakdown",
            f"How {do} {the_subject}{in_sg} differ {by}{only}{tail}, and has that changed "
            f"{when}?",
        )
    add(
        "snapshot",
        f"What do the official figures say about {the_subject}{in_sg} {when}{only}{tail}?",
    )


def _drafts_single_period(spec, c, words, add) -> None:
    """Titles for one year: the snapshot and how it varies."""
    subject, by, when, only, tail, freq, in_sg, plural, has, do, is_, was, the_subject, w = words
    if c["gender_only"]:
        add(
            "breakdown",
            f"How {do} {the_subject}{in_sg} compare between males and females "
            f"{when}{only}{tail}?",
        )
    if by:
        add(
            "snapshot",
            f"In {spec.periods[0]}, how {'did' } {the_subject}{in_sg} differ {by}{only}{tail}?",
        )
        add("breakdown", f"How {do} {the_subject}{in_sg} vary {by} {when}{only}{tail}?")
    else:
        add("snapshot", f"What {was} {the_subject}{in_sg} {when}{only}{tail}?")
    add(
        "snapshot",
        f"What do the official figures say about {the_subject}{in_sg} {when}{only}{tail}?",
    )


def _drafts_no_period(spec, c, words, add) -> None:
    """No period in the title (census-style or latest table)."""
    subject, by, when, only, tail, freq, in_sg, plural, has, do, is_, was, the_subject, w = words
    if c["gender_only"]:
        add(
            "breakdown",
            f"How {do} {the_subject}{in_sg} compare between males and females{only}{tail}?",
        )
    if by:
        add("breakdown", f"How {do} {the_subject}{in_sg} differ {by}{only}{tail}?")
        add(
            "breakdown",
            f"What does the breakdown of {the_subject}{in_sg} {by} look like{only}{tail}?",
        )
    else:
        add("snapshot", f"What {is_} {the_subject}{in_sg}{only}{tail}?")
    add("snapshot", f"What do the official figures say about {the_subject}{in_sg}{only}{tail}?")
    add(
        "snapshot",
        f"Can you explain what {the_subject}{in_sg} "
        f"{'show' if plural else 'shows'}{only}{tail}?",
    )


# --------------------------------------------------------------------------------------------
# 4. Quality gate
# --------------------------------------------------------------------------------------------
_BANNED = (
    (r"\bsex\b", "says 'sex' instead of 'gender'", False),
    (r"\b(?:decile|deciles|quintile|quintiles)\b", "statistical jargon (decile/quintile)", False),
    (r"\b\d+-digit\b", "classification jargon (n-digit)", False),
    (r"\b(?:ssic|ssoc)\b", "classification jargon (SSIC/SSOC)", False),
    (r"\bn\.?e\.?c\b", "classification jargon (n.e.c.)", False),
    (r"\bchained\b", "statistical jargon (chained dollars)", False),
    (r"multi-dimensional|data cube", "database wording", False),
    (r"\btables?\b", "mentions the table instead of asking about the topic", True),
    (r"\bhousehold reference person\b", "jargon (household reference person)", False),
    (r"\bresidential status\b", "jargon (residential status)", False),
    (r"\bplanning (?:area|region)\b", "jargon (planning area/region)", False),
    (r"\bat (?:purchasers'?|basic) prices\b", "jargon (purchasers'/basic prices)", True),
    (r"[(\[]\s*[)\]]|\(\s*,|,\s*,|\.\.|\s-\s*$|\s{2,}|\(\s|\s\)", "stray punctuation", False),
    (r"\b(\w+)\s+\1\b", "repeated word", False),
)


def problems(question: str, *, technical: bool = False) -> list:
    """Reasons a question is not clear enough to use ([] = fine).

    `technical=True` (specialist input-output tables only) tolerates the table's own technical
    name, because there is no plainer way to refer to it; every other rule still applies.
    """
    text = str(question or "")
    found = []
    if not text.endswith("?"):
        found.append("not a question")
    if not 25 <= len(text) <= 280:
        found.append("too short or too long")
    if text.count("(") != text.count(")"):
        found.append("unbalanced brackets")
    low = text.lower()
    for pattern, reason, technical_ok in _BANNED:
        if technical and technical_ok:
            continue
        if re.search(pattern, low):
            found.append(reason)
    if not technical and re.search(r"(?:\s[A-Z][a-z]+){3,}", text[1:]):
        found.append("left in Title Case")
    if "singapore" not in low:
        found.append("does not say where (Singapore)")
    return list(dict.fromkeys(found))


def draft_questions(title: str) -> tuple:
    """(spec, [Draft ...] that pass the quality gate, [(text, problems) ...] that did not)."""
    spec = parse_title(title)
    good, rejected = [], []
    for draft in build_questions(spec):
        issues = problems(draft.text, technical=spec.technical)
        (rejected.append((draft.text, issues)) if issues else good.append(draft))
    return spec, good, rejected
