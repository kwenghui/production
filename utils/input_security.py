"""First line of defence: clean the user's text and refuse injection attempts.

Runs before any model call that could use tools:

* sanitize_user_text - strip control and invisible characters; REJECT input
  longer than MAX_USER_INPUT_CHARS (never silently truncate it);
* looks_like_prompt_or_code_injection - high-confidence patterns such as
  "ignore previous instructions" or embedded code (INJECTION_PATTERNS), also
  checked on a normalised copy so spacing and look-alike tricks do not help;
* security_block_response - the fixed, safe reply when a request is refused.

Pure functions: no model, network or Streamlit.

Used by: utils/core_secure.py.
Tested in: tests/test_core_modules.py.
"""

import re

from utils.errors import InputRejectedError


MAX_USER_INPUT_CHARS = 4000  # one question
INJECTION_PATTERNS = [
    r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?|prompts?)",
    r"(?i)(reveal|show|print|dump|leak|repeat)\s+(the\s+)?(system|developer)\s+(prompt|message|instructions?)",
    r"(?i)(system|developer)\s*(prompt|message)\s*[:=]",
    r"(?i)(execute|run)\s+(this\s+)?(python|shell|bash|powershell|cmd|command|code)",
    r"(?i)(os\.system|subprocess\.|eval\s*\(|exec\s*\(|__import__\s*\()",
    r"(?i)(tool|function)[-_ ]?call\s*[:=]",
]
# Invisible characters with no legitimate use in a statistics question. They can
# hide text from reviewers (bidi overrides, "Trojan Source") or split a phrase so
# the injection patterns below do not match. ZWJ/ZWNJ (U+200C/U+200D) are kept
# because Tamil and other Indic scripts need them.
_INVISIBLE_CHARS = re.compile(
    "[\u00ad\u180e\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def sanitize_user_text(value, *, max_chars=MAX_USER_INPUT_CHARS):
    """Normalize untrusted text and reject obvious control/injection payloads.

    Raises :class:`InputRejectedError` (a ``ValueError``) whose message is
    written for the user and is therefore safe to display.
    """
    if not isinstance(value, str):
        raise InputRejectedError("Input must be text.")

    value = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    value = _INVISIBLE_CHARS.sub("", value).strip()
    if not value:
        raise InputRejectedError("Question cannot be empty.")
    if len(value) > max_chars:
        raise InputRejectedError(f"Question is too long (maximum {max_chars} characters).")

    # Remove C0/C1 control characters and DEL, except tabs/newlines.
    value = "".join(
        ch for ch in value if ch in "\n\t" or not (ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F)
    )
    return value


def _injection_probe_text(text) -> str:
    """A copy of *text* folded so look-alike tricks cannot dodge the patterns.

    NFKC folds full-width and compatibility forms (e.g. full-width "ｅｘｅｃ")
    to ASCII and every format character (category Cf) is removed. Only this
    probe copy is folded; the user's question itself is not rewritten.
    """
    import unicodedata

    folded = unicodedata.normalize("NFKC", str(text))
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


def looks_like_prompt_or_code_injection(text) -> bool:
    """Return True when text matches a known prompt- or code-injection pattern.

    This is a deny-list and therefore a *detective* control only. The
    preventive controls are the tool allow-list, the bank-source guard and the
    requirement that every number shown is verified against official data.
    """
    probes = {str(text), _injection_probe_text(text)}
    return any(re.search(pattern, probe) for probe in probes for pattern in INJECTION_PATTERNS)


def security_block_response(reason: str = "unsafe_input") -> dict:
    """Build the standard response returned when unsafe input is blocked."""
    return {
        "answer": (
            "I can help with official Singapore statistics, but I can’t process "
            "instructions that try to override the application rules, expose hidden "
            "prompts/secrets, or execute code or system commands. Please rephrase the "
            "question as a statistics request."
        ),
        "rag_results": [],
        "tool_results": [],
        "security_blocked": True,
        "security_reason": reason,
    }
