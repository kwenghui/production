"""Sage's six categories and how every table gets one.

The category is decided from published metadata only (the SingStat theme, then title rules, then
keyword scoring), never from the wording of a question, so rewording a question can never move it
to another topic. Every table ends up with exactly one of OFFICIAL_CATEGORIES.

Used by: utils/question_ai.py, utils/question_generator.py.
Tested in: tests/test_question_tools.py, tests/test_small_modules.py,
tests/test_summaries_and_notes.py.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path


def _canonical_sage_category(raw_theme: str, table_title: str = "") -> str | None:
    """Map official SingStat theme metadata into Sage's six UI categories.

    SingStat metadata can use narrower theme labels than Sage's six-category
    navigation. Exact Sage labels win first; otherwise deterministic aliases
    map the official theme into the corresponding Sage bucket. The published
    table title is used only when the local metadata theme is blank/too broad.
    """
    official = (
        "Economy & Price",
        "Industry",
        "Trade & Investment",
        "Population",
        "Households",
        "Society",
    )

    def norm(value: str) -> str:
        """Lower-case words with punctuation removed and & as "and"."""
        text = " ".join(str(value or "").casefold().replace("&", " and ").split())
        return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())

    theme = norm(raw_theme)
    title = norm(table_title)
    for name in official:
        if theme == norm(name):
            return name

    # Map narrower official SingStat themes into Sage's six navigation buckets.
    theme_rules = (
        (
            "Trade & Investment",
            ("trade", "investment", "balance of payments", "external sector", "exports", "imports"),
        ),
        ("Households", ("household", "households", "household income", "household expenditure")),
        (
            "Economy & Price",
            (
                "economy",
                "economic",
                "national accounts",
                "prices",
                "price",
                "inflation",
                "income",
                "earnings",
                "labour",
                "employment",
                "unemployment",
                "productivity",
                "fiscal",
            ),
        ),
        (
            "Population",
            (
                "population",
                "demography",
                "birth",
                "fertility",
                "death",
                "marriage",
                "divorce",
                "migration",
            ),
        ),
        (
            "Society",
            (
                "society",
                "social",
                "health",
                "education",
                "crime",
                "justice",
                "culture",
                "environment",
                "sports",
            ),
        ),
        (
            "Industry",
            (
                "industry",
                "industries",
                "manufacturing",
                "construction",
                "business",
                "services",
                "tourism",
                "transport",
                "infocomm",
                "energy",
                "property",
                "housing",
            ),
        ),
    )
    for category, aliases in theme_rules:
        if any(alias in theme for alias in aliases):
            return category

    # Final deterministic fallback from the official published table title.
    title_rules = (
        (
            "Trade & Investment",
            (
                "merchandise trade",
                "exports",
                "imports",
                "re exports",
                "foreign direct investment",
                "balance of payments",
            ),
        ),
        (
            "Economy & Price",
            (
                "gross domestic product",
                "gdp",
                "gross national income",
                "consumer price",
                "price index",
                "retail prices",
                "producer price",
                "earnings",
                "labour force",
                "employment",
                "unemployment",
                "fiscal position",
                "value added per worker",
                "monthly income",
            ),
        ),
        (
            "Population",
            (
                "population",
                "live births",
                "births and fertility",
                "death",
                "life expectancy",
                "marriage",
                "divorce",
                "singles among resident",
            ),
        ),
        (
            "Households",
            (
                "resident households",
                "household income",
                "household market income",
                "household employment income",
                "household size",
                "household living arrangement",
                "household expenditure",
            ),
        ),
        (
            "Society",
            (
                "health",
                "hospital",
                "disease",
                "immunisation",
                "smoking",
                "school",
                "students",
                "teachers",
                "air quality",
                "pollution",
                "water quality",
            ),
        ),
        (
            "Industry",
            (
                "manufacturing",
                "industrial",
                "retail sales",
                "food and beverage",
                "tourism",
                "motor vehicle",
                "public transport",
                "aircraft",
                "hdb",
                "housing and development board",
                "residential units",
                "electricity",
                "energy",
            ),
        ),
    )
    for category, aliases in title_rules:
        if any(alias in title for alias in aliases):
            return category
    return None


OFFICIAL_CATEGORIES = (
    "Economy & Price",
    "Industry",
    "Trade & Investment",
    "Population",
    "Households",
    "Society",
)

# Keyword scoring used ONLY when the SingStat theme and the exact title rules
# found nothing. Ties go to the earlier category in this order.
_KEYWORD_SETS = (
    (
        "Trade & Investment",
        (
            "trade",
            "export",
            "import",
            "direct investment",
            "foreign affiliate",
            "balance of payments",
            "international investment",
            "portfolio investment",
            "merchandise",
            "foreign reserves",
            "overseas",
            "abroad",
        ),
    ),
    ("Households", ("household", "dwelling", "living arrangement")),
    (
        "Population",
        (
            "population",
            "birth",
            "fertility",
            "death",
            "mortality",
            "life expectancy",
            "marriage",
            "married",
            "divorce",
            "children born",
            "singles",
            "migration",
            "citizen",
            "ageing",
            "dependency ratio",
        ),
    ),
    (
        "Society",
        (
            "health",
            "hospital",
            "disease",
            "medical",
            "school",
            "student",
            "education",
            "teacher",
            "graduate",
            "crime",
            "offender",
            "central provident fund",
            "cpf",
            "pension",
            "social",
            "volunteer",
            "culture",
            "arts",
            "sport",
            "environment",
            "air quality",
            "pollution",
            "recycling",
            "waste",
            "water",
            "mode of transport",
            "commut",
            "language",
            "religion",
            "literacy",
            "disabilit",
            "welfare",
            "elderly",
        ),
    ),
    (
        "Industry",
        (
            "manufactur",
            "industrial",
            "construction",
            "contracts awarded",
            "progress payments",
            "business entit",
            "formation of",
            "cessation",
            "enterprise",
            "listed issuers",
            "sustainability report",
            "research and development",
            "utilities",
            "electricity",
            "energy",
            "tourism",
            "visitor",
            "hotel",
            "retail sales",
            "food and beverage",
            "services sector",
            "business expectations",
            "rental",
            "property",
            "residential",
            "office",
            "real estate",
            "transport",
            "aircraft",
            "airport",
            "shipping",
            "port ",
            "motor vehicle",
            "hdb",
            "housing",
            "infocomm",
            "telecommunication",
            "wholesale",
            "services",
            "sector",
            "industry",
            "industries",
            "business",
            "company",
            "companies",
            "establishment",
            "firms",
        ),
    ),
    (
        "Economy & Price",
        (
            "gross domestic product",
            "gdp",
            "national income",
            "national accounts",
            "input output",
            "multiplier",
            "requirement coefficient",
            "linkage",
            "use table",
            "supply table",
            "domestic output",
            "final demand",
            "value added",
            "capital formation",
            "consumer price",
            "price index",
            "prices",
            "inflation",
            "producer price",
            "earnings",
            "wage",
            "labour",
            "employment",
            "employed",
            "unemployment",
            "productivity",
            "fiscal",
            "government debt",
            "government expenditure",
            "government operating",
            "government revenue",
            "budget",
            "money supply",
            "interest rate",
            "exchange rate",
            "savings",
            "income account",
            "expenditure",
            "income",
            "economy",
            "economic",
            "cost",
            "price",
            "dollar",
        ),
    ),
)

# Whole families of tables whose wording would otherwise be pulled to the wrong
# bucket by generic words such as "industry" or "sector". Checked before scoring.
_PRIORITY_RULES = (
    (
        r"\b(input output|requirement coefficients?|use table|supply "
        r"table|linkages|multipliers?|final demand|primary inputs?)\b",
        "Economy & Price",
    ),
    (r"\b(trade unions?|trade disputes?)\b", "Economy & Price"),
    (
        r"\b(trade in services|global trade|foreign direct|direct (equity )?investment|equity "
        r"investment|foreign affiliates|international investment|portfolio investment)\b",
        "Trade & Investment",
    ),
    (
        r"\b(wsq|trainees?|skills qualifications|discrimination|harassment|material "
        r"consumption|material footprint)\b",
        "Society",
    ),
    (
        r"\b(retrench\w*|recruitment|resignation|vacanc\w+|paid "
        r"hours|workplace|employers?|employees?|workforce|civil service|managers and administrators)\b",
        "Economy & Price",
    ),
    (
        r"\b(taxable|taxes|tax|debt|government finance|duty paid|development fund|compensation of "
        r"employees)\b",
        "Economy & Price",
    ),
    (r"\b(patents?|condominiums?|jtc|ready built|cruise|passenger)\b", "Industry"),
    (r"\bconsumer durables\b", "Households"),
)


def _normalise_words(value: str) -> str:
    """Lower-case words with punctuation removed and & as "and"."""
    text = " ".join(str(value or "").casefold().replace("&", " and ").split())
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text).split())


def best_category(raw_theme: str, table_title: str = "") -> tuple[str, str]:
    """Return (category, source) for EVERY table: never None, never "Uncategorised".

    source is one of: singstat_theme (official theme/title rules matched),
    keyword_score (best keyword match in the title) or default (nothing matched;
    the broad "Society" bucket is used so the question is still reachable).
    """
    exact = _canonical_sage_category(raw_theme, table_title)
    if exact:
        return exact, "singstat_theme"
    title = " " + _normalise_words(table_title) + " "
    for pattern, category in _PRIORITY_RULES:
        if re.search(pattern, title):
            return category, "keyword_score"
    best, best_score = None, 0
    for category, words in _KEYWORD_SETS:
        score = sum(len(word) for word in words if word in title)
        if score > best_score:
            best, best_score = category, score
    if best:
        return best, "keyword_score"
    return "Society", "default"


def _official_themes(metadata_db_paths, table_titles=None):
    """Read SingStat themes and normalise them into Sage's six categories."""
    themes = {}
    table_titles = {str(k).strip().upper(): str(v) for k, v in (table_titles or {}).items()}
    for candidate in metadata_db_paths or ():
        path = Path(candidate)
        if not path.is_file():
            continue
        conn = None
        try:
            conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
            fields = {row[1] for row in conn.execute("PRAGMA table_info(kb_tables)")}
            if {"table_id", "theme"} <= fields:
                for table_id, theme in conn.execute("SELECT table_id, theme FROM kb_tables"):
                    key = str(table_id).strip().upper()
                    category = _canonical_sage_category(theme, table_titles.get(key, ""))
                    if category and key not in themes:
                        themes[key] = category
        except (sqlite3.Error, OSError):
            continue
        finally:
            if conn is not None:
                conn.close()
    return themes


def _raw_themes(metadata_db_paths) -> dict[str, str]:
    """Official SingStat theme text per table (may be blank); never discards a table."""
    themes = {}
    for candidate in metadata_db_paths or ():
        path = Path(candidate)
        if not path.is_file():
            continue
        conn = None
        try:
            conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
            fields = {row[1] for row in conn.execute("PRAGMA table_info(kb_tables)")}
            if {"table_id", "theme"} <= fields:
                for table_id, theme in conn.execute("SELECT table_id, theme FROM kb_tables"):
                    themes.setdefault(str(table_id).strip().upper(), str(theme or ""))
        except (sqlite3.Error, OSError):
            continue
        finally:
            if conn is not None:
                conn.close()
    return themes
