"""Generic word handling for table discovery (no subject-specific rules).

Used by utils/hybrid_discovery.py to find and rank candidate tables:

* subject_terms   - the words of a question or title that carry its subject:
                    filler words dropped, singular and plural treated alike
                    ("withdrawals" = "withdrawal", "industries" = "industry").
* acronym_in_title - whether an acronym the QUESTION uses is spelt out by the
                    initials of consecutive title words ("cpf" in "Central
                    Provident Fund", "ict" in "Information And Communications
                    Technology"). Nothing is invented: the letters come from the title.
* SearchVocabulary.correct
                  - fixes a misspelt question word using only words that occur
                    in the official table and series names of the local index
                    ("teackers" -> "teachers", "grwoth" -> "growth"). Short
                    words, known words, other forms of a word and ambiguous
                    cases are left alone; every correction is in the trace.
* currency_names  - names for ISO currency codes in a question ("myr" ->
                    "malaysian ringgit"), so search finds the official wording.

None of this reads or changes statistics; it only affects which tables are
offered as candidates. The data checks downstream are unchanged.

Used by: utils/answer_verification.py, utils/hybrid_discovery.py.
Tested in: tests/test_discovery_ranking.py.
"""

import re
import sqlite3
import threading
from pathlib import Path

from utils.vocabulary import CURRENCY_CODES

#: Words that say how a question is asked, not what it is about.
FILLER_WORDS = frozenset(
    {
        'in',
        'the',
        'a',
        'an',
        'of',
        'on',
        'for',
        'by',
        'is',
        'are',
        'was',
        'were',
        'has',
        'have',
        'how',
        'did',
        'does',
        'do',
        'from',
        'and',
        'or',
        'at',
        'to',
        'over',
        'time',
        'what',
        'which',
        'change',
        'changed',
        'changes',
        'singapore',
        'annual',
        'monthly',
        'quarterly',
        'years',
        'year',
        'yrs',
        'number',
        'tell',
        'about',
        'info',
        'information',
        'please',
        'some',
        'show',
        'give',
        'me',
        'my',
        'past',
        'last',
        'few',
        'recent',
        'recently',
        'latest',
        'with',
        'many',
        'much',
        'there',
        'their',
        'this',
        'that',
        'these',
        'those',
        'between',
        'since',
        'during',
        # How people phrase a request in chat ("can i ask abt ..."): never the subject.
        'can',
        'could',
        'would',
        'should',
        'ask',
        'abt',
        'want',
        'know',
        'like',
        'need',
        'pls',
        'plz',
        'find',
        'get',
        'see',
        'let',
        'regarding',
        'kindly',
        'you',
        'your',
        # Asking to compare is not a subject either ("compare sgd and myr").
        'compare',
        'compared',
        'comparing',
        'comparison',
        'versus',
        'against',
    }
)
#: Small words inside a capitalised name that do not give a letter to its acronym.
_CONNECTORS = frozenset({'and', 'of', 'the', 'for', 'in', 'on', 'by', '&', 'to', 'at'})
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def singular(word: str) -> str:
    """A simple singular form used only for matching ("withdrawals" -> "withdrawal")."""
    if len(word) > 4 and word.endswith('ies'):
        return word[:-3] + 'y'
    if len(word) > 4 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
        return word[:-1]
    return word


def subject_terms(text: str) -> set[str]:
    """Subject words of ``text``: lower-case, 3+ letters, filler removed, singular form."""
    return {
        singular(word)
        for word in (w.casefold() for w in _WORD.findall(str(text or '')))
        if len(word) >= 3 and word not in FILLER_WORDS
    }


def acronym_in_title(term: str, title: str) -> bool:
    """True when ``term`` (2-6 letters) is spelt by the initials of consecutive title words.

    Connector words ("And", "Of", ...) inside the run give no letter, so "ict"
    matches "Information And Communications Technology" and "cpf" matches
    "Central Provident Fund". Only acronyms the QUESTION uses are checked, so
    nothing is invented.
    """
    term = str(term or '').casefold()
    if not 2 <= len(term) <= 6 or not term.isalpha():
        return False
    words = [w.casefold() for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", str(title or ''))]
    for start, word in enumerate(words):
        if word[0] != term[0] or word in _CONNECTORS:
            continue
        letters, position = [], start
        while position < len(words) and len(letters) < len(term):
            if words[position] not in _CONNECTORS:
                letters.append(words[position][0])
            position += 1
        if ''.join(letters) == term:
            return True
    return False


def title_terms(title: str, question_terms) -> set[str]:
    """Subject words of a title, plus the question's acronyms and currency codes that the
    title names ("cpf" for "Central Provident Fund", "myr" for "Malaysian Ringgit")."""
    terms = subject_terms(title)
    lowered = str(title or '').casefold()
    for term in question_terms:
        if term in terms:
            continue
        if term in CURRENCY_CODES and CURRENCY_CODES[term] in lowered:
            terms.add(term)
        elif len(term) <= 6 and acronym_in_title(term, title):
            terms.add(term)
    return terms


def currency_names(text: str) -> list[str]:
    """Currency names for the ISO codes in ``text`` ("sgd" -> "singapore dollar"), in order."""
    return [
        CURRENCY_CODES[w.casefold()]
        for w in _WORD.findall(str(text or ''))
        if w.casefold() in CURRENCY_CODES
    ]


class SearchVocabulary:
    """Words that occur in the official table and series names of one local index."""

    def __init__(self, words) -> None:
        """``words``: an iterable of lower-case words (4+ letters are kept)."""
        self.words = frozenset(w for w in words if len(w) >= 4)
        self._by_start = {}
        for word in self.words:
            self._by_start.setdefault(word[0], []).append(word)

    def correct(self, text: str) -> tuple[str, dict]:
        """(text with misspelt words replaced, {misspelt: corrected}).

        Only words of 5+ letters that are not in the vocabulary (in singular or
        plural form), not filler and not a currency code are considered. A
        correction must start with the same letter and be ONE typing slip away
        (one letter wrong, missing, extra, or two neighbours swapped as in
        "grwoth"; a cut-off word such as "impor" counts too); two slips are
        allowed for words of 9+ letters. A word is never "corrected" into another
        form of itself ("compare" is not changed to "compared"), and when two
        words are equally close nothing is guessed.
        """
        corrections = {}
        for word in set(w.casefold() for w in _WORD.findall(str(text or ''))):
            if (
                len(word) < 5
                or word in FILLER_WORDS
                or word in self.words
                or word.isdigit()
                or singular(word) in self.words
                or word in CURRENCY_CODES
            ):
                continue
            allowed = 2 if len(word) >= 9 else 1
            best, best_distance, tie = None, allowed + 1, False
            for candidate in self._by_start.get(word[0], ()):
                if abs(len(candidate) - len(word)) > allowed:
                    continue
                if _is_other_form(word, candidate):
                    continue  # another form of the same word, not a typo
                distance = _typing_distance(word, candidate, allowed)
                if distance < best_distance:
                    best, best_distance, tie = candidate, distance, False
                elif distance == best_distance:
                    tie = True
            if best is not None and not tie:
                corrections[word] = best
        if not corrections:
            return text, {}
        fixed = re.sub(
            r"[A-Za-z][A-Za-z0-9]*",
            lambda m: corrections.get(m.group(0).casefold(), m.group(0)),
            str(text),
        )
        return fixed, corrections


#: Endings that turn a word into another form of itself ("compare" -> "compared").
_INFLECTIONS = ('s', 'es', 'd', 'ed', 'ing', 'er', 'ers', 'ly', 'al', 'ion', 'ions')


def _is_other_form(word: str, candidate: str) -> bool:
    """True when one word is the other plus an ordinary ending ("compare"/"compared").

    A cut-off word is still a typo: "impor" is "import" with a letter missing,
    not another form of "impor", so it may be corrected.
    """
    short, long_ = sorted((word, candidate), key=len)
    return long_.startswith(short) and long_[len(short) :] in _INFLECTIONS


def _typing_distance(a: str, b: str, limit: int) -> int:
    """Edits between two words (insert, delete, replace, swap two neighbours); limit+1 if more."""
    previous2, previous = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if (
                i > 1
                and j > 1
                and a[i - 1] == b[j - 2]
                and a[i - 2] == b[j - 1]
                and previous2 is not None
            ):
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


_VOCABULARIES: dict = {}
_VOCABULARY_LOCK = threading.Lock()


def vocabulary_for(db_path) -> SearchVocabulary | None:
    """The vocabulary of a knowledge-base file, built once per file version; None if unreadable."""
    path = Path(db_path)
    try:
        key = (str(path.resolve()), path.stat().st_mtime_ns)
    except OSError:
        return None
    with _VOCABULARY_LOCK:
        if key in _VOCABULARIES:
            return _VOCABULARIES[key]
    words = set()
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        for (title,) in connection.execute('SELECT title FROM kb_tables'):
            words.update(w.casefold() for w in _WORD.findall(str(title or '')))
        for (name,) in connection.execute('SELECT DISTINCT title FROM kb_series'):
            words.update(w.casefold() for w in _WORD.findall(str(name or '')))
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()
    vocabulary = SearchVocabulary(words)
    with _VOCABULARY_LOCK:
        _VOCABULARIES.clear()  # keep only the current file version
        _VOCABULARIES[key] = vocabulary
    return vocabulary
